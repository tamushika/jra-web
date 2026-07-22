import hashlib

import numpy as np
import pytest

import backtest_t54a as t54a


def _row(date, race, horse, c4, total=10, umaban=1):
    return {"date": date, "place": "東京", "r": race, "horse": horse,
            "c4": c4, "total_horses": total, "umaban": umaban}


def test_style_is_strictly_asof_and_excludes_same_day_and_future():
    runs = [
        _row("20240101", 1, "A", 2), _row("20240201", 1, "A", 4),
        _row("20240301", 1, "A", 10), _row("20240301", 9, "A", 9),
        _row("20240401", 1, "A", 8),
    ]
    target = [_row("20240301", 9, "A", 9)]
    matrix = t54a.compute_style_matrix(runs, target)
    assert matrix[0, 0] == pytest.approx((0.2 + 0.4) / 2)
    assert matrix[0, 3] == 1.0


def test_pressure_includes_each_runner_and_uses_fallback_zero():
    histories = [
        _row("20240101", 1, "A", 2), _row("20240201", 1, "A", 4),
        _row("20240101", 1, "B", 8, umaban=2), _row("20240201", 1, "B", 6, umaban=2),
    ]
    targets = [_row("20240301", 9, "A", 3), _row("20240301", 9, "B", 7, umaban=2),
               _row("20240301", 9, "C", 5, umaban=3)]
    matrix = t54a.compute_style_matrix(histories + targets, targets)
    expected = (0.3 + 0.7 + 0.0) / 3
    assert np.allclose(matrix[:, 1], expected)
    assert matrix[2].tolist() == pytest.approx([0.0, expected, 0.0, 0.0])
    assert matrix[0, 2] == pytest.approx(0.3 * expected)


def test_fewer_than_two_valid_histories_falls_back():
    runs = [_row("20240101", 1, "A", 2), _row("20240201", 1, "A", None)]
    matrix = t54a.compute_style_matrix(runs, [_row("20240301", 9, "A", 4)])
    assert matrix[0, [0, 2, 3]].tolist() == [0.0, 0.0, 0.0]


def test_stage0_correlation_is_diagnostic_only():
    runs = []
    for i, c4 in enumerate((2, 4, 6), 1):
        horse = f"H{i}"
        runs.extend([_row("20230101", 1, horse, c4), _row("20230201", 1, horse, c4),
                     _row("20240101", 9, horse, c4, umaban=i)])
    result = t54a.stage0_diagnostic(runs)
    assert result["style_realized_c4_pearson"] == pytest.approx(1.0)
    assert result["selection_guard"] == "diagnostic_only_not_used_for_candidate_selection"


def test_pack_off_is_byte_identical_and_pack_on_has_four_columns():
    base = np.arange(12, dtype=float).reshape(3, 4)
    style = np.ones((3, 4))
    disabled = t54a.model_matrix(base, style, False)
    assert disabled is base
    assert hashlib.sha256(disabled.tobytes()).digest() == hashlib.sha256(base.tobytes()).digest()
    assert t54a.model_matrix(base, style, True).shape == (3, 8)


def test_evaluation_gate_fails_before_registration(tmp_path):
    db = tmp_path / "ability.db"
    ledger = tmp_path / "experiments.jsonl"
    db.write_bytes(b"fixture")
    ledger.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        t54a.require_registration(ledger, db)


def test_stage0_value_cannot_change_candidate_choice():
    rows = {
        ("20250101", "東京", 9): {
            "top3_logloss": 0.4, "top3_brier": 0.1,
            "distance_band": "1600_1999", "pace_pressure_band": "front_dense",
            "winner_style_band": "closer",
        }
    }
    assert t54a._aggregate_top3(rows) == {
        "race_count": 1, "logloss": 0.4, "brier": 0.1,
    }
    # Candidate selection reads only selection.models.model.logloss; the
    # Stage-0 correlation is not an argument to the fitting path.
    assert "stage0" not in t54a._fit_candidate.__code__.co_varnames
