"""SPEC-T79 §2.2: 重賞データページ用の表示専用キャッシュを作る。

ability.db (SQLite, read-only. `file:...?mode=ro` で開き、一切書き込まない) から
重賞 (race_class='重賞'、または race_name に (GI|GII|GIII) を含む2026netkeiba由来行)
を抽出し、data/graded_cache.sqlite (master/races/runs/meta) を作り直す。

障害重賞 (J・GI等) は除外する (api.graded_names.is_jump_race)。

【実行】
    python build_graded_cache.py [--years-from 1986] [--report] [--out PATH]

出力は一時ファイルに書いてから os.replace() で置き換える (アトミック)。
--report を付けると outputs/t79_master_report.json に、display_name が
TARGET略称のまま (2026netkeibaの正式名で上書きされていない) のキー一覧を書く
(手動で data/graded_aliases.json や表示名を補正する際の参考用)。
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from api.graded_names import (  # noqa: E402
    is_jump_race,
    load_aliases,
    load_sponsors,
    normalize_race_name,
    race_key,
    resolve_full_name,
)
from api.past_data_service import _kyaku_from_c4, calculate_waku  # noqa: E402

ABILITY_DB_PATH = os.path.join(BASE_DIR, "ability.db")
PEDIGREE_PATH = os.path.join(BASE_DIR, "pedigree_cache.json")
DEFAULT_CACHE_PATH = os.path.join(BASE_DIR, "data", "graded_cache.sqlite")
DEFAULT_REPORT_PATH = os.path.join(BASE_DIR, "outputs", "t79_master_report.json")
DEFAULT_MERGES_PATH = os.path.join(BASE_DIR, "data", "graded_merges.json")
DEFAULT_DISPLAY_NAMES_PATH = os.path.join(BASE_DIR, "data", "graded_display_names.json")

_PAYOUT_BET_TYPES = {"馬連": "pay_umaren", "三連複": "pay_sanrenpuku", "三連単": "pay_sanrentan"}

_SCHEMA = """
CREATE TABLE master (
  key TEXT PRIMARY KEY,
  display_name TEXT,
  grade_latest TEXT,
  place_latest TEXT,
  track_type_latest TEXT,
  distance_latest INTEGER,
  month_latest INTEGER,
  first_year INTEGER,
  last_year INTEGER,
  n_years INTEGER
);
CREATE TABLE races (
  race_id TEXT PRIMARY KEY,
  key TEXT,
  date TEXT,
  year INTEGER,
  grade TEXT,
  race_name_raw TEXT,
  place TEXT,
  track_type TEXT,
  distance INTEGER,
  condition TEXT,
  head_count INTEGER,
  win_time_sec REAL,
  winner_pop INTEGER,
  winner_pay REAL,
  pay_umaren REAL,
  pay_sanrenpuku REAL,
  pay_sanrentan REAL,
  same_course_as_latest INTEGER
);
CREATE INDEX idx_races_key ON races(key);
CREATE TABLE runs (
  race_id TEXT,
  umaban INTEGER,
  waku INTEGER,
  horse TEXT,
  sex TEXT,
  age INTEGER,
  jockey TEXT,
  kinryo REAL,
  weight INTEGER,
  affi_norm TEXT,
  popularity INTEGER,
  rank INTEGER,
  c4 INTEGER,
  kyaku TEXT,
  agari REAL,
  time_sec REAL,
  chakusa REAL,
  win_pay REAL,
  fukusho_pay REAL,
  sire TEXT,
  prev_date TEXT,
  prev_race_name TEXT,
  prev_place TEXT,
  prev_distance INTEGER,
  prev_rank INTEGER,
  prev_popularity INTEGER,
  prev_interval_days INTEGER
);
CREATE INDEX idx_runs_race ON runs(race_id);
CREATE INDEX idx_runs_horse ON runs(horse);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

_RUN_COLUMNS = (
    "date", "place", "r", "race_name", "race_class", "horse", "sex", "age",
    "jockey", "kinryo", "total_horses", "umaban", "popularity", "rank",
    "track_type", "distance", "condition", "time_sec", "chakusa", "c4",
    "agari", "pci", "weight", "affi", "win_pay", "fukusho_pay",
)


