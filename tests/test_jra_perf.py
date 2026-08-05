import sqlite3
from datetime import datetime, timedelta

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


# ─── T55: WIN5詳細表示 (買い目×人気・勝ち馬・払戻金額) ────────────────────────

def test_popularity_by_race_ranks_ascending_with_ties_when_full_coverage():
    # final_win_odds を昇順順位化: 同値はmin rank。全馬分が揃うレースのみ順位化する。
    race_id = "20260714:東京:01"
    results = {
        (race_id, f"{race_id}:01"): {"horse_id": f"{race_id}:01", "final_win_odds": 3.0},
        (race_id, f"{race_id}:02"): {"horse_id": f"{race_id}:02", "final_win_odds": 3.0},
        (race_id, f"{race_id}:03"): {"horse_id": f"{race_id}:03", "final_win_odds": 8.5},
    }
    pop = jra_perf._popularity_by_race(results)
    assert pop[race_id] == {1: 1, 2: 1, 3: 3}


def test_popularity_by_race_suppresses_partial_coverage():
    # 勝ち馬しかオッズを持たない実データでは部分順位化が「勝ち馬=常に1人気」の
    # 誤表示になるため、1頭でも欠損があれば全馬 None (「-」表示、推測しない)。
    race_id = "20260714:東京:01"
    results = {
        (race_id, f"{race_id}:01"): {"horse_id": f"{race_id}:01", "final_win_odds": 3.0},
        (race_id, f"{race_id}:02"): {"horse_id": f"{race_id}:02", "final_win_odds": None},
        (race_id, f"{race_id}:03"): {"horse_id": f"{race_id}:03", "final_win_odds": None},
    }
    pop = jra_perf._popularity_by_race(results)
    assert pop[race_id] == {1: None, 2: None, 3: None}


def test_win5_race_details_shows_hit_miss_and_unsettled_states(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "win5-detail.db")
    race_date = "20260714"
    race_hit = f"{race_date}:東京:01"      # 選択馬が勝ち馬と一致 (的中)
    race_miss = f"{race_date}:中山:02"     # 選択馬が勝ち馬と不一致 (不的中)
    race_open = f"{race_date}:阪神:03"     # 結果未確定
    for rid in (race_hit, race_miss, race_open):
        store.save_race(race_id=rid, race_date=race_date, venue=rid.split(":")[1],
                         race_no=int(rid.split(":")[2]))
    store.save_race_results([
        {"race_id": race_hit, "horse_id": f"{race_hit}:01", "horse_name": "本命馬",
         "finish_position": 1, "official_status": "official", "final_win_odds": 2.5,
         "win_payout": 250, "place_payout": 120, "result_fetched_at": datetime.now().astimezone(),
         "source_hash": f"{race_hit}:1", "data_quality_flags": []},
        {"race_id": race_hit, "horse_id": f"{race_hit}:02", "horse_name": "対抗馬",
         "finish_position": 2, "official_status": "official", "final_win_odds": 10.0,
         "result_fetched_at": datetime.now().astimezone(),
         "source_hash": f"{race_hit}:2", "data_quality_flags": []},
        {"race_id": race_miss, "horse_id": f"{race_miss}:03", "horse_name": "勝ち馬中山",
         "finish_position": 1, "official_status": "official", "final_win_odds": 4.0,
         "win_payout": 400, "place_payout": 150, "result_fetched_at": datetime.now().astimezone(),
         "source_hash": f"{race_miss}:1", "data_quality_flags": []},
        {"race_id": race_miss, "horse_id": f"{race_miss}:04", "horse_name": "対抗馬中山",
         "finish_position": 2, "official_status": "official",
         "result_fetched_at": datetime.now().astimezone(),
         "source_hash": f"{race_miss}:2", "data_quality_flags": []},
    ])
    race_ids = [race_hit, race_miss, race_open]
    selections = [
        {"race_id": race_hit, "horse_numbers": [1]},
        {"race_id": race_miss, "horse_numbers": [4]},
        {"race_id": race_open, "horse_numbers": [7]},
    ]
    store.save_win5_prediction(
        prediction_run_ids=["r1", "r2", "r3"], race_ids=race_ids, budget=100, total_points=100,
        selections=selections, coverage=[0.5, 0.5, 0.5], estimated_hit_rate=0.05,
        allocation_method="prob", allocation_version=1, single_axis=False, axis_index=None,
        idempotency_key="detail-key")

    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    result = jra_perf.collect("2026-07-14")

    entry = result["win5"][0]
    races = {row["race_id"]: row for row in entry["races"]}

    hit_row = races[race_hit]
    assert hit_row["settled"] is True and hit_row["hit"] is True
    assert hit_row["picks"] == [{"no": 1, "pop": 1, "hit": True}]
    assert hit_row["winner"] == {"no": 1, "name": "本命馬", "pop": 1}

    miss_row = races[race_miss]
    assert miss_row["settled"] is True and miss_row["hit"] is False
    assert miss_row["picks"] == [{"no": 4, "pop": None, "hit": False}]
    # 中山は対抗馬のオッズが無い部分カバレッジ → 順位化を抑止 (勝ち馬=常に1人気の誤表示防止)
    assert miss_row["winner"] == {"no": 3, "name": "勝ち馬中山", "pop": None}

    open_row = races[race_open]
    assert open_row["settled"] is False and open_row["hit"] is None
    assert open_row["picks"] == [{"no": 7, "pop": None, "hit": False}]
    assert open_row["winner"] is None


