"""重賞データページ (SPEC-T79)。
======================================================
過去の重賞 (ability.db, race_class='重賞' または 2026netkeiba由来のGI/GII/GIII表記)
の傾向・データを一覧表示する、表示専用 (予測・スコア・購入判断には一切関与しない)
のローカルダッシュボード。

事前に `python build_graded_cache.py` で作った data/graded_cache.sqlite
(ability.db から生成した読み取り専用キャッシュ) だけを参照する。ability.db 自体は
このモジュールからは一切開かない (500MBを毎回走査しない)。

【起動】 統合版 jra_suite.py に /graded として統合 (単体起動は無い)
"""
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date as _date
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, jsonify, request, send_from_directory

from api import graded_pick, graded_weekend
from api.graded_names import load_aliases, load_sponsors, match_key, normalize_race_name
from api.logging_store import LoggingStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "graded_cache.sqlite")

# SPEC-T79c §2.1: 週末重賞の先行表示。JRAへのアクセス頻度制限 (0.5秒間隔・
# 9〜12Rのみ・6時間キャッシュ) はこの節の定数で一元管理する。
_WEEKEND_HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "Referer": "https://www.jra.go.jp/"}
_WEEKEND_GRADED_TTL_SEC = 6 * 3600
_WEEKEND_GRADED_MAX_FETCHES = 36  # 3場 x 3日 x 4R (9〜12R)
_WEEKEND_GRADED_SLEEP_SEC = 0.5
_WEEKEND_GRADED_TIMEOUT_SEC = 10


def _weekend_store():
    """weekend_graded_cards の読み書きに使う LoggingStore (既定: data/jra_logging.db)。
    呼び出し箇所を1つに揃えることで、テストが monkeypatch で差し替えて本番の
    ログDBを汚さないようにできる (SPEC-T79c §2.1-d)。"""
    return LoggingStore()

_CURRENT_YEAR = datetime.now().year

bp = Blueprint("graded", __name__)

_MISSING_CACHE_MSG = "graded_cache.sqlite がありません。build_graded_cache.py を実行してください"

# SPEC-T79b レビュー対応: /api/pick が jra_ev のキャッシュを持たないURLを
# analyze_race_url() で直接スクレイプすると毎回数秒かかるため、年数切替や
# 「人気込み」トグルのたびに再スクレイプしないよう、プロセス内TTLキャッシュを
# 挟む (URL -> analyze_race_url()の戻り値そのもの)。出走馬構成はレース確定後は
# 変わらない前提で10分保持。EVの実況ポップ更新等はこのキャッシュの対象外
# (ev_by_num側は毎回jra_ev.STATEから取り直す)。
_SCRAPE_CACHE_TTL_SEC = 600
_SCRAPE_CACHE_MAX = 32
_SCRAPE_CACHE_LOCK = threading.Lock()
_SCRAPE_CACHE = {}  # url -> (expires_at, result)


def _scrape_cache_get(url):
    with _SCRAPE_CACHE_LOCK:
        entry = _SCRAPE_CACHE.get(url)
        if entry is None:
            return None
        expires_at, result = entry
        if expires_at < time.time():
            del _SCRAPE_CACHE[url]
            return None
        return result


def _scrape_cache_put(url, result):
    with _SCRAPE_CACHE_LOCK:
        _SCRAPE_CACHE[url] = (time.time() + _SCRAPE_CACHE_TTL_SEC, result)
        while len(_SCRAPE_CACHE) > _SCRAPE_CACHE_MAX:
            oldest_url = min(_SCRAPE_CACHE, key=lambda u: _SCRAPE_CACHE[u][0])
            del _SCRAPE_CACHE[oldest_url]

_POP_BUCKETS = (
    ("1番人気", lambda p: p == 1),
    ("2番人気", lambda p: p == 2),
    ("3番人気", lambda p: p == 3),
    ("4-6番人気", lambda p: 4 <= p <= 6),
    ("7-9番人気", lambda p: 7 <= p <= 9),
    ("10番人気以下", lambda p: p >= 10),
)

_WEIGHT_BUCKETS = (
    ("〜439", lambda w: w <= 439),
    ("440-479", lambda w: 440 <= w <= 479),
    ("480-519", lambda w: 480 <= w <= 519),
    ("520〜", lambda w: w >= 520),
)

_INTERVAL_BUCKETS = (
    ("中1週以下", lambda d: d <= 14),
    ("中2-3週", lambda d: 15 <= d <= 28),
    ("中4-8週", lambda d: 29 <= d <= 63),
    ("中9週以上", lambda d: d >= 64),
)

_PREV_RANK_BUCKETS = (
    ("1着", lambda r: r == 1),
    ("2-3着", lambda r: 2 <= r <= 3),
    ("4-9着", lambda r: 4 <= r <= 9),
    ("10着以下", lambda r: r >= 10),
)

