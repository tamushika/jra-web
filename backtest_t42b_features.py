"""SPEC-T42b Stage B: frozen training-feature evaluation.

The harness is read-only for ability.db and the T42 cache/store.  It evaluates
exactly the seven configurations sealed in T39 and never writes production
artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import race_level_winner_logloss
from backtest_fold_stats import same_population_metrics
from backtest_ml import FEATURES, fit_conditional_logit, fit_temperature
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_t54d_sectional_features import market_topk_floor, popularity_increment
from backtest_win5 import load_win5_cfg
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
EXPERIMENT_ID = "T42b-training-features-v1"
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_TRAINING_DB = ROOT / "data" / "t42" / "t42_training.sqlite"
DEFAULT_MANIFEST = ROOT / "data" / "t42" / "manifest.sqlite"
DEFAULT_SPEC = ROOT / "docs" / "codex" / "SPEC-T42b-training-cache-features.md"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "t42b_result.json"
ABILITY_SHA256 = "bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a"
TRAINING_DB_SHA256 = "f13be0829b4379f66e5ffc8480ba6b276b2eaa6c94a574289e727604ebb6115f"
MANIFEST_SHA256 = "982c73fc0898a47c28b74e5520c68154feacd344fe10558b0ab2a134b6f6d8b4"
SPEC_SHA256 = "e1d9e7221cc7bc73f3571626fdd8e332fd0302a686843d74aeb4af2d13b3989c"

DATA_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
DATA_TO = "20260630"
PERIODS = {"2025": ("20250101", "20251231"),
           "2026H1": ("20260101", "20260630")}
BASE_L2 = 1.0
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 4220

FEATURE_NAMES = (
    "F1_final_time_z", "F2_finish_bite", "F3_workout_count",
    "F4_days_since_final", "F5_intensity_share", "F6_zero_workout",
)
FLAG_NAMES = tuple(f"{name}_missing" for name in FEATURE_NAMES)
ALL_TRAINING_COLUMNS = tuple(
    item for pair in zip(FEATURE_NAMES, FLAG_NAMES) for item in pair)
CONFIGS = {
    "baseline+F1_final_time_z": (0,),
    "baseline+F2_finish_bite": (1,),
    "baseline+F3_workout_count": (2,),
    "baseline+F4_days_since_final": (3,),
    "baseline+F5_intensity_share": (4,),
    "baseline+F6_zero_workout": (5,),
    "baseline+all6": tuple(range(6)),
}


@dataclass(frozen=True)
class Preliminary:
    race_date: str
    course: str | None
    workout_date: str | None
    month: str | None
    main_time: float | None
    finish_bite: float | None
    workout_count: int
    days_since_final: int | None
    intensity_share: float
    zero_workout: float
    has_rows: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_registration(ledger_path=DEFAULT_LEDGER, db_path=DEFAULT_DB,
                         training_db=DEFAULT_TRAINING_DB,
                         manifest_path=DEFAULT_MANIFEST, spec_path=DEFAULT_SPEC) -> dict:
    actual = {
        "ability_db_sha256": sha256_file(Path(db_path)),
        "t42b_structured_db_sha256": sha256_file(Path(training_db)),
        "t42a_manifest_db_sha256": sha256_file(Path(manifest_path)),
        "spec_sha256": sha256_file(Path(spec_path)),
    }
    expected_files = {
        "ability_db_sha256": ABILITY_SHA256,
        "t42b_structured_db_sha256": TRAINING_DB_SHA256,
        "t42a_manifest_db_sha256": MANIFEST_SHA256,
        "spec_sha256": SPEC_SHA256,
    }
    if actual != expected_files:
        raise RuntimeError(f"T42b sealed input SHA mismatch: {actual}")
    registration = None
    for line in Path(ledger_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("experiment_id") == EXPERIMENT_ID and row.get("result_summary") is None:
            registration = row
    if registration is None:
        raise RuntimeError(f"T42b evaluation blocked: register {EXPERIMENT_ID} first")
    if registration.get("candidate_count") != 7:
        raise RuntimeError("T39 registration does not seal seven candidates")
    if registration.get("data_hashes") != actual:
        raise RuntimeError("T39 registration input hashes do not match sealed files")
    if registration.get("search_grid", {}).get("configs") != list(CONFIGS):
        raise RuntimeError("T39 registration config list mismatch")
    return registration


def _parse_json_numbers(payload: str) -> list[float]:
    values = json.loads(payload or "[]")
    return [float(value) for value in values]


def _days_between(date8: str, iso_date: str) -> int:
    from datetime import date
    race = date(int(date8[:4]), int(date8[4:6]), int(date8[6:8]))
    workout = date.fromisoformat(iso_date)
    return (race - workout).days


def _finalize_group(race_date: str, rows: list[sqlite3.Row]) -> Preliminary:
    timed = [row for row in rows if int(row["lap_count"] or 0) > 0
             and row["training_date"]]
    # Same-day ties use the first displayed row (smallest immutable row_index).
    final = max(timed, key=lambda row: (str(row["training_date"]),
                                        -int(row["row_index"]))) if timed else None
    times = _parse_json_numbers(final["times_json"]) if final else []
    main_time = times[0] if times else None
    finish_bite = (times[-1] - times[0] / len(times)) if times else None
    strong = sum(str(row["intensity_norm"] or "") in {"一杯", "強め"} for row in rows)
    return Preliminary(
        race_date=race_date,
        course=str(final["course_norm"] or "") if final else None,
        workout_date=str(final["training_date"]) if final else None,
        month=str(final["training_date"])[:7] if final else None,
        main_time=main_time, finish_bite=finish_bite,
        workout_count=len(timed),
        days_since_final=_days_between(race_date, str(final["training_date"])) if final else None,
        intensity_share=strong / len(rows) if rows else 0.0,
        zero_workout=float(bool(rows) and not timed), has_rows=bool(rows),
    )


def load_preliminary(training_db=DEFAULT_TRAINING_DB) -> dict[tuple, Preliminary]:
    connection = sqlite3.connect(
        f"file:{Path(training_db).resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""SELECT m.date8,m.place,m.race_no,t.race_id,
            t.horse_name,t.row_index,t.training_date,t.course_norm,t.times_json,
            t.lap_count,t.intensity_norm
            FROM race_training_rows t JOIN race_id_map m ON m.race_id=t.race_id
            ORDER BY t.race_id,t.horse_name,t.row_index""")
        output, group, group_key, race_date = {}, [], None, None
        for row in rows:
            key = (row["race_id"], row["horse_name"])
            if group_key is not None and key != group_key:
                previous = _finalize_group(race_date, group)
                map_key = (race_date, group[0]["place"], int(group[0]["race_no"]),
                           str(group[0]["horse_name"]))
                output[map_key] = previous
                group = []
            group_key, race_date = key, str(row["date8"])
            group.append(row)
        if group:
            previous = _finalize_group(race_date, group)
            map_key = (race_date, group[0]["place"], int(group[0]["race_no"]),
                       str(group[0]["horse_name"]))
            output[map_key] = previous
        return output
    finally:
        connection.close()


