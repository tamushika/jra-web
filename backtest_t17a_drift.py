"""T17a Stage A: isolated harness for the odds-drift diagnostic (D3).

SPEC: docs/codex/SPEC-T17a-odds-drift-diagnostic.md (v3)
Review: docs/T17a-pre-audit-review.md

Stage A only. This module intentionally keeps three facilities apart so a
Stage-A-only run can never score real data:

  --audit-only   Read-only population / exclusion / time-point audit against
                 data/jra_logging.db (mode=ro, PRAGMA query_only=ON). Reports
                 counts, exclusion reasons, breakdowns, and the final race_id
                 set sha256. Never fits a model, never computes a log-loss or
                 a correlation on real rows.
  --smoke        Synthetic-data-only self test of the modelling pipeline
                 (market probabilities, race-conditional-logit fit, paired
                 block bootstrap, the SS3.5 machine judgment). Never opens
                 the real database.
  --run          Gated execution path. Refuses immediately (exit code 2)
                 unless the ledger (eval/experiments.jsonl, read-only) already
                 carries a registered experiment_id whose
                 data_hashes["frozen_extract_sha256"] matches the sha256 of
                 the frozen extract file named on the command line. Stage A
                 does not invoke this mode against real data; it exists so
                 the refusal behaviour is implemented and testable now.

A fourth, separate maintenance action, --freeze-extract, implements (but does
not, in Stage A, execute against the real database) the frozen-extraction
procedure described in SPEC SS1/SS3.3: a read-only export of
odds_snapshots (stage 30/10/2/15/5 rows), race_results, the stage-30-
equivalent predictions rows, and races, for a fixed race_id set, into a new
destination sqlite file. The source connection stays read-only; only the new
destination file is written.

No production, notification, EV, or virtual-purchase code is imported or
touched. No network access. No writes to eval/experiments.jsonl or its
verify-state file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "jra_logging.db"
LEDGER_PATH = REPO_ROOT / "eval" / "experiments.jsonl"
EXPERIMENT_ID = "T17a-odds-drift-diagnostic-v1"

# SPEC SS3.1 item 2: seconds_to_post window per stage, inclusive both ends.
STAGE_WINDOWS: dict[int, tuple[float, float]] = {
    30: (30 * 60 - 60, 30 * 60 + 120),
    10: (10 * 60 - 60, 10 * 60 + 120),
    2: (2 * 60 - 60, 2 * 60 + 120),
}
PRIMARY_STAGES = (30, 10, 2)
DESCRIPTIVE_STAGES = (15, 5)
ALL_EXTRACT_STAGES = (30, 15, 10, 5, 2)

FIELD_BANDS: tuple[tuple[str, int, int], ...] = (
    ("8-11", 8, 11),
    ("12-15", 12, 15),
    ("16+", 16, 10_000),
)
POPULARITY_BANDS: tuple[tuple[str, int, int], ...] = (
    ("1-3", 1, 3),
    ("4-8", 4, 8),
    ("9+", 9, 10_000),
)

# SPEC SS3.2 item (d): stage-30 <-> predictions association tolerance.
D_ASSOCIATION_TOLERANCE_SECONDS = 90.0

EXCLUSION_PRIORITY = (
    "acquisition_missing",
    "outside_window",
    "quality_flagged",
    "odds_incomplete",
    "result_incomplete",
)


class T17aError(Exception):
    """Base error for the T17a Stage A harness."""


class QualityError(T17aError):
    """Raised when the harness itself would otherwise violate its own contract."""


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_dt(value: str | None) -> datetime | None:
    """Parse a timezone-aware ISO-8601 timestamp; reject naive timestamps."""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualityError(f"timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc)


def read_only_connect(path: str | Path) -> sqlite3.Connection:
    """Open a sqlite3 database strictly read-only; verify no write is possible."""

    uri = f"file:{Path(path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        connection.execute("CREATE TABLE __t17a_write_probe (x INTEGER)")
    except sqlite3.OperationalError:
        pass
    else:
        connection.rollback()
        raise QualityError(f"connection to {path} accepted a write; not read-only")
    return connection


def field_band(field_size: int) -> str:
    for label, lo, hi in FIELD_BANDS:
        if lo <= field_size <= hi:
            return label
    raise QualityError(f"field_size {field_size} does not fall in any reporting band")


def popularity_band(rank: int) -> str:
    for label, lo, hi in POPULARITY_BANDS:
        if lo <= rank <= hi:
            return label
    raise QualityError(f"popularity rank {rank} does not fall in any reporting band")


def year_month(race_date: str) -> str:
    if len(race_date) != 8 or not race_date.isdigit():
        raise QualityError(f"race_date is not YYYYMMDD: {race_date!r}")
    return race_date[:6]


def final_set_sha256(race_ids: Iterable[str]) -> str:
    """Deterministic sha256 of a sorted, de-duplicated, newline-joined race_id set."""

    ordered = sorted(set(race_ids))
    return sha256_text("\n".join(ordered) + "\n")


# --------------------------------------------------------------------------
# SPEC SS3.1: population, per-stage capture selection, exclusion classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Capture:
    """One selected, quality-passing snapshot capture for one race/stage."""

    race_id: str
    stage: int
    observed_at: datetime
    scheduled_post_at: datetime
    seconds_to_post: float
    field_size: int
    horse_odds: dict[str, float]  # horse_id -> win_odds
    horse_umaban: dict[str, int]  # horse_id -> umaban (from horse_id suffix)


@dataclass
class RaceAudit:
    race_id: str
    race_date: str
    venue: str
    surface: str
    applicable: bool = True
    not_applicable_reason: str | None = None
    primary_exclusion: str | None = None
    all_flags: set[str] = field(default_factory=set)
    per_stage_field_size: dict[int, int] = field(default_factory=dict)
    captures: dict[int, Capture] = field(default_factory=dict)
    capture_selected: bool = False
    eligible: bool = False
    result_row_count: int | None = None
    time_assert_violations: list[str] = field(default_factory=list)


def _horse_umaban(horse_id: str) -> int:
    suffix = horse_id.rsplit(":", 1)[-1]
    if not suffix.isdigit():
        raise QualityError(f"horse_id has no numeric umaban suffix: {horse_id!r}")
    return int(suffix)


def _load_candidate_races(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """JRA flat races (surface in {turf, dirt}) with >=1 attempted capture at
    every primary stage. This is the structural population before quality or
    headcount filtering (SPEC SS3.1 item 1, pre-quality half)."""

    rows = connection.execute(
        """
        SELECT r.race_id, r.race_date, r.venue, r.surface
        FROM races r
        WHERE r.surface IN ('芝', 'ダート')
        """
    ).fetchall()
    candidates: dict[str, sqlite3.Row] = {row["race_id"]: row for row in rows}
    stage_present: dict[str, set[int]] = defaultdict(set)
    for row in connection.execute(
        "SELECT DISTINCT race_id, stage FROM odds_snapshots WHERE stage IN ('30','10','2')"
    ):
        stage_present[row["race_id"]].add(int(row["stage"]))
    return {
        race_id: row
        for race_id, row in candidates.items()
        if stage_present.get(race_id, set()) >= set(PRIMARY_STAGES)
    }


def _select_capture_for_stage(
    connection: sqlite3.Connection, race_id: str, stage: int
) -> tuple[Capture | None, str | None, set[str]]:
    """Apply SPEC SS3.1 item 2/3 for one race/stage.

    Returns (selected_capture_or_None, failure_reason_or_None, all_flags_seen).
    failure_reason is one of acquisition_missing / outside_window /
    quality_flagged / odds_incomplete, or None if a capture was selected.
    """

    rows = connection.execute(
        """
        SELECT race_id, horse_id, observed_at, scheduled_post_at, seconds_to_post,
               field_size, valid_odds_count, win_odds, data_quality_flags_json,
               is_stale
        FROM odds_snapshots
        WHERE race_id = ? AND stage = ?
        ORDER BY observed_at
        """,
        (race_id, str(stage)),
    ).fetchall()
    if not rows:
        return None, "acquisition_missing", set()

    all_flags: set[str] = set()
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[row["observed_at"]].append(row)
        flags = json.loads(row["data_quality_flags_json"] or "[]")
        all_flags.update(flags)
        if row["is_stale"]:
            all_flags.add("is_stale")

    lo, hi = STAGE_WINDOWS[stage]
    any_in_window = False
    any_unflagged_in_window = False
    best: tuple[float, list[sqlite3.Row]] | None = None
    for observed_at, group_rows in groups.items():
        seconds = {row["seconds_to_post"] for row in group_rows}
        if len(seconds) != 1 or next(iter(seconds)) is None:
            raise QualityError(
                f"inconsistent seconds_to_post within one capture: {race_id} stage={stage} at={observed_at}"
            )
        seconds_to_post = next(iter(seconds))
        if seconds_to_post <= 0:
            # SPEC SS3.1 item 3: unconditional post-race exclusion. Also always
            # outside the (positive-only) stage windows above, kept explicit
            # as a defence-in-depth check.
            continue
        if not (lo <= seconds_to_post <= hi):
            continue
        any_in_window = True
        group_flags: set[str] = set()
        for row in group_rows:
            group_flags.update(json.loads(row["data_quality_flags_json"] or "[]"))
            if row["is_stale"]:
                group_flags.add("is_stale")
        if group_flags:
            continue
        any_unflagged_in_window = True
        field_sizes = {row["field_size"] for row in group_rows}
        valid_counts = {row["valid_odds_count"] for row in group_rows}
        if len(field_sizes) != 1 or len(valid_counts) != 1:
            raise QualityError(
                f"inconsistent field_size/valid_odds_count within one capture: {race_id} stage={stage}"
            )
        f_size = next(iter(field_sizes))
        v_count = next(iter(valid_counts))
        if f_size is None or v_count is None or v_count != f_size:
            continue
        if best is None or seconds_to_post < best[0]:
            best = (seconds_to_post, group_rows)

    if best is None:
        if not any_in_window:
            return None, "outside_window", all_flags
        if not any_unflagged_in_window:
            return None, "quality_flagged", all_flags
        return None, "odds_incomplete", all_flags

    seconds_to_post, group_rows = best
    observed_at = parse_dt(group_rows[0]["observed_at"])
    scheduled_post_at = parse_dt(group_rows[0]["scheduled_post_at"])
    if observed_at is None or scheduled_post_at is None:
        raise QualityError(f"selected capture missing timestamps: {race_id} stage={stage}")
    horse_odds: dict[str, float] = {}
    horse_umaban: dict[str, int] = {}
    for row in group_rows:
        odds = row["win_odds"]
        if odds is None or not math.isfinite(odds) or odds <= 1.0:
            return None, "odds_incomplete", all_flags
        horse_odds[row["horse_id"]] = float(odds)
        horse_umaban[row["horse_id"]] = _horse_umaban(row["horse_id"])
    if len(horse_odds) != len(group_rows):
        return None, "odds_incomplete", all_flags
    field_size = int(group_rows[0]["field_size"])
    capture = Capture(
        race_id=race_id,
        stage=stage,
        observed_at=observed_at,
        scheduled_post_at=scheduled_post_at,
        seconds_to_post=float(seconds_to_post),
        field_size=field_size,
        horse_odds=horse_odds,
        horse_umaban=horse_umaban,
    )
    return capture, None, all_flags


def _check_result_and_horse_set(
    connection: sqlite3.Connection, race_id: str, captures: Mapping[int, Capture], n: int
) -> tuple[bool, set[str], int | None]:
    """SPEC SS3.1 item 4. Returns (passed, flags, result_row_count).

    ``n`` is the already-validated, consistent field_size across the adopted
    30/10/2 captures (SPEC v3 SS3.6-1: the cross-stage consistency check
    itself happens in the caller, audit_one_race, before headcount
    applicability is decided, and is reported as its own odds_incomplete
    exclusion rather than folded into result_incomplete here).
    """

    flags: set[str] = set()

    rows = connection.execute(
        "SELECT horse_id, finish_position FROM race_results WHERE race_id = ?",
        (race_id,),
    ).fetchall()
    result_row_count = len(rows)
    if result_row_count != n:
        flags.add("result_row_count_mismatch")

    positions: list[int] = []
    result_horse_ids: set[str] = set()
    for row in rows:
        result_horse_ids.add(row["horse_id"])
        pos = row["finish_position"]
        if pos is None or not isinstance(pos, int):
            flags.add("finish_position_not_strict_int")
            continue
        positions.append(pos)
    if len(result_horse_ids) != len(rows):
        flags.add("result_horse_id_duplicate")
    if not flags.intersection({"finish_position_not_strict_int"}):
        if sorted(positions) != list(range(1, n + 1)):
            flags.add("finish_position_not_1_to_n")

    horse_sets = {stage: set(capture.horse_odds) for stage, capture in captures.items()}
    all_sets = list(horse_sets.values()) + [result_horse_ids]
    reference = all_sets[0]
    if any(s != reference for s in all_sets[1:]) or any(
        len(s) != n for s in all_sets
    ):
        flags.add("horse_set_mismatch")

    passed = not flags
    return passed, flags, result_row_count


def audit_one_race(
    connection: sqlite3.Connection, race_row: sqlite3.Row
) -> RaceAudit:
    race_id = race_row["race_id"]
    audit = RaceAudit(
        race_id=race_id,
        race_date=race_row["race_date"],
        venue=race_row["venue"],
        surface=race_row["surface"],
    )

    stage_failures: dict[int, str] = {}
    for stage in PRIMARY_STAGES:
        capture, failure, flags = _select_capture_for_stage(connection, race_id, stage)
        audit.all_flags.update(f"{stage}m:{flag}" for flag in flags)
        if capture is None:
            stage_failures[stage] = failure  # type: ignore[assignment]
            continue
        audit.captures[stage] = capture
        audit.per_stage_field_size[stage] = capture.field_size

    if stage_failures:
        for reason in EXCLUSION_PRIORITY:
            if reason in stage_failures.values():
                audit.primary_exclusion = reason
                break
        audit.all_flags.update(f"{stage}m:{reason}" for stage, reason in stage_failures.items())
        return audit

    audit.capture_selected = True

    # SPEC v3 SS3.6-1: headcount applicability and cross-stage field_size
    # consistency are judged strictly from the *adopted* 30/10/2 captures
    # selected above. A non-adopted attempt's field_size (e.g. a flagged or
    # out-of-window capture with a different headcount) must never enter
    # this decision.
    field_sizes = {capture.field_size for capture in audit.captures.values()}
    if len(field_sizes) != 1:
        audit.primary_exclusion = "odds_incomplete"
        audit.all_flags.add("field_size_mismatch_across_stages")
        return audit
    n = next(iter(field_sizes))

    if n < 8:
        audit.applicable = False
        audit.not_applicable_reason = "headcount_below_8_or_unknown"
        return audit

    passed, result_flags, result_row_count = _check_result_and_horse_set(
        connection, race_id, audit.captures, n
    )
    audit.result_row_count = result_row_count
    audit.all_flags.update(result_flags)
    if not passed:
        audit.primary_exclusion = "result_incomplete"
        return audit

    # SPEC SS3.3: information-timing asserts on the final adopted rows only.
    for stage, capture in audit.captures.items():
        if not (capture.observed_at < capture.scheduled_post_at):
            audit.time_assert_violations.append(
                f"stage {stage}: observed_at {capture.observed_at.isoformat()} "
                f"not before scheduled_post_at {capture.scheduled_post_at.isoformat()}"
            )
    result_fetched_rows = connection.execute(
        "SELECT DISTINCT result_fetched_at FROM race_results WHERE race_id = ?",
        (race_id,),
    ).fetchall()
    canonical_post = audit.captures[2].scheduled_post_at
    for row in result_fetched_rows:
        fetched_at = parse_dt(row["result_fetched_at"])
        if fetched_at is None or not (fetched_at > canonical_post):
            audit.time_assert_violations.append(
                f"result_fetched_at {row['result_fetched_at']!r} not after "
                f"scheduled_post_at {canonical_post.isoformat()}"
            )

    audit.eligible = not audit.time_assert_violations
    return audit


# --------------------------------------------------------------------------
# SPEC SS3.2 candidate (d): stage-30-equivalent prediction association
# --------------------------------------------------------------------------


def check_d_association(
    connection: sqlite3.Connection, audit: RaceAudit
) -> tuple[bool, str | None, dict[str, float] | None]:
    """Find the stage-30-equivalent prediction run for one eligible race.

    Returns (compatible, exclusion_reason_or_None, horse_id -> calibrated
    win probability for the matched run, or None).
    """

    if not audit.eligible or 30 not in audit.captures:
        return False, "not_in_primary_eligible_set", None
    t30 = audit.captures[30].observed_at
    lo = (t30 - _timedelta_seconds(D_ASSOCIATION_TOLERANCE_SECONDS)).isoformat()
    hi = (t30 + _timedelta_seconds(D_ASSOCIATION_TOLERANCE_SECONDS)).isoformat()
    rows = connection.execute(
        """
        SELECT horse_id, predicted_at, calibrated_win_probability
        FROM predictions
        WHERE race_id = ? AND predicted_at >= ? AND predicted_at <= ?
        """,
        (audit.race_id, lo, hi),
    ).fetchall()

    by_run: dict[str, dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        by_run[row["predicted_at"]][row["horse_id"]] = row["calibrated_win_probability"]

    horse_set = set(audit.captures[30].horse_odds)
    return resolve_d_association(t30, by_run, horse_set)


def resolve_d_association(
    t30: datetime,
    prediction_runs: Mapping[str, Mapping[str, float | None]],
    horse_set: set[str],
    *,
    tolerance_seconds: float = D_ASSOCIATION_TOLERANCE_SECONDS,
) -> tuple[bool, str | None, dict[str, float] | None]:
    """Pure SPEC SS3.2(d) association rule: pick the prediction run whose
    predicted_at is within tolerance_seconds of t30 and closest to it, among
    runs that cover exactly horse_set with finite positive probabilities.
    DB-free so it can be exercised directly by synthetic tests."""

    best_run: str | None = None
    best_gap: float | None = None
    for predicted_at, horse_probs in prediction_runs.items():
        predicted_dt = parse_dt(predicted_at) if isinstance(predicted_at, str) else predicted_at
        if predicted_dt is None:
            continue
        gap = abs((predicted_dt - t30).total_seconds())
        if gap > tolerance_seconds:
            continue
        if set(horse_probs) != horse_set:
            continue
        if any(
            value is None or not math.isfinite(value) or value <= 0.0
            for value in horse_probs.values()
        ):
            continue
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_run = predicted_at

    if best_run is None:
        return False, "no_matching_full_field_probability_run", None
    probabilities = {
        horse_id: float(value)
        for horse_id, value in prediction_runs[best_run].items()
    }
    return True, None, probabilities


def _timedelta_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# Top-level audit-only aggregation (SPEC SS1, SS5; SS3.1.5/.6)
# --------------------------------------------------------------------------


def run_audit(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Read-only population/exclusion/time-point audit. No scoring of any kind."""

    connection = read_only_connect(db_path)
    try:
        result = run_audit_on_connection(connection)
        result["db_path"] = str(db_path)
        return result
    finally:
        connection.close()