_AFFI_LABELS = {"美": "美浦", "栗": "栗東"}


@bp.after_request
def _no_cache_html(resp):
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@bp.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index_graded.html")


def _cache_available():
    return os.path.exists(CACHE_PATH)


def _conn():
    conn = sqlite3.connect(f"file:{CACHE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _missing_cache_response():
    resp = jsonify({"error": _MISSING_CACHE_MSG})
    resp.status_code = 503
    return resp


def _base_key(key):
    """分割キー ('スプリン@3' 等) から4文字の基底キーを取り出す。
    match_key() は常に4文字キーしか返さないため、突合用の known_keys は
    基底キー単位に畳んで渡す (レビュー対応: SPEC §1 の4文字キー衝突分割)。"""
    return key.split("@", 1)[0]


def _known_keys_dict(conn):
    """match_key() 用の既知キー集合。分割された sub-key (例 'スプリン@3'/'@10') は
    基底キー ('スプリン') に畳んで1エントリにする (どちらの条件が代表になっても
    match_key の合否には影響しない。place/distance 一致による絞り込みは
    this_week 側の _resolve_final_key() で sub-key 単位に改めて行う)。"""
    keys = {}
    best_year = {}
    for row in conn.execute(
            "SELECT key, place_latest, track_type_latest, distance_latest, last_year FROM master"):
        base = _base_key(row["key"])
        year = row["last_year"] or 0
        if base not in keys or year >= best_year.get(base, -1):
            best_year[base] = year
            keys[base] = {"place": row["place_latest"], "track_type": row["track_type_latest"],
                          "distance": row["distance_latest"]}
    return keys


def _circ_month_dist(m1, m2):
    d = abs(m1 - m2) % 12
    return min(d, 12 - d)


def _resolve_final_key(conn, base_key, place=None, distance=None, target_month=None):
    """4文字の基底キーに対して、分割済みsub-keyが複数あるときは
    place/distanceが一致するものを、無ければ指定月に最も近いものを選ぶ
    (レビュー対応)。分割されていなければ base_key をそのまま返す。
    該当するmasterエントリが無ければ None。"""
    rows = conn.execute(
        "SELECT key, place_latest, distance_latest, month_latest FROM master "
        "WHERE key=? OR key LIKE ?", (base_key, base_key + "@%")).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]["key"]
    for row in rows:
        if place is not None and row["place_latest"] == place:
            return row["key"]
    for row in rows:
        if distance is not None and row["distance_latest"] == distance:
            return row["key"]
    month = target_month or datetime.now().month
    best = min(rows, key=lambda row: _circ_month_dist(row["month_latest"] or 1, month))
    return best["key"]


def _latest_month_day(conn, key, last_year):
    row = conn.execute(
        "SELECT date FROM races WHERE key=? AND year=? ORDER BY date DESC LIMIT 1",
        (key, last_year)).fetchone()
    if not row or not row["date"]:
        return None
    try:
        return int(row["date"][4:6]), int(row["date"][6:8])
    except (TypeError, ValueError, IndexError):
        return None


def _days_until_next_occurrence(month, day, today=None):
    """今日から見て、次に (month, day) が来るまでの日数 (今日なら0)。"""
    today = today or _date.today()
    for year_offset in (0, 1):
        try:
            occurrence = _date(today.year + year_offset, month, day)
        except ValueError:
            # 2/29 のような日付が存在しない年は 3/1 として扱う
            occurrence = _date(today.year + year_offset, month, day if day <= 28 else 28)
        delta = (occurrence - today).days
        if delta >= 0:
            return delta
    return 9999


@bp.route("/api/master")
def api_master():
    if not _cache_available():
        return _missing_cache_response()
    conn = _conn()
    try:
        built_at = conn.execute(
            "SELECT value FROM meta WHERE key='built_at'").fetchone()
        rows = conn.execute(
            "SELECT key, display_name, grade_latest, place_latest, track_type_latest, "
            "distance_latest, month_latest, first_year, last_year, n_years FROM master"
        ).fetchall()
        out = []
        for r in rows:
            month_day = _latest_month_day(conn, r["key"], r["last_year"])
            month, day = month_day if month_day else (r["month_latest"] or 1, 1)
            out.append({
                "key": r["key"], "display_name": r["display_name"],
                "grade_latest": r["grade_latest"], "place_latest": r["place_latest"],
                "track_type_latest": r["track_type_latest"],
                "distance_latest": r["distance_latest"], "month_latest": r["month_latest"],
                "n_years": r["n_years"], "last_year": r["last_year"],
                "_sort": _days_until_next_occurrence(month, day),
                # 直近3年に開催が無いキーは終了/改称済みとみなし、一覧の末尾に回す (Fable レビュー)
                "active": bool((r["last_year"] or 0) >= _CURRENT_YEAR - 2),
            })
        out.sort(key=lambda x: (0 if x["active"] else 1, x["_sort"], x["key"]))
        for item in out:
            item.pop("_sort", None)
        return jsonify({"races": out, "built_at": built_at["value"] if built_at else None})
    finally:
        conn.close()


