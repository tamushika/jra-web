"""SPEC-T61a read-only comparison of T16, market and CL place probabilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from backtest_ml import feature_matrix
from backtest_place_model import (
    DATA_FROM,
    build_place_feature_dataset,
    load_model,
    predict_place_probability,
)
from backtest_t59c_lambda import Race as MarketRace
from backtest_t59c_lambda import load_races, ticket_probabilities
from backtest_win5 import load_win5_cfg
from backtest_ability import load_runs


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_T16_MODEL = ROOT / "api" / "data_files" / "common" / "web_place_model.json"
DEFAULT_CL_MODEL = ROOT / "api" / "data_files" / "common" / "win5_ml_model.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "t61a_result.json"
DATA_TO = "20260630"
EPS = 1e-15
PERIODS = {
    "evaluation_2024": ("20240101", "20241231"),
    "benchmark_2025": ("20250101", "20251231"),
    "benchmark_2026H1": ("20260101", "20260630"),
}
MODEL_NAMES = ("t16", "market", "cl")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _softmax(scores: np.ndarray, temperature: float = 1.0) -> tuple[float, ...]:
    if temperature <= 0.0:
        raise ValueError("CL temperature must be positive")
    values = np.asarray(scores, dtype=float) / temperature
    values -= np.max(values)
    exponentials = np.exp(values)
    return tuple(float(value) for value in exponentials / exponentials.sum())


def predict_cl_scores(model: dict, X: np.ndarray) -> np.ndarray:
    """Apply the sealed production conditional-logit artifact without refitting."""
    if model.get("objective") != "conditional_logit":
        raise ValueError("CL artifact objective must be conditional_logit")
    matrix = feature_matrix(X, model["features"])
    mean = np.asarray(model["mean"], dtype=float)
    scale = np.asarray(model["sd"], dtype=float)
    coef = np.asarray(model["coef"], dtype=float)
    if not (matrix.shape[1] == len(mean) == len(scale) == len(coef)):
        raise ValueError("CL artifact coefficient dimensions do not match")
    if np.any(scale == 0.0):
        raise ValueError("CL artifact contains a zero standard deviation")
    return ((matrix - mean) / scale) @ coef


def derive_place_probabilities(win_probabilities: tuple[float, ...]) -> tuple[float, ...]:
    """Naive Harville place probabilities (lambda2=lambda3=1)."""
    values = ticket_probabilities(win_probabilities, 1.0, 1.0, places=3)["place"]
    return tuple(float(value) for value in values)


def pearson_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right):
        raise ValueError("correlation inputs must have equal lengths")
    if len(left) < 2:
        return None
    x, y = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    x_centered, y_centered = x - x.mean(), y - y.mean()
    denominator = math.sqrt(float(x_centered @ x_centered) * float(y_centered @ y_centered))
    if denominator == 0.0:
        return None
    return float((x_centered @ y_centered) / denominator)


def calibration_curve(observations: list[tuple[float, int]]) -> list[dict]:
    ordered = sorted(observations, key=lambda row: (row[0], row[1]))
    rows = []
    for decile in range(10):
        start = len(ordered) * decile // 10
        end = len(ordered) * (decile + 1) // 10
        group = ordered[start:end]
        if group:
            rows.append({
                "decile": decile + 1,
                "n": len(group),
                "mean_probability": sum(row[0] for row in group) / len(group),
                "observed_rate": sum(row[1] for row in group) / len(group),
            })
    return rows


def evaluate_races(races: list[dict]) -> dict:
    if not races:
        raise ValueError("at least one common-population race is required")
    race_id_sets = {
        name: {race["race_id"] for race in races if name in race["probabilities"]}
        for name in MODEL_NAMES
    }
    if len({frozenset(ids) for ids in race_id_sets.values()}) != 1:
        raise AssertionError("the three model race populations differ")

    output = {"race_count": len(races), "runner_count": sum(len(race["target"]) for race in races)}
    residuals: dict[str, list[float]] = {name: [] for name in MODEL_NAMES}
    for name in MODEL_NAMES:
        race_losses, race_briers = [], []
        observations: list[tuple[float, int]] = []
        captured = {1: 0, 3: 0, 5: 0}
        positives = 0
        for race in races:
            probabilities = race["probabilities"][name]
            targets = race["target"]
            if len(probabilities) != len(targets):
                raise AssertionError(f"{name} runner population differs")
            losses, briers = [], []
            for probability, target in zip(probabilities, targets):
                probability = min(1.0 - EPS, max(EPS, float(probability)))
                losses.append(-math.log(probability if target else 1.0 - probability))
                briers.append((probability - target) ** 2)
                observations.append((probability, target))
                residuals[name].append(target - probability)
            race_losses.append(sum(losses) / len(losses))
            race_briers.append(sum(briers) / len(briers))
            order = sorted(range(len(probabilities)), key=lambda i: (-probabilities[i], i))
            positives += sum(targets)
            for k in captured:
                captured[k] += sum(targets[i] for i in order[:k])
        output[name] = {
            "top3_bernoulli_logloss": sum(race_losses) / len(race_losses),
            "brier": sum(race_briers) / len(race_briers),
            "capture": {f"top{k}": captured[k] / positives for k in captured},
            "calibration_deciles": calibration_curve(observations),
        }
    output["residual_correlations"] = {
        "t16_market": pearson_correlation(residuals["t16"], residuals["market"]),
        "t16_cl": pearson_correlation(residuals["t16"], residuals["cl"]),
        "market_cl": pearson_correlation(residuals["market"], residuals["cl"]),
    }
    output["race_ids_sha256"] = hashlib.sha256(
        ("\n".join(sorted(race_id_sets["t16"])) + "\n").encode("utf-8")
    ).hexdigest()
    return output


def build_comparison_races(
    market_races: list[MarketRace], X: np.ndarray, race_keys: list[tuple], meta: list[dict],
    t16_probabilities: np.ndarray, cl_scores: np.ndarray, cl_temperature: float,
) -> tuple[list[dict], dict[str, int]]:
    feature_rows: dict[str, list[int]] = defaultdict(list)
    for index, key in enumerate(race_keys):
        feature_rows[f"{key[0]}{key[1]}{int(key[2]):02d}"].append(index)

    selected, audit = [], defaultdict(int)
    for race in market_races:
        audit["market_eligible"] += 1
        indices = feature_rows.get(race.race_id, [])
        if len(indices) != len(race.probabilities):
            audit["incomplete_model_features"] += 1
            continue
        try:
            indices = sorted(indices, key=lambda i: int(meta[i]["umaban"]))
        except (KeyError, TypeError, ValueError):
            audit["invalid_horse_number"] += 1
            continue
        t16 = tuple(float(t16_probabilities[i]) for i in indices)
        cl_win = _softmax(cl_scores[indices], cl_temperature)
        target = tuple(int(i in set(race.finish)) for i in range(len(indices)))
        probabilities = {
            "t16": t16,
            "market": derive_place_probabilities(race.probabilities),
            "cl": derive_place_probabilities(cl_win),
        }
        if any(len(values) != len(target) for values in probabilities.values()):
            raise AssertionError("model runner populations differ")
        selected.append({"race_id": race.race_id, "target": target, "probabilities": probabilities})
        audit["included_common_population"] += 1
    return selected, dict(sorted(audit.items()))


def run(db_path: Path, t16_path: Path, cl_path: Path) -> dict:
    t16_model = load_model(t16_path)
    cl_model = json.loads(cl_path.read_text(encoding="utf-8"))
    cfg = load_win5_cfg()
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, DATA_FROM, DATA_TO)
    X, _winner_y, race_keys, meta = build_place_feature_dataset(
        runs, cfg, DATA_TO, db_path=str(db_path), stats_source="ability")
    t16_probabilities = predict_place_probability(t16_model, X)
    cl_scores = predict_cl_scores(cl_model, X)
    market_races, market_audit = load_races(db_path)
    periods = {}
    population_audit = {}
    for name, (date_from, date_to) in PERIODS.items():
        market_subset = [race for race in market_races if date_from <= race.race_id[:8] <= date_to]
        common, audit = build_comparison_races(
            market_subset, X, race_keys, meta, t16_probabilities, cl_scores,
            float(cl_model.get("prob_temperature", 1.0) or 1.0),
        )
        periods[name] = evaluate_races(common)
        population_audit[name] = audit
    all_common = []
    for name, (date_from, date_to) in PERIODS.items():
        market_subset = [race for race in market_races if date_from <= race.race_id[:8] <= date_to]
        common, _audit = build_comparison_races(
            market_subset, X, race_keys, meta, t16_probabilities, cl_scores,
            float(cl_model.get("prob_temperature", 1.0) or 1.0),
        )
        all_common.extend(common)
    return {
        "spec": "T61a",
        "definitions": {
            "population": "T59c book band [1.15,1.45], field>=8, complete top3 and odds; then complete T16/CL features",
            "market_place": "inverse-win-odds normalized, naive Harville lambda2=lambda3=1",
            "cl_place": "production CL softmax win probabilities, naive Harville lambda2=lambda3=1",
            "primary_metric": "race-mean runner-level top3 Bernoulli log loss",
        },
        "artifacts": {
            "ability_db_sha256": sha256_file(db_path),
            "t16_model_sha256": sha256_file(t16_path),
            "cl_model_sha256": sha256_file(cl_path),
            "t16_meta": t16_model.get("meta"),
            "cl_meta": cl_model.get("meta"),
        },
        "market_population_audit_all_periods": market_audit,
        "population_audit": population_audit,
        "periods": periods,
        "combined_2024_2026H1": evaluate_races(all_common),
    }


def write_deterministic_json(payload: dict, path: Path) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-T61a T16/market/CL place comparison")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--t16-model", type=Path, default=DEFAULT_T16_MODEL)
    parser.add_argument("--cl-model", type=Path, default=DEFAULT_CL_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = run(args.db, args.t16_model, args.cl_model)
    print(write_deterministic_json(payload, args.output))


if __name__ == "__main__":
    main()
