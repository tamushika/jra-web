import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from api.logging_store import LoggingStore, config_hash, stable_json


@pytest.fixture
def store(tmp_path):
    result = LoggingStore(tmp_path / "logging.db", busy_timeout_ms=50, retries=1)
    result.initialize()
    return result


def scalar(store, sql):
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(sql).fetchone()[0]


def test_initialize_is_one_command_and_enables_wal(store):
    assert scalar(store, "SELECT count(*) FROM schema_migrations") == 11
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_win5_results_table_is_added_to_pre_t55_database(tmp_path):
    # Simulate a real DB that predates T55: schema_migrations exists with versions
    # 1-8 applied but no win5_results table yet.
    db_path = tmp_path / "pre-t55.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
        conn.executemany("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                          [(v, "2026-01-01T00:00:00Z") for v in range(1, 9)])

    migrated = LoggingStore(db_path)
    migrated.initialize()
    migrated.initialize()  # must be safe to run twice (idempotent migration)
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version_count = conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=9").fetchone()[0]
    assert "win5_results" in tables
    assert version_count == 1


def test_win5_results_migration_is_idempotent_on_existing_db(store):
    # T55: re-running initialize() on a DB that already has win5_results (and on
    # one that predates it) must not raise and must not duplicate the migration row.
    store.initialize()
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(win5_results)")}
        migration_rows = conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=9").fetchone()[0]
    assert {"race_date", "payout_yen", "hit_ticket_count", "carryover_flag",
            "carryover_amount", "winning_numbers_json", "fetched_at", "source_url",
            "source_hash"} <= columns
    assert migration_rows == 1


def test_save_and_get_win5_result_upserts_by_date(store):
    store.save_win5_result(
        race_date="20260712", payout_yen=6_412_100, hit_ticket_count=79,
        carryover_flag=False, carryover_amount=None,
        winning_numbers=[10, 11, 10, 6, 11],
        source_url="https://race.netkeiba.com/top/win5.html?date=20260712",
        source_hash="abc123")
    row = store.get_win5_result("20260712")
    assert row["payout_yen"] == 6_412_100
    assert row["hit_ticket_count"] == 79
    assert json.loads(row["winning_numbers_json"]) == [10, 11, 10, 6, 11]

    # Upsert: a later fetch for the same date overwrites rather than duplicating.
    store.save_win5_result(
        race_date="20260712", payout_yen=6_412_100, hit_ticket_count=80,
        carryover_flag=False, carryover_amount=None,
        winning_numbers=[10, 11, 10, 6, 11],
        source_url="https://race.netkeiba.com/top/win5.html?date=20260712",
        source_hash="def456")
    row2 = store.get_win5_result("20260712")
    assert row2["hit_ticket_count"] == 80
    assert scalar(store, "SELECT count(*) FROM win5_results") == 1
    assert store.get_win5_result("20260713") is None


def test_run_and_prediction_are_idempotent(store):
    run1 = store.start_run(app_name="ev_monitor", config={"threshold": 1.3}, idempotency_key="event-1")
    run2 = store.start_run(app_name="ev_monitor", config={"threshold": 1.3}, idempotency_key="event-1")
    assert run1 == run2
    row = {"race_id": "20260711:tokyo:01", "horse_id": "horse-1", "web_score": 8.2}
    assert store.save_predictions(run1, [row]) == 1
    assert store.save_predictions(run1, [row]) == 0
    assert scalar(store, "SELECT count(*) FROM predictions") == 1


def test_place_probability_is_nullable_and_old_database_is_migrated(tmp_path):
    db_path = tmp_path / "old-logging.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE predictions (
            prediction_run_id TEXT NOT NULL, race_id TEXT NOT NULL,
            horse_id TEXT NOT NULL, predicted_at TEXT NOT NULL,
            web_score REAL, win5_score REAL, ml_score REAL,
            raw_win_probability REAL, calibrated_win_probability REAL,
            confidence REAL, score_details_json TEXT,
            feature_snapshot_json TEXT, data_quality_flags_json TEXT,
            PRIMARY KEY (prediction_run_id,race_id,horse_id))""")

    migrated = LoggingStore(db_path)
    migrated.initialize()
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    assert "place_probability" in columns

    run_id = migrated.start_run(app_name="web")
    migrated.save_predictions(run_id, [{
        "race_id": "r1", "horse_id": "h1", "place_probability": 0.375,
    }])
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT place_probability FROM predictions").fetchone()[0]
    assert stored == pytest.approx(0.375)


def test_separate_runs_append_prediction_snapshots(store):
    row = {"race_id": "r1", "horse_id": "h1"}
    first = store.start_run(app_name="web")
    second = store.start_run(app_name="web")
    store.save_predictions(first, [row])
    store.save_predictions(second, [row])
    assert scalar(store, "SELECT count(*) FROM predictions") == 2


def test_odds_idempotency_and_missing_odds_remain_null(store):
    row = {"idempotency_key": "fetch:r1:h1", "race_id": "r1", "horse_id": "h1",
           "observed_at": datetime(2026, 7, 11, tzinfo=timezone.utc), "win_odds": None,
           "data_quality_flags": ["win_odds_unavailable"]}
    assert store.save_odds([row]) == 1
    assert store.save_odds([row]) == 0
    with sqlite3.connect(store.db_path) as conn:
        odds, flags = conn.execute("SELECT win_odds,data_quality_flags_json FROM odds_snapshots").fetchone()
    assert odds is None
    assert json.loads(flags) == ["win_odds_unavailable"]


def test_snapshot_quality_columns_are_written_in_new_database(store):
    jst = timezone(timedelta(hours=9))
    store.save_odds([{
        "idempotency_key": "snapshot:r1:h1:30",
        "race_id": "r1",
        "horse_id": "h1",
        "observed_at": datetime(2026, 7, 17, 11, 30, tzinfo=jst),
        "scheduled_post_at": datetime(2026, 7, 17, 12, 0, tzinfo=jst),
        "seconds_to_post": 1800.0,
        "fetch_duration_ms": 321,
        "valid_odds_count": 12,
        "field_size": 14,
        "stage": 30,
    }])
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("""SELECT scheduled_post_at,seconds_to_post,fetch_duration_ms,
            valid_odds_count,field_size,stage FROM odds_snapshots""").fetchone()
    assert row == (
        "2026-07-17T12:00:00.000000+09:00", 1800.0, 321, 12, 14, "30")


def test_snapshot_quality_columns_are_migrated_into_old_database(tmp_path):
    db_path = tmp_path / "old-odds.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE odds_snapshots (
            odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            race_id TEXT NOT NULL,
            horse_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_updated_at TEXT,
            win_odds REAL,
            place_odds_low REAL,
            place_odds_high REAL,
            popularity INTEGER,
            source TEXT NOT NULL,
            fetch_id TEXT,
            is_stale INTEGER NOT NULL DEFAULT 0,
            data_quality_flags_json TEXT
        )""")

    migrated = LoggingStore(db_path)
    migrated.initialize()
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(odds_snapshots)")
        }
    assert columns["stage"] == "TEXT"
    assert columns["scheduled_post_at"] == "TEXT"
    assert columns["seconds_to_post"] == "REAL"
    assert columns["fetch_duration_ms"] == "INTEGER"
    assert columns["valid_odds_count"] == "INTEGER"
    assert columns["field_size"] == "INTEGER"

    assert migrated.save_odds([{
        "idempotency_key": "migrated:r1:h1",
        "race_id": "r1",
        "horse_id": "h1",
        "stage": "10",
        "seconds_to_post": 600,
    }]) == 1
    assert scalar(migrated, "SELECT seconds_to_post FROM odds_snapshots") == 600


