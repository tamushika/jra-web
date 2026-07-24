"""SPEC-T54d sectional-history feature experiment.

Stage 0 is diagnostic and may run before registration.  The fixed six-model
evaluation fails closed until its T39 base registration exists.  Every
sectional feature is built from races strictly before the target date.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

from backtest_ability import load_runs
from backtest_feature_pack import race_level_winner_logloss
from backtest_fold_stats import same_population_metrics
from backtest_ml import FEATURES, fit_conditional_logit, fit_temperature
from backtest_stats_retrain import build_consistent_feature_dataset
from backtest_t54a import compute_style_matrix
from backtest_t61a import pearson_correlation
from backtest_win5 import load_win5_cfg
from eval.blocks import paired_block_bootstrap


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "ability.db"
DEFAULT_LEDGER = ROOT / "eval" / "experiments.jsonl"
DEFAULT_STAGE0_OUTPUT = ROOT / "outputs" / "t54d_stage0.json"
DEFAULT_RESULT_OUTPUT = ROOT / "outputs" / "t54d_result.json"
EXPERIMENT_ID = "T54d-sectional-lap-features-v1"
ABILITY_DB_SHA256 = "bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a"
SECTIONAL_FEATURES = (
    "early_position_asof",
    "late_gain_asof",
    "measured_pace_fit_asof",
    "field_pace_projection_asof",
    "sectional_available",
)
WARMUP_FROM, DATA_FROM, DATA_TO = "20180101", "20210101", "20260630"
TRAIN_TO, SELECTION_FROM, SELECTION_TO = "20231231", "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"), "2026H1": ("20260101", "20260630")}
L2_GRID = (0.3, 1.0, 3.0)
MIN_HISTORY = 2
RECENT_STARTS = 4
MIN_PACE_BASELINE_RACES = 8


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _race_key(row: dict) -> tuple[str, str, int]:
    return (str(row.get("date")), str(row.get("place")), int(row.get("r") or 0))


def _runner_key(row: dict) -> tuple[str, str, int, int]:
    return (*_race_key(row), int(row.get("umaban") or 0))


def _safe_json_numbers(raw: str) -> list[float]:
    try:
        values = json.loads(raw)
        if not isinstance(values, list):
            return []
        parsed = [float(value) for value in values]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if all(np.isfinite(parsed)) else []


def _early_lap_mean(laps: list[float]) -> float | None:
    """Mean of the first half of the measured 200m sections."""
    if len(laps) < 4 or any(value <= 0.0 for value in laps):
        return None
    count = max(2, len(laps) // 2)
    return float(np.mean(laps[:count]))


def load_sectional_rows(connection: sqlite3.Connection) -> list[dict]:
    """Read matched flat-run sectional rows without mutating the source DB."""
    cursor = connection.execute(
        """
        SELECT r.date, r.place, r.r, r.horse, r.umaban, r.rank,
               r.total_horses, r.track_type, r.distance, r.agari,
               rc.corner_positions_json, rl.lap_sequence_json
          FROM runs AS r
          JOIN runner_corners AS rc
            ON rc.date=r.date AND rc.place=r.place AND rc.r=r.r
           AND rc.umaban=r.umaban
          JOIN race_laps AS rl
            ON rl.date=r.date AND rl.place=r.place AND rl.r=r.r
         WHERE r.date BETWEEN ? AND ?
        """,
        (WARMUP_FROM, DATA_TO),
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def build_date_atomic_pace_map(rows: list[dict]) -> dict[tuple, float]:
    """Normalize measured early pace against strictly earlier races.

    Venue/surface/distance is preferred.  A surface/distance fallback is used
    until eight venue-specific prior races exist.  Same-day races are added
    only after every ratio for that day has been computed.
    """
    races: dict[tuple, dict] = {}
    for row in rows:
        key = _race_key(row)
        if key in races:
            continue
        laps = _safe_json_numbers(row.get("lap_sequence_json"))
        raw = _early_lap_mean(laps)
        if raw is None:
            continue
        races[key] = {
            "date": key[0],
            "raw": raw,
            "specific": (str(row.get("place")), str(row.get("track_type")),
                         int(row.get("distance") or 0)),
            "fallback": (str(row.get("track_type")), int(row.get("distance") or 0)),
        }

    by_date: dict[str, list[tuple[tuple, dict]]] = defaultdict(list)
    for key, row in races.items():
        by_date[row["date"]].append((key, row))
    specific_history: dict[tuple, list[float]] = defaultdict(list)
    fallback_history: dict[tuple, list[float]] = defaultdict(list)
    output: dict[tuple, float] = {}
    for date in sorted(by_date):
        for key, row in by_date[date]:
            specific = specific_history[row["specific"]]
            fallback = fallback_history[row["fallback"]]
            reference = specific if len(specific) >= MIN_PACE_BASELINE_RACES else fallback
            if len(reference) >= MIN_PACE_BASELINE_RACES:
                output[key] = float(np.mean(reference[-64:]) / row["raw"])
        for _key, row in by_date[date]:
            specific_history[row["specific"]].append(row["raw"])
            fallback_history[row["fallback"]].append(row["raw"])
    return output


def build_sectional_history(rows: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    """Build valid realized-history records and independent diagnostics."""
    pace_map = build_date_atomic_pace_map(rows)
    by_horse: dict[str, list[dict]] = defaultdict(list)
    agari_differences: list[float] = []
    skipped = defaultdict(int)
    for row in rows:
        corners = _safe_json_numbers(row.get("corner_positions_json"))
        if not corners:
            skipped["empty_corner_sequence"] += 1
            continue
        try:
            total = int(row.get("total_horses") or 0)
            rank = int(row.get("rank") or 0)
        except (TypeError, ValueError):
            total, rank = 0, 0
        if total <= 0 or rank <= 0:
            skipped["invalid_runner_result"] += 1
            continue
        if any(value < 1.0 or value > total for value in corners):
            skipped["invalid_corner_position"] += 1
            continue
        pace_ratio = pace_map.get(_race_key(row))
        if pace_ratio is None:
            skipped["pace_baseline_unavailable"] += 1
            continue
        early = float(corners[0] / total)
        late_gain = float((rank - corners[-1]) / total)
        # Positive means the horse held a forward position in a faster-than-
        # normal race (or stayed back in a slower one); centered to avoid
        # simply reproducing early_position_asof.
        measured_fit = float((pace_ratio - 1.0) * (0.5 - early))
        horse = str(row.get("horse") or "")
        if not horse:
            skipped["missing_horse"] += 1
            continue
        by_horse[horse].append({
            "date": str(row["date"]),
            "key": _runner_key(row),
            "early": early,
            "late_gain": late_gain,
            "measured_fit": measured_fit,
            "pace_ratio": float(pace_ratio),
        })
        laps = _safe_json_numbers(row.get("lap_sequence_json"))
        try:
            agari = float(row.get("agari"))
        except (TypeError, ValueError):
            agari = 0.0
        if agari > 0.0 and len(laps) >= 3:
            agari_differences.append(abs(agari - sum(laps[-3:])))
    for histories in by_horse.values():
        histories.sort(key=lambda item: (item["date"], item["key"]))
    diagnostics = {
        "matched_rows": len(rows),
        "valid_history_rows": sum(map(len, by_horse.values())),
        "skipped": dict(sorted(skipped.items())),
        "agari_lap_tail_abs_diff": {
            "n": len(agari_differences),
            "mean": float(np.mean(agari_differences)) if agari_differences else None,
            "median": float(np.median(agari_differences)) if agari_differences else None,
            "within_1_5_seconds_rate": (
                float(np.mean(np.asarray(agari_differences) <= 1.5))
                if agari_differences else None
            ),
            "diagnostic_only": True,
        },
    }
    return by_horse, diagnostics


def compute_sectional_matrix(
    histories: dict[str, list[dict]], target_rows: list[dict]
) -> np.ndarray:
    """Return five fixed columns using only dates before each target.

    Histories are date-atomic: no race on the target date can enter any row.
    The most recent four valid sectional starts are used, with at least two
    required for availability.
    """
    dates_by_horse = {
        horse: [item["date"] for item in rows] for horse, rows in histories.items()
    }
    individual = np.zeros((len(target_rows), 4), dtype=float)
    for index, current in enumerate(target_rows):
        horse = str(current.get("horse") or "")
        rows = histories.get(horse, [])
        cutoff = bisect.bisect_left(
            dates_by_horse.get(horse, []), str(current.get("date"))
        )
        prior = rows[max(0, cutoff - RECENT_STARTS):cutoff]
        if len(prior) < MIN_HISTORY:
            continue
        individual[index, 0] = float(np.mean([row["early"] for row in prior]))
        individual[index, 1] = float(np.mean([row["late_gain"] for row in prior]))
        individual[index, 2] = float(np.mean([row["measured_fit"] for row in prior]))
        individual[index, 3] = 1.0

    result = np.zeros((len(target_rows), len(SECTIONAL_FEATURES)), dtype=float)
    result[:, :3] = individual[:, :3]
    result[:, 4] = individual[:, 3]
    race_members: dict[tuple, list[int]] = defaultdict(list)
    for index, row in enumerate(target_rows):
        race_members[_race_key(row)].append(index)
    for indices in race_members.values():
        # Unavailable entrants contribute zero pressure, rather than being
        # mistaken for known front-runners.
        pressure = float(np.mean(
            (1.0 - individual[indices, 0]) * individual[indices, 3]
        ))
        result[indices, 3] = pressure
    return result


def require_registration(ledger_path: Path, db_path: Path) -> dict:
    actual = sha256_file(db_path)
    if actual != ABILITY_DB_SHA256:
        raise RuntimeError(
            f"ability.db SHA mismatch: expected {ABILITY_DB_SHA256}, got {actual}"
        )
    registration = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("experiment_id") == EXPERIMENT_ID and row.get("result_summary") is None:
            registration = row
    if registration is None:
        raise RuntimeError(
            f"T54d evaluation blocked: register {EXPERIMENT_ID} in T39 ledger first"
        )
    sealed = registration.get("data_hashes", {}).get("ability_db_sha256")
    if sealed != actual:
        raise RuntimeError(
            f"T39 registration seals {sealed!r}, not current ability.db {actual}"
        )
    expected = registration.get("search_grid", {})
    if expected.get("l2") != list(L2_GRID) or expected.get("configs") != [
        "baseline", "sectional5"
    ]:
        raise RuntimeError("T39 registration does not seal the fixed six candidates")
    return registration


def _mask(dates: np.ndarray, date_from: str, date_to: str) -> np.ndarray:
    return (dates >= date_from) & (dates <= date_to)


def _selected(rows: list, mask: np.ndarray) -> list:
    return [row for row, keep in zip(rows, mask) if keep]


def model_matrix(base: np.ndarray, sectional: np.ndarray | None) -> np.ndarray:
    if sectional is None:
        return base
    if sectional.shape != (len(base), len(SECTIONAL_FEATURES)):
        raise ValueError("sectional matrix must contain five aligned columns")
    return np.column_stack((base, sectional))


def _fit_candidate(base, sectional, labels, keys, meta, dates, l2, label):
    from sklearn.preprocessing import StandardScaler

    matrix = model_matrix(base, sectional)
    train = _mask(dates, DATA_FROM, TRAIN_TO)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = StandardScaler().fit(matrix[train])
    weights = fit_conditional_logit(
        scaler.transform(matrix[train]), labels[train], _selected(keys, train), l2=l2
    )
    selection_keys, selection_meta = _selected(keys, selection), _selected(meta, selection)
    scores = scaler.transform(matrix[selection]) @ weights
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, selection_meta,
        temperature=temperature,
    )
    metrics.pop("sample_signature", None)
    losses = race_level_winner_logloss(
        scores, labels[selection], selection_keys, selection_meta,
        temperature=temperature,
    )
    return {
        "label": label, "l2": l2, "sectional": sectional, "scaler": scaler,
        "weights": weights, "temperature": temperature, "selection": metrics,
        "selection_losses": losses,
    }


def _refit(base, labels, keys, dates, candidate, through):
    from sklearn.preprocessing import StandardScaler

    matrix = model_matrix(base, candidate["sectional"])
    fit = _mask(dates, DATA_FROM, through)
    scaler = StandardScaler().fit(matrix[fit])
    weights = fit_conditional_logit(
        scaler.transform(matrix[fit]), labels[fit], _selected(keys, fit),
        l2=candidate["l2"],
    )
    return {
        "sectional": candidate["sectional"], "scaler": scaler, "weights": weights,
        "temperature": candidate["temperature"],
    }


def _score(model, base, labels, keys, meta, dates, date_from, date_to):
    period = _mask(dates, date_from, date_to)
    matrix = model_matrix(base, model["sectional"])[period]
    scores = model["scaler"].transform(matrix) @ model["weights"]
    period_keys, period_meta = _selected(keys, period), _selected(meta, period)
    metrics = same_population_metrics(
        scores, labels[period], period_keys, period_meta,
        temperature=model["temperature"],
    )
    signature = metrics.pop("sample_signature")
    losses = race_level_winner_logloss(
        scores, labels[period], period_keys, period_meta,
        temperature=model["temperature"],
    )
    return metrics, signature, losses


def _paired(selected_losses: dict, baseline_losses: dict, seed: int) -> dict:
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("selected/baseline race populations differ")
    selected_blocks, baseline_blocks = defaultdict(list), defaultdict(list)
    for key in sorted(selected_losses):
        selected_blocks[key[0]].append(selected_losses[key])
        baseline_blocks[key[0]].append(baseline_losses[key])
    result = paired_block_bootstrap(
        lambda rows: sum(rows) / len(rows), selected_blocks, baseline_blocks,
        2000, seed,
    )
    return {
        "difference": result.observed_difference,
        "ci95": [result.ci_low, result.ci_high],
        "p_value": result.p_value,
        "races": len(selected_losses),
        "day_blocks": len(selected_blocks),
        "resamples": result.n_resamples,
        "seed": result.seed,
    }


def popularity_increment(
    selected_losses: dict, baseline_losses: dict, meta: list[dict]
) -> dict:
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("popularity diagnostic populations differ")
    winners: dict[tuple, list[int]] = defaultdict(list)
    for row in meta:
        try:
            if int(row.get("rank")) == 1:
                winners[_race_key(row)].append(int(row.get("popularity")))
        except (TypeError, ValueError):
            continue
    groups: dict[str, list[float]] = defaultdict(list)
    for key in sorted(selected_losses):
        popularity = min(winners.get(key, [999]))
        band = "1-3" if popularity <= 3 else "4-8" if popularity <= 8 else "9+"
        groups[band].append(selected_losses[key] - baseline_losses[key])
    return {
        band: {
            "races": len(values),
            "winner_logloss_diff": float(np.mean(values)),
        }
        for band, values in sorted(groups.items())
    }


def market_topk_floor(metrics: dict) -> dict:
    model = metrics["models"]["model"]["coverage"]
    market = metrics["models"]["market"]["coverage"]
    gaps = [float(left - right) for left, right in zip(model, market)]
    return {
        "model_minus_market": gaps,
        "pass_all_k": all(value >= 0.0 for value in gaps),
        "k": list(range(1, len(gaps) + 1)),
    }


def coverage_by_period(matrix: np.ndarray, dates: np.ndarray) -> dict:
    periods = {
        "train_2021_2023": (DATA_FROM, TRAIN_TO),
        "selection_2024": (SELECTION_FROM, SELECTION_TO),
        **PERIODS,
    }
    output = {}
    for name, (date_from, date_to) in periods.items():
        rows = matrix[_mask(dates, date_from, date_to)]
        available = rows[:, 4] == 1.0
        output[name] = {
            "rows": len(rows),
            "available_rows": int(available.sum()),
            "availability_rate": float(available.mean()) if len(rows) else None,
            "feature_summary": {
                feature: {
                    "mean": float(np.mean(rows[:, index])) if len(rows) else None,
                    "std": float(np.std(rows[:, index])) if len(rows) else None,
                }
                for index, feature in enumerate(SECTIONAL_FEATURES)
            },
        }
    return output


def _load_inputs(db_path: Path):
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        runs = load_runs(connection, DATA_FROM, DATA_TO)
        sectional_rows = load_sectional_rows(connection)
    histories, diagnostics = build_sectional_history(sectional_rows)
    return runs, histories, diagnostics


def stage0_diagnostic(db_path: Path) -> dict:
    runs, histories, source_diagnostics = _load_inputs(db_path)
    cfg = load_win5_cfg()
    base, _labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(db_path)
    )
    dates = np.asarray([key[0] for key in keys])
    sectional = compute_sectional_matrix(histories, meta)
    style = compute_style_matrix(runs, meta)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    available = sectional[:, 4] == 1.0
    selection_available = selection & available
    common_style_available = selection_available & (style[:, 3] == 1.0)
    pace_index = FEATURES.index("pace_fit")
    return {
        "period": "2024_selection",
        "source_diagnostics": source_diagnostics,
        "coverage": coverage_by_period(sectional, dates),
        "correlations_2024_available": {
            "n": int(selection_available.sum()),
            "early_vs_existing_pace_fit": pearson_correlation(
                sectional[selection_available, 0].tolist(),
                base[selection_available, pace_index].tolist(),
            ),
            "early_vs_t54a_style_pos_asof": pearson_correlation(
                sectional[common_style_available, 0].tolist(),
                style[common_style_available, 0].tolist(),
            ),
            "early_vs_t54a_style_pos_common_available_n": int(
                common_style_available.sum()
            ),
            "measured_pace_fit_vs_t54a_proxy_pressure": pearson_correlation(
                sectional[selection_available, 2].tolist(),
                style[selection_available, 1].tolist(),
            ),
            "extreme_redundancy_threshold_abs_r": 0.9,
        },
        "fixed_features": list(SECTIONAL_FEATURES),
        "fixed_candidates": {
            "configs": ["baseline", "sectional5"],
            "l2": list(L2_GRID),
            "candidate_count": 6,
            "grid_search": False,
        },
        "selection_guard": "diagnostic_only_not_used_for_candidate_selection",
    }


def run_evaluation(db_path: Path, ledger_path: Path) -> dict:
    registration = require_registration(ledger_path, db_path)
    runs, histories, source_diagnostics = _load_inputs(db_path)
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_consistent_feature_dataset(
        runs, cfg, DATA_TO, stats_source="ability", db_path=str(db_path)
    )
    dates = np.asarray([key[0] for key in keys])
    sectional = compute_sectional_matrix(histories, meta)
    base_sha = hashlib.sha256(base.tobytes()).hexdigest()
    if model_matrix(base, None) is not base:
        raise AssertionError("pack-OFF must return the original base matrix")
    if hashlib.sha256(model_matrix(base, None).tobytes()).hexdigest() != base_sha:
        raise AssertionError("pack-OFF feature matrix is not byte-identical")

    candidates = [
        _fit_candidate(
            base, None if config == "baseline" else sectional,
            labels, keys, meta, dates, l2, config,
        )
        for config in ("baseline", "sectional5")
        for l2 in L2_GRID
    ]
    baseline = min(
        (row for row in candidates if row["label"] == "baseline"),
        key=lambda row: row["selection"]["models"]["model"]["logloss"],
    )
    sectional_best = min(
        (row for row in candidates if row["label"] == "sectional5"),
        key=lambda row: row["selection"]["models"]["model"]["logloss"],
    )
    selected = min(
        candidates, key=lambda row: row["selection"]["models"]["model"]["logloss"]
    )
    baseline_final = _refit(base, labels, keys, dates, baseline, SELECTION_TO)
    sectional_final = _refit(base, labels, keys, dates, sectional_best, SELECTION_TO)
    sectional_reference = _refit(base, labels, keys, dates, sectional_best, TRAIN_TO)

    historical, paired, popularity, floors = {}, {}, {}, {}
    paired["2024_selection"] = _paired(
        sectional_best["selection_losses"], baseline["selection_losses"], 5440
    )
    popularity["2024_selection"] = popularity_increment(
        sectional_best["selection_losses"], baseline["selection_losses"],
        [row for row in meta if SELECTION_FROM <= str(row.get("date")) <= SELECTION_TO],
    )
    floors["2024_selection"] = {
        "sectional5": market_topk_floor(sectional_best["selection"]),
        "baseline": market_topk_floor(baseline["selection"]),
    }
    for index, (name, (date_from, date_to)) in enumerate(PERIODS.items()):
        sectional_metrics, signature, sectional_losses = _score(
            sectional_final, base, labels, keys, meta, dates, date_from, date_to
        )
        baseline_metrics, baseline_signature, baseline_losses = _score(
            baseline_final, base, labels, keys, meta, dates, date_from, date_to
        )
        reference_metrics, reference_signature, _ = _score(
            sectional_reference, base, labels, keys, meta, dates, date_from, date_to
        )
        if signature != baseline_signature or signature != reference_signature:
            raise RuntimeError(f"{name}: evaluation populations differ")
        historical[name] = {
            "sectional5_final_refit_2021_2024": sectional_metrics,
            "sectional5_trained_2021_2023_reference": reference_metrics,
            "best_baseline_final_refit": baseline_metrics,
        }
        paired[name] = _paired(sectional_losses, baseline_losses, 5441 + index)
        popularity[name] = popularity_increment(
            sectional_losses, baseline_losses,
            [row for row in meta if date_from <= str(row.get("date")) <= date_to],
        )
        floors[name] = {
            "sectional5": market_topk_floor(sectional_metrics),
            "baseline": market_topk_floor(baseline_metrics),
        }

    return {
        "spec": "T54d",
        "experiment_id": EXPERIMENT_ID,
        "ability_db_sha256": sha256_file(db_path),
        "registration": registration,
        "features": list(SECTIONAL_FEATURES),
        "source_diagnostics": source_diagnostics,
        "pack_off_base_sha256": base_sha,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "config": row["label"],
                "l2": row["l2"],
                "selection_2024": row["selection"],
            }
            for row in candidates
        ],
        "selected": {"config": selected["label"], "l2": selected["l2"]},
        "sectional5_best_l2": sectional_best["l2"],
        "baseline_best_l2": baseline["l2"],
        "coverage": coverage_by_period(sectional, dates),
        "historical_benchmark": historical,
        "win5_day_paired": paired,
        "popularity_band_increment": popularity,
        "market_topk_floor": floors,
    }


def write_json(payload: dict, path: Path) -> str:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage0", "evaluate"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "stage0":
        payload = {
            "spec": "T54d",
            "ability_db_sha256": sha256_file(args.db),
            "stage0": stage0_diagnostic(args.db),
        }
        output = args.output or DEFAULT_STAGE0_OUTPUT
    else:
        payload = run_evaluation(args.db, args.ledger)
        output = args.output or DEFAULT_RESULT_OUTPUT
    print(write_json(payload, output))


if __name__ == "__main__":
    main()
