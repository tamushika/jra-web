"""SPEC-T80b (post-waku-fix retrain gate) acceptance tests.

Corresponding spec: docs/codex/SPEC-T80b-waku-fix-retrain.md §2-7
  - legacy/fixed の枠が異なる行が存在すること
  - 両実装で market baseline が一致すること (母集団同一)
  - ゲート判定関数の境界
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

import backtest_t80b_waku_retrain as subject
from backtest_fold_stats import build_feature_dataset, same_population_metrics
from backtest_win5 import load_win5_cfg


# ─── Pure calculate_waku comparison (no DB / feature pipeline needed) ──────

def test_legacy_waku_differs_from_fixed_for_16_runners():
    """16 runners: JRA rule gives [2]*8, the pre-T80 bug gives [1,1,1,1,3,3,3,3]."""
    head_count = 16
    fixed = [subject._FIXED_CALCULATE_WAKU(u, head_count) for u in range(1, head_count + 1)]
    legacy = [subject._legacy_calculate_waku(u, head_count) for u in range(1, head_count + 1)]
    assert fixed != legacy
    mismatches = sum(1 for a, b in zip(fixed, legacy) if a != b)
    assert mismatches > 0
    assert fixed == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]
    assert legacy == [1, 2, 3, 4, 5, 5, 5, 6, 6, 6, 7, 7, 7, 8, 8, 8]


@pytest.mark.parametrize("head_count", [1, 5, 8])
def test_legacy_and_fixed_agree_for_small_fields(head_count):
    for umaban in range(1, head_count + 1):
        assert subject._legacy_calculate_waku(umaban, head_count) == umaban
        assert subject._FIXED_CALCULATE_WAKU(umaban, head_count) == umaban


def test_waku_impl_context_manager_restores_original():
    original = subject.backtest_ml.calculate_waku
    with subject._waku_impl("legacy"):
        assert subject.backtest_ml.calculate_waku is subject._legacy_calculate_waku
    assert subject.backtest_ml.calculate_waku is original
    with subject._waku_impl("fixed"):
        assert subject.backtest_ml.calculate_waku is subject._FIXED_CALCULATE_WAKU
    assert subject.backtest_ml.calculate_waku is original


def test_waku_impl_rejects_unknown_kind():
    with pytest.raises(ValueError):
        with subject._waku_impl("other"):
            pass


# ─── Synthetic feature-pipeline integration (small, no ability.db) ────────

def _run(horse, date, place, r, total_horses, umaban, rank, popularity):
    win_pay = "250" if rank == 1 else f"({2.0 + umaban:.1f})"
    return {
        "date": date, "place": place, "r": r, "race_name": "",
        "race_class": "1勝クラス", "horse": horse, "total_horses": total_horses,
        "popularity": popularity, "rank": rank, "track_type": "芝",
        "distance": 1600, "condition": "良", "time_sec": None, "agari": None,
        "win_pay": win_pay, "jockey": "J", "umaban": umaban, "pci": None,
        "affi": "美浦", "c4": None, "sex": "牡", "age": 4, "kinryo": 55.0,
        "weight": 470, "agari_ratio": None,
    }


def _synthetic_runs():
    """16 horses with one prior 8-runner race and one 16-runner target race.

    The target race (2024-03-01, 16 runners) is where legacy and fixed waku
    disagree (SPEC-T80); the prior race gives every horse the one required
    history row so build_dataset does not skip it as a debut.
    """
    runs = []
    for i in range(1, 17):
        prior_slot = ((i - 1) % 8) + 1
        runs.append(_run(f"H{i}", "20231201", "東京", 1, 8, prior_slot,
                         prior_slot, prior_slot))
        runs.append(_run(f"H{i}", "20240301", "東京", 2, 16, i, i, i))
    return runs


@pytest.fixture(scope="module")
def synthetic_dataset():
    runs = _synthetic_runs()
    cfg = load_win5_cfg()
    legacy_runs = copy.deepcopy(runs)
    fixed_runs = copy.deepcopy(runs)
    with subject._waku_impl("legacy"):
        legacy = build_feature_dataset(legacy_runs, cfg, "20241231", None)
    with subject._waku_impl("fixed"):
        fixed = build_feature_dataset(fixed_runs, cfg, "20241231", None)
    return legacy, fixed


def test_legacy_and_fixed_feature_rows_have_same_population(synthetic_dataset):
    legacy, fixed = synthetic_dataset
    _, legacy_labels, legacy_keys, _ = legacy
    _, fixed_labels, fixed_keys, _ = fixed
    assert legacy_keys == fixed_keys
    assert np.array_equal(legacy_labels, fixed_labels)


def test_legacy_and_fixed_differ_on_f_pts_for_some_rows(synthetic_dataset):
    """The 16-runner target race must produce at least one differing f_pts."""
    legacy, fixed = synthetic_dataset
    legacy_features, _, keys, _ = legacy
    fixed_features, _, _, _ = fixed
    f_idx = subject.FEATURES.index("f_pts")
    target_mask = np.array([key[:2] == ("20240301", "東京") for key in keys])
    assert target_mask.any(), "target race rows must be present in the feature dataset"
    differing = np.sum(
        legacy_features[target_mask, f_idx] != fixed_features[target_mask, f_idx])
    assert differing > 0


def test_market_baseline_matches_between_legacy_and_fixed(synthetic_dataset):
    """Market baseline only depends on labels/odds/popularity, never on waku."""
    legacy, fixed = synthetic_dataset
    legacy_features, legacy_labels, legacy_keys, legacy_meta = legacy
    fixed_features, fixed_labels, fixed_keys, fixed_meta = fixed

    legacy_scores = legacy_features @ np.ones(legacy_features.shape[1])
    fixed_scores = fixed_features @ np.ones(fixed_features.shape[1])

    legacy_result = same_population_metrics(
        legacy_scores, legacy_labels, legacy_keys, legacy_meta,
        min_race_no=1, min_horses=1)
    fixed_result = same_population_metrics(
        fixed_scores, fixed_labels, fixed_keys, fixed_meta,
        min_race_no=1, min_horses=1)

    assert legacy_result["sample_signature"] == fixed_result["sample_signature"]
    assert legacy_result["models"]["market"] == fixed_result["models"]["market"]


# ─── Gate / stop-rule boundary tests (pure functions on synthetic results) ─

def _comparison(current_ll, candidate_ll, market_ll, current_topk, candidate_topk,
                market_topk, races=100):
    return {
        "races": races, "horses": races * 10,
        "current": {"logloss": current_ll, "coverage": current_topk, "brier": 0.1},
        "candidate": {"logloss": candidate_ll, "coverage": candidate_topk, "brier": 0.1},
        "market": {"logloss": market_ll, "coverage": market_topk, "brier": 0.1},
        "skipped": {},
    }


def _bootstrap(observed_difference, ci_low, ci_high):
    return {
        "observed_difference": observed_difference, "differences": (),
        "ci_low": ci_low, "ci_high": ci_high, "p_value": 0.5,
        "confidence_level": 0.95, "n_resamples": 100, "n_blocks": 10, "seed": 1,
    }


def _origin(all_races_diff=0.0, win5_diff=0.0, all_races_ci=(-0.01, 0.01),
           win5_ci=(-0.01, 0.01), topk_delta_pt=0.0, production_diff=0.0):
    legacy_topk = [0.30, 0.48, 0.60, 0.70]
    candidate_topk = [
        legacy_topk[0] + topk_delta_pt / 100.0,
        legacy_topk[1] + topk_delta_pt / 100.0,
        legacy_topk[2] + topk_delta_pt / 100.0,
        legacy_topk[3],
    ]
    base_ll = 2.0
    all_races = {
        "legacy_vs_fixed": _comparison(
            base_ll, base_ll + all_races_diff, base_ll + 0.05,
            legacy_topk, candidate_topk, legacy_topk),
        "production_vs_fixed": _comparison(
            base_ll + production_diff, base_ll, base_ll + 0.05,
            legacy_topk, candidate_topk, legacy_topk),
        "bootstrap": _bootstrap(all_races_diff, *all_races_ci),
    }
    win5 = {
        "legacy_vs_fixed": _comparison(
            base_ll, base_ll + win5_diff, base_ll + 0.05,
            legacy_topk, candidate_topk, legacy_topk),
        "production_vs_fixed": None,
        "bootstrap": _bootstrap(win5_diff, *win5_ci),
    }
    return {
        "eval_from": "20250101", "eval_to": "20251231", "role": "test",
        "populations": {"all_races": all_races, "win5": win5},
    }


def _results(**overrides):
    b = _origin(**overrides.get("b", {}))
    c = _origin(**overrides.get("c", {}))
    return {"A_2024": _origin(), "B_2025": b, "C_2026H1": c}


def test_gate_pass_all_conditions_hold():
    results = _results()
    passed, detail = subject.gate_pass(results)
    assert passed is True
    assert detail["a_ll_all_races"]["pass"] is True
    assert detail["b_topk_floor_win5"]["pass"] is True
    assert detail["c_win5_sign_consistent"]["pass"] is True
    assert detail["d_production_2026h1"]["pass"] is True


def test_gate_a_boundary_exactly_at_tolerance_passes():
    results = _results(b={"all_races_diff": subject.GATE_LL_TOLERANCE})
    passed, detail = subject.gate_pass(results)
    assert detail["a_ll_all_races"]["checks"]["2025"]["pass"] is True
    assert passed is True


def test_gate_a_boundary_just_over_tolerance_fails():
    results = _results(b={"all_races_diff": subject.GATE_LL_TOLERANCE + 1e-9})
    passed, detail = subject.gate_pass(results)
    assert detail["a_ll_all_races"]["checks"]["2025"]["pass"] is False
    assert passed is False


def test_gate_b_boundary_exactly_at_floor_passes():
    results = _results(b={"topk_delta_pt": subject.GATE_TOPK_FLOOR_PT})
    passed, detail = subject.gate_pass(results)
    assert detail["b_topk_floor_win5"]["checks"]["2025"]["pass"] is True
    assert passed is True


def test_gate_b_boundary_just_below_floor_fails():
    results = _results(b={"topk_delta_pt": subject.GATE_TOPK_FLOOR_PT - 0.01})
    passed, detail = subject.gate_pass(results)
    assert detail["b_topk_floor_win5"]["checks"]["2025"]["pass"] is False
    assert passed is False


def test_gate_c_fails_on_sign_mismatch():
    results = _results(
        b={"win5_diff": 0.01, "win5_ci": (0.005, 0.02)},
        c={"win5_diff": -0.01, "win5_ci": (-0.02, -0.005)},
    )
    passed, detail = subject.gate_pass(results)
    assert detail["c_win5_sign_consistent"]["pass"] is False
    assert passed is False


def test_gate_c_fails_when_both_periods_worsen():
    results = _results(
        b={"win5_diff": 0.01, "win5_ci": (0.005, 0.02)},
        c={"win5_diff": 0.01, "win5_ci": (0.005, 0.02)},
    )
    passed, detail = subject.gate_pass(results)
    assert detail["c_win5_sign_consistent"]["pass"] is False
    assert passed is False


def test_gate_d_boundary_exactly_at_tolerance_passes():
    results = _results(c={"production_diff": -subject.GATE_LL_TOLERANCE})
    passed, detail = subject.gate_pass(results)
    assert detail["d_production_2026h1"]["pass"] is True
    assert passed is True


def test_gate_d_boundary_just_over_tolerance_fails():
    results = _results(c={"production_diff": -subject.GATE_LL_TOLERANCE - 1e-9})
    passed, detail = subject.gate_pass(results)
    assert detail["d_production_2026h1"]["pass"] is False
    assert passed is False


def test_gate_d_missing_production_model_fails():
    results = _results()
    results["C_2026H1"]["populations"]["all_races"]["production_vs_fixed"] = None
    passed, detail = subject.gate_pass(results)
    assert detail["d_production_2026h1"]["pass"] is False
    assert passed is False


def test_stop_rule_triggers_on_material_significant_regression():
    origin_all_races = {
        "bootstrap": _bootstrap(
            subject.STOP_RULE_POINT_THRESHOLD + 0.001, 0.0005, 0.003)
    }
    assert subject.stop_rule_would_trigger(origin_all_races) is True


def test_stop_rule_does_not_trigger_when_ci_excludes_negative():
    """If the CI upper bound is < 0, the fix is (significantly) better, not worse."""
    origin_all_races = {
        "bootstrap": _bootstrap(-0.01, -0.02, -0.001)
    }
    assert subject.stop_rule_would_trigger(origin_all_races) is False


def test_stop_rule_does_not_trigger_below_point_threshold():
    origin_all_races = {
        "bootstrap": _bootstrap(
            subject.STOP_RULE_POINT_THRESHOLD - 1e-6, 0.0001, 0.002)
    }
    assert subject.stop_rule_would_trigger(origin_all_races) is False


def test_stop_rule_returns_none_without_bootstrap():
    assert subject.stop_rule_would_trigger({"bootstrap": None}) is None
