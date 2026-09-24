"""SPEC-T81 Stage A required tests (SS13) for backtest_rank_display.py.

Every test here uses synthetic/in-memory data only. None of them open the
real ability.db, write any production resource, or touch the network -- see
test_isolated_pedigree_adapter_never_touches_network /
test_readonly_connection_rejects_writes for the explicit fail-closed checks.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import backtest_rank_display as rd
from backtest_rank_objective import pl_objective_and_gradient, stage_weights

FIXTURES = Path(__file__).parent / "fixtures" / "t81_rank_display"


def _load_case(name: str) -> list[dict]:
    with open(FIXTURES / "raw_population_cases.json", "r", encoding="utf-8") as handle:
        cases = json.load(handle)
    return cases[name]


def _key_of(rows: list[dict]) -> tuple:
    row = rows[0]
    return (row["date"], row["place"], row["r"])


# ---------------------------------------------------------------------------
# 1. Spearman: forward=1, reverse=-1, ties=average rank, all-tied=0+flag
# ---------------------------------------------------------------------------

def test_spearman_forward_and_reverse_orderings():
    score = np.array([8.0, 7.0, 6.0, 5.0])
    actual_rank = np.array([1, 2, 3, 4])  # score already agrees with -rank
    rho, all_tied = rd.spearman_with_flag(score, actual_rank)
    assert rho == pytest.approx(1.0)
    assert not all_tied

    reversed_rank = np.array([4, 3, 2, 1])  # score disagrees completely
    rho2, _ = rd.spearman_with_flag(score, reversed_rank)
    assert rho2 == pytest.approx(-1.0)


def test_spearman_ties_use_average_rank():
    # Two runners share the top score; spearman must use average-rank ties,
    # not an arbitrary tie-break, when comparing against the outcome.
    score = np.array([5.0, 5.0, 3.0, 1.0])
    actual_rank = np.array([1, 2, 3, 4])
    rho, all_tied = rd.spearman_with_flag(score, actual_rank)
    assert not all_tied
    # score ranks (avg ties, lower=better): [1.5, 1.5, 3, 4]. actual_rank is
    # already a "lower=better" rank scale, so rho must equal the ordinary
    # Pearson correlation between the two same-orientation rank scales.
    score_rank = rd.average_rank_ties(score, descending=True)
    assert score_rank.tolist() == [1.5, 1.5, 3.0, 4.0]
    assert rho == pytest.approx(np.corrcoef(score_rank, actual_rank.astype(float))[0, 1])


def test_spearman_all_tied_scores_returns_zero_with_flag():
    score = np.array([2.0, 2.0, 2.0, 2.0])
    actual_rank = np.array([1, 2, 3, 4])
    rho, all_tied = rd.spearman_with_flag(score, actual_rank)
    assert rho == 0.0
    assert all_tied is True


# ---------------------------------------------------------------------------
# 2. winner_in_top3 vs top3_set_recall are different fixtures; MAE hand-check
# ---------------------------------------------------------------------------

def test_winner_in_top3_and_top3_set_recall_differ():
    # Winner predicted 1st (winner_in_top3=1), but the OTHER two predicted
    # top-3 slots are runners who actually finished outside the real top 3.
    # actual top-3 = horses with actual_rank<=3 -> indices {0,4,5} (ranks 1,2,3)
    pred_rank = np.array([1, 4, 5, 6, 7, 8])
    actual_rank = np.array([1, 4, 5, 6, 2, 3])
    recall = rd.top3_set_recall(pred_rank, actual_rank)
    winner_in_top3 = int(pred_rank[int(np.argmin(actual_rank))] <= 3)
    assert winner_in_top3 == 1
    assert recall == pytest.approx(1 / 3)  # only the winner slot overlaps


def test_actual_top3_rank_mae_matches_hand_calculation():
    # n=8; actual top3 are indices 0 (rank1), 1 (rank2), 2 (rank3).
    pred_rank = np.array([2, 1, 5, 4, 3, 6, 7, 8])
    actual_rank = np.array([1, 2, 3, 4, 5, 6, 7, 8])
    mae = rd.actual_top3_rank_mae(pred_rank, actual_rank)
    # |2-1| + |1-2| + |5-3| = 1 + 1 + 2 = 4; /3 winners /(n-1)=7
    assert mae == pytest.approx((4 / 3) / 7)


def test_top3_order_exact_requires_full_order_match():
    actual_rank = np.array([1, 2, 3, 4, 5, 6, 7, 8])
    exact_pred = np.array([1, 2, 3, 4, 5, 6, 7, 8])
    assert rd.top3_order_exact(exact_pred, actual_rank) == 1
    swapped_pred = np.array([2, 1, 3, 4, 5, 6, 7, 8])
    assert rd.top3_order_exact(swapped_pred, actual_rank) == 0


# ---------------------------------------------------------------------------
# 3. Predicted display order ties resolved by umaban asc; row-order invariant
# ---------------------------------------------------------------------------

def test_display_order_ties_broken_by_umaban_and_is_row_order_invariant():
    score = np.array([9.0, 9.0, 4.0, 1.0])
    umaban = np.array([3.0, 1.0, 2.0, 4.0])
    order = rd.display_order(score, umaban)
    # umaban 1 (score 9, index1) beats umaban 3 (score 9, index0) for rank1
    assert order.tolist() == [2, 1, 3, 4]

    # Shuffle every array by the same permutation; the *mapping* from a given
    # runner to its predicted rank must not depend on row order.
    # permutation[k] = original index of the runner now sitting at position k.
    permutation = [3, 1, 0, 2]
    shuffled_order = rd.display_order(score[permutation], umaban[permutation])
    for shuffled_position, original_index in enumerate(permutation):
        assert shuffled_order[shuffled_position] == order[original_index]


# ---------------------------------------------------------------------------
# 4. Raw-level exclusions: tie / cancel / duplicate / rank gap / headcount
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case,expected_flag", [
    ("tie_race", "rank_not_strict_1_to_n"),
    ("cancelled_runner_race", "rank_not_strict_1_to_n"),
    ("row_count_mismatch_race", "raw_duplicate_or_row_mismatch"),
    ("small_field_race", "headcount_invalid"),
    ("duplicate_umaban_race", "raw_duplicate_or_row_mismatch"),
    ("non_jra_venue_race", "invalid_race_key_or_scope"),
    ("rank_gap_race", "rank_not_strict_1_to_n"),
    ("race_no_out_of_scope", "invalid_race_key_or_scope"),
])
def test_classify_raw_race_flags_each_bad_case(case, expected_flag):
    rows = _load_case(case)
    flags, _n_declared, _n_raw = rd.classify_raw_race(_key_of(rows), rows)
    assert expected_flag in flags


def test_classify_raw_race_accepts_the_good_case():
    rows = _load_case("good_race")
    flags, n_declared, n_raw = rd.classify_raw_race(_key_of(rows), rows)
    assert flags == set()
    assert n_declared == 8
    assert n_raw == 8


# ---------------------------------------------------------------------------
# 5. Winner present but another runner's features are missing -> not complete
# ---------------------------------------------------------------------------

def test_incomplete_feature_field_is_excluded_even_with_a_winner_present():
    rows = _load_case("good_race")
    key = _key_of(rows)
    runs = rows  # only need the fields classify_raw_race/build_population read
    # 8 raw runners, but only 7 feature/meta rows (umaban 8 missing entirely).
    feature_rows = rows[:7]
    features = np.ones((7, 21), dtype=float)
    labels = np.array([1 if row["rank"] == 1 else 0 for row in feature_rows])
    race_keys = [key] * 7
    meta = feature_rows
    population = rd.build_population(runs, features, labels, race_keys, meta,
                                     date_from=key[0], date_to=key[0])
    assert key not in population.eligible_keys
    assert population.primary_reason[key] == "feature_row_mismatch"
    assert "feature_row_mismatch" in population.all_flags[key]


# ---------------------------------------------------------------------------
# 6. Odds missing/1.0/NaN/Inf excluded; popularity order != odds order uses odds
# ---------------------------------------------------------------------------

def _complete_feature_dataset_for(rows, win_pay_overrides=None):
    key = _key_of(rows)
    rows = [dict(row) for row in rows]
    if win_pay_overrides:
        for row, win_pay in zip(rows, win_pay_overrides):
            row["win_pay"] = win_pay
    else:
        for row in rows:
            row["win_pay"] = "250" if row["rank"] == 1 else "(3.0)"
    features = np.ones((len(rows), 21), dtype=float)
    labels = np.array([1 if row["rank"] == 1 else 0 for row in rows])
    race_keys = [key] * len(rows)
    return rows, features, labels, race_keys, rows, key


def test_missing_or_degenerate_odds_are_excluded():
    good_rows = _load_case("good_race")

    def check(win_pay_for_winner, expect_eligible):
        overrides = [win_pay_for_winner if row["rank"] == 1 else "(3.0)" for row in good_rows]
        rows, features, labels, race_keys, meta, key = _complete_feature_dataset_for(
            good_rows, overrides)
        population = rd.build_population(rows, features, labels, race_keys, meta,
                                          date_from=key[0], date_to=key[0])
        assert (key in population.eligible_keys) is expect_eligible
        if not expect_eligible:
            assert "missing_or_invalid_odds" in population.all_flags[key]

    check("250", True)          # normal settled odds (2.5x)
    check(None, False)          # missing
    check("100", False)         # odds == 1.0 exactly, excluded per SPEC SS5
    check("nan", False)         # unparsable -> None from parse_final_odds


def test_market_order_uses_odds_not_popularity_when_they_disagree():
    good_rows = _load_case("good_race")
    rows = [dict(row) for row in good_rows]
    # Make popularity disagree with odds-implied order for every runner.
    for row in rows:
        row["popularity"] = 9 - row["umaban"]
        row["win_pay"] = "250" if row["rank"] == 1 else f"({(row['umaban'] + 1) * 1.0:.1f})"
    scores = rd.market_score(rows)
    odds = np.array([rd.parse_final_odds(row["win_pay"], row["rank"]) for row in rows])
    order = rd.display_order(scores, np.array([row["umaban"] for row in rows], dtype=float))
    # Best predicted rank must go to the smallest odds runner, not the
    # smallest-popularity-number runner (they disagree by construction here).
    best_index = int(np.argmin(order))
    assert odds[best_index] == pytest.approx(odds.min())


# ---------------------------------------------------------------------------
# 7. PL top-1 / PL@3 numeric gradient match; stage weights sum to 1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("objective", [rd.CANDIDATE_OBJECTIVE, rd.CONTROL_OBJECTIVE])
def test_pl_gradient_matches_central_difference(objective):
    X = np.asarray([[0.3, -0.2], [1.0, 0.4], [-0.6, 0.7],
                    [0.4, 0.1], [-0.2, -0.4], [0.8, 0.6]])
    ranks = np.asarray([2, 1, 3, 3, 1, 2])
    keys = [("d1", "p", 1)] * 3 + [("d2", "p", 2)] * 3
    weights = np.asarray([0.11, -0.22])
    _loss, analytic = pl_objective_and_gradient(
        weights, X, ranks, keys, objective=objective, l2=0.3)
    numeric = np.zeros_like(weights)
    epsilon = 1e-6
    for i in range(len(weights)):
        plus, minus = weights.copy(), weights.copy()
        plus[i] += epsilon
        minus[i] -= epsilon
        lp, _ = pl_objective_and_gradient(plus, X, ranks, keys, objective=objective, l2=0.3)
        lm, _ = pl_objective_and_gradient(minus, X, ranks, keys, objective=objective, l2=0.3)
        numeric[i] = (lp - lm) / (2 * epsilon)
    assert analytic == pytest.approx(numeric, rel=1e-6, abs=1e-7)
    assert analytic == pytest.approx(numeric, abs=1e-5)


@pytest.mark.parametrize("objective", [rd.CANDIDATE_OBJECTIVE, rd.CONTROL_OBJECTIVE])
@pytest.mark.parametrize("field_size", [8, 12, 18])
def test_stage_weights_sum_to_one(objective, field_size):
    assert stage_weights(objective, field_size).sum() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 8. scaler never sees eval rows; future rows/labels do not change past scores
# ---------------------------------------------------------------------------

def test_fit_pl_model_scaler_uses_only_the_rows_it_is_given():
    rng = np.random.RandomState(0)
    train = rng.normal(loc=5.0, scale=2.0, size=(20, 3))
    eval_only = rng.normal(loc=-50.0, scale=0.1, size=(20, 3))
    ranks = np.tile([1, 2, 3, 1], 5)
    keys = [(f"d{i}", "p", 1) for i in range(5) for _ in range(4)]
    fit_train_only = rd.fit_pl_model(train, ranks, keys, objective="pl_top3", l2=1.0)
    combined = np.vstack([train, eval_only])
    fit_train_only_again = rd.fit_pl_model(train, ranks, keys, objective="pl_top3", l2=1.0)
    assert fit_train_only.scaler_mean.tolist() == pytest.approx(
        fit_train_only_again.scaler_mean.tolist())
    # A scaler fit on train+eval must differ from one fit on train alone,
    # proving fit_pl_model's StandardScaler is not silently seeing eval_only.
    from sklearn.preprocessing import StandardScaler
    combined_scaler = StandardScaler().fit(combined)
    assert not np.allclose(fit_train_only.scaler_mean, combined_scaler.mean_)


def test_strict_sire_gate_ignores_horses_outside_its_own_train_window():
    runs = [
        {"date": "20210301", "horse": "OldA", "track_type": "芝", "place": "東京"},
        {"date": "20210301", "horse": "OldB", "track_type": "芝", "place": "東京"},
    ]
    pedigree = {"OldA": {"sire": "S1"}, "OldB": {"sire": "S2"}}
    gate_without_future = rd.strict_sire_gate(runs, pedigree, "20210101", "20211231")

    future_runs = runs + [
        {"date": "20250601", "horse": "FutureHorseNotInPedigree",
         "track_type": "芝", "place": "東京"},
    ]
    gate_with_future = rd.strict_sire_gate(future_runs, pedigree, "20210101", "20211231")
    assert gate_without_future == gate_with_future


def test_apply_sire_gate_only_ever_removes_information():
    features = np.ones((3, 21), dtype=float) * 5.0
    sire_index = rd.SPEC_FEATURES.index("sire_pts")
    enabled = rd.apply_sire_gate(features, True)
    disabled = rd.apply_sire_gate(features, False)
    assert enabled is features
    assert np.all(disabled[:, sire_index] == 0.0)
    other_columns = [i for i in range(21) if i != sire_index]
    assert np.array_equal(disabled[:, other_columns], features[:, other_columns])


# ---------------------------------------------------------------------------
# 9. All metrics share the same race/horse set; 1R-8R in the primary table,
#    9R+ reported separately with its own denominator
# ---------------------------------------------------------------------------

def _race_metrics_dict(race_no, n=8):
    key = ("20220101", "東京", race_no)
    score = np.arange(n, 0, -1, dtype=float)
    actual_rank = np.arange(1, n + 1)
    umaban = np.arange(1, n + 1, dtype=float)
    return {key: rd.compute_race_metrics(key, score, actual_rank, umaban)}


def test_race1_through_8_are_in_the_primary_population_not_only_9_plus():
    metrics_r1 = _race_metrics_dict(1)
    metrics_r9 = _race_metrics_dict(9)
    combined = {**metrics_r1, **metrics_r9}
    all_races = list(combined.values())
    nine_plus = [v for v in all_races if v.race_no is not None and v.race_no >= 9]
    assert len(all_races) == 2
    assert len(nine_plus) == 1  # only the r9 race, proving 1-8 racess are kept
                                # in the primary population and reported apart


def test_compare_period_requires_identical_race_populations():
    candidate = _race_metrics_dict(5)
    top1 = _race_metrics_dict(5)
    market = _race_metrics_dict(6)  # different race key -> mismatched population
    with pytest.raises(rd.QualityError):
        rd.compare_period(candidate, top1, market, "2025")


# ---------------------------------------------------------------------------
# 10. Event-day block correspondence, race-equal weighting, seed reproducibility
# ---------------------------------------------------------------------------

def test_blocks_by_date_groups_all_venues_of_one_day_together():
    metrics = {
        ("20220101", "東京", 1): 0.1,
        ("20220101", "中山", 1): 0.2,
        ("20220102", "東京", 1): 0.3,
    }
    blocks = rd._blocks_by_date(metrics)
    assert set(blocks) == {"20220101", "20220102"}
    assert sorted(blocks["20220101"]) == [0.1, 0.2]
    assert blocks["20220102"] == [0.3]


def test_paired_metric_diff_is_race_equal_weighted_and_seed_reproducible():
    candidate = {**_race_metrics_dict(1), **_race_metrics_dict(9)}
    market = {**_race_metrics_dict(1), **_race_metrics_dict(9)}
    result_a = rd.paired_metric_diff(candidate, market, "spearman", n_resamples=200, seed=81)
    result_b = rd.paired_metric_diff(candidate, market, "spearman", n_resamples=200, seed=81)
    assert result_a == result_b  # identical seed -> byte-identical bootstrap


def test_paired_metric_diff_raises_when_populations_differ():
    candidate = _race_metrics_dict(5)
    market = _race_metrics_dict(6)
    with pytest.raises(rd.QualityError):
        rd.paired_metric_diff(candidate, market, "spearman")


# ---------------------------------------------------------------------------
# 11. Fail-closed: non-convergence, ledger/manifest mismatch, no network
#     fallback, no writable DB connection
# ---------------------------------------------------------------------------

def test_fit_pl_model_raises_on_non_convergence(monkeypatch):
    import scipy.optimize as optimize

    class FakeResult:
        success = False
        message = "synthetic non-convergence for test"
        x = np.zeros(2)

    monkeypatch.setattr(optimize, "minimize", lambda *a, **k: FakeResult())
    features = np.random.RandomState(0).normal(size=(12, 2))
    ranks = np.tile([1, 2, 3], 4)
    keys = [(f"d{i}", "p", 1) for i in range(4) for _ in range(3)]
    with pytest.raises(rd.NonConvergenceError):
        rd.fit_pl_model(features, ranks, keys, objective="pl_top3", l2=1.0)


def test_run_historical_stops_without_a_matching_ledger_registration(tmp_path, monkeypatch):
    # T81-rank-display-pl3-v1 IS registered in the real eval/experiments.jsonl
    # (Stage B was approved after this test was first written), so this test
    # must point run_historical at an isolated, tmp_path-only ledger that
    # carries no such row -- never the real production ledger -- to exercise
    # the "not registered at all" refusal path in isolation.
    ledger_path = tmp_path / "experiments.jsonl"
    ledger_path.write_text(json.dumps({"experiment_id": "some-other-experiment-v1"}) + "\n",
                           encoding="utf-8")
    monkeypatch.setattr(rd, "LEDGER_PATH", str(ledger_path))

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "implementation_sha256": {"backtest_rank_display.py": "deadbeef"},
        "ability_db_sha256": "deadbeef",
    }), encoding="utf-8")

    class Args:
        manifest = str(manifest_path)
        registration_id = "T81-rank-display-pl3-v1"
        db = rd.DB_PATH

    with pytest.raises(rd.RankDisplayError, match="no ledger registration"):
        rd.run_historical(Args())


def test_run_historical_stops_on_manifest_sha_mismatch(tmp_path, monkeypatch):
    ledger_path = tmp_path / "experiments.jsonl"
    ledger_path.write_text(json.dumps({"experiment_id": "T81-rank-display-pl3-v1"}) + "\n",
                           encoding="utf-8")
    monkeypatch.setattr(rd, "LEDGER_PATH", str(ledger_path))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "implementation_sha256": {"backtest_rank_display.py": "not-the-real-sha"},
        "ability_db_sha256": "irrelevant",
    }), encoding="utf-8")

    class Args:
        manifest = str(manifest_path)
        registration_id = "T81-rank-display-pl3-v1"
        db = rd.DB_PATH

    with pytest.raises(rd.RankDisplayError, match="sha256 no longer matches"):
        rd.run_historical(Args())


def test_run_historical_stops_on_ledger_manifest_design_mismatch(tmp_path, monkeypatch):
    harness_sha = rd.sha256_file(os.path.abspath(rd.__file__))
    db_sha = rd.sha256_file(rd.DB_PATH)
    ledger_path = tmp_path / "experiments.jsonl"
    # Ledger row exists and its own hashes look self-consistent, but it
    # points at a *different* ability_db snapshot than the manifest does --
    # SPEC SS11's "design match" check, not just "a row with this id exists".
    ledger_path.write_text(json.dumps({
        "experiment_id": "T81-rank-display-pl3-v1",
        "data_hashes": {
            "ability_db": "sha256:some-other-snapshot",
            "harness_source": f"sha256:{harness_sha}",
        },
        "primary_metric": "race_mean_spearman_delta_pl3_minus_market_2025",
        "candidate_count": 6,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(rd, "LEDGER_PATH", str(ledger_path))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "implementation_sha256": {"backtest_rank_display.py": harness_sha},
        "ability_db_sha256": db_sha,
    }), encoding="utf-8")

    class Args:
        manifest = str(manifest_path)
        registration_id = "T81-rank-display-pl3-v1"
        db = rd.DB_PATH

    with pytest.raises(rd.RankDisplayError, match="data_hashes.ability_db"):
        rd.run_historical(Args())


def test_isolated_pedigree_adapter_never_touches_network(monkeypatch, tmp_path):
    import pedigree_store

    cache_path = tmp_path / "pedigree_cache.json"
    cache_path.write_text(json.dumps({"H1": {"sire": "SireA", "bms": "BmsA"}}),
                          encoding="utf-8")

    def _forbidden_network_call(*args, **kwargs):
        raise AssertionError("pedigree_store must never open a DB connection "
                             "while the T81 isolated adapter is active")

    monkeypatch.setattr(pedigree_store, "_get_conn", _forbidden_network_call)
    with rd.isolated_pedigree_adapter(str(cache_path)) as loaded:
        assert loaded == {"H1": {"sire": "SireA", "bms": "BmsA"}}
        # Calling load_all() through the patched reference must not touch
        # pedigree_store._get_conn (patched above to fail loudly if it does).
        assert pedigree_store.load_all() == loaded
        with pytest.raises(rd.QualityError):
            pedigree_store.load_all(refresh=True)
    # Outside the context, the original production function is restored.
    assert pedigree_store.load_all is not None


def test_validate_pedigree_cache_fails_closed_on_missing_or_invalid_file(tmp_path):
    with pytest.raises(rd.QualityError):
        rd.validate_pedigree_cache(str(tmp_path / "does_not_exist.json"))

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(rd.QualityError):
        rd.validate_pedigree_cache(str(bad))

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(rd.QualityError):
        rd.validate_pedigree_cache(str(not_an_object))


def test_readonly_connection_rejects_writes(tmp_path):
    db_path = tmp_path / "probe.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE runs (x INTEGER)")
    conn.commit()
    conn.close()

    ro_conn = rd.open_readonly_connection(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO runs VALUES (1)")
        # assert_connection_is_readonly must accept a genuinely read-only
        # connection without raising.
        rd.assert_connection_is_readonly(ro_conn)
    finally:
        ro_conn.close()


def test_assert_connection_is_readonly_detects_a_writable_connection(tmp_path):
    db_path = tmp_path / "writable.db"
    writable_conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(rd.QualityError):
            rd.assert_connection_is_readonly(writable_conn)
    finally:
        writable_conn.close()


# ---------------------------------------------------------------------------
# 12. Synthetic smoke re-run reproducibility (<=1e-10), deterministic payload
# ---------------------------------------------------------------------------

def _flatten(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v, f"{prefix}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, value


def test_smoke_rerun_matches_within_tight_tolerance():
    class Args:
        pass

    result_a = rd.run_smoke(Args())
    result_b = rd.run_smoke(Args())
    flat_a = dict(_flatten(result_a))
    flat_b = dict(_flatten(result_b))
    assert set(flat_a) == set(flat_b)
    for key, value_a in flat_a.items():
        value_b = flat_b[key]
        if isinstance(value_a, (int, float)) and isinstance(value_b, (int, float)):
            assert abs(value_a - value_b) <= 1e-10, key
        else:
            assert value_a == value_b, key


# ---------------------------------------------------------------------------
# 13. Machine judgment: quality / direction / CI fixtures (SPEC SS9)
# ---------------------------------------------------------------------------

def _period_comparison(spearman_market_point, spearman_market_ci_low,
                       spearman_top1_point, spearman_top1_ci_low,
                       recall_point=0.0, mae_point=0.0):
    def diff(point, ci_low=None):
        return {"observed_difference": point,
               "ci_low": ci_low if ci_low is not None else point - 1.0,
               "ci_high": point + 1.0, "p_value": 0.5}

    return rd.PeriodComparison(
        period="x", n_races=10,
        spearman_vs_market=diff(spearman_market_point, spearman_market_ci_low),
        spearman_vs_top1=diff(spearman_top1_point, spearman_top1_ci_low),
        top3_recall_vs_market=diff(recall_point),
        top3_recall_vs_top1=diff(recall_point),
        top3_mae_vs_market=diff(mae_point),
        top3_mae_vs_top1=diff(mae_point),
        winner_reference={},
    )


def test_adjudicate_invalid_overrides_everything():
    comparisons = {
        "2025": _period_comparison(0.5, 0.1, 0.5, 0.1),
        "2026H1": _period_comparison(0.5, 0.1, 0.5, 0.1),
    }
    assert rd.adjudicate(comparisons, quality_ok=False) == "INVALID"


def test_adjudicate_pass_when_all_points_and_2025_ci_are_positive():
    comparisons = {
        "2025": _period_comparison(0.3, 0.1, 0.2, 0.05, recall_point=0.0, mae_point=0.0),
        "2026H1": _period_comparison(0.3, -0.1, 0.2, -0.05, recall_point=0.0, mae_point=0.0),
    }
    assert rd.adjudicate(comparisons, quality_ok=True) == "HISTORICAL_SCREEN_PASS"


def test_adjudicate_fail_when_a_point_direction_is_wrong():
    comparisons = {
        "2025": _period_comparison(0.3, 0.1, -0.1, -0.2),  # spearman_vs_top1 <= 0
        "2026H1": _period_comparison(0.3, -0.1, 0.2, -0.05),
    }
    assert rd.adjudicate(comparisons, quality_ok=True) == "HISTORICAL_SCREEN_FAIL"


def test_adjudicate_inconclusive_when_2025_ci_crosses_zero():
    comparisons = {
        # point estimates all favourable, but the 2025 market-spearman CI
        # includes 0 (ci_low <= 0) -> must not be treated as a PASS.
        "2025": _period_comparison(0.3, -0.05, 0.2, 0.05),
        "2026H1": _period_comparison(0.3, -0.1, 0.2, -0.05),
    }
    assert rd.adjudicate(comparisons, quality_ok=True) == "INCONCLUSIVE"


def test_adjudicate_top3_recall_or_mae_violation_fails():
    comparisons = {
        "2025": _period_comparison(0.3, 0.1, 0.2, 0.05, recall_point=-0.01, mae_point=0.0),
        "2026H1": _period_comparison(0.3, -0.1, 0.2, -0.05),
    }
    assert rd.adjudicate(comparisons, quality_ok=True) == "HISTORICAL_SCREEN_FAIL"
