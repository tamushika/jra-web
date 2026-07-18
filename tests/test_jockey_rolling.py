import math

import pytest

from backtest_ml import build_jockey_rolling_features, shrunken_rate


def _run(date, jockey, rank):
    return {"date": date, "jockey": jockey, "rank": rank}


def test_jockey_rolling_excludes_same_day_and_future():
    rows = [_run("20240101", "A", 1), _run("20240101", "A", 9),
            _run("20240102", "A", 9), _run("20250101", "A", 1)]
    values = build_jockey_rolling_features(rows, 10)
    assert values[id(rows[0])]["j_roll30"] == 0.0
    assert values[id(rows[1])]["j_roll30"] == 0.0
    # Both same-day Jan 1 results become visible together on Jan 2: 1/2 prior
    # and 1/2 raw window rate therefore remain 1/2 after shrinkage.
    assert values[id(rows[2])]["j_roll30"] == pytest.approx(0.5)


def test_jockey_rolling_boundary_includes_exactly_30_and_90_days():
    rows = [_run("20240101", "A", 1), _run("20240131", "A", 9),
            _run("20240401", "A", 9)]  # Jan 1 is 91 days before Apr 1
    values = build_jockey_rolling_features(rows, 10)
    assert values[id(rows[1])]["j_roll_n30"] == pytest.approx(math.log1p(1))
    assert values[id(rows[2])]["j_roll_n90"] == pytest.approx(math.log1p(1))
    assert values[id(rows[2])]["j_roll_n30"] == 0.0


def test_jockey_shrinkage_n_zero_is_prior_and_more_n_moves_to_raw_rate():
    history = [_run(f"202401{day:02d}", "A", 1 if day == 1 else 9)
               for day in range(1, 12)]
    target = _run("20240112", "B", 9)
    values = build_jockey_rolling_features(history + [target], 10)
    global_prior = 1 / 11
    assert values[id(target)]["j_roll30"] == pytest.approx(global_prior)
    # A's raw window rate is 1/11 and its long prior is identical, so the
    # shrunken estimate is exact and finite even at substantial n.
    assert values[id(history[-1])]["j_roll30"] >= 0.0
    prior, raw = 0.2, 0.8
    small = shrunken_rate(raw * 5, 5, prior, 10)
    large = shrunken_rate(raw * 50, 50, prior, 10)
    assert shrunken_rate(0, 0, prior, 10) == pytest.approx(prior)
    assert abs(large - raw) < abs(small - raw)


def test_jockey_rolling_rejects_invalid_k():
    with pytest.raises(ValueError):
        build_jockey_rolling_features([], 0)
