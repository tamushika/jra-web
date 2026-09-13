"""SPEC-T78 (週末自動起動時に稼働中サーバーへ解析開始を委譲する) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T78-auto-start-delegate.md §3
  1. api.port_guard.is_port_in_use: 空きポートでFalse、LISTEN中のポートでTrue。
  2. jra_suite.delegate_auto_start_to_running_server:
     urllib.request.urlopen をmonkeypatchして200/409/接続エラーの3系統を検証。
  3. 実サーバー経路: werkzeug.serving.make_server で空きポートに立てたjra_suiteの
     Flask appへ実際にdelegateし、STATE["status"]がanalyzingになることを確認。
  4. jra_suite.py の __main__ ブロックで is_port_in_use の判定が ensure_port_free
     より前にあることをソース上の位置で確認。

安全事項: 本番のポート5005には一切接続しない。空きポートは必ず
`socket.socket()` を ("127.0.0.1", 0) にbindしてOSに割り当てさせる。
"""
import os
import socket
import threading
import urllib.error
import urllib.request

from werkzeug.serving import make_server

import jra_ev
import jra_suite
from api.port_guard import is_port_in_use

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ─── 1. is_port_in_use ───────────────────────────────────────────────────────

def test_is_port_in_use_false_for_free_port():
    port = _free_port()
    assert is_port_in_use(port) is False


def test_is_port_in_use_true_for_listening_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert is_port_in_use(port) is True


# ─── 2. delegate_auto_start_to_running_server (urlopenをmonkeypatch) ────────

class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_delegate_success_sends_expected_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, msg = jra_suite.delegate_auto_start_to_running_server(12345)

    assert ok is True
    assert "解析開始を依頼しました" in msg
    req = captured["req"]
    full_url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    assert full_url.endswith("/ev/api/analyze_start")
    assert req.get_method() == "POST"
    assert req.data == b"{}"


def test_delegate_conflict_409_is_treated_as_success(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, msg = jra_suite.delegate_auto_start_to_running_server(12345)

    assert ok is True
    assert msg == "既に解析実行中です"


def test_delegate_connection_error_returns_false(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, msg = jra_suite.delegate_auto_start_to_running_server(12345)

    assert ok is False


# ─── 3. 実サーバー経路 ────────────────────────────────────────────────────────

def test_delegate_to_real_running_server(monkeypatch):
    monkeypatch.setattr(jra_ev, "worker_analyze_all", lambda params: None)
    monkeypatch.setitem(jra_ev.STATE, "status", "idle")

    app = jra_suite.create_app()
    # ポート0を指定してOSに空きポートを割り当てさせる。5005には一切接続しない。
    server = make_server("127.0.0.1", 0, app)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        ok, msg = jra_suite.delegate_auto_start_to_running_server(server.server_port)
        assert ok is True
        assert jra_ev.STATE["status"] == "analyzing"
    finally:
        server.shutdown()
        th.join(timeout=5)


# ─── 4. __main__ ブロックの判定順序 ──────────────────────────────────────────

def test_auto_start_port_check_precedes_ensure_port_free_in_source():
    src_path = os.path.join(REPO_DIR, "jra_suite.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read().replace("\r\n", "\n")

    main_idx = src.index('if __name__ == "__main__":')
    main_block = src[main_idx:]

    is_port_in_use_idx = main_block.index("is_port_in_use(PORT)")
    ensure_port_free_idx = main_block.index("ensure_port_free(PORT")

    assert is_port_in_use_idx < ensure_port_free_idx