def _build_race_audits(connection: sqlite3.Connection) -> dict[str, RaceAudit]:
    """Run SPEC SS3.1's per-race audit over the structural candidate set.

    Shared by run_audit_on_connection (--audit-only) and run_evaluation
    (--run / --smoke's end-to-end path), so both read exactly the same
    population/exclusion logic. Never scores anything.
    """

    candidates = _load_candidate_races(connection)
    audits: dict[str, RaceAudit] = {}
    for race_id, row in candidates.items():
        audits[race_id] = audit_one_race(connection, row)
    return audits


def run_audit_on_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    """Core audit logic against an already-open connection (read-only from the
    real DB via run_audit(), or an in-memory synthetic connection from tests /
    --smoke). Never scores, never opens a database itself."""

    if True:
        total_races = connection.execute("SELECT COUNT(*) AS n FROM races").fetchone()["n"]
        flat_races = connection.execute(
            "SELECT COUNT(*) AS n FROM races WHERE surface IN ('芝','ダート')"
        ).fetchone()["n"]
        candidates = _load_candidate_races(connection)
        audits = _build_race_audits(connection)

        not_applicable = {
            race_id: a for race_id, a in audits.items() if not a.applicable
        }
        applicable = {race_id: a for race_id, a in audits.items() if a.applicable}
        # "Quality pass" = passed stage-30/10/2 capture selection (SPEC SS3.1
        # items 1-3), independent of the subsequent result/horse-set check.
        # This is the SS1/SS5 funnel step distinct from the final eligible
        # count; it intentionally does NOT reuse or reproduce 570R (that was
        # the acquisition-quality-only figure under the pre-v3 method).
        capture_quality_pass = {
            race_id: a for race_id, a in applicable.items() if a.capture_selected
        }
        excluded_by_result_or_horseset = {
            race_id: a
            for race_id, a in capture_quality_pass.items()
            if not a.eligible
        }
        final_eligible = {
            race_id: a for race_id, a in applicable.items() if a.eligible
        }
        time_violations = {
            race_id: a.time_assert_violations
            for race_id, a in applicable.items()
            if a.time_assert_violations
        }

        primary_reason_counts = Counter(
            a.primary_exclusion for a in applicable.values() if a.primary_exclusion
        )
        all_flag_counts = Counter()
        for a in applicable.values():
            all_flag_counts.update(a.all_flags)

        def _rate_breakdown(
            keyfn: Callable[[RaceAudit], str]
        ) -> dict[str, dict[str, Any]]:
            totals: Counter = Counter()
            passes: Counter = Counter()
            for a in applicable.values():
                key = keyfn(a)
                totals[key] += 1
                if a.eligible:
                    passes[key] += 1
            return {
                key: {
                    "eligible": passes.get(key, 0),
                    "total": total,
                    "rate": passes.get(key, 0) / total if total else 0.0,
                }
                for key, total in sorted(totals.items())
            }

        by_year_month = _rate_breakdown(lambda a: year_month(a.race_date))
        by_venue = _rate_breakdown(lambda a: a.venue)
        by_surface = _rate_breakdown(lambda a: a.surface)

        def _field_band_of(a: RaceAudit) -> str:
            # SPEC v3 SS3.6-1: only an *adopted* capture's field_size may be
            # used, never a non-adopted attempt's. Prefer stage 30, then 10,
            # then 2; races with no adopted capture at all (a stage_failures
            # exclusion) report "unknown" rather than falling back to any
            # non-adopted row's headcount.
            for stage in PRIMARY_STAGES:
                capture = a.captures.get(stage)
                if capture is not None and capture.field_size >= 8:
                    return field_band(capture.field_size)
            return "unknown"

        by_field_band = _rate_breakdown(_field_band_of)

        d_compatible: dict[str, dict[str, float]] = {}
        d_exclusion_reasons: Counter = Counter()
        for race_id, a in final_eligible.items():
            compatible, reason, probs = check_d_association(connection, a)
            if compatible and probs is not None:
                d_compatible[race_id] = probs
            else:
                d_exclusion_reasons[reason or "unknown"] += 1

        final_race_ids = sorted(final_eligible)
        event_dates = sorted({race_id[:8] for race_id in final_race_ids})

        machine_status = "OK"
        invalid_reasons: list[str] = []
        if time_violations:
            machine_status = "INVALID"
            invalid_reasons.append(
                f"time_point_assert_violation_in_adopted_set: {len(time_violations)} race(s)"
            )
        if len(final_race_ids) < 800:
            invalid_reasons.append(
                "final_eligible_population_below_800: Stage B gate not met "
                f"({len(final_race_ids)} < 800); this is a stop_rule condition for "
                "Stage B, not a defect in this audit"
            )

        result = {
            "mode": "audit-only",
            "spec_path": "docs/codex/SPEC-T17a-odds-drift-diagnostic.md",
            "spec_version": "v3",
            "total_races_in_db": total_races,
            "flat_surface_races": flat_races,
            "structural_excluded_nonflat_or_missing_a_primary_stage_attempt": flat_races
            - len(candidates),
            "candidate_structural_count": len(candidates),
            "not_applicable_headcount_or_unknown": len(not_applicable),
            "applicable_count": len(applicable),
            "capture_quality_pass_count": len(capture_quality_pass),
            "additional_excluded_by_result_or_horseset": len(
                excluded_by_result_or_horseset
            ),
            "final_eligible_count": len(final_eligible),
            "final_eligible_event_dates": len(event_dates),
            "final_eligible_event_date_list": event_dates,
            "gate_800_reached": len(final_race_ids) >= 800,
            "gate_800_shortfall": max(0, 800 - len(final_race_ids)),
            "exclusion_primary_reason": dict(primary_reason_counts),
            "exclusion_all_flags": dict(all_flag_counts),
            "eligible_rate_by_year_month": by_year_month,
            "eligible_rate_by_venue": by_venue,
            "eligible_rate_by_surface": by_surface,
            "eligible_rate_by_field_band": by_field_band,
            "d_compatible_count": len(d_compatible),
            "d_exclusion_reasons": dict(d_exclusion_reasons),
            "time_assert_violations": time_violations,
            "final_race_id_set_sha256": final_set_sha256(final_race_ids),
            "machine_status": machine_status,
            "invalid_reasons": invalid_reasons,
            "note": (
                "audit-only performs no scoring, log-loss, or correlation on real "
                "data. 570R from the pre-audit review was the acquisition-quality "
                "side only; this audit recomputes the SPEC v3 final eligible "
                "population independently and does not reuse or assume 570/800."
            ),
        }
        return result


# --------------------------------------------------------------------------
# SPEC SS3.2/3.4/3.5: market probabilities, features, conditional-logit
# candidates, metrics, and the machine judgment. Used only by --smoke and by
# the (not executed in Stage A) --run evaluation path; never by --audit-only.
# --------------------------------------------------------------------------