def _extract_name_from_race_info(race_info):
    """'【新潟 8R】芝2000m 15:45発走　農林水産省賞典 新潟記念' から
    レース名部分 (全角空白の後) を取り出す。track_type/distanceも併せて返す。"""
    text = str(race_info or "")
    m = re.search(r"発走[\s　]*(.+)$", text)
    name = m.group(1).strip() if m else None
    tm = re.search(r"(芝|ダート)(\d+)m", text)
    track_type = tm.group(1) if tm else None
    distance = int(tm.group(2)) if tm else None
    return name, track_type, distance


def _date_from_day_label(day_label, today=None):
    """rec['day_label'] ('9/26(土)' 等) から日付 (ISO文字列) を推定する。
    解析できなければ today (省略時は本日) を返す (SPEC-T79c §2.2: EV分にも
    weekend分と同じ形式の date を持たせ、統合後の並び替えに使う)。"""
    today = today or datetime.now().date()
    m = re.match(r"(\d{1,2})/(\d{1,2})", str(day_label or ""))
    if not m:
        return today.isoformat()
    try:
        return _date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return today.isoformat()


@bp.route("/api/this_week")
def api_this_week():
    if not _cache_available():
        return _missing_cache_response()
    import jra_ev

    races = list(jra_ev.STATE.get("races", {}).values())
    day_label = next((r.get("day_label") for r in races if r.get("day_label")), "")

    conn = _conn()
    try:
        known_keys = _known_keys_dict(conn)
        sponsors = load_sponsors()
        aliases = load_aliases()
        master_by_key = {row["key"]: row for row in conn.execute(
            "SELECT key, display_name, grade_latest FROM master")}

        ev_out = []
        for rec in races:
            name, track_type, distance = _extract_name_from_race_info(rec.get("race_info"))
            if not name:
                continue
            base_key, _grade, how = match_key(
                name, known_keys, place=rec.get("venue"), track_type=track_type,
                distance=distance, sponsors=sponsors, aliases=aliases)
            if not base_key:
                continue
            # 4文字キーが分割されている場合、place/distanceが一致するsub-key、
            # 無ければ今日の月に最も近いsub-keyを選ぶ (レビュー対応)。
            key = _resolve_final_key(conn, base_key, place=rec.get("venue"),
                                     distance=distance)
            if not key:
                continue
            master_row = master_by_key.get(key)
            ev_out.append({
                "date": _date_from_day_label(rec.get("day_label")),
                "venue": rec.get("venue"), "race_num": rec.get("race_num"),
                "race_info": rec.get("race_info"), "start_time": rec.get("start_time"),
                "url": rec.get("url"),
                "key": key, "display_name": master_row["display_name"] if master_row else key,
                "grade_latest": master_row["grade_latest"] if master_row else None,
                "how": how,
                # EV監視で既に解析済み = 出馬表を取得できた = 枠順確定済み。
                "draw_fixed": True, "source": "ev_state",
            })

        # SPEC-T79c §2.2: this_week (EV分) と weekend_graded (JRA出馬表からの
        # 先行取得分、木・金の解析なし期間にも使える) を url で dedupe して結合する。
        # ここではネットワークアクセスはしない (取得済みキャッシュを読むだけ)。
        today_iso = datetime.now().date().isoformat()
        weekend_cards = _weekend_store().list_weekend_graded_cards(min_date=today_iso)
        merged = graded_weekend.merge_this_week(ev_out, weekend_cards)
        return jsonify({"races": merged, "day_label": day_label})
    finally:
        conn.close()


# ─── SPEC-T79c §2.1: 週末重賞の先行表示 ────────────────────────────────────

def _weekend_graded_fresh(store):
    """weekend_graded_cards の最新取得時刻が6時間以内かどうか。"""
    latest = store.weekend_graded_cards_fetched_at()
    if not latest:
        return False
    try:
        fetched = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(fetched.tzinfo) if fetched.tzinfo else datetime.now()
    return (now - fetched).total_seconds() < _WEEKEND_GRADED_TTL_SEC


