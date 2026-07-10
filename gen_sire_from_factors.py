"""
注目産駒データの欠損コース補完
================================
書籍由来の sire/<コース>.csv が無いコースを、db-keiba factors の father_w
統計から自動生成する (勝率降順トップ10、出走数20以上)。
書籍データがある既存ファイルは上書きしない。

factors は半年ごとに自動更新されるため、更新後に再実行すると
生成分も最新化される (--force で生成分を作り直し)。

【使い方】 python gen_sire_from_factors.py [--force]
"""
import argparse
import csv
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(BASE_DIR, "api", "data_files")
SLUGS = ["sapporo", "hakodate", "fukushima", "niigata", "tokyo",
         "nakayama", "chukyo", "kyoto", "hanshin", "kokura"]
MIN_STARTS = 20
TOP_N = 10
# 生成ファイルの管理: CSV自体は書籍データと同形式 (ローダー互換) のため、
# 生成分のファイル名は sire/.generated に記録して書籍データと区別する


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="自動生成分 (マーカー付き) を作り直す")
    args = ap.parse_args()

    n_made, n_skip = 0, 0
    for slug in SLUGS:
        fdir = os.path.join(ROOT, slug, "factors")
        sdir = os.path.join(ROOT, slug, "sire")
        if not os.path.isdir(fdir):
            continue
        os.makedirs(sdir, exist_ok=True)
        for fname in sorted(os.listdir(fdir)):
            if not fname.endswith(".csv"):
                continue
            out_path = os.path.join(sdir, fname)
            manifest = os.path.join(sdir, ".generated")
            generated = set()
            if os.path.exists(manifest):
                with open(manifest, "r", encoding="utf-8") as f:
                    generated = {l.strip() for l in f if l.strip()}
            if os.path.exists(out_path):
                if fname not in generated or not args.force:
                    n_skip += 1
                    continue  # 書籍データ or 生成済み (forceなし) は温存

            all_rows = []
            with open(os.path.join(fdir, fname), "r", encoding="utf-8-sig",
                      newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("factor_type") != "father_w":
                        continue
                    try:
                        all_rows.append((int(float(r["starts"])),
                                         r["entity"].strip(), float(r["win_rate"]),
                                         float(r["quinella_rate"]), float(r["show_rate"])))
                    except (KeyError, ValueError):
                        continue
            # 出走数20以上を基本とし、少頻度コースは10以上→5以上へ段階的に緩和
            rows = []
            for th in (MIN_STARTS, 10, 5):
                rows = [(n, w, q, s) for st, n, w, q, s in all_rows if st >= th]
                if rows:
                    break
            if not rows:
                continue
            rows.sort(key=lambda x: -x[1])  # 勝率降順 (書籍データと同じ並び)
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                for name, win, qui, show in rows[:TOP_N]:
                    w.writerow([name, win, qui, show])
            with open(manifest, "a", encoding="utf-8") as f:
                if fname not in generated:
                    f.write(fname + "\n")
            n_made += 1
            print(f"  生成: {slug}/{fname} ({len(rows[:TOP_N])}頭)")
    print(f"\n[OK] 生成 {n_made} / 既存温存 {n_skip}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
