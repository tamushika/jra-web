"""SPEC-T79 (重賞データページ) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T79-graded-race-page.md §3
  1. api.graded_names: normalize_race_name / race_key / match_key
  2. build_graded_cache.py: 小さなability.db風フィクスチャからキャッシュを作り、
     master/races/runs の件数・waku/kyaku/sire/prev の値・same_courseフラグを検証
  3. API: jra_suite.create_app() の test client で /graded/ 系エンドポイントを検証
"""
import json
import os
import sqlite3

import pytest

import build_graded_cache
import jra_ev
import jra_graded
import jra_suite
from api.graded_names import match_key, normalize_race_name, race_key


# ─── §3.1: api.graded_names ────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected_base,expected_grade", [
    ("第75回日刊スポ賞中山金杯(GIII)", "日刊スポ賞中山金杯", "G3"),
    ("アルゼンＨG2", "アルゼン", "G2"),
])
def test_normalize_race_name(name, expected_base, expected_grade):
    base, grade = normalize_race_name(name)
    assert base == expected_base
    assert grade == expected_grade


@pytest.mark.parametrize("name,expected_key", [
    ("みやこステークス", "みやこS"),
    ("アメリカジョッキーC", "アメリカ"),
    ("エリザベス女王杯", "エリザベ"),
])
def test_race_key(name, expected_key):
    assert race_key(name) == expected_key


# TARGET由来 (2015-2025相当) の主要キー + §0で列挙された2026netkeiba名の
# 9件の不一致ケースをすべて含む、match_key突合の受け入れテーブル。
_KNOWN_KEYS = {
    "中山金杯", "新潟記念", "みやこS", "阪神牝馬", "エリザベ", "アメリカ", "ラジオN",
    "弥生賞", "スプリン", "金鯱賞", "ファルコ", "クイーン", "京都金杯", "シンザン",
}


@pytest.mark.parametrize("name,expected_key,expected_how", [
    ("第75回日刊スポ賞中山金杯(GIII)", "中山金杯", "strip_sponsor"),
    ("農林水産省賞典 新潟記念", "新潟記念", "last_token"),
    ("みやこステークス", "みやこS", "exact"),
    ("エリザベス女王杯", "エリザベ", "exact"),
    ("ラジオNIKKEI賞", "ラジオN", "exact"),
    ("報知弥生ディープ記念", "弥生賞", "alias"),
    ("サンスポ杯阪神牝馬S", "阪神牝馬", "strip_sponsor"),
    ("東海テレビ杯金鯱賞", "金鯱賞", "strip_sponsor"),
    ("中スポ賞ファルコンS", "ファルコ", "strip_sponsor"),
    ("デイリー杯クイーンC", "クイーン", "strip_sponsor"),
    ("スポニチ賞京都金杯", "京都金杯", "strip_sponsor"),
    ("日刊スポシンザン記念", "シンザン", "sponsor_list"),
])
def test_match_key_resolves_known_mismatches(name, expected_key, expected_how):
    key, _grade, how = match_key(name, _KNOWN_KEYS)
    assert key == expected_key
    assert how == expected_how


def test_match_key_no_stripping_for_whole_name_that_looks_like_sponsor_plus_shou():
    # "ラジオNIKKEI賞" はそれ自体が正式なレース名であり、冠除去されない
    # (既知キーに直接一致するため、last_token/strip_sponsor/sponsor_list を経由しない)。
    key, _grade, how = match_key("ラジオNIKKEI賞", _KNOWN_KEYS)
    assert key == "ラジオN"
    assert how == "exact"


def test_match_key_returns_none_when_unresolvable():
    key, grade, how = match_key("存在しないレース名", _KNOWN_KEYS)
    assert (key, grade, how) == (None, None, None)