def test_win5_payout_view_unfetched_fetched_and_mismatch():
    race_rows_settled = [
        {"race_id": "r1", "winner": {"no": 1, "name": "A", "pop": 1}},
        {"race_id": "r2", "winner": {"no": 3, "name": "B", "pop": 2}},
        {"race_id": "r3", "winner": None},  # 未確定レースは比較対象外
    ]
    assert jra_perf._win5_payout_view(None, race_rows_settled) == {"fetched": False}

    matching_row = {"payout_yen": 6_412_100, "hit_ticket_count": 79, "carryover_flag": 0,
                     "carryover_amount": None, "winning_numbers_json": "[1,3,7]"}
    view = jra_perf._win5_payout_view(matching_row, race_rows_settled)
    assert view == {"fetched": True, "payout_yen": 6_412_100, "hit_ticket_count": 79,
                     "carryover_flag": False, "carryover_amount": None, "mismatch": False}

    mismatched_row = dict(matching_row, winning_numbers_json="[999,3,7]")
    view2 = jra_perf._win5_payout_view(mismatched_row, race_rows_settled)
    assert view2["mismatch"] is True


def test_collect_win5_attaches_payout_from_win5_results_table(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "win5-payout.db")
    race_date = "20260714"
    race_id = f"{race_date}:東京:01"
    store.save_race(race_id=race_id, race_date=race_date, venue="東京", race_no=1)
    store.save_race_results([
        {"race_id": race_id, "horse_id": f"{race_id}:01", "horse_name": "本命馬",
         "finish_position": 1, "official_status": "official", "final_win_odds": 2.5,
         "win_payout": 250, "place_payout": 120, "result_fetched_at": datetime.now().astimezone(),
         "source_hash": f"{race_id}:1", "data_quality_flags": []},
    ])
    store.save_win5_prediction(
        prediction_run_ids=["r1"], race_ids=[race_id], budget=100, total_points=100,
        selections=[{"race_id": race_id, "horse_numbers": [1]}], coverage=[0.9],
        estimated_hit_rate=0.5, allocation_method="prob", allocation_version=1,
        single_axis=False, axis_index=None, idempotency_key="payout-key")

    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    # win5_results 未取得 (レコード無し) -> 「未取得」
    unfetched = jra_perf.collect("2026-07-14")
    assert unfetched["win5"][0]["payout"] == {"fetched": False}

    # 取得済み -> 配当情報が反映される
    store.save_win5_result(
        race_date=race_date, payout_yen=250, hit_ticket_count=100, carryover_flag=False,
        carryover_amount=None, winning_numbers=[1],
        source_url="https://race.netkeiba.com/top/win5.html?date=20260714", source_hash="h1")
    fetched = jra_perf.collect("2026-07-14")
    payout = fetched["win5"][0]["payout"]
    assert payout["fetched"] is True
    assert payout["payout_yen"] == 250
    assert payout["mismatch"] is False


def test_collect_is_fail_soft_when_win5_results_table_is_missing(tmp_path, monkeypatch):
    # T55: dashboards opened against a DB whose migration hasn't run yet (or a
    # read-only connection racing a fresh initialize()) must not 500.
    store = LoggingStore(tmp_path / "no-win5-table.db")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE win5_results")
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))
    result = jra_perf.collect()
    assert result["win5"] == []


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