def _mean_std(values):
    array = np.asarray(values, dtype=float)
    return float(array.mean()), float(array.std())


def compute_training_matrix(preliminary: dict[tuple, Preliminary], keys, meta):
    exact, monthly = defaultdict(list), defaultdict(list)
    for row in preliminary.values():
        if row.main_time is not None and row.course and row.workout_date:
            exact[(row.course, row.workout_date)].append(row.main_time)
            monthly[(row.course, row.month)].append(row.main_time)
    exact_stats = {key: _mean_std(values) for key, values in exact.items()}
    monthly_stats = {key: _mean_std(values) for key, values in monthly.items()}
    matrix = np.full((len(meta), len(ALL_TRAINING_COLUMNS)), np.nan, dtype=float)
    audit = defaultdict(int)
    for index, (race_key, runner) in enumerate(zip(keys, meta)):
        lookup = (str(race_key[0]), str(race_key[1]), int(race_key[2]),
                  str(runner.get("horse") or ""))
        row = preliminary.get(lookup)
        missing = [True] * 6
        values = [np.nan, np.nan, 0.0, np.nan, 0.0, 0.0]
        if row is not None:
            missing[2] = missing[4] = missing[5] = False
            values[2] = float(row.workout_count)
            values[4] = float(row.intensity_share)
            values[5] = float(row.zero_workout)
            if row.main_time is not None and row.course and row.workout_date:
                stats = exact_stats[(row.course, row.workout_date)]
                if len(exact[(row.course, row.workout_date)]) < 5:
                    stats = monthly_stats.get((row.course, row.month), stats)
                    audit["f1_month_fallback"] += 1
                mean, scale = stats
                values[0] = 0.0 if scale == 0.0 else (row.main_time - mean) / scale
                missing[0] = False
            if row.finish_bite is not None:
                values[1], missing[1] = row.finish_bite, False
            if row.days_since_final is not None:
                values[3], missing[3] = float(row.days_since_final), False
        else:
            audit["race_horse_unmatched"] += 1
        for feature_index, value in enumerate(values):
            matrix[index, feature_index * 2] = value
            matrix[index, feature_index * 2 + 1] = float(missing[feature_index])
    return matrix, dict(sorted(audit.items()))


