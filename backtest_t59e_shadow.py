"""SPEC-T59e read-only prospective ticket-board evaluation.

The CLI checks the pre-registered accumulation gate before loading any model
probabilities, outcomes, or prices.  Until then it creates no result files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from backtest_t59c_lambda import ticket_probabilities
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "jra_logging.db"
DEFAULT_OUTPUT = ROOT / "outputs" / "t59e_shadow.json"
REQUIRED_DATES = 4
REQUIRED_RACES = 200
STAGES = ("30", "10", "2")
TICKETS = ("place", "wide", "umaren")
BOOK_MIN, BOOK_MAX = 1.15, 1.45
EPS = 1e-15


@dataclass(frozen=True)
class GateStatus:
    event_dates: int
    races: int

    @property
    def ready(self) -> bool:
        return self.event_dates >= REQUIRED_DATES and self.races >= REQUIRED_RACES


@dataclass
class Capture:
    race_id: str
    date: str
    stage: str
    fetch_id: str
    received_at: str
    board_rows: dict[str, dict[str, dict]]
    horse_numbers: tuple[int, ...]
    win_probabilities: tuple[float, ...]
    book_sum: float


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def accumulation_gate(db_path: Path) -> GateStatus:
    """Read only the two gate counts; do not expose interim model outcomes."""
    with _connect_readonly(db_path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='board_odds_snapshots'").fetchone()
        if table is None:
            return GateStatus(0, 0)
        row = connection.execute(
            "SELECT COUNT(DISTINCT date), COUNT(DISTINCT race_id) "
            "FROM board_odds_snapshots WHERE stage='30' AND status='ok'"
        ).fetchone()
    return GateStatus(int(row[0]), int(row[1]))


def require_accumulation_gate(db_path: Path) -> GateStatus:
    status = accumulation_gate(db_path)
    if not status.ready:
        raise RuntimeError(
            "T59e evaluation blocked: "
            f"{status.races} races / {REQUIRED_RACES} required; "
            f"{status.event_dates} event dates / {REQUIRED_DATES} required"
        )
    return status


def _parse_horse_number(horse_id: str) -> int | None:
    try:
        return int(str(horse_id).rsplit(":", 1)[-1])
    except ValueError:
        return None


def _normalise_win_odds(rows) -> tuple[tuple[int, ...], tuple[float, ...], float] | None:
    values = {}
    declared_sizes = []
    for row in rows:
        if "field_size" in row.keys() and row["field_size"] is not None:
            declared_sizes.append(int(row["field_size"]))
        number = _parse_horse_number(row["horse_id"])
        odds = row["win_odds"]
        if number is None or odds is None:
            continue
        odds = float(odds)
        if not math.isfinite(odds) or not 1.0 < odds < 999.0:
            continue
        values[number] = odds
    field_size = max(declared_sizes) if declared_sizes else len(rows)
    if len(values) < 8 or len(values) != field_size:
        return None
    numbers = tuple(sorted(values))
    inverse = [1.0 / values[number] for number in numbers]
    book_sum = sum(inverse)
    if not BOOK_MIN <= book_sum <= BOOK_MAX:
        return None
    probabilities = tuple(value / book_sum for value in inverse)
    return numbers, probabilities, book_sum


def load_captures(db_path: Path) -> tuple[dict[tuple[str, str], Capture], dict[str, int]]:
    """Choose the earliest successful fetch per race/stage, fixed before results."""
    with _connect_readonly(db_path) as connection:
        board = connection.execute(
            "SELECT * FROM board_odds_snapshots WHERE stage IN ('30','10','2') "
            "ORDER BY race_id, CAST(stage AS INTEGER) DESC, received_at, "
            "board_odds_snapshot_id"
        ).fetchall()
        odds = connection.execute(
            "SELECT race_id,stage,fetch_id,horse_id,win_odds,field_size FROM odds_snapshots "
            "WHERE stage IN ('30','10','2')"
        ).fetchall()
    board_groups = defaultdict(list)
    for row in board:
        board_groups[(row["race_id"], str(row["stage"]), row["fetch_id"] or "")].append(row)
    win_groups = defaultdict(list)
    for row in odds:
        win_groups[(row["race_id"], str(row["stage"]), row["fetch_id"] or "")].append(row)
    candidates = defaultdict(list)
    for key, rows in board_groups.items():
        if any(row["status"] == "ok" for row in rows):
            received = min(str(row["received_at"] or "") for row in rows)
            candidates[key[:2]].append((received, key, rows))
    audit = defaultdict(int)
    captures = {}
    for race_stage, groups in candidates.items():
        received, key, rows = min(groups, key=lambda item: (item[0], item[1][2]))
        normalised = _normalise_win_odds(win_groups.get(key, ()))
        if normalised is None:
            audit["invalid_field_or_book"] += 1
            continue
        numbers, probabilities, book_sum = normalised
        by_ticket = {ticket: {} for ticket in TICKETS}
        for row in rows:
            if row["status"] != "ok" or row["combo"] == "*":
                continue
            by_ticket[row["bet_type"]][row["combo"]] = dict(row)
        captures[race_stage] = Capture(
            race_id=key[0], date=str(rows[0]["date"]), stage=key[1],
            fetch_id=key[2], received_at=received, board_rows=by_ticket,
            horse_numbers=numbers, win_probabilities=probabilities,
            book_sum=book_sum,
        )
        audit["captures"] += 1
    return captures, dict(sorted(audit.items()))


def load_results(db_path: Path) -> dict[str, dict]:
    with _connect_readonly(db_path) as connection:
        rows = connection.execute(
            "SELECT race_id,horse_id,finish_position,official_status,place_payout "
            "FROM race_results ORDER BY race_id,horse_id").fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["race_id"]].append(row)
    results = {}
    for race_id, race_rows in grouped.items():
        positions = defaultdict(list)
        place_payouts = {}
        for row in race_rows:
            if row["official_status"] != "official":
                continue
            number = _parse_horse_number(row["horse_id"])
            position = row["finish_position"]
            if number is None or position is None:
                continue
            positions[int(position)].append(number)
            if row["place_payout"] is not None:
                place_payouts[number] = int(row["place_payout"])
        if any(len(positions[position]) != 1 for position in (1, 2, 3)):
            continue
        finish = tuple(positions[position][0] for position in (1, 2, 3))
        results[race_id] = {"finish": finish, "place_payouts": place_payouts}
    return results


def _combo(numbers) -> str:
    return "-".join(str(number) for number in sorted(numbers))


def _baseline_maps(capture: Capture) -> dict[str, dict[str, float]]:
    probabilities = ticket_probabilities(
        capture.win_probabilities, 1.0, 1.0, places=3)
    place = {str(number): probabilities["place"][index]
             for index, number in enumerate(capture.horse_numbers)}
    wide = {_combo((capture.horse_numbers[i], capture.horse_numbers[j])): value
            for (i, j), value in probabilities["wide"].items()}
    umaren = {_combo((capture.horse_numbers[i], capture.horse_numbers[j])): value
              for (i, j), value in probabilities["umaren"].items()}
    return {"place": place, "wide": wide, "umaren": umaren}


def _actual(ticket: str, combo: str, finish: tuple[int, int, int]) -> bool:
    values = {int(value) for value in combo.split("-")}
    if ticket == "place":
        return bool(values & set(finish))
    if ticket == "wide":
        return values <= set(finish)
    return values == set(finish[:2])


def bernoulli_logloss(observations) -> float:
    values = []
    for probability, actual in observations:
        probability = min(1.0 - EPS, max(EPS, float(probability)))
        values.append(-math.log(probability if actual else 1.0 - probability))
    if not values:
        raise ValueError("observations are empty")
    return sum(values) / len(values)


def realised_logloss(probability: float) -> float:
    return -math.log(max(EPS, float(probability)))


def evaluate_primary(captures, results) -> tuple[list[dict], list[dict], dict[str, int]]:
    race_rows, observations = [], []
    audit = defaultdict(int)
    for (race_id, stage), capture in sorted(captures.items()):
        if stage != "30" or race_id not in results:
            continue
        finish = results[race_id]["finish"]
        baseline = _baseline_maps(capture)
        expected = {
            "place": len(capture.horse_numbers),
            "wide": math.comb(len(capture.horse_numbers), 2),
            "umaren": math.comb(len(capture.horse_numbers), 2),
        }
        for ticket in TICKETS:
            candidate_rows = capture.board_rows[ticket]
            if len(candidate_rows) != expected[ticket]:
                audit[f"incomplete_{ticket}"] += 1
                continue
            if ticket in ("place", "wide"):
                candidate_obs, baseline_obs = [], []
                for combo, row in sorted(candidate_rows.items()):
                    actual = _actual(ticket, combo, finish)
                    candidate_probability = float(row["model_probability"])
                    baseline_probability = baseline[ticket][combo]
                    candidate_obs.append((candidate_probability, actual))
                    baseline_obs.append((baseline_probability, actual))
                    observations.extend((
                        {"ticket": ticket, "model": "candidate",
                         "probability": candidate_probability, "actual": actual},
                        {"ticket": ticket, "model": "baseline",
                         "probability": baseline_probability, "actual": actual},
                    ))
                candidate_loss = bernoulli_logloss(candidate_obs)
                baseline_loss = bernoulli_logloss(baseline_obs)
            else:
                winning_combo = _combo(finish[:2])
                if winning_combo not in candidate_rows:
                    audit["missing_winning_umaren"] += 1
                    continue
                candidate_probability = float(
                    candidate_rows[winning_combo]["model_probability"])
                baseline_probability = baseline[ticket][winning_combo]
                candidate_loss = realised_logloss(candidate_probability)
                baseline_loss = realised_logloss(baseline_probability)
                for combo, row in sorted(candidate_rows.items()):
                    actual = combo == winning_combo
                    observations.extend((
                        {"ticket": ticket, "model": "candidate",
                         "probability": float(row["model_probability"]), "actual": actual},
                        {"ticket": ticket, "model": "baseline",
                         "probability": baseline[ticket][combo], "actual": actual},
                    ))
            race_rows.append({
                "race_id": race_id, "date": capture.date, "ticket": ticket,
                "candidate_logloss": candidate_loss,
                "baseline_logloss": baseline_loss,
            })
            audit[f"evaluated_{ticket}"] += 1
    return race_rows, observations, dict(sorted(audit.items()))


def calibration_curve(observations) -> list[dict]:
    ordered = sorted(observations, key=lambda row: (
        row["probability"], row["actual"]))
    result = []
    for decile in range(10):
        start = len(ordered) * decile // 10
        end = len(ordered) * (decile + 1) // 10
        group = ordered[start:end]
        if group:
            result.append({
                "decile": decile + 1, "n": len(group),
                "mean_probability": sum(row["probability"] for row in group) / len(group),
                "observed_rate": sum(row["actual"] for row in group) / len(group),
            })
    return result


def pearson_correlation(xs, ys) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom_x = sum((x - mean_x) ** 2 for x in xs)
    denom_y = sum((y - mean_y) ** 2 for y in ys)
    if denom_x <= 0.0 or denom_y <= 0.0:
        return None
    return numerator / math.sqrt(denom_x * denom_y)


def evaluate_clv(captures) -> dict:
    output = {}
    for stage in ("30", "10"):
        for ticket in TICKETS:
            deviations, movements = [], []
            for (race_id, capture_stage), early in sorted(captures.items()):
                if capture_stage != stage:
                    continue
                closing = captures.get((race_id, "2"))
                if closing is None:
                    continue
                for combo, row in early.board_rows[ticket].items():
                    close = closing.board_rows[ticket].get(combo)
                    early_odds = row.get("odds")
                    close_odds = close.get("odds") if close else None
                    fair = row.get("fair_odds")
                    if not early_odds or not close_odds or not fair:
                        continue
                    deviations.append(math.log(float(fair) / float(early_odds)))
                    movements.append(math.log(float(close_odds) / float(early_odds)))
            output[f"{ticket}_{stage}m"] = {
                "n": len(deviations),
                "correlation": pearson_correlation(deviations, movements),
                "x": "log(fair_odds_stage / actual_odds_stage)",
                "y": "log(actual_odds_2m / actual_odds_stage)",
            }
    return output


def evaluate_reference_roi(captures, results) -> dict:
    """100-yen shadow selections; 2-minute odds proxy settlement for all tickets."""
    output = {}
    for stage in STAGES:
        for ticket in TICKETS:
            stakes = returns = hits = selections = 0
            for (race_id, capture_stage), capture in sorted(captures.items()):
                if capture_stage != stage or race_id not in results:
                    continue
                closing = captures.get((race_id, "2"))
                if closing is None:
                    continue
                finish = results[race_id]["finish"]
                for combo, row in capture.board_rows[ticket].items():
                    actual_odds, fair = row.get("odds"), row.get("fair_odds")
                    close = closing.board_rows[ticket].get(combo)
                    close_odds = close.get("odds") if close else None
                    if not actual_odds or not fair or not close_odds:
                        continue
                    if float(fair) <= float(actual_odds):
                        continue
                    selections += 1
                    stakes += 100
                    if _actual(ticket, combo, finish):
                        hits += 1
                        returns += round(float(close_odds) * 100)
            output[f"{ticket}_{stage}m"] = {
                "selections": selections, "hits": hits, "stake_yen": stakes,
                "return_yen_2m_odds_proxy": returns,
                "roi": returns / stakes if stakes else None,
            }
    return output


def _bootstrap(ticket_rows) -> dict:
    blocks_candidate, blocks_baseline = defaultdict(list), defaultdict(list)
    for row in ticket_rows:
        blocks_candidate[row["date"]].append({"value": row["candidate_logloss"]})
        blocks_baseline[row["date"]].append({"value": row["baseline_logloss"]})
    metric = lambda rows: sum(row["value"] for row in rows) / len(rows)
    result = paired_block_bootstrap(
        metric, blocks_candidate, blocks_baseline, n_resamples=5000, seed=5905)
    return {
        "candidate_logloss": metric(list(itertools.chain.from_iterable(
            blocks_candidate.values()))),
        "baseline_logloss": metric(list(itertools.chain.from_iterable(
            blocks_baseline.values()))),
        "candidate_minus_baseline": result.observed_difference,
        "ci95": [result.ci_low, result.ci_high], "p_value": result.p_value,
        "event_date_blocks": result.n_blocks, "races": len(ticket_rows),
    }


def run(db_path: Path, gate: GateStatus) -> tuple[dict, list[dict]]:
    captures, capture_audit = load_captures(db_path)
    results = load_results(db_path)
    race_rows, observations, evaluation_audit = evaluate_primary(captures, results)
    primary, calibration = {}, {}
    for ticket in TICKETS:
        rows = [row for row in race_rows if row["ticket"] == ticket]
        if rows:
            primary[ticket] = _bootstrap(rows)
        for model in ("candidate", "baseline"):
            calibration[f"{ticket}_{model}"] = calibration_curve([
                row for row in observations
                if row["ticket"] == ticket and row["model"] == model])
    daily = []
    grouped = defaultdict(list)
    for row in race_rows:
        grouped[(row["date"], row["ticket"])].append(row)
    for (date, ticket), rows in sorted(grouped.items()):
        daily.append({
            "date": date, "ticket": ticket, "races": len(rows),
            "candidate_logloss": sum(row["candidate_logloss"] for row in rows) / len(rows),
            "baseline_logloss": sum(row["baseline_logloss"] for row in rows) / len(rows),
        })
    payload = {
        "spec": "T59e", "gate": {"event_dates": gate.event_dates, "races": gate.races,
                                      "required_event_dates": REQUIRED_DATES,
                                      "required_races": REQUIRED_RACES},
        "capture_audit": capture_audit, "evaluation_audit": evaluation_audit,
        "primary": primary, "calibration": calibration,
        "clv": evaluate_clv(captures),
        "reference_roi": evaluate_reference_roi(captures, results),
        "reference_roi_definition": (
            "100 yen per fair_odds > actual_odds combination; winning return uses "
            "the 2-minute actual odds as a near-final proxy; never connected to display/notifications"
        ),
        "daily_ticket_breakdown": daily,
    }
    return payload, daily


def write_outputs(payload, daily, output_path: Path) -> tuple[str, Path]:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(encoded)
    daily_path = output_path.with_name(output_path.stem + "_daily.csv")
    with daily_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "date", "ticket", "races", "candidate_logloss", "baseline_logloss"))
        writer.writeheader()
        writer.writerows(daily)
    return hashlib.sha256(encoded).hexdigest(), daily_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    gate = require_accumulation_gate(args.db)
    payload, daily = run(args.db, gate)
    digest, daily_path = write_outputs(payload, daily, args.output)
    print(digest)
    print(daily_path)


if __name__ == "__main__":
    main()
