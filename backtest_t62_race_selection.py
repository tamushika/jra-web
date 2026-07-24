"""SPEC-T62 historical race-level relative-confidence gate.

The harness is isolated from production.  It creates strictly chronological
base-CL predictions, trains one fixed 11-column ridge score, selects only the
three preregistered L2 candidates on 2024, freezes the chosen score, and uses
2025/2026H1 for refutation only.
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
from statistics import median
from typing import Iterable

import numpy as np

from backtest_ability import load_runs
from backtest_fold_stats import _factor_loader
from backtest_ml import build_dataset, fit_conditional_logit, fit_temperature
from backtest_stats_retrain import API_DIR, PEDIGREE_CACHE_PATH
from backtest_win5 import load_win5_cfg, parse_final_odds
from eval.blocks import paired_block_bootstrap
from eval.ledger import load_ledger, verify_ledger
from fold_stats import FoldFactorTableProvider, fold_as_of_for

import scoring


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "t62_result.json"
EXPERIMENT_ID = "T62-race-relative-confidence-v1"
ABILITY_DB_SHA256 = "bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a"
META_FEATURES = (
    "js_divergence",
    "top1_disagree",
    "rank_corr",
    "model_entropy",
    "market_entropy",
    "market_top1_margin",
    "hist_coverage",
    "odds_takeout",
    "field_size",
    "is_maiden",
    "class_level",
)
RIDGE_L2 = (0.3, 1.0, 3.0)
BASE_L2 = 1.0
PRIMARY_RATE = 0.20
DIAGNOSTIC_RATES = (0.10, 0.15, 0.20, 0.25, 0.30)
BOOTSTRAP_RESAMPLES = 2000
CANDIDATE_SEEDS = (6200, 6201, 6202)
PRIMARY_METRIC = (
    "2024_selected_race_level_model_minus_market_win_logloss_"
    "one_sided_JST_day_bootstrap"
)
SAFETY_METRICS = (
    "selected_and_unselected_race_level_logloss_2024_2025_2026H1",
    "market_topk_floor_k1_k2_k3_k4_selected_and_unselected",
    "winner_popularity_bands_1_3_4_8_9plus_selected_and_unselected",
    "selection_rate_curve_10_15_20_25_30_diagnostic_only",
    "paired_JST_day_bootstrap_2000_one_sided_Bonferroni_3",
)
STOP_RULE = (
    "Evaluate exactly ridge L2 {0.3,1.0,3.0} at the 2024 primary top-20% "
    "selection; choose minimum selected model-minus-market LogLoss with "
    "stronger-L2 exact tie-break; freeze ridge coefficients, scaler, and all "
    "2024 thresholds; report 2025/2026H1 without refit or re-quantiling; "
    "10/15/20/25/30 curves are diagnostic only; gate/adoption decision is "
    "reserved for the upper model."
)
TRAIN_FROM, DATA_TO = "20210101", "20260630"
PERIODS = {
    "2024_selection": ("20240101", "20241231"),
    "2025_refutation": ("20250101", "20251231"),
    "2026H1_refutation": ("20260101", "20260630"),
}
EPS = 1e-15
CLASS_LEVELS = {
    "新馬": 0.0,
    "未勝利": 0.0,
    "1勝クラス": 1.0,
    "2勝クラス": 2.0,
    "3勝クラス": 3.0,
    "オープン": 4.0,
    "重賞": 4.0,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def array_sha256(values: np.ndarray) -> str:
    array = np.ascontiguousarray(np.asarray(values))
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(array.tobytes())
    return digest.hexdigest()


def observation_sha256(observations: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in observations:
        digest.update(json.dumps(
            row["key"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        digest.update(np.asarray(row["features"], dtype="<f8").tobytes())
        digest.update(np.asarray([
            row["y"], row["model_loss"], row["market_loss"]
        ], dtype="<f8").tobytes())
    return digest.hexdigest()


def base_prediction_sha256(observations: list[dict]) -> str:
    """Hash only pre-race inputs/predictions, excluding winners and losses."""
    digest = hashlib.sha256()
    for row in observations:
        digest.update(json.dumps(
            row["key"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        digest.update(np.asarray(row["features"], dtype="<f8").tobytes())
        digest.update(np.asarray(
            row["model_probabilities"], dtype="<f8"
        ).tobytes())
        digest.update(np.asarray(
            row["market_probabilities"], dtype="<f8"
        ).tobytes())
    return digest.hexdigest()


def _race_key(row: dict) -> tuple[str, str, int]:
    return (str(row.get("date")), str(row.get("place")), int(row.get("r") or 0))


def _runner_sort_key(row: dict) -> tuple[int, str]:
    try:
        number = int(row.get("umaban"))
    except (TypeError, ValueError):
        number = 999
    return number, str(row.get("horse") or "")


def _mask(dates: np.ndarray, date_from: str, date_to: str) -> np.ndarray:
    return (dates >= date_from) & (dates <= date_to)


def _selected(values, mask: np.ndarray):
    if isinstance(values, np.ndarray):
        return values[mask]
    return [value for value, keep in zip(values, mask) if keep]


def _softmax(scores: Iterable[float], temperature: float) -> np.ndarray:
    values = np.asarray(list(scores), dtype=float) / float(temperature or 1.0)
    values -= values.max()
    exponentials = np.exp(values)
    return exponentials / exponentials.sum()


def rolling_origin_contract() -> list[dict]:
    """Frozen chronological base-model schedule.

    Temperature uses only information available before the target period.
    The first fold has no earlier calibration year and therefore fixes tau=1;
    every later fold calibrates a nested model on the immediately previous
    OOS year, matching ``evaluate_feature_dataset``.
    """
    return [
        {
            "target": "2022",
            "target_from": "20220101",
            "target_to": "20221231",
            "train_from": TRAIN_FROM,
            "train_to": "20211231",
            "temperature_source": "fixed_1_no_prior_calibration_year",
        },
        {
            "target": "2023",
            "target_from": "20230101",
            "target_to": "20231231",
            "train_from": TRAIN_FROM,
            "train_to": "20221231",
            "temperature_source": "2022_oos_scores",
        },
        {
            "target": "2024",
            "target_from": "20240101",
            "target_to": "20241231",
            "train_from": TRAIN_FROM,
            "train_to": "20231231",
            "temperature_source": "2023_oos_scores",
        },
        {
            "target": "2025_2026H1",
            "target_from": "20250101",
            "target_to": "20260630",
            "train_from": TRAIN_FROM,
            "train_to": "20241231",
            "temperature_source": "2024_oos_scores",
        },
    ]


def registration_contract() -> dict:
    """Immutable T39 contract checked before any historical evaluation."""
    return {
        "experiment_id": EXPERIMENT_ID,
        "features": list(META_FEATURES),
        "missing_numeric_policy": "zero_without_extra_flags",
        "base_l2": BASE_L2,
        "rolling_origin": rolling_origin_contract(),
        "standard_time_policy": (
            "yearly_asof_previous_year_end_2016_history_winner_median_min3_"
            "and_track_variants_rebuilt_from_that_snapshot"
        ),
        "meta_train_period": ["20220101", "20231231"],
        "ridge": {
            "l2": list(RIDGE_L2),
            "solver": "cholesky",
            "fit_intercept": True,
            "candidate_tie_break": (
                "minimum_2024_selected_difference_then_stronger_l2"
            ),
        },
        "selection": {
            "primary_rate": PRIMARY_RATE,
            "diagnostic_rates": list(DIAGNOSTIC_RATES),
            "threshold_rule": "score>=midpoint_between_boundary_scores",
            "future_policy": "freeze_2024_coefficients_scaler_and_thresholds",
        },
        "bootstrap": {
            "unit": "JST_race_date",
            "resamples": BOOTSTRAP_RESAMPLES,
            "alternative": "model_logloss_minus_market_logloss<0",
            "one_sided_rule": "(count(diff>=0)+1)/(resamples+1)",
            "multiplicity": "Bonferroni_3_candidates",
            "candidate_seeds": list(CANDIDATE_SEEDS),
        },
        "population": {
            "races": "9R_or_later_flat_only",
            "minimum_field_size": 8,
            "complete_official_field_required": True,
            "winner_and_all_final_odds_required": True,
        },
        "refutation_periods": {
            key: list(value) for key, value in list(PERIODS.items())[1:]
        },
    }


def registration_features() -> list[str]:
    """T39 features field, including the two SPEC-mandated limitations."""
    return [
        *META_FEATURES,
        (
            "LIMITATION: historical market probabilities use final odds; "
            "prospective cutoff-snapshot distribution may differ"
        ),
        (
            "LIMITATION: 2025/2026H1 have been reused in prior historical "
            "adjudications and support prospective progression only"
        ),
    ]


def registration_search_grid() -> dict:
    return {
        "ridge_l2": list(RIDGE_L2),
        "primary_selection_rate": PRIMARY_RATE,
        "diagnostic_selection_rates": list(DIAGNOSTIC_RATES),
        "ridge_solver": "cholesky",
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "candidate_seeds": list(CANDIDATE_SEEDS),
    }


def validate_rolling_origin_contract(contract: list[dict]) -> None:
    previous_target_to = None
    for index, row in enumerate(contract):
        if row["train_to"] >= row["target_from"]:
            raise ValueError(f"{row['target']}: training crosses target boundary")
        if index and row["train_to"] != previous_target_to:
            raise ValueError(f"{row['target']}: rolling training does not expand atomically")
        previous_target_to = row["target_to"]


def _fit_base(
    features: np.ndarray,
    labels: np.ndarray,
    keys: list,
    dates: np.ndarray,
    train_from: str,
    train_to: str,
):
    from sklearn.preprocessing import StandardScaler

    train = _mask(dates, train_from, train_to)
    if not train.any():
        raise RuntimeError(f"empty base training period: {train_from}-{train_to}")
    scaler = StandardScaler().fit(features[train])
    weights = fit_conditional_logit(
        scaler.transform(features[train]), labels[train],
        _selected(keys, train), l2=BASE_L2,
    )
    return {
        "scaler": scaler,
        "weights": weights,
        "train_from": train_from,
        "train_to": train_to,
        "train_rows": int(train.sum()),
    }


def _score_base(model: dict, features: np.ndarray, period: np.ndarray) -> np.ndarray:
    return model["scaler"].transform(features[period]) @ model["weights"]


def build_standard_times_asof(
    rows: list[dict], cutoff: str, *, date_from: str = "20160101"
) -> dict:
    """Rebuild the production standard-time shape from past winners only."""
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for row in rows:
        date = str(row.get("date") or "")
        if not date_from <= date <= cutoff:
            continue
        try:
            is_winner = int(row.get("rank") or 0) == 1
            time_sec = float(row.get("time_sec") or 0.0)
            distance = int(row.get("distance") or 0)
        except (TypeError, ValueError):
            continue
        track = str(row.get("track_type") or "")
        condition = str(row.get("condition") or "")
        if (
            not is_winner or not math.isfinite(time_sec) or time_sec <= 0.0
            or distance <= 0
            or track not in ("芝", "ダート") or not condition
        ):
            continue
        buckets[(
            str(row.get("place") or ""),
            track,
            distance,
            str(row.get("race_class") or ""),
            condition,
        )].append(time_sec)
    standards = {}
    for (place, track, distance, race_class, condition), values in buckets.items():
        if len(values) < 3:
            continue
        standards.setdefault(place, {}).setdefault(track, {}).setdefault(
            str(distance), {}
        ).setdefault(race_class, {})[condition] = round(float(median(values)), 2)
    return standards


def build_track_variants_asof(
    rows: list[dict],
    standards: dict,
    date_to: str,
    *,
    date_from: str = "20160101",
) -> dict:
    """Rebuild daily variants using only the supplied as-of standard table."""
    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        date = str(row.get("date") or "")
        if not date_from <= date <= date_to:
            continue
        try:
            is_winner = int(row.get("rank") or 0) == 1
            time_sec = float(row.get("time_sec") or 0.0)
            distance = int(row.get("distance") or 0)
        except (TypeError, ValueError):
            continue
        place = str(row.get("place") or "")
        track = str(row.get("track_type") or "")
        race_class = str(row.get("race_class") or "")
        condition = str(row.get("condition") or "")
        if (
            not is_winner or not math.isfinite(time_sec) or time_sec <= 0.0
            or distance <= 0 or track not in ("芝", "ダート") or not condition
        ):
            continue
        standard = (
            standards.get(place, {})
            .get(track, {})
            .get(str(distance), {})
            .get(race_class, {})
            .get(condition)
        )
        if standard is None:
            continue
        buckets[(date, place, track)].append(time_sec - float(standard))
    variants = {}
    for (date, place, track), deviations in buckets.items():
        if len(deviations) < 3:
            continue
        value = max(-5.0, min(5.0, float(median(deviations))))
        variants.setdefault(date, {}).setdefault(place, {})[track] = round(
            value, 2
        )
    return variants


def build_strict_feature_dataset(
    runs: list[dict],
    standard_rows: list[dict],
    cfg: dict,
    db_path: Path,
) -> tuple[np.ndarray, np.ndarray, list, list, dict]:
    """Build yearly features with both factors and standard times as-of.

    The ordinary T33 builder already freezes outcome-rate factor tables at the
    previous year-end.  T62 also rebuilds ``venue_standard_times`` through the
    same cutoff.  Daily track variants are rebuilt from that as-of standard
    snapshot too; the production variant asset was derived from an all-period
    standard table and is therefore not valid inside early rolling folds.
    """
    provider = FoldFactorTableProvider(
        str(db_path),
        fold_as_of_for(DATA_TO),
        pedigree_cache_path=PEDIGREE_CACHE_PATH,
        legacy_api_dir=API_DIR,
    )
    feature_parts, label_parts, all_keys, all_meta = [], [], [], []
    standard_audit = {}
    original_standard_data = scoring._STD_TIMES_CACHE["data"]
    original_variant_data = scoring._STD_TIMES_CACHE["variants"]
    try:
        for year in range(2021, 2027):
            year_from = f"{year:04d}0101"
            year_to = min(f"{year:04d}1231", DATA_TO)
            cutoff = f"{year - 1:04d}1231"
            standards = build_standard_times_asof(standard_rows, cutoff)
            variants = build_track_variants_asof(
                standard_rows, standards, year_to
            )
            scoring._STD_TIMES_CACHE["data"] = standards
            scoring._STD_TIMES_CACHE["variants"] = variants
            snapshot = provider.for_as_of(min(provider.as_of, cutoff))
            with _factor_loader(snapshot):
                features, labels, keys, meta = build_dataset(
                    runs, year_from, cfg
                )
            keep = np.asarray(
                [year_from <= key[0] <= year_to for key in keys], dtype=bool
            )
            if not keep.any():
                continue
            feature_parts.append(features[keep])
            label_parts.append(labels[keep])
            all_keys.extend(key for key, selected in zip(keys, keep) if selected)
            all_meta.extend(row for row, selected in zip(meta, keep) if selected)
            standard_audit[str(year)] = {
                "standard_as_of": cutoff,
                "standard_cells": sum(
                    len(conditions)
                    for tracks in standards.values()
                    for distances in tracks.values()
                    for classes in distances.values()
                    for conditions in classes.values()
                ),
                "standard_sha256": canonical_sha256(standards),
                "variant_data_through": year_to,
                "variant_cells": sum(
                    len(tracks)
                    for places in variants.values()
                    for tracks in places.values()
                ),
                "variant_sha256": canonical_sha256(variants),
            }
    finally:
        scoring._STD_TIMES_CACHE["data"] = original_standard_data
        scoring._STD_TIMES_CACHE["variants"] = original_variant_data
    return (
        np.concatenate(feature_parts),
        np.concatenate(label_parts),
        all_keys,
        all_meta,
        standard_audit,
    )


def build_run_history(runs: list[dict]) -> dict[str, tuple[list[str], list[bool]]]:
    by_horse: dict[str, list[tuple[str, tuple, bool]]] = defaultdict(list)
    for row in runs:
        horse = str(row.get("horse") or "")
        if not horse:
            continue
        try:
            valid = int(row.get("rank") or 0) > 0
        except (TypeError, ValueError):
            valid = False
        by_horse[horse].append((str(row.get("date")), _race_key(row), valid))
    output = {}
    for horse, rows in by_horse.items():
        rows.sort(key=lambda item: (item[0], item[1]))
        output[horse] = (
            [item[0] for item in rows],
            [item[2] for item in rows],
        )
    return output


def history_available(
    history: dict[str, tuple[list[str], list[bool]]], horse: str, target_date: str
) -> float:
    dates, valid = history.get(str(horse or ""), ([], []))
    cutoff = bisect.bisect_left(dates, str(target_date))
    valid_prior = [flag for flag in valid[:cutoff] if flag]
    return float(len(valid_prior[-4:]) >= 2)


def _rank_values(probabilities: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(-np.asarray(probabilities, dtype=float), method="average")


def compute_race_features(
    model_probabilities: Iterable[float],
    market_probabilities: Iterable[float],
    decoded_odds: Iterable[float],
    history_flags: Iterable[float],
    *,
    race_class: str | None,
) -> np.ndarray:
    """Compute the fixed 11 pre-race columns without outcomes.

    Historical final odds are decoded before this function because the legacy
    ``runs.win_pay`` representation needs finish rank only to distinguish a
    winner payout from a stored decimal odd.  Neither rank nor winner identity
    is accepted by this feature function.
    """
    model = np.asarray(list(model_probabilities), dtype=float)
    market = np.asarray(list(market_probabilities), dtype=float)
    odds = np.asarray(list(decoded_odds), dtype=float)
    flags = np.asarray(list(history_flags), dtype=float)
    if not len(model) or not (len(model) == len(market) == len(odds) == len(flags)):
        raise ValueError("race feature inputs must be non-empty and aligned")
    if (
        not np.all(np.isfinite(model)) or not np.all(np.isfinite(market))
        or not np.all(np.isfinite(odds)) or not np.all(np.isfinite(flags))
        or np.any(model <= 0.0) or np.any(market <= 0.0)
        or np.any(odds <= 1.0)
        or not np.isclose(model.sum(), 1.0)
        or not np.isclose(market.sum(), 1.0)
    ):
        raise ValueError("race feature inputs must be finite, valid, and normalized")
    midpoint = 0.5 * (model + market)
    js = 0.5 * float(np.sum(model * np.log(model / midpoint)))
    js += 0.5 * float(np.sum(market * np.log(market / midpoint)))
    model_ranks, market_ranks = _rank_values(model), _rank_values(market)
    if np.std(model_ranks) == 0.0 or np.std(market_ranks) == 0.0:
        rank_corr = 0.0
    else:
        rank_corr = float(np.corrcoef(model_ranks, market_ranks)[0, 1])
    normalizer = math.log(len(model)) if len(model) > 1 else 1.0
    model_entropy = -float(np.sum(model * np.log(model))) / normalizer
    market_entropy = -float(np.sum(market * np.log(market))) / normalizer
    market_sorted = np.sort(market)[::-1]
    margin = float(market_sorted[0] - market_sorted[1]) if len(market) > 1 else 0.0
    race_class_text = str(race_class or "")
    return np.asarray([
        js,
        float(int(np.argmax(model)) != int(np.argmax(market))),
        rank_corr,
        model_entropy,
        market_entropy,
        margin,
        float(np.mean(flags)) if len(flags) else 0.0,
        float(np.sum(1.0 / odds) - 1.0),
        float(len(model)),
        float(race_class_text in ("新馬", "未勝利")),
        CLASS_LEVELS.get(race_class_text, 0.0),
    ], dtype=float)


def winner_log_ratio(
    model_probabilities: Iterable[float],
    market_probabilities: Iterable[float],
    winner_indices: Iterable[int],
) -> float:
    model = np.asarray(list(model_probabilities), dtype=float)
    market = np.asarray(list(market_probabilities), dtype=float)
    winners = list(winner_indices)
    if not winners:
        raise ValueError("at least one winner is required")
    return float(np.mean([
        math.log(max(EPS, float(model[index])))
        - math.log(max(EPS, float(market[index])))
        for index in winners
    ]))


def build_race_observations(
    scores: np.ndarray,
    labels: np.ndarray,
    keys: list,
    meta: list[dict],
    temperature: float,
    history: dict,
) -> list[dict]:
    grouped: dict[tuple, list[tuple[float, int, dict]]] = defaultdict(list)
    for score, label, key, row in zip(scores, labels, keys, meta):
        grouped[key].append((float(score), int(label), row))
    observations = []
    for key in sorted(grouped):
        members = sorted(
            grouped[key],
            key=lambda item: _runner_sort_key(item[2]),
        )
        if key[2] < 9 or len(members) < 8:
            continue
        field_size = max(
            (int(row.get("total_horses") or 0) for _, _, row in members),
            default=0,
        )
        if not field_size or len(members) != field_size:
            continue
        first = members[0][2]
        if str(first.get("track_type") or "") not in ("芝", "ダート"):
            continue
        winner_indices = [
            index for index, (_score, label, _row) in enumerate(members)
            if label == 1
        ]
        if not winner_indices:
            continue
        odds = [
            parse_final_odds(row.get("win_pay"), row.get("rank"))
            for _, _, row in members
        ]
        if any(
            value is None
            or not math.isfinite(float(value))
            or float(value) <= 1.0
            for value in odds
        ):
            continue
        odds_array = np.asarray(odds, dtype=float)
        market = 1.0 / odds_array
        market /= market.sum()
        model = _softmax([score for score, _, _ in members], temperature)
        flags = [
            history_available(
                history, str(row.get("horse") or ""), str(row.get("date"))
            )
            for _, _, row in members
        ]
        features = compute_race_features(
            model, market, odds_array, flags, race_class=first.get("race_class")
        )
        model_losses = [
            -math.log(max(EPS, float(model[index]))) for index in winner_indices
        ]
        market_losses = [
            -math.log(max(EPS, float(market[index]))) for index in winner_indices
        ]
        try:
            winner_popularity = min(
                int(members[index][2].get("popularity")) for index in winner_indices
            )
        except (TypeError, ValueError):
            winner_popularity = 999

        def market_order_key(index: int) -> tuple[int, float, int]:
            try:
                popularity = int(members[index][2].get("popularity"))
            except (TypeError, ValueError):
                popularity = 999
            return (
                popularity,
                float(odds_array[index]),
                _runner_sort_key(members[index][2])[0],
            )

        observations.append({
            "key": key,
            "features": features,
            "model_probabilities": tuple(float(value) for value in model),
            "market_probabilities": tuple(float(value) for value in market),
            "y": winner_log_ratio(model, market, winner_indices),
            "model_loss": float(np.mean(model_losses)),
            "market_loss": float(np.mean(market_losses)),
            "model_order": tuple(sorted(
                range(len(members)),
                key=lambda index: (
                    -float(model[index]),
                    _runner_sort_key(members[index][2])[0],
                ),
            )),
            "market_order": tuple(sorted(
                range(len(members)), key=market_order_key
            )),
            "winners": tuple(winner_indices),
            "winner_popularity": winner_popularity,
        })
    return observations


def generate_observations(db_path: Path) -> tuple[dict[str, list[dict]], dict]:
    contract = rolling_origin_contract()
    validate_rolling_origin_contract(contract)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, TRAIN_FROM, DATA_TO)
        cursor = connection.execute(
            """
            SELECT date, place, rank, track_type, distance, race_class,
                   condition, time_sec
              FROM runs
             WHERE date BETWEEN '20160101' AND ?
            """,
            (DATA_TO,),
        )
        columns = [item[0] for item in cursor.description]
        standard_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cfg = load_win5_cfg()
    features, labels, keys, meta, standard_audit = build_strict_feature_dataset(
        runs, standard_rows, cfg, db_path
    )
    dates = np.asarray([key[0] for key in keys])
    history = build_run_history(runs)
    observations: dict[str, list[dict]] = {}
    fold_audit = []
    previous_oos = None
    for index, fold in enumerate(contract):
        model = _fit_base(
            features, labels, keys, dates, fold["train_from"], fold["train_to"]
        )
        target = _mask(dates, fold["target_from"], fold["target_to"])
        if not target.any():
            raise RuntimeError(f"{fold['target']}: empty target period")
        if index == 0:
            temperature = 1.0
        else:
            calibration_scores = previous_oos["scores"]
            calibration_labels = previous_oos["labels"]
            calibration_keys = previous_oos["keys"]
            temperature = fit_temperature(
                calibration_scores, calibration_labels, calibration_keys
            )
        target_scores = _score_base(model, features, target)
        target_labels = labels[target]
        target_keys = _selected(keys, target)
        target_meta = _selected(meta, target)
        observations[fold["target"]] = build_race_observations(
            target_scores, target_labels, target_keys, target_meta,
            temperature, history,
        )
        base_model_payload = {
            "train_from": fold["train_from"],
            "train_to": fold["train_to"],
            "base_l2": BASE_L2,
            "temperature": temperature,
            "scaler_mean": [float(value) for value in model["scaler"].mean_],
            "scaler_scale": [float(value) for value in model["scaler"].scale_],
            "weights": [float(value) for value in model["weights"]],
        }
        fold_audit.append({
            **fold,
            "base_l2": BASE_L2,
            "train_rows": model["train_rows"],
            "temperature": temperature,
            "target_runner_rows": int(target.sum()),
            "eligible_races": len(observations[fold["target"]]),
            "base_model": base_model_payload,
            "base_model_sha256": canonical_sha256(base_model_payload),
            "base_prediction_sha256": base_prediction_sha256(
                observations[fold["target"]]
            ),
            "race_observations_sha256": observation_sha256(
                observations[fold["target"]]
            ),
        })
        previous_oos = {
            "scores": target_scores,
            "labels": target_labels,
            "keys": target_keys,
        }
    audit = {
        "contract": fold_audit,
        "dataset_rows": len(features),
        "feature_columns": int(features.shape[1]),
        "feature_matrix_sha256": array_sha256(features),
        "labels_sha256": array_sha256(labels),
        "runner_sample_sha256": canonical_sha256({
            "rows": [
                [list(key), str(row.get("umaban") or ""), str(row.get("horse") or "")]
                for key, row in zip(keys, meta)
            ]
        }),
        "standard_times_yearly_asof": standard_audit,
    }
    base_contract = {
        "feature_matrix_sha256": audit["feature_matrix_sha256"],
        "runner_sample_sha256": audit["runner_sample_sha256"],
        "standard_times_yearly_asof": standard_audit,
        "folds": [
            {
                "target": row["target"],
                "base_model_sha256": row["base_model_sha256"],
                "base_prediction_sha256": row["base_prediction_sha256"],
            }
            for row in fold_audit
        ],
    }
    audit["base_prediction_contract"] = base_contract
    audit["base_prediction_contract_sha256"] = canonical_sha256(base_contract)
    return observations, audit


def require_registration(ledger_path: Path, db_path: Path) -> dict:
    actual = sha256_file(db_path)
    if actual != ABILITY_DB_SHA256:
        raise RuntimeError(
            f"ability.db SHA mismatch: expected {ABILITY_DB_SHA256}, got {actual}"
        )
    verify_ledger(ledger_path, update_state=False)
    matching = [
        row for row in load_ledger(ledger_path)
        if row.get("experiment_id") == EXPERIMENT_ID
        and row.get("result_summary") is None
    ]
    if not matching:
        raise RuntimeError(
            f"T62 evaluation blocked: register {EXPERIMENT_ID} in T39 ledger first"
        )
    if len(matching) != 1:
        raise RuntimeError("T39 ledger contains multiple T62 preregistrations")
    registration = matching[0]
    if registration.get("data_hashes", {}).get("ability_db_sha256") != actual:
        raise RuntimeError("T39 registration does not seal the current ability.db")
    expected_contract_sha = canonical_sha256(registration_contract())
    if (
        registration.get("data_hashes", {}).get("t62_contract_sha256")
        != expected_contract_sha
    ):
        raise RuntimeError("T39 registration does not seal the full T62 contract")
    harness_sha = sha256_file(Path(__file__).resolve())
    if registration.get("data_hashes", {}).get("harness_sha256") != harness_sha:
        raise RuntimeError("T39 registration does not seal the current T62 harness")
    if (
        registration.get("features") != registration_features()
        or registration.get("primary_metric") != PRIMARY_METRIC
        or registration.get("safety_metrics") != list(SAFETY_METRICS)
        or registration.get("search_grid") != registration_search_grid()
        or registration.get("candidate_count") != 3
        or registration.get("stop_rule") != STOP_RULE
        or registration.get("benchmark_type") != "historical"
        or registration.get("prospective_start_date") is not None
        or registration.get("adjudication") is not None
    ):
        raise RuntimeError("T39 registration does not seal the fixed T62 candidates")
    return registration


def selection_threshold(scores: np.ndarray, rate: float) -> float:
    if not 0.0 < rate < 1.0 or not len(scores):
        raise ValueError("selection rate must be in (0,1) for non-empty scores")
    ordered = np.sort(np.asarray(scores, dtype=float))[::-1]
    count = max(1, int(math.ceil(len(ordered) * rate)))
    if count == len(ordered):
        return float(np.nextafter(ordered[-1], -np.inf))
    if ordered[count - 1] == ordered[count]:
        raise RuntimeError("selection boundary tie cannot be frozen as one threshold")
    return float((ordered[count - 1] + ordered[count]) / 2.0)


def _bootstrap(rows: list[dict], seed: int) -> dict | None:
    if not rows:
        return None
    model_blocks: dict[str, list[float]] = defaultdict(list)
    market_blocks: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        model_blocks[row["key"][0]].append(row["model_loss"])
        market_blocks[row["key"][0]].append(row["market_loss"])
    result = paired_block_bootstrap(
        lambda values: sum(values) / len(values),
        model_blocks, market_blocks, BOOTSTRAP_RESAMPLES, seed,
    )
    one_sided = (
        sum(value >= 0.0 for value in result.differences) + 1
    ) / (result.n_resamples + 1)
    return {
        "difference": result.observed_difference,
        "ci95": [result.ci_low, result.ci_high],
        "p_one_sided_model_better": one_sided,
        "p_two_sided": result.p_value,
        "day_blocks": result.n_blocks,
        "resamples": result.n_resamples,
        "seed": result.seed,
    }


def _topk(rows: list[dict]) -> dict:
    if not rows:
        return {"model": [], "market": [], "gap": [], "pass_all_k": False}
    model_hits = np.zeros(4, dtype=float)
    market_hits = np.zeros(4, dtype=float)
    for row in rows:
        winners = set(row["winners"])
        for index in range(4):
            model_hits[index] += bool(winners.intersection(row["model_order"][:index + 1]))
            market_hits[index] += bool(winners.intersection(row["market_order"][:index + 1]))
    model = (model_hits / len(rows)).tolist()
    market = (market_hits / len(rows)).tolist()
    gap = [left - right for left, right in zip(model, market)]
    return {
        "model": model,
        "market": market,
        "gap": gap,
        "pass_all_k": all(value >= 0.0 for value in gap),
    }


def _popularity(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        popularity = row["winner_popularity"]
        band = "1-3" if popularity <= 3 else "4-8" if popularity <= 8 else "9+"
        groups[band].append(row)
    bands = {}
    for band in ("1-3", "4-8", "9+"):
        values = groups[band]
        bands[band] = {
            "races": len(values),
            "model_logloss": (
                float(np.mean([row["model_loss"] for row in values]))
                if values else None
            ),
            "market_logloss": (
                float(np.mean([row["market_loss"] for row in values]))
                if values else None
            ),
            "mean_model_minus_market": (
                float(np.mean([
                    row["model_loss"] - row["market_loss"] for row in values
                ]))
                if values else None
            ),
        }
    weighted = sum(
        row["races"] * row["mean_model_minus_market"]
        for row in bands.values() if row["races"]
    ) / len(rows)
    overall = float(np.mean([
        row["model_loss"] - row["market_loss"] for row in rows
    ]))
    if not math.isclose(weighted, overall, rel_tol=0.0, abs_tol=1e-12):
        raise AssertionError("popularity bands do not reconstruct the full subset")
    return {
        "bands": bands,
        "weighted_mean_model_minus_market": weighted,
    }


def subset_report(rows: list[dict], seed: int) -> dict:
    if not rows:
        return {
            "races": 0, "model_logloss": None, "market_logloss": None,
            "difference": None, "bootstrap": None, "market_topk_floor": _topk([]),
            "winner_popularity_bands": {
                "bands": {}, "weighted_mean_model_minus_market": None,
            },
        }
    return {
        "races": len(rows),
        "model_logloss": float(np.mean([row["model_loss"] for row in rows])),
        "market_logloss": float(np.mean([row["market_loss"] for row in rows])),
        "difference": float(np.mean([
            row["model_loss"] - row["market_loss"] for row in rows
        ])),
        "bootstrap": _bootstrap(rows, seed),
        "market_topk_floor": _topk(rows),
        "winner_popularity_bands": _popularity(rows),
    }


def score_frozen(manifest: dict, observations: list[dict]) -> np.ndarray:
    if not observations:
        return np.asarray([], dtype=float)
    matrix = np.asarray([row["features"] for row in observations], dtype=float)
    mean = np.asarray(manifest["scaler_mean"], dtype=float)
    scale = np.asarray(manifest["scaler_scale"], dtype=float)
    coefficients = np.asarray(manifest["coefficients"], dtype=float)
    return ((matrix - mean) / scale) @ coefficients + float(manifest["intercept"])


def freeze_manifest(
    model, scaler, threshold: float, l2: float,
    *, base_prediction_contract_sha256: str | None = None,
    diagnostic_thresholds: dict[str, float] | None = None,
) -> dict:
    payload = {
        "format_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "features": list(META_FEATURES),
        "ridge_l2": float(l2),
        "coefficients": [float(value) for value in model.coef_],
        "intercept": float(model.intercept_),
        "scaler_mean": [float(value) for value in scaler.mean_],
        "scaler_scale": [float(value) for value in scaler.scale_],
        "selection_threshold": float(threshold),
        "threshold_rule": "score>=selection_threshold",
        "selection_rate_2024": PRIMARY_RATE,
        "diagnostic_thresholds_2024": diagnostic_thresholds or {},
        "base_l2": BASE_L2,
        "base_train_for_refutation": "20210101-20241231",
        "base_prediction_contract_sha256": base_prediction_contract_sha256,
    }
    return {**payload, "manifest_sha256": canonical_sha256(payload)}


def _period_report(
    observations: list[dict],
    scores: np.ndarray,
    threshold: float,
    seed: int,
    *,
    diagnostic_thresholds: dict[str, float] | None = None,
) -> dict:
    if len(observations) != len(scores):
        raise ValueError("observations and scores must be aligned")
    selected_mask = scores >= threshold
    selected = [row for row, keep in zip(observations, selected_mask) if keep]
    unselected = [row for row, keep in zip(observations, selected_mask) if not keep]
    if len(selected) + len(unselected) != len(observations):
        raise AssertionError("selected and unselected are not an exhaustive partition")
    curves = {}
    for index, rate in enumerate(DIAGNOSTIC_RATES):
        rate_key = str(rate)
        diagnostic_threshold = (
            diagnostic_thresholds[rate_key]
            if diagnostic_thresholds is not None
            else selection_threshold(scores, rate)
        )
        rows = [
            row for row, score in zip(observations, scores)
            if score >= diagnostic_threshold
        ]
        curves[str(rate)] = {
            "diagnostic_only": True,
            "threshold": diagnostic_threshold,
            "threshold_source": (
                "2024_selection_frozen"
                if diagnostic_thresholds is not None
                else "current_2024_candidate"
            ),
            "races": len(rows),
            "selected_rate": len(rows) / len(observations) if observations else 0.0,
            "difference": (
                float(np.mean([
                    row["model_loss"] - row["market_loss"] for row in rows
                ]))
                if rows else None
            ),
        }
    return {
        "total_races": len(observations),
        "selected_rate": len(selected) / len(observations) if observations else 0.0,
        "threshold": threshold,
        "selected": subset_report(selected, seed),
        "unselected": subset_report(unselected, seed + 20),
        "selection_rate_curve": curves,
    }


def choose_candidate(candidates: list[dict]) -> dict:
    """Choose on the preregistered primary metric; exact ties prefer more L2."""
    return min(candidates, key=lambda row: (
        row["selection_2024"]["selected"]["difference"],
        -row["ridge_l2"],
    ))


def _selected_difference_is_negative(report: dict) -> bool:
    difference = report.get("selected", {}).get("difference")
    return difference is not None and difference < 0.0


def run_evaluation(db_path: Path, ledger_path: Path) -> dict:
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    registration = require_registration(ledger_path, db_path)
    observations, rolling_audit = generate_observations(db_path)
    meta_train = observations["2022"] + observations["2023"]
    selection = observations["2024"]
    if not meta_train or not selection:
        raise RuntimeError("empty ridge training or 2024 selection population")
    train_x = np.asarray([row["features"] for row in meta_train], dtype=float)
    train_y = np.asarray([row["y"] for row in meta_train], dtype=float)
    selection_x = np.asarray([row["features"] for row in selection], dtype=float)
    scaler = StandardScaler().fit(train_x)
    train_z, selection_z = scaler.transform(train_x), scaler.transform(selection_x)
    candidates = []
    fitted = {}
    for index, l2 in enumerate(RIDGE_L2):
        model = Ridge(alpha=l2, fit_intercept=True, solver="cholesky")
        model.fit(train_z, train_y)
        scores = model.predict(selection_z)
        threshold = selection_threshold(scores, PRIMARY_RATE)
        report = _period_report(
            selection, scores, threshold, CANDIDATE_SEEDS[index]
        )
        corrected = min(
            1.0,
            report["selected"]["bootstrap"]["p_one_sided_model_better"]
            * len(RIDGE_L2),
        )
        candidates.append({
            "ridge_l2": l2,
            "threshold": threshold,
            "selection_2024": report,
            "bonferroni_corrected_one_sided_p": corrected,
        })
        fitted[l2] = (model, scores, threshold)
    selected_candidate = choose_candidate(candidates)
    selected_l2 = selected_candidate["ridge_l2"]
    selected_model, selected_scores, threshold = fitted[selected_l2]
    diagnostic_thresholds = {
        str(rate): selection_threshold(selected_scores, rate)
        for rate in DIAGNOSTIC_RATES
    }
    base_contract_sha = rolling_audit["base_prediction_contract_sha256"]
    manifest = freeze_manifest(
        selected_model, scaler, threshold, selected_l2,
        base_prediction_contract_sha256=base_contract_sha,
        diagnostic_thresholds=diagnostic_thresholds,
    )
    if canonical_sha256({
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }) != manifest["manifest_sha256"]:
        raise AssertionError("freeze manifest SHA is not reproducible")
    if not np.allclose(score_frozen(manifest, selection), selected_scores):
        raise AssertionError("frozen ridge does not reproduce 2024 scores")

    period_reports = {"2024_selection": selected_candidate["selection_2024"]}
    refutation = observations["2025_2026H1"]
    for index, (name, (date_from, date_to)) in enumerate(list(PERIODS.items())[1:]):
        rows = [
            row for row in refutation if date_from <= row["key"][0] <= date_to
        ]
        scores = score_frozen(manifest, rows)
        period_reports[name] = _period_report(
            rows, scores, threshold, 6231 + index,
            diagnostic_thresholds=diagnostic_thresholds,
        )
    return {
        "spec": "T62",
        "experiment_id": EXPERIMENT_ID,
        "ability_db_sha256": sha256_file(db_path),
        "registration": registration,
        "preimplementation_audit": {
            "all_11_columns_pre_race_computable": True,
            "missing_numeric_policy": "zero_without_extra_flags",
            "market_odds_policy": "exclude race when any final odd cannot be decoded",
            "legacy_odds_decoder_note": (
                "runs.win_pay needs rank only to decode winner payout; decoded odds "
                "are passed to a result-blind feature function"
            ),
            "history_policy": (
                "four starts strictly before target date; flag=1 when at least "
                "two have positive rank"
            ),
            "base_l2_fixed": BASE_L2,
            "temperature_policy": (
                "2022 tau=1 because no prior calibration year; later refits "
                "calibrate a nested model on the immediately previous OOS year"
            ),
            "standard_time_policy": (
                "winner-time medians rebuilt per feature year from 2016 "
                "through the previous year-end; daily track variants are "
                "rebuilt from that snapshot through each feature-year end; "
                "minimum three races per cell"
            ),
        },
        "features": list(META_FEATURES),
        "rolling_origin": rolling_audit,
        "ridge_training": {
            "period": "2022-2023 OOS predictions only",
            "races": len(meta_train),
            "rows": len(meta_train),
            "target_mean": float(train_y.mean()),
            "target_std": float(train_y.std()),
        },
        "candidates": candidates,
        "selected": {
            "ridge_l2": selected_l2,
            "selection_rule": "minimum 2024 selected-set model-minus-market logloss",
            "candidate_tie_break": "stronger ridge_l2",
            "bonferroni_corrected_one_sided_p": (
                selected_candidate["bonferroni_corrected_one_sided_p"]
            ),
        },
        "freeze_manifest": manifest,
        "periods": period_reports,
        "historical_gate_inputs": {
            "selection_2024_corrected_p_lt_0_05": (
                selected_candidate["bonferroni_corrected_one_sided_p"] < 0.05
            ),
            "2025_selected_difference_negative": (
                _selected_difference_is_negative(
                    period_reports["2025_refutation"]
                )
            ),
            "2026H1_selected_difference_negative": (
                _selected_difference_is_negative(
                    period_reports["2026H1_refutation"]
                )
            ),
            "decision_reserved_for_upper_model": True,
        },
    }


def write_json(payload: dict, path: Path) -> str:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(write_json(run_evaluation(args.db, args.ledger), args.output))


if __name__ == "__main__":
    main()
