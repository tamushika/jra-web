"""SPEC-T79c (重賞データページの好成績セル強調と、週末重賞の金曜先行表示) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T79c-graded-highlight-weekend.md §3
  1. jra_graded.classify_highlight: 比率・n閾値・境界の純関数。history APIの
     overall_win_rate。
  2. api.graded_weekend: 出馬表HTMLからのメタ抽出 (実データfixture)、CNAMEの
     日付/レース番号/場コード解析、重賞判定 (match_keyラッパー)、dedupe/並び順、
     this_week統合。
  3. jra_graded._fetch_and_store_weekend_graded 周りの6時間キャッシュ規則と
     weekend_graded_cards の保存 (api.logging_store.LoggingStore)。
  4. /graded/api/weekend_graded, /graded/api/this_week (jra_suite経由)。
  5. 既存 test_t79_graded.py / test_t79b_graded_pick.py の回帰、通知関数AST不変
     (test_t62b_shadow.py::test_notification_function_sources_are_frozen で担保)。

出馬表HTML fixture (tests/fixtures/t79c/*.html) のうち card_draw_not_fixed.html
以外は、実装時 (2026-09-25) に実際にJRAへアクセスして取得した実データ。
card_draw_not_fixed.html だけは、当時取得できた出馬表が全て枠順確定済みだった
ため「枠順未確定」ケースを検証するために手書きした合成フィクスチャ (ファイル冒頭に
コメントで明記)。
"""
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import jra_ev
import jra_graded
import jra_suite
from api import graded_weekend
from api.logging_store import LoggingStore
from test_t79_graded import _build_fixture_ability_db

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "t79c")

# jra_graded._known_keys_dict() が返す形式 (key -> {"place","track_type","distance"})
# を模した、実際のability.db由来キーの一部。実際のキャッシュでは「スプリン」は
# 分割済みサブキー ("スプリン@10" 等) だが、_known_keys_dict() は突合用に基底キー
# ("スプリン") へ畳んで返す (jra_graded._base_key) ので、ここでも基底キーで揃える。
# サブキーへの解決 (「スプリン」→「スプリン@10」) は jra_graded._resolve_final_key
# の責務で、classify_card (=match_key) の対象外 (実データ確認で別途検証済み)。
_KNOWN_KEYS = {
    "シリウス": {"place": "阪神", "track_type": "ダート", "distance": 2000},
    "スプリン": {"place": "中山", "track_type": "芝", "distance": 1200},
}


def _read_fixture(name):
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


# ─── §3.1: classify_highlight (強調判定の純関数) ───────────────────────────

@pytest.mark.parametrize("n,fuku_rate,win_rate,overall_fuku,overall_win,expected", [
    # n<8はどれだけ比が高くても色を付けない。
    (7, 0.9, 0.9, 0.3, 0.1, None),
    # 複勝率比 1.5倍ちょうど (境界含む) -> strong (win側は低比率で無関係)。
    (8, 0.375, 0.05, 0.25, 0.1, "strong"),
    # 勝率比 2.0倍ちょうど (境界含む) -> strong (複勝率比は低くても勝率側で強)。
    (8, 0.1, 0.2, 0.25, 0.1, "strong"),
    # 複勝率比 1.2倍ちょうど (strongの下限未満) -> mid。
    (8, 0.3, 0.05, 0.25, 0.5, "mid"),
    # 勝率比 1.5倍ちょうど (複勝率比は1.0=中間帯) -> mid。
    (8, 0.25, 0.375, 0.25, 0.25, "mid"),
    # 複勝率比 1.2倍未満・0.6倍超、勝率比も低い -> 中間帯で色無し。
    (8, 0.25, 0.05, 0.25, 0.5, None),
    # 複勝率比 0.6倍ちょうど (境界含む) -> weak。
    (8, 0.15, 0.05, 0.25, 0.5, "weak"),
    # 複勝率比 0.6倍未満 -> weak。
    (8, 0.05, 0.0, 0.25, 0.5, "weak"),
    # 全体値が無い (レースデータ不足) -> None。
    (20, 0.9, 0.9, None, None, None),
])
def test_classify_highlight_boundaries(n, fuku_rate, win_rate, overall_fuku, overall_win, expected):
    assert jra_graded.classify_highlight(n, fuku_rate, win_rate, overall_fuku, overall_win) == expected


