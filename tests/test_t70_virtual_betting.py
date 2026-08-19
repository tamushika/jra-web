import ast
import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from api import race_confidence, virtual_betting
from api.logging_store import LoggingStore


ROOT = Path(__file__).resolve().parents[1]
SHA = race_confidence.EXPECTED_MANIFEST_SHA256


def _snapshot(**overrides):
    base = {
        "date": "20260815", "place": "小倉", "r": 12, "cutoff_at": "2026-08-15T06:45:02.860960Z",
        # 予測1位 = 2番 (0.55)、市場最人気 (最小オッズ) = 1番 (2.0)。決定的にずらして
        # 本体 (CL勝率1位) と対照 (1番人気) が別馬になる前提でテストする。
        "model_probs_json": json.dumps({"1": 0.10, "2": 0.55, "3": 0.35}),
        "market_odds_json": json.dumps({"1": 2.0, "2": 3.0, "3": 5.0}),
        "selected": 1, "manifest_sha256": SHA, "race_confidence_snapshot_id": 999,
    }
    base.update(overrides)
    return base


# ─── 1. 決定 ─────────────────────────────────────────────────────────────────

def test_selected_race_records_main_top_prob_and_control_favorite():
    rows = virtual_betting.build_bet_decisions(_snapshot(), main_budget_used_yen=0)
    assert len(rows) == 2
    main = next(r for r in rows if not r["is_control"])
    control = next(r for r in rows if r["is_control"])
    assert main["horse_number"] == 2  # CL勝率1位 (0.55)
    assert main["stake_yen"] == 2000
    assert main["status"] == "pending"
    assert main["bet_type"] == "fukusho"
    assert control["horse_number"] == 1  # cutoffオッズ最小=1番人気 (2.0)
    assert control["stake_yen"] == 2000
    assert control["is_control"] == 1
    assert control["status"] == "pending"


def test_tie_break_picks_smaller_horse_number():
    tied = _snapshot(model_probs_json=json.dumps({"3": 0.5, "1": 0.5, "2": 0.4}),
                      market_odds_json=json.dumps({"3": 2.0, "1": 2.0, "2": 4.0}))
    rows = virtual_betting.build_bet_decisions(tied, main_budget_used_yen=0)
    main = next(r for r in rows if not r["is_control"])
    control = next(r for r in rows if r["is_control"])
    assert main["horse_number"] == 1  # 0.5 で1番・3番が同率 -> 馬番小
    assert control["horse_number"] == 1  # 2.0 で1番・3番が同オッズ -> 馬番小


def test_non_selected_race_records_nothing():
    assert virtual_betting.build_bet_decisions(_snapshot(selected=0), main_budget_used_yen=0) == []
    assert virtual_betting.build_bet_decisions(_snapshot(selected=None), main_budget_used_yen=0) == []


def test_manifest_sha_mismatch_records_nothing():
    assert virtual_betting.build_bet_decisions(
        _snapshot(manifest_sha256="not-the-frozen-sha"), main_budget_used_yen=0) == []


def test_missing_probability_or_odds_records_nothing():
    assert virtual_betting.build_bet_decisions(
        _snapshot(model_probs_json=None), main_budget_used_yen=0) == []
    assert virtual_betting.build_bet_decisions(
        _snapshot(market_odds_json=None), main_budget_used_yen=0) == []


def test_decision_rows_never_carry_settlement_fields():
    rows = virtual_betting.build_bet_decisions(_snapshot(), main_budget_used_yen=0)
    for row in rows:
        assert row["payout_yen"] is None
        assert row["settled_at"] is None
        assert row["status"] in ("pending", "skipped_budget")


def test_over_budget_main_is_skipped_but_control_still_recorded():
    rows = virtual_betting.build_bet_decisions(_snapshot(), main_budget_used_yen=9000)
    main = next(r for r in rows if not r["is_control"])
    control = next(r for r in rows if r["is_control"])
    assert main["status"] == "skipped_budget"
    assert main["stake_yen"] == 0
    assert main["payout_yen"] is None  # skipped時もpayout=NULL維持
    assert control["status"] == "pending"  # 対照は予算外
    assert control["stake_yen"] == 2000


