"""
期待値レース監視 (jra_ev.py) のピックアップ条件のバックテスト
================================================================
EV = ML推定勝率(conditional logit softmax) × 確定単勝オッズ が閾値以上の馬を
単勝100円で買い続けた場合の的中率・回収率を検証する (複勝も参考表示)。

  - jra_ev.py と同じフィルタ: EV >= 閾値 / オッズ <= 50倍 / 勝率 >= 2%
  - MLスコア: リーク回避のため 2021-2024学習の conditional logit で再現
  - 期間: 2025年(参考: 学習期間と重複) / 2026年1-6月(OOS)
  - 複勝回収は backfill_fukusho_netkeiba.py 充填済みレースのみ

【注意】バックテストは確定オッズ基準。実運用 (15分前/5分前のオッズ) では
        オッズが変動するためEV判定・回収率ともにズレる。

【使い方】 python backtest_ev.py
"""
import math
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from sklearn.preprocessing import StandardScaler  # noqa: E402

from backtest_ability import load_runs  # noqa: E402
from backtest_ml import build_dataset, fit_conditional_logit  # noqa: E402
from backtest_win5 import load_win5_cfg, parse_final_odds  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
THRESHOLDS = [1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
MAX_ODDS = 50.0
MIN_PROB = 0.02
PERIODS = [("2025年 (参考: 学習期間と重複)", "20250101", "20251231"),
           ("2026年1-6月 (OOS)", "20260101", "20260630")]


def main():
    cfg5 = load_win5_cfg()
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, "20210101", "20260630")
    cur = conn.cursor()
    cur.execute("""SELECT date, place, r, umaban, fukusho_pay FROM runs
                   WHERE fukusho_pay IS NOT NULL""")
    fuku_pay = {(d, p, r, u): f for d, p, r, u, f in cur.fetchall()}
    fuku_races = {(d, p, r) for d, p, r, _u in
                  {(k[0], k[1], k[2], None) for k in fuku_pay}}
    fuku_races = {(k[0], k[1], k[2]) for k in fuku_pay}
    conn.close()
    print(f"ロード: {len(runs)}行 / 複勝配当あり: {len(fuku_races)}レース")

    X, y, race_keys, meta = build_dataset(runs, "20210101", cfg5)
    dates = np.array([k[0] for k in race_keys])
    tr = dates <= "20241231"
    scaler = StandardScaler().fit(X[tr])
    w_cl = fit_conditional_logit(scaler.transform(X[tr]), y[tr],
                                 [k for k, t in zip(race_keys, tr) if t])
    ev_mask = ~tr
    scores = scaler.transform(X[ev_mask]) @ w_cl
    ev_keys = [k for k, t in zip(race_keys, ev_mask) if t]
    ev_meta = [m for m, t in zip(meta, ev_mask) if t]

    # レース内softmax勝率
    races = defaultdict(list)
    for s, k, m in zip(scores, ev_keys, ev_meta):
        races[k].append((float(s), m))
    bets_all = []  # (date, key, prob, odds, ev, rank, win_pay, umaban)
    for key, mem in races.items():
        mx = max(s for s, _ in mem)
        es = [math.exp(s - mx) for s, _ in mem]
        z = sum(es)
        for (s, m), e in zip(mem, es):
            p = e / z
            odds = parse_final_odds(m.get("win_pay"), m["rank"])
            if not odds or odds <= 1.0:
                continue
            ev = p * odds
            bets_all.append((key[0], key, p, odds, ev, m["rank"],
                             m.get("win_pay"), m["umaban"]))
    print(f"評価レース: {len(races)} / 候補馬: {len(bets_all)}")

    for label, d_from, d_to in PERIODS:
        print(f"\n===== {label}: EV条件該当馬に単勝100円 (オッズ<=50倍, 勝率>=2%) =====")
        print("EV閾値 | 賭け数  | 的中   | 的中率 | 単勝回収 | 平均オッズ | 複勝回収(n)")
        for th in THRESHOLDS:
            picks = [b for b in bets_all
                     if d_from <= b[0] <= d_to and b[4] >= th
                     and b[3] <= MAX_ODDS and b[2] >= MIN_PROB]
            if not picks:
                print(f"  {th:4.1f} | 該当なし")
                continue
            n = len(picks)
            wins = [b for b in picks if b[5] == 1]
            tan_ret = 0.0
            for b in wins:
                try:
                    tan_ret += float(str(b[6]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            avg_odds = sum(b[3] for b in picks) / n
            fuku_n, fuku_ret = 0, 0.0
            for b in picks:
                d, key = b[0], b[1]
                if (key[0], key[1], key[2]) in fuku_races:
                    fuku_n += 1
                    if b[5] <= 3:
                        fuku_ret += fuku_pay.get((key[0], key[1], key[2], b[7]), 0.0)
            print(f"  {th:4.1f} | {n:6d} | {len(wins):5d} | {100.0*len(wins)/n:5.1f}% |"
                  f" {tan_ret/n:7.1f}% | {avg_odds:7.1f}倍 |"
                  f" {fuku_ret/fuku_n if fuku_n else 0:6.1f}% ({fuku_n})")

    print("\n[注意] 確定オッズ基準 (実運用の15分前/5分前オッズとはズレる)。"
          "2025年は学習期間と重複するため2026年OOSを優先して判断。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
