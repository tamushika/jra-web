import inspect
from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

import jra_ev
import index as api_index


JST = timezone(timedelta(hours=9))


def _horses(valid, field_size=8):
    rows = [{"odds": 2.0 + index} for index in range(valid)]
    rows.extend({"odds": 999.0} for _ in range(field_size - valid))
    return rows


def test_parse_horse_row_only_uses_current_entry_for_scratched_status(monkeypatch):
    monkeypatch.setattr(api_index, "fetch_history_data", lambda *args, **kwargs: {
        "raw": "2025年7月1日 取消", "corners": "-", "total": "-"})
    active_html = """
    <tr>
      <td>1</td><td>1</td><td class="horse">現役取消馬</td>
      <td>牡3 56.0 騎手A</td>
      <td><div class="odds_line"><strong>3.2</strong></div></td>
      <td class="history">2025年7月1日 <span>取消</span></td>
    </tr>
    """
    active = api_index.parse_horse_row(
        BeautifulSoup(active_html, "html.parser").tr,
        "https://example.invalid/race", "2026年7月18日", "簡易")
    assert active["status"] == ""
    assert active["scratched"] is False

    scratched_html = """
    <tr class="entry-row">
      <td>1</td><td>2</td><td class="horse">取消対象馬</td>
      <td>牝4 55.0 騎手B</td>
      <td class="entry_status"><span>取消</span></td>
      <td class="history">2025年8月1日 1着</td>
    </tr>
    """
    scratched = api_index.parse_horse_row(
        BeautifulSoup(scratched_html, "html.parser").tr,
        "https://example.invalid/race", "2026年7月18日", "簡易")
    assert scratched["status"] == "取消"
    assert scratched["scratched"] is True


def test_snapshot_quality_boundaries_and_cross_snapshot_flags():
    observed = datetime(2026, 7, 18, 11, 29, tzinfo=JST)
    scheduled = datetime(2026, 7, 18, 12, 0, tzinfo=JST)
    quality = jra_ev._snapshot_quality(
        30,
        scheduled,
        observed,
        123.9,
        _horses(4) + [{"odds": 10.0, "status": "取消"}],
        previous_snapshots=[{
            "stage": 10,
            "observed_at": (observed - timedelta(seconds=120)).isoformat(),
            "scheduled_post_at": (scheduled - timedelta(minutes=5)).isoformat(),
        }],
        scheduler_restart=True,
    )

    # stage±60秒は窓内。直前120秒ちょうどの別stageはcatchup扱い。
    assert quality["seconds_to_post"] == pytest.approx(1860.0)
    assert quality["fetch_duration_ms"] == 123
    assert quality["valid_odds_count"] == 4
    assert quality["field_size"] == 8
    assert quality["data_quality_flags"] == [
        "catchup_burst", "post_time_changed", "scheduler_restart"]

    outside = jra_ev._snapshot_quality(
        30, scheduled, observed - timedelta(milliseconds=1), 0, _horses(3))
    assert outside["seconds_to_post"] > 1860
    assert outside["data_quality_flags"] == [
        "late_capture", "insufficient_odds"]


def test_snapshot_odds_retries_insufficient_without_any_notification(monkeypatch):
    calls = []

    def fake_analyze(url, params, **kwargs):
        calls.append(kwargs)
        return {
            "horses": _horses(3),
            "n_picked": 0,
            "wide_picks": [],
            "last_update": "11:30:00",
            "odds_ok": False,
            "n_odds": 3,
            "_snapshot_quality": {
                "observed_at": f"2026-07-18T11:30:0{len(calls)}+09:00",
                "scheduled_post_at": "2026-07-18T12:00:00+09:00",
                "data_quality_flags": ["insufficient_odds"],
            },
            "_snapshot_persisted": True,
        }

    def unexpected(*args, **kwargs):
        raise AssertionError("snapshot経路から通知処理を呼んではならない")

    monkeypatch.setattr(jra_ev, "analyze_one", fake_analyze)
    monkeypatch.setattr(jra_ev, "refresh_and_alert", unexpected)
    monkeypatch.setattr(jra_ev, "_send_discord", unexpected)
    monkeypatch.setattr(jra_ev, "_send_line", unexpected)
    alerts_before = list(jra_ev.STATE["alerts"])
    rec = {
        "url": "https://example.invalid/race",
        "venue": "東京",
        "race_num": 11,
        "_start_dt": datetime(2026, 7, 18, 12, 0),
    }

    assert jra_ev.snapshot_odds(rec, 30, scheduler_restart=True) is False
    assert jra_ev.snapshot_odds(rec, 30) is False
    assert len(calls) == 2
    assert calls[0]["snapshot_context"]["scheduler_restart"] is True
    assert len(calls[1]["snapshot_context"]["previous_snapshots"]) == 1
    assert len(rec["_snapshot_history"]) == 2
    assert jra_ev.STATE["alerts"] == alerts_before


