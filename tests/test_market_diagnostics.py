from __future__ import annotations

import numpy as np
import pytest

import backtest_market_diagnostics as diagnostics
import backtest_ml
from backtest_ml import FEATURES


def _race(date, *, total_horses=8, missing_odds=False):
    key = (date, "東京", 9)
    features = []
    labels = []
    keys = []
    meta = []
    for index in range(8):
        row = np.zeros(len(FEATURES), dtype=float)
        row[0] = index
        row[1] = int(date[:4]) - 2020
        features.append(row)
        labels.append(1 if index == 0 else 0)
        keys.append(key)
        if index == 0:
            win_pay = "200"
        else:
            win_pay = f"({index + 2:.1f})"
        if missing_odds and index == 7:
            win_pay = ""
        meta.append({
            "horse": f"{date}-H{index}",
            "umaban": index + 1,
            "rank": 1 if index == 0 else index + 1,
            "popularity": index + 1,
            "total_horses": total_horses,
            "win_pay": win_pay,
        })
    return features, labels, keys, meta


def _combine(*parts):
    features, labels, keys, meta = [], [], [], []
    for part_features, part_labels, part_keys, part_meta in parts:
        features.extend(part_features)
        labels.extend(part_labels)
        keys.extend(part_keys)
        meta.extend(part_meta)
    return np.asarray(features), np.asarray(labels), keys, meta


def _all_period_dataset():
    return _combine(
        _race("20210110"),
        _race("20220110"),
        _race("20230110"),
        _race("20240110"),
        _race("20250110"),
        _race("20260110"),
    )


def test_run_uses_t33_ability_yearly_dataset_builder(monkeypatch):
    calls = []

    def builder(runs, cfg, date_to, **kwargs):
        calls.append((runs, cfg, date_to, kwargs))
        return "features", "labels", "keys", "meta"

    monkeypatch.setattr(
        diagnostics,
        "evaluate_m3_dataset",
        lambda *args, **kwargs: {"result": (args, kwargs)},
    )
    result = diagnostics.run_m3_diagnostic(
        ["runs"], {"cfg": 1}, db_path="ability-test.db",
        dataset_builder=builder,
    )

    assert result["result"][0] == ("features", "labels", "keys", "meta")
    assert calls == [(
        ["runs"], {"cfg": 1}, "20260630",
        {"stats_source": "ability", "db_path": "ability-test.db"},
    )]


def test_common_filter_excludes_incomplete_and_missing_odds_races():
    dataset = diagnostics.prepare_complete_races(*_combine(
        _race("20250110"),
        _race("20250111", total_horses=9),
        _race("20250112", missing_odds=True),
    ))

    assert dataset.race_count == 1
    assert len(dataset.labels) == 8
    assert dataset.skipped == {"incomplete_features": 1, "missing_odds": 1}
    reproduced = diagnostics._race_softmax(
        dataset.market_offsets, dataset.race_keys)
    np.testing.assert_allclose(
        reproduced, dataset.market_probabilities, rtol=0.0, atol=1e-15)


def test_market_inversion_metrics_count_reversed_pairs():
    dataset = diagnostics.prepare_complete_races(*_combine(_race("20250110")))

    result = diagnostics.evaluate_same_population(
        dataset, {"m3": -dataset.market_offsets})
    changes = result["market_changes"]["m3"]

    assert changes["mean_inversion_pairs"] == 28
    assert changes["inversion_pair_rate"] == 1.0
    assert changes["any_swap_rate"] == 1.0
    assert result["races"] == 1
    assert result["horses"] == 8
    assert set(result["models"]) == {"m0", "m3"}


def test_lambda_tie_selects_stronger_regularization():
    selected = diagnostics.select_lambda([
        {"lambda": 0.1, "tune_logloss": 0.5},
        {"lambda": 100.0, "tune_logloss": 0.5 + 5e-13},
        {"lambda": 1.0, "tune_logloss": 0.6},
    ])
    assert selected["lambda"] == 100.0


def test_time_split_l2_scaling_fixed_models_and_report(monkeypatch, capsys):
    fit_calls = []
    temperature_calls = []

    def fake_fit(X, y, race_keys, l2=1.0, max_iter=300, offset=None):
        fit_calls.append({
            "columns": X.shape[1],
            "l2": l2,
            "races": len(set(race_keys)),
            "offset": None if offset is None else np.asarray(offset).copy(),
        })
        return np.zeros(X.shape[1])

    def fake_temperature(scores, y, race_keys, grid=None):
        temperature_calls.append((len(scores), len(set(race_keys))))
        return 1.25

    monkeypatch.setattr(diagnostics, "fit_conditional_logit", fake_fit)
    monkeypatch.setattr(diagnostics, "fit_temperature", fake_temperature)

    report = diagnostics.evaluate_m3_dataset(*_all_period_dataset())

    assert report["splits"] == {
        "train": {"rows": 24, "races": 3},
        "tune": {"rows": 8, "races": 1},
        "2025": {"rows": 8, "races": 1},
        "2026H1": {"rows": 8, "races": 1},
    }
    assert len(fit_calls) == 8
    assert fit_calls[0] == {
        "columns": len(FEATURES), "l2": 1.0, "races": 3, "offset": None,
    }
    assert fit_calls[1] == {
        "columns": len(FEATURES), "l2": 1.0, "races": 4, "offset": None,
    }
    residual_calls = fit_calls[2:]
    assert [call["columns"] for call in residual_calls] == [len(FEATURES) - 1] * 6
    assert [call["l2"] for call in residual_calls] == pytest.approx(
        [0.03, 0.3, 3.0, 30.0, 300.0, 400.0])
    assert all(call["offset"] is not None for call in residual_calls)
    assert temperature_calls == [(8, 1)]
    assert report["regularization"]["selected_lambda"] == 100.0
    assert report["regularization"]["final_fit_races"] == 4
    assert report["regularization"]["final_internal_l2"] == 400.0
    assert report["current_cl"]["temperature"] == 1.25
    assert report["current_cl"]["train_rows"] == 32
    assert report["m3"]["temperature"] is None
    assert report["m3"]["market_limit_max_abs_error"] <= 1e-15
    assert report["m3"]["residual_feature_names"] == [
        name for name in FEATURES if name != "ln_odds"]
    assert report["m3"]["selected_weights"] == [0.0] * (len(FEATURES) - 1)
    assert all(result["m3_floor"]["passed"]
               for result in report["periods"].values())

    diagnostics.print_report(report)
    output = capsys.readouterr().out
    assert "internal L2 = lambda * 3 train races" in output
    assert "population: race 9+, official field 8+" in output
    assert "M3 temperature: disabled" in output
    assert "pair rate=" in output
    assert "M3 realized top-k floor: PASS" in output


@pytest.mark.parametrize("argv", [
    ["--m3", "--write"],
    ["--m3", "--stats-snapshot"],
    ["--m3", "--no-market"],
    ["--m3", "--lgbm"],
])
def test_m3_cli_rejects_unsafe_or_ambiguous_combinations(argv):
    with pytest.raises(SystemExit) as exc_info:
        backtest_ml.parse_cli_args(argv)
    assert exc_info.value.code == 2


def test_backtest_ml_m3_delegates_without_requiring_snapshot_flag(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["backtest_ml.py", "--m3"])
    monkeypatch.setattr(diagnostics, "main", lambda: calls.append("m3"))

    backtest_ml.main()

    assert calls == ["m3"]
