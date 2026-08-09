"""SPEC-T68 (WIN5推定的中率が中央値超のときLINE通知) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T68-win5-line-notify-above-median.md §6
  1. est > median -> 送信される (broadcast URL・トークンヘッダ・本文にest/medianを含む)
  2. est <= median -> 送信されない
  3. est >= p75 -> 文言が「上位25%圏」になる
  4. est_referenceに該当点数が無い / alloc_method != "prob" / トークン未設定 /
     WIN5_LINE_NOTIFY=0 -> 送信されない
  5. requests.postが例外 -> 監視ループが落ちず line_notify="failed" が記録される
  6. 既存テスト全パス (python -m pytest tests/ -q で確認、本ファイル外)

requests.post は一切実発火させず、jra_win5.requests.post をモックする。
"""
import threading
import time as real_time

import pytest

import jra_win5


BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def _base_kaime(*, points=200, est=0.13, median=0.115, p75=0.153, alloc_method="prob"):
    return {
        "success": True,
        "picks": [],
        "total_points": points,
        "formula": "2x3x2x3x2=72点",
        "est_hit_rate": est,
        "est_reference": [median, p75] if median is not None else None,
        "budget": points,
        "alloc_method": alloc_method,
    }


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _line_env(monkeypatch):
    # 各テストで明示的に上書きしない限り、送信が許可される既定状態にする。
    monkeypatch.setenv("EV_LINE_CHANNEL_TOKEN", "dummy-token")
    monkeypatch.delenv("WIN5_LINE_NOTIFY", raising=False)
    yield


# ─── §6-1: est > median -> 送信される ──────────────────────────────────────

