import json
import sqlite3
from pathlib import Path

import pytest

import backtest_t63_ev_threshold as t63


def _gate_db(path: Path, dates: int, races: int) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE board_odds_snapshots "
            "(date TEXT,race_id TEXT,stage TEXT,status TEXT)"
        )
        db.executemany(
            "INSERT INTO board_odds_snapshots VALUES(?,?,?,?)",
            [
                (
                    f"202607{25 + index % dates:02d}",
                    f"20260725:race:{index:03d}", "30", "ok",
                )
                for index in range(races)
            ],
        )


def _row(ev: float, date="20260725", won=False):
    probability = 0.1
    return t63.CounterfactualRow(
        race_id=f"{date}:東京:01", event_date=date, horse_id="h",
        probability=probability, cutoff_odds=ev / probability,
        popularity=1, ev=ev, won=won,
        final_odds=ev / probability, win_payout=round(ev / probability * 100),
    )


def test_distribution_gate_reads_only_fixed_counts(tmp_path):
    db = tmp_path / "gate.db"
    _gate_db(db, 2, 69)
    assert t63.accumulation_gate(db) == t63.GateStatus(2, 69)
    assert t63.accumulation_gate(tmp_path / "missing.db") == t63.GateStatus(0, 0)


def test_blocked_evaluate_never_loads_rows_or_writes_output(tmp_path, monkeypatch):
    db = tmp_path / "blocked.db"
    _gate_db(db, 2, 69)
    monkeypatch.setattr(t63, "verify_contract", lambda: {"ok": True})
    monkeypatch.setattr(
        t63, "load_counterfactual_rows",
        lambda *args, **kwargs: pytest.fail("must not aggregate before gate"),
    )
    payload = t63.evaluate(db)
    assert payload["phase"] == "blocked_distribution_gate"
    output = tmp_path / "result.json"
    monkeypatch.setattr(t63, "DEFAULT_DB", db)
    assert t63.main(["--db", str(db), "--output", str(output)]) == 2
    assert not output.exists()


def test_second_gate_hides_all_metrics(monkeypatch, tmp_path):
    db = tmp_path / "ready.db"
    _gate_db(db, 4, 200)
    monkeypatch.setattr(t63, "verify_contract", lambda: {"ok": True})
    rows = [_row(1.2) for _ in range(50)]
    monkeypatch.setattr(
        t63, "load_counterfactual_rows", lambda *args, **kwargs: (rows, {}),
    )
    payload = t63.evaluate(db, leading_threshold=1.2)
    assert payload["phase"] == "distribution_only"
    assert "metrics" not in payload
    assert payload["adjudication_gate"]["ready"] is False


def test_no_leading_threshold_exposes_distribution_only(monkeypatch, tmp_path):
    db = tmp_path / "ready.db"
    _gate_db(db, 4, 200)
    monkeypatch.setattr(t63, "verify_contract", lambda: {"ok": True})
    monkeypatch.setattr(
        t63, "load_counterfactual_rows",
        lambda *args, **kwargs: ([_row(1.1)], {"valid_races": 1}),
    )
    payload = t63.evaluate(db)
    assert payload["phase"] == "distribution_only"
    assert "metrics" not in payload
    assert payload["adjudication_gate"]["status"] == "leading_threshold_not_supplied"


def test_ev_reconstruction_is_deterministic():
    first = t63.reconstruct_ev(0.2, 6.5)
    second = t63.reconstruct_ev(0.2, 6.5)
    assert first == second == 1.3
    with pytest.raises(ValueError):
        t63.reconstruct_ev(1.1, 2.0)


def test_threshold_is_restricted_to_frozen_grid():
    rows = [_row(1.0), _row(1.2), _row(1.3)]
    assert t63.notification_count(rows, 1.2) == 2
    with pytest.raises(t63.T63Error, match="frozen grid"):
        t63.notification_count(rows, 1.25)


def test_distribution_report_contains_no_outcome_metrics():
    report = t63.distribution_report([_row(1.0), _row(1.4)])
    encoded = json.dumps(report)
    assert report["eligible_horses"] == 2
    assert report["daily_max"][0]["max_ev"] == 1.4
    assert all(token not in encoded for token in ("roi", "calibration", "wins"))


def test_full_metrics_are_deterministic_and_use_both_odds():
    rows = [_row(1.3, "20260725", True), _row(1.2, "20260726", False)]
    first = t63.full_metrics(rows)
    second = t63.full_metrics(rows)
    assert first == second
    assert first["1.30"]["notifications"] == 1
    assert first["1.30"]["calibration_ratio"] == pytest.approx(10.0)
    assert first["1.30"]["cutoff_odds_roi"] == pytest.approx(13.0)
    assert first["1.30"]["final_odds_roi"] == pytest.approx(13.0)


def test_capture_contract_rejects_post_time_and_bad_flags():
    base = {
        "horse_id": "r:01", "field_size": 1, "win_odds": 2.0,
        "win_probability": .125, "quality_flags": "[]", "is_stale": 0,
        "observed_at": "2026-07-25T01:00:00+00:00",
        "scheduled_post_at": "2026-07-25T01:01:00+00:00",
    }
    assert t63._valid_capture([base]) is False  # fewer than eight
    rows = [{**base, "horse_id": f"r:{n:02d}", "field_size": 8,
             "win_odds": odds}
            for n, odds in enumerate((3, 4, 6, 8, 10, 12, 15, 20), 1)]
    assert t63._valid_capture(rows) is True
    rows[0]["observed_at"] = "2026-07-25T01:02:00+00:00"
    assert t63._valid_capture(rows) is False
    rows[0]["observed_at"] = base["observed_at"]
    rows[0]["quality_flags"] = '["catchup_burst"]'
    assert t63._valid_capture(rows) is False


def test_contract_matches_registered_ledger_and_artifacts():
    verified = t63.verify_contract()
    assert verified["experiment_id"] == t63.EXPERIMENT_ID
    assert verified["model_sha256"] == t63.MODEL_SHA256


def test_adjudication_gate_notification_count_is_v2_revised_to_100():
    # SPEC-T63 v2 (2026-08-29): gate (2) count 300 -> 100.
    assert t63.EXPERIMENT_ID == "T63-ev-threshold-rederivation-v2"
    assert t63.ADJUDICATION_NOTIFICATIONS == 100
    assert t63.ADJUDICATION_DAYS == 12


def test_database_connection_is_read_only(tmp_path):
    db = tmp_path / "readonly.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
        connection.execute("INSERT INTO sample VALUES(1)")
    with t63._connect_readonly(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO sample VALUES(2)")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
