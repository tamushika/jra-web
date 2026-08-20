"""Append-only SQLite storage for prediction and odds snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_DB_PATH = Path(os.environ.get("JRA_LOG_DB", Path(__file__).parents[1] / "data" / "jra_logging.db"))
_SECRET_KEY = re.compile(r"(token|secret|password|webhook|database_url|authorization)", re.I)
JST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_runs (
    prediction_run_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    app_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    model_name TEXT,
    model_version TEXT,
    config_hash TEXT,
    git_commit TEXT,
    data_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    error_summary TEXT
);
CREATE TABLE IF NOT EXISTS predictions (
    prediction_run_id TEXT NOT NULL REFERENCES prediction_runs(prediction_run_id),
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    web_score REAL,
    win5_score REAL,
    ml_score REAL,
    raw_win_probability REAL,
    calibrated_win_probability REAL,
    place_probability REAL,
    confidence REAL,
    score_details_json TEXT,
    feature_snapshot_json TEXT,
    data_quality_flags_json TEXT,
    PRIMARY KEY (prediction_run_id, race_id, horse_id)
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
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
    stage TEXT,
    scheduled_post_at TEXT,
    seconds_to_post REAL,
    fetch_duration_ms INTEGER,
    valid_odds_count INTEGER,
    field_size INTEGER
);
CREATE INDEX IF NOT EXISTS ix_predictions_race ON predictions(race_id, horse_id, predicted_at);
CREATE INDEX IF NOT EXISTS ix_odds_race ON odds_snapshots(race_id, horse_id, observed_at);
CREATE TABLE IF NOT EXISTS ev_evaluations (
    ev_evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    prediction_run_id TEXT NOT NULL REFERENCES prediction_runs(prediction_run_id),
    odds_snapshot_id INTEGER NOT NULL REFERENCES odds_snapshots(odds_snapshot_id),
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    win_probability REAL,
    ev REAL,
    threshold REAL NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('alert','watch','skip')),
    reason_json TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    ev_evaluation_id INTEGER NOT NULL REFERENCES ev_evaluations(ev_evaluation_id),
    channel TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','suppressed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    response_code INTEGER,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_retry ON notifications(status, attempt_count, requested_at);
CREATE TABLE IF NOT EXISTS monitored_races (
    monitor_key TEXT PRIMARY KEY,
    race_id TEXT,
    start_time TEXT,
    payload_json TEXT NOT NULL,
    checked15 INTEGER NOT NULL DEFAULT 0,
    checked5 INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('active','finished')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS race_results (
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    horse_name TEXT,
    finish_position INTEGER,
    official_status TEXT NOT NULL CHECK (official_status IN ('unofficial','official','corrected','cancelled')),
    final_win_odds REAL,
    win_payout INTEGER,
    place_payout INTEGER,
    result_fetched_at TEXT NOT NULL,
    source_url TEXT,
    source_hash TEXT NOT NULL,
    data_quality_flags_json TEXT,
    PRIMARY KEY (race_id, horse_id)
);
CREATE TABLE IF NOT EXISTS race_result_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    horse_name TEXT,
    finish_position INTEGER,
    official_status TEXT NOT NULL,
    final_win_odds REAL,
    win_payout INTEGER,
    place_payout INTEGER,
    result_fetched_at TEXT NOT NULL,
    source_url TEXT,
    source_hash TEXT NOT NULL,
    data_quality_flags_json TEXT,
    UNIQUE (race_id, horse_id, source_hash)
);
CREATE INDEX IF NOT EXISTS ix_results_fetched ON race_results(result_fetched_at, official_status);
CREATE TABLE IF NOT EXISTS win5_predictions (
    win5_prediction_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    prediction_run_ids_json TEXT NOT NULL,
    race_ids_json TEXT NOT NULL,
    budget INTEGER NOT NULL,
    total_points INTEGER NOT NULL,
    selections_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    estimated_hit_rate REAL,
    allocation_method TEXT NOT NULL,
    allocation_version TEXT,
    single_axis INTEGER NOT NULL DEFAULT 0,
    axis_index INTEGER,
    status TEXT NOT NULL CHECK (status IN ('proposed','skipped'))
);
CREATE TABLE IF NOT EXISTS races (
    race_id TEXT PRIMARY KEY,
    race_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    race_name TEXT,
    surface TEXT,
    distance_m INTEGER,
    race_class TEXT,
    start_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_races_filters ON races(race_date, venue, distance_m);
CREATE TABLE IF NOT EXISTS win5_results (
    race_date TEXT PRIMARY KEY,
    payout_yen INTEGER,
    hit_ticket_count INTEGER,
    carryover_flag INTEGER NOT NULL DEFAULT 0,
    carryover_amount INTEGER,
    winning_numbers_json TEXT,
    fetched_at TEXT NOT NULL,
    source_url TEXT,
    source_hash TEXT
);
CREATE TABLE IF NOT EXISTS board_odds_snapshots (
    board_odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    race_id TEXT NOT NULL,
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    bet_type TEXT NOT NULL CHECK (bet_type IN ('place','wide','umaren')),
    combo TEXT NOT NULL,
    model_probability REAL,
    fair_odds REAL,
    odds REAL,
    odds_low REAL,
    odds_high REAL,
    gap_ratio REAL,
    requested_at TEXT,
    received_at TEXT,
    source TEXT NOT NULL,
    stage TEXT,
    fetch_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok','fetch_failed','book_outside','unavailable')),
    data_quality_flags_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_board_odds_race_stage
    ON board_odds_snapshots(race_id, stage, bet_type, combo);
CREATE TABLE IF NOT EXISTS race_confidence_snapshots (
    race_confidence_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    cutoff_at TEXT,
    snapshot_source TEXT NOT NULL,
    model_probs_json TEXT,
    market_odds_json TEXT,
    features_json TEXT,
    score REAL,
    threshold REAL,
    selected INTEGER CHECK (selected IN (0,1) OR selected IS NULL),
    manifest_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_race_confidence_date_race
    ON race_confidence_snapshots(date, place, r);
CREATE TABLE IF NOT EXISTS virtual_bets (
    virtual_bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    policy_version TEXT NOT NULL,
    race_id TEXT NOT NULL,
    date TEXT NOT NULL,
    bet_type TEXT NOT NULL CHECK (bet_type IN ('fukusho','tansho')),
    horse_number INTEGER NOT NULL,
    stake_yen INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    cutoff_source TEXT,
    decision_odds REAL,
    decision_prob REAL,
    is_control INTEGER NOT NULL DEFAULT 0 CHECK (is_control IN (0,1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','settled','refunded','skipped_budget','skipped_data')),
    payout_yen INTEGER,
    settled_at TEXT,
    created_at TEXT NOT NULL,
    -- T70: 結果由来列 (payout_yen/settled_at) の事前混入をスキーマレベルで拒否。
    -- decided時点 (pending/skipped_budget/skipped_data) はpayout=NULL必須。settled/
    -- refundedに遷移するのは精算 (settle_pending_bets) のUPDATEのみで、payoutが必ず入る。
    -- T70b: skipped_data (P3のboard未取得・7頭以下等、データが無くベットできず
    -- 記録だけ残す行) はskipped_budgetと同じくpayout=NULL側に属する。
    CHECK (
        (status IN ('pending','skipped_budget','skipped_data') AND payout_yen IS NULL AND settled_at IS NULL)
        OR (status IN ('settled','refunded') AND payout_yen IS NOT NULL AND settled_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_virtual_bets_date ON virtual_bets(date, race_id);
CREATE INDEX IF NOT EXISTS ix_virtual_bets_status ON virtual_bets(status);
"""

