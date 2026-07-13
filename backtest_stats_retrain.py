"""T33: consistent factor-source retraining and requested three-way evaluation.

This harness is evaluation-only.  It never writes the production model,
factor snapshot, or rule files.  The current 21-feature WIN5 model does not
contain a mined-rules feature; rule files are validated and recorded as
evaluation metadata.  Their effect on this CL is explicitly reported as N/A.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from backtest_ability import load_runs  # noqa: E402
from backtest_fold_stats import (build_feature_dataset,
                                 evaluate_feature_dataset)  # noqa: E402
from backtest_ml import FEATURES  # noqa: E402
from backtest_win5 import load_upset_map, load_win5_cfg  # noqa: E402
from fold_stats import FoldFactorTableProvider, fold_as_of_for  # noqa: E402


DB_PATH = os.path.join(BASE_DIR, "ability.db")
PEDIGREE_CACHE_PATH = os.path.join(BASE_DIR, "pedigree_cache.json")
CURRENT_RULES_PATH = os.path.join(
    API_DIR, "data_files", "common", "mined_rules.csv")
V2_RULES_PATH = os.path.join(BASE_DIR, "mined_rules_v2.csv")
STATS_FROM = "20210101"
PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
}
REQUIRED_RULE_COLUMNS = (
    "場", "芝ダ", "距離", "条件1", "条件2", "条件3", "種別", "点数")


@dataclass(frozen=True)
class RuleSetMetadata:
    """Validated identity of a mined-rules CSV used in a comparison label."""

    label: str
    path: str
    sha256: str
    rules: int
    buys: int
    kills: int


def _rules_label(path: str) -> str:
    resolved = os.path.normcase(os.path.abspath(path))
    if resolved == os.path.normcase(os.path.abspath(CURRENT_RULES_PATH)):
        return "current rules"
    if resolved == os.path.normcase(os.path.abspath(V2_RULES_PATH)):
        return "v2 rules"
    return Path(path).stem


def validate_rules_file(path: str | os.PathLike[str]) -> RuleSetMetadata:
    """Validate the production mined-rules CSV contract and fingerprint it."""
    path = os.path.abspath(os.fspath(path))
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"rules file not found or unreadable: {path}") from exc

    rules = buys = kills = 0
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            if reader.fieldnames != list(REQUIRED_RULE_COLUMNS):
                raise ValueError(
                    "rules CSV header must be: " + ",".join(REQUIRED_RULE_COLUMNS))
            for line_number, row in enumerate(reader, start=2):
                try:
                    venue = str(row["場"] or "").strip()
                    race_type = str(row["芝ダ"] or "").strip()
                    distance = int(row["距離"])
                    kind = str(row["種別"] or "").strip()
                    points = float(row["点数"])
                    conditions = [str(row[name] or "").strip()
                                  for name in ("条件1", "条件2", "条件3")]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid rules row at line {line_number}") from exc
                if (not venue or race_type not in ("芝", "ダート") or distance <= 0
                        or kind not in ("買い", "消し")
                        or not math.isfinite(points) or points < 0
                        or not any(conditions)):
                    raise ValueError(f"invalid rules row at line {line_number}")
                rules += 1
                buys += kind == "買い"
                kills += kind == "消し"
    except UnicodeError as exc:
        raise ValueError(f"rules file is not valid UTF-8 CSV: {path}") from exc
    if not rules:
        raise ValueError(f"rules file contains no valid rules: {path}")
    return RuleSetMetadata(
        label=_rules_label(path),
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        rules=rules,
        buys=buys,
        kills=kills,
    )


def build_consistent_feature_dataset(
    runs,
    cfg,
    date_to: str,
    *,
    stats_source: str = "ability",
    db_path: str = DB_PATH,
    stats_as_of: str | None = None,
    factor_provider=None,
    dataset_kwargs=None,
):
    """Return ``(X, y, race_keys, meta)`` from one consistent stats source.

    ``stats_source='ability'`` applies previous-year-end snapshots to every
    row (2021 rows use <=2020, ..., 2026 rows use <=2025), preventing target
    encoding leakage in both training and evaluation.  ``'current'`` preserves
    the legacy db-keiba CSV path for the control quadrant.  No files are
    written.
    """
    if stats_source not in ("current", "ability"):
        raise ValueError("stats_source must be 'current' or 'ability'")
    if stats_source == "current":
        if factor_provider is not None or stats_as_of is not None:
            raise ValueError(
                "factor_provider/stats_as_of are only valid for ability stats")
        return build_feature_dataset(
            runs, cfg, date_to, provider=None, dataset_kwargs=dataset_kwargs)

    if factor_provider is None:
        stats_as_of = stats_as_of or fold_as_of_for(date_to)
        factor_provider = FoldFactorTableProvider(
            db_path,
            stats_as_of,
            pedigree_cache_path=PEDIGREE_CACHE_PATH,
            legacy_api_dir=API_DIR,
        )
    return build_feature_dataset(
        runs, cfg, date_to, provider=factor_provider,
        dataset_kwargs=dataset_kwargs)


def _metric_fingerprint(metrics):
    return (
        tuple(metrics["coverage"]), metrics["logloss"], metrics["brier"])


def evaluate_quadrants(
    runs,
    cfg,
    upset_map,
    rule_sets,
    *,
    periods=PERIODS,
    stats_sources=("current", "ability"),
    db_path=DB_PATH,
):
    """Evaluate the requested stats/rules labels without production writes.

    ``mined_rules`` is not an input to the 21-feature CL.  The two ability
    rows therefore validate and fingerprint the requested files but are an
    intentionally identical N/A comparison, not a claimed rules A/B effect.
    """
    if not rule_sets:
        raise ValueError("at least one validated rules file is required")
    if not periods:
        raise ValueError("at least one evaluation period is required")
    if "mined_rules" in FEATURES or "mined_pts" in FEATURES:
        raise AssertionError("T33 assumes the current 21-feature CL is rules-independent")

    max_to = max(date_to for _date_from, date_to in periods.values())
    datasets = {
        source: build_consistent_feature_dataset(
            runs, cfg, max_to, stats_source=source, db_path=db_path)
        for source in stats_sources
    }
    rows = []
    market_by_period = {}
    sample_by_period = {}
    for period_name, (date_from, date_to) in periods.items():
        for stats_source in stats_sources:
            result = evaluate_feature_dataset(
                *datasets[stats_source], upset_map, date_from, date_to)
            comparison = result["comparison"]
            if not comparison["races"]:
                raise RuntimeError(f"no same-population races for {period_name}")
            signature = comparison["sample_signature"]
            if period_name in sample_by_period and sample_by_period[period_name] != signature:
                raise RuntimeError(
                    f"stats sources use different runner populations: {period_name}")
            sample_by_period.setdefault(period_name, signature)
            market = comparison["models"]["market"]
            if period_name in market_by_period:
                if _metric_fingerprint(market_by_period[period_name]) != _metric_fingerprint(market):
                    raise RuntimeError(
                        f"market baseline differs by stats source: {period_name}")
            else:
                market_by_period[period_name] = market

            model_metrics = comparison["models"]["model"]
            source_rows = []
            source_rule_sets = rule_sets if stats_source == "ability" else rule_sets[:1]
            for rules in source_rule_sets:
                row = {
                    "period": period_name,
                    "stats_source": stats_source,
                    "rules": rules,
                    "metrics": dict(model_metrics),
                    "races": comparison["races"],
                    "horses": comparison["horses"],
                    "temperature": result["temperature"],
                    "skipped": comparison["skipped"],
                    "rules_effect": (
                        "N/A: current 21-feature CL does not consume mined_rules"
                    ),
                }
                rows.append(row)
                source_rows.append(row)
            fingerprints = {
                _metric_fingerprint(row["metrics"]) for row in source_rows}
            if len(fingerprints) != 1:
                raise AssertionError(
                    "current 21-feature CL unexpectedly changed with mined-rules file")

    return {
        "rows": rows,
        "market": market_by_period,
        "samples": sample_by_period,
        "rules_effect": (
            "N/A: current 21-feature CL does not consume mined_rules"
        ),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="T33 統計ソース比較（mined rulesはCLへの効果N/A）")
    parser.add_argument(
        "--stats-source", choices=("all", "current", "ability"), default="all")
    parser.add_argument(
        "--rules-file", action="append", default=None, metavar="PATH",
        help="評価メタデータに使うrules CSV (複数指定可; 既定: current+v2)")
    parser.add_argument(
        "--period", choices=("all", *PERIODS), default="all")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _cells(metrics):
    coverage = list(metrics["coverage"][:4])
    coverage.extend([float("nan")] * (4 - len(coverage)))
    topk = "  ".join(
        " n/a" if math.isnan(value) else f"{100.0 * value:4.1f}"
        for value in coverage)
    return f"{topk} | {metrics['logloss']:.6f} | {metrics['brier']:.8f}"


def print_report(report, rule_sets, periods):
    print("\nvalidated rules metadata:")
    for rules in rule_sets:
        print(f"  {rules.label}: {rules.rules} rules "
              f"(買{rules.buys}/消{rules.kills}), sha256={rules.sha256[:12]}")
    print("rules effect: N/A — production compute_score_ml と現行21特徴CLは "
          "mined_rules を入力に含みません。rules差し替えはWeb手動スコア側だけに効くため、"
          "ability+current/v2の2行はファイル検証用ラベルでありCLの実測A/Bではありません。")

    for period_name in periods:
        matching = [row for row in report["rows"] if row["period"] == period_name]
        print(f"\n===== T33 {period_name} (9R以降・8頭以上・全馬特徴/オッズあり) =====")
        print("model                              | k=1   k=2   k=3   k=4 | LogLoss | Brier      | n")
        market = report["market"][period_name]
        n_races = matching[0]["races"]
        print(f"{'人気ベースライン':34s} | {_cells(market)} | {n_races}")
        for row in matching:
            stats_label = "現行統計" if row["stats_source"] == "current" else "ability統計"
            label = f"{stats_label}+{row['rules'].label} [rules N/A]"
            print(f"{label:34s} | {_cells(row['metrics'])} | {row['races']}")


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isfile(args.db):
        raise FileNotFoundError(f"ability database not found: {args.db}")
    rules_paths = args.rules_file or [CURRENT_RULES_PATH, V2_RULES_PATH]
    rule_sets = [validate_rules_file(path) for path in rules_paths]
    if len({rules.path for rules in rule_sets}) != len(rule_sets):
        raise ValueError("the same rules file was specified more than once")
    periods = PERIODS if args.period == "all" else {args.period: PERIODS[args.period]}
    stats_sources = (
        ("current", "ability") if args.stats_source == "all"
        else (args.stats_source,))
    eval_to = max(date_to for _date_from, date_to in periods.values())
    with closing(sqlite3.connect(args.db)) as conn:
        runs = load_runs(conn, STATS_FROM, eval_to)
    print(f"ロード: {len(runs)}行 / 期間上限 {eval_to}")
    report = evaluate_quadrants(
        runs, load_win5_cfg(), load_upset_map(), rule_sets,
        periods=periods, stats_sources=stats_sources, db_path=args.db)
    print_report(report, rule_sets, periods)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
