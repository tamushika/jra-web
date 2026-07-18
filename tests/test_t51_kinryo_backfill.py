import sqlite3
from pathlib import Path

import backfill_kinryo_netkeiba as t51


FIXTURE = Path(__file__).parent / "fixtures" / "t51" / "netkeiba_result.html"


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE runs (
        date TEXT, place TEXT, r INTEGER, umaban INTEGER, horse TEXT,
        kinryo REAL, untouched TEXT)""")
    conn.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", [
        ("20260621", "東京", 11, 1, "テスト馬A", None, "keep-a"),
        ("20260621", "東京", 11, 2, "テスト馬B", 56.0, "keep-b"),
        ("20250621", "東京", 11, 1, "前年馬", None, "keep-old"),
        ("20260621", "東京", 12, 1, "別レース", None, "keep-race"),
    ])
    conn.commit()
    return conn


def test_parse_result_page_reads_weights_and_separate_handicap_flag():
    weights, handicap = t51.parse_result_page(FIXTURE.read_text(encoding="utf-8"))
    assert weights == {1: 50.0, 2: 57.5}
    assert handicap is True


def test_load_targets_selects_only_null_2026_rows(tmp_path):
    conn = _db(tmp_path / "target.db")
    rows, races = t51.load_targets(conn)
    assert [tuple(row[:5]) for row in rows] == [
        ("20260621", "東京", 11, 1, "テスト馬A"),
        ("20260621", "東京", 12, 1, "別レース"),
    ]
    assert races == [("20260621", "東京", 11), ("20260621", "東京", 12)]


def test_dry_run_main_writes_nothing(tmp_path):
    db = tmp_path / "dry.db"
    conn = _db(db)
    before = list(conn.execute("SELECT * FROM runs ORDER BY date, r, umaban"))
    conn.close()
    t51.main(["--db", str(db), "--cache", str(tmp_path / "missing-cache.json")])
    after = list(sqlite3.connect(db).execute("SELECT * FROM runs ORDER BY date, r, umaban"))
    assert after == before


def test_apply_plan_changes_only_null_2026_target_and_keeps_other_columns(tmp_path):
    conn = _db(tmp_path / "apply.db")
    before = list(conn.execute("SELECT * FROM runs ORDER BY date, r, umaban"))
    plan = [{
        "date": "20260621", "place": "東京", "r": 11, "umaban": 1,
        "horse": "テスト馬A", "kinryo": 50.0, "handicap": True,
        "source_url": "https://db.netkeiba.com/race/202605030611/",
    }, {
        "date": "20250621", "place": "東京", "r": 11, "umaban": 1,
        "horse": "前年馬", "kinryo": 55.0, "handicap": False,
        "source_url": "https://example.invalid/old",
    }]
    assert t51.apply_plan(conn, plan) == 1
    rows = list(conn.execute("SELECT * FROM runs ORDER BY date, r, umaban"))
    changed = next(row for row in rows if row[0] == "20260621" and row[2:4] == (11, 1))
    assert changed[5:] == (50.0, "keep-a")
    assert next(row for row in rows if row[0] == "20250621")[5] is None
    assert next(row for row in rows if row[0] == "20260621" and row[3] == 2)[5] == 56.0
    assert conn.execute("SELECT handicap FROM netkeiba_race_metadata").fetchone() == (1,)
