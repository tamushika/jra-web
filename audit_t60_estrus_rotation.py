"""T60 Stage A: read-only audit of the estrus-rotation hypothesis (H2).

Preregistered design: docs/codex/SPEC-T60-estrus-rotation.md (fixed before
any number was computed). ability.db is opened read-only; no scraping.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date

import numpy as np

DB = "ability.db"
FROM, TO = "20210101", "20260630"
POP_BANDS = (("pop1_3", 1, 3), ("pop4_8", 4, 8), ("pop9plus", 9, 99))
GROUPS = (("naka2", 15, 21), ("naka3", 22, 28), ("naka4_6", 29, 49))
BOOT, SEED = 4000, 60


def to_date(compact):
    return date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))


def load_rows():
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(connection.execute(
        """SELECT horse, date, place, r, sex, rank, popularity, total_horses
             FROM runs WHERE date >= '20180101' AND date <= ? AND rank IS NOT NULL
                       AND horse IS NOT NULL ORDER BY horse, date""", (TO,)))
    connection.close()
    return rows


def build_samples(rows):
    samples = []
    prev_by_horse = {}
    for horse, day, place, race_no, sex, rank, pop, total in rows:
        prev = prev_by_horse.get(horse)
        prev_by_horse[horse] = (day, rank)
        if day < FROM or prev is None:
            continue
        try:
            rank_i, pop_i, total_i = int(rank), int(pop), int(total)
            prev_rank = int(prev[1])
        except (TypeError, ValueError):
            continue
        if total_i < 8 or pop_i < 1:
            continue
        interval = (to_date(day) - to_date(prev[0])).days
        group = next((name for name, lo, hi in GROUPS if lo <= interval <= hi), None)
        if group is None:
            continue
        samples.append({
            "day": day, "month": int(day[4:6]), "sex": sex, "group": group,
            "pop_band": next(name for name, lo, hi in POP_BANDS if lo <= pop_i <= hi),
            "prev_top3": prev_rank <= 3, "hit": rank_i <= 3,
        })
    return samples


def seg(samples, *, female, spring, group, prev_top3=None):
    sexes = ("牝",) if female else ("牡", "セ")
    months = range(1, 9) if spring else range(9, 13)
    return [s for s in samples if s["sex"] in sexes and s["month"] in months
            and s["group"] == group
            and (prev_top3 is None or s["prev_top3"] == prev_top3)]


def rate(rows):
    return (sum(r["hit"] for r in rows) / len(rows)) if rows else float("nan"), len(rows)


def did(samples, *, base_spring, prev_top3, pop_band=None):
    """(fem naka3 - fem naka2) - (ctrl naka3 - ctrl naka2)."""
    def pick(female, group):
        rows = seg(samples, female=female, spring=base_spring, group=group,
                   prev_top3=prev_top3)
        if pop_band:
            rows = [r for r in rows if r["pop_band"] == pop_band]
        return rows
    f3, f2 = pick(True, "naka3"), pick(True, "naka2")
    m3, m2 = pick(False, "naka3"), pick(False, "naka2")
    value = (rate(f3)[0] - rate(f2)[0]) - (rate(m3)[0] - rate(m2)[0])
    return value, (f3, f2, m3, m2)


def day_bootstrap(cells, seed=SEED, n=BOOT):
    """Resample race days within each cell; p-value for DiD != 0 (two-sided)."""
    rng = np.random.default_rng(seed)
    by_day = [defaultdict(list) for _ in cells]
    for index, cell in enumerate(cells):
        for row in cell:
            by_day[index][row["day"]].append(row["hit"])
    day_lists = [list(mapping.values()) for mapping in by_day]
    stats = []
    for _ in range(n):
        means = []
        for days in day_lists:
            if not days:
                means.append(float("nan"))
                continue
            picks = rng.integers(0, len(days), len(days))
            values = [hit for pick in picks for hit in days[pick]]
            means.append(np.mean(values) if values else float("nan"))
        stats.append((means[0] - means[1]) - (means[2] - means[3]))
    stats = np.asarray(stats)
    stats = stats[~np.isnan(stats)]
    observed = np.mean(stats)
    p = 2 * min((stats >= 0).mean(), (stats <= 0).mean())
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)), float(p)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(open(DB, "rb").read()).hexdigest()
    print(f"ability.db sha256 = {digest}")
    samples = build_samples(load_rows())
    print(f"samples (2021-2026H1, 8+ field, interval 15-49d): {len(samples)}")

    output = {"db_sha256": digest, "n_samples": len(samples), "cells": {}, "did": {}}
    for spring in (True, False):
        tag = "spring" if spring else "autumn"
        for female in (True, False):
            for group_name, _, _ in GROUPS:
                for prev in (True, False):
                    rows = seg(samples, female=female, spring=spring,
                               group=group_name, prev_top3=prev)
                    value, count = rate(rows)
                    output["cells"][f"{tag}|{'f' if female else 'm'}|{group_name}|prev3={prev}"] = {
                        "hit_rate": round(value, 4) if count else None, "n": count}

    for prev in (True, False):
        for pop_band in (None, "pop1_3", "pop4_8", "pop9plus"):
            key = f"spring_did|prev3={prev}|{pop_band or 'all'}"
            value, cells = did(samples, base_spring=True, prev_top3=prev, pop_band=pop_band)
            lo, hi, p = day_bootstrap(cells)
            output["did"][key] = {"did": round(value, 4), "ci": [round(lo, 4), round(hi, 4)],
                                   "p": round(p, 4), "n": [len(c) for c in cells]}
            print(key, output["did"][key])
    # autumn placebo (non-breeding season females)
    for prev in (True, False):
        key = f"autumn_placebo|prev3={prev}|all"
        value, cells = did(samples, base_spring=False, prev_top3=prev, pop_band=None)
        lo, hi, p = day_bootstrap(cells)
        output["did"][key] = {"did": round(value, 4), "ci": [round(lo, 4), round(hi, 4)],
                               "p": round(p, 4), "n": [len(c) for c in cells]}
        print(key, output["did"][key])

    with open("outputs/t60_audit.json", "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=1)
    print("written outputs/t60_audit.json")


if __name__ == "__main__":
    main()
