"""SPEC-T82 (開催カレンダー: 開幕週・使用コース・天候/降水・馬場状態) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T82-meeting-calendar.md
  §2 kaisai文字列の解析・週番号の割当 (土日+月曜祝日・飛び週)
  §2 アーカイブPDFテキスト (fixture) からの日別使用コース解析
  §2 馬場情報ページHTML (fixture、単一場・複数場) からの使用コース抽出
  §2 Open-Meteo応答の整形とweather_code変換・キャッシュ再取得規則
  §2 meeting_course_usage の append-only・API応答形 (Neonをモック)
  §2 本番Webで API が無いときにUIが何も描かないこと (文字列検査)
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import meeting_calendar as mc  # noqa: E402
from api.logging_store import LoggingStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "t82"
JST = timezone(timedelta(hours=9))


def _read_fixture(name):
    # SPEC §6: HTMLはCRLFの場合があるため \n に正規化して読む。
    return (FIXTURES / name).read_text(encoding="utf-8").replace("\r\n", "\n")


# ─── kaisai文字列の解析 ─────────────────────────────────────────────────────

def test_parse_kaisai_extracts_meeting_venue_day():
    info = mc.parse_kaisai("2026年9月20日（日曜）4回中山6日 10レース")
    assert info == {"meeting_no": 4, "venue": "中山", "day_no": 6}


def test_parse_kaisai_returns_none_for_unparseable():
    assert mc.parse_kaisai("") is None
    assert mc.parse_kaisai(None) is None
    assert mc.parse_kaisai("よくわからない文字列") is None


# ─── 週番号の割当 (土曜始まり。月曜祝日は同じ週。飛び週があっても連番) ─────────

def test_week_start_for_date_saturday_anchored():
    assert mc.week_start_for_date(date(2026, 9, 19)) == date(2026, 9, 19)  # 土
    assert mc.week_start_for_date(date(2026, 9, 20)) == date(2026, 9, 19)  # 日
    assert mc.week_start_for_date(date(2026, 9, 21)) == date(2026, 9, 19)  # 月(祝) → 同じ週
    assert mc.week_start_for_date(date(2026, 9, 25)) == date(2026, 9, 19)  # 金 → 前週扱い


def test_assign_week_numbers_handles_gaps_and_holiday_monday():
    dates = [date(2026, 9, 5), date(2026, 9, 6),  # week1 (土日)
             date(2026, 9, 19), date(2026, 9, 20), date(2026, 9, 21)]  # week3 (土日+月祝)
    weeks = mc.assign_week_numbers(dates)
    assert weeks[date(2026, 9, 5)] == 1
    assert weeks[date(2026, 9, 6)] == 1
    assert weeks[date(2026, 9, 19)] == 2  # 飛び週があっても連番 (欠番週は数えない)
    assert weeks[date(2026, 9, 20)] == 2
    assert weeks[date(2026, 9, 21)] == 2  # 月曜祝日開催は土日と同じ週


def test_current_course_week_start_weekday_points_to_upcoming_weekend():
    # 実データ確認 (2026-09-24 木曜取得): 平日は次の週末を指す
    assert mc.current_course_week_start(date(2026, 9, 24)) == date(2026, 9, 26)
    assert mc.current_course_week_start(date(2026, 9, 21)) == date(2026, 9, 26)  # 月
    # 土日はその週末自身
    assert mc.current_course_week_start(date(2026, 9, 19)) == date(2026, 9, 19)
    assert mc.current_course_week_start(date(2026, 9, 20)) == date(2026, 9, 19)


# ─── アーカイブPDF (fitz抽出テキスト) の日別使用コース解析 ─────────────────────

def test_parse_archive_pdf_text_nakayama03_real_extraction():
    # tests/fixtures/t82/nakayama03_archive_text.txt は
    # data/t50/raw/2026/nakayama03.pdf を実際にfitzで抽出したテキストそのもの。
    text = _read_fixture("nakayama03_archive_text.txt")
    rows = mc.parse_archive_pdf_text(text)
    assert [r["date"] for r in rows] == [
        date(2026, 3, 27), date(2026, 3, 28), date(2026, 3, 29),
        date(2026, 4, 3), date(2026, 4, 4), date(2026, 4, 5),
        date(2026, 4, 10), date(2026, 4, 11), date(2026, 4, 12),
        date(2026, 4, 17), date(2026, 4, 18), date(2026, 4, 19),
    ]
    assert [r["weekday"] for r in rows] == list("金土日") * 4
    assert [r["course"] for r in rows] == list("AAA") + list("BBB") * 2 + list("CCC")
    # 第N日ラベルは金曜(pre-measurement)には付かず、土日にのみ付く
    # (fitzのテキスト順ではラベルが1つ前の行に出現するため、次行に帰属させる)
    assert [r["day_no"] for r in rows] == [
        None, 1, 2, None, 3, 4, None, 5, 6, None, 7, 8,
    ]


def test_parse_archive_pdf_text_empty_for_unrecognized_text():
    assert mc.parse_archive_pdf_text("") == []
    assert mc.parse_archive_pdf_text("年も月日も無い文章") == []


# ─── 馬場情報ページHTML (単一場・複数場) の使用コース抽出 ───────────────────────

def test_parse_baba_index_page_multi_venue_nakayama():
    html = _read_fixture("baba_index_nakayama.html")
    result = mc.parse_baba_index_page(html)
    assert result["venue"] == "中山"
    assert result["meeting_no"] == 4
    assert result["day_no"] == 7
    assert result["date"] == date(2026, 9, 22)
    assert result["course"] == "C"
    assert "今週からCコースを使用します" in result["course_note"]
    assert {"venue": "中山競馬場", "href": "index.html"} in result["tabs"]
    assert {"venue": "阪神競馬場", "href": "index2.html"} in result["tabs"]
    assert len(result["tabs"]) == 2  # 複数場開催 (2場)


def test_parse_baba_index_page_multi_venue_hanshin():
    html = _read_fixture("baba_index2_hanshin.html")
    result = mc.parse_baba_index_page(html)
    assert result["venue"] == "阪神"
    assert result["meeting_no"] == 4
    assert result["course"] == "B"
    assert "今週からBコースを使用します" in result["course_note"]
    # 複数場開催時、どのページからも同じタブ一覧が取れる (SPEC-T82 §0)
    assert len(result["tabs"]) == 2


def test_parse_baba_index_page_single_venue_synthetic_fixture():
    # 実データ確認時点 (2026-09-24) は単一場開催の実例が無かったため、
    # 実データと同じDOM構造をタブ1つに削った合成フィクスチャで検証する
    # (tests/fixtures/t82/baba_index_single_venue.html の先頭コメント参照)。
    html = _read_fixture("baba_index_single_venue.html")
    result = mc.parse_baba_index_page(html)
    assert result["venue"] == "東京"
    assert result["meeting_no"] == 1
    assert result["day_no"] == 2
    assert result["course"] == "D"
    assert len(result["tabs"]) == 1


def test_parse_baba_index_page_handles_missing_or_empty_html():
    empty = mc.parse_baba_index_page("")
    assert empty["venue"] is None
    assert empty["tabs"] == []
    assert mc.parse_baba_index_page("<html><body>no data</body></html>")["course"] is None


# ─── Open-Meteo 応答の整形とweather_code変換 ────────────────────────────────

def test_format_open_meteo_daily_matches_real_response_shape():
    # SPEC §0 実データ確認 (中山 9/12-9/21) の値をそのまま使う。
    payload = {
        "daily": {
            "time": ["2026-09-20", "2026-09-21"],
            "precipitation_sum": [76.9, 153.0],
            "precipitation_hours": [19.0, 24.0],
            "weather_code": [65, 65],
        }
    }
    formatted = mc.format_open_meteo_daily(payload)
    assert formatted["2026-09-20"] == {
        "weather_code": 65, "precipitation_mm": 76.9, "precipitation_hours": 19.0}
    assert formatted["2026-09-21"]["precipitation_mm"] == 153.0


def test_format_open_meteo_daily_handles_empty_payload():
    assert mc.format_open_meteo_daily(None) == {}
    assert mc.format_open_meteo_daily({}) == {}


@pytest.mark.parametrize("code,label", [
    (0, "晴"), (1, "晴"), (3, "曇"), (45, "曇"), (51, "雨"), (63, "雨"),
    (65, "雨"), (71, "雪"), (85, "雪"), (95, "雨"), (None, "不明"), (999, "不明"),
])
def test_weather_code_label(code, label):
    assert mc.weather_code_label(code) == label


# ─── weather_daily キャッシュの再取得規則 ──────────────────────────────────

def test_weather_cache_is_stale_today_refetches_after_6_hours():
    today = date(2026, 9, 24)
    now = datetime(2026, 9, 24, 18, 0, tzinfo=JST)
    fresh = now - timedelta(hours=2)
    stale = now - timedelta(hours=7)
    assert mc.weather_cache_is_stale(today, today, fresh, now) is False
    assert mc.weather_cache_is_stale(today, today, stale, now) is True
    assert mc.weather_cache_is_stale(today, today, None, now) is True


def test_weather_cache_is_stale_past_day_never_refetches():
    past_day = date(2026, 9, 20)
    today = date(2026, 9, 24)
    now = datetime(2026, 9, 24, 18, 0, tzinfo=JST)
    long_ago = now - timedelta(days=30)
    assert mc.weather_cache_is_stale(past_day, today, long_ago, now) is False


# ─── meeting_course_usage の append-only (LoggingStore) ────────────────────

def test_save_course_usage_is_append_only(tmp_path):
    store = LoggingStore(tmp_path / "t82.db")
    assert store.save_course_usage(venue="中山", week_start="2026-09-26", meeting_no=4,
                                   course="C", note="first", source_url="u1",
                                   fetched_at="2026-09-24T00:00:00+09:00") is True
    # 同一 (venue, week_start) への2度目の保存は上書きしない
    assert store.save_course_usage(venue="中山", week_start="2026-09-26", meeting_no=4,
                                   course="X", note="second", source_url="u2",
                                   fetched_at="2026-09-25T00:00:00+09:00") is False
    rows = store.list_course_usage("中山")
    assert len(rows) == 1
    assert rows[0]["course"] == "C"
    assert rows[0]["note"] == "first"


def test_weather_daily_cache_upserts(tmp_path):
    store = LoggingStore(tmp_path / "t82.db")
    store.save_weather_daily(venue="中山", date_str="20260920", weather_code=63,
                             precipitation_mm=76.9, precipitation_hours=19.0, source="archive")
    store.save_weather_daily(venue="中山", date_str="20260920", weather_code=65,
                             precipitation_mm=80.0, precipitation_hours=20.0, source="archive")
    cached = store.get_weather_daily("中山", "20260920")
    assert cached["weather_code"] == 65
    assert cached["precipitation_mm"] == 80.0


# ─── fetch_course_usage: 馬場情報ページのタブを辿って保存する ────────────────

def test_fetch_and_store_current_week_usage_walks_tabs(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    import fetch_course_usage as fcu

    pages = {
        "index.html": _read_fixture("baba_index_nakayama.html"),
        "index2.html": _read_fixture("baba_index2_hanshin.html"),
    }
    store = LoggingStore(tmp_path / "t82.db")
    result = fcu.fetch_and_store_current_week_usage(
        store=store, sleep_seconds=0, fetch=lambda path, timeout=10: pages[path])
    assert result["errors"] == []
    saved_venues = {r["venue"] for r in result["saved"]}
    assert saved_venues == {"中山", "阪神"}
    # SPEC-T82: 週の帰属先はページ内日付ではなく取得実行時点の実日付で決める
    for row in result["saved"]:
        assert row["week_start"] == mc.current_course_week_start(date.today()).isoformat()


def test_fetch_and_store_current_week_usage_reports_index_html_failure(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    import fetch_course_usage as fcu

    def fail(path, timeout=10):
        raise RuntimeError("network down")

    store = LoggingStore(tmp_path / "t82.db")
    result = fcu.fetch_and_store_current_week_usage(store=store, sleep_seconds=0, fetch=fail)
    assert result["saved"] == []
    assert result["errors"]


def test_import_archive_folds_days_into_weeks(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    import fetch_course_usage as fcu

    text = _read_fixture("nakayama03_archive_text.txt")

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def get_text(self):
            return self._text

    class _FakeDoc:
        def __init__(self, text):
            self._pages = [_FakePage(text)]

        def __iter__(self):
            return iter(self._pages)

    fake_fitz = type(sys)("fitz")
    fake_fitz.open = lambda path: _FakeDoc(text)
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)
    monkeypatch.setattr(fcu, "_download_archive_pdf",
                        lambda year, venue_en, meeting_no, dest_dir=None: ("dummy.pdf", "http://example/dummy.pdf"))

    store = LoggingStore(tmp_path / "t82.db")
    result = fcu.import_archive(2026, "nakayama", 3, store=store)
    rows = store.list_course_usage("中山")
    week_starts = sorted(r["week_start"] for r in rows)
    # 12日分のアーカイブ記録 (金曜のpre-measurementを除く土日8日) が4週分に畳まれる
    assert len(week_starts) == 4
    assert any(r["course"] == "A" for r in rows)
    assert any(r["course"] == "C" for r in rows)
    assert result["saved"]  # 何らかの新規保存があった


# ─── build_meeting_calendar_response: API応答形の組み立て (Neonをモック) ────

def test_build_meeting_calendar_response_shape_and_rain_and_condition():
    neon_rows = [
        {"date": date(2026, 9, 19), "kaisai": "2026年9月19日（土曜）4回中山5日 1レース",
         "track_type": "芝", "condition": "良"},
        {"date": date(2026, 9, 20), "kaisai": "2026年9月20日（日曜）4回中山6日 10レース",
         "track_type": "芝", "condition": "重"},
        {"date": date(2026, 9, 20), "kaisai": "2026年9月20日（日曜）4回中山6日 10レース",
         "track_type": "ダート", "condition": "不"},
    ]
    course_usage_rows = [
        {"week_start": date(2026, 9, 26), "meeting_no": 4, "course": "C", "note": "今週からCコース"},
    ]
    weather_by_date = {
        date(2026, 9, 19): {"weather_code": 63, "precipitation_mm": 11.4, "precipitation_hours": 12.0},
        date(2026, 9, 20): {"weather_code": 65, "precipitation_mm": 76.9, "precipitation_hours": 19.0},
    }
    body = mc.build_meeting_calendar_response(
        venue="中山", requested_date=date(2026, 9, 20), today=date(2026, 9, 24),
        neon_rows=neon_rows, course_usage_rows=course_usage_rows,
        weather_by_date=weather_by_date, course_usage_recording_start=date(2026, 9, 26))

    assert body["meeting"]["venue"] == "中山"
    assert body["meeting"]["meeting_no"] == 4
    assert body["meeting"]["label"] == "4回中山"
    assert body["meeting"]["today_day_no"] == 6
    days_by_date = {d["date"]: d for d in body["days"]}
    assert days_by_date["2026-09-20"]["turf_condition"] == "重"
    assert days_by_date["2026-09-20"]["dirt_condition"] == "不"
    assert days_by_date["2026-09-20"]["precipitation_mm"] == 76.9
    assert days_by_date["2026-09-20"]["rain_prev_day_mm"] == 11.4
    # 記録が9/26からなので、9/19-20週はまだ「記録なし」
    assert days_by_date["2026-09-20"]["course"] is None
    assert "使用コースの記録は 2026-09-26 から" in body["notes"]


def test_build_meeting_calendar_response_excludes_future_dates():
    body = mc.build_meeting_calendar_response(
        venue="中山", requested_date=date(2026, 9, 20), today=date(2026, 9, 20),
        neon_rows=[{"date": date(2026, 9, 27), "kaisai": "4回中山7日", "track_type": "芝", "condition": "良"}])
    assert all(d["date"] != "2026-09-27" for d in body["days"])


def test_build_meeting_calendar_response_empty_inputs_degrade_gracefully():
    body = mc.build_meeting_calendar_response(
        venue="中山", requested_date=date(2026, 9, 20), today=date(2026, 9, 20))
    assert body["days"] == [{
        "date": "2026-09-20", "weekday": "日", "day_no": None, "week_no": 1,
        "course": None, "course_note": None, "weather_code": None,
        "precipitation_mm": None, "precipitation_hours": None, "rain_prev_day_mm": None,
        "turf_condition": None, "dirt_condition": None, "cushion": None,
        "turf_moisture_goal": None, "dirt_moisture_goal": None, "is_today": True,
        "source": {"course": None, "weather": None},
    }]
    assert body["meeting"]["meeting_no"] is None
    assert body["meeting"]["label"] == "中山"


# ─── API (/race/api/meeting_calendar): jra_suiteの前段フックとの結線 ─────────

@pytest.fixture()
def suite_client(monkeypatch):
    import jra_suite
    app = jra_suite.create_app()
    return app.test_client()


def test_meeting_calendar_endpoint_rejects_bad_params(suite_client):
    resp = suite_client.get("/race/api/meeting_calendar?venue=不明&date=20260920")
    assert resp.status_code == 400
    resp2 = suite_client.get("/race/api/meeting_calendar?venue=中山&date=notadate")
    assert resp2.status_code == 400


def test_meeting_calendar_endpoint_builds_response_with_mocked_neon_and_weather(
        suite_client, monkeypatch, tmp_path):
    import jra_suite

    class _FakeConn:
        is_pg = False

        def cursor(self):
            return self

        def execute(self, query, params):
            self._rows = [
                {"date": "260920", "kaisai": "2026年9月20日（日曜）4回中山6日 10レース",
                 "track_type": "芝", "condition": "重"},
            ]

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class _FakePds:
        @staticmethod
        def get_db_connection(_base_dir):
            return _FakeConn()

    monkeypatch.setattr(jra_suite, "t82_pds", _FakePds)
    monkeypatch.setattr(jra_suite, "T82LoggingStore", lambda: LoggingStore(tmp_path / "t82_api.db"))
    monkeypatch.setattr(jra_suite, "_t82_fetch_weather_by_date",
                        lambda *a, **kw: {date(2026, 9, 20): {
                            "weather_code": 65, "precipitation_mm": 76.9, "precipitation_hours": 19.0}})

    resp = suite_client.get("/race/api/meeting_calendar?venue=中山&date=20260920")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["meeting"]["venue"] == "中山"
    assert body["meeting"]["meeting_no"] == 4
    day = next(d for d in body["days"] if d["date"] == "2026-09-20")
    assert day["turf_condition"] == "重"
    assert day["precipitation_mm"] == 76.9


# ─── 本番Web: APIが無い環境ではUIが何も描かないこと ─────────────────────────

def test_production_web_index_py_source_has_no_meeting_calendar_route():
    # api/index.py は不変 (SPEC §0/§5: 本番Web=Vercelはこのファイルのみで動く)。
    # index.app はプロセス内シングルトンで、他テストが先に jra_suite.create_app()
    # を呼ぶと before_request フックがそのモジュールオブジェクトに残ってしまうため
    # (test_t73b_autofill.py と同じ既知の特性)、実HTTPリクエストではテスト順序に
    # 依存せず検証できない。ソース文字列検査 (SPEC §2の意図どおり) で確認する。
    source = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert "meeting_calendar" not in source


def test_script_js_hides_panel_on_fetch_failure():
    # 文字列検査: fetchが失敗/非200の場合にパネルを隠すロジックが存在すること
    # (本番Webでは/race/api/meeting_calendarが無いため常にこの経路を通り、
    # パネルはindex.htmlのhidden属性のまま何も描かれない)。
    js = (ROOT / "script.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "function fetchMeetingCalendar" in js
    assert "t82HidePanel" in js
    assert "if (!res.ok) { t82HidePanel(); return; }" in js
    assert "if (!IS_EMBEDDED" in js


def test_index_html_meeting_calendar_panel_starts_hidden():
    html = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert 'id="meetingCalendar"' in html
    # 既定でhidden (JSが成功時のみhidden=falseにする) だが、統合版では開いた状態で出す
    assert 'id="meetingCalendar" class="meeting-calendar-panel" open hidden' in html
