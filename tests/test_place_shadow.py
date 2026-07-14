"""T36 Web複勝率βのライブ接続契約。"""

import json
from pathlib import Path

import numpy as np
import pytest
from bs4 import BeautifulSoup

import backtest_ml
import backtest_place_model as place_model
from api import scoring
from api.factor_snapshot import validate_snapshot_payload


ROOT = Path(__file__).parents[1]
PRODUCTION_MODEL_PATH = (
    ROOT / "api" / "data_files" / "common" / "web_place_model.json"
)


def _synthetic_model():
    feature_count = len(place_model.NO_MARKET_FEATURES)
    return {
        "meta": {
            "purpose": "web_shadow_display",
            "feature_config": "win5_weights.json",
        },
        "objective": "independent_place_logistic_l2",
        "features": list(place_model.NO_MARKET_FEATURES),
        "mean": [float(index) / 20.0 for index in range(feature_count)],
        "scale": [1.0 + float(index) / 50.0 for index in range(feature_count)],
        "coef": [(-1.0 if index % 2 else 1.0) * 0.025
                 for index in range(feature_count)],
        "intercept": -0.2,
        "calibration": {"method": "platt", "slope": 0.8, "intercept": 0.1},
    }


def test_live_place_probability_matches_offline_predictor(monkeypatch):
    model = _synthetic_model()
    feature_values = {
        name: (index + 1) / 10.0
        for index, name in enumerate(backtest_ml.FEATURES)
    }
    matrix = np.asarray([[
        feature_values[name] for name in backtest_ml.FEATURES
    ]], dtype=float)
    expected = place_model.predict_place_probability(model, matrix)[0]

    win5_cfg = {"source": "win5_weights.json"}
    seen = []

    def fake_features(_horse, _context, _table, cfg):
        seen.append(cfg)
        return feature_values

    monkeypatch.setattr(scoring, "load_place_model", lambda: model)
    monkeypatch.setattr(scoring, "_ml_features", fake_features)
    probability, details = scoring.compute_place_prob(
        {}, {}, None, win5_cfg)

    assert probability == pytest.approx(expected, abs=1e-12)
    assert seen == [win5_cfg]
    assert details[-1].startswith("校正済み複勝率:")


