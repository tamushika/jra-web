"""T81 Stage A: isolated rank-display research harness.

SPEC: docs/codex/SPEC-T81-rank-display-limited-validation.md

Scope (Stage A only -- see SPEC SS3):
  - ``--audit-only``: population/schema/quality/hash audit against the real,
    read-only ``ability.db``.  Builds the 21-feature dataset (needed to know
    which races have a complete feature field) but never fits a model, never
    computes a score/rank correlation against real outcomes, and never reads
    a real historical result for scoring purposes.
  - ``--smoke``: exercises the *entire* candidate/control/market pipeline
    (PL fit, L2 selection, historical evaluation, paired bootstrap, machine
    adjudication) end to end on a small deterministic synthetic dataset.  No
    DB connection, no network access.
  - ``--run-historical``: the Stage B interface.  It first verifies a
    matching T39 ledger registration exists; because Stage A never creates
    one, invoking this before registration fails closed by design.

This module must not modify the production model, factor snapshot, weights,
rule CSVs, or any DB.  It reuses (never edits) the shared research code in
``backtest_rank_objective.py``, ``backtest_stats_retrain.py``,
``backtest_ability.py``, ``backtest_win5.py``, ``backtest_criteria.py``,
``fold_stats.py`` and ``eval/blocks.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backtest_ability import load_runs  # noqa: E402
from backtest_criteria import VENUES as JRA_VENUES  # noqa: E402
from backtest_ml import FEATURES, PEDIGREE_MIN_COVERAGE  # noqa: E402
from backtest_rank_objective import pl_objective_and_gradient  # noqa: E402
from backtest_stats_retrain import build_consistent_feature_dataset  # noqa: E402
from backtest_win5 import load_win5_cfg, parse_final_odds  # noqa: E402
from eval.blocks import paired_block_bootstrap  # noqa: E402


EXPERIMENT_ID = "T81-rank-display-pl3-v1"
DB_PATH = os.path.join(BASE_DIR, "ability.db")
PEDIGREE_CACHE_PATH = os.path.join(BASE_DIR, "pedigree_cache.json")
LEDGER_PATH = os.path.join(BASE_DIR, "eval", "experiments.jsonl")

DATA_FROM, DATA_TO = "20210101", "20260630"
LOOKBACK_FROM = "20180101"  # informational only; load_runs derives this itself

SPEC_FEATURES = (
    "j_pts", "f_pts", "tfeat", "cfeat", "agari_flag", "ln_odds", "prev_rank",
    "ln_interval", "age", "is_male", "kinryo", "weight", "n_prior", "wet_match",
    "dist_pts", "surf_pts", "affi_pts", "pace_fit", "course_fit", "grade_pts",
    "sire_pts",
)

CANDIDATE_OBJECTIVE = "pl_top3"
CONTROL_OBJECTIVE = "pl_top1"
L2_GRID = (0.3, 1.0, 3.0)
SELECTION_FOLDS = (
    # (label, train_from, train_to, eval_from, eval_to)
    ("fold1", "20210101", "20211231", "20220101", "20221231"),
    ("fold2", "20210101", "20221231", "20230101", "20231231"),
    ("fold3", "20210101", "20231231", "20240101", "20241231"),
)
FINAL_TRAIN_FROM, FINAL_TRAIN_TO = "20210101", "20241231"
HISTORICAL_PERIODS = {
    "2025": ("20250101", "20251231"),
    "2026H1": ("20260101", "20260630"),
}
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 81
MIN_RACE_NO, MAX_RACE_NO = 1, 12
MIN_FIELD_SIZE = 8
L2_TIE_EPS = 1e-12

SPEC_PATH = os.path.join(BASE_DIR, "docs", "codex",
                         "SPEC-T81-rank-display-limited-validation.md")


class RankDisplayError(RuntimeError):
    """Base error for anything that must stop the T81 harness (INVALID)."""


class NonConvergenceError(RankDisplayError):
    """Raised when the PL optimizer does not report success."""


class QualityError(RankDisplayError):
    """Raised for NaN/Inf metrics or a broken input/time-point contract."""


# ---------------------------------------------------------------------------
# Hashing / read-only DB / git identity helpers
# ---------------------------------------------------------------------------

def sha256_file(path) -> str | None:
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """Open ability.db strictly read-only (mode=ro + query_only=ON, SPEC SS4.1)."""
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def assert_connection_is_readonly(conn: sqlite3.Connection) -> None:
    """Fail closed if a write ever becomes possible on this connection."""
    try:
        conn.execute("CREATE TABLE __t81_write_probe (x INTEGER)")
    except sqlite3.OperationalError:
        return
    # If the statement above did not raise, undo it and fail loudly: this
    # connection is not actually read-only and must not be used.
    conn.execute("DROP TABLE IF EXISTS __t81_write_probe")
    raise QualityError("ability.db connection accepted a write; not read-only")


def git_identity(base_dir: str = BASE_DIR) -> dict:
    def run(args):
        try:
            out = subprocess.run(
                ["git", *args], cwd=base_dir, capture_output=True,
                text=True, check=False, timeout=30)
            return out.stdout.strip() if out.returncode == 0 else None
        except OSError:
            return None

    head = run(["rev-parse", "HEAD"])
    status = run(["status", "--porcelain"])
    return {
        "head": head,
        "uncommitted_changes": bool(status) if status is not None else None,
        "status_lines": status.splitlines() if status else [],
    }


def python_and_library_versions() -> dict:
    import scipy
    import sklearn
    return {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "scikit_learn": sklearn.__version__,
    }


# ---------------------------------------------------------------------------
# Pedigree fail-closed adapter (SPEC SS4.3)
# ---------------------------------------------------------------------------

def validate_pedigree_cache(path: str) -> dict:
    """Load and validate the local pedigree cache. Never touches the network.

    Raises ``QualityError`` if the file is missing or not a well-formed
    ``{horse: {"sire": ..., "bms": ...}}`` mapping; callers must stop rather
    than silently degrade to an empty/absent pedigree source.
    """
    if not os.path.isfile(path):
        raise QualityError(f"pedigree cache not found (fail-closed): {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise QualityError(f"pedigree cache is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise QualityError(f"pedigree cache is not a JSON object: {path}")
    for horse, value in data.items():
        if not isinstance(horse, str) or not isinstance(value, dict):
            raise QualityError(
                f"pedigree cache entry has an unexpected shape: {horse!r}")
    return data


@contextmanager
def isolated_pedigree_adapter(cache_path: str = PEDIGREE_CACHE_PATH):
    """Patch ``pedigree_store.load_all`` to a validated, local-only closure.

    The production function reachable from ``backtest_ml.build_dataset``
    (``pedigree_store.load_all``) falls back to a Neon network read when the
    local cache is missing.  This adapter validates the cache *before*
    entering scope (fail-closed on any problem) and then returns that frozen
    dict for every call inside the ``with`` block -- no disk or network I/O
    happens while the adapter is active.  The production function object is
    restored afterwards, unmodified.
    """
    import pedigree_store

    validated = validate_pedigree_cache(cache_path)

    def _frozen_loader(use_cache: bool = True, refresh: bool = False):
        if refresh:
            raise QualityError(
                "T81 harness forbids pedigree refresh (would require network)")
        return validated

    original = pedigree_store.load_all
    pedigree_store.load_all = _frozen_loader
    try:
        yield validated
    finally:
        pedigree_store.load_all = original


# ---------------------------------------------------------------------------
# Population contract (SPEC SS6): raw judgement -> row/horse match -> feature
# -> odds, in that priority order, with all flags additionally recorded.
# ---------------------------------------------------------------------------

RAW_REASON_ORDER = (
    "invalid_race_key_or_scope",
    "headcount_invalid",
    "raw_duplicate_or_row_mismatch",
    "rank_not_strict_1_to_n",
)
FEATURE_REASON_ORDER = ("feature_row_mismatch", "feature_nonfinite")
ODDS_REASON = "missing_or_invalid_odds"
ALL_REASON_ORDER = RAW_REASON_ORDER + FEATURE_REASON_ORDER + (ODDS_REASON,)


def _surface_ok(track_type) -> bool:
    return track_type in ("芝", "ダート")


def group_raw_races(runs, date_from, date_to):
    grouped = defaultdict(list)
    for row in runs:
        date = row.get("date")
        if date is None or not (date_from <= date <= date_to):
            continue
        place, race_no = row.get("place"), row.get("r")
        if place is None or race_no is None:
            continue
        grouped[(date, place, race_no)].append(row)
    return grouped


@dataclass
class RaceClassification:
    key: tuple
    flags: set
    primary_reason: str | None  # None means eligible
    n_declared: int | None
    n_raw: int
    n_features: int


def classify_raw_race(key, rows) -> tuple[set, int | None, int]:
    """Apply SPEC SS6 checks 1/2/5/6 to one race's raw rows."""
    date, place, race_no = key
    flags = set()

    venue_ok = place in JRA_VENUES
    race_no_ok = isinstance(race_no, int) and MIN_RACE_NO <= race_no <= MAX_RACE_NO
    surfaces_ok = all(_surface_ok(row.get("track_type")) for row in rows)
    if not (venue_ok and race_no_ok and surfaces_ok):
        flags.add("invalid_race_key_or_scope")

    declared = {row.get("total_horses") for row in rows}
    n_declared = None
    if len(declared) == 1:
        value = next(iter(declared))
        if isinstance(value, int) and value >= MIN_FIELD_SIZE:
            n_declared = value
    if n_declared is None:
        flags.add("headcount_invalid")

    umabans = [row.get("umaban") for row in rows]
    horses = [str(row.get("horse") or "").strip() for row in rows]
    n_raw = len(rows)
    umaban_ok = all(u is not None for u in umabans) and len(set(umabans)) == n_raw
    horse_ok = all(horses) and len(set(horses)) == n_raw
    rowcount_ok = n_declared is not None and n_raw == n_declared
    if not (umaban_ok and horse_ok and rowcount_ok):
        flags.add("raw_duplicate_or_row_mismatch")

    parsed_ranks = []
    rank_parse_ok = True
    for row in rows:
        try:
            rank = int(row.get("rank"))
        except (TypeError, ValueError):
            rank_parse_ok = False
            break
        if rank <= 0:
            rank_parse_ok = False
            break
        parsed_ranks.append(rank)
    if not rank_parse_ok or len(set(parsed_ranks)) != len(parsed_ranks):
        flags.add("rank_not_strict_1_to_n")
    elif n_declared is not None and set(parsed_ranks) != set(range(1, n_declared + 1)):
        flags.add("rank_not_strict_1_to_n")
    elif n_declared is None and set(parsed_ranks) != set(range(1, n_raw + 1)):
        flags.add("rank_not_strict_1_to_n")

    return flags, n_declared, n_raw


