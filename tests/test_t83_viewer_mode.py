"""SPEC-T83 (閲覧モード: 監視ループ・通知を抑止して起動する) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T83-viewer-mode.md §3
  1. viewer_mode() の真偽判定
  2. 閲覧モード: start_background_loops() がEV/WIN5の監視スレッドを起動しない
     (T82使用コース・T79c週末重賞の起動時キャッシュ先読みは従来どおり実行する。
      2026-09-26レビュー是正: 通知・prospective契約と無関係なローカルキャッシュ
      更新のため閲覧モードでも止めない)
  3. 閲覧モード: _ensure_scheduler() が即returnし _SCHEDULER_STARTED を立てない
  4. 閲覧モード: scheduler_loop() が即returnする
  5. 閲覧モード: _restore_phase2_state() が通知を再送しない (pendingのまま残す)
  6. 閲覧モード: jra_win5._watch_loop() が即returnする
  7. 通常モード: 上記2〜6がすべて従来どおり動く
  8. /ev/api/state に viewer_mode が入り、値がモードと一致する
  9. ポータルHTML/index_ev.htmlの帯表示
  10. start_suite_viewer.bat の内容検査
  11-12. 凍結ハッシュテスト・全体回帰は本ファイル外 (README/報告側で実行)

このファイルはネットワーク・DBに一切アクセスしない。JRA_VIEWER_MODEは
monkeypatchのsetenv/delenvで管理し、テスト終了後は自動的に元に戻る。
"""
import threading
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

import jra_ev
import jra_suite
import jra_win5
from api import run_mode

ROOT = Path(__file__).resolve().parents[1]


# ─── §3-1: viewer_mode() の真偽判定 ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("True", True),
    ("on", True), ("ON", True), ("yes", True), ("YES", True),
    ("  1  ", True), ("  true  ", True), ("\ton\t", True),
    ("0", False), ("false", False), ("FALSE", False),
    ("", False), ("other", False), ("2", False), (" ", False),
])
def test_viewer_mode_truthiness(monkeypatch, raw, expected):
    monkeypatch.setenv("JRA_VIEWER_MODE", raw)
    assert run_mode.viewer_mode() is expected


def test_viewer_mode_defaults_false_when_unset(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    assert run_mode.viewer_mode() is False


def test_viewer_mode_reason_is_shared_text():
    reason = run_mode.viewer_mode_reason()
    assert "閲覧モード" in reason
    assert "監視ループ" in reason
    assert "通知" in reason


# ─── §3-2: start_background_loops() はEV/WIN5の監視スレッドだけを起動しない ─
# (2026-09-26 レビュー是正: T82使用コース・T79c週末重賞の起動時キャッシュ先読みは
#  通知・prospective契約と無関係なローカルキャッシュ更新のため、閲覧モードでも
#  従来どおり実行する。止めるのはEV scheduler_loop / WIN5 _watch_loop の2スレッドのみ)

def test_viewer_mode_start_background_loops_starts_no_ev_win5_thread(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    monkeypatch.setattr(jra_suite, "_LOOPS_STARTED", threading.Event())
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])
    monkeypatch.setattr(jra_win5, "_WATCH_THREAD", [False])

    thread_calls = []

    class _NoThread:
        def __init__(self, *args, **kwargs):
            thread_calls.append((args, kwargs))

        def start(self):
            raise AssertionError("threading.Thread.start() must not be called in viewer mode")

    monkeypatch.setattr(jra_suite.threading, "Thread", _NoThread)
    monkeypatch.setattr(jra_ev, "_restore_phase2_state", lambda: None)
    monkeypatch.setattr(jra_suite, "_t82_startup_course_usage_refresh", lambda: None)
    monkeypatch.setattr(jra_suite, "_t79c_startup_weekend_graded_refresh", lambda: None)

    # 起動時キャッシュ先読みは実行されるので、戻り値は「1回目の起動処理を行った」True。
    assert jra_suite.start_background_loops() is True
    assert thread_calls == []
    assert jra_ev._SCHEDULER_STARTED[0] is False
    assert jra_win5._WATCH_THREAD[0] is False
    assert jra_suite._LOOPS_STARTED.is_set() is True