def _collect_weekend_candidates(matrix, target_dates):
    """出馬表ページのnavから得た matrix (venue x day の全レースURL) から、
    今週末 (target_dates) の9〜12Rだけを候補として抜き出す
    (SPEC-T79c §2.1-a/b)。戻り値: [(cname_meta, url), ...] (url で重複無し)。"""
    seen = set()
    out = []
    for v in matrix or []:
        for r in v.get("races", []):
            url = r.get("url")
            if not url or url in seen:
                continue
            meta_cname = graded_weekend.parse_cname(url)
            if not meta_cname or meta_cname["date"] not in target_dates:
                continue
            if not (9 <= meta_cname["race_num"] <= 12):
                continue
            seen.add(url)
            out.append((meta_cname, url))
    return out


def _collect_thisweek_page_candidates(target_dates, seen_urls):
    """https://www.jra.go.jp/keiba/thisweek/ (今週の注目レース) のリンクを候補に
    追加する (SPEC-T79c §2.1-c: 注目レースに無いG3等の取りこぼし対策)。
    失敗しても空リストを返す (呼び出し側は警告ログのみ)。"""
    out = []
    try:
        res = requests.get("https://www.jra.go.jp/keiba/thisweek/", headers=_WEEKEND_HDRS,
                           timeout=_WEEKEND_GRADED_TIMEOUT_SEC)
        res.encoding = "shift_jis"
        soup = BeautifulSoup(res.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "accessD.html" not in href or "CNAME=" not in href:
                continue
            full = urljoin("https://www.jra.go.jp/", href)
            if full in seen_urls:
                continue
            meta_cname = graded_weekend.parse_cname(full)
            if not meta_cname or meta_cname["date"] not in target_dates:
                continue
            seen_urls.add(full)
            out.append((meta_cname, full))
    except Exception as e:
        print(f"[WARN] T79c 今週の注目レースページの取得に失敗: {e}")
    return out


def _fetch_and_store_weekend_graded(conn, *, force=False):
    """SPEC-T79c §2.1: 今週末 (次の土・日・月曜開催があればそれも) の重賞候補を
    JRAから取得し weekend_graded_cards に保存する。6時間以内に取得済みなら
    再取得しない (force=Trueで強制)。JRAへのアクセスは0.5秒間隔・9〜12Rのみ・
    最大36件 (3場x3日x4R) に制限する (SPEC §2.1-b)。

    戻り値: 新たに保存したカードのリスト (再取得しなかった場合・失敗時は空)。"""
    store = _weekend_store()
    if not force and _weekend_graded_fresh(store):
        return []
    import jra_ev

    try:
        entry = jra_ev.find_entry_url()
        if not entry:
            print("[WARN] T79c 週末重賞: 出馬表の起点URLが見つかりません")
            return []
        res = requests.get(entry, headers=_WEEKEND_HDRS, timeout=_WEEKEND_GRADED_TIMEOUT_SEC)
        res.encoding = "shift_jis"
        matrix = jra_ev.build_matrix_data(BeautifulSoup(res.text, "html.parser"))
    except Exception as e:
        print(f"[WARN] T79c 週末重賞: 出馬表ナビの取得に失敗: {e}")
        return []

    target_dates = set(graded_weekend.weekend_target_dates())
    candidates = _collect_weekend_candidates(matrix, target_dates)
    seen_urls = {url for _meta, url in candidates}
    candidates.extend(_collect_thisweek_page_candidates(target_dates, seen_urls))
    candidates = candidates[:_WEEKEND_GRADED_MAX_FETCHES]

    known_keys = _known_keys_dict(conn)
    sponsors = load_sponsors()
    aliases = load_aliases()
    master_by_key = {row["key"]: row for row in conn.execute(
        "SELECT key, display_name, grade_latest FROM master")}

    cards = []
    for i, (meta_cname, url) in enumerate(candidates):
        if i > 0:
            time.sleep(_WEEKEND_GRADED_SLEEP_SEC)
        try:
            r = requests.get(url, headers=_WEEKEND_HDRS, timeout=_WEEKEND_GRADED_TIMEOUT_SEC)
            r.encoding = "shift_jis"
        except Exception as e:
            print(f"[WARN] T79c 週末重賞: 出馬表取得失敗 {url}: {e}")
            continue
        meta = graded_weekend.extract_card_meta(r.text)
        base_key, _grade, _how = graded_weekend.classify_card(
            meta.get("race_name"), known_keys, place=meta_cname.get("venue"),
            track_type=meta.get("track_type"), distance=meta.get("distance"),
            sponsors=sponsors, aliases=aliases)
        if not base_key:
            continue
        key = _resolve_final_key(conn, base_key, place=meta_cname.get("venue"),
                                 distance=meta.get("distance"))
        if not key:
            continue
        master_row = master_by_key.get(key)
        cards.append({
            "date": meta_cname["date"], "venue": meta_cname.get("venue"),
            "race_num": meta_cname["race_num"], "race_name": meta.get("race_name"),
            "url": url, "key": key,
            "display_name": master_row["display_name"] if master_row else key,
            "grade_latest": master_row["grade_latest"] if master_row else None,
            "start_time": meta.get("start_time"), "draw_fixed": meta.get("draw_fixed"),
            "source": "jra_card",
        })

    if cards:
        store.save_weekend_graded_cards(cards)
    return cards


@bp.route("/api/weekend_graded")
def api_weekend_graded():
    if not _cache_available():
        return _missing_cache_response()
    refresh = request.args.get("refresh") == "1"
    conn = _conn()
    try:
        fetched = _fetch_and_store_weekend_graded(conn, force=refresh)
    finally:
        conn.close()
    store = _weekend_store()
    today_iso = datetime.now().date().isoformat()
    cards = store.list_weekend_graded_cards(min_date=today_iso)
    return jsonify({"races": cards, "fetched_now": len(fetched)})


def _weekend_graded_background_refresh(label):
    """起動時/解析完了時にバックグラウンドで週末重賞候補を1回更新する
    (SPEC-T79c §2.1-d)。失敗しても警告ログのみ (呼び出し元の処理は止めない)。"""
    def _run():
        try:
            if not _cache_available():
                return
            conn = _conn()
            try:
                _fetch_and_store_weekend_graded(conn, force=False)
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] T79c 週末重賞バックグラウンド更新失敗 ({label}): {e}")
    threading.Thread(target=_run, daemon=True, name=f"jra-graded-t79c-weekend-{label}").start()


