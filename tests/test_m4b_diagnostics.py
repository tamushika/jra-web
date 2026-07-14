from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pytest

import backtest_m4b_diagnostics as m4b
import backtest_ml
from backtest_market_diagnostics import (
    EVALUATION_PERIODS,
    _race_softmax,
    evaluate_same_population,
    prepare_complete_races,
)
from backtest_ml import FEATURES


def _race(date, place="東京", race_no=9):
    key = (date, place, race_no)
    features = []
    labels = []
    keys = []
    meta = []
    priors = {}
    for index in range(8):
        row = np.zeros(len(FEATURES), dtype=float)
        row[FEATURES.index("prev_rank")] = index + 1
        row[FEATURES.index("ln_interval")] = math.log(28 + index)
        features.append(row)
        labels.append(1 if index == 0 else 0)
        keys.append(key)
        runner = {
            "date": date,
            "place": place,
            "r": race_no,
            "horse": f"{date}-H{index}",
            "umaban": index + 1,
            "rank": 1 if index == 0 else index + 1,
            "popularity": index + 1,
            "race_class": "2勝クラス",
            "total_horses": 8,
            "win_pay": "200" if index == 0 else f"({2.0 + index:.1f})",
        }
        meta.append(runner)
        priors[id(runner)] = m4b.PriorRun(
            date=(datetime.strptime(date, "%Y%m%d") - timedelta(days=28)).strftime(
                "%Y%m%d"),
            rank=float(index + 2),
            popularity=float(index + 1),
            race_class="1勝クラス",
        )
    return features, labels, keys, meta, priors


def _combine(*parts):
    features, labels, keys, meta = [], [], [], []
    prior_ids = {}
    for part_features, part_labels, part_keys, part_meta, part_priors in parts:
        features.extend(part_features)
        labels.extend(part_labels)
        keys.extend(part_keys)
        meta.extend(part_meta)
        prior_ids.update(part_priors)
    context = m4b.PriorContext(prior_ids, {})
    return np.asarray(features), np.asarray(labels), keys, meta, context


def _all_period_dataset():
    return _combine(
        _race("20210110"),
        _race("20220110"),
        _race("20230110"),
        _race("20240110"),
        _race("20250110"),
        _race("20260110"),
    )


def test_clip_correction_is_monotone_and_exact_at_boundaries():
    raw = np.array([-0.5, -0.1, -0.099, 0.0, 0.099, 0.1, 0.5])
    clipped = m4b.clip_correction(raw, 0.1)

    np.testing.assert_array_equal(
        clipped, np.array([-0.1, -0.1, -0.099, 0.0, 0.099, 0.1, 0.1]))
    assert np.all(np.diff(clipped) >= 0.0)
    with pytest.raises(ValueError):
        m4b.clip_correction(raw, 0.0)


def test_candidate_features_calculate_all_but_exclude_race_constants():
    features, labels, keys, meta, context = _combine(
        _race("20250110"), _race("20250111"))
    dataset = prepare_complete_races(features, labels, keys, meta)
    m2_scores = np.arange(len(dataset.labels), dtype=float)

    candidates = m4b.build_candidate_features(dataset, m2_scores, context)
    selected, selected_available = candidates.selected()

    assert candidates.names == m4b.ALL_CANDIDATE_FEATURES
    assert selected.shape == (16, 6)
    assert selected_available.all()
    assert set(m4b.EXCLUSION_REASONS) == {"pop_conc", "field_size"}
    for name in m4b.EXCLUDED_RACE_CONSTANT_FEATURES:
        column = candidates.values[:, candidates.names.index(name)]
        assert m4b.is_race_constant(column, dataset.race_keys)

    # Adding either excluded race-constant value to every horse score cancels
    # exactly in the race softmax and cannot alter ranking/probability.
    base_scores = np.linspace(-1.0, 1.0, len(dataset.labels))
    constants = (
        candidates.values[:, candidates.names.index("pop_conc")]
        + candidates.values[:, candidates.names.index("field_size")]
    )
    np.testing.assert_allclose(
        _race_softmax(base_scores, dataset.race_keys),
        _race_softmax(base_scores + constants, dataset.race_keys),
        rtol=0.0,
        atol=1e-15,
    )