@dataclass
class PopulationResult:
    eligible_keys: set
    primary_reason: dict  # key -> reason string
    all_flags: dict  # key -> set[str]
    features: np.ndarray
    labels: np.ndarray
    race_keys: list
    meta: list
    duplicate_horse_day_violations: list
    counts_by_year: dict
    counts_by_venue: dict
    counts_by_surface: dict
    counts_by_field_band: dict
    total_candidate_races: int


def _field_band(n: int) -> str:
    if n <= 11:
        return "8-11"
    if n <= 15:
        return "12-15"
    return "16+"


def audit_same_day_duplicate_horse_history(runs) -> list:
    """SPEC SS5: a horse must not run twice on the same calendar date.

    Such a duplicate would break the ``lst[:i]`` "before this row" history
    slice used throughout the shared feature builders.  Returns a list of
    (horse, date) violations; an empty list is the expected, healthy case.
    """
    seen = defaultdict(set)
    violations = []
    for row in runs:
        horse = row.get("horse")
        date = row.get("date")
        if not horse or not date:
            continue
        if date in seen[horse]:
            violations.append((horse, date))
        seen[horse].add(date)
    return violations


def build_population(runs, features, labels, race_keys, meta,
                      date_from=DATA_FROM, date_to=DATA_TO) -> PopulationResult:
    raw_groups = group_raw_races(runs, date_from, date_to)

    feature_groups = defaultdict(list)
    for index, key in enumerate(race_keys):
        if date_from <= key[0] <= date_to:
            feature_groups[key].append(index)

    primary_reason: dict = {}
    all_flags: dict = {}
    eligible_keys = set()
    counts_by_year = defaultdict(lambda: [0, 0])       # [eligible, total]
    counts_by_venue = defaultdict(lambda: [0, 0])
    counts_by_surface = defaultdict(lambda: [0, 0])
    counts_by_field_band = defaultdict(lambda: [0, 0])

    for key, raw_rows in raw_groups.items():
        flags, n_declared, n_raw = classify_raw_race(key, raw_rows)
        feature_indices = feature_groups.get(key, [])
        n_features = len(feature_indices)
        n_reference = n_declared if n_declared is not None else n_raw

        # These two checks run independently of the raw-judgement flags above
        # so that ``all_flags`` genuinely records every applicable reason
        # (SPEC SS6: "別に全理由のフラグも記録する"), not only the first one.
        raw_horses = {str(row.get("horse") or "").strip() for row in raw_rows}
        feature_horses = {
            str(meta[i].get("horse") or "").strip() for i in feature_indices}
        if n_features != n_reference or feature_horses != raw_horses:
            flags.add("feature_row_mismatch")
        elif not all(
            np.all(np.isfinite(features[i])) and features[i].shape[0] == 21
            for i in feature_indices
        ):
            flags.add("feature_nonfinite")

        odds_values = [
            parse_final_odds(meta[i].get("win_pay"), meta[i].get("rank"))
            for i in feature_indices]
        if (not odds_values
                or any(v is None or not math.isfinite(v) or v <= 1.0 for v in odds_values)):
            flags.add(ODDS_REASON)

        all_flags[key] = flags
        is_eligible = not flags
        if is_eligible:
            eligible_keys.add(key)
        else:
            for reason in ALL_REASON_ORDER:
                if reason in flags:
                    primary_reason[key] = reason
                    break

        year = key[0][:4]
        place = key[1]
        surface = raw_rows[0].get("track_type") if raw_rows else None
        band = _field_band(n_declared if n_declared is not None else n_raw)
        for bucket, dim_key in (
            (counts_by_year, year), (counts_by_venue, place),
            (counts_by_surface, surface), (counts_by_field_band, band),
        ):
            bucket[dim_key][1] += 1
            if is_eligible:
                bucket[dim_key][0] += 1

    keep_mask = np.asarray([key in eligible_keys for key in race_keys], dtype=bool)
    filtered_features = features[keep_mask]
    filtered_labels = labels[keep_mask]
    filtered_keys = [key for key, keep in zip(race_keys, keep_mask) if keep]
    filtered_meta = [row for row, keep in zip(meta, keep_mask) if keep]

    return PopulationResult(
        eligible_keys=eligible_keys,
        primary_reason=primary_reason,
        all_flags=all_flags,
        features=filtered_features,
        labels=filtered_labels,
        race_keys=filtered_keys,
        meta=filtered_meta,
        duplicate_horse_day_violations=audit_same_day_duplicate_horse_history(runs),
        counts_by_year={k: tuple(v) for k, v in counts_by_year.items()},
        counts_by_venue={k: tuple(v) for k, v in counts_by_venue.items()},
        counts_by_surface={k: tuple(v) for k, v in counts_by_surface.items()},
        counts_by_field_band={k: tuple(v) for k, v in counts_by_field_band.items()},
        total_candidate_races=len(raw_groups),
    )


# ---------------------------------------------------------------------------
# sire_pts fold gate (SPEC SS5): strict, leak-free, per fold train-period
# coverage.  ``backtest_ml.build_dataset`` itself computes a *global* (and
# therefore potentially leaky across our fold boundaries) pedigree coverage
# gate once per calendar year; see SS5 discussion in the final report for why
# this harness combines the two gates conservatively (AND) instead of
# overriding the shared function.
# ---------------------------------------------------------------------------

def strict_sire_gate(runs, pedigree, train_from, train_to) -> dict:
    horses = {
        str(row.get("horse") or "").strip()
        for row in runs
        if row.get("horse") and train_from <= (row.get("date") or "") <= train_to
        and _surface_ok(row.get("track_type")) and row.get("place") in JRA_VENUES
    }
    horses.discard("")
    if not horses:
        return {"coverage": 0.0, "threshold": PEDIGREE_MIN_COVERAGE,
                "enabled": False, "eligible_horses": 0}
    covered = sum(1 for h in horses if h in pedigree and pedigree[h].get("sire"))
    coverage = covered / len(horses)
    return {
        "coverage": coverage, "threshold": PEDIGREE_MIN_COVERAGE,
        "enabled": coverage >= PEDIGREE_MIN_COVERAGE,
        "eligible_horses": len(horses),
    }


def apply_sire_gate(features: np.ndarray, gate_enabled: bool) -> np.ndarray:
    """Conservative override: zero ``sire_pts`` whenever the strict fold gate
    says disabled.  Never invents a value the shared builder did not already
    compute; only ever removes information relative to the shared function's
    own (potentially leakier) decision."""
    if gate_enabled:
        return features
    out = features.copy()
    out[:, SPEC_FEATURES.index("sire_pts")] = 0.0
    return out


# ---------------------------------------------------------------------------
# Ranking / metrics (SPEC SS8)
# ---------------------------------------------------------------------------

