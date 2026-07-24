import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import backtest_t62_race_selection as t62


def _registration_row(digest):
    return {
        "experiment_id": t62.EXPERIMENT_ID,
        "registered_at_utc": "2026-07-24T00:00:00+00:00",
        "commit_sha": "test-commit",
        "data_hashes": {
            "ability_db_sha256": digest,
            "t62_contract_sha256": t62.canonical_sha256(
                t62.registration_contract()
            ),
            "harness_sha256": t62.sha256_file(Path(t62.__file__)),
        },
        "features": t62.registration_features(),
        "primary_metric": t62.PRIMARY_METRIC,
        "safety_metrics": list(t62.SAFETY_METRICS),
        "search_grid": t62.registration_search_grid(),
        "candidate_count": 3,
        "stop_rule": t62.STOP_RULE,
        "benchmark_type": "historical",
        "prospective_start_date": None,
        "result_summary": None,
        "adjudication": None,
        "superseded_by": None,
    }


def test_race_features_are_result_blind_and_have_fixed_11_columns():
    model = [0.5, 0.3, 0.2]
    market = [0.4, 0.35, 0.25]
    odds = [2.5, 3.0, 4.0]
    first = t62.compute_race_features(
        model, market, odds, [1, 0, 1], race_class="1勝クラス"
    )
    second = t62.compute_race_features(
        model, market, odds, [1, 0, 1], race_class="1勝クラス"
    )
    assert len(first) == len(t62.META_FEATURES) == 11
    assert np.array_equal(first, second)
    assert "rank" not in t62.compute_race_features.__code__.co_varnames
    assert "winner" not in t62.compute_race_features.__code__.co_varnames


def test_history_availability_excludes_target_date_atomically():
    history = {
        "H": (
            ["20240101", "20240201", "20240301", "20240301"],
            [True, True, False, True],
        )
    }
    assert t62.history_available(history, "H", "20240301") == 1.0
    assert t62.history_available(history, "H", "20240201") == 0.0
    interrupted = {
        "H": (
            ["20240101", "20240201", "20240301", "20240401", "20240501"],
            [True, True, False, False, False],
        )
    }
    assert t62.history_available(interrupted, "H", "20240601") == 1.0


def test_rolling_origin_boundaries_never_train_on_target_or_future():
    contract = t62.rolling_origin_contract()
    t62.validate_rolling_origin_contract(contract)
    assert [(row["train_to"], row["target_from"]) for row in contract] == [
        ("20211231", "20220101"),
        ("20221231", "20230101"),
        ("20231231", "20240101"),
        ("20241231", "20250101"),
    ]
    broken = [dict(row) for row in contract]
    broken[1]["train_to"] = "20231231"
    with pytest.raises(ValueError, match="crosses target"):
        t62.validate_rolling_origin_contract(broken)


def test_standard_times_use_only_winners_through_cutoff():
    rows = [
        {
            "date": f"20200{index + 1}01", "place": "東京", "rank": 1,
            "track_type": "芝", "distance": 1600, "race_class": "1勝クラス",
            "condition": "良", "time_sec": value,
        }
        for index, value in enumerate((94.0, 95.0, 96.0))
    ]
    rows.extend([
        {**rows[0], "date": "20210101", "time_sec": 80.0},
        {**rows[0], "date": "20200601", "rank": 2, "time_sec": 70.0},
    ])
    before = t62.build_standard_times_asof(rows, "20201231")
    after = t62.build_standard_times_asof(rows, "20211231")
    assert before["東京"]["芝"]["1600"]["1勝クラス"]["良"] == 95.0
    assert after["東京"]["芝"]["1600"]["1勝クラス"]["良"] == 94.5


def test_track_variants_use_only_supplied_asof_standards_and_dates():
    standards = {
        "東京": {"芝": {"1600": {"1勝クラス": {"良": 100.0}}}}
    }
    rows = [
        {
            "date": "20210101",
            "place": "東京",
            "rank": 1,
            "track_type": "芝",
            "distance": 1600,
            "race_class": "1勝クラス",
            "condition": "良",
            "time_sec": value,
        }
        for value in (101.0, 102.0, 103.0)
    ]
    rows.extend({**rows[0], "date": "20220101", "time_sec": value}
                for value in (90.0, 91.0, 92.0))
    variants = t62.build_track_variants_asof(
        rows, standards, "20211231"
    )
    assert variants == {"20210101": {"東京": {"芝": 2.0}}}
    shifted = {
        "東京": {"芝": {"1600": {"1勝クラス": {"良": 101.0}}}}
    }
    assert t62.build_track_variants_asof(
        rows, shifted, "20211231"
    )["20210101"]["東京"]["芝"] == 1.0