def fit_imputation(matrix: np.ndarray, train_mask: np.ndarray) -> dict[str, float]:
    train = matrix[train_mask]
    f2 = train[:, 2]
    f4 = train[:, 6]
    return {
        "F1_final_time_z": 0.0,
        "F2_finish_bite": float(np.nanmean(f2)),
        "F3_workout_count": 0.0,
        "F4_days_since_final": float(np.nanmedian(f4)),
        "F5_intensity_share": 0.0,
        "F6_zero_workout": 0.0,
    }


def apply_imputation(matrix: np.ndarray, values: dict[str, float]) -> np.ndarray:
    result = np.asarray(matrix, dtype=float).copy()
    for index, feature in enumerate(FEATURE_NAMES):
        column = index * 2
        missing = np.isnan(result[:, column])
        result[missing, column] = values[feature]
    if not np.all(np.isfinite(result)):
        raise ValueError("T42b matrix remains non-finite after imputation")
    return result


def config_matrix(base: np.ndarray, training: np.ndarray, config: str):
    if config == "baseline":
        return base, list(FEATURES)
    indexes = CONFIGS[config]
    columns = [column for index in indexes for column in (index * 2, index * 2 + 1)]
    names = list(FEATURES) + [ALL_TRAINING_COLUMNS[column] for column in columns]
    return np.column_stack((base, training[:, columns])), names


def _mask(dates, start, end):
    return (dates >= start) & (dates <= end)


def _selected(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def _fit(base, training, labels, keys, meta, dates, config):
    from sklearn.preprocessing import StandardScaler
    train = _mask(dates, DATA_FROM, TRAIN_TO)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    imputation = fit_imputation(training, train)
    filled = apply_imputation(training, imputation)
    matrix, names = config_matrix(base, filled, config)
    scaler = StandardScaler().fit(matrix[train])
    weights = fit_conditional_logit(
        scaler.transform(matrix[train]), labels[train], _selected(keys, train), l2=BASE_L2)
    selection_keys, selection_meta = _selected(keys, selection), _selected(meta, selection)
    scores = scaler.transform(matrix[selection]) @ weights
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, selection_meta, temperature=temperature)
    metrics.pop("sample_signature", None)
    losses = race_level_winner_logloss(
        scores, labels[selection], selection_keys, selection_meta, temperature=temperature)
    return {"config": config, "names": names, "imputation": imputation,
            "training": filled, "scaler": scaler, "weights": weights,
            "temperature": temperature, "selection": metrics,
            "selection_losses": losses}


def _refit(base, labels, keys, dates, candidate):
    from sklearn.preprocessing import StandardScaler
    fit = _mask(dates, DATA_FROM, SELECTION_TO)
    matrix, names = config_matrix(base, candidate["training"], candidate["config"])
    scaler = StandardScaler().fit(matrix[fit])
    weights = fit_conditional_logit(
        scaler.transform(matrix[fit]), labels[fit], _selected(keys, fit), l2=BASE_L2)
    return {"config": candidate["config"], "names": names, "training": candidate["training"],
            "scaler": scaler, "weights": weights, "temperature": candidate["temperature"]}


