"""T80b: post-waku-fix (T80) retrain gate on frozen rolling-origin splits.

SPEC: docs/codex/SPEC-T80b-waku-fix-retrain.md

T80 (2026-09-19) fixed ``past_data_service.calculate_waku`` so training/mining
features use the same JRA frame (waku) allocation as the live scorer
(``api/index.py``).  Before the fix, the two disagreed on 53.6% of
2021-2025 rows for fields with 9+ runners.  The current production model
(``win5_ml_model.json``, T45 candidate, 2026-07-19) was trained under the
*old, buggy* allocation but is scored live with the *corrected* one
(train/serve skew).

This script measures the effect of that one-line fix in isolation.  It does
not search for new features or hyperparameters: there is exactly one
candidate (the current 21-feature conditional logit, retrained on the
corrected waku), evaluated on three frozen rolling-origin splits identical
in spirit to SPEC-T45's train/calibrate/test recipe.

Everything here is evaluation-only: ability.db is opened read-only, the
production artifact is only ever *read*, and no file this script writes is
inside the production artifact directory.  It imports and reuses
``fit_candidate``, ``compare_period``, ``model_document``,
``serialize_factor_snapshot``, ``align_to_reference``, ``sha256_file`` and
``write_json`` from ``backtest_t45_candidate.py``, and ``build_feature_dataset``
/ ``same_population_metrics`` from ``backtest_fold_stats.py``, without
modifying either file.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

import backtest_ml
from backtest_ability import load_runs
from backtest_fold_stats import (_softmax, build_feature_dataset,
                                 same_population_metrics)
from backtest_ml import FEATURES
from backtest_t45_candidate import (align_to_reference, compare_period,
                                    model_document, serialize_factor_snapshot,
                                    sha256_file, validate_output_dir,
                                    write_json, fit_candidate)
from backtest_win5 import load_win5_cfg, parse_final_odds
from eval.blocks import paired_block_bootstrap
from fold_stats import FoldFactorTableProvider


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "ability.db"
PRODUCTION_MODEL = ROOT / "api" / "data_files" / "common" / "win5_ml_model.json"
RULES_V2 = ROOT / "mined_rules_v2.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "t80b"

TRAIN_FROM = "20210101"
STATS_AS_OF = "20251231"  # same factor-table cutoff as T45's candidate bundle

# Rolling-origin splits (SPEC §2-2). "role" documents intent only; the gate
# in SPEC §1 only reads the "fixed_test" (2025) and "confirmation" (2026H1)
# rows -- 2024 is the temperature-adjustment origin reused verbatim from
# fit_candidate's fixed 2021-23/2024 calibration windows (SPEC §0), not a
# decision-bearing fold.
ORIGINS = {
    "A_2024": {
        "train_to": "20231231",
        "eval_from": "20240101",
        "eval_to": "20241231",
        "role": "tuning",
    },
    "B_2025": {
        "train_to": "20241231",
        "eval_from": "20250101",
        "eval_to": "20251231",
        "role": "fixed_test",
    },
    "C_2026H1": {
        "train_to": "20251231",
        "eval_from": "20260101",
        "eval_to": "20260630",
        "role": "confirmation",
    },
}

POPULATIONS = {
    "all_races": {"min_race_no": 1, "min_horses": 1},
    "win5": {"min_race_no": 9, "min_horses": 8},
}

# Gate thresholds (SPEC §1 "gate"). Documented here because gate_pass() is a
# pure, independently-testable function (SPEC §2-7 boundary tests).
GATE_LL_TOLERANCE = 0.0005          # (a)/(d) "同等以上" == fixed <= reference + this
GATE_TOPK_FLOOR_PT = -0.3           # (b) fixed - legacy top-k floor, percentage points
GATE_SIGN_EPS = 0.0005              # (c) sign-consistency epsilon on the paired LL diff
STOP_RULE_POINT_THRESHOLD = 0.001   # SPEC §1 stop_rule point-estimate threshold

N_RESAMPLES = 2000
BOOTSTRAP_SEED = 80  # T80b; independent of the candidate-bundle reproducibility seed


# ─── Waku implementations ──────────────────────────────────────────────────

def _legacy_calculate_waku(umaban, head_count):
    """Pre-T80 frame allocation (api/past_data_service.py before 5b6ef82).

    Kept here only to measure the effect of the T80 fix; never used outside
    this backtest. Adds the remainder two per outer frame (e.g. 16 runners ->
    [1,1,1,1,3,3,3,3]) instead of the JRA rule's one-per-frame remainder.
    """
    if not umaban or not head_count:
        return None
    try:
        u = int(umaban)
        h = int(head_count)
    except (TypeError, ValueError):
        return None
    if h <= 8:
        return u

    waku_sizes = [1] * 8
    rem = h - 8
    for i in range(8, 0, -1):
        if rem > 0:
            waku_sizes[i - 1] += 1
            rem -= 1
        if rem > 0 and i > 1:
            waku_sizes[i - 1] += 1
            rem -= 1

    current_u = 1
    for waku in range(1, 9):
        size = waku_sizes[waku - 1]
        if current_u <= u < current_u + size:
            return waku
        current_u += size
    return None


# Captured once at import time: backtest_ml already imports the T80-fixed
# implementation (``from past_data_service import calculate_waku``), since
# api/past_data_service.py was fixed by T80 before this script existed.
_FIXED_CALCULATE_WAKU = backtest_ml.calculate_waku


@contextmanager
def _waku_impl(kind: str):
    """Monkeypatch backtest_ml's module-global ``calculate_waku`` in place.

    ``build_dataset`` (backtest_ml.py) looks up ``calculate_waku`` as a
    module global at call time, so reassigning ``backtest_ml.calculate_waku``
    changes what it resolves to without touching backtest_ml.py itself.
    """
    if kind not in ("legacy", "fixed"):
        raise ValueError(f"unknown waku impl: {kind!r}")
    original = backtest_ml.calculate_waku
    backtest_ml.calculate_waku = (
        _legacy_calculate_waku if kind == "legacy" else _FIXED_CALCULATE_WAKU
    )
    try:
        yield
    finally:
        backtest_ml.calculate_waku = original


def build_waku_datasets(runs, cfg, provider, data_through):
    """Build the legacy- and fixed-waku feature datasets on the same rows.

    Both use the same leak-free ability-db factor-table ``provider``; only
    the calculate_waku implementation differs, isolating the T80 fix from
    the (already-adopted, T45) ability-db-vs-legacy-CSV question.
    """
    with _waku_impl("legacy"):
        legacy_features, labels, keys, meta = build_feature_dataset(
            runs, cfg, data_through, provider)
    with _waku_impl("fixed"):
        fixed_features, fixed_labels, fixed_keys, fixed_meta = build_feature_dataset(
            runs, cfg, data_through, provider)

    if keys != fixed_keys or not np.array_equal(labels, fixed_labels):
        fixed_features, fixed_labels, fixed_keys, fixed_meta = align_to_reference(
            keys, meta, (fixed_features, fixed_labels, fixed_keys, fixed_meta))
        if keys != fixed_keys or not np.array_equal(labels, fixed_labels):
            raise RuntimeError(
                "legacy/fixed feature datasets diverge in row selection; "
                "the waku fix must only change f_pts values"
            )

    f_idx = FEATURES.index("f_pts")
    differing_rows = int(np.sum(legacy_features[:, f_idx] != fixed_features[:, f_idx]))
    return {
        "legacy_features": legacy_features,
        "fixed_features": fixed_features,
        "labels": labels,
        "race_keys": keys,
        "meta": meta,
        "differing_f_pts_rows": differing_rows,
        "total_rows": len(keys),
    }


# ─── Per-origin fit + comparison ───────────────────────────────────────────

def _model_from_fit(features, labels, race_keys, train_to, tag, data_hash, data_through):
    scaler, coef, temperature, n_samples = fit_candidate(
        features, labels, race_keys, train_to=train_to)
    document = model_document(
        scaler, coef, temperature, n_samples, data_hash, data_through)
    document["meta"]["candidate"] = tag
    return document


def _scores(features, model):
    mean = np.asarray(model["mean"], dtype=float)
    sd = np.asarray(model["sd"], dtype=float)
    coef = np.asarray(model["coef"], dtype=float)
    return ((features - mean) / sd) @ coef


def _ll_metric(rows):
    total_logloss = sum(row["logloss"] for row in rows)
    total_winners = sum(row["winners"] for row in rows)
    if total_winners <= 0:
        raise ValueError("resampled block has no winner events")
    return total_logloss / total_winners


def _population_day_blocks(current_scores, candidate_scores, labels, race_keys, meta,
                           *, current_temperature, candidate_temperature,
                           min_race_no, min_horses, require_complete_field=True):
    """Group per-race winner-logloss contributions by JST event day.

    Mirrors the population filter in same_population_metrics (same race_no /
    min_horses / complete-field / settled-odds rules) so the bootstrap runs
    on exactly the same races as the reported point estimate.
    """
    grouped = {}
    for cs, ds, label, key, row in zip(
        current_scores, candidate_scores, labels, race_keys, meta
    ):
        grouped.setdefault(key, []).append((float(cs), float(ds), int(label), row))

    current_blocks, candidate_blocks = {}, {}
    for key, members in grouped.items():
        race_no = key[2]
        if race_no is None or race_no < min_race_no or len(members) < min_horses:
            continue
        if require_complete_field:
            field_size = max(
                (int(row.get("total_horses") or 0) for _, _, _, row in members),
                default=0)
            if field_size and len(members) != field_size:
                continue
        winners = [i for i, (_, _, label, _row) in enumerate(members) if label == 1]
        if not winners:
            continue
        odds = [
            parse_final_odds(row.get("win_pay"), row.get("rank"))
            for _, _, _, row in members
        ]
        if any(value is None or value <= 1.0 for value in odds):
            continue

        current_probs = _softmax([m[0] for m in members], current_temperature)
        candidate_probs = _softmax([m[1] for m in members], candidate_temperature)
        current_logloss = -sum(
            math.log(max(1e-15, float(current_probs[i]))) for i in winners)
        candidate_logloss = -sum(
            math.log(max(1e-15, float(candidate_probs[i]))) for i in winners)

        block = key[0]
        current_blocks.setdefault(block, []).append(
            {"logloss": current_logloss, "winners": len(winners)})
        candidate_blocks.setdefault(block, []).append(
            {"logloss": candidate_logloss, "winners": len(winners)})
    return current_blocks, candidate_blocks


def paired_ll_bootstrap(legacy_features, fixed_features, labels, race_keys, meta,
                        legacy_model, fixed_model, date_from, date_to,
                        *, min_race_no, min_horses, n_resamples=N_RESAMPLES,
                        seed=BOOTSTRAP_SEED):
    """Paired open-day block bootstrap of (fixed - legacy) winner logloss."""
    dates = np.asarray([key[0] for key in race_keys])
    mask = (dates >= date_from) & (dates <= date_to)
    if not mask.any():
        return None
    legacy_scores = _scores(legacy_features[mask], legacy_model)
    fixed_scores = _scores(fixed_features[mask], fixed_model)
    sub_labels = labels[mask]
    sub_keys = [key for key, keep in zip(race_keys, mask) if keep]
    sub_meta = [row for row, keep in zip(meta, mask) if keep]

    legacy_blocks, fixed_blocks = _population_day_blocks(
        legacy_scores, fixed_scores, sub_labels, sub_keys, sub_meta,
        current_temperature=legacy_model.get("prob_temperature", 1.0),
        candidate_temperature=fixed_model.get("prob_temperature", 1.0),
        min_race_no=min_race_no, min_horses=min_horses,
    )
    if not legacy_blocks or not fixed_blocks:
        return None
    result = paired_block_bootstrap(
        _ll_metric, fixed_blocks, legacy_blocks, n_resamples, seed)
    return dict(result)


def evaluate_origin(name, spec, datasets, production_model, *, n_resamples=N_RESAMPLES):
    legacy_features = datasets["legacy_features"]
    fixed_features = datasets["fixed_features"]
    labels = datasets["labels"]
    race_keys = datasets["race_keys"]
    meta = datasets["meta"]
    train_to = spec["train_to"]
    eval_from, eval_to = spec["eval_from"], spec["eval_to"]

    legacy_model = _model_from_fit(
        legacy_features, labels, race_keys, train_to,
        f"T80b-legacy-{name}", "n/a", train_to)
    fixed_model = _model_from_fit(
        fixed_features, labels, race_keys, train_to,
        f"T80b-fixed-{name}", "n/a", train_to)

    legacy_vs_fixed = compare_period(
        legacy_features, fixed_features, labels, race_keys, meta, meta,
        legacy_model, fixed_model, eval_from, eval_to)

    production_vs_fixed = None
    if production_model is not None:
        production_vs_fixed = compare_period(
            fixed_features, fixed_features, labels, race_keys, meta, meta,
            production_model, fixed_model, eval_from, eval_to)

    populations = {}
    for population, params in POPULATIONS.items():
        bootstrap = paired_ll_bootstrap(
            legacy_features, fixed_features, labels, race_keys, meta,
            legacy_model, fixed_model, eval_from, eval_to,
            min_race_no=params["min_race_no"], min_horses=params["min_horses"],
            n_resamples=n_resamples,
        )
        entry = {
            "legacy_vs_fixed": (
                legacy_vs_fixed.get(population) if legacy_vs_fixed else None
            ),
            "production_vs_fixed": (
                production_vs_fixed.get(population) if production_vs_fixed else None
            ),
            "bootstrap": bootstrap,
        }
        populations[population] = entry

    return {
        "eval_from": eval_from,
        "eval_to": eval_to,
        "role": spec["role"],
        "legacy_model": legacy_model,
        "fixed_model": fixed_model,
        "populations": populations,
    }


# ─── Gate + stop rule (pure functions; SPEC §1 gate / §2-7 boundary tests) ──

def _sign(value, eps=GATE_SIGN_EPS):
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def stop_rule_would_trigger(origin_b_all_races):
    """SPEC §1 stop_rule, evaluated on origin B (2025), all_races population.

    True means: had this been run prospectively, 2026H1 would not have been
    read because 2025 already showed a significant, material regression.
    This function only reports the fact; it makes no adoption decision.
    """
    bootstrap = origin_b_all_races["bootstrap"]
    if bootstrap is None:
        return None
    ci_excludes_zero_below = bootstrap["ci_high"] < 0.0
    material_regression = bootstrap["observed_difference"] > STOP_RULE_POINT_THRESHOLD
    return (not ci_excludes_zero_below) and material_regression


def gate_pass(results):
    """Mechanically apply SPEC §1 gate (a)-(d) to the origin B/C results.

    ``results`` is the ``origins`` mapping produced by ``main()`` (or an
    equivalent dict with "B_2025" and "C_2026H1" keys, each shaped like
    ``evaluate_origin``'s return value). Returns ``(passed, detail)``.
    """
    detail = {}
    b = results["B_2025"]
    c = results["C_2026H1"]

    # (a) all_races LL: fixed <= legacy + tolerance, both periods.
    a_checks = {}
    for period, origin in (("2025", b), ("2026H1", c)):
        pop = origin["populations"]["all_races"]["legacy_vs_fixed"]
        fixed_ll = pop["candidate"]["logloss"]
        legacy_ll = pop["current"]["logloss"]
        a_checks[period] = {
            "fixed_ll": fixed_ll,
            "legacy_ll": legacy_ll,
            "pass": fixed_ll <= legacy_ll + GATE_LL_TOLERANCE,
        }
    gate_a = all(v["pass"] for v in a_checks.values())
    detail["a_ll_all_races"] = {"checks": a_checks, "pass": gate_a}

    # (b) WIN5 top-k (k=1..3) floor: fixed - legacy >= -0.3pt, both periods.
    b_checks = {}
    for period, origin in (("2025", b), ("2026H1", c)):
        pop = origin["populations"]["win5"]["legacy_vs_fixed"]
        fixed_topk = pop["candidate"]["coverage"]
        legacy_topk = pop["current"]["coverage"]
        deltas_pt = [
            (fixed_topk[k] - legacy_topk[k]) * 100.0 for k in range(3)
        ]
        b_checks[period] = {
            "delta_pt_k1_3": deltas_pt,
            "pass": all(delta >= GATE_TOPK_FLOOR_PT - 1e-9 for delta in deltas_pt),
        }
    gate_b = all(v["pass"] for v in b_checks.values())
    detail["b_topk_floor_win5"] = {"checks": b_checks, "pass": gate_b}

    # (c) WIN5 paired LL diff sign-consistent and not worsening, both periods.
    c_checks = {}
    for period, origin in (("2025", b), ("2026H1", c)):
        bootstrap = origin["populations"]["win5"]["bootstrap"]
        diff = bootstrap["observed_difference"] if bootstrap else None
        c_checks[period] = {"diff": diff, "sign": _sign(diff) if diff is not None else None}
    signs = [v["sign"] for v in c_checks.values()]
    gate_c = (
        all(sign is not None for sign in signs)
        and len(set(signs)) == 1
        and signs[0] != 1
    )
    detail["c_win5_sign_consistent"] = {"checks": c_checks, "pass": gate_c}

    # (d) production (skewed) vs fixed candidate, all_races, 2026H1 only.
    d_pop = c["populations"]["all_races"]["production_vs_fixed"]
    if d_pop is None:
        gate_d = False
        d_detail = {"pass": False, "reason": "production model unavailable"}
    else:
        production_ll = d_pop["current"]["logloss"]
        fixed_ll_for_prod = d_pop["candidate"]["logloss"]
        gate_d = fixed_ll_for_prod <= production_ll + GATE_LL_TOLERANCE
        d_detail = {
            "production_ll": production_ll,
            "fixed_ll": fixed_ll_for_prod,
            "pass": gate_d,
        }
    detail["d_production_2026h1"] = d_detail

    passed = bool(gate_a and gate_b and gate_c and gate_d)
    detail["gate_pass"] = passed
    return passed, detail


# ─── Report assembly ───────────────────────────────────────────────────────

def _round_floats(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(v) for v in value]
    return value


def render_markdown(report):
    lines = []
    lines.append("# T80b: waku 修正後 CL 再学習と凍結分割ゲート")
    lines.append("")
    lines.append(f"- ability.db sha256 (実行前): `{report['ability_db_sha256_before']}`")
    lines.append(f"- ability.db sha256 (実行後): `{report['ability_db_sha256_after']}`")
    lines.append(f"- production model sha256: `{report['production_model_sha256']}`")
    lines.append(f"- candidate model sha256 (2回一致): `{report['candidate_model_sha256']}`"
                 f" (match={report['candidate_reproducible']})")
    lines.append(f"- factor snapshot sha256: `{report['factor_snapshot_sha256']}`")
    lines.append(f"- rows: 全{report['total_rows']}行中 f_pts が legacy/fixed で異なる行: "
                 f"{report['differing_f_pts_rows']} "
                 f"({report['differing_f_pts_rate']*100:.1f}%)")
    lines.append("")
    lines.append("## Origin × 母集団 × {legacy, fixed, market, production} の LL / top-k")
    lines.append("")
    lines.append("| origin | population | legacy_ll | fixed_ll | market_ll | "
                 "production_ll | fixed-legacy (95%CI) | topk legacy | topk fixed | topk market |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for origin_name, origin in report["origins"].items():
        for population, entry in origin["populations"].items():
            lf = entry["legacy_vs_fixed"]
            pf = entry["production_vs_fixed"]
            bootstrap = entry["bootstrap"]
            if lf is None:
                continue
            legacy_ll = lf["current"]["logloss"]
            fixed_ll = lf["candidate"]["logloss"]
            market_ll = lf["market"]["logloss"]
            production_ll = pf["current"]["logloss"] if pf else float("nan")
            ci = (f"{bootstrap['observed_difference']:+.6f} "
                  f"[{bootstrap['ci_low']:+.6f}, {bootstrap['ci_high']:+.6f}]"
                  if bootstrap else "n/a")
            topk_legacy = ", ".join(f"{v*100:.2f}" for v in lf["current"]["coverage"][:4])
            topk_fixed = ", ".join(f"{v*100:.2f}" for v in lf["candidate"]["coverage"][:4])
            topk_market = ", ".join(f"{v*100:.2f}" for v in lf["market"]["coverage"][:4])
            lines.append(
                f"| {origin_name} ({origin['eval_from']}-{origin['eval_to']}) | {population} | "
                f"{legacy_ll:.6f} | {fixed_ll:.6f} | {market_ll:.6f} | {production_ll:.6f} | "
                f"{ci} | {topk_legacy} | {topk_fixed} | {topk_market} |"
            )
    lines.append("")
    lines.append(f"- races/horses per origin×population: see `outputs/t80b/report.json`.")
    lines.append("")
    lines.append("## ゲート判定 (機械適用)")
    lines.append("")
    lines.append(f"`gate_pass = {report['gate_pass']}`")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(_round_floats(report["gate_detail"]), ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append(
        f"stop_rule_would_trigger (2025, all_races, informational only): "
        f"`{report['stop_rule_would_trigger']}`"
    )
    lines.append("")
    lines.append("## 実行情報")
    lines.append("")
    lines.append(f"- 実行時間: {report['elapsed_seconds']:.1f}秒")
    lines.append(f"- テスト: {report.get('test_summary', 'pytest tests/test_t80b_waku_retrain.py')}")
    lines.append("")
    lines.append(
        "採否の記述はしない (上位モデルが裁定)。本ファイルは SPEC-T80b §2-5 の"
        "報告物であり、gate_pass は機械適用結果に過ぎない。"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# ─── Candidate bundle reproducibility (origin C; T45-style) ────────────────

def build_candidate_bundle(fixed_features, labels, race_keys, provider, db_hash,
                           data_through, output_dir, *, seed):
    np.random.seed(seed)
    scaler, coef, temperature, n_samples = fit_candidate(fixed_features, labels, race_keys)
    document = model_document(scaler, coef, temperature, n_samples, db_hash, data_through)
    document["meta"]["candidate"] = "T80b-waku-fix-retrain-v1"
    model_path = output_dir / "win5_ml_model_candidate.json"
    model_hash = write_json(model_path, document)
    factor_path = output_dir / "ability_factor_snapshot_20251231.json"
    factor_hash = write_json(factor_path, serialize_factor_snapshot(provider))
    return model_hash, factor_hash


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=45)
    parser.add_argument("--n-resamples", type=int, default=N_RESAMPLES)
    args = parser.parse_args(argv)

    import time
    started = time.monotonic()

    output_dir = validate_output_dir(args.output_dir)
    db_ro_uri = f"file:{args.db.resolve()}?mode=ro"
    db_hash_before = sha256_file(args.db)

    with sqlite3.connect(db_ro_uri, uri=True) as connection:
        data_through = connection.execute(
            "SELECT max(date) FROM runs").fetchone()[0]
        runs = load_runs(connection, TRAIN_FROM, data_through)

    cfg = load_win5_cfg()
    provider = FoldFactorTableProvider(
        args.db, STATS_AS_OF,
        pedigree_cache_path=ROOT / "pedigree_cache.json",
        legacy_api_dir=ROOT / "api",
    )
    datasets = build_waku_datasets(runs, cfg, provider, data_through)

    production_model = None
    production_model_sha256 = None
    if PRODUCTION_MODEL.exists():
        production_model = json.loads(PRODUCTION_MODEL.read_text(encoding="utf-8"))
        production_model_sha256 = sha256_file(PRODUCTION_MODEL)

    origins = {
        name: evaluate_origin(name, spec, datasets, production_model,
                              n_resamples=args.n_resamples)
        for name, spec in ORIGINS.items()
    }

    passed, gate_detail = gate_pass(origins)
    stop_rule = stop_rule_would_trigger(origins["B_2025"]["populations"]["all_races"])

    model_hash_1, factor_hash_1 = build_candidate_bundle(
        datasets["fixed_features"], datasets["labels"], datasets["race_keys"],
        provider, db_hash_before, data_through, output_dir, seed=args.seed)
    model_hash_2, factor_hash_2 = build_candidate_bundle(
        datasets["fixed_features"], datasets["labels"], datasets["race_keys"],
        provider, db_hash_before, data_through, output_dir, seed=args.seed)
    candidate_reproducible = (model_hash_1 == model_hash_2 and factor_hash_1 == factor_hash_2)

    rules_path = output_dir / "mined_rules_v2.csv"
    shutil.copy2(RULES_V2, rules_path)

    db_hash_after = sha256_file(args.db)

    elapsed = time.monotonic() - started

    report = {
        "experiment_id": "T80b-waku-fix-retrain-v1",
        "ability_db_sha256_before": db_hash_before,
        "ability_db_sha256_after": db_hash_after,
        "data_through": data_through,
        "production_model_sha256": production_model_sha256,
        "candidate_model_sha256": model_hash_2,
        "candidate_reproducible": candidate_reproducible,
        "factor_snapshot_sha256": factor_hash_2,
        "mined_rules_v2_sha256": sha256_file(rules_path),
        "total_rows": datasets["total_rows"],
        "differing_f_pts_rows": datasets["differing_f_pts_rows"],
        "differing_f_pts_rate": (
            datasets["differing_f_pts_rows"] / datasets["total_rows"]
            if datasets["total_rows"] else 0.0
        ),
        "origins": origins,
        "gate_pass": passed,
        "gate_detail": gate_detail,
        "stop_rule_would_trigger": stop_rule,
        "elapsed_seconds": elapsed,
    }
    report_hash = write_json(output_dir / "report.json", report)

    print(f"legacy/fixed differing f_pts rows: {datasets['differing_f_pts_rows']}"
          f"/{datasets['total_rows']} ({report['differing_f_pts_rate']*100:.1f}%)")
    for name, origin in origins.items():
        for population, entry in origin["populations"].items():
            lf = entry["legacy_vs_fixed"]
            if lf is None:
                continue
            bootstrap = entry["bootstrap"]
            diff = bootstrap["observed_difference"] if bootstrap else float("nan")
            print(f"{name} {population}: n={lf['races']} legacy_ll={lf['current']['logloss']:.6f} "
                  f"fixed_ll={lf['candidate']['logloss']:.6f} "
                  f"market_ll={lf['market']['logloss']:.6f} diff(fixed-legacy)={diff:+.6f}")
    print(f"gate_pass={passed}")
    print(f"candidate model sha256={model_hash_2} reproducible={candidate_reproducible}")
    print(f"report sha256={report_hash}")
    print(f"ability.db sha256 before={db_hash_before} after={db_hash_after} "
          f"unchanged={db_hash_before == db_hash_after}")
    print(f"elapsed={elapsed:.1f}s")

    markdown_path = ROOT / "docs" / "T80b-waku-retrain-report.md"
    markdown_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(f"markdown report: {markdown_path}")
    return report


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
