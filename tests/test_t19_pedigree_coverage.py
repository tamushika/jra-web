import sqlite3

import audit_t19_pedigree_coverage as t19


def test_coverage_by_year_and_bms_gate():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (date TEXT, horse TEXT)")
    conn.executemany("INSERT INTO runs VALUES (?,?)", [
        ("20210101", "A"), ("20210102", "A"), ("20210103", "B"),
        ("20220101", "C"),
    ])
    rows = t19.coverage_by_year(conn, {
        "A": {"sire": "SA", "bms": "BA"},
        "B": {"sire": "SB", "bms": "BB"},
        "C": {"sire": "SC", "bms": ""},
    }, date_to="20221231")
    assert rows[0] == {
        "year": "2021", "rows": 3, "horses": 2,
        "sire_row_pct": 100.0, "bms_row_pct": 100.0,
        "sire_horse_pct": 100.0, "bms_horse_pct": 100.0,
    }
    assert rows[1]["bms_row_pct"] == 0.0
    assert t19.gate_passed(rows) is False
    assert t19.gate_passed(rows[:1]) is True
