import math

from backtest_m4a import (
    CalibrationModel,
    assert_same_rank,
    calibrate_probabilities,
    fit_calibration_map,
    load_calibration_model,
    save_calibration_model,
)


def _synthetic_map():
    probabilities = [
        0.01, 0.015, 0.02, 0.03,
        0.05, 0.06, 0.08, 0.10,
        0.15, 0.18, 0.25, 0.40,
    ]
    outcomes = [0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1, 1]
    return fit_calibration_map(
        probabilities, outcomes, min_bin_size=3, max_bins=4
    )


def test_monotone_calibration_preserves_popularity_rank():
    calibration_map = _synthetic_map()
    raw = [0.42, 0.25, 0.16, 0.09, 0.05, 0.03]
    calibrated = calibrate_probabilities(raw, calibration_map)

    assert_same_rank(raw, calibrated)
    assert all(a > b for a, b in zip(calibrated, calibrated[1:]))
    assert all(a <= b for a, b in zip(
        calibration_map.corrected_scores,
        calibration_map.corrected_scores[1:],
    ))


def test_calibrated_probabilities_sum_to_one():
    calibrated = calibrate_probabilities(
        [0.50, 0.20, 0.12, 0.08, 0.06, 0.04], _synthetic_map()
    )

    assert math.isclose(sum(calibrated), 1.0, rel_tol=0.0, abs_tol=1e-12)
    assert all(0.0 < value < 1.0 for value in calibrated)


def test_calibration_model_save_load_round_trip(tmp_path):
    model = CalibrationModel(
        variant="V1",
        global_map=_synthetic_map(),
        segment_maps={"芝": _synthetic_map()},
        metadata={"training_period": {"from": "2021-01-01", "to": "2023-12-31"}},
    )
    path = tmp_path / "m4a.json"

    save_calibration_model(model, path)
    restored = load_calibration_model(path)

    assert restored.to_dict() == model.to_dict()
    raw = [0.50, 0.25, 0.15, 0.10]
    assert calibrate_probabilities(raw, restored.global_map) == \
        calibrate_probabilities(raw, model.global_map)
