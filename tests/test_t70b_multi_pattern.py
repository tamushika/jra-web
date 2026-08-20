"""SPEC-T70b: P5 (全適格ベースライン) / P3 (λ複勝EV) の追加パターン。

合意の正は docs/T70-pattern-discussion.md。P1 (v1) は SPEC-T70 §1で凍結済みの
confirmatory primaryであり、tests/test_t70_virtual_betting.py が検証を継続する。
このファイルはP5/P3 (estimation-only) 固有の挙動と、P1との「ポリシー分離」
(SPEC-T70b §6-1) を検証する。
"""

import json
import sqlite3
from datetime import datetime

import pytest

from api import board_market, race_confidence, virtual_betting
from api.logging_store import LoggingStore


SHA = race_confidence.EXPECTED_MANIFEST_SHA256

# 8頭・book_sumが正常レンジ [1.15, 1.45] 内に収まる単勝オッズ (T59dのbook判定を
# 満たす、P3のcompute_p3_place_probabilitiesがNoneを返さない最小構成)。
EIGHT_HORSE_ODDS = {str(n): odds for n, odds in enumerate(
    [2.5, 3.5, 5.0, 6.0, 9.0, 12.0, 18.0, 25.0], start=1)}
EIGHT_HORSE_PROBS = {str(n): p for n, p in enumerate(
    [0.30, 0.22, 0.17, 0.12, 0.08, 0.05, 0.035, 0.025], start=1)}


def _snapshot(**overrides):
    base = {
        "date": "20260815", "place": "小倉", "r": 12, "cutoff_at": "2026-08-15T06:45:02.860960Z",
        "model_probs_json": json.dumps({"1": 0.10, "2": 0.55, "3": 0.35}),
        "market_odds_json": json.dumps({"1": 2.0, "2": 3.0, "3": 5.0}),
        "selected": 1, "manifest_sha256": SHA, "race_confidence_snapshot_id": 999,
    }
    base.update(overrides)
    return base


def _eight_horse_snapshot(**overrides):
    return _snapshot(
        model_probs_json=json.dumps(EIGHT_HORSE_PROBS),
        market_odds_json=json.dumps(EIGHT_HORSE_ODDS),
        **overrides,
    )


def _seed_board(store, *, race_id, date, place, r, received_at, odds_low_by_horse,
                status="ok", stage=virtual_betting.P3_BOARD_STAGE):
    """board_odds_snapshots (bet_type='place') を1バッチ分 (同一received_at) 投入する。"""
    rows = [{
        "race_id": race_id, "date": date, "place": place, "r": r,
        "bet_type": virtual_betting.P3_BOARD_BET_TYPE, "combo": str(horse_number),
        "model_probability": None, "fair_odds": None, "odds": odds_low, "odds_low": odds_low,
        "odds_high": odds_low, "gap_ratio": None,
        "requested_at": received_at, "received_at": received_at, "source": "jra_official",
        "stage": stage, "fetch_id": f"fetch:{received_at}", "status": status,
        "data_quality_flags": [],
    } for horse_number, odds_low in odds_low_by_horse.items()]
    store.save_board_odds(rows)


# ─── 1. ポリシー分離 (SPEC-T70b §6-1) ────────────────────────────────────────

