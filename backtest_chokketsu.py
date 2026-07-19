"""T58 Stage 1: frozen six-candidate evaluation of course-transfer features.

Evaluation-only.  The production ``backtest_ml.FEATURES`` contract and all
production artifacts remain untouched.  The graph and ability.db identities,
feature columns, L2 grid, selection period, and stop rule are preregistered as
``T58-chokketsu-course-graph-v1``.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import (
    PERIODS, _blocks_by_date, _keys_for_mask, _mean_metric, _period_mask,
    _rows_for_mask, race_level_winner_logloss,
)
from backtest_fold_stats import same_population_metrics
from backtest_ml import (
    FEATURES, RECENCY, _time_index_value, fit_conditional_logit, fit_temperature,
)
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_win5 import load_win5_cfg
from eval.blocks import paired_block_bootstrap


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ability.db"
GRAPH_PATH = BASE_DIR / "data" / "t58" / "chokketsu_graph.json"
EXPERIMENT_ID = "T58-chokketsu-course-graph-v1"
ABILITY_SHA256 = "ea6b8e5b989d722658170d2499342f98d82f01c5b875c3570abf26c63112f061"
GRAPH_SHA256 = "217836190d547aa2e31b98fd7c7473786de37d4ceecf2a5c821bd7ff2f23b498"

DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
FINAL_REFIT_TO = "20241231"
L2_GRID = (0.3, 1.0, 3.0)
FEATURE_NAMES = (
    "bp_connect_s", "bp_connect_a", "bp_nonconnect", "bp_same_course",
    "bp_connect_available", "bp_nonconnect_available",
)
CLASS_GROUPS = ("古馬重賞OP", "古馬条件未勝利", "2-3歳限定")
INNER_OUTER_AMBIGUOUS_BASES = frozenset({"京都芝1400", "京都芝1600", "新潟芝2000"})
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 58


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_course_name(value: str) -> str:
    """Project book names to ability.db's venue/surface/distance resolution.

    ability.db has no inner/outer-course column.  This lossy projection is
    fixed before evaluation and is reported as a coverage limitation.
    """
    return str(value or "").strip().removesuffix("内").removesuffix("外")


def run_course(row) -> str:
    surface = "ダ" if str(row.get("track_type") or "") in ("ダ", "ダート") else "芝"
    return f"{row.get('place') or ''}{surface}{int(row.get('distance') or 0)}"


def race_class_group(members) -> str:
    ages = [int(row["age"]) for row in members if row.get("age") is not None]
    if ages and max(ages) <= 3:
        return "2-3歳限定"
    race_class = str(next((row.get("race_class") for row in members
                           if row.get("race_class")), ""))
    if race_class in ("重賞", "オープン"):
        return "古馬重賞OP"
    return "古馬条件未勝利"


def race_group_map(runs) -> dict[tuple, str]:
    grouped = defaultdict(list)
    for row in runs:
        grouped[(row.get("date"), row.get("place"), row.get("r"))].append(row)
    return {key: race_class_group(members) for key, members in grouped.items()}


def load_graph(path: Path) -> tuple[dict, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    graph = {}
    projected_source_duplicates = []
    projected_rank_overlaps = []
    for row in rows:
        target = project_course_name(row["target_course"])
        key = (target, row["class_group"])
        if key in graph:
            raise ValueError(f"course/class collision after projection: {key}")
        s_raw = [project_course_name(value) for value in row["s_rank"]]
        a_raw = [project_course_name(value) for value in row["a_rank"]]
        s_rank = set(s_raw)
        a_all = set(a_raw)
        overlap = sorted(s_rank & a_all)
        if overlap:
            projected_rank_overlaps.append({
                "target_course": target, "class_group": row["class_group"],
                "courses": overlap,
            })
        a_rank = a_all - s_rank
        for rank, raw in (("s_rank", s_raw), ("a_rank", a_raw)):
            for course, count in Counter(raw).items():
                if count > 1:
                    projected_source_duplicates.append({
                        "target_course": target, "class_group": row["class_group"],
                        "rank": rank, "course": course, "count": count,
                    })
        graph[key] = {"s": s_rank, "a": a_rank}
    audit = {
        "raw_rows": len(rows), "projected_rows": len(graph),
        "projected_source_duplicates": projected_source_duplicates,
        "projected_s_a_overlaps_resolved_to_s": projected_rank_overlaps,
        "resolution": "venue + surface + distance; inner/outer suffix collapsed",
    }
    return graph, audit


def chokketsu_values(prior, current, class_group, graph, *,
                     performance_fn=_time_index_value, use_variant=False):
    """Return the fixed six features using only the supplied prior rows."""
    target = run_course(current)
    edge = graph.get((target, class_group))
    buckets = {"s": [], "a": [], "non": [], "same": []}
    for recency_index, row in enumerate(list(prior)[::-1]):
        value = performance_fn(row, use_variant)
        if value is None:
            continue
        weight = RECENCY[min(recency_index, len(RECENCY) - 1)]
        value = float(value) * float(weight)
        source = run_course(row)
        if source == target:
            buckets["same"].append(value)
        elif edge is None:
            continue
        elif source in edge["s"]:
            buckets["s"].append(value)
        elif source in edge["a"]:
            buckets["a"].append(value)
        else:
            buckets["non"].append(value)
    connect_available = bool(buckets["s"] or buckets["a"])
    nonconnect_available = bool(buckets["non"])
    return np.asarray([
        max(buckets["s"], default=0.0),
        max(buckets["a"], default=0.0),
        max(buckets["non"], default=0.0),
        max(buckets["same"], default=0.0),
        float(connect_available), float(nonconnect_available),
    ], dtype=np.float64), float(edge is not None)


def _runner_key(row) -> tuple:
    return (row.get("date"), row.get("place"), row.get("r"),
            row.get("horse"), row.get("umaban"))


def build_chokketsu_features(meta, runs, graph, *, use_variant=False,
                             performance_fn=_time_index_value):
    groups = race_group_map(runs)
    by_horse = defaultdict(list)
    for row in runs:
        if row.get("horse"):
            by_horse[row["horse"]].append(row)
    computed = {}
    for history in by_horse.values():
        history.sort(key=lambda row: (str(row.get("date") or ""),
                                      str(row.get("place") or ""),
                                      int(row.get("r") or 0)))
        for index, current in enumerate(history):
            if current.get("rank") is None or not current.get("date"):
                continue
            prior = history[max(0, index - 4):index]
            if not prior:
                continue
            race_key = (current.get("date"), current.get("place"), current.get("r"))
            values, graph_available = chokketsu_values(
                prior, current, groups[race_key], graph,
                performance_fn=performance_fn, use_variant=use_variant)
            computed[_runner_key(current)] = (values, graph_available, groups[race_key])
    matrix = np.zeros((len(meta), len(FEATURE_NAMES)), dtype=np.float64)
    graph_available = np.zeros(len(meta), dtype=np.float64)
    inner_outer_ambiguous = np.zeros(len(meta), dtype=np.float64)
    class_groups = []
    for index, row in enumerate(meta):
        race_key = (row.get("date"), row.get("place"), row.get("r"))
        class_group = groups[race_key]
        class_groups.append(class_group)
        inner_outer_ambiguous[index] = float(
            run_course(row) in INNER_OUTER_AMBIGUOUS_BASES)
        found = computed.get(_runner_key(row))
        if found is not None:
            matrix[index], graph_available[index], _ = found
    return matrix, graph_available, class_groups, inner_outer_ambiguous


def candidate_features(base, extra, enabled=False):
    return np.column_stack((base, extra)) if enabled else base


def _fit_candidate(base, extra, labels, keys, meta, dates, *, enabled, l2):
    from sklearn.preprocessing import StandardScaler
    X = candidate_features(base, extra, enabled)
    train = _period_mask(dates, TRAIN_FROM, TRAIN_TO)
    selection = _period_mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(X[train])
    weights = fit_conditional_logit(
        scaler.transform(X[train]), labels[train], _keys_for_mask(keys, train), l2=l2)
    scores = scaler.transform(X[selection]) @ weights
    selection_keys = _keys_for_mask(keys, selection)
    selection_meta = _rows_for_mask(meta, selection)
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, selection_meta,
        temperature=temperature)
    return {"config": "+chokketsu_features" if enabled else "baseline",
            "enabled": enabled, "l2": l2, "scaler": scaler, "weights": weights,
            "temperature": temperature, "selection": metrics}


def run_grid(base, extra, labels, keys, meta, dates):
    candidates = [
        _fit_candidate(base, extra, labels, keys, meta, dates,
                       enabled=enabled, l2=l2)
        for enabled in (False, True) for l2 in L2_GRID
    ]
    best = min(candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline = min((row for row in candidates if not row["enabled"]),
                   key=lambda row: row["selection"]["models"]["model"]["logloss"])
    feature_best = min((row for row in candidates if row["enabled"]),
                       key=lambda row: row["selection"]["models"]["model"]["logloss"])
    return candidates, best, baseline, feature_best


def _refit(candidate, base, extra, labels, keys, dates, *, final=True):
    from sklearn.preprocessing import StandardScaler
    X = candidate_features(base, extra, candidate["enabled"])
    mask = _period_mask(dates, TRAIN_FROM, FINAL_REFIT_TO if final else TRAIN_TO)
    scaler = StandardScaler().fit(X[mask])
    weights = fit_conditional_logit(
        scaler.transform(X[mask]), labels[mask], _keys_for_mask(keys, mask),
        l2=candidate["l2"])
    return {"candidate": candidate, "scaler": scaler, "weights": weights,
            "temperature": candidate["temperature"]}


def _score(model, base, extra, labels, keys, meta, dates, date_from, date_to):
    X = candidate_features(base, extra, model["candidate"]["enabled"])
    mask = _period_mask(dates, date_from, date_to)
    scores = model["scaler"].transform(X[mask]) @ model["weights"]
    period_keys = _keys_for_mask(keys, mask)
    period_meta = _rows_for_mask(meta, mask)
    period_labels = labels[mask]
    metrics = same_population_metrics(
        scores, period_labels, period_keys, period_meta,
        temperature=model["temperature"])
    race_ll = race_level_winner_logloss(
        scores, period_labels, period_keys, period_meta,
        temperature=model["temperature"])
    return metrics, race_ll


def _race_context(meta):
    members = defaultdict(list)
    for row in meta:
        members[(row["date"], row["place"], row["r"])].append(row)
    result = {}
    for key, rows in members.items():
        winners = [row for row in rows if row.get("rank") == 1]
        popularity = min((int(row["popularity"]) for row in winners
                          if row.get("popularity")), default=None)
        pop_band = ("1-3" if popularity and popularity <= 3 else
                    "4-8" if popularity and popularity <= 8 else
                    "9+" if popularity else "unknown")
        result[key] = (pop_band, race_class_group(rows))
    return result


def context_breakdown(selected_ll, baseline_ll, meta):
    contexts = _race_context(meta)
    groups = {"winner_popularity": defaultdict(list), "class_group": defaultdict(list)}
    for key in sorted(set(selected_ll) & set(baseline_ll)):
        difference = selected_ll[key] - baseline_ll[key]
        popularity, class_group = contexts[key]
        groups["winner_popularity"][popularity].append(difference)
        groups["class_group"][class_group].append(difference)
    return {
        dimension: {label: {"races": len(values), "mean_logloss_diff": float(np.mean(values))}
                    for label, values in sorted(labels.items())}
        for dimension, labels in groups.items()
    }


def evaluate(candidate, baseline, base, extra, labels, keys, meta, dates):
    selected_final = _refit(candidate, base, extra, labels, keys, dates)
    baseline_final = _refit(baseline, base, extra, labels, keys, dates)
    selected_reference = _refit(candidate, base, extra, labels, keys, dates, final=False)
    report, bootstrap = {}, {}
    for name, (date_from, date_to) in PERIODS.items():
        metrics, selected_ll = _score(
            selected_final, base, extra, labels, keys, meta, dates, date_from, date_to)
        reference, _ = _score(
            selected_reference, base, extra, labels, keys, meta, dates, date_from, date_to)
        baseline_metrics, baseline_ll = _score(
            baseline_final, base, extra, labels, keys, meta, dates, date_from, date_to)
        blocks_s, blocks_b = _blocks_by_date(selected_ll), _blocks_by_date(baseline_ll)
        boot = paired_block_bootstrap(
            _mean_metric, blocks_s, blocks_b,
            n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
        report[name] = {
            "selected_final": metrics["models"],
            "selected_reference": reference["models"],
            "baseline_final": baseline_metrics["models"],
            "selected_minus_baseline_breakdown": context_breakdown(
                selected_ll, baseline_ll, meta),
        }
        bootstrap[name] = {
            "day_blocks": len(blocks_s), "n_races": len(selected_ll),
            "observed_diff": boot.observed_difference,
            "ci_low": boot.ci_low, "ci_high": boot.ci_high,
            "p_value": boot.p_value,
        }
    return report, bootstrap, selected_final


def selection_breakdown(candidate, baseline, base, extra, labels, keys, meta, dates):
    selected = {"candidate": candidate, "scaler": candidate["scaler"],
                "weights": candidate["weights"], "temperature": candidate["temperature"]}
    baseline_model = {"candidate": baseline, "scaler": baseline["scaler"],
                      "weights": baseline["weights"], "temperature": baseline["temperature"]}
    _, selected_ll = _score(selected, base, extra, labels, keys, meta, dates,
                            SELECTION_FROM, SELECTION_TO)
    _, baseline_ll = _score(baseline_model, base, extra, labels, keys, meta, dates,
                            SELECTION_FROM, SELECTION_TO)
    return context_breakdown(selected_ll, baseline_ll, meta)


def coverage_report(extra, graph_available, class_groups, inner_outer_ambiguous, dates):
    periods = {"train_2021_2023": (TRAIN_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    result = {}
    class_groups = np.asarray(class_groups)
    for name, (date_from, date_to) in periods.items():
        mask = _period_mask(dates, date_from, date_to)
        values = extra[mask]
        result[name] = {
            "rows": int(mask.sum()),
            "graph_row_available_rate": float(np.mean(graph_available[mask])),
            "inner_outer_ambiguous_rate": float(np.mean(inner_outer_ambiguous[mask])),
            "connect_available_rate": float(np.mean(values[:, 4])),
            "nonconnect_available_rate": float(np.mean(values[:, 5])),
            "feature_nonzero_rate": {feature: float(np.mean(values[:, index] != 0))
                                     for index, feature in enumerate(FEATURE_NAMES)},
            "feature_std": {feature: float(np.std(values[:, index]))
                            for index, feature in enumerate(FEATURE_NAMES)},
            "class_rows": {group: int(np.sum(class_groups[mask] == group))
                           for group in CLASS_GROUPS},
        }
    return result


def _safe(value):
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--graph", type=Path, default=GRAPH_PATH)
    parser.add_argument("--json-out", type=Path,
                        default=BASE_DIR / "outputs" / "t58_result.json")
    args = parser.parse_args(argv)
    ability_hash, graph_hash = sha256_file(args.db), sha256_file(args.graph)
    if ability_hash != ABILITY_SHA256 or graph_hash != GRAPH_SHA256:
        raise SystemExit(f"[STOP] hash mismatch: ability={ability_hash} graph={graph_hash}")
    graph, graph_audit = load_graph(args.graph)
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(args.db))
    dates = np.asarray([key[0] for key in keys])
    use_variant = cfg["params"]["ability"].get("track_variant", False)
    extra, graph_available, class_groups, inner_outer_ambiguous = build_chokketsu_features(
        meta, runs, graph, use_variant=use_variant)
    del runs
    gc.collect()

    candidates, best, baseline, feature_best = run_grid(
        base, extra, labels, keys, meta, dates)
    historical, bootstrap, selected_model = evaluate(
        best, baseline, base, extra, labels, keys, meta, dates)
    feature_historical, feature_bootstrap, feature_model = evaluate(
        feature_best, baseline, base, extra, labels, keys, meta, dates)
    feature_coefficients = {
        name: float(weight) for name, weight in zip(
            list(FEATURES) + list(FEATURE_NAMES), feature_model["weights"])
        if name in FEATURE_NAMES
    }
    payload = {
        "experiment_id": EXPERIMENT_ID,
        "warning": "historical is a refutation filter only; adoption requires prospective confirmation",
        "ability_db_sha256": ability_hash,
        "chokketsu_graph_json_sha256": graph_hash,
        "graph_audit": graph_audit,
        "feature_definition": {
            "history_window": "four prior starts only",
            "performance": "existing tfeat standardized time difference times RECENCY",
            "same_course_excluded_from_s_a": True,
            "inner_outer_projection": "collapsed because ability.db has no course-layout column",
        },
        "candidates": [
            {"config": row["config"], "l2": row["l2"],
             "selection_2024": row["selection"]["models"]}
            for row in candidates
        ],
        "best": {"config": best["config"], "l2": best["l2"]},
        "baseline_best": {"l2": baseline["l2"]},
        "historical_benchmark": historical,
        "win5_day_paired_bootstrap": bootstrap,
        "best_feature_diagnostic": {
            "config": feature_best["config"], "l2": feature_best["l2"],
            "selection_breakdown": selection_breakdown(
                feature_best, baseline, base, extra, labels, keys, meta, dates),
            "historical_benchmark": feature_historical,
            "win5_day_paired_bootstrap": feature_bootstrap,
            "standardized_coefficients_final_refit": feature_coefficients,
        },
        "coverage": coverage_report(
            extra, graph_available, class_groups, inner_outer_ambiguous, dates),
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(_safe(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    print("===== T58 2024 selection (6 candidates) =====")
    for row in sorted(candidates, key=lambda item: item["selection"]["models"]["model"]["logloss"]):
        metric = row["selection"]["models"]["model"]
        print(f"{row['config']:22s} L2={row['l2']:<3} LL={metric['logloss']:.6f} "
              f"Brier={metric['brier']:.8f}")
    print("best", payload["best"], "feature_best", payload["best_feature_diagnostic"]["l2"])
    for period, value in feature_historical.items():
        selected = value["selected_final"]["model"]
        baseline_metric = value["baseline_final"]["model"]
        print(period, f"feature LL={selected['logloss']:.6f}",
              f"baseline LL={baseline_metric['logloss']:.6f}",
              feature_bootstrap[period])
    print(f"[OK] {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
