"""
能力スコアの per-runner バックテスト
=====================================
ability.db (1980.csv由来・馬名つき) を使い、各出走馬の「その時点までの直近4走」
から能力スコアを再現計算し、実際の着順・配当と照合する。
種牡馬・騎手系と異なり、馬名で履歴を連結できるため本物のバックテスト。

【使い方】
  python backtest_ability.py                 # 2024-2025 レポート
  python backtest_ability.py --grid          # time_k/class_k/agari_bonus グリッド
  python backtest_ability.py --grid --write  # 最良パラメータを score_weights.json へ

【設計メモ】
  - 基準タイムは 2016-2023 集計 (venue_standard_times.json) → 評価期間へのリーク無し
  - 上がり順位はレース内で再計算 (JRA表示の「上がりN位/頭数」を再現)
  - グリッドは per-runner 特徴量を先に抽出して線形結合で回すため高速
"""
import argparse
import itertools
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
WEIGHTS_PATH = os.path.join(API_DIR, "data_files", "common", "score_weights.json")

RECENCY = [1.0, 0.85, 0.7, 0.55]
TIME_DIFF_CLIP = 4.0  # 秒 (time_clamp/time_k のデフォルト比)

CLASS_RANK = {"重賞": 70, "オープン": 60, "3勝クラス": 50, "2勝クラス": 40,
              "1勝クラス": 30, "未勝利": 20, "新馬": 10}
POS_FACTOR = {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.25, 5: 0.25}


def load_runs(conn, date_from, date_to):
    """評価期間 + 遡り用に3年前からロードし、レース内上がり順位を付与"""
    lookback = str(int(date_from[:4]) - 3) + date_from[4:]
    cur = conn.cursor()
    cur.execute(
        """SELECT date, place, r, race_name, race_class, horse, total_horses,
                  popularity, rank, track_type, distance, condition,
                  time_sec, agari, win_pay, jockey, umaban, pci
           FROM runs WHERE date >= ? AND date <= ?""",
        (lookback, date_to),
    )
    cols = [d[0] for d in cur.description]
    runs = [dict(zip(cols, row)) for row in cur.fetchall()]

    # レース内上がり順位
    by_race = defaultdict(list)
    for r in runs:
        by_race[(r["date"], r["place"], r["r"])].append(r)
    for members in by_race.values():
        with_agari = sorted((m for m in members if m["agari"]), key=lambda m: m["agari"])
        n = len(with_agari)
        for rank_i, m in enumerate(with_agari, start=1):
            m["agari_ratio"] = rank_i / n if n else None
        for m in members:
            m.setdefault("agari_ratio", None)
    return runs


def extract_features(runs, date_from, use_variant=False):
    """
    各評価出走に対し、直近4走から能力特徴量を抽出。
    use_variant: 馬場差 (track_variants.json) でタイムを補正するか
    戻り値: [{date, rank, popularity, win_pay, tfeat, cfeat, agari_min}, ...]
      tfeat = max_i clip(基準タイム差, ±4秒) × recency_i   (time_k を掛ける前)
      cfeat = max_i クラス点(10-70) × 着順係数 × recency_i  (class_k を掛ける前)
      agari_min = 直近4走の上がり順位比率の最小値
    """
    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda r: r["date"])

    feats = []
    n_no_hist = 0
    for horse, lst in by_horse.items():
        for i, cur_run in enumerate(lst):
            if cur_run["date"] < date_from or cur_run["rank"] is None:
                continue
            prior = lst[max(0, i - 4):i][::-1]  # 直近4走 (新しい順)
            if not prior:
                n_no_hist += 1
                continue
            tfeat = None
            cfeat = None
            agari_min = None
            for j, pr in enumerate(prior):
                rw = RECENCY[j] if j < len(RECENCY) else RECENCY[-1]
                # タイム (use_variant時は馬場差補正)
                if pr["time_sec"] and pr["condition"]:
                    base = scoring.std_time(pr["place"], pr["track_type"], pr["distance"],
                                            pr["race_class"], pr["condition"])
                    if base is not None:
                        tv = scoring.track_variant(pr["date"], pr["place"],
                                                   pr["track_type"]) if use_variant else 0.0
                        diff = max(-TIME_DIFF_CLIP,
                                   min(TIME_DIFF_CLIP, base + tv - pr["time_sec"]))
                        v = diff * rw
                        tfeat = v if tfeat is None else max(tfeat, v)
                # クラス実績
                pf = POS_FACTOR.get(pr["rank"], 0.0)
                if pf:
                    v = CLASS_RANK.get(pr["race_class"], 0) * pf * rw
                    cfeat = v if cfeat is None else max(cfeat, v)
                # 上がり
                if pr.get("agari_ratio") is not None:
                    agari_min = pr["agari_ratio"] if agari_min is None else min(agari_min, pr["agari_ratio"])
            feats.append({
                "rank": cur_run["rank"], "popularity": cur_run["popularity"],
                "win_pay": cur_run["win_pay"],
                "tfeat": tfeat, "cfeat": cfeat, "agari_min": agari_min,
            })
    return feats, n_no_hist


def ability_points(f, time_k, class_k, agari_bonus, agari_ratio=0.3):
    pts = 0.0
    used = False
    if f["tfeat"] is not None:
        pts += f["tfeat"] * time_k
        used = True
    if f["cfeat"] is not None:
        pts += f["cfeat"] * class_k
        used = True
    if f["agari_min"] is not None and f["agari_min"] <= agari_ratio:
        pts += agari_bonus
        used = True
    return pts if used else None


