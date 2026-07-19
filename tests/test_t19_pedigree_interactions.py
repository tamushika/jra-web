import hashlib

import numpy as np

import backtest_pedigree_interactions as t19


def _meta(date="20240101", condition="良"):
    return [{"date": date, "place": "東京", "r": 1, "horse": "Target",
             "track_type": "芝", "distance": 1600, "condition": condition}]


def test_asof_excludes_current_and_future_outcomes():
    pedigree = {"Target": {"sire": "S", "bms": "B"},
                "Past": {"sire": "S", "bms": "B"},
                "Future": {"sire": "S", "bms": "B"}}
    past = {"date": "20231231", "horse": "Past", "rank": 1,
            "track_type": "芝", "distance": 1600, "condition": "良"}
    future = {"date": "20240101", "horse": "Future", "rank": 18,
              "track_type": "芝", "distance": 1600, "condition": "良"}
    before = t19.build_pedigree_sufficient(_meta(), [past], pedigree)
    after = t19.build_pedigree_sufficient(_meta(), [past, future], pedigree)
    np.testing.assert_array_equal(before, after)


def test_shrinkage_weight_is_monotone_and_zero_at_n_zero():
    raw = np.zeros((3, 10))
    raw[:, 0] = 0.2
    raw[:, 1] = [0, 10, 100]
    values = t19.shrink_features(raw, 50)[:, 0]
    assert values[0] == 0.0
    assert 0 < values[1] < values[2] < 0.2


def test_wet_feature_is_identically_zero_on_good_track():
    pedigree = {"Target": {"sire": "S", "bms": "B"},
                "Past": {"sire": "S", "bms": "B"}}
    history = [{"date": "20231231", "horse": "Past", "rank": 1,
                "track_type": "芝", "distance": 1600, "condition": "重"}]
    raw = t19.build_pedigree_sufficient(_meta(condition="良"), history, pedigree)
    assert t19.shrink_features(raw, 50)[0, 3] == 0.0
    assert raw[0, 9] == 0.0


def test_pack_off_keeps_baseline_sha_identical():
    baseline = np.arange(42, dtype=np.float64).reshape(2, 21)
    raw = np.ones((2, 10), dtype=np.float64)
    before = hashlib.sha256(baseline.tobytes()).hexdigest()
    off = t19.candidate_features(baseline, raw, None)
    assert hashlib.sha256(off.tobytes()).hexdigest() == before
    assert off.shape == (2, len(t19.FEATURES))
