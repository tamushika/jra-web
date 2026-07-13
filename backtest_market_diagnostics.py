"""Evaluation-only diagnostics for the M3 market-offset model.

M3 fixes ``log(normalized 1 / final_odds)`` at coefficient one and learns
only a no-market residual with conditional logit.  All model comparisons use
one strict complete-race population built with T33's yearly ability snapshots.
Nothing in this module writes or updates the production model.
"""
from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np

from backtest_ml import (FEATURES, NO_MARKET_FEATURES, feature_matrix,
                         fit_conditional_logit, fit_temperature)
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_win5 import parse_final_odds


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ability.db")
TRAIN_PERIOD = ("20210101", "20231231")
TUNE_PERIOD = ("20240101", "20241231")
EVALUATION_PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
}
LAMBDA_GRID = (0.01, 0.1, 1.0, 10.0, 100.0)


@dataclass
class CompleteRaceDataset:
    """Rows retained by the common complete-race/settled-odds filter."""

    features: np.ndarray
    labels: np.ndarray
    race_keys: list
    meta: list
    market_probabilities: np.ndarray
    market_offsets: np.ndarray
    skipped: dict

    @property
    def race_count(self) -> int:
        return len(set(self.race_keys))

    def subset(self, date_from: str, date_to: str) -> "CompleteRaceDataset":
        keep = np.asarray(
            [date_from <= key[0] <= date_to for key in self.race_keys],
            dtype=bool,
        )
        return CompleteRaceDataset(
            features=self.features[keep],
            labels=self.labels[keep],
            race_keys=[key for key, selected in zip(self.race_keys, keep) if selected],
            meta=[row for row, selected in zip(self.meta, keep) if selected],
            market_probabilities=self.market_probabilities[keep],
            market_offsets=self.market_offsets[keep],
            skipped={},
        )


def _race_sort_key(key):
    try:
        race_no = int(key[2])
    except (IndexError, TypeError, ValueError):
        race_no = -1
    return str(key[0]), str(key[1]), race_no


def _field_size(rows):
    sizes = []
    for row in rows:
        try:
            value = int(row.get("total_horses") or 0)
        except (TypeError, ValueError):
            value = 0
        sizes.append(value)
    if not sizes or any(value <= 0 for value in sizes) or len(set(sizes)) != 1:
        return None
    return sizes[0]


def prepare_complete_races(features, labels, race_keys, meta, *,
                           min_race_no=9, min_horses=8):
    """Apply one strict population filter before any model is fitted.

    A retained race has all official runners represented, finite features, at
    least one winner row, and valid settled final odds for every runner.  The
    same retained rows therefore feed M0, current CL and M3.
    """
    features = np.asarray(features, dtype=float)
    labels = np.asarray(labels, dtype=float)
    race_keys = list(race_keys)
    meta = list(meta)
    row_count = len(race_keys)
    if features.ndim != 2:
        raise ValueError("features must be a two-dimensional matrix")
    if (len(features) != row_count or len(labels) != row_count
            or len(meta) != row_count):
        raise ValueError("features, labels, race_keys and meta must align")

    grouped = defaultdict(list)
    for index, key in enumerate(race_keys):
        grouped[key].append(index)

    retained = []
    probabilities = []
    offsets = []
    skipped = Counter()
    for key in sorted(grouped, key=_race_sort_key):
        indices = grouped[key]
        try:
            race_no = int(key[2])
        except (IndexError, TypeError, ValueError):
            race_no = -1
        if race_no < min_race_no or len(indices) < min_horses:
            skipped["filter"] += 1
            continue

        rows = [meta[index] for index in indices]
        official_size = _field_size(rows)
        if official_size is None or len(indices) != official_size:
            skipped["incomplete_features"] += 1
            continue
        if not np.isfinite(features[indices]).all():
            skipped["missing_features"] += 1
            continue
        if not np.any(labels[indices] == 1):
            skipped["no_winner"] += 1
            continue

        odds = [
            parse_final_odds(row.get("win_pay"), row.get("rank"))
            for row in rows
        ]
        if any(value is None or not math.isfinite(value) or value <= 1.0
               for value in odds):
            skipped["missing_odds"] += 1
            continue
        inverse_odds = 1.0 / np.asarray(odds, dtype=float)
        market_probability = inverse_odds / inverse_odds.sum()
        retained.extend(indices)
        probabilities.extend(market_probability.tolist())
        offsets.extend(np.log(market_probability).tolist())

    retained = np.asarray(retained, dtype=np.int64)
    return CompleteRaceDataset(
        features=features[retained],
        labels=labels[retained],
        race_keys=[race_keys[index] for index in retained],
        meta=[meta[index] for index in retained],
        market_probabilities=np.asarray(probabilities, dtype=float),
        market_offsets=np.asarray(offsets, dtype=float),
        skipped=dict(skipped),
    )


