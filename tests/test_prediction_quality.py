"""Prediction probabilities require a complete, finite, ML-derived field.

All fixtures are synthetic. Model/config loaders and persistence are mocked;
these tests neither consume racing outcomes nor touch the production database.
"""
from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jra_ev  # noqa: E402
import jra_win5  # noqa: E402


scoring = jra_ev.scoring
PARAMS = {"ev_threshold": 1.1, "max_odds": 50.0, "min_prob": 0.02}


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    """Production persistence and outbound HTTP must never run in this suite."""
    store = Mock()
    store.save_win5_prediction.return_value = "test-win5-prediction"
    monkeypatch.setattr(jra_ev, "LoggingStore", Mock(return_value=store))
    monkeypatch.setattr(jra_win5, "LoggingStore", Mock(return_value=store))
    monkeypatch.setattr(jra_win5, "log_race_prediction", Mock(return_value={}))

    def no_network(*_args, **_kwargs):
        raise AssertionError("Network access is forbidden in prediction quality tests")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)


@pytest.fixture
def ml_model(monkeypatch):
    model = {
        "features": ["n_prior"], "mean": [0.0], "sd": [1.0],
        "coef": [2.0], "display_scale": 10.0,
        "objective": "conditional_logit", "prob_temperature": 1.0,
    }
    monkeypatch.setattr(scoring, "load_ml_model", lambda: model)
    monkeypatch.setattr(scoring, "_ml_features", lambda *_args: {"n_prior": 1.5})
    return model


def _raw_horse():
    return {"num": 1, "name": "既走馬", "hist": [{"rank": "4"}]}


def _horse(num, score=1.0, *, source="ml", scratched=False):
    return {
        "num": num, "name": f"馬{num}", "odds": 4.0, "pop": num,
        "ml_score": score, "score": score, "score_source": source,
        "scratched": scratched, "web_score": 10.0 - num,
    }


def test_assess_valid_ml_preserves_score_and_legacy_tuple(ml_model):
    assessment = scoring.assess_ml_score(_raw_horse(), {}, {}, {})

    assert assessment["source"] == "ml"
    assert assessment["failure_reason"] is None
    assert assessment["score"] == 30.0
    assert assessment["details"]
    assert scoring.compute_score_ml(_raw_horse(), {}, {}, {}) == (
        assessment["score"], assessment["details"])


def test_assess_missing_model_marks_manual_score_as_reference(monkeypatch):
    monkeypatch.setattr(scoring, "load_ml_model", lambda: None)
    fallback = Mock(return_value=(8.5, ["手調整スコア"]))
    monkeypatch.setattr(scoring, "compute_score", fallback)

    assessment = scoring.assess_ml_score(_raw_horse(), {}, {}, {})

    assert assessment["source"] == "fallback"
    assert assessment["score"] == 8.5
    assert assessment["failure_reason"]
    fallback.assert_called_once()