def market_probabilities(odds: Sequence[float]) -> list[float]:
    if not odds:
        raise QualityError("market_probabilities requires at least one runner")
    for value in odds:
        if not math.isfinite(value) or value <= 1.0:
            raise QualityError(f"odds must be finite and > 1.0, got {value!r}")
    inverse = [1.0 / value for value in odds]
    total = sum(inverse)
    return [value / total for value in inverse]


def popularity_ranks(odds: Sequence[float], umaban: Sequence[int]) -> list[int]:
    """Rank 1..n by odds ascending, ties broken by umaban ascending."""

    order = sorted(range(len(odds)), key=lambda i: (odds[i], umaban[i]))
    ranks = [0] * len(odds)
    for rank, index in enumerate(order, start=1):
        ranks[index] = rank
    return ranks


@dataclass(frozen=True)
class RaceRow:
    """One race's per-horse data for the conditional-logit candidates."""

    race_id: str
    horse_ids: tuple[str, ...]
    winner_index: int
    log_p2: tuple[float, ...]
    drift: tuple[float, ...]
    dp: tuple[float, ...]
    rank_change: tuple[float, ...]
    pop2_rank_of_winner: int
    # Descriptive-only / candidate-(d)-only fields (SPEC v3 SS3.2/SS3.4).
    # Optional so that every existing positional-args call site (tests,
    # --smoke's original modelling checks) keeps working unchanged.
    log_p30: tuple[float, ...] = ()
    log_p10: tuple[float, ...] | None = None
    d_feature: tuple[float, ...] | None = None


def build_race_row(
    race_id: str,
    horse_ids: Sequence[str],
    odds30: Sequence[float],
    odds2: Sequence[float],
    umaban: Sequence[int],
    winner_horse_id: str,
    *,
    odds10: Sequence[float] | None = None,
    predictions30: Mapping[str, float] | None = None,
) -> RaceRow:
    """Build one race's modelling row.

    odds10 (SPEC v3 SS3.4 descriptive-only stage-10 market LogLoss) and
    predictions30 (candidate (d)'s stage-30-equivalent calibrated_win_
    probability, keyed by horse_id, SPEC v3 SS3.2(d)/SS3.6-3) are both
    optional and additive: omitting them reproduces the original (a)/(b)/(c)
    -only row exactly as before.
    """

    p30 = market_probabilities(odds30)
    p2 = market_probabilities(odds2)
    pop30 = popularity_ranks(odds30, umaban)
    pop2 = popularity_ranks(odds2, umaban)
    drift = [math.log(o2 / o30) for o30, o2 in zip(odds30, odds2)]
    dp = [b - a for a, b in zip(p30, p2)]
    rank_change = [float(b - a) for a, b in zip(pop30, pop2)]
    log_p2 = [math.log(p) for p in p2]
    log_p30 = [math.log(p) for p in p30]
    winner_index = list(horse_ids).index(winner_horse_id)

    log_p10: tuple[float, ...] | None = None
    if odds10 is not None:
        p10 = market_probabilities(odds10)
        log_p10 = tuple(math.log(p) for p in p10)

    d_feature: tuple[float, ...] | None = None
    if predictions30 is not None:
        d_feature = tuple(
            math.log(predictions30[horse_id]) - math.log(p30_i)
            for horse_id, p30_i in zip(horse_ids, p30)
        )

    return RaceRow(
        race_id=race_id,
        horse_ids=tuple(horse_ids),
        winner_index=winner_index,
        log_p2=tuple(log_p2),
        drift=tuple(drift),
        dp=tuple(dp),
        rank_change=tuple(rank_change),
        pop2_rank_of_winner=pop2[winner_index],
        log_p30=tuple(log_p30),
        log_p10=log_p10,
        d_feature=d_feature,
    )


def _score(row: RaceRow, params: Sequence[float], feature_names: Sequence[str]) -> list[float]:
    n = len(row.horse_ids)
    scores = list(row.log_p2)
    features = {
        "drift": row.drift,
        "dp": row.dp,
        "rank_change": row.rank_change,
        "d": row.d_feature,
    }
    for beta, name in zip(params, feature_names):
        values = features[name]
        if values is None:
            raise QualityError(
                f"race {row.race_id} has no {name!r} feature but a candidate "
                "using it was scored against it"
            )
        for i in range(n):
            scores[i] += beta * values[i]
    return scores


def _softmax_probabilities(scores: Sequence[float]) -> list[float]:
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    total = sum(exps)
    return [value / total for value in exps]


def market_mean_log_loss(rows: Sequence["RaceRow"], attr: str) -> float:
    """SPEC v3 SS3.4: descriptive-only race-mean LogLoss of a raw market/log-
    probability attribute (log_p30 / log_p10 / log_p2), skipping rows where
    that attribute is unavailable (e.g. log_p10 on a row built without
    odds10). Raises if no row has the attribute at all."""

    total = 0.0
    n = 0
    for row in rows:
        log_p = getattr(row, attr)
        if log_p is None or len(log_p) == 0:
            continue
        total += _race_neg_log_prob(log_p, row.winner_index)
        n += 1
    if n == 0:
        raise QualityError(f"market_mean_log_loss: no row has a usable {attr!r}")
    return total / n


def _race_neg_log_prob(scores: Sequence[float], winner_index: int) -> float:
    m = max(scores)
    shifted = [s - m for s in scores]
    log_sum = m + math.log(sum(math.exp(s) for s in shifted))
    return log_sum - scores[winner_index]


def neg_log_likelihood(
    params: Sequence[float], rows: Sequence[RaceRow], feature_names: Sequence[str]
) -> float:
    total = 0.0
    for row in rows:
        scores = _score(row, params, feature_names)
        total += _race_neg_log_prob(scores, row.winner_index)
    return total


@dataclass(frozen=True)
class FitResult:
    params: tuple[float, ...]
    feature_names: tuple[str, ...]
    converged: bool
    message: str


def fit_conditional_logit(
    rows: Sequence[RaceRow], feature_names: Sequence[str]
) -> FitResult:
    """Race-conditional logit MLE, no L2, L-BFGS-B, must converge (SPEC SS3.2)."""

    if not feature_names:
        return FitResult(params=(), feature_names=(), converged=True, message="no free parameters (control)")
    if not rows:
        raise QualityError("fit_conditional_logit requires at least one race")
    from scipy.optimize import minimize

    x0 = [0.0] * len(feature_names)
    objective = lambda params: neg_log_likelihood(params, rows, feature_names)
    optimisation = minimize(objective, x0, method="L-BFGS-B")
    return FitResult(
        params=tuple(float(v) for v in optimisation.x),
        feature_names=tuple(feature_names),
        converged=bool(optimisation.success),
        message=str(optimisation.message),
    )


def win_log_loss_per_race(
    rows: Sequence[RaceRow], fit: FitResult
) -> dict[str, float]:
    per_race: dict[str, float] = {}
    for row in rows:
        scores = _score(row, fit.params, fit.feature_names)
        per_race[row.race_id] = _race_neg_log_prob(scores, row.winner_index)
    return per_race


def topk_hit_rate(rows: Sequence[RaceRow], fit: FitResult, k: int) -> float:
    if not rows:
        raise QualityError("topk_hit_rate requires at least one race")
    hits = 0
    for row in rows:
        scores = _score(row, fit.params, fit.feature_names)
        # Display rank: score descending, ties broken by horse index (stable
        # deterministic order; horse_ids already reflect umaban order).
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        rank_of_winner = order.index(row.winner_index) + 1
        if rank_of_winner <= k:
            hits += 1
    return hits / len(rows)


def race_mean_log_loss(rows: Sequence[RaceRow], fit: FitResult) -> float:
    per_race = win_log_loss_per_race(rows, fit)
    return sum(per_race.values()) / len(per_race)


def popularity_band_deltas(
    rows: Sequence[RaceRow], fit_b: FitResult, fit_a: FitResult
) -> dict[str, dict[str, Any]]:
    """SPEC SS3.4: race-equal-weight LogLoss(b)-LogLoss(a), grouped by the
    winner's 2-minute market popularity band. Bands with zero races report
    diff=None and must never be treated as a zero or as an improvement."""

    ll_b = win_log_loss_per_race(rows, fit_b)
    ll_a = win_log_loss_per_race(rows, fit_a)
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        band = popularity_band(row.pop2_rank_of_winner)
        grouped[band].append(ll_b[row.race_id] - ll_a[row.race_id])
    result: dict[str, dict[str, Any]] = {}
    for label, _lo, _hi in POPULARITY_BANDS:
        diffs = grouped.get(label, [])
        result[label] = {
            "count": len(diffs),
            "diff": (sum(diffs) / len(diffs)) if diffs else None,
        }
    return result


def split_train_validation_by_event_date(
    rows: Sequence[RaceRow], train_fraction: float = 0.6
) -> tuple[list[RaceRow], list[RaceRow]]:
    """SPEC SS3.3: chronological 60/40 split by event date (not by race)."""

    if not rows:
        raise QualityError("split requires at least one race")
    dates = sorted({row.race_id[:8] for row in rows})
    if not dates:
        raise QualityError("no event dates found")
    cut = max(1, math.floor(len(dates) * train_fraction))
    cut = min(cut, len(dates) - 1) if len(dates) > 1 else len(dates)
    train_dates = set(dates[:cut])
    train = [row for row in rows if row.race_id[:8] in train_dates]
    validation = [row for row in rows if row.race_id[:8] not in train_dates]
    if not train or not validation:
        raise QualityError("60/40 event-date split produced an empty side")
    return train, validation


def paired_metric_by_block(
    rows: Sequence[RaceRow], fit_b: FitResult, fit_a: FitResult
) -> dict[str, list[float]]:
    """Per-event-date-block paired (LL_b - LL_a) values for bootstrap input."""

    ll_b = win_log_loss_per_race(rows, fit_b)
    ll_a = win_log_loss_per_race(rows, fit_a)
    blocks: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        blocks[row.race_id[:8]].append(ll_b[row.race_id] - ll_a[row.race_id])
    return dict(blocks)


