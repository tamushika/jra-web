"""
週末全レースカードの血統一括収集
==================================
開催日の全会場・全レースの出馬表をスクレイプし、(馬名, 父, 母父, 母) を
Neon の horse_pedigree テーブルへ蓄積する (analyze_race_url 内の受動的蓄積
フックと同じ経路。本スクリプトは「アプリで見なかったレース」も拾うのが目的)。

蓄積が学習期間の出走馬を十分カバーした時点で、ML特徴量に血統
(db-keiba factors の father_w 照合等) を追加できるようになる。

【使い方】 python collect_pedigree.py        # 本日開催の全レース
           python collect_pedigree.py --url <任意のレースカードURL>  # 起点指定
【前提】  .env に DATABASE_URL (Neon) / レース開催日 (木〜日) に実行
"""
import argparse
import os
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.jra.go.jp/"}


def find_entry_url():
    """JRAトップ/今週ページから任意のレースカードURL (accessD dde) を1つ見つける"""
    for candidate in ("https://www.jra.go.jp/", "https://www.jra.go.jp/keiba/thisweek/"):
        try:
            res = requests.get(candidate, headers=HDRS, timeout=10)
            res.encoding = "cp932"
            soup = BeautifulSoup(res.text, "html.parser")
            fallback = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "accessD.html" not in href or "CNAME=" not in href:
                    continue
                from urllib.parse import urljoin
                full = urljoin("https://www.jra.go.jp/", href)
                if "dde" in href:
                    return full
                fallback = fallback or full
            if fallback:
                return fallback
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="起点レースカードURL (省略時は自動発見)")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    import index as api_index  # noqa: E402  (api/index.py)
    import pedigree_store  # noqa: E402

    entry = args.url or find_entry_url()
    if not entry:
        print("[ERROR] 出馬表URLが見つかりません (開催日にお試しください)")
        sys.exit(1)
    print(f"起点: {entry}")

    res = requests.get(entry, headers=HDRS, timeout=15)
    res.encoding = "shift_jis"
    matrix = api_index.build_matrix_data(BeautifulSoup(res.text, "html.parser"))
    all_races = [(v["text"], r["r"], r["url"]) for v in matrix for r in v["races"]]
    print(f"発見: {len(matrix)}会場 {len(all_races)}レース")

    n_ok = n_ng = 0
    for text, rn, url in all_races:
        try:
            # analyze_race_url 内の蓄積フックが horse_pedigree へ upsert する
            result = api_index.analyze_race_url(url, "簡易")
            n = len(result.get("horses", []))
            n_ok += 1
            print(f"  {text} {rn}R: {n}頭")
        except Exception as e:
            n_ng += 1
            print(f"  {text} {rn}R: 失敗 ({e})")
        time.sleep(args.sleep)

    stats = pedigree_store.coverage_stats()
    print(f"\n完了: 成功{n_ok} / 失敗{n_ng}")
    if stats:
        print(f"horse_pedigree 蓄積状況: {stats[0]}頭 (最終更新 {stats[1]})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
