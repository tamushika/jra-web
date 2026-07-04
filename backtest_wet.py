"""
道悪適性 (当日馬場状態) のバックテスト
========================================
「当日が道悪 (稍重/重/不良) のとき、直近4走の道悪実績は着順を予測するか」を検証する。
本番のスコア計算が参照できるのは直近4走のみのため、それに合わせる。

グループ定義 (直近4走ベース):
  道悪好走 : 道悪での出走があり、いずれか3着以内
  道悪凡走 : 道悪での出走はあるが全て4着以下
  道悪未経験: 直近4走に道悪出走なし

プラセボ検証: 同じグループ分けを「当日良馬場」のレースにも適用。
道悪適性が本物なら道悪日だけ差が出るはず (良馬場でも差が出るなら単なる能力の代理)。

【使い方】 python backtest_wet.py [--from 20240101 --to 20251231]
"""
import argparse
import sqlite3
import sys
from collections import defaultdict

import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from backtest_ability import load_runs, parse_win_payout  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
WET = {"稍", "重", "不"}


def classify(prior4):
    """直近4走から道悪適性グループを判定"""
    wet_runs = [p for p in prior4 if p["condition"] in WET]
    if not wet_runs:
        return "道悪未経験"
    if any(p["rank"] is not None and p["rank"] <= 3 for p in wet_runs):
        return "道悪好走"
    return "道悪凡走"


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
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    def bucket():
        return {"n": 0, "win": 0, "top3": 0, "payout": 0.0}

    # {当日馬場カテゴリ: {グループ: bucket}}
    tables = {"道悪日 (稍/重/不)": defaultdict(bucket), "良馬場日 (プラセボ)": defaultdict(bucket)}

    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < args.date_from or cur["rank"] is None or not cur["condition"]:
                continue
            prior4 = lst[max(0, i - 4):i]
            if not prior4:
                continue
            group = classify(prior4)
            day = "道悪日 (稍/重/不)" if cur["condition"] in WET else "良馬場日 (プラセボ)"
            b = tables[day][group]
            b["n"] += 1
            b["win"] += 1 if cur["rank"] == 1 else 0
            b["top3"] += 1 if cur["rank"] <= 3 else 0
            b["payout"] += parse_win_payout(cur["win_pay"], cur["rank"])

    for day, table in tables.items():
        total = bucket()
        for b in table.values():
            for k in total:
                total[k] += b[k]
        print(f"\n===== {day} (全体: n={total['n']}, "
              f"勝率{100.0*total['win']/max(total['n'],1):.1f}%, "
              f"複勝率{100.0*total['top3']/max(total['n'],1):.1f}%) =====")
        print("  グループ      | 出走    | 勝率   | 複勝率 | 単回収")
        for g in ("道悪好走", "道悪凡走", "道悪未経験"):
            b = table.get(g)
            if not b or b["n"] == 0:
                continue
            print(f"  {g:8s}| {b['n']:7d} | {100.0*b['win']/b['n']:5.1f}% | "
                  f"{100.0*b['top3']/b['n']:5.1f}% | {100.0*b['payout']/(100.0*b['n']):5.1f}%")

    print("\n[読み方] 道悪日にグループ間の差があり、良馬場日には差が小さい場合のみ"
          "「道悪適性」として有効 (良馬場でも同じ差が出るなら能力の代理に過ぎない)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