def _race_indices(race_keys):
    grouped = defaultdict(list)
    for index, key in enumerate(race_keys):
        grouped[key].append(index)
    return [grouped[key] for key in sorted(grouped, key=_race_sort_key)]


def _race_softmax(scores, race_keys, temperature=1.0):
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    scores = np.asarray(scores, dtype=float)
    if scores.ndim != 1 or len(scores) != len(race_keys):
        raise ValueError("scores must align with race_keys")
    probabilities = np.empty(len(scores), dtype=float)
    for indices in _race_indices(race_keys):
        values = scores[indices] / float(temperature)
        values = values - values.max()
        exp_values = np.exp(values)
        probabilities[indices] = exp_values / exp_values.sum()
    return probabilities


def _horse_number(row, fallback):
    try:
        return int(row.get("umaban"))
    except (TypeError, ValueError):
        return 1000 + fallback


def _order(indices, values, meta):
    return sorted(
        indices,
        key=lambda index: (-float(values[index]),
                           _horse_number(meta[index], index)),
    )


def _market_order(indices, dataset):
    """JRA popularity order used for M0 ranking and swap comparisons.

    Settled inverse odds remain the probability source.  ``popularity`` is a
    separate rank field and must be the primary ordering key when displayed
    odds tie; otherwise horse number would create artificial market swaps.
    """
    def key(index):
        try:
            popularity = int(dataset.meta[index].get("popularity"))
        except (TypeError, ValueError):
            popularity = 999
        return (
            popularity,
            -float(dataset.market_probabilities[index]),
            _horse_number(dataset.meta[index], index),
        )

    return sorted(indices, key=key)


def _aggregate_metrics(dataset, probabilities, *, market_order=False):
    hits = np.zeros(4, dtype=float)
    logloss = 0.0
    winner_events = 0
    brier = 0.0
    horse_count = 0
    for indices in _race_indices(dataset.race_keys):
        order = (_market_order(indices, dataset) if market_order else
                 _order(indices, probabilities, dataset.meta))
        winners = [index for index in indices if dataset.labels[index] == 1]
        winner_set = set(winners)
        for k in range(1, 5):
            hits[k - 1] += bool(winner_set.intersection(order[:k]))
        for winner in winners:
            logloss -= math.log(max(1e-15, float(probabilities[winner])))
        winner_events += len(winners)
        outcomes = np.zeros(len(indices), dtype=float)
        outcomes[[indices.index(winner) for winner in winners]] = 1.0
        brier += float(np.square(
            outcomes - np.asarray(probabilities)[indices]).sum())
        horse_count += len(indices)
    race_count = dataset.race_count
    return {
        "coverage": (hits / race_count).tolist() if race_count else [],
        "logloss": logloss / winner_events if winner_events else float("nan"),
        "brier": brier / horse_count if horse_count else float("nan"),
    }


def _market_changes(dataset, model_probabilities):
    inversion_counts = []
    total_pairs = 0
    probability_error = 0.0
    for indices in _race_indices(dataset.race_keys):
        market_order = _market_order(indices, dataset)
        model_order = _order(indices, model_probabilities, dataset.meta)
        market_position = {member: rank for rank, member in enumerate(market_order)}
        model_position = {member: rank for rank, member in enumerate(model_order)}
        inversions = 0
        for left_position, left in enumerate(indices):
            for right in indices[left_position + 1:]:
                market_delta = market_position[left] - market_position[right]
                model_delta = model_position[left] - model_position[right]
                inversions += market_delta * model_delta < 0
        inversion_counts.append(inversions)
        total_pairs += len(indices) * (len(indices) - 1) // 2
        probability_error += float(np.abs(
            model_probabilities[indices]
            - dataset.market_probabilities[indices]).sum())
    race_count = dataset.race_count
    return {
        "mean_inversion_pairs": (
            float(np.mean(inversion_counts)) if inversion_counts else float("nan")
        ),
        "inversion_pair_rate": (
            sum(inversion_counts) / total_pairs if total_pairs else float("nan")
        ),
        "any_swap_rate": (
            sum(value > 0 for value in inversion_counts) / race_count
            if race_count else float("nan")
        ),
        "market_probability_mae": (
            probability_error / len(dataset.labels)
            if len(dataset.labels) else float("nan")
        ),
    }


