"""Compare legacy all-period course factors with leak-free fold factors.

This is an evaluation-only harness.  It never updates the production model or
factor CSV files.  The default run prints both modes on the same race sample;
``--fold-stats`` runs only the leak-free mode for a faster focused check.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing, contextmanager

import numpy as np


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
import analysis  # noqa: E402
from backtest_ability import load_runs  # noqa: E402
from backtest_ml import (FEATURES, build_dataset, coverage_from_scores,
                         fit_conditional_logit, fit_temperature)  # noqa: E402
from backtest_win5 import load_upset_map, load_win5_cfg  # noqa: E402
from fold_stats import FoldFactorTableProvider, fold_as_of_for  # noqa: E402


DB_PATH = os.path.join(BASE_DIR, "ability.db")
STATS_FROM = "20210101"


def _date8(value: str, label: str) -> str:
    value = str(value or "")
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"{label} must be YYYYMMDD: {value!r}")
    return value


@contextmanager
def _factor_loader(provider=None):
    """Inject factors and neutralize outcome-derived automatic rule weights."""
    original = scoring.load_factor_table
    original_criteria_weights = analysis._criteria_weights_cache
    # grade_pts depends only on ◎/〇/△, not rule weights.  Setting this to an
    # empty cache makes that independence explicit and prevents future changes
    # from silently importing criteria_weights (2024-2025) into this harness.
    analysis._criteria_weights_cache = {}
    if provider is not None:
        scoring.load_factor_table = (
            lambda place, track, distance, _base_dir=None:
            provider(place, track, distance)
        )
    try:
        yield
    finally:
        scoring.load_factor_table = original
        analysis._criteria_weights_cache = original_criteria_weights


def conditional_log_loss(scores, labels, race_keys, temperature=1.0,
                         min_race_no=9, min_horses=8) -> float:
    """Winner-event LogLoss on the same race population as top-k coverage."""
    sizes = Counter(race_keys)
    eligible = {
        key for key, size in sizes.items()
        if key[2] is not None and key[2] >= min_race_no and size >= min_horses
    }
    keep = np.array([key in eligible for key in race_keys], dtype=bool)
    scores = np.asarray(scores)[keep]
    labels = np.asarray(labels)[keep]
    race_keys = [key for key, selected in zip(race_keys, keep) if selected]
    if not race_keys:
        return float("nan")
    race_ids = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, key in enumerate(race_keys):
        rid[i] = race_ids.setdefault(key, len(race_ids))
    order = np.argsort(rid, kind="stable")
    scores = np.asarray(scores, dtype=float)[order] / float(temperature or 1.0)
    labels = np.asarray(labels, dtype=float)[order]
    rid = rid[order]
    starts = np.flatnonzero(np.r_[True, rid[1:] != rid[:-1]])
    segments = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rid)]))
    winners = np.add.reduceat(labels, starts)
    events = float(winners.sum())
    if not events:
        return float("nan")
    score_max = np.maximum.reduceat(scores, starts)
    log_sum_exp = score_max + np.log(np.add.reduceat(
        np.exp(scores - score_max[segments]), starts))
    log_likelihood = float(
        scores[labels == 1].sum() - (winners * log_sum_exp).sum())
    return -log_likelihood / events


def build_feature_dataset(runs, cfg, eval_to, provider=None):
    """Build legacy features once or leak-free expanding yearly folds.

    Outcome-rate features are target encodings.  For fold mode every training
    year therefore uses the previous year-end snapshot; using the outer
    2024-12-31 table on the 2021-2024 training rows would let each row's own
    result influence its jockey/frame/etc. feature.
    """
    if provider is None:
        with _factor_loader():
            return build_dataset(runs, STATS_FROM, cfg)

    feature_parts, label_parts, all_keys, all_meta = [], [], [], []
    for year in range(int(STATS_FROM[:4]), int(eval_to[:4]) + 1):
        year_from = f"{year:04d}0101"
        year_to = min(f"{year:04d}1231", eval_to)
        if year_to < year_from:
            continue
        snapshot_as_of = min(provider.as_of, f"{year - 1:04d}1231")
        snapshot = provider.for_as_of(snapshot_as_of)
        with _factor_loader(snapshot):
            features, labels, race_keys, meta = build_dataset(
                runs, year_from, cfg)
        keep = np.array(
            [year_from <= key[0] <= year_to for key in race_keys], dtype=bool)
        if not keep.any():
            continue
        feature_parts.append(features[keep])
        label_parts.append(labels[keep])
        all_keys.extend(key for key, selected in zip(race_keys, keep) if selected)
        all_meta.extend(row for row, selected in zip(meta, keep) if selected)
    if not feature_parts:
        raise RuntimeError("fold特徴量を構築できませんでした")
    return (np.concatenate(feature_parts), np.concatenate(label_parts),
            all_keys, all_meta)


def evaluate_mode(runs, cfg, upset_map, eval_from, eval_to, provider=None):
    """Fit on all years before eval_from and evaluate one fixed time fold."""
    features, labels, race_keys, meta = build_feature_dataset(
        runs, cfg, eval_to, provider)
    dates = np.array([key[0] for key in race_keys])
    train_to = fold_as_of_for(eval_from)
    train = dates <= train_to
    test = (dates >= eval_from) & (dates <= eval_to)
    if not train.any() or not test.any():
        raise RuntimeError("学習または評価サンプルがありません。期間とability.dbを確認してください")

    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(features[train])
    train_z = scaler.transform(features[train])
    test_z = scaler.transform(features[test])
    train_keys = [key for key, keep in zip(race_keys, train) if keep]
    test_keys = [key for key, keep in zip(race_keys, test) if keep]
    test_meta = [row for row, keep in zip(meta, test) if keep]
    weights = fit_conditional_logit(train_z, labels[train], train_keys)
    test_scores = test_z @ weights

    # Last complete training year is calibration-only; earlier years fit the
    # temperature model.  The evaluation year is never consulted.
    calibration_from = train_to[:4] + "0101"
    calibration_train = dates <= train_to
    inner = dates[calibration_train] < calibration_from
    calibration = ~inner
    if inner.any() and calibration.any():
        nested_features = features[calibration_train]
        nested_labels = labels[calibration_train]
        nested_keys = [key for key, keep in zip(
            race_keys, calibration_train) if keep]
        nested_scaler = StandardScaler().fit(nested_features[inner])
        nested_z = nested_scaler.transform(nested_features)
        inner_keys = [key for key, keep in zip(nested_keys, inner) if keep]
        calibration_keys = [key for key, keep in zip(nested_keys, calibration) if keep]
        inner_weights = fit_conditional_logit(
            nested_z[inner], nested_labels[inner], inner_keys)
        temperature = fit_temperature(
            nested_z[calibration] @ inner_weights,
            nested_labels[calibration], calibration_keys)
    else:
        temperature = 1.0

    coverage, counts = coverage_from_scores(
        test_scores, test_keys, test_meta, upset_map)
    return {
        "features": len(FEATURES),
        "train_samples": int(train.sum()),
        "test_samples": int(test.sum()),
        "races": counts.get("default_all", 0),
        "coverage": coverage.get("default", []),
        "temperature": temperature,
        "logloss": conditional_log_loss(
            test_scores, labels[test], test_keys, temperature),
        "_sample_signature": tuple(sorted(
            (key, str(row.get("umaban") or ""), str(row.get("horse") or ""))
            for key, row in zip(test_keys, test_meta))),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="eval_from", default="20250101",
                        help="評価開始日 YYYYMMDD (既定: 20250101)")
    parser.add_argument("--to", dest="eval_to", default="20251231",
                        help="評価終了日 YYYYMMDD (既定: 20251231)")
    parser.add_argument("--fold-stats", action="store_true",
                        help="fold統計だけを実行 (省略時は現行とfoldを比較)")
    parser.add_argument("--stats-as-of", default=None, metavar="YYYYMMDD",
                        help="fold統計の締切日 (既定: 評価年の前年末)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    eval_from = _date8(args.eval_from, "--from")
    eval_to = _date8(args.eval_to, "--to")
    if eval_to < eval_from:
        raise ValueError("--to must be on or after --from")
    expected_as_of = fold_as_of_for(eval_from)
    stats_as_of = _date8(args.stats_as_of or expected_as_of, "--stats-as-of")
    if stats_as_of > expected_as_of:
        raise ValueError(
            f"未来情報防止のため --stats-as-of は {expected_as_of} 以下にしてください")

    cfg = load_win5_cfg()
    upset_map = load_upset_map()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        runs = load_runs(conn, STATS_FROM, eval_to)
    print(f"ロード: {len(runs)}行 / 評価 {eval_from}-{eval_to}")

    provider = FoldFactorTableProvider(
        DB_PATH,
        stats_as_of,
        pedigree_cache_path=os.path.join(BASE_DIR, "pedigree_cache.json"),
        legacy_api_dir=API_DIR,
    )
    print(f"fold統計: {provider.stats_from}-{stats_as_of} / "
          f"{provider.course_count}コース (締切以後の行はSQLで除外)")

    results = []
    if not args.fold_stats:
        results.append(("現行CSV (2021-2025)", evaluate_mode(
            runs, cfg, upset_map, eval_from, eval_to)))
    results.append((f"fold (<= {stats_as_of})", evaluate_mode(
        runs, cfg, upset_map, eval_from, eval_to, provider)))
    if (len(results) == 2
            and results[0][1]["_sample_signature"] != results[1][1]["_sample_signature"]):
        raise RuntimeError("現行/foldで評価出走母集団が一致しません")

    print("\n===== 現行 vs foldコース統計 (同一レース・conditional logit) =====")
    print("mode                     | k=1   k=2   k=3   k=4   | LogLoss  | n")
    for label, result in results:
        coverage = list(result["coverage"][:4])
        coverage.extend([float("nan")] * (4 - len(coverage)))
        cells = "  ".join(
            "  n/a" if math.isnan(value) else f"{100.0 * value:4.1f}"
            for value in coverage)
        print(f"{label:24s} | {cells} | {result['logloss']:.6f} | "
              f"{result['races']}")
    print("採否・差の解釈は行わず、同一母集団の数値のみを報告します。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
