"""T19: 母父・父×文脈の6列固定特徴を12候補で評価する隔離ハーネス。"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import (PERIODS, _blocks_by_date, _keys_for_mask,
                                   _mean_metric, _period_mask, _rows_for_mask,
                                   race_level_winner_logloss)
from backtest_fold_stats import same_population_metrics
from backtest_ml import FEATURES, fit_conditional_logit, fit_temperature
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_win5 import load_win5_cfg
from eval.blocks import paired_block_bootstrap


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ability.db"
PEDIGREE_PATH = BASE_DIR / "pedigree_cache.json"
EXPERIMENT_ID = "T19-pedigree-interactions-v1"
ABILITY_SHA256 = "ea6b8e5b989d722658170d2499342f98d82f01c5b875c3570abf26c63112f061"
PEDIGREE_SHA256 = "452a322ec6ec2a92f8dd9e1da3f7c05f4801780987ad2ff35c1f9aa3a95afb50"
DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
FINAL_REFIT_TO = "20241231"
L2_GRID = (0.3, 1.0, 3.0)
K_GRID = (50.0, 200.0, 800.0)
FEATURE_NAMES = ("bms_surface_pts", "bms_dist_pts", "sire_dist_pts", "bms_wet_pts",
                 "bms_available", "bms_wet_available")
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 19


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _surface(value):
    return "ダート" if str(value) in ("ダ", "ダート") else "芝"


def _dist_band(value):
    distance = int(value or 0)
    return "sprint" if distance <= 1400 else ("mile" if distance <= 1800 else "route")


def _is_wet(value):
    return str(value or "")[:1] in ("稍", "重", "不")


def _accumulate(rows, pedigree):
    """集計窓内の複勝率 sufficient statsを作る。"""
    entity = defaultdict(lambda: [0, 0])
    overall = defaultdict(lambda: [0, 0])
    for row in rows:
        try:
            rank = int(row.get("rank"))
        except (TypeError, ValueError):
            continue
        surface, band, wet = _surface(row.get("track_type")), _dist_band(row.get("distance")), _is_wet(row.get("condition"))
        hit = int(rank <= 3)
        for context in (("surface", surface), ("dist", band)):
            overall[context][0] += 1
            overall[context][1] += hit
        if wet:
            overall[("wet", True)][0] += 1
            overall[("wet", True)][1] += hit
        ped = pedigree.get(row.get("horse")) or {}
        sire, bms = str(ped.get("sire") or "").strip(), str(ped.get("bms") or "").strip()
        if bms:
            for context in (("bms_surface", bms, surface), ("bms_dist", bms, band)):
                entity[context][0] += 1
                entity[context][1] += hit
            if wet:
                entity[("bms_wet", bms)][0] += 1
                entity[("bms_wet", bms)][1] += hit
        if sire:
            entity[("sire_dist", sire, band)][0] += 1
            entity[("sire_dist", sire, band)][1] += hit
    return entity, overall


def _cell(entity, overall, entity_key, overall_key):
    n, hits = entity.get(entity_key, (0, 0))
    global_n, global_hits = overall.get(overall_key, (0, 0))
    delta = (hits / n - global_hits / global_n) if n and global_n else 0.0
    return float(delta), float(n)


def build_pedigree_sufficient(meta, history_rows, pedigree, window_years=5):
    """各評価年を前年末で凍結し、4特徴の(rate差,n)+flag2を返す。"""
    output = np.zeros((len(meta), 10), dtype=np.float64)
    by_year = defaultdict(list)
    for index, row in enumerate(meta):
        by_year[int(str(row["date"])[:4])].append((index, row))
    for year, targets in sorted(by_year.items()):
        date_from, date_to = f"{year-window_years:04d}0101", f"{year-1:04d}1231"
        window = [row for row in history_rows if date_from <= str(row.get("date")) <= date_to]
        entity, overall = _accumulate(window, pedigree)
        for index, row in targets:
            ped = pedigree.get(row.get("horse")) or {}
            sire, bms = str(ped.get("sire") or "").strip(), str(ped.get("bms") or "").strip()
            surface, band, wet = _surface(row.get("track_type")), _dist_band(row.get("distance")), _is_wet(row.get("condition"))
            values = (
                _cell(entity, overall, ("bms_surface", bms, surface), ("surface", surface)) if bms else (0.0, 0.0),
                _cell(entity, overall, ("bms_dist", bms, band), ("dist", band)) if bms else (0.0, 0.0),
                _cell(entity, overall, ("sire_dist", sire, band), ("dist", band)) if sire else (0.0, 0.0),
                _cell(entity, overall, ("bms_wet", bms), ("wet", True)) if bms and wet else (0.0, 0.0),
            )
            for feature_index, (delta, n) in enumerate(values):
                output[index, 2 * feature_index:2 * feature_index + 2] = (delta, n)
            output[index, 8] = float(bool(bms))
            output[index, 9] = float(bool(bms and wet and values[3][1] > 0))
    return output


def shrink_features(sufficient, k):
    out = np.zeros((len(sufficient), 6), dtype=np.float64)
    for index in range(4):
        delta, n = sufficient[:, 2 * index], sufficient[:, 2 * index + 1]
        out[:, index] = delta * n / (n + float(k))
    out[:, 4:] = sufficient[:, 8:10]
    return out


def candidate_features(base, sufficient, k=None):
    return base if k is None else np.column_stack((base, shrink_features(sufficient, k)))


def _fit_candidate(base, sufficient, labels, keys, meta, dates, *, config, k, l2):
    from sklearn.preprocessing import StandardScaler
    X = candidate_features(base, sufficient, k if config == "pedigree" else None)
    train, selection = _period_mask(dates, TRAIN_FROM, TRAIN_TO), _period_mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(scaler.transform(X[train]), labels[train], _keys_for_mask(keys, train), l2=l2)
    scores = scaler.transform(X[selection]) @ weights
    selection_keys, selection_meta = _keys_for_mask(keys, selection), _rows_for_mask(meta, selection)
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(scores, labels[selection], selection_keys, selection_meta, temperature=temperature)
    return {"config": config, "k": k, "l2": l2, "scaler": scaler, "weights": weights,
            "temperature": temperature, "selection": metrics}


def run_grid(base, sufficient, labels, keys, meta, dates):
    candidates = []
    for l2 in L2_GRID:
        candidates.append(_fit_candidate(base, sufficient, labels, keys, meta, dates,
                                         config="baseline", k=None, l2=l2))
    for k in K_GRID:
        for l2 in L2_GRID:
            candidates.append(_fit_candidate(base, sufficient, labels, keys, meta, dates,
                                             config="pedigree", k=k, l2=l2))
    best = min(candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline = min((row for row in candidates if row["config"] == "baseline"),
                   key=lambda row: row["selection"]["models"]["model"]["logloss"])
    return candidates, best, baseline


def _refit(candidate, base, sufficient, labels, keys, dates, *, final=True):
    from sklearn.preprocessing import StandardScaler
    X = candidate_features(base, sufficient, candidate["k"] if candidate["config"] == "pedigree" else None)
    mask = _period_mask(dates, TRAIN_FROM, FINAL_REFIT_TO) if final else _period_mask(dates, TRAIN_FROM, TRAIN_TO)
    scaler = StandardScaler().fit(X[mask])
    weights = fit_conditional_logit(scaler.transform(X[mask]), labels[mask], _keys_for_mask(keys, mask), l2=candidate["l2"])
    return {"candidate": candidate, "scaler": scaler, "weights": weights, "temperature": candidate["temperature"]}


def _score(model, base, sufficient, labels, keys, meta, dates, date_from, date_to):
    candidate = model["candidate"]
    X = candidate_features(base, sufficient, candidate["k"] if candidate["config"] == "pedigree" else None)
    mask = _period_mask(dates, date_from, date_to)
    scores = model["scaler"].transform(X[mask]) @ model["weights"]
    period_keys, period_meta, period_labels = _keys_for_mask(keys, mask), _rows_for_mask(meta, mask), labels[mask]
    metrics = same_population_metrics(scores, period_labels, period_keys, period_meta, temperature=model["temperature"])
    race_ll = race_level_winner_logloss(scores, period_labels, period_keys, period_meta, temperature=model["temperature"])
    return metrics, race_ll


def _context_breakdown(selected_ll, baseline_ll, meta):
    race_context = {}
    for row in meta:
        key = (row["date"], row["place"], row["r"])
        race_context.setdefault(key, (_surface(row.get("track_type")), _dist_band(row.get("distance")),
                                      "wet" if _is_wet(row.get("condition")) else "good"))
    groups = defaultdict(list)
    for key in set(selected_ll) & set(baseline_ll):
        for label in race_context.get(key, ()):
            groups[label].append(selected_ll[key] - baseline_ll[key])
    return {label: {"races": len(values), "mean_diff": float(np.mean(values))}
            for label, values in sorted(groups.items())}


def evaluate(best, baseline, base, sufficient, labels, keys, meta, dates):
    selected_final, baseline_final = _refit(best, base, sufficient, labels, keys, dates), _refit(baseline, base, sufficient, labels, keys, dates)
    selected_reference = _refit(best, base, sufficient, labels, keys, dates, final=False)
    report, bootstrap = {}, {}
    for name, (date_from, date_to) in PERIODS.items():
        metrics, selected_ll = _score(selected_final, base, sufficient, labels, keys, meta, dates, date_from, date_to)
        ref_metrics, _ = _score(selected_reference, base, sufficient, labels, keys, meta, dates, date_from, date_to)
        baseline_metrics, baseline_ll = _score(baseline_final, base, sufficient, labels, keys, meta, dates, date_from, date_to)
        blocks_s, blocks_b = _blocks_by_date(selected_ll), _blocks_by_date(baseline_ll)
        boot = paired_block_bootstrap(_mean_metric, blocks_s, blocks_b,
                                      n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
        report[name] = {"selected_final": metrics["models"], "selected_reference": ref_metrics["models"],
                        "baseline_final": baseline_metrics["models"],
                        "context_selected_minus_baseline": _context_breakdown(selected_ll, baseline_ll, meta)}
        bootstrap[name] = {"day_blocks": len(blocks_s), "observed_diff": boot.observed_difference,
                           "ci_low": boot.ci_low, "ci_high": boot.ci_high, "p_value": boot.p_value}
    return report, bootstrap


def selection_context(candidate, baseline, base, sufficient, labels, keys, meta, dates):
    selected_model = {"candidate": candidate, "scaler": candidate["scaler"],
                      "weights": candidate["weights"], "temperature": candidate["temperature"]}
    baseline_model = {"candidate": baseline, "scaler": baseline["scaler"],
                      "weights": baseline["weights"], "temperature": baseline["temperature"]}
    _metrics, selected_ll = _score(selected_model, base, sufficient, labels, keys, meta, dates,
                                   SELECTION_FROM, SELECTION_TO)
    _baseline_metrics, baseline_ll = _score(baseline_model, base, sufficient, labels, keys, meta, dates,
                                            SELECTION_FROM, SELECTION_TO)
    return _context_breakdown(selected_ll, baseline_ll, meta)


def coverage_report(sufficient, dates):
    periods = {"train_2021_2023": (TRAIN_FROM, TRAIN_TO), "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    result = {}
    for name, (date_from, date_to) in periods.items():
        mask = _period_mask(dates, date_from, date_to)
        result[name] = {"bms_available_rate": float(np.mean(sufficient[mask, 8])),
                        "bms_wet_available_rate": float(np.mean(sufficient[mask, 9]))}
    return result


def _safe(value):
    if isinstance(value, dict): return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_safe(v) for v in value]
    if isinstance(value, np.generic): return value.item()
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--pedigree", type=Path, default=PEDIGREE_PATH)
    parser.add_argument("--json-out", type=Path, default=BASE_DIR / "outputs" / "t19_result.json")
    args = parser.parse_args(argv)
    ability_hash, pedigree_hash = sha256_file(args.db), sha256_file(args.pedigree)
    if ability_hash != ABILITY_SHA256 or pedigree_hash != PEDIGREE_SHA256:
        raise SystemExit(f"[STOP] hash mismatch: ability={ability_hash} pedigree={pedigree_hash}")
    pedigree = json.loads(args.pedigree.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
        history = [dict(zip(("date", "horse", "rank", "track_type", "distance", "condition"), row))
                   for row in conn.execute("""SELECT date,horse,rank,track_type,distance,condition
                                               FROM runs WHERE date BETWEEN '20160101' AND '20251231'""")]
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(args.db))
    del runs
    gc.collect()
    dates = np.array([key[0] for key in keys])
    sufficient = build_pedigree_sufficient(meta, history, pedigree)
    candidates, best, baseline = run_grid(base, sufficient, labels, keys, meta, dates)
    historical, bootstrap = evaluate(best, baseline, base, sufficient, labels, keys, meta, dates)
    pedigree_best = min((row for row in candidates if row["config"] == "pedigree"),
                        key=lambda row: row["selection"]["models"]["model"]["logloss"])
    pedigree_historical, pedigree_bootstrap = evaluate(
        pedigree_best, baseline, base, sufficient, labels, keys, meta, dates)
    payload = {"experiment_id": EXPERIMENT_ID, "ability_db_sha256": ability_hash,
               "pedigree_cache_json_sha256": pedigree_hash,
               "candidates": [{"config": row["config"], "k": row["k"], "l2": row["l2"],
                                "selection_2024": row["selection"]["models"]} for row in candidates],
               "best": {"config": best["config"], "k": best["k"], "l2": best["l2"]},
               "baseline_best": {"l2": baseline["l2"]}, "historical_benchmark": historical,
               "win5_day_paired_bootstrap": bootstrap,
               "best_pedigree_diagnostic": {
                   "config": pedigree_best["config"], "k": pedigree_best["k"], "l2": pedigree_best["l2"],
                   "selection_context_selected_minus_baseline": selection_context(
                       pedigree_best, baseline, base, sufficient, labels, keys, meta, dates),
                   "historical_benchmark": pedigree_historical,
                   "win5_day_paired_bootstrap": pedigree_bootstrap,
               },
               "coverage": coverage_report(sufficient, dates)}
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(_safe(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    print("===== T19 2024 selection (12 candidates) =====")
    for row in sorted(candidates, key=lambda value: value["selection"]["models"]["model"]["logloss"]):
        metric = row["selection"]["models"]["model"]
        print(f"{row['config']:8s} k={str(row['k']):>5s} L2={row['l2']:<3} LL={metric['logloss']:.6f} Brier={metric['brier']:.8f}")
    print("best", payload["best"])
    for period, value in historical.items():
        selected, base_metric = value["selected_final"]["model"], value["baseline_final"]["model"]
        print(period, f"selected LL={selected['logloss']:.6f}", f"baseline LL={base_metric['logloss']:.6f}", bootstrap[period])
    print("best pedigree diagnostic", {"k": pedigree_best["k"], "l2": pedigree_best["l2"]})
    for period, value in pedigree_historical.items():
        selected, base_metric = value["selected_final"]["model"], value["baseline_final"]["model"]
        print(period, f"pedigree LL={selected['logloss']:.6f}",
              f"baseline LL={base_metric['logloss']:.6f}", pedigree_bootstrap[period])
    print(f"[OK] {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