# ---------------------------------------------------------------------------
# T55: ability.db 確定人気 (全馬分) の第一ソース化
# ---------------------------------------------------------------------------

def _make_ability_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE runs (date TEXT, place TEXT, r INTEGER,"
                 " umaban INTEGER, popularity INTEGER)")
    conn.executemany("INSERT INTO runs VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_ability_popularity_is_primary_source(tmp_path):
    db = tmp_path / "ability.db"
    _make_ability_db(db, [
        ("20260712", "小倉", 10, 5, 3),
        ("20260712", "小倉", 10, 10, 1),
        ("20260712", "小倉", 10, 7, None),  # 取消等: popularity無しは載せない
    ])
    pops = jra_perf._ability_popularity_for(["20260712:小倉:10"], db_path=str(db))
    assert pops == {"20260712:小倉:10": {5: 3, 10: 1}}
    merged = jra_perf._merge_popularity({"20260712:小倉:10": {5: 9, 2: 4}}, pops)
    # ability値 (5番=3人気) がオッズ順位化 (5番=9) より優先。
    # ability非収載の2番はオッズ順位化で補完、10番はability単独でも入る
    assert merged["20260712:小倉:10"] == {5: 3, 2: 4, 10: 1}


def test_snapshot_popularity_uses_latest_snapshot_only(tmp_path):
    db = tmp_path / "log.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE odds_snapshots (race_id TEXT, horse_id TEXT,"
                 " observed_at TEXT, popularity INTEGER)")
    rid = "20260718:福島:10"
    conn.executemany("INSERT INTO odds_snapshots VALUES (?,?,?,?)", [
        (rid, f"{rid}:01", "2026-07-18T06:00:00Z", 5),   # 古いスナップショット (無視)
        (rid, f"{rid}:01", "2026-07-18T06:08:00Z", 3),   # 最終スナップショット
        (rid, f"{rid}:02", "2026-07-18T06:08:00Z", 1),
        (rid, f"{rid}:03", "2026-07-18T06:08:00Z", None),  # 人気欠損は載せない
    ])
    conn.commit()
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    assert jra_perf._snapshot_popularity_for(cur, [rid]) == {rid: {1: 3, 2: 1}}
    # テーブルが無いDBでもfail-soft
    empty = sqlite3.connect(str(tmp_path / "empty.db"))
    assert jra_perf._snapshot_popularity_for(empty.cursor(), [rid]) == {}


def test_ability_popularity_fails_soft_when_db_missing_or_unsynced(tmp_path):
    missing = tmp_path / "no_such.db"
    assert jra_perf._ability_popularity_for(
        ["20260712:小倉:10"], db_path=str(missing)) == {}
    db = tmp_path / "ability.db"
    _make_ability_db(db, [("20260101", "中山", 1, 1, 1)])
    # 該当日未同期は空 (フォールバック)。壊れたrace_idは例外を出さずスキップ
    assert jra_perf._ability_popularity_for(
        ["20260712:小倉:10", "bogus", "a:b:c"], db_path=str(db)) == {}


# ---------------------------------------------------------------------------
# T56: 全期間の日次グラフとレース結果の日付グループ
# ---------------------------------------------------------------------------

def test_collect_all_period_series_matches_daily_and_has_no_day_cap(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "series.db")
    start = datetime(2026, 1, 1)
    for offset in range(61):
        date = (start + timedelta(days=offset)).strftime("%Y%m%d")
        _seed_prediction(store, f"{date}:東京:01", date, with_result=True)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    result = jra_perf.collect()
    assert len(result["daily"]) == 60  # 従来の表は上限を維持
    points = result["series"]["models"][0]["points"]
    assert len(points) == 61
    assert points[0]["date"] == "2026-01-01"
    assert points[-1]["date"] == "2026-03-02"
    daily = next(row for row in result["daily"] if row["date"] == "20260302")
    point = next(row for row in points if row["date"] == "2026-03-02")
    assert point == {
        "date": "2026-03-02", "settled": daily["settled"],
        "win_rate": daily["win_rate"], "tan_roi": daily["tan_roi"],
        "fuku_roi": daily["fuku_roi"],
    }


