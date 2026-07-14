"""Evaluation-only diagnostics for M4-B selective market correction.

M4-B keeps ``log(normalized 1 / final_odds)`` at coefficient one and learns a
small residual that describes *where* the market may be wrong.  Lambda and
the inference cap are selected by a 2021-2023 fit and a 2024 tune split; the
selected residual is then refitted on strict 2021-2024 races before the
frozen 2025/2026H1 evaluation.  The M2 rank gap used by that residual is
out-of-fold for the 2021-2023 training rows and is produced by one frozen
2021-2023 M2 model for every later row.  Nothing in this module writes a model
or changes production scoring.
"""
from __future__ import annotations

import argparse
import gc
import math
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from backtest_ability import CLASS_RANK, load_runs
from backtest_market_diagnostics import (
    DB_PATH,
    EVALUATION_PERIODS,
    LAMBDA_GRID,
    TRAIN_PERIOD,
    TUNE_PERIOD,
    CompleteRaceDataset,
    _floor_check,
    _market_order,
    _order,
    _race_indices,
    _required_subset,
    evaluate_m3_dataset,
    evaluate_same_population,
    prepare_complete_races,
)
from backtest_ml import (
    FEATURES,
    NO_MARKET_FEATURES,
    feature_matrix,
    fit_conditional_logit,
)
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_win5 import load_win5_cfg


CAP_GRID = (0.1, 0.2, 0.4)
M2_OOF_SPLITS = 5

# pop_conc and field_size are intentionally calculated below, but a
# conditional-logit likelihood cannot identify a value shared by every horse
# in a race: it cancels exactly from the race softmax denominator.
CORRECTION_FEATURES = (
    "prev_rank_pop_gap",
    "prev_rank",
    "ln_interval",
    "class_up",
    "m2_gap",
    "fav_flag_x_prev_rank",
)
EXCLUDED_RACE_CONSTANT_FEATURES = ("pop_conc", "field_size")
ALL_CANDIDATE_FEATURES = (
    "prev_rank_pop_gap",
    "prev_rank",
    "ln_interval",
    "class_up",
    "m2_gap",
    "pop_conc",
    "field_size",
    "fav_flag_x_prev_rank",
)
EXCLUSION_REASONS = {
    "pop_conc": (
        "race-constant: identical for every runner and therefore cancels "
        "from conditional-logit softmax"
    ),
    "field_size": (
        "race-constant: identical for every runner and therefore cancels "
        "from conditional-logit softmax"
    ),
}


@dataclass(frozen=True)
class PriorRun:
    date: str
    rank: float | None
    popularity: float | None
    race_class: str | None


@dataclass
class PriorContext:
    """Compact previous-run lookup that does not retain the full run history."""

    by_object_id: dict
    by_runner_key: dict

    def get(self, runner):
        value = self.by_object_id.get(id(runner))
        if value is not None:
            return value
        return self.by_runner_key.get(_runner_key(runner))


@dataclass
class CandidateFeatureSet:
    values: np.ndarray
    available: np.ndarray
    names: tuple = ALL_CANDIDATE_FEATURES

    def selected(self, names=CORRECTION_FEATURES):
        indices = [self.names.index(name) for name in names]
        return self.values[:, indices], self.available[:, indices]


def _runner_key(row):
    return (
        str(row.get("date") or ""),
        str(row.get("horse") or ""),
        str(row.get("place") or ""),
        row.get("r"),
        row.get("umaban"),
    )


