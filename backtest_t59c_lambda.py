"""SPEC-T59c isolated discounted-Harville evaluation harness.

This module deliberately does not import or modify ``api.combo_probs``.  The
CLI is fail-closed until the three ticket-specific T39 registrations exist.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from backtest_win5 import parse_final_odds
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "t59c_lambda_result.json"
ABILITY_DB_SHA256 = "7ffcfe21618612b603053544cd888eec637b6fdf69192470c5751c5f89b00c79"
EXPERIMENT_IDS = (
    "T59c-place-lambda-v1",
    "T59c-wide-lambda-v1",
    "T59c-umaren-lambda-v1",
)
BOOK_MIN, BOOK_MAX = 1.15, 1.45
EPS = 1e-15


@dataclass(frozen=True)
class Race:
    race_id: str
    probabilities: tuple[float, ...]
    finish: tuple[int, int, int]
    popularity: tuple[int | None, ...]
    book_sum: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_registrations(ledger_path: Path, db_path: Path) -> None:
    """Require all three pre-evaluation base registrations and sealed DB."""
    actual_sha = sha256_file(db_path)
    if actual_sha != ABILITY_DB_SHA256:
        raise RuntimeError(
            f"ability.db SHA mismatch: expected {ABILITY_DB_SHA256}, got {actual_sha}"
        )
    registrations = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        experiment_id = row.get("experiment_id")
        if experiment_id in EXPERIMENT_IDS and row.get("result_summary") is None:
            registrations[experiment_id] = row
    missing = [item for item in EXPERIMENT_IDS if item not in registrations]
    if missing:
        raise RuntimeError(
            "T59c evaluation blocked: register three T39 experiments first; "
            f"missing={','.join(missing)}"
        )
    for experiment_id, row in registrations.items():
        sealed = row.get("data_hashes", {}).get("ability_db_sha256")
        if sealed != actual_sha:
            raise RuntimeError(
                f"{experiment_id} seals {sealed!r}, not current ability.db {actual_sha}"
            )


def market_probabilities(odds: list[float]) -> tuple[tuple[float, ...], float] | None:
    if len(odds) < 2 or any(not math.isfinite(x) or x <= 1.0 for x in odds):
        return None
    inverse = [1.0 / x for x in odds]
    book_sum = sum(inverse)
    if not (BOOK_MIN <= book_sum <= BOOK_MAX):
        return None
    probabilities = tuple(value / book_sum for value in inverse)
    assert math.isclose(sum(probabilities), 1.0, abs_tol=1e-12)
    return probabilities, book_sum


def conditional_probabilities(
    probabilities: tuple[float, ...], excluded: tuple[int, ...], exponent: float
) -> tuple[float, ...]:
    weights = [0.0 if i in excluded else p ** exponent
               for i, p in enumerate(probabilities)]
    denominator = sum(weights)
    if denominator <= 0.0:
        raise ValueError("conditional denominator is zero")
    result = tuple(weight / denominator for weight in weights)
    assert math.isclose(sum(result), 1.0, abs_tol=1e-12)
    assert all(result[i] == 0.0 for i in excluded)
    return result


def stage_log_likelihood(races: list[Race], stage: int, exponent: float) -> float:
    if stage not in (2, 3):
        raise ValueError("stage must be 2 or 3")
    total = 0.0
    for race in races:
        first, second, third = race.finish
        excluded = (first,) if stage == 2 else (first, second)
        actual = second if stage == 2 else third
        probability = conditional_probabilities(
            race.probabilities, excluded, exponent)[actual]
        total += math.log(max(probability, EPS))
    return total


def fit_stage_lambda(races: list[Race], stage: int) -> float:
    """Deterministic golden-section MLE on the fixed [0.05, 2.0] domain."""
    if not races:
        raise ValueError("at least one race is required")
    left, right = 0.05, 2.0
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    x1, x2 = right - ratio * (right - left), left + ratio * (right - left)
    f1 = stage_log_likelihood(races, stage, x1)
    f2 = stage_log_likelihood(races, stage, x2)
    for _ in range(96):
        if f1 < f2:
            left, x1, f1 = x1, x2, f2
            x2 = left + ratio * (right - left)
            f2 = stage_log_likelihood(races, stage, x2)
        else:
            right, x2, f2 = x2, x1, f1
            x1 = right - ratio * (right - left)
            f1 = stage_log_likelihood(races, stage, x1)
    return (left + right) / 2.0


def ticket_probabilities(
    probabilities: tuple[float, ...], lambda2: float, lambda3: float,
    places: int = 3,
) -> dict[str, object]:
    """Derive place, wide, umaren and sanrenpuku from one stage model."""
    n = len(probabilities)
    if places not in (2, 3) or places > n:
        raise ValueError("places must be 2 or 3 and no greater than field size")
    place = [0.0] * n
    umaren = defaultdict(float)
    wide = defaultdict(float)
    sanrenpuku = defaultdict(float)
    ordered_top3 = {}
    for first in range(n):
        second_probs = conditional_probabilities(probabilities, (first,), lambda2)
        for second in range(n):
            if second == first:
                continue
            p12 = probabilities[first] * second_probs[second]
            umaren[tuple(sorted((first, second)))] += p12
            if places == 2:
                place[first] += p12
                place[second] += p12
            third_probs = conditional_probabilities(
                probabilities, (first, second), lambda3)
            for third in range(n):
                if third in (first, second):
                    continue
                value = p12 * third_probs[third]
                ordered_top3[(first, second, third)] = value
                sanrenpuku[tuple(sorted((first, second, third)))] += value
                if places == 3:
                    place[first] += value
                    place[second] += value
                    place[third] += value
                for pair in itertools.combinations(sorted((first, second, third)), 2):
                    wide[pair] += value
    assert math.isclose(sum(place), float(places), abs_tol=1e-10)
    assert math.isclose(sum(umaren.values()), 1.0, abs_tol=1e-10)
    assert math.isclose(sum(sanrenpuku.values()), 1.0, abs_tol=1e-10)
    return {
        "place": tuple(place), "wide": dict(wide), "umaren": dict(umaren),
        "sanrenpuku": dict(sanrenpuku), "ordered_top3": ordered_top3,
    }


def load_races(db_path: Path) -> tuple[list[Race], dict[str, int]]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT date, place, r, umaban, popularity, rank, total_horses, win_pay "
        "FROM runs WHERE date BETWEEN '20210101' AND '20260630' "
        "ORDER BY date, place, r, umaban"
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[(str(row[0]), str(row[1]), int(row[2]))].append(row)
    connection.close()
    audit = defaultdict(int)
    races = []
    for (date, place, race_no), runners in grouped.items():
        if len(runners) < 8:
            audit["field_under_8"] += 1
            continue
        ranks = [runner[5] for runner in runners]
        if any(ranks.count(position) != 1 for position in (1, 2, 3)):
            audit["ambiguous_top3"] += 1
            continue
        odds = [parse_final_odds(runner[7], runner[5]) for runner in runners]
        if any(value is None for value in odds):
            audit["incomplete_odds"] += 1
            continue
        parsed = market_probabilities([float(value) for value in odds])
        if parsed is None:
            audit["outside_book_band"] += 1
            continue
        probabilities, book_sum = parsed
        finish = tuple(ranks.index(position) for position in (1, 2, 3))
        popularity = tuple(
            int(runner[4]) if runner[4] is not None else None for runner in runners
        )
        race_id = f"{date}{place}{race_no:02d}"
        races.append(Race(race_id, probabilities, finish, popularity, book_sum))
        audit["included"] += 1
    return races, dict(sorted(audit.items()))


def _bernoulli_loss(probability: float, actual: bool) -> float:
    probability = min(1.0 - EPS, max(EPS, probability))
    return -math.log(probability if actual else 1.0 - probability)


def race_metrics(race: Race, lambda2: float, lambda3: float) -> dict[str, float]:
    tickets = ticket_probabilities(race.probabilities, lambda2, lambda3)
    top3 = set(race.finish)
    place_values = tickets["place"]
    place_loss = sum(_bernoulli_loss(value, i in top3)
                     for i, value in enumerate(place_values)) / len(place_values)
    place_brier = sum((value - (i in top3)) ** 2
                      for i, value in enumerate(place_values)) / len(place_values)
    pairs = list(itertools.combinations(range(len(race.probabilities)), 2))
    wide_values = tickets["wide"]
    wide_loss = sum(_bernoulli_loss(wide_values[pair], set(pair) <= top3)
                    for pair in pairs) / len(pairs)
    wide_brier = sum((wide_values[pair] - float(set(pair) <= top3)) ** 2
                     for pair in pairs) / len(pairs)
    actual_pair = tuple(sorted(race.finish[:2]))
    actual_triple = tuple(sorted(race.finish))
    umaren_brier = sum(
        (value - float(pair == actual_pair)) ** 2
        for pair, value in tickets["umaren"].items()
    )
    return {
        "place_logloss": place_loss, "place_brier": place_brier,
        "wide_logloss": wide_loss, "wide_brier": wide_brier,
        "umaren_logloss": -math.log(max(tickets["umaren"][actual_pair], EPS)),
        "umaren_brier": umaren_brier,
        "sanrenpuku_logloss": -math.log(
            max(tickets["sanrenpuku"][actual_triple], EPS)),
    }


def _popularity_band(popularity: int | None) -> str:
    if popularity is None:
        return "unknown"
    if popularity <= 3:
        return "1-3"
    if popularity <= 8:
        return "4-8"
    return "9+"


def _calibration_curve(observations: list[tuple[float, bool, str]]) -> list[dict]:
    ordered = sorted(observations, key=lambda item: (item[0], item[1], item[2]))
    result = []
    for decile in range(10):
        start = len(ordered) * decile // 10
        end = len(ordered) * (decile + 1) // 10
        group = ordered[start:end]
        if not group:
            continue
        result.append({
            "decile": decile + 1, "n": len(group),
            "mean_probability": sum(item[0] for item in group) / len(group),
            "observed_rate": sum(item[1] for item in group) / len(group),
        })
    return result


def ticket_diagnostics(
    races: list[Race], lambda2: float, lambda3: float, ticket: str,
) -> dict:
    observations = []
    actual_losses = []
    for race in races:
        tickets = ticket_probabilities(race.probabilities, lambda2, lambda3)
        top3 = set(race.finish)
        if ticket == "place":
            for index, probability in enumerate(tickets["place"]):
                observations.append((probability, index in top3,
                                     _popularity_band(race.popularity[index])))
        else:
            actual_pair = tuple(sorted(race.finish[:2]))
            values = tickets[ticket]
            for pair, probability in values.items():
                actual = (set(pair) <= top3) if ticket == "wide" else pair == actual_pair
                pair_popularity = [race.popularity[index] for index in pair]
                known = [value for value in pair_popularity if value is not None]
                band = _popularity_band(max(known) if known else None)
                observations.append((probability, actual, band))
                if ticket == "umaren" and actual:
                    actual_losses.append((band, -math.log(max(probability, EPS))))
    band_rows = {}
    for band in ("1-3", "4-8", "9+", "unknown"):
        group = [item for item in observations if item[2] == band]
        if not group:
            continue
        band_rows[band] = {
            "n": len(group),
            "bernoulli_logloss": sum(_bernoulli_loss(p, y) for p, y, _ in group) / len(group),
            "brier": sum((p - y) ** 2 for p, y, _ in group) / len(group),
        }
        if ticket == "umaren":
            losses = [loss for loss_band, loss in actual_losses if loss_band == band]
            band_rows[band]["actual_pair_count"] = len(losses)
            band_rows[band]["actual_pair_logloss"] = (
                sum(losses) / len(losses) if losses else None
            )
    return {"calibration_deciles": _calibration_curve(observations),
            "popularity_bands": band_rows}


def _period(date: str) -> str | None:
    if "20210101" <= date <= "20231231":
        return "fit_2021_2023"
    if "20240101" <= date <= "20241231":
        return "evaluation_2024"
    if "20250101" <= date <= "20251231":
        return "benchmark_2025"
    if "20260101" <= date <= "20260630":
        return "benchmark_2026H1"
    return None


def evaluate_period(races: list[Race], lambda2: float, lambda3: float) -> dict:
    candidate = [(race, race_metrics(race, lambda2, lambda3)) for race in races]
    baseline = [(race, race_metrics(race, 1.0, 1.0)) for race in races]
    output = {"race_count": len(races)}
    for ticket in ("place", "wide", "umaren"):
        key = f"{ticket}_logloss"
        values_a = [{"race_id": race.race_id, "value": metrics[key]}
                    for race, metrics in candidate]
        values_b = [{"race_id": race.race_id, "value": metrics[key]}
                    for race, metrics in baseline]
        blocks_a, blocks_b = defaultdict(list), defaultdict(list)
        for row_a, row_b in zip(values_a, values_b):
            block = row_a["race_id"][:8]
            blocks_a[block].append(row_a)
            blocks_b[block].append(row_b)
        metric = lambda rows: sum(row["value"] for row in rows) / len(rows)
        bootstrap = paired_block_bootstrap(metric, blocks_a, blocks_b, 5000, 5903)
        output[ticket] = {
            "candidate_logloss": metric(values_a),
            "baseline_logloss": metric(values_b),
            "candidate_minus_baseline": bootstrap.observed_difference,
            "ci95": [bootstrap.ci_low, bootstrap.ci_high],
            "p_value": bootstrap.p_value,
            "candidate_brier": sum(m[f"{ticket}_brier"] for _, m in candidate) / len(candidate),
            "baseline_brier": sum(m[f"{ticket}_brier"] for _, m in baseline) / len(baseline),
            "candidate_diagnostics": ticket_diagnostics(races, lambda2, lambda3, ticket),
            "baseline_diagnostics": ticket_diagnostics(races, 1.0, 1.0, ticket),
        }
    output["sanrenpuku_reference_logloss"] = sum(
        metrics["sanrenpuku_logloss"] for _, metrics in candidate) / len(candidate)
    return output


def run(db_path: Path) -> dict:
    races, audit = load_races(db_path)
    fit = [race for race in races if _period(race.race_id[:8]) == "fit_2021_2023"]
    lambda2, lambda3 = fit_stage_lambda(fit, 2), fit_stage_lambda(fit, 3)
    curve = []
    for step in range(8):
        exponent = round(0.5 + step * 0.1, 1)
        curve.append({
            "lambda": exponent,
            "stage2_loglikelihood": stage_log_likelihood(fit, 2, exponent),
            "stage3_loglikelihood": stage_log_likelihood(fit, 3, exponent),
        })
    periods = {}
    for name in ("evaluation_2024", "benchmark_2025", "benchmark_2026H1"):
        subset = [race for race in races if _period(race.race_id[:8]) == name]
        periods[name] = evaluate_period(subset, lambda2, lambda3)
    return {
        "spec": "T59c", "ability_db_sha256": sha256_file(db_path),
        "population_audit": audit, "fit_race_count": len(fit),
        "lambda2": lambda2, "lambda3": lambda3,
        "loglikelihood_curve": curve, "periods": periods,
    }


def write_deterministic_json(payload: dict, path: Path) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    require_registrations(args.ledger, args.db)
    payload = run(args.db)
    print(write_deterministic_json(payload, args.output))


if __name__ == "__main__":
    main()
