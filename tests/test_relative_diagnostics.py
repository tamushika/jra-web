from types import SimpleNamespace

import numpy as np
import pytest

import backtest_ml
import backtest_relative_diagnostics as relative


def _dataset_rows():
    dates = ["20210101", "20220101", "20230101", "20240101", "20250101"]
    X = np.arange(len(dates) * 29, dtype=float).reshape(len(dates), 29)
    y = np.array([1, 0, 1, 0, 1])
    keys = [(date, "東京", 9) for date in dates]
    return X, y, keys


def test_t33_builder_is_called_once_with_relative_ability_path(monkeypatch):
    expected = (np.zeros((1, 29)), np.array([1]), [("20250101", "東京", 9)], [{}])
    calls = []

    def fake_builder(runs, cfg, date_to, **kwargs):
        calls.append((runs, cfg, date_to, kwargs))
        return expected

    monkeypatch.setattr(relative, "build_consistent_feature_dataset", fake_builder)
    actual = relative.build_relative_feature_dataset(["runs"], {"cfg": 1}, db_path="x.db")

    assert actual is expected
    assert calls == [(["runs"], {"cfg": 1}, "20260630", {
        "stats_source": "ability",
        "db_path": "x.db",
        "dataset_kwargs": {"relative": True},
    })]


def test_models_tune_from_2021_23_then_final_refit_through_2024(monkeypatch):
    X, y, keys = _dataset_rows()
    fit_shapes = []
    tune_shapes = []

    def fake_fit(matrix, labels, race_keys, **_kwargs):
        fit_shapes.append((matrix.shape, len(labels), tuple(race_keys)))
        return np.zeros(matrix.shape[1])

    def fake_temperature(scores, labels, race_keys):
        tune_shapes.append((scores.shape, len(labels), tuple(race_keys)))
        return 1.25

    monkeypatch.setattr(relative, "fit_conditional_logit", fake_fit)
    monkeypatch.setattr(relative, "fit_temperature", fake_temperature)
    models = relative.fit_relative_diagnostic_models(X, y, keys)

    assert [shape[0] for shape, _n, _keys in fit_shapes] == [3, 4, 3, 4]
    assert [shape[1] for shape, _n, _keys in fit_shapes] == [21, 21, 29, 29]
    assert all(item[0] == (1,) and item[1] == 1 for item in tune_shapes)
    assert all(item[2][0][0] == "20240101" for item in tune_shapes)
    assert models["base"]["temperature"] == 1.25
    assert models["relative"]["temperature"] == 1.25
    assert models["base"]["selection_train_rows"] == 3
    assert models["base"]["train_rows"] == 4


class _IdentityScaler:
    def __init__(self, columns):
        self.scale_ = np.ones(columns)

    def transform(self, matrix):
        return np.asarray(matrix)


def _complete_race(date):
    X = np.zeros((8, 29), dtype=float)
    # Both models rank horse 1 first; relative model also has a distinct column.
    X[:, 0] = np.arange(8, 0, -1)
    X[:, 21] = np.arange(8, 0, -1)
    y = np.array([1] + [0] * 7)
    key = (date, "東京", 9)
    keys = [key] * 8
    meta = [
        {
            "horse": f"H{i}", "umaban": i + 1,
            "rank": 1 if i == 0 else i + 1,
            "popularity": i + 1, "total_horses": 8,
            "win_pay": "200" if i == 0 else f"({3.0 + i:.1f})",
        }
        for i in range(8)
    ]
    return X, y, keys, meta