def test_classify_highlight_n_zero_or_none_is_none():
    assert jra_graded.classify_highlight(0, 0.9, 0.9, 0.3, 0.1) is None
    assert jra_graded.classify_highlight(None, 0.9, 0.9, 0.3, 0.1) is None


# ─── §3.1: overall_win_rate がhistory APIに含まれる ────────────────────────

@pytest.fixture()
def fixture_env(tmp_path):
    ability_db = tmp_path / "ability_fixture.db"
    _build_fixture_ability_db(str(ability_db))
    out_path = tmp_path / "graded_cache.sqlite"
    import build_graded_cache
    result = build_graded_cache.build_cache(
        years_from=2023, ability_db_path=str(ability_db), out_path=str(out_path))
    return {"out_path": str(out_path), "result": result}


@pytest.fixture()
def suite_client(fixture_env, monkeypatch, tmp_path):
    monkeypatch.setattr(jra_graded, "CACHE_PATH", fixture_env["out_path"])
    store = LoggingStore(str(tmp_path / "test_logging.db"))
    monkeypatch.setattr(jra_graded, "_weekend_store", lambda: store)
    app = jra_suite.create_app()
    return app.test_client(), store


def test_history_api_includes_overall_win_rate_and_row_highlight(suite_client):
    client, _store = suite_client
    resp = client.get("/graded/api/history?key=サンプル&years=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "overall_win_rate" in data
    # 「サンプル」全3開催・のべ9走中 rank=None (2024年ウマC) の1走を除く8走が対象。
    # 1着はウマA(2023)・ウマB(2024)・ウマA(2025) の3件 -> 3/8。
    assert data["overall_win_rate"] == pytest.approx(3 / 8, abs=1e-3)
    pop1 = next(g for g in data["aggregates"]["popularity"] if g["label"] == "1番人気")
    # n=3 < 8 なので強調なし (「n少」表示はUI側の責務)。
    assert pop1["highlight"] is None
    assert "roi_win_gold" in pop1 and "roi_fuku_gold" in pop1


# ─── §3.2: api.graded_weekend の出馬表HTMLメタ抽出 (実データfixture) ──────────

@pytest.mark.parametrize("fixture_name,expected_name,expected_start,expected_track,expected_dist", [
    ("card_hanshin_09_26_11_sirius.html", "シリウスステークス", "15:45", "ダート", 2000),
    ("card_nakayama_09_27_11_sprinters.html", "スプリンターズステークス", "15:40", "芝", 1200),
    ("card_9_26_4_8__11.html", "秋風ステークス", "15:30", "芝", 1600),
])
def test_extract_card_meta_from_real_fixtures(fixture_name, expected_name, expected_start,
                                              expected_track, expected_dist):
    meta = graded_weekend.extract_card_meta(_read_fixture(fixture_name))
    assert meta["race_name"] == expected_name
    assert meta["start_time"] == expected_start
    assert meta["track_type"] == expected_track
    assert meta["distance"] == expected_dist
    assert meta["head_count"] > 0
    assert meta["draw_fixed"] is True  # 実データ取得時点でいずれも枠順確定済み


def test_extract_card_meta_draw_not_fixed_synthetic_fixture():
    meta = graded_weekend.extract_card_meta(_read_fixture("card_draw_not_fixed.html"))
    assert meta["race_name"] == "サンプル特別"
    assert meta["head_count"] == 3
    assert meta["draw_fixed"] is False


def test_extract_card_meta_handles_empty_html():
    meta = graded_weekend.extract_card_meta("")
    assert meta == {"race_name": None, "start_time": None, "track_type": None,
                    "distance": None, "head_count": 0, "draw_fixed": False}


# ─── §3.2: CNAMEからの日付/レース番号/場 解析 ──────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0109202604081120260926/08",
     {"venue_code": "09", "venue": "阪神", "race_num": 11, "date": "2026-09-26"}),
    ("https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0106202604091120260927/5A",
     {"venue_code": "06", "venue": "中山", "race_num": 11, "date": "2026-09-27"}),
    ("https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0106202604080920260926/80",
     {"venue_code": "06", "venue": "中山", "race_num": 9, "date": "2026-09-26"}),
])
def test_parse_cname(url, expected):
    assert graded_weekend.parse_cname(url) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/not-jra",
    "https://www.jra.go.jp/JRADB/accessD.html?CNAME=garbage",
])
def test_parse_cname_returns_none_for_unrecognized_urls(url):
    assert graded_weekend.parse_cname(url) is None


