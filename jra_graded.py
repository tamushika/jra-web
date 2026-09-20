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

from flask import Blueprint, jsonify, request, send_from_directory

from api import graded_pick
from api.graded_names import load_aliases, load_sponsors, match_key, normalize_race_name

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "graded_cache.sqlite")

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


@bp.route("/api/this_week")
def api_this_week():
    if not _cache_available():
        return _missing_cache_response()
    import jra_ev

    races = list(jra_ev.STATE.get("races", {}).values())
    day_label = next((r.get("day_label") for r in races if r.get("day_label")), "")
    if not races:
        return jsonify({"races": [], "day_label": day_label})

    conn = _conn()
    try:
        known_keys = _known_keys_dict(conn)
        sponsors = load_sponsors()
        aliases = load_aliases()
        master_by_key = {row["key"]: row for row in conn.execute(
            "SELECT key, display_name, grade_latest FROM master")}

        out = []
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
            out.append({
                "venue": rec.get("venue"), "race_num": rec.get("race_num"),
                "race_info": rec.get("race_info"), "start_time": rec.get("start_time"),
                "url": rec.get("url"),
                "key": key, "display_name": master_row["display_name"] if master_row else key,
                "grade_latest": master_row["grade_latest"] if master_row else None,
                "how": how,
            })
        return jsonify({"races": out, "day_label": day_label})
    finally:
        conn.close()


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
    n_all = len(numeric_runs)
    c123_all = sum(1 for r in numeric_runs if r["rank"] is not None and r["rank"] <= 3)
    overall_fuku_rate = round(c123_all / n_all, 4) if n_all else None

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
