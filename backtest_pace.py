"""
ペース適性 (PCI) の3段階バックテスト
======================================
PCI = 100×(道中600m換算ペース)/上がり3F − 50。高 = 瞬発戦(スロー→上がり勝負)、低 = 持続戦(前傾)。

検証は3段階 (どこかで切れたら実装しない):
  A) 適性の存在: 実現したレースペース別に「瞬発型/持続型の馬」の成績が交差するか
  B) ペースの予測可能性: 出走メンバーの過去PCI平均から当該レースのペースを予測できるか
  C) ライブ実装可能な特徴量: 「馬の型 × 予測ペースの一致度」が成績を予測するか
     (簡易モード相当 = 前走のみ / 詳細モード相当 = 直近4走 の両方で評価)

【使い方】 python backtest_pace.py [--from 20240101 --to 20251231]
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from statistics import median, mean

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from backtest_ability import load_runs, parse_win_payout  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

# 芝/ダートでPCI水準が違うため、型判定は track_type 平均からの偏差で行う
TRACK_MEAN = {"芝": 51.96, "ダート": 45.83}


def pci_dev(run):
    if run.get("pci") is None or run["track_type"] not in TRACK_MEAN:
        return None
    return run["pci"] - TRACK_MEAN[run["track_type"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20240101")
    ap.add_argument("--to", dest="date_to", default="20251231")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.date_from, args.date_to)
    conn.close()
    print(f"ロード: {len(runs)}行")

    by_horse = defaultdict(list)
    by_race = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
        by_race[(r["date"], r["place"], r["r"])].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # レースの実現ペース偏差 (出走馬PCIの中央値 − track平均)
    race_pace = {}
    for key, members in by_race.items():
        devs = [pci_dev(m) for m in members]
        devs = [d for d in devs if d is not None]
        if len(devs) >= 5:
            race_pace[key] = median(devs)

    # 馬ごとの事前情報: 直近4走の (型PCI偏差, 前走のみのPCI偏差) を評価走に紐づけ
    samples = []  # (cur_run, style4, style1, prior_index)
    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < args.date_from or cur["rank"] is None:
                continue
            prior = lst[max(0, i - 4):i][::-1]
            devs = [pci_dev(p) for p in prior]
            devs = [d for d in devs if d is not None]
            if not devs:
                continue
            style4 = mean(devs)
            style1 = pci_dev(prior[0])
            samples.append((cur, style4, style1))

    print(f"評価出走: {len(samples)}")

    def bucket():
        return {"n": 0, "win": 0, "top3": 0, "pay": 0.0}

    def add(b, cur):
        b["n"] += 1
        b["win"] += cur["rank"] == 1
        b["top3"] += cur["rank"] <= 3
        b["pay"] += parse_win_payout(cur["win_pay"], cur["rank"])

    def show(table, row_order, col_order, title):
        print(f"\n===== {title} =====")
        header = "  " + " " * 14 + " | " + " | ".join(f"{c:>16s}" for c in col_order)
        print(header)
        for rk in row_order:
            cells = []
            for ck in col_order:
                b = table.get((rk, ck))
                if b and b["n"] > 50:
                    cells.append(f"複{100*b['top3']/b['n']:5.1f}% n={b['n']:5d}")
                else:
                    cells.append(" " * 16)
            print(f"  {rk:14s} | " + " | ".join(cells))

    # ── A) 適性の存在 (実現ペース使用 = 上限評価) ──
    tableA = defaultdict(bucket)
    for cur, style4, _ in samples:
        rp = race_pace.get((cur["date"], cur["place"], cur["r"]))
        if rp is None:
            continue
        style_g = "瞬発型(+2以上)" if style4 >= 2 else ("持続型(-2以下)" if style4 <= -2 else "中間")
        pace_g = "瞬発戦(+2以上)" if rp >= 2 else ("持続戦(-2以下)" if rp <= -2 else "平均的")
        add(tableA[(style_g, pace_g)], cur)
    show(tableA, ["瞬発型(+2以上)", "中間", "持続型(-2以下)"],
         ["瞬発戦(+2以上)", "平均的", "持続戦(-2以下)"],
         "A) 馬の型 × 実現ペース (複勝率)。交差があれば適性は実在")

    # ── B) ペースの予測可能性 ──
    xs, ys = [], []
    pred_pace = {}
    for key, members in by_race.items():
        if key not in race_pace:
            continue
        # 事前情報: 各出走馬の直近4走PCI偏差平均 → その平均
        pre = []
        for m in members:
            lst = by_horse.get(m["horse"], [])
            idx = next((j for j, x in enumerate(lst) if x is m), None)
            if idx is None or idx == 0:
                continue
            devs = [pci_dev(p) for p in lst[max(0, idx - 4):idx]]
            devs = [d for d in devs if d is not None]
            if devs:
                pre.append(mean(devs))
        if len(pre) >= 5:
            pv = mean(pre)
            pred_pace[key] = pv
            xs.append(pv)
            ys.append(race_pace[key])
    if xs:
        n = len(xs)
        mx, my = mean(xs), mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
        sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
        sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
        print(f"\n===== B) ペース予測可能性 =====")
        print(f"  予測(メンバー過去PCI平均) vs 実現ペース: 相関 r = {cov/(sx*sy):+.3f} (n={n}レース)")

    # ── C) ライブ実装可能な特徴量: 型と予測ペースの一致 ──
    for label, use4 in (("直近4走 (詳細モード相当)", True), ("前走のみ (簡易モード相当)", False)):
        tableC = defaultdict(bucket)
        for cur, style4, style1 in samples:
            key = (cur["date"], cur["place"], cur["r"])
            pv = pred_pace.get(key)
            style = style4 if use4 else style1
            if pv is None or style is None:
                continue
            match = style * pv  # 正 = 型と予測ペースが同方向 (瞬発型×瞬発戦見込み等)
            g = "一致(+4以上)" if match >= 4 else ("不一致(-4以下)" if match <= -4 else "中間")
            add(tableC[(g, "all")], cur)
        print(f"\n===== C) 型×予測ペース一致度 [{label}] =====")
        for g in ("一致(+4以上)", "中間", "不一致(-4以下)"):
            b = tableC.get((g, "all"))
            if not b or not b["n"]:
                continue
            print(f"  {g:12s}| n={b['n']:6d} | 勝率{100*b['win']/b['n']:5.1f}% | "
                  f"複勝率{100*b['top3']/b['n']:5.1f}% | 単回収{100*b['pay']/(100*b['n']):5.1f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
