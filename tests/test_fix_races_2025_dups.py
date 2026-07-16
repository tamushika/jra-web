import copy
import csv
import inspect
import json
import os
import sqlite3
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

import jra_db_updater as updater
import fix_races_2025_dups as fixer


def _row(*, horse="A", race_num=1, horse_number=3, ctid="(0,1)", **changes):
    row = {
        "date": "250105",
        "place": "東京",
        "race_num": race_num,
        "horse_number": horse_number,
        "馬名": horse,
        "rank": 1,
        "time": "1:35.0",
        "track_type": "芝",
        "distance": 1600,
        "total_horses": 18,
        "agari_3f": "34.5",
        "corner_4": 1,
        "weight": 500,
        "condition": "良",
        "jockey": "騎手A",
        "race_name": "テスト競走",
        fixer.ROW_ID: ctid,
    }
    row.update(changes)
    return row


def _truth(*, horse="A", race=1, horse_number=3, **changes):
    row = {
        "date": "20250105",
        "place": "東京",
        "r": race,
        "umaban": horse_number,
        "horse": horse,
        "rank": 1,
        "time_sec": 95.0,
        "track_type": "芝",
        "distance": 1600,
        "total_horses": 18,
        "agari": 34.5,
        "c4": 1,
        "weight": 500,
        "condition": "良",
        "jockey": "騎手A",
        "race_name": "テスト競走",
    }
    row.update(changes)
    return row


def _classifications(plan):
    return [item["classification"] for item in plan["classifications"]]


def _bundle_ability():
    return {
        "path": "C:/fixtures/ability.db", "size": 123, "mtime_ns": 456,
        "sha256": "a" * 64,
    }


def _bundle_database():
    return {
        "database_name": "synthetic", "schema_name": "public",
        "url_backend": "postgresql", "url_host": "fixture.invalid",
        "url_port": 5432, "url_database": "synthetic",
    }


def _bundle_snapshot():
    return {
        "transaction_time": "2026-07-14T00:00:00+00:00",
        "transaction_snapshot": "1:1:",
        "scoped_duplicate_groups": 1,
        "candidate_2025_duplicate_groups": 1,
    }


def _manual_fix_rows():
    return [
        {"date": "250208", "place": "小倉", "race_num": 5,
         "horse_number": 3, "馬名": "ミグラテール"},
        {"date": "250208", "place": "小倉", "race_num": 5,
         "horse_number": 5, "馬名": "トーアマリシテン"},
        {"date": "250208", "place": "小倉", "race_num": 5,
         "horse_number": 7, "馬名": "アルカンサス"},
        {"date": "250412", "place": "中山", "race_num": 3,
         "horse_number": 9, "馬名": "スターコンパス"},
        {"date": "250412", "place": "中山", "race_num": 1,
         "horse_number": 9, "馬名": "モーニングマジック"},
        {"date": "251116", "place": "福島", "race_num": 5,
         "horse_number": 9, "馬名": "チンプンカンプン"},
    ]


def _with_manual_fix_gate(plan):
    plan = copy.deepcopy(plan)
    plan["manual_fix_gate"] = fixer.evaluate_manual_fix_gate(_manual_fix_rows())
    plan["plan_sha256"] = fixer.canonical_plan_sha256(plan)
    return plan


def test_normalizers_are_strict_and_canonical():
    assert fixer.normalize_text(" Ａ  馬\t") == "A馬"
    assert fixer.normalize_neon_date("２５０１０５") == "20250105"
    assert fixer.normalize_neon_date("250229") is None
    assert fixer.normalize_neon_date("240105") is None
    assert fixer.normalize_neon_date("20250105") is None
    assert fixer.strict_int("０３", 1, 18) == 3
    assert fixer.strict_int("3.0") is None
    assert fixer.strict_int(True) is None
    assert fixer.strict_int(19, 1, 18) is None
    assert fixer.to_deciseconds("1:35.0") == 950
    assert fixer.to_deciseconds(95.0) == 950
    assert fixer.to_deciseconds("95.01") is None
    assert fixer.to_deciseconds("1:60.0") is None
    assert fixer.canonical_track_type(" ダ ") == "ダート"
    assert fixer.canonical_condition("稍重") == "稍"


def test_manual_fix_gate_requires_each_approved_key_exactly_once():
    rows = _manual_fix_rows()
    accepted = fixer.evaluate_manual_fix_gate(rows)
    assert accepted["ok"] is True
    assert len(accepted["rows"]) == 6

    missing = fixer.evaluate_manual_fix_gate(rows[:-1])
    assert missing["ok"] is False

    duplicated = fixer.evaluate_manual_fix_gate(rows + [dict(rows[0])])
    assert duplicated["ok"] is False

    # v2.3.5: a different horse sharing an expected key is a normal cleanup
    # candidate (e.g. the known duplicate co-resident at 中山250412 R1 u9);
    # the gate records it but must not fail on it.
    co_resident = dict(rows[4])
    co_resident["馬名"] = "タイセイアダマス"
    shared = fixer.evaluate_manual_fix_gate(rows + [co_resident])
    assert shared["ok"] is True
    assert shared["rows"][4]["observed_count"] == 2
    assert shared["rows"][4]["matching_horse_count"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3137", 1937),
        ("1134", 734),
        ("1143", 743),
        ("1095", 695),
        ("1000", 600),
        ("1599", 1199),
        ("10000", 6000),
        ("0599", 599),
        ("３１３７", 1937),
        ("3:13.7", 1937),
        ("193.7", 1937),
        (193.7, 1937),
    ],
)
def test_neon_time_parser_accepts_compact_and_explicit_seconds(raw, expected):
    assert fixer.to_neon_time_deciseconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["1600", "1999", "137", "95", "123456", "01134", "0000", "+1134", "11 34", "11A4"],
)
def test_neon_time_parser_rejects_ambiguous_or_invalid_compact_values(raw):
    assert fixer.to_neon_time_deciseconds(raw) is None


def test_compact_neon_time_matches_truth_without_changing_agari_semantics():
    comparison = fixer.compare_payload(
        _row(time="3137", agari_3f="345"),
        _truth(time_sec=193.7, agari=34.5),
    )

    assert comparison["fields"]["time"]["status"] == "match"
    assert comparison["mandatory_ok"] is True
    assert comparison["fields"]["agari_3f"]["status"] == "mismatch"
    assert comparison["veto_ok"] is False
    assert fixer.to_deciseconds("3137") == 31370