def test_period_comparison_uses_one_population_for_m0_base_and_relative():
    X, y, keys, meta = _complete_race("20250101")
    base_weights = np.zeros(21)
    base_weights[0] = 1.0
    relative_weights = np.zeros(29)
    relative_weights[21] = 1.0
    models = {
        "base": {"scaler": _IdentityScaler(21), "weights": base_weights,
                 "temperature": 1.0},
        "relative": {"scaler": _IdentityScaler(29), "weights": relative_weights,
                     "temperature": 1.0},
    }

    report = relative.evaluate_relative_period(
        models, X, y, keys, meta, "20250101", "20251231")

    assert report["races"] == 1
    assert report["horses"] == 8
    assert list(report["models"]) == [
        "M0 popularity", "base current21", "base+relative"]
    assert all(metrics["coverage"] == [1.0] * 4
               for metrics in report["models"].values())


def test_coefficient_report_exposes_relative_and_effective_raw_slopes():
    names = backtest_ml.active_feature_names(relative=True)
    weights = np.arange(1, 30, dtype=float)
    scales = np.arange(1, 30, dtype=float)  # every raw coefficient is exactly 1
    model = {
        "feature_names": names,
        "weights": weights,
        "scaler": SimpleNamespace(scale_=scales),
    }

    report = relative.relative_coefficient_report(model)

    assert list(report["standardized_relative"]) == backtest_ml.RELATIVE_FEATURES
    assert report["effective"]["tfeat"]["raw_coefficient"] == pytest.approx(3.0)
    assert report["effective"]["j_pts"]["raw_coefficient"] == pytest.approx(2.0)
    assert report["effective"]["tfeat_rank"]["raw_coefficient"] == pytest.approx(1.0)
    assert "一意に解釈できない" in report["identifiability_note"]


def test_run_diagnostic_connects_m3_with_no_market_plus_relative(monkeypatch):
    dataset = (np.zeros((1, 29)), np.array([1]),
               [("20250101", "東京", 9)], [{}])
    m3_calls = []

    monkeypatch.setattr(relative, "build_relative_feature_dataset",
                        lambda *_args, **_kwargs: dataset)
    monkeypatch.setattr(relative, "fit_relative_diagnostic_models",
                        lambda *_args: {"base": {}, "relative": {}})
    monkeypatch.setattr(relative, "evaluate_relative_period",
                        lambda *_args: {
                            "models": {},
                            "sample_signature": ((
                                ("20250101", "東京", 9), (("1", "H1"),)),),
                        })
    monkeypatch.setattr(relative, "relative_coefficient_report",
                        lambda _model: {"ok": True})

    def fake_m3(*args, **kwargs):
        m3_calls.append((args, kwargs))
        signature = ((("20250101", "東京", 9), (("1", "H1"),)),)
        return {"periods": {
            period_name: {"sample_signature": signature}
            for period_name in relative.PERIODS
        }}

    monkeypatch.setattr(
        relative.importlib, "import_module",
        lambda name: SimpleNamespace(evaluate_m3_dataset=fake_m3)
    )
    report = relative.run_relative_diagnostic([], {}, db_path="x.db")

    assert all(actual is expected
               for actual, expected in zip(m3_calls[0][0], dataset))
    residual_names = m3_calls[0][1]["residual_feature_names"]
    assert residual_names == (backtest_ml.NO_MARKET_FEATURES
                              + backtest_ml.RELATIVE_FEATURES)
    assert "ln_odds" not in residual_names
    assert set(report["m3_relative"]["periods"]) == set(relative.PERIODS)


def test_write_is_not_an_accepted_cli_option():
    with pytest.raises(SystemExit) as exc_info:
        relative.parse_args(["--write"])
    assert exc_info.value.code == 2


def test_backtest_ml_relative_cli_is_evaluation_only():
    args = backtest_ml.parse_cli_args(["--relative"])
    assert args.relative
    with pytest.raises(SystemExit) as exc_info:
        backtest_ml.parse_cli_args(["--relative", "--write"])
    assert exc_info.value.code == 2


def test_backtest_ml_delegates_relative_diagnostic(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["backtest_ml.py", "--relative"])
    monkeypatch.setattr(relative, "main", lambda argv=None: calls.append(argv))

    backtest_ml.main()

    assert calls == [[]]
