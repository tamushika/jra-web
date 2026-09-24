"""SPEC-T82: 開催カレンダー (開幕週・使用コース・天候/降水・馬場状態) の純関数群。

このモジュールは意図的に I/O を持たない (HTTP・DB・ファイル読み込みは呼び出し側の
責務)。実際の取得・保存は `fetch_course_usage.py` (CLI・フック共通の入口) と
`jra_suite.py` の `/race/api/meeting_calendar` ハンドラが行う。

- kaisai文字列 (Neon races.kaisai / 出馬表 r_info) の解析: `parse_kaisai`
- 土曜始まりの週番号割当 (月曜祝日開催は前週扱い): `week_start_for_date` / `assign_week_numbers`
- 馬場情報ページ (https://www.jra.go.jp/keiba/baba/index*.html) のHTML解析: `parse_baba_index_page`
- 馬場情報アーカイブPDF (fitz抽出テキスト) の日別使用コース解析: `parse_archive_pdf_text`
- Open-Meteo 応答の整形・weather_code変換: `format_open_meteo_daily` / `weather_code_label`
- 週次キャッシュの再取得規則: `weather_cache_is_stale`
- API応答の組み立て: `build_meeting_calendar_response`
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import date, timedelta

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 環境診断用 (bs4は既存依存)
    BeautifulSoup = None


VENUES = ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉")

# script.js の COURSE_DIRECTION と同一の座標 (T76で導入済み)。JS側は別ファイルの
# ため、Open-Meteo取得用にPython側でも同じ値を保持する (意図的な重複)。
VENUE_COORDS = {
    "札幌": {"lat": 43.075, "lon": 141.275},
    "函館": {"lat": 41.791, "lon": 140.781},
    "福島": {"lat": 37.766, "lon": 140.457},
    "新潟": {"lat": 37.954, "lon": 139.172},
    "中山": {"lat": 35.733, "lon": 139.957},
    "東京": {"lat": 35.662, "lon": 139.485},
    "中京": {"lat": 35.068, "lon": 136.988},
    "京都": {"lat": 34.908, "lon": 135.722},
    "阪神": {"lat": 34.779, "lon": 135.361},
    "小倉": {"lat": 33.834, "lon": 130.875},
}

# t50_track_measurements.py の VENUES と同じ英字表記 (アーカイブPDFのURL用)。
VENUE_EN = {
    "札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata",
    "東京": "tokyo", "中山": "nakayama", "中京": "chukyo", "京都": "kyoto",
    "阪神": "hanshin", "小倉": "kokura",
}

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

BABA_BASE_URL = "https://www.jra.go.jp/keiba/baba/"
ARCHIVE_URL_TEMPLATE = "https://www.jra.go.jp/keiba/baba/archive/{year}pdf/{venue_en}{meeting_no:02d}.pdf"

# Neon races.condition の短縮表記 (良/稍/重/不) → 表示用フルラベル。
CONDITION_LABELS = {"良": "良", "稍": "稍重", "重": "重", "不": "不良"}


# ─── kaisai 文字列の解析 ────────────────────────────────────────────────────

_KAISAI_RE = re.compile(
    r"(?P<meeting_no>\d+)回(?P<venue>" + "|".join(VENUES) + r")(?P<day_no>\d+)日"
)


def parse_kaisai(kaisai_str):
    """"2026年9月20日（日曜）4回中山6日 10レース" 形式から回次・場・日目を取り出す。
    解析できなければ None。"""
    if not kaisai_str:
        return None
    m = _KAISAI_RE.search(str(kaisai_str))
    if not m:
        return None
    return {
        "meeting_no": int(m.group("meeting_no")),
        "venue": m.group("venue"),
        "day_no": int(m.group("day_no")),
    }


# ─── 週番号の割当 (土曜始まり。月曜祝日開催は同じ週) ──────────────────────────

def week_start_for_date(d):
    """d を含む「土曜始まりの週」の土曜日を返す。
    date.weekday(): 月=0 ... 土=5, 日=6 なので、直近の土曜からの経過日数は
    (weekday-5) mod 7。月曜(0)なら2日前の土曜 → 直前の週末と同じ週になる。"""
    offset = (d.weekday() - 5) % 7
    return d - timedelta(days=offset)


def current_course_week_start(reference_date):
    """馬場情報ページ (https://www.jra.go.jp/keiba/baba/) の「今週から◯コースを
    使用します」という注記が指す週の開始土曜日を、取得を実行した実際の日付
    (reference_date) から求める。

    実データ確認 (SPEC-T82 §0) では、ページのh2に載る日付は「ページの最終更新
    タイミング」に依存して場ごとにずれ (同時刻に取得した中山ページは火曜、阪神
    ページは月曜の日付を表示していた) 、週の帰属先としては不安定だった。
    一方で「今週から」の注記はJRAが直近の週末に向けて発表するもので、平日
    (月〜金) に取得した場合は次の週末を、土日に取得した場合はその週末を指すと
    考えるのが素直なため、ページ内の日付ではなく取得実行時点の実日付でこの
    関数を呼ぶ想定 (fetch_course_usage.fetch_and_store_current_week_usage)。"""
    weekday = reference_date.weekday()
    if weekday in (5, 6):  # 土・日はその週末
        return week_start_for_date(reference_date)
    return reference_date + timedelta(days=(5 - weekday) % 7)  # 月〜金は次の週末


def assign_week_numbers(dates):
    """開催日の集合に、週の開始 (土曜) が古い順に 1,2,3... を割り振る。
    戻り値: {date: week_no}。飛び週があっても連番になる (欠番週は数えない)。"""
    starts = sorted({week_start_for_date(d) for d in dates})
    rank = {s: i + 1 for i, s in enumerate(starts)}
    return {d: rank[week_start_for_date(d)] for d in dates}


# ─── 馬場情報ページ (index.html / index2.html / index3.html) のHTML解析 ──────

_MEETING_HEADER_RE = re.compile(
    r"第(?P<meeting_no>\d+)回(?P<venue>" + "|".join(VENUES) + r")競馬第(?P<day_no>\d+)日"
    r"\s*[（(](?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
)
_COURSE_RE = re.compile(r"使用コース\s*([ABCD])\s*コース")


def _find_section_note(soup, heading_text):
    """<h3>heading_text</h3> と同じ data_list_unit 内の <div class="content"><p> を返す
    (使用コース節・芝の状態節の構造。SPEC-T82 §0で実データ確認済み)。"""
    h3 = soup.find("h3", string=lambda s: bool(s) and heading_text in s)
    if not h3:
        return None
    head_div = h3.find_parent("div", class_="head")
    unit = (head_div or h3).find_parent("div", class_="data_list_unit")
    if not unit:
        return None
    content = unit.find("div", class_="content")
    if not content:
        return None
    p = content.find("p")
    text = (p.get_text(" ", strip=True) if p else content.get_text(" ", strip=True))
    return text or None


def parse_baba_index_page(html_text):
    """JRA馬場情報ページ (1場分。index.html/index2.html/index3.html) のHTMLから、
    開催 (回次・場・日目・日付)・使用コース・使用コースの注記・場タブ一覧を取り出す。

    複数場開催時は各ページに `.kaisai_tab` があり、"中山競馬場"→index.html,
    "阪神競馬場"→index2.html のようにタブが並ぶ (SPEC-T82 §0で実データ確認済み)。
    解析できない項目はNoneのまま返す (呼び出し側でフォールバック/警告)。"""
    result = {"venue": None, "meeting_no": None, "day_no": None, "date": None,
              "course": None, "course_note": None, "tabs": []}
    if not html_text or BeautifulSoup is None:
        return result
    soup = BeautifulSoup(html_text, "html.parser")

    tab_div = soup.select_one(".kaisai_tab")
    if tab_div:
        for a in tab_div.select("a[href]"):
            label = a.get_text(strip=True)
            href = a.get("href")
            if href:
                result["tabs"].append({"venue": label, "href": href})

    text = soup.get_text("\n")
    m = _MEETING_HEADER_RE.search(text)
    if m:
        result["meeting_no"] = int(m.group("meeting_no"))
        result["venue"] = m.group("venue")
        result["day_no"] = int(m.group("day_no"))
        try:
            result["date"] = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        except ValueError:
            result["date"] = None

    cm = _COURSE_RE.search(text)
    if cm:
        result["course"] = cm.group(1)

    result["course_note"] = _find_section_note(soup, "芝の状態")
    return result


# ─── 馬場情報アーカイブPDF (fitzで抽出したテキスト) の日別使用コース解析 ──────

_YEAR_RE = re.compile(r"(?P<year>20\d{2})年")
_DATE_RE = re.compile(r"(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日")
_WEEKDAY_RE = re.compile(r"([月火水木金土日])曜日")
_COURSE_LETTER_RE = re.compile(r"(?<![A-Za-z])([ABCD])(?![A-Za-z])")
_DAY_NO_RE = re.compile(r"第\s*(\d+)\s*日")


def parse_archive_pdf_text(text):
    """馬場情報アーカイブPDF (2025年以降フォーマット。fitzでページ順に抽出したテキスト)
    から日別の (日付・曜日・使用コース・開催日次) を取り出す。

    フォーマット例 (nakayama03.pdf, fitz抽出): 各日の行は
    "<M月D日>\\n<曜日>曜日\\nA\\n<測定時刻>\\n<クッション値>\\n<測定時刻>\\n
    <芝ゴール前>\\n<芝4角>\\n<ダートゴール前>\\n<ダート4角>\\n[第N日]" の順で並ぶ。
    「第N日」は開催日次ラベルだが、fitzのテキスト順では *次の日付行の直前* に出現する
    (=このモジュールの実装時に実データで確認した、当該行そのものではなく次の行に
    帰属するレイアウト上のズレ)。したがって「この行のブロック内で見つかった第N日」は
    次の日付レコードのday_noとして採用する。2021-2024年の旧フォーマット (レイアウト
    が異なる) は非対応 (空リストを返す)。"""
    text = unicodedata.normalize("NFKC", text or "")
    year_m = _YEAR_RE.search(text)
    if not year_m:
        return []
    year = int(year_m.group("year"))

    matches = list(_DATE_RE.finditer(text))
    if not matches:
        return []

    bounds = []
    for i, m in enumerate(matches):
        line_start = text.rfind("\n", 0, m.start()) + 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        bounds.append((line_start, end, m))

    rows = []
    pending_day_no_for_next = {}
    for i, (start, end, m) in enumerate(bounds):
        chunk = text[start:end]
        wd = _WEEKDAY_RE.search(chunk)
        weekday = wd.group(1) if wd else None
        search_from = wd.end() if wd else max(0, m.end() - start)
        course = None
        cm = _COURSE_LETTER_RE.search(chunk, search_from)
        if cm:
            course = cm.group(1)
        dn = _DAY_NO_RE.search(chunk)
        if dn and i + 1 < len(bounds):
            pending_day_no_for_next[i + 1] = int(dn.group(1))
        try:
            day_date = date(year, int(m.group("month")), int(m.group("day")))
        except ValueError:
            continue
        rows.append({"date": day_date, "weekday": weekday, "course": course, "day_no": None})

    for idx, day_no in pending_day_no_for_next.items():
        if idx < len(rows):
            rows[idx]["day_no"] = day_no
    return rows


# ─── Open-Meteo 応答の整形 ──────────────────────────────────────────────────

def build_open_meteo_request(venue, start_date, end_date, *, forecast=False):
    """Open-Meteo (過去はarchive-api、当日/翌日はforecast api) のリクエストURL・
    パラメータを組み立てる。座標未知の場は None。"""
    coords = VENUE_COORDS.get(venue)
    if not coords:
        return None
    base = ("https://api.open-meteo.com/v1/forecast" if forecast
            else "https://archive-api.open-meteo.com/v1/archive")
    return {
        "url": base,
        "params": {
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "daily": "precipitation_sum,precipitation_hours,weather_code",
            "timezone": "Asia/Tokyo",
        },
    }


def format_open_meteo_daily(payload):
    """Open-Meteo応答 (archive/forecastいずれも同じdaily構造) を
    {date_iso: {weather_code, precipitation_mm, precipitation_hours}} に整形する。"""
    daily = (payload or {}).get("daily") or {}
    times = daily.get("time") or []
    precip = daily.get("precipitation_sum") or []
    hours = daily.get("precipitation_hours") or []
    codes = daily.get("weather_code") or []
    out = {}
    for i, d in enumerate(times):
        out[d] = {
            "weather_code": codes[i] if i < len(codes) else None,
            "precipitation_mm": precip[i] if i < len(precip) else None,
            "precipitation_hours": hours[i] if i < len(hours) else None,
        }
    return out


# WMO weather_code (Open-Meteo) → 晴/曇/雨/雪 の簡易分類。
_WMO_BANDS = (
    (0, 1, "晴"), (2, 3, "曇"), (45, 48, "曇"),
    (51, 67, "雨"), (71, 77, "雪"),
    (80, 82, "雨"), (85, 86, "雪"), (95, 99, "雨"),
)


def weather_code_label(code):
    if code is None:
        return "不明"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "不明"
    for lo, hi, label in _WMO_BANDS:
        if lo <= code <= hi:
            return label
    return "不明"


def weather_cache_is_stale(date_obj, today, fetched_at, now):
    """weather_daily キャッシュの再取得規則 (SPEC-T82 §1.3):
    当日分は6時間で再取得、それ以外 (過去の確定値) は再取得不要。
    fetched_at が無ければ常に再取得が必要。"""
    if fetched_at is None:
        return True
    if date_obj != today:
        return False
    return (now - fetched_at) >= timedelta(hours=6)


# ─── API応答の組み立て ──────────────────────────────────────────────────────

def build_meeting_calendar_response(*, venue, requested_date, today,
                                     neon_rows=None, course_usage_rows=None,
                                     archive_day_courses=None, weather_by_date=None,
                                     course_usage_recording_start=None):
    """`/race/api/meeting_calendar` の応答本体を組み立てる純関数。

    neon_rows: [{"date": date, "kaisai": str, "track_type": str, "condition": str}, ...]
        (Neon races の同一場の行。回次はkaisaiから解析する)
    course_usage_rows: [{"week_start": date, "meeting_no": int|None,
                          "course": str|None, "note": str|None}, ...]
        (meeting_course_usage の当該場の全行)
    archive_day_courses: {date: "A"|"B"|"C"|"D"} (アーカイブPDFの日別使用コース。
        週次記録より優先度は高い=より粒度が細かいので日別に上書きする)
    weather_by_date: {date: {"weather_code":.., "precipitation_mm":.., "precipitation_hours":..}}
    course_usage_recording_start: date|None (使用コースの自前記録を開始した日。
        あれば notes に注記する)
    """
    neon_rows = neon_rows or []
    course_usage_rows = course_usage_rows or []
    archive_day_courses = archive_day_courses or {}
    weather_by_date = weather_by_date or {}

    parsed = []
    for row in neon_rows:
        info = parse_kaisai(row.get("kaisai"))
        if not info or info["venue"] != venue or row.get("date") is None:
            continue
        parsed.append({
            "date": row["date"], "meeting_no": info["meeting_no"], "day_no": info["day_no"],
            "track_type": row.get("track_type"), "condition": row.get("condition"),
        })

    meeting_no_by_date = {}
    for p in parsed:
        meeting_no_by_date.setdefault(p["date"], p["meeting_no"])

    target_meeting_no = meeting_no_by_date.get(requested_date)
    if target_meeting_no is None:
        usage_before = [r for r in course_usage_rows
                        if r.get("meeting_no") is not None
                        and r["week_start"] <= week_start_for_date(requested_date)]
        if usage_before:
            target_meeting_no = max(usage_before, key=lambda r: r["week_start"])["meeting_no"]
    if target_meeting_no is None:
        earlier = [d for d in meeting_no_by_date if d <= requested_date]
        if earlier:
            target_meeting_no = meeting_no_by_date[max(earlier)]

    dates = {d for d, mno in meeting_no_by_date.items() if mno == target_meeting_no}
    dates.add(requested_date)
    dates = {d for d in dates if d <= today}  # 今後の日程は出さない
    sorted_dates = sorted(dates)
    week_no_map = assign_week_numbers(sorted_dates) if sorted_dates else {}

    week_start_map = {}
    for d in sorted_dates:
        week_start_map.setdefault(week_no_map[d], week_start_for_date(d))

    cond_by_date_track = {}
    for p in parsed:
        if p["date"] not in dates or not p["condition"]:
            continue
        cond_by_date_track.setdefault((p["date"], p["track_type"]), []).append(p["condition"])

    def _mode_condition(d, track_type):
        vals = cond_by_date_track.get((d, track_type)) or []
        if not vals:
            return None
        return Counter(vals).most_common(1)[0][0]

    day_no_by_date = {p["date"]: p["day_no"] for p in parsed if p["date"] in dates}
    usage_by_week = {r["week_start"]: r for r in course_usage_rows}

    days = []
    for d in sorted_dates:
        wk = week_no_map[d]
        wstart = week_start_map[wk]
        usage = usage_by_week.get(wstart)
        course = archive_day_courses.get(d)
        course_note = None
        course_source = None
        if course is not None:
            course_source = "archive"
        elif usage is not None:
            course = usage.get("course")
            course_note = usage.get("note")
            course_source = "live" if course else None
        weather = weather_by_date.get(d) or {}
        prev_weather = weather_by_date.get(d - timedelta(days=1)) or {}
        days.append({
            "date": d.isoformat(),
            "weekday": WEEKDAY_JP[d.weekday()],
            "day_no": day_no_by_date.get(d),
            "week_no": wk,
            "course": course,
            "course_note": course_note,
            "weather_code": weather.get("weather_code"),
            "precipitation_mm": weather.get("precipitation_mm"),
            "precipitation_hours": weather.get("precipitation_hours"),
            "rain_prev_day_mm": prev_weather.get("precipitation_mm"),
            "turf_condition": _mode_condition(d, "芝"),
            "dirt_condition": _mode_condition(d, "ダート"),
            "cushion": None,
            "turf_moisture_goal": None,
            "dirt_moisture_goal": None,
            "is_today": d == today,
            "source": {"course": course_source, "weather": ("archive" if d in weather_by_date else None)},
        })

    weeks = []
    for wk in sorted(set(week_no_map.values())):
        wk_days = [x for x in days if x["week_no"] == wk]
        wstart = week_start_map[wk]
        usage = usage_by_week.get(wstart)
        rain_dates = [wstart - timedelta(days=2), wstart - timedelta(days=1), wstart, wstart + timedelta(days=1)]
        rain_values = [weather_by_date.get(rd, {}).get("precipitation_mm") for rd in rain_dates]
        rain_mm_week = round(sum(v for v in rain_values if v is not None), 1) if any(
            v is not None for v in rain_values) else None
        weeks.append({
            "week_no": wk,
            "week_start": wstart.isoformat(),
            "course": usage.get("course") if usage else None,
            "course_note": usage.get("note") if usage else None,
            "rain_mm_week": rain_mm_week,
            "days": wk_days,
        })

    requested_iso = requested_date.isoformat()
    requested_day_no = day_no_by_date.get(requested_date)
    requested_week_no = week_no_map.get(requested_date)

    notes = []
    if course_usage_recording_start:
        notes.append(f"使用コースの記録は {course_usage_recording_start.isoformat()} から")
    if not usage_by_week and not archive_day_courses:
        notes.append("使用コースの記録がまだありません")

    label = f"{target_meeting_no}回{venue}" if target_meeting_no else venue

    return {
        "meeting": {
            "venue": venue,
            "meeting_no": target_meeting_no,
            "label": label,
            "first_date": sorted_dates[0].isoformat() if sorted_dates else None,
            "last_known_date": sorted_dates[-1].isoformat() if sorted_dates else None,
            "today_day_no": requested_day_no,
            "today_week_no": requested_week_no,
        },
        "weeks": weeks,
        "days": days,
        "notes": notes,
        "requested_date": requested_iso,
    }