def average_rank_ties(values: np.ndarray, descending: bool = True) -> np.ndarray:
    """1-indexed rank with ties resolved by the average-rank convention."""
    values = np.asarray(values, dtype=float)
    order = np.argsort(-values if descending else values, kind="stable")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=float)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def spearman_with_flag(score: np.ndarray, actual_rank: np.ndarray) -> tuple[float, bool]:
    """Spearman(score, -actual_rank) with average-rank ties.

    Returns ``(rho, all_tied)``.  ``all_tied`` is True only when every score
    in the race is identical (undefined correlation, spec-mandated 0.0).
    Any other non-finite outcome is a quality error.
    """
    score = np.asarray(score, dtype=float)
    n = len(score)
    if n < 2:
        raise QualityError("spearman requires at least 2 runners")
    if np.all(score == score[0]):
        return 0.0, True
    score_rank = average_rank_ties(score, descending=True)
    outcome_rank = average_rank_ties(-np.asarray(actual_rank, dtype=float), descending=True)
    sx = score_rank - score_rank.mean()
    sy = outcome_rank - outcome_rank.mean()
    denom = math.sqrt(float(sx @ sx) * float(sy @ sy))
    if denom == 0.0:
        return 0.0, True
    rho = float(sx @ sy) / denom
    if not math.isfinite(rho):
        raise QualityError("spearman correlation is not finite")
    return rho, False


def display_order(score: np.ndarray, umaban: np.ndarray) -> np.ndarray:
    """Unique predicted rank: score desc -> umaban asc (SPEC SS8)."""
    score = np.asarray(score, dtype=float)
    umaban = np.asarray(umaban, dtype=float)
    order = sorted(range(len(score)), key=lambda i: (-score[i], umaban[i]))
    ranks = np.empty(len(score), dtype=int)
    for position, index in enumerate(order, start=1):
        ranks[index] = position
    return ranks


def top3_set_recall(pred_rank: np.ndarray, actual_rank: np.ndarray) -> float:
    n = len(pred_rank)
    predicted_top3 = {i for i in range(n) if pred_rank[i] <= 3}
    actual_top3 = {i for i in range(n) if actual_rank[i] <= 3}
    return len(predicted_top3 & actual_top3) / 3.0


def actual_top3_rank_mae(pred_rank: np.ndarray, actual_rank: np.ndarray) -> float:
    n = len(pred_rank)
    if n <= 1:
        raise QualityError("actual_top3_rank_mae requires at least 2 runners")
    winners = [i for i in range(n) if actual_rank[i] <= 3]
    total = sum(abs(int(pred_rank[i]) - int(actual_rank[i])) for i in winners)
    return (total / 3.0) / (n - 1)


def top3_order_exact(pred_rank: np.ndarray, actual_rank: np.ndarray) -> int:
    n = len(pred_rank)
    predicted_order = [None, None, None]
    actual_order = [None, None, None]
    for i in range(n):
        if pred_rank[i] <= 3:
            predicted_order[int(pred_rank[i]) - 1] = i
        if actual_rank[i] <= 3:
            actual_order[int(actual_rank[i]) - 1] = i
    return int(predicted_order == actual_order and None not in predicted_order)


@dataclass
class RaceMetrics:
    key: tuple
    n: int
    race_no: int
    spearman: float
    spearman_all_tied: bool
    top3_recall: float
    top3_mae: float
    top3_exact: int
    winner_in_top1: int
    winner_in_top2: int
    winner_in_top3: int
    winner_logloss: float


def _softmax_neglogp_winner(score: np.ndarray, actual_rank: np.ndarray,
                            temperature: float = 1.0) -> float:
    values = np.asarray(score, dtype=float) / float(temperature)
    values = values - values.max()
    exp_values = np.exp(values)
    probabilities = exp_values / exp_values.sum()
    winner_index = int(np.argmin(actual_rank))
    return -math.log(max(1e-15, float(probabilities[winner_index])))


def compute_race_metrics(key, score: np.ndarray, actual_rank: np.ndarray,
                         umaban: np.ndarray) -> RaceMetrics:
    if not np.all(np.isfinite(score)):
        raise QualityError(f"non-finite score for race {key}")
    rho, all_tied = spearman_with_flag(score, actual_rank)
    pred_rank = display_order(score, umaban)
    winner_index = int(np.argmin(actual_rank))
    winner_pred_rank = int(pred_rank[winner_index])
    return RaceMetrics(
        key=key, n=len(score), race_no=key[2],
        spearman=rho, spearman_all_tied=all_tied,
        top3_recall=top3_set_recall(pred_rank, actual_rank),
        top3_mae=actual_top3_rank_mae(pred_rank, actual_rank),
        top3_exact=top3_order_exact(pred_rank, actual_rank),
        winner_in_top1=int(winner_pred_rank <= 1),
        winner_in_top2=int(winner_pred_rank <= 2),
        winner_in_top3=int(winner_pred_rank <= 3),
        winner_logloss=_softmax_neglogp_winner(score, actual_rank),
    )


def race_metrics_for_population(scores: np.ndarray, race_keys, meta) -> dict:
    grouped = defaultdict(list)
    for index, key in enumerate(race_keys):
        grouped[key].append(index)
    out = {}
    for key, indices in grouped.items():
        score = scores[indices]
        actual_rank = np.asarray([int(meta[i]["rank"]) for i in indices])
        umaban = np.asarray([float(meta[i].get("umaban") or 0) for i in indices])
        out[key] = compute_race_metrics(key, score, actual_rank, umaban)
    return out


# ---------------------------------------------------------------------------
# PL model fitting (SPEC SS7): reuses backtest_rank_objective's loss/gradient.
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    weights: np.ndarray
    objective: str
    l2: float


def fit_pl_model(features, ranks, race_keys, *, objective, l2, max_iter=300):
    from scipy.optimize import minimize
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(features)
    standardized = scaler.transform(features)
    result = minimize(
        lambda w: pl_objective_and_gradient(
            w, standardized, ranks, race_keys, objective=objective, l2=l2),
        np.zeros(standardized.shape[1]), jac=True, method="L-BFGS-B",
        options={"maxiter": max_iter})
    if not result.success or not np.all(np.isfinite(result.x)):
        raise NonConvergenceError(
            f"PL fit did not converge (objective={objective}, l2={l2}): "
            f"{result.message}")
    return FitResult(
        scaler_mean=scaler.mean_, scaler_scale=scaler.scale_,
        weights=result.x, objective=objective, l2=l2)


def score_with_fit(fit: FitResult, features) -> np.ndarray:
    standardized = (features - fit.scaler_mean) / fit.scaler_scale
    return standardized @ fit.weights


def market_score(meta) -> np.ndarray:
    odds = np.asarray(
        [parse_final_odds(row.get("win_pay"), row.get("rank")) for row in meta],
        dtype=float)
    if np.any(~np.isfinite(odds)) or np.any(odds <= 1.0):
        raise QualityError("market_score requires valid odds for every runner")
    return -np.log(odds)


def _period_mask(race_keys, date_from, date_to):
    return np.asarray([date_from <= key[0] <= date_to for key in race_keys], dtype=bool)


def _select(rows, mask):
    return [row for row, keep in zip(rows, mask) if keep]


def race_mean_spearman(features, meta, race_keys, mask) -> tuple[float, int, int]:
    """Race-mean Spearman for one already-scored period. Returns
    (mean, n_races, n_all_tied)."""
    grouped = defaultdict(list)
    for index, (key, keep) in enumerate(zip(race_keys, mask)):
        if keep:
            grouped[key].append(index)
    values, all_tied_count = [], 0
    for key, indices in grouped.items():
        score = features[indices]
        actual_rank = np.asarray([int(meta[i]["rank"]) for i in indices])
        rho, all_tied = spearman_with_flag(score, actual_rank)
        values.append(rho)
        all_tied_count += int(all_tied)
    if not values:
        raise QualityError("race_mean_spearman requires at least one race")
    return float(np.mean(values)), len(values), all_tied_count


def select_l2(features, ranks, meta, race_keys, objective, sire_gates) -> dict:
    """SPEC SS7 L2 selection: fold-count-weighted race_mean_spearman.

    ``sire_gates`` maps each fold label to its ``strict_sire_gate`` result;
    the fold's own gate decision is applied (SPEC SS5) to both that fold's
    training rows and its evaluation rows before fitting/scoring.
    """
    per_l2 = []
    for l2 in L2_GRID:
        fold_rows = []
        weighted_sum, weighted_n = 0.0, 0
        for label, train_from, train_to, eval_from, eval_to in SELECTION_FOLDS:
            fold_features = apply_sire_gate(features, sire_gates[label]["enabled"])
            train_mask = _period_mask(race_keys, train_from, train_to)
            eval_mask = _period_mask(race_keys, eval_from, eval_to)
            train_keys = _select(race_keys, train_mask)
            fit = fit_pl_model(fold_features[train_mask], ranks[train_mask], train_keys,
                               objective=objective, l2=l2)
            eval_scores = score_with_fit(fit, fold_features)
            mean_rho, n_races, n_all_tied = race_mean_spearman(
                eval_scores, meta, race_keys, eval_mask)
            weighted_sum += mean_rho * n_races
            weighted_n += n_races
            fold_rows.append({
                "fold": label, "train": (train_from, train_to),
                "eval": (eval_from, eval_to), "race_mean_spearman": mean_rho,
                "races": n_races, "all_tied_races": n_all_tied,
                "sire_gate_enabled": sire_gates[label]["enabled"],
            })
        weighted_mean = weighted_sum / weighted_n if weighted_n else float("nan")
        per_l2.append({"l2": l2, "weighted_race_mean_spearman": weighted_mean,
                       "total_races": weighted_n, "folds": fold_rows})

    best_value = max(row["weighted_race_mean_spearman"] for row in per_l2)
    tied = [row for row in per_l2 if
            best_value - row["weighted_race_mean_spearman"] <= L2_TIE_EPS]
    selected = max(tied, key=lambda row: row["l2"])
    return {"objective": objective, "candidates": per_l2, "selected_l2": selected["l2"],
           "selected": selected}


