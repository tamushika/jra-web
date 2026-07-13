from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
API_DIR = ROOT / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import scoring  # noqa: E402
from factor_snapshot import (build_snapshot_payload, load_factor_snapshot,
                             reset_snapshot_cache, validate_snapshot_payload,
                             write_factor_snapshot)  # noqa: E402
from gen_factor_snapshot import generate_snapshot


COURSE = ("東京", "芝", 1600)


def _row(entity="ALL", win_rate=10.0, show_rate=30.0):
    return {
        "entity": entity, "n1": 1, "n2": 1, "n3": 1, "out": 7,
        "starts": 10, "win_rate": win_rate, "quinella_rate": 20.0,
        "show_rate": show_rate, "win_roi": 80.0, "show_roi": 75.0,
    }


def _payload():
    return build_snapshot_payload(
        {
            COURSE: {
                "baseline": _row(),
                "jockey_w": {"J": _row("J", 20.0, 40.0)},
                "_meta": {"source": "ability.db:runs", "as_of": "20251231"},
            },
        },
        as_of="20251231",
        stats_from="20210101",
        generated_at="2026-01-01T00:00:00+09:00",
    )


def test_snapshot_round_trip_and_metadata(tmp_path):
    path = tmp_path / "factor_snapshot.json"
    write_factor_snapshot(path, _payload())
    reset_snapshot_cache()
    snapshot = load_factor_snapshot(path)
    assert snapshot["meta"]["schema_version"] == 1
    assert snapshot["meta"]["stats_from"] == "20210101"
    assert snapshot["meta"]["as_of"] == "20251231"
    assert snapshot["meta"]["course_count"] == 1
    assert snapshot["tables"][COURSE]["jockey_w"]["J"]["win_rate"] == 20.0


def test_invalid_snapshot_is_rejected_as_a_whole(tmp_path):
    payload = _payload()
    payload["courses"][0]["table"]["baseline"]["show_rate"] = None
    with pytest.raises(ValueError):
        validate_snapshot_payload(payload)

    path = tmp_path / "factor_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    reset_snapshot_cache()
    assert load_factor_snapshot(path) is None


def test_empty_or_partial_snapshot_is_rejected_as_a_whole(tmp_path):
    empty = _payload()
    empty["courses"] = []
    empty["_meta"]["course_count"] = 0
    with pytest.raises(ValueError, match="at least one course"):
        validate_snapshot_payload(empty)

    partial = build_snapshot_payload(
        {
            COURSE: {"baseline": _row()},
            ("中山", "芝", 1800): {"baseline": _row()},
        },
        as_of="20251231",
        stats_from="20210101",
        generated_at="2026-01-01T00:00:00+09:00",
    )
    partial["courses"].pop()
    with pytest.raises(ValueError, match="course_count"):
        validate_snapshot_payload(partial)

    path = tmp_path / "factor_snapshot.json"
    path.write_text(json.dumps(partial), encoding="utf-8")
    reset_snapshot_cache()
    assert load_factor_snapshot(path) is None


def test_invalid_nested_factor_stats_reject_the_whole_snapshot(tmp_path):
    payload = _payload()
    payload["courses"][0]["table"]["jockey_w"]["J"]["win_rate"] = "bad"
    with pytest.raises(ValueError, match="factor row is invalid"):
        validate_snapshot_payload(payload)

    path = tmp_path / "factor_snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    reset_snapshot_cache()
    assert load_factor_snapshot(path) is None


def test_scoring_missing_or_invalid_snapshot_falls_back_to_legacy_csv(
        tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(scoring, "FACTOR_SNAPSHOT_PATH", str(missing))
    reset_snapshot_cache()
    legacy = scoring.load_factor_table(*COURSE, None)
    assert legacy is not None
    assert legacy["baseline"]["starts"] > 0

    missing.write_text("{broken", encoding="utf-8")
    reset_snapshot_cache()
    invalid_fallback = scoring.load_factor_table(*COURSE, None)
    assert invalid_fallback == legacy


def test_scoring_uses_snapshot_as_single_authoritative_source(tmp_path, monkeypatch):
    path = tmp_path / "factor_snapshot.json"
    write_factor_snapshot(path, _payload())
    monkeypatch.setattr(scoring, "FACTOR_SNAPSHOT_PATH", str(path))
    reset_snapshot_cache()

    table = scoring.load_factor_table("東京", "芝", "1600", None)
    assert table["baseline"]["starts"] == 10
    assert table["jockey_w"]["J"]["win_rate"] == 20.0
    # A valid snapshot is not silently mixed with a legacy CSV when a course
    # is absent from the snapshot.
    assert scoring.load_factor_table("中山", "芝", 1600, None) is None


def test_generator_uses_fold_provider_and_writes_versioned_payload(
        tmp_path, monkeypatch):
    db = tmp_path / "ability.db"
    db.touch()
    output = tmp_path / "factor_snapshot.json"

    class Provider:
        def __init__(self, db_path, as_of, **kwargs):
            assert str(db_path) == str(db)
            assert kwargs["legacy_api_dir"]
            self.tables = {COURSE: {"baseline": _row()}}
            self.as_of = as_of
            self.stats_from = "20210101"

    monkeypatch.setattr("gen_factor_snapshot.FoldFactorTableProvider", Provider)
    monkeypatch.setattr(
        "gen_factor_snapshot.discover_legacy_courses", lambda _api_dir: {COURSE})
    payload = generate_snapshot(str(db), str(output), "20251231")
    assert output.is_file()
    assert payload["_meta"]["source"] == "ability.db:runs"
    assert payload["_meta"]["course_count"] == 1
    assert load_factor_snapshot(output)["tables"][COURSE]["baseline"]["starts"] == 10


def test_generator_rejects_future_cutoff_before_writing(tmp_path):
    db = tmp_path / "ability.db"
    db.touch()
    output = tmp_path / "factor_snapshot.json"
    with pytest.raises(ValueError, match="future"):
        generate_snapshot(str(db), str(output), "29991231")
    assert not output.exists()


@pytest.mark.parametrize(
    ("generated", "legacy", "message"),
    [
        ({COURSE}, {COURSE, ("中山", "芝", 1800)}, "missing"),
        ({COURSE, ("中山", "芝", 1800)}, {COURSE}, "extra"),
        ({}, set(), "legacy course universe is empty"),
    ],
)
def test_generator_rejects_incomplete_course_coverage_before_writing(
        tmp_path, monkeypatch, generated, legacy, message):
    db = tmp_path / "ability.db"
    db.touch()
    output = tmp_path / "factor_snapshot.json"

    class Provider:
        def __init__(self, _db_path, as_of, **_kwargs):
            self.tables = {
                course: {"baseline": _row()} for course in generated
            }
            self.as_of = as_of
            self.stats_from = "20210101"

    monkeypatch.setattr("gen_factor_snapshot.FoldFactorTableProvider", Provider)
    monkeypatch.setattr(
        "gen_factor_snapshot.discover_legacy_courses", lambda _api_dir: legacy)

    with pytest.raises(ValueError, match=message):
        generate_snapshot(str(db), str(output), "20251231")
    assert not output.exists()
