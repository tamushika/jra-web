"""Read-only prospective evaluator for SPEC-T62b.

Until the sealed 4-event-day/200-race gate is reached, only gate counts are
read and no result or probability rows are loaded.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "jra_logging.db"
DEFAULT_OUTPUT = ROOT / "outputs" / "t62b_shadow.json"
EXPERIMENT_ID = "T62b-race-selection-shadow-v1"
REQUIRED_EVENT_DAYS = 4
REQUIRED_RACES = 200
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 6202
EPS = 1e-15

EVALUATION_CONTRACT = {
    "experiment_id": EXPERIMENT_ID,
    "population": "successful frozen T62 score at fixed 30-minute cutoff",
    "gate": {"event_days": 4, "races": 200, "pre_gate_output": "counts_only"},
    "metrics": [
        "selected/unselected model-minus-market winner logloss",
        "JST-event-day block bootstrap 2000 seed 6202",
        "selected market top-k floor k=1..4",
        "selection rate", "winner popularity bands 1-3/4-8/9+",
        "cutoff-versus-final win-odds distribution",
    ],
    "adjudication": "none",
}


@dataclass(frozen=True)
class GateStatus:
    event_days: int
    races: int

    @property
    def ready(self):
        return self.event_days >= REQUIRED_EVENT_DAYS and self.races >= REQUIRED_RACES


def _connect_readonly(path: Path):
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def accumulation_gate(db_path: Path) -> GateStatus:
    with _connect_readonly(db_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='race_confidence_snapshots'").fetchone()
        if exists is None:
            return GateStatus(0, 0)
        row = connection.execute(
            "SELECT COUNT(DISTINCT date), "
            "COUNT(DISTINCT date || ':' || place || ':' || printf('%02d', r)) "
            "FROM race_confidence_snapshots WHERE score IS NOT NULL "
            "AND manifest_sha256=?", ("db8df644fc5025f91d1ae6a7c67fe394a02f690a54f73f8b6aeb4d445055b1f3",)
        ).fetchone()
    return GateStatus(int(row[0]), int(row[1]))


def require_accumulation_gate(db_path: Path) -> GateStatus:
    status = accumulation_gate(db_path)
    if not status.ready:
        raise RuntimeError(
            f"T62b evaluation blocked: {status.races}/{REQUIRED_RACES} races; "
            f"{status.event_days}/{REQUIRED_EVENT_DAYS} event days")
    return status


def _horse_number(horse_id):
    return int(str(horse_id).rsplit(":", 1)[-1])


def load_observations(db_path: Path):
    with _connect_readonly(db_path) as connection:
        snapshots = connection.execute("""SELECT * FROM race_confidence_snapshots
            WHERE score IS NOT NULL ORDER BY date,place,r,created_at,
            race_confidence_snapshot_id""").fetchall()
        results = connection.execute("""SELECT race_id,horse_id,finish_position,
            official_status,final_win_odds FROM race_results
            WHERE official_status='official'""").fetchall()
    result_maps = defaultdict(lambda: {"winner": None, "final_odds": {}})
    for row in results:
        number = _horse_number(row["horse_id"])
        if row["final_win_odds"] is not None:
            result_maps[row["race_id"]]["final_odds"][number] = float(row["final_win_odds"])
        if row["finish_position"] == 1:
            result_maps[row["race_id"]]["winner"] = number
    first = {}
    for row in snapshots:
        key = (row["date"], row["place"], int(row["r"]))
        first.setdefault(key, row)
    observations = []
    for (date, place, race_no), row in first.items():
        race_id = f"{date}:{place}:{race_no:02d}"
        result = result_maps.get(race_id)
        if not result or result["winner"] is None:
            continue
        winner = result["winner"]
        model = {int(key): float(value) for key, value in json.loads(row["model_probs_json"]).items()}
        odds = {int(key): float(value) for key, value in json.loads(row["market_odds_json"]).items()}
        if winner not in model or winner not in odds:
            continue
        inverse_sum = sum(1.0 / value for value in odds.values())
        market = {key: (1.0 / value) / inverse_sum for key, value in odds.items()}
        market_rank = sorted(market, key=market.get, reverse=True).index(winner) + 1
        observations.append({
            "race_id": race_id, "date": date, "selected": bool(row["selected"]),
            "winner": winner, "model": model, "market": market,
            "model_loss": -math.log(max(EPS, model[winner])),
            "market_loss": -math.log(max(EPS, market[winner])),
            "market_rank": market_rank, "cutoff_odds": odds[winner],
            "final_odds": result["final_odds"].get(winner),
            "cutoff_odds_by_horse": odds,
            "final_odds_by_horse": result["final_odds"],
        })
    return observations


def _mean(rows, key):
    return sum(float(row[key]) for row in rows) / len(rows)


def _bootstrap(rows, seed=BOOTSTRAP_SEED):
    blocks = defaultdict(list)
    for row in rows:
        blocks[row["date"]].append(row)
    dates = sorted(blocks)
    rng = random.Random(seed)
    differences = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = [row for _date in (rng.choice(dates) for _ in dates) for row in blocks[_date]]
        differences.append(_mean(sample, "model_loss") - _mean(sample, "market_loss"))
    differences.sort()
    return {"ci_low": differences[int(.025 * (len(differences) - 1))],
            "ci_high": differences[int(.975 * (len(differences) - 1))],
            "resamples": BOOTSTRAP_RESAMPLES, "seed": seed}


def _subset_report(rows):
    if not rows:
        return {"races": 0}
    difference = _mean(rows, "model_loss") - _mean(rows, "market_loss")
    bands = {}
    for label, low, high in (("1-3", 1, 3), ("4-8", 4, 8), ("9+", 9, 10**6)):
        band = [row for row in rows if low <= row["market_rank"] <= high]
        bands[label] = ({"races": len(band), "difference":
                         _mean(band, "model_loss") - _mean(band, "market_loss")}
                        if band else {"races": 0})
    return {"races": len(rows), "model_logloss": _mean(rows, "model_loss"),
            "market_logloss": _mean(rows, "market_loss"), "difference": difference,
            "bootstrap": _bootstrap(rows), "popularity_bands": bands}


def evaluate(rows, gate):
    selected = [row for row in rows if row["selected"]]
    unselected = [row for row in rows if not row["selected"]]
    topk = {}
    for k in range(1, 5):
        model_hits = sum(row["winner"] in sorted(row["model"], key=row["model"].get,
                                                  reverse=True)[:k] for row in selected)
        market_hits = sum(row["market_rank"] <= k for row in selected)
        topk[str(k)] = {"model_rate": model_hits / len(selected) if selected else None,
                        "market_rate": market_hits / len(selected) if selected else None,
                        "floor_pass": model_hits >= market_hits if selected else None}
    odds_change = []
    for row in rows:
        for horse, cutoff_odds in row["cutoff_odds_by_horse"].items():
            final_odds = row["final_odds_by_horse"].get(horse)
            if final_odds is not None:
                odds_change.append(float(final_odds) / cutoff_odds - 1.0)
    return {"experiment_id": EXPERIMENT_ID, "adjudication": None,
            "gate": {"event_days": gate.event_days, "races": gate.races, "ready": True},
            "joined_result_races": len(rows), "selection_rate": len(selected) / len(rows) if rows else None,
            "selected": _subset_report(selected), "unselected": _subset_report(unselected),
            "selected_market_topk_floor": topk,
            "cutoff_final_odds": {"races": len(odds_change),
                "mean_relative_change": sum(odds_change) / len(odds_change) if odds_change else None,
                "min_relative_change": min(odds_change) if odds_change else None,
                "max_relative_change": max(odds_change) if odds_change else None}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    gate = require_accumulation_gate(args.db)
    report = evaluate(load_observations(args.db), gate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["gate"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