# ---------------------------------------------------------------------------
# Bootstrap + machine adjudication (SPEC SS9)
# ---------------------------------------------------------------------------

def _blocks_by_date(race_metric_by_key: dict) -> dict:
    blocks = defaultdict(list)
    for key, value in race_metric_by_key.items():
        blocks[key[0]].append(value)
    return dict(blocks)


def _mean_metric(rows) -> float:
    return sum(rows) / len(rows)


def paired_metric_diff(candidate_metrics: dict, comparator_metrics: dict,
                       attribute: str, *, n_resamples=BOOTSTRAP_RESAMPLES,
                       seed=BOOTSTRAP_SEED):
    a_by_key = {key: getattr(value, attribute) for key, value in candidate_metrics.items()}
    b_by_key = {key: getattr(value, attribute) for key, value in comparator_metrics.items()}
    if set(a_by_key) != set(b_by_key):
        raise QualityError(
            "paired bootstrap requires identical race populations between models")
    blocks_a = _blocks_by_date(a_by_key)
    blocks_b = _blocks_by_date(b_by_key)
    result = paired_block_bootstrap(
        _mean_metric, blocks_a, blocks_b, n_resamples=n_resamples, seed=seed)
    return {
        "observed_difference": result.observed_difference,
        "ci_low": result.ci_low, "ci_high": result.ci_high,
        "p_value": result.p_value, "n_resamples": result.n_resamples,
        "n_day_blocks": result.n_blocks, "seed": result.seed,
    }


@dataclass
class PeriodComparison:
    period: str
    n_races: int
    spearman_vs_market: dict
    spearman_vs_top1: dict
    top3_recall_vs_market: dict
    top3_recall_vs_top1: dict
    top3_mae_vs_market: dict
    top3_mae_vs_top1: dict
    winner_reference: dict


def compare_period(candidate_metrics: dict, top1_metrics: dict, market_metrics: dict,
                   period: str) -> PeriodComparison:
    if not (set(candidate_metrics) == set(top1_metrics) == set(market_metrics)):
        raise QualityError(f"race population differs between models in {period}")

    def _aggregate(subset):
        n = len(subset)
        if n == 0:
            return {"n": 0, "winner_in_top1": None, "winner_in_top2": None,
                    "winner_in_top3": None, "winner_logloss": None}
        return {
            "n": n,
            "winner_in_top1": sum(v.winner_in_top1 for v in subset.values()) / n,
            "winner_in_top2": sum(v.winner_in_top2 for v in subset.values()) / n,
            "winner_in_top3": sum(v.winner_in_top3 for v in subset.values()) / n,
            "winner_logloss": sum(v.winner_logloss for v in subset.values()) / n,
        }

    def winner_row(metrics):
        # SPEC SS8: report the full eligible population and, separately with
        # its own denominator, the 9R+ subset (the WIN5-style scope some
        # existing common code defaults to) -- never mix the two tables.
        nine_plus = {key: value for key, value in metrics.items()
                    if value.race_no is not None and value.race_no >= 9}
        return {"all_eligible_races": _aggregate(metrics),
               "race9_plus_subset": _aggregate(nine_plus)}

    return PeriodComparison(
        period=period, n_races=len(candidate_metrics),
        spearman_vs_market=paired_metric_diff(candidate_metrics, market_metrics, "spearman"),
        spearman_vs_top1=paired_metric_diff(candidate_metrics, top1_metrics, "spearman"),
        top3_recall_vs_market=paired_metric_diff(candidate_metrics, market_metrics, "top3_recall"),
        top3_recall_vs_top1=paired_metric_diff(candidate_metrics, top1_metrics, "top3_recall"),
        top3_mae_vs_market=paired_metric_diff(candidate_metrics, market_metrics, "top3_mae"),
        top3_mae_vs_top1=paired_metric_diff(candidate_metrics, top1_metrics, "top3_mae"),
        winner_reference={
            "candidate": winner_row(candidate_metrics),
            "top1_control": winner_row(top1_metrics),
            "market": winner_row(market_metrics),
        },
    )


def adjudicate(comparisons: dict, quality_ok: bool) -> str:
    """SPEC SS9 fixed machine judgment."""
    if not quality_ok:
        return "INVALID"

    point_ok = True
    ci_ok = True
    for period, cmp in comparisons.items():
        if cmp.spearman_vs_market["observed_difference"] <= 0:
            point_ok = False
        if cmp.spearman_vs_top1["observed_difference"] <= 0:
            point_ok = False
        if cmp.top3_recall_vs_market["observed_difference"] < 0:
            point_ok = False
        if cmp.top3_recall_vs_top1["observed_difference"] < 0:
            point_ok = False
        if cmp.top3_mae_vs_market["observed_difference"] > 0:
            point_ok = False
        if cmp.top3_mae_vs_top1["observed_difference"] > 0:
            point_ok = False
        if period == "2025":
            if cmp.spearman_vs_market["ci_low"] <= 0:
                ci_ok = False
            if cmp.spearman_vs_top1["ci_low"] <= 0:
                ci_ok = False

    if not point_ok:
        return "HISTORICAL_SCREEN_FAIL"
    if not ci_ok:
        return "INCONCLUSIVE"
    return "HISTORICAL_SCREEN_PASS"


# ---------------------------------------------------------------------------
# Orchestration shared by --smoke and --run-historical
# ---------------------------------------------------------------------------

def run_pipeline(runs, cfg, *, db_path, pedigree, out) -> dict:
    """Full candidate/control/market historical pipeline on an already
    loaded ``runs`` list. ``pedigree`` must already be validated (SPEC SS4.3);
    the caller is responsible for choosing a real or synthetic source."""
    with isolated_pedigree_adapter_from(pedigree):
        features_raw, labels, race_keys, meta = build_consistent_feature_dataset(
            runs, cfg, DATA_TO, stats_source="ability", db_path=db_path)

    if list(FEATURES) != list(SPEC_FEATURES):
        raise QualityError("backtest_ml.FEATURES no longer matches the SPEC-T81 21-feature contract")

    population = build_population(runs, features_raw, labels, race_keys, meta)
    if population.duplicate_horse_day_violations:
        raise QualityError(
            "same-day duplicate horse history detected: "
            f"{population.duplicate_horse_day_violations[:5]}")

    features = population.features
    ranks = np.asarray([int(row["rank"]) for row in population.meta])
    keys = population.race_keys
    meta_pop = population.meta
    sire_gates = {}
    for label, train_from, train_to, _ef, _et in SELECTION_FOLDS:
        sire_gates[label] = strict_sire_gate(runs, pedigree, train_from, train_to)
    sire_gates["final"] = strict_sire_gate(runs, pedigree, FINAL_TRAIN_FROM, FINAL_TRAIN_TO)

    # Selection applies each fold's own sire_pts gate to its own train+eval
    # rows (SPEC SS5); select_l2 handles that internally per fold.
    candidate_selection = select_l2(
        features, ranks, meta_pop, keys, CANDIDATE_OBJECTIVE, sire_gates)
    control_selection = select_l2(
        features, ranks, meta_pop, keys, CONTROL_OBJECTIVE, sire_gates)

    final_gate = sire_gates["final"]
    final_features = apply_sire_gate(features, final_gate["enabled"])
    final_train_mask = _period_mask(keys, FINAL_TRAIN_FROM, FINAL_TRAIN_TO)

    candidate_fit = fit_pl_model(
        final_features[final_train_mask], ranks[final_train_mask],
        _select(keys, final_train_mask),
        objective=CANDIDATE_OBJECTIVE, l2=candidate_selection["selected_l2"])
    control_fit = fit_pl_model(
        final_features[final_train_mask], ranks[final_train_mask],
        _select(keys, final_train_mask),
        objective=CONTROL_OBJECTIVE, l2=control_selection["selected_l2"])

    comparisons = {}
    quality_ok = True
    for period, (eval_from, eval_to) in HISTORICAL_PERIODS.items():
        eval_mask = _period_mask(keys, eval_from, eval_to)
        eval_features = final_features[eval_mask]
        eval_meta = _select(meta_pop, eval_mask)
        eval_keys = _select(keys, eval_mask)
        if not eval_keys:
            quality_ok = False
            continue
        candidate_scores = score_with_fit(candidate_fit, eval_features)
        control_scores = score_with_fit(control_fit, eval_features)
        market_scores = market_score(eval_meta)
        candidate_metrics = race_metrics_for_population(candidate_scores, eval_keys, eval_meta)
        control_metrics = race_metrics_for_population(control_scores, eval_keys, eval_meta)
        market_metrics = race_metrics_for_population(market_scores, eval_keys, eval_meta)
        comparisons[period] = compare_period(
            candidate_metrics, control_metrics, market_metrics, period)

    verdict = adjudicate(comparisons, quality_ok)
    return {
        "experiment_id": EXPERIMENT_ID,
        "candidate_selection": candidate_selection,
        "control_selection": control_selection,
        "sire_gates": sire_gates,
        "final_train": (FINAL_TRAIN_FROM, FINAL_TRAIN_TO),
        "comparisons": {
            period: {
                "n_races": cmp.n_races,
                "spearman_vs_market": cmp.spearman_vs_market,
                "spearman_vs_top1": cmp.spearman_vs_top1,
                "top3_recall_vs_market": cmp.top3_recall_vs_market,
                "top3_recall_vs_top1": cmp.top3_recall_vs_top1,
                "top3_mae_vs_market": cmp.top3_mae_vs_market,
                "top3_mae_vs_top1": cmp.top3_mae_vs_top1,
                "winner_reference": cmp.winner_reference,
            }
            for period, cmp in comparisons.items()
        },
        "machine_judgment": verdict,
        "reviewer_adjudication": None,
    }


