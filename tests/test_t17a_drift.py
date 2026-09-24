"""SPEC-T17a v3 Stage A required tests (SS5) for backtest_t17a_drift.py.

Every test here uses synthetic or in-memory data only. None of them open the
real data/jra_logging.db, fit anything on real rows, write to
eval/experiments.jsonl, or touch the network. See
test_read_only_connect_rejects_writes and test_run_gate_refuses_without_ledger_entry
for the explicit fail-closed checks.

Mapping to SPEC SS5's required test list is in the module docstring of each
section below and is summarised in the Stage A report
(docs/T17a-drift-report.md).
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import backtest_t17a_drift as m


def _mk_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    m.create_synthetic_schema(connection)
    return connection


# ---------------------------------------------------------------------------
# SS5 (1)/(2): drift uninformative -> beta1~=0, ddLL~=0; informative -> ddLL<0
# ---------------------------------------------------------------------------


def test_uninformative_drift_gives_near_zero_beta_and_delta():
    rows = m.generate_synthetic_race_rows(101, 1200, gamma=0.0, kappa=1.0)
    train, validation = m.split_train_validation_by_event_date(rows)
    control = m.FitResult(params=(), feature_names=(), converged=True, message="control")
    fit_b = m.fit_conditional_logit(train, ["drift"])
    assert fit_b.converged
    delta = m.race_mean_log_loss(validation, fit_b) - m.race_mean_log_loss(validation, control)
    # "Near zero" is judged relative to the informative scenario's effect
    # size (beta1~=1.6, delta~=-0.7 in the analogous test below), not an
    # absolute numerical tolerance: finite synthetic samples always leave
    # some sampling noise.
    assert abs(fit_b.params[0]) < 0.08
    assert abs(delta) < 0.01


def test_informative_drift_gives_negative_delta_log_loss():
    rows = m.generate_synthetic_race_rows(101, 400, gamma=1.5, kappa=1.0)
    train, validation = m.split_train_validation_by_event_date(rows)
    control = m.FitResult(params=(), feature_names=(), converged=True, message="control")
    fit_b = m.fit_conditional_logit(train, ["drift"])
    assert fit_b.converged
    delta = m.race_mean_log_loss(validation, fit_b) - m.race_mean_log_loss(validation, control)
    assert delta < 0.0


# ---------------------------------------------------------------------------
# SS5 (3): exclusion of post-race capture / out-of-window / cancellations
# ---------------------------------------------------------------------------


def test_post_race_capture_is_excluded():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 1, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(1)
    m.build_clean_synthetic_race(connection, rng, "20270201:合成:01", "20270201", "合成", 10, scheduled)
    # Overwrite the stage-2 capture with a post-race (negative seconds_to_post) one.
    connection.execute("DELETE FROM odds_snapshots WHERE race_id=? AND stage='2'", ("20270201:合成:01",))
    horse_odds = {f"20270201:合成:01:{i:02d}": 3.0 + i for i in range(1, 11)}
    m.insert_clean_capture(connection, "20270201:合成:01", 2, scheduled, horse_odds, seconds_offset=-15.0)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("outside_window") == 1


def test_out_of_window_capture_is_excluded():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 2, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(2)
    m.build_clean_synthetic_race(connection, rng, "20270202:合成:01", "20270202", "合成", 9, scheduled)
    connection.execute("DELETE FROM odds_snapshots WHERE race_id=? AND stage='10'", ("20270202:合成:01",))
    horse_odds = {f"20270202:合成:01:{i:02d}": 3.0 + i for i in range(1, 10)}
    # 10-minute window is [540, 720] seconds; 1000s is well outside it.
    m.insert_clean_capture(connection, "20270202:合成:01", 10, scheduled, horse_odds, seconds_offset=1000.0)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("outside_window") == 1


def test_scratch_after_30min_excludes_race_via_result_mismatch():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 3, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(3)
    horse_ids, winner_id = m.build_clean_synthetic_race(
        connection, rng, "20270203:合成:01", "20270203", "合成", 10, scheduled
    )
    # Simulate a post-30-minute scratch: remove one horse from the results
    # table entirely (result row count now n-1, finish positions no longer
    # {1..n}).
    connection.execute(
        "DELETE FROM race_results WHERE race_id=? AND horse_id=?",
        ("20270203:合成:01", horse_ids[-1]),
    )
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("result_incomplete") == 1


def test_quality_flagged_capture_is_excluded_when_no_clean_alternative():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 4, 6, 0, 0, tzinfo=timezone.utc)
    m.insert_race_row(connection, "20270204:合成:01", "20270204", "合成", "芝")
    horse_odds = {f"20270204:合成:01:{i:02d}": 3.0 + i for i in range(1, 9)}
    m.insert_clean_capture(connection, "20270204:合成:01", 30, scheduled, horse_odds)
    m.insert_clean_capture(connection, "20270204:合成:01", 10, scheduled, horse_odds)
    # The only stage-2 capture is flagged; there is no unflagged alternative.
    m.insert_clean_capture(
        connection, "20270204:合成:01", 2, scheduled, horse_odds, flags=["late_capture"]
    )
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("quality_flagged") == 1


def test_odds_incomplete_capture_is_excluded():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 5, 6, 0, 0, tzinfo=timezone.utc)
    m.insert_race_row(connection, "20270205:合成:01", "20270205", "合成", "芝")
    horse_odds = {f"20270205:合成:01:{i:02d}": 3.0 + i for i in range(1, 9)}
    m.insert_clean_capture(connection, "20270205:合成:01", 30, scheduled, horse_odds)
    m.insert_clean_capture(connection, "20270205:合成:01", 10, scheduled, horse_odds)
    # valid_odds_count (7) != field_size (8): one horse's odds were unusable.
    m.insert_clean_capture(
        connection, "20270205:合成:01", 2, scheduled, horse_odds, valid_odds_count=7
    )
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("odds_incomplete") == 1


def test_headcount_below_8_is_not_applicable_not_a_normal_exclusion():
    connection = _mk_connection()
    scheduled = datetime(2027, 2, 6, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(6)
    m.build_clean_synthetic_race(connection, rng, "20270206:合成:01", "20270206", "合成", 7, scheduled)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["not_applicable_headcount_or_unknown"] == 1
    assert result["applicable_count"] == 0
    assert result["exclusion_primary_reason"] == {}


# ---------------------------------------------------------------------------
# v3 SS3.6-1 boundary tests: headcount applicability is judged strictly from
# the *adopted* capture at each stage, never from a non-adopted attempt, and
# a field_size mismatch across the adopted 30/10/2 captures is its own
# odds_incomplete exclusion, not folded into result_incomplete.
# ---------------------------------------------------------------------------


def test_adopted_headcount_7_with_non_adopted_8_is_not_applicable_no_exception():
    # A non-adopted (out-of-window) stage-2 attempt happens to have 8 horses,
    # but the actually adopted stage-2 capture (and both other stages) has
    # only 7. The 8-horse attempt must never be consulted or cause an
    # exception; the race must be not_applicable via the adopted 7.
    connection = _mk_connection()
    scheduled = datetime(2027, 8, 1, 6, 0, 0, tzinfo=timezone.utc)
    race_id = "20270801:合成:01"
    m.insert_race_row(connection, race_id, "20270801", "合成", "芝")
    horse_odds_7 = {f"{race_id}:{i:02d}": 3.0 + i for i in range(1, 8)}
    horse_odds_8 = {f"{race_id}:{i:02d}": 3.0 + i for i in range(1, 9)}
    m.insert_clean_capture(connection, race_id, 30, scheduled, horse_odds_7)
    m.insert_clean_capture(connection, race_id, 10, scheduled, horse_odds_7)
    m.insert_clean_capture(connection, race_id, 2, scheduled, horse_odds_7)
    # Out-of-window (1000s) stage-2 attempt with a different headcount: must
    # not be selectable and must not affect the applicability judgment.
    m.insert_clean_capture(connection, race_id, 2, scheduled, horse_odds_8, seconds_offset=1000.0)
    finish = {horse_id: rank for rank, horse_id in enumerate(horse_odds_7, start=1)}
    m.insert_result_rows(connection, race_id, finish, scheduled)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["not_applicable_headcount_or_unknown"] == 1
    assert result["applicable_count"] == 0
    assert result["exclusion_primary_reason"] == {}


def test_adopted_headcount_exactly_8_is_applicable_and_eligible():
    connection = _mk_connection()
    scheduled = datetime(2027, 8, 2, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(82)
    m.build_clean_synthetic_race(connection, rng, "20270802:合成:01", "20270802", "合成", 8, scheduled)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["not_applicable_headcount_or_unknown"] == 0
    assert result["applicable_count"] == 1
    assert result["final_eligible_count"] == 1


def test_field_size_mismatch_across_adopted_stages_is_odds_incomplete():
    # Distinct from result_incomplete: the adopted stage-2 capture has a
    # different headcount from the adopted stage-30/10 captures, discovered
    # BEFORE the result/horse-set check runs (SS3.6-1's ordering).
    connection = _mk_connection()
    scheduled = datetime(2027, 8, 3, 6, 0, 0, tzinfo=timezone.utc)
    race_id = "20270803:合成:01"
    m.insert_race_row(connection, race_id, "20270803", "合成", "芝")
    horse_odds_8 = {f"{race_id}:{i:02d}": 3.0 + i for i in range(1, 9)}
    horse_odds_9 = {f"{race_id}:{i:02d}": 3.0 + i for i in range(1, 10)}
    m.insert_clean_capture(connection, race_id, 30, scheduled, horse_odds_8)
    m.insert_clean_capture(connection, race_id, 10, scheduled, horse_odds_8)
    m.insert_clean_capture(connection, race_id, 2, scheduled, horse_odds_9)
    finish = {horse_id: rank for rank, horse_id in enumerate(horse_odds_9, start=1)}
    m.insert_result_rows(connection, race_id, finish, scheduled)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("odds_incomplete") == 1
    assert result["exclusion_primary_reason"].get("result_incomplete") is None
    assert result["exclusion_all_flags"].get("field_size_mismatch_across_stages") == 1


# ---------------------------------------------------------------------------
# SS5 (4): the 4 fixed machine judgments, applied in priority order
# ---------------------------------------------------------------------------

_PASS_KWARGS = dict(
    delta=-0.02,
    ci_low=-0.03,
    ci_high=-0.01,
    topk_diffs={1: 0.01, 2: 0.0, 3: 0.02},
    band_stats={
        "1-3": {"count": 100, "diff": -0.01},
        "4-8": {"count": 80, "diff": -0.02},
        "9+": {"count": 20, "diff": 0.0},
    },
)


def test_judgment_pass_all_conditions_met():
    verdict, reasons = m.machine_judgment(quality_violations=[], **_PASS_KWARGS)
    assert verdict == m.HISTORICAL_SCREEN_PASS


def test_judgment_invalid_takes_priority_over_everything():
    kwargs = dict(_PASS_KWARGS)
    kwargs["delta"] = 0.5  # would otherwise be a clear FAIL
    verdict, reasons = m.machine_judgment(
        quality_violations=["final_eligible_population_below_800"], **kwargs
    )
    assert verdict == m.INVALID
    assert any("final_eligible_population_below_800" in r for r in reasons)


def test_judgment_fail_when_only_topk_condition_fails():
    kwargs = dict(_PASS_KWARGS)
    kwargs["topk_diffs"] = {1: -0.001, 2: 0.0, 3: 0.02}
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.HISTORICAL_SCREEN_FAIL
    assert "topk_diff_negative" in reasons
    assert "delta_not_negative" not in reasons


def test_judgment_fail_when_only_popularity_band_condition_fails():
    kwargs = dict(_PASS_KWARGS)
    kwargs["band_stats"] = {
        "1-3": {"count": 100, "diff": 0.01},
        "4-8": {"count": 80, "diff": 0.02},
        "9+": {"count": 20, "diff": -0.01},
    }
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.HISTORICAL_SCREEN_FAIL
    assert "fewer_than_two_negative_bands_with_all_bands_computable" in reasons


def test_judgment_fail_beats_inconclusive_when_both_conditions_unmet():
    # delta >= 0 (a clear point-estimate FAIL) together with ci_high >= 0
    # (would otherwise be INCONCLUSIVE): FAIL must win.
    kwargs = dict(_PASS_KWARGS)
    kwargs["delta"] = 0.01
    kwargs["ci_high"] = 0.02
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.HISTORICAL_SCREEN_FAIL
    assert "delta_not_negative" in reasons


def test_judgment_inconclusive_when_ci_high_exactly_zero():
    kwargs = dict(_PASS_KWARGS)
    kwargs["ci_high"] = 0.0
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.INCONCLUSIVE
    assert "ci_high_not_negative" in reasons


def test_judgment_inconclusive_when_a_popularity_band_has_zero_races():
    kwargs = dict(_PASS_KWARGS)
    kwargs["band_stats"] = {
        "1-3": {"count": 100, "diff": -0.01},
        "4-8": {"count": 0, "diff": None},
        "9+": {"count": 20, "diff": -0.01},
    }
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.INCONCLUSIVE
    assert "popularity_band_zero_count" in reasons


def test_judgment_invalid_on_non_finite_band_diff_with_races_present():
    kwargs = dict(_PASS_KWARGS)
    kwargs["band_stats"] = {
        "1-3": {"count": 100, "diff": float("nan")},
        "4-8": {"count": 80, "diff": -0.02},
        "9+": {"count": 20, "diff": -0.01},
    }
    verdict, reasons = m.machine_judgment(quality_violations=[], **kwargs)
    assert verdict == m.INVALID


# ---------------------------------------------------------------------------
# SS5 (5): block bootstrap reproducibility (seed=17)
# ---------------------------------------------------------------------------


def test_bootstrap_is_reproducible_with_fixed_seed():
    rows = m.generate_synthetic_race_rows(202, 300, gamma=1.2, kappa=1.0)
    train, validation = m.split_train_validation_by_event_date(rows)
    control = m.FitResult(params=(), feature_names=(), converged=True, message="control")
    fit_b = m.fit_conditional_logit(train, ["drift"])
    first = m.bootstrap_primary_delta(validation, fit_b, control, n_resamples=500, seed=17)
    second = m.bootstrap_primary_delta(validation, fit_b, control, n_resamples=500, seed=17)
    assert first.differences == second.differences
    assert first.ci_low == second.ci_low
    assert first.ci_high == second.ci_high


# ---------------------------------------------------------------------------
# SS5 (6): candidate (d) stage-30-equivalent prediction association rule
# ---------------------------------------------------------------------------


def test_d_association_matches_closest_run_within_tolerance():
    t30 = datetime(2027, 3, 1, 5, 30, 0, tzinfo=timezone.utc)
    horse_set = {"h1", "h2", "h3"}
    runs = {
        (t30 - m._timedelta_seconds(80)).isoformat(): {"h1": 0.2, "h2": 0.5, "h3": 0.3},
        (t30 + m._timedelta_seconds(20)).isoformat(): {"h1": 0.25, "h2": 0.45, "h3": 0.3},
        (t30 + m._timedelta_seconds(500)).isoformat(): {"h1": 0.9, "h2": 0.05, "h3": 0.05},
    }
    compatible, reason, probs = m.resolve_d_association(t30, runs, horse_set)
    assert compatible
    assert reason is None
    assert probs == {"h1": 0.25, "h2": 0.45, "h3": 0.3}


def test_d_association_rejects_beyond_90_seconds():
    t30 = datetime(2027, 3, 1, 5, 30, 0, tzinfo=timezone.utc)
    runs = {(t30 + m._timedelta_seconds(91)).isoformat(): {"h1": 0.5, "h2": 0.5}}
    compatible, reason, probs = m.resolve_d_association(t30, runs, {"h1", "h2"})
    assert not compatible
    assert reason == "no_matching_full_field_probability_run"
    assert probs is None


def test_d_association_accepts_exactly_at_90_seconds():
    t30 = datetime(2027, 3, 1, 5, 30, 0, tzinfo=timezone.utc)
    runs = {(t30 + m._timedelta_seconds(90)).isoformat(): {"h1": 0.5, "h2": 0.5}}
    compatible, reason, probs = m.resolve_d_association(t30, runs, {"h1", "h2"})
    assert compatible


def test_d_association_rejects_partial_horse_set():
    t30 = datetime(2027, 3, 1, 5, 30, 0, tzinfo=timezone.utc)
    runs = {(t30).isoformat(): {"h1": 0.5}}  # missing h2
    compatible, reason, probs = m.resolve_d_association(t30, runs, {"h1", "h2"})
    assert not compatible
    assert reason == "no_matching_full_field_probability_run"


def test_d_association_rejects_null_probability():
    t30 = datetime(2027, 3, 1, 5, 30, 0, tzinfo=timezone.utc)
    runs = {(t30).isoformat(): {"h1": None, "h2": 0.5}}
    compatible, reason, probs = m.resolve_d_association(t30, runs, {"h1", "h2"})
    assert not compatible


# ---------------------------------------------------------------------------
# v3 required additional test: quality-pass >= 800 but final < 800 after
# result/horse-set exclusion must still be reported as gate-not-reached
# (i.e. the gate must read the final eligible count, never quality-pass).
# ---------------------------------------------------------------------------


def test_quality_pass_above_800_but_final_below_800_is_not_executable():
    connection = _mk_connection()
    import random

    rng = random.Random(999)
    for day in range(90):
        scheduled = datetime(2027, 4, 1, 6, 0, 0, tzinfo=timezone.utc) + m._timedelta_seconds(day * 86400)
        for race_no in range(1, 10):
            race_id = f"202704{1 + day:02d}:合成:{race_no:02d}"
            race_date = f"202704{1 + day:02d}"
            horse_ids, winner_id = m.build_clean_synthetic_race(
                connection, rng, race_id, race_date, "合成", 9, scheduled, with_predictions=False
            )
            if race_no == 1:
                # Every day's first race fails the result/horse-set check
                # (a post-30-minute scratch), after already passing capture
                # selection cleanly.
                connection.execute(
                    "DELETE FROM race_results WHERE race_id=? AND horse_id=?",
                    (race_id, horse_ids[-1]),
                )
    connection.commit()
    result = m.run_audit_on_connection(connection)
    connection.close()

    assert result["capture_quality_pass_count"] == 810  # 90 days * 9 races
    assert result["additional_excluded_by_result_or_horseset"] == 90
    assert result["final_eligible_count"] == 720
    assert result["gate_800_reached"] is False
    assert result["gate_800_shortfall"] == 80


# ---------------------------------------------------------------------------
# v3 required additional test: a duplicate/retry snapshot capture is still
# counted as one race, not two.
# ---------------------------------------------------------------------------


def test_duplicate_snapshot_capture_counts_as_one_race():
    connection = _mk_connection()
    scheduled = datetime(2027, 5, 1, 6, 0, 0, tzinfo=timezone.utc)
    m.insert_race_row(connection, "20270501:合成:01", "20270501", "合成", "芝")
    horse_odds = {f"20270501:合成:01:{i:02d}": 3.0 + i for i in range(1, 9)}
    m.insert_clean_capture(connection, "20270501:合成:01", 30, scheduled, horse_odds)
    m.insert_clean_capture(connection, "20270501:合成:01", 10, scheduled, horse_odds)
    m.insert_clean_capture(
        connection, "20270501:合成:01", 2, scheduled, horse_odds, duplicate_stale_capture=True
    )
    finish = {horse_id: rank for rank, horse_id in enumerate(horse_odds, start=1)}
    m.insert_result_rows(connection, "20270501:合成:01", finish, scheduled)
    connection.commit()

    row_count = connection.execute(
        "SELECT COUNT(DISTINCT observed_at) AS n FROM odds_snapshots WHERE race_id=? AND stage='2'",
        ("20270501:合成:01",),
    ).fetchone()["n"]
    assert row_count == 2  # two distinct capture attempts exist

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["candidate_structural_count"] == 1
    assert result["final_eligible_count"] == 1


# ---------------------------------------------------------------------------
# v3 required additional test: same headcount but a different horse set
# must still be excluded (no rescue via odds re-normalisation).
# ---------------------------------------------------------------------------


def test_same_headcount_different_horse_set_is_excluded():
    connection = _mk_connection()
    scheduled = datetime(2027, 5, 2, 6, 0, 0, tzinfo=timezone.utc)
    m.insert_race_row(connection, "20270502:合成:01", "20270502", "合成", "芝")
    horse_odds = {f"20270502:合成:01:{i:02d}": 3.0 + i for i in range(1, 9)}
    m.insert_clean_capture(connection, "20270502:合成:01", 30, scheduled, horse_odds)
    m.insert_clean_capture(connection, "20270502:合成:01", 10, scheduled, horse_odds)
    m.insert_clean_capture(connection, "20270502:合成:01", 2, scheduled, horse_odds)
    # Same count (8), but a disjoint set of horse_ids (a different race's ids).
    other_horse_ids = [f"20270502:合成:99:{i:02d}" for i in range(1, 9)]
    finish = {horse_id: rank for rank, horse_id in enumerate(other_horse_ids, start=1)}
    m.insert_result_rows(connection, "20270502:合成:01", finish, scheduled)
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["exclusion_primary_reason"].get("result_incomplete") == 1


# ---------------------------------------------------------------------------
# v3 required additional test: (d) incompleteness never shrinks the primary
# (a)/(b)/(c) population.
# ---------------------------------------------------------------------------


def test_d_incompleteness_does_not_shrink_primary_population():
    connection = _mk_connection()
    import random

    rng = random.Random(55)
    m.build_clean_synthetic_race(
        connection, rng, "20270503:合成:01", "20270503", "合成", 9, datetime(2027, 5, 3, 6, 0, 0, tzinfo=timezone.utc),
        with_predictions=False,  # no predictions rows at all for this race
    )
    connection.commit()
    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 1
    assert result["d_compatible_count"] == 0
    assert sum(result["d_exclusion_reasons"].values()) == 1


# ---------------------------------------------------------------------------
# v3 required additional test: (d) vs (a) comparison must use the identical
# (d)-compatible race set for both, never a wider (a) population.
# ---------------------------------------------------------------------------


def test_d_vs_a_comparison_uses_the_same_restricted_race_set():
    rows = m.generate_synthetic_race_rows(303, 200, gamma=0.0, kappa=1.0)
    # Simulate half the races being (d)-incompatible.
    d_compatible_rows = rows[: len(rows) // 2]
    control = m.FitResult(params=(), feature_names=(), converged=True, message="control")
    ll_a_full = m.win_log_loss_per_race(rows, control)
    ll_a_restricted = m.win_log_loss_per_race(d_compatible_rows, control)
    assert set(ll_a_restricted) == {row.race_id for row in d_compatible_rows}
    assert set(ll_a_restricted) < set(ll_a_full)
    # The contract under test: the harness must always pass the SAME row
    # list to both the (a) and (d) log-loss calls when comparing them: no
    # helper here silently widens one side back to the full population.
    for row in d_compatible_rows:
        assert row.race_id in ll_a_restricted


# ---------------------------------------------------------------------------
# Registration draft: schema-valid, historical / prospective_start_date=null
# ---------------------------------------------------------------------------


def test_registration_draft_is_historical_with_null_prospective_start_date():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef"
    )
    assert draft["benchmark_type"] == "historical"
    assert draft["prospective_start_date"] is None
    assert draft["experiment_id"] == m.EXPERIMENT_ID
    assert draft["candidate_count"] == 3
    assert draft["search_grid"] == {}


def test_registration_draft_validates_against_ledger_schema():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef"
    )
    outcome = m.validate_registration_draft(draft)
    assert outcome["schema_valid"] is True
    assert outcome["error"] is None


def test_registration_draft_stays_null_registered_at_utc_on_disk_shape():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef"
    )
    # validate_registration_draft must not mutate the caller's draft.
    m.validate_registration_draft(draft)
    assert draft["registered_at_utc"] is None


def test_registration_draft_omits_manifest_and_conditions_hash_by_default():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef"
    )
    assert draft["data_hashes"]["manifest_sha256"] is None
    assert draft["data_hashes"]["evaluation_conditions_sha256"] is None
    assert draft["data_hashes"]["frozen_extract_sha256"] is None


def test_registration_draft_fills_manifest_and_conditions_hash_when_given():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef",
        manifest_sha256="3" * 64, evaluation_conditions_sha256_value="4" * 64,
    )
    assert draft["data_hashes"]["manifest_sha256"] == "sha256:" + "3" * 64
    assert draft["data_hashes"]["evaluation_conditions_sha256"] == "sha256:" + "4" * 64
    outcome = m.validate_registration_draft(draft)
    assert outcome["schema_valid"] is True


def test_registration_draft_notes_embed_section_3_6_clarifications():
    draft = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64, commit_sha="deadbeef"
    )
    assert m.SECTION_3_6_CLARIFICATIONS_TEXT in draft["notes"]
    # Spot-check a couple of the 5 clarifications are actually present, not
    # just the constant's name.
    assert "field_size_mismatch_across_stages" in draft["notes"]
    assert "calibrated_win_probability" in draft["notes"]


# ---------------------------------------------------------------------------
# SPEC v3 SS3.2(d)/SS3.4: build_race_row's optional odds10/predictions30 args
# ---------------------------------------------------------------------------


def test_build_race_row_without_optional_args_is_unchanged():
    row = m.build_race_row(
        "20270901:合成:01", ["h1", "h2"], [3.0, 2.0], [2.5, 2.0], [1, 2], "h1",
    )
    assert row.log_p10 is None
    assert row.d_feature is None
    assert row.log_p30 != ()


def test_build_race_row_with_odds10_computes_descriptive_log_p10():
    row = m.build_race_row(
        "20270901:合成:01", ["h1", "h2"], [3.0, 2.0], [2.5, 2.0], [1, 2], "h1",
        odds10=[2.8, 2.1],
    )
    assert row.log_p10 is not None
    assert len(row.log_p10) == 2
    # log_p10 is a valid log-probability vector (sums to 1 after exp).
    assert abs(sum(math.exp(v) for v in row.log_p10) - 1.0) < 1e-9


def test_build_race_row_with_predictions30_computes_d_feature_zero_when_model_equals_market():
    # When the model's stage-30 probability equals the market's stage-30
    # probability exactly, log(p_model,30) - log(p_30) must be exactly 0.
    row = m.build_race_row(
        "20270901:合成:01", ["h1", "h2"], [3.0, 2.0], [2.5, 2.0], [1, 2], "h1",
        predictions30={"h1": 0.4, "h2": 0.6},  # matches market_probabilities([3.0, 2.0])
    )
    assert row.d_feature is not None
    assert all(abs(v) < 1e-12 for v in row.d_feature)


# ---------------------------------------------------------------------------
# SPEC v3 SS3.7: --run evaluation body, end-to-end on synthetic data only
# ---------------------------------------------------------------------------


def test_run_smoke_end_to_end_produces_all_4_artifacts(tmp_path: Path):
    out_dir = tmp_path / "smoke"
    outcome = m.run_smoke_end_to_end(out_dir, seed=17)

    assert (out_dir / "result.json").is_file()
    assert (out_dir / "race_metrics.jsonl").is_file()
    assert (out_dir / "predictions.jsonl").is_file()
    assert (out_dir / "result_record.draft.json").is_file()

    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["mode"] == "smoke"
    assert result["machine_judgment"]["verdict"] in {
        m.INVALID, m.HISTORICAL_SCREEN_FAIL, m.INCONCLUSIVE, m.HISTORICAL_SCREEN_PASS,
    }
    assert result["quality_violations"] == []  # a clean synthetic population converges

    race_metrics_lines = (out_dir / "race_metrics.jsonl").read_text(encoding="utf-8").splitlines()
    predictions_lines = (out_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(race_metrics_lines) == result["final_eligible_count"]
    assert len(predictions_lines) == result["final_eligible_count"]
    for line in race_metrics_lines:
        row = json.loads(line)
        assert row["log_loss"]["a_control_market2"] >= 0.0

    record_draft = json.loads((out_dir / "result_record.draft.json").read_text(encoding="utf-8"))
    assert record_draft["adjudication"] is None
    assert record_draft["registered_at_utc"] is None
    assert record_draft["superseded_by"] == m.EXPERIMENT_ID


def test_run_evaluation_reports_invalid_when_population_below_gate(tmp_path: Path):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    m.create_synthetic_schema(connection)
    import random

    rng = random.Random(1)
    scheduled = datetime(2027, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        m.build_clean_synthetic_race(
            connection, rng, f"2027100{i+1}:合成:01", f"2027100{i+1}", "合成", 9,
            scheduled + m._timedelta_seconds(i * 86400),
        )
    connection.commit()
    outcome = m.run_evaluation(connection, enforce_min_population=True, min_population=800)
    connection.close()
    assert outcome.result["machine_judgment"]["verdict"] == m.INVALID
    assert any("final_eligible_population_below_800" in v for v in outcome.result["quality_violations"])


def test_run_evaluation_below_gate_never_calls_the_fitting_function(tmp_path: Path, monkeypatch):
    """SPEC v3 SS3.7 round-3 review: when enforce_min_population=True and the
    final eligible population is short of the gate, run_evaluation must stop
    immediately on the shortfall verdict, before any coefficient estimation.
    Previously the shortfall was only recorded and the run proceeded through
    row-building and fit_conditional_logit anyway, reaching INVALID only
    after the fact. This asserts the learning path is never entered: neither
    build_race_rows_from_audits nor fit_conditional_logit is called."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    m.create_synthetic_schema(connection)
    import random

    rng = random.Random(1)
    scheduled = datetime(2027, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        m.build_clean_synthetic_race(
            connection, rng, f"2027100{i+1}:合成:01", f"2027100{i+1}", "合成", 9,
            scheduled + m._timedelta_seconds(i * 86400),
        )
    connection.commit()

    fit_calls: list[object] = []
    build_rows_calls: list[object] = []
    monkeypatch.setattr(
        m, "fit_conditional_logit",
        lambda *a, **k: fit_calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("fit_conditional_logit must not be called under an unmet population gate")
        ),
    )
    monkeypatch.setattr(
        m, "build_race_rows_from_audits",
        lambda *a, **k: build_rows_calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("build_race_rows_from_audits must not be called under an unmet population gate")
        ),
    )

    outcome = m.run_evaluation(connection, enforce_min_population=True, min_population=800)
    connection.close()

    assert fit_calls == []
    assert build_rows_calls == []
    assert outcome.result["machine_judgment"]["verdict"] == m.INVALID
    assert any("final_eligible_population_below_800" in v for v in outcome.result["quality_violations"])
    assert outcome.race_metrics == []
    assert outcome.predictions == []


