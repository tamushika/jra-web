"""SPEC-T63 read-only counterfactual EV threshold evaluation.

Before the distribution gate this module reads only two accumulation counts.
It never writes to the logging database and never changes production alerts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from eval.cutoff import DISQUALIFYING_QUALITY_FLAGS
from eval.ledger import load_ledger


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "jra_logging.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_MODEL = ROOT / "api" / "data_files" / "common" / "win5_ml_model.json"
DEFAULT_SPEC = ROOT / "docs" / "codex" / "SPEC-T63-ev-threshold-rederivation-v2.md"
DEFAULT_OUTPUT = ROOT / "outputs" / "t63_ev_threshold.json"
EXPERIMENT_ID = "T63-ev-threshold-rederivation-v2"
PROSPECTIVE_START = "2026-07-25"
THRESHOLDS = (1.00, 1.05, 1.10, 1.15, 1.20, 1.30)
CURRENT_THRESHOLD = 1.30
MIN_PROBABILITY = 0.02
MAX_ODDS = 50.0
DISTRIBUTION_DAYS = 4
DISTRIBUTION_RACES = 200
ADJUDICATION_DAYS = 12
ADJUDICATION_NOTIFICATIONS = 100
BOOTSTRAP_RESAMPLES = 2_000
BOOTSTRAP_SEED = 6301
MODEL_SHA256 = "8687f9bfa2278ed1dcafd9f13c90b08fa6b6d58f993a2c139fd39c9edbf34527"
SPEC_SHA256 = "e1077829e5bba06a1fe3ef53bb963ce38bdc86ec33513b8d1bde44dd046726bf"
CONTRACT_SHA256 = "72d34f149e246abd1bf7bc9d0eb0dd62980de636b8af2968e1b635886e3f4c81"
BOOK_MIN, BOOK_MAX = 1.15, 1.45


class T63Error(RuntimeError):
    pass


@dataclass(frozen=True)
class GateStatus:
    event_dates: int
    races: int

    @property
    def distribution_ready(self) -> bool:
        return (
            self.event_dates >= DISTRIBUTION_DAYS
            and self.races >= DISTRIBUTION_RACES
        )


@dataclass(frozen=True)
class CounterfactualRow:
    race_id: str
    event_date: str
    horse_id: str
    probability: float
    cutoff_odds: float
    popularity: int | None
    ev: float
    won: bool = False
    final_odds: float | None = None
    win_payout: int | None = None


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def accumulation_gate(path: Path) -> GateStatus:
    """Read only the pre-registered two counts."""
    if not Path(path).is_file():
        return GateStatus(0, 0)
    with _connect_readonly(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='board_odds_snapshots'"
        ).fetchone()
        if exists is None:
            return GateStatus(0, 0)
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT date), COUNT(DISTINCT race_id)
            FROM board_odds_snapshots
            WHERE date>=? AND stage='30' AND status='ok'
            """,
            (PROSPECTIVE_START.replace("-", ""),),
        ).fetchone()
    return GateStatus(int(row[0]), int(row[1]))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_contract(
    *, ledger_path: Path = DEFAULT_LEDGER, model_path: Path = DEFAULT_MODEL,
    spec_path: Path = DEFAULT_SPEC,
) -> dict[str, Any]:
    records = load_ledger(ledger_path)
    matches = [
        row for row in records if row["experiment_id"] == EXPERIMENT_ID
    ]
    if len(matches) != 1:
        raise T63Error(f"missing or duplicate ledger row: {EXPERIMENT_ID}")
    row = matches[0]
    expected_grid = {
        "candidate_thresholds": list(THRESHOLDS),
        "gates": {
            "adjudication": {
                "min_counterfactual_notifications_at_leading_threshold":
                    ADJUDICATION_NOTIFICATIONS,
                "min_event_days": ADJUDICATION_DAYS,
                "output": "all_metrics",
            },
            "distribution_report": {
                "min_event_days": DISTRIBUTION_DAYS,
                "min_races": DISTRIBUTION_RACES,
                "output": "distribution_shape_only",
            },
        },
        "line_free_tier_constraint":
            "projected_notifications_per_month<=140 (70% of 200)",
        "population_params": {
            "ev_threshold_current": CURRENT_THRESHOLD,
            "max_odds": MAX_ODDS,
            "min_prob": MIN_PROBABILITY,
        },
    }
    failures = []
    if row["benchmark_type"] != "prospective":
        failures.append("benchmark_type")
    if row["prospective_start_date"] != PROSPECTIVE_START:
        failures.append("prospective_start_date")
    if row["candidate_count"] != len(THRESHOLDS):
        failures.append("candidate_count")
    if row["search_grid"] != expected_grid:
        failures.append("search_grid")
    hashes = row.get("data_hashes", {})
    if hashes.get("production_model_sha256") != MODEL_SHA256:
        failures.append("registered_model_sha256")
    if hashes.get("spec_sha256") != SPEC_SHA256:
        failures.append("registered_spec_sha256")
    if hashes.get("t63_contract_sha256") != CONTRACT_SHA256:
        failures.append("registered_contract_sha256")
    if _sha256(model_path) != MODEL_SHA256:
        failures.append("production_model_changed_restart_required")
    if _sha256(spec_path) != SPEC_SHA256:
        failures.append("spec_changed")
    if failures:
        raise T63Error("T63 frozen contract mismatch: " + ", ".join(failures))
    return {
        "experiment_id": EXPERIMENT_ID,
        "model_sha256": MODEL_SHA256,
        "spec_sha256": SPEC_SHA256,
        "contract_sha256": CONTRACT_SHA256,
    }


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _flags(value: Any) -> set[str] | None:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or not all(
        isinstance(flag, str) for flag in parsed
    ):
        return None
    return set(parsed)


