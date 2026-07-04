"""
種牡馬ファクターのアウトオブサンプル検証 (案C)
================================================
db-keiba.com の各コースページには year5 (2021-2025集計 = スコアの根拠) と
year1 (2026年単年 = 集計に含まれない当年データ) の種牡馬表が併載されている。

「year5で複勝率がコース基準を上回っていた種牡馬 (=スコアがプラスに振れる種牡馬) が、
 2026年のレースでも基準超えを維持しているか」を全コース横断で検証する。

【使い方】
  python sire_oos_check.py                 # 全コース取得+検証 (~3分)
  python sire_oos_check.py --only sapporo  # 対象を絞る
  python sire_oos_check.py --min-starts 3  # year1側の最低出走数 (デフォルト5)
  python sire_oos_check.py --csv out.csv   # ペア明細をCSV出力

【バイアス注意 (レポートにも表示)】
  year1表も「当年上位種牡馬のみ掲載」のため、2026年に全く走らなかった/惨敗した
  種牡馬は表から消える (生存バイアス)。高シグナル群・低シグナル群とも同じ
  フィルタを通るため群間比較は概ね公平だが、絶対値は上振れしうる。
"""
import argparse
import csv
import sys
import time

import requests

from scrape_dbkeiba import (
    SLUGS, fetch_page, parse_factor_tables, parse_slug, norm_text,
)


def baseline_show(tables):
    rows = tables.get("allall", [])
    if rows and rows[0].get("show_rate") is not None:
        return rows[0]["show_rate"]
    return None


def collect_pairs(args):
    """全コースから (course, sire, year5シグナル, year1成績) ペアを収集"""
    session = requests.Session()
    pairs = []
    n_page = 0
    for slug in SLUGS:
        if args.only and args.only not in slug:
            continue
        venue, race_type, dist, _ = parse_slug(slug)
        print(f"  fetching {slug} ...")
        html = fetch_page(slug, session)
        time.sleep(args.delay)
        if html is None:
            print(f"    [NG] {slug}")
            continue
        n_page += 1

        t5 = parse_factor_tables(html, "year5")
        t1 = parse_factor_tables(html, "year1")
        b5, b1 = baseline_show(t5), baseline_show(t1)
        if b5 is None or b1 is None:
            print(f"    [WARN] {slug}: baseline欠落 (year5={b5}, year1={b1})")
            continue

        y1_map = {norm_text(r["entity"]): r for r in t1.get("father_w", [])}
        for r5 in t5.get("father_w", []):
            key = norm_text(r5["entity"])
            r1 = y1_map.get(key)
            if r1 is None:
                continue  # 2026年表に不在 (生存バイアスの源泉 → レポートで開示)
            if (r1.get("starts") or 0) < args.min_starts:
                continue
            if r5.get("show_rate") is None or r1.get("show_rate") is None:
                continue
            pairs.append({
                "course": f"{venue}{race_type}{dist}",
                "sire": r5["entity"],
                "y5_starts": r5["starts"], "y5_show": r5["show_rate"],
                "y5_baseline": b5, "signal": round(r5["show_rate"] - b5, 1),
                "y1_starts": r1["starts"], "y1_show": r1["show_rate"],
                "y1_baseline": b1, "outcome": round(r1["show_rate"] - b1, 1),
            })
    return pairs, n_page


def weighted_mean(vals_weights):
    tw = sum(w for _, w in vals_weights)
    return sum(v * w for v, w in vals_weights) / tw if tw else 0.0


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def report(pairs, n_page, args):
    print(f"\n===== 種牡馬ファクター アウトオブサンプル検証 =====")
    print(f"対象: {n_page}ページ / 検証ペア(コース×種牡馬): {len(pairs)}件 "
          f"(year1出走{args.min_starts}以上)")
    if len(pairs) < 30:
        print("[WARN] ペア数が少なく統計的に不安定です (--min-starts を下げる等を検討)")
    if not pairs:
        return

    xs = [p["signal"] for p in pairs]
    ys = [p["outcome"] for p in pairs]
    rho = spearman(xs, ys)
    print(f"\nSpearman相関 (year5シグナル vs 2026年乖離): ρ = {rho:+.3f}")

    # シグナル3分位 → 2026年成績 (year1出走数で加重)
    sorted_pairs = sorted(pairs, key=lambda p: p["signal"])
    n = len(sorted_pairs)
    print("\nシグナル3分位ごとの2026年成績 (出走数加重):")
    print("  分位            | シグナル範囲      | 2026年乖離 | 2026年複勝率 | 出走数")
    labels = ["下位 (消し側)", "中位          ", "上位 (買い側)"]
    for i in range(3):
        chunk = sorted_pairs[i * n // 3:(i + 1) * n // 3]
        sig_lo, sig_hi = chunk[0]["signal"], chunk[-1]["signal"]
        out_w = weighted_mean([(p["outcome"], p["y1_starts"]) for p in chunk])
        show_w = weighted_mean([(p["y1_show"], p["y1_starts"]) for p in chunk])
        starts = sum(p["y1_starts"] for p in chunk)
        print(f"  {labels[i]} | {sig_lo:+5.1f}〜{sig_hi:+5.1f}pt | {out_w:+8.1f}pt | "
              f"{show_w:10.1f}% | {starts}")

    # プラスシグナル vs マイナスシグナル (実運用の加点/減点に対応)
    pos = [p for p in pairs if p["signal"] > 0]
    neg = [p for p in pairs if p["signal"] <= 0]
    print("\n実運用視点 (シグナル正=加点対象 / 負=減点対象):")
    for label, grp in (("加点対象 (signal>0)", pos), ("減点対象 (signal<=0)", neg)):
        if not grp:
            continue
        out_w = weighted_mean([(p["outcome"], p["y1_starts"]) for p in grp])
        keep = sum(1 for p in grp if (p["outcome"] > 0) == (p["signal"] > 0))
        print(f"  {label}: {len(grp)}件, 2026年平均乖離 {out_w:+.1f}pt, "
              f"方向一致率 {100.0 * keep / len(grp):.0f}%")

    print("\n[注意] year1表も当年上位種牡馬のみ掲載のため生存バイアスあり "
          "(群間比較は概ね公平・絶対値は上振れ側)。2026年は7月時点の半年分。")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(pairs[0].keys()))
            w.writeheader()
            w.writerows(pairs)
        print(f"\n明細CSV: {args.csv}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="スラッグ部分一致フィルタ")
    ap.add_argument("--min-starts", type=int, default=5, help="year1側の最低出走数")
    ap.add_argument("--delay", type=float, default=1.5, help="リクエスト間隔(秒)")
    ap.add_argument("--csv", default="", help="ペア明細CSVの出力先")
    args = ap.parse_args()

    pairs, n_page = collect_pairs(args)
    report(pairs, n_page, args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
