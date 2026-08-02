import hashlib
import json
import sqlite3
from pathlib import Path

import t42b_parse_training as parser


FIXTURES = Path(__file__).parent / "fixtures" / "t42b"


def body(name):
    return (FIXTURES / name).read_bytes()


def test_race_dom_extracts_all_columns_and_separates_laps():
    rows, skipped, reasons = parser.parse_race_page(body("race.html"), "202605010101", "abc")
    assert skipped == 0 and not reasons
    assert len(rows) == 2
    row = rows[0]
    assert row[:7] == ("202605010101", "2021100001", "テストホース", 1,
                       "2026-07-01", "美坂", "美浦坂路")
    assert json.loads(row[9]) == [54.4, 39.3, 25.9, 12.5]
    assert json.loads(row[10]) == [15.1, 13.4, 13.4, 12.5]
    assert row[11:18] == (4, "1", "強め", "強め", "C", "目立たず",
                          "外 パートナー 強めと併せ０秒２遅れ")
    assert json.loads(rows[1][9]) == []
    assert rows[1][11] == 0


def test_horse_dom_extracts_group_and_clock_values():
    rows, skipped, reasons = parser.parse_horse_page(body("horse.html"), "2013101904", "def")
    assert skipped == 0 and not reasons and len(rows) == 1
    row = rows[0]
    assert row[:6] == ("2013101904", 1, 1, "2025-11-19", "美ダ", "美浦ダート")
    assert json.loads(row[8]) == [55.2, 40.4, 12.6]
    assert row[10:16] == (3, "2", "馬也", "馬也", "C", "目立たず")


def test_known_normalization_and_unknown_passthrough():
    assert parser.normalize_course("栗ＣＷ") == "栗東CW"
    assert parser.normalize_course("ＣＷ 一番時計") == "栗東CW"
    assert parser.normalize_course("南Ｗ") == "美浦W"
    assert parser.normalize_intensity("直一杯") == "一杯"
    assert parser.normalize_intensity("Ｇ強") == "強め"
    assert parser.normalize_course("未知コース") == "未知コース"
    assert parser.normalize_intensity("未知脚色") == "未知脚色"


def test_blank_date_all_dash_row_is_preserved_as_zero_workout():
    sample = b"""<table><tr><td class='Horse_Info'><a href='/horse/h1'>H</a></td>
      <td class='Training_Day'></td><td></td><td></td><td></td>
      <td class='TrainingTimeData'><ul class='TrainingTimeDataList'><li>-</li></ul></td>
      <td></td><td class='TrainingLoad'></td><td class='Training_Critic'></td><td class='Rank_'></td>
      </tr></table>"""
    rows, skipped, _ = parser.parse_race_page(sample, "202605010101", "sha")
    assert skipped == 0 and len(rows) == 1
    assert rows[0][4] is None and rows[0][11] == 0


def test_race_index_builds_map_and_ignores_movie_link():
    rows = parser.parse_race_index(body("race_index.html"), "20260104")
    assert rows == [("202605010101", "20260104", "東京", 1),
                    ("202605010102", "20260104", "東京", 2)]


def test_asof_sanity_detects_future_training_row():
    connection = sqlite3.connect(":memory:")
    connection.executescript(parser.SCHEMA)
    connection.execute("INSERT INTO race_id_map VALUES('r1','20260701','東京',9)")
    base = ("r1", "h1", "馬", 1, "2026-07-02", "美坂", "美浦坂路", "良", "助手",
            "[]", "[]", 0, "", "", "", "", "", "", "sha", parser.PARSER_VERSION)
    connection.execute("INSERT INTO race_training_rows VALUES(" + ",".join("?" * 20) + ")", base)
    assert parser.count_future_training_rows(connection) == 1


def test_rebuild_is_offline_idempotent_and_audits_unknown_vocab(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    files = {}
    for category, key, fixture in (("race_index", "20260704", "race_index.html"),
                                    ("race", "202605010101", "race.html"),
                                    ("horse", "2013101904", "horse.html")):
        path = raw / f"{category}-{key}.html"
        path.write_bytes(body(fixture))
        files[(category, key)] = path
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files.values()}
    manifest = tmp_path / "manifest.sqlite"
    with sqlite3.connect(manifest) as connection:
        connection.execute("""CREATE TABLE fetch_manifest(url TEXT,category TEXT,target_key TEXT,
            cache_path TEXT,sha256 TEXT,error_code TEXT)""")
        for (category, key), path in files.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            connection.execute("INSERT INTO fetch_manifest VALUES(?,?,?,?,?,NULL)",
                               (f"offline:{category}:{key}", category, key, str(path), digest))
    ability = tmp_path / "ability.db"
    with sqlite3.connect(ability) as connection:
        connection.execute("CREATE TABLE runs(date TEXT,place TEXT,r INTEGER,horse TEXT)")
        connection.execute("INSERT INTO runs VALUES('20260704','東京',1,'テストホース')")
    output = tmp_path / "structured.sqlite"
    first = parser.rebuild_store(manifest, output, ability, progress_every=99)
    second = parser.rebuild_store(manifest, output, ability, progress_every=99, resume=True)
    assert first["totals"] == second["totals"]
    assert first["vocab"]["course"]["unknown"] == [["未知コース", 1]] or \
           first["vocab"]["course"]["unknown"] == [("未知コース", 1)]
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files.values()}
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT count(*) FROM race_training_rows").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM horse_training_rows").fetchone()[0] == 1
