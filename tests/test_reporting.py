import csv
import json
from io import StringIO

from api.logging_store import LoggingStore
from api.reporting import ReportFilters, format_text, generate_report


def seeded_store(tmp_path):
    store = LoggingStore(tmp_path / "report.db")
    race_id = "20260711:東京:03"
    store.save_race(race_id=race_id, race_date="20260711", venue="東京", race_no=3,
                    surface="芝", distance_m=1600, race_class="G3")
    run = store.start_run(app_name="ev_monitor", model_name="win5", model_version=4,
                          config={"threshold": 1.3})
    store.save_predictions(run, [
        {"race_id": race_id, "horse_id": f"{race_id}:07", "calibrated_win_probability": 0.3},
        {"race_id": race_id, "horse_id": f"{race_id}:08", "calibrated_win_probability": 0.1},
    ])
    store.save_odds([
        {"idempotency_key": "o7", "race_id": race_id, "horse_id": f"{race_id}:07", "fetch_id": run, "win_odds": 5.0},
        {"idempotency_key": "o8", "race_id": race_id, "horse_id": f"{race_id}:08", "fetch_id": run, "win_odds": None},
    ])
    evaluation = store.save_ev_evaluation(
        prediction_run_id=run, race_id=race_id, horse_id=f"{race_id}:07",
        win_probability=0.3, ev=1.5, threshold=1.3, decision="alert", idempotency_key="e7")
    store.save_ev_evaluation(
        prediction_run_id=run, race_id=race_id, horse_id=f"{race_id}:08",
        win_probability=0.1, ev=None, threshold=1.3, decision="skip", idempotency_key="e8")
    notification, _ = store.reserve_notification(
        ev_evaluation_id=evaluation, channel="discord", dedupe_key="n7", payload={"picks": []})
    store.mark_notification(notification, status="sent", response_code=204)
    store.save_race_results([
        {"race_id": race_id, "horse_id": f"{race_id}:07", "horse_name": "勝馬",
         "finish_position": 1, "official_status": "official", "final_win_odds": 5.3,
         "win_payout": 530, "place_payout": 150, "source_hash": "r7"},
        {"race_id": race_id, "horse_id": f"{race_id}:08", "horse_name": "二着馬",
         "finish_position": 2, "official_status": "official", "place_payout": 110, "source_hash": "r8"},
    ])
    return store


def test_report_metrics_filters_and_calibration(tmp_path):
    store = seeded_store(tmp_path)
    report = generate_report(store.db_path, filters=ReportFilters(
        date_from="20260701", date_to="20260731", app_name="ev_monitor",
        model_version="4", threshold=1.3, venue="東京", distance_m=1600))
    assert report["summary"]["matched_runners"] == 2
    assert report["summary"]["match_rate_pct"] == 100.0
    assert report["ev"]["settled_alerts"] == 1
    assert report["ev"]["win_hit_rate_pct"] == 100.0
    assert report["ev"]["win_roi_pct"] == 530.0
    assert report["notifications"]["sent"] == 1
    assert report["data_quality"]["missing_win_odds"] == 1
    assert report["by_period"][0]["period"] == "20260711"
    assert {row["probability_band_pct"] for row in report["calibration"]} == {10, 30}
    assert "単勝回収率 530.0%" in format_text(report)


def test_empty_report_and_period_options(tmp_path):
    store = LoggingStore(tmp_path / "empty.db")
    for period in ("day", "week", "month"):
        report = generate_report(store.db_path, period=period)
        assert report["summary"]["runs"] == 0
        assert report["by_period"] == []