def evaluate_same_population(dataset, model_scores, *, temperatures=None):
    """Evaluate M0 and every supplied score vector on identical retained rows."""
    if not dataset.race_count:
        raise RuntimeError("no complete races in evaluation population")
    temperatures = temperatures or {}
    probability_sets = {"m0": dataset.market_probabilities}
    for name, scores in model_scores.items():
        scores = np.asarray(scores, dtype=float)
        if scores.ndim != 1 or len(scores) != len(dataset.labels):
            raise ValueError(f"{name} scores do not align with the dataset")
        if not np.isfinite(scores).all():
            raise ValueError(f"{name} scores contain non-finite values")
        probability_sets[name] = _race_softmax(
            scores, dataset.race_keys, temperatures.get(name, 1.0))

    models = {
        name: _aggregate_metrics(
            dataset, probabilities, market_order=(name == "m0"))
        for name, probabilities in probability_sets.items()
    }
    changes = {
        name: _market_changes(dataset, probabilities)
        for name, probabilities in probability_sets.items() if name != "m0"
    }
    signature = tuple(
        (key, tuple(
            (str(dataset.meta[index].get("umaban") or ""),
             str(dataset.meta[index].get("horse") or ""))
            for index in indices
        ))
        for key, indices in zip(
            sorted(set(dataset.race_keys), key=_race_sort_key),
            _race_indices(dataset.race_keys),
        )
    )
    return {
        "races": dataset.race_count,
        "horses": len(dataset.labels),
        "models": models,
        "market_changes": changes,
        "sample_signature": signature,
    }


def select_lambda(lambda_diagnostics, *, tie_tolerance=1e-12):
    """Select lowest tune LogLoss; choose stronger lambda for a numeric tie."""
    if not lambda_diagnostics:
        raise ValueError("lambda diagnostics must not be empty")
    best_loss = min(float(row["tune_logloss"]) for row in lambda_diagnostics)
    if not math.isfinite(best_loss):
        raise ValueError("lambda diagnostics contain no finite tune LogLoss")
    tolerance = tie_tolerance * max(1.0, abs(best_loss))
    tied = [
        row for row in lambda_diagnostics
        if abs(float(row["tune_logloss"]) - best_loss) <= tolerance
    ]
    return max(tied, key=lambda row: float(row["lambda"]))


def _floor_check(market_metrics, m3_metrics, tolerance=1e-12):
    deltas = [
        float(m3) - float(market)
        for market, m3 in zip(
            market_metrics["coverage"][:4], m3_metrics["coverage"][:4])
    ]
    failed = [index + 1 for index, value in enumerate(deltas) if value < -tolerance]
    return {"passed": not failed, "coverage_delta": deltas, "failed_k": failed}


def _required_subset(dataset, period, label):
    subset = dataset.subset(*period)
    if not subset.race_count:
        raise RuntimeError(f"no complete races for {label}: {period[0]}-{period[1]}")
    return subset


