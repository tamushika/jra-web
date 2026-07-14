from pathlib import Path

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text

import jra_db_updater as updater


def _create_races_table(engine, *, unique=True):
    column_types = {
        "distance": "INTEGER",
        "total_horses": "INTEGER",
        "horse_number": "INTEGER NOT NULL",
        "rank": "REAL",
        "corner_4": "INTEGER",
        "horse_odds": "REAL",
        "weight": "REAL",
        "race_num": "INTEGER NOT NULL",
    }
    definitions = [
        f'"{column}" {column_types.get(column, "TEXT")}'
        for column in updater.UPSERT_COLUMNS
    ]
    if unique:
        definitions.append(
            'UNIQUE ("date", "place", "race_num", "horse_number")'
        )
    ddl = 'CREATE TABLE "races" (' + ", ".join(definitions) + ")"
    with engine.begin() as connection:
        connection.execute(text(ddl))


def _race_data(*, race_name="テスト特別", rank="1"):
    return {
        "info": {
            "レース情報": "2026年4月25日 東京 第11競走",
            "レース名": race_name,
            "コース": "ダート1600メートル",
            "馬場": "良",
        },
        "rows": [{
            "着順": rank,
            "馬番": "3",
            "馬名": "テストホース",
            "性齢": "牡3",
            "負担重量": "57.0",
            "騎手名": "テスト騎手",
            "タイム": "1:35.0",
            "コーナー通過順位": "4-3",
            "推定上り": "35.1",
            "馬体重（増減）": "480(+2)",
            "調教師名": "テスト調教師",
            "単勝人気": "2",
            "単勝配当": "",
            "複勝配当": "",
        }],
    }


def test_extract_race_num_supports_cname_query_and_visible_text():
    cname_url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1005202602011120260425/AA"
    )
    assert updater.extract_race_num(cname_url) == 11
    assert updater.extract_race_num("https://example.test/result?race_no=12") == 12
    assert updater.extract_race_num("https://example.test/result", "第１１競走") == 11
    assert updater.extract_race_num("https://example.test/result", "11レース") == 11
    assert updater.extract_race_num("https://example.test/result", "第13競走") is None


def test_race_headings_ignore_site_wide_h1_h2_elements():
    soup = BeautifulSoup(
        """
        <h1></h1><h2 class="sr-only">検索ウィンドウ</h2><h2>緊急情報</h2>
        <h1>レース結果 2026年4月25日（土曜）2回東京1日 11レース</h1>
        <h2>青葉特別</h2><h2>払戻金</h2>
        """,
        "html.parser",
    )
    race_info, race_name = updater.extract_page_headings(soup)
    assert race_info.endswith("11レース")
    assert race_name == "青葉特別"


def test_insert_rejects_unknown_race_num_before_database_access(monkeypatch):
    monkeypatch.setattr(updater, "engine", None)
    data = _race_data()
    data["info"]["レース情報"] = "2026年4月25日 東京"
    result = updater.insert_into_db(
        data,
        "https://example.test/result/without-race-number",
    )
    assert "R番号を特定できませんでした" in result["error"]


def test_insert_rejects_url_and_body_race_number_mismatch_before_db(monkeypatch):
    monkeypatch.setattr(updater, "engine", None)
    data = _race_data()
    data["info"]["R番号"] = 10
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1005202602011120260425/AA"
    )

    result = updater.insert_into_db(data, url)

    assert "URLのR番号(11R)" in result["error"]
    assert "本文のR番号(10R)" in result["error"]
    assert "登録を中止" in result["error"]


def test_semantic_header_mapping_skips_mismatched_row():
    soup = BeautifulSoup(
        """
        <table>
          <thead><tr>
            <th class="place">着順</th><th class="num">馬番</th>
            <th class="horse">馬名</th><th class="jockey">騎手名</th>
          </tr></thead>
          <tbody>
            <tr><td class="horse">正常馬</td><td class="place">1</td>
                <td class="jockey">正常騎手</td><td class="num">7</td></tr>
            <tr><td class="place">2</td><td class="num">8</td>
                <td class="horse">取消で崩れた行</td></tr>
          </tbody>
        </table>
        """,
        "html.parser",
    )
    columns, rows, warnings = updater.parse_result_table(
        soup.find("table"), {"7": "250円"}, {"7": "120円"}
    )

    assert columns == ["着順", "馬番", "馬名", "騎手名", "単勝配当", "複勝配当"]
    assert rows == [{
        "着順": "1", "馬番": "7", "馬名": "正常馬", "騎手名": "正常騎手",
        "単勝配当": "250円", "複勝配当": "120円",
    }]
    assert len(warnings) == 1
    assert "一致しないためスキップ" in warnings[0]


