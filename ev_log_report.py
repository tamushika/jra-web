"""
期待値監視の実測レポート
==========================
jra_ev.py が通知時に記録した ev_alert_log (通知時オッズ・勝率・EV) を、
Neon races テーブルの確定結果 (日曜夜に登録) と突き合わせ、
実運用での的中率・回収率・オッズドリフトを算出する。

  - 単勝回収: 勝ち馬の odds 列 (確定払戻円) を使用
  - オッズドリフト: 通知時オッズ → 確定オッズ (horse_odds, backfill済み分) の変化
  - 複勝回収は races に複勝配当が無いため複勝的中率のみ表示

【使い方】 python ev_log_report.py [--since 260711]
"""
import argparse
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import past_data_service as pds  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="集計開始日 (Neon形式 YYMMDD)")
    args = ap.parse_args()

    conn = pds.get_db_connection(API_DIR)
    if conn is None or not getattr(conn, "is_pg", False):
        print("[ERROR] Neon未接続")
        sys.exit(1)
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM ev_alert_log WHERE date >= %s ORDER BY date, race_num",
                    (args.since or "0",))
        logs = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"通知ログがありません ({e})")
        sys.exit(0)
    if not logs:
        print("通知ログ0件")
        sys.exit(0)

    # 結果を照合
    stats = defaultdict(lambda: {"n": 0, "win": 0, "top3": 0, "tan_ret": 0.0,
                                 "drift": [], "no_result": 0})
    rows_detail = []
    for lg in logs:
        cur.execute("""SELECT rank, odds, horse_odds FROM races
                       WHERE date = %s AND place = %s AND race_num = %s
                         AND horse_number = %s LIMIT 1""",
                    (lg["date"], lg["venue"], lg["race_num"], str(lg["horse_num"])))
        res = cur.fetchone()
        for key in (f"{lg['stage']}分前", "全体"):
            b = stats[key]
            if res is None or res["rank"] is None:
                b["no_result"] += 1
                continue
            rank = float(res["rank"])
            b["n"] += 1
            if rank == 1:
                b["win"] += 1
                try:
                    b["tan_ret"] += float(str(res["odds"]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            if rank <= 3:
                b["top3"] += 1
            try:
                final = float(res["horse_odds"]) if res["horse_odds"] else None
                if final and lg["odds_alert"]:
                    b["drift"].append(final / lg["odds_alert"])
            except (ValueError, TypeError):
                pass
        if res:
            rows_detail.append((lg, res))

    print(f"===== 期待値監視 実測レポート (通知 {len(logs)}件) =====")
    print("区分     | 照合済み | 単勝的中 | 複勝的中 | 単勝回収 | オッズドリフト(確定/通知時) | 結果未登録")
    for key in ("15分前", "5分前", "全体"):
        b = stats.get(key)
        if not b or (b["n"] + b["no_result"]) == 0:
            continue
        drift = (sum(b["drift"]) / len(b["drift"])) if b["drift"] else None
        print(f"  {key:7s}| {b['n']:6d} | "
              f"{100.0*b['win']/b['n'] if b['n'] else 0:6.1f}% | "
              f"{100.0*b['top3']/b['n'] if b['n'] else 0:6.1f}% | "
              f"{b['tan_ret']/b['n'] if b['n'] else 0:6.1f}% | "
              f"{f'×{drift:.2f}' if drift else '   n/a':>10s}          | {b['no_result']}")
    print("\n[見方] バックテスト (確定オッズ・EV>=1.3) は 2025年単勝回収158.6% / 2026年100.1%。"
          "実測がこれからどれだけ下振れるか (ドリフト<1.0 = 通知後にオッズが下がる) を確認する。"
          "結果未登録は日曜夜のDB登録後に再実行。")
    conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
