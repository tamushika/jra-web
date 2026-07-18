"""T44 same-day as-of track-state diagnostic (evaluation only).

The production 21-feature contract and live paths are untouched.  Track state
is reset every venue-day and is built only from already-finished races.
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
from dataclasses import dataclass
from datetime import datetime

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from backtest_ability import load_runs  # noqa: E402
from backtest_feature_pack import (  # noqa: E402
    _blocks_by_date, _mean_metric, race_level_winner_logloss,
    verify_ability_db_hash)
from backtest_fold_stats import same_population_metrics  # noqa: E402
from backtest_ml import FEATURES, build_dataset, fit_temperature  # noqa: E402
from backtest_pace import TRACK_MEAN  # noqa: E402
from backtest_stats_retrain import DB_PATH, PEDIGREE_CACHE_PATH  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402

EXPERIMENT_ID = "T44-sameday-track-state-v1"
ABILITY_DB_SHA256_EXPECTED = (
    "ea6b8e5b989d722658170d2499342f98d82f01c5b875c3570abf26c63112f061")
DATA_FROM, DATA_TO = "20210101", "20260630"
TRAIN_FROM, TRAIN_TO = "20210101", "20231231"
SELECTION_FROM, SELECTION_TO = "20240101", "20241231"
PERIODS = {"2025": ("20250101", "20251231"),
           "2026H1": ("20260101", "20260630")}
K_GRID, L2_GRID = (2.0, 5.0, 10.0), (0.3, 1.0, 3.0)
X1_FEATURES = ("day_pace_x_horse_pace", "day_draw_x_horse_draw",
               "day_time_x_horse_speed", "day_pace_available",
               "day_draw_available", "day_time_available")
MIN_BASELINE_RACES = 20
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 44


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


def distance_band(distance):
    distance = int(distance or 0)
    return "sprint" if distance <= 1400 else ("mile" if distance <= 1800 else "route")


def shrinkage_weight(n, k):
    return float(n) / (float(n) + float(k)) if n else 0.0


def _race_raw_observations(runs):
    grouped = defaultdict(list)
    for row in runs:
        key = (row.get("date"), row.get("place"), row.get("r"))
        if all(value is not None for value in key):
            grouped[key].append(row)
    observations = {}
    for key, members in grouped.items():
        ranked = []
        for row in members:
            try:
                rank = int(row.get("rank"))
            except (TypeError, ValueError):
                continue
            if rank > 0:
                ranked.append((rank, row))
        if not ranked:
            continue
        exemplar = min(ranked, key=lambda item: item[0])[1]
        top3 = [row for rank, row in ranked if rank <= 3]
        times = [float(row["time_sec"]) for row in top3 if row.get("time_sec")]
        front = []
        draw = []
        for row in top3:
            total = row.get("total_horses")
            c4, umaban = row.get("c4"), row.get("umaban")
            if total and total > 1 and c4 and 1 <= c4 <= total:
                front.append(1.0 - (float(c4) - 1.0) / (float(total) - 1.0))
            if total and total > 1 and umaban and 1 <= umaban <= total:
                draw.append((float(umaban) - 1.0) / (float(total) - 1.0))
        observations[key] = {
            "date": exemplar["date"], "place": exemplar["place"],
            "r": int(exemplar["r"]), "track_type": exemplar["track_type"],
            "distance_band": distance_band(exemplar.get("distance")),
            "condition": exemplar.get("condition"),
            "time": float(np.mean(times)) if times else None,
            "front": float(np.mean(front)) if front else None,
            "draw": float(np.mean(draw)) if draw else None,
        }
    return observations


def load_race_observations_from_db(db_path):
    """Load one compact aggregate per race instead of retaining all runners."""
    query = """
        SELECT date, place, r, track_type, distance, condition,
               AVG(CASE WHEN CAST(rank AS INTEGER) BETWEEN 1 AND 3
                        THEN time_sec END) AS top3_time,
               AVG(CASE WHEN CAST(rank AS INTEGER) BETWEEN 1 AND 3
                             AND total_horses > 1 AND c4 BETWEEN 1 AND total_horses
                        THEN 1.0 - (c4 - 1.0) / (total_horses - 1.0) END) AS top3_front,
               AVG(CASE WHEN CAST(rank AS INTEGER) BETWEEN 1 AND 3
                             AND total_horses > 1 AND umaban BETWEEN 1 AND total_horses
                        THEN (umaban - 1.0) / (total_horses - 1.0) END) AS top3_draw
          FROM runs
         WHERE date >= '20180101' AND date <= ? AND rank IS NOT NULL
         GROUP BY date, place, r, track_type, distance, condition
    """
    raw = {}
    with closing(sqlite3.connect(db_path)) as connection:
        for date, place, race_no, track, distance, condition, time_v, front, draw in connection.execute(query, (DATA_TO,)):
            key = (date, place, race_no)
            raw[key] = {"date": date, "place": place, "r": int(race_no),
                        "track_type": track, "distance_band": distance_band(distance),
                        "condition": condition, "time": time_v,
                        "front": front, "draw": draw}
    return raw


def _baseline(stats, keys, *, require_std=False):
    for key in keys:
        row = stats.get(key)
        if row is None or row.n < MIN_BASELINE_RACES:
            continue
        if require_std and (row.std is None or row.std <= 1e-9):
            continue
        return row
    return None


def _asof_signals_from_raw(raw):
    """Build per-race residuals using history strictly before that date.

    Dates are processed atomically: no race on a date enters any baseline used
    for that date.  The signals are later consumed only within their own date.
    """
    by_date = defaultdict(list)
    for key, observation in raw.items():
        by_date[observation["date"]].append((key, observation))

    time_stats, front_stats, draw_stats = {}, {}, {}
    signals = {}
    for date in sorted(by_date):
        for key, obs in by_date[date]:
            p, t, b, c = (obs["place"], obs["track_type"],
                          obs["distance_band"], obs["condition"])
            time_base = _baseline(time_stats, ((p, t, b, c), (p, t, b, None),
                                                (p, t, None, None)), require_std=True)
            front_base = _baseline(front_stats, ((p, t, b), (p, t, None)))
            draw_base = _baseline(draw_stats, ((p, t, b), (p, t, None)))
            signals[key] = {
                "time": ((obs["time"] - time_base.mean) / time_base.std
                         if obs["time"] is not None and time_base else None),
                # frontness is 1 at the lead, hence observed-baseline =>
                # positive means the same-day earlier races favoured speed.
                "pace": (obs["front"] - front_base.mean
                         if obs["front"] is not None and front_base else None),
                # normalised horse number: positive means outer draws did well.
                "draw": (obs["draw"] - draw_base.mean
                         if obs["draw"] is not None and draw_base else None),
                "raw": obs,
            }
        # Only after every signal for the date has been frozen may the date
        # enter the expanding historical baseline.
        for _key, obs in by_date[date]:
            p, t, b, c = (obs["place"], obs["track_type"],
                          obs["distance_band"], obs["condition"])
            if obs["time"] is not None:
                for stat_key in ((p, t, b, c), (p, t, b, None), (p, t, None, None)):
                    time_stats.setdefault(stat_key, RunningStats()).add(obs["time"])
            if obs["front"] is not None:
                for stat_key in ((p, t, b), (p, t, None)):
                    front_stats.setdefault(stat_key, RunningStats()).add(obs["front"])
            if obs["draw"] is not None:
                for stat_key in ((p, t, b), (p, t, None)):
                    draw_stats.setdefault(stat_key, RunningStats()).add(obs["draw"])
    return signals


def build_asof_race_signals(runs):
    return _asof_signals_from_raw(_race_raw_observations(runs))


def win5_cutoffs(race_keys, margin=2):
    """Approximate each weekend's five WIN5 targets exactly as backtest_win5.

    The common numeric cutoff is min(target race number)-margin and is applied
    to every venue.  The stricter sensitivity uses margin=3.
    """
    by_date = defaultdict(set)
    for date, place, race_no in race_keys:
        if race_no is None or race_no < 9:
            continue
        try:
            if datetime.strptime(date, "%Y%m%d").weekday() not in (5, 6):
                continue
        except ValueError:
            continue
        by_date[date].add((int(race_no), place))
    cutoffs, targets = {}, set()
    for date, candidates in by_date.items():
        ordered = sorted(candidates, key=lambda item: (-item[0], item[1]))
        if len(ordered) < 5:
            continue
        selected = ordered[:5]
        cutoffs[date] = min(race_no for race_no, _place in selected) - margin
        targets.update((date, place, race_no) for race_no, place in selected)
    return cutoffs, targets


def _run_identity(row):
    return (row.get("date"), row.get("place"), row.get("r"),
            row.get("horse"), row.get("umaban"))


def _horse_availability(runs):
    by_horse = defaultdict(list)
    for row in runs:
        if row.get("horse"):
            by_horse[row["horse"]].append(row)
    pace, speed = {}, {}
    for rows in by_horse.values():
        rows.sort(key=lambda row: (row.get("date") or "", int(row.get("r") or 0)))
        for index, current in enumerate(rows):
            prior = rows[max(0, index - 4):index]
            pci_values = [float(row["pci"]) - TRACK_MEAN[row["track_type"]]
                          for row in prior if row.get("pci") is not None
                          and row.get("track_type") in TRACK_MEAN]
            identity = _run_identity(current)
            pace[identity] = float(np.mean(pci_values)) if pci_values else None
            speed[identity] = any(row.get("time_sec") and row.get("condition")
                                  for row in prior)
    return pace, speed


def load_horse_types_from_db(db_path, meta):
    """Stream horse histories and retain types only for constructed rows."""
    targets = {_run_identity(row) for row in meta}
    pace, speed = {}, {}
    query = """
        SELECT date, place, r, horse, umaban, pci, track_type, time_sec, condition
          FROM runs
         WHERE date >= '20180101' AND date <= ? AND horse IS NOT NULL
               AND rank IS NOT NULL
         ORDER BY horse, date, r
    """
    current_horse, history = None, []
    with closing(sqlite3.connect(db_path)) as connection:
        for date, place, race_no, horse, umaban, pci, track, time_sec, condition in connection.execute(query, (DATA_TO,)):
            if horse != current_horse:
                current_horse, history = horse, []
            identity = (date, place, race_no, horse, umaban)
            if identity in targets:
                pci_values = [value - TRACK_MEAN[surface]
                              for value, surface, _time, _condition in history
                              if value is not None and surface in TRACK_MEAN]
                pace[identity] = float(np.mean(pci_values)) if pci_values else None
                speed[identity] = any(time and cond for _pci, _surface, time, cond in history)
            history.append((pci, track, time_sec, condition))
            if len(history) > 4:
                history.pop(0)
    return pace, speed


def build_base_dataset_yearwise(db_path, cfg):
    """T33-equivalent previous-year snapshots without retaining 9y of runs."""
    from backtest_fold_stats import _factor_loader
    from fold_stats import FoldFactorTableProvider, load_pedigree_cache

    pedigree = load_pedigree_cache(PEDIGREE_CACHE_PATH)
    feature_parts, label_parts, keys, meta = [], [], [], []
    for year in range(2021, 2027):
        year_from = f"{year:04d}0101"
        year_to = min(f"{year:04d}1231", DATA_TO)
        if year_to < year_from:
            continue
        provider = FoldFactorTableProvider(
            db_path, f"{year - 1:04d}1231", pedigree_by_horse=pedigree,
            legacy_api_dir=API_DIR)
        # Build the factor snapshot before materialising the three-year runner
        # lookback.  Reversing this order materially lowers peak memory while
        # preserving the exact T33 fold inputs.
        with closing(sqlite3.connect(db_path)) as connection:
            runs = load_runs(connection, year_from, year_to)
        with _factor_loader(provider):
            X, y, year_keys, year_meta = build_dataset(runs, year_from, cfg)
        feature_parts.append(X)
        label_parts.append(y)
        keys.extend(year_keys)
        meta.extend(year_meta)
        del runs, provider, X, y, year_keys, year_meta
        gc.collect()
    return np.concatenate(feature_parts), np.concatenate(label_parts), keys, meta


def build_daystate_matrix(runs, base_features, meta, race_keys, k, *,
                          single_race_margin=1, common_cutoffs=None,
                          signals=None, horse_types=None):
    """Return the six X1 columns and row-level state diagnostics."""
    signals = signals if signals is not None else build_asof_race_signals(runs)
    pace_type, speed_available = (horse_types if horse_types is not None
                                  else _horse_availability(runs))
    same_day = defaultdict(list)
    for (date, place, race_no), signal in signals.items():
        surface = signal["raw"]["track_type"]
        same_day[(date, place, surface)].append((int(race_no), signal))
    for rows in same_day.values():
        rows.sort(key=lambda item: item[0])

    tfeat_col = FEATURES.index("tfeat")
    matrix = np.zeros((len(meta), len(X1_FEATURES)), dtype=np.float32)
    diagnostics = []
    for index, (row, key) in enumerate(zip(meta, race_keys)):
        date, place, race_no = key
        if common_cutoffs is None:
            cutoff = int(race_no) - int(single_race_margin)
        else:
            cutoff = common_cutoffs.get(date, -1)
        prior = [(r, signal) for r, signal in same_day.get(
            (date, place, row.get("track_type")), ()) if r <= cutoff]
        n = len(prior)
        weight = shrinkage_weight(n, k)

        def state_value(name):
            values = [signal[name] for _r, signal in prior if signal[name] is not None]
            return (float(np.mean(values)) * weight, bool(values)) if values else (0.0, False)

        day_time, time_ok = state_value("time")
        day_pace, pace_ok = state_value("pace")
        day_draw, draw_ok = state_value("draw")
        identity = _run_identity(row)
        horse_pace = pace_type.get(identity)
        total, number = row.get("total_horses"), row.get("umaban")
        draw_pos = ((float(number) - 1.0) / (float(total) - 1.0)
                    if total and total > 1 and number and 1 <= number <= total else None)
        horse_speed_ok = bool(speed_available.get(identity))
        pace_available = pace_ok and horse_pace is not None
        draw_available = draw_ok and draw_pos is not None
        time_available = time_ok and horse_speed_ok
        matrix[index] = (
            day_pace * horse_pace if pace_available else 0.0,
            day_draw * draw_pos if draw_available else 0.0,
            day_time * float(base_features[index, tfeat_col]) if time_available else 0.0,
            float(pace_available), float(draw_available), float(time_available),
        )
        diagnostics.append({"n": n, "weight": weight, "cutoff": cutoff,
                            "day_time_resid": day_time, "day_pace_bias": day_pace,
                            "day_draw_bias": day_draw,
                            "pace_available": pace_available,
                            "draw_available": draw_available,
                            "time_available": time_available})
    return matrix, diagnostics


def _mask(dates, date_from, date_to):
    return (dates >= date_from) & (dates <= date_to)


def _selected(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def _fit_scaler_in_chunks(values, chunk_size=20000):
    """Fit StandardScaler without its full-matrix temporary allocation."""
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler(copy=False)
    for start in range(0, len(values), chunk_size):
        scaler.partial_fit(values[start:start + chunk_size])
    return scaler


def fit_conditional_logit_compact(Xz, y, race_keys, l2=1.0, max_iter=300):
    """Memory-bounded equivalent of backtest_ml.fit_conditional_logit.

    The diagnostic matrix stays float32 during repeated score/gradient matrix
    products.  L-BFGS parameters, returned gradients, and regularisation stay
    float64.  This function is isolated from production training.
    """
    from scipy.optimize import minimize
    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for index, key in enumerate(race_keys):
        rid[index] = rid_map.setdefault(key, len(rid_map))
    order = np.argsort(rid, kind="stable")
    Xs = np.asarray(Xz, dtype=np.float32)[order]
    ys = np.asarray(y)[order].astype(np.float32)
    rs = rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    segments = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rs)]))
    winners = np.add.reduceat(ys, starts)
    winner_sum = Xs[ys == 1].sum(axis=0, dtype=np.float64)

    def objective(weights):
        scores = Xs @ np.asarray(weights, dtype=np.float32)
        maxima = np.maximum.reduceat(scores, starts)
        exponentials = np.exp(scores - maxima[segments])
        totals = np.add.reduceat(exponentials, starts)
        logsumexp = maxima + np.log(totals)
        ll = (float(scores[ys == 1].sum(dtype=np.float64)
                    - np.sum(winners * logsumexp, dtype=np.float64))
              - 0.5 * l2 * float(weights @ weights))
        probabilities = exponentials / totals[segments]
        weighted = np.asarray(probabilities * winners[segments], dtype=np.float32)
        gradient = (np.asarray(Xs.T @ weighted, dtype=np.float64)
                    - winner_sum + l2 * weights)
        return -ll, gradient

    result = minimize(objective, np.zeros(Xs.shape[1], dtype=float), jac=True,
                      method="L-BFGS-B", options={"maxiter": max_iter})
    if not result.success:
        print(f"  [WARN] compact conditional logit: {result.message}")
    return result.x


def _fit_candidate(X, labels, keys, meta, dates, *, config, l2, k=None):
    train = _mask(dates, TRAIN_FROM, TRAIN_TO)
    selection = _mask(dates, SELECTION_FROM, SELECTION_TO)
    train_X = X[train]
    scaler = _fit_scaler_in_chunks(train_X)
    weights = fit_conditional_logit_compact(
        scaler.transform(train_X), labels[train], _selected(keys, train), l2=l2)
    selection_X = X[selection]
    scores = scaler.transform(selection_X) @ weights
    selection_keys = _selected(keys, selection)
    temperature = fit_temperature(scores, labels[selection], selection_keys)
    metrics = same_population_metrics(
        scores, labels[selection], selection_keys, _selected(meta, selection),
        temperature=temperature)
    return {"config": config, "l2": l2, "k": k, "scaler": scaler,
            "weights": weights, "temperature": temperature, "selection": metrics}


def run_grid(base, x1_by_k, labels, keys, meta, dates):
    candidates = [_fit_candidate(base, labels, keys, meta, dates,
                                 config="baseline", l2=l2) for l2 in L2_GRID]
    for k in K_GRID:
        X = np.hstack([base, x1_by_k[k]])
        for l2 in L2_GRID:
            candidates.append(_fit_candidate(
                X, labels, keys, meta, dates, config="+X1_interactions", l2=l2, k=k))
    return candidates, min(candidates,
        key=lambda row: row["selection"]["models"]["model"]["logloss"])


def _final_model(X, labels, keys, dates, candidate):
    train = _mask(dates, TRAIN_FROM, SELECTION_TO)
    train_X = X[train]
    scaler = _fit_scaler_in_chunks(train_X)
    weights = fit_conditional_logit_compact(
        scaler.transform(train_X), labels[train], _selected(keys, train),
        l2=candidate["l2"])
    return {"scaler": scaler, "weights": weights,
            "temperature": candidate["temperature"]}


def _score(model, X, labels, keys, meta, dates, period, *, row_mask=None,
           min_race_no=9):
    mask = _mask(dates, *period)
    if row_mask is not None:
        mask &= row_mask
    period_X = X[mask]
    scores = model["scaler"].transform(period_X) @ model["weights"]
    period_keys, period_meta = _selected(keys, mask), _selected(meta, mask)
    metrics = same_population_metrics(
        scores, labels[mask], period_keys, period_meta,
        temperature=model["temperature"], min_race_no=min_race_no)
    losses = race_level_winner_logloss(
        scores, labels[mask], period_keys, period_meta,
        temperature=model["temperature"], min_race_no=min_race_no)
    return metrics, losses


def _compact(metrics):
    return {"races": metrics["races"], "horses": metrics["horses"],
            "models": metrics["models"], "skipped": metrics["skipped"]}


def race_band_report(selected_model, baseline_model, selected_X, base,
                     labels, keys, meta, dates, diagnostics, period):
    bands = {"r1_4": (1, 4), "r5_8": (5, 8), "r9_12": (9, 12)}
    out = {}
    for name, (lo, hi) in bands.items():
        band_mask = np.asarray([lo <= int(key[2]) <= hi for key in keys])
        sm, _ = _score(selected_model, selected_X, labels, keys, meta, dates,
                       period, row_mask=band_mask, min_race_no=1)
        bm, _ = _score(baseline_model, base, labels, keys, meta, dates,
                       period, row_mask=band_mask, min_race_no=1)
        period_mask = _mask(dates, *period) & band_mask
        rows = [diag for diag, keep in zip(diagnostics, period_mask) if keep]
        out[name] = {
            "races": sm["races"],
            "selected_logloss": sm["models"]["model"]["logloss"],
            "baseline_logloss": bm["models"]["model"]["logloss"],
            "delta": (sm["models"]["model"]["logloss"]
                      - bm["models"]["model"]["logloss"]),
            "mean_n": float(np.mean([row["n"] for row in rows])) if rows else None,
            "n_zero_rate": float(np.mean([row["n"] == 0 for row in rows])) if rows else None,
            "n_histogram": {str(n): sum(row["n"] == n for row in rows)
                            for n in sorted({row["n"] for row in rows})},
            "availability": {field: float(np.mean([row[field] for row in rows])) if rows else None
                             for field in ("pace_available", "draw_available", "time_available")},
        }
    return out


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--json-out")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    global K_GRID, L2_GRID
    args = parse_args(argv)
    digest, hash_ok = verify_ability_db_hash(args.db)
    print(f"ability.db sha256 = {digest}")
    if not hash_ok or digest != ABILITY_DB_SHA256_EXPECTED:
        print(f"[STOP] expected={ABILITY_DB_SHA256_EXPECTED}")
        return 1
    cfg = load_win5_cfg()
    base, labels, keys, meta = build_base_dataset_yearwise(args.db, cfg)
    # The diagnostic retains three k-specific matrices.  float32 halves the
    # permanent footprint; optimization weights/objective remain float64.
    base = np.asarray(base, dtype=np.float32)
    dates = np.asarray([key[0] for key in keys])
    race_signals = _asof_signals_from_raw(load_race_observations_from_db(args.db))
    horse_types = load_horse_types_from_db(args.db, meta)
    if args.smoke:
        K_GRID, L2_GRID = (5.0,), (1.0,)
    x1_by_k, diagnostics_by_k = {}, {}
    for k in K_GRID:
        x1_by_k[k], diagnostics_by_k[k] = build_daystate_matrix(
            None, base, meta, keys, k, single_race_margin=1,
            signals=race_signals, horse_types=horse_types)
    candidates, best = run_grid(base, x1_by_k, labels, keys, meta, dates)
    selected_X = (base if best["config"] == "baseline" else
                  np.hstack([base, x1_by_k[best["k"]]]))
    selected_final = _final_model(selected_X, labels, keys, dates, best)
    selected_reference = {"scaler": best["scaler"], "weights": best["weights"],
                          "temperature": best["temperature"]}
    baseline_best = min((row for row in candidates if row["config"] == "baseline"),
                        key=lambda row: row["selection"]["models"]["model"]["logloss"])
    baseline_final = _final_model(base, labels, keys, dates, baseline_best)

    historical, paired = {}, {}
    report_periods = {"selection_2024": (SELECTION_FROM, SELECTION_TO), **PERIODS}
    for name, period in report_periods.items():
        model = selected_reference if name == "selection_2024" else selected_final
        sm, sl = _score(model, selected_X, labels, keys, meta, dates, period)
        bm_model = ({"scaler": baseline_best["scaler"], "weights": baseline_best["weights"],
                     "temperature": baseline_best["temperature"]}
                    if name == "selection_2024" else baseline_final)
        bm, bl = _score(bm_model, base, labels, keys, meta, dates, period)
        entry = {"selected": _compact(sm), "baseline": _compact(bm),
                 "market_topk_floor": [a >= b for a, b in zip(
                     sm["models"]["model"]["coverage"],
                     sm["models"]["market"]["coverage"])]}
        if name != "selection_2024":
            rm, _ = _score(selected_reference, selected_X, labels, keys, meta, dates, period)
            entry["trained_2021_2023_reference"] = _compact(rm)
        historical[name] = entry
        sb, bb = _blocks_by_date(sl), _blocks_by_date(bl)
        boot = paired_block_bootstrap(_mean_metric, sb, bb,
                                      n_resamples=BOOTSTRAP_RESAMPLES,
                                      seed=BOOTSTRAP_SEED)
        paired[name] = {"observed_diff": boot.observed_difference,
                        "ci_low": boot.ci_low, "ci_high": boot.ci_high,
                        "p_value": boot.p_value, "day_blocks": len(sb)}

    sensitivity, race_bands, win5_report = {}, {}, {}
    if best["config"] == "+X1_interactions":
        strict_matrix, strict_diag = build_daystate_matrix(
            None, base, meta, keys, best["k"], single_race_margin=2,
            signals=race_signals, horse_types=horse_types)
        strict_X = np.hstack([base, strict_matrix])
        for name, period in report_periods.items():
            model = selected_reference if name == "selection_2024" else selected_final
            metrics, _ = _score(model, strict_X, labels, keys, meta, dates, period)
            baseline_model = ({"scaler": baseline_best["scaler"],
                               "weights": baseline_best["weights"],
                               "temperature": baseline_best["temperature"]}
                              if name == "selection_2024" else baseline_final)
            baseline_metrics, _ = _score(
                baseline_model, base, labels, keys, meta, dates, period)
            sensitivity[name] = {
                "selected": _compact(metrics),
                "baseline": _compact(baseline_metrics),
                "logloss_delta": (metrics["models"]["model"]["logloss"]
                                  - baseline_metrics["models"]["model"]["logloss"]),
            }
            race_bands[name] = race_band_report(
                model, baseline_model, selected_X, base, labels, keys, meta, dates,
                diagnostics_by_k[best["k"]], period)

        for margin in (2, 3):
            cutoffs, targets = win5_cutoffs(keys, margin=margin)
            common_matrix, _common_diag = build_daystate_matrix(
                None, base, meta, keys, best["k"], common_cutoffs=cutoffs,
                signals=race_signals, horse_types=horse_types)
            common_X = np.hstack([base, common_matrix])
            target_mask = np.asarray([key in targets for key in keys])
            policy = "registered_first_target_minus_2" if margin == 2 else "strict_first_target_minus_3"
            win5_report[policy] = {}
            for name, period in report_periods.items():
                model = selected_reference if name == "selection_2024" else selected_final
                baseline_model = ({"scaler": baseline_best["scaler"],
                                   "weights": baseline_best["weights"],
                                   "temperature": baseline_best["temperature"]}
                                  if name == "selection_2024" else baseline_final)
                sm, sl = _score(model, common_X, labels, keys, meta, dates, period,
                                row_mask=target_mask, min_race_no=1)
                bm, bl = _score(baseline_model, base, labels, keys, meta, dates, period,
                                row_mask=target_mask, min_race_no=1)
                sb, bb = _blocks_by_date(sl), _blocks_by_date(bl)
                boot = paired_block_bootstrap(_mean_metric, sb, bb,
                                              n_resamples=BOOTSTRAP_RESAMPLES,
                                              seed=BOOTSTRAP_SEED)
                win5_report[policy][name] = {
                    "selected": _compact(sm), "baseline": _compact(bm),
                    "observed_diff": boot.observed_difference,
                    "ci_low": boot.ci_low, "ci_high": boot.ci_high,
                    "p_value": boot.p_value, "day_blocks": len(sb)}

    payload = {
        "experiment_id": EXPERIMENT_ID, "ability_db_sha256": digest,
        "definitions": {
            "baseline": "expanding history strictly before date; exact place/surface/distance-band/condition, fixed fallback at n<20",
            "single_race_policy": "same venue races r' <= r-1",
            "single_race_sensitivity": "same venue races r' <= r-2",
            "win5_policy": "common cutoff min(approximated five target race numbers)-2; sensitivity -3",
            "speed_interaction": "day_time_resid * raw tfeat; equivalent to standardized tfeat in conditional logit because the subtracted state main effect is race-constant and scaling is absorbed by the fitted coefficient",
            "distance_bands": {"sprint": "<=1400", "mile": "1401-1800", "route": ">1800"},
            "features": list(X1_FEATURES)},
        "candidates": [{"config": row["config"], "k": row["k"], "l2": row["l2"],
                         "selection_2024": row["selection"]["models"]}
                       for row in candidates],
        "selected": {"config": best["config"], "k": best["k"], "l2": best["l2"]},
        "periods": historical, "paired_day_bootstrap": paired,
        "strict_single_race_sensitivity": sensitivity,
        "race_number_bands": race_bands, "win5_common_cutoff": win5_report,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=float))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