def test_same_natural_key_is_updated_without_adding_a_duplicate(monkeypatch):
    sqlite_engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_races_table(sqlite_engine)
    monkeypatch.setattr(updater, "engine", sqlite_engine)
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1005202602011120260425/AA"
    )

    first = updater.insert_into_db(_race_data(), url)
    second = updater.insert_into_db(
        _race_data(race_name="表記更新後", rank="2"), url
    )

    assert first["success"] is True and first["updated"] is False
    assert second["success"] is True and second["updated"] is True
    with sqlite_engine.connect() as connection:
        row = connection.execute(text(
            'SELECT COUNT(*) AS n, MAX("rank") AS rank, MAX("race_name") AS race_name, '
            'MAX("corner_4") AS corner_4, MAX("horse_odds") AS horse_odds '
            'FROM "races" WHERE "date"=:d AND "place"=:p '
            'AND "race_num"=:r AND "horse_number"=:h'
        ), {"d": "260425", "p": "東京", "r": 11, "h": 3}).one()
    assert row.n == 1
    assert row.rank == 2.0
    assert row.race_name == "表記更新後"
    assert row.corner_4 == 3
    assert row.horse_odds is None


def test_reregister_keeps_backfilled_odds_but_accepts_new_non_null_odds(monkeypatch):
    sqlite_engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_races_table(sqlite_engine)
    monkeypatch.setattr(updater, "engine", sqlite_engine)
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1005202602011120260425/AA"
    )

    assert updater.insert_into_db(_race_data(), url)["success"] is True
    with sqlite_engine.begin() as connection:
        connection.execute(text(
            'UPDATE "races" SET "horse_odds"=4.6 '
            'WHERE "date"=:d AND "place"=:p AND "race_num"=:r '
            'AND "horse_number"=:h'
        ), {"d": "260425", "p": "東京", "r": 11, "h": 3})

    # JRA結果ページ由来のNULL再登録は、後段backfillの4.6を消さない。
    assert updater.insert_into_db(_race_data(rank="2"), url)["success"] is True
    with sqlite_engine.connect() as connection:
        kept = connection.execute(text(
            'SELECT "horse_odds" FROM "races" WHERE "date"=:d '
            'AND "place"=:p AND "race_num"=:r AND "horse_number"=:h'
        ), {"d": "260425", "p": "東京", "r": 11, "h": 3}).scalar_one()
    assert kept == 4.6

    # 入力側に確定値がある場合は、その新しい値で更新する。
    with_odds = _race_data(rank="3")
    with_odds["rows"][0]["単勝"] = "5.2"
    assert updater.insert_into_db(with_odds, url)["success"] is True
    with sqlite_engine.connect() as connection:
        updated = connection.execute(text(
            'SELECT "horse_odds" FROM "races" WHERE "date"=:d '
            'AND "place"=:p AND "race_num"=:r AND "horse_number"=:h'
        ), {"d": "260425", "p": "東京", "r": 11, "h": 3}).scalar_one()
    assert updated == 5.2


def test_preexisting_duplicate_aborts_instead_of_silently_selecting_a_row(monkeypatch):
    sqlite_engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_races_table(sqlite_engine, unique=False)
    monkeypatch.setattr(updater, "engine", sqlite_engine)
    url = (
        "https://www.jra.go.jp/JRADB/accessS.html?"
        "CNAME=pw01sde1005202602011120260425/AA"
    )
    row = {column: None for column in updater.UPSERT_COLUMNS}
    row.update({"date": "260425", "place": "東京", "race_num": 11,
                "horse_number": 3})
    columns = ", ".join(f'"{column}"' for column in updater.UPSERT_COLUMNS)
    values = ", ".join(f":{column}" for column in updater.UPSERT_COLUMNS)
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(f'INSERT INTO "races" ({columns}) VALUES ({values})'),
            [row, row],
        )

    result = updater.insert_into_db(_race_data(), url)
    assert "既存データに自然キー重複があります" in result["error"]
    with sqlite_engine.connect() as connection:
        count = connection.execute(text('SELECT COUNT(*) FROM "races"')).scalar()
    assert count == 2


def test_migration_checks_nulls_and_duplicates_before_not_null_and_unique():
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "20260714_t34_races_natural_key.sql"
    ).read_text(encoding="utf-8")
    null_check = migration.index("WHERE date IS NULL")
    duplicate_check = migration.index("HAVING COUNT(*) > 1")
    not_null = migration.index("ALTER COLUMN date SET NOT NULL")
    unique_constraint = migration.index(
        "ADD CONSTRAINT uq_races_date_place_race_num_horse_number"
    )
    assert null_check < not_null < unique_constraint
    assert duplicate_check < not_null
    for column in ("date", "place", "race_num", "horse_number"):
        assert f"ALTER COLUMN {column} SET NOT NULL" in migration
    assert "UNIQUE (date, place, race_num, horse_number)" in migration
