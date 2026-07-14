import numpy as np
import pytest

import backtest_ml
import backtest_place_model as place_model


def test_place_model_feature_contract_has_no_market_fields():
    assert place_model.NO_MARKET_FEATURES == [
        name for name in backtest_ml.FEATURES if name != "ln_odds"
    ]
    assert "ln_odds" not in place_model.NO_MARKET_FEATURES
    assert all("odds" not in name and "popularity" not in name
               for name in place_model.NO_MARKET_FEATURES)


def test_feature_dataset_delegates_to_t33_ability_interface(monkeypatch):
    calls = []

    def fake_builder(runs, cfg, date_to, **kwargs):
        calls.append((runs, cfg, date_to, kwargs))
        return "X", "y", "keys", "meta"

    monkeypatch.setattr(place_model, "build_consistent_feature_dataset", fake_builder)
    result = place_model.build_place_feature_dataset(
        ["run"], {"cfg": 1}, "20260630", db_path="fixture.db")

    assert result == ("X", "y", "keys", "meta")
    assert calls == [
        (["run"], {"cfg": 1}, "20260630",
         {"stats_source": "ability", "db_path": "fixture.db"})
    ]


def test_current_web_ability_scores_use_previous_year_snapshots(monkeypatch):
    snapshots = []

    class FakeProvider:
        def __init__(self, _db_path, as_of, **_kwargs):
            self.as_of = as_of
            snapshots.append(as_of)

        def __call__(self, *_args):
            return None

    def fake_scores(_runs, year_from, _cfg, **kwargs):
        year = int(year_from[:4])
        provider = kwargs["factor_table_provider"]
        runner = {"umaban": 1}
        return {(f"{year}0105", "中山", 1): [(float(year), runner)]}

    monkeypatch.setattr(place_model, "FoldFactorTableProvider", FakeProvider)
    monkeypatch.setattr(place_model, "score_all_runners", fake_scores)
    keys = [("20240105", "中山", 1), ("20250105", "中山", 1),
            ("20260105", "中山", 1)]
    meta = [{"umaban": 1}] * 3
    values = place_model.current_web_scores(
        [], keys, meta, {}, db_path="fixture.db", stats_source="ability")

    assert snapshots == ["20231231", "20241231", "20251231"]
    assert list(values) == [2024.0, 2025.0, 2026.0]


def test_current_web_control_is_pinned_to_production_csv_loader(monkeypatch):
    seen = []

    def fake_legacy(place, track, distance, base_dir):
        seen.append((place, track, distance, base_dir))
        return "legacy"

    def fake_scores(_runs, _date_from, _cfg, **kwargs):
        provider = kwargs["factor_table_provider"]
        assert provider("東京", "芝", 1600) == "legacy"
        return {("20250105", "東京", 1): [(3.0, {"umaban": 1})]}

    monkeypatch.setattr(place_model.scoring, "load_legacy_factor_table", fake_legacy)
    monkeypatch.setattr(place_model, "score_all_runners", fake_scores)
    values = place_model.current_web_scores(
        [], [("20250105", "東京", 1)], [{"umaban": 1}], {},
        stats_source="current")

    assert list(values) == [3.0]
    assert seen == [("東京", "芝", 1600, place_model.API_DIR)]


@pytest.mark.parametrize(
    ("rank", "field_size", "expected"),
    [
        (1, 7, 1),
        (2, 7, 1),
        (3, 7, 0),
        (3, 8, 1),
        (4, 8, 0),
    ],
)
def test_place_target_switches_for_seven_or_fewer_runners(rank, field_size, expected):
    assert place_model.place_target(rank, field_size) == expected


def _synthetic_model():
    n_features = len(place_model.NO_MARKET_FEATURES)
    return {
        "meta": {"purpose": "test"},
        "objective": "independent_place_logistic_l2",
        "features": list(place_model.NO_MARKET_FEATURES),
        "mean": [0.0] * n_features,
        "scale": [1.0] * n_features,
        "coef": [0.1] * n_features,
        "intercept": -0.2,
        "calibration": {"method": "platt", "slope": 0.8, "intercept": 0.1},
    }


