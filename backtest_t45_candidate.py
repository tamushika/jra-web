"""T45: build and evaluate an ability-as-of candidate without production writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.preprocessing import StandardScaler

from backtest_ability import load_runs
from backtest_fold_stats import build_feature_dataset, same_population_metrics
from backtest_ml import FEATURES, fit_conditional_logit, fit_temperature
from backtest_win5 import load_win5_cfg
from fold_stats import FoldFactorTableProvider


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "ability.db"
PRODUCTION_MODEL = ROOT / "api" / "data_files" / "common" / "win5_ml_model.json"
RULES_V2 = ROOT / "mined_rules_v2.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "t45"
TRAIN_FROM = "20210101"
FINAL_TRAIN_TO = "20251231"
PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
    "2026-07": ("20260701", "20260731"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sha256_file(path)


def validate_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    production_parent = PRODUCTION_MODEL.resolve().parent
    if resolved == production_parent or production_parent in resolved.parents:
        raise ValueError("candidate output must not be inside the production artifact directory")
    return resolved


def fit_candidate(features, labels, race_keys, train_to=FINAL_TRAIN_TO):
    dates = np.asarray([key[0] for key in race_keys])
    train = dates <= train_to
    if not train.any():
        raise RuntimeError("candidate training sample is empty")
    scaler = StandardScaler().fit(features[train])
    standardized = scaler.transform(features[train])
    train_keys = [key for key, keep in zip(race_keys, train) if keep]
    coef = fit_conditional_logit(standardized, labels[train], train_keys)

    calibration_train = dates <= "20241231"
    inner = dates[calibration_train] <= "20231231"
    calibration = ~inner
    calibration_features = features[calibration_train]
    calibration_labels = labels[calibration_train]
    calibration_keys_all = [
        key for key, keep in zip(race_keys, calibration_train) if keep
    ]
    calibration_scaler = StandardScaler().fit(calibration_features)
    calibration_z = calibration_scaler.transform(calibration_features)
    inner_keys = [
        key for key, keep in zip(calibration_keys_all, inner) if keep
    ]
    calibration_keys = [
        key for key, keep in zip(calibration_keys_all, calibration) if keep
    ]
    inner_coef = fit_conditional_logit(
        calibration_z[inner], calibration_labels[inner], inner_keys
    )
    temperature = fit_temperature(
        calibration_z[calibration] @ inner_coef,
        calibration_labels[calibration],
        calibration_keys,
    )
    return scaler, coef, float(temperature), int(train.sum())


def model_document(scaler, coef, temperature, n_samples, data_hash, data_through):
    return {
        "meta": {
            "candidate": "T45-ability-asof-v1",
            "data_sha256": data_hash,
            "data_through": data_through,
            "train_period": f"{TRAIN_FROM}-{FINAL_TRAIN_TO}",
            "n_samples": n_samples,
            "stats_source": "ability yearly as-of; live snapshot <=2025-12-31",
            "rules": "mined_rules_v2 (not consumed by current 21-feature CL)",
        },
        "objective": "conditional_logit",
        "features": list(FEATURES),
        "mean": [round(float(value), 6) for value in scaler.mean_],
        "sd": [round(float(value), 6) for value in scaler.scale_],
        "coef": [round(float(value), 6) for value in coef],
        "display_scale": 10.0,
        "prob_temperature": round(float(temperature), 6),
    }


def _slice(values, mask):
    if isinstance(values, np.ndarray):
        return values[mask]
    return [value for value, keep in zip(values, mask) if keep]


def align_to_reference(reference_keys, reference_meta, other):
    """Align another feature dataset by immutable runner identity."""
    features, labels, keys, meta = other

    def identity(key, row):
        return (
            tuple(key),
            str(row.get("umaban") or ""),
            str(row.get("horse") or ""),
        )

    identities = [identity(key, row) for key, row in zip(keys, meta)]
    if len(identities) != len(set(identities)):
        raise RuntimeError("comparison dataset has duplicate runner identities")
    positions = {value: index for index, value in enumerate(identities)}
    reference = [
        identity(key, row) for key, row in zip(reference_keys, reference_meta)
    ]
    missing = [value for value in reference if value not in positions]
    extra = set(positions) - set(reference)
    if missing or extra:
        raise RuntimeError(
            "ability/current feature datasets use different samples: "
            f"missing={len(missing)} extra={len(extra)}"
        )
    order = np.asarray([positions[value] for value in reference], dtype=int)
    return features[order], labels[order], [keys[index] for index in order], [
        meta[index] for index in order
    ]


def compare_period(
    current_features,
    ability_features,
    labels,
    race_keys,
    current_meta,
    ability_meta,
    current_model,
    candidate_model,
    date_from,
    date_to,
):
    dates = np.asarray([key[0] for key in race_keys])
    mask = (dates >= date_from) & (dates <= date_to)
    if not mask.any():
        return None
    current_scores = (
        (current_features[mask] - np.asarray(current_model["mean"]))
        / np.asarray(current_model["sd"])
    ) @ np.asarray(current_model["coef"])
    candidate_scores = (
        (ability_features[mask] - np.asarray(candidate_model["mean"]))
        / np.asarray(candidate_model["sd"])
    ) @ np.asarray(candidate_model["coef"])
    keys = _slice(race_keys, mask)
    period_labels = labels[mask]
    comparisons = {}
    for population, min_race, min_horses in (
        ("all_races", 1, 1),
        ("win5", 9, 8),
    ):
        current = same_population_metrics(
            current_scores,
            period_labels,
            keys,
            _slice(current_meta, mask),
            temperature=current_model.get("prob_temperature", 1.0),
            min_race_no=min_race,
            min_horses=min_horses,
        )
        candidate = same_population_metrics(
            candidate_scores,
            period_labels,
            keys,
            _slice(ability_meta, mask),
            temperature=candidate_model.get("prob_temperature", 1.0),
            min_race_no=min_race,
            min_horses=min_horses,
        )
        if current["sample_signature"] != candidate["sample_signature"]:
            raise RuntimeError(f"candidate/current population mismatch: {population}")
        if current["models"]["market"] != candidate["models"]["market"]:
            raise RuntimeError(f"market baseline mismatch: {population}")
        comparisons[population] = {
            "races": current["races"],
            "horses": current["horses"],
            "current": current["models"]["model"],
            "candidate": candidate["models"]["model"],
            "market": current["models"]["market"],
            "skipped": current["skipped"],
        }
    return comparisons


def serialize_factor_snapshot(provider):
    return {
        "meta": {
            "as_of": provider.as_of,
            "stats_from": provider.stats_from,
            "course_count": provider.course_count,
        },
        "courses": {
            f"{place}|{surface}|{distance}": table
            for (place, surface, distance), table in sorted(provider.tables.items())
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=45)
    args = parser.parse_args(argv)
    output_dir = validate_output_dir(args.output_dir)
    np.random.seed(args.seed)
    db_hash = sha256_file(args.db)
    with sqlite3.connect(args.db) as connection:
        data_through = connection.execute("SELECT max(date) FROM runs").fetchone()[0]
        runs = load_runs(connection, TRAIN_FROM, data_through)
    cfg = load_win5_cfg()
    provider = FoldFactorTableProvider(
        args.db,
        "20251231",
        pedigree_cache_path=ROOT / "pedigree_cache.json",
        legacy_api_dir=ROOT / "api",
    )
    ability = build_feature_dataset(runs, cfg, data_through, provider)
    current = build_feature_dataset(runs, cfg, data_through, None)
    ability_features, labels, race_keys, ability_meta = ability
    current_features, current_labels, current_keys, current_meta = align_to_reference(
        race_keys, ability_meta, current
    )
    if not np.array_equal(labels, current_labels) or race_keys != current_keys:
        raise RuntimeError("ability/current feature datasets use different samples")

    scaler, coef, temperature, n_samples = fit_candidate(
        ability_features, labels, race_keys
    )
    candidate = model_document(
        scaler, coef, temperature, n_samples, db_hash, data_through
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "win5_ml_model_candidate.json"
    model_hash = write_json(model_path, candidate)
    factor_path = output_dir / "ability_factor_snapshot_20251231.json"
    factor_hash = write_json(factor_path, serialize_factor_snapshot(provider))
    rules_path = output_dir / "mined_rules_v2.csv"
    shutil.copy2(RULES_V2, rules_path)

    current_model = json.loads(PRODUCTION_MODEL.read_text(encoding="utf-8"))
    comparisons = {
        name: compare_period(
            current_features,
            ability_features,
            labels,
            race_keys,
            current_meta,
            ability_meta,
            current_model,
            candidate,
            date_from,
            min(date_to, data_through),
        )
        for name, (date_from, date_to) in PERIODS.items()
        if date_from <= data_through
    }
    report = {
        "experiment_id": "T45-candidate-retrain-v1",
        "seed": args.seed,
        "ability_db_sha256": db_hash,
        "data_through": data_through,
        "candidate_model_sha256": model_hash,
        "factor_snapshot_sha256": factor_hash,
        "mined_rules_v2_sha256": sha256_file(rules_path),
        "production_model_sha256": sha256_file(PRODUCTION_MODEL),
        "candidate_temperature": temperature,
        "candidate_samples": n_samples,
        "comparisons": comparisons,
        "caveat": (
            "2025 is an in-sample artifact comparison because both fixed artifacts "
            "train through 2025; 2026 periods are OOS. No period was used for tuning."
        ),
    }
    report_hash = write_json(output_dir / "report.json", report)
    print(f"candidate model: {model_path} sha256={model_hash}")
    print(f"factor snapshot: {factor_path} sha256={factor_hash}")
    print(f"report sha256={report_hash}")
    for period, populations in comparisons.items():
        for population, result in populations.items():
            current_ll = result["current"]["logloss"]
            candidate_ll = result["candidate"]["logloss"]
            market_ll = result["market"]["logloss"]
            print(
                f"{period} {population}: n={result['races']} "
                f"LL current={current_ll:.6f} candidate={candidate_ll:.6f} "
                f"market={market_ll:.6f} delta={candidate_ll-current_ll:+.6f}"
            )


if __name__ == "__main__":
    main()
