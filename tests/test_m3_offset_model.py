import numpy as np

import backtest_ml


def _training_sample():
    X = np.array([
        [1.5, -0.2], [-0.4, 0.7], [0.1, -0.5],
        [-0.8, 0.4], [1.2, -0.6], [0.3, 0.8],
    ])
    y = np.array([1, 0, 0, 0, 1, 0])
    race_keys = ["race-a"] * 3 + ["race-b"] * 3
    return X, y, race_keys


def test_offset_none_matches_zero_offset():
    X, y, race_keys = _training_sample()

    without_offset = backtest_ml.fit_conditional_logit(X, y, race_keys)
    zero_offset = backtest_ml.fit_conditional_logit(
        X, y, race_keys, offset=np.zeros(len(y))
    )

    np.testing.assert_array_equal(without_offset, zero_offset)


def test_offset_stays_aligned_when_race_keys_are_unsorted():
    X, y, grouped_keys = _training_sample()
    offsets = np.array([-0.7, 0.3, 1.1, 0.8, -1.2, 0.2])
    interleaved_order = np.array([0, 3, 1, 4, 2, 5])

    grouped = backtest_ml.fit_conditional_logit(
        X, y, grouped_keys, offset=offsets
    )
    interleaved = backtest_ml.fit_conditional_logit(
        X[interleaved_order],
        y[interleaved_order],
        np.asarray(grouped_keys)[interleaved_order].tolist(),
        offset=offsets[interleaved_order],
    )

    np.testing.assert_allclose(interleaved, grouped, rtol=0.0, atol=1e-12)


def test_zero_residual_reproduces_market_probabilities():
    odds = np.array([2.4, 4.7, 7.5, 12.0])
    market_probabilities = (1.0 / odds) / np.sum(1.0 / odds)
    market_offset = np.log(market_probabilities)
    X = np.array([
        [0.5, -0.2], [-1.0, 0.4], [0.7, 1.1], [0.0, -0.8]
    ])
    zero_residual = np.zeros(X.shape[1])

    probabilities = backtest_ml._softmax(market_offset + X @ zero_residual)

    np.testing.assert_allclose(
        probabilities, market_probabilities, rtol=0.0, atol=1e-15
    )


def test_strong_l2_shrinks_residual_weight_norm():
    X, y, race_keys = _training_sample()
    offsets = np.array([-1.5, 0.2, 0.9, 0.7, -1.3, 0.1])

    weakly_regularized = backtest_ml.fit_conditional_logit(
        X, y, race_keys, l2=0.01, offset=offsets
    )
    strongly_regularized = backtest_ml.fit_conditional_logit(
        X, y, race_keys, l2=1e5, offset=offsets
    )

    assert np.linalg.norm(strongly_regularized) < np.linalg.norm(weakly_regularized)
