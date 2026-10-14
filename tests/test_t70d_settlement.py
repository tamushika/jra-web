"""SPEC-T70d: 精算が永久に止まる経路 (race_results が途中で欠けているレース) の
安全網テスト。

対象は `api/virtual_betting.py` の `compute_settlement` (純関数、安全網の判定
本体) と `settle_pending_bets` (DB統合、race_rows の取得と反映)。決定ロジック・
冪等キー・スキーマは変更していないため、ここでは精算ロジックのみを検証する。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from api import virtual_betting
from api.logging_store import LoggingStore


# ─── 1. compute_settlement 純関数レベル (SPEC §4 項目1-5) ───────────────────

def _row(finish_position, official_status="official", place_payout=None):
    return {
        "finish_position": finish_position,
        "official_status": official_status,
        "place_payout": place_payout,
    }


def test_item1_top3_complete_and_bet_horse_missing_settles_as_miss():
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    race_rows = [
        _row(1, place_payout=150),
        _row(2, place_payout=200),
        _row(3, place_payout=350),
        _row(4),
        _row(5),
    ]
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=race_rows)
    assert outcome == {"status": "settled", "payout_yen": 0}


def test_item2_missing_one_of_top3_stays_pending():
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    # 2着の行が欠けている (1着・3着のみ) → 判定不能、保留のまま
    race_rows = [
        _row(1, place_payout=150),
        _row(3, place_payout=350),
    ]
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=race_rows)
    assert outcome is None


def test_item3_non_official_status_rows_do_not_count_stays_pending():
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    race_rows = [
        _row(1, official_status="unofficial", place_payout=150),
        _row(2, official_status="unofficial", place_payout=200),
        _row(3, official_status="unofficial", place_payout=350),
    ]
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=race_rows)
    assert outcome is None


def test_item4_zero_result_rows_stays_pending():
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=[])
    assert outcome is None
    # race_rows自体を渡さない (None) 場合も従来どおり保留
    assert virtual_betting.compute_settlement(bet, None, race_rows=None) is None


def test_item5_small_field_7_or_fewer_only_needs_top2():
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    # 7頭以下: 3着の行は存在するがplace_payoutがNone (複勝は2着まで)
    race_rows = [
        _row(1, place_payout=110),
        _row(2, place_payout=140),
        _row(3, place_payout=None),
        _row(4),
        _row(5),
    ]
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=race_rows)
    assert outcome == {"status": "settled", "payout_yen": 0}


def test_item5b_small_field_without_rank3_row_at_all_stays_pending():
    # 3着の行自体が存在しない (阪神:02型の実データでは3着行は必ず存在して
    # いたため、このケースは観測されていないが保守的に保留を維持する)
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    race_rows = [_row(1, place_payout=110), _row(2, place_payout=140)]
    outcome = virtual_betting.compute_settlement(bet, None, race_rows=race_rows)
    assert outcome is None


def test_normal_path_with_result_row_ignores_race_rows_safety_net():
    # result_row が存在する通常系は race_rows を渡しても無視される (既存の
    # 正常系判定を一切変更しない)
    bet = {"bet_type": "fukusho", "stake_yen": 2000}
    result_row = {"official_status": "official", "finish_position": 2, "place_payout": 150}
    # race_rowsが(意図的に)安全網不成立な内容でも、result_rowがある限り無関係
    outcome = virtual_betting.compute_settlement(bet, result_row, race_rows=[])
    assert outcome == {"status": "settled", "payout_yen": 3000}


# ─── 2. settle_pending_bets 統合レベル (SPEC §4 項目6-7) ────────────────────

def _snapshot_for_race(race_no: int, horse_number: int) -> dict:
    import json

    return {
        "date": "20260815", "place": "小倉", "r": race_no,
        "cutoff_at": "2026-08-15T06:45:02.860960Z",
        "model_probs_json": json.dumps({str(horse_number): 0.9, "9": 0.1}),
        "market_odds_json": json.dumps({str(horse_number): 2.0, "9": 20.0}),
        "selected": 1, "manifest_sha256": _expected_sha(),
        "race_confidence_snapshot_id": 999,
    }


def _expected_sha() -> str:
    from api import race_confidence

    return race_confidence.EXPECTED_MANIFEST_SHA256


def _seed_main_bet(store: LoggingStore, *, race_no: int, horse_number: int) -> str:
    virtual_betting.record_decision(_snapshot_for_race(race_no, horse_number), db_path=store.db_path)
    return f"20260815:小倉:{race_no:02d}"


def test_item6_normal_path_hit_and_miss_rows_unaffected_by_settlement_change(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id_hit = _seed_main_bet(store, race_no=1, horse_number=2)
    race_id_miss = _seed_main_bet(store, race_no=2, horse_number=2)
    store.save_race_results([
        {"race_id": race_id_hit, "horse_id": f"{race_id_hit}:02", "horse_name": "テスト馬",
         "finish_position": 1, "official_status": "official", "place_payout": 150,
         "result_fetched_at": datetime.now().astimezone(), "source_hash": "hit", "data_quality_flags": []},
        {"race_id": race_id_miss, "horse_id": f"{race_id_miss}:02", "horse_name": "テスト馬",
         "finish_position": 5, "official_status": "official", "place_payout": None,
         "result_fetched_at": datetime.now().astimezone(), "source_hash": "miss", "data_quality_flags": []},
    ])

    result = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert result["settled"] == 4  # race1(本体+対照) + race2(本体+対照)

    with sqlite3.connect(store.db_path) as conn:
        rows = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
            "SELECT race_id, is_control, status, payout_yen FROM virtual_bets")}
    assert rows[(race_id_hit, 0)] == ("settled", 3000)
    assert rows[(race_id_miss, 0)] == ("settled", 0)


def test_safety_net_settles_stuck_bet_when_top3_official_and_bet_horse_absent(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = _seed_main_bet(store, race_no=11, horse_number=15)  # 賭けた馬=15番
    # race_resultsには1-5着のみ保存 (15番の行は無い) : 20260829:新潟:11の実例を模す
    store.save_race_results([
        {"race_id": race_id, "horse_id": f"{race_id}:{n:02d}", "horse_name": f"馬{n}",
         "finish_position": pos, "official_status": "official",
         "place_payout": payout,
         "result_fetched_at": datetime.now().astimezone(), "source_hash": f"r{pos}",
         "data_quality_flags": []}
        for n, pos, payout in [(18, 1, 2220), (11, 2, 540), (17, 3, 880), (5, 4, None), (3, 5, None)]
    ])

    result = virtual_betting.settle_pending_bets(db_path=store.db_path)
    # 本体(15番, 行が無い→安全網で外れ確定) + 対照(9番, こちらも行が無い→同様)
    assert result["settled"] == 2
    assert result["still_pending"] == 0

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT horse_number, is_control, status, payout_yen, settled_at FROM virtual_bets "
            "WHERE race_id=?", (race_id,)).fetchall()
    for horse_number, is_control, status, payout_yen, settled_at in rows:
        assert status == "settled"
        assert payout_yen == 0
        assert settled_at is not None


def test_item7_idempotent_safety_net_settlement_does_not_change_on_rerun(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = _seed_main_bet(store, race_no=11, horse_number=15)
    store.save_race_results([
        {"race_id": race_id, "horse_id": f"{race_id}:{n:02d}", "horse_name": f"馬{n}",
         "finish_position": pos, "official_status": "official",
         "place_payout": payout,
         "result_fetched_at": datetime.now().astimezone(), "source_hash": f"r{pos}",
         "data_quality_flags": []}
        for n, pos, payout in [(18, 1, 2220), (11, 2, 540), (17, 3, 880), (5, 4, None), (3, 5, None)]
    ])

    first = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert first["settled"] == 2
    with sqlite3.connect(store.db_path) as conn:
        after_first = dict(conn.execute(
            "SELECT idempotency_key, payout_yen || '|' || settled_at FROM virtual_bets").fetchall())

    second = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert second == {"settled": 0, "refunded": 0, "still_pending": 0}
    with sqlite3.connect(store.db_path) as conn:
        after_second = dict(conn.execute(
            "SELECT idempotency_key, payout_yen || '|' || settled_at FROM virtual_bets").fetchall())
    assert after_first == after_second  # payout_yen/settled_atが再実行で変化しない


def test_unresolved_race_without_top3_still_pending_via_settle_pending_bets(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = _seed_main_bet(store, race_no=9, horse_number=15)
    # 1着の行しか無い (2・3着が欠ける) → 安全網不成立、保留のまま
    store.save_race_results([
        {"race_id": race_id, "horse_id": f"{race_id}:01", "horse_name": "馬1",
         "finish_position": 1, "official_status": "official", "place_payout": 150,
         "result_fetched_at": datetime.now().astimezone(), "source_hash": "r1",
         "data_quality_flags": []},
    ])
    result = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert result == {"settled": 0, "refunded": 0, "still_pending": 2}
    with sqlite3.connect(store.db_path) as conn:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM virtual_bets WHERE race_id=?", (race_id,))]
    assert statuses == ["pending", "pending"]
