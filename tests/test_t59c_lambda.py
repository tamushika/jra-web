import hashlib
import json
import math
import random

import pytest

import backtest_t59c_lambda as t59c


def _race(race_id, probabilities, finish):
    return t59c.Race(
        race_id, tuple(probabilities), tuple(finish),
        tuple(range(1, len(probabilities) + 1)), 1.25,
    )


def test_market_book_band_and_seven_runner_place_rule():
    assert t59c.market_probabilities([10.0] * 8) is None
    parsed = t59c.market_probabilities([5.0] * 7)
    assert parsed is not None
    probabilities, _ = parsed
    tickets = t59c.ticket_probabilities(probabilities, 0.7, 0.8, places=2)
    assert sum(tickets["place"]) == pytest.approx(2.0)


def test_probability_invariants_and_harville_closed_form():
    probabilities = (0.4, 0.3, 0.2, 0.1)
    tickets = t59c.ticket_probabilities(probabilities, 1.0, 1.0)
    assert sum(tickets["place"]) == pytest.approx(3.0)
    assert sum(tickets["umaren"].values()) == pytest.approx(1.0)
    assert sum(tickets["sanrenpuku"].values()) == pytest.approx(1.0)
    expected = (
        probabilities[0]
        * probabilities[1] / (1.0 - probabilities[0])
        * probabilities[2] / (1.0 - probabilities[0] - probabilities[1])
    )
    assert tickets["ordered_top3"][(0, 1, 2)] == pytest.approx(expected)


def test_period_report_contains_bootstrap_and_secondary_diagnostics():
    races = [
        _race("20240101Tokyo01", (0.30, 0.22, 0.18, 0.12, 0.08, 0.05, 0.03, 0.02),
              (0, 2, 1)),
        _race("20240102Tokyo01", (0.26, 0.21, 0.17, 0.13, 0.09, 0.06, 0.05, 0.03),
              (1, 0, 3)),
    ]
    report = t59c.evaluate_period(races, 0.7, 0.8)
    assert report["race_count"] == 2
    for ticket in ("place", "wide", "umaren"):
        assert report[ticket]["ci95"]
        assert report[ticket]["candidate_brier"] >= 0.0
        assert report[ticket]["candidate_diagnostics"]["calibration_deciles"]


def test_stage_mle_recovers_synthetic_lambda():
    rng = random.Random(5903)
    truth2, truth3 = 0.68, 0.83
    races = []
    for index in range(5000):
        raw = [rng.uniform(0.02, 0.30) for _ in range(8)]
        probabilities = tuple(value / sum(raw) for value in raw)
        first = rng.choices(range(8), weights=probabilities)[0]
        q2 = t59c.conditional_probabilities(probabilities, (first,), truth2)
        second = rng.choices(range(8), weights=q2)[0]
        q3 = t59c.conditional_probabilities(probabilities, (first, second), truth3)
        third = rng.choices(range(8), weights=q3)[0]
        races.append(_race(f"20220101X{index:05d}", probabilities,
                           (first, second, third)))
    assert t59c.fit_stage_lambda(races, 2) == pytest.approx(truth2, abs=0.07)
    assert t59c.fit_stage_lambda(races, 3) == pytest.approx(truth3, abs=0.07)


def test_registration_gate_fails_before_evaluation(tmp_path):
    db = tmp_path / "ability.db"
    ledger = tmp_path / "experiments.jsonl"
    db.write_bytes(b"not-the-sealed-db")
    ledger.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        t59c.require_registrations(ledger, db)


def test_registration_gate_names_all_missing_experiments(monkeypatch, tmp_path):
    db = tmp_path / "ability.db"
    ledger = tmp_path / "experiments.jsonl"
    db.write_bytes(b"sealed-by-test")
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setattr(t59c, "sha256_file", lambda _path: t59c.ABILITY_DB_SHA256)
    with pytest.raises(RuntimeError, match="T59c-place-lambda-v1") as error:
        t59c.require_registrations(ledger, db)
    assert all(experiment_id in str(error.value)
               for experiment_id in t59c.EXPERIMENT_IDS)


def test_deterministic_json_sha(tmp_path):
    payload = {"z": [2, 1], "日本語": "値", "a": 0.5}
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    sha1 = t59c.write_deterministic_json(payload, first)
    sha2 = t59c.write_deterministic_json(payload, second)
    assert sha1 == sha2 == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == payload
