import numpy as np
import pytest

from backtest_rank_objective import (
    OBJECTIVES, classify_raw_rank_races, filter_rank_dataset,
    pl_objective_and_gradient, stage_weights)


@pytest.mark.parametrize("objective", OBJECTIVES)
def test_pl_analytic_gradient_matches_central_difference(objective):
    X = np.asarray([[0.2, -0.4], [1.1, 0.3], [-0.7, 0.8],
                    [0.5, 0.2], [-0.1, -0.3], [0.9, 0.7]])
    ranks = np.asarray([2, 1, 3, 3, 1, 2])
    keys = [("d1", "p", 1)] * 3 + [("d2", "p", 2)] * 3
    weights = np.asarray([0.13, -0.27])
    _loss, analytic = pl_objective_and_gradient(
        weights, X, ranks, keys, objective=objective, l2=0.3)
    numeric = np.zeros_like(weights)
    epsilon = 1e-6
    for i in range(len(weights)):
        plus, minus = weights.copy(), weights.copy()
        plus[i] += epsilon; minus[i] -= epsilon
        lp, _ = pl_objective_and_gradient(plus, X, ranks, keys,
                                          objective=objective, l2=0.3)
        lm, _ = pl_objective_and_gradient(minus, X, ranks, keys,
                                          objective=objective, l2=0.3)
        numeric[i] = (lp - lm) / (2 * epsilon)
    assert analytic == pytest.approx(numeric, rel=1e-6, abs=1e-7)


@pytest.mark.parametrize("field_size", [3, 8, 18])
@pytest.mark.parametrize("objective", OBJECTIVES)
def test_stage_weights_sum_to_one_for_every_field_size(objective, field_size):
    assert stage_weights(objective, field_size).sum() == pytest.approx(1.0)


def test_ties_and_nonfinishes_are_excluded_before_feature_filtering():
    good = [{"date": "20240101", "place": "A", "r": 1, "rank": rank}
            for rank in (1, 2, 3)]
    tie = [{"date": "20240101", "place": "A", "r": 2, "rank": rank}
           for rank in (1, 2, 2)]
    stopped = [{"date": "20240101", "place": "A", "r": 3, "rank": rank}
               for rank in (1, 2, None)]
    valid, reasons = classify_raw_rank_races(good + tie + stopped)
    assert ("20240101", "A", 1) in valid
    assert reasons[("20240101", "A", 2)] == "tie"
    assert reasons[("20240101", "A", 3)] == "nonfinish_disqualified_cancelled"


def test_complete_field_is_reported_separately():
    keys = [("20240101", "A", 1)] * 2
    meta = [{"rank": 1, "total_horses": 3}, {"rank": 3, "total_horses": 3}]
    result = filter_rank_dataset(
        np.zeros((2, 1)), np.asarray([1, 0]), keys, meta, {keys[0]})
    assert result[4] == set()
    assert result[5]["incomplete_feature_field"] == 1


def test_missing_official_winner_is_not_relabelled_as_constructed_winner():
    key = ("20240101", "A", 1)
    result = filter_rank_dataset(
        np.zeros((2, 1)), np.asarray([0, 0]), [key, key],
        [{"rank": 2, "total_horses": 3}, {"rank": 3, "total_horses": 3}],
        {key})
    assert len(result[0]) == 0
    assert result[5]["winner_missing_features"] == 1
