"""T41: low-cost feature pack (A3 weight / A7 rotation / A8 kinryo / X3 time-index).

Covers SPEC-T41 SS5:
  1. as-of health: current/future rows never influence a pack feature.
  2. boundary values: no-prior-race / layoff-return / missing weight / debut.
  3. kinryo_per_weight's fallback denominator rule.
  4. non-regression: build_dataset output is byte-identical whenever the pack
     is off, regardless of how "off" is spelled (omitted / None / empty set).

These tests exercise the pure per-row helpers directly (``_pack_feature_values``,
``_horse_layoff_states``) with synthetic horse histories so they do not depend
on the shipped factor-table/pedigree resource files.  The non-regression test
is the only one that goes through the full ``build_dataset`` pipeline against
a small slice of ability.db.
"""
import math
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import backtest_ml


ROOT = Path(__file__).parents[1]
ABILITY_DB = ROOT / "ability.db"


def _run(date, **overrides):
    row = {
        "date": date, "place": "東京", "r": 5, "race_name": "テスト特別",
        "race_class": "1勝クラス", "horse": "テストホース", "sex": "牡", "age": 4,
        "jockey": "A騎手", "kinryo": 55.0, "total_horses": 12, "umaban": 5,
        "popularity": 3, "rank": 3, "track_type": "芝", "distance": 1600,
        "condition": "良", "time_sec": None, "chakusa": None, "c4": None,
        "agari": None, "pci": None, "weight": 480, "affi": "美浦",
        "win_pay": None, "fukusho_pay": None,
    }
    row.update(overrides)
    return row


ALL_GROUPS = frozenset(backtest_ml.PACK_GROUP_ORDER)


# ---------------------------------------------------------------------------
# pack_feature_names / group wiring
# ---------------------------------------------------------------------------

def test_pack_feature_names_fixed_order_and_group_membership():
    assert backtest_ml.pack_feature_names(None) == []
    assert backtest_ml.pack_feature_names(set()) == []
    names = backtest_ml.pack_feature_names(ALL_GROUPS)
    assert names == (backtest_ml.A3_FEATURES + backtest_ml.A7_FEATURES
                     + backtest_ml.A8_FEATURES + backtest_ml.X3_FEATURES)
    # group order is fixed (A3->A7->A8->X3) regardless of set iteration order
    assert backtest_ml.pack_feature_names({"X3", "A3"}) == (
        backtest_ml.A3_FEATURES + backtest_ml.X3_FEATURES)


def test_build_dataset_rejects_unknown_pack_group():
    with pytest.raises(ValueError):
        backtest_ml.build_dataset([], "20250101", {}, pack_groups={"BOGUS"})


# ---------------------------------------------------------------------------
# as-of health: current/future rows must not leak into a pack feature
# ---------------------------------------------------------------------------

def test_asof_future_rows_do_not_change_current_row_pack_values():
    prefix = [
        _run("20250101", weight=470),
        _run("20250201", weight=480),
        _run("20250301", weight=460),
    ]
    cur = _run("20250401", weight=500)
    lst_no_future = prefix + [cur]
    # Same prefix + same evaluated row, but with extra rows appended AFTER it
    # (different horse-history "future").  Values at index len(prefix) must
    # be identical between the two lists.
    lst_with_future = prefix + [cur, _run("20250501", weight=1.0),
                                _run("20250601", weight=999.0)]

    layoff_a = backtest_ml._horse_layoff_states(lst_no_future)
    layoff_b = backtest_ml._horse_layoff_states(lst_with_future)
    i = len(prefix)
    values_a = backtest_ml._pack_feature_values(
        lst_no_future, i, ALL_GROUPS, use_variant=False,
        layoff_states=layoff_a, tfeat_current=0.0)
    values_b = backtest_ml._pack_feature_values(
        lst_with_future, i, ALL_GROUPS, use_variant=False,
        layoff_states=layoff_b, tfeat_current=0.0)
    assert values_a == values_b