def test_snapshot_stage_retries_until_success_or_giveup(monkeypatch):
    attempts = []
    persisted = []

    def insufficient(rec, stage, *, scheduler_restart=False):
        attempts.append((stage, scheduler_restart))
        rec["saved_with_flags"] = ["insufficient_odds"]
        return False

    monkeypatch.setattr(jra_ev, "snapshot_odds", insufficient)
    monkeypatch.setattr(jra_ev, "_persist_monitor", lambda rec: persisted.append(rec.copy()))
    monkeypatch.setattr(jra_ev, "_SNAPSHOT_STAGES", ((30, 1800, 1200),))
    rec = {"checked30": False}

    jra_ev._process_snapshot_stages(rec, 1500, scheduler_restart=True)
    assert rec["checked30"] is False
    assert attempts == [(30, True)]
    assert persisted[-1]["checked30"] is False

    # giveup境界では、そのサイクルのinsufficient記録を残したうえでcheckedにする。
    jra_ev._process_snapshot_stages(rec, 1200)
    assert rec["checked30"] is True
    assert attempts[-1] == (30, False)
    assert persisted[-1]["saved_with_flags"] == ["insufficient_odds"]

    monkeypatch.setattr(jra_ev, "snapshot_odds", lambda *args, **kwargs: True)
    successful = {"checked30": False}
    jra_ev._process_snapshot_stages(successful, 1500)
    assert successful["checked30"] is True

    # 取得/DB保存失敗(None)は窓内 (giveup前) では再試行するが、giveup境界を
    # 過ぎたら打ち切る (T40レビュー判断: LoggingStore不全等の持続故障時に
    # 通知だけ生きたままsnapshotが終日無限リトライするsilent failureを防ぐ)。
    monkeypatch.setattr(jra_ev, "snapshot_odds", lambda *args, **kwargs: None)
    failed = {"checked30": False}
    jra_ev._process_snapshot_stages(failed, 1500)
    assert failed["checked30"] is False
    jra_ev._process_snapshot_stages(failed, 1200)
    assert failed["checked30"] is True


def test_analyze_one_persists_snapshot_metadata_and_race_start_time(monkeypatch):
    class FakeStore:
        def __init__(self):
            self.race = None
            self.odds = None

        def save_race(self, **kwargs):
            self.race = kwargs

        def start_run(self, **kwargs):
            return "run-t40"

        def save_predictions(self, run_id, rows):
            return len(rows)

        def save_odds(self, rows):
            self.odds = rows
            return len(rows)

        def finish_run(self, run_id):
            return None

    fake_store = FakeStore()
    raw_horses = [
        {"num": index + 1, "name": f"馬{index + 1}", "jock": "騎手",
         "odds": (2.0 + index if index < 4 else 999.0), "pop": index + 1,
         "score": 1.0}
        for index in range(8)
    ]
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: fake_store)
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda *args, **kwargs: {
        "venue": "東京", "race_type": "芝", "dist_val": 1600,
        "race_class": "1勝クラス", "baba_cond": "良",
        "race_info": "【東京 11R】芝1600m 12:00発走　テスト",
        "horses": raw_horses,
    })
    monkeypatch.setattr(jra_ev.scoring, "load_score_weights", lambda *args: {"version": 1})
    monkeypatch.setattr(jra_ev.scoring, "load_factor_table", lambda *args: {})
    monkeypatch.setattr(jra_ev.scoring, "compute_score_ml", lambda *args: (0.0, {}))
    monkeypatch.setattr(
        jra_ev.scoring, "win_probs_from_ml_scores",
        lambda scores: [1.0 / len(scores)] * len(scores))
    monkeypatch.setattr(jra_ev, "_data_version", lambda: "test")
    base = datetime(2026, 7, 18, 12, 0, tzinfo=JST)
    params = {
        "ev_threshold": 1.3, "max_odds": 50.0, "min_prob": 0.02,
        "wide_overlay": None,
    }

    result = jra_ev.analyze_one(
        "https://example.invalid/race", params, base_date=base, stage=30,
        snapshot_context={"scheduled_post_at": base, "previous_snapshots": []})

    assert fake_store.race["start_time"] == base
    assert len(fake_store.odds) == 8
    assert {row["scheduled_post_at"] for row in fake_store.odds} == {
        base.isoformat()}
    assert {row["valid_odds_count"] for row in fake_store.odds} == {4}
    assert {row["field_size"] for row in fake_store.odds} == {8}
    assert result["_snapshot_quality"]["valid_odds_count"] == 4
    assert result["_snapshot_persisted"] is True
    # sentinel 999.0はPhase C品質・保存のどちらでも有効オッズに数えない。
    assert fake_store.odds[-1]["win_odds"] is None
    assert "win_odds_unavailable" in fake_store.odds[-1]["data_quality_flags"]

    class FailingStore(FakeStore):
        def save_odds(self, rows):
            raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: FailingStore())
    failed_result = jra_ev.analyze_one(
        "https://example.invalid/race", params, base_date=base, stage=30,
        snapshot_context={"scheduled_post_at": base, "previous_snapshots": []})
    assert failed_result["_snapshot_persisted"] is False

    class FinishFailStore(FakeStore):
        def finish_run(self, run_id):
            raise RuntimeError("synthetic finish failure")

    finish_fail_store = FinishFailStore()
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: finish_fail_store)
    finish_failed = jra_ev.analyze_one(
        "https://example.invalid/race", params, base_date=base, stage=30,
        snapshot_context={"scheduled_post_at": base, "previous_snapshots": []})
    # odds行が全件保存済みなら、後続のrun終了更新失敗とは切り離して成功とする。
    assert finish_failed["_snapshot_persisted"] is True