# ─── §3.2: 重賞判定 (match_keyラッパー) ─────────────────────────────────────

@pytest.mark.parametrize("race_name,place,track_type,distance,expected_key", [
    ("シリウスステークス", "阪神", "ダート", 2000, "シリウス"),
    ("スプリンターズステークス", "中山", "芝", 1200, "スプリン"),
])
def test_classify_card_resolves_graded_races(race_name, place, track_type, distance, expected_key):
    # 出馬表ページのレース名 (class="race_name") にはG1/G2/G3の格表記が無いため、
    # ここで返るgradeは常にNone。実際のgrade表示はjra_graded側でmaster.grade_latest
    # を引いて付与する (実データ確認: /graded/api/weekend_graded?refresh=1 で
    # grade_latest="G3"/"G1"が正しく載ることを確認済み)。
    key, grade, how = graded_weekend.classify_card(
        race_name, _KNOWN_KEYS, place=place, track_type=track_type, distance=distance)
    assert key == expected_key
    assert grade is None
    assert how == "exact"


@pytest.mark.parametrize("race_name", [
    "秋風ステークス",
    "勝浦特別",
    "芙蓉ステークス",
    "ながつきステークス",  # SPEC-T79c §3: 非重賞は除外の例として明示されている名前
])
def test_classify_card_excludes_non_graded_races(race_name):
    key, grade, how = graded_weekend.classify_card(race_name, _KNOWN_KEYS)
    assert (key, grade, how) == (None, None, None)


def test_classify_card_none_race_name_returns_none():
    assert graded_weekend.classify_card(None, _KNOWN_KEYS) == (None, None, None)


# ─── §3.2: weekend_target_dates (今週末の対象日) ───────────────────────────

@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 24), ["2026-09-26", "2026-09-27", "2026-09-28"]),  # 木
    (date(2026, 9, 25), ["2026-09-26", "2026-09-27", "2026-09-28"]),  # 金
    (date(2026, 9, 26), ["2026-09-26", "2026-09-27", "2026-09-28"]),  # 土 (当日含む)
    (date(2026, 9, 27), ["2026-09-26", "2026-09-27", "2026-09-28"]),  # 日
    (date(2026, 9, 28), ["2026-10-03", "2026-10-04", "2026-10-05"]),  # 月 (次の週末)
])
def test_weekend_target_dates(today, expected):
    assert graded_weekend.weekend_target_dates(today) == expected


# ─── §3.2: dedupe・並び順 ───────────────────────────────────────────────────

def test_dedupe_by_url_keeps_first_and_drops_urlless_duplicates_only():
    cards = [
        {"url": "https://a", "label": "first"},
        {"url": "https://a", "label": "second (dup, dropped)"},
        {"url": None, "label": "no-url-1 (kept, not a dup of anything)"},
        {"url": None, "label": "no-url-2 (kept)"},
        {"url": "https://b", "label": "third"},
    ]
    out = graded_weekend.dedupe_by_url(cards)
    labels = [c["label"] for c in out]
    assert labels == ["first", "no-url-1 (kept, not a dup of anything)",
                      "no-url-2 (kept)", "third"]


def test_sort_cards_orders_by_date_then_start_time():
    cards = [
        {"date": "2026-09-27", "start_time": "11:00", "venue": "中山", "race_num": 9},
        {"date": "2026-09-26", "start_time": "15:45", "venue": "阪神", "race_num": 11},
        {"date": "2026-09-26", "start_time": "10:00", "venue": "中山", "race_num": 1},
    ]
    out = graded_weekend.sort_cards(cards)
    assert [(c["date"], c["start_time"]) for c in out] == [
        ("2026-09-26", "10:00"), ("2026-09-26", "15:45"), ("2026-09-27", "11:00")]


