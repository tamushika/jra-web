"""T43 isolated Plackett-Luce top-k training-objective diagnostic.

This module does not modify or call the production conditional-logit fitter.
The feature contract remains the current 21 ``backtest_ml.FEATURES`` columns.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from backtest_ability import load_runs  # noqa: E402
from backtest_feature_pack import (  # noqa: E402
    ABILITY_DB_SHA256_EXPECTED, _blocks_by_date, _mean_metric,
    race_level_winner_logloss, verify_ability_db_hash)
from backtest_fold_stats import same_population_metrics  # noqa: E402
from backtest_ml import FEATURES  # noqa: E402
from backtest_stats_retrain import DB_PATH, build_consistent_feature_dataset  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402

EXPERIMENT_ID = "T43-rank-objective-diagnostic-v1"
DATA_FROM, DATA_TO = "20210101", "20260630"
OBJECTIVES = ("pl_top1", "pl_top3", "pl_top5", "pl_full")
L2_GRID = (0.3, 1.0, 3.0)
SELECTION_FOLDS = (("20210101", "20211231", "20220101", "20221231"),
                   ("20210101", "20221231", "20230101", "20231231"),
                   ("20210101", "20231231", "20240101", "20241231"))
PERIODS = {"2025": ("20250101", "20251231"),
           "2026H1": ("20260101", "20260630")}
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 43


def _race_groups(race_keys):
    groups = defaultdict(list)
    for index, key in enumerate(race_keys):
        groups[key].append(index)
    return groups


def _stage_count(objective, field_size):
    if objective not in ("pl_top1", "pl_top3", "pl_top5", "pl_full"):
        raise ValueError(f"unknown objective: {objective}")
    limit = {"pl_top1": 1, "pl_top3": 3, "pl_top5": 5,
             "pl_full": field_size - 1}[objective]
    return min(limit, field_size - 1)


def pl_objective_and_gradient(weights, features, ranks, race_keys,
                              objective="pl_top1", l2=0.0):
    """Race-normalized negative PL log-likelihood and analytic gradient."""
    weights = np.asarray(weights, dtype=float)
    features = np.asarray(features, dtype=float)
    ranks = np.asarray(ranks)
    loss = 0.0
    gradient = np.zeros_like(weights)
    used_races = 0
    for indices in _race_groups(race_keys).values():
        ordered = sorted(indices, key=lambda index: int(ranks[index]))
        stages = _stage_count(objective, len(ordered))
        if stages <= 0:
            continue
        stage_weight = 1.0 / stages
        used_races += 1
        for stage in range(stages):
            remaining = ordered[stage:]
            Xr = features[remaining]
            scores = Xr @ weights
            maximum = float(scores.max())
            exponentials = np.exp(scores - maximum)
            probabilities = exponentials / exponentials.sum()
            loss += stage_weight * (
                maximum + math.log(float(exponentials.sum())) - scores[0])
            gradient += stage_weight * (
                probabilities @ Xr - features[ordered[stage]])
    if not used_races:
        raise ValueError("no rank races")
    loss += 0.5 * float(l2) * float(weights @ weights)
    gradient += float(l2) * weights
    return float(loss), gradient


def stage_weights(objective, field_size):
    stages = _stage_count(objective, field_size)
    return np.full(stages, 1.0 / stages) if stages else np.empty(0)


def fit_rank_objective(features, ranks, race_keys, *, objective, l2,
                       max_iter=300):
    from scipy.optimize import minimize
    result = minimize(
        lambda w: pl_objective_and_gradient(
            w, features, ranks, race_keys, objective=objective, l2=l2),
        np.zeros(features.shape[1]), jac=True, method="L-BFGS-B",
        options={"maxiter": max_iter})
    if not result.success:
        print(f"  [WARN] {objective} convergence: {result.message}")
    return result.x


def classify_raw_rank_races(runs):
    """Classify official races before feature-row loss can hide bad ranks."""
    grouped = defaultdict(list)
    for row in runs:
        if row.get("date") and row.get("place") is not None and row.get("r") is not None:
            grouped[(row["date"], row["place"], row["r"])].append(row)
    valid, reason = set(), {}
    for key, members in grouped.items():
        parsed = []
        invalid = False
        for row in members:
            try:
                rank = int(row.get("rank"))
            except (TypeError, ValueError):
                invalid = True
                break
            if rank <= 0:
                invalid = True
                break
            parsed.append(rank)
        if invalid:
            reason[key] = "nonfinish_disqualified_cancelled"
        elif len(set(parsed)) != len(parsed):
            reason[key] = "tie"
        else:
            valid.add(key)
    return valid, reason


def filter_rank_dataset(features, labels, race_keys, meta, valid_raw_keys):
    groups = _race_groups(race_keys)
    keep_keys, excluded = set(), Counter()
    complete_keys = set()
    for key, indices in groups.items():
        if key not in valid_raw_keys:
            excluded["invalid_official_rank"] += 1
            continue
        ranks = [meta[index].get("rank") for index in indices]
        try:
            parsed = [int(value) for value in ranks]
        except (TypeError, ValueError):
            excluded["invalid_constructed_rank"] += 1
            continue
        if len(set(parsed)) != len(parsed):
            excluded["tie_constructed"] += 1
            continue
        # pl_top1 is the current winner likelihood, not "best retained horse".
        # Use one common population for all four objectives, so a missing
        # feature row for the official winner excludes the race throughout.
        if 1 not in parsed:
            excluded["winner_missing_features"] += 1
            continue
        keep_keys.add(key)
        field_size = max(int(meta[index].get("total_horses") or 0) for index in indices)
        if field_size and len(indices) == field_size:
            complete_keys.add(key)
        else:
            excluded["incomplete_feature_field"] += 1
    mask = np.asarray([key in keep_keys for key in race_keys], dtype=bool)
    return (features[mask], labels[mask], [key for key, ok in zip(race_keys, mask) if ok],
            [row for row, ok in zip(meta, mask) if ok], complete_keys, dict(excluded))


def _mask(dates, date_from, date_to):
    return (dates >= date_from) & (dates <= date_to)


def _select(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def _fit(features, meta, keys, dates, train_period, objective, l2):
    from sklearn.preprocessing import StandardScaler
    train = _mask(dates, *train_period)
    scaler = StandardScaler().fit(features[train])
    ranks = np.asarray([int(row["rank"]) for row, keep in zip(meta, train) if keep])
    weights = fit_rank_objective(
        scaler.transform(features[train]), ranks, _select(keys, train),
        objective=objective, l2=l2)
    return {"scaler": scaler, "weights": weights, "objective": objective, "l2": l2}


def _evaluate(model, features, labels, keys, meta, dates, period,
              *, require_complete_field=False):
    mask = _mask(dates, *period)
    period_features, period_labels = features[mask], labels[mask]
    period_keys, period_meta = _select(keys, mask), _select(meta, mask)
    if require_complete_field:
        groups = _race_groups(period_keys)
        complete = set()
        for key, indices in groups.items():
            field_size = max(int(period_meta[index].get("total_horses") or 0)
                             for index in indices)
            if field_size and len(indices) == field_size:
                complete.add(key)
        complete_mask = np.asarray([key in complete for key in period_keys], dtype=bool)
        period_features, period_labels = (period_features[complete_mask],
                                          period_labels[complete_mask])
        period_keys = _select(period_keys, complete_mask)
        period_meta = _select(period_meta, complete_mask)
    X = model["scaler"].transform(period_features)
    scores = X @ model["weights"]
    metrics = same_population_metrics(
        scores, period_labels, period_keys, period_meta, temperature=1.0,
        require_complete_field=False)
    ranks = np.asarray([int(row["rank"]) for row in period_meta])
    rank_nll, _gradient = pl_objective_and_gradient(
        model["weights"], X, ranks, period_keys, objective="pl_full", l2=0.0)
    correlations = []
    from scipy.stats import spearmanr
    for indices in _race_groups(period_keys).values():
        if len(indices) >= 2:
            rho = spearmanr(scores[indices], -ranks[indices]).statistic
            if math.isfinite(rho):
                correlations.append(float(rho))
    losses = race_level_winner_logloss(
        scores, period_labels, period_keys, period_meta, temperature=1.0,
        min_race_no=9, min_horses=8)
    return metrics, {"all_rank_nll_per_race": rank_nll / max(1, len(_race_groups(period_keys))),
                     "mean_spearman": float(np.mean(correlations)) if correlations else None}, losses


def rolling_origin_grid(features, labels, keys, meta, dates):
    candidates = []
    for objective in OBJECTIVES:
        for l2 in L2_GRID:
            folds, weighted_loss, races = [], 0.0, 0
            for train_from, train_to, eval_from, eval_to in SELECTION_FOLDS:
                model = _fit(features, meta, keys, dates, (train_from, train_to), objective, l2)
                metrics, rank_metrics, _losses = _evaluate(
                    model, features, labels, keys, meta, dates,
                    (eval_from, eval_to), require_complete_field=False)
                n = metrics["races"]
                loss = metrics["models"]["model"]["logloss"]
                weighted_loss += n * loss; races += n
                folds.append({"year": eval_from[:4], "races": n,
                              "winner_metrics": metrics["models"],
                              "rank_metrics": rank_metrics})
            candidates.append({"objective": objective, "l2": l2,
                               "rolling_origin_win_logloss": weighted_loss / races,
                               "races": races, "folds": folds})
    return candidates, min(candidates, key=lambda row: row["rolling_origin_win_logloss"])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--json-out")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _compact(metrics):
    return {"races": metrics["races"], "horses": metrics["horses"],
            "models": metrics["models"], "skipped": metrics["skipped"]}


def main(argv=None):
    global OBJECTIVES, L2_GRID, SELECTION_FOLDS
    args = parse_args(argv)
    digest, hash_ok = verify_ability_db_hash(args.db)
    print(f"ability.db sha256 = {digest}")
    if not hash_ok:
        print(f"[STOP] expected={ABILITY_DB_SHA256_EXPECTED}")
        return 1
    with closing(sqlite3.connect(args.db)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    valid_raw, raw_reasons = classify_raw_rank_races(runs)
    cfg = load_win5_cfg()
    features, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=args.db)
    features, labels, keys, meta, complete_keys, excluded = filter_rank_dataset(
        features, labels, keys, meta, valid_raw)
    dates = np.asarray([key[0] for key in keys])
    if args.smoke:
        OBJECTIVES, L2_GRID = ("pl_top1",), (1.0,)
        SELECTION_FOLDS = SELECTION_FOLDS[-1:]
    candidates, best = rolling_origin_grid(features, labels, keys, meta, dates)
    final_model = _fit(features, meta, keys, dates,
                       ("20210101", "20241231"), best["objective"], best["l2"])
    top1_best = min((row for row in candidates if row["objective"] == "pl_top1"),
                    key=lambda row: row["rolling_origin_win_logloss"])
    baseline_model = _fit(features, meta, keys, dates,
                          ("20210101", "20241231"), "pl_top1", top1_best["l2"])
    historical, bootstrap = {}, {}
    for name, period in PERIODS.items():
        metrics, rank_metrics, losses = _evaluate(
            final_model, features, labels, keys, meta, dates, period)
        sensitivity, sensitivity_rank, _ = _evaluate(
            final_model, features, labels, keys, meta, dates, period,
            require_complete_field=True)
        baseline_metrics, _baseline_rank, baseline_losses = _evaluate(
            baseline_model, features, labels, keys, meta, dates, period)
        sb, bb = _blocks_by_date(losses), _blocks_by_date(baseline_losses)
        boot = paired_block_bootstrap(_mean_metric, sb, bb,
                                      n_resamples=BOOTSTRAP_RESAMPLES,
                                      seed=BOOTSTRAP_SEED)
        historical[name] = {
            "selected": _compact(metrics), "rank_metrics": rank_metrics,
            "complete_field_sensitivity": _compact(sensitivity),
            "complete_field_rank_metrics": sensitivity_rank,
            "market_topk_floor": [a >= b for a, b in zip(
                metrics["models"]["model"]["coverage"],
                metrics["models"]["market"]["coverage"])],
        }
        bootstrap[name] = {"observed_diff_selected_minus_top1": boot.observed_difference,
                           "ci_low": boot.ci_low, "ci_high": boot.ci_high,
                           "p_value": boot.p_value, "day_blocks": len(sb)}
    payload = {
        "experiment_id": EXPERIMENT_ID, "ability_db_sha256": digest,
        "feature_count": len(FEATURES),
        "rank_definition": "relative order among runners with constructed features",
        "raw_rank_exclusions": dict(Counter(raw_reasons.values())),
        "dataset_exclusions": excluded, "complete_field_races": len(complete_keys),
        "candidates": candidates, "selected": best,
        "ln_odds_coefficients": {
            row["objective"] + f"_l2_{row['l2']}": float(_fit(
                features, meta, keys, dates, ("20210101", "20241231"),
                row["objective"], row["l2"])["weights"][FEATURES.index("ln_odds")])
            for row in candidates},
        "coefficient_scale": "weights after StandardScaler on current 21 FEATURES",
        "historical_benchmark": historical,
        "win5_day_paired_bootstrap": bootstrap,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=float))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
