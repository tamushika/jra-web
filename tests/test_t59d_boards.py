import ast
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import backtest_t59c_lambda as t59c
import jra_ev
from api import board_market
from api.logging_store import LoggingStore


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "t59d"
FORBIDDEN = ("買い", "推奨", "◎", "⭐")


def _horses(odds):
    return [{"num": index + 1, "name": f"馬{index + 1}", "odds": value}
            for index, value in enumerate(odds)]


def _fetched(numbers):
    pairs = [(a, b) for a in numbers for b in numbers if a < b]
    return {
        "source": "jra_official", "requested_at": "2026-07-19T05:00:00+00:00",
        "received_at": "2026-07-19T05:00:01+00:00",
        "odds": {
            "place": {(number,): (2.0 + number, 2.5 + number) for number in numbers},
            "wide": {pair: (5.0, 6.0) for pair in pairs},
            "umaren": {pair: (12.0, 12.0) for pair in pairs},
        },
    }


def test_calibrated_probabilities_match_t59c_and_umaren_uses_lambda_one():
    probabilities = (0.30, 0.22, 0.17, 0.12, 0.08, 0.05, 0.035, 0.025)
    actual = board_market.derive_probabilities(probabilities)
    calibrated = t59c.ticket_probabilities(
        probabilities, board_market.LAMBDA2, board_market.LAMBDA3)
    baseline = t59c.ticket_probabilities(probabilities, 1.0, 1.0)
    assert actual["place"] == pytest.approx(calibrated["place"])
    assert actual["wide"] == pytest.approx(calibrated["wide"])
    assert actual["umaren"] == pytest.approx(baseline["umaren"])
    assert sum(actual["place"]) == pytest.approx(3.0)
    assert sum(actual["umaren"].values()) == pytest.approx(1.0)


def test_seven_runner_place_rule_and_book_band_failure():
    probabilities = (0.25, 0.20, 0.17, 0.14, 0.10, 0.08, 0.06)
    derived = board_market.derive_probabilities(probabilities, places=2)
    assert sum(derived["place"]) == pytest.approx(2.0)
    # T59c's evaluation population excluded every field-under-8 race outright,
    # so wide's fitted lambda3 (top-3 formula) is untested there and must not
    # be extrapolated - it stays undefined rather than silently reporting a
    # top-3-shaped probability (whose combination sum would misleadingly be 3
    # instead of the 1 a true reduced-field wide payout would need).
    assert derived["wide"] is None
    board = board_market.build_board(_horses([10.0] * 8), _fetched(range(1, 9)))
    assert board["status"] == "book_outside"
    assert board["message"] == "表示不可"


def test_seven_runner_wide_is_marked_unavailable_not_extrapolated():
    odds = [2.564, 3.497, 4.525, 6.410, 8.547, 12.821, 19.231]  # book sum ~1.30
    board = board_market.build_board(_horses(odds), _fetched(range(1, 8)))
    assert board["status"] == "ok"
    assert board["rows"]["wide"] == []
    assert board["rows"]["place"] and board["rows"]["umaren"]
    assert "wide" in board["unavailable_kinds"]
    assert "place" not in board["unavailable_kinds"]
    assert "umaren" not in board["unavailable_kinds"]

    rows = board_market.snapshot_rows(
        board, race_id="20260719_test_1", race_date="20260719",
        place="福島", race_no=1)
    wide_sentinel = next(row for row in rows if row["bet_type"] == "wide" and row["combo"] == "*")
    assert wide_sentinel["status"] == "unavailable"
    assert "対象外" in wide_sentinel["data_quality_flags"][0]
    # place/umaren rows for the same small field must still be populated normally.
    assert any(row["bet_type"] == "place" and row["combo"] != "*" for row in rows)
    assert any(row["bet_type"] == "umaren" and row["combo"] != "*" for row in rows)


def test_jra_trimmed_fixtures_parse_all_three_ticket_types():
    entry = (FIXTURES / "jra_entry_trimmed.html").read_text(encoding="utf-8")
    umaren = (FIXTURES / "jra_umaren_trimmed.html").read_text(encoding="utf-8")
    wide = (FIXTURES / "jra_wide_trimmed.html").read_text(encoding="utf-8")
    assert board_market.parse_place_board(entry)[(1,)] == (2.1, 2.8)
    assert board_market.parse_pair_board(umaren, "umaren")[(1, 2)] == (12.4, 12.4)
    assert board_market.parse_pair_board(wide, "wide")[(2, 3)] == (5.2, 6.0)
    real_card = (ROOT / "tests" / "fixtures" / "t48" /
                 "jump_race_20260719_kokura1.html").read_bytes().decode(
                     "cp932", errors="replace")
    assert board_market.extract_entry_cname(real_card) == (
        "pw151ou1010202602080120260719Z/A7")


def test_jra_fetch_follows_entry_navigation_without_netkeiba():
    fixtures = {
        "entry": (FIXTURES / "jra_entry_trimmed.html").read_text(encoding="utf-8"),
        "pw154": (FIXTURES / "jra_umaren_trimmed.html").read_text(encoding="utf-8"),
        "pw155": (FIXTURES / "jra_wide_trimmed.html").read_text(encoding="utf-8"),
    }

    class Response:
        def __init__(self, text): self.text = text
        def raise_for_status(self): return None

    class Session:
        def __init__(self): self.calls = []
        def post(self, url, data, **kwargs):
            self.calls.append((url, data["cname"]))
            cname = data["cname"]
            return Response(fixtures["pw154" if cname.startswith("pw154") else
                                     "pw155" if cname.startswith("pw155") else "entry"])

    session = Session()
    times = iter((datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc),
                  datetime(2026, 7, 19, 5, 0, 1, tzinfo=timezone.utc)))
    result = board_market.fetch_jra_odds(
        "pw151ouS303202602080820260719Z/15", session=session,
        now=lambda: next(times))
    assert len(session.calls) == 3
    assert all(call[0] == board_market.JRA_ODDS_URL for call in session.calls)
    assert len(result["odds"]["place"]) == 3
    assert len(result["odds"]["wide"]) == 3
    assert len(result["odds"]["umaren"]) == 3
    assert "netkeiba" not in json.dumps(session.calls).lower()


