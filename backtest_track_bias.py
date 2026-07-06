"""
ポジションバイアス (脚質/枠) の持続性検証
==========================================
「前日・前週の馬場傾向 (前残り/差し有利、内/外枠有利) は当日も持続するか」を
ability.db (4角通過順つき) で検証する。持続するならスコア特徴量にできる。

  バイアス指標 (開催日×場×芝ダ別):
    脚質バイアス = 3着内馬の前目度平均 − 全出走馬平均
                   (前目度 = 1 − (4角通過順−1)/(頭数−1)、1=先頭 0=最後方)
    枠バイアス   = 3着内馬の馬番正規化平均 − 全出走馬平均 (+=外枠有利)

  持続性 = 前開催日のバイアスと当日バイアスの相関 (日数ギャップ別):
    gap<=2日  : 同一週末 (土→日)
    3-10日    : 同一開催の翌週
    >10日     : 開催跨ぎ (レール移動・馬場改修を挟む)

【使い方】 python backtest_track_bias.py
"""
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from backtest_ability import load_runs  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
MIN_RACES = 4   # 1日×場×芝ダで最低レース数 (これ未満はノイズとして除外)


def compute_day_biases(runs, date_from="20210101"):
    """{(date, place, tt): {"kyaku": float, "waku": float, "n_races": int}}"""
    groups = defaultdict(list)
    for r in runs:
        n = r.get("total_horses")
        c4 = r.get("c4")
        if (r["date"] < date_from or r["rank"] is None or not c4
                or not r.get("umaban") or not n or n < 8
                or not (1 <= c4 <= n) or not (1 <= r["umaban"] <= n)):
            continue
        groups[(r["date"], r["place"], r["track_type"])].append(r)

    biases = {}
    for key, members in groups.items():
        races = defaultdict(list)
        for m in members:
            races[m["r"]].append(m)
        if len(races) < MIN_RACES:
            continue
        front_all, front_top, waku_all, waku_top = [], [], [], []
        for rmem in races.values():
            for m in rmem:
                n = m["total_horses"]
                front = 1.0 - (m["c4"] - 1) / max(n - 1, 1)
                waku = (m["umaban"] - 1) / max(n - 1, 1)
                front_all.append(front)
                waku_all.append(waku)
                if m["rank"] <= 3:
                    front_top.append(front)
                    waku_top.append(waku)
        if len(front_top) < 9:
            continue
        biases[key] = {
            "kyaku": sum(front_top) / len(front_top) - sum(front_all) / len(front_all),
            "waku": sum(waku_top) / len(waku_top) - sum(waku_all) / len(waku_all),
            "n_races": len(races),
        }
    return biases