def _course_history(race_rows_year_asc):
    history = []
    prev = None
    for r in race_rows_year_asc:
        cur = (r["place"], r["track_type"], r["distance"], r["grade"])
        if cur != prev:
            history.append({"year": r["year"], "place": r["place"],
                            "track_type": r["track_type"], "distance": r["distance"],
                            "grade": r["grade"]})
            prev = cur
    return history


def _bucket_stats(rows, buckets, value_fn):
    """rows (rank数値のrun行) を bucket 定義でグルーピングして n/c1/c2/c3/out/率/回収を計算する。
    bucketsは (label, predicate) のtuple列。predicateがNoneに対して呼ばれないよう、
    呼び出し側で value_fn がNoneを返す行は除外すること。"""
    grouped = defaultdict(list)
    for row in rows:
        v = value_fn(row)
        if v is None:
            continue
        for label, pred in buckets:
            if pred(v):
                grouped[label].append(row)
                break
    out = []
    for label, _pred in buckets:
        group = grouped.get(label, [])
        out.append(_group_summary(label, group))
    return out


def _group_summary(label, group):
    n = len(group)
    c1 = sum(1 for r in group if r["rank"] == 1)
    c2 = sum(1 for r in group if r["rank"] == 2)
    c3 = sum(1 for r in group if r["rank"] == 3)
    out_cnt = n - c1 - c2 - c3
    win_ret = sum((r["win_pay"] or 0) for r in group if r["rank"] == 1)
    fuku_ret = sum((r["fukusho_pay"] or 0) for r in group if r["rank"] is not None and r["rank"] <= 3)
    return {
        "label": label, "n": n, "c1": c1, "c2": c2, "c3": c3, "out": out_cnt,
        "win_rate": round(c1 / n, 3) if n else None,
        "ren_rate": round((c1 + c2) / n, 3) if n else None,
        "fuku_rate": round((c1 + c2 + c3) / n, 3) if n else None,
        "roi_win": round(win_ret / (n * 100), 3) if n else None,
        "roi_fuku": round(fuku_ret / (n * 100), 3) if n else None,
    }


_HIGHLIGHT_MIN_N = 8
_ROI_GOLD_THRESHOLD = 1.2


def classify_highlight(n, fuku_rate, win_rate, overall_fuku_rate, overall_win_rate):
    """SPEC-T79c §1.1: 傾向表の1行を、全体との比で「強/中/弱/なし」に分類する純関数。
    n>=8 が前提 (n<8は色を付けず「n少」表示にするのは呼び出し側=UIの責務)。

    - 複勝率比>=1.5 または 勝率比>=2.0 -> 'strong' (★・緑濃)
    - 複勝率比>=1.2 または 勝率比>=1.5 -> 'mid' (緑薄。strongの次に判定)
    - 複勝率比<=0.6 -> 'weak' (赤)
    - それ以外 (n<8、全体値が無い、比が中間帯) -> None
    """
    if not n or n < _HIGHLIGHT_MIN_N or not overall_fuku_rate or not overall_win_rate:
        return None
    fuku_ratio = (fuku_rate / overall_fuku_rate) if fuku_rate is not None else None
    win_ratio = (win_rate / overall_win_rate) if win_rate is not None else None
    if (fuku_ratio is not None and fuku_ratio >= 1.5) or (win_ratio is not None and win_ratio >= 2.0):
        return "strong"
    if (fuku_ratio is not None and fuku_ratio >= 1.2) or (win_ratio is not None and win_ratio >= 1.5):
        return "mid"
    if fuku_ratio is not None and fuku_ratio <= 0.6:
        return "weak"
    return None