def test_collect_all_period_race_details_latest_and_race_dates_aggregate(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "race-dates.db")
    _seed_prediction(store, "20260711:東京:01", "20260711", with_result=True)
    _seed_prediction(store, "20260712:函館:01", "20260712", with_result=True)
    _seed_prediction(store, "20260712:函館:02", "20260712", with_result=False)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    result = jra_perf.collect()
    assert len(result["race_details"]) == 2
    assert {row["date"] for row in result["race_details"]} == {"20260712"}
    assert [row["date"] for row in result["race_dates"]] == ["2026-07-12", "2026-07-11"]
    assert result["race_dates"][0] == {
        "date": "2026-07-12", "races": 2, "settled": 1, "win": 1, "top3": 1,
        "tan_roi": 420.0, "fuku_roi": 150.0, "models": 1,
    }
    assert result["race_dates"][1]["races"] == 1


def test_collect_date_response_keeps_legacy_shape_and_values(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "date-shape.db")
    _seed_prediction(store, "20260712:函館:01", "20260712", with_result=True)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    result = jra_perf.collect("2026-07-12")
    assert "series" not in result
    assert "race_dates" not in result
    assert set(result) == {
        "summary", "daily", "ev", "win5", "race_details", "pending_races",
        "selected_date", "available_dates", "days", "win5_shadow_summary",
    }
    assert result["selected_date"] == "2026-07-12"
    assert result["race_details"][0]["date"] == "20260712"
    assert result["daily"][0]["win_rate"] == 100.0


def test_collect_series_preserves_none_metrics_for_unsettled_date(tmp_path, monkeypatch):
    store = LoggingStore(tmp_path / "unsettled-series.db")
    _seed_prediction(store, "20260712:函館:01", "20260712", with_result=False)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    point = jra_perf.collect()["series"]["models"][0]["points"][0]
    assert point["settled"] == 0
    assert point["win_rate"] is None
    assert point["tan_roi"] is None
    assert point["fuku_roi"] is None


# ---------------------------------------------------------------------------
# T66: WIN5「500点シャドー」(表示時に predictions.ml_score + win5_predictions
# .prediction_run_ids_json から再計算する導出表示。DBには一切書き込まない)
# ---------------------------------------------------------------------------

def _seed_win5_shadow_fixture(store, race_date, *, n_horses=8, null_ml_score=None,
                              winner_num_per_race=None):
    """5レース×n_horses頭の predictions (run 1本/レース) + win5_predictions 1行 +
    race_results を作る。馬番が小さいほど ml_score が高くなるよう構成する
    (シャドーの「上位k頭選択」を馬番の昇順で検証できるようにするため)。
    - null_ml_score=(race_idx, horse_num): 該当馬のml_scoreをNoneにする (算出不可ケース)
    - winner_num_per_race: 各レースの勝ち馬番 (既定は全レース1番=最高ml_score馬)
    戻り値: race_ids"""
    venues = ["東京", "中山", "阪神", "小倉", "福島"]
    if winner_num_per_race is None:
        winner_num_per_race = [1] * 5
    race_ids, run_ids = [], []
    for i, venue in enumerate(venues):
        race_id = f"{race_date}:{venue}:{i + 1:02d}"
        store.save_race(race_id=race_id, race_date=race_date, venue=venue, race_no=i + 1)
        run_id = store.start_run(
            app_name="win5", model_name="win5_score", model_version="1",
            started_at=datetime.fromisoformat(
                f"{race_date[:4]}-{race_date[4:6]}-{race_date[6:]}T01:00:00+00:00"))
        rows = []
        for n in range(1, n_horses + 1):
            score = 100.0 - (n - 1) * 9.0 - i  # 馬番が大きいほどml_scoreが低い (降順)
            if null_ml_score == (i, n):
                score = None
            rows.append({"race_id": race_id, "horse_id": f"{race_id}:{n:02d}", "ml_score": score})
        store.save_predictions(run_id, rows)
        store.finish_run(run_id)
        winner_num = winner_num_per_race[i]
        store.save_race_results([{
            "race_id": race_id, "horse_id": f"{race_id}:{winner_num:02d}", "horse_name": f"勝ち馬{i}",
            "finish_position": 1, "official_status": "official", "win_payout": 300,
            "place_payout": 130, "result_fetched_at": datetime.now().astimezone(),
            "source_hash": f"{race_id}:1", "data_quality_flags": [],
        }])
        race_ids.append(race_id)
        run_ids.append(run_id)
    store.save_win5_prediction(
        prediction_run_ids=run_ids, race_ids=race_ids, budget=200, total_points=200,
        selections=[{"race_id": rid, "horse_numbers": [1]} for rid in race_ids],
        coverage=[0.5] * 5, estimated_hit_rate=0.1, allocation_method="prob",
        allocation_version=1, single_axis=False, axis_index=None,
        idempotency_key=f"shadow-{race_date}-{null_ml_score}-{winner_num_per_race}")
    return race_ids


