import math

import pytest

import backtest_t61a as t61a


def _race(race_id, probabilities, target):
    return {"race_id": race_id, "probabilities": probabilities, "target": target}


def test_naive_harville_place_matches_closed_form():
    win = (0.4, 0.3, 0.2, 0.1)
    actual = t61a.derive_place_probabilities(win)
    expected_first = 0.0
    for first in range(4):
        for second in range(4):
            if first == second:
                continue
            p12 = win[first] * win[second] / (1.0 - win[first])
            for third in range(4):
                if third in (first, second):
                    continue
                p123 = p12 * win[third] / (1.0 - win[first] - win[second])
                if 0 in (first, second, third):
                    expected_first += p123
    assert actual[0] == pytest.approx(expected_first)
    assert sum(actual) == pytest.approx(3.0)


def test_residual_correlation_known_values():
    assert t61a.pearson_correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert t61a.pearson_correlation([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)
    assert t61a.pearson_correlation([1.0], [2.0]) is None


def test_three_models_share_population_and_metrics_are_race_mean():
    probabilities = {
        "t16": (0.8, 0.6, 0.4, 0.2),
        "market": (0.7, 0.6, 0.5, 0.2),
        "cl": (0.75, 0.55, 0.45, 0.25),
    }
    races = [_race("20240101Tokyo01", probabilities, (1, 1, 1, 0))]
    result = t61a.evaluate_races(races)
    expected = sum(-math.log(p) for p in probabilities["t16"][:3])
    expected += -math.log(1.0 - probabilities["t16"][3])
    assert result["t16"]["top3_bernoulli_logloss"] == pytest.approx(expected / 4)
    assert result["t16"]["capture"]["top3"] == 1.0
    assert result["runner_count"] == 4


def test_population_assert_rejects_missing_model():
    probabilities = {"t16": (0.8, 0.2), "market": (0.7, 0.3)}
    with pytest.raises(AssertionError, match="populations differ"):
        t61a.evaluate_races([_race("20240101Tokyo01", probabilities, (1, 0))])