def _annotate_aggregates(aggregates, overall_fuku_rate, overall_win_rate):
    """SPEC-T79c §1.1-2: aggregatesの各行に highlight (strong/mid/weak/None) と、
    単/複回収が120%以上のときの roi_win_gold/roi_fuku_gold (bool, n>=8のみ) を
    その場に追加する (副作用ありのヘルパー。テストはclassify_highlight側で行う)。"""
    for rows in aggregates.values():
        for row in rows:
            n = row.get("n") or 0
            row["highlight"] = classify_highlight(
                n, row.get("fuku_rate"), row.get("win_rate"), overall_fuku_rate, overall_win_rate)
            row["roi_win_gold"] = bool(n >= _HIGHLIGHT_MIN_N and row.get("roi_win") is not None
                                       and row["roi_win"] >= _ROI_GOLD_THRESHOLD)
            row["roi_fuku_gold"] = bool(n >= _HIGHLIGHT_MIN_N and row.get("roi_fuku") is not None
                                        and row["roi_fuku"] >= _ROI_GOLD_THRESHOLD)


def _group_by_key(rows, key_fn, min_n=1, top=None):
    grouped = defaultdict(list)
    for row in rows:
        k = key_fn(row)
        if k is None or k == "":
            continue
        grouped[k].append(row)
    summaries = [_group_summary(k, v) for k, v in grouped.items() if len(v) >= min_n]
    summaries.sort(key=lambda s: s["n"], reverse=True)
    return summaries[:top] if top else summaries


def _sex_age_label(sex, age):
    if not sex or age is None:
        return None
    if sex == "セ":
        return "セ"
    if age >= 6:
        return f"{sex}6+"
    return f"{sex}{age}"


def _runner_view(row):
    return {
        "umaban": row["umaban"], "waku": row["waku"], "horse": row["horse"],
        "sex": row["sex"], "age": row["age"], "kinryo": row["kinryo"],
        "jockey": row["jockey"], "rank": row["rank"], "popularity": row["popularity"],
        "kyaku": row["kyaku"], "agari": row["agari"], "sire": row["sire"],
        "weight": row["weight"], "time_sec": row["time_sec"],
        "prev": {"race_name": row["prev_race_name"], "rank": row["prev_rank"],
                 "popularity": row["prev_popularity"]},
    }