def _horse_number(horse_id: str) -> int | None:
    try:
        return int(str(horse_id).rsplit(":", 1)[-1])
    except ValueError:
        return None


def _valid_capture(rows: list[sqlite3.Row]) -> bool:
    if not rows:
        return False
    field_sizes = {
        int(row["field_size"]) for row in rows if row["field_size"] is not None
    }
    if len(field_sizes) != 1 or len(rows) != next(iter(field_sizes)):
        return False
    numbers, inverse, probabilities = set(), [], []
    for row in rows:
        number = _horse_number(row["horse_id"])
        odds = row["win_odds"]
        probability = row["win_probability"]
        flags = _flags(row["quality_flags"])
        observed = _aware(row["observed_at"])
        post = _aware(row["scheduled_post_at"])
        if (
            number is None or number in numbers
            or odds is None or not 1.0 < float(odds) < 999.0
            or probability is None or not 0.0 <= float(probability) <= 1.0
            or flags is None or flags & DISQUALIFYING_QUALITY_FLAGS
            or int(row["is_stale"] or 0) != 0
            or observed is None or post is None or observed > post
        ):
            return False
        numbers.add(number)
        inverse.append(1.0 / float(odds))
        probabilities.append(float(probability))
    return (
        len(rows) >= 8
        and BOOK_MIN <= sum(inverse) <= BOOK_MAX
        and abs(sum(probabilities) - 1.0) <= 1e-6
    )


