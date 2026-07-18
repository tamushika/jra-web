"""T51: netkeibaから2026年ability.dbの斤量欠損を埋め戻す隔離ツール。

既定動作はinventory dry-runで、DB書込みもHTTPアクセスも行わない。取得は
``--fetch``、Stage Cの書込みは ``--apply --confirm-t50-complete --backup-path``
をすべて明示した場合だけ許可する。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ability.db"
CACHE_PATH = BASE_DIR / "outputs" / "t51_kinryo_cache.json"
DATE_FROM, DATE_TO = "20260101", "20261231"
SLEEP_SEC = 1.0
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PLACE_CODE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
              "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


def load_targets(conn):
    rows = conn.execute("""
        SELECT date, place, r, umaban, horse
        FROM runs
        WHERE date BETWEEN ? AND ? AND kinryo IS NULL
          AND date LIKE '2026%'
        ORDER BY date, place, r, umaban
    """, (DATE_FROM, DATE_TO)).fetchall()
    races = sorted({(row[0], row[1], int(row[2])) for row in rows})
    return rows, races


def parse_race_list(html, date8):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/race/(\d{12})/?", anchor["href"])
        if not match:
            continue
        race_id = match.group(1)
        place = PLACE_CODE.get(race_id[4:6])
        if place:
            out[(date8, place, int(race_id[-2:]))] = race_id
    return out


def parse_result_page(html):
    """成績表から馬番→斤量と、条件欄のハンデ表記を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="race_table_01")
    weights = {}
    if table is not None:
        rows = table.find_all("tr")
        header = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])] if rows else []
        try:
            horse_index, weight_index = header.index("馬番"), header.index("斤量")
        except ValueError:
            horse_index = weight_index = -1
        if horse_index >= 0:
            for row in rows[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                if len(cells) <= max(horse_index, weight_index):
                    continue
                try:
                    weights[int(cells[horse_index])] = float(cells[weight_index])
                except (TypeError, ValueError):
                    continue
    condition_nodes = soup.select(".data_intro, .RaceData01, .RaceData02")
    condition_text = " ".join(node.get_text(" ", strip=True) for node in condition_nodes)
    handicap = bool(re.search(r"(?:^|\s)ハンデ(?:\s|$)", condition_text))
    return weights, handicap


def _read_cache(path):
    if not path.exists():
        return {"races": {}}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("races", {})
    return data


def _write_cache(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp, path)


def fetch_targets(races, cache, *, limit=0, session=None, sleep_sec=SLEEP_SEC):
    session = session or requests.Session()
    cached = cache.setdefault("races", {})
    pending = [race for race in races if "|".join(map(str, race)) not in cached]
    if limit:
        pending = pending[:limit]
    by_date = defaultdict(list)
    for race in pending:
        by_date[race[0]].append(race)
    for date8, day_races in sorted(by_date.items()):
        response = session.get(f"https://db.netkeiba.com/race/list/{date8}/", headers=UA, timeout=20)
        response.raise_for_status()
        response.encoding = "EUC-JP"
        race_ids = parse_race_list(response.text, date8)
        time.sleep(sleep_sec)
        for race in day_races:
            race_id = race_ids.get(race)
            if not race_id:
                continue
            response = session.get(f"https://db.netkeiba.com/race/{race_id}/", headers=UA, timeout=20)
            response.raise_for_status()
            response.encoding = "EUC-JP"
            weights, handicap = parse_result_page(response.text)
            cached["|".join(map(str, race))] = {
                "race_id": race_id, "weights": {str(k): v for k, v in weights.items()},
                "handicap": handicap,
                "source_url": f"https://db.netkeiba.com/race/{race_id}/",
            }
            time.sleep(sleep_sec)
    return cache


def build_plan(target_rows, cache):
    plan = []
    for date8, place, race_no, horse_no, horse in target_rows:
        race = cache.get("races", {}).get(f"{date8}|{place}|{int(race_no)}")
        weight = (race or {}).get("weights", {}).get(str(int(horse_no))) if horse_no is not None else None
        if weight is None:
            continue
        plan.append({
            "date": date8, "place": place, "r": int(race_no), "umaban": int(horse_no),
            "horse": horse, "kinryo": float(weight), "handicap": bool(race["handicap"]),
            "source_url": race["source_url"],
        })
    return plan


def apply_plan(conn, plan):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS netkeiba_race_metadata (
            date TEXT NOT NULL, place TEXT NOT NULL, r INTEGER NOT NULL,
            handicap INTEGER NOT NULL, source_url TEXT NOT NULL,
            PRIMARY KEY (date, place, r)
        )
    """)
    updated = 0
    for row in plan:
        cursor = conn.execute("""
            UPDATE runs SET kinryo = ?
            WHERE date = ? AND place = ? AND r = ? AND umaban = ?
              AND kinryo IS NULL AND date BETWEEN ? AND ? AND date LIKE '2026%'
        """, (row["kinryo"], row["date"], row["place"], row["r"], row["umaban"],
              DATE_FROM, DATE_TO))
        updated += cursor.rowcount
        if cursor.rowcount:
            conn.execute("""
                INSERT INTO netkeiba_race_metadata
                    (date, place, r, handicap, source_url)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date, place, r) DO UPDATE SET
                    handicap=excluded.handicap, source_url=excluded.source_url
            """, (row["date"], row["place"], row["r"], int(row["handicap"]), row["source_url"]))
    conn.commit()
    return updated


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--fetch", action="store_true", help="netkeiba取得を明示的に行う")
    parser.add_argument("--limit-races", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Stage C専用: ability.dbへ反映")
    parser.add_argument("--confirm-t50-complete", action="store_true")
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args(argv)

    if args.apply and (not args.confirm_t50_complete or not args.backup_path):
        parser.error("--applyには --confirm-t50-complete と --backup-path が必要です")
    conn = (sqlite3.connect(args.db) if args.apply
            else sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True))
    before_changes = conn.total_changes
    targets, races = load_targets(conn)
    dates = {race[0] for race in races}
    print(f"対象: {len(targets)}行 / {len(races)}レース / {len(dates)}開催日")
    print(f"最大HTTP見積り: {len(dates) + len(races)}回 (日一覧{len(dates)} + 成績{len(races)})")
    print("対象サンプル20行:")
    for row in targets[:20]:
        print("  ", tuple(row))

    cache = _read_cache(args.cache)
    if args.fetch:
        cache = fetch_targets(races, cache, limit=args.limit_races)
        _write_cache(args.cache, cache)
    plan = build_plan(targets, cache)
    print(f"取得済みキャッシュからの更新予定: {len(plan)}行")
    for row in plan[:20]:
        print("  PLAN", row)

    if args.apply:
        if not plan:
            parser.error("取得済み更新計画が0行のため --apply を拒否しました")
        args.backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.db, args.backup_path)
        print(f"バックアップ: {args.backup_path}")
        print(f"更新: {apply_plan(conn, plan)}行")
    else:
        assert conn.total_changes == before_changes
        print("DRY-RUN: DB書込み0")
    conn.close()


if __name__ == "__main__":
    main()
