from datetime import datetime

import jra_perf
from api.logging_store import LoggingStore


def _seed_prediction(store, race_id, race_date, *, with_result):
    venue = race_id.split(":")[1]
    race_no = int(race_id.split(":")[2])
    store.save_race(race_id=race_id, race_date=race_date, venue=venue, race_no=race_no)
    run_id = store.start_run(
        app_name="ev_monitor", model_name="test_model", model_version="1",
        started_at=datetime.fromisoformat(f"{race_date[:4]}-{race_date[4:6]}-{race_date[6:]}T01:00:00+00:00"),
    )
    store.save_predictions(run_id, [
        {
            "race_id": race_id, "horse_id": f"{race_id}:01", "ml_score": 10.0,
            "feature_snapshot": {"horse_no": 1, "horse_name": "本命馬"},
        },
        {
            "race_id": race_id, "horse_id": f"{race_id}:02", "ml_score": 5.0,
            "feature_snapshot": {"horse_no": 2, "horse_name": "対抗馬"},
        },
    ])
    store.finish_run(run_id)
    if with_result:
        store.save_race_results([
            {
                "race_id": race_id, "horse_id": f"{race_id}:01", "horse_name": "本命馬",
                "finish_position": 1, "official_status": "official", "win_payout": 420,
                "place_payout": 150, "result_fetched_at": datetime.now().astimezone(),
                "source_hash": f"{race_id}:1", "data_quality_flags": [],
            },
            {
                "race_id": race_id, "horse_id": f"{race_id}:02", "horse_name": "対抗馬",
                "finish_position": 2, "official_status": "official", "place_payout": 180,
                "result_fetched_at": datetime.now().astimezone(),
                "source_hash": f"{race_id}:2", "data_quality_flags": [],
            },
        ])


def test_collect_filters_by_race_date_and_keeps_pending_day(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "perf.db")
    _seed_prediction(store, "20260711:東京:01", "20260711", with_result=True)
    _seed_prediction(store, "20260712:函館:01", "20260712", with_result=False)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    settled = jra_perf.collect("2026-07-11")
    assert settled["selected_date"] == "2026-07-11"
    assert settled["pending_races"] == 0
    assert settled["summary"][0]["races"] == 1
    assert settled["summary"][0]["settled"] == 1
    assert {row["date"] for row in settled["race_details"]} == {"20260711"}
    assert settled["race_details"][0]["result"] == 1

    pending = jra_perf.collect("20260712")
    assert pending["pending_races"] == 1
    assert pending["daily"][0]["races"] == 1
    assert pending["daily"][0]["settled"] == 0
    assert pending["race_details"][0]["result"] is None
    assert pending["available_dates"] == ["2026-07-12", "2026-07-11"]


def test_perf_api_validates_date_and_allows_empty_day(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "api.db")
    store.initialize()
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    client = jra_perf.app.test_client()

    invalid = client.get("/api/perf?date=2026/07/11")
    assert invalid.status_code == 400

    empty = client.get("/api/perf?date=2026-07-11")
    assert empty.status_code == 200
    payload = empty.get_json()
    assert payload["selected_date"] == "2026-07-11"
    assert payload["race_details"] == []
    assert payload["pending_races"] == 0


def test_result_sync_api_uses_selected_date(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "sync-api.db")
    store.initialize()
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    called = []

    def fake_sync(value, *, store):
        called.append((value, str(store.db_path)))
        return {"date": "20260711", "sources": 1, "synced": 1, "failed": 0}

    monkeypatch.setattr(jra_perf, "sync_results_for_date", fake_sync)
    response = jra_perf.app.test_client().post("/api/results/sync", json={"date": "2026-07-11"})

    assert response.status_code == 200
    assert response.get_json()["synced"] == 1
    assert called == [("2026-07-11", str(store.db_path))]