def evaluate_m3_dataset(features, labels, race_keys, meta, *,
                        lambda_grid=LAMBDA_GRID,
                        residual_feature_names=NO_MARKET_FEATURES):
    """Tune from a 2021-23 fit, refit through 2024, then freeze for OOS.

    ``fit_conditional_logit`` minimizes a *summed* conditional NLL.  The public
    lambda grid is therefore converted to ``internal_l2 = lambda * n_races``;
    this gives lambda a mean-loss interpretation that is stable across sample
    sizes.  M3 probabilities are never temperature-scaled because that would
    change the fixed market coefficient away from one.
    """
    import gc

    full_features = np.asarray(features, dtype=float)
    full_labels = np.asarray(labels)
    full_keys = list(race_keys)
    full_dates = np.asarray([key[0] for key in full_keys])
    current_inner_mask = (
        (full_dates >= TRAIN_PERIOD[0]) & (full_dates <= TRAIN_PERIOD[1]))
    current_tune_mask = (
        (full_dates >= TUNE_PERIOD[0]) & (full_dates <= TUNE_PERIOD[1]))
    current_final_mask = (
        (full_dates >= TRAIN_PERIOD[0]) & (full_dates <= TUNE_PERIOD[1]))
    if not current_inner_mask.any():
        raise RuntimeError("no current-CL training rows in 2021-2023")
    if not current_final_mask.any():
        raise RuntimeError("no current-CL final-fit rows in 2021-2024")
    if not current_tune_mask.any():
        raise RuntimeError("no current-CL temperature-tuning rows in 2024")

    complete = prepare_complete_races(features, labels, race_keys, meta)
    train = _required_subset(complete, TRAIN_PERIOD, "train")
    tune = _required_subset(complete, TUNE_PERIOD, "tune")
    final_train = _required_subset(
        complete, (TRAIN_PERIOD[0], TUNE_PERIOD[1]), "final train")
    evaluations = {
        name: _required_subset(complete, period, name)
        for name, period in EVALUATION_PERIODS.items()
    }

    lambda_grid = tuple(sorted({float(value) for value in lambda_grid}))
    if not lambda_grid or any(value <= 0 or not math.isfinite(value)
                              for value in lambda_grid):
        raise ValueError("lambda_grid must contain positive finite values")
    residual_feature_names = list(residual_feature_names)
    if not residual_feature_names or len(set(residual_feature_names)) != len(
            residual_feature_names):
        raise ValueError("residual_feature_names must be non-empty and unique")

    from sklearn.preprocessing import StandardScaler

    # Tune the comparison CL's temperature without using 2024 in its inner
    # coefficient fit, then refit the frozen comparison through 2024.  This is
    # the same split convention as fit_cl_diagnostic_model (T12).  M3 itself
    # uses the stricter complete-field/odds population because its fixed
    # offset is a race probability vector.
    current_inner = feature_matrix(
        full_features[current_inner_mask], FEATURES)
    current_tune = feature_matrix(
        full_features[current_tune_mask], FEATURES)
    current_inner_scaler = StandardScaler().fit(current_inner)
    current_inner_weights = fit_conditional_logit(
        current_inner_scaler.transform(current_inner),
        full_labels[current_inner_mask],
        [key for key, keep in zip(full_keys, current_inner_mask) if keep],
        l2=1.0,
        offset=None,
    )
    current_tune_scores = (
        current_inner_scaler.transform(current_tune) @ current_inner_weights)
    current_temperature = fit_temperature(
        current_tune_scores,
        full_labels[current_tune_mask],
        [key for key, keep in zip(full_keys, current_tune_mask) if keep],
    )
    current_final = feature_matrix(
        full_features[current_final_mask], FEATURES)
    current_scaler = StandardScaler().fit(current_final)
    current_weights = fit_conditional_logit(
        current_scaler.transform(current_final),
        full_labels[current_final_mask],
        [key for key, keep in zip(full_keys, current_final_mask) if keep],
        l2=1.0,
        offset=None,
    )
    del (current_inner, current_tune, current_tune_scores,
         current_inner_scaler, current_inner_weights, current_final)
    gc.collect()

    residual_train = feature_matrix(train.features, residual_feature_names)
    residual_tune = feature_matrix(tune.features, residual_feature_names)
    residual_scaler = StandardScaler().fit(residual_train)
    residual_train_z = residual_scaler.transform(residual_train)
    residual_tune_z = residual_scaler.transform(residual_tune)
    del residual_train, residual_tune
    gc.collect()
    train_races = train.race_count

    lambda_rows = []
    weights_by_lambda = {}
    for regularization in lambda_grid:
        internal_l2 = regularization * train_races
        weights = fit_conditional_logit(
            residual_train_z,
            train.labels,
            train.race_keys,
            l2=internal_l2,
            offset=train.market_offsets,
        )
        tune_scores = tune.market_offsets + residual_tune_z @ weights
        comparison = evaluate_same_population(tune, {"m3": tune_scores})
        changes = comparison["market_changes"]["m3"]
        row = {
            "lambda": regularization,
            "internal_l2": internal_l2,
            "residual_norm": float(np.linalg.norm(weights)),
            "tune_logloss": comparison["models"]["m3"]["logloss"],
            **changes,
        }
        lambda_rows.append(row)
        weights_by_lambda[regularization] = weights

    selected = select_lambda(lambda_rows)
    selected_lambda = float(selected["lambda"])
    selection_weights = weights_by_lambda[selected_lambda]
    del residual_train_z, residual_tune_z, weights_by_lambda
    gc.collect()

    # The 2024 rows choose only lambda.  Once selected, refit the deployable
    # diagnostic model on 2021-2024 and keep 2025/2026H1 untouched.
    residual_final = feature_matrix(
        final_train.features, residual_feature_names)
    residual_scaler = StandardScaler().fit(residual_final)
    final_races = final_train.race_count
    final_internal_l2 = selected_lambda * final_races
    selected_weights = fit_conditional_logit(
        residual_scaler.transform(residual_final),
        final_train.labels,
        final_train.race_keys,
        l2=final_internal_l2,
        offset=final_train.market_offsets,
    )
    del residual_final
    gc.collect()

    period_results = {}
    for period_name, dataset in evaluations.items():
        current_matrix = feature_matrix(dataset.features, FEATURES)
        residual_matrix = feature_matrix(dataset.features, residual_feature_names)
        current_scores = current_scaler.transform(current_matrix) @ current_weights
        # No temperature is applied to this full M3 score.
        m3_scores = (
            dataset.market_offsets
            + residual_scaler.transform(residual_matrix) @ selected_weights
        )
        comparison = evaluate_same_population(
            dataset,
            {"current_cl": current_scores, "m3": m3_scores},
            temperatures={"current_cl": current_temperature},
        )
        comparison["m3_floor"] = _floor_check(
            comparison["models"]["m0"], comparison["models"]["m3"])
        period_results[period_name] = comparison

    market_limit = _race_softmax(
        complete.market_offsets, complete.race_keys, temperature=1.0)
    market_limit_error = float(np.max(np.abs(
        market_limit - complete.market_probabilities)))
    if market_limit_error > 1e-12:
        raise AssertionError("zero-residual M3 failed to reproduce market probabilities")

    return {
        "stats_source": "ability yearly as-of",
        "population_filter": (
            "race 9+, official field 8+, complete feature rows, settled odds"
        ),
        "common_skipped": complete.skipped,
        "splits": {
            "train": {"rows": len(train.labels), "races": train.race_count},
            "tune": {"rows": len(tune.labels), "races": tune.race_count},
            **{
                name: {"rows": len(dataset.labels), "races": dataset.race_count}
                for name, dataset in evaluations.items()
            },
        },
        "regularization": {
            "objective": "summed conditional NLL",
            "conversion": "internal_l2 = lambda * train_races",
            "train_races": train_races,
            "lambda_diagnostics": lambda_rows,
            "selected_lambda": selected_lambda,
            "selected_internal_l2": float(selected["internal_l2"]),
            "selected_residual_norm": float(selected["residual_norm"]),
            "final_fit_races": final_races,
            "final_internal_l2": final_internal_l2,
            "final_residual_norm": float(np.linalg.norm(selected_weights)),
        },
        "current_cl": {
            "temperature": current_temperature,
            "weight_norm": float(np.linalg.norm(current_weights)),
            "train_rows": int(current_final_mask.sum()),
            "train_races": len({
                key for key, keep in zip(full_keys, current_final_mask) if keep
            }),
            "train_population": "all available feature rows (2021-2024 final refit)",
            "temperature_fit_rows": int(current_inner_mask.sum()),
            "temperature_tune_rows": int(current_tune_mask.sum()),
            "temperature_fit_population": (
                "all available 2021-2023 coefficient rows; "
                "all available 2024 temperature rows"
            ),
        },
        "m3": {
            "temperature": None,
            "market_limit_max_abs_error": market_limit_error,
            "residual_feature_names": residual_feature_names,
            "selection_weights": [float(value) for value in selection_weights],
            "selected_weights": [float(value) for value in selected_weights],
            "fit_population": "2021-2024 final refit after 2024 lambda selection",
        },
        "periods": period_results,
    }


