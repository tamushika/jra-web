"""
Web版スコア vs WIN5 MLスコア の的中率・回収率比較
====================================================
各スコアの「1位馬」に単勝100円・複勝100円を賭けた場合の
単勝的中率 / 複勝的中率(3着内) / 単勝回収率 / 複勝回収率 を比較する。
ベースライン: 1番人気。

  - 期間: 2025年(参考※) と 2026年1-6月(アウトオブサンプル)
    ※各スコアの重み/学習は2021-2025データでチューニングされているため
      2025年は楽観バイアスがかかる。判断は2026年を優先する
  - MLスコア: リーク回避のため 2021-2024学習の conditional logit で再現
  - Web版スコア: DB再現可能な主成分 (騎手/枠バイアス(複勝率ベース) + 能力)。
    血統辞典・距離変更・判定ボーナス等は ability.db に父が無く再現不能のため
    含まない (実運用スコアより控えめな近似)
  - 複勝配当: backfill_fukusho_netkeiba.py で充填した runs.fukusho_pay を使用

【使い方】 python backtest_score_compare.py
【前提】  ability.db + fukusho_pay 充填済み
"""
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
from backtest_win5 import load_win5_cfg, score_all_runners, parse_final_odds  # noqa: E402
import scoring  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
PERIODS = [("2025年 (参考: 学習期間と重複)", "20250101", "20251231"),
           ("2026年1-6月 (OOS)", "20260101", "20260630")]


def load_fukusho(conn, date_from, date_to):
    """(date, place, r, umaban) → 複勝配当。レース単位の充填有無判定にも使う"""
    cur = conn.cursor()
    cur.execute("""SELECT date, place, r, umaban, fukusho_pay FROM runs
                   WHERE date >= ? AND date <= ? AND fukusho_pay IS NOT NULL""",
                (date_from, date_to))
    pay = {}
    races_with = set()
    for d, p, r, u, f in cur.fetchall():
        pay[(d, p, r, u)] = f
        races_with.add((d, p, r))
    return pay, races_with


def evaluate_top1(races, fuku_pay, fuku_races, date_from, date_to, min_runners=8):
    """races: {key: [(score, runner), ...]} → スコア1位馬の成績集計"""
    n = win = top3 = 0
    tan_ret = 0.0
    n_fuku = 0
    fuku_ret = 0.0
    for (d, p, r), members in races.items():
        if not (date_from <= d <= date_to) or r is None or len(members) < min_runners:
            continue
        members.sort(key=lambda x: -x[0])
        top = members[0][1]
        if top["rank"] is None:
            continue
        n += 1
        if top["rank"] == 1:
            win += 1
            try:
                tan_ret += float(str(top["win_pay"]).replace(",", ""))
            except (ValueError, TypeError):
                pass
        if top["rank"] <= 3:
            top3 += 1
        if (d, p, r) in fuku_races:
            n_fuku += 1
            if top["rank"] <= 3:
                fuku_ret += fuku_pay.get((d, p, r, top["umaban"]), 0.0)
    return {"n": n, "win_rate": 100.0 * win / n if n else 0,
            "top3_rate": 100.0 * top3 / n if n else 0,
            "tan_roi": tan_ret / n if n else 0,
            "n_fuku": n_fuku, "fuku_roi": fuku_ret / n_fuku if n_fuku else 0}


def main():
    cfg5 = load_win5_cfg()
    web_cfg_path = os.path.join(API_DIR, "data_files", "common", "score_weights.json")
    import json
    with open(web_cfg_path, "r", encoding="utf-8-sig") as f:
        web_cfg = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, "20210101", "20260630")
    fuku_pay, fuku_races = load_fukusho(conn, "20250101", "20260630")
    conn.close()
    print(f"ロード: {len(runs)}行 / 複勝配当あり: {len(fuku_races)}レース")

    # ── MLスコア (2021-2024学習 conditional logit → 2025/2026を採点) ──
    X, y, race_keys, meta = build_dataset(runs, "20210101", cfg5)
    dates = np.array([k[0] for k in race_keys])
    tr = dates <= "20241231"
    scaler = StandardScaler().fit(X[tr])
    w_cl = fit_conditional_logit(scaler.transform(X[tr]), y[tr],
                                 [k for k, t in zip(race_keys, tr) if t])
    ev = ~tr
    scores_ml = scaler.transform(X[ev]) @ w_cl
    races_ml = defaultdict(list)
    for s, k, m in zip(scores_ml, [k for k, t in zip(race_keys, ev) if t],
                       [m for m, t in zip(meta, ev) if t]):
        races_ml[k].append((float(s), m))
    print(f"MLスコア (21-24学習CL): {len(races_ml)}レース採点")

    # ── Web版スコア主成分 (騎手/枠=複勝率ベース + 能力, オッズ不使用) ──
    races_web = score_all_runners(runs, "20250101", web_cfg, rate_key="show_rate")
    print(f"Web版スコア (主成分再現): {len(races_web)}レース採点")

    # ── 1番人気ベースライン ──
    races_pop = defaultdict(list)
    for cur_r in runs:
        if cur_r["date"] < "20250101" or cur_r["rank"] is None:
            continue
        pop = cur_r.get("popularity")
        races_pop[(cur_r["date"], cur_r["place"], cur_r["r"])].append(
            (-float(pop) if pop else -99.0, cur_r))

    for label, d_from, d_to in PERIODS:
        print(f"\n===== {label}: スコア1位馬に単勝/複勝100円 =====")
        print(f"{'scorer':22s} | レース  | 単勝的中 | 複勝的中 | 単勝回収 | 複勝回収(n)")
        for name, races in (("WIN5 MLスコア", races_ml),
                            ("Web版スコア(主成分)", races_web),
                            ("1番人気", races_pop)):
            m = evaluate_top1(races, fuku_pay, fuku_races, d_from, d_to)
            print(f"{name:22s} | {m['n']:6d} | {m['win_rate']:6.1f}% | {m['top3_rate']:6.1f}% |"
                  f" {m['tan_roi']:6.1f}% | {m['fuku_roi']:6.1f}% ({m['n_fuku']})")

    print("\n[注意] Web版スコアは騎手/枠(複勝率)+能力のみのDB再現 (血統辞典・距離変更・"
          "判定ボーナスは父データが無く再現不能 → 実運用より控えめ)。"
          "MLスコアは確定オッズ使用 (実運用は購入時点オッズでやや低下)。"
          "2025年は両者ともチューニング期間と重複するため2026年OOSを優先して判断。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