def _score(model, base, labels, keys, meta, dates, start, end):
    period = _mask(dates, start, end)
    matrix, _ = config_matrix(base, model["training"], model["config"])
    scores = model["scaler"].transform(matrix[period]) @ model["weights"]
    period_keys, period_meta = _selected(keys, period), _selected(meta, period)
    metrics = same_population_metrics(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    signature = metrics.pop("sample_signature")
    losses = race_level_winner_logloss(
        scores, labels[period], period_keys, period_meta, temperature=model["temperature"])
    return metrics, signature, losses


def _paired(candidate, baseline, seed):
    if set(candidate) != set(baseline):
        raise RuntimeError("T42b paired populations differ")
    left, right = defaultdict(list), defaultdict(list)
    for key in sorted(candidate):
        left[key[0]].append(candidate[key])
        right[key[0]].append(baseline[key])
    result = paired_block_bootstrap(
        lambda rows: sum(rows) / len(rows), left, right, BOOTSTRAP_RESAMPLES, seed)
    return {"difference": result.observed_difference,
            "ci95": [result.ci_low, result.ci_high], "p_value": result.p_value,
            "races": len(candidate), "day_blocks": len(left),
            "resamples": result.n_resamples, "seed": result.seed}


def _floor_vs_baseline(candidate_metrics, baseline_metrics):
    candidate = candidate_metrics["models"]["model"]["coverage"]
    baseline = baseline_metrics["models"]["model"]["coverage"]
    gaps = [float(left - right) for left, right in zip(candidate, baseline)]
    return {"candidate_minus_baseline": gaps,
            "no_degradation_all_k": all(value >= 0.0 for value in gaps),
            "candidate_vs_market": market_topk_floor(candidate_metrics),
            "baseline_vs_market": market_topk_floor(baseline_metrics)}


def coverage_report(matrix, dates):
    output = {}
    periods = {"train_2021_2023": (DATA_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    for period, (start, end) in periods.items():
        rows = matrix[_mask(dates, start, end)]
        output[period] = {
            feature: {"missing": int(rows[:, index * 2 + 1].sum()),
                      "missing_rate": float(rows[:, index * 2 + 1].mean())}
            for index, feature in enumerate(FEATURE_NAMES)}
    return output


def missing_flag_coefficients(candidate):
    values = {}
    for name, coefficient in zip(candidate["names"], candidate["weights"]):
        if name in FLAG_NAMES:
            values[name] = float(coefficient)
    return values


def run_evaluation(db_path=DEFAULT_DB, training_db=DEFAULT_TRAINING_DB,
                   ledger_path=DEFAULT_LEDGER, manifest_path=DEFAULT_MANIFEST,
                   spec_path=DEFAULT_SPEC):
    registration = require_registration(
        ledger_path, db_path, training_db, manifest_path, spec_path)
    with sqlite3.connect(
            f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, DATA_FROM, DATA_TO)
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(db_path))
    dates = np.asarray([str(key[0]) for key in keys])
    preliminary = load_preliminary(training_db)
    training, source_audit = compute_training_matrix(preliminary, keys, meta)
    base_sha = hashlib.sha256(base.tobytes()).hexdigest()
    candidates = [_fit(base, training, labels, keys, meta, dates, config)
                  for config in CONFIGS]
    baseline_training = apply_imputation(training, fit_imputation(
        training, _mask(dates, DATA_FROM, TRAIN_TO)))
    # Baseline is fit separately but does not count as an eighth candidate.
    baseline_config = "baseline+F1_final_time_z"
    from sklearn.preprocessing import StandardScaler
    train, selection = (_mask(dates, DATA_FROM, TRAIN_TO),
                        _mask(dates, SELECTION_FROM, SELECTION_TO))
    baseline_scaler = StandardScaler().fit(base[train])
    baseline_weights = fit_conditional_logit(
        baseline_scaler.transform(base[train]), labels[train], _selected(keys, train),
        l2=BASE_L2)
    baseline_scores = baseline_scaler.transform(base[selection]) @ baseline_weights
    baseline_temperature = fit_temperature(
        baseline_scores, labels[selection], _selected(keys, selection))
    baseline_metrics = same_population_metrics(
        baseline_scores, labels[selection], _selected(keys, selection),
        _selected(meta, selection), temperature=baseline_temperature)
    baseline_metrics.pop("sample_signature", None)
    baseline_losses = race_level_winner_logloss(
        baseline_scores, labels[selection], _selected(keys, selection),
        _selected(meta, selection), temperature=baseline_temperature)
    baseline_candidate = {"config": "baseline", "names": list(FEATURES),
                          "training": baseline_training, "scaler": baseline_scaler,
                          "weights": baseline_weights, "temperature": baseline_temperature,
                          "selection": baseline_metrics, "selection_losses": baseline_losses}
    selected = min(candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"])
    selected_final, baseline_final = _refit(base, labels, keys, dates, selected), None
    fit = _mask(dates, DATA_FROM, SELECTION_TO)
    baseline_final_scaler = StandardScaler().fit(base[fit])
    baseline_final_weights = fit_conditional_logit(
        baseline_final_scaler.transform(base[fit]), labels[fit], _selected(keys, fit),
        l2=BASE_L2)
    baseline_final = {**baseline_candidate, "scaler": baseline_final_scaler,
                      "weights": baseline_final_weights}
    paired = {"2024_selection": _paired(
        selected["selection_losses"], baseline_losses, BOOTSTRAP_SEED)}
    floors = {"2024_selection": _floor_vs_baseline(
        selected["selection"], baseline_metrics)}
    popularity = {"2024_selection": popularity_increment(
        selected["selection_losses"], baseline_losses,
        [row for row in meta if SELECTION_FROM <= str(row.get("date")) <= SELECTION_TO])}
    historical = {}
    for index, (period, (start, end)) in enumerate(PERIODS.items(), 1):
        selected_metrics, signature, selected_losses = _score(
            selected_final, base, labels, keys, meta, dates, start, end)
        baseline_period, baseline_signature, baseline_period_losses = _score(
            baseline_final, base, labels, keys, meta, dates, start, end)
        if signature != baseline_signature:
            raise RuntimeError(f"{period}: selected/baseline populations differ")
        historical[period] = {"selected": selected_metrics, "baseline": baseline_period}
        paired[period] = _paired(
            selected_losses, baseline_period_losses, BOOTSTRAP_SEED + index)
        floors[period] = _floor_vs_baseline(selected_metrics, baseline_period)
        popularity[period] = popularity_increment(
            selected_losses, baseline_period_losses,
            [row for row in meta if start <= str(row.get("date")) <= end])
    return {
        "spec": "T42b-stage-b", "experiment_id": EXPERIMENT_ID,
        "registration": registration, "base_l2_fixed": BASE_L2,
        "candidate_count": len(candidates), "fixed_configs": list(CONFIGS),
        "input_hashes": registration["data_hashes"], "pack_off_base_sha256": base_sha,
        "source_audit": source_audit, "coverage": coverage_report(training, dates),
        "candidates": [{"config": row["config"], "selection_2024": row["selection"],
                        "paired_vs_baseline": _paired(
                            row["selection_losses"], baseline_losses,
                            BOOTSTRAP_SEED + 10 + index),
                        "floor_vs_baseline": _floor_vs_baseline(
                            row["selection"], baseline_metrics)}
                       for index, row in enumerate(candidates)],
        "baseline_selection_2024": baseline_metrics,
        "selected_config": selected["config"],
        "selected_missing_flag_coefficients_train_2021_2023":
            missing_flag_coefficients(selected),
        "selected_missing_flag_coefficients_refit_2021_2024":
            missing_flag_coefficients(selected_final),
        "historical_benchmark": historical, "win5_day_paired": paired,
        "market_topk_floor": floors, "popularity_band_increment": popularity,
        "adjudication": None,
    }


def write_json(payload, path):
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--training-db", type=Path, default=DEFAULT_TRAINING_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = run_evaluation(args.db, args.training_db, args.ledger,
                            args.manifest, args.spec)
    print(write_json(report, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
