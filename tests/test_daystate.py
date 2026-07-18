import hashlib

import numpy as np
import pytest

from backtest_daystate import (
    X1_FEATURES, _fit_scaler_in_chunks, build_asof_race_signals, build_daystate_matrix,
    fit_conditional_logit_compact, shrinkage_weight, win5_cutoffs)


def _runner(date, race_no, rank, *, horse=None, time_sec=100.0,
            place="東京", surface="芝", distance=1600, condition="良",
            c4=None, umaban=None, total=12, pci=50.0):
    return {
        "date": date, "place": place, "r": race_no, "rank": rank,
        "horse": horse or f"{date}-{race_no}-{rank}",
        "time_sec": time_sec, "c4": c4 or rank, "umaban": umaban or rank,
        "total_horses": total, "track_type": surface, "distance": distance,
        "condition": condition, "pci": pci,
    }


def _race(date, race_no, *, time_sec=100.0, place="東京", surface="芝"):
    return [_runner(date, race_no, rank, time_sec=time_sec + 0.1 * rank,
                    place=place, surface=surface) for rank in (1, 2, 3)]


def test_single_race_policy_excludes_current_future_and_previous_day():
    runs = (_race("20240101", 12) + _race("20240102", 1)
            + _race("20240102", 2) + _race("20240102", 3))
    meta = [runs[3], runs[6], runs[9]]  # current date races 1,2,3 winners
    keys = [(row["date"], row["place"], row["r"]) for row in meta]
    base = np.zeros((3, 21))
    _matrix, diagnostics = build_daystate_matrix(
        runs, base, meta, keys, 2, single_race_margin=1)
    assert [row["n"] for row in diagnostics] == [0, 1, 2]
    assert diagnostics[0]["n"] == 0  # previous day is never carried over

    _strict, strict_diagnostics = build_daystate_matrix(
        runs, base, meta, keys, 2, single_race_margin=2)
    assert [row["n"] for row in strict_diagnostics] == [0, 0, 1]


def test_asof_baseline_excludes_same_day_and_future_observations():
    history = []
    for day in range(1, 21):
        history += _race(f"202312{day:02d}", 1, time_sec=98.0 + day % 5)
    current = _race("20240106", 1, time_sec=105.0)
    same_day_future = _race("20240106", 2, time_sec=999.0)
    later_date = _race("20240107", 1, time_sec=777.0)
    base_signals = build_asof_race_signals(history + current)
    extended = build_asof_race_signals(history + current + same_day_future + later_date)
    key = ("20240106", "東京", 1)
    assert extended[key]["time"] == pytest.approx(base_signals[key]["time"])
    assert extended[key]["pace"] == pytest.approx(base_signals[key]["pace"])
    assert extended[key]["draw"] == pytest.approx(base_signals[key]["draw"])


def test_win5_common_cutoff_is_separate_and_has_strict_sensitivity():
    keys = [("20240106", "東京", 12), ("20240106", "中山", 12),
            ("20240106", "東京", 11), ("20240106", "中山", 11),
            ("20240106", "東京", 10), ("20240106", "中山", 10)]
    cutoffs, targets = win5_cutoffs(keys, margin=2)
    strict, strict_targets = win5_cutoffs(keys, margin=3)
    assert cutoffs["20240106"] == 8
    assert strict["20240106"] == 7
    assert len(targets) == len(strict_targets) == 5


def test_shrinkage_zero_and_monotonic_approach_to_one():
    assert shrinkage_weight(0, 5) == 0.0
    weights = [shrinkage_weight(n, 5) for n in (1, 2, 5, 10)]
    assert weights == sorted(weights)
    assert all(0 < value < 1 for value in weights)


def test_daystate_matrix_is_deterministic_and_six_columns():
    runs = _race("20240101", 1) + _race("20240101", 2)
    meta = [runs[3]]
    keys = [("20240101", "東京", 2)]
    base = np.zeros((1, 21))
    first, first_diagnostics = build_daystate_matrix(runs, base, meta, keys, 5)
    second, second_diagnostics = build_daystate_matrix(runs, base, meta, keys, 5)
    assert first.shape == (1, len(X1_FEATURES)) == (1, 6)
    assert np.array_equal(first, second)
    assert first_diagnostics == second_diagnostics
    assert hashlib.sha256(first.tobytes()).hexdigest() == hashlib.sha256(
        second.tobytes()).hexdigest()


def test_chunked_scaler_matches_one_shot_statistics():
    from sklearn.preprocessing import StandardScaler
    values = np.arange(120, dtype=float).reshape(40, 3)
    expected = StandardScaler().fit(values)
    actual = _fit_scaler_in_chunks(values.copy(), chunk_size=7)
    assert actual.mean_ == pytest.approx(expected.mean_)
    assert actual.var_ == pytest.approx(expected.var_)


def test_compact_conditional_logit_matches_production_on_small_fixture():
    from backtest_ml import fit_conditional_logit
    X = np.asarray([[0.2, -0.3], [1.1, 0.4], [-0.5, 0.7],
                    [0.6, 0.1], [-0.2, -0.8], [0.9, 0.5]], dtype=np.float32)
    y = np.asarray([0, 1, 0, 0, 0, 1])
    keys = [("d1", "p", 1)] * 3 + [("d2", "p", 2)] * 3
    expected = fit_conditional_logit(X.astype(float), y, keys, l2=0.7)
    actual = fit_conditional_logit_compact(X, y, keys, l2=0.7)
    # The compact fitter evaluates scores in float32, so L-BFGS may stop at a
    # slightly different point in weight space (gradient noise floor ~1e-4).
    # The invariant is a shared optimum: near-equal float64 objective values.
    assert actual == pytest.approx(expected, rel=5e-4, abs=5e-4)

    def objective(weights):
        scores = X.astype(float) @ weights
        starts = np.asarray([0, 3])
        maxima = np.maximum.reduceat(scores, starts)
        exponentials = np.exp(scores - maxima[np.repeat([0, 1], 3)])
        logsumexp = maxima + np.log(np.add.reduceat(exponentials, starts))
        return (-(scores[y == 1].sum() - logsumexp.sum())
                + 0.5 * 0.7 * float(weights @ weights))

    assert objective(actual) == pytest.approx(objective(expected), rel=1e-7)
