import numpy as np
import pytest

from backtest_ml import FEATURES
from backtest_t50 import (
    A5_FEATURES,
    active_a5_columns,
    build_a5_matrix,
    build_asof_measurement_states,
)


def measurement(day, venue, turf, dirt, cushion=9.0):
    return {"date": day, "venue": venue, "turf_moisture_goal": turf,
            "dirt_moisture_goal": dirt, "cushion": cushion}


def test_asof_standardization_excludes_same_date_and_future_values():
    rows = [
        measurement("20210101", "東京", 10, 4),
        measurement("20210101", "中山", 20, 8),
        measurement("20210102", "東京", 12, 5),
        measurement("20210102", "中山", 22, 9),
        measurement("20210103", "東京", 14, 6),
    ]
    states = build_asof_measurement_states(rows, min_history=2)

    assert states[("20210101", "東京", "芝")]["history_source"] == "insufficient_history"
    # Day 2 can use the two day-1 global values, but not 中山's same-day 22.
    assert states[("20210102", "東京", "芝")]["history_source"] == "surface_global"
    assert states[("20210102", "東京", "芝")]["moisture_z"] == pytest.approx(-0.6)
    # Day 3 has two exact prior Tokyo observations: mean=11, population std=1.
    final = states[("20210103", "東京", "芝")]
    assert final["exact_history_available"] is True
    assert final["history_n"] == 2
    assert final["moisture_z"] == pytest.approx(3.0)


def test_asof_states_are_invariant_to_input_order_within_date():
    rows = [measurement("20210101", "東京", 10, 4),
            measurement("20210101", "中山", 20, 8),
            measurement("20210102", "東京", 12, 5)]
    forward = build_asof_measurement_states(rows, min_history=2)
    reverse = build_asof_measurement_states(list(reversed(rows)), min_history=2)

    assert forward == reverse


def test_a5_matrix_uses_surface_measurement_and_does_not_mutate_base():
    base = np.zeros((2, len(FEATURES)), dtype=np.float32)
    base[:, FEATURES.index("wet_match")] = [1.0, -1.0]
    base[:, FEATURES.index("tfeat")] = [2.0, 3.0]
    original = base.copy()
    meta = [
        {"date": "20240101", "place": "東京", "r": 1, "horse": "A",
         "umaban": 1, "track_type": "芝"},
        {"date": "20240101", "place": "東京", "r": 2, "horse": "B",
         "umaban": 2, "track_type": "ダート"},
    ]
    keys = [("20240101", "東京", 1), ("20240101", "東京", 2)]
    states = {
        ("20240101", "東京", "芝"): {
            "moisture_z": 1.5, "moisture_present": True,
            "exact_history_available": True, "history_source": "venue_surface",
            "history_n": 30, "cushion": 9.5},
        ("20240101", "東京", "ダート"): {
            "moisture_z": -2.0, "moisture_present": True,
            "exact_history_available": True, "history_source": "venue_surface",
            "history_n": 30, "cushion": None},
    }
    profiles = {
        ("20240101", "東京", 1, "A", 1): {
            "pace_type": 2.0, "wet_available": True, "speed_available": True},
        ("20240101", "東京", 2, "B", 2): {
            "pace_type": -1.0, "wet_available": True, "speed_available": True},
    }

    matrix, diagnostics = build_a5_matrix(base, meta, keys, states, profiles)

    assert matrix.shape == (2, 6) == (2, len(A5_FEATURES))
    assert matrix[0].tolist() == pytest.approx([1.5, 3.0, 19.0, 1, 1, 1])
    assert matrix[1].tolist() == pytest.approx([2.0, 2.0, 0.0, 1, 1, 0])
    assert diagnostics[1]["cushion_speed_available"] is False
    assert np.array_equal(base, original)


def test_missing_or_fallback_history_has_zero_availability_flags():
    base = np.zeros((1, len(FEATURES)), dtype=np.float32)
    meta = [{"date": "20210101", "place": "東京", "r": 1, "horse": "A",
             "umaban": 1, "track_type": "芝"}]
    keys = [("20210101", "東京", 1)]
    states = {("20210101", "東京", "芝"): {
        "moisture_z": 0.5, "moisture_present": True,
        "exact_history_available": False, "history_source": "surface_global",
        "history_n": 3, "cushion": 9.0}}
    profiles = {("20210101", "東京", 1, "A", 1): {
        "pace_type": 1.0, "wet_available": True, "speed_available": False}}

    matrix, _ = build_a5_matrix(base, meta, keys, states, profiles)

    assert matrix[0, 3:].tolist() == [0.0, 0.0, 0.0]
    assert matrix[0, 1] == pytest.approx(0.5)  # fallback value remains usable


def test_only_constant_availability_flags_are_removed():
    a5 = np.asarray([
        [0, 0, 0, 1, 0, 1],
        [1, 2, 3, 1, 0, 1],
        [2, 4, 6, 1, 0, 1],
    ], dtype=np.float32)
    dates = np.asarray(["20210101", "20220101", "20230101"])

    active, removed = active_a5_columns(a5, dates)

    assert [A5_FEATURES[index] for index in active] == list(A5_FEATURES[:3])
    assert removed == list(A5_FEATURES[3:])
