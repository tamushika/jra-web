import inspect
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import backtest_t42b_features as t42b


def make_training_db(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE race_id_map(race_id TEXT,date8 TEXT,place TEXT,race_no INTEGER);
        CREATE TABLE race_training_rows(
          race_id TEXT,horse_name TEXT,row_index INTEGER,training_date TEXT,
          course_norm TEXT,times_json TEXT,lap_count INTEGER,intensity_norm TEXT);
        """)
        connection.execute("INSERT INTO race_id_map VALUES('r1','20240110','東京',9)")
        rows = [
            ("r1", "A", 1, "2024-01-07", "美浦坂路", "[52,38,24,12]", 4, "強め"),
            ("r1", "A", 2, "2024-01-03", "美浦坂路", "[54,40,26,13]", 4, "馬也"),
            ("r1", "B", 1, None, "", "[]", 0, ""),
        ]
        connection.executemany("INSERT INTO race_training_rows VALUES(?,?,?,?,?,?,?,?)", rows)


def test_fixed_contract_is_exactly_seven_configs_with_flags():
    assert len(t42b.CONFIGS) == 7
    assert list(t42b.CONFIGS) == [
        "baseline+F1_final_time_z", "baseline+F2_finish_bite",
        "baseline+F3_workout_count", "baseline+F4_days_since_final",
        "baseline+F5_intensity_share", "baseline+F6_zero_workout",
        "baseline+all6",
    ]
    assert len(t42b.ALL_TRAINING_COLUMNS) == 12
    assert all(t42b.ALL_TRAINING_COLUMNS[index * 2 + 1].endswith("_missing")
               for index in range(6))


def test_feature_calculation_is_deterministic_and_preserves_zero_workout(tmp_path):
    db = tmp_path / "training.sqlite"
    make_training_db(db)
    first = t42b.load_preliminary(db)
    second = t42b.load_preliminary(db)
    assert first == second
    a = first[("20240110", "東京", 9, "A")]
    assert (a.workout_count, a.days_since_final, a.intensity_share,
            a.zero_workout, a.main_time, a.finish_bite) == (2, 3, .5, 0.0, 52.0, -1.0)
    b = first[("20240110", "東京", 9, "B")]
    assert b.has_rows and b.workout_count == 0 and b.zero_workout == 1.0


def test_matrix_missing_flags_and_train_only_imputation(tmp_path):
    db = tmp_path / "training.sqlite"
    make_training_db(db)
    preliminary = t42b.load_preliminary(db)
    keys = [("20240110", "東京", 9)] * 3
    meta = [{"horse": "A"}, {"horse": "B"}, {"horse": "C"}]
    raw, audit = t42b.compute_training_matrix(preliminary, keys, meta)
    assert audit["race_horse_unmatched"] == 1
    assert raw[0, 0] == 0.0 and raw[0, 1] == 0.0
    assert raw[0, 2] == -1.0 and raw[0, 3] == 0.0
    assert raw[1, 4] == 0.0 and raw[1, 5] == 0.0
    assert raw[1, 10] == 1.0 and raw[1, 11] == 0.0
    assert np.all(raw[2, 1::2] == 1.0)
    # Supply one fully available training row so mean/median are defined.
    train = np.vstack((raw[0], raw[0]))
    values = t42b.fit_imputation(train, np.asarray([True, True]))
    filled = t42b.apply_imputation(raw, values)
    assert np.all(np.isfinite(filled))
    assert filled[2, 0] == 0.0 and filled[2, 2] == -1.0 and filled[2, 6] == 3.0


def test_config_matrix_adds_only_feature_and_companion_flag():
    base = np.ones((2, len(t42b.FEATURES)))
    training = np.arange(24, dtype=float).reshape(2, 12)
    single, names = t42b.config_matrix(base, training, "baseline+F3_workout_count")
    assert single.shape == (2, len(t42b.FEATURES) + 2)
    assert names[-2:] == ["F3_workout_count", "F3_workout_count_missing"]
    all6, all_names = t42b.config_matrix(base, training, "baseline+all6")
    assert all6.shape == (2, len(t42b.FEATURES) + 12)
    assert all_names[-12:] == list(t42b.ALL_TRAINING_COLUMNS)
    baseline, baseline_names = t42b.config_matrix(base, training, "baseline")
    assert baseline is base and baseline_names == list(t42b.FEATURES)


def test_registration_and_all_four_sealed_hashes_match():
    registration = t42b.require_registration()
    assert registration["experiment_id"] == t42b.EXPERIMENT_ID
    assert registration["candidate_count"] == 7
    assert registration["data_hashes"] == {
        "ability_db_sha256": t42b.ABILITY_SHA256,
        "spec_sha256": t42b.SPEC_SHA256,
        "t42a_manifest_db_sha256": t42b.MANIFEST_SHA256,
        "t42b_structured_db_sha256": t42b.TRAINING_DB_SHA256,
    }


def test_registration_fails_closed_on_input_mutation(tmp_path):
    mutated = tmp_path / "ability.db"
    mutated.write_bytes(b"mutated")
    with pytest.raises(RuntimeError, match="sealed input SHA mismatch"):
        t42b.require_registration(db_path=mutated)


def test_stage_b_loader_never_reads_horse_training_rows():
    assert "horse_training_rows" not in inspect.getsource(t42b.load_preliminary)


def test_output_writer_is_deterministic(tmp_path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    payload = {"z": 1, "a": [2, 3]}
    assert t42b.write_json(payload, first) == t42b.write_json(payload, second)
    assert first.read_bytes() == second.read_bytes()