def test_merge_this_week_dedupes_by_url_and_prefers_ev_state():
    ev_entries = [
        {"url": "https://a", "source": "ev_state", "draw_fixed": True,
         "date": "2026-09-26", "start_time": "15:45", "display_name": "EV版"},
    ]
    weekend_entries = [
        {"url": "https://a", "source": "jra_card", "draw_fixed": True,
         "date": "2026-09-26", "start_time": "15:45", "display_name": "重複 (捨てられる)"},
        {"url": "https://b", "source": "jra_card", "draw_fixed": False,
         "date": "2026-09-27", "start_time": "15:40", "display_name": "週末版のみ"},
    ]
    merged = graded_weekend.merge_this_week(ev_entries, weekend_entries)
    assert len(merged) == 2  # url重複が1件に (https://a はev_state側のみ残る)
    assert merged[0]["url"] == "https://a"
    assert merged[0]["source"] == "ev_state"
    assert merged[0]["display_name"] == "EV版"
    assert merged[1]["url"] == "https://b"
    assert merged[1]["source"] == "jra_card"
    assert merged[1]["draw_fixed"] is False


# ─── §3.3: weekend_graded_cards の保存・6時間キャッシュ規則 ────────────────

@pytest.fixture()
def weekend_store(tmp_path):
    return LoggingStore(str(tmp_path / "test_logging.db"))


def test_save_and_list_weekend_graded_cards_round_trip(weekend_store):
    cards = [
        {"url": "https://a", "date": "2026-09-26", "venue": "阪神", "race_num": 11,
         "race_name": "シリウスステークス", "key": "シリウス", "display_name": "シリウスステークス",
         "grade_latest": "G3", "start_time": "15:45", "draw_fixed": True, "source": "jra_card"},
    ]
    saved = weekend_store.save_weekend_graded_cards(cards)
    assert saved == 1
    rows = weekend_store.list_weekend_graded_cards(min_date="2026-09-01")
    assert len(rows) == 1
    assert rows[0]["key"] == "シリウス"
    assert rows[0]["draw_fixed"] is True  # INTEGER(0/1) -> bool に変換されている

    # min_date フィルタ: それより前の日付は返らない。
    assert weekend_store.list_weekend_graded_cards(min_date="2026-09-27") == []


def test_save_weekend_graded_cards_upserts_by_url(weekend_store):
    base = {"url": "https://a", "date": "2026-09-26", "venue": "阪神", "race_num": 11,
           "race_name": "シリウスステークス", "key": "シリウス", "display_name": "シリウスステークス",
           "grade_latest": "G3", "start_time": "15:45", "draw_fixed": False, "source": "jra_card"}
    weekend_store.save_weekend_graded_cards([base])
    updated = dict(base, draw_fixed=True)
    weekend_store.save_weekend_graded_cards([updated])
    rows = weekend_store.list_weekend_graded_cards(min_date="2026-09-01")
    assert len(rows) == 1
    assert rows[0]["draw_fixed"] is True


def test_weekend_graded_fresh_within_6_hours(weekend_store):
    now = datetime.now(timezone.utc)
    weekend_store.save_weekend_graded_cards(
        [{"url": "https://a", "date": "2026-09-26"}],
        fetched_at=(now - timedelta(hours=3)).isoformat().replace("+00:00", "Z"))
    assert jra_graded._weekend_graded_fresh(weekend_store) is True


def test_weekend_graded_stale_after_6_hours(weekend_store):
    now = datetime.now(timezone.utc)
    weekend_store.save_weekend_graded_cards(
        [{"url": "https://a", "date": "2026-09-26"}],
        fetched_at=(now - timedelta(hours=7)).isoformat().replace("+00:00", "Z"))
    assert jra_graded._weekend_graded_fresh(weekend_store) is False


