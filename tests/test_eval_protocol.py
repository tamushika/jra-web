import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import pytest

from eval.blocks import (group_by_race_date, paired_block_bootstrap,
                         race_date_block)
from eval.cutoff import (SnapshotSelection, select_snapshots,
                         win5_cutoff)
from eval.ledger import (LedgerIntegrityError, LedgerValidationError,
                         append_experiment, validate_experiment,
                         verify_ledger)


def _snapshot_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE odds_snapshots (
            odds_snapshot_id INTEGER PRIMARY KEY,
            race_id TEXT NOT NULL,
            horse_id TEXT NOT NULL,
            observed_at TEXT,
            win_odds REAL,
            is_stale INTEGER DEFAULT 0,
            data_quality_flags_json TEXT,
            stage TEXT
        )"""
    )
    return connection


def _add_snapshot(
    connection,
    row_id,
    race_id,
    horse_id,
    observed_at,
    odds,
    *,
    flags=(),
    stale=0,
    stage="30",
):
    connection.execute(
        """INSERT INTO odds_snapshots
           (odds_snapshot_id,race_id,horse_id,observed_at,win_odds,is_stale,
            data_quality_flags_json,stage) VALUES(?,?,?,?,?,?,?,?)""",
        (
            row_id,
            race_id,
            horse_id,
            observed_at,
            odds,
            stale,
            json.dumps(list(flags)),
            stage,
        ),
    )


def test_cutoff_boundary_missing_timestamp_and_latest_clean_selection():
    connection = _snapshot_connection()
    race_id = "20260718:東京:09"
    _add_snapshot(connection, 1, race_id, "h1", "2026-07-18T05:54:00Z", 4.0)
    _add_snapshot(connection, 2, race_id, "h1", "2026-07-18T05:55:00Z", 3.8)
    _add_snapshot(connection, 3, race_id, "h1", "2026-07-18T05:55:00.001Z", 3.7)
    _add_snapshot(connection, 4, race_id, "h2", None, 5.0)
    _add_snapshot(connection, 5, race_id, "h3", "2026-07-18T05:53:00Z", 7.0)
    _add_snapshot(
        connection,
        6,
        race_id,
        "h3",
        "2026-07-18T05:54:30Z",
        6.5,
        flags=("insufficient_odds",),
    )

    selected = select_snapshots(
        connection,
        [race_id],
        cutoff=datetime(2026, 7, 18, 5, 55, tzinfo=timezone.utc),
        purpose="single_race_ev",
    )

    assert isinstance(selected, list)
    assert isinstance(selected, SnapshotSelection)
    assert [(row.horse_id, row.win_odds) for row in selected] == [
        ("h1", 3.8),
        ("h3", 7.0),
    ]
    assert selected[0]["observed_at"] == datetime(
        2026, 7, 18, 5, 55, tzinfo=timezone.utc
    )
    assert selected.excluded_counts == {
        "quality": 1,
        "missing_or_ambiguous_observed_at": 1,
        "after_cutoff": 1,
    }


def test_quality_filter_reports_flags_stale_and_malformed_json():
    connection = _snapshot_connection()
    race_id = "20260718:東京:10"
    _add_snapshot(connection, 1, race_id, "clean", "2026-07-18T06:00:00Z", 2.0)
    _add_snapshot(
        connection,
        2,
        race_id,
        "bad-flag",
        "2026-07-18T06:00:00Z",
        3.0,
        flags=("catchup_burst",),
    )
    _add_snapshot(
        connection, 3, race_id, "stale", "2026-07-18T06:00:00Z", 4.0, stale=1
    )
    connection.execute(
        """INSERT INTO odds_snapshots
           (odds_snapshot_id,race_id,horse_id,observed_at,win_odds,
            data_quality_flags_json) VALUES(?,?,?,?,?,?)""",
        (4, race_id, "malformed", "2026-07-18T06:00:00Z", 5.0, "not-json"),
    )

    selected = select_snapshots(
        connection,
        [race_id],
        cutoff=datetime(2026, 7, 18, 6, 1, tzinfo=timezone.utc),
        purpose="single_race_ev",
    )
    assert [row.horse_id for row in selected] == ["clean"]
    assert selected.excluded_quality_count == 3

    unfiltered = select_snapshots(
        connection,
        [race_id],
        cutoff=datetime(2026, 7, 18, 6, 1, tzinfo=timezone.utc),
        purpose="single_race_ev",
        require_quality=False,
    )
    assert len(unfiltered) == 4
    assert unfiltered.excluded_quality_count == 0


def test_win5_common_cutoff_excludes_later_leg_near_post_snapshot():
    connection = _snapshot_connection()
    first = "20260718:東京:09"
    later = "20260718:東京:11"
    first_post = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
    cutoff = win5_cutoff(first_post)
    assert cutoff == datetime(2026, 7, 18, 14, 55, tzinfo=timezone.utc)

    _add_snapshot(connection, 1, first, "h1", "2026-07-18T14:55:00Z", 3.0)
    _add_snapshot(connection, 2, later, "h2", "2026-07-18T14:54:00Z", 4.0)
    _add_snapshot(connection, 3, later, "h2", "2026-07-18T15:25:00Z", 2.5, stage="2")

    selected = select_snapshots(
        connection, [first, later], cutoff=cutoff, purpose="win5"
    )
    assert [(row.race_id, row.win_odds) for row in selected] == [
        (first, 3.0),
        (later, 4.0),
    ]
    assert selected.excluded_counts["after_cutoff"] == 1


def test_cutoff_and_win5_first_leg_reject_naive_datetimes():
    connection = _snapshot_connection()
    naive = datetime(2026, 7, 18, 14, 55)

    with pytest.raises(ValueError, match="cutoff must be timezone-aware"):
        select_snapshots(
            connection,
            ["20260718:東京:09"],
            cutoff=naive,
            purpose="win5",
        )
    with pytest.raises(
        ValueError, match="first_leg_post_time must be timezone-aware"
    ):
        win5_cutoff(naive)


def test_naive_observed_at_is_never_compared_with_aware_cutoff():
    connection = _snapshot_connection()
    race_id = "20260718:東京:09"
    _add_snapshot(connection, 1, race_id, "naive", "2026-07-18T14:54:00", 2.0)
    _add_snapshot(connection, 2, race_id, "aware", "2026-07-18T14:54:00Z", 3.0)

    selected = select_snapshots(
        connection,
        [race_id],
        cutoff=datetime(2026, 7, 18, 14, 55, tzinfo=timezone.utc),
        purpose="win5",
    )

    assert [row.horse_id for row in selected] == ["aware"]
    assert selected.excluded_counts["missing_or_ambiguous_observed_at"] == 1


def test_snapshot_purpose_is_required_and_validated():
    connection = _snapshot_connection()
    cutoff = datetime(2026, 7, 18, 14, 55, tzinfo=timezone.utc)
    with pytest.raises(TypeError, match="purpose"):
        select_snapshots(connection, [], cutoff=cutoff)
    with pytest.raises(ValueError, match="purpose must be one of"):
        select_snapshots(connection, [], cutoff=cutoff, purpose="mixed")

    selected = select_snapshots(
        connection, [], cutoff=cutoff, purpose="single_race_ev"
    )
    assert selected.purpose == "single_race_ev"


def _mean(rows):
    return sum(row["value"] for row in rows) / len(rows)


def test_paired_block_bootstrap_is_deterministic_and_order_invariant():
    blocks_a = {
        "20260103": [{"value": 1.0}, {"value": 3.0}],
        "20260110": [{"value": 2.0}],
        "20260117": [{"value": 7.0}, {"value": 5.0}],
    }
    blocks_b = {
        "20260103": [{"value": 0.0}, {"value": 2.0}],
        "20260110": [{"value": 1.0}],
        "20260117": [{"value": 6.0}, {"value": 4.0}],
    }
    first = paired_block_bootstrap(_mean, blocks_a, blocks_b, 250, 42)
    second = paired_block_bootstrap(_mean, blocks_a, blocks_b, 250, 42)
    shuffled_a = {
        key: list(reversed(value)) for key, value in reversed(list(blocks_a.items()))
    }
    shuffled_b = {
        key: list(reversed(value)) for key, value in reversed(list(blocks_b.items()))
    }
    shuffled = paired_block_bootstrap(_mean, shuffled_a, shuffled_b, 250, 42)

    assert first == second
    assert first.differences == shuffled.differences
    assert first.observed_difference == pytest.approx(1.0)
    assert first.n_blocks == 3
    assert 0.0 < first.p_value <= 1.0


def test_bootstrap_resamples_whole_blocks_and_requires_paired_keys():
    block_sizes = {"d1": 2, "d2": 3}
    blocks_a = {
        day: [{"day": day, "value": 1.0}] * size
        for day, size in block_sizes.items()
    }
    blocks_b = {
        day: [{"day": day, "value": 0.0}] * size
        for day, size in block_sizes.items()
    }

    def guarded_mean(rows):
        counts = Counter(row["day"] for row in rows)
        assert all(counts[day] % block_sizes[day] == 0 for day in counts)
        return _mean(rows)

    result = paired_block_bootstrap(guarded_mean, blocks_a, blocks_b, 50, 7)
    assert len(result.differences) == 50
    with pytest.raises(ValueError, match="keys differ"):
        paired_block_bootstrap(guarded_mean, blocks_a, {"d1": blocks_b["d1"]}, 5, 1)


def test_race_date_grouping_uses_first_eight_jst_date_digits():
    rows = [
        {"race_id": "20260718:東京:09", "x": 1},
        {"race_id": "20260718:函館:10", "x": 2},
        {"race_id": "20260719:東京:09", "x": 3},
    ]
    assert race_date_block(rows[0]["race_id"]) == "20260718"
    grouped = group_by_race_date(rows)
    assert {day: len(day_rows) for day, day_rows in grouped.items()} == {
        "20260718": 2,
        "20260719": 1,
    }


@pytest.mark.parametrize("race_id", ["20260230:東京:09", "20261301:東京:09"])
def test_race_date_block_rejects_nonexistent_jst_dates(race_id):
    with pytest.raises(ValueError, match="real JST date"):
        race_date_block(race_id)


def _experiment(experiment_id="T41-test-v1", **updates):
    record = {
        "experiment_id": experiment_id,
        "registered_at_utc": "2026-07-17T00:00:00Z",
        "commit_sha": "0123456789abcdef",
        "data_hashes": {"ability_db": "sha256:abc", "logging_db_rows": 10},
        "features": ["weight delta; median imputation; available at WIN5 cutoff"],
        "primary_metric": "win_logloss_2124_cv",
        "safety_metrics": ["market_topk_floor", "win5_day_paired"],
        "search_grid": {"l2": [0.3, 1.0]},
        "candidate_count": 2,
        "stop_rule": "evaluate the complete grid once",
        "benchmark_type": "historical",
        "prospective_start_date": None,
        "result_summary": None,
        "adjudication": None,
    }
    record.update(updates)
    return record


def test_ledger_rejects_missing_key_and_multiple_primary_metrics():
    missing = _experiment()
    missing.pop("features")
    with pytest.raises(LedgerValidationError, match="missing required keys"):
        validate_experiment(missing)
    with pytest.raises(LedgerValidationError, match="exactly one"):
        validate_experiment(_experiment(primary_metric=["logloss", "top1"]))


def test_ledger_rejects_duplicate_experiment_ids(tmp_path):
    path = tmp_path / "experiments.jsonl"
    record = _experiment()
    path.write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )
    with pytest.raises(LedgerValidationError, match="duplicate experiment_id"):
        verify_ledger(path, state_path=tmp_path / "state.json")


def test_ledger_verify_detects_modification_of_verified_prefix(tmp_path):
    path = tmp_path / "experiments.jsonl"
    state = tmp_path / "verify.json"
    result = append_experiment(_experiment(), path, state_path=state)
    assert result.line_count == 1
    assert state.exists()

    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("weight delta", "weight shift"), encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="modified"):
        verify_ledger(path, state_path=state)


def test_ledger_allows_only_new_unique_append_and_requires_prospective_date(tmp_path):
    path = tmp_path / "experiments.jsonl"
    state = tmp_path / "verify.json"
    append_experiment(_experiment(), path, state_path=state)
    second = _experiment(
        "T45-freeze-v1",
        benchmark_type="prospective",
        prospective_start_date="2026-07-18",
    )
    result = append_experiment(second, path, state_path=state)
    assert result.line_count == 2
    assert verify_ledger(path, state_path=state).line_count == 2

    with pytest.raises(LedgerValidationError, match="duplicate experiment_id"):
        append_experiment(second, path, state_path=state)
    with pytest.raises(LedgerValidationError, match="ISO date"):
        validate_experiment(
            _experiment(
                "T45-bad-v1",
                benchmark_type="prospective",
                prospective_start_date=None,
            )
        )


def test_ledger_supersession_must_point_from_new_row_to_earlier_id(tmp_path):
    path = tmp_path / "experiments.jsonl"
    state = tmp_path / "verify.json"
    append_experiment(_experiment(), path, state_path=state)
    replacement = _experiment(
        "T41-test-v2",
        superseded_by="T41-test-v1",
        features=["revised preregistered feature"],
    )
    assert append_experiment(replacement, path, state_path=state).line_count == 2

    with pytest.raises(LedgerValidationError, match="earlier experiment_id"):
        append_experiment(
            _experiment("T41-test-v3", superseded_by="missing-v1"),
            path,
            state_path=state,
        )
    assert verify_ledger(path, state_path=state).line_count == 2
    with pytest.raises(LedgerValidationError, match="same experiment"):
        validate_experiment(_experiment("T41-self", superseded_by="T41-self"))


def test_ledger_result_and_adjudication_rows_preserve_registered_contract(tmp_path):
    path = tmp_path / "experiments.jsonl"
    state = tmp_path / "verify.json"
    registration = _experiment()
    append_experiment(registration, path, state_path=state)

    result = _experiment(
        "T41-test-v1-result",
        registered_at_utc="2026-08-01T00:00:00Z",
        superseded_by="T41-test-v1",
        data_hashes={
            **registration["data_hashes"],
            "prospective_eval_sha256": "sha256:def",
            "prospective_eval_rows": 250,
        },
        result_summary={"primary_metric": 0.412, "excluded_quality_rows": 3},
    )
    assert append_experiment(result, path, state_path=state).line_count == 2

    adjudication = {
        **result,
        "experiment_id": "T41-test-v1-adjudication",
        "registered_at_utc": "2026-08-02T00:00:00Z",
        "superseded_by": "T41-test-v1-result",
        "adjudication": {"decision": "reject", "reason": "safety floor"},
    }
    assert append_experiment(adjudication, path, state_path=state).line_count == 3

    changed_metric = {
        **result,
        "experiment_id": "T41-test-v1-bad-result",
        "superseded_by": "T41-test-v1",
        "primary_metric": "top1_accuracy",
    }
    with pytest.raises(LedgerValidationError, match="immutable fields"):
        append_experiment(changed_metric, path, state_path=state)

    overwritten_hash = {
        **result,
        "experiment_id": "T41-test-v1-bad-data",
        "superseded_by": "T41-test-v1",
        "data_hashes": {"ability_db": "sha256:changed"},
    }
    with pytest.raises(LedgerValidationError, match="must preserve data_hashes"):
        append_experiment(overwritten_hash, path, state_path=state)

    changed_adjudication_result = {
        **adjudication,
        "experiment_id": "T41-test-v1-bad-adjudication",
        "result_summary": {"primary_metric": 0.999},
    }
    with pytest.raises(LedgerValidationError, match="preserve result_summary"):
        append_experiment(changed_adjudication_result, path, state_path=state)

    skipped_result_row = {
        **result,
        "experiment_id": "T41-test-v1-direct-adjudication",
        "superseded_by": "T41-test-v1",
        "adjudication": {"decision": "adopt"},
    }
    with pytest.raises(LedgerValidationError, match="prior result row"):
        append_experiment(skipped_result_row, path, state_path=state)
    assert verify_ledger(path, state_path=state).line_count == 3


def test_ledger_result_rows_cannot_bypass_preregistration_link():
    with pytest.raises(LedgerValidationError, match="prior row"):
        validate_experiment(_experiment(result_summary={"metric": 0.4}))
    with pytest.raises(LedgerValidationError, match="carry forward"):
        validate_experiment(
            _experiment(
                "T41-adjudication-only",
                superseded_by="T41-test-v1",
                adjudication={"decision": "adopt"},
            )
        )