@contextmanager
def isolated_pedigree_adapter_from(pedigree: dict):
    """Like ``isolated_pedigree_adapter`` but for an already-validated dict
    (used so --smoke can inject a synthetic pedigree without touching disk)."""
    import pedigree_store

    def _frozen_loader(use_cache: bool = True, refresh: bool = False):
        if refresh:
            raise QualityError("T81 harness forbids pedigree refresh")
        return pedigree

    original = pedigree_store.load_all
    pedigree_store.load_all = _frozen_loader
    try:
        yield pedigree
    finally:
        pedigree_store.load_all = original


# ---------------------------------------------------------------------------
# --audit-only
# ---------------------------------------------------------------------------

def dependency_side_effect_audit() -> dict:
    return {
        "pedigree_store.load_all": (
            "patched for the duration of dataset construction to a validated, "
            "in-memory local-only closure (isolated_pedigree_adapter); the "
            "production Neon fallback (past_data_service.get_db_connection, "
            "psycopg2/DATABASE_URL) is never invoked in this harness"),
        "fold_stats.FoldFactorTableProvider": (
            "opens ability.db via fold_stats._read_only_connection "
            "(mode=ro URI); no PRAGMA query_only, but mode=ro already makes "
            "writes fail at the SQLite VFS layer -- observation only, no "
            "change made to shared code"),
        "backtest_ability.load_runs": (
            "takes the caller's connection; this harness always passes its "
            "own mode=ro + PRAGMA query_only=ON connection"),
        "scoring/analysis/backtest_criteria factor and criteria loaders": (
            "local CSV/JSON reads only; no requests/urllib/socket usage found "
            "by static grep"),
        "no_write_paths_used": (
            "pedigree_store.upsert_horses and any Neon writer are not "
            "reachable from this harness's call graph"),
    }


def as_of_limitations() -> list:
    return [
        {"asset": "venue_standard_times.json", "limitation": (
            "meta.period is 2016-2023; the 2022/2023 selection folds' base "
            "times therefore include information from after their own "
            "training-period end (SPEC SS5). Known, pre-declared limitation; "
            "not a leak this harness introduces or can correct without a "
            "separate SPEC.")},
        {"asset": "track_variant (track_variants.json)", "limitation": (
            "reads a past run date's track condition but the correction is "
            "computed from the same fixed-period base times above.")},
        {"asset": "criteria.csv (grade_pts)", "limitation": (
            "current rule conditions are applied to every historical year; "
            "disabling automatic rule weights (analysis._criteria_weights_cache) "
            "removes the outcome-derived leak but not the rule-authorship-date "
            "as-of gap.")},
        {"asset": "sire_pts pedigree coverage gate", "limitation": (
            "backtest_ml.build_dataset computes its own per-calendar-year "
            "coverage gate from all rows visible to that call, which can "
            "include rows after the fold's own training period. This harness "
            "computes an independent, strict, fold-scoped gate (SPEC SS5) and "
            "combines it with the shared function's gate conservatively via "
            "AND (apply_sire_gate never re-enables a value the shared "
            "function already zeroed). Flagged to review in SS5 of the final "
            "report as an interpretation decision.")},
        {"asset": "pedigree_cache.json", "limitation": (
            "static local snapshot; its own as-of freshness relative to any "
            "given evaluation date is not tracked by this harness.")},
    ]


def run_audit_only(args) -> dict:
    db_path = args.db
    db_sha_start = sha256_file(db_path)
    with closing(open_readonly_connection(db_path)) as conn:
        assert_connection_is_readonly(conn)
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    db_sha_end = sha256_file(db_path)
    if db_sha_start != db_sha_end:
        raise QualityError(
            "ability.db changed during the audit run (concurrent external "
            "update); no consistent read-only snapshot could be guaranteed")

    cfg = load_win5_cfg()
    with isolated_pedigree_adapter(PEDIGREE_CACHE_PATH) as pedigree:
        features_raw, labels, race_keys, meta = build_consistent_feature_dataset(
            runs, cfg, DATA_TO, stats_source="ability", db_path=db_path)

    if list(FEATURES) != list(SPEC_FEATURES):
        raise QualityError(
            "backtest_ml.FEATURES no longer matches the SPEC-T81 21-feature contract")

    population = build_population(runs, features_raw, labels, race_keys, meta)

    sire_gates = {}
    for label, train_from, train_to, _ef, _et in SELECTION_FOLDS:
        sire_gates[label] = strict_sire_gate(runs, pedigree, train_from, train_to)
    sire_gates["final"] = strict_sire_gate(runs, pedigree, FINAL_TRAIN_FROM, FINAL_TRAIN_TO)

    exclusion_primary = Counter(population.primary_reason.values())
    exclusion_all_flags = Counter()
    for flags in population.all_flags.values():
        for flag in flags:
            exclusion_all_flags[flag] += 1

    def rate_table(counts_by_key):
        return {
            str(key): {"eligible": eligible, "total": total,
                      "rate": (eligible / total if total else None)}
            for key, (eligible, total) in sorted(counts_by_key.items(), key=str)
        }

    audit = {
        "experiment_id": EXPERIMENT_ID,
        "mode": "audit-only",
        "spec_path": os.path.relpath(SPEC_PATH, BASE_DIR),
        "population_window": {"from": DATA_FROM, "to": DATA_TO},
        "raw_rows_loaded": len(runs),
        "candidate_races_in_window": population.total_candidate_races,
        "eligible_races": len(population.eligible_keys),
        "eligible_race_rate": (
            len(population.eligible_keys) / population.total_candidate_races
            if population.total_candidate_races else None),
        "exclusion_primary_reason": dict(exclusion_primary),
        "exclusion_all_flags": dict(exclusion_all_flags),
        "eligible_rate_by_year": rate_table(population.counts_by_year),
        "eligible_rate_by_venue": rate_table(population.counts_by_venue),
        "eligible_rate_by_surface": rate_table(population.counts_by_surface),
        "eligible_rate_by_field_band": rate_table(population.counts_by_field_band),
        "same_day_duplicate_horse_history_violations": (
            population.duplicate_horse_day_violations),
        "sire_pts_fold_gates": sire_gates,
        "as_of_limitations": as_of_limitations(),
        "dependency_and_side_effect_audit": dependency_side_effect_audit(),
        "ability_db_sha256": db_sha_start,
        "pedigree_cache_sha256": sha256_file(PEDIGREE_CACHE_PATH),
        "feature_names_in_order": list(FEATURES),
        "notes": (
            "This audit builds the 21-feature dataset to determine population "
            "eligibility and completeness only. No model was fit and no "
            "score/rank correlation against real outcomes was computed "
            "(SPEC-T81 SS3)."),
    }
    return audit


# ---------------------------------------------------------------------------
# --smoke: synthetic-only, full pipeline
# ---------------------------------------------------------------------------

def synthetic_win5_cfg() -> dict:
    return {
        "params": {
            "ability": {
                "pos_factor": {"1": 1.0, "2": 0.7, "3": 0.5, "4": 0.25, "5": 0.25},
                "time_k": 1.0, "class_k": 0.1,
                "agari_best_ratio": 0.3, "agari_bonus": 1.5,
                "track_variant": False,
            },
            "market": {"odds_k": 0.0, "odds_ref": 10.0, "clamp": 10.0},
        },
        "weights": {"jockey_w": 1.0, "frame": 1.0},
    }