def test_viewer_mode_start_background_loops_still_runs_t82_t79c_startup_hooks(monkeypatch):
    # SPEC-T83 §1.2: JRAを読んでローカルキャッシュ表に書くだけの起動時フックは
    # 閲覧モードでも実行する (実際のネットワーク/JRAアクセスはmonkeypatchで防ぐ)。
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    monkeypatch.setattr(jra_suite, "_LOOPS_STARTED", threading.Event())
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])
    monkeypatch.setattr(jra_win5, "_WATCH_THREAD", [False])

    calls = {"t82": 0, "t79c": 0, "restore": 0}
    monkeypatch.setattr(jra_suite, "_t82_startup_course_usage_refresh",
                        lambda: calls.__setitem__("t82", calls["t82"] + 1))
    monkeypatch.setattr(jra_suite, "_t79c_startup_weekend_graded_refresh",
                        lambda: calls.__setitem__("t79c", calls["t79c"] + 1))
    monkeypatch.setattr(jra_ev, "_restore_phase2_state",
                        lambda: calls.__setitem__("restore", calls["restore"] + 1))

    def _forbidden_thread(*_a, **_k):
        raise AssertionError("EV/WIN5 threads must not be constructed in viewer mode")

    monkeypatch.setattr(jra_suite.threading, "Thread", _forbidden_thread)

    assert jra_suite.start_background_loops() is True
    assert calls == {"t82": 1, "t79c": 1, "restore": 1}


# ─── §3-3: _ensure_scheduler() が即returnする ───────────────────────────────

def test_viewer_mode_ensure_scheduler_returns_immediately(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])

    def _forbidden(*_a, **_k):
        raise AssertionError("threading.Thread must not be constructed in viewer mode")

    monkeypatch.setattr(jra_ev.threading, "Thread", _forbidden)

    jra_ev._ensure_scheduler()
    assert jra_ev._SCHEDULER_STARTED[0] is False


# ─── §3-4: scheduler_loop() が即returnする ─────────────────────────────────