def parse_win_payout(pay, rank):
    if rank != 1:
        return 0.0
    s = str(pay or "").strip().replace(",", "")
    if not s or s.startswith("("):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def bucket_report(scored, n_buckets=5):
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    rows = []
    for i in range(n_buckets):
        chunk = scored[i * n // n_buckets:(i + 1) * n // n_buckets]
        runs = len(chunk)
        top3 = sum(1 for _, f in chunk if f["rank"] <= 3)
        wins = sum(1 for _, f in chunk if f["rank"] == 1)
        payout = sum(parse_win_payout(f["win_pay"], f["rank"]) for _, f in chunk)
        rows.append({
            "bucket": i + 1, "runs": runs,
            "smin": chunk[0][0], "smax": chunk[-1][0],
            "top3": 100.0 * top3 / runs, "win": 100.0 * wins / runs,
            "roi": 100.0 * payout / (100.0 * runs),
        })
    return rows


def objective(rows):
    rates = [r["top3"] for r in rows]
    mono = sum(1 for a, b in zip(rates, rates[1:]) if b > a)
    return mono * 10 + (rates[-1] - rates[0]), rows[-1]["roi"]


def pop_crosstab(scored):
    scored.sort(key=lambda x: x[0])
    half = len(scored) // 2
    parts = [("スコア下位半分", scored[:half]), ("スコア上位半分", scored[half:])]
    bands = [("1-3人気", 1, 3), ("4-8人気", 4, 8), ("9人気-", 9, 99)]
    print("\n===== 人気帯クロス集計 (市場を超えるリフトの確認) =====")
    for label, lo, hi in bands:
        line = f"  {label:8s}"
        for pl, part in parts:
            sel = [f for _, f in part if f["popularity"] and lo <= f["popularity"] <= hi]
            if sel:
                t3 = sum(1 for f in sel if f["rank"] <= 3)
                line += f" | {pl}: 複{100.0 * t3 / len(sel):5.1f}% (n={len(sel)})"
            else:
                line += f" | {pl}: -"
        print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20240101")
    ap.add_argument("--to", dest="date_to", default="20251231")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    cfg = scoring.load_score_weights(API_DIR)
    ab = cfg["params"]["ability"]
    cur_params = (ab["time_k"], ab["class_k"], ab["agari_bonus"])
    print(f"期間: {args.date_from}-{args.date_to} / 現行: time_k={cur_params[0]}, "
          f"class_k={cur_params[1]}, agari_bonus={cur_params[2]}")

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.date_from, args.date_to)
    conn.close()
    print(f"ロード: {len(runs)}行 (遡り3年含む)")

    feats, n_no_hist = extract_features(runs, args.date_from)
    print(f"評価対象: {len(feats)}出走 (履歴なし除外 {n_no_hist})")
    n_t = sum(1 for f in feats if f["tfeat"] is not None)
    print(f"タイム特徴量あり: {n_t} ({100.0 * n_t / len(feats):.0f}%)")

    def run_eval(tk, ck, agb):
        scored = []
        for f in feats:
            p = ability_points(f, tk, ck, agb, ab.get("agari_best_ratio", 0.3))
            if p is not None:
                scored.append((p, f))
        return scored

    scored = run_eval(*cur_params)
    rows = bucket_report(scored)
    print(f"\n===== 現行パラメータのバケット別成績 (n={len(scored)}) =====")
    print("バケット | スコア範囲       | 出走   | 複勝率 | 勝率  | 単回収")
    for r in rows:
        print(f"  {r['bucket']} (低→高) | {r['smin']:6.1f}〜{r['smax']:6.1f} | {r['runs']:6d} | "
              f"{r['top3']:5.1f}% | {r['win']:4.1f}% | {r['roi']:5.1f}%")
    pop_crosstab(scored)

    if not args.grid:
        return

    print("\n===== グリッドサーチ =====")
    best = None
    for tk, ck, agb in itertools.product([1.0, 1.5, 2.0, 3.0], [0.06, 0.12, 0.18, 0.24],
                                         [0.0, 1.5, 3.0]):
        rows_g = bucket_report(run_eval(tk, ck, agb))
        sc = objective(rows_g)
        if best is None or sc > best[0]:
            best = (sc, (tk, ck, agb), rows_g)
            print(f"  改善: time_k={tk}, class_k={ck}, agari={agb} -> "
                  f"単調性+リフト={sc[0]:.1f}, 上位単回収={sc[1]:.1f}%")

    _sc, (tk, ck, agb), rows_b = best
    print(f"\n最良: time_k={tk}, class_k={ck}, agari_bonus={agb}")
    for r in rows_b:
        print(f"  {r['bucket']} | 複{r['top3']:5.1f}% | 勝{r['win']:4.1f}% | 単回{r['roi']:5.1f}%")

    if args.write:
        with open(WEIGHTS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        data["params"]["ability"]["time_k"] = tk
        data["params"]["ability"]["class_k"] = ck
        data["params"]["ability"]["agari_bonus"] = agb
        meta = data.get("backtest_meta", {})
        meta["note"] = (meta.get("note", "") +
                        f"。能力: per-runnerバックテスト({args.date_from[:4]}-{args.date_to[:4]}, "
                        f"n={len(scored)})でtime_k={tk}/class_k={ck}/agari={agb}に調整 "
                        f"({datetime.now().strftime('%Y-%m-%d')})")
        data["backtest_meta"] = meta
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] {WEIGHTS_PATH} に反映")
    else:
        print("\n(--write で score_weights.json に反映)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