def test_logging_failure_is_not_a_successful_snapshot(monkeypatch):
    monkeypatch.setattr(jra_ev, "analyze_one", lambda *args, **kwargs: {
        "horses": _horses(8), "n_picked": 0, "wide_picks": [],
        "last_update": "11:30:00", "odds_ok": True, "n_odds": 8,
        "_snapshot_quality": {
            "observed_at": "2026-07-18T11:30:00+09:00",
            "scheduled_post_at": "2026-07-18T12:00:00+09:00",
            "data_quality_flags": [],
        },
        "_snapshot_persisted": False,
    })
    rec = {
        "url": "https://example.invalid/race", "venue": "東京", "race_num": 11,
        "_start_dt": datetime(2026, 7, 18, 12, 0),
    }

    assert jra_ev.snapshot_odds(rec, 30) is None
    assert "_snapshot_history" not in rec


def test_refresh_notification_path_and_checked_branches_are_unchanged(monkeypatch):
    sent = []
    monkeypatch.setattr(jra_ev, "LoggingStore", None)
    monkeypatch.setattr(jra_ev, "analyze_one", lambda *args, **kwargs: {
        "horses": [{
            "num": 1, "name": "通知テスト馬", "odds": 5.0,
            "win_prob": 0.3, "ev": 1.5, "picked": True,
            "web_value": False,
        }],
        "n_picked": 1,
        "wide_picks": [],
        "last_update": "11:55:00",
        "_log_context": {},
    })
    monkeypatch.setattr(
        jra_ev, "_send_discord",
        lambda alert: (sent.append(("discord", alert["stage"])) or ("sent", 204, None)))
    monkeypatch.setattr(
        jra_ev, "_send_line",
        lambda alert: (sent.append(("line", alert["stage"])) or ("sent", 200, None)))
    monkeypatch.setitem(jra_ev.STATE, "alerts", [])
    rec = {
        "url": "https://example.invalid/race",
        "venue": "東京",
        "race_num": 11,
        "start_time": "12:00",
        "horses": [],
        "wide_picks": [],
    }

    assert jra_ev.refresh_and_alert(rec, 5) is True
    assert sent == [("discord", 5), ("line", 5)]
    assert jra_ev.STATE["alerts"][0]["label"] == "東京11R 12:00発走"

    scheduler_source = inspect.getsource(jra_ev.scheduler_loop)
    assert 'if not rec["checked15"] and remain <= 15 * 60:' in scheduler_source
    assert "if refresh_and_alert(rec, 15) or remain <= 6 * 60:" in scheduler_source
    assert 'elif not rec["checked5"] and remain <= 5 * 60:' in scheduler_source
    assert "if refresh_and_alert(rec, 5) or remain <= 0:" in scheduler_source
    assert scheduler_source.index('if not rec["checked15"]') < scheduler_source.index(
        "_process_snapshot_stages(")


def test_pending_notification_guard_skips_snapshot_until_next_cycle():
    # remain<=5で15分通知だけ処理済み: 5分通知を次周期で先に処理する。
    after_fifteen = {"checked15": True, "checked5": False}
    assert jra_ev._notification_due_and_pending(after_fifteen, 5 * 60) is True

    # 通知再取得に失敗してcheckedが立たない間もsnapshotを挟まない。
    failed_fifteen = {"checked15": False, "checked5": False}
    assert jra_ev._notification_due_and_pending(failed_fifteen, 10 * 60) is True
    failed_five = {"checked15": True, "checked5": False}
    assert jra_ev._notification_due_and_pending(failed_five, 4 * 60) is True

    assert jra_ev._notification_due_and_pending(
        {"checked15": True, "checked5": False}, 6 * 60) is False
    assert jra_ev._notification_due_and_pending(
        {"checked15": True, "checked5": True}, 4 * 60) is False

    source = inspect.getsource(jra_ev.scheduler_loop)
    guard_at = source.index("if _notification_due_and_pending(rec, remain):")
    snapshot_at = source.index("_process_snapshot_stages(")
    assert guard_at < snapshot_at