def test_record_decision_is_idempotent_and_enforces_payout_null(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    inserted_first = virtual_betting.record_decision(_snapshot(), db_path=store.db_path)
    assert len(inserted_first) == 2
    inserted_second = virtual_betting.record_decision(_snapshot(), db_path=store.db_path)
    assert inserted_second == []  # 同一レースへの再呼び出しは冪等 (二重記帳なし)

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT status, payout_yen, settled_at, is_control FROM virtual_bets").fetchall()
    assert len(rows) == 2
    for status, payout_yen, settled_at, _is_control in rows:
        assert status == "pending"
        assert payout_yen is None
        assert settled_at is None


def test_record_decision_for_snapshot_id_reads_back_persisted_row(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    snapshot_id = store.save_race_confidence({
        "date": "20260815", "place": "小倉", "r": 12, "cutoff_at": "2026-08-15T06:45:02Z",
        "snapshot_source": "jra",
        "model_probs": {"1": 0.10, "2": 0.55, "3": 0.35},
        "market_odds": {"1": 2.0, "2": 3.0, "3": 5.0},
        "features": [0] * 11, "score": 0.02, "threshold": 0.008,
        "selected": True, "manifest_sha256": SHA,
    })
    inserted = virtual_betting.record_decision_for_snapshot_id(snapshot_id, db_path=store.db_path)
    assert len(inserted) == 2
    assert virtual_betting.record_decision_for_snapshot_id(None, db_path=store.db_path) == []
    assert virtual_betting.record_decision_for_snapshot_id(999999, db_path=store.db_path) == []


def test_schema_check_rejects_pending_row_with_payout_prefilled(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO virtual_bets (idempotency_key,policy_version,race_id,date,bet_type,
                   horse_number,stake_yen,decided_at,is_control,status,payout_yen,settled_at,created_at)
                   VALUES('x','v1','20260815:小倉:12','20260815','fukusho',2,2000,'2026-08-15T00:00:00Z',
                   0,'pending',999,NULL,'2026-08-15T00:00:00Z')""")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO virtual_bets (idempotency_key,policy_version,race_id,date,bet_type,
                   horse_number,stake_yen,decided_at,is_control,status,payout_yen,settled_at,created_at)
                   VALUES('y','v1','20260815:小倉:12','20260815','fukusho',2,2000,'2026-08-15T00:00:00Z',
                   0,'settled',NULL,NULL,'2026-08-15T00:00:00Z')""")


# ─── 2. 精算 ─────────────────────────────────────────────────────────────────

def _seed_bet_and_result(store, *, horse_number, finish_position, place_payout=None,
                         official_status="official", is_control=0, race_no=12):
    race_id = f"20260815:小倉:{race_no:02d}"
    virtual_betting.record_decision(
        _snapshot(r=race_no, model_probs_json=json.dumps({str(horse_number): 0.9, "9": 0.1}),
                  market_odds_json=json.dumps({str(horse_number): 2.0, "9": 20.0})),
        db_path=store.db_path)
    store.save_race_results([{
        "race_id": race_id, "horse_id": f"{race_id}:{horse_number:02d}",
        "horse_name": "テスト馬", "finish_position": finish_position,
        "official_status": official_status, "place_payout": place_payout,
        "result_fetched_at": datetime.now().astimezone(),
        "source_hash": f"{race_id}:{horse_number}", "data_quality_flags": [],
    }])
    return race_id


def test_compute_settlement_hit_place_and_miss_and_unresolved():
    hit = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000},
        {"official_status": "official", "finish_position": 2, "place_payout": 150})
    assert hit == {"status": "settled", "payout_yen": 3000}

    miss = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000},
        {"official_status": "official", "finish_position": 5, "place_payout": None})
    assert miss == {"status": "settled", "payout_yen": 0}

    unresolved_result = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000}, None)
    assert unresolved_result is None

    still_unofficial = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000},
        {"official_status": "unofficial", "finish_position": 1, "place_payout": 150})
    assert still_unofficial is None

    payout_not_yet_available = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000},
        {"official_status": "official", "finish_position": 1, "place_payout": None})
    assert payout_not_yet_available is None