def test_json_is_stable_finite_and_redacts_secrets():
    value = {"b": Decimal("1.25"), "a": float("nan"), "webhook_url": "https://secret"}
    encoded = stable_json(value)
    assert encoded == '{"a":null,"b":1.25,"webhook_url":"[REDACTED]"}'
    assert config_hash(value) == config_hash({"webhook_url": "different", "a": float("inf"), "b": 1.25})


def test_locked_database_fails_quickly(store):
    lock = sqlite3.connect(store.db_path, timeout=0)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.start_run(app_name="web")
    finally:
        lock.rollback()
        lock.close()


def test_ev_evaluation_and_notification_are_traceable_and_deduplicated(store):
    run_id = store.start_run(app_name="ev_monitor")
    store.save_predictions(run_id, [{"race_id": "r1", "horse_id": "h1"}])
    store.save_odds([{"idempotency_key": "o1", "race_id": "r1", "horse_id": "h1",
                      "fetch_id": run_id, "win_odds": 4.5}])
    evaluation_id = store.save_ev_evaluation(
        prediction_run_id=run_id, race_id="r1", horse_id="h1", win_probability=0.3,
        ev=1.35, threshold=1.3, decision="alert", idempotency_key="eval-1")
    first_id, created = store.reserve_notification(
        ev_evaluation_id=evaluation_id, channel="discord", dedupe_key="r1:h1:5:discord:v1",
        payload={"stage": 5, "picks": [{"name": "horse"}]})
    second_id, created_again = store.reserve_notification(
        ev_evaluation_id=evaluation_id, channel="discord", dedupe_key="r1:h1:5:discord:v1",
        payload={"stage": 5})
    assert (first_id, True) == (second_id, created)
    assert created_again is False
    assert len(store.retryable_notifications()) == 1
    store.mark_notification(first_id, status="sent", response_code=204)
    assert store.retryable_notifications() == []
    assert scalar(store, "SELECT count(*) FROM ev_evaluations") == 1
    assert scalar(store, "SELECT count(*) FROM notifications") == 1


def test_monitor_state_survives_restart(store):
    payload = {"venue": "東京", "race_num": 1, "url": "https://example.invalid/race"}
    store.save_monitor_state("東京_1", payload, race_id="r1",
                             start_time=datetime(2026, 7, 11, 6, tzinfo=timezone.utc), checked15=True)
    restored = store.active_monitors()
    assert restored[0]["monitor_key"] == "東京_1"
    assert restored[0]["checked15"] is True
    assert restored[0]["payload"] == payload
    store.save_monitor_state("東京_1", payload, race_id="r1", finished=True)
    assert store.active_monitors() == []


def test_win5_plan_is_idempotent_and_traceable(store):
    kwargs = dict(
        prediction_run_ids=["run1", "run2", "run3", "run4", "run5"],
        race_ids=["r1", "r2", "r3", "r4", "r5"], budget=100, total_points=72,
        selections=[{"race_id": "r1", "horse_numbers": [1, 2]}], coverage=[0.7] * 5,
        estimated_hit_rate=0.08, allocation_method="prob", allocation_version=4,
        single_axis=False, axis_index=None, idempotency_key="plan-key")
    first = store.save_win5_prediction(**kwargs)
    second = store.save_win5_prediction(**kwargs)
    assert first == second
    assert scalar(store, "SELECT count(*) FROM win5_predictions") == 1
