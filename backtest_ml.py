"""
重み付けの機械学習化の検証
============================
現行スコアの各成分 + 追加素性を特徴量に、勝ち馬予測のロジスティック回帰を学習し、
手調整の現行スコアとアウトオブサンプル (時系列分割) で勝ち馬カバレッジを比較する。

  学習: 2021-2024 / 検証: 2025 (学習に一切使わない)

特徴量はすべてライブで計算可能なもの (バックテスト再現可能な範囲) に限定:
  騎手/枠バイアス点・タイム指数(馬場差込み)・クラス実績・上がり質・市場(ln odds)・
  前走着順・出走間隔・年齢・性別・斤量・前走馬体重・キャリア数(直近窓)・道悪適性

【使い方】
  python backtest_ml.py            # 時系列分割で検証 (学習2021-24 / 検証2025)
  python backtest_ml.py --write    # 検証後、全期間(2021-2025)で再学習して
                                   # api/data_files/common/win5_ml_model.json に出力
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from backtest_ability import load_runs, RECENCY, TIME_DIFF_CLIP, CLASS_RANK  # noqa: E402
from backtest_win5 import (load_win5_cfg, load_upset_map, score_all_runners,
                           measure_coverage, parse_final_odds)  # noqa: E402
from backtest_score import JockeyMatcher  # noqa: E402
from past_data_service import calculate_waku  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

TRAIN_FROM, TRAIN_TO = "20210101", "20241231"
TEST_FROM, TEST_TO = "20250101", "20251231"

FEATURES = ["j_pts", "f_pts", "tfeat", "cfeat", "agari_flag", "ln_odds",
            "prev_rank", "ln_interval", "age", "is_male", "kinryo", "weight",
            "n_prior", "wet_match"]

WET = {"稍", "重", "不"}
MODEL_PATH = os.path.join(API_DIR, "data_files", "common", "win5_ml_model.json")


def build_dataset(runs, date_from, cfg):
    """評価出走ごとに特徴量ベクトルを構築 (score_all_runners と同じ部品を使用)"""
    params = cfg["params"]
    weights = cfg["weights"]
    ab = params["ability"]
    pos_factor = {int(k): v for k, v in ab["pos_factor"].items()}
    use_variant = ab.get("track_variant", False)

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    ftables = {}

    def get_ftable(place, track, dist):
        key = (place, track, dist)
        if key not in ftables:
            ftables[key] = scoring.load_factor_table(place, track, dist, API_DIR)
        return ftables[key]

    jmatcher = JockeyMatcher()
    X, y, race_keys, meta = [], [], [], []

    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < date_from or cur["rank"] is None:
                continue
            prior = lst[max(0, i - 4):i][::-1]
            if not prior:
                continue

            f = dict.fromkeys(FEATURES, 0.0)

            # バイアス (騎手/枠) — 現行と同じ点数を特徴量として使用
            table = get_ftable(cur["place"], cur["track_type"], cur["distance"])
            if table:
                baseline = table["baseline"].get("win_rate")
                if baseline is not None:
                    row = jmatcher.match(table.get("jockey_w") or {}, cur["jockey"])
                    if row is not None:
                        p, _ = scoring._factor_points(row, baseline, params,
                                                      weights.get("jockey_w", 0), "win_rate")
                        if p is not None:
                            f["j_pts"] = p
                    waku = calculate_waku(cur["umaban"], cur["total_horses"])
                    if waku:
                        row = (table.get("frame") or {}).get(f"{waku}枠")
                        if row is not None:
                            p, _ = scoring._factor_points(row, baseline, params,
                                                          weights.get("frame", 0), "win_rate")
                            if p is not None:
                                f["f_pts"] = p

            # 能力系
            tbest, cbest, amin = None, None, None
            for j, pr in enumerate(prior):
                rw = RECENCY[j] if j < len(RECENCY) else RECENCY[-1]
                if pr["time_sec"] and pr["condition"]:
                    base = scoring.std_time(pr["place"], pr["track_type"], pr["distance"],
                                            pr["race_class"], pr["condition"])
                    if base is not None:
                        tv = scoring.track_variant(pr["date"], pr["place"],
                                                   pr["track_type"]) if use_variant else 0.0
                        diff = max(-TIME_DIFF_CLIP, min(TIME_DIFF_CLIP, base + tv - pr["time_sec"]))
                        v = diff * rw
                        tbest = v if tbest is None else max(tbest, v)
                pf = pos_factor.get(pr["rank"], 0.0)
                if pf:
                    v = CLASS_RANK.get(pr["race_class"], 0) * pf * rw
                    cbest = v if cbest is None else max(cbest, v)
                if pr.get("agari_ratio") is not None:
                    amin = pr["agari_ratio"] if amin is None else min(amin, pr["agari_ratio"])
            f["tfeat"] = tbest if tbest is not None else 0.0
            f["cfeat"] = cbest if cbest is not None else 0.0
            f["agari_flag"] = 1.0 if (amin is not None and amin <= ab["agari_best_ratio"]) else 0.0

            # 市場
            odds = parse_final_odds(cur.get("win_pay"), cur["rank"])
            f["ln_odds"] = math.log(odds) if odds and odds > 1.0 else math.log(30.0)

            # 追加素性
            f["prev_rank"] = min(prior[0]["rank"], 18) if prior[0]["rank"] else 10
            try:
                d1 = datetime.strptime(cur["date"], "%Y%m%d")
                d0 = datetime.strptime(prior[0]["date"], "%Y%m%d")
                f["ln_interval"] = math.log(max((d1 - d0).days, 1))
            except ValueError:
                f["ln_interval"] = math.log(30)
            f["age"] = cur.get("age") or 4
            f["is_male"] = 1.0 if cur.get("sex") in ("牡", "セ") else 0.0
            f["kinryo"] = cur.get("kinryo") or 55.0
            f["weight"] = cur.get("weight") or 470
            f["n_prior"] = len(prior)

            # 道悪適性 (当日道悪 × 相対適性。backtest_wetで検証済みの定義)
            if cur.get("condition") in WET:
                wet_ranks = [p["rank"] for p in prior if p["condition"] in WET and p["rank"]]
                dry_ranks = [p["rank"] for p in prior if p["condition"] == "良" and p["rank"]]
                if wet_ranks and dry_ranks:
                    gap = min(wet_ranks) - min(dry_ranks)
                    if gap <= -2:
                        f["wet_match"] = 1.0
                    elif gap >= 2:
                        f["wet_match"] = -1.0

            X.append([f[k] for k in FEATURES])
            y.append(1 if cur["rank"] == 1 else 0)
            race_keys.append((cur["date"], cur["place"], cur["r"]))
            meta.append(cur)
    return np.array(X), np.array(y), race_keys, meta


def coverage_from_scores(scores, race_keys, meta, upset_map):
    races = defaultdict(list)
    for s, key, m in zip(scores, race_keys, meta):
        races[key].append((float(s), m))
    return measure_coverage(races, upset_map)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="全期間で再学習してモデルJSONを出力")
    args = ap.parse_args()

    cfg = load_win5_cfg()
    upset_map = load_upset_map()

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, TRAIN_FROM, TEST_TO)
    conn.close()
    print(f"ロード: {len(runs)}行")

    X, y, race_keys, meta = build_dataset(runs, TRAIN_FROM, cfg)
    dates = np.array([k[0] for k in race_keys])
    tr = dates <= TRAIN_TO
    te = dates >= TEST_FROM
    print(f"特徴量: {len(FEATURES)}次元 / 学習 {tr.sum()}出走 / 検証 {te.sum()}出走")

    scaler = StandardScaler().fit(X[tr])
    model = LogisticRegression(max_iter=2000, C=1.0)
    model.fit(scaler.transform(X[tr]), y[tr])

    print("\n===== 学習された係数 (標準化後、絶対値順) =====")
    coefs = sorted(zip(FEATURES, model.coef_[0]), key=lambda x: -abs(x[1]))
    for name, c in coefs:
        print(f"  {name:12s}: {c:+.3f}")

    # ── 検証セットでのカバレッジ比較 ──
    # (1) ML
    proba = model.decision_function(scaler.transform(X[te]))
    te_keys = [k for k, t in zip(race_keys, te) if t]
    te_meta = [m for m, t in zip(meta, te) if t]
    cov_ml, n_ml = coverage_from_scores(proba, te_keys, te_meta, upset_map)

    # (2) 現行の手調整スコア (同じ検証期間。履歴なし馬の扱いが僅かに異なる点は誤差)
    races_cur = score_all_runners(runs, TEST_FROM, cfg)
    cov_cur, n_cur = measure_coverage(races_cur, upset_map)

    print(f"\n===== 検証セット (2025年) の勝ち馬カバレッジ =====")
    print(f"{'model':24s} | k=1   k=2   k=3   k=4   (n)")
    for label, cov, n in (("現行 (手調整)", cov_cur, n_cur), ("ロジスティック回帰", cov_ml, n_ml)):
        d = cov.get("default", [])
        row = "  ".join(f"{c*100:4.1f}" for c in d[:4]) if d else "n/a"
        print(f"{label:24s} | {row}  ({n.get('default_all', 0)})")

    # ── 本番モデル出力 (全期間で再学習) ──
    if args.write:
        scaler_f = StandardScaler().fit(X)
        model_f = LogisticRegression(max_iter=2000, C=1.0)
        model_f.fit(scaler_f.transform(X), y)
        out = {
            "meta": {
                "trained_at": datetime.now().strftime("%Y-%m-%d"),
                "train_period": f"{TRAIN_FROM}-{TEST_TO}",
                "n_samples": int(len(y)),
                "oos_note": f"時系列検証(学習21-24/検証25): ML k1-4 = "
                            f"{[round(c*100,1) for c in cov_ml.get('default', [])[:4]]} vs "
                            f"手調整 {[round(c*100,1) for c in cov_cur.get('default', [])[:4]]}",
                "missing_fill": {"ln_odds": "ln(30)", "prev_rank": 10, "ln_interval": "ln(30)",
                                 "age": 4, "kinryo": 55.0, "weight": 470},
            },
            "features": FEATURES,
            "mean": [round(float(v), 6) for v in scaler_f.mean_],
            "sd": [round(float(v), 6) for v in scaler_f.scale_],
            "coef": [round(float(v), 6) for v in model_f.coef_[0]],
            "display_scale": 10.0,
        }
        with open(MODEL_PATH, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=1)
        print(f"\n[OK] 本番モデル (全期間再学習, n={len(y)}) -> {MODEL_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