def test_compute_settlement_scratched_horse_is_refunded():
    refund = virtual_betting.compute_settlement(
        {"bet_type": "fukusho", "stake_yen": 2000},
        {"official_status": "official", "finish_position": None, "place_payout": None})
    assert refund == {"status": "refunded", "payout_yen": 2000}


def test_settle_pending_bets_hits_misses_and_refunds(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    _seed_bet_and_result(store, horse_number=2, finish_position=1, place_payout=150, race_no=1)
    _seed_bet_and_result(store, horse_number=2, finish_position=5, place_payout=None, race_no=2)
    _seed_bet_and_result(store, horse_number=2, finish_position=None, place_payout=None, race_no=3)

    result = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert result["settled"] == 4  # 各レース: 本体+対照 のうち払戻確定できたもの (hit/missレース分)
    assert result["refunded"] == 2  # 取消レースの本体+対照

    with sqlite3.connect(store.db_path) as conn:
        rows = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
            "SELECT race_id, is_control, status, payout_yen FROM virtual_bets")}
    assert rows[("20260815:小倉:01", 0)] == ("settled", 3000)
    assert rows[("20260815:小倉:02", 0)] == ("settled", 0)
    assert rows[("20260815:小倉:03", 0)] == ("refunded", 2000)


def test_settle_pending_bets_is_idempotent_no_double_settlement(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    _seed_bet_and_result(store, horse_number=2, finish_position=1, place_payout=150, race_no=1)

    first = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert first["settled"] == 2
    with sqlite3.connect(store.db_path) as conn:
        after_first = dict(conn.execute(
            "SELECT idempotency_key, settled_at FROM virtual_bets").fetchall())

    second = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert second["settled"] == 0 and second["refunded"] == 0 and second["still_pending"] == 0
    with sqlite3.connect(store.db_path) as conn:
        after_second = dict(conn.execute(
            "SELECT idempotency_key, settled_at FROM virtual_bets").fetchall())
    assert after_first == after_second  # settled_atが変化していない = 再精算されていない


def test_settle_pending_bets_leaves_unresolved_race_pending(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    virtual_betting.record_decision(_snapshot(r=9), db_path=store.db_path)
    result = virtual_betting.settle_pending_bets(db_path=store.db_path)
    assert result == {"settled": 0, "refunded": 0, "still_pending": 2}
    with sqlite3.connect(store.db_path) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM virtual_bets")]
    assert statuses == ["pending", "pending"]


# ─── 3. 予算 ─────────────────────────────────────────────────────────────────

def test_daily_budget_skips_from_sixth_selected_race(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    for race_no in range(1, 7):
        virtual_betting.record_decision(_snapshot(r=race_no), db_path=store.db_path)

    with sqlite3.connect(store.db_path) as conn:
        main_rows = conn.execute(
            "SELECT race_id, stake_yen, status FROM virtual_bets WHERE is_control=0 "
            "ORDER BY race_id").fetchall()
        control_rows = conn.execute(
            "SELECT race_id, stake_yen, status FROM virtual_bets WHERE is_control=1 "
            "ORDER BY race_id").fetchall()

    assert len(main_rows) == 6
    assert [r[2] for r in main_rows[:5]] == ["pending"] * 5
    assert [r[1] for r in main_rows[:5]] == [2000] * 5
    assert main_rows[5][2] == "skipped_budget"
    assert main_rows[5][1] == 0
    assert sum(r[1] for r in main_rows) == 10000  # 日上限を超えない

    # 対照は予算外なので全6件が通常どおり記録される
    assert len(control_rows) == 6
    assert all(r[2] == "pending" and r[1] == 2000 for r in control_rows)


# ─── 4. 表示 (禁止語彙・仮想明示) ────────────────────────────────────────────

def test_disclaimer_is_present_and_forbidden_words_absent_in_dashboard_html():
    html = (ROOT / "index_perf.html").read_text(encoding="utf-8")
    assert virtual_betting.DISCLAIMER_JA in html
    assert "仮想運用" in html
    for prohibited in ("必勝", "オススメ", "⭐", "◎", "買い目はこちら", "儲か", "絶対"):
        assert prohibited not in html


def test_summarize_reports_disclaimer_and_max_drawdown(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    _seed_bet_and_result(store, horse_number=2, finish_position=1, place_payout=150, race_no=1)
    _seed_bet_and_result(store, horse_number=2, finish_position=5, place_payout=None, race_no=2)
    virtual_betting.settle_pending_bets(db_path=store.db_path)

    payload = virtual_betting.summarize(virtual_betting.load_bets(db_path=store.db_path))
    assert payload["disclaimer"] == virtual_betting.DISCLAIMER_JA
    assert payload["policy_version"] == virtual_betting.POLICY_VERSION
    assert payload["cumulative"]["main"]["n_settled"] == 2
    # レース1 (的中: 純損益+1000) でピーク+1000、レース2 (不的中: 純損益-2000) で
    # 累積-1000まで下落。最大DD = 谷(-1000) - ピーク(+1000) = -2000。
    assert payload["max_drawdown_yen"] == -2000


def test_dashboard_payload_settles_lazily_then_summarizes(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    _seed_bet_and_result(store, horse_number=2, finish_position=1, place_payout=150, race_no=1)
    payload = virtual_betting.dashboard_payload(db_path=store.db_path)
    assert payload["cumulative"]["main"]["n_settled"] == 1
    assert payload["cumulative"]["main"]["payout_yen"] == 3000


# ─── 5. AST: jra_ev.py 通知関数3つの不変検証 ────────────────────────────────

def _function_ast_dump(source: str, names: set[str]) -> dict[str, str]:
    tree = ast.parse(source)
    return {node.name: ast.dump(node, include_attributes=False)
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names}


def test_jra_ev_notification_functions_ast_unchanged_by_t70_hook():
    names = {"_send_line", "_send_discord", "refresh_and_alert"}
    before = subprocess.check_output(
        ["git", "show", "HEAD:jra_ev.py"], text=True, encoding="utf-8", cwd=ROOT)
    after = (ROOT / "jra_ev.py").read_text(encoding="utf-8")
    before_ast = _function_ast_dump(before, names)
    after_ast = _function_ast_dump(after, names)
    assert set(before_ast) == names == set(after_ast)
    assert before_ast == after_ast


def test_virtual_betting_hook_is_not_called_from_inside_protected_functions():
    # T70フックはT62bスナップショット書込直後 (_capture_race_confidence) に限定され、
    # 通知関数3つの内部には一切現れない (=通知条件を変えていないことの静的確認)。
    source = (ROOT / "jra_ev.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_send_line", "_send_discord", "refresh_and_alert"):
            body_source = ast.dump(node)
            assert "virtual_betting" not in body_source


# ─── 6. suiteフックの結線 (統合) ─────────────────────────────────────────────

def test_capture_race_confidence_hook_records_virtual_bets_for_selected_race(tmp_path, monkeypatch):
    import jra_ev

    store = LoggingStore(tmp_path / "logging.db")
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)
    monkeypatch.setattr(jra_ev.race_confidence, "build_live_score", lambda *_a, **_k: {
        "status": "ok", "label": "選定", "score": 0.02, "threshold": 0.008, "selected": True,
        "features": [0.0] * 11,
        "model_probs": {"1": 0.10, "2": 0.55, "3": 0.35},
        "market_odds": {"1": 2.0, "2": 3.0, "3": 5.0},
        "manifest_sha256": SHA, "cutoff_at": "2026-08-15T06:45:02Z", "snapshot_source": "jra",
    })
    rec = {"race_date": "20260815", "venue": "小倉", "race_num": 12,
           "_snapshot_quality": {"observed_at": "2026-08-15T06:45:02Z"}}

    confidence = jra_ev._capture_race_confidence(rec, "30")
    assert confidence["selected"] is True

    with sqlite3.connect(store.db_path) as conn:
        bets = conn.execute("SELECT race_id, is_control, horse_number FROM virtual_bets "
                            "ORDER BY is_control").fetchall()
    assert bets == [("20260815:小倉:12", 0, 2), ("20260815:小倉:12", 1, 1)]
