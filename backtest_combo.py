"""
組み合わせ馬券 (ワイド・馬連) のEVバックテスト
================================================
conditional logit のレース内勝率から Harville式 (減衰λつき) で
組み合わせ的中確率を導出し、「モデル確率が市場の織り込みを上回る組み合わせ」
を買い続けた場合の実現回収率を検証する。

  - モデル勝率: 21-24年学習CL (血統込み・リーク回避)
  - 減衰λ: 2着以降の Harville 過大評価補正。21-24年の実現2着で最尤推定
  - 市場確率: 単勝オッズ由来の市場勝率に同じHarville式を適用 (組みオッズの近似)
  - 判定: overlay = モデル確率/市場確率 >= 閾値 で購入
  - 実現回収: race_payouts (netkeiba払戻、backfill_pay_netkeiba.py) の実額

【使い方】 python backtest_combo.py
【前提】  race_payouts 充填済み (2025-2026H1)
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
PERIODS = [("2025年 (モデルはOOS)", "20250101", "20251231"),
           ("2026年1-6月 (OOS)", "20260101", "20260630")]
OVERLAYS = [1.0, 1.2, 1.4, 1.6, 2.0]
MIN_PROB = {"馬連": 0.005, "ワイド": 0.02}   # 極小確率の組みは除外 (オッズ誤差が暴れる)
MAX_FIELD = 18


def harville_pair_probs(probs, lam):
    """勝率リスト → (馬連P[(i,j)], ワイドP[(i,j)]) 辞書 (i<j)。
    1着は素の確率 p、2着以降の条件付きは減衰確率 s∝p^λ を使う (Harville補正)。
    ワイドは上位3頭集合の確率を列挙し、含まれる3ペアに加算する。"""
    from itertools import combinations, permutations
    from collections import defaultdict
    z = sum(probs)
    p = [x / z for x in probs]
    sz = sum(x ** lam for x in p)
    s = [x ** lam / sz for x in p]
    n = len(p)

    quinella = {}
    for i in range(n):
        for j in range(i + 1, n):
            quinella[(i, j)] = p[i] * s[j] / (1 - s[i]) + p[j] * s[i] / (1 - s[j])

    wide = defaultdict(float)
    for a, b, c in combinations(range(n), 3):
        pset = 0.0
        for x, y, w in permutations((a, b, c)):
            d1 = 1 - s[x]
            d2 = d1 - s[y]
            if d1 > 0 and d2 > 0:
                pset += p[x] * s[y] / d1 * s[w] / d2
        wide[(a, b)] += pset
        wide[(a, c)] += pset
        wide[(b, c)] += pset
    return quinella, dict(wide)


def fit_lambda(races_train):
    """実現した (1着,2着) の対数尤度で減衰λを最尤推定 (グリッド)"""
    best_lam, best_ll = 1.0, -1e18
    for lam in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        ll = 0.0
        n = 0
        for probs, ranks, _odds in races_train:
            p = np.asarray(probs)
            p = p / p.sum()
            s = p ** lam
            s = s / s.sum()
            try:
                i1 = ranks.index(1)
                i2 = ranks.index(2)
            except ValueError:
                continue
            d = 1 - s[i1]
            if d <= 0 or p[i1] <= 0 or s[i2] <= 0:
                continue
            ll += math.log(p[i1]) + math.log(s[i2] / d)
            n += 1
        print(f"  λ={lam}: 平均loglik {ll/n:.4f} (n={n})")
        if ll > best_ll:
            best_ll, best_lam = ll, lam
    return best_lam


def main():
    cfg5 = load_win5_cfg()
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, "20210101", "20260630")
    cur = conn.cursor()
    payouts = defaultdict(dict)  # (date,place,r) -> {(type,combo): pay}
    for d, p, r, t, c, pay in cur.execute(
            "SELECT date, place, r, bet_type, combo, pay FROM race_payouts"):
        payouts[(d, p, r)][(t, c)] = pay
    conn.close()
    print(f"ロード: {len(runs)}行 / 払戻あり {len(payouts)}レース")

    X, y, race_keys, meta = build_dataset(runs, "20210101", cfg5)
    dates = np.array([k[0] for k in race_keys])
    tr = dates <= "20241231"
    scaler = StandardScaler().fit(X[tr])
    w_cl = fit_conditional_logit(scaler.transform(X[tr]), y[tr],
                                 [k for k, t in zip(race_keys, tr) if t])
    scores = scaler.transform(X) @ w_cl

    # レース単位に整形: (モデル勝率, 着順, 単勝オッズ, 馬番) キーつき
    by_race = defaultdict(list)
    for s, k, m in zip(scores, race_keys, meta):
        by_race[k].append((float(s), m))
    races_data = {}
    for k, mem in by_race.items():
        if len(mem) < 8 or len(mem) > MAX_FIELD:
            continue
        mem.sort(key=lambda x: x[1]["umaban"])
        mx = max(s for s, _ in mem)
        es = [math.exp(s - mx) for s, _ in mem]
        z = sum(es)
        probs = [e / z for e in es]
        ranks = [int(m["rank"]) if m["rank"] else 99 for _, m in mem]
        odds = [parse_final_odds(m.get("win_pay"), m["rank"]) for _, m in mem]
        umaban = [m["umaban"] for _, m in mem]
        if any(o is None or o <= 1.0 for o in odds):
            continue
        races_data[k] = (probs, ranks, odds, umaban)

    # λ推定 (学習期間)
    train_races = [(v[0], v[1], v[2]) for k, v in races_data.items() if k[0] <= "20241231"]
    print(f"λ推定 (21-24年, {len(train_races)}レース):")
    lam = fit_lambda(train_races)
    print(f"→ λ = {lam}")

    # ── バックテスト ──
    for label, d_from, d_to in PERIODS:
        eval_races = {k: v for k, v in races_data.items()
                      if d_from <= k[0] <= d_to and (k[0], k[1], k[2]) in payouts}
        print(f"\n===== {label}: 対象 {len(eval_races)}レース (払戻あり) =====")
        for bet_type in ("ワイド", "馬連"):
            print(f"--- {bet_type} (100円/点) ---")
            print("  overlay | 賭け数  | 的中   | 的中率 | 回収率")
            results = {th: {"n": 0, "hit": 0, "ret": 0.0} for th in OVERLAYS}
            results_top1 = {th: {"n": 0, "hit": 0, "ret": 0.0} for th in OVERLAYS}
            for k, (probs, ranks, odds, umaban) in eval_races.items():
                q_model, w_model = harville_pair_probs(probs, lam)
                inv = [1.0 / o for o in odds]
                z = sum(inv)
                mkt = [v / z for v in inv]
                q_mkt, w_mkt = harville_pair_probs(mkt, lam)
                model_map = w_model if bet_type == "ワイド" else q_model
                mkt_map = w_mkt if bet_type == "ワイド" else q_mkt
                pay_map = payouts[(k[0], k[1], k[2])]
                best = None  # レース内 overlay 最大の1点 (通知運用の想定)
                for (i, j), mp in model_map.items():
                    if mp < MIN_PROB[bet_type]:
                        continue
                    mk = mkt_map[(i, j)]
                    if mk <= 0:
                        continue
                    overlay = mp / mk
                    combo = f"{min(umaban[i], umaban[j])}-{max(umaban[i], umaban[j])}"
                    pay = pay_map.get((bet_type, combo), 0.0)
                    if best is None or overlay > best[0]:
                        best = (overlay, pay)
                    for th in OVERLAYS:
                        if overlay >= th:
                            b = results[th]
                            b["n"] += 1
                            if pay:
                                b["hit"] += 1
                                b["ret"] += pay
                if best is not None:
                    for th in OVERLAYS:
                        if best[0] >= th:
                            b = results_top1[th]
                            b["n"] += 1
                            if best[1]:
                                b["hit"] += 1
                                b["ret"] += best[1]
            for th in OVERLAYS:
                b = results[th]
                if not b["n"]:
                    continue
                print(f"  {th:7.1f} | {b['n']:6d} | {b['hit']:5d} | "
                      f"{100.0*b['hit']/b['n']:5.1f}% | {b['ret']/b['n']:6.1f}%")
            print("  [1レース1点 (overlay最大のみ購入)]")
            for th in OVERLAYS:
                b = results_top1[th]
                if not b["n"]:
                    continue
                print(f"  {th:7.1f} | {b['n']:6d} | {b['hit']:5d} | "
                      f"{100.0*b['hit']/b['n']:5.1f}% | {b['ret']/b['n']:6.1f}%")

    print("\n[注意] 市場確率は単勝オッズからのHarville近似 (実際の組みオッズとは乖離あり)。"
          "回収率は実払戻ベースで正確。確定オッズ基準のため実運用では低下方向。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
