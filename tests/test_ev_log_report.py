import sqlite3
from datetime import datetime

import pytest

import ev_log_report
from api.logging_store import LoggingStore


def _add_alert(store, race_id, horse_no, *, stage, result=None, line_status="sent"):
    horse_id = f"{race_id}:{horse_no:02d}"
    run_id = store.start_run(app_name="ev_monitor", model_name="test", model_version="1")
    store.save_predictions(run_id, [{"race_id": race_id, "horse_id": horse_id}])
    store.save_odds([{
        "race_id": race_id, "horse_id": horse_id, "win_odds": 5.0,
        "source": "jra", "fetch_id": run_id, "idempotency_key": f"odds:{race_id}:{horse_no}",
    }])
    evaluation_id = store.save_ev_evaluation(
        prediction_run_id=run_id, race_id=race_id, horse_id=horse_id,
        win_probability=0.3, ev=1.5, threshold=1.3, decision="alert",
        reason={"stage": stage}, idempotency_key=f"eval:{race_id}:{horse_no}:{stage}",
    )
    for channel, status in (("browser", "sent"), ("line", line_status)):
        notification_id, _created = store.reserve_notification(
            ev_evaluation_id=evaluation_id, channel=channel,
            dedupe_key=f"notice:{race_id}:{horse_no}:{stage}:{channel}", payload={},
        )
        store.mark_notification(notification_id, status=status)
    if result is not None:
        store.save_race_results([{
            "race_id": race_id, "horse_id": horse_id, "horse_name": "テスト馬",
            "finish_position": result, "official_status": "official",
            "final_win_odds": 4.0, "win_payout": 400 if result == 1 else None,
            "place_payout": 150 if result <= 3 else None,
            "result_fetched_at": datetime.now().astimezone(),
            "source_hash": f"result:{race_id}:{horse_no}", "data_quality_flags": [],
        }])


def test_sqlite_report_counts_channels_returns_and_pending(tmp_path):
    store = LoggingStore(tmp_path / "ev-report.db")
    _add_alert(store, "20260712:東京:01", 1, stage=5, result=1)
    _add_alert(store, "20260712:東京:02", 2, stage=15, result=None,
               line_status="suppressed")

    with ev_log_report.open_readonly(store.db_path) as conn:
        report = ev_log_report.load_report(conn, "20260712")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden(id INTEGER)")

    overall = report["sections"]["全体"]
    assert report["alerts"] == 2
    assert (overall["settled"], overall["pending"], overall["win"]) == (1, 1, 1)
    assert (overall["tan_ret"], overall["fuku_ret"]) == (400.0, 150.0)
    assert report["channel_status"]["browser"] == {"sent": 2}
    assert report["channel_status"]["line"] == {"sent": 1, "suppressed": 1}
    assert report["channels"]["line"]["alerts"] == 1

    rendered = ev_log_report.render_report(report)
    assert "alert 2件" in rendered
    assert "結果待ち" in rendered
    assert "line" in rendered


@pytest.mark.parametrize("value,expected", [
    ("20260712", "20260712"), ("260712", "20260712"), ("2026-07-12", "20260712")
])
def test_since_formats(value, expected):
    assert ev_log_report.normalize_since(value) == expected


def test_invalid_since_is_rejected():
    with pytest.raises(ValueError):
        ev_log_report.normalize_since("20260230")