def load_counterfactual_rows(
    path: Path, *, include_results: bool,
) -> tuple[list[CounterfactualRow], dict[str, int]]:
    """Select the final valid pre-post capture per race deterministically."""
    with _connect_readonly(path) as connection:
        raw = connection.execute(
            """
            SELECT o.race_id,o.horse_id,o.observed_at,o.win_odds,o.popularity,
                   o.fetch_id,o.is_stale,o.data_quality_flags_json AS quality_flags,
                   o.scheduled_post_at,o.field_size,
                   COALESCE(p.calibrated_win_probability,
                            p.raw_win_probability) AS win_probability
            FROM odds_snapshots o
            JOIN predictions p
              ON p.prediction_run_id=o.fetch_id
             AND p.race_id=o.race_id AND p.horse_id=o.horse_id
            WHERE substr(o.race_id,1,8)>=?
              AND EXISTS (
                SELECT 1 FROM board_odds_snapshots b
                WHERE b.race_id=o.race_id AND b.fetch_id=o.fetch_id
                  AND b.status='ok'
              )
              AND NOT EXISTS (
                SELECT 1 FROM board_odds_snapshots b
                WHERE b.race_id=o.race_id AND b.fetch_id=o.fetch_id
                  AND b.status<>'ok'
              )
            ORDER BY o.race_id,o.observed_at,o.fetch_id,o.horse_id
            """,
            (PROSPECTIVE_START.replace("-", ""),),
        ).fetchall()
        results = {}
        if include_results:
            results = {
                (row["race_id"], row["horse_id"]): row
                for row in connection.execute(
                    """
                    SELECT race_id,horse_id,finish_position,official_status,
                           final_win_odds,win_payout
                    FROM race_results
                    """
                )
            }
    captures: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in raw:
        captures[(row["race_id"], row["fetch_id"])].append(row)
    valid: dict[str, tuple[datetime, str, list[sqlite3.Row]]] = {}
    audit = defaultdict(int)
    for (race_id, fetch_id), rows in sorted(captures.items()):
        if not _valid_capture(rows):
            audit["invalid_capture"] += 1
            continue
        observed = max(_aware(row["observed_at"]) for row in rows)
        candidate = (observed, fetch_id, rows)
        if race_id not in valid or candidate[:2] > valid[race_id][:2]:
            valid[race_id] = candidate
    output = []
    for race_id, (_, _, rows) in sorted(valid.items()):
        event_date = race_id[:8]
        if include_results:
            winners = [
                row for (result_race, _), row in results.items()
                if result_race == race_id
                and row["official_status"] == "official"
                and row["finish_position"] == 1
            ]
            if len(winners) != 1:
                audit["unsettled_or_dead_heat_race"] += 1
                continue
        for row in rows:
            probability = float(row["win_probability"])
            odds = float(row["win_odds"])
            if probability < MIN_PROBABILITY or odds > MAX_ODDS:
                continue
            result = results.get((race_id, row["horse_id"]))
            won = bool(
                result is not None
                and result["official_status"] == "official"
                and result["finish_position"] == 1
            )
            output.append(CounterfactualRow(
                race_id=race_id, event_date=event_date,
                horse_id=row["horse_id"], probability=probability,
                cutoff_odds=odds,
                popularity=(
                    int(row["popularity"])
                    if row["popularity"] is not None else None
                ),
                ev=probability * odds, won=won,
                final_odds=(
                    float(result["final_win_odds"])
                    if result is not None and result["final_win_odds"] is not None
                    else None
                ),
                win_payout=(
                    int(result["win_payout"])
                    if result is not None and result["win_payout"] is not None
                    else None
                ),
            ))
    audit["valid_races"] = len(valid)
    audit["eligible_horses"] = len(output)
    return output, dict(sorted(audit.items()))


def reconstruct_ev(probability: float, cutoff_odds: float) -> float:
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability outside [0,1]")
    if not math.isfinite(cutoff_odds) or cutoff_odds <= 1.0:
        raise ValueError("invalid cutoff odds")
    return probability * cutoff_odds


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution_report(rows: list[CounterfactualRow]) -> dict[str, Any]:
    values = [row.ev for row in rows]
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_day[row.event_date].append(row.ev)
    return {
        "eligible_horses": len(values),
        "ev_quantiles": {
            key: _quantile(values, probability)
            for key, probability in (
                ("p50", .50), ("p75", .75), ("p90", .90),
                ("p95", .95), ("p99", .99),
            )
        },
        "daily_max": [
            {"date": key, "max_ev": max(day_values)}
            for key, day_values in sorted(by_day.items())
        ],
    }


def notification_count(
    rows: Iterable[CounterfactualRow], threshold: float,
) -> int:
    if threshold not in THRESHOLDS:
        raise T63Error("leading threshold must be in the frozen grid")
    return sum(row.ev >= threshold for row in rows)


def _roi(rows: list[CounterfactualRow], *, final: bool) -> float | None:
    if not rows:
        return None
    returns = 0.0
    for row in rows:
        if not row.won:
            continue
        if final:
            returns += (
                float(row.win_payout)
                if row.win_payout is not None
                else 100.0 * float(row.final_odds or 0.0)
            )
        else:
            returns += 100.0 * row.cutoff_odds
    return returns / (100.0 * len(rows))


def _bootstrap_roi(rows: list[CounterfactualRow]) -> list[float] | None:
    blocks: dict[str, list[CounterfactualRow]] = defaultdict(list)
    for row in rows:
        blocks[row.event_date].append(row)
    keys = sorted(blocks)
    if not keys:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    values = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = []
        for _ in keys:
            sample.extend(blocks[rng.choice(keys)])
        values.append(float(_roi(sample, final=True)))
    return [_quantile(values, .025), _quantile(values, .975)]


