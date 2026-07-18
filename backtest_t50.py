"""T50 Stage 2: isolated A5 moisture/cushion interaction evaluation.

The preregistered grid is exactly baseline/+A5 x L2 {0.3, 1.0, 3.0}.
No production feature, model artifact, or live path is changed.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from backtest_daystate import (  # noqa: E402
    _blocks_by_date, _fit_scaler_in_chunks, _mean_metric,
    build_base_dataset_yearwise, fit_conditional_logit_compact,
    race_level_winner_logloss,
)
from backtest_fold_stats import same_population_metrics  # noqa: E402
from backtest_ml import FEATURES, WET, fit_temperature  # noqa: E402
from backtest_pace import TRACK_MEAN  # noqa: E402
from backtest_stats_retrain import DB_PATH  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402

EXPERIMENT_ID = "T50-moisture-cushion-v2"
ABILITY_DB_SHA256_EXPECTED = (
    "ea6b8e5b989d722658170d2499342f98d82f01c5b875c3570abf26c63112f061")
T50_DB_SHA256_EXPECTED = (
    "be2e1a6cd7c29c6214871565b49ee3aa4339fc524d04f75824381566ca93b21f")
T50_DB_PATH = os.path.join(BASE_DIR, "data", "t50", "track_measurements.sqlite")

TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"),
           "2026H1": ("20260101", "20260630")}
DATA_TO = "20260630"
L2_GRID = (0.3, 1.0, 3.0)
MIN_HISTORY = 20
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 50

A5_FEATURES = (
    "moisture_x_wet_match",
    "moisture_x_pace_type",
    "cushion_x_speed_type",
    "moisture_wet_available",
    "moisture_pace_available",
    "cushion_speed_available",
)
CONDITIONS = ("良", "稍", "重", "不")


@dataclass
class RunningStats:
    n: int = 0
    total: float = 0.0
    total_sq: float = 0.0

    def add(self, value):
        value = float(value)
        self.n += 1
        self.total += value
        self.total_sq += value * value

    @property
    def mean(self):
        return self.total / self.n if self.n else None

    @property
    def std(self):
        if self.n < 2:
            return None
        variance = max(0.0, self.total_sq / self.n - self.mean ** 2)
        return math.sqrt(variance)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_inputs(ability_db=DB_PATH, t50_db=T50_DB_PATH):
    actual = {"ability_db_sha256": file_sha256(ability_db),
              "t50_track_measurements_sqlite_sha256": file_sha256(t50_db)}
    expected = {"ability_db_sha256": ABILITY_DB_SHA256_EXPECTED,
                "t50_track_measurements_sqlite_sha256": T50_DB_SHA256_EXPECTED}
    return actual, actual == expected


def load_measurements(t50_db=T50_DB_PATH):
    query = """
        SELECT replace(date, '-', ''), venue, turf_moisture_goal,
               dirt_moisture_goal, cushion
          FROM track_measurements
         WHERE is_race_day = 1
         ORDER BY date, venue
    """
    with closing(sqlite3.connect(t50_db)) as connection:
        return [dict(date=row[0], venue=row[1], turf_moisture_goal=row[2],
                     dirt_moisture_goal=row[3], cushion=row[4])
                for row in connection.execute(query)]


def _standardized(value, exact, fallback, min_history):
    """Return z, exact-history flag, and fallback source.

    Exact venue/surface history is preferred.  Before it reaches n=20, the
    same-surface global expanding history is used.  Both histories contain
    only dates strictly before the target date.
    """
    if value is None:
        return 0.0, False, "missing"
    if exact and exact.n >= min_history and exact.std and exact.std > 1e-12:
        return (float(value) - exact.mean) / exact.std, True, "venue_surface"
    if fallback and fallback.n >= min_history and fallback.std and fallback.std > 1e-12:
        return (float(value) - fallback.mean) / fallback.std, False, "surface_global"
    return 0.0, False, "insufficient_history"


def build_asof_measurement_states(measurements, min_history=MIN_HISTORY):
    """Build venue/date/surface states without same-date or future leakage."""
    by_date = defaultdict(list)
    for row in measurements:
        by_date[row["date"]].append(row)
    exact_stats, global_stats, states = {}, {}, {}
    for day in sorted(by_date):
        pending = []
        for row in by_date[day]:
            for surface, field in (("芝", "turf_moisture_goal"),
                                   ("ダート", "dirt_moisture_goal")):
                value = row.get(field)
                exact_key = (row["venue"], surface)
                z, exact_ok, source = _standardized(
                    value, exact_stats.get(exact_key), global_stats.get(surface),
                    min_history)
                states[(day, row["venue"], surface)] = {
                    "moisture_z": float(z),
                    "moisture_present": value is not None,
                    "exact_history_available": bool(exact_ok),
                    "history_source": source,
                    "history_n": exact_stats.get(exact_key, RunningStats()).n,
                    "cushion": (float(row["cushion"])
                                if surface == "芝" and row.get("cushion") is not None
                                else None),
                }
                if value is not None:
                    pending.append((exact_key, surface, float(value)))
        # Same-date venues and Saturday/Sunday rows never enter one another's
        # history until their own earlier calendar date has completed.
        for exact_key, surface, value in pending:
            exact_stats.setdefault(exact_key, RunningStats()).add(value)
            global_stats.setdefault(surface, RunningStats()).add(value)
    return states


def _identity(row):
    return (row.get("date"), row.get("place"), row.get("r"),
            row.get("horse"), row.get("umaban"))


def load_horse_profiles_from_db(db_path, meta):
    """Reproduce the existing last-four-run wet/PCI/tfeat availability rules."""
    targets = {_identity(row) for row in meta}
    profiles = {}
    query = """
        SELECT date, place, r, horse, umaban, pci, track_type, time_sec,
               condition, rank
          FROM runs
         WHERE date >= '20180101' AND date <= ? AND horse IS NOT NULL
               AND rank IS NOT NULL
         ORDER BY horse, date, r
    """
    current_horse, history = None, []
    with closing(sqlite3.connect(db_path)) as connection:
        for row in connection.execute(query, (DATA_TO,)):
            date, place, race_no, horse, umaban, pci, surface, time_sec, condition, rank = row
            if horse != current_horse:
                current_horse, history = horse, []
            identity = (date, place, race_no, horse, umaban)
            if identity in targets:
                pci_values = [old[0] - TRACK_MEAN[old[1]] for old in history
                              if old[0] is not None and old[1] in TRACK_MEAN]
                wet_ranks = [int(old[4]) for old in history
                             if old[3] in WET and old[4] is not None]
                dry_ranks = [int(old[4]) for old in history
                             if old[3] == "良" and old[4] is not None]
                profiles[identity] = {
                    "pace_type": (float(np.mean(pci_values)) if pci_values else None),
                    "wet_available": bool(condition in WET and wet_ranks and dry_ranks),
                    "speed_available": any(old[2] and old[3] for old in history),
                }
            history.append((pci, surface, time_sec, condition, rank))
            if len(history) > 4:
                history.pop(0)
    return profiles


def build_a5_matrix(base, meta, keys, states, profiles):
    """Return the six preregistered A5 columns and row diagnostics."""
    wet_column, tfeat_column = FEATURES.index("wet_match"), FEATURES.index("tfeat")
    matrix = np.zeros((len(meta), len(A5_FEATURES)), dtype=np.float32)
    diagnostics = []
    for index, (row, key) in enumerate(zip(meta, keys)):
        date, place, _race_no = key
        surface = row.get("track_type")
        state = states.get((date, place, surface))
        profile = profiles.get(_identity(row), {})
        moisture_ok = bool(state and state["moisture_present"])
        moisture_z = state["moisture_z"] if moisture_ok else 0.0
        exact_ok = bool(state and state["exact_history_available"])
        wet_available = exact_ok and bool(profile.get("wet_available"))
        pace_type = profile.get("pace_type")
        pace_available = exact_ok and pace_type is not None
        cushion = state.get("cushion") if state else None
        speed_available = bool(cushion is not None and profile.get("speed_available"))
        matrix[index] = (
            moisture_z * float(base[index, wet_column]) if moisture_ok else 0.0,
            moisture_z * float(pace_type) if moisture_ok and pace_type is not None else 0.0,
            float(cushion) * float(base[index, tfeat_column]) if speed_available else 0.0,
            float(wet_available), float(pace_available), float(speed_available),
        )
        diagnostics.append({
            "measurement_available": bool(state),
            "moisture_present": moisture_ok,
            "exact_history_available": exact_ok,
            "history_source": state["history_source"] if state else "missing",
            "history_n": state["history_n"] if state else 0,
            "wet_available": wet_available,
            "pace_available": pace_available,
            "cushion_speed_available": speed_available,
        })
    return matrix, diagnostics


def active_a5_columns(a5, dates):
    """Drop preregistered availability flags only if constant in training."""
    train = period_mask(dates, TRAIN_FROM, TRAIN_TO)
    active, removed = [], []
    for index, name in enumerate(A5_FEATURES):
        if name.endswith("_available") and np.ptp(a5[train, index]) == 0.0:
            removed.append(name)
        else:
            active.append(index)
    return active, removed


def period_mask(dates, date_from, date_to):
    dates = np.asarray(dates)
    return (dates >= date_from) & (dates <= date_to)


def selected(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def fit_candidate(X, labels, keys, meta, dates, config, l2):
    train = period_mask(dates, TRAIN_FROM, TRAIN_TO)
    selection = period_mask(dates, SELECTION_FROM, SELECTION_TO)
    scaler = _fit_scaler_in_chunks(X[train])
    weights = fit_conditional_logit_compact(
        scaler.transform(X[train]), labels[train], selected(keys, train), l2=l2)
    scores = scaler.transform(X[selection]) @ weights
    selection_keys = selected(keys, selection)
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, selected(meta, selection),
        temperature=temperature)
    return {"config": config, "l2": l2, "scaler": scaler, "weights": weights,
            "temperature": temperature, "selection": metrics}


def final_model(X, labels, keys, dates, candidate):
    train = period_mask(dates, TRAIN_FROM, SELECTION_TO)
    scaler = _fit_scaler_in_chunks(X[train])
    weights = fit_conditional_logit_compact(
        scaler.transform(X[train]), labels[train], selected(keys, train),
        l2=candidate["l2"])
    return {"scaler": scaler, "weights": weights,
            "temperature": candidate["temperature"]}


def score(model, X, labels, keys, meta, dates, period, row_mask=None):
    mask = period_mask(dates, *period)
    if row_mask is not None:
        mask &= row_mask
    scores = model["scaler"].transform(X[mask]) @ model["weights"]
    out_keys, out_meta = selected(keys, mask), selected(meta, mask)
    metrics = same_population_metrics(
        scores, labels[mask], out_keys, out_meta,
        temperature=model["temperature"])
    losses = race_level_winner_logloss(
        scores, labels[mask], out_keys, out_meta,
        temperature=model["temperature"])
    return metrics, losses


def compact(metrics):
    return {key: metrics[key] for key in ("races", "horses", "models", "skipped")}


def paired_report(selected_losses, baseline_losses):
    if set(selected_losses) != set(baseline_losses):
        raise RuntimeError("selected/baseline evaluation races differ")
    selected_blocks, baseline_blocks = (_blocks_by_date(selected_losses),
                                        _blocks_by_date(baseline_losses))
    result = paired_block_bootstrap(
        _mean_metric, selected_blocks, baseline_blocks,
        n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
    return {"observed_diff": result.observed_difference,
            "ci_low": result.ci_low, "ci_high": result.ci_high,
            "p_value": result.p_value, "day_blocks": len(selected_blocks),
            "races": len(selected_losses)}


def condition_report(selected_model, baseline_model, selected_X, base,
                     labels, keys, meta, dates, period):
    output = {}
    for condition in CONDITIONS:
        row_mask = np.asarray([row.get("condition") == condition for row in meta])
        sm, sl = score(selected_model, selected_X, labels, keys, meta, dates,
                       period, row_mask=row_mask)
        bm, bl = score(baseline_model, base, labels, keys, meta, dates,
                       period, row_mask=row_mask)
        output[condition] = {
            "selected": compact(sm), "baseline": compact(bm),
            "delta": (sm["models"]["model"]["logloss"]
                      - bm["models"]["model"]["logloss"]),
            "paired": paired_report(sl, bl) if sl else None,
        }
    return output


def coverage_report(a5, diagnostics, dates, active, removed):
    periods = {"train_2021_2023": (TRAIN_FROM, TRAIN_TO),
               "selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    report = {"active_features": [A5_FEATURES[i] for i in active],
              "removed_constant_flags": removed, "periods": {}}
    for name, period in periods.items():
        mask = period_mask(dates, *period)
        rows = [row for row, keep in zip(diagnostics, mask) if keep]
        feature_rows = {}
        for index, feature in enumerate(A5_FEATURES):
            column = a5[mask, index]
            feature_rows[feature] = {
                "mean": float(np.mean(column)), "std": float(np.std(column)),
                "nonzero_rate": float(np.mean(column != 0.0)),
                "constant": bool(np.ptp(column) == 0.0),
            }
        report["periods"][name] = {
            "rows": int(mask.sum()),
            "measurement_missing_rate": float(np.mean(
                [not row["measurement_available"] for row in rows])),
            "exact_history_rate": float(np.mean(
                [row["exact_history_available"] for row in rows])),
            "history_source_rates": {source: float(np.mean(
                [row["history_source"] == source for row in rows]))
                for source in ("venue_surface", "surface_global",
                               "insufficient_history", "missing")},
            "features": feature_rows,
        }
    return report


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--json-out")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--t50-db", default=T50_DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    hashes, hashes_ok = verify_inputs(args.db, args.t50_db)
    print(json.dumps(hashes, ensure_ascii=False))
    if not hashes_ok:
        print("[STOP] input hash mismatch against T39 preregistration")
        return 1

    cfg = load_win5_cfg()
    base, labels, keys, meta = build_base_dataset_yearwise(args.db, cfg)
    base = np.asarray(base, dtype=np.float32)
    dates = np.asarray([key[0] for key in keys])
    states = build_asof_measurement_states(load_measurements(args.t50_db))
    profiles = load_horse_profiles_from_db(args.db, meta)
    a5, diagnostics = build_a5_matrix(base, meta, keys, states, profiles)
    active, removed = active_a5_columns(a5, dates)
    a5_active = a5[:, active]
    a5_X = np.hstack([base, a5_active])
    print(f"dataset={base.shape}; A5={a5.shape}; active={len(active)}; removed={removed}")
    gc.collect()

    l2_grid = (1.0,) if args.smoke else L2_GRID
    configs = ("baseline",) if args.smoke else ("baseline", "+A5_interactions")
    candidates = []
    for config in configs:
        X = base if config == "baseline" else a5_X
        for l2 in l2_grid:
            candidate = fit_candidate(X, labels, keys, meta, dates, config, l2)
            candidates.append(candidate)
            model = candidate["selection"]["models"]["model"]
            print(f"[2024] {config} L2={l2}: LL={model['logloss']:.9f} "
                  f"Brier={model['brier']:.9f}")
    best = min(candidates,
               key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline_best = min((row for row in candidates if row["config"] == "baseline"),
                        key=lambda row: row["selection"]["models"]["model"]["logloss"])
    selected_X = base if best["config"] == "baseline" else a5_X
    selected_final = final_model(selected_X, labels, keys, dates, best)
    baseline_final = final_model(base, labels, keys, dates, baseline_best)
    selected_reference = {key: best[key] for key in ("scaler", "weights", "temperature")}
    baseline_reference = {key: baseline_best[key]
                          for key in ("scaler", "weights", "temperature")}

    report_periods = {"selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    periods, paired, conditions = {}, {}, {}
    for name, period in report_periods.items():
        smodel = selected_reference if name == "selection_2024" else selected_final
        bmodel = baseline_reference if name == "selection_2024" else baseline_final
        sm, sl = score(smodel, selected_X, labels, keys, meta, dates, period)
        bm, bl = score(bmodel, base, labels, keys, meta, dates, period)
        periods[name] = {
            "selected": compact(sm), "baseline": compact(bm),
            "delta": (sm["models"]["model"]["logloss"]
                      - bm["models"]["model"]["logloss"]),
            "market_topk_floor": [model >= market for model, market in zip(
                sm["models"]["model"]["coverage"],
                sm["models"]["market"]["coverage"])],
        }
        if name != "selection_2024":
            reference, _ = score(selected_reference, selected_X, labels, keys,
                                 meta, dates, period)
            baseline_ref, _ = score(baseline_reference, base, labels, keys,
                                    meta, dates, period)
            periods[name]["trained_2021_2023_reference"] = compact(reference)
            periods[name]["baseline_trained_2021_2023_reference"] = compact(baseline_ref)
        paired[name] = paired_report(sl, bl)
        conditions[name] = condition_report(
            smodel, bmodel, selected_X, base, labels, keys, meta, dates, period)

    payload = {
        "experiment_id": EXPERIMENT_ID,
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_hashes": hashes,
        "definitions": {
            "moisture_standardization": (
                "strictly-prior-date expanding z-score by venue/surface; "
                "surface-global fallback while exact n<20; zero while fallback n<20"),
            "pace_type": "last-four prior-run mean PCI deviation from surface TRACK_MEAN",
            "wet_match": "existing backtest_ml wet_match value and last-four availability",
            "speed_type": "raw tfeat interaction (global scaling absorbed by CL scaler)",
            "features_registered": list(A5_FEATURES),
            "features_active": [A5_FEATURES[index] for index in active],
        },
        "candidates": [{"config": row["config"], "l2": row["l2"],
                         "selection_2024": row["selection"]["models"]}
                       for row in candidates],
        "selected": {"config": best["config"], "l2": best["l2"]},
        "baseline_best": {"l2": baseline_best["l2"]},
        "periods": periods,
        "paired_day_bootstrap": paired,
        "condition_breakdown": conditions,
        "coverage": coverage_report(a5, diagnostics, dates, active, removed),
    }
    payload = json_safe(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
