"""T13: race-relative feature diagnostics on the leak-free T33 data path.

This module is evaluation-only.  It builds one 29-column dataset with yearly
ability.db as-of factor tables, tunes from a 2021-2023 fit on 2024, refits
through 2024, and reports the untouched 2025 and 2026H1 periods.  It never
writes a model, factor snapshot, rule file, or any production artifact.
"""
from __future__ import annotations

import argparse
import gc
import importlib
import math
import os
import sqlite3
import sys
from contextlib import closing

import numpy as np


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from backtest_ability import load_runs  # noqa: E402
from backtest_fold_stats import same_population_metrics  # noqa: E402
from backtest_ml import (FEATURES, NO_MARKET_FEATURES, RELATIVE_FEATURES,
                         active_feature_names, fit_conditional_logit,
                         fit_temperature)  # noqa: E402
from backtest_stats_retrain import (DB_PATH,
                                    build_consistent_feature_dataset)  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402


DATA_FROM = "20210101"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
TUNE_FROM, TUNE_TO = "20240101", "20241231"
PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
}
DATA_TO = max(date_to for _date_from, date_to in PERIODS.values())

IDENTIFIABILITY_NOTE = (
    "conditional logitではレース内の全馬に共通する定数はsoftmaxから消えるため、"
    "全馬で値を取得できたレースのtfeat_gap_med / tfeat_gap_bestと各 *_devは、"
    "元の絶対特徴量と同値です。欠損を含むレースでは偏差0への補完により、"
    "取得可否とのinteractionも含みます。L2正則化下では同値部分の係数が複数列へ"
    "分配され、個々の係数は一意に解釈できないので、raw scaleで合算したeffective"
    "係数を併記します。同値部分の重複は実効的なL2罰則も弱めるため、baseとの差を"
    "純粋な『相対情報だけの追加効果』とは解釈できません。"
)

LIVE_COMPATIBILITY_NOTE = (
    "ライブ側の analyze_race_url は採点前に全馬のカード情報を保持し、"
    "scoring._ml_features で tfeat/j_pts/f_pts/sire_pts/kinryo/n_prior を"
    "既に算出できます。このため8相対特徴は同じレース集約で再現可能です。"
    "採用時は補完値から欠損を推測せず、学習側と同じ取得可否maskを保持する必要があります。"
)


def _period_mask(dates, date_from, date_to):
    dates = np.asarray(dates)
    return (dates >= date_from) & (dates <= date_to)


def _keys_for_mask(race_keys, mask):
    return [key for key, keep in zip(race_keys, mask) if keep]


def _meta_for_mask(meta, mask):
    return [row for row, keep in zip(meta, mask) if keep]


def build_relative_feature_dataset(runs, cfg, *, db_path=DB_PATH):
    """Build the 29 columns once through T33's yearly ability-stat folds."""
    return build_consistent_feature_dataset(
        runs,
        cfg,
        DATA_TO,
        stats_source="ability",
        db_path=db_path,
        dataset_kwargs={"relative": True},
    )


def _validate_dataset(features, labels, race_keys, meta):
    features = np.asarray(features, dtype=float)
    labels = np.asarray(labels)
    expected_columns = len(FEATURES) + len(RELATIVE_FEATURES)
    if features.ndim != 2 or features.shape[1] != expected_columns:
        raise ValueError(
            f"relative dataset must have {expected_columns} columns: {features.shape}")
    if not (len(features) == len(labels) == len(race_keys) == len(meta)):
        raise ValueError("features/labels/race_keys/meta row counts differ")
    return features, labels


def _fit_one_model(features, labels, race_keys, dates, feature_names):
    """Tune temperature from 2021-23 -> 2024, then refit through 2024."""
    from sklearn.preprocessing import StandardScaler

    train = _period_mask(dates, TRAIN_FROM, TRAIN_TO)
    tune = _period_mask(dates, TUNE_FROM, TUNE_TO)
    if not train.any() or not tune.any():
        raise RuntimeError("T13 requires both 2021-23 training and 2024 tuning rows")

    inner_scaler = StandardScaler().fit(features[train])
    train_z = inner_scaler.transform(features[train])
    tune_z = inner_scaler.transform(features[tune])
    train_keys = _keys_for_mask(race_keys, train)
    tune_keys = _keys_for_mask(race_keys, tune)
    inner_weights = fit_conditional_logit(
        train_z, labels[train], train_keys, l2=1.0)
    temperature = fit_temperature(
        tune_z @ inner_weights, labels[tune], tune_keys)
    final = train | tune
    scaler = StandardScaler().fit(features[final])
    weights = fit_conditional_logit(
        scaler.transform(features[final]),
        labels[final],
        _keys_for_mask(race_keys, final),
        l2=1.0,
    )
    return {
        "feature_names": list(feature_names),
        "scaler": scaler,
        "weights": np.asarray(weights, dtype=float),
        "temperature": float(temperature),
        "train_rows": int(final.sum()),
        "selection_train_rows": int(train.sum()),
        "tune_rows": int(tune.sum()),
        "fit_population": "2021-2024 final refit after 2024 temperature tuning",
    }