# T70b: SQLiteのCHECK制約は列挙値を後から追加できないため、既存DBの virtual_bets
# は「新テーブル作成→全行コピー→旧テーブル破棄」で再構築して 'skipped_data' を
# 受理させる (SPEC-T70b §4)。列定義・列順は上のSCHEMA内CREATE TABLEと完全一致
# させ、SELECT * での列位置ずれによるデータ破損を防ぐ。既存行の値・意味論
# (pending/settled/refunded/skipped_budget) は一切変更しない。
_VIRTUAL_BETS_V13_REBUILD_SQL = """
ALTER TABLE virtual_bets RENAME TO virtual_bets_pre_v13;
CREATE TABLE virtual_bets (
    virtual_bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    policy_version TEXT NOT NULL,
    race_id TEXT NOT NULL,
    date TEXT NOT NULL,
    bet_type TEXT NOT NULL CHECK (bet_type IN ('fukusho','tansho')),
    horse_number INTEGER NOT NULL,
    stake_yen INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    cutoff_source TEXT,
    decision_odds REAL,
    decision_prob REAL,
    is_control INTEGER NOT NULL DEFAULT 0 CHECK (is_control IN (0,1)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','settled','refunded','skipped_budget','skipped_data')),
    payout_yen INTEGER,
    settled_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (status IN ('pending','skipped_budget','skipped_data') AND payout_yen IS NULL AND settled_at IS NULL)
        OR (status IN ('settled','refunded') AND payout_yen IS NOT NULL AND settled_at IS NOT NULL)
    )
);
INSERT INTO virtual_bets (
    virtual_bet_id, idempotency_key, policy_version, race_id, date, bet_type,
    horse_number, stake_yen, decided_at, cutoff_source, decision_odds, decision_prob,
    is_control, status, payout_yen, settled_at, created_at)
    SELECT
    virtual_bet_id, idempotency_key, policy_version, race_id, date, bet_type,
    horse_number, stake_yen, decided_at, cutoff_source, decision_odds, decision_prob,
    is_control, status, payout_yen, settled_at, created_at
    FROM virtual_bets_pre_v13;
DROP TABLE virtual_bets_pre_v13;
CREATE INDEX IF NOT EXISTS ix_virtual_bets_date ON virtual_bets(date, race_id);
CREATE INDEX IF NOT EXISTS ix_virtual_bets_status ON virtual_bets(status);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _jst(value: datetime | str | None) -> str | None:
    """Return an ISO timestamp with an explicit JST offset.

    Race post times are published in JST.  Treating a naive post time as the
    host's local timezone would make archived snapshots machine-dependent.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(JST).isoformat(timespec="microseconds")


