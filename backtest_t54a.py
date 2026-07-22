"""SPEC-T54a: as-of running style and race pace-pressure experiment.

Stage 0 is diagnostic and may run before registration.  The six-candidate
evaluation is fail-closed until its T39 base registration exists.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import race_level_winner_logloss
from backtest_fold_stats import same_population_metrics
from backtest_ml import FEATURES, fit_conditional_logit, fit_temperature
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_t61a import derive_place_probabilities, pearson_correlation
from backtest_win5 import load_win5_cfg, parse_final_odds
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_STAGE0_OUTPUT = ROOT / "outputs" / "t54a_stage0.json"
DEFAULT_RESULT_OUTPUT = ROOT / "outputs" / "t54a_result.json"
EXPERIMENT_ID = "T54a-running-style-pace-v1"
ABILITY_DB_SHA256 = "7ffcfe21618612b603053544cd888eec637b6fdf69192470c5751c5f89b00c79"
STYLE_FEATURES = (
    "style_pos_asof",
    "race_pace_pressure_asof",
    "style_pace_interaction",
    "style_available",
)
DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_TO, SELECTION_FROM, SELECTION_TO = "20231231", "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"), "2026H1": ("20260101", "20260630")}
L2_GRID = (0.3, 1.0, 3.0)
EPS = 1e-15


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_key(row: dict) -> tuple:
    return (str(row.get("date")), str(row.get("place")), int(row.get("r") or 0))


def _runner_key(row: dict) -> tuple:
    return (*_run_key(row), int(row.get("umaban") or 0), str(row.get("horse") or ""))


def _position_ratio(row: dict) -> float | None:
    try:
        c4, total = float(row.get("c4")), int(row.get("total_horses"))
    except (TypeError, ValueError):
        return None
    if total <= 0 or not (1.0 <= c4 <= total):
        return None
    return c4 / total


def compute_style_matrix(runs: list[dict], target_rows: list[dict]) -> np.ndarray:
    """Return the four frozen T54a columns in target-row order.

    Histories are date-atomic: even an earlier race on the target date is
    excluded.  The last four starts are selected first, then valid c4 ratios
    are counted; fewer than two valid ratios triggers the specified fallback.
    """
    by_horse: dict[str, list[dict]] = defaultdict(list)
    for row in runs:
        horse = str(row.get("horse") or "")
        if horse:
            by_horse[horse].append(row)
    dates_by_horse = {}
    for horse, rows in by_horse.items():
        rows.sort(key=lambda row: (_run_key(row), int(row.get("umaban") or 0)))
        dates_by_horse[horse] = [str(row.get("date")) for row in rows]

    base = np.zeros((len(target_rows), 2), dtype=float)
    for index, current in enumerate(target_rows):
        horse = str(current.get("horse") or "")
        histories = by_horse.get(horse, [])
        cutoff = bisect.bisect_left(dates_by_horse.get(horse, []), str(current.get("date")))
        prior = histories[max(0, cutoff - 4):cutoff]
        ratios = [value for value in (_position_ratio(row) for row in prior) if value is not None]
        if len(ratios) >= 2:
            base[index] = (sum(ratios) / len(ratios), 1.0)

    race_members: dict[tuple, list[int]] = defaultdict(list)
    for index, row in enumerate(target_rows):
        race_members[_run_key(row)].append(index)
    result = np.zeros((len(target_rows), len(STYLE_FEATURES)), dtype=float)
    result[:, 0], result[:, 3] = base[:, 0], base[:, 1]
    for indices in race_members.values():
        pressure = float(np.mean(base[indices, 0]))
        result[indices, 1] = pressure
        result[indices, 2] = base[indices, 0] * pressure
    return result


def stage0_diagnostic(runs: list[dict]) -> dict:
    targets = [row for row in runs if SELECTION_FROM <= str(row.get("date")) <= SELECTION_TO]
    matrix = compute_style_matrix(runs, targets)
    predicted, realized = [], []
    for values, row in zip(matrix, targets):
        actual = _position_ratio(row)
        if values[3] == 1.0 and actual is not None:
            predicted.append(float(values[0]))
            realized.append(actual)
    return {
        "period": "2024_selection",
        "rows": len(targets),
        "available_rows": int(np.sum(matrix[:, 3])),
        "availability_rate": float(np.mean(matrix[:, 3])) if len(matrix) else None,
        "correlation_n": len(predicted),
        "style_realized_c4_pearson": pearson_correlation(predicted, realized),
        "feature_summary": {
            name: {"mean": float(np.mean(matrix[:, i])), "std": float(np.std(matrix[:, i]))}
            for i, name in enumerate(STYLE_FEATURES)
        } if len(matrix) else {},
        "selection_guard": "diagnostic_only_not_used_for_candidate_selection",
    }


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
        raise RuntimeError(
            f"T54a evaluation blocked: register {EXPERIMENT_ID} in T39 ledger first")
    sealed = registration.get("data_hashes", {}).get("ability_db_sha256")
    if sealed != actual:
        raise RuntimeError(f"T39 registration seals {sealed!r}, not current ability.db {actual}")
    return registration


def model_matrix(base: np.ndarray, style: np.ndarray, enabled: bool) -> np.ndarray:
    """Pack OFF is byte-identical; pack ON appends exactly four columns."""
    if not enabled:
        return base
    if style.shape != (len(base), 4):
        raise ValueError("style matrix must contain exactly four aligned columns")
    return np.column_stack((base, style))


def _mask(dates: np.ndarray, date_from: str, date_to: str) -> np.ndarray:
    return (dates >= date_from) & (dates <= date_to)


def _selected(values: list, mask: np.ndarray) -> list:
    return [value for value, keep in zip(values, mask) if keep]


def _fit_candidate(base, style, labels, keys, meta, dates, enabled, l2):
    from sklearn.preprocessing import StandardScaler
    X = model_matrix(base, style, enabled)
    train = _mask(dates, DATA_FROM, TRAIN_TO)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(
        scaler.transform(X[train]), labels[train], _selected(keys, train), l2=l2)
    selection_scores = scaler.transform(X[selection]) @ weights
    selection_keys, selection_meta = _selected(keys, selection), _selected(meta, selection)
    temperature = fit_temperature(selection_scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        selection_scores, labels[selection], selection_keys, selection_meta,
        temperature=temperature)
    metrics.pop("sample_signature", None)
    secondary = _race_secondary(
        selection_scores, selection_keys, selection_meta, style[selection], temperature)
    metrics["top3"] = _aggregate_top3(secondary)
    return {
        "config": "style4" if enabled else "baseline",
        "l2": l2,
        "selection": metrics,
        "scaler": scaler,
        "weights": weights,
        "temperature": temperature,
        "enabled": enabled,
        "selection_secondary": secondary,
    }


def _refit(base, style, labels, keys, dates, candidate, through):
    from sklearn.preprocessing import StandardScaler
    X = model_matrix(base, style, candidate["enabled"])
    fit = _mask(dates, DATA_FROM, through)
    scaler = StandardScaler().fit(X[fit])
    weights = fit_conditional_logit(
        scaler.transform(X[fit]), labels[fit], _selected(keys, fit), l2=candidate["l2"])
    return {"scaler": scaler, "weights": weights, "temperature": candidate["temperature"],
            "enabled": candidate["enabled"]}


def _score(model, base, style, labels, keys, meta, dates, date_from, date_to):
    period = _mask(dates, date_from, date_to)
    X = model_matrix(base, style, model["enabled"])[period]
    scores = model["scaler"].transform(X) @ model["weights"]
    period_keys, period_meta = _selected(keys, period), _selected(meta, period)
    metrics = same_population_metrics(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    signature = metrics.pop("sample_signature")
    losses = race_level_winner_logloss(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    secondary = _race_secondary(
        scores, period_keys, period_meta, style[period], model["temperature"])
    metrics["top3"] = _aggregate_top3(secondary)
    return metrics, signature, losses, secondary


def _band(value: float, labels=("front", "middle", "closer")) -> str:
    if value < 1.0 / 3.0:
        return labels[0]
    if value < 2.0 / 3.0:
        return labels[1]
    return labels[2]


def _race_secondary(scores, keys, meta, style, temperature) -> dict:
    """Top3 probability metrics and frozen diagnostic contexts per strict race."""
    grouped = defaultdict(list)
    for score, key, row, style_row in zip(scores, keys, meta, style):
        grouped[key].append((float(score), row, np.asarray(style_row, dtype=float)))
    output = {}
    for key, members in grouped.items():
        if key[2] is None or key[2] < 9 or len(members) < 8:
            continue
        field_size = max((int(row.get("total_horses") or 0) for _, row, _ in members), default=0)
        if field_size and len(members) != field_size:
            continue
        if not any(int(row.get("rank") or 999) == 1 for _, row, _ in members):
            continue
        odds = [parse_final_odds(row.get("win_pay"), row.get("rank")) for _, row, _ in members]
        if any(value is None or value <= 1.0 for value in odds):
            continue
        raw = np.asarray([score for score, _, _ in members], dtype=float) / float(temperature or 1.0)
        raw -= raw.max()
        win = np.exp(raw)
        win /= win.sum()
        place = derive_place_probabilities(tuple(float(value) for value in win))
        targets = [int(int(row.get("rank") or 999) <= 3) for _, row, _ in members]
        losses = [-math.log(max(EPS, p if y else 1.0 - p)) for p, y in zip(place, targets)]
        briers = [(p - y) ** 2 for p, y in zip(place, targets)]
        winners = [style_row for _, row, style_row in members if int(row.get("rank") or 999) == 1]
        known_winners = [row[0] for row in winners if row[3] == 1.0]
        winner_style_band = (_band(float(np.mean(known_winners))) if known_winners else "unavailable")
        pressure = float(np.mean([style_row[1] for _, _, style_row in members]))
        try:
            distance = int(members[0][1].get("distance") or 0)
        except (TypeError, ValueError):
            distance = 0
        distance_band = "under1600" if distance < 1600 else "1600_1999" if distance < 2000 else "2000plus"
        output[key] = {
            "top3_logloss": sum(losses) / len(losses),
            "top3_brier": sum(briers) / len(briers),
            "distance_band": distance_band,
            "pace_pressure_band": _band(pressure, ("front_dense", "balanced", "closer_heavy")),
            "winner_style_band": winner_style_band,
        }
    return output


def _aggregate_top3(rows: dict) -> dict:
    if not rows:
        return {"race_count": 0, "logloss": None, "brier": None}
    values = list(rows.values())
    return {"race_count": len(values),
            "logloss": sum(row["top3_logloss"] for row in values) / len(values),
            "brier": sum(row["top3_brier"] for row in values) / len(values)}


def _increment_breakdown(selected: dict, baseline: dict) -> dict:
    if set(selected) != set(baseline):
        raise RuntimeError("secondary diagnostic populations differ")
    groups = defaultdict(list)
    for key in sorted(selected):
        row = selected[key]
        difference = row["top3_logloss"] - baseline[key]["top3_logloss"]
        groups[f"distance:{row['distance_band']}"] .append(difference)
        groups[f"context:{row['pace_pressure_band']}/{row['winner_style_band']}"] .append(difference)
    return {name: {"race_count": len(values), "top3_logloss_diff": sum(values) / len(values)}
            for name, values in sorted(groups.items())}


def _style_coverage(style: np.ndarray, dates: np.ndarray) -> dict:
    periods = {"train_2021_2023": (DATA_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    result = {}
    for name, (date_from, date_to) in periods.items():
        rows = style[_mask(dates, date_from, date_to)]
        result[name] = {
            feature: {"mean": float(np.mean(rows[:, index])),
                      "std": float(np.std(rows[:, index])),
                      "nonzero_rate": float(np.mean(rows[:, index] != 0.0))}
            for index, feature in enumerate(STYLE_FEATURES)
        }
    return result


def _paired(selected_losses: dict, baseline_losses: dict, seed: int) -> dict:
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("selected and baseline race populations differ")
    left, right = defaultdict(list), defaultdict(list)
    for key in sorted(selected_losses):
        left[key[0]].append(selected_losses[key])
        right[key[0]].append(baseline_losses[key])
    result = paired_block_bootstrap(
        lambda rows: sum(rows) / len(rows), left, right, 2000, seed)
    return {"difference": result.observed_difference, "ci95": [result.ci_low, result.ci_high],
            "p_value": result.p_value, "race_count": len(selected_losses),
            "day_blocks": len(left), "resamples": result.n_resamples, "seed": result.seed}


def run_evaluation(db_path: Path, ledger_path: Path) -> dict:
    registration = require_registration(ledger_path, db_path)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, DATA_FROM, DATA_TO)
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(db_path))
    style = compute_style_matrix(runs, meta)
    dates = np.asarray([key[0] for key in keys])
    if hashlib.sha256(base.tobytes()).digest() != hashlib.sha256(
            model_matrix(base, style, False).tobytes()).digest():
        raise AssertionError("pack-OFF feature matrix is not byte-identical")
    candidates = [
        _fit_candidate(base, style, labels, keys, meta, dates, enabled, l2)
        for enabled in (False, True) for l2 in L2_GRID
    ]
    best = min(candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline = min((row for row in candidates if not row["enabled"]),
                   key=lambda row: row["selection"]["models"]["model"]["logloss"])
    best_final, baseline_final = (_refit(base, style, labels, keys, dates, row, SELECTION_TO)
                                  for row in (best, baseline))
    reference = _refit(base, style, labels, keys, dates, best, TRAIN_TO)
    historical, paired = {}, {}
    for index, (name, (date_from, date_to)) in enumerate(PERIODS.items()):
        selected_metrics, selected_signature, selected_losses, selected_secondary = _score(
            best_final, base, style, labels, keys, meta, dates, date_from, date_to)
        baseline_metrics, baseline_signature, baseline_losses, baseline_secondary = _score(
            baseline_final, base, style, labels, keys, meta, dates, date_from, date_to)
        if selected_signature != baseline_signature:
            raise RuntimeError(f"{name}: selected/baseline populations differ")
        reference_metrics, reference_signature, _, _reference_secondary = _score(
            reference, base, style, labels, keys, meta, dates, date_from, date_to)
        if selected_signature != reference_signature:
            raise RuntimeError(f"{name}: final/reference populations differ")
        historical[name] = {"final_refit_2021_2024": selected_metrics,
                            "trained_2021_2023_reference": reference_metrics,
                            "best_baseline_final_refit": baseline_metrics,
                            "distance_and_style_increment": _increment_breakdown(
                                selected_secondary, baseline_secondary)}
        paired[name] = _paired(selected_losses, baseline_losses, 5400 + index)
    return {
        "spec": "T54a", "experiment_id": EXPERIMENT_ID,
        "ability_db_sha256": sha256_file(db_path),
        "registration": registration,
        "features": list(STYLE_FEATURES),
        "stage0": stage0_diagnostic(runs),
        "pack_off_base_sha256": hashlib.sha256(base.tobytes()).hexdigest(),
        "candidates": [{"config": row["config"], "l2": row["l2"],
                        "selection": row["selection"]} for row in candidates],
        "selected": {"config": best["config"], "l2": best["l2"]},
        "baseline_best_l2": baseline["l2"],
        "selection_2024_distance_and_style_increment": _increment_breakdown(
            best["selection_secondary"], baseline["selection_secondary"]),
        "coverage": _style_coverage(style, dates),
        "historical_benchmark": historical,
        "win5_day_paired": paired,
    }


def write_json(payload: dict, path: Path) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _load_runs_readonly(db_path: Path) -> list[dict]:
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        return load_runs(connection, DATA_FROM, DATA_TO)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage0", "evaluate"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "stage0":
        payload = {"spec": "T54a", "ability_db_sha256": sha256_file(args.db),
                   "stage0": stage0_diagnostic(_load_runs_readonly(args.db))}
        output = args.output or DEFAULT_STAGE0_OUTPUT
    else:
        payload = run_evaluation(args.db, args.ledger)
        output = args.output or DEFAULT_RESULT_OUTPUT
    print(write_json(payload, output))


if __name__ == "__main__":
    main()