def _number_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def build_prior_context(runs, target_meta=None):
    """Build the exact previous-run context used by ``build_dataset``.

    Only contexts for ``target_meta`` are retained.  This permits the caller
    to release the much larger source ``runs`` list before model fitting.
    """
    target_meta = list(target_meta) if target_meta is not None else list(runs)
    target_ids = {id(row) for row in target_meta}
    target_keys = {_runner_key(row) for row in target_meta}

    by_horse = defaultdict(list)
    for row in runs:
        horse = row.get("horse")
        if horse:
            by_horse[horse].append(row)
    for rows in by_horse.values():
        # Stable date-only ordering deliberately matches backtest_ml.build_dataset.
        rows.sort(key=lambda row: row.get("date") or "")

    by_object_id = {}
    by_runner_key = {}
    for rows in by_horse.values():
        for index in range(1, len(rows)):
            current = rows[index]
            key = _runner_key(current)
            if id(current) not in target_ids and key not in target_keys:
                continue
            previous = rows[index - 1]
            compact = PriorRun(
                date=str(previous.get("date") or ""),
                rank=_number_or_none(previous.get("rank")),
                popularity=_number_or_none(previous.get("popularity")),
                race_class=(str(previous.get("race_class"))
                            if previous.get("race_class") else None),
            )
            if id(current) in target_ids:
                by_object_id[id(current)] = compact
            if key in target_keys:
                by_runner_key[key] = compact
    return PriorContext(by_object_id=by_object_id, by_runner_key=by_runner_key)