def full_metrics(rows: list[CounterfactualRow]) -> dict[str, Any]:
    event_dates = sorted({row.event_date for row in rows})
    if event_dates:
        elapsed = (
            date.fromisoformat(
                f"{event_dates[-1][:4]}-{event_dates[-1][4:6]}-{event_dates[-1][6:]}"
            )
            - date.fromisoformat(
                f"{event_dates[0][:4]}-{event_dates[0][4:6]}-{event_dates[0][6:]}"
            )
        ).days + 1
    else:
        elapsed = 0
    output = {}
    for threshold in THRESHOLDS:
        selected = [row for row in rows if row.ev >= threshold]
        predicted = sum(row.probability for row in selected)
        wins = sum(row.won for row in selected)
        bands = {}
        for name, predicate in (
            ("1-3", lambda value: value is not None and value <= 3),
            ("4-8", lambda value: value is not None and 4 <= value <= 8),
            ("9+", lambda value: value is not None and value >= 9),
        ):
            group = [row for row in selected if predicate(row.popularity)]
            bands[name] = {"n": len(group), "wins": sum(row.won for row in group)}
        movements = [
            math.log(row.final_odds / row.cutoff_odds)
            for row in selected
            if row.final_odds is not None and row.final_odds > 0
        ]
        output[f"{threshold:.2f}"] = {
            "notifications": len(selected),
            "predicted_wins": predicted,
            "realized_wins": wins,
            "calibration_ratio": wins / predicted if predicted else None,
            "cutoff_odds_roi": _roi(selected, final=False),
            "final_odds_roi": _roi(selected, final=True),
            "final_roi_event_day_bootstrap_ci95": _bootstrap_roi(selected),
            "projected_notifications_per_month": (
                len(selected) / elapsed * 30.4375 if elapsed else None
            ),
            "popularity_bands": bands,
            "log_final_to_cutoff_odds": {
                "n": len(movements),
                "mean": sum(movements) / len(movements) if movements else None,
                "p50": _quantile(movements, .5),
            },
        }
    return output


def evaluate(
    db_path: Path, *, leading_threshold: float | None = None,
) -> dict[str, Any]:
    contract = verify_contract()
    gate = accumulation_gate(db_path)
    base = {
        "spec": "T63",
        "contract": contract,
        "gate": {
            "event_dates": gate.event_dates, "races": gate.races,
            "required_event_dates": DISTRIBUTION_DAYS,
            "required_races": DISTRIBUTION_RACES,
        },
    }
    if not gate.distribution_ready:
        return {**base, "phase": "blocked_distribution_gate"}
    rows, audit = load_counterfactual_rows(db_path, include_results=False)
    distribution = distribution_report(rows)
    if leading_threshold is None:
        return {
            **base, "phase": "distribution_only",
            "distribution": distribution, "capture_audit": audit,
            "adjudication_gate": {"status": "leading_threshold_not_supplied"},
        }
    count = notification_count(rows, leading_threshold)
    ready = (
        gate.event_dates >= ADJUDICATION_DAYS
        and count >= ADJUDICATION_NOTIFICATIONS
    )
    if not ready:
        return {
            **base, "phase": "distribution_only",
            "distribution": distribution, "capture_audit": audit,
            "adjudication_gate": {
                "leading_threshold": leading_threshold,
                "counterfactual_notifications": count,
                "required_event_dates": ADJUDICATION_DAYS,
                "required_notifications": ADJUDICATION_NOTIFICATIONS,
                "ready": False,
            },
        }
    settled, settled_audit = load_counterfactual_rows(
        db_path, include_results=True,
    )
    return {
        **base, "phase": "all_metrics",
        "distribution": distribution,
        "capture_audit": settled_audit,
        "adjudication_gate": {
            "leading_threshold": leading_threshold,
            "counterfactual_notifications": count,
            "required_event_dates": ADJUDICATION_DAYS,
            "required_notifications": ADJUDICATION_NOTIFICATIONS,
            "ready": True,
        },
        "metrics": full_metrics(settled),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--leading-threshold", type=float)
    args = parser.parse_args(argv)
    payload = evaluate(args.db, leading_threshold=args.leading_threshold)
    if payload["phase"] == "blocked_distribution_gate":
        gate = payload["gate"]
        print(
            "T63 evaluation blocked: "
            f"{gate['races']} races / {gate['required_races']} required; "
            f"{gate['event_dates']} event dates / "
            f"{gate['required_event_dates']} required"
        )
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