def pearson(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else None


def main():
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, "20210101", "20260630")
    conn.close()
    print(f"ロード: {len(runs)}行")

    biases = compute_day_biases(runs)
    print(f"バイアス計測: {len(biases)} (開催日×場×芝ダ)")

    # 恒常成分 (場×芝ダの長期平均) を引いて「一時バイアス」に変換。
    # これをしないと場ごとの恒常的な枠有利がペア相関を水増しする
    pt_sum = defaultdict(lambda: [0.0, 0.0, 0])
    for (date, place, tt), b in biases.items():
        s = pt_sum[(place, tt)]
        s[0] += b["kyaku"]
        s[1] += b["waku"]
        s[2] += 1
    for (date, place, tt), b in biases.items():
        s = pt_sum[(place, tt)]
        b["kyaku"] -= s[0] / s[2]
        b["waku"] -= s[1] / s[2]

    ks = [b["kyaku"] for b in biases.values()]
    ws = [b["waku"] for b in biases.values()]
    n = len(ks)
    print(f"\n脚質バイアス分布: 平均{sum(ks)/n:+.3f} / "
          f"SD {(sum((k-sum(ks)/n)**2 for k in ks)/n)**0.5:.3f}")
    print(f"枠バイアス分布:   平均{sum(ws)/n:+.3f} / "
          f"SD {(sum((w-sum(ws)/n)**2 for w in ws)/n)**0.5:.3f}")

    # ── 持続性: 同場×同芝ダの連続開催日ペア ──
    by_pt = defaultdict(list)
    for (date, place, tt), b in biases.items():
        by_pt[(place, tt)].append((date, b))
    pairs = {"同一週末(gap<=2)": ([], []), "同一開催翌週(3-10)": ([], []),
             "開催跨ぎ(>10)": ([], [])}
    pairs_w = {k: ([], []) for k in pairs}
    for lst in by_pt.values():
        lst.sort()
        for (d0, b0), (d1, b1) in zip(lst, lst[1:]):
            try:
                gap = (datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d0, "%Y%m%d")).days
            except ValueError:
                continue
            bucket = ("同一週末(gap<=2)" if gap <= 2
                      else "同一開催翌週(3-10)" if gap <= 10 else "開催跨ぎ(>10)")
            pairs[bucket][0].append(b0["kyaku"])
            pairs[bucket][1].append(b1["kyaku"])
            pairs_w[bucket][0].append(b0["waku"])
            pairs_w[bucket][1].append(b1["waku"])

    print("\n===== バイアス持続性 (前開催日 vs 当日 の相関) =====")
    print("  区分               | ペア数 | 脚質バイアス r | 枠バイアス r")
    for k in pairs:
        xs, ys = pairs[k]
        xw, yw = pairs_w[k]
        rk = pearson(xs, ys)
        rw = pearson(xw, yw)
        print(f"  {k:18s}| {len(xs):5d} | "
              f"{rk:+.3f}" if rk is not None else f"  {k:18s}| {len(xs):5d} |   n/a",
              end="")
        print(f"        | {rw:+.3f}" if rw is not None else "        |   n/a")

    # ── 実利チェック: 前日バイアスに「乗る」馬の成績 ──
    # 前日バイアスが前有利 (kyaku>+0.02) の翌日、前走4角3番手以内の馬 vs 10番手以下の馬
    print("\n===== 実利チェック: 同週末の前日バイアス別 × 馬の脚質タイプ別 複勝率 =====")
    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    prev_day = {}  # (date, place, tt) -> 前開催日バイアス (同週末のみ)
    for (place, tt), lst in by_pt.items():
        lst.sort()
        for (d0, b0), (d1, _b1) in zip(lst, lst[1:]):
            try:
                gap = (datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d0, "%Y%m%d")).days
            except ValueError:
                continue
            if gap <= 2:
                prev_day[(d1, place, tt)] = b0

    def bucket():
        return {"n": 0, "top3": 0}

    table = defaultdict(bucket)
    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < "20210101" or cur["rank"] is None or i == 0:
                continue
            pb = prev_day.get((cur["date"], cur["place"], cur["track_type"]))
            if pb is None:
                continue
            prior = [p for p in lst[max(0, i - 4):i] if p.get("c4") and p.get("total_horses")]
            if not prior:
                continue
            style = sum(1.0 - (p["c4"] - 1) / max(p["total_horses"] - 1, 1)
                        for p in prior) / len(prior)
            style_g = "前型" if style >= 0.65 else ("後型" if style <= 0.35 else "中団")
            bias_g = ("前有利日" if pb["kyaku"] >= 0.02
                      else "差し有利日" if pb["kyaku"] <= -0.02 else "フラット日")
            b = table[(bias_g, style_g)]
            b["n"] += 1
            b["top3"] += 1 if cur["rank"] <= 3 else 0

    print("  前日バイアス \\ 馬 | " + " | ".join(f"{s:12s}" for s in ("前型", "中団", "後型")))
    for bg in ("前有利日", "フラット日", "差し有利日"):
        cells = []
        for sg in ("前型", "中団", "後型"):
            b = table.get((bg, sg))
            cells.append(f"{100.0*b['top3']/b['n']:5.1f}% n={b['n']:6d}" if b and b["n"] else " " * 14)
        print(f"  {bg:14s}| " + " | ".join(cells))

    print("\n[見方] 「前有利日の前型」と「差し有利日の前型」の複勝率差が大きければ持続性に実利あり。"
          "交差 (前有利日は前型>後型、差し有利日は逆転) が理想形。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
