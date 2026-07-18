"""SPEC-T19 Stage 0: ability.dbに対する父・母父の年別被覆率を読み取り専用集計する。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MIN_BMS_COVERAGE = 0.80


def coverage_by_year(conn, pedigree, date_from="20210101", date_to="20260630"):
    buckets = defaultdict(lambda: {
        "rows": 0, "sire_rows": 0, "bms_rows": 0,
        "horses": set(), "sire_horses": set(), "bms_horses": set(),
    })
    for date8, horse in conn.execute(
            "SELECT date, horse FROM runs WHERE date BETWEEN ? AND ?", (date_from, date_to)):
        year = str(date8)[:4]
        bucket = buckets[year]
        bucket["rows"] += 1
        bucket["horses"].add(horse)
        values = pedigree.get(horse) or {}
        if str(values.get("sire") or "").strip():
            bucket["sire_rows"] += 1
            bucket["sire_horses"].add(horse)
        if str(values.get("bms") or "").strip():
            bucket["bms_rows"] += 1
            bucket["bms_horses"].add(horse)
    result = []
    for year, bucket in sorted(buckets.items()):
        rows, horses = bucket["rows"], len(bucket["horses"])
        result.append({
            "year": year, "rows": rows, "horses": horses,
            "sire_row_pct": round(100 * bucket["sire_rows"] / rows, 2) if rows else 0.0,
            "bms_row_pct": round(100 * bucket["bms_rows"] / rows, 2) if rows else 0.0,
            "sire_horse_pct": round(100 * len(bucket["sire_horses"]) / horses, 2) if horses else 0.0,
            "bms_horse_pct": round(100 * len(bucket["bms_horses"]) / horses, 2) if horses else 0.0,
        })
    return result


def gate_passed(rows, threshold=MIN_BMS_COVERAGE):
    return bool(rows) and all(row["bms_row_pct"] >= 100 * threshold for row in rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=BASE_DIR / "ability.db")
    parser.add_argument("--pedigree", type=Path, default=BASE_DIR / "pedigree_cache.json")
    args = parser.parse_args(argv)
    with args.pedigree.open(encoding="utf-8") as handle:
        pedigree = json.load(handle)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = coverage_by_year(conn, pedigree)
    conn.close()
    print("year rows sire_row% bms_row% horses sire_horse% bms_horse%")
    for row in rows:
        print(row["year"], row["rows"], f'{row["sire_row_pct"]:.2f}',
              f'{row["bms_row_pct"]:.2f}', row["horses"],
              f'{row["sire_horse_pct"]:.2f}', f'{row["bms_horse_pct"]:.2f}')
    print("GATE:", "PASS" if gate_passed(rows) else "STOP")
    return 0 if gate_passed(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
