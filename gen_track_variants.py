"""
馬場差 (デイリー・トラックバリアント) の生成
==============================================
「その日・その場・その馬場(芝/ダ)の勝ちタイムが、クラス×距離×馬場状態別の
基準タイムから平均何秒ズレていたか」を日別に集計する。
正 = その日は時計がかかる(遅い)馬場 / 負 = 高速馬場。

データソース:
  - ability.db (1980.csv由来): 2016年〜2025年
  - Neon (DATABASE_URL, races): 2026年〜 (ライブの直近過去走をカバー)

出力: api/data_files/common/track_variants.json
【使い方】
  python gen_track_variants.py            # フル生成 (ability.db + Neon)
  python gen_track_variants.py --update   # 差分更新 (Neonのみ・直近120日を再計算し既存JSONへマージ)
                                          # ability.db が無い環境 (GitHub Actions) 用
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from build_ability_db import parse_race_class  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
OUT_PATH = os.path.join(API_DIR, "data_files", "common", "track_variants.json")

MIN_RACES_PER_DAY = 3
CLAMP = 5.0  # 秒


def std_exact(place, track, dist, rclass, cond):
    """基準タイム (馬場状態の完全一致のみ。フォールバックすると馬場差と二重計上になる)"""
    node = (scoring._load_standard_times()
            .get(place, {}).get(track, {}).get(str(dist), {}).get(rclass, {}))
    return node.get(cond)


def collect_from_ability_db(acc):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""SELECT date, place, track_type, distance, condition, race_class, time_sec
                   FROM runs WHERE rank = 1 AND date >= '20160101' AND time_sec IS NOT NULL""")
    n = 0
    for date, place, track, dist, cond, rclass, tsec in cur.fetchall():
        if not cond:
            continue
        std = std_exact(place, track, dist, rclass, cond)
        if std is None:
            continue
        acc[(date, place, track)].append(tsec - std)
        n += 1
    conn.close()
    return n


def collect_from_neon(acc, date_from_yymmdd="260101"):
    """Neonから date_from 以降の勝ちタイムを追加"""
    try:
        import past_data_service as pds
        conn = pds.get_db_connection(API_DIR)
        if conn is None or not getattr(conn, "is_pg", False):
            print("  [SKIP] Neon未接続 (DATABASE_URL なし)")
            return 0
        cur = conn.cursor()
        cur.execute("""SELECT date, place, track_type, distance, condition, race_name, time
                       FROM races WHERE rank = 1 AND date >= %s""", (date_from_yymmdd,))
        n = 0
        for row in cur.fetchall():
            d = dict(row)
            if "障" in str(d.get("race_name", "")):
                continue
            tsec = scoring.parse_run_time(d.get("time", ""))
            cond = str(d.get("condition", ""))[:1]
            if tsec is None or not cond:
                continue
            track = "ダート" if d["track_type"] in ("ダ", "ダート") else "芝"
            try:
                dist = int(d["distance"])
            except (TypeError, ValueError):
                continue
            rclass = parse_race_class(d.get("race_name", ""))
            std = std_exact(d["place"], track, dist, rclass, cond)
            if std is None:
                continue
            date8 = "20" + str(d["date"])  # '260606' → '20260606'
            acc[(date8, d["place"], track)].append(tsec - std)
            n += 1
        conn.close()
        return n
    except Exception as e:
        print(f"  [WARN] Neon取得失敗: {e}")
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="差分更新: Neonのみで直近120日を再計算し既存JSONへマージ (ability.db不要)")
    args = ap.parse_args()

    acc = defaultdict(list)
    variants = {}

    if args.update:
        # 既存JSONを土台に、直近120日だけNeonで再計算して差し替え
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                variants = json.load(f).get("variants", {})
            print(f"既存JSON: {len(variants)}日分をロード")
        except Exception:
            print("[WARN] 既存JSONなし — Neon分のみで生成します")
        cutoff = datetime.now() - timedelta(days=120)
        date_from = cutoff.strftime("%y%m%d")
        n2 = collect_from_neon(acc, date_from)
        print(f"Neon ({cutoff.strftime('%Y-%m-%d')}以降): {n2}勝ちタイム")
        if n2 == 0:
            print("[ERROR] Neonからデータを取得できませんでした (DATABASE_URL を確認)")
            sys.exit(1)
        # 再計算対象日の旧値を除去 (Neon側の再計算で置き換え)
        cutoff8 = cutoff.strftime("%Y%m%d")
        for d in [d for d in variants if d >= cutoff8]:
            del variants[d]
    else:
        n1 = collect_from_ability_db(acc)
        print(f"ability.db (2016-2025): {n1}勝ちタイム")
        n2 = collect_from_neon(acc)
        print(f"Neon (2026-): {n2}勝ちタイム")

    n_new = 0
    for (date, place, track), devs in acc.items():
        if len(devs) < MIN_RACES_PER_DAY:
            continue
        v = max(-CLAMP, min(CLAMP, median(devs)))
        variants.setdefault(date, {}).setdefault(place, {})[track] = round(v, 2)
        n_new += 1

    n_days = sum(len(tracks) for places in variants.values() for tracks in places.values())
    out = {
        "meta": {"basis": "日別・場別・芝ダ別の勝ちタイム中央値偏差 (基準タイム=馬場状態完全一致のみ)",
                 "min_races": MIN_RACES_PER_DAY, "clamp_sec": CLAMP, "cells": n_days,
                 "updated_at": datetime.now().strftime("%Y-%m-%d")},
        "variants": variants,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"馬場差: 計{n_days}セル ({len(variants)}日分, 今回更新{n_new}セル) -> {OUT_PATH} ({size_kb:.0f}KB)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