def test_win5_shadow_500_is_deterministic_and_selects_top_ml_score(tmp_path, monkeypatch):
    # SPEC-T66 §6-1: シャドー配分が決定的に再現され、総点数<=500・各レースk>=1・
    # 選択馬番がml_score上位であること。
    store = LoggingStore(tmp_path / "shadow-alloc.db")
    race_date = "20260713"
    _seed_win5_shadow_fixture(store, race_date)
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    shadow1 = jra_perf.collect("2026-07-13")["win5"][0]["shadow_500"]
    shadow2 = jra_perf.collect("2026-07-13")["win5"][0]["shadow_500"]

    assert shadow1 is not None
    assert shadow1 == shadow2  # 同一入力から常に同一結果 (決定的)
    assert shadow1["budget"] == 500
    assert shadow1["points"] <= 500
    assert len(shadow1["races"]) == 5
    for row in shadow1["races"]:
        nums = [p["no"] for p in row["picks"]]
        assert len(nums) >= 1  # 各レースk>=1
        # fixtureは馬番が小さいほどml_scoreが高いよう構成しているので、
        # 選択される馬番は常に1番から連番の上位k頭になる
        assert nums == list(range(1, len(nums) + 1))


def test_win5_shadow_500_hit_all_races_uses_payout_yen_in_aggregate(tmp_path, monkeypatch):
    # SPEC-T66 §6-2: 勝ち馬が全レースのシャドー選択に入るケース -> hit=True、
    # payout=win5_results.payout_yenが「500点シャドー累計」に載る。
    store = LoggingStore(tmp_path / "shadow-hit.db")
    race_date = "20260713"
    _seed_win5_shadow_fixture(store, race_date, winner_num_per_race=[1, 1, 1, 1, 1])
    store.save_win5_result(
        race_date=race_date, payout_yen=6_412_100, hit_ticket_count=3,
        carryover_flag=False, carryover_amount=None, winning_numbers=[1, 1, 1, 1, 1],
        source_url="https://race.netkeiba.com/top/win5.html?date=" + race_date,
        source_hash="shadow-hit-h1")
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    result = jra_perf.collect("2026-07-13")
    shadow = result["win5"][0]["shadow_500"]
    assert shadow is not None
    assert shadow["all_settled"] is True
    assert shadow["win5_hit"] is True
    assert shadow["payout"]["fetched"] is True
    assert shadow["payout"]["payout_yen"] == 6_412_100

    summary = result["win5_shadow_summary"]
    assert summary["budget"] == 500
    assert summary["target_days"] == 1
    assert summary["hit_days"] == 1
    assert summary["unavailable_days"] == 0
    assert summary["cost_yen"] == shadow["cost_yen"]
    assert summary["payout_yen"] == 6_412_100
    assert summary["roi_pct"] == round(100.0 * 6_412_100 / summary["cost_yen"], 1)


def test_win5_shadow_500_is_none_when_any_ml_score_missing(tmp_path, monkeypatch):
    # SPEC-T66 §6-3: 1レースのml_scoreがNULLの馬が1頭でもいれば、シャドーは
    # None (「算出不可」)。代替値のでっち上げをしない。公式結果があっても
    # 「500点シャドー累計」の対象日数には含めない (unavailable_daysで計上)。
    store = LoggingStore(tmp_path / "shadow-null.db")
    race_date = "20260713"
    _seed_win5_shadow_fixture(store, race_date, null_ml_score=(2, 3))
    store.save_win5_result(
        race_date=race_date, payout_yen=1_000_000, hit_ticket_count=5,
        carryover_flag=False, carryover_amount=None, winning_numbers=[1, 1, 1, 1, 1],
        source_url="https://race.netkeiba.com/top/win5.html?date=" + race_date,
        source_hash="shadow-null-h1")
    monkeypatch.setattr(jra_perf, "DB_PATH", str(store.db_path))

    result = jra_perf.collect("2026-07-13")
    assert result["win5"][0]["shadow_500"] is None

    summary = result["win5_shadow_summary"]
    assert summary["target_days"] == 0
    assert summary["hit_days"] == 0
    assert summary["unavailable_days"] == 1
    assert summary["cost_yen"] == 0
    assert summary["payout_yen"] == 0
    assert summary["roi_pct"] is None
