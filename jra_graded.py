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
from collections import defaultdict
from datetime import date as _date
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory

from api.graded_names import load_aliases, load_sponsors, match_key, normalize_race_name

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "graded_cache.sqlite")

_CURRENT_YEAR = datetime.now().year

bp = Blueprint("graded", __name__)

_MISSING_CACHE_MSG = "graded_cache.sqlite がありません。build_graded_cache.py を実行してください"

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


@bp.route("/api/history")
def api_history():
    if not _cache_available():
        return _missing_cache_response()
    key = request.args.get("key")
    if not key:
        return jsonify({"error": "key is required"}), 400

    years_param = request.args.get("years", "10")
    same_course = request.args.get("same_course") == "1"
    limit = None
    if str(years_param).lower() not in ("all", "0", ""):
        try:
            limit = int(years_param)
        except (TypeError, ValueError):
            limit = 10

    conn = _conn()
    try:
        master_row = conn.execute("SELECT * FROM master WHERE key=?", (key,)).fetchone()
        if master_row is None:
            return jsonify({"error": f"unknown key: {key}"}), 404

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
        payload = {
            "master": dict(master_row),
            "course_history": course_history,
            "results": results,
            "aggregates": aggregates,
            "upset": upset,
            "n_years_used": len(years_used),
            "years_range": f"{years_used[0]}〜{years_used[-1]}" if years_used else None,
            "same_course_filter": same_course,
        }
        return jsonify(payload)
    finally:
        conn.close()