def fit_relative_diagnostic_models(features, labels, race_keys):
    """Fit current-21 and current-21-plus-relative on identical rows."""
    features = np.asarray(features, dtype=float)
    dates = np.asarray([key[0] for key in race_keys])
    base_count = len(FEATURES)
    return {
        "base": _fit_one_model(
            features[:, :base_count], labels, race_keys, dates, FEATURES),
        "relative": _fit_one_model(
            features, labels, race_keys, dates, active_feature_names(relative=True)),
    }


def _score(model, features):
    return model["scaler"].transform(features) @ model["weights"]


def evaluate_relative_period(models, features, labels, race_keys, meta,
                             date_from, date_to):
    """Compare M0/base/relative on one strict, identical race population."""
    dates = np.asarray([key[0] for key in race_keys])
    keep = _period_mask(dates, date_from, date_to)
    if not keep.any():
        raise RuntimeError(f"no evaluation rows for {date_from}-{date_to}")
    keys = _keys_for_mask(race_keys, keep)
    period_meta = _meta_for_mask(meta, keep)
    period_labels = np.asarray(labels)[keep]
    base_features = np.asarray(features)[keep, :len(FEATURES)]
    relative_features = np.asarray(features)[keep]

    base_result = same_population_metrics(
        _score(models["base"], base_features),
        period_labels,
        keys,
        period_meta,
        temperature=models["base"]["temperature"],
    )
    relative_result = same_population_metrics(
        _score(models["relative"], relative_features),
        period_labels,
        keys,
        period_meta,
        temperature=models["relative"]["temperature"],
    )
    if base_result["sample_signature"] != relative_result["sample_signature"]:
        raise RuntimeError("base and relative models used different race populations")
    if base_result["models"]["market"] != relative_result["models"]["market"]:
        raise RuntimeError("M0 market baseline changed between model comparisons")
    if not base_result["races"]:
        raise RuntimeError(f"no complete same-population races for {date_from}-{date_to}")

    return {
        "races": base_result["races"],
        "horses": base_result["horses"],
        "sample_signature": base_result["sample_signature"],
        "skipped": base_result["skipped"],
        "models": {
            "M0 popularity": base_result["models"]["market"],
            "base current21": base_result["models"]["model"],
            "base+relative": relative_result["models"]["model"],
        },
    }


def relative_coefficient_report(relative_model):
    """Return standardized relative coefficients and identifiable raw sums."""
    names = list(relative_model["feature_names"])
    weights = np.asarray(relative_model["weights"], dtype=float)
    scales = np.asarray(relative_model["scaler"].scale_, dtype=float)
    if len(names) != len(weights) or len(weights) != len(scales):
        raise ValueError("relative model coefficient dimensions differ")
    raw = {
        name: float(weight / scale) if scale else 0.0
        for name, weight, scale in zip(names, weights, scales)
    }
    standardized = {
        name: float(weights[names.index(name)]) for name in RELATIVE_FEATURES
    }
    groups = {
        "tfeat": ("tfeat", "tfeat_gap_med", "tfeat_gap_best"),
        "j_pts": ("j_pts", "j_pts_dev"),
        "f_pts": ("f_pts", "f_pts_dev"),
        "sire_pts": ("sire_pts", "sire_pts_dev"),
        "kinryo": ("kinryo", "kinryo_dev"),
        "n_prior": ("n_prior", "n_prior_dev"),
        "tfeat_rank": ("tfeat_rank",),
    }
    effective = {
        source: {
            "components": list(components),
            "raw_coefficient": float(sum(raw[name] for name in components)),
        }
        for source, components in groups.items()
    }
    return {
        "standardized_relative": standardized,
        "raw_by_feature": raw,
        "effective": effective,
        "identifiability_note": IDENTIFIABILITY_NOTE,
    }


def _canonical_sample_signature(signature):
    return tuple(sorted(
        (key, tuple(sorted(tuple(member) for member in members)))
        for key, members in signature
    ))