def test_match_key_alias_overrides_default_resolution(tmp_path):
    # 別名表 (aliases) は最優先で参照される: strip_sponsor等で別のキーに
    # 解決できる場合でも、aliasが与えられればそちらを優先する。
    aliases = {"サンスポ杯阪神牝馬S": "みやこS"}
    key, _grade, how = match_key("サンスポ杯阪神牝馬S", _KNOWN_KEYS, aliases=aliases)
    assert key == "みやこS"
    assert how == "alias"


# ─── §3.2: build_graded_cache.py ───────────────────────────────────────────

_RUN_COLUMNS = (
    "date", "place", "r", "race_name", "race_class", "horse", "sex", "age",
    "jockey", "kinryo", "total_horses", "umaban", "popularity", "rank",
    "track_type", "distance", "condition", "time_sec", "chakusa", "c4",
    "agari", "pci", "weight", "affi", "win_pay", "fukusho_pay",
)


def _insert_run(cur, **kwargs):
    row = {c: None for c in _RUN_COLUMNS}
    row.update(kwargs)
    cur.execute(
        f"INSERT INTO runs({','.join(_RUN_COLUMNS)}) VALUES ({','.join('?' * len(_RUN_COLUMNS))})",
        [row[c] for c in _RUN_COLUMNS])


