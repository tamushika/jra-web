"""T41: low-cost feature pack (A3 weight-delta / A7 rotation / A8 kinryo-delta /
X3 time-index state) — frozen grid evaluation.

Evaluation-only.  Never writes the production model, factor snapshot, rule
file, or any production artifact.  ``backtest_ml.FEATURES`` (the production
21-feature contract) is untouched; this harness only ever adds pack columns
through ``backtest_ml.build_dataset(..., pack_groups=...)``, which is fully
inert when ``pack_groups`` is not supplied (SPEC-T41 SS2/SS5.4).

Search grid is preregistered in eval/experiments.jsonl as
``T41-lowcost-feature-pack-v1``: L2 in {0.3, 1.0, 3.0} x group-config in
{baseline, +A3, +A7, +A8, +X3, +ALL} = 18 candidates, selected on the 2024
primary metric (win_logloss_2024_selection).  This script does not add,
remove, or otherwise explore beyond that fixed 18-candidate grid.

  学習: 2021-2023 / 選抜(L2・構成のみ): 2024 / 2025・2026H1: historical benchmark
  (2024で選抜した構成・L2のまま2021-24 final refitを固定評価。再調整禁止)

【使い方】
  python backtest_feature_pack.py             # 18候補フルグリッド + historical benchmark
  python backtest_feature_pack.py --smoke      # 1候補 (baseline, L2=1.0) だけで動作確認
  python backtest_feature_pack.py --json-out P # 数値レポートをJSONでも書き出す
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
from backtest_fold_stats import same_population_metrics  # noqa: E402
from backtest_ml import (FEATURES, A3_FEATURES, A7_FEATURES, A8_FEATURES,
                         X3_FEATURES, PACK_GROUP_ORDER, pack_feature_names,
                         fit_conditional_logit, fit_temperature)  # noqa: E402
from backtest_stats_retrain import (DB_PATH,
                                    build_consistent_feature_dataset)  # noqa: E402
from backtest_win5 import load_win5_cfg, parse_final_odds  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402


EXPERIMENT_ID = "T41-lowcost-feature-pack-v1"
ABILITY_DB_SHA256_EXPECTED = (
    "ea6b8e5b989d722658170d2499342f98d82f01c5b875c3570abf26c63112f061")

DATA_FROM = "20210101"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
FINAL_REFIT_TO = "20241231"
PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
}
DATA_TO = max(date_to for _from, date_to in PERIODS.values())

L2_GRID = (0.3, 1.0, 3.0)
GROUP_CONFIGS = {
    "baseline": frozenset(),
    "+A3": frozenset({"A3"}),
    "+A7": frozenset({"A7"}),
    "+A8": frozenset({"A8"}),
    "+X3": frozenset({"X3"}),
    "+ALL": frozenset(PACK_GROUP_ORDER),
}
ALL_PACK_GROUPS = frozenset(PACK_GROUP_ORDER)
ALL_COLUMN_NAMES = list(FEATURES) + pack_feature_names(ALL_PACK_GROUPS)
COLUMN_INDEX = {name: index for index, name in enumerate(ALL_COLUMN_NAMES)}

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 41


def verify_ability_db_hash(db_path=DB_PATH):
    import hashlib
    digest = hashlib.sha256()
    with open(db_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    return actual, actual == ABILITY_DB_SHA256_EXPECTED


def _period_mask(dates, date_from, date_to):
    dates = np.asarray(dates)
    return (dates >= date_from) & (dates <= date_to)


def _keys_for_mask(keys, mask):
    return [key for key, keep in zip(keys, mask) if keep]


def _rows_for_mask(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def _columns_for_config(group_config):
    names = list(FEATURES) + pack_feature_names(group_config)
    return [COLUMN_INDEX[name] for name in names], names


def build_all_pack_dataset(runs, cfg, *, db_path=DB_PATH):
    """Build the single 45-column (21 FEATURES + 24 pack) dataset once,
    through T33's leak-free yearly ability.db as-of factor folds."""
    return build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=db_path,
        dataset_kwargs={"pack_groups": ALL_PACK_GROUPS})


def _softmax_winner_neglogp(values, winner_index, temperature):
    values = np.asarray(values, dtype=float) / float(temperature or 1.0)
    values = values - values.max()
    exp_values = np.exp(values)
    probabilities = exp_values / exp_values.sum()
    return -math.log(max(1e-15, float(probabilities[winner_index])))


