"""
能力評価用DB + 基準タイムの構築
================================
C:/project/1980.csv (TARGET系エクスポート, cp932, 1986-2025 約184万行) から

  1) ability.db (SQLite)  — 馬名つき全出走テーブル runs (ローカル専用・バックテスト用)
  2) api/data_files/common/venue_standard_times.json
     — 場×芝ダ×距離×クラス×馬場状態 別の基準タイム (勝ち馬タイムの中央値)
       生成期間はデフォルト 2016-2023 (2024-25バックテストへのリーク防止)

【使い方】
  python build_ability_db.py                 # 両方生成
  python build_ability_db.py --std-from 16 --std-to 23
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from statistics import median

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, "1980.csv")
DB_PATH = os.path.join(BASE_DIR, "ability.db")
STD_PATH = os.path.join(BASE_DIR, "api", "data_files", "common", "venue_standard_times.json")

PLACE_MAP = {
    "札": "札幌", "函": "函館", "福": "福島", "新": "新潟", "東": "東京",
    "中": "中山", "名": "中京", "京": "京都", "阪": "阪神", "小": "小倉",
}
ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_place(kaisai):
    """'5中8' → 中山 (中京は「名」)"""
    for k, v in PLACE_MAP.items():
        if k in str(kaisai):
            return v
    return None


def parse_date(yymmdd):
    """'861221' → '19861221' / '251228' → '20251228' (時系列ソート可能に)"""
    s = str(yymmdd).strip()
    if len(s) != 6 or not s.isdigit():
        return None
    yy = int(s[:2])
    century = "19" if yy >= 80 else "20"
    return century + s


def parse_time_sec(t):
    """'1336' → 93.6 / '594' → 59.4 / 空・非数値 → None"""
    s = str(t).strip()
    if not s.isdigit() or len(s) < 3:
        return None
    tenth = int(s[-1])
    sec = int(s[-3:-1])
    minute = int(s[:-3]) if len(s) > 3 else 0
    if sec >= 60:
        return None
    return minute * 60 + sec + tenth / 10.0


def parse_rank(r):
    s = str(r).translate(ZEN2HAN)
    m = re.search(r"\d+", s)
    if not m:
        return None
    if any(x in s for x in ("取", "除", "中", "失")):  # 取消/除外/中止/失格
        return None
    return int(m.group())


def parse_race_class(name):
    s = str(name).upper()
    if any(g in s for g in ("G1", "G2", "G3", "GⅠ", "GⅡ", "GⅢ", "JG")):
        return "重賞"
    if "(L)" in s or "オープ" in s or "OP" in s:
        return "オープン"
    if "3勝" in s or "３勝" in s or "1600万" in s:
        return "3勝クラス"
    if "2勝" in s or "２勝" in s or "1000万" in s:
        return "2勝クラス"
    if "1勝" in s or "１勝" in s or "500万" in s or "５００万" in s:
        return "1勝クラス"
    if "未勝利" in s:
        return "未勝利"
    if "新馬" in s:
        return "新馬"
    return "オープン"


def parse_float(v):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def build():
    ap = argparse.ArgumentParser()
    ap.add_argument("--std-from", default="2016", help="基準タイム集計開始年 (YYYY)")
    ap.add_argument("--std-to", default="2023", help="基準タイム集計終了年 (YYYY)")
    args = ap.parse_args()
    std_lo, std_hi = args.std_from + "0101", args.std_to + "1231"

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE runs (
            date TEXT, place TEXT, r INTEGER, race_name TEXT, race_class TEXT,
            horse TEXT, sex TEXT, age INTEGER, jockey TEXT, kinryo REAL,
            total_horses INTEGER, umaban INTEGER, popularity INTEGER, rank INTEGER,
            track_type TEXT, distance INTEGER, condition TEXT,
            time_sec REAL, chakusa REAL, c4 INTEGER, agari REAL, pci REAL,
            weight INTEGER, affi TEXT, win_pay TEXT
        )""")

    std_acc = defaultdict(list)  # (place,type,dist,class,cond) -> [win time]
    batch, n_in, n_ok = [], 0, 0

    with open(SRC, encoding="cp932", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}

        def g(row, col):
            i = idx.get(col)
            return row[i].strip() if i is not None and i < len(row) else ""

        for row in reader:
            n_in += 1
            date = parse_date(g(row, "日付"))
            place = parse_place(g(row, "開催"))
            rank = parse_rank(g(row, "着順"))
            if not date or not place or rank is None:
                continue
            track = g(row, "芝・ダ")
            if track == "ダ":
                track = "ダート"
            if track not in ("芝", "ダート"):
                continue  # 障害等は除外
            try:
                dist = int(g(row, "距離"))
                total = int(g(row, "頭数"))
                umaban = int(g(row, "馬番"))
            except ValueError:
                continue
            race_name = g(row, "レース名")
            rclass = parse_race_class(race_name)
            cond = g(row, "馬場状態")[:1]  # 良/稍/重/不
            tsec = parse_time_sec(g(row, "走破タイム"))
            c4_m = re.search(r"\d+", g(row, "4角"))
            pop_m = re.search(r"\d+", g(row, "人気").translate(ZEN2HAN))
            wt_m = re.search(r"\d{3}", g(row, "馬体重"))

            batch.append((
                date, place, int(g(row, "Ｒ") or 0), race_name, rclass,
                g(row, "馬名"), g(row, "性別"),
                int(g(row, "年齢")) if g(row, "年齢").isdigit() else None,
                g(row, "騎手"), parse_float(g(row, "斤量")),
                total, umaban,
                int(pop_m.group()) if pop_m else None, rank,
                track, dist, cond,
                tsec, parse_float(g(row, "着差")),
                int(c4_m.group()) if c4_m else None,
                parse_float(g(row, "上り3F")), parse_float(g(row, "PCI")),
                int(wt_m.group()) if wt_m else None,
                g(row, "所属"), g(row, "単勝配当"),
            ))
            n_ok += 1

            if rank == 1 and tsec and cond and std_lo <= date <= std_hi:
                std_acc[(place, track, dist, rclass, cond)].append(tsec)

            if len(batch) >= 50000:
                cur.executemany("INSERT INTO runs VALUES (" + ",".join("?" * 25) + ")", batch)
                batch.clear()
                print(f"  ... {n_ok} rows", end="\r")

    if batch:
        cur.executemany("INSERT INTO runs VALUES (" + ",".join("?" * 25) + ")", batch)

    print(f"\nDB取込: {n_ok}/{n_in} 行")
    cur.execute("CREATE INDEX idx_horse_date ON runs(horse, date)")
    cur.execute("CREATE INDEX idx_date ON runs(date)")
    cur.execute("CREATE INDEX idx_course ON runs(place, track_type, distance, race_class, condition)")
    conn.commit()
    conn.close()
    print(f"SQLite: {DB_PATH}")

    # ── 基準タイムJSON (勝ち馬タイム中央値, n>=3のみ) ──
    std = {}
    n_cell = 0
    for (place, track, dist, rclass, cond), times in std_acc.items():
        if len(times) < 3:
            continue
        std.setdefault(place, {}).setdefault(track, {}).setdefault(str(dist), {}) \
           .setdefault(rclass, {})[cond] = round(median(times), 2)
        n_cell += 1
    out = {
        "meta": {"period": f"{args.std_from}-{args.std_to}", "basis": "勝ち馬タイム中央値",
                 "min_samples": 3, "cells": n_cell},
        "times": std,
    }
    os.makedirs(os.path.dirname(STD_PATH), exist_ok=True)
    with open(STD_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"基準タイム: {n_cell}セル -> {STD_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    build()