def clip_correction(correction, cap):
    """Clip an uncapped residual at inference time with a monotone transform."""
    cap = float(cap)
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("cap must be a positive finite value")
    values = np.asarray(correction, dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("correction contains non-finite values")
    return np.clip(values, -cap, cap)


def is_race_constant(values, race_keys, *, tolerance=1e-12):
    """Return whether a vector is constant inside every represented race."""
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or len(values) != len(race_keys):
        raise ValueError("values must be one-dimensional and align with race_keys")
    for indices in _race_indices(race_keys):
        race_values = values[indices]
        if not np.isfinite(race_values).all():
            return False
        if float(np.max(race_values) - np.min(race_values)) > tolerance:
            return False
    return True


def _valid_interval(current_date, previous_date):
    try:
        current = datetime.strptime(str(current_date), "%Y%m%d")
        previous = datetime.strptime(str(previous_date), "%Y%m%d")
    except (TypeError, ValueError):
        return False
    return current >= previous


def build_candidate_features(dataset, m2_scores, prior_context):
    """Calculate all eight specified candidates and retain the six identifiable.

    Missing historical values remain NaN here.  They are imputed from the
    2021-2023 training split only, so later-period information cannot leak into
    the fitted residual.
    """
    m2_scores = np.asarray(m2_scores, dtype=float)
    if m2_scores.ndim != 1 or len(m2_scores) != len(dataset.labels):
        raise ValueError("m2_scores must align with the complete dataset")
    if not np.isfinite(m2_scores).all():
        raise ValueError("m2_scores contain non-finite values")

    values = np.full(
        (len(dataset.labels), len(ALL_CANDIDATE_FEATURES)), np.nan, dtype=float)
    available = np.zeros(values.shape, dtype=bool)
    columns = {name: index for index, name in enumerate(ALL_CANDIDATE_FEATURES)}
    base = feature_matrix(dataset.features, ("prev_rank", "ln_interval"))

    for indices in _race_indices(dataset.race_keys):
        market_order = _market_order(indices, dataset)
        m2_order = _order(indices, m2_scores, dataset.meta)
        market_position = {member: rank for rank, member in enumerate(market_order, 1)}
        m2_position = {member: rank for rank, member in enumerate(m2_order, 1)}
        ordered_probabilities = sorted(
            (float(dataset.market_probabilities[index]) for index in indices),
            reverse=True,
        )
        concentration = (
            ordered_probabilities[0] - ordered_probabilities[1]
            if len(ordered_probabilities) >= 2 else 0.0
        )
        for index in indices:
            values[index, columns["m2_gap"]] = (
                m2_position[index] - market_position[index])
            available[index, columns["m2_gap"]] = True
            values[index, columns["pop_conc"]] = concentration
            available[index, columns["pop_conc"]] = True
            values[index, columns["field_size"]] = len(indices)
            available[index, columns["field_size"]] = True

    for index, runner in enumerate(dataset.meta):
        previous = prior_context.get(runner)
        if previous is None:
            continue
        previous_rank = previous.rank
        if previous_rank is not None:
            previous_rank = min(previous_rank, 18.0)
            values[index, columns["prev_rank"]] = previous_rank
            available[index, columns["prev_rank"]] = True
        if previous_rank is not None and previous.popularity is not None:
            values[index, columns["prev_rank_pop_gap"]] = (
                previous_rank - previous.popularity)
            available[index, columns["prev_rank_pop_gap"]] = True

        if _valid_interval(runner.get("date"), previous.date):
            # Reuse build_dataset's exact ln_interval value rather than
            # independently rounding or parsing it a second way.
            values[index, columns["ln_interval"]] = base[index, 1]
            available[index, columns["ln_interval"]] = True

        current_class = runner.get("race_class")
        if current_class in CLASS_RANK and previous.race_class in CLASS_RANK:
            values[index, columns["class_up"]] = float(
                CLASS_RANK[current_class] > CLASS_RANK[previous.race_class])
            available[index, columns["class_up"]] = True

        current_popularity = _number_or_none(runner.get("popularity"))
        if previous_rank is not None and current_popularity is not None:
            values[index, columns["fav_flag_x_prev_rank"]] = (
                previous_rank if int(current_popularity) == 1 else 0.0)
            available[index, columns["fav_flag_x_prev_rank"]] = True

    for name in EXCLUDED_RACE_CONSTANT_FEATURES:
        if not is_race_constant(values[:, columns[name]], dataset.race_keys):
            raise AssertionError(f"{name} must be race-constant")
    return CandidateFeatureSet(values=values, available=available)


def _date_mask(race_keys, period):
    return np.asarray(
        [period[0] <= key[0] <= period[1] for key in race_keys], dtype=bool)


def _keys_for_indices(race_keys, indices):
    return [race_keys[int(index)] for index in indices]


def fit_m2_oof_scores(dataset, *, n_splits=M2_OOF_SPLITS):
    """Return leakage-safe M2 scores for M4-B's rank-gap feature.

    Training rows (2021-2023) receive race-grouped out-of-fold predictions.
    A final model fitted only on 2021-2023 supplies all 2024+ scores.
    """
    from sklearn.model_selection import GroupKFold
    from sklearn.preprocessing import StandardScaler

    matrix = feature_matrix(dataset.features, NO_MARKET_FEATURES)
    train_mask = _date_mask(dataset.race_keys, TRAIN_PERIOD)
    train_indices = np.flatnonzero(train_mask)
    train_keys = _keys_for_indices(dataset.race_keys, train_indices)
    unique_keys = list(dict.fromkeys(train_keys))
    fold_count = min(int(n_splits), len(unique_keys))
    if fold_count < 2:
        raise RuntimeError("M2 OOF requires at least two training races")
    group_id = {key: index for index, key in enumerate(unique_keys)}
    groups = np.asarray([group_id[key] for key in train_keys], dtype=np.int64)

    scores = np.full(len(dataset.labels), np.nan, dtype=float)
    assignments = np.zeros(len(train_indices), dtype=np.int8)
    folds = []
    splitter = GroupKFold(n_splits=fold_count)
    for fold_no, (fit_local, validation_local) in enumerate(
            splitter.split(matrix[train_indices], dataset.labels[train_indices], groups), 1):
        fit_indices = train_indices[fit_local]
        validation_indices = train_indices[validation_local]
        scaler = StandardScaler().fit(matrix[fit_indices])
        weights = fit_conditional_logit(
            scaler.transform(matrix[fit_indices]),
            dataset.labels[fit_indices],
            _keys_for_indices(dataset.race_keys, fit_indices),
        )
        scores[validation_indices] = (
            scaler.transform(matrix[validation_indices]) @ weights)
        assignments[validation_local] += 1
        fit_races = set(_keys_for_indices(dataset.race_keys, fit_indices))
        validation_races = set(
            _keys_for_indices(dataset.race_keys, validation_indices))
        if fit_races.intersection(validation_races):
            raise AssertionError("GroupKFold split a race across M2 fit/validation")
        folds.append({
            "fold": fold_no,
            "train_races": len(fit_races),
            "validation_races": len(validation_races),
            "race_overlap": 0,
        })
    if not np.all(assignments == 1) or not np.isfinite(scores[train_indices]).all():
        raise AssertionError("every M2 training row must receive exactly one OOF score")

    final_scaler = StandardScaler().fit(matrix[train_indices])
    final_weights = fit_conditional_logit(
        final_scaler.transform(matrix[train_indices]),
        dataset.labels[train_indices],
        train_keys,
    )
    later_indices = np.flatnonzero(~train_mask)
    scores[later_indices] = (
        final_scaler.transform(matrix[later_indices]) @ final_weights)
    if not np.isfinite(scores).all():
        raise AssertionError("M2 score construction left non-finite rows")
    return scores, {
        "features": list(NO_MARKET_FEATURES),
        "train_period": TRAIN_PERIOD,
        "train_races": len(unique_keys),
        "oof_folds": folds,
        "oof_assignment_min": int(assignments.min()),
        "oof_assignment_max": int(assignments.max()),
        "final_weight_norm": float(np.linalg.norm(final_weights)),
    }


def select_lambda_cap(diagnostics, *, tie_tolerance=1e-12):
    """Select tune LL; numeric ties prefer stronger lambda then smaller cap."""
    if not diagnostics:
        raise ValueError("lambda/cap diagnostics must not be empty")
    best_loss = min(float(row["tune_logloss"]) for row in diagnostics)
    if not math.isfinite(best_loss):
        raise ValueError("lambda/cap diagnostics contain no finite tune LogLoss")
    tolerance = tie_tolerance * max(1.0, abs(best_loss))
    tied = [
        row for row in diagnostics
        if abs(float(row["tune_logloss"]) - best_loss) <= tolerance
    ]
    return max(tied, key=lambda row: (float(row["lambda"]), -float(row["cap"])))


def _imputation_values(train_matrix):
    values = []
    for column in range(train_matrix.shape[1]):
        observed = train_matrix[np.isfinite(train_matrix[:, column]), column]
        values.append(float(np.median(observed)) if len(observed) else 0.0)
    return np.asarray(values, dtype=float)


def _impute(matrix, values):
    result = np.asarray(matrix, dtype=float).copy()
    missing = ~np.isfinite(result)
    if missing.any():
        result[missing] = np.take(values, np.nonzero(missing)[1])
    return result


def _swapped_pair_outcomes(dataset, model_scores):
    """Count whether market/model inverted pairs move the realized winner."""
    model_scores = np.asarray(model_scores, dtype=float)
    counts = {"improved": 0, "worsened": 0, "neutral": 0, "total": 0}
    for indices in _race_indices(dataset.race_keys):
        market_order = _market_order(indices, dataset)
        model_order = _order(indices, model_scores, dataset.meta)
        market_position = {member: rank for rank, member in enumerate(market_order)}
        model_position = {member: rank for rank, member in enumerate(model_order)}
        winners = {index for index in indices if dataset.labels[index] == 1}
        for left_position, left in enumerate(indices):
            for right in indices[left_position + 1:]:
                if ((market_position[left] - market_position[right])
                        * (model_position[left] - model_position[right]) >= 0):
                    continue
                counts["total"] += 1
                left_wins = left in winners
                right_wins = right in winners
                if left_wins == right_wins:
                    counts["neutral"] += 1
                    continue
                winner = left if left_wins else right
                loser = right if left_wins else left
                if (model_position[winner] < model_position[loser]
                        and market_position[winner] > market_position[loser]):
                    counts["improved"] += 1
                elif (model_position[winner] > model_position[loser]
                      and market_position[winner] < market_position[loser]):
                    counts["worsened"] += 1
                else:
                    counts["neutral"] += 1
    if counts["total"] != sum(counts[name] for name in (
            "improved", "worsened", "neutral")):
        raise AssertionError("swapped-pair outcome counts do not add up")
    return counts


def _assert_same_m3_sample(m3_period, m4b_period):
    if m3_period["sample_signature"] != m4b_period["sample_signature"]:
        raise AssertionError("M3 and M4-B evaluation samples differ")
    for metric in ("coverage", "logloss", "brier"):
        left = np.asarray(m3_period["models"]["m0"][metric], dtype=float)
        right = np.asarray(m4b_period["models"]["m0"][metric], dtype=float)
        if not np.allclose(left, right, rtol=0.0, atol=1e-15):
            raise AssertionError(f"M3 and M4-B market {metric} differ")


def evaluate_m4b_dataset(features, labels, race_keys, meta, *, prior_context,
                         lambda_grid=LAMBDA_GRID, cap_grid=CAP_GRID,
                         m2_oof_splits=M2_OOF_SPLITS, m3_report=None):
    """Fit/tune M4-B and compare frozen 2025/2026H1 periods with T11 M3."""
    # T11 remains the single implementation of M3/current-CL fitting and its
    # same-population metrics.  M4-B asserts its retained samples match it.
    if m3_report is None:
        m3_report = evaluate_m3_dataset(features, labels, race_keys, meta)
    complete = prepare_complete_races(features, labels, race_keys, meta)
    train = _required_subset(complete, TRAIN_PERIOD, "train")
    tune = _required_subset(complete, TUNE_PERIOD, "tune")
    final_train = _required_subset(
        complete, (TRAIN_PERIOD[0], TUNE_PERIOD[1]), "final train")
    evaluations = {
        name: _required_subset(complete, period, name)
        for name, period in EVALUATION_PERIODS.items()
    }
    expected_splits = {
        "train": {"rows": len(train.labels), "races": train.race_count},
        "tune": {"rows": len(tune.labels), "races": tune.race_count},
        **{
            name: {"rows": len(dataset.labels), "races": dataset.race_count}
            for name, dataset in evaluations.items()
        },
    }
    if ("splits" in m3_report and m3_report["splits"] != expected_splits):
        raise AssertionError("M3 and M4-B train/tune/evaluation splits differ")

    lambda_grid = tuple(sorted({float(value) for value in lambda_grid}))
    cap_grid = tuple(sorted({float(value) for value in cap_grid}))
    if (not lambda_grid or any(value <= 0 or not math.isfinite(value)
                               for value in lambda_grid)):
        raise ValueError("lambda_grid must contain positive finite values")
    if (not cap_grid or any(value <= 0 or not math.isfinite(value)
                            for value in cap_grid)):
        raise ValueError("cap_grid must contain positive finite values")

    m2_scores, m2_report = fit_m2_oof_scores(
        complete, n_splits=m2_oof_splits)
    candidates = build_candidate_features(complete, m2_scores, prior_context)
    candidate_matrix, candidate_available = candidates.selected()
    train_mask = _date_mask(complete.race_keys, TRAIN_PERIOD)
    tune_mask = _date_mask(complete.race_keys, TUNE_PERIOD)
    final_mask = _date_mask(
        complete.race_keys, (TRAIN_PERIOD[0], TUNE_PERIOD[1]))
    selection_imputation = _imputation_values(candidate_matrix[train_mask])
    selection_matrix = _impute(candidate_matrix, selection_imputation)

    from sklearn.preprocessing import StandardScaler

    selection_scaler = StandardScaler().fit(selection_matrix[train_mask])
    selection_standardized = selection_scaler.transform(selection_matrix)
    train_races = train.race_count
    diagnostics = []
    weights_by_lambda = {}
    train_keys = [key for key, keep in zip(complete.race_keys, train_mask) if keep]
    for regularization in lambda_grid:
        weights = fit_conditional_logit(
            selection_standardized[train_mask],
            complete.labels[train_mask],
            train_keys,
            l2=regularization * train_races,
            offset=complete.market_offsets[train_mask],
        )
        uncapped_tune = selection_standardized[tune_mask] @ weights
        for cap in cap_grid:
            tune_scores = (
                complete.market_offsets[tune_mask]
                + clip_correction(uncapped_tune, cap)
            )
            comparison = evaluate_same_population(tune, {"m4b": tune_scores})
            changes = comparison["market_changes"]["m4b"]
            diagnostics.append({
                "lambda": regularization,
                "cap": cap,
                "internal_l2": regularization * train_races,
                "residual_norm": float(np.linalg.norm(weights)),
                "tune_logloss": comparison["models"]["m4b"]["logloss"],
                "tune_saturation_rate": float(
                    np.mean(np.abs(uncapped_tune) >= cap)),
                **changes,
            })
        weights_by_lambda[regularization] = weights
    selected = select_lambda_cap(diagnostics)
    selected_lambda = float(selected["lambda"])
    selected_cap = float(selected["cap"])
    selection_weights = weights_by_lambda[selected_lambda]

    # The 2024 rows choose only lambda/cap.  Refit every learned preprocessing
    # quantity and the offset residual on strict 2021-2024 races, then freeze
    # this final model for both OOS periods.  The inference cap itself is not
    # fitted and remains the value selected on 2024.
    final_imputation = _imputation_values(candidate_matrix[final_mask])
    final_matrix = _impute(candidate_matrix, final_imputation)
    final_scaler = StandardScaler().fit(final_matrix[final_mask])
    final_standardized = final_scaler.transform(final_matrix)
    final_races = final_train.race_count
    final_internal_l2 = selected_lambda * final_races
    final_weights = fit_conditional_logit(
        final_standardized[final_mask],
        complete.labels[final_mask],
        [key for key, keep in zip(complete.race_keys, final_mask) if keep],
        l2=final_internal_l2,
        offset=complete.market_offsets[final_mask],
    )

    availability = {}
    periods_for_availability = {
        "train": TRAIN_PERIOD,
        "tune": TUNE_PERIOD,
        **EVALUATION_PERIODS,
    }
    for feature_index, name in enumerate(CORRECTION_FEATURES):
        availability[name] = {}
        for period_name, period in periods_for_availability.items():
            mask = _date_mask(complete.race_keys, period)
            availability[name][period_name] = float(
                np.mean(candidate_available[mask, feature_index]))

    coefficients = []
    for index, name in enumerate(CORRECTION_FEATURES):
        standardized_weight = float(final_weights[index])
        raw_weight = standardized_weight / float(final_scaler.scale_[index])
        coefficients.append({
            "feature": name,
            "standardized_weight": standardized_weight,
            "raw_weight": raw_weight,
            "sign": ("positive" if standardized_weight > 0 else
                     "negative" if standardized_weight < 0 else "zero"),
            "final_mean_after_imputation": float(final_scaler.mean_[index]),
            "final_scale": float(final_scaler.scale_[index]),
            "final_imputation": float(final_imputation[index]),
            "availability": availability[name],
        })

    period_results = {}
    for period_name, dataset in evaluations.items():
        mask = _date_mask(complete.race_keys, EVALUATION_PERIODS[period_name])
        uncapped = final_standardized[mask] @ final_weights
        correction = clip_correction(uncapped, selected_cap)
        scores = dataset.market_offsets + correction
        m4b_only = evaluate_same_population(dataset, {"m4b": scores})
        m3_period = m3_report["periods"][period_name]
        _assert_same_m3_sample(m3_period, m4b_only)
        period_results[period_name] = {
            "races": m4b_only["races"],
            "horses": m4b_only["horses"],
            "models": {
                "m0": m3_period["models"]["m0"],
                "current_cl": m3_period["models"]["current_cl"],
                "m3": m3_period["models"]["m3"],
                "m4b": m4b_only["models"]["m4b"],
            },
            "market_changes": {
                **m3_period["market_changes"],
                "m4b": m4b_only["market_changes"]["m4b"],
            },
            "m3_floor": m3_period["m3_floor"],
            "m4b_floor": _floor_check(
                m4b_only["models"]["m0"], m4b_only["models"]["m4b"]),
            "swapped_pair_outcomes": _swapped_pair_outcomes(dataset, scores),
            "correction": {
                "cap": selected_cap,
                "uncapped_min": float(np.min(uncapped)),
                "uncapped_max": float(np.max(uncapped)),
                "saturation_count": int(np.count_nonzero(
                    np.abs(uncapped) >= selected_cap)),
                "saturation_rate": float(np.mean(
                    np.abs(uncapped) >= selected_cap)),
            },
            "sample_signature": m4b_only["sample_signature"],
        }

    del (weights_by_lambda, selection_standardized, final_standardized,
         selection_matrix, final_matrix)
    gc.collect()
    return {
        "stats_source": "ability yearly as-of",
        "population_filter": (
            "race 9+, official field 8+, complete feature rows, settled odds"
        ),
        "common_skipped": complete.skipped,
        "splits": expected_splits,
        "candidate_features": {
            "calculated": list(ALL_CANDIDATE_FEATURES),
            "selected": list(CORRECTION_FEATURES),
            "excluded": dict(EXCLUSION_REASONS),
            "availability": availability,
        },
        "m2": m2_report,
        "selection": {
            "objective": "2024 race LogLoss",
            "fit": "uncapped offset conditional logit",
            "inference": "clip(w*z, -cap, +cap)",
            "l2_conversion": "internal_l2 = lambda * train_races",
            "train_races": train_races,
            "diagnostics": diagnostics,
            "selected_lambda": selected_lambda,
            "selected_cap": selected_cap,
            "selected_internal_l2": float(selected["internal_l2"]),
            "selected_residual_norm": float(np.linalg.norm(selection_weights)),
            "final_fit_period": (TRAIN_PERIOD[0], TUNE_PERIOD[1]),
            "final_fit_rows": int(final_mask.sum()),
            "final_fit_races": final_races,
            "final_internal_l2": final_internal_l2,
            "final_residual_norm": float(np.linalg.norm(final_weights)),
        },
        "coefficients": coefficients,
        "m3_reused": {
            "evaluator": "backtest_market_diagnostics.evaluate_m3_dataset",
            "selected_lambda": m3_report["regularization"]["selected_lambda"],
            "sample_signature_asserted": True,
        },
        "periods": period_results,
    }


def run_m4b_diagnostic(runs, cfg, *, db_path=DB_PATH, dataset_builder=None,
                       **evaluation_kwargs):
    """Build one T33 ability dataset and release source runs before fitting."""
    builder = dataset_builder or build_consistent_feature_dataset
    dataset = builder(
        runs,
        cfg,
        EVALUATION_PERIODS["2026H1"][1],
        stats_source="ability",
        db_path=db_path,
    )
    prior_context = build_prior_context(runs, dataset[3])
    # Drop this local reference before scipy/sklearn create repeated matrices.
    del runs
    gc.collect()
    return evaluate_m4b_dataset(
        *dataset, prior_context=prior_context, **evaluation_kwargs)


def _metric_cells(metrics):
    coverage = list(metrics["coverage"][:4])
    coverage.extend([float("nan")] * (4 - len(coverage)))
    topk = "  ".join(
        " n/a" if math.isnan(value) else f"{100.0 * value:4.1f}"
        for value in coverage)
    return f"{topk} | {metrics['logloss']:.6f} | {metrics['brier']:.8f}"


def print_report(report, *, file=None):
    stream = file or sys.stdout
    selection = report["selection"]
    print("\n===== M4-B selective market correction (evaluation only) =====",
          file=stream)
    print(f"stats source: {report['stats_source']}", file=stream)
    print(f"population: {report['population_filter']}", file=stream)
    print("calculated candidates: " + ", ".join(
        report["candidate_features"]["calculated"]), file=stream)
    for name, reason in report["candidate_features"]["excluded"].items():
        print(f"excluded {name}: {reason}", file=stream)
    print(
        f"selected on 2024 LL: lambda={selection['selected_lambda']:g}, "
        f"cap={selection['selected_cap']:g}, "
        f"selection races={selection['train_races']}, "
        f"internal L2={selection['selected_internal_l2']:.6g}, "
        f"||w||={selection['selected_residual_norm']:.6f}",
        file=stream,
    )
    print(
        "final refit: 2021-2024, "
        f"{selection['final_fit_races']} races, "
        f"internal L2={selection['final_internal_l2']:.6g}, "
        f"||w||={selection['final_residual_norm']:.6f}",
        file=stream,
    )
    print("lambda | cap | tune LL | ||w|| | sat | pair rate | any swap",
          file=stream)
    for row in selection["diagnostics"]:
        marker = " *" if (
            row["lambda"] == selection["selected_lambda"]
            and row["cap"] == selection["selected_cap"]) else ""
        print(
            f"{row['lambda']:6g} | {row['cap']:.1f} | "
            f"{row['tune_logloss']:.6f} | {row['residual_norm']:.6f} | "
            f"{100.0 * row['tune_saturation_rate']:.1f}% | "
            f"{100.0 * row['inversion_pair_rate']:.2f}% | "
            f"{100.0 * row['any_swap_rate']:.1f}%{marker}", file=stream)

    print("\nM4-B coefficients (positive=increase vs market):", file=stream)
    print("feature                | standardized | raw       | available "
          "train/24/25/26H1", file=stream)
    for row in report["coefficients"]:
        availability = row["availability"]
        print(
            f"{row['feature']:22s} | {row['standardized_weight']:+.6f} | "
            f"{row['raw_weight']:+.6f} | "
            f"{100*availability['train']:5.1f}/"
            f"{100*availability['tune']:5.1f}/"
            f"{100*availability['2025']:5.1f}/"
            f"{100*availability['2026H1']:5.1f}%", file=stream)

    for period_name, period in report["periods"].items():
        print(f"\n{period_name}: n={period['races']} races/{period['horses']} horses",
              file=stream)
        print("model      | k=1   k=2   k=3   k=4 | LogLoss | Brier", file=stream)
        for name in ("m0", "current_cl", "m3", "m4b"):
            print(f"{name:10s} | {_metric_cells(period['models'][name])}",
                  file=stream)
        swaps = period["swapped_pair_outcomes"]
        for model_name in ("current_cl", "m3", "m4b"):
            changes = period["market_changes"][model_name]
            print(
                f"{model_name} vs market: "
                f"mean inversion pairs={changes['mean_inversion_pairs']:.3f}, "
                f"pair rate={100.0 * changes['inversion_pair_rate']:.2f}%, "
                f"any swap={100.0 * changes['any_swap_rate']:.1f}%",
                file=stream,
            )
        print(
            "swapped pairs: "
            f"improved={swaps['improved']}, worsened={swaps['worsened']}, "
            f"neutral={swaps['neutral']}, total={swaps['total']}", file=stream)
        for label, floor in (("M3", period["m3_floor"]),
                             ("M4-B", period["m4b_floor"])):
            floor_label = "PASS" if floor["passed"] else (
                "FAIL k=" + ",".join(map(str, floor["failed_k"])))
            print(f"{label} realized top-k floor: {floor_label}", file=stream)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="M4-B selective market correction diagnostic (read-only)")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isfile(args.db):
        raise FileNotFoundError(f"ability database not found: {args.db}")
    with closing(sqlite3.connect(args.db)) as connection:
        runs = load_runs(
            connection, TRAIN_PERIOD[0], EVALUATION_PERIODS["2026H1"][1])
    print(f"loaded: {len(runs)} rows (2021-2026H1)")
    dataset = build_consistent_feature_dataset(
        runs,
        load_win5_cfg(),
        EVALUATION_PERIODS["2026H1"][1],
        stats_source="ability",
        db_path=args.db,
    )
    prior_context = build_prior_context(runs, dataset[3])
    # main owns the only source-history reference.  Release it before T11 M3,
    # five M2 folds, and the M4-B lambda grid allocate fitting matrices.
    del runs
    gc.collect()
    report = evaluate_m4b_dataset(*dataset, prior_context=prior_context)
    print_report(report)
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