def _build_fixture_ability_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE runs ({','.join(_RUN_COLUMNS)})")
    cur.execute("CREATE INDEX idx_horse_date ON runs(horse, date)")
    cur.execute("CREATE TABLE race_payouts (date TEXT, place TEXT, r INTEGER, "
               "bet_type TEXT, combo TEXT, pay REAL)")

    common = dict(race_class="重賞", track_type="芝", distance=2000, condition="良",
                 total_horses=3, sex="牡", age=4, kinryo=57.0, weight=480,
                 affi="(美)", agari=34.5, pci=50.0, chakusa=0.5)

    # レース1「サンプル」(race_key="サンプル"): 2023年=東京、2024/2025年=中山
    # (2024年にコース変更、same_course_as_latestは2024/2025年のみ1になる想定)。
    _insert_run(cur, date="20230101", place="東京", r=11, race_name="サンプルG2",
               horse="ウマA", umaban=1, popularity=1, rank=1, c4=1,
               win_pay="250", fukusho_pay=120.0, **common)
    _insert_run(cur, date="20230101", place="東京", r=11, race_name="サンプルG2",
               horse="ウマB", umaban=2, popularity=2, rank=2, c4=5,
               win_pay="(35.0)", fukusho_pay=140.0, **common)
    _insert_run(cur, date="20230101", place="東京", r=11, race_name="サンプルG2",
               horse="ウマC", umaban=3, popularity=3, rank=3, c4=8,
               win_pay="(80.0)", fukusho_pay=200.0, **common)

    common2024 = dict(common, place="中山")
    _insert_run(cur, date="20240107", place="中山", r=11, race_name="サンプルG2",
               horse="ウマA", umaban=1, popularity=1, rank=2, c4=2,
               win_pay="(15.0)", fukusho_pay=110.0, **{k: v for k, v in common2024.items() if k != "place"})
    _insert_run(cur, date="20240107", place="中山", r=11, race_name="サンプルG2",
               horse="ウマB", umaban=2, popularity=2, rank=1, c4=1,
               win_pay="380", fukusho_pay=160.0, **{k: v for k, v in common2024.items() if k != "place"})
    _insert_run(cur, date="20240107", place="中山", r=11, race_name="サンプルG2",
               horse="ウマC", umaban=3, popularity=3, rank=None, c4=None,
               win_pay=None, fukusho_pay=None, **{k: v for k, v in common2024.items() if k != "place"})

    _insert_run(cur, date="20250105", place="中山", r=11, race_name="サンプルG2",
               horse="ウマA", umaban=1, popularity=1, rank=1, c4=1,
               win_pay="300", fukusho_pay=130.0, **{k: v for k, v in common2024.items() if k != "place"})
    _insert_run(cur, date="20250105", place="中山", r=11, race_name="サンプルG2",
               horse="ウマB", umaban=2, popularity=3, rank=3, c4=9,
               win_pay="(90.0)", fukusho_pay=210.0, **{k: v for k, v in common2024.items() if k != "place"})
    _insert_run(cur, date="20250105", place="中山", r=11, race_name="サンプルG2",
               horse="ウマD", umaban=3, popularity=2, rank=2, c4=4,
               win_pay="(20.0)", fukusho_pay=150.0, **{k: v for k, v in common2024.items() if k != "place"})

    # レース2「テスト」(race_key="テスト"): 2025年のみ、頭数2。
    common_test = dict(common, place="京都", distance=1800)
    _insert_run(cur, date="20250202", place="京都", r=9, race_name="テストG3",
               horse="ウマA", umaban=1, popularity=1, rank=1, c4=1,
               win_pay="150", fukusho_pay=110.0,
               **{k: v for k, v in common_test.items() if k not in ("place", "distance")})
    _insert_run(cur, date="20250202", place="京都", r=9, race_name="テストG3",
               horse="ウマE", umaban=2, popularity=2, rank=2, c4=6,
               win_pay="(25.0)", fukusho_pay=170.0,
               **{k: v for k, v in common_test.items() if k not in ("place", "distance")})

    # ウマAの前走: 重賞ではない一般戦 (前走参照はability.db全体から拾う)。
    _insert_run(cur, date="20221215", place="中山", r=5, race_name="一般戦",
               race_class="3勝", horse="ウマA", umaban=4, popularity=1, rank=1,
               c4=2, track_type="芝", distance=1800, condition="良", total_horses=10,
               sex="牡", age=3, kinryo=56.0, weight=478, affi="(美)", win_pay="200",
               fukusho_pay=110.0, agari=34.0, pci=50.0, chakusa=0.0)

    # 障害重賞 (除外対象)。
    _insert_run(cur, date="20250101", place="中山", r=10, race_name="障害サンプルG1",
               horse="ウマF", umaban=1, popularity=1, rank=1, c4=1,
               win_pay="500", fukusho_pay=150.0, race_class="重賞",
               track_type="芝", distance=3200, condition="良", total_horses=12,
               sex="牡", age=5, kinryo=60.0, weight=490, affi="美浦", agari=36.0,
               pci=50.0, chakusa=0.0)

    # race_payouts (2025年のみ、SPEC §0通り2025〜のみ存在)。
    cur.execute("INSERT INTO race_payouts VALUES (?,?,?,?,?,?)",
               ("20250105", "中山", 11, "馬連", "1-3", 980.0))
    cur.execute("INSERT INTO race_payouts VALUES (?,?,?,?,?,?)",
               ("20250105", "中山", 11, "三連複", "1-2-3", 2400.0))
    cur.execute("INSERT INTO race_payouts VALUES (?,?,?,?,?,?)",
               ("20250105", "中山", 11, "三連単", "1>3>2", 9800.0))

    conn.commit()
    conn.close()