def test_viewer_mode_scheduler_loop_returns_immediately(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")

    def _forbidden_snapshot(*_a, **_k):
        raise AssertionError("snapshot_odds must not be called in viewer mode")

    def _forbidden_sleep(*_a, **_k):
        raise AssertionError("scheduler_loop must return before its while-loop body")

    monkeypatch.setattr(jra_ev, "snapshot_odds", _forbidden_snapshot)
    monkeypatch.setattr(jra_ev.time, "sleep", _forbidden_sleep)

    jra_ev.scheduler_loop()  # 例外が飛ばず、かつフリーズせず戻ってくることが確認事項


# ─── §3-5: _restore_phase2_state() が未送信通知を再送しない ────────────────

def test_viewer_mode_restore_phase2_state_skips_pending_notification_resend(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    mark_calls = []
    store = SimpleNamespace(
        active_monitors=lambda: [],
        retryable_notifications=lambda: [
            {"notification_id": 1, "channel": "line", "payload": {"stage": 5}},
        ],
        mark_notification=lambda *a, **k: mark_calls.append((a, k)),
    )
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)

    def _forbidden(*_a, **_k):
        raise AssertionError("_send_line/_send_discord must not be called in viewer mode")

    monkeypatch.setattr(jra_ev, "_send_line", _forbidden)
    monkeypatch.setattr(jra_ev, "_send_discord", _forbidden)

    jra_ev._restore_phase2_state()

    assert mark_calls == []  # pendingの行はDB側でそのまま残る


# ─── §3-6: jra_win5._watch_loop() が即returnする ───────────────────────────

def test_viewer_mode_win5_watch_loop_returns_immediately(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")

    def _forbidden(*_a, **_k):
        raise AssertionError("analyze_one_core must not run in viewer mode")

    monkeypatch.setattr(jra_win5, "analyze_one_core", _forbidden)
    monkeypatch.setattr(real_time, "sleep",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("_watch_loop must return before sleeping/looping")))

    jra_win5._watch_loop()  # 即returnし、時間もCPUも使わないことが確認事項


def test_viewer_mode_win5_confidence_notify_call_site_is_suppressed(monkeypatch):
    # SPEC-T83 §1.1: _watch_loop冒頭の即returnとは別経路の多重防御として、
    # _notify_win5_confidence(...) の呼び出し箇所自体もviewer_mode()を見て
    # スキップすることを確認する。冒頭ガードとこの呼び出し箇所ガードは
    # どちらもviewer_mode()を呼ぶ別々の箇所なので、1回目 (冒頭) はFalseを
    # 返してループ本体に入らせ、2回目 (呼び出し箇所) でTrueを返して
    # 呼び出し箇所自身の抑止だけを切り分けて検証する。
    call_count = {"n": 0}

    def _fake_viewer_mode():
        call_count["n"] += 1
        return call_count["n"] > 1  # 1回目=通常モード扱い、2回目以降=閲覧モード扱い

    monkeypatch.setattr(jra_win5, "viewer_mode", _fake_viewer_mode)

    race = {"idx": 0, "venue": "東京", "race_info": "1R", "upset_rank": "A",
            "race_date": "20260805",
            "horses": [{"num": 1, "name": "テスト馬", "score": 10.0, "odds": 2.0,
                        "pop": 1, "grade": ""}]}
    monkeypatch.setattr(jra_win5, "analyze_one_core", lambda i, u: dict(race, idx=i))
    kaime = {"success": True, "picks": [], "total_points": 200,
             "formula": "2x3x2x3x2=72点", "est_hit_rate": 0.5,
             "est_reference": [0.1, 0.2], "budget": 200, "alloc_method": "prob"}
    monkeypatch.setattr(jra_win5, "build_kaime", lambda races, points, single_axis: dict(kaime))

    def _forbidden(*_a, **_k):
        raise AssertionError("_notify_win5_confidence must not be called when the call-site guard fires")

    monkeypatch.setattr(jra_win5, "_notify_win5_confidence", _forbidden)

    # sleep(20)を短縮しつつタイトループで暴走しないよう小さいが非ゼロにする
    # (test_win5_line_notify.py の既存パターンに準拠)
    _orig_sleep = real_time.sleep
    monkeypatch.setattr(real_time, "sleep", lambda s: _orig_sleep(0.02))

    import datetime as _dt
    now = _dt.datetime.now()
    for key, value in {
        "status": "armed", "first_time": now.strftime("%H:%M"),
        "urls": ["http://example.invalid/1"], "points": 200,
        "single_axis": False, "result": None, "races": None,
        "error": "", "updated_at": "", "line_notify": "",
    }.items():
        jra_win5.WATCH[key] = value

    t = threading.Thread(target=jra_win5._watch_loop, daemon=True)
    t.start()
    try:
        deadline = real_time.time() + 5.0
        while real_time.time() < deadline:
            if jra_win5.WATCH["status"] in ("done", "error"):
                break
            real_time.sleep(0.02)
        assert jra_win5.WATCH["status"] == "done"
        assert jra_win5.WATCH["line_notify"] == "suppressed"
    finally:
        with jra_win5._WATCH_LOCK:
            jra_win5.WATCH["status"] = "idle"


# ─── §3-7: 通常モードでは従来どおり動く ─────────────────────────────────────

def test_normal_mode_ensure_scheduler_starts_thread(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])
    started = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None, **kwargs):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(jra_ev.threading, "Thread", _FakeThread)

    jra_ev._ensure_scheduler()
    assert started == [jra_ev.scheduler_loop]
    assert jra_ev._SCHEDULER_STARTED[0] is True


