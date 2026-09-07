"""SPEC-T74 (タブシェル左上の統合「解析開始」ボタン) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T74-global-start-button.md §3
  - ポータル `/` のHTMLに id="globalStart" / id="globalStatus" が含まれ、
    data-tab="ev" のタブより前に出現する (タブバー左端に配置されている)。
  - `POST /ev/api/analyze_start` にbody `{}` を送っても200で、
    STATE["params"]["ev_threshold"] が変わらない (worker_analyze_all はmonkeypatch)。
  - index_ev.html に埋め込み時 (iframe内) の startBtn 非表示ロジックが含まれる。
  - index_win5.html に autofetch と jra-win5-fetch メッセージ処理が含まれる。
"""
import os

import pytest

import jra_ev
import jra_suite


@pytest.fixture()
def suite_client():
    app = jra_suite.create_app()
    return app.test_client()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)


def _read(relpath):
    path = os.path.join(REPO_DIR, relpath)
    with open(path, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


# ─── ポータルHTML ────────────────────────────────────────────────────────────

def test_portal_has_global_start_button_and_status(suite_client):
    resp = suite_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'id="globalStart"' in html
    assert 'id="globalStatus"' in html


def test_portal_global_start_appears_before_ev_tab(suite_client):
    resp = suite_client.get("/")
    html = resp.data.decode("utf-8")
    pos_start = html.index('id="globalStart"')
    pos_ev_tab = html.index('data-tab="ev"')
    assert pos_start < pos_ev_tab


# ─── /ev/api/analyze_start (body {}) ────────────────────────────────────────

def test_analyze_start_with_empty_body_keeps_threshold(suite_client, monkeypatch):
    monkeypatch.setattr(jra_ev, "worker_analyze_all", lambda params: None)
    monkeypatch.setitem(jra_ev.STATE, "status", "idle")
    original_threshold = jra_ev.STATE["params"]["ev_threshold"]

    resp = suite_client.post(
        "/ev/api/analyze_start",
        data="{}",
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert jra_ev.STATE["params"]["ev_threshold"] == original_threshold


# ─── index_ev.html: iframe埋め込み時にstartBtnを非表示 ──────────────────────

def test_index_ev_hides_start_button_when_embedded_in_iframe():
    html = _read("index_ev.html")
    assert "window.parent !== window" in html
    assert "'startBtn'" in html
    assert "display = 'none'" in html


# ─── index_win5.html: autofetch と jra-win5-fetch 受信 ──────────────────────

def test_index_win5_supports_autofetch_query_param():
    html = _read("index_win5.html")
    assert "autofetch" in html
    assert "loadRaces()" in html


def test_index_win5_listens_for_fetch_message():
    html = _read("index_win5.html")
    assert "jra-win5-fetch" in html
    assert "addEventListener('message'" in html


def test_index_win5_notifies_parent_on_fetch_completion():
    html = _read("index_win5.html")
    assert "jra-win5-fetched" in html
    assert "window.parent.postMessage" in html
