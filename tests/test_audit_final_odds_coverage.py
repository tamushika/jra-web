import sqlite3

import pytest

import audit_final_odds_coverage as subject


def _row(date, race, umaban, rank, win_pay, *, place="東京", total=8):
    return {
        "date": date,
        "place": place,
        "r": race,
        "total_horses": total,
        "umaban": umaban,
        "horse": f"馬{umaban}",
        "rank": rank,
        "win_pay": win_pay,
    }


def _race(date, race, *, missing=None, invalid=None):
    rows = []
    for number in range(1, 9):
        rank = number
        raw = "250" if rank == 1 else f"({number + 1}.0)"
        if number == missing:
            raw = None
        if number == invalid:
            raw = "not-odds"
        rows.append(_row(date, race, number, rank, raw))
    return rows


def test_recover_odds_uses_target_compatible_contract():
    assert subject.recover_odds("260", 1).odds == 2.6
    assert subject.recover_odds("(12.3)", 4).odds == 12.3
    assert subject.recover_odds(None, 2).reason == "win_pay_null"
    assert subject.recover_odds("", 2).reason == "format_mismatch"
    assert subject.recover_odds("(nan)", 2).reason == "other_invalid_odds"


def test_audit_rates_populations_and_missing_classification():
    rows = _race("20210102", 9)  # Saturday, evaluation and WIN5 populations.
    rows += _race("20210102", 10, missing=8)
    rows += _race("20210104", 1, invalid=7)  # Monday, evaluation only.
    result = subject.audit_rows(rows, seed=59)
    year = result["yearly"]["2021"]
    assert year["runner_rows"] == 24
    assert year["recovered_rows"] == 22
    assert year["evaluation_races"] == 3
    assert year["complete_evaluation_races"] == 1
    assert year["win5_races"] == 2
    assert year["complete_win5_races"] == 1
    assert year["missing_reasons"] == {"win_pay_null": 1, "format_mismatch": 1}
    assert len(result["qa"]) == 1


def test_book_sum_and_source_proxy():
    rows = _race("20251228", 1) + _race("20260103", 9)
    result = subject.audit_rows(rows)
    target, extension = result["systematic"]["source"]
    assert target["group"] == "TARGET"
    assert target["rows"] == 8
    assert extension["group"] == "netkeiba_extension"
    assert extension["rows"] == 8
    expected = 1 / 2.5 + sum(1 / value for value in range(3, 10))
    assert result["yearly"]["2025"]["book_median"] == pytest.approx(expected)


def test_open_readonly_rejects_writes(tmp_path):
    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
    with subject.open_readonly(path) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO sample VALUES (1)")