def race_level_winner_logloss(scores, labels, race_keys, meta, *, temperature=1.0,
                              min_race_no=9, min_horses=8):
    """Per-race winner LogLoss on the same strict population as
    ``same_population_metrics`` (min race#9+, 8+ runners, complete field,
    settled odds for every retained runner).  Returns ``{race_key: logloss}``
    for T39 paired event-day block bootstrap use (eval/blocks.py)."""
    grouped = defaultdict(list)
    for score, label, key, runner in zip(scores, labels, race_keys, meta):
        grouped[key].append((float(score), int(label), runner))

    out = {}
    for key, members in grouped.items():
        race_no = key[2]
        if race_no is None or race_no < min_race_no or len(members) < min_horses:
            continue
        field_size = max(
            (int(row.get("total_horses") or 0) for _, _, row in members), default=0)
        if field_size and len(members) != field_size:
            continue
        winner_indices = [i for i, (_, label, _r) in enumerate(members) if label == 1]
        if not winner_indices:
            continue
        odds = [parse_final_odds(row.get("win_pay"), row.get("rank"))
                for _, _, row in members]
        if any(value is None or value <= 1.0 for value in odds):
            continue
        values = [score for score, _, _ in members]
        loglosses = [_softmax_winner_neglogp(values, index, temperature)
                    for index in winner_indices]
        out[key] = sum(loglosses) / len(loglosses)
    return out


def _blocks_by_date(race_logloss):
    blocks = defaultdict(list)
    for (date, _place, _r), value in race_logloss.items():
        blocks[date].append(value)
    return dict(blocks)


def _mean_metric(rows):
    return sum(rows) / len(rows)


def fit_selection_candidate(features_all, labels, race_keys, meta, dates,
                            group_config, l2):
    """Fit weights on 2021-23, calibrate temperature on 2024 (T13/T33
    convention: weights are not refit on the calibration year), and report
    the 2024 win LogLoss (primary metric) plus safety metrics on the same
    strict same-population race set as the market baseline."""
    from sklearn.preprocessing import StandardScaler

    columns, _names = _columns_for_config(group_config)
    X = features_all[:, columns]
    train = _period_mask(dates, TRAIN_FROM, TRAIN_TO)
    selection = _period_mask(dates, SELECTION_FROM, SELECTION_TO)
    if not train.any() or not selection.any():
        raise RuntimeError("2021-23学習 または 2024選抜のサンプルがありません")

    scaler = StandardScaler().fit(X[train])
    train_z = scaler.transform(X[train])
    train_keys = _keys_for_mask(race_keys, train)
    weights = fit_conditional_logit(train_z, labels[train], train_keys, l2=l2)

    selection_z = scaler.transform(X[selection])
    selection_keys = _keys_for_mask(race_keys, selection)
    selection_meta = _rows_for_mask(meta, selection)
    selection_scores = selection_z @ weights
    temperature = fit_temperature(selection_scores, labels[selection], selection_keys)

    comparison = same_population_metrics(
        selection_scores, labels[selection], selection_keys, selection_meta,
        temperature=temperature)
    return {
        "group_config": sorted(group_config),
        "l2": l2,
        "inner_scaler": scaler,
        "inner_weights": weights,
        "temperature": temperature,
        "selection": comparison,
    }


def _final_refit(features_all, labels, race_keys, meta, dates, group_config, l2,
                 temperature):
    """2021-2024 final refit at the given (group_config, l2), reusing the
    temperature already calibrated during selection (T13 convention: the
    temperature is fit once and carried into the final refit unchanged)."""
    from sklearn.preprocessing import StandardScaler

    columns, _names = _columns_for_config(group_config)
    X = features_all[:, columns]
    final = _period_mask(dates, TRAIN_FROM, FINAL_REFIT_TO)
    if not final.any():
        raise RuntimeError("2021-24 final refitのサンプルがありません")
    scaler = StandardScaler().fit(X[final])
    weights = fit_conditional_logit(
        scaler.transform(X[final]), labels[final],
        _keys_for_mask(race_keys, final), l2=l2)
    return {
        "group_config": sorted(group_config),
        "l2": l2,
        "scaler": scaler,
        "weights": weights,
        "temperature": temperature,
        "columns": columns,
        "train_rows": int(final.sum()),
    }


def _score_period(model, features_all, labels, race_keys, meta, dates,
                  date_from, date_to):
    mask = _period_mask(dates, date_from, date_to)
    if not mask.any():
        raise RuntimeError(f"評価サンプルがありません: {date_from}-{date_to}")
    X = features_all[mask][:, model["columns"]]
    scores = model["scaler"].transform(X) @ model["weights"]
    keys = _keys_for_mask(race_keys, mask)
    period_meta = _rows_for_mask(meta, mask)
    period_labels = labels[mask]
    comparison = same_population_metrics(
        scores, period_labels, keys, period_meta, temperature=model["temperature"])
    race_logloss = race_level_winner_logloss(
        scores, period_labels, keys, period_meta, temperature=model["temperature"])
    return comparison, race_logloss