def _load_key_history(conn, key, years_param, same_course):
    """SPEC-T79b §2.3-3: /api/history の中核処理 (master取得〜aggregates計算)。
    /graded/api/pick からもHTTP経由せず直接呼べるよう切り出したもの。
    戻り値は /api/history のレスポンスと同じ形 + overall_fuku_rate。
    keyがmasterに無ければ None を返す (呼び出し側で404にする)。"""
    limit = None
    if str(years_param).lower() not in ("all", "0", ""):
        try:
            limit = int(years_param)
        except (TypeError, ValueError):
            limit = 10

    master_row = conn.execute("SELECT * FROM master WHERE key=?", (key,)).fetchone()
    if master_row is None:
        return None

    all_races = conn.execute(
        "SELECT * FROM races WHERE key=? ORDER BY year ASC", (key,)).fetchall()
    course_history = _course_history(all_races)

    races_desc = list(reversed(all_races))
    if same_course:
        races_desc = [r for r in races_desc if r["same_course_as_latest"] == 1]
    selected = races_desc[:limit] if limit else races_desc

    race_ids = [r["race_id"] for r in selected]
    runs_by_race = defaultdict(list)
    if race_ids:
        placeholders = ",".join("?" * len(race_ids))
        for row in conn.execute(
                f"SELECT * FROM runs WHERE race_id IN ({placeholders})", race_ids):
            runs_by_race[row["race_id"]].append(row)

    results = []
    for r in selected:
        runs = sorted(runs_by_race.get(r["race_id"], ()),
                      key=lambda x: (x["rank"] is None, x["rank"] if x["rank"] is not None else 0))
        top3 = [_runner_view(row) for row in runs if row["rank"] is not None and row["rank"] <= 3]
        runners = [_runner_view(row) for row in runs]
        results.append({
            "year": r["year"], "date": r["date"], "place": r["place"],
            "track_type": r["track_type"], "distance": r["distance"],
            "condition": r["condition"], "head_count": r["head_count"],
            "win_time": r["win_time_sec"], "winner_pop": r["winner_pop"],
            "pay": {"win": r["winner_pay"], "umaren": r["pay_umaren"],
                    "sanrenpuku": r["pay_sanrenpuku"], "sanrentan": r["pay_sanrentan"]},
            "top3": top3, "runners": runners,
        })

    all_runs = [row for race_id in race_ids for row in runs_by_race.get(race_id, ())]
    numeric_runs = [row for row in all_runs if row["rank"] is not None]

    aggregates = {
        "popularity": _bucket_stats(numeric_runs, _POP_BUCKETS, lambda r: r["popularity"]),
        "waku": [_group_summary(str(w), [r for r in numeric_runs if r["waku"] == w])
                for w in range(1, 9)],
        "kyaku": [_group_summary(label, [r for r in numeric_runs if r["kyaku"] == label])
                 for label in ("逃げ", "先行", "差し", "追込")],
        "sex_age": _group_by_key(numeric_runs,
                                 lambda r: _sex_age_label(r["sex"], r["age"]), min_n=1),
        "affi": [_group_summary(_AFFI_LABELS[code],
                                [r for r in numeric_runs if r["affi_norm"] == code])
                for code in ("美", "栗")
                if any(r["affi_norm"] == code for r in numeric_runs)],
        "sire": _group_by_key(numeric_runs, lambda r: r["sire"], min_n=3, top=8),
        "jockey": _group_by_key(numeric_runs, lambda r: r["jockey"], min_n=3, top=8),
        "prev_race": _group_by_key(
            numeric_runs,
            lambda r: normalize_race_name(r["prev_race_name"])[0] if r["prev_race_name"] else None,
            min_n=3, top=10),
        "prev_rank_band": _bucket_stats(
            [r for r in numeric_runs if r["prev_rank"] is not None], _PREV_RANK_BUCKETS,
            lambda r: r["prev_rank"]),
        "weight": _bucket_stats([r for r in numeric_runs if r["weight"] is not None],
                                _WEIGHT_BUCKETS, lambda r: r["weight"]),
        "interval": _bucket_stats(
            [r for r in numeric_runs if r["prev_interval_days"] is not None],
            _INTERVAL_BUCKETS, lambda r: r["prev_interval_days"]),
    }

    # SPEC-T79b §2.2: score_entry の lift 計算で使う「この履歴全体の複勝率」。
    # SPEC-T79c §1.1: 傾向表の色分けは、この overall_fuku_rate / overall_win_rate
    # (Σ1着/Σ出走) との比で行う。
    n_all = len(numeric_runs)
    c1_all = sum(1 for r in numeric_runs if r["rank"] == 1)
    c123_all = sum(1 for r in numeric_runs if r["rank"] is not None and r["rank"] <= 3)
    overall_fuku_rate = round(c123_all / n_all, 4) if n_all else None
    overall_win_rate = round(c1_all / n_all, 4) if n_all else None
    _annotate_aggregates(aggregates, overall_fuku_rate, overall_win_rate)

    fav = next((g for g in aggregates["popularity"] if g["label"] == "1番人気"), None)
    winner_pops = [r["winner_pop"] for r in selected if r["winner_pop"] is not None]
    winner_pays = [r["winner_pay"] for r in selected if r["winner_pay"] is not None]
    sanrentan_rows = [r for r in selected if r["year"] >= 2025 and r["pay_sanrentan"] is not None]
    max_win = None
    if winner_pays:
        best = max(selected, key=lambda r: r["winner_pay"] if r["winner_pay"] is not None else -1)
        if best["winner_pay"] is not None:
            winner_run = next((row for row in runs_by_race.get(best["race_id"], ())
                               if row["rank"] == 1), None)
            max_win = {"year": best["year"],
                      "horse": winner_run["horse"] if winner_run else None,
                      "pay": best["winner_pay"]}
    upset = {
        "fav_win_rate": fav["win_rate"] if fav else None,
        "fav_fuku_rate": fav["fuku_rate"] if fav else None,
        "avg_winner_pop": round(sum(winner_pops) / len(winner_pops), 2) if winner_pops else None,
        "avg_win_pay": round(sum(winner_pays) / len(winner_pays), 1) if winner_pays else None,
        "avg_sanrentan": {
            "value": round(sum(r["pay_sanrentan"] for r in sanrentan_rows) / len(sanrentan_rows), 1)
                    if sanrentan_rows else None,
            "n": len(sanrentan_rows),
        },
        "max_win_pay": max_win,
    }

    years_used = sorted({r["year"] for r in selected})
    return {
        "master": dict(master_row),
        "course_history": course_history,
        "results": results,
        "aggregates": aggregates,
        "upset": upset,
        "n_years_used": len(years_used),
        "years_range": f"{years_used[0]}〜{years_used[-1]}" if years_used else None,
        "same_course_filter": same_course,
        "overall_fuku_rate": overall_fuku_rate,
        "overall_win_rate": overall_win_rate,
    }


