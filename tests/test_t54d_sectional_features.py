import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import backtest_t54d_sectional_features as t54d


def _row(date, horse="H", race=1, umaban=1):
    return {
        "date": date, "place": "東京", "r": race, "umaban": umaban,
        "horse": horse,
    }


def test_asof_is_date_atomic_and_target_result_cannot_leak():
    histories = {
        "H": [
            {"date": "20240101", "key": ("20240101", "東京", 1, 1),
             "early": 0.2, "late_gain": -0.1, "measured_fit": 0.01},
            {"date": "20240201", "key": ("20240201", "東京", 1, 1),
             "early": 0.4, "late_gain": 0.1, "measured_fit": 0.03},
            {"date": "20240301", "key": ("20240301", "東京", 1, 1),
             "early": 0.9, "late_gain": 0.8, "measured_fit": 9.0},
        ]
    }
    targets = [_row("20240301"), _row("20240302")]
    matrix = t54d.compute_sectional_matrix(histories, targets)
    assert matrix[0, :3].tolist() == pytest.approx([0.3, 0.0, 0.02])
    assert matrix[1, :3].tolist() == pytest.approx([0.5, 0.2666666667, 3.0133333333])


def test_same_day_other_race_is_excluded_from_history():
    histories = {
        "H": [
            {"date": "20240101", "key": ("20240101", "東京", 1, 1),
             "early": 0.2, "late_gain": 0.0, "measured_fit": 0.0},
            {"date": "20240201", "key": ("20240201", "東京", 1, 1),
             "early": 0.4, "late_gain": 0.0, "measured_fit": 0.0},
            {"date": "20240301", "key": ("20240301", "東京", 1, 1),
             "early": 1.0, "late_gain": 1.0, "measured_fit": 1.0},
        ]
    }
    first = t54d.compute_sectional_matrix(histories, [_row("20240301", race=12)])
    assert first[0, :3].tolist() == pytest.approx([0.3, 0.0, 0.0])


def test_availability_requires_two_valid_prior_sectionals():
    histories = {
        "one": [{"date": "20240101", "key": ("20240101", "東京", 1, 1),
                 "early": 0.2, "late_gain": 0.0, "measured_fit": 0.0}],
        "two": [
            {"date": "20240101", "key": ("20240101", "東京", 1, 2),
             "early": 0.2, "late_gain": 0.0, "measured_fit": 0.0},
            {"date": "20240201", "key": ("20240201", "東京", 1, 2),
             "early": 0.4, "late_gain": 0.0, "measured_fit": 0.0},
        ],
    }
    matrix = t54d.compute_sectional_matrix(
        histories, [_row("20240301", "one", umaban=1),
                    _row("20240301", "two", umaban=2)]
    )
    assert matrix[:, 4].tolist() == [0.0, 1.0]
    assert matrix[0, :3].tolist() == [0.0, 0.0, 0.0]
    assert matrix[0, 3] == pytest.approx((0.0 + 0.7) / 2.0)


def test_pace_normalization_is_date_atomic():
    rows = []
    for index in range(8):
        rows.append({
            **_row("20240101", horse=f"H{index}", race=index + 1),
            "track_type": "芝", "distance": 1600,
            "lap_sequence_json": "[12,12,12,12,12,12,12,12]",
        })
    target = {
        **_row("20240201", race=9), "track_type": "芝", "distance": 1600,
        "lap_sequence_json": "[10,10,10,10,10,10,10,10]",
    }
    same_day = {
        **_row("20240201", race=10), "track_type": "芝", "distance": 1600,
        "lap_sequence_json": "[20,20,20,20,20,20,20,20]",
    }
    pace = t54d.build_date_atomic_pace_map(rows + [target, same_day])
    assert pace[("20240201", "東京", 9)] == pytest.approx(1.2)
    assert pace[("20240201", "東京", 10)] == pytest.approx(0.6)


def test_empty_corner_sequence_is_not_a_history():
    row = {
        **_row("20240101"), "rank": 1, "total_horses": 10,
        "track_type": "芝", "distance": 1600, "agari": 34.0,
        "corner_positions_json": "[]",
        "lap_sequence_json": "[12,12,12,12,12,12,12,12]",
    }
    histories, diagnostics = t54d.build_sectional_history([row])
    assert histories == {}
    assert diagnostics["skipped"]["empty_corner_sequence"] == 1


def test_model_pack_off_is_same_object_and_byte_identical():
    base = np.arange(12.0).reshape(3, 4)
    result = t54d.model_matrix(base, None)
    assert result is base
    assert result.tobytes() == base.tobytes()


def test_model_pack_rejects_wrong_width():
    with pytest.raises(ValueError, match="five aligned"):
        t54d.model_matrix(np.zeros((2, 3)), np.zeros((2, 4)))


def test_registration_gate_fails_before_evaluation(tmp_path, monkeypatch):
    db = tmp_path / "ability.db"
    db.write_bytes(b"sealed")
    ledger = tmp_path / "experiments.jsonl"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setattr(t54d, "ABILITY_DB_SHA256", t54d.sha256_file(db))
    with pytest.raises(RuntimeError, match="register T54d-sectional-lap-features-v1"):
        t54d.require_registration(ledger, db)


def test_registration_must_seal_hash_and_exact_six_candidates(tmp_path, monkeypatch):
    db = tmp_path / "ability.db"
    db.write_bytes(b"sealed")
    digest = t54d.sha256_file(db)
    monkeypatch.setattr(t54d, "ABILITY_DB_SHA256", digest)
    ledger = tmp_path / "experiments.jsonl"
    base = {
        "experiment_id": t54d.EXPERIMENT_ID,
        "result_summary": None,
        "data_hashes": {"ability_db_sha256": digest},
        "search_grid": {"configs": ["baseline", "sectional5"], "l2": [0.3, 1.0]},
    }
    ledger.write_text(json.dumps(base) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fixed six"):
        t54d.require_registration(ledger, db)
    base["search_grid"]["l2"].append(3.0)
    ledger.write_text(json.dumps(base) + "\n", encoding="utf-8")
    assert t54d.require_registration(ledger, db) == base


def test_readonly_loader_does_not_change_database(tmp_path):
    db = tmp_path / "ability.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
              date TEXT, place TEXT, r INTEGER, horse TEXT, umaban INTEGER,
              rank INTEGER, total_horses INTEGER, track_type TEXT,
              distance INTEGER, agari REAL
            );
            CREATE TABLE runner_corners (
              date TEXT, place TEXT, r INTEGER, umaban INTEGER,
              corner_positions_json TEXT
            );
            CREATE TABLE race_laps (
              date TEXT, place TEXT, r INTEGER, lap_sequence_json TEXT
            );
            INSERT INTO runs VALUES
              ('20240101','東京',1,'H',1,1,10,'芝',1600,34.0);
            INSERT INTO runner_corners VALUES
              ('20240101','東京',1,1,'[2,1]');
            INSERT INTO race_laps VALUES
              ('20240101','東京',1,'[12,12,12,12,12,12,12,12]');
            """
        )
    before = t54d.sha256_file(db)
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
        rows = t54d.load_sectional_rows(connection)
    assert len(rows) == 1
    assert t54d.sha256_file(db) == before

