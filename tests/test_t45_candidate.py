import json

import numpy as np
import pytest

import backtest_t45_candidate as subject


def test_validate_output_dir_rejects_production_directory():
    with pytest.raises(ValueError, match="production"):
        subject.validate_output_dir(subject.PRODUCTION_MODEL.parent)


def test_model_document_is_deterministic(tmp_path):
    class Scaler:
        mean_ = np.array([1.0, 2.0])
        scale_ = np.array([0.5, 4.0])

    document = subject.model_document(
        Scaler(), np.array([0.25, -0.5]), 1.2, 10, "abc", "20260704"
    )
    first = subject.write_json(tmp_path / "first.json", document)
    second = subject.write_json(tmp_path / "second.json", document)
    assert first == second
    assert json.loads((tmp_path / "first.json").read_text(encoding="utf-8"))[
        "meta"
    ]["candidate"] == "T45-ability-asof-v1"


def test_align_to_reference_reorders_by_runner_identity():
    keys = [("20250101", "東京", 1), ("20250101", "東京", 1)]
    reference_meta = [{"umaban": 1, "horse": "A"}, {"umaban": 2, "horse": "B"}]
    other = (
        np.array([[20.0], [10.0]]),
        np.array([0, 1]),
        list(reversed(keys)),
        list(reversed(reference_meta)),
    )
    features, labels, aligned_keys, aligned_meta = subject.align_to_reference(
        keys, reference_meta, other
    )
    assert features[:, 0].tolist() == [10.0, 20.0]
    assert labels.tolist() == [1, 0]
    assert aligned_keys == keys
    assert aligned_meta == reference_meta