def make_synthetic_runs(seed: int = 81, n_races: int = 30) -> list:
    """Deterministic synthetic JRA-shaped race history spanning 2021-2026H1.

    Every race is a complete field (8-12 runners) with unique ranks 1..n,
    settled odds > 1.0 for every runner. Every runner slot gets its own,
    never-reused horse identity plus exactly one earlier "warm-up" start (on
    a date that never collides with any other row for that same horse) so
    build_dataset finds a non-empty prior history without ever letting one
    horse appear twice on the same date. No real DB or network is touched.
    """
    rng = np.random.RandomState(seed)
    venues = list(JRA_VENUES)
    dates = []
    races_per_year = {2021: 5, 2022: 5, 2023: 5, 2024: 5, 2025: 6, 2026: 4}
    for year, count in races_per_year.items():
        for i in range(count):
            month = 1 + (i % 6)
            if year == 2026 and month > 6:
                continue
            day = 3 + (i % 20)
            dates.append(f"{year:04d}{month:02d}{day:02d}")
    dates = sorted(set(dates))[:n_races]

    horse_counter = 0

    def new_horse():
        nonlocal horse_counter
        horse_counter += 1
        return f"SmokeHorse{horse_counter:05d}"

    rows = []
    for race_index, date in enumerate(dates):
        venue = venues[race_index % len(venues)]
        surface = "芝" if race_index % 2 == 0 else "ダート"
        race_no = 1 + (race_index % 12)
        n = 8 + (race_index % 5)
        odds = list(rng.uniform(1.5, 40.0, size=n))
        rng.shuffle(odds)
        warmup_date = f"{int(date[:4]) - 1:04d}0101"
        for umaban in range(1, n + 1):
            rank = umaban  # deterministic permutation, still exactly 1..n
            horse = new_horse()
            win_pay = (f"{odds[umaban - 1] * 100:.0f}" if rank == 1
                      else f"({odds[umaban - 1]:.1f})")
            base = {
                "place": venue, "r": race_no, "race_name": "スモーク特別",
                "race_class": "1勝クラス", "horse": horse, "total_horses": n,
                "popularity": umaban, "track_type": surface, "distance": 1600,
                "condition": "良", "chakusa": 0.1, "c4": umaban,
                "jockey": f"J{umaban % 5}", "umaban": umaban, "pci": 50.0,
                "affi": "美浦" if umaban % 2 else "栗東",
                "sex": "牡" if umaban % 2 else "牝", "age": 4, "kinryo": 55.0,
                "weight": 470 + umaban, "fukusho_pay": None,
            }
            warmup = dict(base)
            warmup.update({
                "date": warmup_date, "rank": 1, "win_pay": "250",
                "time_sec": 96.0, "agari": 35.5,
            })
            current = dict(base)
            current.update({
                "date": date, "rank": rank, "win_pay": win_pay,
                "time_sec": 95.0 + umaban * 0.1, "agari": 35.0 + umaban * 0.1,
            })
            rows.append(warmup)
            rows.append(current)
    rows.sort(key=lambda r: (r["horse"], r["date"]))
    return rows


def synthetic_pedigree(runs) -> dict:
    horses = sorted({row["horse"] for row in runs if row.get("horse")})
    return {horse: {"sire": f"SmokeSire{i % 5}", "bms": f"SmokeBMS{i % 3}"}
            for i, horse in enumerate(horses)}


ABILITY_RUNS_COLUMNS = (
    ("date", "TEXT"), ("place", "TEXT"), ("r", "INTEGER"), ("race_name", "TEXT"),
    ("race_class", "TEXT"), ("horse", "TEXT"), ("sex", "TEXT"), ("age", "INTEGER"),
    ("jockey", "TEXT"), ("kinryo", "REAL"), ("total_horses", "INTEGER"),
    ("umaban", "INTEGER"), ("popularity", "INTEGER"), ("rank", "INTEGER"),
    ("track_type", "TEXT"), ("distance", "INTEGER"), ("condition", "TEXT"),
    ("time_sec", "REAL"), ("chakusa", "REAL"), ("c4", "INTEGER"), ("agari", "REAL"),
    ("pci", "REAL"), ("weight", "INTEGER"), ("affi", "TEXT"), ("win_pay", "TEXT"),
    ("fukusho_pay", "REAL"),
)


def _build_synthetic_ability_db(runs, path) -> None:
    """Create a throwaway sqlite file with the exact ``runs`` schema shape,
    filled only with synthetic rows.  This lets ``--smoke`` exercise
    ``FoldFactorTableProvider`` (which queries ``db_path`` directly for
    course/jockey/frame bias tables, independently of any in-memory ``runs``
    list) without ever opening ability.db or the network."""
    conn = sqlite3.connect(path)
    try:
        columns_sql = ", ".join(f"{name} {kind}" for name, kind in ABILITY_RUNS_COLUMNS)
        conn.execute(f"CREATE TABLE runs ({columns_sql})")
        names = [name for name, _kind in ABILITY_RUNS_COLUMNS]
        placeholders = ", ".join(["?"] * len(names))
        conn.executemany(
            f"INSERT INTO runs ({', '.join(names)}) VALUES ({placeholders})",
            [tuple(row.get(name) for name in names) for row in runs])
        conn.commit()
    finally:
        conn.close()


def run_smoke(args) -> dict:
    runs = make_synthetic_runs(seed=81)
    cfg = synthetic_win5_cfg()
    pedigree = synthetic_pedigree(runs)
    with tempfile.TemporaryDirectory(prefix="t81_smoke_") as tmp_dir:
        synthetic_db_path = os.path.join(tmp_dir, "synthetic_ability.db")
        _build_synthetic_ability_db(runs, synthetic_db_path)
        result = run_pipeline(runs, cfg, db_path=synthetic_db_path,
                              pedigree=pedigree, out=None)
    result["mode"] = "smoke"
    result["synthetic_seed"] = 81
    result["notes"] = (
        "Synthetic-only dry run of the full candidate/control/market "
        "pipeline. A throwaway temp-directory sqlite file (deleted before "
        "this call returns) stands in for ability.db so "
        "FoldFactorTableProvider has something to query; ability.db and the "
        "network are never touched in this mode (SPEC-T81 SS3/SS11).")
    return result


# ---------------------------------------------------------------------------
# --run-historical (Stage B interface; gated on ledger registration)
# ---------------------------------------------------------------------------