@pytest.fixture()
def fixture_env(tmp_path, monkeypatch):
    ability_db = tmp_path / "ability_fixture.db"
    _build_fixture_ability_db(str(ability_db))

    pedigree_path = tmp_path / "pedigree_cache.json"
    pedigree_path.write_text(json.dumps({
        "ウマA": {"sire": "サイアーA", "bms": "母父A"},
        "ウマB": {"sire": "サイアーB", "bms": "母父B"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_graded_cache, "PEDIGREE_PATH", str(pedigree_path))

    out_path = tmp_path / "graded_cache.sqlite"
    result = build_graded_cache.build_cache(
        years_from=2023, ability_db_path=str(ability_db), out_path=str(out_path))
    return {"out_path": str(out_path), "result": result}


def test_build_cache_counts_and_jump_exclusion(fixture_env):
    conn = sqlite3.connect(fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    masters = {r["key"]: dict(r) for r in cur.execute("SELECT * FROM master")}
    assert set(masters.keys()) == {"サンプル", "テスト"}
    assert masters["サンプル"]["n_years"] == 3
    assert masters["サンプル"]["first_year"] == 2023
    assert masters["サンプル"]["last_year"] == 2025
    assert masters["サンプル"]["place_latest"] == "中山"

    races = cur.execute("SELECT * FROM races").fetchall()
    assert len(races) == 4  # サンプル x3 + テスト x1 (障害サンプルG1は除外)
    assert cur.execute("SELECT COUNT(*) FROM races WHERE race_name_raw LIKE '%障害%'").fetchone()[0] == 0

    runs = cur.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert runs == 3 + 3 + 3 + 2  # 障害サンプルG1 (1行) は含まれない
    conn.close()


def test_build_cache_same_course_flag(fixture_env):
    conn = sqlite3.connect(fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    rows = {r["year"]: r for r in conn.execute(
        "SELECT * FROM races WHERE key='サンプル'").fetchall()}
    assert rows[2023]["same_course_as_latest"] == 0  # 東京開催 (最新の中山と不一致)
    assert rows[2024]["same_course_as_latest"] == 1
    assert rows[2025]["same_course_as_latest"] == 1
    conn.close()


def test_build_cache_waku_kyaku_sire_and_prev(fixture_env):
    conn = sqlite3.connect(fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    race_2025 = conn.execute(
        "SELECT race_id FROM races WHERE key='サンプル' AND year=2025").fetchone()["race_id"]
    runs = {r["horse"]: r for r in conn.execute(
        "SELECT * FROM runs WHERE race_id=?", (race_2025,)).fetchall()}

    uma_a = runs["ウマA"]
    assert uma_a["waku"] == 1  # 頭数3・馬番1 -> 1枠
    assert uma_a["kyaku"] == "逃げ"  # c4=1
    assert uma_a["sire"] == "サイアーA"
    assert uma_a["win_pay"] == 300.0  # rank=1: 実払戻
    assert uma_a["fukusho_pay"] == 130.0
    # 前走: 2024年の同レース (中山金杯的な直前の重賞ではなく、日付が一番近いもの)
    assert uma_a["prev_date"] == "20240107"
    assert uma_a["prev_race_name"] == "サンプルG2"
    assert uma_a["prev_rank"] == 2
    assert uma_a["prev_interval_days"] == (
        __import__("datetime").datetime(2025, 1, 5) - __import__("datetime").datetime(2024, 1, 7)
    ).days

    uma_b = runs["ウマB"]
    assert uma_b["win_pay"] is None  # rank=3: (90.0)は払戻ではなくオッズなのでNone
    assert uma_b["fukusho_pay"] == 210.0

    uma_d = runs["ウマD"]
    assert uma_d["prev_date"] is None  # 初出走 (前走なし)
    conn.close()


def test_build_cache_prev_uses_any_race_not_only_graded(fixture_env):
    # ウマAの2023年 (初めての重賞出走) の前走は、非重賞の「一般戦」(20221215)。
    conn = sqlite3.connect(fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    race_2023 = conn.execute(
        "SELECT race_id FROM races WHERE key='サンプル' AND year=2023").fetchone()["race_id"]
    uma_a = conn.execute("SELECT * FROM runs WHERE race_id=? AND horse='ウマA'",
                        (race_2023,)).fetchone()
    assert uma_a["prev_date"] == "20221215"
    assert uma_a["prev_race_name"] == "一般戦"
    assert uma_a["prev_place"] == "中山"
    conn.close()


def test_build_cache_is_atomic_and_replaces_existing(fixture_env, tmp_path):
    # 既存ファイルがあっても壊れず作り直せる (一時ファイル→rename)。
    out_path = fixture_env["out_path"]
    assert os.path.exists(out_path)
    mtime_before = os.path.getmtime(out_path)
    ability_db = tmp_path / "ability_fixture.db"
    build_graded_cache.build_cache(years_from=2023, ability_db_path=str(ability_db),
                                   out_path=out_path)
    assert os.path.exists(out_path)
    assert os.path.getmtime(out_path) >= mtime_before


def test_build_cache_meta_records_waku_impl(fixture_env):
    # T80 (calculate_wakuの割当バグ修正) がマージされたら build_graded_cache.py を
    # 再実行するだけで良いように、meta に現在の calculate_waku(5,16) を記録する。
    from api.past_data_service import calculate_waku
    conn = sqlite3.connect(fixture_env["out_path"])
    row = conn.execute("SELECT value FROM meta WHERE key='waku_impl'").fetchone()
    assert row is not None
    assert row[0] == str(calculate_waku(5, 16))
    conn.close()


def test_build_cache_applies_display_name_override(fixture_env, tmp_path):
    display_names_path = tmp_path / "graded_display_names.json"
    display_names_path.write_text(json.dumps({"サンプル": "サンプルカスタム表示名"},
                                              ensure_ascii=False), encoding="utf-8")
    ability_db = tmp_path / "ability_fixture.db"
    out_path2 = tmp_path / "graded_cache_display_override.sqlite"
    build_graded_cache.build_cache(
        years_from=2023, ability_db_path=str(ability_db), out_path=str(out_path2),
        display_names_path=str(display_names_path))
    conn = sqlite3.connect(str(out_path2))
    row = conn.execute("SELECT display_name FROM master WHERE key='サンプル'").fetchone()
    assert row[0] == "サンプルカスタム表示名"
    conn.close()


def test_build_cache_applies_merges(fixture_env, tmp_path):
    # 旧キー (ここでは「テスト」) を「サンプル」に統合する。race_name_raw は
    # 元のまま保持され、course_history 相当の情報が失われないことを確認する。
    merges_path = tmp_path / "graded_merges.json"
    merges_path.write_text(json.dumps({"テスト": "サンプル"}, ensure_ascii=False),
                           encoding="utf-8")
    ability_db = tmp_path / "ability_fixture.db"
    out_path2 = tmp_path / "graded_cache_merged.sqlite"
    build_graded_cache.build_cache(
        years_from=2023, ability_db_path=str(ability_db), out_path=str(out_path2),
        merges_path=str(merges_path))
    conn = sqlite3.connect(str(out_path2))
    conn.row_factory = sqlite3.Row
    keys = {r["key"] for r in conn.execute("SELECT DISTINCT key FROM master")}
    assert keys == {"サンプル"}  # 「テスト」は統合されて残らない
    races = conn.execute("SELECT race_name_raw FROM races WHERE key='サンプル'").fetchall()
    assert any(r["race_name_raw"] == "テストG3" for r in races)  # 旧名は保持される
    conn.close()


# ─── レビュー対応: 4文字キーの衝突分割 (同一年に2開催あるキーだけ分割) ──────

def _insert_collision_run(cur, **kwargs):
    common = dict(race_class="重賞", track_type="芝", condition="良",
                 total_horses=3, sex="牡", age=4, kinryo=57.0, weight=480,
                 affi="(美)", agari=34.5, pci=50.0, chakusa=0.5,
                 popularity=1, rank=1, c4=1, win_pay="200", fukusho_pay=110.0)
    common.update(kwargs)
    _insert_run(cur, **common)


def _build_collision_fixture_ability_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE runs ({','.join(_RUN_COLUMNS)})")
    cur.execute("CREATE INDEX idx_horse_date ON runs(horse, date)")
    cur.execute("CREATE TABLE race_payouts (date TEXT, place TEXT, r INTEGER, "
               "bet_type TEXT, combo TEXT, pay REAL)")

    # 「コリジョン」(race_key基底="コリジョ"): 2024年・2025年ともに同一年に
    # 2開催 (3月1600m@東京 / 9月1200m@中山) → 分割対象。
    for year, march_date, sept_date in ((2024, "20240301", "20240905"),
                                        (2025, "20250305", "20250903")):
        _insert_collision_run(cur, date=march_date, place="東京", r=11,
                              race_name="コリジョンG2", horse=f"馬march{year}",
                              umaban=1, distance=1600)
        _insert_collision_run(cur, date=sept_date, place="中山", r=11,
                              race_name="コリジョンG2", horse=f"馬sept{year}",
                              umaban=1, distance=1200)

    # 「ネンズレ」: 年ごとに1開催のみ、月がわずかに前後するだけ (3→4→3) →
    # 同一年に2開催が無いので分割不要 (1キーのまま)。
    for year, date in ((2023, "20230310"), (2024, "20240405"), (2025, "20250312")):
        _insert_collision_run(cur, date=date, place="京都", r=9,
                              race_name="ネンズレG3", horse=f"馬n{year}",
                              umaban=1, distance=2000)

    conn.commit()
    conn.close()


@pytest.fixture()
def collision_fixture_env(tmp_path):
    ability_db = tmp_path / "ability_collision.db"
    _build_collision_fixture_ability_db(str(ability_db))
    out_path = tmp_path / "graded_cache_collision.sqlite"
    build_graded_cache.build_cache(years_from=2023, ability_db_path=str(ability_db),
                                   out_path=str(out_path))
    return {"out_path": str(out_path)}


def test_build_cache_splits_key_with_two_meetings_in_same_year(collision_fixture_env):
    conn = sqlite3.connect(collision_fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    keys = {r["key"] for r in conn.execute(
        "SELECT DISTINCT key FROM master WHERE key LIKE 'コリジョ%'")}
    assert keys == {"コリジョ@3", "コリジョ@9"}

    march = conn.execute("SELECT * FROM master WHERE key='コリジョ@3'").fetchone()
    sept = conn.execute("SELECT * FROM master WHERE key='コリジョ@9'").fetchone()
    assert march["distance_latest"] == 1600
    assert march["place_latest"] == "東京"
    assert march["n_years"] == 2
    assert sept["distance_latest"] == 1200
    assert sept["place_latest"] == "中山"
    assert sept["n_years"] == 2

    march_races = conn.execute("SELECT year FROM races WHERE key='コリジョ@3'").fetchall()
    assert {r["year"] for r in march_races} == {2024, 2025}
    conn.close()


def test_build_cache_does_not_split_key_with_year_drift_only(collision_fixture_env):
    # 「ネンズレ」は同一年に2開催が無い (月が前後するだけ) ので分割されず、
    # サフィックス無しの1キーのまま残る。
    conn = sqlite3.connect(collision_fixture_env["out_path"])
    conn.row_factory = sqlite3.Row
    keys = {r["key"] for r in conn.execute(
        "SELECT DISTINCT key FROM master WHERE key LIKE 'ネンズレ%'")}
    assert keys == {"ネンズレ"}
    n_years = conn.execute(
        "SELECT n_years FROM master WHERE key='ネンズレ'").fetchone()["n_years"]
    assert n_years == 3
    conn.close()


# ─── §3.3: API (jra_suite経由) ─────────────────────────────────────────────

@pytest.fixture()
def suite_client(fixture_env, monkeypatch):
    monkeypatch.setattr(jra_graded, "CACHE_PATH", fixture_env["out_path"])
    app = jra_suite.create_app()
    return app.test_client()


def test_graded_index_page_returns_200(suite_client):
    resp = suite_client.get("/graded/")
    assert resp.status_code == 200
    assert "重賞".encode("utf-8") in resp.data


def test_graded_api_master_lists_both_keys(suite_client):
    resp = suite_client.get("/graded/api/master")
    assert resp.status_code == 200
    data = resp.get_json()
    keys = {r["key"] for r in data["races"]}
    assert keys == {"サンプル", "テスト"}
    assert data["built_at"]


def test_graded_api_history_matches_hand_computed_stats(suite_client):
    resp = suite_client.get("/graded/api/history?key=サンプル&years=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["master"]["key"] == "サンプル"
    assert data["n_years_used"] == 3
    assert len(data["results"]) == 3

    # 1番人気は3頭 (2023ウマA[1着], 2024ウマA[2着], 2025ウマA[1着])
    pop1 = next(g for g in data["aggregates"]["popularity"] if g["label"] == "1番人気")
    assert pop1["n"] == 3
    assert pop1["c1"] == 2
    assert pop1["c2"] == 1
    # APIはround(x, 3)で丸めて返すため、許容誤差は丸め粒度に合わせる
    assert pop1["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    # roi_win = (300 [2025 ウマA rank1] + 250 [2023 ウマA rank1]) / (3*100)
    assert pop1["roi_win"] == pytest.approx((300.0 + 250.0) / 300.0, abs=1e-3)

    course_changes = data["course_history"]
    assert course_changes[0]["place"] == "東京"
    assert course_changes[-1]["place"] == "中山"


def test_graded_api_history_same_course_filter(suite_client):
    resp = suite_client.get("/graded/api/history?key=サンプル&years=10&same_course=1")
    data = resp.get_json()
    assert data["n_years_used"] == 2
    years = {r["year"] for r in data["results"]}
    assert years == {2024, 2025}


def test_graded_api_history_unknown_key_404(suite_client):
    resp = suite_client.get("/graded/api/history?key=存在しない")
    assert resp.status_code == 404


def test_graded_api_missing_cache_returns_503(monkeypatch, tmp_path):
    monkeypatch.setattr(jra_graded, "CACHE_PATH", str(tmp_path / "does_not_exist.sqlite"))
    app = jra_suite.create_app()
    client = app.test_client()
    resp = client.get("/graded/api/master")
    assert resp.status_code == 503
    assert "build_graded_cache.py" in resp.get_json()["error"]


def test_graded_api_this_week_filters_by_match(suite_client, monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "中山", "race_num": 11, "start_time": "15:40",
              "race_info": "【中山 11R】芝2000m 15:40発走　サンプルG2",
              "day_label": "テスト当日"},
        "r2": {"venue": "東京", "race_num": 3, "start_time": "10:10",
              "race_info": "【東京 3R】芝1600m 10:10発走　全く一致しない未勝利戦",
              "day_label": "テスト当日"},
    })
    resp = suite_client.get("/graded/api/this_week")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["races"]) == 1
    assert data["races"][0]["key"] == "サンプル"
    assert data["day_label"] == "テスト当日"


def test_graded_api_this_week_empty_state(suite_client, monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {})
    resp = suite_client.get("/graded/api/this_week")
    assert resp.status_code == 200
    assert resp.get_json() == {"races": [], "day_label": ""}


def test_graded_api_this_week_resolves_split_subkey_by_place(collision_fixture_env, monkeypatch):
    # 「コリジョン」は分割済みなので、当日出馬表の会場(place)で正しいsub-keyへ
    # 解決できることを確認する (東京開催→3月側の"コリジョ@3")。
    monkeypatch.setattr(jra_graded, "CACHE_PATH", collision_fixture_env["out_path"])
    app = jra_suite.create_app()
    client = app.test_client()
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "東京", "race_num": 11, "start_time": "15:40",
              "race_info": "【東京 11R】芝1600m 15:40発走　コリジョンG2",
              "day_label": "テスト当日"},
    })
    resp = client.get("/graded/api/this_week")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["races"]) == 1
    assert data["races"][0]["key"] == "コリジョ@3"


def test_graded_api_history_accepts_subkey(collision_fixture_env, monkeypatch):
    monkeypatch.setattr(jra_graded, "CACHE_PATH", collision_fixture_env["out_path"])
    app = jra_suite.create_app()
    client = app.test_client()
    resp = client.get("/graded/api/history?key=コリジョ@9&years=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["master"]["key"] == "コリジョ@9"
    assert data["master"]["distance_latest"] == 1200