def test_winner_log_ratio_and_dead_heat_average():
    model = [0.6, 0.3, 0.1]
    market = [0.5, 0.2, 0.3]
    expected = (
        np.log(model[0] / market[0]) + np.log(model[1] / market[1])
    ) / 2.0
    assert t62.winner_log_ratio(model, market, [0, 1]) == pytest.approx(expected)
    model_loss = np.mean([-np.log(model[0]), -np.log(model[1])])
    market_loss = np.mean([-np.log(market[0]), -np.log(market[1])])
    assert model_loss - market_loss == pytest.approx(-expected)


def test_registration_gate_fails_closed(tmp_path, monkeypatch):
    db = tmp_path / "ability.db"
    db.write_bytes(b"sealed")
    ledger = tmp_path / "experiments.jsonl"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setattr(t62, "ABILITY_DB_SHA256", t62.sha256_file(db))
    with pytest.raises(RuntimeError, match="register T62-race-relative-confidence-v1"):
        t62.require_registration(ledger, db)


def test_registration_must_fix_three_candidates_and_20_percent(tmp_path, monkeypatch):
    db = tmp_path / "ability.db"
    db.write_bytes(b"sealed")
    digest = t62.sha256_file(db)
    monkeypatch.setattr(t62, "ABILITY_DB_SHA256", digest)
    ledger = tmp_path / "experiments.jsonl"
    row = _registration_row(digest)
    row["search_grid"]["primary_selection_rate"] = 0.3
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fixed T62"):
        t62.require_registration(ledger, db)
    row["search_grid"]["primary_selection_rate"] = 0.2
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert t62.require_registration(ledger, db) == row
    row["primary_metric"] = "wrong"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fixed T62"):
        t62.require_registration(ledger, db)


def test_selection_threshold_selects_exact_top_20_percent():
    scores = np.arange(10.0)
    threshold = t62.selection_threshold(scores, 0.2)
    assert int(np.sum(scores >= threshold)) == 2
    assert set(scores[scores >= threshold]) == {8.0, 9.0}
    with pytest.raises(RuntimeError, match="boundary tie"):
        t62.selection_threshold(np.asarray([3.0, 2.0, 2.0, 1.0]), 0.5)


def test_freeze_manifest_sha_and_scores_are_reproducible():
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    matrix = np.arange(55.0).reshape(5, 11)
    target = np.arange(5.0)
    scaler = StandardScaler().fit(matrix)
    model = Ridge(alpha=1.0).fit(scaler.transform(matrix), target)
    manifest = t62.freeze_manifest(model, scaler, 0.25, 1.0)
    payload = {key: value for key, value in manifest.items()
               if key != "manifest_sha256"}
    assert manifest["manifest_sha256"] == t62.canonical_sha256(payload)
    observations = [{"features": row} for row in matrix]
    assert t62.score_frozen(manifest, observations) == pytest.approx(
        model.predict(scaler.transform(matrix))
    )


def test_popularity_bands_report_selected_and_unselected_families():
    rows = [
        {
            "key": ("20240101", "東京", 9),
            "model_loss": 1.0, "market_loss": 1.2,
            "model_order": (0, 1), "market_order": (1, 0),
            "winners": (0,), "winner_popularity": 2,
        },
        {
            "key": ("20240102", "東京", 9),
            "model_loss": 1.3, "market_loss": 1.0,
            "model_order": (0, 1), "market_order": (1, 0),
            "winners": (1,), "winner_popularity": 10,
        },
    ]
    report = t62.subset_report(rows, 1)
    bands = report["winner_popularity_bands"]["bands"]
    assert bands["1-3"]["races"] == 1
    assert bands["4-8"]["races"] == 0
    assert bands["4-8"]["model_logloss"] is None
    assert bands["9+"]["races"] == 1
    assert report["difference"] == pytest.approx(0.05)
    assert (
        report["winner_popularity_bands"]["weighted_mean_model_minus_market"]
        == pytest.approx(report["difference"])
    )