def test_weekend_graded_fresh_false_when_no_rows(weekend_store):
    assert jra_graded._weekend_graded_fresh(weekend_store) is False


# ─── §3.3: /graded/api/weekend_graded は6時間以内なら再取得しない ─────────

def test_api_weekend_graded_uses_cache_without_refetch_when_fresh(suite_client, monkeypatch):
    client, store = suite_client
    now = datetime.now(timezone.utc)
    store.save_weekend_graded_cards(
        [{"url": "https://a", "date": "2026-09-26", "venue": "阪神", "race_num": 11,
          "race_name": "シリウスステークス", "key": "シリウス",
          "display_name": "シリウスステークス", "grade_latest": "G3",
          "start_time": "15:45", "draw_fixed": True, "source": "jra_card"}],
        fetched_at=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))

    called = {"n": 0}

    def _boom(*_a, **_kw):
        called["n"] += 1
        raise AssertionError("再取得してはいけない (6時間キャッシュ内)")
    monkeypatch.setattr(jra_graded.requests, "get", _boom)

    resp = client.get("/graded/api/weekend_graded")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["fetched_now"] == 0
    assert called["n"] == 0
    assert len(data["races"]) == 1
    assert data["races"][0]["key"] == "シリウス"


# ─── §3.4: /graded/api/this_week の統合 (EV分 + weekend分、url重複が1件に) ──

def test_this_week_merges_ev_state_and_weekend_graded(suite_client, monkeypatch):
    client, store = suite_client
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "中山", "race_num": 11, "start_time": "15:40",
              "race_info": "【中山 11R】芝2000m 15:40発走　サンプルG2",
              "day_label": "テスト当日", "url": "https://ev-only"},
    })
    today_iso = datetime.now().date().isoformat()
    store.save_weekend_graded_cards([
        # ev_state分と同じurl -> dedupeで1件に畳まれる。
        {"url": "https://ev-only", "date": today_iso, "venue": "中山", "race_num": 11,
         "race_name": "サンプルG2", "key": "サンプル", "display_name": "サンプル",
         "grade_latest": "G2", "start_time": "15:40", "draw_fixed": True, "source": "jra_card"},
        # weekend分のみのレース。
        {"url": "https://weekend-only", "date": today_iso, "venue": "阪神", "race_num": 12,
         "race_name": "テストG3", "key": "テスト", "display_name": "テスト",
         "grade_latest": "G3", "start_time": "16:00", "draw_fixed": False, "source": "jra_card"},
    ])
    resp = client.get("/graded/api/this_week")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["races"]) == 2  # url重複 (https://ev-only) が1件に畳まれている
    by_url = {r["url"]: r for r in data["races"]}
    assert by_url["https://ev-only"]["source"] == "ev_state"
    assert by_url["https://ev-only"]["draw_fixed"] is True
    assert by_url["https://weekend-only"]["source"] == "jra_card"
    assert by_url["https://weekend-only"]["draw_fixed"] is False


def test_this_week_shows_weekend_only_races_when_ev_state_empty(suite_client, monkeypatch):
    # SPEC-T79c の主目的: 金曜など解析なしの期間でも週末重賞が見える。
    client, store = suite_client
    monkeypatch.setitem(jra_ev.STATE, "races", {})
    today_iso = datetime.now().date().isoformat()
    store.save_weekend_graded_cards([
        {"url": "https://weekend-only", "date": today_iso, "venue": "阪神", "race_num": 12,
         "race_name": "テストG3", "key": "テスト", "display_name": "テスト",
         "grade_latest": "G3", "start_time": "16:00", "draw_fixed": False, "source": "jra_card"},
    ])
    resp = client.get("/graded/api/this_week")
    data = resp.get_json()
    assert len(data["races"]) == 1
    assert data["races"][0]["source"] == "jra_card"


# ─── §3: index_graded.html にUI文言・要素が存在する (回帰・簡易チェック) ────

def test_index_graded_html_has_legend_and_weekend_ui_elements():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "index_graded.html"), encoding="utf-8").read()
    assert "強" in html and "1.5倍" in html  # 凡例
    assert "枠順未確定" in html
    assert "週末の重賞を再取得" in html
