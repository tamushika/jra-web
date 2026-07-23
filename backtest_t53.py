"""SPEC-T53 winner-only online PL opponent-rating experiment.

The evaluation is isolated from production and fails closed until the T39
base registration exists.  Rating construction is date-atomic and continuous
from the 2018 warm-up through 2026H1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import race_level_winner_logloss
from backtest_fold_stats import same_population_metrics
from backtest_ml import fit_conditional_logit, fit_temperature
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_win5 import load_win5_cfg
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "t53_result.json"
EXPERIMENT_ID = "T53-dynamic-opponent-rating-v1"
ABILITY_DB_SHA256 = "7ffcfe21618612b603053544cd888eec637b6fdf69192470c5751c5f89b00c79"
RATING_FEATURES = ("opponent_rating_asof", "opponent_rating_available")
WARMUP_FROM, FEATURE_FROM, DATA_TO = "20180101", "20210101", "20260630"
TRAIN_TO, SELECTION_FROM, SELECTION_TO = "20231231", "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"), "2026H1": ("20260101", "20260630")}
ETA_GRID, K_GRID, L2_GRID = (0.05, 0.1, 0.2), (5.0, 15.0, 40.0), (0.3, 1.0, 3.0)
FIXED_STAGE1_L2 = 1.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _race_key(row: dict) -> tuple:
    return (str(row.get("date")), str(row.get("place")), int(row.get("r") or 0))


def _runner_key(row: dict) -> tuple:
    return (*_race_key(row), int(row.get("umaban") or 0), str(row.get("horse") or ""))


def shrink_rating(rating: float, starts: int, k: float) -> float:
    if starts <= 0:
        return 0.0
    if k <= 0.0:
        raise ValueError("shrinkage k must be positive")
    return float(rating) * starts / (starts + k)


def winner_only_gradient(ratings: list[float], winner_mask: list[bool]) -> np.ndarray:
    """One aggregate PL-top1 gradient; lower placings are never inspected."""
    values = np.asarray(ratings, dtype=float)
    winners = np.asarray(winner_mask, dtype=float)
    if len(values) != len(winners) or not len(values):
        raise ValueError("ratings and winner mask must be non-empty and aligned")
    winner_count = float(winners.sum())
    if winner_count <= 0.0:
        return np.zeros(len(values), dtype=float)
    exponentials = np.exp(values - values.max())
    probabilities = exponentials / exponentials.sum()
    return winners - winner_count * probabilities


def build_rating_feature_map(runs: list[dict], eta: float, k: float) -> dict[tuple, tuple[float, float]]:
    """Build a date-atomic 2018-through-2026 rating series once."""
    if eta <= 0.0:
        raise ValueError("eta must be positive")
    eligible = [row for row in runs if WARMUP_FROM <= str(row.get("date")) <= DATA_TO
                and row.get("rank") is not None and str(row.get("horse") or "")]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in eligible:
        by_date[str(row["date"])].append(row)

    ratings: dict[str, float] = defaultdict(float)
    starts: Counter = Counter()
    features = {}
    for date in sorted(by_date):
        day_rows = by_date[date]
        # Every feature and probability on this date uses the prior-day state.
        if date >= FEATURE_FROM:
            for row in day_rows:
                horse = str(row["horse"])
                n = int(starts[horse])
                available = float(n >= 5)
                value = shrink_rating(ratings[horse], n, k) if available else 0.0
                features[_runner_key(row)] = (value, available)

        races: dict[tuple, list[dict]] = defaultdict(list)
        for row in day_rows:
            races[_race_key(row)].append(row)
        day_delta: dict[str, float] = defaultdict(float)
        day_starts: Counter = Counter()
        for members in races.values():
            winners = [int(row.get("rank")) == 1 for row in members]
            if not any(winners):
                continue
            raw = [ratings[str(row["horse"])] for row in members]
            gradient = winner_only_gradient(raw, winners)
            for row, value in zip(members, gradient):
                horse = str(row["horse"])
                day_delta[horse] += eta * float(value)
                day_starts[horse] += 1
        for horse, value in day_delta.items():
            ratings[horse] += value
        starts.update(day_starts)
    return features


def rating_matrix(feature_map: dict, meta: list[dict]) -> np.ndarray:
    return np.asarray([feature_map.get(_runner_key(row), (0.0, 0.0)) for row in meta], dtype=float)


def model_matrix(base: np.ndarray, rating: np.ndarray | None) -> np.ndarray:
    if rating is None:
        return base
    if rating.shape != (len(base), 2):
        raise ValueError("rating matrix must contain exactly two aligned columns")
    return np.column_stack((base, rating))


def require_registration(ledger_path: Path, db_path: Path) -> dict:
    actual = sha256_file(db_path)
    if actual != ABILITY_DB_SHA256:
        raise RuntimeError(f"ability.db SHA mismatch: expected {ABILITY_DB_SHA256}, got {actual}")
    registration = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("experiment_id") == EXPERIMENT_ID and row.get("result_summary") is None:
            registration = row
    if registration is None:
        raise RuntimeError(f"T53 evaluation blocked: register {EXPERIMENT_ID} in T39 ledger first")
    sealed = registration.get("data_hashes", {}).get("ability_db_sha256")
    if sealed != actual:
        raise RuntimeError(f"T39 registration seals {sealed!r}, not current ability.db {actual}")
    return registration


def _mask(dates, date_from, date_to):
    return (dates >= date_from) & (dates <= date_to)


def _selected(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def _fit_candidate(base, rating, labels, keys, meta, dates, l2, label, eta=None, k=None):
    from sklearn.preprocessing import StandardScaler
    X = model_matrix(base, rating)
    train, selection = _mask(dates, FEATURE_FROM, TRAIN_TO), _mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(
        scaler.transform(X[train]), labels[train], _selected(keys, train), l2=l2)
    selection_keys = _selected(keys, selection)
    scores = scaler.transform(X[selection]) @ weights
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, _selected(meta, selection),
        temperature=temperature)
    metrics.pop("sample_signature", None)
    selection_losses = race_level_winner_logloss(
        scores, labels[selection], selection_keys, _selected(meta, selection),
        temperature=temperature)
    return {"label": label, "eta": eta, "k": k, "l2": l2, "rating": rating,
            "scaler": scaler, "weights": weights, "temperature": temperature,
            "selection": metrics, "selection_losses": selection_losses}


def _refit(base, labels, keys, dates, candidate, through):
    from sklearn.preprocessing import StandardScaler
    X = model_matrix(base, candidate["rating"])
    fit = _mask(dates, FEATURE_FROM, through)
    scaler = StandardScaler().fit(X[fit])
    weights = fit_conditional_logit(
        scaler.transform(X[fit]), labels[fit], _selected(keys, fit), l2=candidate["l2"])
    return {"rating": candidate["rating"], "scaler": scaler, "weights": weights,
            "temperature": candidate["temperature"]}


def _score(model, base, labels, keys, meta, dates, date_from, date_to):
    period = _mask(dates, date_from, date_to)
    X = model_matrix(base, model["rating"])[period]
    scores = model["scaler"].transform(X) @ model["weights"]
    period_keys, period_meta = _selected(keys, period), _selected(meta, period)
    metrics = same_population_metrics(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    signature = metrics.pop("sample_signature")
    losses = race_level_winner_logloss(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    return metrics, signature, losses


def _paired(selected_losses, baseline_losses, seed):
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("selected/baseline race populations differ")
    selected_blocks, baseline_blocks = defaultdict(list), defaultdict(list)
    for key in sorted(selected_losses):
        selected_blocks[key[0]].append(selected_losses[key])
        baseline_blocks[key[0]].append(baseline_losses[key])
    result = paired_block_bootstrap(
        lambda rows: sum(rows) / len(rows), selected_blocks, baseline_blocks, 2000, seed)
    return {"difference": result.observed_difference, "ci95": [result.ci_low, result.ci_high],
            "p_value": result.p_value, "races": len(selected_losses),
            "day_blocks": len(selected_blocks), "resamples": result.n_resamples, "seed": result.seed}


def rating_distribution(matrix: np.ndarray, dates: np.ndarray) -> dict:
    periods = {"train_2021_2023": (FEATURE_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    output = {}
    for name, (date_from, date_to) in periods.items():
        rows = matrix[_mask(dates, date_from, date_to)]
        values, available = rows[:, 0], rows[:, 1] == 1.0
        known = values[available]
        output[name] = {
            "rows": len(rows), "available_rows": int(available.sum()),
            "availability_rate": float(available.mean()),
            "available_mean": float(known.mean()) if len(known) else None,
            "available_std": float(known.std()) if len(known) else None,
            "available_quantiles": ([float(x) for x in np.quantile(known, [0.05, 0.5, 0.95])]
                                    if len(known) else None),
            "fallback_zero_rate": float((~available).mean()),
            "unavailable_mean": float(values[~available].mean()) if (~available).any() else None,
            "unavailable_std": float(values[~available].std()) if (~available).any() else None,
        }
    return output


def popularity_increment(selected_losses: dict, baseline_losses: dict, meta: list[dict]) -> dict:
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("popularity diagnostic populations differ")
    winners = defaultdict(list)
    for row in meta:
        try:
            if int(row.get("rank")) == 1:
                winners[_race_key(row)].append(int(row.get("popularity")))
        except (TypeError, ValueError):
            continue
    groups = defaultdict(list)
    for key in sorted(selected_losses):
        popularity = min(winners.get(key, [999]))
        band = "1-3" if popularity <= 3 else "4-8" if popularity <= 8 else "9+"
        groups[band].append(selected_losses[key] - baseline_losses[key])
    return {band: {"races": len(values), "winner_logloss_diff": sum(values) / len(values)}
            for band, values in sorted(groups.items())}


def run(db_path: Path, ledger_path: Path) -> dict:
    registration = require_registration(ledger_path, db_path)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, WARMUP_FROM, DATA_TO)
    cfg = load_win5_cfg()
    # load_runs() always fetches a three-year lookback.  T53 needs that call
    # shape to obtain the 2018 rating warm-up, but the production/T41 base
    # dataset for 2021 starts at 2018.  Explicitly remove the incidental
    # 2015-2017 rows so the pack-OFF baseline stays identical to T41/T54a.
    base_runs = [row for row in runs if str(row.get("date")) >= WARMUP_FROM]
    base, labels, keys, meta = build_consistent_feature_dataset(
        base_runs, cfg, DATA_TO, stats_source="ability", db_path=str(db_path))
    dates = np.asarray([key[0] for key in keys])
    base_sha = hashlib.sha256(base.tobytes()).hexdigest()
    if model_matrix(base, None) is not base or hashlib.sha256(model_matrix(base, None).tobytes()).hexdigest() != base_sha:
        raise AssertionError("pack-OFF base matrix is not byte-identical")

    stage1, matrices = [], {}
    for eta in ETA_GRID:
        for k in K_GRID:
            feature_map = build_rating_feature_map(runs, eta, k)
            matrix = rating_matrix(feature_map, meta)
            matrices[(eta, k)] = matrix
            stage1.append(_fit_candidate(
                base, matrix, labels, keys, meta, dates, FIXED_STAGE1_L2,
                "rating_stage1", eta, k))
    stage1_best = min(stage1, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    selected_matrix = matrices[(stage1_best["eta"], stage1_best["k"])]
    stage2 = [_fit_candidate(
        base, selected_matrix, labels, keys, meta, dates, l2,
        "rating_stage2_l2_sensitivity", stage1_best["eta"], stage1_best["k"])
        for l2 in L2_GRID]
    baselines = [_fit_candidate(
        base, None, labels, keys, meta, dates, l2, "baseline") for l2 in L2_GRID]
    selected = min(stage2, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline = min(baselines, key=lambda row: row["selection"]["models"]["model"]["logloss"])

    selected_final, baseline_final = (_refit(base, labels, keys, dates, row, SELECTION_TO)
                                      for row in (selected, baseline))
    selected_reference = _refit(base, labels, keys, dates, selected, TRAIN_TO)
    historical, paired, popularity = {}, {}, {}
    paired["2024_selection"] = _paired(
        selected["selection_losses"], baseline["selection_losses"], 5299)
    popularity["2024_selection"] = popularity_increment(
        selected["selection_losses"], baseline["selection_losses"],
        [row for row in meta if SELECTION_FROM <= str(row.get("date")) <= SELECTION_TO])
    for index, (name, (date_from, date_to)) in enumerate(PERIODS.items()):
        selected_metrics, signature, selected_losses = _score(
            selected_final, base, labels, keys, meta, dates, date_from, date_to)
        baseline_metrics, baseline_signature, baseline_losses = _score(
            baseline_final, base, labels, keys, meta, dates, date_from, date_to)
        reference_metrics, reference_signature, _ = _score(
            selected_reference, base, labels, keys, meta, dates, date_from, date_to)
        if signature != baseline_signature or signature != reference_signature:
            raise RuntimeError(f"{name}: evaluation populations differ")
        historical[name] = {"final_refit_2021_2024": selected_metrics,
                            "trained_2021_2023_reference": reference_metrics,
                            "best_baseline_final_refit": baseline_metrics}
        paired[name] = _paired(selected_losses, baseline_losses, 5300 + index)
        popularity[name] = popularity_increment(
            selected_losses, baseline_losses,
            [row for row in meta if date_from <= str(row.get("date")) <= date_to])

    def candidate_json(row):
        return {"label": row["label"], "eta": row["eta"], "k": row["k"],
                "l2": row["l2"], "selection_2024": row["selection"]}
    return {
        "spec": "T53", "experiment_id": EXPERIMENT_ID,
        "ability_db_sha256": sha256_file(db_path), "registration": registration,
        "features": list(RATING_FEATURES), "pack_off_base_sha256": base_sha,
        "stage1_candidates": [candidate_json(row) for row in stage1],
        "stage1_selected_eta_k": {"eta": stage1_best["eta"], "k": stage1_best["k"]},
        "stage2_candidates": [candidate_json(row) for row in stage2],
        "baseline_candidates": [candidate_json(row) for row in baselines],
        "selected": {"eta": selected["eta"], "k": selected["k"], "l2": selected["l2"]},
        "baseline_best_l2": baseline["l2"], "historical_benchmark": historical,
        "win5_day_paired": paired, "popularity_band_increment": popularity,
        "rating_distribution": rating_distribution(selected_matrix, dates),
    }


def write_json(payload: dict, path: Path) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(write_json(run(args.db, args.ledger), args.output))


if __name__ == "__main__":
    main()