def run_grid(features_all, labels, race_keys, meta, dates):
    """Fit all 18 preregistered candidates and pick the primary-metric winner."""
    candidates = []
    for config_name, group_config in GROUP_CONFIGS.items():
        for l2 in L2_GRID:
            result = fit_selection_candidate(
                features_all, labels, race_keys, meta, dates, group_config, l2)
            result["config_name"] = config_name
            candidates.append(result)
            print(f"  [2024選抜] {config_name:8s} L2={l2:<4} "
                  f"win_logloss_2024={result['selection']['models']['model']['logloss']:.6f} "
                  f"brier={result['selection']['models']['model']['brier']:.8f} "
                  f"k1-3={[round(c*100,2) for c in result['selection']['models']['model']['coverage'][:3]]}")

    overall_best = min(
        candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline_candidates = [row for row in candidates if row["config_name"] == "baseline"]
    baseline_best = min(
        baseline_candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    return candidates, overall_best, baseline_best


def historical_benchmark(features_all, labels, race_keys, meta, dates, selected):
    """Final-refit 2021-24 at the selected (group_config, l2); report 2025 and
    2026H1 without any re-tuning, plus the 2021-23-trained model as a
    reference row (SPEC-T41 SS4.1)."""
    group_config = frozenset(selected["group_config"])
    l2 = selected["l2"]
    temperature = selected["temperature"]

    final_model = _final_refit(
        features_all, labels, race_keys, meta, dates, group_config, l2, temperature)
    reference_model = {
        "group_config": sorted(group_config),
        "l2": l2,
        "scaler": selected["inner_scaler"],
        "weights": selected["inner_weights"],
        "temperature": temperature,
        "columns": _columns_for_config(group_config)[0],
    }

    report = {}
    for period_name, (date_from, date_to) in PERIODS.items():
        final_comparison, final_race_logloss = _score_period(
            final_model, features_all, labels, race_keys, meta, dates,
            date_from, date_to)
        reference_comparison, _reference_race_logloss = _score_period(
            reference_model, features_all, labels, race_keys, meta, dates,
            date_from, date_to)
        if final_comparison["sample_signature"] != reference_comparison["sample_signature"]:
            raise RuntimeError(
                f"{period_name}: final-refit と 21-23学習版で評価母集団が一致しません")
        report[period_name] = {
            "final_refit_2021_2024": final_comparison,
            "trained_2021_2023_reference": reference_comparison,
            "race_logloss": final_race_logloss,
        }
    return final_model, reference_model, report


def market_topk_floor(historical_report):
    """Factual comparison of the selected model's top-k capture against the
    market (M0 popularity) baseline on the same population, per period/k.
    Reports numbers and a raw floor boolean only; no adoption judgement."""
    floor = {}
    for period_name, period in historical_report.items():
        comparison = period["final_refit_2021_2024"]
        model_cov = comparison["models"]["model"]["coverage"]
        market_cov = comparison["models"]["market"]["coverage"]
        floor[period_name] = {
            "model_coverage": model_cov,
            "market_coverage": market_cov,
            "model_meets_or_exceeds_market": [
                bool(m >= k) for m, k in zip(model_cov, market_cov)
            ],
        }
    return floor


def paired_win5_day_bootstrap(selected_final, baseline_final, features_all, labels,
                              race_keys, meta, dates):
    """T39 paired event-day block bootstrap (eval/blocks.py) of mean per-race
    winner LogLoss, selected-pack model vs. best-baseline model, per period."""
    results = {}
    for period_name, (date_from, date_to) in PERIODS.items():
        _selected_comparison, selected_logloss = _score_period(
            selected_final, features_all, labels, race_keys, meta, dates,
            date_from, date_to)
        _baseline_comparison, baseline_logloss = _score_period(
            baseline_final, features_all, labels, race_keys, meta, dates,
            date_from, date_to)
        common_keys = set(selected_logloss) & set(baseline_logloss)
        if common_keys != set(selected_logloss) or common_keys != set(baseline_logloss):
            raise RuntimeError(
                f"{period_name}: selected/baselineモデルで評価対象レースが一致しません")
        blocks_selected = _blocks_by_date(selected_logloss)
        blocks_baseline = _blocks_by_date(baseline_logloss)
        if set(blocks_selected) != set(blocks_baseline):
            raise RuntimeError(f"{period_name}: WIN5開催日ブロックのキーが一致しません")
        bootstrap = paired_block_bootstrap(
            _mean_metric, blocks_selected, blocks_baseline,
            n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
        results[period_name] = {
            "n_races": len(selected_logloss),
            "n_day_blocks": len(blocks_selected),
            "observed_diff_selected_minus_baseline": bootstrap.observed_difference,
            "ci_low": bootstrap.ci_low,
            "ci_high": bootstrap.ci_high,
            "p_value": bootstrap.p_value,
            "n_resamples": bootstrap.n_resamples,
            "seed": bootstrap.seed,
        }
    return results


def coverage_report(features_all, dates):
    """Per-feature missing/flag rate, by period (train 21-23 / selection 2024 /
    2025 / 2026H1), for every pack column (SPEC-T41 report requirement)."""
    periods = {
        "train_2021_2023": (TRAIN_FROM, TRAIN_TO),
        "selection_2024": (SELECTION_FROM, SELECTION_TO),
        **PERIODS,
    }
    pack_names = pack_feature_names(ALL_PACK_GROUPS)
    report = {}
    for period_name, (date_from, date_to) in periods.items():
        mask = _period_mask(dates, date_from, date_to)
        period_report = {}
        for name in pack_names:
            column = features_all[mask, COLUMN_INDEX[name]]
            period_report[name] = {
                "mean": float(np.mean(column)) if len(column) else None,
                "std": float(np.std(column)) if len(column) else None,
                "nonzero_rate": float(np.mean(column != 0.0)) if len(column) else None,
            }
        report[period_name] = period_report
    return report, pack_names


def _cells(metrics):
    coverage = list(metrics["coverage"][:4])
    coverage.extend([float("nan")] * (4 - len(coverage)))
    topk = "  ".join(
        " n/a" if math.isnan(value) else f"{100.0 * value:4.1f}" for value in coverage)
    return f"{topk} | {metrics['logloss']:.6f} | {metrics['brier']:.8f}"


def print_report(candidates, overall_best, baseline_best, historical, floor,
                 bootstrap_result, coverage, pack_names, ability_hash, hash_ok):
    print(f"\nability.db sha256: {ability_hash} "
          f"({'MATCH' if hash_ok else 'MISMATCH -- STOP'})")
    print(f"experiment_id: {EXPERIMENT_ID}")

    print("\n===== 18候補: 2024選抜 (primary=win_logloss_2024_selection) =====")
    print("config    | L2   | LogLoss  | Brier      | k1    k2    k3")
    for row in sorted(candidates, key=lambda r: r["selection"]["models"]["model"]["logloss"]):
        model = row["selection"]["models"]["model"]
        cov = "  ".join(f"{100*c:4.1f}" for c in model["coverage"][:3])
        print(f"{row['config_name']:9s} | {row['l2']:<4} | {model['logloss']:.6f} | "
              f"{model['brier']:.8f} | {cov}")
    market = candidates[0]["selection"]["models"]["market"]
    print(f"{'M0 popularity (2024)':9s} |      | {market['logloss']:.6f} | "
          f"{market['brier']:.8f} | " + "  ".join(f"{100*c:4.1f}" for c in market["coverage"][:3]))

    print(f"\n選抜構成: {overall_best['config_name']} (groups={overall_best['group_config']}) "
          f"L2={overall_best['l2']} / baseline最良L2={baseline_best['l2']}")

    for period_name, period in historical.items():
        print(f"\n===== historical benchmark {period_name} =====")
        print("model                          | k=1   k=2   k=3   k=4 | LogLoss  | Brier")
        final_models = period["final_refit_2021_2024"]["models"]
        ref_models = period["trained_2021_2023_reference"]["models"]
        print(f"{'M0 popularity':30s} | {_cells(final_models['market'])}")
        print(f"{'selected (2021-24 final refit)':30s} | {_cells(final_models['model'])}")
        print(f"{'selected (2021-23参考値)':30s} | {_cells(ref_models['model'])}")

    print("\n===== 市場top-kフロア (判定のみ・factual) =====")
    for period_name, row in floor.items():
        print(f"{period_name}: model={[round(100*c,2) for c in row['model_coverage'][:4]]} "
              f"market={[round(100*c,2) for c in row['market_coverage'][:4]]} "
              f"model>=market各k={row['model_meets_or_exceeds_market']}")

    print("\n===== WIN5開催日paired block bootstrap (selected - baseline, winner LogLoss平均) =====")
    for period_name, row in bootstrap_result.items():
        print(f"{period_name}: n_races={row['n_races']} n_day_blocks={row['n_day_blocks']} "
              f"diff={row['observed_diff_selected_minus_baseline']:+.6f} "
              f"CI95=[{row['ci_low']:+.6f},{row['ci_high']:+.6f}] p={row['p_value']:.4f} "
              f"(n_resamples={row['n_resamples']}, seed={row['seed']})")

    print("\n===== 特徴別coverage/flag率 (mean/std/nonzero率) =====")
    for period_name, period_report in coverage.items():
        print(f"\n--- {period_name} ---")
        for name in pack_names:
            row = period_report[name]
            print(f"  {name:34s} mean={row['mean']:+.4f} std={row['std']:.4f} "
                  f"nonzero率={100*row['nonzero_rate']:.2f}%")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="baseline/L2=1.0の1候補だけで動作確認 (歴史ベンチマークも同構成で実行)")
    parser.add_argument("--json-out", default=None, metavar="PATH",
                        help="数値レポートをJSONでも書き出す (リポジトリ外推奨)")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def main(argv=None):
    args = parse_args(argv)
    ability_hash, hash_ok = verify_ability_db_hash(args.db)
    print(f"ability.db sha256 = {ability_hash}")
    if not hash_ok:
        print(f"[STOP] ability.db sha256が事前登録値と不一致です: "
              f"expected={ABILITY_DB_SHA256_EXPECTED}")
        return 1

    if not os.path.isfile(args.db):
        raise FileNotFoundError(f"ability database not found: {args.db}")
    with closing(sqlite3.connect(args.db)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    print(f"ロード: {len(runs)}行 / dataset={len(ALL_COLUMN_NAMES)}列を1回構築 "
          f"({len(FEATURES)} FEATURES + {len(ALL_COLUMN_NAMES) - len(FEATURES)} pack)")

    cfg = load_win5_cfg()
    features_all, labels, race_keys, meta = build_all_pack_dataset(
        runs, cfg, db_path=args.db)
    del runs
    gc.collect()
    dates = np.array([key[0] for key in race_keys])
    print(f"データセット構築完了: {features_all.shape}")

    global GROUP_CONFIGS, L2_GRID
    if args.smoke:
        GROUP_CONFIGS = {"baseline": frozenset()}
        L2_GRID = (1.0,)
        print("[smoke] baseline/L2=1.0 の1候補のみで動作確認します")

    candidates, overall_best, baseline_best = run_grid(
        features_all, labels, race_keys, meta, dates)

    selected_final, _selected_reference, historical = historical_benchmark(
        features_all, labels, race_keys, meta, dates, overall_best)
    baseline_final, _baseline_reference, _baseline_historical = historical_benchmark(
        features_all, labels, race_keys, meta, dates, baseline_best)

    floor = market_topk_floor(historical)
    bootstrap_result = paired_win5_day_bootstrap(
        selected_final, baseline_final, features_all, labels, race_keys, meta, dates)
    coverage, pack_names = coverage_report(features_all, dates)

    print_report(candidates, overall_best, baseline_best, historical, floor,
                bootstrap_result, coverage, pack_names, ability_hash, hash_ok)

    if args.json_out:
        payload = {
            "experiment_id": EXPERIMENT_ID,
            "ability_db_sha256": ability_hash,
            "candidates": [
                {
                    "config_name": row["config_name"],
                    "group_config": row["group_config"],
                    "l2": row["l2"],
                    "selection_2024": row["selection"]["models"],
                    "selection_2024_races": row["selection"]["races"],
                    "selection_2024_skipped": row["selection"]["skipped"],
                }
                for row in candidates
            ],
            "overall_best": {
                "config_name": overall_best["config_name"],
                "group_config": overall_best["group_config"],
                "l2": overall_best["l2"],
            },
            "baseline_best": {
                "l2": baseline_best["l2"],
            },
            "historical_benchmark": {
                period_name: {
                    "final_refit_2021_2024": period["final_refit_2021_2024"]["models"],
                    "trained_2021_2023_reference": period["trained_2021_2023_reference"]["models"],
                    "races": period["final_refit_2021_2024"]["races"],
                }
                for period_name, period in historical.items()
            },
            "market_topk_floor": floor,
            "win5_day_paired_bootstrap": bootstrap_result,
            "coverage": coverage,
        }
        with open(args.json_out, "w", encoding="utf-8") as fp:
            json.dump(_json_safe(payload), fp, ensure_ascii=False, indent=1)
        print(f"\n[OK] JSONレポート -> {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