def test_calibrated_model_json_round_trip(tmp_path):
    model = _synthetic_model()
    matrix = np.arange(2 * len(backtest_ml.FEATURES), dtype=float).reshape(
        2, len(backtest_ml.FEATURES)
    ) / 100.0
    before = place_model.predict_place_probability(model, matrix)

    path = tmp_path / "web_place_model.json"
    place_model.save_model(model, path)
    restored = place_model.load_model(path)
    after = place_model.predict_place_probability(restored, matrix)

    assert restored["calibration"] == model["calibration"]
    assert np.allclose(after, before)
    assert np.all((after > 0.0) & (after < 1.0))
    assert "ln_odds" not in restored["features"]


def test_scratch_output_path_rejects_production_or_other_model_names(tmp_path):
    allowed = tmp_path / "web_place_model.json"
    assert place_model.validate_model_output_path(allowed) == str(allowed.resolve())

    with pytest.raises(ValueError, match="web_place_model.json"):
        place_model.validate_model_output_path(tmp_path / "win5_ml_model.json")
    with pytest.raises(ValueError, match="api配下"):
        place_model.validate_model_output_path(
            place_model.API_DIR + "/data_files/common/web_place_model.json")


def test_production_refit_periods_and_metadata_are_explicit():
    rng = np.random.default_rng(36)
    rows_per_period = 8
    matrix = rng.normal(size=(rows_per_period * 3, len(backtest_ml.FEATURES)))
    targets = np.asarray([0, 1] * (len(matrix) // 2), dtype=int)
    dates = np.asarray(
        ["20210101"] * rows_per_period
        + ["20240101"] * rows_per_period
        + ["20250101"] * rows_per_period
    )

    model = place_model.fit_place_model(
        matrix, targets, dates,
        train_from=place_model.PRODUCTION_TRAIN_FROM,
        train_to=place_model.PRODUCTION_TRAIN_TO,
        calibration_from=place_model.PRODUCTION_CALIBRATION_FROM,
        calibration_to=place_model.PRODUCTION_CALIBRATION_TO,
        purpose="web_shadow_display",
    )

    assert model["meta"] == {
        "created_at": model["meta"]["created_at"],
        "purpose": "web_shadow_display",
        "statistics_source": "ability_db_yearly_as_of",
        "feature_config": "win5_weights.json",
        "train_period": "20210101-20241231",
        "calibration_period": "20250101-20251231",
        "n_train": 16,
        "n_calibration": 8,
    }
    assert "ln_odds" not in model["features"]


def test_private_factor_snapshot_attachment_is_explicit_and_valid():
    payload = place_model.build_snapshot_payload(
        {
            ("東京", "芝", 1600): {
                "baseline": {"win_rate": 8.0, "show_rate": 24.0},
                "jockey_w": {
                    "騎手A": {"win_rate": 10.0, "show_rate": 30.0},
                },
            },
        },
        as_of="20251231", stats_from="20210101",
        generated_at="2026-07-14T00:00:00+09:00",
    )
    model = {"meta": {"purpose": "web_shadow_display"}}

    assert place_model.attach_private_factor_snapshot(model, payload) is model
    assert model["meta"]["private_factor_snapshot"] == {
        "scope": "compute_place_prob_only",
        "source": "ability.db:runs",
        "schema_version": 1,
        "as_of": "20251231",
        "stats_from": "20210101",
        "strict_as_of": True,
        "course_count": 1,
    }


def test_platt_application_is_monotonic():
    calibration = {"method": "platt", "slope": 1.5, "intercept": -0.2}
    probabilities = place_model.apply_platt([-2.0, 0.0, 2.0], calibration)
    assert list(probabilities) == sorted(probabilities)
    assert all(0.0 < value < 1.0 for value in probabilities)


def test_selected_popularity_report_uses_place_payout_per_100_yen():
    members = [
        {"target": 1, "popularity": 2, "payout": 180.0},
        {"target": 0, "popularity": 5, "payout": None},
        {"target": 1, "popularity": 10, "payout": 420.0},
    ]
    races = [{"members": members, "model_order": [0, 1, 2]}]
    report = place_model.selected_popularity_report(races, "model_order", top_k=3)

    assert report["1-3人気"]["place_rate"] == 1.0
    assert report["1-3人気"]["place_roi"] == 180.0
    assert report["4-8人気"]["place_rate"] == 0.0
    assert report["4-8人気"]["place_roi"] == 0.0
    assert report["9人気以下"]["place_roi"] == 420.0