def run_m3_diagnostic(runs, cfg, *, db_path=DB_PATH, lambda_grid=LAMBDA_GRID,
                      residual_feature_names=NO_MARKET_FEATURES,
                      dataset_builder=None):
    """Build T33 ability features and run the frozen M3 diagnostic."""
    builder = dataset_builder or build_consistent_feature_dataset
    features, labels, race_keys, meta = builder(
        runs,
        cfg,
        EVALUATION_PERIODS["2026H1"][1],
        stats_source="ability",
        db_path=db_path,
    )
    return evaluate_m3_dataset(
        features, labels, race_keys, meta, lambda_grid=lambda_grid,
        residual_feature_names=residual_feature_names)


def _metric_cells(metrics):
    coverage = list(metrics["coverage"][:4])
    coverage.extend([float("nan")] * (4 - len(coverage)))
    topk = "  ".join(
        " n/a" if math.isnan(value) else f"{100.0 * value:4.1f}"
        for value in coverage
    )
    return f"{topk} | {metrics['logloss']:.6f} | {metrics['brier']:.8f}"


def print_report(report, *, file=None):
    """Print selection diagnostics and fixed-period same-population metrics."""
    import sys

    stream = file or sys.stdout
    regularization = report["regularization"]
    print("\n===== M3 market-offset diagnostic (evaluation only) =====", file=stream)
    print(f"stats source: {report['stats_source']}", file=stream)
    print(f"population: {report['population_filter']}", file=stream)
    print(
        "regularization: summed conditional NLL; "
        f"internal L2 = lambda * {regularization['train_races']} train races",
        file=stream,
    )
    print(
        "final refit: 2021-2024, "
        f"{regularization['final_fit_races']} races, "
        f"internal L2={regularization['final_internal_l2']:.6g}",
        file=stream,
    )
    print("M3 temperature: disabled (fixed market coefficient = 1)", file=stream)
    print("lambda | internal L2 | residual ||w|| | tune LL | market MAE | "
          "mean inversions | pair rate | any swap", file=stream)
    for row in regularization["lambda_diagnostics"]:
        marker = " *" if row["lambda"] == regularization["selected_lambda"] else ""
        print(
            f"{row['lambda']:6g} | {row['internal_l2']:11.4g} | "
            f"{row['residual_norm']:14.6f} | {row['tune_logloss']:.6f} | "
            f"{row['market_probability_mae']:.8f} | "
            f"{row['mean_inversion_pairs']:.3f} | "
            f"{100.0 * row['inversion_pair_rate']:.2f}% | "
            f"{100.0 * row['any_swap_rate']:.1f}%{marker}",
            file=stream,
        )

    for period_name, result in report["periods"].items():
        print(f"\n{period_name}: n={result['races']} races/{result['horses']} horses",
              file=stream)
        print("model      | k=1   k=2   k=3   k=4 | LogLoss | Brier", file=stream)
        for name in ("m0", "current_cl", "m3"):
            print(f"{name:10s} | {_metric_cells(result['models'][name])}", file=stream)
        changes = result["market_changes"]["m3"]
        floor = result["m3_floor"]
        print(
            "M3 vs market: "
            f"mean inversion pairs={changes['mean_inversion_pairs']:.3f}, "
            f"pair rate={100.0 * changes['inversion_pair_rate']:.2f}%, "
            f"any swap={100.0 * changes['any_swap_rate']:.1f}%",
            file=stream,
        )
        floor_label = "PASS" if floor["passed"] else (
            "FAIL k=" + ",".join(map(str, floor["failed_k"]))
        )
        print(f"M3 realized top-k floor: {floor_label}", file=stream)


def main(*, db_path=DB_PATH):
    """Load the fixed 2021-2026H1 window, run diagnostics, and print it."""
    import sqlite3
    from contextlib import closing

    from backtest_ability import load_runs
    from backtest_win5 import load_win5_cfg

    with closing(sqlite3.connect(db_path)) as connection:
        runs = load_runs(
            connection, TRAIN_PERIOD[0], EVALUATION_PERIODS["2026H1"][1])
    print(f"loaded: {len(runs)} rows (2021-2026H1)")
    # build_consistent_feature_dataset needs the full history, but fitting does
    # not.  Release the several-hundred-thousand source dictionaries before
    # allocating standardized train/tune matrices.
    dataset = build_consistent_feature_dataset(
        runs,
        load_win5_cfg(),
        EVALUATION_PERIODS["2026H1"][1],
        stats_source="ability",
        db_path=db_path,
    )
    del runs
    import gc
    gc.collect()
    report = evaluate_m3_dataset(*dataset)
    print_report(report)
    return report


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
