"""SPEC-T79c §2: 週末重賞の先行表示のための純関数群 (副作用なし。DB・ネットワークには
一切アクセスしない)。表示専用の参考情報であり、予測・EV・仮想購入判断には使わない。

実際のHTTP取得・DB保存 (6時間キャッシュの判定含む) は jra_graded.py 側が行い、
このモジュールはその材料 (出馬表HTMLからのメタ抽出・CNAMEの日付/レース番号解析・
重賞判定・dedupe/並び順・this_week統合) だけを提供する。

VENUE_MAP は jra_ev.py / api/index.py と同じ値を意図的に重複定義している
(このモジュールを純関数のみに保ち、jra_ev/api.index の重い import 依存を
持たせないため。値を変える場合は3箇所とも直すこと)。
"""
import re
from datetime import date as _date
from datetime import timedelta

from api.graded_names import match_key

VENUE_MAP = {"06": "中山", "08": "京都", "05": "東京", "09": "阪神", "07": "中京",
             "04": "新潟", "03": "福島", "10": "小倉", "01": "札幌", "02": "函館"}

# JRAの出馬表URL: CNAME=pw01dde01<場2桁><年4桁><回2桁><日2桁><R2桁><YYYYMMDD>/<hex>
# (SPEC-T79c §0)。"pw01dde01" の直後2桁は固定コードで、そのあとに場・年・回・日・R・
# 日付が続く。日付8桁は末尾の "/hex" の直前にあるので、そこから逆算して取り出す。
_CNAME_RE = re.compile(
    r"CNAME=pw\d+dde\d{2}(\d{2})\d{4}\d{2}\d{2}(\d{2})(\d{8})/[0-9A-Fa-f]+$")

_RACE_NAME_TAG_CLASS = "race_name"


def parse_cname(url):
    """出馬表URLのCNAMEから (venue_code, race_num, date) を取り出す。
    戻り値: {"venue_code": "09", "venue": "阪神", "race_num": 11, "date": "2026-09-26"}
    形式に一致しなければ None。"""
    m = _CNAME_RE.search(str(url or ""))
    if not m:
        return None
    venue_code, race_num_s, date_s = m.groups()
    try:
        race_num = int(race_num_s)
        d = _date(int(date_s[0:4]), int(date_s[4:6]), int(date_s[6:8]))
    except ValueError:
        return None
    if not (1 <= race_num <= 12):
        return None
    return {
        "venue_code": venue_code,
        "venue": VENUE_MAP.get(venue_code),
        "race_num": race_num,
        "date": d.isoformat(),
    }


_COURSE_RE = re.compile(r"コース[：:]\s*([\d,]+)メートル\s*[（(]\s*(芝|ダート)")
_START_TIME_RE = re.compile(r"発走時刻[：:]\s*(\d{1,2})時(\d{1,2})分")