def evaluate_relative_dataset(dataset):
    """Evaluate an already-built dataset, allowing the large run list to go."""
    features, labels = _validate_dataset(*dataset)
    _features, _labels, race_keys, meta = dataset
    models = fit_relative_diagnostic_models(features, labels, race_keys)
    period_reports = {
        name: evaluate_relative_period(
            models, features, labels, race_keys, meta, date_from, date_to)
        for name, (date_from, date_to) in PERIODS.items()
    }
    report = {
        "stats_source": "ability yearly as-of",
        "models": models,
        "periods": period_reports,
        "coefficients": relative_coefficient_report(models["relative"]),
        "live_compatibility": LIVE_COMPATIBILITY_NOTE,
        "m3_relative": None,
    }
    # T11 is imported lazily to keep module import order acyclic.  The M3
    # residual excludes ln_odds because market probability is its fixed offset.
    market_diagnostics = importlib.import_module("backtest_market_diagnostics")
    report["m3_relative"] = market_diagnostics.evaluate_m3_dataset(
        *dataset,
        residual_feature_names=list(NO_MARKET_FEATURES) + list(RELATIVE_FEATURES),
    )
    for period_name, period_report in period_reports.items():
        try:
            m3_signature = report["m3_relative"]["periods"][period_name][
                "sample_signature"]
        except KeyError as exc:
            raise RuntimeError(
                f"M3+relative report is missing {period_name} sample signature") from exc
        if (_canonical_sample_signature(period_report["sample_signature"])
                != _canonical_sample_signature(m3_signature)):
            raise RuntimeError(
                f"M3+relative used a different race population for {period_name}")
    return report


def run_relative_diagnostic(runs, cfg, *, db_path=DB_PATH):
    dataset = build_relative_feature_dataset(runs, cfg, db_path=db_path)
    del runs
    gc.collect()
    return evaluate_relative_dataset(dataset)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="T13 レース内相対特徴量の固定時系列診断（評価専用）")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _metric_cells(metrics):
    coverage = list(metrics["coverage"][:4])
    coverage.extend([float("nan")] * (4 - len(coverage)))
    topk = "  ".join(
        " n/a" if math.isnan(value) else f"{100.0 * value:4.1f}"
        for value in coverage)
    return f"{topk} | {metrics['logloss']:.6f} | {metrics['brier']:.8f}"


def print_report(report):
    print("\n===== T13 レース内相対特徴量 / ability.db 年次as-of統計 =====")
    print(f"固定分割: 学習 {TRAIN_FROM}-{TRAIN_TO} / 温度調整 {TUNE_FROM}-{TUNE_TO} / "
          f"固定テスト 2025 / 追加確認 2026H1")
    print(f"temperature: base={report['models']['base']['temperature']:.3f}, "
          f"relative={report['models']['relative']['temperature']:.3f}")
    for period_name, period in report["periods"].items():
        print(f"\n--- {period_name}: {period['races']} races / {period['horses']} horses ---")
        print("model                    | k=1   k=2   k=3   k=4 | LogLoss | Brier")
        for label, metrics in period["models"].items():
            print(f"{label:24s} | {_metric_cells(metrics)}")
        optional = report.get("m3_relative") or {}
        optional_periods = optional.get("periods", {}) if isinstance(optional, dict) else {}
        if period_name in optional_periods:
            m3_metrics = optional_periods[period_name]["models"]["m3"]
            print(f"{'M3+relative':24s} | {_metric_cells(m3_metrics)}")

    coef = report["coefficients"]
    print("\nrelative coefficients (standardized):")
    for name in RELATIVE_FEATURES:
        print(f"  {name:18s} {coef['standardized_relative'][name]:+.6f}")
    print("\neffective coefficients (raw within-race slope):")
    for source, row in coef["effective"].items():
        components = "+".join(row["components"])
        print(f"  {source:12s} {row['raw_coefficient']:+.6f}  ({components})")
    print(f"\n識別性: {coef['identifiability_note']}")
    print(f"ライブ一致可能性: {report['live_compatibility']}")


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isfile(args.db):
        raise FileNotFoundError(f"ability database not found: {args.db}")
    with closing(sqlite3.connect(args.db)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    print(f"ロード: {len(runs)}行 / 統計=ability.db年次as-of / dataset=29列を1回構築")
    dataset = build_relative_feature_dataset(
        runs, load_win5_cfg(), db_path=args.db)
    del runs
    gc.collect()
    report = evaluate_relative_dataset(dataset)
    print_report(report)
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