def test_compact_neon_time_enables_keep_and_move_but_invalid_time_is_nonmutation():
    plan = fixer.build_operation_plan(
        [
            _row(horse="A", race_num=1, ctid="(1,1)", time="1134"),
            _row(horse="B", race_num=1, ctid="(1,2)", time="1143"),
        ],
        [
            _truth(horse="A", race=1, time_sec=73.4),
            _truth(horse="B", race=2, time_sec=74.3),
        ],
    )
    assert plan["status"] == "APPLICABLE"
    assert sorted(_classifications(plan)) == ["KEEP_CURRENT", "MOVE_CANDIDATE"]

    invalid = fixer.build_operation_plan(
        [_row(time="1600")],
        [_truth(time_sec=100.0)],
    )
    assert invalid["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(invalid) == ["UNVERIFIED_KEEP"]
    assert invalid["classifications"][0]["reason_code"] == "INVALID_VALUE"
    assert invalid["classifications"][0]["comparison"]["fields"]["time"]["status"] == "invalid"


@pytest.mark.parametrize(
    "bad_date",
    ["20250230", "20251301", "2025010", "2025015", "20240105"],
)
def test_truth_index_rejects_invalid_or_non_2025_real_dates(bad_date):
    with pytest.raises(fixer.SafetyError, match="invalid 2025 identity"):
        fixer.build_truth_index([_truth(date=bad_date)])


def test_payload_contract_mandatory_veto_and_auxiliary_fields():
    comparison = fixer.compare_payload(_row(), _truth())
    assert comparison["mandatory_ok"] is True
    assert comparison["veto_ok"] is True
    assert all(
        comparison["fields"][field]["status"] == "match"
        for field in fixer.MANDATORY_FIELDS
    )

    rank_mismatch = fixer.compare_payload(_row(rank=2), _truth())
    assert rank_mismatch["mandatory_ok"] is False
    assert rank_mismatch["fields"]["rank"]["status"] == "mismatch"

    null_time = fixer.compare_payload(_row(time=None), _truth())
    assert null_time["mandatory_ok"] is False
    assert null_time["fields"]["time"]["status"] == "null"
    assert null_time["fields"]["time"]["row_raw"] is None

    invalid_rank = fixer.compare_payload(_row(rank="not-a-rank"), _truth())
    assert invalid_rank["mandatory_ok"] is False
    assert invalid_rank["fields"]["rank"]["status"] == "invalid"
    assert invalid_rank["fields"]["rank"]["row_raw"] == "not-a-rank"
    assert invalid_rank["fields"]["rank"]["truth_raw"] == 1

    optional_missing = fixer.compare_payload(_row(agari_3f=None), _truth())
    assert optional_missing["veto_ok"] is True
    assert optional_missing["fields"]["agari_3f"]["status"] == "not_compared"

    optional_mismatch = fixer.compare_payload(_row(weight=501), _truth())
    assert optional_mismatch["mandatory_ok"] is True
    assert optional_mismatch["veto_ok"] is False

    auxiliary_mismatch = fixer.compare_payload(
        _row(jockey="別騎手", race_name="別競走"), _truth()
    )
    assert auxiliary_mismatch["mandatory_ok"] is True
    assert auxiliary_mismatch["veto_ok"] is True
    assert auxiliary_mismatch["fields"]["jockey"]["status"] == "mismatch"


def test_build_plan_keep_and_move_same_slot_is_applicable_without_prepare_keyerror():
    rows = [
        _row(horse="A", race_num=1, ctid="(0,1)"),
        _row(horse="B", race_num=1, ctid="(0,2)"),
    ]
    truth = [_truth(horse="A", race=1), _truth(horse="B", race=2)]

    # Regression: build_operation_plan prepares rows, then classify_rows prepares
    # them again.  Internal fingerprint fields must never trigger a KeyError or
    # become part of the logical fingerprint.
    plan = fixer.build_operation_plan(rows, truth)

    assert plan["status"] == "APPLICABLE"
    assert sorted(_classifications(plan)) == ["KEEP_CURRENT", "MOVE_CANDIDATE"]
    assert plan["counts"] == {
        "rows": 2,
        "keep": 1,
        "update": 1,
        "delete": 0,
        "non_mutation": 0,
        "unresolved": 0,
        "final_row_delta": 0,
    }


@pytest.mark.parametrize(
    ("rows", "truth", "reason_code"),
    [
        ([_row(horse="NO_TRUTH")], [_truth()], "TRUTH_ZERO"),
        (
            [_row()],
            [_truth(race=1), _truth(race=2)],
            "TRUTH_MULTI",
        ),
        ([_row(rank=2)], [_truth()], "PAYLOAD_MISMATCH"),
        ([_row(time="1:35.1")], [_truth()], "PAYLOAD_MISMATCH"),
        ([_row(rank=2, jockey="Aとは別")], [_truth()], "PAYLOAD_MISMATCH"),
    ],
)
def test_unproven_rows_are_nonmutation_when_their_keys_do_not_collide(
        rows, truth, reason_code):
    plan = fixer.build_operation_plan(rows, truth)
    assert plan["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(plan) == ["UNVERIFIED_KEEP"]
    assert plan["classifications"][0]["reason_code"] == reason_code
    assert plan["counts"]["unresolved"] == 0
    assert plan["operations"] == []


def test_horse_name_alone_never_authorizes_move():
    plan = fixer.build_operation_plan(
        [_row(race_num=1, rank=2, time="1:36.0")],
        [_truth(race=2)],
    )
    assert _classifications(plan) == ["UNVERIFIED_KEEP"]
    assert plan["classifications"][0]["reason_code"] == "PAYLOAD_MISMATCH"
    assert plan["counts"]["update"] == 0


def test_exact_duplicate_has_one_deterministic_keeper():
    rows = [_row(ctid="(9,9)"), _row(ctid="(1,1)")]
    plan = fixer.build_operation_plan(rows, [_truth()])

    assert plan["status"] == "APPLICABLE"
    assert sorted(_classifications(plan)) == [
        "DELETE_EXACT_DUPLICATE", "KEEP_CURRENT"
    ]
    assert plan["counts"]["delete"] == 1
    deletion = next(item for item in plan["classifications"]
                    if item["classification"] == "DELETE_EXACT_DUPLICATE")
    assert deletion["keeper"]["occurrence"] == 0


def test_redundant_error_requires_a_unique_complete_keeper():
    complete = _row(ctid="(1,1)")
    mixed = _row(rank=2, ctid="(1,2)")

    with_keeper = fixer.build_operation_plan([mixed, complete], [_truth()])
    assert with_keeper["status"] == "APPLICABLE"
    assert sorted(_classifications(with_keeper)) == [
        "DELETE_REDUNDANT_ERROR", "KEEP_CURRENT"
    ]

    without_keeper = fixer.build_operation_plan([mixed], [_truth()])
    assert without_keeper["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(without_keeper) == ["UNVERIFIED_KEEP"]
    assert without_keeper["classifications"][0]["reason_code"] == "PAYLOAD_MISMATCH"
    assert without_keeper["operations"] == []


def test_veto_mismatch_is_deletable_only_with_one_verified_truth_keeper():
    keeper = _row(ctid="(2,1)")
    veto_mismatch = _row(weight=501, ctid="(2,2)")

    with_keeper = fixer.build_operation_plan(
        [veto_mismatch, keeper], [_truth()]
    )
    assert with_keeper["status"] == "APPLICABLE"
    assert sorted(_classifications(with_keeper)) == [
        "DELETE_REDUNDANT_ERROR", "KEEP_CURRENT",
    ]
    deletion = next(
        item for item in with_keeper["classifications"]
        if item["classification"] == "DELETE_REDUNDANT_ERROR"
    )
    assert deletion["comparison"]["veto_ok"] is False
    assert deletion["keeper"] is not None

    without_keeper = fixer.build_operation_plan([veto_mismatch], [_truth()])
    assert without_keeper["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(without_keeper) == ["UNVERIFIED_KEEP"]
    assert without_keeper["classifications"][0]["reason_code"] == "PAYLOAD_MISMATCH"


def test_redundant_delete_is_blocked_when_complete_keeper_is_not_unique():
    rows = [
        _row(ctid="(2,3)", jockey="表記A"),
        _row(ctid="(2,4)", jockey="表記B"),
        _row(ctid="(2,5)", weight=501),
    ]
    plan = fixer.build_operation_plan(rows, [_truth()])

    assert "DELETE_REDUNDANT_ERROR" not in _classifications(plan)
    assert plan["status"] == "NOT_APPLICABLE"
    assert plan["counts"]["delete"] == 0


def test_truth_zero_and_truth_multi_rows_are_never_reclassified_to_delete():
    zero_rows = [
        _row(horse="NO_TRUTH", ctid="(3,1)"),
        _row(horse="A", ctid="(3,2)"),
    ]
    zero_plan = fixer.build_operation_plan(zero_rows, [_truth()])
    assert "DELETE_REDUNDANT_ERROR" not in _classifications(zero_plan)
    assert zero_plan["status"] == "NOT_APPLICABLE"
    assert sorted(_classifications(zero_plan)) == ["KEEP_CURRENT", "UNRESOLVED"]

    multi_plan = fixer.build_operation_plan(
        [_row(race_num=1, ctid="(4,1)"), _row(race_num=2, ctid="(4,2)")],
        [_truth(race=1), _truth(race=2)],
    )
    assert "DELETE_REDUNDANT_ERROR" not in _classifications(multi_plan)
    assert multi_plan["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(multi_plan) == ["UNVERIFIED_KEEP", "UNVERIFIED_KEEP"]
    assert all(
        item["reason_code"] == "TRUTH_MULTI"
        for item in multi_plan["classifications"]
    )


def test_non_runner_and_all_unverified_reason_codes_follow_decision_table():
    cases = [
        (_row(date="not-a-date"), [_truth()], "UNVERIFIED_KEEP", "INVALID_VALUE"),
        (_row(horse="NO_TRUTH"), [_truth()], "UNVERIFIED_KEEP", "TRUTH_ZERO"),
        (_row(), [_truth(race=1), _truth(race=2)], "UNVERIFIED_KEEP", "TRUTH_MULTI"),
        (_row(rank="bad"), [_truth()], "UNVERIFIED_KEEP", "INVALID_VALUE"),
        (_row(), [_truth(horse_number=4)], "UNVERIFIED_KEEP", "UMABAN_MISMATCH"),
        (_row(rank=2), [_truth()], "UNVERIFIED_KEEP", "PAYLOAD_MISMATCH"),
        (_row(horse="NO_TRUTH", rank=None, time="----"), [_truth()], "NON_RUNNER_KEEP", None),
    ]

    for row, truth, classification, reason_code in cases:
        plan = fixer.build_operation_plan([row], truth)
        item = plan["classifications"][0]
        assert plan["status"] == "NO_MUTATIONS_NEEDED"
        assert item["classification"] == classification
        assert item["reason_code"] == reason_code
        assert plan["operations"] == []


def test_nonmutation_rows_are_promoted_to_unresolved_only_on_final_collision():
    rows = [
        _row(horse="NO_TRUTH_A", ctid="(5,1)"),
        _row(horse="NO_TRUTH_B", ctid="(5,2)"),
    ]
    plan = fixer.build_operation_plan(rows, [_truth()])

    assert plan["status"] == "NOT_APPLICABLE"
    assert _classifications(plan) == ["UNRESOLVED", "UNRESOLVED"]
    assert all(item["reason_code"] is None for item in plan["classifications"])
    assert plan["counts"]["unresolved"] == 2
    assert plan["operations"] == []


def test_invalid_identity_rows_use_raw_db_natural_keys_for_final_collisions():
    distinct = fixer.build_operation_plan(
        [
            _row(horse="INVALID_A", race_num=99, ctid="(5,3)"),
            _row(horse="INVALID_B", race_num=100, ctid="(5,4)"),
        ],
        [_truth()],
    )
    assert distinct["status"] == "NO_MUTATIONS_NEEDED"
    assert _classifications(distinct) == ["UNVERIFIED_KEEP", "UNVERIFIED_KEEP"]
    assert all(
        item["reason_code"] == "INVALID_VALUE"
        for item in distinct["classifications"]
    )

    same_raw_key = fixer.build_operation_plan(
        [
            _row(horse="INVALID_A", race_num=99, ctid="(5,5)"),
            _row(horse="INVALID_B", race_num=99, ctid="(5,6)"),
        ],
        [_truth()],
    )
    assert same_raw_key["status"] == "NOT_APPLICABLE"
    assert _classifications(same_raw_key) == ["UNRESOLVED", "UNRESOLVED"]


def test_nonmutation_postcheck_preserves_invalid_raw_key_and_fingerprint(
        monkeypatch):
    row = _row(horse="INVALID", race_num=99, ctid="(5,7)")
    plan = fixer.build_operation_plan([row], [_truth()])
    assert plan["status"] == "NO_MUTATIONS_NEEDED"
    fetched_targets = []

    def unchanged(connection, targets):
        fetched_targets.extend(targets)
        return [row]

    monkeypatch.setattr(fixer, "_fetch_target_rows", unchanged)
    fixer._verify_nonmutation_poststate(object(), plan)
    assert fetched_targets == [("250105", "東京", 99, 3)]

    monkeypatch.setattr(
        fixer, "_fetch_target_rows",
        lambda connection, targets: [{**row, "rank": 2}],
    )
    with pytest.raises(fixer.SafetyError, match="fingerprint"):
        fixer._verify_nonmutation_poststate(object(), plan)


def test_mutation_free_plan_with_nonidentical_duplicate_keepers_is_not_noop():
    rows = [
        _row(ctid="(6,1)", jockey="騎手A"),
        _row(ctid="(6,2)", jockey="表記違い"),
    ]
    plan = fixer.build_operation_plan(rows, [_truth()])

    assert plan["status"] == "NOT_APPLICABLE"
    assert plan["status"] != "NO_MUTATIONS_NEEDED"
    assert plan["operations"] == []


def test_wrong_slot_complete_row_is_deleted_when_truth_keeper_already_exists():
    rows = [
        _row(horse="A", race_num=1, ctid="(1,1)"),
        _row(horse="B", race_num=1, ctid="(1,2)"),
        _row(horse="B", race_num=2, ctid="(1,3)"),
    ]
    truth = [_truth(horse="A", race=1), _truth(horse="B", race=2)]

    plan = fixer.build_operation_plan(rows, truth)

    assert plan["status"] == "APPLICABLE"
    assert sorted(_classifications(plan)) == [
        "DELETE_REDUNDANT_ERROR", "KEEP_CURRENT", "KEEP_CURRENT",
    ]
    assert plan["counts"]["update"] == 0
    assert plan["counts"]["delete"] == 1
    deletion = next(
        item for item in plan["classifications"]
        if item["classification"] == "DELETE_REDUNDANT_ERROR"
    )
    assert deletion["target"] is None
    assert deletion["keeper"] is not None


def test_existing_destination_and_multiple_moves_are_not_applicable():
    destination_occupant = _row(
        horse="UNKNOWN", race_num=2, ctid="(0,3)", rank=9, time="1:50.0"
    )
    occupied = fixer.build_operation_plan(
        [_row(horse="B", race_num=1), destination_occupant],
        [_truth(horse="B", race=2)],
    )
    assert occupied["status"] == "NOT_APPLICABLE"
    assert any("collision" in reason for reason in occupied["reasons"])

    duplicate_movers = fixer.build_operation_plan(
        [_row(horse="B", race_num=1, ctid="(0,1)"),
         _row(horse="B", race_num=1, ctid="(0,2)")],
        [_truth(horse="B", race=2)],
    )
    assert duplicate_movers["status"] == "NOT_APPLICABLE"
    assert any("collision" in reason for reason in duplicate_movers["reasons"])


def test_verified_move_chain_and_cycle_are_planned_as_one_final_state():
    chain = fixer.build_operation_plan(
        [_row(horse="A", race_num=1, ctid="(0,1)"),
         _row(horse="B", race_num=2, ctid="(0,2)")],
        [_truth(horse="A", race=2), _truth(horse="B", race=3)],
    )
    assert chain["status"] == "APPLICABLE"
    assert chain["counts"]["update"] == 2
    assert chain["reasons"] == []

    cycle = fixer.build_operation_plan(
        [_row(horse="A", race_num=1, ctid="(0,1)"),
         _row(horse="B", race_num=2, ctid="(0,2)")],
        [_truth(horse="A", race=2), _truth(horse="B", race=1)],
    )
    assert cycle["status"] == "APPLICABLE"
    assert cycle["counts"]["update"] == 2
    assert cycle["reasons"] == []

    three_cycle = fixer.build_operation_plan(
        [
            _row(horse="A", race_num=1, ctid="(0,1)"),
            _row(horse="B", race_num=2, ctid="(0,2)"),
            _row(horse="C", race_num=3, ctid="(0,3)"),
        ],
        [
            _truth(horse="A", race=2),
            _truth(horse="B", race=3),
            _truth(horse="C", race=1),
        ],
    )
    assert three_cycle["status"] == "APPLICABLE"
    assert three_cycle["counts"]["update"] == 3


def test_logical_fingerprint_and_plan_hash_ignore_physical_row_ids():
    left = _row(ctid="(0,1)")
    right = _row(ctid="(99,99)")
    assert fixer.logical_row_fingerprint(left) == fixer.logical_row_fingerprint(right)
    assert fixer.logical_row_fingerprint(left) != fixer.logical_row_fingerprint(
        _row(ctid="(0,1)", rank=2)
    )

    plan = fixer.build_operation_plan([left], [_truth()])
    altered = copy.deepcopy(plan)
    altered["classifications"][0]["raw_ctid"] = "(888,1)"
    altered["classifications"][0]["__ctid__"] = "(888,2)"
    altered["plan_sha256"] = "stale-self-hash"
    assert fixer.canonical_plan_sha256(plan) == fixer.canonical_plan_sha256(altered)


def test_canonical_plan_hash_is_order_independent_but_content_sensitive():
    rows = [_row(horse="A", race_num=1), _row(horse="B", race_num=1, ctid="(0,2)")]
    truth = [_truth(horse="A", race=1), _truth(horse="B", race=2)]
    forward = fixer.build_operation_plan(rows, truth)
    reverse = fixer.build_operation_plan(list(reversed(rows)), list(reversed(truth)))
    assert forward["plan_sha256"] == reverse["plan_sha256"]

    reordered = copy.deepcopy(forward)
    reordered["classifications"].reverse()
    reordered["operations"].reverse()
    assert fixer.canonical_plan_sha256(forward) == fixer.canonical_plan_sha256(reordered)

    changed = copy.deepcopy(forward)
    changed["operations"][0]["target"]["race_num"] = 3
    assert fixer.canonical_plan_sha256(forward) != fixer.canonical_plan_sha256(changed)


def test_ability_database_is_opened_read_only_and_identity_stays_unchanged(tmp_path):
    ability = tmp_path / "ability.db"
    connection = sqlite3.connect(ability)
    try:
        connection.execute("""
            CREATE TABLE runs (
                date TEXT, place TEXT, r INTEGER, umaban INTEGER, horse TEXT,
                rank INTEGER, time_sec REAL, track_type TEXT, distance INTEGER,
                total_horses INTEGER, agari REAL, c4 INTEGER, weight INTEGER,
                condition TEXT, jockey TEXT, race_name TEXT
            )
        """)
        truth = _truth()
        columns = ", ".join(truth)
        placeholders = ", ".join("?" for _ in truth)
        connection.execute(
            f"INSERT INTO runs ({columns}) VALUES ({placeholders})",
            tuple(truth.values()),
        )
        connection.commit()
    finally:
        connection.close()

    before = fixer.ability_identity(ability)
    assert fixer.load_ability_truth(ability) == [_truth()]
    with fixer.open_ability_read_only(ability) as read_only:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute("DELETE FROM runs")
    assert fixer.ability_identity(ability) == before


def test_bundle_is_lossless_hashed_and_revalidatable(tmp_path):
    rows = [
        _row(
            ctid="(1,1)", nullable_note=None, empty_note="",
            exact_float=-0.0, exact_decimal=Decimal("1.2300"),
            exact_date=date(2025, 1, 5),
            exact_datetime=datetime(2025, 1, 5, 12, 34, tzinfo=timezone.utc),
            exact_bytes=b"\x00\xff",
        ),
        _row(
            ctid="(1,2)", nullable_note=None, empty_note="",
            exact_float=-0.0, exact_decimal=Decimal("1.2300"),
            exact_date=date(2025, 1, 5),
            exact_datetime=datetime(2025, 1, 5, 12, 34, tzinfo=timezone.utc),
            exact_bytes=b"\x00\xff",
        ),
    ]
    plan = _with_manual_fix_gate(fixer.build_operation_plan(rows, [_truth()]))
    bundle = tmp_path / "bundle"
    manifest = fixer.write_bundle(
        bundle,
        candidate_rows=rows,
        destination_rows=[],
        plan=plan,
        ability=_bundle_ability(),
        database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )

    expected_names = {
        "classification.csv", "candidate_rows.csv", "destination_rows.csv",
        "rows.jsonl", "plan.json", "manifest.json",
    }
    assert {path.name for path in bundle.iterdir()} == expected_names
    assert manifest["format_version"] == 3
    assert manifest["plan_status"] == plan["status"] == "APPLICABLE"
    assert manifest["applicable"] is True
    assert set(manifest["classification_counts"]) == {
        "KEEP_CURRENT", "MOVE_CANDIDATE", "DELETE_EXACT_DUPLICATE",
        "DELETE_REDUNDANT_ERROR", "NON_RUNNER_KEEP", "UNVERIFIED_KEEP",
        "UNRESOLVED",
    }
    assert set(manifest["unverified_reason_counts"]) == {
        "TRUTH_ZERO", "TRUTH_MULTI", "PAYLOAD_MISMATCH",
        "UMABAN_MISMATCH", "INVALID_VALUE",
    }
    assert manifest["candidate_reason_counts"] == {"a": 0, "b": 0, "c": 0}
    assert manifest["manual_fix_gate"]["ok"] is True
    assert manifest["plan_sha256"] == plan["plan_sha256"]
    assert manifest["manifest_sha256"] == fixer.file_sha256(bundle / "manifest.json")
    assert manifest["counts"]["candidate_rows"] == 2
    assert manifest["counts"]["destination_rows"] == 0
    for name, metadata in manifest["artifacts"].items():
        artifact = bundle / name
        assert artifact.stat().st_size == metadata["size"]
        assert fixer.file_sha256(artifact) == metadata["sha256"]

    records = [json.loads(line) for line in
               (bundle / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["row"]["nullable_note"] is None
    assert records[0]["row"]["empty_note"] == ""
    assert records[0]["row"]["exact_float"] == {
        "__t34b_type__": "float", "value": "-0x0.0p+0",
    }
    assert records[0]["row"]["exact_decimal"] == {
        "__t34b_type__": "decimal", "value": "1.2300",
    }
    assert records[0]["row"]["exact_date"] == {
        "__t34b_type__": "date", "value": "2025-01-05",
    }
    assert records[0]["row"]["exact_datetime"] == {
        "__t34b_type__": "datetime", "value": "2025-01-05T12:34:00+00:00",
    }
    assert records[0]["row"]["exact_bytes"] == {
        "__t34b_type__": "bytes", "value": "00ff",
    }
    expected_validation = (
        json.loads((bundle / "manifest.json").read_text(encoding="utf-8")),
        json.loads((bundle / "plan.json").read_text(encoding="utf-8")),
    )
    assert expected_validation[1]["version"] == 3
    assert all(
        item["reason_code"] is None
        for item in expected_validation[1]["classifications"]
    )
    assert fixer.validate_bundle(bundle / "manifest.json") == expected_validation
    assert fixer.validate_bundle(
        bundle / "manifest.json",
        approved_manifest_sha256=manifest["manifest_sha256"],
    ) == expected_validation


def test_closure_only_keeper_is_classified_and_saved_in_all_audit_artifacts(
        tmp_path):
    candidate = _row(race_num=2, ctid="(8,1)")
    keeper = _row(race_num=1, ctid="(8,2)")
    plan = fixer.build_operation_plan(
        [candidate, keeper], [_truth()],
        candidate_reasons={0: ["c"], 1: []},
    )
    plan = _with_manual_fix_gate(plan)

    by_fingerprint = {
        item["source"]["fingerprint"]: item
        for item in plan["classifications"]
    }
    assert by_fingerprint[fixer.logical_row_fingerprint(candidate)]["candidate_reasons"] == ["c"]
    closure_item = by_fingerprint[fixer.logical_row_fingerprint(keeper)]
    assert closure_item["classification"] == "KEEP_CURRENT"
    assert closure_item["candidate_reasons"] == []
    assert closure_item["reason_code"] is None
    assert plan["operations"][0]["candidate_reasons"] == ["c"]
    assert plan["operations"][0]["reason_code"] is None

    bundle = tmp_path / "closure-bundle"
    manifest = fixer.write_bundle(
        bundle,
        candidate_rows=[candidate],
        destination_rows=[keeper],
        plan=plan,
        ability=_bundle_ability(),
        database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )

    with (bundle / "classification.csv").open(
            encoding="utf-8-sig", newline="") as handle:
        classifications = list(csv.DictReader(handle))
    with (bundle / "destination_rows.csv").open(
            encoding="utf-8-sig", newline="") as handle:
        destinations = list(csv.DictReader(handle))
    records = [
        json.loads(line)
        for line in (bundle / "rows.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    persisted_plan = json.loads((bundle / "plan.json").read_text(encoding="utf-8"))

    assert len(classifications) == 2
    assert sorted(row["candidate_reasons"] for row in classifications) == ["", "c"]
    assert all(row["reason_code"] == "" for row in classifications)
    assert len(destinations) == 1
    assert Counter(record["kind"] for record in records) == {
        "candidate": 1, "destination": 1,
    }
    persisted_closure = next(
        item for item in persisted_plan["classifications"]
        if item["source"]["fingerprint"] == fixer.logical_row_fingerprint(keeper)
    )
    assert persisted_closure["candidate_reasons"] == []
    assert persisted_closure["reason_code"] is None
    assert manifest["counts"]["candidate_rows"] == 1
    assert manifest["counts"]["destination_rows"] == 1
    assert manifest["candidate_reason_counts"] == {"a": 0, "b": 0, "c": 1}


def test_candidate_reason_schema_rejects_unknown_or_duplicate_codes():
    with pytest.raises(fixer.SafetyError, match="candidate reason"):
        fixer.build_operation_plan(
            [_row()], [_truth()], candidate_reasons={0: ["not-a-code"]}
        )
    with pytest.raises(fixer.SafetyError, match="candidate reason"):
        fixer.build_operation_plan(
            [_row()], [_truth()], candidate_reasons={0: ["a", "a"]}
        )


def test_generalized_candidate_selection_covers_a_b_and_c_without_duplicate_seed():
    correct = _row(horse="A", race_num=1, ctid="(10,1)")
    wrong_horse = _row(horse="WRONG", race_num=1, ctid="(10,2)")
    truth_absent = _row(horse="NO_SLOT", race_num=3, ctid="(10,3)")

    candidates = fixer._select_generalized_candidates(
        [correct, wrong_horse, truth_absent], [_truth()]
    )
    reasons = {
        row["馬名"]: row["__candidate_reasons__"] for row in candidates
    }

    assert reasons == {
        "A": ["a"],
        "WRONG": ["a", "b"],
        "NO_SLOT": ["c"],
    }


def test_generalized_closure_fetches_truth_position_keeper_for_c_candidate(
        monkeypatch):
    candidate = _row(race_num=2, ctid="(11,1)")
    keeper = _row(race_num=1, ctid="(11,2)")
    fetched_targets = []

    monkeypatch.setattr(
        fixer, "_fetch_2025_complete_rows",
        lambda connection: [candidate, keeper],
    )

    def fake_fetch_targets(connection, targets):
        fetched_targets.extend(targets)
        return [keeper]

    monkeypatch.setattr(fixer, "_fetch_target_rows", fake_fetch_targets)
    candidates, destinations = fixer.fetch_target_closure(object(), [_truth()])

    assert fetched_targets == [("250105", "東京", 1, 3)]
    assert len(candidates) == 1
    assert candidates[0]["__candidate_reasons__"] == ["c"]
    assert len(destinations) == 1
    assert destinations[0][fixer.ROW_ID] == keeper[fixer.ROW_ID]
    assert destinations[0]["__candidate_reasons__"] == []


def test_truth_and_candidate_snapshot_limits_fail_closed(monkeypatch):
    class FakeAbilityResult:
        def fetchall(self):
            return [{}, {}, {}]

    class FakeAbilityConnection:
        row_factory = None

        def execute(self, statement, params):
            assert params == (3,)
            return FakeAbilityResult()

        def close(self):
            pass

    monkeypatch.setattr(fixer, "TRUTH_ROW_LIMIT", 2)
    monkeypatch.setattr(
        fixer, "open_ability_read_only", lambda path: FakeAbilityConnection()
    )
    with pytest.raises(fixer.SafetyError, match="truth exceeds"):
        fixer.load_ability_truth("unused.db")

    class FakeRaceResult:
        def mappings(self):
            return self

        def all(self):
            return [_row(ctid="(12,1)"), _row(ctid="(12,2)"), _row(ctid="(12,3)")]

    class FakeRaceConnection:
        def execute(self, statement, params):
            assert params == {"row_limit": 3}
            return FakeRaceResult()

    monkeypatch.setattr(fixer, "CLOSURE_ROW_LIMIT", 2)
    with pytest.raises(fixer.SafetyError, match="snapshot exceeds"):
        fixer._fetch_2025_complete_rows(FakeRaceConnection())


def test_global_duplicate_count_sql_has_no_2025_date_filter():
    captured = {}

    class FakeResult:
        def scalar_one(self):
            return 0

    class FakeConnection:
        def execute(self, statement):
            captured["sql"] = str(statement)
            return FakeResult()

    assert fixer._count_scoped_duplicate_groups(FakeConnection()) == 0
    normalized_sql = " ".join(captured["sql"].split()).casefold()
    assert "date between" not in normalized_sql
    assert "group by date, place, race_num, horse_number" in normalized_sql
    assert all(fragment in normalized_sql for fragment in (
        "date is not null", "place is not null", "race_num is not null",
        "horse_number is not null",
    ))


@pytest.mark.parametrize(
    "catalog_rows",
    [
        [{
            "index_name": "uq_races_natural_key",
            "indisunique": False,
            "key_columns": ["date"],
        }],
        [{
            "index_name": "other_covering_unique",
            "indisunique": True,
            "key_columns": [
                "horse_number", "date", "race_num", "place", "extra",
            ],
        }],
    ],
)
def test_catalog_guard_rejects_named_or_covering_natural_key_unique(
        catalog_rows):
    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return catalog_rows

    class FakeConnection:
        def execute(self, statement):
            return FakeResult()

    with pytest.raises(fixer.SafetyError, match="unique index/constraint"):
        fixer._assert_no_covering_natural_key_unique(FakeConnection())


def test_catalog_guard_ignores_irrelevant_or_nonunique_indexes():
    catalog_rows = [
        {
            "index_name": "unique_date_place_only",
            "indisunique": True,
            "key_columns": ["date", "place"],
        },
        {
            "index_name": "nonunique_covering_index",
            "indisunique": False,
            "key_columns": ["date", "place", "race_num", "horse_number"],
        },
    ]

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return catalog_rows

    class FakeConnection:
        def execute(self, statement):
            return FakeResult()

    fixer._assert_no_covering_natural_key_unique(FakeConnection())


def test_bundle_validation_rejects_tampering(tmp_path):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    bundle = tmp_path / "bundle"
    fixer.write_bundle(
        bundle,
        candidate_rows=[row], destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    with (bundle / "rows.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    with pytest.raises(fixer.SafetyError, match="artifact verification failed"):
        fixer.validate_bundle(bundle / "manifest.json")


def test_apply_rejects_manifest_metadata_tamper_before_validation_or_connect(tmp_path):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    bundle = tmp_path / "bundle"
    result = fixer.write_bundle(
        bundle,
        candidate_rows=[row], destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["database_name"] = "tampered"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )

    class NoConnectEngine:
        def connect(self):  # pragma: no cover - must never be called
            raise AssertionError("manifest tamper reached the database")

    with pytest.raises(fixer.SafetyError, match="manifest hash.*file bytes"):
        fixer.run_apply(
            NoConnectEngine(), tmp_path / "missing-ability.db", tmp_path,
            manifest_path, result["manifest_sha256"], plan["plan_sha256"],
            writer_stopped=True,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.__setitem__("format_version", 2), "format_version"),
        (lambda manifest: manifest.__setitem__("mode", "reviewed"), "mode"),
        (lambda manifest: manifest["database"].pop("url_host"), "database"),
        (lambda manifest: manifest.__setitem__("unreviewed", True), "required schema"),
    ],
)
def test_manifest_schema_is_fail_closed_even_for_matching_raw_hash(
        tmp_path, mutation, message):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    bundle = tmp_path / "bundle"
    fixer.write_bundle(
        bundle,
        candidate_rows=[row], destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )

    with pytest.raises(fixer.SafetyError, match=message):
        fixer.validate_bundle(
            manifest_path,
            approved_manifest_sha256=fixer.file_sha256(manifest_path),
        )


def test_manifest_status_must_agree_with_mutation_counts(tmp_path):
    gate = fixer.evaluate_manual_fix_gate(_manual_fix_rows())

    duplicate_rows = [_row(ctid="(14,1)"), _row(ctid="(14,2)")]
    applicable_plan = fixer.build_operation_plan(
        duplicate_rows, [_truth()], manual_fix_gate=gate,
    )
    assert applicable_plan["status"] == "APPLICABLE"
    applicable_bundle = tmp_path / "applicable"
    fixer.write_bundle(
        applicable_bundle,
        candidate_rows=duplicate_rows,
        destination_rows=[],
        plan=applicable_plan,
        ability=_bundle_ability(),
        database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    mutation_as_noop = json.loads(
        (applicable_bundle / "manifest.json").read_text(encoding="utf-8")
    )
    mutation_as_noop["plan_status"] = "NO_MUTATIONS_NEEDED"
    mutation_as_noop["applicable"] = False
    with pytest.raises(
            fixer.SafetyError,
            match="NO_MUTATIONS_NEEDED status is inconsistent"):
        fixer._validate_manifest_schema(mutation_as_noop)

    nonmutation = _row(horse="NO_TRUTH", ctid="(14,3)")
    noop_plan = fixer.build_operation_plan(
        [nonmutation], [], candidate_reasons={0: ["c"]},
        manual_fix_gate=gate,
    )
    assert noop_plan["status"] == "NO_MUTATIONS_NEEDED"
    noop_bundle = tmp_path / "noop"
    fixer.write_bundle(
        noop_bundle,
        candidate_rows=[nonmutation],
        destination_rows=[],
        plan=noop_plan,
        ability=_bundle_ability(),
        database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    noop_as_mutation = json.loads(
        (noop_bundle / "manifest.json").read_text(encoding="utf-8")
    )
    noop_as_mutation["plan_status"] = "APPLICABLE"
    noop_as_mutation["applicable"] = True
    with pytest.raises(
            fixer.SafetyError,
            match="APPLICABLE status is inconsistent"):
        fixer._validate_manifest_schema(noop_as_mutation)


def test_bundle_writer_reloads_and_counts_each_csv(monkeypatch, tmp_path):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    original = fixer._write_csv

    def corrupt_candidate_csv(path, rows, columns=None):
        original(path, rows, columns)
        if path.name == "candidate_rows.csv":
            path.write_text("date\n", encoding="utf-8-sig")

    monkeypatch.setattr(fixer, "_write_csv", corrupt_candidate_csv)
    with pytest.raises(fixer.SafetyError, match="CSV.*count mismatch"):
        fixer.write_bundle(
            tmp_path / "bundle",
            candidate_rows=[row], destination_rows=[], plan=plan,
            ability=_bundle_ability(), database=_bundle_database(),
            snapshot=_bundle_snapshot(),
        )


def test_bundle_validation_requires_every_mandatory_artifact(tmp_path):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    bundle = tmp_path / "bundle"
    fixer.write_bundle(
        bundle,
        candidate_rows=[row], destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].pop("rows.jsonl")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(fixer.SafetyError, match="required artifact"):
        fixer.validate_bundle(manifest_path)


def test_bundle_validation_rejects_plan_symlink_outside_bundle(tmp_path):
    row = _row()
    plan = _with_manual_fix_gate(fixer.build_operation_plan([row], [_truth()]))
    bundle = tmp_path / "bundle"
    fixer.write_bundle(
        bundle,
        candidate_rows=[row], destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )
    plan_path = bundle / "plan.json"
    outside = tmp_path / "outside-plan.json"
    outside.write_bytes(plan_path.read_bytes())
    plan_path.unlink()
    try:
        plan_path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host policy
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(fixer.SafetyError, match="escapes its bundle"):
        fixer.validate_bundle(bundle / "manifest.json")


def test_unresolved_approved_bundle_stops_before_engine_connection(tmp_path):
    rows = [
        _row(horse="NO_TRUTH_A", ctid="(1,1)"),
        _row(horse="NO_TRUTH_B", ctid="(1,2)"),
    ]
    plan = _with_manual_fix_gate(fixer.build_operation_plan(rows, [_truth()]))
    assert plan["status"] == "NOT_APPLICABLE"
    assert _classifications(plan) == ["UNRESOLVED", "UNRESOLVED"]
    bundle = tmp_path / "unresolved"
    fixer.write_bundle(
        bundle,
        candidate_rows=rows, destination_rows=[], plan=plan,
        ability=_bundle_ability(), database=_bundle_database(),
        snapshot=_bundle_snapshot(),
    )

    class NoConnectEngine:
        def connect(self):  # pragma: no cover - must never be called
            raise AssertionError("mutation-capable DB path was reached")

    with pytest.raises(fixer.SafetyError, match="not APPLICABLE"):
        fixer.run_apply(
            NoConnectEngine(), tmp_path / "missing-ability.db", tmp_path,
            bundle / "manifest.json", fixer.file_sha256(bundle / "manifest.json"),
            plan["plan_sha256"], writer_stopped=True,
        )


def test_cli_defaults_to_dry_run_never_calls_apply_and_fails_closed(monkeypatch, tmp_path):
    calls = []

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    monkeypatch.setenv("T34B_TEST_URL", "postgresql://fixture.invalid/test")
    monkeypatch.setattr("sqlalchemy.create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(
        fixer, "run_dry_run",
        lambda engine, ability, output: (
            tmp_path / "dry-run", {
                "manifest_sha256": "manifest-hash", "plan_sha256": "plan-hash",
                "plan_status": "NOT_APPLICABLE", "applicable": False,
            }
        ),
    )
    monkeypatch.setattr(
        fixer, "run_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default CLI called apply")
        ),
    )

    assert fixer.main([
        "--database-url-env", "T34B_TEST_URL",
        "--ability-db", str(tmp_path / "ability.db"),
        "--output-dir", str(tmp_path),
    ]) == 3
    assert calls == ["dispose"]


def test_cli_no_mutations_needed_is_success_without_calling_apply(
        monkeypatch, tmp_path):
    calls = []

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    monkeypatch.setenv("T34B_TEST_URL", "postgresql://fixture.invalid/test")
    monkeypatch.setattr("sqlalchemy.create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(
        fixer, "run_dry_run",
        lambda engine, ability, output: (
            tmp_path / "dry-run", {
                "manifest_sha256": "manifest-hash", "plan_sha256": "plan-hash",
                "plan_status": "NO_MUTATIONS_NEEDED", "applicable": False,
            }
        ),
    )
    monkeypatch.setattr(
        fixer, "run_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default CLI called apply")
        ),
    )

    assert fixer.main([
        "--database-url-env", "T34B_TEST_URL",
        "--ability-db", str(tmp_path / "ability.db"),
        "--output-dir", str(tmp_path),
    ]) == 0
    assert calls == ["dispose"]


def test_cli_apply_requires_explicit_manifest_sha256(monkeypatch, tmp_path, capsys):
    calls = []

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    monkeypatch.setenv("T34B_TEST_URL", "postgresql://fixture.invalid/test")
    monkeypatch.setattr("sqlalchemy.create_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(
        fixer, "run_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("CLI reached apply without a manifest hash")
        ),
    )

    assert fixer.main([
        "--apply", "--database-url-env", "T34B_TEST_URL",
        "--approved-manifest", str(tmp_path / "manifest.json"),
        "--approved-plan-sha256", "c" * 64,
        "--writer-stopped",
    ]) == 3
    captured = capsys.readouterr()
    assert "--approved-manifest-sha256" in captured.err
    assert calls == ["dispose"]


def test_apply_uses_set_config_for_parameterized_lock_timeout():
    source = inspect.getsource(fixer.run_apply)
    assert "set_config('lock_timeout', :timeout, true)" in source
    assert "SET LOCAL lock_timeout = :timeout" not in source


def test_update_returning_trigger_change_is_rejected():
    row = _row(horse="B", race_num=1)
    plan = fixer.build_operation_plan([row], [_truth(horse="B", race=2)])
    assert plan["status"] == "APPLICABLE"
    returned = {**row, "race_num": 2, "rank": 7, fixer.ROW_ID: "(9,9)"}

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [returned]

    class FakeConnection:
        def execute(self, statement, params):
            return FakeResult()

    columns = [key for key in row if key != fixer.ROW_ID]
    with pytest.raises(fixer.SafetyError, match="UPDATE guard/RETURNING mismatch"):
        fixer._apply_operations(FakeConnection(), [row], plan, columns)


@pytest.mark.parametrize("returned_count", [0, 2])
def test_update_guard_requires_exactly_one_returning_row(returned_count):
    row = _row(horse="B", race_num=1)
    plan = fixer.build_operation_plan([row], [_truth(horse="B", race=2)])
    expected = {**row, "race_num": 2, fixer.ROW_ID: "(9,9)"}

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [expected] * returned_count

    class FakeConnection:
        def execute(self, statement, params):
            return FakeResult()

    with pytest.raises(fixer.SafetyError, match="UPDATE guard/RETURNING mismatch"):
        fixer._apply_operations(
            FakeConnection(), [row], plan,
            [key for key in row if key != fixer.ROW_ID],
        )


def test_row_guard_uses_fresh_ctid_and_every_column_null_safely():
    row = {fixer.ROW_ID: "(4,2)", "date": "250105", "horse_odds": None}
    sql, params = fixer._guard_sql(row, ["date", "horse_odds"])
    assert "ctid = CAST(:guard_ctid AS tid)" in sql
    assert '"date" IS NOT DISTINCT FROM :guard_0' in sql
    assert '"horse_odds" IS NOT DISTINCT FROM :guard_1' in sql
    assert params == {
        "guard_ctid": "(4,2)", "guard_0": "250105", "guard_1": None,
    }


@pytest.mark.parametrize(
    "actual_rows",
    [
        [],
        [_row(), _row(ctid="(0,2)")],
        [_row(rank=2)],
    ],
)
def test_truth_postcheck_requires_one_complete_matching_retained_row(
        monkeypatch, actual_rows):
    plan = fixer.build_operation_plan([_row()], [_truth()])
    monkeypatch.setattr(
        fixer, "_fetch_target_rows", lambda connection, targets: actual_rows
    )
    with pytest.raises(fixer.SafetyError, match="postcheck"):
        fixer._verify_truth_poststate(object(), plan, [_truth()])


def test_truth_postcheck_validates_delete_keeper_not_mismatching_delete_target(
        monkeypatch):
    keeper = _row(ctid="(7,1)")
    delete_target = _row(weight=501, ctid="(7,2)")
    plan = fixer.build_operation_plan([delete_target, keeper], [_truth()])
    assert sorted(_classifications(plan)) == [
        "DELETE_REDUNDANT_ERROR", "KEEP_CURRENT",
    ]

    fetched_targets = []
    compared_rows = []
    original_compare = fixer.compare_payload

    def fake_fetch(connection, targets):
        fetched_targets.extend(targets)
        return [keeper]

    def recording_compare(row, truth):
        compared_rows.append(row)
        return original_compare(row, truth)

    monkeypatch.setattr(fixer, "_fetch_target_rows", fake_fetch)
    monkeypatch.setattr(fixer, "compare_payload", recording_compare)
    # The DELETE target is deliberately veto-inconsistent.  Only its verified
    # keeper is a truth-postcheck subject under v2.3.4.
    fixer._verify_truth_poststate(object(), plan, [_truth()])
    assert fetched_targets == [("250105", "東京", 1, 3)]
    assert [fixer.logical_row_fingerprint(row) for row in compared_rows] == [
        fixer.logical_row_fingerprint(keeper)
    ]


def test_runtime_database_identity_is_sanitized_and_normalizes_neon_pooler():
    class FakeResult:
        def mappings(self):
            return self

        def one(self):
            return {"database_name": "jra", "schema_name": "public"}

    class FakeConnection:
        def __init__(self, url):
            self.engine = type("Engine", (), {"url": make_url(url)})()

        def execute(self, statement):
            return FakeResult()

    pooled = fixer._database_identity(FakeConnection(
        "postgresql://alice:top-secret@ep-safe-pooler.us-east-2.aws.neon.tech/jra"
    ))
    direct = fixer._database_identity(FakeConnection(
        "postgresql://bob:another-secret@ep-safe.us-east-2.aws.neon.tech/jra"
    ))
    other = fixer._database_identity(FakeConnection(
        "postgresql://bob:another-secret@ep-other.us-east-2.aws.neon.tech/jra"
    ))
    assert pooled == direct
    assert pooled != other
    rendered = json.dumps(pooled, ensure_ascii=False)
    assert "alice" not in rendered
    assert "top-secret" not in rendered


def test_dry_run_marks_global_duplicates_outside_2025_scope_not_applicable(
        monkeypatch, tmp_path):
    identity = {"path": "fixture", "sha256": "a" * 64}
    captured = {}

    class FakeResult:
        def mappings(self):
            return self

        def one(self):
            return {
                "transaction_time": "2026-07-14T00:00:00Z",
                "transaction_snapshot": "1:1:",
            }

    class FakeTransaction:
        committed = False
        rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    transaction = FakeTransaction()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return transaction

        def execute(self, statement, params=None):
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(fixer, "ability_identity", lambda path: dict(identity))
    monkeypatch.setattr(fixer, "load_ability_truth", lambda path: [])
    monkeypatch.setattr(fixer, "_database_identity", lambda connection: {"database_name": "fixture"})
    monkeypatch.setattr(fixer, "_fetch_race_columns", lambda connection: [])
    monkeypatch.setattr(fixer, "fetch_target_closure", lambda connection, truth: ([], []))
    monkeypatch.setattr(
        fixer, "_fetch_manual_fix_gate_rows",
        lambda connection: _manual_fix_rows(),
    )
    # One global duplicate group exists, but the 2025 candidate query found none.
    monkeypatch.setattr(fixer, "_count_scoped_duplicate_groups", lambda connection: 1)
    monkeypatch.setattr(fixer, "_timestamped_dir", lambda base, label: tmp_path / label)

    def fake_write_bundle(output, **kwargs):
        captured["plan"] = kwargs["plan"]
        return {
            "applicable": kwargs["plan"]["status"] == "APPLICABLE",
            "plan_sha256": kwargs["plan"]["plan_sha256"],
        }

    monkeypatch.setattr(fixer, "write_bundle", fake_write_bundle)
    _, manifest = fixer.run_dry_run(FakeEngine(), tmp_path / "ability.db", tmp_path)

    assert manifest["applicable"] is False
    assert captured["plan"]["status"] == "NOT_APPLICABLE"
    assert any("outside" in reason.casefold() for reason in captured["plan"]["reasons"])
    assert transaction.committed is True
    assert transaction.rolled_back is False


def test_apply_noop_rechecks_ability_hash_before_commit(monkeypatch, tmp_path):
    approved_manifest_hash = "b" * 64
    approved_plan_hash = "c" * 64
    approved_gate = fixer.evaluate_manual_fix_gate(_manual_fix_rows())
    approved_plan = {
        "version": 3, "status": "NO_MUTATIONS_NEEDED",
        "plan_sha256": approved_plan_hash, "operations": [],
    }
    approved_manifest = {
        "format_version": 3, "plan_status": "NO_MUTATIONS_NEEDED",
        "applicable": False, "plan_sha256": approved_plan_hash,
        "mode": "dry-run",
        "ability": {"sha256": "stable"},
        "database": {"database_name": "fixture"},
        "manual_fix_gate": approved_gate,
    }
    identities = iter([
        {"sha256": "stable"},
        {"sha256": "changed"},
    ])

    class FakeResult:
        pass

    class FakeTransaction:
        committed = False
        rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    transaction = FakeTransaction()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return transaction

        def execute(self, statement, params=None):
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(
        fixer, "validate_bundle",
        lambda path, *, approved_manifest_sha256: (approved_manifest, approved_plan),
    )
    monkeypatch.setattr(fixer, "ability_identity", lambda path: next(identities))
    monkeypatch.setattr(fixer, "load_ability_truth", lambda path: [])
    monkeypatch.setattr(fixer, "_database_identity", lambda connection: {"database_name": "fixture"})
    monkeypatch.setattr(fixer, "_fetch_race_columns", lambda connection: [])
    monkeypatch.setattr(fixer, "_count_table_rows", lambda connection: 0)
    monkeypatch.setattr(fixer, "fetch_target_closure", lambda connection, truth: ([], []))
    monkeypatch.setattr(fixer, "_count_scoped_duplicate_groups", lambda connection: 0)
    monkeypatch.setattr(fixer, "_timestamped_dir", lambda base, label: tmp_path / label)
    monkeypatch.setattr(
        fixer, "write_bundle",
        lambda *args, **kwargs: {"applicable": True, "plan_sha256": "noop"},
    )

    with pytest.raises(fixer.SafetyError, match=r"ability\.db (?:hash )?changed"):
        fixer.run_apply(
            FakeEngine(), tmp_path / "ability.db", tmp_path,
            tmp_path / "manifest.json", approved_manifest_hash,
            approved_plan_hash, writer_stopped=True,
        )
    assert transaction.committed is False
    assert transaction.rolled_back is True


@pytest.mark.parametrize(
    ("current_rows", "message"),
    [
        (lambda rows: rows[:-1], "not satisfied"),
        (lambda rows: [{**rows[0], "rank": 1}, *rows[1:]], "differs"),
    ],
)
def test_apply_rechecks_manual_gate_under_lock_before_backup_or_mutation(
        monkeypatch, tmp_path, current_rows, message):
    approved_manifest_hash = "b" * 64
    candidate = _row(horse="NO_TRUTH", ctid="(13,0)")
    candidate["__candidate_reasons__"] = ["c"]
    approved_gate = fixer.evaluate_manual_fix_gate(_manual_fix_rows())
    approved_plan = fixer.build_operation_plan(
        [candidate], [], manual_fix_gate=approved_gate,
    )
    approved_plan_hash = approved_plan["plan_sha256"]
    approved_manifest = {
        "format_version": 3, "plan_status": "NO_MUTATIONS_NEEDED",
        "applicable": False, "plan_sha256": approved_plan_hash,
        "mode": "dry-run", "ability": {"sha256": "stable"},
        "database": {"database_name": "fixture"},
        "manual_fix_gate": approved_gate,
    }
    sql_calls = []

    class FakeTransaction:
        committed = False
        rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    transaction = FakeTransaction()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return transaction

        def execute(self, statement, params=None):
            sql_calls.append(str(statement))
            return object()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(
        fixer, "validate_bundle",
        lambda path, *, approved_manifest_sha256: (approved_manifest, approved_plan),
    )
    monkeypatch.setattr(fixer, "ability_identity", lambda path: {"sha256": "stable"})
    monkeypatch.setattr(fixer, "load_ability_truth", lambda path: [])
    monkeypatch.setattr(fixer, "_database_identity", lambda connection: {"database_name": "fixture"})
    monkeypatch.setattr(fixer, "_fetch_race_columns", lambda connection: [])
    monkeypatch.setattr(fixer, "_count_table_rows", lambda connection: 1)
    monkeypatch.setattr(
        fixer, "fetch_target_closure",
        lambda connection, truth: ([dict(candidate)], []),
    )
    monkeypatch.setattr(
        fixer, "_fetch_manual_fix_gate_rows",
        lambda connection: current_rows(_manual_fix_rows()),
    )
    monkeypatch.setattr(
        fixer, "write_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("manual gate failure reached backup")
        ),
    )
    monkeypatch.setattr(
        fixer, "_apply_operations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("manual gate failure reached mutation")
        ),
    )

    with pytest.raises(fixer.SafetyError, match=message):
        fixer.run_apply(
            FakeEngine(), tmp_path / "ability.db", tmp_path,
            tmp_path / "manifest.json", approved_manifest_hash,
            approved_plan_hash, writer_stopped=True,
        )

    assert any("LOCK TABLE public.races" in statement for statement in sql_calls)
    assert transaction.committed is False
    assert transaction.rolled_back is True


def test_apply_no_mutations_commits_without_catalog_guard_or_mutation(
        monkeypatch, tmp_path):
    approved_manifest_hash = "b" * 64
    candidate = _row(horse="NO_TRUTH", ctid="(13,1)")
    candidate["__candidate_reasons__"] = ["c"]
    gate = fixer.evaluate_manual_fix_gate(_manual_fix_rows())
    approved_plan = fixer.build_operation_plan(
        [candidate], [], manual_fix_gate=gate,
    )
    assert approved_plan["status"] == "NO_MUTATIONS_NEEDED"
    approved_plan_hash = approved_plan["plan_sha256"]
    approved_manifest = {
        "format_version": 3, "plan_status": "NO_MUTATIONS_NEEDED",
        "applicable": False, "plan_sha256": approved_plan_hash,
        "mode": "dry-run", "ability": {"sha256": "stable"},
        "database": {"database_name": "fixture"},
        "manual_fix_gate": gate,
    }
    calls = []

    class FakeResult:
        pass

    class FakeTransaction:
        committed = False
        rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    transaction = FakeTransaction()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return transaction

        def execute(self, statement, params=None):
            calls.append(str(statement))
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(
        fixer, "validate_bundle",
        lambda path, *, approved_manifest_sha256: (approved_manifest, approved_plan),
    )
    monkeypatch.setattr(fixer, "ability_identity", lambda path: {"sha256": "stable"})
    monkeypatch.setattr(fixer, "load_ability_truth", lambda path: [])
    monkeypatch.setattr(fixer, "_database_identity", lambda connection: {"database_name": "fixture"})
    monkeypatch.setattr(fixer, "_fetch_race_columns", lambda connection: [])
    monkeypatch.setattr(fixer, "_count_table_rows", lambda connection: 1)
    monkeypatch.setattr(
        fixer, "fetch_target_closure",
        lambda connection, truth: ([dict(candidate)], []),
    )
    monkeypatch.setattr(
        fixer, "_fetch_manual_fix_gate_rows",
        lambda connection: _manual_fix_rows(),
    )
    monkeypatch.setattr(fixer, "_count_scoped_duplicate_groups", lambda connection: 0)
    monkeypatch.setattr(
        fixer, "_assert_no_covering_natural_key_unique",
        lambda connection: (_ for _ in ()).throw(
            AssertionError("NO_MUTATIONS_NEEDED performed the catalog guard")
        ),
    )
    monkeypatch.setattr(
        fixer, "_apply_operations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("NO_MUTATIONS_NEEDED attempted a mutation")
        ),
    )
    monkeypatch.setattr(
        fixer, "_verify_nonmutation_poststate",
        lambda connection, plan: calls.append("verified-nonmutation"),
    )
    monkeypatch.setattr(fixer, "_timestamped_dir", lambda base, label: tmp_path / label)
    monkeypatch.setattr(
        fixer, "write_bundle",
        lambda *args, **kwargs: {
            "applicable": False, "plan_status": "NO_MUTATIONS_NEEDED",
            "plan_sha256": approved_plan_hash,
        },
    )

    fixer.run_apply(
        FakeEngine(), tmp_path / "ability.db", tmp_path,
        tmp_path / "manifest.json", approved_manifest_hash,
        approved_plan_hash, writer_stopped=True,
    )

    assert transaction.committed is True
    assert transaction.rolled_back is False
    assert "verified-nonmutation" in calls
    assert any("LOCK TABLE public.races" in statement for statement in calls)


def test_cli_catches_sqlalchemy_error_without_printing_secret_url(
        monkeypatch, capsys):
    secret_url = "postgresql://secret-user:secret-password@example.test/jra"
    monkeypatch.setenv("T34B_SECRET_URL", secret_url)

    def fail_create_engine(*args, **kwargs):
        raise SQLAlchemyError(f"cannot connect to {secret_url}")

    monkeypatch.setattr("sqlalchemy.create_engine", fail_create_engine)
    result = fixer.main(["--database-url-env", "T34B_SECRET_URL"])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 3
    assert secret_url not in output
    assert "secret-user" not in output
    assert "secret-password" not in output


def _database_identity(raw_url):
    """Compare destinations without passwords or non-identity query options."""
    url = make_url(raw_url)
    backend = url.get_backend_name()
    default_port = 5432 if backend == "postgresql" else None
    host = (url.host or "").casefold()
    # Neon exposes direct and pooled hostnames for the same database.  Treat
    # ``ep-name-pooler...`` and ``ep-name...`` as one destination.
    first, separator, remainder = host.partition(".")
    if first.endswith("-pooler"):
        first = first[:-len("-pooler")]
    host = first + (separator + remainder if separator else "")
    return (
        backend,
        host,
        url.port or default_port,
        (url.database or "").strip("/"),
    )


def _non_production_postgres_url():
    """Return only the explicitly configured test DB; never fall back to prod."""
    test_url = os.getenv("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL is not configured; non-production PG test skipped")
    try:
        identity = _database_identity(test_url)
    except Exception as exc:  # pragma: no cover - environment-specific diagnostic
        raise RuntimeError(f"TEST_DATABASE_URL is invalid: {exc}") from exc
    if identity[0] != "postgresql":
        raise RuntimeError("TEST_DATABASE_URL must use PostgreSQL")

    production_url = os.getenv("DATABASE_URL")
    if not production_url:
        raise RuntimeError(
            "DATABASE_URL must also be configured so the test destination can "
            "be proven different from production"
        )
    try:
        production_identity = _database_identity(production_url)
    except Exception as exc:
        raise RuntimeError(
            f"DATABASE_URL identity could not be checked safely: {exc}"
        ) from exc
    if identity == production_identity:
        raise RuntimeError(
            "TEST_DATABASE_URL points to the same database as DATABASE_URL"
        )
    return test_url


def test_database_identity_ignores_password_and_query_options():
    left = "postgresql://tester:one@example.test:5432/t34b?sslmode=require"
    right = "postgresql+psycopg2://other_user:two@EXAMPLE.test/t34b?application_name=x"
    assert _database_identity(left) == _database_identity(right)
    assert _database_identity(
        "postgresql://a@ep-safe-pooler.us-east-2.aws.neon.tech/db"
    ) == _database_identity(
        "postgresql://b@ep-safe.us-east-2.aws.neon.tech/db"
    )
    assert _database_identity(
        "postgresql://a@ep-safe.us-east-2.aws.neon.tech/db"
    ) != _database_identity(
        "postgresql://a@ep-other.us-east-2.aws.neon.tech/db"
    )
    rendered = repr(_database_identity(
        "postgresql://secret-user:secret-password@example.test/db"
    ))
    assert "secret-user" not in rendered
    assert "secret-password" not in rendered


def test_non_production_guard_refuses_production_identity(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://tester:test-secret@example.test/t34b?sslmode=require",
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://production_user:production-secret@EXAMPLE.test:5432/t34b",
    )
    with pytest.raises(RuntimeError, match="same database"):
        _non_production_postgres_url()


def test_non_production_guard_requires_production_identity_for_comparison(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://tester:test-secret@example.test/t34b_test",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="must also be configured"):
        _non_production_postgres_url()


def test_non_production_guard_treats_neon_pooler_as_production_identity(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://test:test-secret@ep-safe-pooler.us-east-2.aws.neon.tech/db",
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://prod:prod-secret@ep-safe.us-east-2.aws.neon.tech/db",
    )
    with pytest.raises(RuntimeError, match="same database"):
        _non_production_postgres_url()


def test_non_production_postgres_partial_index_upsert_and_catalog():
    """Optional destructive fixture, isolated in a unique schema on TEST DB only."""
    test_url = _non_production_postgres_url()
    engine = create_engine(test_url)
    schema = "t34b_test_" + uuid.uuid4().hex
    quoted_schema = '"' + schema + '"'
    column_types = {
        "distance": "INTEGER", "total_horses": "INTEGER",
        "horse_number": "INTEGER", "rank": "DOUBLE PRECISION",
        "corner_4": "INTEGER", "horse_odds": "DOUBLE PRECISION",
        "weight": "DOUBLE PRECISION", "race_num": "INTEGER",
    }
    definitions = ", ".join(
        f'"{column}" {column_types.get(column, "TEXT")}'
        for column in updater.UPSERT_COLUMNS
    )
    predicate = (
        '"date" IS NOT NULL AND "place" IS NOT NULL '
        'AND "race_num" IS NOT NULL AND "horse_number" IS NOT NULL'
    )
    row = {column: None for column in updater.UPSERT_COLUMNS}
    row.update({
        "date": "250105", "place": "東京", "race_num": 11,
        "horse_number": 3, "馬名": "テスト馬", "rank": 1.0,
        "horse_odds": 4.2,
    })
    changed = {**row, "rank": 2.0, "horse_odds": None}

    try:
        # CREATE INDEX CONCURRENTLY must be issued on an AUTOCOMMIT connection.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            conn.execute(text(f"SET search_path TO {quoted_schema}"))
            conn.execute(text(f'CREATE TABLE "races" ({definitions})'))
            conn.execute(text(
                f'CREATE UNIQUE INDEX CONCURRENTLY "uq_races_natural_key" '
                f'ON {quoted_schema}."races" '
                '("date", "place", "race_num", "horse_number") '
                f"WHERE {predicate}"
            ))

            conn.execute(text(updater.build_races_upsert_sql()), row)
            conn.execute(text(updater.build_races_upsert_sql()), changed)
            saved = conn.execute(text(
                'SELECT COUNT(*), MAX("rank"), MAX("horse_odds") FROM "races" '
                'WHERE "date"=:date AND "place"=:place '
                'AND "race_num"=:race_num AND "horse_number"=:horse_number'
            ), row).one()
            assert tuple(saved) == (1, 2.0, 4.2)

            # Incomplete historical keys are outside the partial index.
            incomplete = {**row, "race_num": None}
            columns = ", ".join(f'"{column}"' for column in updater.UPSERT_COLUMNS)
            values = ", ".join(f":{column}" for column in updater.UPSERT_COLUMNS)
            raw_insert = text(
                f'INSERT INTO "races" ({columns}) VALUES ({values})'
            )
            conn.execute(raw_insert, [incomplete, incomplete])
            assert conn.execute(text(
                'SELECT COUNT(*) FROM "races" WHERE "race_num" IS NULL'
            )).scalar_one() == 2

            catalog = conn.execute(text("""
                SELECT i.indisunique, i.indisvalid, i.indisready,
                       pg_get_indexdef(i.indexrelid) AS indexdef,
                       pg_get_expr(i.indpred, i.indrelid) AS predicate
                  FROM pg_index i
                  JOIN pg_class idx ON idx.oid = i.indexrelid
                  JOIN pg_namespace ns ON ns.oid = idx.relnamespace
                 WHERE ns.nspname=:schema AND idx.relname='uq_races_natural_key'
            """), {"schema": schema}).one()
            assert catalog.indisunique and catalog.indisvalid and catalog.indisready
            assert all(column in catalog.indexdef for column in (
                "date", "place", "race_num", "horse_number"))
            assert all(column in catalog.predicate for column in (
                "date", "place", "race_num", "horse_number"))

            # A conflict predicate that does not imply the full index predicate
            # must not silently infer another constraint (PostgreSQL 42P10).
            wrong_sql = updater.build_races_upsert_sql().replace(
                'AND "horse_number" IS NOT NULL', "", 1
            )
            with pytest.raises(Exception) as exc_info:
                conn.execute(text(wrong_sql), row)
            original = getattr(exc_info.value, "orig", None)
            sqlstate = (getattr(original, "sqlstate", None)
                        or getattr(original, "pgcode", None))
            assert sqlstate == "42P10"
    finally:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        finally:
            engine.dispose()