def test_m2_gap_uses_official_popularity_when_displayed_odds_tie():
    features, labels, keys, meta, context = _combine(_race("20250110"))
    # Displayed odds tie, but JRA's official popularity is deliberately
    # reversed.  M2 matches that official order for these two runners.
    meta[0]["win_pay"] = "(2.0)"
    meta[1]["win_pay"] = "(2.0)"
    meta[0]["popularity"] = 2
    meta[1]["popularity"] = 1
    dataset = prepare_complete_races(features, labels, keys, meta)
    m2_scores = np.arange(8, 0, -1, dtype=float)
    m2_scores[0], m2_scores[1] = 7.0, 8.0

    candidates = m4b.build_candidate_features(dataset, m2_scores, context)
    gap = candidates.values[:, candidates.names.index("m2_gap")]

    assert gap[1] == 0.0
    assert gap[0] == 0.0


def test_m2_training_scores_are_race_group_oof(monkeypatch):
    features, labels, keys, meta, _context = _combine(
        _race("20210110"), _race("20210111"), _race("20210112"),
        _race("20210113"), _race("20210114"), _race("20210115"),
        _race("20240110"),
    )
    dataset = prepare_complete_races(features, labels, keys, meta)
    fit_calls = []

    def fake_fit(matrix, outcomes, race_keys, **_kwargs):
        fit_calls.append(set(race_keys))
        return np.zeros(matrix.shape[1])

    monkeypatch.setattr(m4b, "fit_conditional_logit", fake_fit)
    scores, report = m4b.fit_m2_oof_scores(dataset, n_splits=3)

    assert np.isfinite(scores).all()
    assert report["oof_assignment_min"] == 1
    assert report["oof_assignment_max"] == 1
    assert len(report["oof_folds"]) == 3
    assert all(row["race_overlap"] == 0 for row in report["oof_folds"])
    assert len(fit_calls) == 4  # three fold fits plus final 2021-23 fit
    assert all(len(keys_used) == 4 for keys_used in fit_calls[:3])
    assert len(fit_calls[-1]) == 6


def test_lambda_cap_tie_prefers_stronger_lambda_then_smaller_cap():
    selected = m4b.select_lambda_cap([
        {"lambda": 1.0, "cap": 0.1, "tune_logloss": 0.5},
        {"lambda": 100.0, "cap": 0.4, "tune_logloss": 0.5 + 5e-13},
        {"lambda": 100.0, "cap": 0.1, "tune_logloss": 0.5 + 4e-13},
        {"lambda": 10.0, "cap": 0.2, "tune_logloss": 0.6},
    ])
    assert selected["lambda"] == 100.0
    assert selected["cap"] == 0.1


def _market_only_m3_report(features, labels, keys, meta):
    complete = prepare_complete_races(features, labels, keys, meta)
    periods = {}
    for name, period in EVALUATION_PERIODS.items():
        dataset = complete.subset(*period)
        result = evaluate_same_population(
            dataset, {
                "current_cl": dataset.market_offsets,
                "m3": dataset.market_offsets,
            })
        result["m3_floor"] = {
            "passed": True, "coverage_delta": [0.0] * 4, "failed_k": []}
        periods[name] = result
    return {
        "regularization": {"selected_lambda": 100.0},
        "periods": periods,
    }