def test_notify_sends_when_est_above_median(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse(200)

    monkeypatch.setattr(jra_win5.requests, "post", fake_post)

    kaime = _base_kaime(est=0.13, median=0.115, p75=0.153)
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "sent"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == BROADCAST_URL
    assert call["headers"]["Authorization"] == "Bearer dummy-token"
    assert call["timeout"] == 10
    text = call["json"]["messages"][0]["text"]
    assert "13.00%" in text          # est*100:.2f
    assert "11.5%" in text           # median*100:.1f
    # SPEC§4: 購入推奨の文言を入れない (テンプレ自体の「購入推奨ではありません」という
    # 打ち消し表現は許容し、肯定形の推奨文言が無いことだけを確認する)
    assert "参考情報です" in text
    assert "購入推奨ではありません" in text
    assert "買い目" not in text
    assert "全買い" not in text


def test_notify_message_includes_horse_numbers_per_race(monkeypatch):
    # 2026-08-09 ユーザー要望: 頭数だけでなくレース別の選択馬番も記載する。
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"][0]["text"])
        return _FakeResponse(200)

    monkeypatch.setattr(jra_win5.requests, "post", fake_post)

    kaime = _base_kaime(est=0.13, median=0.115, p75=0.153)
    kaime["picks"] = [
        {"venue": "東京", "is_axis": False, "horses": [{"num": 1}, {"num": 5}, {"num": 8}]},
        {"venue": "中山", "is_axis": True, "horses": [{"num": 3}]},
        {"venue": "中京", "is_axis": False, "horses": [{"num": 2}, {"num": 12}]},
        {"venue": "新潟", "is_axis": False, "horses": [{"num": 7}, {"num": 9}]},
        {"venue": "札幌", "is_axis": False, "horses": [{"num": 4}, {"num": 6}, {"num": 10}]},
    ]
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260809"}])

    assert result == "sent"
    text = calls[0]
    assert "①東京: 1,5,8" in text
    assert "②中山: 3 (軸)" in text
    assert "③中京: 2,12" in text
    assert "④新潟: 7,9" in text
    assert "⑤札幌: 4,6,10" in text
    # 推奨文言は引き続き入れない
    assert "参考情報です" in text
    assert "買い目" not in text


# ─── §6-2: est <= median -> 送信されない ───────────────────────────────────

@pytest.mark.parametrize("est,median", [(0.115, 0.115), (0.10, 0.115)])
def test_notify_suppressed_when_est_not_above_median(monkeypatch, est, median):
    calls = []
    monkeypatch.setattr(jra_win5.requests, "post", lambda *a, **k: calls.append(1) or _FakeResponse(200))

    kaime = _base_kaime(est=est, median=median, p75=0.153)
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "suppressed"
    assert calls == []


# ─── §6-3: est >= p75 -> 「上位25%圏」の文言 ────────────────────────────────

def test_notify_message_says_top_quartile_when_est_at_or_above_p75(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"][0]["text"])
        return _FakeResponse(200)

    monkeypatch.setattr(jra_win5.requests, "post", fake_post)

    kaime = _base_kaime(est=0.20, median=0.115, p75=0.153)  # est >= p75
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "sent"
    assert "上位25%圏" in calls[0]
    assert "高め (上位25%圏)" in calls[0]

    # 対照: 中央値超えだがp75未満なら「中央値超え」表記のまま
    calls.clear()
    kaime2 = _base_kaime(est=0.13, median=0.115, p75=0.153)
    jra_win5._notify_win5_confidence(kaime2, 200, [{"race_date": "20260805"}])
    assert "上位25%圏" not in calls[0]
    assert "高め (中央値超え)" in calls[0]


# ─── §6-4: 送信されない4条件 ────────────────────────────────────────────────

def test_notify_suppressed_when_points_missing_from_est_reference(monkeypatch):
    calls = []
    monkeypatch.setattr(jra_win5.requests, "post", lambda *a, **k: calls.append(1))

    kaime = _base_kaime(est=0.5, median=None, p75=None)  # 該当点数の est_reference が無い
    result = jra_win5._notify_win5_confidence(kaime, 999, [{"race_date": "20260805"}])

    assert result == "suppressed"
    assert calls == []


def test_notify_suppressed_when_alloc_method_is_not_prob(monkeypatch):
    calls = []
    monkeypatch.setattr(jra_win5.requests, "post", lambda *a, **k: calls.append(1))

    kaime = _base_kaime(est=0.5, median=0.115, p75=0.153, alloc_method="rank")
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "suppressed"
    assert calls == []


def test_notify_suppressed_when_token_missing(monkeypatch):
    monkeypatch.delenv("EV_LINE_CHANNEL_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(jra_win5.requests, "post", lambda *a, **k: calls.append(1))

    kaime = _base_kaime(est=0.5, median=0.115, p75=0.153)
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "suppressed"
    assert calls == []


def test_notify_suppressed_when_win5_line_notify_disabled(monkeypatch):
    monkeypatch.setenv("WIN5_LINE_NOTIFY", "0")
    calls = []
    monkeypatch.setattr(jra_win5.requests, "post", lambda *a, **k: calls.append(1))

    kaime = _base_kaime(est=0.5, median=0.115, p75=0.153)
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "suppressed"
    assert calls == []


# ─── §6-5: requests.postが例外 -> line_notify="failed" (監視ループは落ちない) ──

def test_notify_returns_failed_without_raising_when_requests_post_raises(monkeypatch):
    def raise_post(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr(jra_win5.requests, "post", raise_post)

    kaime = _base_kaime(est=0.13, median=0.115, p75=0.153)
    # 例外が外に漏れないことそのものがテスト対象 (漏れれば pytest がテスト失敗として拾う)
    result = jra_win5._notify_win5_confidence(kaime, 200, [{"race_date": "20260805"}])

    assert result == "failed"


def test_watch_loop_stays_done_and_records_failed_line_notify_on_send_exception(monkeypatch):
    # SPEC§3: 送信失敗はWARN printのみで監視ループを壊さない。_watch_loop の
    # 実配線 (build_kaime成功後に通知判定 -> WATCH["line_notify"]に記録) を、
    # 実スレッドで1周させて確認する (test_jra_suite.py の既存パターンに準拠)。
    import datetime as _dt

    race = {
        "idx": 0, "venue": "東京", "race_info": "1R", "upset_rank": "A",
        "race_date": "20260805",
        "horses": [{"num": 1, "name": "テスト馬", "score": 10.0, "odds": 2.0,
                     "pop": 1, "grade": ""}],
    }
    monkeypatch.setattr(jra_win5, "analyze_one_core", lambda i, u: dict(race, idx=i))

    kaime = _base_kaime(est=0.13, median=0.115, p75=0.153)
    monkeypatch.setattr(jra_win5, "build_kaime", lambda races, points, single_axis: dict(kaime))

    def raise_post(*a, **k):
        raise ConnectionError("boom")
    monkeypatch.setattr(jra_win5.requests, "post", raise_post)

    # sleep(20)を短縮しつつタイトループで暴走しないよう小さいが非ゼロにする
    _orig_sleep = real_time.sleep
    monkeypatch.setattr(real_time, "sleep", lambda s: _orig_sleep(0.02))

    now = _dt.datetime.now()
    for key, value in {
        "status": "armed", "first_time": now.strftime("%H:%M"),
        "urls": ["http://example.invalid/1"] * 5, "points": 200,
        "single_axis": False, "result": None, "races": None,
        "error": "", "updated_at": "", "line_notify": "",
    }.items():
        monkeypatch.setitem(jra_win5.WATCH, key, value)

    t = threading.Thread(target=jra_win5._watch_loop, daemon=True)
    t.start()
    try:
        deadline = real_time.time() + 5.0
        while real_time.time() < deadline:
            if jra_win5.WATCH["status"] in ("done", "error"):
                break
            real_time.sleep(0.02)
        assert jra_win5.WATCH["status"] == "done"  # "error" になっていない = ループが落ちていない
        assert jra_win5.WATCH["line_notify"] == "failed"
    finally:
        # armed以外にして以降のループ本体をno-op化 (daemonなのでプロセス終了時に片付く)
        with jra_win5._WATCH_LOCK:
            jra_win5.WATCH["status"] = "idle"