@bp.route("/api/history")
def api_history():
    if not _cache_available():
        return _missing_cache_response()
    key = request.args.get("key")
    if not key:
        return jsonify({"error": "key is required"}), 400

    years_param = request.args.get("years", "10")
    same_course = request.args.get("same_course") == "1"

    conn = _conn()
    try:
        payload = _load_key_history(conn, key, years_param, same_course)
        if payload is None:
            return jsonify({"error": f"unknown key: {key}"}), 404
        return jsonify(payload)
    finally:
        conn.close()


def _is_valid_race_url(url):
    """JRAの出馬表URL (accessD.html?CNAME=...) かどうかの簡易チェック
    (SPEC-T79b §2.3-1)。既存コード (jra_win5.py 等) と同じ判定基準。"""
    return bool(url) and "accessD.html" in url and "CNAME=" in url


@bp.route("/api/pick")
def api_pick():
    if not _cache_available():
        return _missing_cache_response()
    url = request.args.get("url")
    if not _is_valid_race_url(url):
        return jsonify({"error": "url is required (JRA accessD.html?CNAME=... 形式)"}), 400
    key = request.args.get("key")
    if not key:
        return jsonify({"error": "key is required"}), 400
    years_param = request.args.get("years", "10")
    same_course = request.args.get("same_course") == "1"

    import jra_ev

    cached = jra_ev.get_cached_analysis(url)
    if cached and cached.get("result"):
        result = cached["result"]
        source = "cache"
    else:
        result = _scrape_cache_get(url)
        if result is not None:
            source = "scrape_cache"
        else:
            from api.index import analyze_race_url
            source = "scrape"
            try:
                result = analyze_race_url(url, "簡易")
            except Exception as e:  # noqa: BLE001 - 外部サイト解析なので何が飛んでくるか分からない
                return jsonify({"error": f"出馬表の解析に失敗しました: {e}"}), 502
            _scrape_cache_put(url, result)

    horses = (result or {}).get("horses") or []
    if not horses:
        return jsonify({"error": "出走馬データを取得できませんでした"}), 502

    # SPEC-T79b §2.3-2: EV監視の同URLレースがあれば horses[].pop (実際の人気) を使う。
    ev_by_num = {}
    ev_rec = next((rec for rec in jra_ev.STATE.get("races", {}).values()
                  if rec.get("url") == url), None)
    if ev_rec:
        for h in ev_rec.get("horses") or []:
            if h.get("num") is not None:
                ev_by_num[h["num"]] = h

    conn = _conn()
    try:
        history = _load_key_history(conn, key, years_param, same_course)
        if history is None:
            return jsonify({"error": f"unknown key: {key}"}), 404
        known_keys = _known_keys_dict(conn)
    finally:
        conn.close()

    sponsors = load_sponsors()
    aliases = load_aliases()
    aggregates = history["aggregates"]
    overall_fuku_rate = history["overall_fuku_rate"]

    entries = []
    for h in horses:
        if h.get("scratched"):
            continue
        ev_h = ev_by_num.get(h.get("num"))
        attrs = graded_pick.derive_entry_attrs(
            h, ev_horse=ev_h, known_keys=known_keys, sponsors=sponsors, aliases=aliases)
        scored = graded_pick.score_entry(attrs, aggregates, overall_fuku_rate)

        pop_val = (ev_h or {}).get("pop")
        if pop_val is None:
            pop_val = h.get("pop")
        try:
            pop_val = int(float(pop_val)) if pop_val is not None else None
        except (TypeError, ValueError):
            pop_val = None

        entries.append({
            "num": h.get("num"), "name": h.get("name"), "pop": pop_val,
            "odds": h.get("odds"), "attrs": attrs,
            "score": scored["score"], "score_ex_pop": scored["score_ex_pop"],
            "hits": scored["hits"], "misses": scored["misses"], "detail": scored["detail"],
        })

    entries.sort(key=lambda e: -e["score_ex_pop"])

    def _pop_sort_key(pop):
        return pop if pop is not None else 9999

    picks = [e["num"] for e in
             sorted(entries, key=lambda e: (-e["score_ex_pop"], _pop_sort_key(e["pop"])))[:3]]

    return jsonify({
        "race": {"venue": result.get("venue"), "race_num": result.get("race_num"),
                "race_info": result.get("race_info"), "url": url, "source": source},
        "key": key, "display_name": history["master"].get("display_name") or key,
        "n_years_used": history["n_years_used"], "overall_fuku_rate": overall_fuku_rate,
        "entries": entries, "picks": picks,
    })