def test_expanding_weight_median_excludes_current_row():
    # Past weights 460/470/480 (median 470). Current weight is an extreme
    # 900 that must not be able to pull the median it's compared against.
    prefix = [_run("20250101", weight=460), _run("20250201", weight=470),
             _run("20250301", weight=480)]
    lst = prefix + [_run("20250401", weight=900)]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 3, {"A3"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    # median=470, MAD=median(|460-470|,|470-470|,|480-470|)=10
    assert values["weight_dev_expanding"] == pytest.approx((900 - 470) / 10)
    assert values["weight_dev_expanding_missing_flag"] == 0.0


def test_starts_28d_does_not_count_a_future_start():
    lst = [
        _run("20250101"),          # 90 days before cur: outside both windows
        _run("20250320"),          # 12 days before cur (2025-04-01): counts
    ]
    cur = _run("20250401")
    lst = lst + [cur, _run("20250410")]  # future row after cur
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 2, {"A7"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["starts_28d"] == 1.0
    assert values["starts_56d"] == 1.0


# ---------------------------------------------------------------------------
# boundary values: no prior race / debut
# ---------------------------------------------------------------------------

def test_debut_row_has_zeroed_deltas_and_flags():
    lst = [_run("20250101", weight=480, kinryo=54.0)]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 0, ALL_GROUPS, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["weight_delta_prev"] == 0.0
    assert values["weight_delta_rate"] == 0.0
    assert values["weight_no_prior_flag"] == 1.0
    assert values["weight_delta_x_layoff"] == 0.0
    assert values["class_delta"] == 0.0
    assert values["dist_delta_log"] == 0.0
    assert values["surface_change"] == 0.0
    assert values["venue_change_transport_proxy"] == 0.0
    assert values["jockey_change"] == 0.0
    assert values["run_after_layoff"] == 0.0
    assert values["kinryo_delta_prev"] == 0.0
    # kinryo_per_weight still computable from today's own weight/kinryo
    assert values["kinryo_per_weight"] == pytest.approx(54.0 / 480)


def test_weight_dev_expanding_missing_flag_before_two_prior_weights():
    lst = [_run("20250101", weight=470), _run("20250201", weight=480)]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 1, {"A3"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["weight_dev_expanding"] == 0.0
    assert values["weight_dev_expanding_missing_flag"] == 1.0


# ---------------------------------------------------------------------------
# missing current-day weight -> prior-value fallback + flag
# ---------------------------------------------------------------------------

def test_missing_today_weight_falls_back_to_prior_weight():
    lst = [_run("20250101", weight=470), _run("20250201", weight=None, kinryo=56.0)]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 1, {"A3", "A8"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["weight_today_missing_flag"] == 1.0
    # effective weight falls back to the prior 470 -> delta collapses to 0
    assert values["weight_delta_prev"] == 0.0
    assert values["weight_delta_rate"] == 0.0
    # A8 kinryo_per_weight uses the same fallback denominator
    assert values["kinryo_per_weight"] == pytest.approx(56.0 / 470)


def test_kinryo_per_weight_is_zero_when_both_weights_are_missing():
    lst = [_run("20250101", weight=None), _run("20250201", weight=None, kinryo=57.0)]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 1, {"A8"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["kinryo_per_weight"] == 0.0


# ---------------------------------------------------------------------------
# run_after_layoff sequencing
# ---------------------------------------------------------------------------

def test_run_after_layoff_counts_and_resets_and_clips():
    dates = ["20240101", "20240108", "20240115",  # normal short-gap streak
             "20240701",                          # >=90d layoff return (1)
             "20240708", "20240715", "20240722", "20240729", "20240805",
             "20240812",                          # would be 7th -> clipped to 5
             "20250101"]                          # another >=90d layoff -> resets to 1
    lst = [_run(d) for d in dates]
    states = backtest_ml._horse_layoff_states(lst)
    assert states[0] == 0  # debut: no observed layoff yet
    assert states[1] == 0
    assert states[2] == 0
    assert states[3] == 1   # >=90 day gap from 20240115 -> return race
    assert states[4] == 2
    assert states[5] == 3
    assert states[6] == 4
    assert states[7] == 5
    assert states[8] == 5   # clipped
    assert states[9] == 5   # clipped
    assert states[10] == 1  # new >=90 day gap resets the streak


def test_tfeat_post_layoff_delta_only_fires_when_prior_race_was_the_return_race():
    dates = ["20240101", "20240701", "20240708"]
    lst = [_run(d) for d in dates]
    layoff = backtest_ml._horse_layoff_states(lst)
    assert layoff[1] == 1  # 20240701 is itself a layoff-return race

    calls = {}

    def fake_time_index(pr, use_variant):
        return calls[pr["date"]]

    calls["20240101"] = 1.0
    calls["20240701"] = 3.5
    calls["20240708"] = None

    orig = backtest_ml._time_index_value
    backtest_ml._time_index_value = fake_time_index
    try:
        values = backtest_ml._pack_feature_values(
            lst, 2, {"X3"}, use_variant=False, layoff_states=layoff,
            tfeat_current=0.0)
    finally:
        backtest_ml._time_index_value = orig
    assert values["tfeat_post_layoff_delta"] == pytest.approx(3.5 - 1.0)


# ---------------------------------------------------------------------------
# X3 recency weighting / std / slope (index computation stubbed out so this
# does not depend on the shipped standard-time resource tables)
# ---------------------------------------------------------------------------

def test_x3_rwmean_std_slope_use_only_stubbed_index_values():
    dates = [f"2024010{n}" for n in range(1, 6)]  # 5 prior races
    lst = [_run(d) for d in dates] + [_run("20240201")]
    layoff = backtest_ml._horse_layoff_states(lst)

    # recency order (most recent first) values: 5,4,3,2,1
    stub_by_date = dict(zip(dates, [1.0, 2.0, 3.0, 4.0, 5.0]))

    def fake_time_index(pr, use_variant):
        return stub_by_date[pr["date"]]

    orig = backtest_ml._time_index_value
    backtest_ml._time_index_value = fake_time_index
    try:
        values = backtest_ml._pack_feature_values(
            lst, 5, {"X3"}, use_variant=False, layoff_states=layoff,
            tfeat_current=2.0)
    finally:
        backtest_ml._time_index_value = orig

    # most recent (j=0) is 20240105 -> value 5.0; j=1 is 20240104 -> value 4.0
    weights = [2.0 ** (-j / 2.0) for j in range(5)]
    expected_values = [5.0, 4.0, 3.0, 2.0, 1.0]
    expected_rwmean = (sum(w * v for w, v in zip(weights, expected_values))
                       / sum(weights))
    assert values["tfeat_rwmean"] == pytest.approx(expected_rwmean)
    assert values["tfeat_slope2"] == pytest.approx(5.0 - 4.0)
    assert values["tfeat_std_missing_flag"] == 0.0
    assert values["tfeat_gap_from_max"] == pytest.approx(expected_rwmean - 2.0)


def test_x3_std_missing_flag_when_fewer_than_two_valid_values():
    lst = [_run("20240101"), _run("20240201")]

    def fake_time_index(pr, use_variant):
        return 3.0 if pr["date"] == "20240101" else None

    layoff = backtest_ml._horse_layoff_states(lst)
    orig = backtest_ml._time_index_value
    backtest_ml._time_index_value = fake_time_index
    try:
        values = backtest_ml._pack_feature_values(
            lst, 1, {"X3"}, use_variant=False, layoff_states=layoff,
            tfeat_current=0.0)
    finally:
        backtest_ml._time_index_value = orig
    assert values["tfeat_std"] == 0.0
    assert values["tfeat_std_missing_flag"] == 1.0


# ---------------------------------------------------------------------------
# A7 class/surface/venue/jockey changes
# ---------------------------------------------------------------------------

def test_class_delta_sign_and_change_flags():
    prev = _run("20250101", race_class="1勝クラス", track_type="芝",
               place="東京", jockey="A騎手", distance=1600)
    cur = _run("20250201", race_class="2勝クラス", track_type="ダート",
              place="中山", jockey="B騎手", distance=1800)
    lst = [prev, cur]
    layoff = backtest_ml._horse_layoff_states(lst)
    values = backtest_ml._pack_feature_values(
        lst, 1, {"A7"}, use_variant=False, layoff_states=layoff,
        tfeat_current=0.0)
    assert values["class_delta"] == pytest.approx(40 - 30)  # promotion
    assert values["surface_change"] == 1.0
    assert values["venue_change_transport_proxy"] == 1.0
    assert values["jockey_change"] == 1.0
    assert values["dist_delta_log"] == pytest.approx(math.log(1800 / 1600))

    # demotion + no change in surface/venue/jockey
    prev2 = _run("20250101", race_class="2勝クラス", track_type="芝",
                place="東京", jockey="A騎手")
    cur2 = _run("20250201", race_class="1勝クラス", track_type="芝",
               place="東京", jockey="A騎手")
    lst2 = [prev2, cur2]
    layoff2 = backtest_ml._horse_layoff_states(lst2)
    values2 = backtest_ml._pack_feature_values(
        lst2, 1, {"A7"}, use_variant=False, layoff_states=layoff2,
        tfeat_current=0.0)
    assert values2["class_delta"] == pytest.approx(30 - 40)
    assert values2["surface_change"] == 0.0
    assert values2["venue_change_transport_proxy"] == 0.0
    assert values2["jockey_change"] == 0.0


# ---------------------------------------------------------------------------
# A8 handicap marker
# ---------------------------------------------------------------------------

def test_handicap_flag_from_race_name_marker():
    prev = _run("20250101", kinryo=55.0)
    cur_handicap = _run("20250201", kinryo=57.0, race_name="函館記念ＨG3")
    cur_plain = _run("20250201", kinryo=57.0, race_name="有馬記念G1")

    lst_h = [prev, cur_handicap]
    layoff_h = backtest_ml._horse_layoff_states(lst_h)
    values_h = backtest_ml._pack_feature_values(
        lst_h, 1, {"A8"}, use_variant=False, layoff_states=layoff_h,
        tfeat_current=0.0)
    assert values_h["kinryo_delta_x_handicap"] == pytest.approx(2.0)

    lst_p = [prev, cur_plain]
    layoff_p = backtest_ml._horse_layoff_states(lst_p)
    values_p = backtest_ml._pack_feature_values(
        lst_p, 1, {"A8"}, use_variant=False, layoff_states=layoff_p,
        tfeat_current=0.0)
    assert values_p["kinryo_delta_x_handicap"] == 0.0


def test_handicap_marker_is_positional_not_substring():
    marker = backtest_ml.HANDICAP_RACE_NAME_MARKER_RE
    # marker positions: before grade / (L) / class separator / distance / end
    for name in ("小倉大賞ＨG3", "仁川ＳＨ(L)", "早春ＳＨ･3勝", "いわきＨ1000",
                 "ＨＢＣＨ900", "ＴＶｈＨ･3勝", "しらかばＨ"):
        assert marker.search(name), name
    # FW-H as part of the race name (broadcaster call letters) is NOT a marker
    for name in ("ＮＨＫマG1", "ＮＨＫ杯G2", "ＨＢＣ賞1000", "ＨＴＢ杯1000",
                 "ＵＨＢ杯", "ＨＴＢ賞･2勝", "有馬記念G1"):
        assert not marker.search(name), name


# ---------------------------------------------------------------------------
# Non-regression: pack OFF must be byte-identical to the pre-T41 output,
# regardless of how "off" is spelled.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not ABILITY_DB.exists(), reason="ability.db not present")
def test_build_dataset_pack_off_is_identical_across_call_styles():
    from backtest_win5 import load_win5_cfg
    from backtest_ability import load_runs

    with sqlite3.connect(ABILITY_DB) as conn:
        row = conn.execute(
            "SELECT date FROM runs WHERE rank IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        date_to = row[0]
        date_from = date_to[:6] + "01"  # first day of the latest loaded month
        runs = load_runs(conn, date_from, date_to)
    cfg = load_win5_cfg()

    x_default, y_default, keys_default, _meta_default = backtest_ml.build_dataset(
        runs, date_from, cfg)
    x_none, y_none, keys_none, _meta_none = backtest_ml.build_dataset(
        runs, date_from, cfg, pack_groups=None)
    x_empty, y_empty, keys_empty, _meta_empty = backtest_ml.build_dataset(
        runs, date_from, cfg, pack_groups=frozenset())

    assert x_default.shape[1] == len(backtest_ml.FEATURES) == 21
    assert np.array_equal(x_default, x_none)
    assert np.array_equal(x_default, x_empty)
    assert np.array_equal(y_default, y_none)
    assert keys_default == keys_none == keys_empty
