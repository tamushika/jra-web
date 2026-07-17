import csv
import sqlite3
from datetime import datetime, timedelta, timezone

from api.logging_store import LoggingStore
from audit_odds_snapshots import audit_snapshots, open_readonly, write_report


JST = timezone(timedelta(hours=9))


def _snapshot_rows(*, race_id, stage, observed_at, scheduled_post_at,
                   field_size, valid_odds_count, fetch_id):
    seconds_to_post = (scheduled_post_at - observed_at).total_seconds()
    return [{
        "idempotency_key": f"{fetch_id}:{horse_no}",
        "race_id": race_id,
        "horse_id": f"{race_id}:{horse_no:02d}",
        "observed_at": observed_at,
        "scheduled_post_at": scheduled_post_at,
        "seconds_to_post": seconds_to_post,
        "fetch_duration_ms": 100 + horse_no,
        "valid_odds_count": valid_odds_count,
        "field_size": field_size,
        "win_odds": 2.0 + horse_no if horse_no <= valid_odds_count else None,
        "stage": stage,
        "fetch_id": fetch_id,
        "data_quality_flags": (
            ["win_odds_unavailable"] if horse_no > valid_odds_count else []
        ),
    } for horse_no in range(1, field_size + 1)]


def test_audit_aggregates_fetches_and_preserves_source_database(tmp_path):
    db_path = tmp_path / "logging.db"
    store = LoggingStore(db_path)
    store.initialize()
    for race_no in (1, 2, 3):
        store.save_race(
            race_id=f"20260717:tokyo:{race_no:02d}",
            race_date="20260717",
            venue="tokyo",
            race_no=race_no,
            start_time=datetime(2026, 7, 17, 12, 0, tzinfo=JST),
        )

    race1 = "20260717:tokyo:01"
    store.save_odds(_snapshot_rows(
        race_id=race1,
        stage=30,
        observed_at=datetime(2026, 7, 17, 11, 30, tzinfo=JST),
        scheduled_post_at=datetime(2026, 7, 17, 12, 0, tzinfo=JST),
        field_size=4,
        valid_odds_count=4,
        fetch_id="r1-stage30",
    ))
    # Thirty seconds later, a different stage and changed post time constitute
    # both a catch-up burst and a suspected post-time change.
    store.save_odds(_snapshot_rows(
        race_id=race1,
        stage=10,
        observed_at=datetime(2026, 7, 17, 11, 30, 30, tzinfo=JST),
        scheduled_post_at=datetime(2026, 7, 17, 12, 1, tzinfo=JST),
        field_size=4,
        valid_odds_count=4,
        fetch_id="r1-stage10",
    ))
    store.save_odds(_snapshot_rows(
        race_id="20260717:tokyo:02",
        stage=2,
        observed_at=datetime(2026, 7, 17, 11, 58, tzinfo=JST),
        scheduled_post_at=datetime(2026, 7, 17, 12, 0, tzinfo=JST),
        field_size=8,
        valid_odds_count=1,
        fetch_id="r2-stage2",
    ))

    before = db_path.read_bytes()
    with open_readonly(db_path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        report = audit_snapshots(conn)
    after = db_path.read_bytes()

    assert before == after
    assert report["source_rows"] == 16
    assert report["snapshot_fetches"] == 3
    assert report["candidate_races"] == 3
    assert len(report["catchup_bursts"]) == 1
    assert report["catchup_bursts"][0]["gap_seconds"] == 30
    assert len(report["post_time_changes"]) == 1
    assert report["post_time_changes"][0]["change_seconds"] == 60

    summary = {row["stage"]: row for row in report["stage_summary"]}
    assert summary["30"]["captured_races"] == 1
    assert summary["30"]["expected_races"] == 3
    assert summary["30"]["within_window_rate_pct"] == 100.0
    assert summary["30"]["clean_races"] == 1
    assert summary["10"]["within_window_rate_pct"] == 0.0
    assert summary["10"]["clean_races"] == 0
    assert summary["2"]["within_window_rate_pct"] == 100.0
    assert summary["2"]["clean_races"] == 0

    stage2 = next(item for item in report["snapshots"] if item["stage"] == "2")
    assert stage2["valid_odds_count"] == 1
    assert stage2["field_size"] == 8
    assert "insufficient_odds" in stage2["flags"]

    paths = write_report(report, tmp_path / "outputs" / "t40")
    assert all(path.exists() for path in paths.values())
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "DBは read-only/query-only" in markdown
    assert "catchup pair: **1**" in markdown
    with paths["stage_summary"].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["stage"] for row in rows] == ["30", "10", "2"]