def bootstrap_primary_delta(
    rows: Sequence[RaceRow],
    fit_b: FitResult,
    fit_a: FitResult,
    *,
    n_resamples: int = 2000,
    seed: int = 17,
):
    """SPEC SS2/3.4: open-day block bootstrap of mean(LL_b - LL_a)."""

    from eval.blocks import paired_block_bootstrap

    ll_b = win_log_loss_per_race(rows, fit_b)
    ll_a = win_log_loss_per_race(rows, fit_a)
    blocks_b: dict[str, list[float]] = defaultdict(list)
    blocks_a: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        blocks_b[row.race_id[:8]].append(ll_b[row.race_id])
        blocks_a[row.race_id[:8]].append(ll_a[row.race_id])

    def _mean(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    return paired_block_bootstrap(
        _mean, blocks_b, blocks_a, n_resamples=n_resamples, seed=seed
    )


def day_mean_log_loss_delta_std(
    rows: Sequence[RaceRow], fit_b: FitResult, fit_a: FitResult
) -> float:
    """SPEC SS3.4: SD_day, the standard deviation of the per-event-day mean
    (LL_b - LL_a), for sizing the T17 main-body sample requirement."""

    blocks = paired_metric_by_block(rows, fit_b, fit_a)
    day_means = [sum(values) / len(values) for values in blocks.values()]
    if len(day_means) < 2:
        raise QualityError("SD_day requires at least two event days")
    mean = sum(day_means) / len(day_means)
    variance = sum((value - mean) ** 2 for value in day_means) / (len(day_means) - 1)
    return math.sqrt(variance)


# --------------------------------------------------------------------------
# SPEC SS3.5: machine judgment (four fixed outcomes, fixed priority order)
# --------------------------------------------------------------------------

MachineVerdict = str  # one of the four literal strings below
INVALID = "INVALID"
HISTORICAL_SCREEN_FAIL = "HISTORICAL_SCREEN_FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
HISTORICAL_SCREEN_PASS = "HISTORICAL_SCREEN_PASS"


def machine_judgment(
    *,
    quality_violations: Sequence[str],
    delta: float,
    ci_low: float,
    ci_high: float,
    topk_diffs: Mapping[int, float],
    band_stats: Mapping[str, Mapping[str, Any]],
) -> tuple[MachineVerdict, list[str]]:
    """SPEC SS3.5, applied in the documented priority order. Never mutates its
    inputs and never substitutes 0 for a missing value."""

    reasons: list[str] = []

    # Priority 1: INVALID overrides everything else.
    if quality_violations:
        return INVALID, [f"stop_rule_violation:{v}" for v in quality_violations]
    for value, label in ((delta, "delta"), (ci_low, "ci_low"), (ci_high, "ci_high")):
        if not math.isfinite(value):
            return INVALID, [f"non_finite_required_value:{label}"]
    if ci_low > ci_high:
        return INVALID, ["ci_low_greater_than_ci_high"]
    for k, diff in topk_diffs.items():
        if not math.isfinite(diff):
            return INVALID, [f"non_finite_topk_diff:k={k}"]
    for label, stats in band_stats.items():
        count = stats.get("count")
        diff = stats.get("diff")
        if count is None or count < 0:
            return INVALID, [f"invalid_band_count:{label}"]
        if count > 0 and (diff is None or not math.isfinite(diff)):
            return INVALID, [f"non_finite_band_diff_with_races_present:{label}"]

    topk_any_negative = any(diff < 0 for diff in topk_diffs.values())
    computable_bands = {
        label: stats for label, stats in band_stats.items() if stats["count"] > 0
    }
    all_bands_computable = len(computable_bands) == len(band_stats)
    negative_band_count = sum(
        1 for stats in computable_bands.values() if stats["diff"] < 0
    )

    # Priority 2: HISTORICAL_SCREEN_FAIL.
    if delta >= 0:
        reasons.append("delta_not_negative")
    if topk_any_negative:
        reasons.append("topk_diff_negative")
    if all_bands_computable and negative_band_count < 2:
        reasons.append("fewer_than_two_negative_bands_with_all_bands_computable")
    if reasons:
        return HISTORICAL_SCREEN_FAIL, reasons

    # Priority 3: INCONCLUSIVE.
    if len(computable_bands) < len(band_stats):
        reasons.append("popularity_band_zero_count")
    if ci_high >= 0:
        reasons.append("ci_high_not_negative")
    if reasons:
        return INCONCLUSIVE, reasons

    # Priority 4: HISTORICAL_SCREEN_PASS.
    return HISTORICAL_SCREEN_PASS, [
        "delta_negative",
        "ci_high_negative",
        "topk_diffs_all_nonnegative",
        "all_bands_computable_and_at_least_two_negative",
    ]


# --------------------------------------------------------------------------
# SPEC SS3.7: the --run evaluation body. End-to-end against any already-open
# connection (the real frozen extract for --run, never exercised against
# real data in Stage A; a synthetic/in-memory connection for --smoke and
# tests). Population/exclusion comes from _build_race_audits (the same
# function --audit-only uses), so the evaluated population is always exactly
# the SS3.1 final-eligible set, never a separately-derived one.
# --------------------------------------------------------------------------


def evaluation_conditions() -> dict[str, Any]:
    """A pure, data-independent fingerprint of SPEC v3's evaluation contract:
    population definition, candidates, primary/safety metrics, split,
    bootstrap configuration, and machine-judgment conditions. Recomputed
    fresh from the running code (never read from a file), so a silent code
    change that alters any of these shows up as a hash mismatch at the
    --run gate (SPEC v3 SS3.7) even if no source file's sha256 changed."""

    return {
        "population_definition": {
            "surface": ["芝", "ダート"],
            "primary_stages": list(PRIMARY_STAGES),
            "min_adopted_field_size": 8,
            "stage_windows_seconds_to_post": {
                str(stage): list(window) for stage, window in STAGE_WINDOWS.items()
            },
            "exclusion_priority": list(EXCLUSION_PRIORITY),
            "field_size_judged_from_adopted_captures_only": True,
            "field_size_must_agree_across_adopted_stages": True,
            "post_race_capture_unconditionally_excluded": True,
            "horse_set_must_exactly_match_result_and_all_stages": True,
            "finish_position_must_be_strict_permutation_of_1_to_n": True,
        },
        "candidates": {
            "a_control_fixed_coefficient_not_counted": ["log_p2"],
            "b": ["log_p2", "drift"],
            "c": ["log_p2", "drift", "dp", "rank_change"],
            "d": ["log_p2", "d"],
            "d_feature_definition": "log(p_model,30) - log(p_30)",
            "d_association_tolerance_seconds": D_ASSOCIATION_TOLERANCE_SECONDS,
            "d_probability_source_column": "predictions.calibrated_win_probability",
        },
        "primary_metric": PRIMARY_METRIC,
        "safety_metrics": list(SAFETY_METRICS),
        "split": {
            "method": "chronological_60_40_by_distinct_event_date",
            "train_fraction": 0.6,
            "validation_coefficients_frozen_from_training_fit": True,
            "per_event_day_paired_delta_reported": True,
        },
        "bootstrap": {
            "method": "eval.blocks.paired_block_bootstrap",
            "n_resamples": 2000,
            "seed": 17,
            "block_unit": "JRA event date (race_id[:8])",
        },
        "topk_ks": [1, 2, 3],
        "popularity_bands": [label for label, _lo, _hi in POPULARITY_BANDS],
        "machine_judgment_priority_order": [
            INVALID,
            HISTORICAL_SCREEN_FAIL,
            INCONCLUSIVE,
            HISTORICAL_SCREEN_PASS,
        ],
        "stop_rule": STOP_RULE_TEXT,
    }


def evaluation_conditions_sha256() -> str:
    return sha256_text(json.dumps(evaluation_conditions(), ensure_ascii=False, sort_keys=True))


def build_race_rows_from_audits(
    connection: sqlite3.Connection, audits: Mapping[str, RaceAudit]
) -> tuple[list[RaceRow], dict[str, str]]:
    """Build one RaceRow per SS3.1 final-eligible race, with candidate (d)'s
    feature attached wherever SS3.2(d)/SS3.6-3 association succeeds.

    Returns (rows, d_exclusion_reasons_by_race_id) for races that ARE in the
    primary (a)/(b)/(c) population but could not be matched for (d); this
    mirrors --audit-only's d_exclusion_reasons and never shrinks ``rows``.
    """

    rows: list[RaceRow] = []
    d_excluded: dict[str, str] = {}
    for race_id, audit in audits.items():
        if not audit.eligible:
            continue
        cap30 = audit.captures[30]
        cap10 = audit.captures[10]
        cap2 = audit.captures[2]
        horse_ids = sorted(cap2.horse_odds, key=lambda h: cap2.horse_umaban[h])
        odds30 = [cap30.horse_odds[h] for h in horse_ids]
        odds10 = [cap10.horse_odds[h] for h in horse_ids]
        odds2 = [cap2.horse_odds[h] for h in horse_ids]
        umaban = [cap2.horse_umaban[h] for h in horse_ids]

        result_rows = connection.execute(
            "SELECT horse_id, finish_position FROM race_results WHERE race_id = ?",
            (race_id,),
        ).fetchall()
        winner_id = next(r["horse_id"] for r in result_rows if r["finish_position"] == 1)

        compatible, reason, probs = check_d_association(connection, audit)
        predictions30 = probs if (compatible and probs is not None) else None
        if predictions30 is None:
            d_excluded[race_id] = reason or "unknown"

        rows.append(
            build_race_row(
                race_id, horse_ids, odds30, odds2, umaban, winner_id,
                odds10=odds10, predictions30=predictions30,
            )
        )
    return rows, d_excluded


@dataclass(frozen=True)
class EvaluationOutcome:
    result: dict[str, Any]
    race_metrics: list[dict[str, Any]]
    predictions: list[dict[str, Any]]


def run_evaluation(
    connection: sqlite3.Connection,
    *,
    enforce_min_population: bool = True,
    min_population: int = 800,
    bootstrap_resamples: int = 2000,
    bootstrap_seed: int = 17,
) -> EvaluationOutcome:
    """SPEC v3 SS3.7 evaluation body: coefficient fits (a/b/c/d), the primary
    and secondary metrics, the paired block bootstrap, and the SS3.5 machine
    judgment. ``enforce_min_population=False`` is used only by --smoke (a
    small synthetic population is expected there); --run always leaves it
    True so a real evaluation can never proceed under the 800-race gate."""

    audits = _build_race_audits(connection)
    eligible_audits = {race_id: a for race_id, a in audits.items() if a.eligible}
    time_violations = {
        race_id: a.time_assert_violations
        for race_id, a in audits.items()
        if a.applicable and a.time_assert_violations
    }

    quality_violations: list[str] = []
    if time_violations:
        quality_violations.append(
            f"time_point_assert_violation_in_adopted_set: {len(time_violations)} race(s)"
        )
    population_shortfall = enforce_min_population and len(eligible_audits) < min_population
    if population_shortfall:
        quality_violations.append(
            f"final_eligible_population_below_{min_population}: "
            f"{len(eligible_audits)} < {min_population}"
        )

    if population_shortfall:
        # SPEC v3 SS3.7 review round 3: stop immediately on the shortfall
        # verdict, before any row-building or coefficient estimation. Neither
        # build_race_rows_from_audits nor fit_conditional_logit is called in
        # this branch, so a real (enforce_min_population=True) run under the
        # 800-race gate can never reach model fitting.
        verdict, reasons = machine_judgment(
            quality_violations=quality_violations,
            delta=float("nan"),
            ci_low=float("nan"),
            ci_high=float("nan"),
            topk_diffs={},
            band_stats={},
        )
        result = {
            "spec_version": "v3",
            "final_eligible_count": len(eligible_audits),
            "quality_violations": quality_violations,
            "machine_judgment": {"verdict": verdict, "reasons": reasons},
        }
        return EvaluationOutcome(result=result, race_metrics=[], predictions=[])

    rows, d_exclusion_reasons = build_race_rows_from_audits(connection, eligible_audits)

    if not rows:
        quality_violations.append("no_final_eligible_rows_to_fit")
        verdict, reasons = machine_judgment(
            quality_violations=quality_violations,
            delta=float("nan"),
            ci_low=float("nan"),
            ci_high=float("nan"),
            topk_diffs={},
            band_stats={},
        )
        result = {
            "final_eligible_count": len(eligible_audits),
            "quality_violations": quality_violations,
            "machine_judgment": {"verdict": verdict, "reasons": reasons},
        }
        return EvaluationOutcome(result=result, race_metrics=[], predictions=[])

    train_rows, validation_rows = split_train_validation_by_event_date(rows)
    train_event_dates = sorted({row.race_id[:8] for row in train_rows})
    validation_event_dates = sorted({row.race_id[:8] for row in validation_rows})

    control = FitResult(params=(), feature_names=(), converged=True, message="control")
    fit_b = fit_conditional_logit(train_rows, ["drift"])
    fit_c = fit_conditional_logit(train_rows, ["drift", "dp", "rank_change"])
    if not fit_b.converged:
        quality_violations.append("non_convergence:b")
    if not fit_c.converged:
        quality_violations.append("non_convergence:c")

    train_d = [row for row in train_rows if row.d_feature is not None]
    validation_d = [row for row in validation_rows if row.d_feature is not None]
    fit_d: FitResult | None = None
    if train_d and validation_d:
        fit_d = fit_conditional_logit(train_d, ["d"])
        if not fit_d.converged:
            quality_violations.append("non_convergence:d")

    delta = race_mean_log_loss(validation_rows, fit_b) - race_mean_log_loss(validation_rows, control)
    boot = bootstrap_primary_delta(
        validation_rows, fit_b, control, n_resamples=bootstrap_resamples, seed=bootstrap_seed
    )
    topk_diffs = {
        k: topk_hit_rate(validation_rows, fit_b, k) - topk_hit_rate(validation_rows, control, k)
        for k in (1, 2, 3)
    }
    band_stats = popularity_band_deltas(validation_rows, fit_b, control)

    delta_c = race_mean_log_loss(validation_rows, fit_c) - race_mean_log_loss(validation_rows, control)
    delta_d = None
    if fit_d is not None:
        # SPEC v3 SS3.4/SS3.6: (d) vs (a) is restricted to the same
        # d-compatible race set on both sides; never widen (a) back out.
        delta_d = race_mean_log_loss(validation_d, fit_d) - race_mean_log_loss(validation_d, control)

    market_ll: dict[str, float] = {
        "stage30": market_mean_log_loss(validation_rows, "log_p30"),
        "stage2": market_mean_log_loss(validation_rows, "log_p2"),
    }
    if all(row.log_p10 is not None for row in validation_rows):
        market_ll["stage10"] = market_mean_log_loss(validation_rows, "log_p10")

    try:
        sd_day = day_mean_log_loss_delta_std(validation_rows, fit_b, control)
    except QualityError:
        sd_day = None  # fewer than 2 validation event dates; null, not 0.

    # SPEC v3 SS3.3 (2026-09-24 review): this is the per-event-day paired
    # delta with coefficients frozen from the training fit, not a leave-
    # one-day-out CV that would refit per day (which would contradict the
    # fixed-coefficient validation rule). Name it accordingly everywhere.
    per_event_day_paired_delta = {
        day: sum(values) / len(values)
        for day, values in paired_metric_by_block(validation_rows, fit_b, control).items()
    }

    for value, label in ((delta, "delta"), (boot.ci_low, "ci_low"), (boot.ci_high, "ci_high")):
        if not math.isfinite(value):
            quality_violations.append(f"non_finite_required_value:{label}")

    verdict, reasons = machine_judgment(
        quality_violations=quality_violations,
        delta=delta,
        ci_low=boot.ci_low,
        ci_high=boot.ci_high,
        topk_diffs=topk_diffs,
        band_stats=band_stats,
    )

    result: dict[str, Any] = {
        "spec_version": "v3",
        "final_eligible_count": len(eligible_audits),
        "population": {
            "train_races": len(train_rows),
            "validation_races": len(validation_rows),
            "train_event_dates": len(train_event_dates),
            "validation_event_dates": len(validation_event_dates),
            "d_compatible_count": sum(1 for row in rows if row.d_feature is not None),
            "d_exclusion_reasons": dict(Counter(d_exclusion_reasons.values())),
        },
        "fits": {
            "b": {"feature_names": list(fit_b.feature_names), "params": list(fit_b.params), "converged": fit_b.converged, "message": fit_b.message},
            "c": {"feature_names": list(fit_c.feature_names), "params": list(fit_c.params), "converged": fit_c.converged, "message": fit_c.message},
            "d": (
                {"feature_names": list(fit_d.feature_names), "params": list(fit_d.params), "converged": fit_d.converged, "message": fit_d.message}
                if fit_d is not None else None
            ),
        },
        "primary_metric": {
            "name": PRIMARY_METRIC,
            "delta": delta,
            "ci_low": boot.ci_low,
            "ci_high": boot.ci_high,
            "p_value": boot.p_value,
            "n_resamples": boot.n_resamples,
            "n_blocks": boot.n_blocks,
            "seed": boot.seed,
        },
        "secondary_metrics": {
            "win_logloss_delta_c_minus_a_validation": delta_c,
            "win_logloss_delta_d_minus_a_dcompatible_subset_validation": delta_d,
            "topk_hit_rate_delta_b_minus_a": {str(k): v for k, v in topk_diffs.items()},
            "popularity_band_logloss_delta_b_minus_a": band_stats,
            "market_win_logloss_by_stage_validation_descriptive": market_ll,
            "sd_day_of_mean_logloss_delta_b_minus_a": sd_day,
            "per_event_day_paired_delta_b_minus_a_by_day": per_event_day_paired_delta,
        },
        "machine_judgment": {"verdict": verdict, "reasons": reasons},
        "quality_violations": quality_violations,
    }

    race_metrics: list[dict[str, Any]] = []
    train_ids = {row.race_id for row in train_rows}
    for row in rows:
        scores_b = _score(row, fit_b.params, fit_b.feature_names)
        scores_c = _score(row, fit_c.params, fit_c.feature_names)
        ll_d = None
        if fit_d is not None and row.d_feature is not None:
            ll_d = _race_neg_log_prob(_score(row, fit_d.params, fit_d.feature_names), row.winner_index)
        race_metrics.append({
            "race_id": row.race_id,
            "event_date": row.race_id[:8],
            "split": "train" if row.race_id in train_ids else "validation",
            "field_size": len(row.horse_ids),
            "popularity_band_of_winner": popularity_band(row.pop2_rank_of_winner),
            "d_compatible": row.d_feature is not None,
            "log_loss": {
                "a_control_market2": _race_neg_log_prob(row.log_p2, row.winner_index),
                "b_drift": _race_neg_log_prob(scores_b, row.winner_index),
                "c_drift_dp_rankchange": _race_neg_log_prob(scores_c, row.winner_index),
                "d_model_residual": ll_d,
                "market_stage30_descriptive": _race_neg_log_prob(row.log_p30, row.winner_index),
                "market_stage10_descriptive": (
                    _race_neg_log_prob(row.log_p10, row.winner_index) if row.log_p10 is not None else None
                ),
            },
        })

    predictions: list[dict[str, Any]] = []
    for row in rows:
        p_a = _softmax_probabilities(row.log_p2)
        p_b = _softmax_probabilities(_score(row, fit_b.params, fit_b.feature_names))
        p_c = _softmax_probabilities(_score(row, fit_c.params, fit_c.feature_names))
        p_d = (
            _softmax_probabilities(_score(row, fit_d.params, fit_d.feature_names))
            if fit_d is not None and row.d_feature is not None
            else None
        )
        predictions.append({
            "race_id": row.race_id,
            "split": "train" if row.race_id in train_ids else "validation",
            "horses": [
                {
                    "horse_id": horse_id,
                    "is_winner": index == row.winner_index,
                    "p_a_control_market2": p_a[index],
                    "p_b_drift": p_b[index],
                    "p_c_drift_dp_rankchange": p_c[index],
                    "p_d_model_residual": (p_d[index] if p_d is not None else None),
                }
                for index, horse_id in enumerate(row.horse_ids)
            ],
        })

    return EvaluationOutcome(result=result, race_metrics=race_metrics, predictions=predictions)


def _write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            handle.write("\n")


def resolve_commit_sha() -> tuple[str, str | None]:
    """Best-effort commit identifier for result_record.draft.json's
    commit_sha (SPEC v3 SS3.7 round-3 review: the field must identify the
    executing code, never the placeholder "unknown"). Returns the real
    ``git rev-parse HEAD`` of this checkout when git is available and the
    checkout is a git repository. When it is not, commit_sha holds an
    explicit reason string (never null, never a bare "unknown"), and the
    second element holds this harness file's own sha256 as a
    commit_sha_fallback substitute identifier."""

    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
    except OSError as exc:
        reason = f"git_executable_unavailable:{exc}"
    else:
        sha = proc.stdout.strip()
        if proc.returncode == 0 and sha:
            return sha, None
        reason = proc.stderr.strip() or f"git_rev_parse_head_exit_{proc.returncode}"
    return f"unavailable:{reason}", f"sha256:{sha256_file(Path(__file__).resolve())}"


def build_result_record_draft(
    registration_draft: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    *,
    result_json_sha256: str,
    commit_sha: str,
    commit_sha_fallback: str | None = None,
) -> dict[str, Any]:
    """SPEC v3 SS3.7/SS5: the ledger-shaped result row draft, in the style of
    T81's result_record.draft.json. Immutable fields and data_hashes are
    carried forward unchanged from the registration; adjudication is always
    null (a reviewer's decision, never made here); superseded_by points back
    at the original registration id. Never appended to eval/experiments.jsonl.
    ``commit_sha_fallback`` (SPEC v3 SS3.7 round-3 review) carries the
    harness's own sha256 when ``commit_sha`` could not be resolved to a real
    git commit; it is None whenever commit_sha is a real commit."""

    data_hashes = dict(registration_draft["data_hashes"])
    data_hashes["result_json_sha256"] = f"sha256:{result_json_sha256}"
    return {
        "experiment_id": registration_draft["experiment_id"] + "-result",
        "registered_at_utc": None,
        "commit_sha": commit_sha,
        "commit_sha_fallback": commit_sha_fallback,
        "data_hashes": data_hashes,
        "features": list(registration_draft["features"]),
        "primary_metric": registration_draft["primary_metric"],
        "safety_metrics": list(registration_draft["safety_metrics"]),
        "search_grid": dict(registration_draft["search_grid"]),
        "candidate_count": registration_draft["candidate_count"],
        "stop_rule": registration_draft["stop_rule"],
        "benchmark_type": registration_draft["benchmark_type"],
        "prospective_start_date": registration_draft["prospective_start_date"],
        "result_summary": {
            "machine_judgment": result_payload.get("machine_judgment"),
            "primary_metric": result_payload.get("primary_metric"),
            "secondary_metrics": result_payload.get("secondary_metrics"),
            "population": result_payload.get("population"),
            "quality_violations": result_payload.get("quality_violations"),
        },
        "adjudication": None,
        "superseded_by": registration_draft["experiment_id"],
        "notes": (
            "T17a result row draft (implementation/execution agent output; "
            "reviewer decides adjudication, never set here). "
            "HISTORICAL_SCREEN_PASS, if reached, is a T17-main-body filing "
            "basis only, never a prospective confirmation or production "
            "adoption. This row is never appended to eval/experiments.jsonl "
            "as-is."
        ),
    }


def write_run_artifacts(
    out_dir: str | Path,
    outcome: EvaluationOutcome,
    registration_draft: Mapping[str, Any],
    *,
    mode: str,
    commit_sha: str | None = None,
    commit_sha_fallback: str | None = None,
) -> dict[str, str]:
    """Write the 4 SPEC v3 SS3.7/SS5 artifacts (result.json, race_metrics.jsonl,
    predictions.jsonl, result_record.draft.json) that T81's Stage B execution
    was missing (docs/T81-rank-display-report.md's artifact_gap). Used by both
    --smoke's end-to-end self-test and (not executed against real data in
    Stage A) --run. ``commit_sha=None`` (the default) resolves the real
    executing commit via :func:`resolve_commit_sha` (SPEC v3 SS3.7 round-3
    review) instead of ever writing the placeholder "unknown"; callers that
    already know a specific identifier (e.g. --smoke's synthetic label) may
    pass one explicitly."""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if commit_sha is None:
        commit_sha, commit_sha_fallback = resolve_commit_sha()

    result_payload = dict(outcome.result)
    result_payload["mode"] = mode
    result_path = out / "result.json"
    _write_json(result_path, result_payload)
    result_json_sha256 = sha256_file(result_path)

    _write_jsonl(out / "race_metrics.jsonl", outcome.race_metrics)
    _write_jsonl(out / "predictions.jsonl", outcome.predictions)

    record_draft = build_result_record_draft(
        registration_draft, result_payload,
        result_json_sha256=result_json_sha256, commit_sha=commit_sha,
        commit_sha_fallback=commit_sha_fallback,
    )
    _write_json(out / "result_record.draft.json", record_draft)

    return {
        "result_json": str(result_path),
        "race_metrics_jsonl": str(out / "race_metrics.jsonl"),
        "predictions_jsonl": str(out / "predictions.jsonl"),
        "result_record_draft_json": str(out / "result_record.draft.json"),
    }


# --------------------------------------------------------------------------
# Synthetic data helpers. Used by --smoke and by tests/test_t17a_drift.py.
# These never touch data/jra_logging.db.
# --------------------------------------------------------------------------

SYNTHETIC_SCHEMA_SQL = """
CREATE TABLE races (
    race_id TEXT PRIMARY KEY,
    race_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    surface TEXT,
    distance_m INTEGER,
    race_class TEXT,
    start_time TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE odds_snapshots (
    odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT,
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_updated_at TEXT,
    win_odds REAL,
    place_odds_low REAL,
    place_odds_high REAL,
    popularity INTEGER,
    source TEXT DEFAULT 'synthetic',
    fetch_id TEXT,
    is_stale INTEGER DEFAULT 0,
    data_quality_flags_json TEXT DEFAULT '[]',
    stage TEXT,
    scheduled_post_at TEXT,
    seconds_to_post REAL,
    fetch_duration_ms INTEGER,
    valid_odds_count INTEGER,
    field_size INTEGER
);
CREATE TABLE race_results (
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    horse_name TEXT,
    finish_position INTEGER,
    official_status TEXT DEFAULT 'official',
    final_win_odds REAL,
    win_payout INTEGER,
    place_payout INTEGER,
    result_fetched_at TEXT NOT NULL,
    source_url TEXT,
    source_hash TEXT DEFAULT 'synthetic',
    data_quality_flags_json TEXT DEFAULT '[]',
    PRIMARY KEY (race_id, horse_id)
);
CREATE TABLE predictions (
    prediction_run_id TEXT,
    race_id TEXT NOT NULL,
    horse_id TEXT NOT NULL,
    predicted_at TEXT NOT NULL,
    web_score REAL,
    win5_score REAL,
    ml_score REAL,
    raw_win_probability REAL,
    calibrated_win_probability REAL,
    confidence REAL,
    score_details_json TEXT,
    feature_snapshot_json TEXT,
    data_quality_flags_json TEXT DEFAULT '[]',
    place_probability REAL
);
"""


def create_synthetic_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SYNTHETIC_SCHEMA_SQL)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def insert_race_row(connection: sqlite3.Connection, race_id: str, race_date: str, venue: str, surface: str) -> None:
    race_no = int(race_id.rsplit(":", 1)[-1])
    connection.execute(
        "INSERT INTO races (race_id, race_date, venue, race_no, surface, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (race_id, race_date, venue, race_no, surface, _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc))),
    )


