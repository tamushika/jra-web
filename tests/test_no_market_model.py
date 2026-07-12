import numpy as np
import pytest

import backtest_ml


def _runner(number, rank, popularity, odds):
    return {
        "date": "20250105",
        "place": "中山",
        "r": 9,
        "rank": rank,
        "popularity": popularity,
        "umaban": number,
        "total_horses": 8,
        "win_pay": str(round(odds * 100)) if rank == 1 else f"({odds})",
    }


def test_no_market_feature_set_excludes_only_ln_odds():
    assert "ln_odds" in backtest_ml.FEATURES
    assert "ln_odds" not in backtest_ml.NO_MARKET_FEATURES
    assert len(backtest_ml.NO_MARKET_FEATURES) == len(backtest_ml.FEATURES) - 1
    assert [name for name in backtest_ml.FEATURES if name != "ln_odds"] == (
        backtest_ml.NO_MARKET_FEATURES
    )

    matrix = np.arange(2 * len(backtest_ml.FEATURES)).reshape(
        2, len(backtest_ml.FEATURES)
    )
    selected = backtest_ml.feature_matrix(matrix, backtest_ml.NO_MARKET_FEATURES)
    odds_index = backtest_ml.FEATURES.index("ln_odds")
    assert selected.shape == (2, len(backtest_ml.FEATURES) - 1)
    assert np.array_equal(selected, np.delete(matrix, odds_index, axis=1))


def test_model_comparison_uses_same_races_and_normalized_market_probability():
    key = ("20250105", "中山", 9)
    odds = [2.5, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]
    meta = [_runner(index + 1, 1 if index == 0 else index + 1,
                    index + 1, value)
            for index, value in enumerate(odds)]
    current_scores = np.arange(8, 0, -1, dtype=float)
    m2_scores = np.arange(1, 9, dtype=float)

    races, skipped = backtest_ml.build_model_comparison_races(
        current_scores, m2_scores, [key] * 8, meta,
        "20250101", "20251231",
    )

    assert skipped == {}
    assert len(races) == 1
    assert races[0]["market_probabilities"].sum() == pytest.approx(1.0)
    assert races[0]["current_probabilities"].sum() == pytest.approx(1.0)
    assert races[0]["m2_probabilities"].sum() == pytest.approx(1.0)

    result = backtest_ml.evaluate_model_comparison(races)
    assert result["races"] == 1
    assert result["models"]["M0 人気順"]["coverage"] == [1.0] * 4
    assert result["models"]["現行CL (オッズ込み)"]["coverage"] == [1.0] * 4
    assert result["models"]["M2 (オッズなし)"]["coverage"] == [0.0] * 4
    assert result["m2_market_spearman"] == pytest.approx(-1.0)


def test_incomplete_feature_race_is_excluded_from_all_models():
    key = ("20250105", "中山", 9)
    meta = [_runner(index + 1, 1 if index == 0 else index + 1,
                    index + 1, 2.0 + index)
            for index in range(8)]
    for runner in meta:
        runner["total_horses"] = 9
    races, skipped = backtest_ml.build_model_comparison_races(
        np.arange(8), np.arange(8), [key] * 8, meta,
        "20250101", "20251231",
    )
    assert races == []
    assert skipped == {"incomplete_features": 1}


def test_no_market_write_combination_is_rejected():
    with pytest.raises(SystemExit) as exc_info:
        backtest_ml.parse_cli_args(["--no-market", "--write"])
    assert exc_info.value.code == 2


def test_default_cli_mode_is_unchanged():
    args = backtest_ml.parse_cli_args([])
    assert not args.no_market
    assert not args.write
    assert not args.lgbm