def _affi_norm(affi):
    a = str(affi or "")
    if "美" in a:
        return "美"
    if "栗" in a:
        return "栗"
    return ""


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _win_pay_if_winner(raw, rank):
    """win_payは実測により rank=1の行のみ実際の単勝払戻 (例 '380')、
    rank>=2の行は '(111.5)' のように当時の単勝オッズが括弧付きで入っている
    (払戻ではない)。rank=1以外はNoneにして誤ったROI計算を防ぐ。"""
    if rank != 1:
        return None
    return _to_float(raw)


def _load_ability_rows(conn, years_from):
    start_date = f"{years_from:04d}0101"
    cols = ", ".join(_RUN_COLUMNS)
    sql = (
        f"SELECT {cols} FROM runs "
        "WHERE date >= ? AND (race_class='重賞' OR race_name LIKE '%(GI)%' "
        "OR race_name LIKE '%(GII)%' OR race_name LIKE '%(GIII)%') "
        "ORDER BY date, place, r"
    )
    rows = []
    for rec in conn.execute(sql, (start_date,)):
        rows.append(dict(zip(_RUN_COLUMNS, rec)))
    return rows


def _load_payouts(conn, years_from):
    start_date = f"{years_from:04d}0101"
    out = {}
    sql = ("SELECT date, place, r, bet_type, pay FROM race_payouts "
           "WHERE date >= ? AND bet_type IN ('馬連','三連複','三連単')")
    for date, place, r, bet_type, pay in conn.execute(sql, (start_date,)):
        out[(date, place, r, bet_type)] = pay
    return out