def insert_clean_capture(
    connection: sqlite3.Connection,
    race_id: str,
    stage: int,
    scheduled_post_at: datetime,
    horse_odds: Mapping[str, float],
    *,
    seconds_offset: float | None = None,
    flags: Sequence[str] = (),
    is_stale: bool = False,
    valid_odds_count: int | None = None,
    duplicate_stale_capture: bool = False,
) -> None:
    """Insert one capture (one observed_at) for every horse at a stage.

    seconds_offset overrides the default (window centre) seconds_to_post, to
    build out-of-window or post-race-capture fixtures.
    """

    n = len(horse_odds)
    lo, hi = STAGE_WINDOWS[stage]
    if seconds_offset is None:
        seconds_offset = (lo + hi) / 2.0
    observed_at = scheduled_post_at - _timedelta_seconds(seconds_offset)
    v_count = n if valid_odds_count is None else valid_odds_count
    flags_json = json.dumps(list(flags))
    rows = [
        (
            race_id,
            horse_id,
            _iso(observed_at),
            odds,
            1 if is_stale else 0,
            flags_json,
            str(stage),
            _iso(scheduled_post_at),
            seconds_offset,
            v_count,
            n,
        )
        for horse_id, odds in horse_odds.items()
    ]
    connection.executemany(
        "INSERT INTO odds_snapshots (race_id, horse_id, observed_at, win_odds, is_stale, "
        "data_quality_flags_json, stage, scheduled_post_at, seconds_to_post, valid_odds_count, field_size) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    if duplicate_stale_capture:
        # A second, earlier, flagged capture attempt for the same stage. Used
        # to test that a duplicate/retry capture is still counted as one race.
        dup_observed_at = observed_at - _timedelta_seconds(30.0)
        dup_seconds = seconds_offset + 30.0
        dup_rows = [
            (
                race_id,
                horse_id,
                _iso(dup_observed_at),
                odds,
                0,
                json.dumps(["catchup_burst"]),
                str(stage),
                _iso(scheduled_post_at),
                dup_seconds,
                v_count,
                n,
            )
            for horse_id, odds in horse_odds.items()
        ]
        connection.executemany(
            "INSERT INTO odds_snapshots (race_id, horse_id, observed_at, win_odds, is_stale, "
            "data_quality_flags_json, stage, scheduled_post_at, seconds_to_post, valid_odds_count, field_size) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            dup_rows,
        )


def insert_result_rows(
    connection: sqlite3.Connection,
    race_id: str,
    finish_positions: Mapping[str, int],
    scheduled_post_at: datetime,
    *,
    result_fetched_after: float = 300.0,
) -> None:
    fetched_at = _iso(scheduled_post_at + _timedelta_seconds(result_fetched_after))
    rows = [
        (race_id, horse_id, position, fetched_at)
        for horse_id, position in finish_positions.items()
    ]
    connection.executemany(
        "INSERT INTO race_results (race_id, horse_id, finish_position, result_fetched_at) VALUES (?,?,?,?)",
        rows,
    )


def insert_prediction_run(
    connection: sqlite3.Connection,
    race_id: str,
    predicted_at: datetime,
    probabilities: Mapping[str, float],
) -> None:
    rows = [
        (race_id, horse_id, _iso(predicted_at), probability)
        for horse_id, probability in probabilities.items()
    ]
    connection.executemany(
        "INSERT INTO predictions (race_id, horse_id, predicted_at, calibrated_win_probability) VALUES (?,?,?,?)",
        rows,
    )


def horse_ids_for(race_id: str, n: int) -> list[str]:
    return [f"{race_id}:{i:02d}" for i in range(1, n + 1)]


def build_clean_synthetic_race(
    connection: sqlite3.Connection,
    rng: Any,
    race_id: str,
    race_date: str,
    venue: str,
    n_horses: int,
    scheduled_post_at: datetime,
    *,
    gamma: float = 0.0,
    kappa: float = 1.0,
    with_predictions: bool = True,
) -> tuple[list[str], str]:
    """Insert a fully clean, eligible race and return (horse_ids, winner_id).

    gamma controls how much a per-horse latent factor r_tilde (which also
    drives the 30->2 minute drift) influences who actually wins, beyond what
    is already in the 2-minute market price. gamma=0 reproduces SPEC SS5
    test (1) (drift uninformative); gamma>0 reproduces test (2) (drift
    informative, ΔLL<0 achievable).
    """

    horse_ids = horse_ids_for(race_id, n_horses)
    mu = [rng.gauss(0.0, 1.0) for _ in horse_ids]
    r = [rng.gauss(0.0, 1.0) for _ in horse_ids]
    mean_r = sum(r) / len(r)
    r_tilde = [value - mean_r for value in r]

    def _softmax(values: Sequence[float]) -> list[float]:
        m = max(values)
        exps = [math.exp(v - m) for v in values]
        total = sum(exps)
        return [v / total for v in exps]

    p2 = _softmax(mu)
    p30 = _softmax([m + kappa * rt for m, rt in zip(mu, r_tilde)])
    true_scores = [m + gamma * rt for m, rt in zip(mu, r_tilde)]
    true_probs = _softmax(true_scores)
    winner_index = rng.choices(range(len(horse_ids)), weights=true_probs, k=1)[0]
    winner_id = horse_ids[winner_index]

    odds2 = {horse_id: 1.0 / p for horse_id, p in zip(horse_ids, p2)}
    odds30 = {horse_id: 1.0 / p for horse_id, p in zip(horse_ids, p30)}
    insert_race_row(connection, race_id, race_date, venue, "芝")
    insert_clean_capture(connection, race_id, 30, scheduled_post_at, odds30)
    insert_clean_capture(connection, race_id, 10, scheduled_post_at, odds2)
    insert_clean_capture(connection, race_id, 2, scheduled_post_at, odds2)
    finish_order = sorted(range(len(horse_ids)), key=lambda i: (0 if i == winner_index else 1, rng.random()))
    finish_positions = {horse_ids[idx]: rank for rank, idx in enumerate(finish_order, start=1)}
    insert_result_rows(connection, race_id, finish_positions, scheduled_post_at)
    if with_predictions:
        t30 = scheduled_post_at - _timedelta_seconds((STAGE_WINDOWS[30][0] + STAGE_WINDOWS[30][1]) / 2.0)
        calibrated = {horse_id: p for horse_id, p in zip(horse_ids, p30)}
        insert_prediction_run(connection, race_id, t30, calibrated)
    return horse_ids, winner_id


def generate_synthetic_race_rows(
    seed: int,
    n_races: int,
    *,
    gamma: float,
    kappa: float = 1.0,
    races_per_day: int = 8,
    horse_range: tuple[int, int] = (8, 16),
) -> list[RaceRow]:
    """RaceRow-level synthetic generation (no DB), for the modelling checks
    in SPEC SS5 tests (1)/(2)/(5)/(6). See build_clean_synthetic_race for the
    gamma/kappa mechanism."""

    import random

    rng = random.Random(seed)
    rows: list[RaceRow] = []
    for i in range(n_races):
        day = i // races_per_day
        race_id = f"2027{(1 + day // 28):02d}{1 + day % 28:02d}:合成:{1 + i % races_per_day:02d}"
        n_horses = rng.randint(*horse_range)
        horse_ids = horse_ids_for(race_id, n_horses)
        umaban = list(range(1, n_horses + 1))
        mu = [rng.gauss(0.0, 1.0) for _ in horse_ids]
        r = [rng.gauss(0.0, 1.0) for _ in horse_ids]
        mean_r = sum(r) / len(r)
        r_tilde = [value - mean_r for value in r]

        def _softmax(values: Sequence[float]) -> list[float]:
            m = max(values)
            exps = [math.exp(v - m) for v in values]
            total = sum(exps)
            return [v / total for v in exps]

        p2 = _softmax(mu)
        p30 = _softmax([m + kappa * rt for m, rt in zip(mu, r_tilde)])
        true_probs = _softmax([m + gamma * rt for m, rt in zip(mu, r_tilde)])
        winner_index = rng.choices(range(n_horses), weights=true_probs, k=1)[0]
        odds2 = [1.0 / p for p in p2]
        odds30 = [1.0 / p for p in p30]
        rows.append(
            build_race_row(race_id, horse_ids, odds30, odds2, umaban, horse_ids[winner_index])
        )
    return rows


def run_smoke(seed: int = 17) -> dict[str, Any]:
    """Synthetic-data-only self test. Never opens data/jra_logging.db."""

    result: dict[str, Any] = {"mode": "smoke", "seed": seed}

    # 1) DB-level exclusion demonstration on an in-memory synthetic database.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_synthetic_schema(connection)
    import random

    rng = random.Random(seed)
    scheduled = datetime(2027, 1, 4, 6, 0, 0, tzinfo=timezone.utc)
    build_clean_synthetic_race(connection, rng, "20270104:合成:01", "20270104", "合成", 10, scheduled)
    connection.commit()
    db_audit = run_audit_on_connection(connection)
    connection.close()
    result["db_level_smoke_audit"] = {
        "candidate_structural_count": db_audit["candidate_structural_count"],
        "final_eligible_count": db_audit["final_eligible_count"],
        "machine_status": db_audit["machine_status"],
    }

    # 2) Modelling-level checks: uninformative vs informative drift.
    def _fit_and_score(rows: list[RaceRow]) -> dict[str, Any]:
        train, validation = split_train_validation_by_event_date(rows)
        fit_a = FitResult(params=(), feature_names=(), converged=True, message="control")
        fit_b = fit_conditional_logit(train, ["drift"])
        delta = race_mean_log_loss(validation, fit_b) - race_mean_log_loss(validation, fit_a)
        return {
            "beta1": fit_b.params[0],
            "converged": fit_b.converged,
            "delta_ll_validation": delta,
        }

    uninformative_rows = generate_synthetic_race_rows(seed, 480, gamma=0.0, kappa=1.0)
    informative_rows = generate_synthetic_race_rows(seed, 480, gamma=1.5, kappa=1.0)
    result["uninformative_drift"] = _fit_and_score(uninformative_rows)
    result["informative_drift"] = _fit_and_score(informative_rows)

    # 3) Bootstrap reproducibility (same seed -> identical distribution).
    train_i, validation_i = split_train_validation_by_event_date(informative_rows)
    fit_a = FitResult(params=(), feature_names=(), converged=True, message="control")
    fit_b = fit_conditional_logit(train_i, ["drift"])
    boot_1 = bootstrap_primary_delta(validation_i, fit_b, fit_a, n_resamples=200, seed=17)
    boot_2 = bootstrap_primary_delta(validation_i, fit_b, fit_a, n_resamples=200, seed=17)
    result["bootstrap_reproducible"] = boot_1.differences == boot_2.differences
    result["bootstrap_ci"] = {
        "observed_difference": boot_1.observed_difference,
        "ci_low": boot_1.ci_low,
        "ci_high": boot_1.ci_high,
        "p_value": boot_1.p_value,
        "n_blocks": boot_1.n_blocks,
    }

    # 4) Machine judgment over the informative scenario's full statistics.
    topk_diffs = {
        k: topk_hit_rate(validation_i, fit_b, k) - topk_hit_rate(validation_i, fit_a, k)
        for k in (1, 2, 3)
    }
    band_stats = popularity_band_deltas(validation_i, fit_b, fit_a)
    verdict, reasons = machine_judgment(
        quality_violations=[],
        delta=boot_1.observed_difference,
        ci_low=boot_1.ci_low,
        ci_high=boot_1.ci_high,
        topk_diffs=topk_diffs,
        band_stats=band_stats,
    )
    result["machine_judgment"] = {"verdict": verdict, "reasons": reasons}

    # 5) (d) association pure-function demo.
    t30 = datetime(2027, 1, 4, 5, 30, 0, tzinfo=timezone.utc)
    runs = {
        _iso(t30): {"h1": 0.4, "h2": 0.6},
        _iso(t30 + _timedelta_seconds(200)): {"h1": 0.5, "h2": 0.5},
    }
    compatible, reason, probs = resolve_d_association(t30, runs, {"h1", "h2"})
    result["d_association_demo"] = {
        "compatible": compatible,
        "reason": reason,
        "matched_probabilities": probs,
    }
    return result


def run_smoke_end_to_end(
    out_dir: str | Path | None = None, *, seed: int = 17
) -> EvaluationOutcome:
    """SPEC v3 SS3.7: synthetic-data-only end-to-end self-test of the full
    --run evaluation body (population -> capture selection -> (a)/(b)/(c)/(d)
    fits -> primary/secondary metrics -> paired block bootstrap -> SS3.5
    machine judgment -> the 4 result artifacts), never opening
    data/jra_logging.db. Distinct from run_smoke() (the original, narrower
    modelling-only self test kept unchanged so its existing tests/--smoke
    behaviour never regresses); this exercises the same code path Stage B's
    real --run will use, with enforce_min_population=False since a Stage-A
    synthetic population is intentionally far below the 800-race gate."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_synthetic_schema(connection)
    import random

    rng = random.Random(seed)
    base_date = datetime(2027, 9, 1, 6, 0, 0, tzinfo=timezone.utc)
    n_days = 40
    races_per_day = 6
    for day in range(n_days):
        scheduled = base_date + _timedelta_seconds(day * 86400)
        race_date = scheduled.strftime("%Y%m%d")
        for race_no in range(1, races_per_day + 1):
            race_id = f"{race_date}:合成:{race_no:02d}"
            n_horses = rng.randint(8, 16)
            build_clean_synthetic_race(
                connection, rng, race_id, race_date, "合成", n_horses, scheduled,
                gamma=1.2, kappa=1.0,
            )
    connection.commit()
    outcome = run_evaluation(connection, enforce_min_population=False)
    connection.close()

    if out_dir is not None:
        draft = build_registration_draft(
            spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64,
            commit_sha="smoke-end-to-end",
        )
        write_run_artifacts(out_dir, outcome, draft, mode="smoke")
    return outcome


# --------------------------------------------------------------------------
# Frozen extraction procedure (SPEC SS1, SS3.3). Implemented and unit-tested
# with synthetic fixtures in Stage A. Not run against the real database in
# Stage A: gate not reached, and the SPEC reserves this for after the gate.
# --------------------------------------------------------------------------


def create_frozen_extract(
    source_connection: sqlite3.Connection,
    dest_path: str | Path,
    race_ids: Sequence[str],
) -> str:
    """Read-only export of one race_id set into a brand-new sqlite file.

    source_connection must already be opened read-only (see
    read_only_connect). dest_path must not exist; this function only ever
    creates a new file, never overwrites an existing extract. Returns the
    sha256 of the resulting file's bytes.
    """

    dest = Path(dest_path)
    if dest.exists():
        raise QualityError(f"frozen extract already exists, refusing to overwrite: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    race_id_list = sorted(set(race_ids))
    if not race_id_list:
        raise QualityError("create_frozen_extract requires at least one race_id")

    dest_connection = sqlite3.connect(str(dest))
    try:
        dest_connection.executescript(SYNTHETIC_SCHEMA_SQL)
        placeholders = ",".join("?" for _ in race_id_list)

        races_rows = source_connection.execute(
            f"SELECT * FROM races WHERE race_id IN ({placeholders})", race_id_list
        ).fetchall()
        _copy_rows(dest_connection, "races", races_rows)

        odds_rows = source_connection.execute(
            f"SELECT * FROM odds_snapshots WHERE race_id IN ({placeholders}) "
            "AND stage IN ('30','15','10','5','2')",
            race_id_list,
        ).fetchall()
        _copy_rows(dest_connection, "odds_snapshots", odds_rows, skip_column="odds_snapshot_id")

        result_rows = source_connection.execute(
            f"SELECT * FROM race_results WHERE race_id IN ({placeholders})", race_id_list
        ).fetchall()
        _copy_rows(dest_connection, "race_results", result_rows)

        # predictions: only the stage-30-equivalent rows (SPEC SS1/SS3.3),
        # i.e. rows whose predicted_at is within the (d) association
        # tolerance of that race's selected stage-30 capture.
        stage30_rows = source_connection.execute(
            f"SELECT race_id, observed_at FROM odds_snapshots "
            f"WHERE race_id IN ({placeholders}) AND stage = '30'",
            race_id_list,
        ).fetchall()
        t30_candidates: dict[str, set[datetime]] = defaultdict(set)
        for row in stage30_rows:
            parsed = parse_dt(row["observed_at"])
            if parsed is not None:
                t30_candidates[row["race_id"]].add(parsed)

        prediction_rows: list[sqlite3.Row] = []
        seen_prediction_keys: set[tuple[str, str, str]] = set()
        for race_id, observed_ats in t30_candidates.items():
            for t30 in observed_ats:
                lo = (t30 - _timedelta_seconds(D_ASSOCIATION_TOLERANCE_SECONDS)).isoformat()
                hi = (t30 + _timedelta_seconds(D_ASSOCIATION_TOLERANCE_SECONDS)).isoformat()
                for row in source_connection.execute(
                    "SELECT * FROM predictions WHERE race_id = ? "
                    "AND predicted_at >= ? AND predicted_at <= ?",
                    (race_id, lo, hi),
                ).fetchall():
                    key = (row["race_id"], row["horse_id"], row["predicted_at"])
                    if key not in seen_prediction_keys:
                        seen_prediction_keys.add(key)
                        prediction_rows.append(row)
        _copy_rows(dest_connection, "predictions", prediction_rows)

        dest_connection.commit()
    finally:
        dest_connection.close()

    return sha256_file(dest)


def _copy_rows(
    dest_connection: sqlite3.Connection,
    table: str,
    rows: Sequence[sqlite3.Row],
    *,
    skip_column: str | None = None,
) -> None:
    if not rows:
        return
    columns = [key for key in rows[0].keys() if key != skip_column]
    placeholders = ",".join("?" for _ in columns)
    dest_connection.executemany(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


# --------------------------------------------------------------------------
# Registration draft (SPEC SS2). Not appended to eval/experiments.jsonl.
# Validated in-memory only, via eval.ledger.validate_experiment.
# --------------------------------------------------------------------------

STOP_RULE_TEXT = (
    "SPEC-T17a v3 SS2: stop and report INVALID on any of: final eligible "
    "population < 800 unique race_id (after acquisition-quality, result-"
    "completeness, horse-set, and time-point checks); frozen extract sha256 "
    "mismatch; post-race-capture contamination in the adopted set after "
    "exclusion; result/horse-set mismatch in the adopted set; non-convergence "
    "of any conditional-logit fit; a non-finite or otherwise invalid required "
    "metric or CI. Ordinary exclusions under SS3.1 (missing capture, out-of-"
    "window, quality flag, incomplete odds, incomplete result/horse-set) are "
    "not by themselves an INVALID condition. Results are not used to change "
    "the candidate set, population, period, or metric after the fact."
)

SECTION_3_6_CLARIFICATIONS_TEXT = (
    "SPEC-T17a v3 SS3.6 (Stage A review clarifications, fixed before "
    "registration): "
    "(1) headcount applicability: n is the field_size of the *adopted* "
    "30/10/2 capture (SS3.1-2's single selection per stage); n<8 is out of "
    "scope; a non-adopted attempt's field_size is never used for this "
    "judgment; a field_size mismatch across the adopted 30/10/2 captures "
    "excludes the race as odds_incomplete (field_size_mismatch_across_"
    "stages). "
    "(2) result-side time-point assert basis: scheduled_post_at is the "
    "adopted stage-2 capture's value (closest to post); result_fetched_at > "
    "scheduled_post_at(stage 2) and, per adopted capture, observed_at < "
    "scheduled_post_at(that capture). "
    "(3) candidate (d)'s win-probability column: predictions.calibrated_"
    "win_probability (the same column production notification uses), not "
    "raw_win_probability (NULL in all real rows); matched to the closest "
    "prediction_run within 90s of the adopted stage-30 capture's observed_at, "
    "requiring finite positive probabilities for every horse in the field; "
    "races failing this are excluded from (d) only, with reasons reported, "
    "never shrinking the (a)/(b)/(c) population. "
    "(4) 'acquisition missing' accounting: a race with no capture attempt at "
    "all at one of stage 30/10/2 is a structural non-candidate, counted "
    "separately in audit.json rather than in the primary exclusion-reason "
    "table. "
    "(5) manifest self-hash: computed by hashing manifest.json after it is "
    "written, in the same way as T81; manifest.json never embeds its own "
    "hash, to avoid a circular hash."
)

PRIMARY_METRIC = "win_logloss_delta_b_minus_a_validation"

SAFETY_METRICS = [
    "win_logloss_delta_c_minus_a_validation",
    "win_logloss_delta_d_minus_a_dcompatible_subset",
    "topk_hit_rate_delta_b_minus_a_k1",
    "topk_hit_rate_delta_b_minus_a_k2",
    "topk_hit_rate_delta_b_minus_a_k3",
    "popularity_band_logloss_delta_b_minus_a_1to3",
    "popularity_band_logloss_delta_b_minus_a_4to8",
    "popularity_band_logloss_delta_b_minus_a_9plus",
    "market_win_logloss_stage30_vs_stage10_vs_stage2_descriptive",
    "sd_day_of_mean_logloss_delta_b_minus_a",
    "per_event_day_paired_delta_b_minus_a",
]


def build_registration_draft(
    *,
    spec_sha256: str,
    harness_sha256: str,
    tests_sha256: str,
    commit_sha: str,
    data_start_date: str = "2026-07-12",
    manifest_sha256: str | None = None,
    evaluation_conditions_sha256_value: str | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "registered_at_utc": None,
        "commit_sha": commit_sha,
        "data_hashes": {
            "spec": f"sha256:{spec_sha256}",
            "harness_source": f"sha256:{harness_sha256}",
            "tests_source": f"sha256:{tests_sha256}",
            "frozen_extract_sha256": None,
            "manifest_sha256": (
                f"sha256:{manifest_sha256}" if manifest_sha256 is not None else None
            ),
            "evaluation_conditions_sha256": (
                f"sha256:{evaluation_conditions_sha256_value}"
                if evaluation_conditions_sha256_value is not None
                else None
            ),
        },
        "features": [
            "log_p2 (b,c,d shared term): ln of the 2-minute market win "
            "probability from normalised inverse win_odds at the selected "
            "stage-2 capture (SPEC v3 SS3.2).",
            "drift (b,c): ln(odds_2/odds_30) at the same two selected "
            "captures, free coefficient beta1, no L2, race-conditional "
            "logit MLE, coefficients shared across all races.",
            "dp (c only): p_2 - p_30, free coefficient beta2.",
            "rank_change (c only): pop_2 - pop_30 (market rank by odds "
            "ascending, ties by umaban ascending), free coefficient beta3.",
            "log_p_model30_minus_log_p30 (d only): ln(p_model,30) - ln(p_30) "
            "where p_model,30 is the stage-30-equivalent predictions.calibrated_"
            "win_probability, matched to the selected stage-30 capture by "
            "|predicted_at - observed_at| <= 90s over the full horse set; free "
            "coefficient beta4. (d) is a residual-informativeness diagnostic, "
            "not a market-independence claim: the CL score already contains "
            "ln_odds.",
        ],
        "primary_metric": PRIMARY_METRIC,
        "safety_metrics": SAFETY_METRICS,
        "search_grid": {},
        "candidate_count": 3,
        "stop_rule": STOP_RULE_TEXT,
        "benchmark_type": "historical",
        "prospective_start_date": None,
        "result_summary": None,
        "adjudication": None,
        "notes": (
            "T17a Stage A registration draft (SPEC v3 SS2). Exploratory "
            "diagnostic over pre-existing, pre-freeze snapshots collected "
            "since data_start_date; benchmark_type=historical because the "
            "experiment specification was not frozen before this data was "
            "collected, not because the snapshots were taken after the "
            "race. data_start_date is recorded for provenance only and is "
            "not a retroactive registration or prospective_start_date; a "
            "formal prospective confirmation, if any, will be registered "
            "separately after a freeze and will not reuse races selected "
            "here. data_start_date=" + data_start_date + ". "
            "Candidates (b)(c)(d) are compared against control (a) = "
            "log p_2 with a fixed coefficient of 1 (no free parameters, not "
            "counted in candidate_count). No hyperparameter search (L2-free, "
            "single MLE fit per candidate, L-BFGS-B, must converge). Chrono-"
            "logical 60/40 split by event date (train/validation); validation-"
            "period coefficients are frozen from the training fit. Paired "
            "uncertainty via eval.blocks.paired_block_bootstrap over JRA "
            "event-date blocks, 2000 resamples, seed=17. Machine judgment: "
            "the 4 fixed SPEC v3 SS3.5 outcomes, applied in the documented "
            "priority order (INVALID > FAIL > INCONCLUSIVE > PASS); the "
            "SS3.5 top-k and popularity-band gates apply only to the primary "
            "candidate (b) vs control (a) comparison on the same validation "
            "set and are never satisfied by (c)/(d) or by the per-event-day "
            "paired delta. "
            "HISTORICAL_SCREEN_PASS is only a T17-main-body filing basis, "
            "not a prospective confirmation or a production adoption. "
            "data_hashes.frozen_extract_sha256 is null in this draft because "
            "Stage A does not reach the >=800 gate and creates no frozen "
            "extract; it will be filled in only once that gate is reached "
            "and Stage B creates the real extract. manifest_sha256 here is "
            "this Stage A manifest's own hash for provenance and will be "
            "re-derived from Stage B's manifest at real registration. "
            "evaluation_conditions_sha256 (data_hashes) fingerprints the "
            "population/candidate/metric/split/bootstrap/machine-judgment "
            "contract computed by evaluation_conditions() in backtest_"
            "t17a_drift.py; --run's gate recomputes it live and refuses on "
            "any mismatch (SPEC v3 SS3.7), in addition to the spec/harness/"
            "tests/manifest sha256 checks. This row is never appended to "
            "eval/experiments.jsonl as-is.\n\n" + SECTION_3_6_CLARIFICATIONS_TEXT
        ),
    }


def validate_registration_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Run the draft through eval.ledger's schema validator, file-I/O free.

    registered_at_utc is null in the draft (assigned only at real
    registration); a placeholder UTC timestamp is substituted on a *copy*
    purely so the schema/type/consistency checks in
    eval.ledger.validate_experiment can run. The stored draft file keeps the
    real null. This never touches eval/experiments.jsonl.
    """

    from eval import ledger as eval_ledger

    probe = dict(draft)
    probe["registered_at_utc"] = "2026-09-24T00:00:00Z"
    try:
        eval_ledger.validate_experiment(probe)
    except eval_ledger.LedgerValidationError as exc:
        return {"schema_valid": False, "error": str(exc)}
    return {
        "schema_valid": True,
        "error": None,
        "note": (
            "validated a copy with a placeholder registered_at_utc for the "
            "schema-conformance check only; the draft file itself keeps "
            "registered_at_utc=null until a reviewer performs the real "
            "registration."
        ),
    }


# --------------------------------------------------------------------------
# --run gate (SPEC SS1/SS2/SS5 stop_rule). Not executed against real data in
# Stage A: the ledger carries no T17a registration yet, so this always
# refuses today. Implemented and tested now so Stage B can use it unchanged.
# --------------------------------------------------------------------------


def check_run_gate(
    experiment_id: str,
    extract_path: str | Path,
    manifest_path: str | Path,
    *,
    ledger_path: str | Path = LEDGER_PATH,
    spec_path: str | Path = REPO_ROOT / "docs" / "codex" / "SPEC-T17a-odds-drift-diagnostic.md",
    harness_path: str | Path = REPO_ROOT / "backtest_t17a_drift.py",
    tests_path: str | Path = REPO_ROOT / "tests" / "test_t17a_drift.py",
) -> dict[str, Any]:
    """Return {'allowed': bool, 'reason': str, 'checks': {...}}. Never mutates
    the ledger.

    SPEC v3 SS3.7: refuse (INVALID, before any real row is scored) unless
    ALL of the following, freshly computed right now, match the registered
    ledger row: experiment_id, frozen extract sha256, SPEC/harness/tests/
    manifest sha256, and the live evaluation_conditions fingerprint (SS3.7's
    "population definition / candidates / metrics / split / bootstrap
    configuration / machine judgment conditions"). A handful of the ledger
    row's own fixed fields (benchmark_type, prospective_start_date,
    primary_metric, candidate_count, search_grid) are checked directly too,
    as an extra, redundant safeguard against a hash collision or a
    hand-edited ledger row.
    """

    from eval import ledger as eval_ledger

    checks: dict[str, Any] = {}

    extract = Path(extract_path)
    if not extract.exists():
        return {"allowed": False, "reason": f"frozen extract not found: {extract}", "checks": checks}
    checks["frozen_extract_sha256"] = sha256_file(extract)

    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        return {"allowed": False, "reason": f"manifest not found: {manifest_file}", "checks": checks}
    checks["manifest_sha256"] = sha256_file(manifest_file)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"allowed": False, "reason": f"manifest failed to parse: {exc}", "checks": checks}

    for label, path in (
        ("spec", spec_path),
        ("harness_source", harness_path),
        ("tests_source", tests_path),
    ):
        p = Path(path)
        if not p.exists():
            return {"allowed": False, "reason": f"{label} file not found: {p}", "checks": checks}
        checks[label] = sha256_file(p)

    checks["evaluation_conditions_sha256"] = evaluation_conditions_sha256()
    manifest_conditions_sha256 = manifest.get("evaluation_conditions_sha256")
    if manifest_conditions_sha256 != checks["evaluation_conditions_sha256"]:
        return {
            "allowed": False,
            "reason": (
                "evaluation_conditions_sha256 mismatch between the running "
                f"code and the manifest: manifest has "
                f"{manifest_conditions_sha256!r}, live code computes "
                f"{checks['evaluation_conditions_sha256']!r}"
            ),
            "checks": checks,
        }

    try:
        records = eval_ledger.load_ledger(ledger_path)
    except eval_ledger.LedgerError as exc:
        return {"allowed": False, "reason": f"ledger failed to load: {exc}", "checks": checks}

    matches = [record for record in records if record["experiment_id"] == experiment_id]
    if not matches:
        return {
            "allowed": False,
            "reason": f"experiment_id {experiment_id!r} is not registered in {ledger_path}",
            "checks": checks,
        }
    record = matches[0]
    ledger_hashes = record.get("data_hashes") or {}

    expected_ledger_hashes = {
        "frozen_extract_sha256": checks["frozen_extract_sha256"],
        "manifest_sha256": f"sha256:{checks['manifest_sha256']}",
        "spec": f"sha256:{checks['spec']}",
        "harness_source": f"sha256:{checks['harness_source']}",
        "tests_source": f"sha256:{checks['tests_source']}",
        "evaluation_conditions_sha256": f"sha256:{checks['evaluation_conditions_sha256']}",
    }
    for key, expected_value in expected_ledger_hashes.items():
        registered_value = ledger_hashes.get(key)
        if registered_value != expected_value:
            return {
                "allowed": False,
                "reason": (
                    f"ledger data_hashes[{key!r}] mismatch: registered "
                    f"{registered_value!r}, computed {expected_value!r}"
                ),
                "checks": checks,
            }

    if record.get("benchmark_type") != "historical" or record.get("prospective_start_date") is not None:
        return {
            "allowed": False,
            "reason": (
                "ledger benchmark_type/prospective_start_date does not match "
                "the SPEC-T17a historical/null contract"
            ),
            "checks": checks,
        }
    if record.get("primary_metric") != PRIMARY_METRIC:
        return {
            "allowed": False,
            "reason": "ledger primary_metric does not match the SPEC-T17a contract",
            "checks": checks,
        }
    if record.get("candidate_count") != 3:
        return {
            "allowed": False,
            "reason": "ledger candidate_count does not match the SPEC-T17a contract (3: b, c, d)",
            "checks": checks,
        }
    if record.get("search_grid") != {}:
        return {
            "allowed": False,
            "reason": "ledger search_grid is not empty; SPEC-T17a forbids hyperparameter search",
            "checks": checks,
        }

    return {
        "allowed": True,
        "reason": (
            "registered experiment_id, frozen extract, spec/harness/tests/"
            "manifest sha256, and evaluation conditions all match"
        ),
        "checks": checks,
    }