def test_board_snapshot_schema_is_p4_ready_and_idempotent(tmp_path):
    store = LoggingStore(tmp_path / "logging.db")
    board = board_market.build_board(
        _horses([3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]),
        _fetched(range(1, 9)), stage=30)
    rows = board_market.snapshot_rows(
        board, race_id="20260719:福島:08", race_date="20260719",
        place="福島", race_no=8, fetch_id="run-1")
    assert store.save_board_odds(rows) == len(rows)
    assert store.save_board_odds(rows) == 0
    with sqlite3.connect(store.db_path) as connection:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(board_odds_snapshots)")}
        stored = connection.execute(
            "SELECT count(*),count(DISTINCT bet_type) FROM board_odds_snapshots").fetchone()
        version = connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=10").fetchone()[0]
    assert {"date", "place", "r", "bet_type", "combo", "odds", "odds_low", "odds_high",
            "model_probability", "fair_odds", "gap_ratio", "requested_at",
            "received_at", "source", "stage", "status"} <= columns
    assert stored == (len(rows), 3)
    assert version == 1


def test_display_outputs_have_no_purchase_inducement_vocabulary(monkeypatch):
    horses = _horses([3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0])
    board = board_market.build_board(horses, _fetched(range(1, 9)), stage=10)
    small_field_odds = [2.564, 3.497, 4.525, 6.410, 8.547, 12.821, 19.231]
    small_field_board = board_market.build_board(
        _horses(small_field_odds), _fetched(range(1, 8)), stage=10)
    payloads = [
        (ROOT / "index_boards.html").read_text(encoding="utf-8"),
        (ROOT / "docs" / "T59d-board-preview.html").read_text(encoding="utf-8"),
        json.dumps(board, ensure_ascii=False),
        json.dumps(small_field_board, ensure_ascii=False),
        json.dumps(board_market.unavailable_board("取得失敗"), ensure_ascii=False),
        json.dumps(board_market.unavailable_board("表示不可"), ensure_ascii=False),
    ]
    for payload in payloads:
        assert not any(word in payload for word in FORBIDDEN)

    monkeypatch.setitem(jra_ev.STATE, "races", {"福島_8": {
        "venue": "福島", "race_num": 8, "start_time": "13:45", "board": board}})
    response = jra_ev.app.test_client().get("/api/boards")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert not any(word in text for word in FORBIDDEN)


def test_fetch_failure_is_explicit_and_never_reuses_previous(monkeypatch):
    rec = {
        "venue": "福島", "race_num": 8,
        "horses": _horses([3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]),
        "_board_odds_entry_cname": "pw151ou-test/00", "_log_context": {},
    }
    monkeypatch.setattr(board_market, "fetch_jra_odds",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    board = jra_ev._capture_board_snapshot(rec, 2)
    assert board["status"] == "fetch_failed"
    assert board["message"] == "取得失敗"
    assert all(not rows for rows in board["rows"].values())


def test_board_capture_rides_existing_snapshot_without_notifications(monkeypatch):
    captured = []
    monkeypatch.setattr(jra_ev, "analyze_one", lambda *_args, **_kwargs: {
        "horses": _horses([3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]),
        "n_picked": 0, "wide_picks": [], "last_update": "13:15:00",
        "odds_ok": True, "n_odds": 8,
        "_snapshot_quality": {"data_quality_flags": [],
                              "observed_at": "2026-07-19T13:15:00+09:00"},
        "_snapshot_persisted": True,
    })
    monkeypatch.setattr(
        jra_ev, "_capture_board_snapshot",
        lambda rec, stage: captured.append(stage) or {
            "status": "ok", "stage": stage, "rows": {kind: [] for kind in board_market.BET_TYPES}},
    )
    for name in ("refresh_and_alert", "_send_line", "_send_discord"):
        monkeypatch.setattr(jra_ev, name, lambda *_args, _name=name, **_kwargs:
                            (_ for _ in ()).throw(AssertionError(_name)))
    rec = {"url": "fixture://race", "venue": "福島", "race_num": 8,
           "_start_dt": datetime(2026, 7, 19, 13, 45)}
    assert jra_ev.snapshot_odds(rec, 30) is True
    assert captured == [30]
    assert rec["board"]["status"] == "ok"


def _function_ast(source, names):
    tree = ast.parse(source)
    return {node.name: ast.dump(node, include_attributes=False)
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
            and node.name in names}


def test_notification_functions_and_production_artifacts_are_unchanged():
    names = {"_send_line", "_send_discord", "refresh_and_alert"}
    before = subprocess.check_output(
        ["git", "show", "HEAD:jra_ev.py"], text=True, encoding="utf-8")
    after = (ROOT / "jra_ev.py").read_text(encoding="utf-8")
    assert _function_ast(before, names) == _function_ast(after, names)
    expected = {
        ROOT / "api" / "combo_probs.py":
            "3c17800f4616502c81d8ec518cb596c58134cedb8d02a8e83434924ad6119ffb",
        ROOT / "api" / "data_files" / "common" / "win5_ml_model.json":
            "8687f9bfa2278ed1dcafd9f13c90b08fa6b6d58f993a2c139fd39c9edbf34527",
    }
    for path, digest in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