def test_normal_mode_scheduler_loop_enters_loop_body(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)

    class _Stop(Exception):
        pass

    monkeypatch.setattr(jra_ev.time, "sleep", lambda *_a, **_k: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        jra_ev.scheduler_loop()


def test_normal_mode_restore_phase2_state_resends_pending_notification(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    mark_calls = []
    sent = []
    store = SimpleNamespace(
        active_monitors=lambda: [],
        retryable_notifications=lambda: [
            {"notification_id": 1, "channel": "line", "payload": {"stage": 5}},
        ],
        mark_notification=lambda *a, **k: mark_calls.append((a, k)),
    )
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)

    def _fake_send_line(payload):
        sent.append(payload)
        return ("sent", 200, None)

    def _forbidden_discord(*_a, **_k):
        raise AssertionError("unexpected discord send")

    monkeypatch.setattr(jra_ev, "_send_line", _fake_send_line)
    monkeypatch.setattr(jra_ev, "_send_discord", _forbidden_discord)

    jra_ev._restore_phase2_state()

    assert sent == [{"stage": 5}]
    assert len(mark_calls) == 1


def test_normal_mode_win5_watch_loop_enters_loop_body(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)

    class _Stop(Exception):
        pass

    monkeypatch.setattr(real_time, "sleep", lambda *_a, **_k: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        jra_win5._watch_loop()


def test_normal_mode_start_background_loops_still_starts_threads(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    monkeypatch.setattr(jra_suite, "_LOOPS_STARTED", threading.Event())
    monkeypatch.setattr(jra_ev, "_SCHEDULER_STARTED", [False])
    monkeypatch.setattr(jra_win5, "_WATCH_THREAD", [False])

    calls = {"scheduler": 0, "watch": 0, "restore": 0}
    monkeypatch.setattr(jra_ev, "scheduler_loop", lambda: calls.__setitem__("scheduler", calls["scheduler"] + 1))
    monkeypatch.setattr(jra_win5, "_watch_loop", lambda: calls.__setitem__("watch", calls["watch"] + 1))
    monkeypatch.setattr(jra_ev, "_restore_phase2_state", lambda: calls.__setitem__("restore", calls["restore"] + 1))
    monkeypatch.setattr(jra_suite, "_t82_startup_course_usage_refresh", lambda: None)
    monkeypatch.setattr(jra_suite, "_t79c_startup_weekend_graded_refresh", lambda: None)

    assert jra_suite.start_background_loops() is True

    for _ in range(50):
        if calls["scheduler"] and calls["watch"] and calls["restore"]:
            break
        real_time.sleep(0.05)

    assert calls == {"scheduler": 1, "watch": 1, "restore": 1}
    assert jra_ev._SCHEDULER_STARTED[0] is True
    assert jra_win5._WATCH_THREAD[0] is True


# ─── §3-8: /ev/api/state に viewer_mode が入る ──────────────────────────────

def test_api_state_reports_viewer_mode_true(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    resp = jra_ev.app.test_client().get("/api/state")
    assert resp.status_code == 200
    assert resp.get_json()["viewer_mode"] is True


def test_api_state_reports_viewer_mode_false(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    resp = jra_ev.app.test_client().get("/api/state")
    assert resp.status_code == 200
    assert resp.get_json()["viewer_mode"] is False


# ─── §3-9: 画面表示 (ポータル帯 / index_ev.html) ────────────────────────────

def test_portal_shows_viewer_banner_and_stopped_badge_in_viewer_mode(monkeypatch):
    monkeypatch.setenv("JRA_VIEWER_MODE", "1")
    app = jra_suite.create_app()
    html = app.test_client().get("/").get_data(as_text=True)
    assert "viewerBanner" in html
    assert "閲覧モード" in html
    assert "停止中 (閲覧モード)" in html


def test_portal_hides_viewer_banner_in_normal_mode(monkeypatch):
    monkeypatch.delenv("JRA_VIEWER_MODE", raising=False)
    app = jra_suite.create_app()
    html = app.test_client().get("/").get_data(as_text=True)
    assert "id=\"viewerBanner\"" not in html
    assert "停止中 (閲覧モード)" not in html


def test_index_ev_html_reads_viewer_mode_from_state():
    html = (ROOT / "index_ev.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "viewerBanner" in html
    assert "viewer_mode" in html


# ─── §3-10: start_suite_viewer.bat ─────────────────────────────────────────

def test_start_suite_viewer_bat_sets_env_and_prefers_repo_venv():
    text = (ROOT / "start_suite_viewer.bat").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "JRA_VIEWER_MODE=1" in text
    assert r"..\Scripts\python.exe" in text
    assert "--auto-start" not in text


def test_start_suite_bat_is_unchanged_normal_mode_entrypoint():
    text = (ROOT / "start_suite.bat").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "JRA_VIEWER_MODE" not in text