# --------------------------------------------------------------------------
# Manifest (Stage A provenance record)
# --------------------------------------------------------------------------


def build_manifest(audit_result: Mapping[str, Any] | None) -> dict[str, Any]:
    import platform
    import subprocess

    def _sha_or_none(path: Path) -> str | None:
        return sha256_file(path) if path.exists() else None

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        head = None
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except Exception:
        status = []

    try:
        import scipy
        import numpy

        scipy_version = scipy.__version__
        numpy_version = numpy.__version__
    except Exception:
        scipy_version = None
        numpy_version = None

    manifest: dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID,
        "stage": "A",
        "git": {
            "head": head,
            "uncommitted_changes": bool(status),
            "status_line_count": len(status),
        },
        "implementation_sha256": {
            "backtest_t17a_drift.py": _sha_or_none(REPO_ROOT / "backtest_t17a_drift.py"),
            "SPEC-T17a-odds-drift-diagnostic.md": _sha_or_none(
                REPO_ROOT / "docs" / "codex" / "SPEC-T17a-odds-drift-diagnostic.md"
            ),
            "T17a-pre-audit-review.md": _sha_or_none(REPO_ROOT / "docs" / "T17a-pre-audit-review.md"),
            "test_t17a_drift.py": _sha_or_none(REPO_ROOT / "tests" / "test_t17a_drift.py"),
        },
        "python_and_library_versions": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": platform.platform(),
            "numpy": numpy_version,
            "scipy": scipy_version,
        },
        "stage_windows_seconds_to_post": {
            str(stage): list(window) for stage, window in STAGE_WINDOWS.items()
        },
        "d_association_tolerance_seconds": D_ASSOCIATION_TOLERANCE_SECONDS,
        "bootstrap": {"n_resamples": 2000, "seed": 17, "block_unit": "JRA event date (race_id[:8])"},
        "train_validation_split": "chronological 60/40 by distinct event date; validation coefficients frozen",
        "gate": "final_eligible_count >= 800 unique race_id (SPEC v3 SS1)",
        # SPEC v3 SS3.7: the --run gate re-derives this live and refuses on
        # any mismatch against the ledger's data_hashes.evaluation_conditions_
        # sha256, independent of the file-level sha256 checks above.
        "evaluation_conditions": evaluation_conditions(),
        "evaluation_conditions_sha256": evaluation_conditions_sha256(),
        "audit_summary": (
            {
                key: audit_result[key]
                for key in (
                    "candidate_structural_count",
                    "not_applicable_headcount_or_unknown",
                    "applicable_count",
                    "capture_quality_pass_count",
                    "additional_excluded_by_result_or_horseset",
                    "final_eligible_count",
                    "final_eligible_event_dates",
                    "gate_800_reached",
                    "gate_800_shortfall",
                    "d_compatible_count",
                    "final_race_id_set_sha256",
                    "machine_status",
                )
            }
            if audit_result is not None
            else None
        ),
    }
    return manifest


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_json(path: str | Path, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def validate_run_out_dir(out_dir: str | None) -> str | None:
    """SPEC v3 SS3.7 round-3 review: ``--run`` must not be able to execute
    without persisting its 4 artifacts, and must never silently overwrite an
    existing run's artifacts. Called before the registration gate (SS3.7's
    experiment_id/extract/manifest/sha256 checks) so a bad --out-dir refuses
    before any of that ledger/extract/manifest work runs. Returns an error
    message describing the problem, or None if out_dir is usable."""

    if not out_dir:
        return "--run requires --out-dir PATH (a run must not execute without persisting its 4 artifacts)"

    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        return f"--out-dir {out_dir!r} exists and is not a directory"

    required_artifacts = (
        "result.json", "race_metrics.jsonl", "predictions.jsonl", "result_record.draft.json",
    )
    if out.exists():
        existing = [name for name in required_artifacts if (out / name).exists()]
        if existing:
            return (
                f"--out-dir {out_dir!r} already contains existing run artifact(s) "
                f"{existing}; refusing to overwrite a previous run. Use a new, empty directory."
            )

    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".t17a_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"--out-dir {out_dir!r} is not writable: {exc}"
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit-only", action="store_true", help="read-only population/exclusion/time-point audit")
    mode.add_argument("--smoke", action="store_true", help="synthetic-data-only self test")
    mode.add_argument("--run", action="store_true", help="gated real evaluation (refuses unless registered+matched)")
    mode.add_argument("--freeze-extract", action="store_true", help="create a new frozen extract sqlite file")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to data/jra_logging.db (audit-only only)")
    parser.add_argument("--out", default=None, help="output JSON path; defaults to stdout")
    parser.add_argument("--out-dir", default=None,
                         help="run/smoke: directory for result.json, race_metrics.jsonl, "
                              "predictions.jsonl, result_record.draft.json")
    parser.add_argument("--seed", type=int, default=17, help="smoke mode RNG seed")
    parser.add_argument("--experiment-id", default=EXPERIMENT_ID, help="run mode: ledger experiment_id to check")
    parser.add_argument("--extract", default=None, help="run/freeze-extract: frozen extract sqlite path")
    parser.add_argument("--manifest", default=None, help="run mode: manifest.json path (SPEC v3 SS3.7 gate)")
    parser.add_argument("--race-ids-file", default=None, help="freeze-extract: newline-separated race_id file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.audit_only:
        result = run_audit(args.db)
    elif args.smoke:
        if args.out_dir:
            # SPEC v3 SS3.7: full population->fit->metric->bootstrap->
            # judgment->artifact pipeline, synthetic data only.
            outcome = run_smoke_end_to_end(args.out_dir, seed=args.seed)
            result = dict(outcome.result)
            result["mode"] = "smoke"
        else:
            result = run_smoke(args.seed)
    elif args.run:
        out_dir_error = validate_run_out_dir(args.out_dir)
        if out_dir_error:
            print(f"ERROR: {out_dir_error}", file=sys.stderr)
            return 2
        if not args.extract or not args.manifest:
            print("ERROR: --run requires --extract PATH and --manifest PATH", file=sys.stderr)
            return 2
        gate = check_run_gate(args.experiment_id, args.extract, args.manifest)
        if not gate["allowed"]:
            print(f"REFUSED: {gate['reason']}", file=sys.stderr)
            return 2
        # Unreachable against real data in Stage A: the ledger carries no
        # matching T17a registration yet, so check_run_gate always refuses
        # above. Implemented and smoke-tested now so Stage B can use it
        # unchanged once a reviewer has registered and frozen the extract.
        connection = read_only_connect(args.extract)
        try:
            outcome = run_evaluation(connection, enforce_min_population=True)
        finally:
            connection.close()
        result = dict(outcome.result)
        result["mode"] = "run"
        if args.out_dir:
            from eval import ledger as eval_ledger

            records = eval_ledger.load_ledger(LEDGER_PATH)
            registration_record = next(
                r for r in records if r["experiment_id"] == args.experiment_id
            )
            write_run_artifacts(args.out_dir, outcome, registration_record, mode="run")
    else:
        assert args.freeze_extract
        if not args.extract or not args.race_ids_file:
            print("ERROR: --freeze-extract requires --extract PATH and --race-ids-file PATH", file=sys.stderr)
            return 2
        race_ids = [
            line.strip()
            for line in Path(args.race_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        connection = read_only_connect(args.db)
        try:
            sha = create_frozen_extract(connection, args.extract, race_ids)
        finally:
            connection.close()
        result = {"mode": "freeze-extract", "extract_path": args.extract, "sha256": sha, "race_id_count": len(race_ids)}

    if args.out:
        _write_json(args.out, result)
        print(f"wrote {args.out}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