def test_base_prediction_hash_excludes_results_but_outcome_hash_does_not():
    row = {
        "key": ("20240101", "東京", 9),
        "features": np.arange(11.0),
        "model_probabilities": (0.6, 0.4),
        "market_probabilities": (0.5, 0.5),
        "y": 0.1,
        "model_loss": 0.5,
        "market_loss": 0.7,
    }
    changed = {
        **row,
        "y": -0.2,
        "model_loss": 1.2,
        "market_loss": 0.8,
    }
    assert t62.base_prediction_sha256([row]) == t62.base_prediction_sha256(
        [changed]
    )
    assert t62.observation_sha256([row]) != t62.observation_sha256([changed])


def test_one_sided_bootstrap_uses_nonnegative_tail_and_add_one(monkeypatch):
    fake = SimpleNamespace(
        observed_difference=-0.2,
        differences=(-0.3, -0.1, 0.1, -0.2),
        ci_low=-0.4,
        ci_high=0.1,
        p_value=0.4,
        n_resamples=4,
        n_blocks=1,
        seed=62,
    )
    monkeypatch.setattr(t62, "paired_block_bootstrap", lambda *args: fake)
    row = {
        "key": ("20240101", "東京", 9),
        "model_loss": 1.0,
        "market_loss": 1.2,
    }
    assert t62._bootstrap([row], 62)["p_one_sided_model_better"] == 0.4


def test_exact_candidate_tie_prefers_stronger_l2():
    candidates = [
        {
            "ridge_l2": l2,
            "selection_2024": {"selected": {"difference": -0.1}},
        }
        for l2 in (0.3, 1.0, 3.0)
    ]
    assert t62.choose_candidate(candidates)["ridge_l2"] == 3.0


def test_flat_population_and_horse_number_tie_break_are_explicit():
    order = [8, 2, 6, 1, 4, 7, 3, 5]
    meta = []
    labels = []
    for number in order:
        winner = number == 1
        meta.append({
            "date": "20240101",
            "place": "東京",
            "r": 9,
            "horse": f"H{number}",
            "umaban": number,
            "total_horses": 8,
            "track_type": "芝",
            "race_class": "1勝クラス",
            "rank": 1 if winner else 2,
            "win_pay": "250" if winner else "(3.0)",
            "popularity": number,
        })
        labels.append(int(winner))
    keys = [("20240101", "東京", 9)] * 8
    observations = t62.build_race_observations(
        np.zeros(8), np.asarray(labels), keys, meta, 1.0, {}
    )
    assert len(observations) == 1
    assert observations[0]["model_order"] == tuple(range(8))
    obstacle = [{**row, "track_type": "障害"} for row in meta]
    assert t62.build_race_observations(
        np.zeros(8), np.asarray(labels), keys, obstacle, 1.0, {}
    ) == []
    invalid_odds = [dict(row) for row in meta]
    invalid_odds[0]["win_pay"] = "(nan)"
    invalid_odds[0]["rank"] = 2
    invalid_odds[3]["rank"] = 1
    invalid_odds[3]["win_pay"] = "250"
    invalid_labels = np.asarray([
        int(row["rank"] == 1) for row in invalid_odds
    ])
    assert t62.build_race_observations(
        np.zeros(8), invalid_labels, keys, invalid_odds, 1.0, {}
    ) == []


def test_future_diagnostic_curves_use_frozen_2024_thresholds():
    rows = [
        {
            "key": (f"2025010{index + 1}", "東京", 9),
            "model_loss": 1.0 + index / 10,
            "market_loss": 1.2,
            "model_order": (0,),
            "market_order": (0,),
            "winners": (0,),
            "winner_popularity": 1,
        }
        for index in range(4)
    ]
    report = t62._period_report(
        rows,
        np.asarray([0.9, 0.8, 0.7, 0.6]),
        0.75,
        62,
        diagnostic_thresholds={
            "0.1": 0.95,
            "0.15": 0.90,
            "0.2": 0.85,
            "0.25": 0.75,
            "0.3": 0.65,
        },
    )
    assert report["selected"]["races"] + report["unselected"]["races"] == 4
    assert report["selection_rate_curve"]["0.1"]["races"] == 0
    assert (
        report["selection_rate_curve"]["0.3"]["threshold_source"]
        == "2024_selection_frozen"
    )
    assert not t62._selected_difference_is_negative(
        {"selected": {"difference": None}}
    )