def extract_card_meta(html):
    """出馬表HTML (accessD.html?CNAME=... のページ全文) から、レース名・発走時刻・
    芝ダ・距離・頭数・枠順確定の有無を読む (SPEC-T79c §2.1-b)。分析・スコアリングは
    一切行わず、api.index.analyze_race_url より大幅に軽い (ネットワークアクセスなし・
    このモジュールはHTML文字列を受け取るだけ)。

    戻り値: {"race_name", "start_time" ("HH:MM"|None), "track_type" ("芝"|"ダート"|None),
             "distance" (int|None), "head_count" (int), "draw_fixed" (bool)}
    """
    from bs4 import BeautifulSoup  # 遅延import (このモジュール単体でのimportを軽くする)

    soup = BeautifulSoup(str(html or ""), "html.parser")
    text = soup.get_text()

    rn_tag = soup.find(class_=_RACE_NAME_TAG_CLASS)
    race_name = rn_tag.get_text(strip=True) if rn_tag else None

    start_time = None
    tm = _START_TIME_RE.search(text)
    if tm:
        start_time = f"{int(tm.group(1)):02d}:{int(tm.group(2)):02d}"

    track_type = None
    distance = None
    cm = _COURSE_RE.search(text)
    if cm:
        try:
            distance = int(cm.group(1).replace(",", ""))
        except ValueError:
            distance = None
        track_type = cm.group(2)

    target_table = next((t for t in soup.find_all("table") if "馬名" in t.get_text()), None)
    rows = []
    if target_table is not None:
        rows = [r for r in target_table.find_all("tr")
                if len(r.find_all(["td", "th"])) >= 5 and "馬名" not in r.get_text()]

    numeric_rows = 0
    for r in rows:
        cells = r.find_all(["td", "th"])
        off = 1 if ("着" in cells[0].get_text() or
                    (len(cells) > 2 and cells[2].get_text(strip=True).isdigit())) else 0
        idx = 1 + off
        val = cells[idx].get_text(strip=True) if len(cells) > idx else ""
        if val.isdigit():
            numeric_rows += 1

    head_count = len(rows)
    # 枠順確定の有無 (SPEC-T79c §2.1-b): 頭数が1頭以上あり、そのうち大半 (>=80%) の
    # 行で馬番が数字として読めていれば確定済みとみなす。draw未確定のページは
    # 馬番セルが空/ハイフンになる想定 (実データでは今回検証時点で全て確定済みだった
    # ため、この閾値は仕様の意図に沿った推定ロジック)。
    draw_fixed = head_count > 0 and (numeric_rows / head_count) >= 0.8

    return {
        "race_name": race_name, "start_time": start_time, "track_type": track_type,
        "distance": distance, "head_count": head_count, "draw_fixed": draw_fixed,
    }


def classify_card(race_name, known_keys, *, place=None, track_type=None, distance=None,
                  sponsors=None, aliases=None):
    """レース名を既知の重賞キーに突合する (api.graded_names.match_key の薄いラッパー)。
    戻り値: (key, grade, how)。重賞でなければ (None, None, None)。"""
    if not race_name:
        return None, None, None
    return match_key(race_name, known_keys, place=place, track_type=track_type,
                     distance=distance, sponsors=sponsors, aliases=aliases)


def weekend_target_dates(today=None):
    """今日 (today, 省略時は本日) が属する週の土・日・月曜の3日を返す
    (SPEC-T79c §2.1: 「今週末 (次の土・日、月曜開催があればそれも)」)。
    月曜は常に候補として含める (実際に開催が無ければ後段の重賞判定で自然に0件になる)。
    週の始まりは月曜 (date.weekday()==0) とする。木・金なら「今週」の土日 = 今度来る
    直近の土日、土・日ならその週の土日 (今日を含む) になる。

    戻り値: [date, date, date] (土, 日, 月) の ISO文字列リスト。"""
    base = today or _date.today()
    monday_of_week = base - timedelta(days=base.weekday())
    saturday = monday_of_week + timedelta(days=5)
    sunday = monday_of_week + timedelta(days=6)
    next_monday = monday_of_week + timedelta(days=7)
    return [saturday.isoformat(), sunday.isoformat(), next_monday.isoformat()]


def dedupe_by_url(cards):
    """url で重複を除く (先に現れたものを残す)。url を持たないカード (rec['url']が
    未設定のEV監視レコード等) はそもそも重複判定の対象にならないため、そのまま残す。"""
    seen = set()
    out = []
    for c in cards:
        url = c.get("url")
        if url:
            if url in seen:
                continue
            seen.add(url)
        out.append(c)
    return out


def _sort_key(card):
    return (card.get("date") or "9999-99-99", card.get("start_time") or "99:99",
            card.get("venue") or "", card.get("race_num") or 99)


def sort_cards(cards):
    return sorted(cards, key=_sort_key)


def merge_this_week(ev_entries, weekend_entries):
    """SPEC-T79c §2.2: 既存の this_week (EV解析分、source='ev_state') と
    weekend_graded (source='jra_card') を url で dedupe して結合し、日付・発走順に
    並べる。同じ url が両方にあれば ev_state 側を残す (実際に解析済み = 枠順も
    確定済みなので draw_fixed=True を保証できる)。"""
    ev_urls = {e.get("url") for e in ev_entries if e.get("url")}
    merged = list(ev_entries) + [w for w in weekend_entries if w.get("url") not in ev_urls]
    return sort_cards(dedupe_by_url(merged))