def _load_pedigree():
    try:
        with open(PEDIGREE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _build_horse_history(conn, horses):
    """指定した馬名集合について、ability.db全体 (重賞に限らない) の
    (date, race_name, place, distance, rank, popularity) 履歴を日付昇順で返す。
    前走参照 (idx_horse_date利用) 用。"""
    history = {}
    for horse in horses:
        rows = conn.execute(
            "SELECT date, race_name, place, distance, rank, popularity FROM runs "
            "WHERE horse=? ORDER BY date", (horse,)
        ).fetchall()
        history[horse] = rows
    return history


def _prev_run(history_rows, current_date):
    """history_rows (date昇順) から current_date より前の直近1件を返す (無ければNone)。"""
    prev = None
    for row in history_rows:
        if row[0] is None or row[0] >= current_date:
            break
        prev = row
    return prev


def _date_diff_days(d1, d2):
    try:
        dt1 = datetime.strptime(d1, "%Y%m%d")
        dt2 = datetime.strptime(d2, "%Y%m%d")
        return (dt1 - dt2).days
    except (TypeError, ValueError):
        return None


def _load_json_dict(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_merges(path=None):
    """data/graded_merges.json ({旧キー(sub-key含む): 統合先キー}) を読む。"""
    return _load_json_dict(path or DEFAULT_MERGES_PATH)


def load_display_names(path=None):
    """data/graded_display_names.json ({最終キー: 表示名}) を読む。"""
    return _load_json_dict(path or DEFAULT_DISPLAY_NAMES_PATH)


def _resolve_merge_target(merges, key):
    """merges を辿って最終的な統合先キーを返す (循環があれば最初に戻ってきた時点で止める)。"""
    seen = set()
    cur = key
    while cur in merges and merges[cur] != cur and merges[cur] not in seen:
        seen.add(cur)
        cur = merges[cur]
    return cur


def _pseudo_doy(date_str):
    """'YYYYMMDD' -> 月*31+日 の疑似通日 (暦の厳密さは不要。クラスタリングの
    距離計算用の相対値として使う)。"""
    try:
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        return month * 31 + day
    except (TypeError, ValueError, IndexError):
        return None


_PSEUDO_DOY_PERIOD = 12 * 31 + 31


def _circ_dist(a, b, period=_PSEUDO_DOY_PERIOD):
    d = abs(a - b) % period
    return min(d, period - d)


def _circ_month_dist(m1, m2):
    d = abs(m1 - m2) % 12
    return min(d, 12 - d)


def _try_distance_slots(groups, seed_years, max_k):
    """開催距離 (distance) が全seed年でmax_k種類にきれいに分かれ、
    slot間で値域が重ならないなら {race_id: slot} を返す。分けられなければNone。
    (例: スプリンターズS 1200m と スプリングS 1800m は距離が毎年安定して
    異なるため、月が近い/入れ替わる年があっても正しく分離できる。)"""
    if max_k < 2:
        return None
    for ids in seed_years.values():
        dists = [groups[i]["distance"] for i in ids]
        if any(d is None for d in dists) or len(set(dists)) != max_k:
            return None
    slot_vals = defaultdict(set)
    for ids in seed_years.values():
        for slot, rid in enumerate(sorted(ids, key=lambda i: groups[i]["distance"])):
            slot_vals[slot].add(groups[rid]["distance"])
    ranges = sorted((min(v), max(v)) for v in slot_vals.values())
    for i in range(len(ranges) - 1):
        if ranges[i][1] >= ranges[i + 1][0]:
            return None  # レンジが重複 → distanceでは分けられない
    assign = {}
    for ids in seed_years.values():
        for slot, rid in enumerate(sorted(ids, key=lambda i: groups[i]["distance"])):
            assign[rid] = slot
    return assign


def _split_colliding_base_keys(groups, order):
    """SPEC-T79レビュー対応 (2026-09-19): 4文字キーごとに開催 (year, month) を集め、
    同一年に2開催以上あるキーだけ、開催時期でクラスタリングしてサブキーに分割する。

    分割手順:
      1. 開催数が最大 (max_k) の年を「種」として、まず distance (開催距離) で
         max_k個にきれいに分かれるか試す (安定した識別子。例: スプリンターズS
         1200m と スプリングS 1800m は月が年によって入れ替わっても距離は不変)。
      2. distanceで分けられない場合 (例: 3歳牝馬特別のように同distanceで
         場所だけが違う) は、年内の開催順 (日付の早い方から slot0,1,...) で分ける。
      3. 開催数が max_k に満たない年(単独開催や欠番年)のレースは、種から作った
         スロット中心 (distanceまたは疑似通日の中央値) に最も近いスロットへ割り付ける。
      4. スロットの代表月 (最頻月) を `<4文字>@<月>` として新キーにする。
         代表月が他スロットと衝突する場合 (例: 3歳牝馬特別のように両スロットとも
         12月) は `@12`, `@12a` のように a,b,... を付けて一意にする
         (仕様の `<4文字>@<月>` 形式を、同月衝突時のみ拡張したもの)。

    同一年に2開催以上あるキーが無ければ何もしない (従来どおりサフィックス無し)。
    """
    by_base_key = defaultdict(list)
    for race_id in order:
        by_base_key[groups[race_id]["key"]].append(race_id)

    for base_key, race_ids in by_base_key.items():
        by_year = defaultdict(list)
        for rid in race_ids:
            by_year[groups[rid]["date"][:4]].append(rid)
        if all(len(ids) < 2 for ids in by_year.values()):
            continue  # 同一年2開催が無い (月が近いだけの年ずれ) → 分割不要

        max_k = max(len(ids) for ids in by_year.values())
        seed_years = {y: ids for y, ids in by_year.items() if len(ids) == max_k}

        assign = _try_distance_slots(groups, seed_years, max_k)
        use_distance = assign is not None
        if not use_distance:
            assign = {}
            for ids in seed_years.values():
                for slot, rid in enumerate(sorted(ids, key=lambda i: groups[i]["date"])):
                    assign[rid] = slot

        slot_refs = defaultdict(list)
        for rid, slot in assign.items():
            ref = groups[rid]["distance"] if use_distance else _pseudo_doy(groups[rid]["date"])
            slot_refs[slot].append(ref)
        centers = {slot: sorted(v)[len(v) // 2] for slot, v in slot_refs.items() if v}

        def _dist_to_center(rid, slot):
            if use_distance:
                return abs((groups[rid]["distance"] or 0) - centers[slot])
            return _circ_dist(_pseudo_doy(groups[rid]["date"]), centers[slot])

        for rid in race_ids:
            if rid in assign:
                continue
            best_slot = min(centers, key=lambda s: _dist_to_center(rid, s))
            assign[rid] = best_slot

        slot_months = defaultdict(list)
        for rid, slot in assign.items():
            slot_months[slot].append(int(groups[rid]["date"][4:6]))
        rep_month = {slot: Counter(months).most_common(1)[0][0]
                    for slot, months in slot_months.items()}

        used = {}
        label = {}
        for slot in sorted(rep_month, key=lambda s: (rep_month[s], s)):
            m = rep_month[slot]
            n = used.get(m, 0)
            used[m] = n + 1
            label[slot] = str(m) if n == 0 else f"{m}{chr(ord('a') + n - 1)}"

        for rid, slot in assign.items():
            groups[rid]["key"] = f"{base_key}@{label[slot]}"


def build_cache(years_from=1986, ability_db_path=None, out_path=None,
                pedigree_path=None, report_path=None, write_report=False,
                merges_path=None, display_names_path=None):
    ability_db_path = ability_db_path or ABILITY_DB_PATH
    out_path = out_path or DEFAULT_CACHE_PATH
    report_path = report_path or DEFAULT_REPORT_PATH
    merges = load_merges(merges_path)
    display_names = load_display_names(display_names_path)

    t0 = time.time()
    conn = sqlite3.connect(f"file:{ability_db_path}?mode=ro", uri=True)
    try:
        raw_rows = _load_ability_rows(conn, years_from)
        payouts = _load_payouts(conn, years_from)

        # ── レース単位にグルーピング (jump除外) ──────────────────────────
        groups = {}
        order = []
        for row in raw_rows:
            race_name = row["race_name"]
            track_type = row["track_type"]
            if is_jump_race(race_name, track_type):
                continue
            race_id = f'{row["date"]}{row["place"]}{row["r"]}'
            if race_id not in groups:
                groups[race_id] = {
                    "date": row["date"], "place": row["place"], "r": row["r"],
                    "race_name": race_name, "race_class": row["race_class"],
                    "track_type": track_type, "distance": row["distance"],
                    "condition": row["condition"], "total_horses": row["total_horses"],
                    "rows": [],
                }
                order.append(race_id)
            groups[race_id]["rows"].append(row)

        # ── known_keys構築: TARGET由来 (race_class='重賞') のみで先に作る ──
        known_keys = {}  # key -> {"place":, "track_type":, "distance":, "date":}
        for race_id in order:
            g = groups[race_id]
            if g["race_class"] != "重賞":
                continue
            base, _grade = normalize_race_name(g["race_name"])
            key = race_key(base)
            cond = known_keys.get(key)
            if cond is None or g["date"] > cond["date"]:
                known_keys[key] = {"place": g["place"], "track_type": g["track_type"],
                                    "distance": g["distance"], "date": g["date"]}

        sponsors = load_sponsors()
        aliases = load_aliases()

        # ── 各レースにkeyを確定する ────────────────────────────────────
        new_key_seq = 0
        for race_id in order:
            g = groups[race_id]
            if g["race_class"] == "重賞":
                base, grade = normalize_race_name(g["race_name"])
                g["key"] = race_key(base)
                g["grade"] = grade
                g["display_candidate"] = base
                g["origin"] = "target"
                continue
            full_text, grade, how = resolve_full_name(
                g["race_name"], known_keys, place=g["place"],
                track_type=g["track_type"], distance=g["distance"],
                sponsors=sponsors, aliases=aliases)
            if full_text is None:
                # 既知重賞に突合できない新規レース: 自分自身のkeyを新規登録する
                base, grade2 = normalize_race_name(g["race_name"])
                key = race_key(base)
                grade = grade or grade2
                display_candidate = base
                new_key_seq += 1
            else:
                key = full_text if how == "alias" else race_key(full_text)
                display_candidate = full_text
            if key not in known_keys:
                known_keys[key] = {"place": g["place"], "track_type": g["track_type"],
                                    "distance": g["distance"], "date": g["date"]}
            g["key"] = key
            g["grade"] = grade
            g["display_candidate"] = display_candidate
            g["origin"] = "netkeiba"

        # ── 4文字キーの衝突分割 (レビュー対応): 同一年に2開催以上ある
        # キーだけ、開催時期でクラスタリングして <4文字>@<月> に分割する
        # (例 'スプリン' → 'スプリン@3'(スプリングS) / 'スプリン@9'(スプリンターズS))。
        _split_colliding_base_keys(groups, order)

        # ── 統合 (data/graded_merges.json): キー確定・分割後、master集計前に
        # 旧キー (sub-key含む) を統合先キーへ付け替える。race_name_raw は
        # 元のまま保持するため、course_history には旧名の履歴が残る。
        if merges:
            for race_id in order:
                g = groups[race_id]
                g["key"] = _resolve_merge_target(merges, g["key"])

        # ── master集計 (key -> 年別条件・最新年の表示候補) ────────────────
        master_years = defaultdict(list)  # key -> [(year, month, place, track, distance, grade)]
        master_display = {}  # key -> (year, display_candidate, origin)
        for race_id in order:
            g = groups[race_id]
            key = g["key"]
            year = int(g["date"][:4])
            month = int(g["date"][4:6])
            master_years[key].append((year, month, g["place"], g["track_type"],
                                      g["distance"], g["grade"]))
            cur_best = master_display.get(key)
            if cur_best is None or year > cur_best[0]:
                master_display[key] = (year, g["display_candidate"], g["origin"])

        master_rows = []
        latest_course = {}  # key -> (place, track_type, distance)
        for key, entries in master_years.items():
            entries.sort(key=lambda e: (e[0], e[1]))
            first_year = entries[0][0]
            last_year = entries[-1][0]
            n_years = len({e[0] for e in entries})
            latest = entries[-1]
            _, display_candidate, origin = master_display[key]
            latest_course[key] = (latest[2], latest[3], latest[4])
            master_rows.append({
                "key": key,
                "display_name": display_names.get(key, display_candidate),
                "grade_latest": latest[5], "place_latest": latest[2],
                "track_type_latest": latest[3], "distance_latest": latest[4],
                "month_latest": latest[1], "first_year": first_year,
                "last_year": last_year, "n_years": n_years,
                "origin_latest": origin,
            })

        # ── 前走参照用の馬別履歴 (ability.db全体、重賞に限らない) ──────────
        horses = {row["horse"] for row in raw_rows if row.get("horse")}
        history = _build_horse_history(conn, horses)
        pedigree = _load_pedigree()

        race_rows_out = []
        run_rows_out = []
        for race_id in order:
            g = groups[race_id]
            key = g["key"]
            year = int(g["date"][:4])
            rows = g["rows"]
            rank1 = next((r for r in rows if _to_int(r["rank"]) == 1), None)
            win_time_sec = _to_float(rank1["time_sec"]) if rank1 else None
            winner_pop = _to_int(rank1["popularity"]) if rank1 else None
            winner_pay = _win_pay_if_winner(rank1["win_pay"], 1) if rank1 else None
            pay_umaren = payouts.get((g["date"], g["place"], g["r"], "馬連"))
            pay_sanrenpuku = payouts.get((g["date"], g["place"], g["r"], "三連複"))
            pay_sanrentan = payouts.get((g["date"], g["place"], g["r"], "三連単"))
            latest = latest_course.get(key)
            same_course = 1 if latest and (g["place"], g["track_type"], g["distance"]) == latest else 0

            race_rows_out.append((
                race_id, key, g["date"], year, g["grade"], g["race_name"], g["place"],
                g["track_type"], g["distance"], g["condition"], _to_int(g["total_horses"]),
                win_time_sec, winner_pop, winner_pay, pay_umaren, pay_sanrenpuku,
                pay_sanrentan, same_course,
            ))

            head_count = _to_int(g["total_horses"])
            for row in rows:
                rank = _to_int(row["rank"])
                umaban = _to_int(row["umaban"])
                waku = calculate_waku(umaban, head_count)
                kyaku = _kyaku_from_c4(row["c4"])
                horse = row["horse"]
                sire = (pedigree.get(horse) or {}).get("sire")
                prev = _prev_run(history.get(horse, ()), g["date"])
                if prev:
                    prev_date, prev_race_name, prev_place, prev_distance, prev_rank, prev_pop = prev
                    prev_interval = _date_diff_days(g["date"], prev_date)
                else:
                    prev_date = prev_race_name = prev_place = None
                    prev_distance = prev_rank = prev_pop = None
                    prev_interval = None

                run_rows_out.append((
                    race_id, umaban, waku, horse, row["sex"], _to_int(row["age"]),
                    row["jockey"], _to_float(row["kinryo"]), _to_int(row["weight"]),
                    _affi_norm(row["affi"]), _to_int(row["popularity"]), rank,
                    _to_int(row["c4"]), kyaku, _to_float(row["agari"]),
                    _to_float(row["time_sec"]), _to_float(row["chakusa"]),
                    _win_pay_if_winner(row["win_pay"], rank), _to_float(row["fukusho_pay"]),
                    sire, prev_date, prev_race_name, prev_place, _to_int(prev_distance),
                    _to_int(prev_rank), _to_int(prev_pop), prev_interval,
                ))

    finally:
        conn.close()

    # ── SQLiteへ書き出し (一時ファイル→アトミックrename) ─────────────────
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="graded_cache_", suffix=".sqlite",
                                    dir=os.path.dirname(out_path))
    os.close(fd)
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    try:
        out_conn = sqlite3.connect(tmp_path)
        try:
            out_conn.executescript(_SCHEMA)
            out_conn.executemany(
                "INSERT INTO master(key, display_name, grade_latest, place_latest, "
                "track_type_latest, distance_latest, month_latest, first_year, "
                "last_year, n_years) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(m["key"], m["display_name"], m["grade_latest"], m["place_latest"],
                  m["track_type_latest"], m["distance_latest"], m["month_latest"],
                  m["first_year"], m["last_year"], m["n_years"]) for m in master_rows])
            out_conn.executemany(
                "INSERT INTO races(race_id, key, date, year, grade, race_name_raw, "
                "place, track_type, distance, condition, head_count, win_time_sec, "
                "winner_pop, winner_pay, pay_umaren, pay_sanrenpuku, pay_sanrentan, "
                "same_course_as_latest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                race_rows_out)
            out_conn.executemany(
                "INSERT INTO runs(race_id, umaban, waku, horse, sex, age, jockey, "
                "kinryo, weight, affi_norm, popularity, rank, c4, kyaku, agari, "
                "time_sec, chakusa, win_pay, fukusho_pay, sire, prev_date, "
                "prev_race_name, prev_place, prev_distance, prev_rank, "
                "prev_popularity, prev_interval_days) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                run_rows_out)

            elapsed = time.time() - t0
            ability_stat = os.stat(ability_db_path) if os.path.exists(ability_db_path) else None
            meta = {
                "built_at": datetime.now().isoformat(timespec="seconds"),
                "years_from": str(years_from),
                "ability_db_size": str(ability_stat.st_size) if ability_stat else "",
                "ability_db_mtime": str(int(ability_stat.st_mtime)) if ability_stat else "",
                "n_races": str(len(race_rows_out)),
                "n_runs": str(len(run_rows_out)),
                "n_master": str(len(master_rows)),
                "build_seconds": f"{elapsed:.1f}",
                # T80 (waku割当バグ修正) がマージされたら build_graded_cache.py を
                # 再実行するだけで反映される。この値を見れば、キャッシュがどちらの
                # calculate_waku実装で作られたかを確認できる (レビュー対応)。
                "waku_impl": str(calculate_waku(5, 16)),
            }
            out_conn.executemany("INSERT INTO meta(key, value) VALUES (?,?)",
                                 list(meta.items()))
            out_conn.commit()
        finally:
            out_conn.close()
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_master": len(master_rows),
        "n_target_origin": sum(1 for m in master_rows if m["origin_latest"] == "target"),
        "target_origin_keys": sorted(
            [{"key": m["key"], "display_name": m["display_name"],
              "last_year": m["last_year"], "n_years": m["n_years"]}
             for m in master_rows if m["origin_latest"] == "target"],
            key=lambda x: x["key"]),
        # レビュー対応: data/graded_display_names.json / graded_merges.json を
        # 人手で編集する際の参照用に、最終キー (分割・統合後) の全件を載せる。
        "all_keys": sorted(
            [{"key": m["key"], "display_name": m["display_name"],
              "grade_latest": m["grade_latest"], "place_latest": m["place_latest"],
              "track_type_latest": m["track_type_latest"],
              "distance_latest": m["distance_latest"], "month_latest": m["month_latest"],
              "first_year": m["first_year"], "last_year": m["last_year"],
              "n_years": m["n_years"]}
             for m in master_rows],
            key=lambda x: x["key"]),
    }
    if write_report:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    return {
        "elapsed_seconds": time.time() - t0,
        "n_master": len(master_rows),
        "n_races": len(race_rows_out),
        "n_runs": len(run_rows_out),
        "n_target_origin": report["n_target_origin"],
        "out_path": out_path,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years-from", type=int, default=1986)
    parser.add_argument("--report", action="store_true",
                        help="outputs/t79_master_report.json を書き出す")
    parser.add_argument("--out", default=None, help="出力sqliteパス (既定 data/graded_cache.sqlite)")
    parser.add_argument("--ability-db", default=None, help="ability.dbのパス (テスト用)")
    args = parser.parse_args()

    result = build_cache(years_from=args.years_from, ability_db_path=args.ability_db,
                         out_path=args.out, write_report=args.report)
    print(f"[build_graded_cache] {result['elapsed_seconds']:.1f}s で完了: "
          f"master={result['n_master']} races={result['n_races']} runs={result['n_runs']} "
          f"-> {result['out_path']}")
    print(f"  display_nameがTARGET略称のまま: {result['n_target_origin']}件"
          + (" (outputs/t79_master_report.json に一覧を出力)" if args.report else ""))


if __name__ == "__main__":
    main()
