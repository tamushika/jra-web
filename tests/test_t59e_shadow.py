import json
import math
import sqlite3
from pathlib import Path

import pytest

import backtest_t59e_shadow as t59e


def _gate_db(path, races):
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE board_odds_snapshots (
            board_odds_snapshot_id INTEGER PRIMARY KEY, date TEXT, race_id TEXT,
            stage TEXT, status TEXT)""")
        connection.executemany(
            "INSERT INTO board_odds_snapshots(date,race_id,stage,status) VALUES(?,?,?,?)",
            [(f"202608{index % 4 + 1:02d}", f"race-{index:03d}", "30", "ok")
             for index in range(races)])


def test_accumulation_gate_blocks_below_both_fixed_thresholds(tmp_path):
    db = tmp_path / "below.db"
    _gate_db(db, 199)
    status = t59e.accumulation_gate(db)
    assert status == t59e.GateStatus(event_dates=4, races=199)
    with pytest.raises(RuntimeError, match=r"199 races / 200 required"):
        t59e.require_accumulation_gate(db)

    empty = tmp_path / "no-table.db"
    sqlite3.connect(empty).close()
    assert t59e.accumulation_gate(empty) == t59e.GateStatus(0, 0)


def test_accumulation_gate_runs_only_at_four_dates_and_200_races(tmp_path):
    db = tmp_path / "ready.db"
    _gate_db(db, 200)
    assert t59e.require_accumulation_gate(db).ready is True


def test_logloss_definitions_match_closed_form():
    observations = [(0.8, True), (0.25, False)]
    expected = (-math.log(0.8) - math.log(0.75)) / 2.0
    assert t59e.bernoulli_logloss(observations) == pytest.approx(expected)
    assert t59e.realised_logloss(0.2) == pytest.approx(-math.log(0.2))


def _capture(stage, rows, race_id="20260801:東京:01"):
    probabilities = (0.24, 0.20, 0.16, 0.13, 0.10, 0.07, 0.06, 0.04)
    return t59e.Capture(
        race_id=race_id, date="20260801", stage=str(stage), fetch_id=f"f-{stage}",
        received_at=f"2026-08-01T00:{stage}:00Z", board_rows=rows,
        horse_numbers=tuple(range(1, 9)), win_probabilities=probabilities,
        book_sum=1.25,
    )


def test_primary_ticket_losses_use_race_mean_and_actual_top_three():
    base_capture = _capture("30", {ticket: {} for ticket in t59e.TICKETS})
    baseline = t59e._baseline_maps(base_capture)
    rows = {}
    for ticket in t59e.TICKETS:
        rows[ticket] = {
            combo: {"model_probability": probability}
            for combo, probability in baseline[ticket].items()
        }
    capture = _capture("30", rows)
    race_rows, observations, audit = t59e.evaluate_primary(
        {(capture.race_id, "30"): capture},
        {capture.race_id: {"finish": (1, 3, 2), "place_payouts": {}}})
    assert len(race_rows) == 3
    assert all(row["candidate_logloss"] == pytest.approx(row["baseline_logloss"])
               for row in race_rows)
    place = next(row for row in race_rows if row["ticket"] == "place")
    expected_place = t59e.bernoulli_logloss([
        (baseline["place"][str(number)], number in {1, 2, 3})
        for number in range(1, 9)])
    assert place["candidate_logloss"] == pytest.approx(expected_place)
    assert audit == {"evaluated_place": 1, "evaluated_umaren": 1,
                     "evaluated_wide": 1}
    assert observations


def test_book_band_and_result_ties_are_excluded(tmp_path):
    assert t59e._normalise_win_odds([
        {"horse_id": f"r:{number:02d}", "win_odds": 10.0}
        for number in range(1, 9)]) is None
    valid = t59e._normalise_win_odds([
        {"horse_id": f"r:{number:02d}", "win_odds": odds, "field_size": 8}
        for number, odds in enumerate((3, 4, 6, 8, 10, 12, 15, 20), 1)])
    assert valid is not None and t59e.BOOK_MIN <= valid[2] <= t59e.BOOK_MAX
    incomplete = [
        {"horse_id": f"r:{number:02d}", "win_odds": odds, "field_size": 9}
        for number, odds in enumerate((3, 4, 6, 8, 10, 12, 15, 20), 1)]
    assert t59e._normalise_win_odds(incomplete) is None

    db = tmp_path / "results.db"
    with sqlite3.connect(db) as connection:
        connection.execute("""CREATE TABLE race_results (
            race_id TEXT, horse_id TEXT, finish_position INTEGER,
            official_status TEXT, place_payout INTEGER)""")
        connection.executemany(
            "INSERT INTO race_results VALUES(?,?,?,?,?)", [
                ("clean", "clean:01", 1, "official", 120),
                ("clean", "clean:02", 2, "official", 150),
                ("clean", "clean:03", 3, "official", 180),
                ("clean", "clean:04", None, "cancelled", None),
                ("tie", "tie:01", 1, "official", 120),
                ("tie", "tie:02", 2, "official", 150),
                ("tie", "tie:03", 3, "official", 180),
                ("tie", "tie:04", 3, "official", 200),
            ])
    results = t59e.load_results(db)
    assert results["clean"]["finish"] == (1, 2, 3)
    assert "tie" not in results


def test_capture_loader_uses_earliest_successful_stage_fetch(tmp_path):
    db = tmp_path / "captures.db"
    with sqlite3.connect(db) as connection:
        connection.execute("""CREATE TABLE board_odds_snapshots (
            board_odds_snapshot_id INTEGER PRIMARY KEY, race_id TEXT, date TEXT,
            stage TEXT, fetch_id TEXT, received_at TEXT, status TEXT,
            combo TEXT, bet_type TEXT, model_probability REAL, fair_odds REAL,
            odds REAL)""")
        connection.execute("""CREATE TABLE odds_snapshots (
            race_id TEXT, stage TEXT, fetch_id TEXT, horse_id TEXT,
            win_odds REAL, field_size INTEGER)""")
        for fetch, received, probability in (
                ("first", "2026-08-01T00:00:01Z", 0.2),
                ("later", "2026-08-01T00:00:02Z", 0.3)):
            connection.execute(
                "INSERT INTO board_odds_snapshots "
                "(race_id,date,stage,fetch_id,received_at,status,combo,bet_type,"
                "model_probability,fair_odds,odds) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("20260801:東京:01", "20260801", "30", fetch, received,
                 "ok", "1", "place", probability, 1 / probability, 4.0))
            connection.executemany(
                "INSERT INTO odds_snapshots VALUES(?,?,?,?,?,?)", [
                    ("20260801:東京:01", "30", fetch, f"20260801:東京:01:{number:02d}",
                     odds, 8)
                    for number, odds in enumerate(
                        (3, 4, 6, 8, 10, 12, 15, 20), 1)])
    captures, audit = t59e.load_captures(db)
    capture = captures[("20260801:東京:01", "30")]
    assert capture.fetch_id == "first"
    assert capture.board_rows["place"]["1"]["model_probability"] == 0.2
    assert audit == {"captures": 1}


def test_clv_correlation_sign_matches_fixed_definition():
    early_rows = {ticket: {} for ticket in t59e.TICKETS}
    close_rows = {ticket: {} for ticket in t59e.TICKETS}
    for index, (fair, early, close) in enumerate(
            ((12.0, 10.0, 11.0), (10.0, 10.0, 10.0), (8.0, 10.0, 9.0)), 1):
        combo = str(index)
        early_rows["place"][combo] = {"fair_odds": fair, "odds": early}
        close_rows["place"][combo] = {"fair_odds": fair, "odds": close}
    early = _capture("30", early_rows)
    closing = _capture("2", close_rows)
    result = t59e.evaluate_clv({
        (early.race_id, "30"): early, (closing.race_id, "2"): closing})
    assert result["place_30m"]["n"] == 3
    assert result["place_30m"]["correlation"] > 0.99
    assert result["place_30m"]["x"].startswith("log(fair_odds")


def test_reference_roi_is_isolated_from_live_and_display_paths():
    root = Path(__file__).parents[1]
    for relative in ("jra_ev.py", "jra_win5.py", "index_boards.html"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "backtest_t59e_shadow" not in source
        assert "reference_roi" not in source
        assert "fair_odds > actual_odds" not in source


def test_outputs_are_deterministic(tmp_path):
    payload = {"spec": "T59e", "primary": {"place": {"value": 0.1}}}
    daily = [{"date": "20260801", "ticket": "place", "races": 10,
              "candidate_logloss": 0.2, "baseline_logloss": 0.3}]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    sha1, csv1 = t59e.write_outputs(payload, daily, first)
    sha2, csv2 = t59e.write_outputs(payload, daily, second)
    assert sha1 == sha2
    assert first.read_bytes() == second.read_bytes()
    assert csv1.read_bytes() == csv2.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == payload