def load_ledger_record(ledger_path: str, experiment_id: str) -> dict | None:
    if not os.path.isfile(ledger_path):
        return None
    with open(ledger_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("experiment_id") == experiment_id:
                return record
    return None


def run_historical(args) -> dict:
    manifest_path = args.manifest
    if not os.path.isfile(manifest_path):
        raise RankDisplayError(
            f"manifest not found: {manifest_path}. Stage B requires the "
            "reviewer-approved manifest produced after registration.")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    record = load_ledger_record(LEDGER_PATH, args.registration_id)
    if record is None:
        raise RankDisplayError(
            f"no ledger registration found for {args.registration_id!r} in "
            f"{LEDGER_PATH}. Stage A does not register; run this only after "
            "the reviewer has approved and appended the registration.")

    manifest_sha = manifest.get("implementation_sha256", {}).get("backtest_rank_display.py")
    actual_sha = sha256_file(os.path.abspath(__file__))
    if manifest_sha != actual_sha:
        raise RankDisplayError(
            "backtest_rank_display.py sha256 no longer matches the "
            "registered manifest; refusing to run an un-registered code path")

    db_sha_expected = manifest.get("ability_db_sha256")
    db_sha_actual = sha256_file(args.db)
    if db_sha_expected != db_sha_actual:
        raise RankDisplayError(
            "ability.db sha256 no longer matches the registered manifest; "
            "refusing to run against a different snapshot without a new "
            "registration")

    # SPEC SS11: verify "design match" between the ledger row and the
    # manifest, not just that a row with this experiment_id exists.
    ledger_hashes = record.get("data_hashes") or {}
    if ledger_hashes.get("ability_db") != f"sha256:{db_sha_expected}":
        raise RankDisplayError(
            "ledger data_hashes.ability_db does not match the manifest; "
            "registration and manifest have diverged")
    if ledger_hashes.get("harness_source") != f"sha256:{manifest_sha}":
        raise RankDisplayError(
            "ledger data_hashes.harness_source does not match the manifest; "
            "registration and manifest have diverged")
    if record.get("primary_metric") != "race_mean_spearman_delta_pl3_minus_market_2025":
        raise RankDisplayError(
            "ledger primary_metric does not match the SPEC-T81 contract; "
            "refusing to run against a differently-designed registration")
    if record.get("candidate_count") != 6:
        raise RankDisplayError(
            "ledger candidate_count does not match the SPEC-T81 contract "
            "(3 pl_top3 x L2 + 3 pl_top1 x L2 = 6)")

    # Reaching this point means Stage A -> registration -> Stage B are all
    # consistent; only then do we touch real data for scoring.
    db_sha_start = db_sha_actual
    with closing(open_readonly_connection(args.db)) as conn:
        assert_connection_is_readonly(conn)
        runs = load_runs(conn, DATA_FROM, DATA_TO)
    db_sha_end = sha256_file(args.db)
    if db_sha_start != db_sha_end:
        raise QualityError("ability.db changed during the historical run")

    cfg = load_win5_cfg()
    with isolated_pedigree_adapter(PEDIGREE_CACHE_PATH) as pedigree:
        result = run_pipeline(runs, cfg, db_path=args.db, pedigree=pedigree, out=None)
    result["mode"] = "run-historical"
    result["registration_id"] = args.registration_id
    result["ability_db_sha256"] = db_sha_start
    return result


# ---------------------------------------------------------------------------
# manifest.json / registration.draft.json
# ---------------------------------------------------------------------------

def criteria_csv_bundle_sha256() -> str | None:
    """sha256 of every venue's criteria.csv, concatenated in JRA_VENUES order.

    This is the same fixed venue order ``backtest_criteria.load_filtered_criteria``
    already iterates in; used here only to fingerprint the bundle, never to
    change how criteria are loaded or applied.
    """
    from fold_stats import VENUE_SLUG_MAP

    digest = hashlib.sha256()
    for venue in JRA_VENUES:
        slug = VENUE_SLUG_MAP.get(venue)
        if not slug:
            return None
        path = os.path.join(API_DIR, "data_files", slug, "criteria", "criteria.csv")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()


def build_manifest(args, audit: dict) -> dict:
    git_info = git_identity()
    resource_paths = {
        "ability_db": args.db,
        "pedigree_cache": PEDIGREE_CACHE_PATH,
        "win5_weights": os.path.join(API_DIR, "data_files", "common", "win5_weights.json"),
        "win5_ml_model": os.path.join(API_DIR, "data_files", "common", "win5_ml_model.json"),
        "score_weights": os.path.join(API_DIR, "data_files", "common", "score_weights.json"),
        "mined_rules": os.path.join(API_DIR, "data_files", "common", "mined_rules.csv"),
        "venue_standard_times": os.path.join(
            API_DIR, "data_files", "common", "venue_standard_times.json"),
        "track_variants": os.path.join(
            API_DIR, "data_files", "common", "track_variants.json"),
        "spec": SPEC_PATH,
        "harness_source": os.path.abspath(__file__),
    }
    resource_sha256 = {name: sha256_file(path) for name, path in resource_paths.items()}
    resource_sha256["criteria_csv_bundle"] = criteria_csv_bundle_sha256()

    return {
        "experiment_id": EXPERIMENT_ID,
        "stage": "A",
        "git": git_info,
        "implementation_sha256": {
            "backtest_rank_display.py": resource_sha256["harness_source"],
            "SPEC-T81-rank-display-limited-validation.md": resource_sha256["spec"],
        },
        "protected_resource_sha256_at_stage_a_start": {
            "win5_ml_model.json": resource_sha256["win5_ml_model"],
            "score_weights.json": resource_sha256["score_weights"],
            "mined_rules.csv": resource_sha256["mined_rules"],
        },
        "ability_db_sha256": resource_sha256["ability_db"],
        "pedigree_cache_sha256": resource_sha256["pedigree_cache"],
        "venue_standard_times_sha256": resource_sha256["venue_standard_times"],
        "track_variants_sha256": resource_sha256["track_variants"],
        "criteria_csv_bundle_sha256": resource_sha256["criteria_csv_bundle"],
        "python_and_library_versions": python_and_library_versions(),
        "population_window": {"from": DATA_FROM, "to": DATA_TO},
        "feature_names_in_order": list(SPEC_FEATURES),
        "frame_definition": "past_data_service.calculate_waku (post-T80b fix)",
        "missing_value_handling": (
            "backtest_ml.build_dataset defaults (kinryo->55.0, weight->470, "
            "age->4, etc.); a runner with zero prior starts is dropped "
            "entirely, which then excludes its whole race via the SPEC SS6 "
            "complete-field contract"),
        "population_contract": (
            "SPEC-T81 SS6: JRA turf/dirt flat, race# 1-12, 8+ declared "
            "runners, raw/feature/odds alignment, strict rank set {1..n}"),
        "l2_grid": list(L2_GRID),
        "selection_folds": [
            {"label": label, "train": [train_from, train_to], "eval": [eval_from, eval_to]}
            for label, train_from, train_to, eval_from, eval_to in SELECTION_FOLDS
        ],
        "final_train_period": [FINAL_TRAIN_FROM, FINAL_TRAIN_TO],
        "historical_periods": HISTORICAL_PERIODS,
        "bootstrap": {"n_resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED,
                     "block_unit": "JRA event date (all venues same day = 1 block)"},
        "gates": {
            "screen_conditions": (
                "SPEC-T81 SS9 fixed AND conditions across 2025 and 2026H1, "
                "against both market and re-trained pl_top1 control"),
            "sire_pts_gate_threshold": PEDIGREE_MIN_COVERAGE,
            "sire_pts_gate_combination": (
                "AND of this harness's strict fold-scoped gate (SPEC SS5) "
                "with backtest_ml.build_dataset's own per-calendar-year gate. "
                "Common code was not overridden or modified; the AND only "
                "ever suppresses sire_pts further, never re-enables it. "
                "Reviewed and approved 2026-09-24 (Fable 5.1). With the "
                "current audit snapshot both gates are enabled in every fold "
                "(coverage 99.996-100% >= threshold), so this combination "
                "has no effect on today's data; it only matters if pedigree "
                "coverage later drops below the threshold in either gate."),
        },
        "as_of_limitations": as_of_limitations(),
        "audit_summary": {
            "eligible_races": audit.get("eligible_races"),
            "eligible_race_rate": audit.get("eligible_race_rate"),
            "same_day_duplicate_horse_history_violations": audit.get(
                "same_day_duplicate_horse_history_violations"),
        },
    }


def build_registration_draft(manifest: dict, manifest_file_sha256: str) -> dict:
    stop_rule = (
        "SPEC-T81 SS3/SS6/SS7/SS9 fixed procedure; no additional search, "
        "stop and report on any quality error, no production/network access. "
        "3 rolling-origin folds (train 2021 -> eval 2022; train 2021-2022 -> "
        "eval 2023; train 2021-2023 -> eval 2024) select L2 in {0.3, 1.0, "
        "3.0} per objective (pl_top1, pl_top3) by race-count-weighted "
        "race_mean_spearman, maximized; a candidate within 1e-12 of the max "
        "keeps the larger L2. Final fit uses 2021-2024 for both objectives "
        "at their selected L2, frozen, then evaluated in order on 2025 and "
        "2026H1 without refitting. Tie conventions: predicted display rank "
        "is score descending -> umaban ascending; market display rank is "
        "odds ascending -> umaban ascending; Spearman uses average-rank "
        "ties, and an all-tied-score race scores 0 (counted, not dropped). "
        "Paired uncertainty: eval.blocks.paired_block_bootstrap over JRA "
        "event-date blocks (all venues on one date share one block), 10,000 "
        "resamples, seed 81. Machine judgment per SPEC SS9's 4 fixed "
        "outcomes (HISTORICAL_SCREEN_PASS/FAIL/INCONCLUSIVE/INVALID).")

    notes = (
        "T81 Stage A registration draft (実装担当作成、レビュー承認済みの解釈を含む)。\n"
        "\n"
        "candidate_count=6 の内訳: 学習構成 pl_top3(PL@3) x L2{0.3,1.0,3.0} の3 + "
        "学習構成 pl_top1(再学習top-1対照) x L2{0.3,1.0,3.0} の3 = 6。市場(確定単勝"
        "オッズ順位)は非学習の対照で、調整パラメータを持たないため候補数に含めない。"
        "data_hashes.production_model_sha256 (win5_ml_model.json) は本番モデルの"
        "同一性確認用であり、比較対照や再学習の起点には使わない(SPEC SS7、本番T45は"
        "2025年まで学習済みのため2025年の未見対照として使わない)。\n"
        "\n"
        "fold定義 (L2選抜、rolling-origin): fold1 学習2021 -> 評価2022 / fold2 "
        "学習2021-2022 -> 評価2023 / fold3 学習2021-2023 -> 評価2024。L2選択規則: "
        "各L2につき3foldの適格レース数で重み付けした race_mean_spearman の加重平均を、"
        "pl_top3とpl_top1それぞれ独立に最大化するL2を選ぶ。最大値との差が1e-12以内な"
        "ら大きいLを選ぶ(tie-break)。候補選択を勝馬LogLossや上位3頭指標に切り替えない。\n"
        "\n"
        "最終fit: 選択したL2で2021-2024の適格レース全体を用いてpl_top3・pl_top1を"
        "それぞれ最終fit。この係数・標準化器を固定し、2025年全体→2026年上期の順に"
        "評価する。2025年を見て係数・L2・入力・母集団を変更したり、2026年上期の前に"
        "再学習することはしない。\n"
        "\n"
        "tie規約: 表示用の予測順位はスコア降順→馬番昇順で一意に決定する。市場の順位は"
        "確定単勝オッズ昇順→馬番昇順(popularity列は使わない)。Spearman相関のスコア"
        "同値は平均順位(average rank)を用いる。全馬が同値スコアの場合はSpearmanを0と"
        "し、そのレースを除外せず件数を別途報告する。実着順は同着・取消等が既に主母集"
        "団の適格性チェック(SPEC SS6)で除外されているため、常に{1,...,n}の厳密な順列。\n"
        "\n"
        "不確実性: eval.blocks.paired_block_bootstrap を使用。JRA開催日単位(同日の"
        "全場を1 block)で復元抽出、10,000回、seed=81。candidate minus comparatorの"
        "差・95%CI・p値を、2025・2026年上期それぞれについて対市場・対再学習top-1で"
        "算出する。日内のレース対応は事前assert済み(compare_periodがレース集合不一致"
        "でQualityErrorを出す)。\n"
        "\n"
        "SPEC SS9の固定ゲート条件 (全条件AND): "
        "(a) Spearman差(対市場・対top-1) 点推定>0、2025年はさらに95%CI下限>0 "
        "(2026年上期はCIを報告のみでゲートに使わない)。"
        "(b) top3_set_recall差(対市場・対top-1、両期間) 点推定>=0。"
        "(c) actual_top3_rank_mae差(対市場・対top-1、両期間) 点推定<=0。"
        "(b)(c)の点推定条件は許容誤差なしの厳密比較(厳密な0跨ぎ回避のための1e-12等は"
        "設けない)。実行結果のresultには(a)(b)(c)すべての生の観測差分値(observed_"
        "difference)を必ず出力し、判定の真偽だけでなく数値そのものを報告する。\n"
        "\n"
        "機械判定4種: INVALID(データ・時点整合・封印・収束・母集団・再現性の不備。品質"
        "エラーが最優先で他条件を上書き) / HISTORICAL_SCREEN_FAIL(必要な点推定の方向"
        "が不成立) / INCONCLUSIVE(点推定の方向は成立するが2025年の必要CIが0を跨ぐ"
        "または含む。2026年上期のCIはINCONCLUSIVE判定に使わない) / HISTORICAL_"
        "SCREEN_PASS(品質・再現性に問題なく全条件成立。将来検証の起票候補にとどまり、"
        "採用ではない)。\n"
        "\n"
        "as_of制約の要約 (詳細はmanifest.as_of_limitationsとStage Aレポート参照): "
        "venue_standard_times.jsonの集計期間は2016-2023に固定されており、2022/2023"
        "foldの基準タイムに学習期間終了後の情報が含まれ得る既知の制約。track_variant"
        "(馬場差)補正も同じ固定基準タイムに依存する。grade_pts(criteria.csv)は現行"
        "ルール条件を全期間に適用し、自動重み(analysis._criteria_weights_cache)は"
        "無効化済みだがルール制定時点のas-ofまでは保証しない。sire_ptsの有効/無効"
        "gateは本ハーネス独自の厳格・fold限定coverage判定と、backtest_ml.build_"
        "dataset自身の(暦年ベースの)判定とのAND(共通コード変更なし、抑制のみ・情報"
        "追加なし。現データでは両方が閾値70%以上で有効)。これらは既知の限定であり、"
        "未知の新たな漏洩経路の発見や、対象馬・将来行の分母混入による契約外のINVALID"
        "とは区別する。\n"
        "\n"
        "本実験は確定単勝オッズを用いた歴史反証フィルタであり、発走前に購入可能だった"
        "条件の評価ではない。完全にリークフリーな将来検証、または未見のprospective "
        "OOS検証とは呼ばない。歴史足切りを通過しても、それは将来検証(発走前シャドー"
        "評価、別SPEC・別登録)の起票候補であることのみを意味し、本番採用・表示変更・"
        "通知や購入への接続を意味しない。")

    return {
        "experiment_id": EXPERIMENT_ID,
        "registered_at_utc": None,
        "commit_sha": manifest["git"]["head"],
        "data_hashes": {
            "ability_db": f"sha256:{manifest['ability_db_sha256']}",
            "pedigree_cache": f"sha256:{manifest['pedigree_cache_sha256']}",
            "harness_source": f"sha256:{manifest['implementation_sha256']['backtest_rank_display.py']}",
            "spec": f"sha256:{manifest['implementation_sha256']['SPEC-T81-rank-display-limited-validation.md']}",
            "manifest_sha256": f"sha256:{manifest_file_sha256}",
            "venue_standard_times_json": f"sha256:{manifest['venue_standard_times_sha256']}",
            "track_variants_json": f"sha256:{manifest['track_variants_sha256']}",
            "score_weights_json": (
                f"sha256:{manifest['protected_resource_sha256_at_stage_a_start']['score_weights.json']}"),
            "criteria_csv_bundle": f"sha256:{manifest['criteria_csv_bundle_sha256']}",
            "production_model_sha256": (
                f"sha256:{manifest['protected_resource_sha256_at_stage_a_start']['win5_ml_model.json']}"),
        },
        "features": [
            f"{name} (SPEC-T81 SS5, fixed 21-feature order; see manifest.feature_names_in_order)"
            for name in manifest["feature_names_in_order"]
        ],
        "primary_metric": "race_mean_spearman_delta_pl3_minus_market_2025",
        "safety_metrics": [
            "race_mean_spearman_delta_pl3_minus_pl_top1_2025",
            "race_mean_spearman_delta_pl3_minus_market_2026H1",
            "race_mean_spearman_delta_pl3_minus_pl_top1_2026H1",
            "top3_set_recall_delta_vs_market_and_top1_both_periods",
            "actual_top3_rank_mae_delta_vs_market_and_top1_both_periods",
            "winner_in_top1/2/3_and_winner_logloss_reference_only",
        ],
        "search_grid": {"objective": ["pl_top1", "pl_top3"], "l2": list(L2_GRID)},
        "candidate_count": 6,
        "stop_rule": stop_rule,
        "benchmark_type": "historical",
        "prospective_start_date": None,
        "result_summary": None,
        "adjudication": None,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_mutually_exclusive_group(required=False)
    modes.add_argument("--audit-only", action="store_true",
                       help="quality/schema/hash audit only; no scoring")
    modes.add_argument("--smoke", action="store_true",
                       help="full pipeline on synthetic data only; no DB/network")
    modes.add_argument("--run-historical", action="store_true",
                       help="Stage B only; requires an approved ledger registration")
    parser.add_argument("--db", default=DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--manifest", default=None,
                        help="approved manifest.json (required for --run-historical)")
    parser.add_argument("--registration-id", default=None,
                        help="T39 ledger experiment_id (required for --run-historical)")
    return parser


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False,
                  default=_json_default)
        handle.write("\n")


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (args.audit_only or args.smoke or args.run_historical):
        parser.print_help()
        return 0

    output_dir = args.output_dir or os.path.join(
        BASE_DIR, "outputs", "t81_rank_display",
        "stage_a" if not args.run_historical else "run_001")

    try:
        if args.audit_only:
            audit = run_audit_only(args)
            _write_json(os.path.join(output_dir, "audit.json"), audit)
            manifest = build_manifest(args, audit)
            manifest_path = os.path.join(output_dir, "manifest.json")
            _write_json(manifest_path, manifest)
            # Hash the manifest file exactly as written to disk, so
            # registration.draft.json's manifest_sha256 always matches the
            # sibling manifest.json byte-for-byte (SPEC-T81 review request).
            manifest_file_sha256 = sha256_file(manifest_path)
            registration_draft = build_registration_draft(manifest, manifest_file_sha256)
            _write_json(os.path.join(output_dir, "registration.draft.json"),
                       registration_draft)
            print(json.dumps({"status": "OK", "mode": "audit-only",
                              "output_dir": output_dir,
                              "eligible_races": audit["eligible_races"]},
                             ensure_ascii=False))
            return 0

        if args.smoke:
            result = run_smoke(args)
            _write_json(os.path.join(output_dir, "smoke_result.json"), result)
            print(json.dumps({"status": "OK", "mode": "smoke",
                              "output_dir": output_dir,
                              "machine_judgment": result["machine_judgment"]},
                             ensure_ascii=False))
            return 0

        if args.run_historical:
            if not args.manifest or not args.registration_id:
                raise RankDisplayError(
                    "--run-historical requires --manifest and --registration-id")
            result = run_historical(args)
            _write_json(os.path.join(output_dir, "result.json"), result)
            print(json.dumps({"status": "OK", "mode": "run-historical",
                              "output_dir": output_dir,
                              "machine_judgment": result["machine_judgment"]},
                             ensure_ascii=False))
            return 0
    except RankDisplayError as exc:
        print(json.dumps({"status": "STOP", "reason": str(exc),
                          "error_type": type(exc).__name__}, ensure_ascii=False),
             file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