def test_p5_p3_addition_does_not_change_p1_decision_rows(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    snapshot = _snapshot()

    # P1のみを先に記録し、その結果を「素のP1挙動」の基準として保持する。
    baseline = virtual_betting.record_decision(snapshot, db_path=store.db_path)
    assert len(baseline) == 2

    # P5/P3を追加してもP1の行 (値・件数) は変わらない。冪等キーも衝突しない
    # (INSERT OR IGNOREが「新規0件」ではなく実際に別ポリシーの行を追加する)。
    p5_rows = virtual_betting.record_decision_p5(snapshot, db_path=store.db_path)
    p3_rows = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(p5_rows) == 2
    assert len(p3_rows) == 1  # 3頭のみ -> skipped_data

    with sqlite3.connect(store.db_path) as conn:
        v1_rows = conn.execute(
            "SELECT idempotency_key, race_id, horse_number, stake_yen, status FROM virtual_bets "
            "WHERE policy_version='v1' ORDER BY is_control").fetchall()
        all_keys = [r[0] for r in conn.execute("SELECT idempotency_key FROM virtual_bets").fetchall()]

    assert v1_rows == [
        ("v1:20260815:小倉:12:fukusho:main", "20260815:小倉:12", 2, 2000, "pending"),
        ("v1:20260815:小倉:12:fukusho:control", "20260815:小倉:12", 1, 2000, "pending"),
    ]
    assert len(all_keys) == len(set(all_keys))  # 冪等キー衝突なし (P1/P5/P3で完全分離)
    assert sum(1 for k in all_keys if k.startswith("v1:")) == 2
    assert sum(1 for k in all_keys if k.startswith("p5-v1:")) == 2
    assert sum(1 for k in all_keys if k.startswith("p3-v1:")) == 1


# ─── 2. P5 (全適格ベースライン) ───────────────────────────────────────────────

def test_p5_records_for_non_selected_status_ok_row():
    for selected in (0, None):
        rows = virtual_betting.build_bet_decisions_p5(_snapshot(selected=selected), main_budget_used_yen=0)
        assert len(rows) == 2
        main = next(r for r in rows if not r["is_control"])
        control = next(r for r in rows if r["is_control"])
        assert main["policy_version"] == "p5-v1"
        assert main["horse_number"] == 2  # CL勝率1位 (P1と同じ選択関数)
        assert main["stake_yen"] == virtual_betting.P5_STAKE_YEN == 1000
        assert main["status"] == "pending"
        assert control["horse_number"] == 1  # 1番人気
        assert control["stake_yen"] == 1000
        assert control["is_control"] == 1


def test_p5_manifest_mismatch_and_missing_data_record_nothing():
    assert virtual_betting.build_bet_decisions_p5(
        _snapshot(manifest_sha256="not-the-frozen-sha"), main_budget_used_yen=0) == []
    assert virtual_betting.build_bet_decisions_p5(
        _snapshot(model_probs_json=None), main_budget_used_yen=0) == []
    assert virtual_betting.build_bet_decisions_p5(
        _snapshot(market_odds_json=None), main_budget_used_yen=0) == []


def test_selected_race_records_both_p1_and_p5(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    snapshot = _snapshot(selected=1)
    v1_rows = virtual_betting.record_decision(snapshot, db_path=store.db_path)
    p5_rows = virtual_betting.record_decision_p5(snapshot, db_path=store.db_path)
    assert len(v1_rows) == 2
    assert len(p5_rows) == 2
    with sqlite3.connect(store.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM virtual_bets").fetchone()[0]
    assert count == 4  # T62b選定レースはP1とP5の両方が独立財布として記録する


def test_p5_daily_budget_skips_from_eleventh_selected_race(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    for race_no in range(1, 12):
        virtual_betting.record_decision_p5(_snapshot(r=race_no), db_path=store.db_path)

    with sqlite3.connect(store.db_path) as conn:
        main_rows = conn.execute(
            "SELECT stake_yen, status FROM virtual_bets WHERE is_control=0 AND policy_version='p5-v1' "
            "ORDER BY race_id").fetchall()
        control_rows = conn.execute(
            "SELECT stake_yen, status FROM virtual_bets WHERE is_control=1 AND policy_version='p5-v1' "
            "ORDER BY race_id").fetchall()

    assert len(main_rows) == 11
    assert [r[1] for r in main_rows[:10]] == ["pending"] * 10
    assert main_rows[10] == (0, "skipped_budget")
    assert sum(r[0] for r in main_rows) == 10000  # 日上限¥10,000 (独立財布)
    assert len(control_rows) == 11
    assert all(r == (1000, "pending") for r in control_rows)  # 対照は予算外


def test_p5_idempotent(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    first = virtual_betting.record_decision_p5(_snapshot(), db_path=store.db_path)
    second = virtual_betting.record_decision_p5(_snapshot(), db_path=store.db_path)
    assert len(first) == 2
    assert second == []


# ─── 3. P3 (λ複勝EV) ─────────────────────────────────────────────────────────

def test_select_p3_ev_horse_exact_threshold_buys_and_below_does_not():
    # ちょうど1.00 (2.0 * 0.5) は購入対象。
    assert virtual_betting.select_p3_ev_horse({1: 0.5}, {1: 2.0}) == (1, 1.0)
    # 1.00未満は対象外 -> None (何も買わない)。
    assert virtual_betting.select_p3_ev_horse({1: 0.4999}, {1: 2.0}) is None


def test_select_p3_ev_horse_tie_break_picks_smaller_horse_number():
    place_probs = {3: 0.5, 1: 0.5, 2: 0.4}
    odds_low = {3: 2.0, 1: 2.0, 2: 2.0}  # 馬1・馬3はev=1.0で同値、馬2はev=0.8で対象外
    chosen = virtual_betting.select_p3_ev_horse(place_probs, odds_low)
    assert chosen == (1, 1.0)


def test_select_p3_ev_horse_picks_max_ev_among_multiple_qualifiers():
    place_probs = {1: 0.5, 2: 0.6}
    odds_low = {1: 2.0, 2: 2.0}  # ev: 馬1=1.0, 馬2=1.2
    assert virtual_betting.select_p3_ev_horse(place_probs, odds_low) == (2, pytest.approx(1.2))


def test_compute_p3_place_probabilities_matches_board_market_and_gates_seven_or_fewer():
    market_odds = {int(k): v for k, v in EIGHT_HORSE_ODDS.items()}
    result = virtual_betting.compute_p3_place_probabilities(market_odds)
    assert result is not None
    numbers = sorted(market_odds)
    odds_list = [market_odds[n] for n in numbers]
    probabilities, _book_sum = board_market.market_probabilities(odds_list)
    expected = board_market.derive_probabilities(probabilities, places=3)["place"]
    for index, number in enumerate(numbers):
        assert result[number] == pytest.approx(expected[index])

    seven_horse_odds = {int(k): v for k, v in list(EIGHT_HORSE_ODDS.items())[:7]}
    assert virtual_betting.compute_p3_place_probabilities(seven_horse_odds) is None


def test_build_bet_decisions_p3_seven_or_fewer_runners_is_skipped_data():
    snapshot = _snapshot()  # 3頭のみ (デフォルトfixture)
    rows = virtual_betting.build_bet_decisions_p3(
        snapshot, main_budget_used_yen=0, board_odds_low={1: 2.0, 2: 3.0, 3: 5.0})
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_data"
    assert rows[0]["stake_yen"] == 0
    assert rows[0]["is_control"] == 0
    assert rows[0]["payout_yen"] is None and rows[0]["settled_at"] is None
    assert rows[0]["policy_version"] == "p3-v1"


def test_build_bet_decisions_p3_board_unavailable_is_skipped_data():
    snapshot = _eight_horse_snapshot()
    rows = virtual_betting.build_bet_decisions_p3(
        snapshot, main_budget_used_yen=0, board_odds_low=None)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_data"
    assert rows[0]["decision_odds"] is None
    assert rows[0]["decision_prob"] is None


def test_build_bet_decisions_p3_records_pending_bet_when_ev_at_or_above_threshold():
    snapshot = _eight_horse_snapshot()
    # 馬8 (最長オッズ・最小の複勝確率) にわざと極端に高いodds_lowを与えて
    # evを1.00以上に確実に押し上げる (実際の複勝オッズがこれほど高いことは
    # ないが、閾値到達時に「1頭だけ買う」ロジックの単体検証が目的)。
    board_odds_low = {1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5, 5: 1.5, 6: 1.5, 7: 1.5, 8: 100.0}
    rows = virtual_betting.build_bet_decisions_p3(
        snapshot, main_budget_used_yen=0, board_odds_low=board_odds_low)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["horse_number"] == 8
    assert row["stake_yen"] == virtual_betting.P3_STAKE_YEN == 1000
    assert row["is_control"] == 0
    assert row["decision_odds"] == 100.0
    assert row["decision_prob"] is not None and row["decision_prob"] > 0


def test_build_bet_decisions_p3_all_below_threshold_records_nothing():
    snapshot = _eight_horse_snapshot()
    board_odds_low = {n: 1.01 for n in range(1, 9)}  # ev<1.00 (odds_low極小)
    rows = virtual_betting.build_bet_decisions_p3(
        snapshot, main_budget_used_yen=0, board_odds_low=board_odds_low)
    assert rows == []  # ベット0の日は正常 (行も記録しない)


def test_build_bet_decisions_p3_respects_daily_budget():
    snapshot = _eight_horse_snapshot()
    board_odds_low = {1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5, 5: 1.5, 6: 1.5, 7: 1.5, 8: 100.0}
    rows = virtual_betting.build_bet_decisions_p3(
        snapshot, main_budget_used_yen=9500, board_odds_low=board_odds_low)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_budget"
    assert rows[0]["stake_yen"] == 0
    assert rows[0]["horse_number"] == 8  # 選択自体は行われる (ベットのみ見送り)


def test_record_decision_p3_reads_board_odds_snapshots_and_is_idempotent(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = "20260815:小倉:12"
    cutoff_at = "2026-08-15T06:45:02.860960Z"
    _seed_board(store, race_id=race_id, date="20260815", place="小倉", r=12,
               received_at="2026-08-15T06:44:30Z",
               odds_low_by_horse={n: 1.5 for n in range(1, 8)} | {8: 100.0})
    snapshot = _eight_horse_snapshot(cutoff_at=cutoff_at)

    first = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(first) == 1
    assert first[0]["status"] == "pending"
    assert first[0]["horse_number"] == 8

    second = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert second == []  # 同一レースへの再呼び出しは冪等


def test_record_decision_p3_matches_board_received_a_few_seconds_after_cutoff(tmp_path):
    # 実測ログどおり: 板取得 (board_market.fetch_jra_odds) はcutoff_at (T62bの
    # observed_at) より数秒後に完了する。同一ループ由来なので許容誤差内に
    # 一致させる (_load_p3_board_odds のdocstring参照、単純な<=ではない)。
    store = LoggingStore(tmp_path / "logging.db")
    race_id = "20260815:小倉:12"
    cutoff_at = "2026-08-15T06:45:02.860960Z"
    _seed_board(store, race_id=race_id, date="20260815", place="小倉", r=12,
               received_at="2026-08-15T06:45:09.534745Z",
               odds_low_by_horse={n: 1.5 for n in range(1, 8)} | {8: 100.0})
    snapshot = _eight_horse_snapshot(cutoff_at=cutoff_at)

    rows = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["horse_number"] == 8


def test_record_decision_p3_ignores_board_snapshot_far_outside_tolerance(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = "20260815:小倉:12"
    cutoff_at = "2026-08-15T06:45:02.860960Z"
    # 許容誤差 (15分) を大きく超えて離れた板は「同一ループ由来」とみなさず
    # 対象外 (skipped_data)。
    _seed_board(store, race_id=race_id, date="20260815", place="小倉", r=12,
               received_at="2026-08-15T08:00:00Z",
               odds_low_by_horse={n: 1.5 for n in range(1, 8)} | {8: 100.0})
    snapshot = _eight_horse_snapshot(cutoff_at=cutoff_at)

    rows = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_data"


def test_record_decision_p3_board_never_fetched_is_skipped_data(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    snapshot = _eight_horse_snapshot()
    rows = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_data"


# ─── 4. suiteフックの結線 (統合、AST不変は test_t70_virtual_betting.py が検証) ──

def test_hook_records_p1_p5_p3_together_for_a_selected_race(tmp_path, monkeypatch):
    import jra_ev

    store = LoggingStore(tmp_path / "logging.db")
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)
    monkeypatch.setattr(jra_ev.race_confidence, "build_live_score", lambda *_a, **_k: {
        "status": "ok", "label": "選定", "score": 0.02, "threshold": 0.008, "selected": True,
        "features": [0.0] * 11,
        "model_probs": EIGHT_HORSE_PROBS, "market_odds": EIGHT_HORSE_ODDS,
        "manifest_sha256": SHA, "cutoff_at": "2026-08-15T06:45:02Z", "snapshot_source": "jra",
    })
    _seed_board(store, race_id="20260815:小倉:12", date="20260815", place="小倉", r=12,
               received_at="2026-08-15T06:44:30Z",
               odds_low_by_horse={n: 1.5 for n in range(1, 8)} | {8: 100.0})
    rec = {"race_date": "20260815", "venue": "小倉", "race_num": 12,
           "_snapshot_quality": {"observed_at": "2026-08-15T06:45:02Z"}}

    jra_ev._capture_race_confidence(rec, "30")

    with sqlite3.connect(store.db_path) as conn:
        by_policy = conn.execute(
            "SELECT policy_version, COUNT(*) FROM virtual_bets GROUP BY policy_version"
        ).fetchall()
    assert dict(by_policy) == {"v1": 2, "p5-v1": 2, "p3-v1": 1}


# ─── 5. 表示 (jra_perf.py / index_perf.html 向けの集計、SPEC-T70b §5) ─────────

def test_p3_calibration_gap_is_none_without_settled_bets():
    assert virtual_betting.p3_calibration_gap([]) is None
    pending_only = [{"policy_version": "p3-v1", "is_control": 0, "status": "pending",
                     "decision_prob": 0.4, "payout_yen": None}]
    assert virtual_betting.p3_calibration_gap(pending_only) is None


def test_p3_calibration_gap_matches_predicted_vs_realized(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    race_id = "20260815:小倉:12"
    _seed_board(store, race_id=race_id, date="20260815", place="小倉", r=12,
               received_at="2026-08-15T06:44:30Z",
               odds_low_by_horse={n: 1.5 for n in range(1, 8)} | {8: 100.0})
    snapshot = _eight_horse_snapshot()
    decisions = virtual_betting.record_decision_p3(snapshot, db_path=store.db_path)
    assert len(decisions) == 1
    predicted_prob = decisions[0]["decision_prob"]
    assert predicted_prob is not None

    store.save_race_results([{
        "race_id": race_id, "horse_id": f"{race_id}:08", "horse_name": "テスト馬",
        "finish_position": 1, "official_status": "official", "place_payout": 150,
        "result_fetched_at": datetime.now().astimezone(),
        "source_hash": f"{race_id}:8", "data_quality_flags": [],
    }])
    virtual_betting.settle_pending_bets(db_path=store.db_path)

    bets = virtual_betting.load_bets(db_path=store.db_path)
    gap = virtual_betting.p3_calibration_gap(bets)
    assert gap == {
        "n": 1, "predicted_place_rate": pytest.approx(predicted_prob),
        "realized_place_rate": 1.0, "gap": pytest.approx(1.0 - predicted_prob),
    }


def test_multi_policy_dashboard_payload_has_pattern_blocks_and_labels(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    virtual_betting.record_decision(_snapshot(), db_path=store.db_path)
    virtual_betting.record_decision_p5(_snapshot(), db_path=store.db_path)
    virtual_betting.record_decision_p3(_snapshot(), db_path=store.db_path)  # 3頭のみ -> skipped_data

    payload = virtual_betting.multi_policy_dashboard_payload(db_path=store.db_path)
    assert set(payload["patterns"]) == {"v1", "p5-v1", "p3-v1"}
    assert payload["patterns"]["v1"]["cumulative"]["main"]["n_bets"] == 1
    assert payload["patterns"]["p5-v1"]["cumulative"]["main"]["n_bets"] == 1
    assert payload["patterns"]["p3-v1"]["cumulative"]["main"]["n_skipped_data"] == 1
    assert payload["labels"]["v1"] == virtual_betting.CONFIRMATORY_LABEL_JA
    assert payload["labels"]["p5-v1"] == virtual_betting.ESTIMATION_DISCLAIMER_JA
    assert payload["labels"]["p3-v1"] == virtual_betting.ESTIMATION_DISCLAIMER_JA
    assert set(payload["estimation_only_policies"]) == {"p5-v1", "p3-v1"}
    assert payload["disclaimer"] == virtual_betting.DISCLAIMER_JA
    assert payload["p3_calibration"] is None  # 精算済み本体ベットがまだ無い