def _clean(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(str(key)):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _clean(v, str(k)) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    return str(value)


def stable_json(value: Any) -> str:
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def config_hash(config: Any) -> str:
    return hashlib.sha256(stable_json(config).encode("utf-8")).hexdigest()


def git_commit(repo_dir: str | Path | None = None) -> str | None:
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={Path(repo_dir or Path(__file__).parents[1]).resolve().as_posix()}",
             "-C", str(repo_dir or Path(__file__).parents[1]), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=2,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class LoggingStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, *, busy_timeout_ms: int = 1500, retries: int = 2):
        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.retries = retries

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, callback):
        for attempt in range(self.retries + 1):
            try:
                with self._connect() as conn:
                    return callback(conn)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= self.retries:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def initialize(self) -> None:
        def op(conn):
            conn.executescript(SCHEMA)
            # 既存DBには CREATE TABLE IF NOT EXISTS の列定義が反映されない。
            # INSERTは列名明示のまま、nullable列だけを後方互換で追加する。
            cols = {row[1] for row in conn.execute("PRAGMA table_info(odds_snapshots)")}
            odds_columns = {
                "stage": "TEXT",
                "scheduled_post_at": "TEXT",
                "seconds_to_post": "REAL",
                "fetch_duration_ms": "INTEGER",
                "valid_odds_count": "INTEGER",
                "field_size": "INTEGER",
            }
            for name, sql_type in odds_columns.items():
                if name not in cols:
                    conn.execute(
                        f"ALTER TABLE odds_snapshots ADD COLUMN {name} {sql_type}")
            prediction_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(predictions)")
            }
            if "place_probability" not in prediction_cols:
                conn.execute(
                    "ALTER TABLE predictions ADD COLUMN place_probability REAL")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(7, ?)", (utc_now(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(8, ?)", (utc_now(),))
            # version 9 (T55): win5_results table (created above via CREATE TABLE IF NOT EXISTS,
            # so this migration is idempotent on both fresh and existing DBs).
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(9, ?)", (utc_now(),))
            # version 10 (T59d): prospective-ready ticket board snapshots.
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(10, ?)", (utc_now(),))
            # version 11 (T62b): display-only prospective race confidence snapshots.
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(11, ?)", (utc_now(),))
            # version 12 (T70): virtual betting harness (paper-trading ledger, append-only).
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(12, ?)", (utc_now(),))
            # version 13 (T70b): virtual_bets.status CHECK gains 'skipped_data' (P5/P3
            # estimation-only patterns). Rebuild is skipped when the table already
            # accepts skipped_data (fresh DBs get it straight from SCHEMA above), so
            # this is safe to run on every initialize() call.
            existing_virtual_bets_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='virtual_bets'"
            ).fetchone()
            if existing_virtual_bets_sql and "skipped_data" not in existing_virtual_bets_sql[0]:
                conn.executescript(_VIRTUAL_BETS_V13_REBUILD_SQL)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(13, ?)", (utc_now(),))
        self._write(op)

    def start_run(self, *, app_name: str, trigger_type: str = "manual", model_name: str | None = None,
                  model_version: str | int | None = None, config: Any = None, data_version: str | None = None,
                  idempotency_key: str | None = None, started_at: datetime | str | None = None) -> str:
        self.initialize()
        run_id = str(uuid.uuid4())
        params = (run_id, idempotency_key, app_name, trigger_type, _utc(started_at) or utc_now(), model_name,
                  str(model_version) if model_version is not None else None,
                  config_hash(config) if config is not None else None, git_commit(), data_version, "running")
        def op(conn):
            conn.execute("""INSERT OR IGNORE INTO prediction_runs
                (prediction_run_id,idempotency_key,app_name,trigger_type,started_at,model_name,model_version,
                 config_hash,git_commit,data_version,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", params)
            if idempotency_key:
                row = conn.execute("SELECT prediction_run_id FROM prediction_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                return row[0]
            return run_id
        return self._write(op)

    def finish_run(self, run_id: str, *, status: str = "success", error_summary: str | None = None) -> None:
        safe_error = stable_json({"error": error_summary}) if error_summary else None
        self._write(lambda conn: conn.execute(
            "UPDATE prediction_runs SET completed_at=?, status=?, error_summary=? WHERE prediction_run_id=?",
            (utc_now(), status, safe_error, run_id)))

    def save_predictions(self, run_id: str, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for row in rows:
            values.append((run_id, str(row["race_id"]), str(row["horse_id"]), _utc(row.get("predicted_at")) or utc_now(),
                row.get("web_score"), row.get("win5_score"), row.get("ml_score"), row.get("raw_win_probability"),
                row.get("calibrated_win_probability"), row.get("place_probability"), row.get("confidence"),
                stable_json(row.get("score_details", {})),
                stable_json(row.get("feature_snapshot", {})), stable_json(row.get("data_quality_flags", []))))
        def op(conn):
            before = conn.total_changes
            conn.executemany("""INSERT OR IGNORE INTO predictions
                (prediction_run_id,race_id,horse_id,predicted_at,web_score,win5_score,ml_score,
                 raw_win_probability,calibrated_win_probability,place_probability,confidence,
                 score_details_json,feature_snapshot_json,data_quality_flags_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            return conn.total_changes - before
        return self._write(op)

    def save_odds(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for row in rows:
            observed = _utc(row.get("observed_at")) or utc_now()
            key = row.get("idempotency_key") or config_hash({"race_id": row["race_id"], "horse_id": row["horse_id"],
                                                               "observed_at": observed, "source": row.get("source", "jra")})
            values.append((key, str(row["race_id"]), str(row["horse_id"]), observed, _utc(row.get("source_updated_at")),
                row.get("win_odds"), row.get("place_odds_low"), row.get("place_odds_high"), row.get("popularity"),
                row.get("source", "jra"), row.get("fetch_id"), int(bool(row.get("is_stale"))),
                stable_json(row.get("data_quality_flags", [])),
                str(row["stage"]) if row.get("stage") is not None else None,
                _jst(row.get("scheduled_post_at")), row.get("seconds_to_post"),
                row.get("fetch_duration_ms"), row.get("valid_odds_count"),
                row.get("field_size")))
        def op(conn):
            before = conn.total_changes
            conn.executemany("""INSERT OR IGNORE INTO odds_snapshots
                (idempotency_key,race_id,horse_id,observed_at,source_updated_at,win_odds,place_odds_low,
                 place_odds_high,popularity,source,fetch_id,is_stale,data_quality_flags_json,stage,
                 scheduled_post_at,seconds_to_post,fetch_duration_ms,valid_odds_count,field_size)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            return conn.total_changes - before
        return self._write(op)

    def save_board_odds(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Append one display-only ticket-board capture without stale fallback."""
        self.initialize()
        values = []
        for row in rows:
            received = _utc(row.get("received_at"))
            requested = _utc(row.get("requested_at"))
            key = row.get("idempotency_key") or config_hash({
                "race_id": row["race_id"], "stage": row.get("stage"),
                "bet_type": row["bet_type"], "combo": row["combo"],
                "requested_at": requested, "fetch_id": row.get("fetch_id"),
            })
            values.append((
                key, str(row["race_id"]), str(row["date"]), str(row["place"]),
                int(row["r"]), str(row["bet_type"]), str(row["combo"]),
                row.get("model_probability"), row.get("fair_odds"), row.get("odds"),
                row.get("odds_low"), row.get("odds_high"), row.get("gap_ratio"),
                requested, received, row.get("source", "jra_official"),
                str(row["stage"]) if row.get("stage") is not None else None,
                row.get("fetch_id"), row.get("status", "ok"),
                stable_json(row.get("data_quality_flags", [])),
            ))
        def op(conn):
            before = conn.total_changes
            conn.executemany("""INSERT OR IGNORE INTO board_odds_snapshots
                (idempotency_key,race_id,date,place,r,bet_type,combo,model_probability,
                 fair_odds,odds,odds_low,odds_high,gap_ratio,requested_at,received_at,source,
                 stage,fetch_id,status,data_quality_flags_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            return conn.total_changes - before
        return self._write(op)

    def save_race_confidence(self, row: Mapping[str, Any]) -> int:
        """Append one as-of T62b row; outcome and final-price fields are not accepted."""
        forbidden = {"winner", "winner_id", "finish_position", "final_win_odds", "result"}
        overlap = forbidden.intersection(row)
        if overlap:
            raise ValueError(f"post-race fields are forbidden: {sorted(overlap)}")
        self.initialize()
        values = (
            str(row["date"]), str(row["place"]), int(row["r"]),
            _utc(row.get("cutoff_at")), str(row.get("snapshot_source") or "unknown"),
            stable_json(row["model_probs"]) if row.get("model_probs") is not None else None,
            stable_json(row["market_odds"]) if row.get("market_odds") is not None else None,
            stable_json(row["features"]) if row.get("features") is not None else None,
            row.get("score"), row.get("threshold"),
            None if row.get("selected") is None else int(bool(row.get("selected"))),
            row.get("manifest_sha256"), _utc(row.get("created_at")) or utc_now(),
        )
        def op(conn):
            cursor = conn.execute("""INSERT INTO race_confidence_snapshots
                (date,place,r,cutoff_at,snapshot_source,model_probs_json,market_odds_json,
                 features_json,score,threshold,selected,manifest_sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            return int(cursor.lastrowid)
        return self._write(op)

    def save_ev_evaluation(self, *, prediction_run_id: str, race_id: str, horse_id: str,
                           win_probability: float | None, ev: float | None, threshold: float,
                           decision: str, reason: Any = None, idempotency_key: str) -> int:
        """Persist one EV decision and return its stable row id."""
        self.initialize()
        def op(conn):
            odds = conn.execute(
                """SELECT odds_snapshot_id FROM odds_snapshots
                   WHERE race_id=? AND horse_id=? AND fetch_id=?
                   ORDER BY odds_snapshot_id DESC LIMIT 1""",
                (race_id, horse_id, prediction_run_id),
            ).fetchone()
            if odds is None:
                raise ValueError("odds snapshot not found for EV evaluation")
            conn.execute("""INSERT OR IGNORE INTO ev_evaluations
                (idempotency_key,prediction_run_id,odds_snapshot_id,race_id,horse_id,evaluated_at,
                 win_probability,ev,threshold,decision,reason_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (idempotency_key, prediction_run_id, odds[0], race_id, horse_id, utc_now(),
                 win_probability, ev, threshold, decision, stable_json(reason or {})))
            return conn.execute(
                "SELECT ev_evaluation_id FROM ev_evaluations WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()[0]
        return self._write(op)

    def reserve_notification(self, *, ev_evaluation_id: int, channel: str,
                             dedupe_key: str, payload: Any) -> tuple[str, bool]:
        """Atomically reserve a notification; created=False means it was already handled."""
        notification_id = str(uuid.uuid4())
        def op(conn):
            before = conn.total_changes
            conn.execute("""INSERT OR IGNORE INTO notifications
                (notification_id,ev_evaluation_id,channel,dedupe_key,payload_json,requested_at,status)
                VALUES(?,?,?,?,?,?,'pending')""",
                (notification_id, ev_evaluation_id, channel, dedupe_key, stable_json(payload), utc_now()))
            created = conn.total_changes > before
            row = conn.execute(
                "SELECT notification_id FROM notifications WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            return row[0], created
        return self._write(op)

    def mark_notification(self, notification_id: str, *, status: str,
                          response_code: int | None = None, error_message: str | None = None) -> None:
        safe_error = stable_json({"error": error_message}) if error_message else None
        self._write(lambda conn: conn.execute("""UPDATE notifications
            SET status=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                attempt_count=attempt_count+1, response_code=?, error_message=?
            WHERE notification_id=?""",
            (status, status, utc_now(), response_code, safe_error, notification_id)))

    def retryable_notifications(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute("""SELECT notification_id,channel,payload_json,attempt_count
                FROM notifications WHERE status IN ('pending','failed') AND attempt_count < ?
                ORDER BY requested_at""", (max_attempts,)).fetchall()
        return [{"notification_id": row[0], "channel": row[1],
                 "payload": json.loads(row[2]), "attempt_count": row[3]} for row in rows]

    def save_monitor_state(self, monitor_key: str, payload: Any, *, race_id: str | None = None,
                           start_time: datetime | str | None = None, checked15: bool = False,
                           checked5: bool = False, finished: bool = False) -> None:
        self.initialize()
        self._write(lambda conn: conn.execute("""INSERT INTO monitored_races
            (monitor_key,race_id,start_time,payload_json,checked15,checked5,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(monitor_key) DO UPDATE SET
            race_id=excluded.race_id,start_time=excluded.start_time,payload_json=excluded.payload_json,
            checked15=excluded.checked15,checked5=excluded.checked5,status=excluded.status,updated_at=excluded.updated_at""",
            (monitor_key, race_id, _utc(start_time), stable_json(payload), int(checked15), int(checked5),
             "finished" if finished else "active", utc_now())))

    def active_monitors(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute("""SELECT monitor_key,payload_json,checked15,checked5,start_time
                FROM monitored_races WHERE status='active' ORDER BY start_time""").fetchall()
        return [{"monitor_key": row[0], "payload": json.loads(row[1]),
                 "checked15": bool(row[2]), "checked5": bool(row[3]), "start_time": row[4]} for row in rows]

    def save_race_results(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        """Upsert current results while retaining every changed official revision."""
        self.initialize()
        stats = {"inserted": 0, "updated": 0, "unchanged": 0}
        def op(conn):
            for row in rows:
                flags = stable_json(row.get("data_quality_flags", []))
                values = (str(row["race_id"]), str(row["horse_id"]), row.get("horse_name"),
                          row.get("finish_position"), row.get("official_status", "official"),
                          row.get("final_win_odds"), row.get("win_payout"), row.get("place_payout"),
                          _utc(row.get("result_fetched_at")) or utc_now(), row.get("source_url"),
                          str(row["source_hash"]), flags)
                existing = conn.execute(
                    "SELECT source_hash FROM race_results WHERE race_id=? AND horse_id=?", values[:2]
                ).fetchone()
                if existing and existing[0] == values[10]:
                    stats["unchanged"] += 1
                    continue
                conn.execute("""INSERT OR IGNORE INTO race_result_revisions
                    (race_id,horse_id,horse_name,finish_position,official_status,final_win_odds,
                     win_payout,place_payout,result_fetched_at,source_url,source_hash,data_quality_flags_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                conn.execute("""INSERT INTO race_results
                    (race_id,horse_id,horse_name,finish_position,official_status,final_win_odds,
                     win_payout,place_payout,result_fetched_at,source_url,source_hash,data_quality_flags_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(race_id,horse_id) DO UPDATE SET
                    horse_name=excluded.horse_name,finish_position=excluded.finish_position,
                    official_status=excluded.official_status,final_win_odds=excluded.final_win_odds,
                    win_payout=excluded.win_payout,place_payout=excluded.place_payout,
                    result_fetched_at=excluded.result_fetched_at,source_url=excluded.source_url,
                    source_hash=excluded.source_hash,data_quality_flags_json=excluded.data_quality_flags_json""", values)
                stats["updated" if existing else "inserted"] += 1
            return stats
        return self._write(op)

    def result_match_summary(self, race_id: str | None = None) -> dict[str, int]:
        """Count distinct predicted runners and those joined to an official result."""
        self.initialize()
        where = "WHERE p.race_id=?" if race_id else ""
        params = (race_id,) if race_id else ()
        with self._connect() as conn:
            predicted = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT DISTINCT race_id,horse_id FROM predictions p {where})", params
            ).fetchone()[0]
            matched = conn.execute(
                f"""SELECT COUNT(*) FROM (SELECT DISTINCT p.race_id,p.horse_id FROM predictions p
                    JOIN race_results r ON r.race_id=p.race_id AND r.horse_id=p.horse_id {where})""", params
            ).fetchone()[0]
        return {"predicted": predicted, "matched": matched, "unmatched": predicted - matched}

    def save_win5_prediction(self, *, prediction_run_ids: list[str], race_ids: list[str],
                             budget: int, total_points: int, selections: Any, coverage: Any,
                             estimated_hit_rate: float | None, allocation_method: str,
                             allocation_version: str | int | None, single_axis: bool,
                             axis_index: int | None, skipped: bool = False,
                             idempotency_key: str) -> str:
        self.initialize()
        win5_id = str(uuid.uuid4())
        def op(conn):
            conn.execute("""INSERT OR IGNORE INTO win5_predictions
                (win5_prediction_id,idempotency_key,created_at,prediction_run_ids_json,race_ids_json,
                 budget,total_points,selections_json,coverage_json,estimated_hit_rate,allocation_method,
                 allocation_version,single_axis,axis_index,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (win5_id, idempotency_key, utc_now(), stable_json(prediction_run_ids), stable_json(race_ids),
                 int(budget), int(total_points), stable_json(selections), stable_json(coverage),
                 estimated_hit_rate, allocation_method,
                 str(allocation_version) if allocation_version is not None else None,
                 int(single_axis), axis_index, "skipped" if skipped else "proposed"))
            return conn.execute(
                "SELECT win5_prediction_id FROM win5_predictions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()[0]
        return self._write(op)

    def save_win5_result(self, *, race_date: str, payout_yen: int | None, hit_ticket_count: int | None,
                         carryover_flag: bool, carryover_amount: int | None,
                         winning_numbers: list[int] | None, source_url: str | None,
                         source_hash: str | None, fetched_at: datetime | str | None = None) -> None:
        """Upsert one day's official WIN5 payout (T55). Called from the result-sync path only;
        the read-only dashboard never writes here."""
        self.initialize()
        values = (str(race_date), payout_yen, hit_ticket_count, int(bool(carryover_flag)), carryover_amount,
                  stable_json(winning_numbers) if winning_numbers is not None else None,
                  _utc(fetched_at) or utc_now(), source_url, source_hash)
        self._write(lambda conn: conn.execute("""INSERT INTO win5_results
            (race_date,payout_yen,hit_ticket_count,carryover_flag,carryover_amount,
             winning_numbers_json,fetched_at,source_url,source_hash)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(race_date) DO UPDATE SET
            payout_yen=excluded.payout_yen,hit_ticket_count=excluded.hit_ticket_count,
            carryover_flag=excluded.carryover_flag,carryover_amount=excluded.carryover_amount,
            winning_numbers_json=excluded.winning_numbers_json,fetched_at=excluded.fetched_at,
            source_url=excluded.source_url,source_hash=excluded.source_hash""", values))

    def get_win5_result(self, race_date: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM win5_results WHERE race_date=?", (str(race_date),)).fetchone()
        return dict(row) if row else None

    def save_race(self, *, race_id: str, race_date: str, venue: str, race_no: int,
                  race_name: str | None = None, surface: str | None = None,
                  distance_m: int | None = None, race_class: str | None = None,
                  start_time: datetime | str | None = None) -> None:
        self.initialize()
        now = utc_now()
        self._write(lambda conn: conn.execute("""INSERT INTO races
            (race_id,race_date,venue,race_no,race_name,surface,distance_m,race_class,start_time,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(race_id) DO UPDATE SET
            race_date=excluded.race_date,venue=excluded.venue,race_no=excluded.race_no,
            race_name=COALESCE(excluded.race_name,races.race_name),
            surface=COALESCE(excluded.surface,races.surface),
            distance_m=COALESCE(excluded.distance_m,races.distance_m),
            race_class=COALESCE(excluded.race_class,races.race_class),
            start_time=COALESCE(excluded.start_time,races.start_time),updated_at=excluded.updated_at""",
            (race_id, race_date, venue, int(race_no), race_name, surface, distance_m, race_class,
             _utc(start_time), now, now)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the JRA logging database")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    store = LoggingStore(args.db)
    store.initialize()
    print(f"initialized: {store.db_path}")


if __name__ == "__main__":
    main()