def test_assess_feature_exception_never_labels_fallback_as_ml(monkeypatch, ml_model):
    monkeypatch.setattr(
        scoring, "_ml_features", Mock(side_effect=ValueError("bad feature")))
    monkeypatch.setattr(scoring, "compute_score", lambda *_args: (8.5, ["手調整"]))

    assessment = scoring.assess_ml_score(_raw_horse(), {}, {}, {})

    assert assessment["source"] == "fallback"
    assert assessment["score"] == 8.5
    assert assessment["failure_reason"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_assess_nonfinite_ml_result_cannot_enter_probability(monkeypatch, ml_model, value):
    monkeypatch.setattr(scoring, "_ml_features", lambda *_args: {"n_prior": value})
    monkeypatch.setattr(scoring, "compute_score", lambda *_args: (8.5, ["手調整"]))

    assessment = scoring.assess_ml_score(_raw_horse(), {}, {}, {})

    assert assessment["source"] != "ml"
    assert assessment["failure_reason"]
    assert assessment["score"] is None or assessment["score"] == 8.5


def test_assess_debut_is_unavailable_without_manual_substitution(monkeypatch):
    fallback = Mock(side_effect=AssertionError("Debut horses must not be substituted"))
    monkeypatch.setattr(scoring, "compute_score", fallback)

    assessment = scoring.assess_ml_score({"num": 1, "hist": []}, {}, {}, {})

    assert assessment["source"] == "unavailable"
    assert assessment["score"] is None
    assert assessment["failure_reason"]
    fallback.assert_not_called()


@pytest.mark.parametrize("source", [None, "fallback", "unavailable", "unknown"])
def test_quality_rejects_numerical_scores_without_verified_ml_source(source):
    horses = [_horse(1), _horse(2, source=source)]
    if source is None:
        del horses[1]["score_source"]

    quality = scoring.ml_probability_quality(horses)

    assert quality["ok"] is False
    assert quality["scored"] == 1
    assert quality["total"] == 2
    assert len(quality["missing"]) == 1
    assert quality["missing"][0]["num"] == 2
    assert quality["missing"][0]["reason"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
def test_quality_rejects_missing_or_nonfinite_score_even_with_ml_source(value):
    quality = scoring.ml_probability_quality([_horse(1), _horse(2, value)])

    assert quality["ok"] is False
    assert quality["scored"] == 1
    assert quality["total"] == 2
    assert quality["missing"][0]["num"] == 2
    assert quality["missing"][0]["reason"]


def test_quality_excludes_scratched_horses_and_supports_win5_score_key():
    horses = [_horse(1), _horse(2), _horse(3, None, source=None, scratched=True)]
    for horse in horses:
        horse.pop("ml_score")

    quality = scoring.ml_probability_quality(horses, score_key="score")

    assert quality["ok"] is True
    assert quality["scored"] == 2
    assert quality["total"] == 2
    assert quality["missing"] == []


@pytest.mark.parametrize("horses", [[], [_horse(1)], [_horse(1, scratched=True)]])
def test_quality_requires_at_least_two_active_horses(horses):
    assert scoring.ml_probability_quality(horses)["ok"] is False


def test_ev_thirteen_of_sixteen_cannot_be_renormalized(monkeypatch):
    horses = [_horse(i + 1) for i in range(13)]
    horses.extend(_horse(i + 14, None, source="unavailable") for i in range(3))
    for horse in horses:
        horse.update(win_prob=0.5, ev=2.0, picked=True)
    probability = Mock(side_effect=AssertionError("Partial fields must not reach softmax"))
    monkeypatch.setattr(scoring, "win_probs_from_ml_scores", probability)

    assert jra_ev.compute_picks(horses, PARAMS) == 0

    probability.assert_not_called()
    assert all(h["win_prob"] is None and h["ev"] is None and h["picked"] is False
               for h in horses)


def test_ev_complete_active_field_preserves_probability_and_ev(monkeypatch):
    horses = [_horse(1, 2.0), _horse(2, 1.0), _horse(3, 99.0, scratched=True)]
    probability = Mock(return_value=[0.75, 0.25])
    monkeypatch.setattr(scoring, "win_probs_from_ml_scores", probability)

    assert jra_ev.compute_picks(horses, PARAMS) == 1

    probability.assert_called_once_with([2.0, 1.0])
    assert [h["win_prob"] for h in horses] == [0.75, 0.25, None]
    assert [h["ev"] for h in horses] == [3.0, 1.0, None]
    assert [h["picked"] for h in horses] == [True, False, False]


@pytest.fixture
def win5_races(monkeypatch, ml_model):
    monkeypatch.setattr(scoring, "load_score_weights", lambda *_args: {
        "version": "test", "use_ml": True,
        "allocation": {"max_picks_per_race": 2},
    })
    return [{
        "venue": "東京", "race_info": f"東京 {i + 7}R", "upset_rank": "B",
        "horses": [_horse(1, 10.0), _horse(2, 0.0)],
        "_logging": {"prediction_run_id": f"test-run-{i}", "race_id": f"test-race-{i}"},
    } for i in range(5)]


@pytest.mark.parametrize("problem", ["missing", "fallback", "unknown", "nan"])
def test_win5_one_incomplete_race_blocks_entire_plan(monkeypatch, win5_races, problem):
    horse = win5_races[2]["horses"][1]
    if problem == "missing":
        horse["score"] = None
    elif problem == "nan":
        horse["score"] = float("nan")
    elif problem == "unknown":
        horse.pop("score_source")
    else:
        horse["score_source"] = "fallback"
    allocation = Mock(side_effect=AssertionError("Invalid input must not be allocated"))
    monkeypatch.setattr(scoring, "allocate_picks_prob", allocation)
    monkeypatch.setattr(scoring, "allocate_picks", allocation)

    result = jra_win5.build_kaime(win5_races, 8, False)

    assert result["success"] is False
    assert result["error"]
    assert result["picks"] == []
    assert result["est_hit_rate"] is None
    assert result["alloc_method"] == "unavailable"
    allocation.assert_not_called()
    jra_win5.LoggingStore.assert_not_called()


def test_win5_ml_mode_cannot_fall_back_to_rank_when_probabilities_unavailable(
        monkeypatch, win5_races):
    monkeypatch.setattr(scoring, "win_probs_from_ml_scores", lambda _scores: None)
    allocation = Mock(side_effect=AssertionError("ML mode must not silently change estimator"))
    monkeypatch.setattr(scoring, "allocate_picks", allocation)

    result = jra_win5.build_kaime(win5_races, 8, False)

    assert result["success"] is False
    assert result["picks"] == []
    assert result["est_hit_rate"] is None
    assert result["alloc_method"] == "unavailable"
    allocation.assert_not_called()


@pytest.mark.parametrize("single_axis", [False, True])
def test_win5_complete_ml_field_preserves_existing_probability_allocation(
        win5_races, single_axis):
    before = deepcopy(win5_races)
    probabilities = [scoring.win_probs_from_ml_scores([10.0, 0.0]) for _ in range(5)]
    expected_picks, expected_est = scoring.allocate_picks_prob(
        probabilities, 8, 2, fixed={0} if single_axis else None)

    result = jra_win5.build_kaime(win5_races, 8, single_axis)

    assert result["success"] is True
    assert result["alloc_method"] == "prob"
    assert [race["k"] for race in result["picks"]] == expected_picks
    assert result["est_hit_rate"] == round(expected_est, 5)
    assert result["total_points"] <= 8
    assert result["win5_prediction_id"] == "test-win5-prediction"
    assert win5_races == before


def test_win5_ignores_scratched_high_score_in_probability_allocation(win5_races):
    expected = jra_win5.build_kaime(deepcopy(win5_races), 8, False)
    win5_races[0]["horses"].append(_horse(3, 999.0, scratched=True))

    result = jra_win5.build_kaime(win5_races, 8, False)

    assert result["success"] is True
    assert result["est_hit_rate"] == expected["est_hit_rate"]
    assert result["picks"] == expected["picks"]