def test_full_evaluation_reuses_m3_and_asserts_same_sample(monkeypatch):
    features, labels, keys, meta, context = _all_period_dataset()
    complete = prepare_complete_races(features, labels, keys, meta)
    m3_report = _market_only_m3_report(features, labels, keys, meta)

    monkeypatch.setattr(
        m4b, "fit_m2_oof_scores",
        lambda *_args, **_kwargs: (
            np.zeros(len(complete.labels)),
            {"features": list(m4b.NO_MARKET_FEATURES), "oof_folds": []},
        ),
    )
    fit_calls = []

    def fake_fit(matrix, _labels, fit_keys, **kwargs):
        fit_calls.append({
            "rows": len(matrix),
            "races": len(set(fit_keys)),
            "l2": kwargs.get("l2"),
        })
        return np.zeros(matrix.shape[1])

    monkeypatch.setattr(m4b, "fit_conditional_logit", fake_fit)
    report = m4b.evaluate_m4b_dataset(
        features, labels, keys, meta,
        prior_context=context,
        m3_report=m3_report,
    )

    assert report["selection"]["selected_lambda"] == 100.0
    assert report["selection"]["selected_cap"] == 0.1
    assert report["selection"]["train_races"] == 3
    assert report["selection"]["selected_internal_l2"] == 300.0
    assert report["selection"]["selected_residual_norm"] == 0.0
    assert report["selection"]["final_fit_rows"] == 32
    assert report["selection"]["final_fit_races"] == 4
    assert report["selection"]["final_internal_l2"] == 400.0
    assert report["selection"]["final_residual_norm"] == 0.0
    assert fit_calls[-1] == {"rows": 32, "races": 4, "l2": 400.0}
    assert report["m3_reused"]["sample_signature_asserted"]
    assert report["candidate_features"]["selected"] == list(
        m4b.CORRECTION_FEATURES)
    assert len(report["coefficients"]) == 6
    assert all("final_scale" in row and "train_scale" not in row
               for row in report["coefficients"])
    for period in report["periods"].values():
        assert list(period["models"]) == ["m0", "current_cl", "m3", "m4b"]
        assert period["m4b_floor"]["passed"]
        assert period["swapped_pair_outcomes"] == {
            "improved": 0, "worsened": 0, "neutral": 0, "total": 0}


def test_run_uses_t33_ability_builder_and_compacts_prior_context(monkeypatch):
    features, labels, keys, meta, _context = _all_period_dataset()
    prior_rows = []
    for runner in meta:
        prior_rows.append({
            "date": str(int(runner["date"]) - 100),
            "horse": runner["horse"],
            "place": runner["place"],
            "r": 8,
            "umaban": runner["umaban"],
            "rank": 2,
            "popularity": 1,
            "race_class": "1勝クラス",
        })
    runs = prior_rows + meta
    builder_calls = []
    evaluation_calls = []

    def builder(actual_runs, cfg, date_to, **kwargs):
        builder_calls.append((actual_runs, cfg, date_to, kwargs))
        return features, labels, keys, meta

    def fake_evaluate(*args, **kwargs):
        evaluation_calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(m4b, "evaluate_m4b_dataset", fake_evaluate)
    result = m4b.run_m4b_diagnostic(
        runs, {"cfg": 1}, db_path="ability-test.db", dataset_builder=builder)

    assert result == {"ok": True}
    assert builder_calls[0][2:] == (
        "20260630", {"stats_source": "ability", "db_path": "ability-test.db"})
    assert evaluation_calls[0][0] == (features, labels, keys, meta)
    assert isinstance(evaluation_calls[0][1]["prior_context"], m4b.PriorContext)


def test_module_parser_rejects_write():
    with pytest.raises(SystemExit) as exc_info:
        m4b.parse_args(["--write"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("argv", [
    ["--m4b", "--write"],
    ["--m4b", "--m3"],
    ["--m4b", "--relative"],
    ["--m4b", "--stats-snapshot"],
    ["--m4b", "--no-market"],
    ["--m4b", "--lgbm"],
])
def test_backtest_ml_m4b_rejects_write_and_ambiguous_modes(argv):
    with pytest.raises(SystemExit) as exc_info:
        backtest_ml.parse_cli_args(argv)
    assert exc_info.value.code == 2


def test_backtest_ml_m4b_delegates_sanitized_argv_through_child_parser(
        monkeypatch):
    calls = []

    def parser_reaching_main(argv=None):
        parsed = m4b.parse_args(argv)
        calls.append((argv, parsed.db))

    monkeypatch.setattr("sys.argv", ["backtest_ml.py", "--m4b"])
    monkeypatch.setattr(m4b, "main", parser_reaching_main)
    backtest_ml.main()

    assert calls == [([], m4b.DB_PATH)]
