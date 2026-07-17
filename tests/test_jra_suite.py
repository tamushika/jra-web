"""SPEC-T38 (ローカル3アプリの単一ポート統合) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T38-app-consolidation.md §4
  1. create_app() で3 Blueprintの主要ページが200を返す
  2. 静的資産がprefix配下で解決される + パストラバーサル/.env/*.db/backups/拒否
  3. バックグラウンドループがプロセス内で1回だけ起動する (2回目はno-op)
  4. 旧3ファイルの直接起動が案内を表示して非0終了する
  5. 通知判定関数のシグネチャ・呼び出し経路が不変であることの軽い smoke テスト
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import jra_ev
import jra_perf
import jra_suite
import jra_win5

ROOT = Path(__file__).resolve().parents[1]


# ─── §4-1: 3 Blueprintの主要ページが200 ────────────────────────────────────

@pytest.fixture()
def suite_client():
    app = jra_suite.create_app()
    return app.test_client()


def test_portal_page_returns_200(suite_client):
    resp = suite_client.get("/")
    assert resp.status_code == 200
    assert "JRA".encode() in resp.data or "予想".encode("utf-8") in resp.data


@pytest.mark.parametrize("prefix", ["ev", "win5", "perf"])
def test_blueprint_index_pages_return_200(suite_client, prefix):
    resp = suite_client.get(f"/{prefix}/")
    assert resp.status_code == 200


@pytest.mark.parametrize("prefix,path", [
    ("ev", "/ev/api/state"),
    ("win5", "/win5/api/win5_watch"),
])
def test_blueprint_api_routes_resolve_under_prefix(suite_client, prefix, path):
    # フロントのfetch()が相対パスに変わったことで、APIもprefix配下で解決できる必要がある
    resp = suite_client.get(path)
    assert resp.status_code == 200


# ─── §4-2: 静的資産のprefix配下解決 + 配信拒否 ─────────────────────────────

@pytest.mark.parametrize("prefix", ["ev", "win5", "perf"])
def test_static_asset_resolves_under_prefix(suite_client, prefix):
    # 各HTMLは <link rel="stylesheet" href="style.css"> のように相対参照しており、
    # ページが /{prefix}/ で配信される以上、style.css は /{prefix}/style.css で
    # 解決できなければならない。
    resp = suite_client.get(f"/{prefix}/style.css")
    assert resp.status_code == 200


@pytest.mark.parametrize("denied_path", [
    "/ev/.env",
    "/win5/.env",
    "/perf/.env",
    "/ev/ability.db",
    "/ev/backups/whatever.txt",
    "/win5/backups/x",
    # レビュー追加分: git内部情報・SQLite実体・運用ログも拒否
    "/ev/.git/config",
    "/ev/database.sqlite",
    "/perf/data/jra_logging.db",
    "/ev/ev_monitor.log",
    "/ev/suite_monitor.log",
])
def test_denied_static_paths_return_404(suite_client, denied_path):
    resp = suite_client.get(denied_path)
    assert resp.status_code == 404


@pytest.mark.parametrize("traversal_path", [
    "/ev/../.env",
    "/ev/%2e%2e/.env",
    "/ev/..%2f.env",
    "/win5/../../.env",
])
def test_path_traversal_is_rejected(suite_client, traversal_path):
    resp = suite_client.get(traversal_path)
    assert resp.status_code == 404


def test_root_level_legacy_html_url_not_served(suite_client):
    # SPEC 3.2: ルート直下 (/xxx.html) の旧URLは提供しない
    resp = suite_client.get("/index_ev.html")
    assert resp.status_code == 404


# ─── §4-3: バックグラウンドループの1回起動保証 ──────────────────────────────

def test_start_background_loops_starts_exactly_once(monkeypatch):
    monkeypatch.setattr(jra_suite, "_LOOPS_STARTED", threading.Event())
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])
    monkeypatch.setattr(jra_win5, "_WATCH_THREAD", [False])

    calls = {"scheduler": 0, "watch": 0, "restore": 0}

    def fake_scheduler_loop():
        calls["scheduler"] += 1

    def fake_watch_loop():
        calls["watch"] += 1

    def fake_restore():
        calls["restore"] += 1

    monkeypatch.setattr(jra_ev, "scheduler_loop", fake_scheduler_loop)
    monkeypatch.setattr(jra_win5, "_watch_loop", fake_watch_loop)
    monkeypatch.setattr(jra_ev, "_restore_phase2_state", fake_restore)

    assert jra_suite.start_background_loops() is True
    assert jra_suite.start_background_loops() is False  # 2回目はno-op

    # 起動はdaemonスレッドで非同期なので、実行を少し待つ
    for _ in range(50):
        if calls["scheduler"] and calls["watch"] and calls["restore"]:
            break
        time.sleep(0.05)

    assert calls["scheduler"] == 1
    assert calls["watch"] == 1
    assert calls["restore"] == 1
    assert jra_ev._SCHEDULER_STARTED[0] is True
    assert jra_win5._WATCH_THREAD[0] is True


def test_start_background_loops_noop_when_module_guards_already_set(monkeypatch):
    # 統合エントリ自身のガードとは別に、各モジュール既存の再入ガードが既に
    # 立っている場合はスレッドを追加起動しない (二重起動防止の多層防御)。
    monkeypatch.setattr(jra_suite, "_LOOPS_STARTED", threading.Event())
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [True])
    monkeypatch.setattr(jra_win5, "_WATCH_THREAD", [True])

    calls = {"scheduler": 0, "watch": 0}
    monkeypatch.setattr(jra_ev, "scheduler_loop", lambda: calls.__setitem__("scheduler", calls["scheduler"] + 1))
    monkeypatch.setattr(jra_win5, "_watch_loop", lambda: calls.__setitem__("watch", calls["watch"] + 1))
    monkeypatch.setattr(jra_ev, "_restore_phase2_state", lambda: None)

    assert jra_suite.start_background_loops() is True
    time.sleep(0.1)
    assert calls["scheduler"] == 0
    assert calls["watch"] == 0


# ─── §4-4: 旧3ファイルの直接起動が案内を表示して非0終了する ────────────────

@pytest.mark.parametrize("legacy_file", ["jra_ev.py", "jra_win5.py", "jra_perf.py"])
def test_legacy_entrypoint_direct_run_exits_nonzero_with_guidance(legacy_file):
    proc = subprocess.run(
        [sys.executable, legacy_file],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "jra_suite.py" in combined
    assert "5005" in combined


# ─── §4-5: 通知判定関数のシグネチャ・呼び出し経路が不変 (smoke) ────────────

def test_notification_senders_still_suppressed_without_env(monkeypatch):
    monkeypatch.delenv("EV_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("EV_LINE_CHANNEL_TOKEN", raising=False)
    alert = {"stage": 5, "label": "テスト", "picks": [
        {"num": 1, "name": "テスト馬", "ev": 2.0, "win_prob": 0.5, "odds": "4.0",
         "web_value": False}]}
    assert jra_ev._send_discord(alert) == ("suppressed", None, None)
    assert jra_ev._send_line(alert) == ("suppressed", None, None)


def test_ev_blueprint_registered_under_ev_name():
    assert jra_ev.bp.name == "ev"
    assert jra_win5.bp.name == "win5"
    assert jra_perf.bp.name == "perf"