def test_old_schema_uses_race_start_time_and_reports_unrecoverable(tmp_path):
    db_path = tmp_path / "legacy.db"
    store = LoggingStore(db_path)
    store.initialize()
    store.save_race(
        race_id="20260717:tokyo:01",
        race_date="20260717",
        venue="tokyo",
        race_no=1,
        start_time=datetime(2026, 7, 17, 12, 0, tzinfo=JST),
    )
    store.save_odds([{
        "idempotency_key": "legacy-with-fallback",
        "race_id": "20260717:tokyo:01",
        "horse_id": "h1",
        "observed_at": datetime(2026, 7, 17, 11, 30, tzinfo=JST),
        "stage": 30,
        "win_odds": 2.5,
    }, {
        "idempotency_key": "legacy-unrecoverable",
        "race_id": "unknown-race",
        "horse_id": "h1",
        "observed_at": datetime(2026, 7, 17, 11, 30, tzinfo=JST),
        "stage": 10,
        "win_odds": 3.5,
    }])

    with open_readonly(db_path) as conn:
        report = audit_snapshots(conn)
    snapshots = {(row["race_id"], row["stage"]): row for row in report["snapshots"]}
    recovered = snapshots[("20260717:tokyo:01", "30")]
    unknown = snapshots[("unknown-race", "10")]
    assert recovered["scheduled_post_source"] == "races"
    assert recovered["seconds_to_post"] == 1800
    assert unknown["scheduled_post_source"] == "unrecoverable"
    assert unknown["seconds_to_post"] is None
    summary = {row["stage"]: row for row in report["stage_summary"]}
    assert summary["10"]["unrecoverable_fetches"] == 1


def test_audit_reads_physical_pre_t40_schema_without_initializing(tmp_path):
    """The auditor must not depend on LoggingStore's migration side effect."""
    db_path = tmp_path / "physical-pre-t40.db"
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
            data_quality_flags_json TEXT,
            stage TEXT
        )""")
        conn.execute("CREATE TABLE races (race_id TEXT PRIMARY KEY, start_time TEXT)")
        conn.execute(
            "INSERT INTO races(race_id,start_time) VALUES(?,?)",
            ("20260717:tokyo:01", "2026-07-17T12:00:00+09:00"),
        )
        legacy_rows = []
        for horse_no in range(1, 5):
            legacy_rows.append((
                f"recoverable:{horse_no}", "20260717:tokyo:01", f"h{horse_no}",
                "2026-07-17T02:30:00Z", 2.0 + horse_no,
                "recoverable-fetch", "[]", "30",
            ))
        legacy_rows.append((
            "unrecoverable:1", "unknown-race", "h1",
            "2026-07-17T02:30:00Z", 3.5,
            "unrecoverable-fetch", "[]", "10",
        ))
        conn.executemany("""INSERT INTO odds_snapshots
            (idempotency_key,race_id,horse_id,observed_at,win_odds,source,
             fetch_id,data_quality_flags_json,stage)
            VALUES(?,?,?,?,?,'jra',?,?,?)""", legacy_rows)

    with open_readonly(db_path) as conn:
        report = audit_snapshots(conn)

    assert report["source_rows"] == 5
    assert report["snapshot_fetches"] == 2
    snapshots = {(row["race_id"], row["stage"]): row for row in report["snapshots"]}
    recovered = snapshots[("20260717:tokyo:01", "30")]
    unknown = snapshots[("unknown-race", "10")]
    assert recovered["scheduled_post_source"] == "races"
    assert recovered["seconds_to_post"] == 1800
    assert recovered["valid_odds_count"] == 4
    assert unknown["scheduled_post_source"] == "unrecoverable"
    assert unknown["seconds_to_post"] is None
    summary = {row["stage"]: row for row in report["stage_summary"]}
    assert summary["30"]["reconstructable_fetches"] == 1
    assert summary["30"]["unrecoverable_fetches"] == 0
    assert summary["10"]["reconstructable_fetches"] == 0
    assert summary["10"]["unrecoverable_fetches"] == 1


def test_legacy_valid_odds_count_rejects_boundaries_and_non_finite_values(tmp_path):
    db_path = tmp_path / "odds-boundaries.db"
    store = LoggingStore(db_path)
    store.initialize()
    values = [1.0, 1.01, 998.99, 999.0, float("inf"), float("nan"), None]
    store.save_odds([{
        "idempotency_key": f"boundary:{index}",
        "race_id": "20260717:tokyo:01",
        "horse_id": f"h{index}",
        "observed_at": datetime(2026, 7, 17, 11, 30, tzinfo=JST),
        "stage": 30,
        "fetch_id": "boundary-fetch",
        "win_odds": value,
    } for index, value in enumerate(values)])

    with open_readonly(db_path) as conn:
        report = audit_snapshots(conn)
    assert report["snapshot_fetches"] == 1
    assert report["snapshots"][0]["field_size"] == 7
    assert report["snapshots"][0]["valid_odds_count"] == 2


def test_malformed_quality_flags_are_never_in_clean_subset(tmp_path):
    db_path = tmp_path / "malformed-flags.db"
    store = LoggingStore(db_path)
    store.initialize()
    race_id = "20260717:tokyo:01"
    store.save_odds(_snapshot_rows(
        race_id=race_id,
        stage=30,
        observed_at=datetime(2026, 7, 17, 11, 30, tzinfo=JST),
        scheduled_post_at=datetime(2026, 7, 17, 12, 0, tzinfo=JST),
        field_size=4,
        valid_odds_count=4,
        fetch_id="malformed-fetch",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE odds_snapshots SET data_quality_flags_json='not-json' "
            "WHERE horse_id=?",
            (f"{race_id}:01",),
        )

    with open_readonly(db_path) as conn:
        report = audit_snapshots(conn)
    snapshot = report["snapshots"][0]
    assert snapshot["within_window"] is True
    assert snapshot["valid_odds_count"] == 4
    assert "invalid_quality_flags_json" in snapshot["flags"]
    assert snapshot["clean"] is False
