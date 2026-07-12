from datetime import datetime

import jra_perf
from api.logging_store import LoggingStore


def test_to_jst_converts_z_suffix():
    assert jra_perf._to_jst("2026-07-12T05:40:12.345Z") == "2026-07-12 14:40"


def test_to_jst_converts_offset_suffix():
    assert jra_perf._to_jst("2026-07-12T05:40:12+00:00") == "2026-07-12 14:40"


def test_to_jst_treats_naive_as_utc():
    assert jra_perf._to_jst("2026-07-12T05:40:12") == "2026-07-12 14:40"


def test_to_jst_returns_original_on_parse_failure():
    assert jra_perf._to_jst("not-a-timestamp") == "not-a-timestamp"


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


def test_collect_win5_hits_use_horse_numbers_selection_schema(tmp_path, monkeypatch):
    # win5_predictions.selections_json entries are dicts written by jra_win5.py as
    # {"race_id": ..., "horse_numbers": [...]} -- collect() must read that key
    # (not the unrelated "nums" key from jra_ev.py's combination payloads) or every
    # selection is treated as empty and every race reads as a miss.
    store = LoggingStore(tmp_path / "win5.db")
    store.initialize()
    race_date = "20260712"
    race_ids = [f"{race_date}:小倉:10", f"{race_date}:福島:10", f"{race_date}:函館:11",
                f"{race_date}:小倉:11", f"{race_date}:福島:11"]
    for idx, race_id in enumerate(race_ids):
        venue = race_id.split(":")[1]
        race_no = int(race_id.split(":")[2])
        store.save_race(race_id=race_id, race_date=race_date, venue=venue, race_no=race_no)
        winner_num = 7
        store.save_race_results([
            {
                "race_id": race_id, "horse_id": f"{race_id}:{winner_num:02d}", "horse_name": "勝ち馬",
                "finish_position": 1, "official_status": "official", "win_payout": 300,
                "place_payout": 130, "result_fetched_at": datetime.now().astimezone(),
                "source_hash": f"{race_id}:1", "data_quality_flags": [],
            },
        ])

    # Selections include the winning number (7) only for 福島10R and 福島11R.
    selections = [
        {"race_id": race_ids[0], "horse_numbers": [1, 2]},
        {"race_id": race_ids[1], "horse_numbers": [7, 3]},
        {"race_id": race_ids[2], "horse_numbers": [4, 5]},
        {"race_id": race_ids[3], "horse_numbers": [9]},
        {"race_id": race_ids[4], "horse_numbers": [7, 11]},
    ]
    store.save_win5_prediction(
        prediction_run_ids=["run1", "run2", "run3", "run4", "run5"],
        race_ids=race_ids, budget=200, total_points=200,
        selections=selections, coverage=[0.5] * 5,
        estimated_hit_rate=0.07, allocation_method="prob", allocation_version=1,
        single_axis=True, axis_index=None, idempotency_key="plan-key")

    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    result = jra_perf.collect("2026-07-12")

    assert len(result["win5"]) == 1
    entry = result["win5"][0]
    assert entry["all_settled"] is True
    assert entry["hits"] == [False, True, False, False, True]
    assert entry["win5_hit"] is False


def test_collect_days_aggregates_models_ev_and_win5(tmp_path, monkeypatch):
    # T30: collect() should also return a "days" list that combines, per calendar date,
    # the model daily summary, a deduped EV-alert summary, and the latest WIN5 plan's
    # hit count -- so the perf dashboard can show one row per day.
    store = LoggingStore(tmp_path / "days.db")
    race_date = "20260712"
    race_id = f"{race_date}:東京:01"
    _seed_prediction(store, race_id, race_date, with_result=True)

    # EV: two alerts (15分前/5分前) for the same horse must collapse into 1 unique horse.
    ev_run_id = store.start_run(
        app_name="ev_monitor", model_name="ev_model", model_version="1",
        started_at=datetime.fromisoformat("2026-07-12T02:00:00+00:00"))
    store.save_odds([{"race_id": race_id, "horse_id": f"{race_id}:01", "win_odds": 3.5,
                       "popularity": 1, "fetch_id": ev_run_id, "stage": "15min"}])
    for key in ("alert-15min", "alert-5min"):
        store.save_ev_evaluation(
            prediction_run_id=ev_run_id, race_id=race_id, horse_id=f"{race_id}:01",
            win_probability=0.4, ev=1.4, threshold=1.1, decision="alert",
            idempotency_key=key)

    # WIN5: two races on the same date -- one selection includes the winner, one doesn't.
    race_id2 = f"{race_date}:函館:02"
    store.save_race(race_id=race_id2, race_date=race_date, venue="函館", race_no=2)
    store.save_race_results([
        {
            "race_id": race_id2, "horse_id": f"{race_id2}:03", "horse_name": "勝ち馬2",
            "finish_position": 1, "official_status": "official", "win_payout": 250,
            "place_payout": 120, "result_fetched_at": datetime.now().astimezone(),
            "source_hash": f"{race_id2}:1", "data_quality_flags": [],
        },
    ])
    store.save_win5_prediction(
        prediction_run_ids=["r1", "r2"], race_ids=[race_id, race_id2], budget=100,
        total_points=100,
        selections=[{"race_id": race_id, "horse_numbers": [1]},
                    {"race_id": race_id2, "horse_numbers": [9]}],
        coverage=[0.5, 0.5], estimated_hit_rate=0.1, allocation_method="prob",
        allocation_version=1, single_axis=True, axis_index=None, idempotency_key="win5-day-key")

    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    result = jra_perf.collect()

    day = next(d for d in result["days"] if d["date"] == "2026-07-12")

    assert len(day["models"]) == 1
    assert day["models"][0]["model"] == "test_model"
    assert day["models"][0]["races"] == 1

    assert day["ev"]["n"] == 1  # deduped: same horse alerted twice
    assert day["ev"]["settled"] == 1
    assert day["ev"]["win"] == 1
    assert day["ev"]["tan_roi"] == 420.0
    assert day["ev"]["fuku_roi"] == 150.0

    assert day["win5"]["total"] == 2
    assert day["win5"]["hits"] == 1
    assert day["win5"]["all_settled"] is True
    assert day["win5"]["win5_hit"] is False


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
