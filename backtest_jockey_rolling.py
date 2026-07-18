"""T18 jockey rolling-form diagnostic (evaluation only).

Frozen grid: baseline L2{0.3,1,3} plus JROLL k{10,30,100} x
L2{0.3,1,3}.  The production 21-feature contract and artifacts are untouched.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
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
from backtest_ml import (FEATURES, JROLL_FEATURES, build_jockey_rolling_features,
                         fit_conditional_logit, fit_temperature)  # noqa: E402
from backtest_stats_retrain import DB_PATH, build_consistent_feature_dataset  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402

EXPERIMENT_ID = "T18-jockey-rolling-v1"
DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"),
           "2026H1": ("20260101", "20260630")}
L2_GRID = (0.3, 1.0, 3.0)
K_GRID = (10.0, 30.0, 100.0)
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 18


def _mask(dates, date_from, date_to):
    return (dates >= date_from) & (dates <= date_to)


def _selected(values, mask):
    return [value for value, keep in zip(values, mask) if keep]


def rolling_matrix_for_meta(runs, meta, k):
    by_run = build_jockey_rolling_features(runs, k)
    return np.asarray([[by_run[id(row)][name] for name in JROLL_FEATURES]
                       for row in meta], dtype=float)


def _fit_candidate(X, labels, keys, meta, dates, *, name, l2, k=None):
    from sklearn.preprocessing import StandardScaler
    train = _mask(dates, TRAIN_FROM, TRAIN_TO)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(
        scaler.transform(X[train]), labels[train], _selected(keys, train), l2=l2)
    scores = scaler.transform(X[selection]) @ weights
    selection_keys = _selected(keys, selection)
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, _selected(meta, selection),
        temperature=temperature)
    return {"name": name, "l2": l2, "k": k, "scaler": scaler,
            "weights": weights, "temperature": temperature,
            "selection": metrics}


def run_grid(base, rolling_by_k, labels, keys, meta, dates):
    candidates = []
    for l2 in L2_GRID:
        candidates.append(_fit_candidate(
            base, labels, keys, meta, dates, name="baseline", l2=l2))
    for k in K_GRID:
        X = np.hstack([base, rolling_by_k[k]])
        for l2 in L2_GRID:
            candidates.append(_fit_candidate(
                X, labels, keys, meta, dates, name="+jockey_rolling", l2=l2, k=k))
    return candidates, min(
        candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])


def _final_model(X, labels, keys, dates, candidate):
    from sklearn.preprocessing import StandardScaler
    train = _mask(dates, TRAIN_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(
        scaler.transform(X[train]), labels[train], _selected(keys, train),
        l2=candidate["l2"])
    return {"scaler": scaler, "weights": weights,
            "temperature": candidate["temperature"]}


def _score(model, X, labels, keys, meta, dates, period):
    mask = _mask(dates, *period)
    scores = model["scaler"].transform(X[mask]) @ model["weights"]
    period_keys, period_meta = _selected(keys, mask), _selected(meta, mask)
    metrics = same_population_metrics(
        scores, labels[mask], period_keys, period_meta,
        temperature=model["temperature"])
    losses = race_level_winner_logloss(
        scores, labels[mask], period_keys, period_meta,
        temperature=model["temperature"])
    return metrics, losses, scores, mask


def _runner_probabilities(scores, keys, temperature):
    grouped = defaultdict(list)
    for index, (score, key) in enumerate(zip(scores, keys)):
        grouped[key].append((index, float(score)))
    probabilities = np.zeros(len(scores), dtype=float)
    for members in grouped.values():
        values = np.asarray([score for _index, score in members]) / temperature
        values -= values.max()
        probs = np.exp(values); probs /= probs.sum()
        for (index, _score), probability in zip(members, probs):
            probabilities[index] = probability
    return probabilities


def riding_bucket_report(model, X, rolling, labels, keys, dates, period):
    mask = _mask(dates, *period)
    Xp, rollp, yp = X[mask], rolling[mask], labels[mask]
    keys_p = _selected(keys, mask)
    z = model["scaler"].transform(Xp)
    scores = z @ model["weights"]
    probabilities = _runner_probabilities(scores, keys_p, model["temperature"])
    contribution = z[:, -len(JROLL_FEATURES):] @ model["weights"][-len(JROLL_FEATURES):]
    n30 = np.rint(np.expm1(rollp[:, JROLL_FEATURES.index("j_roll_n30")])).astype(int)
    buckets = {"n30<5": n30 < 5, "n30=5-20": (n30 >= 5) & (n30 <= 20),
               "n30=20+": n30 >= 21}
    return {name: {"rows": int(m.sum()),
                   "mean_coefficient_contribution": float(contribution[m].mean()) if m.any() else None,
                   "mean_predicted_win_probability": float(probabilities[m].mean()) if m.any() else None,
                   "observed_win_rate": float(yp[m].mean()) if m.any() else None}
            for name, m in buckets.items()}


def coverage_report(rolling_by_k, dates):
    periods = {"train_2021_2023": (TRAIN_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    out = {}
    for k, matrix in rolling_by_k.items():
        out[str(int(k))] = {}
        for period_name, period in periods.items():
            mask = _mask(dates, *period)
            out[str(int(k))][period_name] = {
                name: {"mean": float(matrix[mask, i].mean()),
                       "std": float(matrix[mask, i].std()),
                       "nonzero_rate": float(np.mean(matrix[mask, i] != 0))}
                for i, name in enumerate(JROLL_FEATURES)}
    return out


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--json-out")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    global L2_GRID, K_GRID
    args = parse_args(argv)
    digest, hash_ok = verify_ability_db_hash(args.db)
    print(f"ability.db sha256 = {digest}")
    if not hash_ok:
        print(f"[STOP] expected={ABILITY_DB_SHA256_EXPECTED}")
        return 1
    with closing(sqlite3.connect(args.db)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=args.db)
    dates = np.asarray([key[0] for key in keys])
    k_values = (30.0,) if args.smoke else K_GRID
    rolling_by_k = {k: rolling_matrix_for_meta(runs, meta, k) for k in k_values}
    if args.smoke:
        L2_GRID, K_GRID = (1.0,), (30.0,)
    candidates, best = run_grid(base, rolling_by_k, labels, keys, meta, dates)
    selected_X = (base if best["name"] == "baseline" else
                  np.hstack([base, rolling_by_k[best["k"]]]))
    selected_final = _final_model(selected_X, labels, keys, dates, best)
    selected_reference = {"scaler": best["scaler"], "weights": best["weights"],
                          "temperature": best["temperature"]}
    baseline_best = min((row for row in candidates if row["name"] == "baseline"),
                        key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline_final = _final_model(base, labels, keys, dates, baseline_best)
    historical, bootstrap = {}, {}
    for name, period in PERIODS.items():
        sm, sl, _scores, _mask_value = _score(
            selected_final, selected_X, labels, keys, meta, dates, period)
        rm, _rl, _rscores, _rmask = _score(
            selected_reference, selected_X, labels, keys, meta, dates, period)
        bm, bl, _bscores, _bmask = _score(
            baseline_final, base, labels, keys, meta, dates, period)
        historical[name] = {
            "selected": {"races": sm["races"], "horses": sm["horses"],
                         "models": sm["models"], "skipped": sm["skipped"]},
            "trained_2021_2023_reference": {
                "races": rm["races"], "horses": rm["horses"],
                "models": rm["models"], "skipped": rm["skipped"]},
            "baseline": {"races": bm["races"], "horses": bm["horses"],
                         "models": bm["models"], "skipped": bm["skipped"]},
        }
        sb, bb = _blocks_by_date(sl), _blocks_by_date(bl)
        boot = paired_block_bootstrap(_mean_metric, sb, bb,
                                      n_resamples=BOOTSTRAP_RESAMPLES,
                                      seed=BOOTSTRAP_SEED)
        bootstrap[name] = {"observed_diff": boot.observed_difference,
                           "ci_low": boot.ci_low, "ci_high": boot.ci_high,
                           "p_value": boot.p_value, "day_blocks": len(sb)}
    buckets = {}
    if best["name"] != "baseline":
        for name, period in {"selection_2024": (SELECTION_FROM, SELECTION_TO),
                             **PERIODS}.items():
            buckets[name] = riding_bucket_report(
                selected_final if name != "selection_2024" else {
                    "scaler": best["scaler"], "weights": best["weights"],
                    "temperature": best["temperature"]},
                selected_X, rolling_by_k[best["k"]], labels, keys, dates, period)
    coverage = coverage_report(rolling_by_k, dates)
    payload = {
        "experiment_id": EXPERIMENT_ID, "ability_db_sha256": digest,
        "window_boundary": "same day excluded; exactly 30/90 days before included",
        "trainer_column_available": False,
        "jockey_trainer_pair_coverage": "unavailable: no trainer entity column",
        "candidates": [{"config": row["name"], "k": row["k"], "l2": row["l2"],
                         "selection_2024": row["selection"]["models"]}
                       for row in candidates],
        "selected": {"config": best["name"], "k": best["k"], "l2": best["l2"]},
        "historical_benchmark": historical, "market_topk_floor": {
            name: [m >= b for m, b in zip(row["selected"]["models"]["model"]["coverage"],
                                          row["selected"]["models"]["market"]["coverage"])]
            for name, row in historical.items()},
        "win5_day_paired_bootstrap": bootstrap,
        "riding_count_buckets": buckets, "coverage": coverage,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=float))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, default=float)
    del runs, base, selected_X
    gc.collect()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