def test_missing_model_has_no_score_fallback_and_frontend_renders_dash(
        monkeypatch, tmp_path):
    monkeypatch.setattr(scoring, "PLACE_MODEL_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setitem(scoring._PLACE_MODEL_CACHE, "model", None)

    assert scoring.load_place_model() is None
    assert scoring.compute_place_prob({}, {}, None, {}) == (None, [])

    script = (ROOT / "script.js").read_text(encoding="utf-8")
    assert "h.place_prob != null" in script
    assert ": '-')" in script


@pytest.mark.parametrize("market_name", ["ln_odds", "popularity", "market_score"])
def test_live_place_probability_rejects_market_features(monkeypatch, market_name):
    model = _synthetic_model()
    model["features"][0] = market_name
    called = []
    monkeypatch.setattr(scoring, "load_place_model", lambda: model)
    monkeypatch.setattr(
        scoring, "_ml_features",
        lambda *_args: called.append(True) or {},
    )

    assert scoring.compute_place_prob({}, {}, None, {}) == (None, [])
    assert called == []


def test_web_connection_uses_t16_win5_feature_config_and_is_shadow_only():
    source = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    call = "scoring.compute_place_prob(\n                h, race_context, factor_table, win5_cfg)"
    assert call in source
    assert "place_prob" not in (ROOT / "jra_ev.py").read_text(encoding="utf-8")


def test_shadow_ui_contract_tracks_the_nineteen_column_layout():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    headers = [
        node.get_text(strip=True)
        for node in soup.select("#horsesTable thead th")
    ]
    assert len(headers) == 19
    assert headers[3:6] == ["回収スコア", "的中スコア", "複勝率β"]
    legend = soup.select_one(".place-prob-legend").get_text(" ", strip=True)
    assert "市場非依存の参考指標" in legend
    assert "馬券判断には用いない" in legend

    script = (ROOT / "script.js").read_text(encoding="utf-8")
    assert "'複勝率β': 'place_prob'" in script
    assert "h.place_prob != null ? (h.place_prob * 100).toFixed(1) + '%' : '-'" in script
    assert "histTd.colSpan = 19" in script
    assert "if (idx === 9) { // idx 9 is h.name" in script
    assert "if (idx === 15 && h.sire_rank)" in script

    styles = (ROOT / "style.css").read_text(encoding="utf-8")
    assert "6複勝率β" in styles and "19印" in styles
    assert '#horsesTable td:nth-child(6)::before { content: "複勝率β: ";' in styles
    assert "#horsesTable td:nth-child(19) { grid-column: 1 / -1; grid-row: 7;" in styles


def test_private_factor_table_is_selected_only_for_place_probability(monkeypatch):
    model = _synthetic_model()
    private_table = {"baseline": {"win_rate": 8.0, "show_rate": 24.0}}
    caller_table = {"source": "legacy-live"}
    model["meta"]["private_factor_snapshot"] = {
        "scope": "compute_place_prob_only",
    }
    model["_private_factor_tables"] = {
        ("東京", "芝", 1600): private_table,
    }
    captured = []
    feature_values = {
        name: float(index) / 10.0
        for index, name in enumerate(backtest_ml.FEATURES)
    }
    monkeypatch.setattr(scoring, "load_place_model", lambda: model)
    monkeypatch.setattr(
        scoring, "_ml_features",
        lambda _h, _rc, table, _cfg: captured.append(table) or feature_values,
    )

    probability, _details = scoring.compute_place_prob(
        {}, {"venue": "東京", "type": "芝", "dist": 1600},
        caller_table, {})

    assert probability is not None
    assert captured == [private_table]


def test_old_artifact_without_private_snapshot_uses_callers_table(monkeypatch):
    model = _synthetic_model()
    caller_table = {"source": "legacy-live"}
    captured = []
    monkeypatch.setattr(scoring, "load_place_model", lambda: model)
    monkeypatch.setattr(
        scoring, "_ml_features",
        lambda _h, _rc, table, _cfg: captured.append(table) or {
            name: 0.0 for name in backtest_ml.FEATURES
        },
    )

    probability, _details = scoring.compute_place_prob(
        {}, {"venue": "東京", "type": "芝", "dist": 1600},
        caller_table, {})

    assert probability is not None
    assert captured == [caller_table]


@pytest.mark.parametrize(
    "private_payload",
    [{"invalid": True}, {
        "_meta": {
            "schema_version": 1, "source": "ability.db:runs",
            "generated_at": "2026-07-14T00:00:00+09:00",
            "stats_from": "20210101", "as_of": "20251231",
            "window_years": 5, "strict_as_of": True, "course_count": 1,
        },
        "courses": [{
            "venue": "中山", "race_type": "芝", "distance": 1600,
            "table": {"baseline": {"win_rate": 8.0, "show_rate": 24.0}},
        }],
    }],
)
def test_declared_invalid_or_missing_course_snapshot_fails_closed(
        monkeypatch, private_payload):
    model = _synthetic_model()
    model["private_factor_snapshot"] = private_payload
    called = []
    monkeypatch.setattr(scoring, "load_place_model", lambda: model)
    monkeypatch.setattr(
        scoring, "_ml_features",
        lambda *_args: called.append(True) or {},
    )

    assert scoring.compute_place_prob(
        {}, {"venue": "東京", "type": "芝", "dist": 1600},
        {"source": "legacy-live"}, {}) == (None, [])
    assert called == []


def test_private_place_snapshot_cannot_change_existing_ml_score(monkeypatch):
    caller_table = {"source": "existing-score-table"}
    captured = []
    monkeypatch.setattr(scoring, "load_ml_model", lambda: {
        "features": ["j_pts"], "mean": [0.0], "sd": [1.0],
        "coef": [1.0], "display_scale": 1.0,
    })
    monkeypatch.setattr(
        scoring, "_ml_features",
        lambda _h, _rc, table, _cfg: captured.append(table) or {"j_pts": 1.0},
    )

    score, _details = scoring.compute_score_ml(
        {}, {"venue": "東京", "type": "芝", "dist": 1600},
        caller_table, {})

    assert score == 1.0
    assert captured == [caller_table]


def test_production_shadow_model_artifact_contract():
    with PRODUCTION_MODEL_PATH.open(encoding="utf-8") as file_obj:
        model = json.load(file_obj)

    assert model["meta"]["purpose"] == "web_shadow_display"
    assert model["meta"]["feature_config"] == "win5_weights.json"
    assert model["meta"]["train_period"] == "20210101-20241231"
    assert model["meta"]["calibration_period"] == "20250101-20251231"
    assert model["meta"]["n_train"] == 169309
    assert model["meta"]["n_calibration"] == 42552
    private_meta = model["meta"]["private_factor_snapshot"]
    assert private_meta == {
        "scope": "compute_place_prob_only",
        "source": "ability.db:runs",
        "schema_version": 1,
        "as_of": "20251231",
        "stats_from": "20210101",
        "strict_as_of": True,
        "course_count": 99,
    }
    indexed = validate_snapshot_payload(model["private_factor_snapshot"])
    assert indexed["meta"]["course_count"] == 99
    assert len(indexed["tables"]) == 99
    assert model["features"] == place_model.NO_MARKET_FEATURES
    assert len(model["features"]) == 20
    assert not any(
        token in name.casefold()
        for name in model["features"]
        for token in ("odds", "popularity", "market", "pop_", "_pop")
    )