def test_run_evaluation_smoke_style_below_gate_without_enforcement_still_fits(tmp_path: Path, monkeypatch):
    """The same shortfall population must still reach fit_conditional_logit
    when enforce_min_population=False (the --smoke relaxation), confirming
    the early stop above is specific to the enforced gate and not a general
    regression that breaks --smoke's synthetic (small-population) path."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    m.create_synthetic_schema(connection)
    import random

    rng = random.Random(1)
    scheduled = datetime(2027, 10, 1, 6, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        m.build_clean_synthetic_race(
            connection, rng, f"2027100{i+1}:合成:01", f"2027100{i+1}", "合成", 9,
            scheduled + m._timedelta_seconds(i * 86400),
        )
    connection.commit()

    fit_calls: list[object] = []
    real_fit = m.fit_conditional_logit

    def _spy(*a, **k):
        fit_calls.append((a, k))
        return real_fit(*a, **k)

    monkeypatch.setattr(m, "fit_conditional_logit", _spy)

    outcome = m.run_evaluation(connection, enforce_min_population=False)
    connection.close()

    assert len(fit_calls) >= 1
    assert outcome.result["final_eligible_count"] == 3


# ---------------------------------------------------------------------------
# Isolation / fail-closed checks
# ---------------------------------------------------------------------------


def test_read_only_connect_rejects_writes(tmp_path: Path):
    db_path = tmp_path / "probe.db"
    sqlite3.connect(db_path).execute("CREATE TABLE t (x INTEGER)")
    connection = m.read_only_connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO t VALUES (1)")
    connection.close()


def _write_minimal_matching_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"evaluation_conditions_sha256": m.evaluation_conditions_sha256()}),
        encoding="utf-8",
    )
    return manifest_path


def test_run_gate_refuses_without_ledger_entry(tmp_path: Path):
    extract = tmp_path / "extract.sqlite"
    extract.write_bytes(b"synthetic frozen extract placeholder bytes")
    manifest_path = _write_minimal_matching_manifest(tmp_path)
    gate = m.check_run_gate("T17a-odds-drift-diagnostic-v1", extract, manifest_path)
    assert gate["allowed"] is False
    assert "not registered" in gate["reason"]


def test_run_gate_refuses_when_extract_missing(tmp_path: Path):
    manifest_path = _write_minimal_matching_manifest(tmp_path)
    gate = m.check_run_gate(
        "T17a-odds-drift-diagnostic-v1", tmp_path / "does_not_exist.sqlite", manifest_path
    )
    assert gate["allowed"] is False
    assert "not found" in gate["reason"]


def test_run_gate_refuses_when_manifest_missing(tmp_path: Path):
    extract = tmp_path / "extract.sqlite"
    extract.write_bytes(b"synthetic frozen extract placeholder bytes")
    gate = m.check_run_gate(
        "T17a-odds-drift-diagnostic-v1", extract, tmp_path / "does_not_exist.json"
    )
    assert gate["allowed"] is False
    assert "manifest not found" in gate["reason"]


def test_cli_run_mode_refuses_without_extract_flag(capsys):
    exit_code = m.main(["--run"])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# v3 SS3.7 round-3 review: --run must not be able to execute without
# --out-dir, and the out-dir check must run before the registration gate
# (ledger/extract/manifest lookups), so a bad --out-dir refuses even when
# --extract/--manifest are missing or bogus.
# ---------------------------------------------------------------------------


def test_validate_run_out_dir_requires_a_path():
    error = m.validate_run_out_dir(None)
    assert error is not None
    assert "--out-dir" in error


def test_validate_run_out_dir_refuses_existing_artifacts(tmp_path: Path):
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    error = m.validate_run_out_dir(str(tmp_path))
    assert error is not None
    assert "result.json" in error
    assert "overwrite" in error


def test_validate_run_out_dir_refuses_when_path_is_a_file(tmp_path: Path):
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("x", encoding="utf-8")
    error = m.validate_run_out_dir(str(not_a_dir))
    assert error is not None
    assert "not a directory" in error


def test_validate_run_out_dir_accepts_new_empty_directory(tmp_path: Path):
    fresh = tmp_path / "fresh_out"
    assert m.validate_run_out_dir(str(fresh)) is None
    assert fresh.is_dir()  # created as a side effect, same as a real run would


def test_cli_run_mode_refuses_without_out_dir_before_touching_extract_or_manifest(capsys):
    # No --out-dir, and --extract/--manifest are also omitted: the failure
    # must be the out-dir one (checked first), not "requires --extract...".
    exit_code = m.main(["--run"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--out-dir" in captured.err
    assert "--extract" not in captured.err


def test_cli_run_mode_refuses_when_out_dir_already_has_artifacts(tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "predictions.jsonl").write_text("", encoding="utf-8")
    # --extract/--manifest point at nonexistent paths on purpose: the
    # out-dir refusal must fire before either is even looked up.
    exit_code = m.main([
        "--run", "--out-dir", str(out_dir),
        "--extract", str(tmp_path / "does_not_exist.sqlite"),
        "--manifest", str(tmp_path / "does_not_exist.json"),
    ])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "predictions.jsonl" in captured.err
    assert "overwrite" in captured.err


def test_cli_run_end_to_end_with_valid_out_dir_persists_all_4_artifacts(tmp_path: Path, monkeypatch):
    """SPEC v3 SS3.7 round-3 review: once --out-dir is valid and the
    registration gate passes, --run must actually persist result.json,
    race_metrics.jsonl, predictions.jsonl, and result_record.draft.json.
    Exercises main(["--run", ...]) itself (not run_evaluation/
    write_run_artifacts directly): the ledger lookup and check_run_gate are
    replaced with a hermetic, self-consistent fixture so the real
    eval/experiments.jsonl ledger is never read or written, and the
    frozen-extract sqlite is a real on-disk synthetic database (>=800
    eligible races) so run_evaluation's enforce_min_population=True gate
    (always on for --run) is satisfied without touching real data."""
    import random

    from eval import ledger as eval_ledger

    extract_path = tmp_path / "extract.sqlite"
    connection = sqlite3.connect(extract_path)
    connection.row_factory = sqlite3.Row
    m.create_synthetic_schema(connection)
    rng = random.Random(7)
    base_date = datetime(2027, 5, 1, 6, 0, 0, tzinfo=timezone.utc)
    built = 0
    day = 0
    while built < 800:
        scheduled = base_date + m._timedelta_seconds(day * 86400)
        race_date = scheduled.strftime("%Y%m%d")
        for race_no in range(1, 9):
            if built >= 800:
                break
            race_id = f"{race_date}:合成:{race_no:02d}"
            m.build_clean_synthetic_race(
                connection, rng, race_id, race_date, "合成", 9, scheduled,
            )
            built += 1
        day += 1
    connection.commit()
    connection.close()

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"evaluation_conditions_sha256": m.evaluation_conditions_sha256()}),
        encoding="utf-8",
    )
    registration_record = m.build_registration_draft(
        spec_sha256="0" * 64, harness_sha256="1" * 64, tests_sha256="2" * 64,
        commit_sha="deadbeef",
    )
    monkeypatch.setattr(eval_ledger, "load_ledger", lambda *_a, **_k: [registration_record])
    monkeypatch.setattr(
        m, "check_run_gate",
        lambda *a, **k: {"allowed": True, "reason": "test-bypass", "checks": {}},
    )

    out_dir = tmp_path / "out"
    exit_code = m.main([
        "--run", "--extract", str(extract_path), "--manifest", str(manifest_path),
        "--out-dir", str(out_dir),
    ])
    assert exit_code == 0
    assert (out_dir / "result.json").is_file()
    assert (out_dir / "race_metrics.jsonl").is_file()
    assert (out_dir / "predictions.jsonl").is_file()
    assert (out_dir / "result_record.draft.json").is_file()

    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["mode"] == "run"
    assert result["final_eligible_count"] >= 800

    record_draft = json.loads((out_dir / "result_record.draft.json").read_text(encoding="utf-8"))
    assert record_draft["commit_sha"] not in (None, "unknown", "")


# ---------------------------------------------------------------------------
# v3 SS3.7: --run gate content matching (SPEC/harness/tests/manifest sha256
# and the live evaluation_conditions fingerprint), each checked against a
# synthetic, tmp_path-only ledger. Never touches the real
# eval/experiments.jsonl.
# ---------------------------------------------------------------------------


def _build_matching_gate_fixture(tmp_path: Path) -> dict:
    """A fully self-consistent synthetic Stage-B-style fixture: a frozen
    extract file, a manifest whose evaluation_conditions_sha256 matches the
    live code, and a ledger row whose data_hashes/contract fields match both
    the manifest and the actual current SPEC/harness/tests files on disk."""

    extract = tmp_path / "extract.sqlite"
    extract.write_bytes(b"synthetic frozen extract placeholder bytes")
    manifest_path = _write_minimal_matching_manifest(tmp_path)

    spec_sha = m.sha256_file(m.REPO_ROOT / "docs" / "codex" / "SPEC-T17a-odds-drift-diagnostic.md")
    harness_sha = m.sha256_file(m.REPO_ROOT / "backtest_t17a_drift.py")
    tests_sha = m.sha256_file(m.REPO_ROOT / "tests" / "test_t17a_drift.py")
    manifest_sha = m.sha256_file(manifest_path)
    extract_sha = m.sha256_file(extract)
    conditions_sha = m.evaluation_conditions_sha256()

    draft = m.build_registration_draft(
        spec_sha256=spec_sha,
        harness_sha256=harness_sha,
        tests_sha256=tests_sha,
        commit_sha="deadbeef",
        manifest_sha256=manifest_sha,
        evaluation_conditions_sha256_value=conditions_sha,
    )
    draft["registered_at_utc"] = "2026-09-24T00:00:00Z"
    draft["data_hashes"]["frozen_extract_sha256"] = extract_sha

    ledger_path = tmp_path / "experiments.jsonl"
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")

    return {
        "extract": extract,
        "manifest_path": manifest_path,
        "ledger_path": ledger_path,
        "draft": draft,
    }


def test_run_gate_allows_when_everything_matches(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"],
        ledger_path=fixture["ledger_path"],
    )
    assert gate["allowed"] is True


def test_run_gate_refuses_on_frozen_extract_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    fixture["extract"].write_bytes(b"a different, unregistered extract")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"],
        ledger_path=fixture["ledger_path"],
    )
    assert gate["allowed"] is False
    assert "frozen_extract_sha256" in gate["reason"]


def test_run_gate_refuses_on_manifest_sha_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    fixture["manifest_path"].write_text(
        json.dumps({"evaluation_conditions_sha256": m.evaluation_conditions_sha256(), "extra": 1}),
        encoding="utf-8",
    )
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"],
        ledger_path=fixture["ledger_path"],
    )
    assert gate["allowed"] is False
    assert "manifest_sha256" in gate["reason"]


def test_run_gate_refuses_on_evaluation_conditions_mismatch_in_manifest(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    fixture["manifest_path"].write_text(
        json.dumps({"evaluation_conditions_sha256": "0" * 64}), encoding="utf-8",
    )
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"],
        ledger_path=fixture["ledger_path"],
    )
    assert gate["allowed"] is False
    assert "evaluation_conditions_sha256" in gate["reason"]


def test_run_gate_refuses_on_spec_sha_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["data_hashes"] = dict(draft["data_hashes"])
    draft["data_hashes"]["spec"] = "sha256:" + "0" * 64
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "'spec'" in gate["reason"]


def test_run_gate_refuses_on_harness_sha_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["data_hashes"] = dict(draft["data_hashes"])
    draft["data_hashes"]["harness_source"] = "sha256:" + "0" * 64
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "harness_source" in gate["reason"]


def test_run_gate_refuses_on_tests_sha_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["data_hashes"] = dict(draft["data_hashes"])
    draft["data_hashes"]["tests_source"] = "sha256:" + "0" * 64
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "tests_source" in gate["reason"]


def test_run_gate_refuses_on_evaluation_conditions_mismatch_in_ledger(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["data_hashes"] = dict(draft["data_hashes"])
    draft["data_hashes"]["evaluation_conditions_sha256"] = "sha256:" + "0" * 64
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "evaluation_conditions_sha256" in gate["reason"]


def test_run_gate_refuses_on_candidate_count_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["candidate_count"] = 4
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "candidate_count" in gate["reason"]


def test_run_gate_refuses_on_search_grid_mismatch(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["search_grid"] = {"l2": [0.1, 1.0]}
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "search_grid" in gate["reason"]


def test_run_gate_refuses_on_prospective_benchmark_type(tmp_path: Path):
    fixture = _build_matching_gate_fixture(tmp_path)
    ledger_path = tmp_path / "experiments.jsonl"
    draft = dict(fixture["draft"])
    draft["benchmark_type"] = "prospective"
    draft["prospective_start_date"] = "2026-10-01"
    ledger_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    gate = m.check_run_gate(
        m.EXPERIMENT_ID, fixture["extract"], fixture["manifest_path"], ledger_path=ledger_path,
    )
    assert gate["allowed"] is False
    assert "benchmark_type" in gate["reason"]


def test_cli_requires_exactly_one_mode():
    with pytest.raises(SystemExit):
        m.main([])


# ---------------------------------------------------------------------------
# Frozen extraction procedure (synthetic only; never touches the real DB)
# ---------------------------------------------------------------------------


def test_create_frozen_extract_round_trip(tmp_path: Path):
    source = _mk_connection()
    scheduled = datetime(2027, 6, 1, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(7)
    m.build_clean_synthetic_race(source, rng, "20270601:合成:01", "20270601", "合成", 9, scheduled)
    source.commit()
    # create_frozen_extract expects a read-only-style connection; a plain
    # in-memory connection is fine for this synthetic round trip.
    dest = tmp_path / "extract.sqlite"
    sha = m.create_frozen_extract(source, dest, ["20270601:合成:01"])
    source.close()

    assert dest.exists()
    assert sha == m.sha256_file(dest)
    check = sqlite3.connect(dest)
    check.row_factory = sqlite3.Row
    assert check.execute("SELECT COUNT(*) AS n FROM races").fetchone()["n"] == 1
    assert check.execute("SELECT COUNT(*) AS n FROM race_results").fetchone()["n"] == 9
    assert check.execute("SELECT COUNT(*) AS n FROM odds_snapshots").fetchone()["n"] > 0
    assert check.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"] == 9
    check.close()


def test_create_frozen_extract_refuses_to_overwrite(tmp_path: Path):
    source = _mk_connection()
    scheduled = datetime(2027, 6, 2, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(8)
    m.build_clean_synthetic_race(source, rng, "20270602:合成:01", "20270602", "合成", 9, scheduled)
    source.commit()
    dest = tmp_path / "extract.sqlite"
    dest.write_text("not a real sqlite file")
    with pytest.raises(m.QualityError):
        m.create_frozen_extract(source, dest, ["20270602:合成:01"])
    source.close()


# ---------------------------------------------------------------------------
# Time-point asserts (SS3.3): observed_at < scheduled_post_at, and
# result_fetched_at > scheduled_post_at, are checked on the adopted set.
# ---------------------------------------------------------------------------


def test_time_point_violation_marks_audit_invalid_not_a_silent_pass():
    connection = _mk_connection()
    scheduled = datetime(2027, 7, 1, 6, 0, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(11)
    m.build_clean_synthetic_race(connection, rng, "20270701:合成:01", "20270701", "合成", 9, scheduled)
    # Corrupt the stored result_fetched_at to be BEFORE the post time, which
    # a correct implementation must catch even though capture selection and
    # the horse-set check already passed.
    connection.execute(
        "UPDATE race_results SET result_fetched_at=? WHERE race_id=?",
        (m._iso(scheduled - m._timedelta_seconds(10)), "20270701:合成:01"),
    )
    connection.commit()

    result = m.run_audit_on_connection(connection)
    connection.close()
    assert result["final_eligible_count"] == 0
    assert result["machine_status"] == "INVALID"
    assert result["time_assert_violations"]
