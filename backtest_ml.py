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
                           measure_coverage, parse_final_odds,
                           popularity_races)  # noqa: E402
from backtest_score import JockeyMatcher  # noqa: E402
from past_data_service import calculate_waku  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

TRAIN_FROM, TRAIN_TO = "20210101", "20241231"
TEST_FROM, TEST_TO = "20250101", "20251231"

FEATURES = ["j_pts", "f_pts", "tfeat", "cfeat", "agari_flag", "ln_odds",
            "prev_rank", "ln_interval", "age", "is_male", "kinryo", "weight",
            "n_prior", "wet_match",
            # 2026-07 追加パック
            "dist_pts",    # 距離変更バイアス点 (db-keiba)
            "surf_pts",    # コース替わりバイアス点 (db-keiba)
            "affi_pts",    # 所属バイアス点 (db-keiba)
            "pace_fit",    # 型PCI偏差 × メンバー予測ペース (backtest_pace C検証定義)
            "course_fit",  # 直近4走に同場同芝ダ同距離での3着以内あり
            "grade_pts",   # 好走条件判定 ◎3/〇2/△1 (血統以外ルール)
            # 血統 (horse_pedigree バックフィル完了後に有効化。カバレッジ不足時は全0
            # = 定数列 → StandardScaler が scale=1 で無害化し、係数もほぼ0になる)
            "sire_pts"]    # 種牡馬×コースバイアス点 (db-keiba father_w)

PEDIGREE_MIN_COVERAGE = 0.70  # 評価出走の血統判明率がこれ未満なら sire_pts は無効のまま

WET = {"稍", "重", "不"}
PCI_TRACK_MEAN = {"芝": 51.96, "ダート": 45.83}  # backtest_pace.TRACK_MEAN と同値
MODEL_PATH = os.path.join(API_DIR, "data_files", "common", "win5_ml_model.json")


def _pci_dev(run):
    if run.get("pci") is None or run["track_type"] not in PCI_TRACK_MEAN:
        return None
    return run["pci"] - PCI_TRACK_MEAN[run["track_type"]]


def build_dataset(runs, date_from, cfg):
    """評価出走ごとに特徴量ベクトルを構築 (score_all_runners と同じ部品を使用)"""
    from backtest_criteria import (load_filtered_criteria, load_mawari,
                                   build_h, attach_agari_rank)
    import analysis

    params = cfg["params"]
    weights = cfg["weights"]
    ab = params["ability"]
    pos_factor = {int(k): v for k, v in ab["pos_factor"].items()}
    use_variant = ab.get("track_variant", False)

    criteria_map = load_filtered_criteria()
    mawari_map = load_mawari()
    attach_agari_rank(runs)

    # 血統 (horse_pedigree)。カバレッジが閾値未満なら sire_pts は0のまま (安全に退化)
    pedigree = {}
    try:
        import pedigree_store
        pedigree = pedigree_store.load_all()
    except Exception as e:
        print(f"[INFO] 血統読み込みスキップ: {e}")
    eval_horses = {r["horse"] for r in runs if r["horse"] and r["date"] >= date_from}
    ped_cov = (sum(1 for h in eval_horses if h in pedigree) / len(eval_horses)) if eval_horses else 0.0
    use_pedigree = ped_cov >= PEDIGREE_MIN_COVERAGE
    print(f"血統カバレッジ: {100*ped_cov:.1f}% ({len(pedigree)}頭登録) → "
          f"sire_pts {'有効' if use_pedigree else f'無効 (閾値{100*PEDIGREE_MIN_COVERAGE:.0f}%未満)'}")

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # ── 1パス目: レースごとの予測ペース (出走馬の直近4走PCI偏差平均の平均) ──
    race_pre = defaultdict(list)
    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < date_from or cur["rank"] is None:
                continue
            devs = [d for d in (_pci_dev(p) for p in lst[max(0, i - 4):i]) if d is not None]
            if devs:
                race_pre[(cur["date"], cur["place"], cur["r"])].append(sum(devs) / len(devs))
    pred_pace = {k: sum(v) / len(v) for k, v in race_pre.items() if len(v) >= 5}

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

            # バイアス (騎手/枠/距離変更/コース替わり/所属) — db-keiba統計点を特徴量に
            table = get_ftable(cur["place"], cur["track_type"], cur["distance"])
            if table:
                baseline = table["baseline"].get("win_rate")
                if baseline is not None:
                    def bias_pts(fmap, key, w=1.0):
                        row = (fmap or {}).get(key) if key else None
                        if row is None:
                            return None
                        p, _ = scoring._factor_points(row, baseline, params, w, "win_rate")
                        return p

                    row = jmatcher.match(table.get("jockey_w") or {}, cur["jockey"])
                    if row is not None:
                        p, _ = scoring._factor_points(row, baseline, params,
                                                      weights.get("jockey_w", 0), "win_rate")
                        if p is not None:
                            f["j_pts"] = p
                    waku = calculate_waku(cur["umaban"], cur["total_horses"])
                    if waku:
                        p = bias_pts(table.get("frame"), f"{waku}枠", weights.get("frame", 0))
                        if p is not None:
                            f["f_pts"] = p
                    # 距離変更 (前走距離との実差分 → db-keibaの7段階ラベル)
                    d_label = scoring._distance_label(cur["distance"], prior[0]["distance"])
                    p = bias_pts(table.get("distance"), scoring._norm(d_label) if d_label else None)
                    if p is not None:
                        f["dist_pts"] = p
                    # コース替わり (前走芝ダ → 今回芝ダ)
                    s_label = (f"{'芝' if prior[0]['track_type'] == '芝' else 'ダ'}"
                               f"→{'芝' if cur['track_type'] == '芝' else 'ダ'}")
                    p = bias_pts(table.get("surface"), scoring._norm(s_label))
                    if p is not None:
                        f["surf_pts"] = p
                    # 所属 (美浦/栗東)
                    affi = "美浦" if "美" in str(cur.get("affi") or "") else \
                           ("栗東" if "栗" in str(cur.get("affi") or "") else None)
                    p = bias_pts(table.get("stable_trainer"), scoring._norm(affi) if affi else None)
                    if p is not None:
                        f["affi_pts"] = p
                    # 種牡馬 (father_w)。血統カバレッジが閾値以上のときのみ
                    if use_pedigree:
                        ped = pedigree.get(horse)
                        if ped and ped.get("sire"):
                            row = scoring._match_entity(table.get("father_w"), ped["sire"])
                            if row is not None:
                                p, _ = scoring._factor_points(row, baseline, params, 1.0, "win_rate")
                                if p is not None:
                                    f["sire_pts"] = p

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

            # ペース適性: 型PCI偏差 × メンバー予測ペース (backtest_pace C定義)
            pv = pred_pace.get((cur["date"], cur["place"], cur["r"]))
            devs = [d for d in (_pci_dev(p) for p in prior) if d is not None]
            if pv is not None and devs:
                f["pace_fit"] = (sum(devs) / len(devs)) * pv

            # 同コース実績: 直近4走に同場・同芝ダ・同距離で3着以内
            f["course_fit"] = 1.0 if any(
                p["place"] == cur["place"] and p["track_type"] == cur["track_type"]
                and p["distance"] == cur["distance"] and p["rank"] and p["rank"] <= 3
                for p in prior) else 0.0

            # 好走条件判定 (criteria.csv 血統以外ルール): ◎3 / 〇2 / △1
            crits = criteria_map.get(cur["place"])
            if crits:
                h_c = build_h(cur, prior[0])
                if use_pedigree:
                    ped = pedigree.get(horse)
                    if ped:  # 血統ルール評価の準備 (現状criteria_mapは血統以外のみ)
                        h_c["sire"] = ped.get("sire") or "-"
                        h_c["bms"] = ped.get("bms") or "-"
                r_ctx = {"type": cur["track_type"], "dist": cur["distance"],
                         "venue": cur["place"], "total_horses": cur["total_horses"],
                         "class": cur["race_class"]}
                grade, _det, _mt = analysis.evaluate_ultra(h_c, r_ctx, crits, {}, mawari_map)
                f["grade_pts"] = {"◎": 3.0, "〇": 2.0, "△": 1.0}.get(grade, 0.0)

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


def fit_conditional_logit(Xz, y, race_keys, l2=1.0, max_iter=300):
    """
    conditional logit (レース内softmax / Plackett-Luce top-1) を L-BFGS で学習。
    通常のロジスティック回帰と違い「同レースの他馬との相対比較」を直接最適化する。
    softmax(w·x) がそのままレース内勝率になる (確率キャリブレーション込み)。
    同着は勝ち馬ごとに1イベントとして扱う。勝ち馬が特徴量構築から漏れたレース
    (勝ち馬が初出走等) は尤度に寄与しない。
    """
    from scipy.optimize import minimize

    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    Xs, ys, rs = Xz[order], np.asarray(y)[order].astype(float), rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rs)]))
    nwin = np.add.reduceat(ys, starts)          # レースごとの勝ち馬数 (通常1, 同着2)
    xwin_sum = Xs[ys == 1].sum(axis=0)

    def negll(w):
        s = Xs @ w
        smax = np.maximum.reduceat(s, starts)
        e = np.exp(s - smax[seg])
        z = np.add.reduceat(e, starts)
        lse = smax + np.log(z)
        ll = float(s[ys == 1].sum() - (nwin * lse).sum()) - 0.5 * l2 * float(w @ w)
        p = e / z[seg]
        grad = Xs.T @ (p * nwin[seg]) - xwin_sum + l2 * w
        return -ll, grad

    res = minimize(negll, np.zeros(Xs.shape[1]), jac=True,
                   method="L-BFGS-B", options={"maxiter": max_iter})
    if not res.success:
        print(f"  [WARN] conditional logit 収束警告: {res.message}")
    return res.x


def fit_temperature(scores, y, race_keys, grid=None):
    """softmax温度 τ をキャリブレーション用データの条件付き対数尤度最大化で決定。
    τ>1 = モデルが過信気味 (確率を平滑化)。"""
    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    s, ys, rs = np.asarray(scores)[order], np.asarray(y)[order].astype(float), rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(rs)]))
    nwin = np.add.reduceat(ys, starts)

    def nll(tau):
        st = s / tau
        smax = np.maximum.reduceat(st, starts)
        lse = smax + np.log(np.add.reduceat(np.exp(st - smax[seg]), starts))
        return -(st[ys == 1].sum() - (nwin * lse).sum())

    grid = grid if grid is not None else np.arange(0.70, 1.61, 0.02)
    best = min(grid, key=nll)
    return float(round(best, 2))


def _group_by_race(Xz, y, race_keys):
    """レース順に整列した (X, y, グループサイズ配列) を返す (LightGBM group用)"""
    rid_map = {}
    rid = np.empty(len(race_keys), dtype=np.int64)
    for i, k in enumerate(race_keys):
        rid[i] = rid_map.setdefault(k, len(rid_map))
    order = np.argsort(rid, kind="stable")
    rs = rid[order]
    starts = np.flatnonzero(np.r_[True, rs[1:] != rs[:-1]])
    sizes = np.diff(np.r_[starts, len(rs)])
    return Xz[order], np.asarray(y)[order], sizes


def fit_lgbm_rank(Xz_tr, y_tr, keys_tr, Xz_va, y_va, keys_va, seed=7):
    """LambdaRank (レース内ランキング学習)。検証セットで早期停止し、
    (booster, best_iteration) を返す。"""
    import lightgbm as lgb
    Xs, ys, gs = _group_by_race(Xz_tr, y_tr, keys_tr)
    Xv, yv, gv = _group_by_race(Xz_va, y_va, keys_va)
    params = {
        "objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [1, 3],
        "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 100,
        "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1,
        "lambdarank_truncation_level": 8, "verbosity": -1, "seed": seed,
    }
    ds_tr = lgb.Dataset(Xs, label=ys, group=gs)
    ds_va = lgb.Dataset(Xv, label=yv, group=gv, reference=ds_tr)
    booster = lgb.train(params, ds_tr, num_boost_round=800, valid_sets=[ds_va],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    return booster, booster.best_iteration


def build_prob_races(scores, race_keys, meta, temp=1.0):
    """スコア → レースごとに softmax(スコア/τ) 勝率を付与。
    戻り値: {key: [(score, prob, meta), ...] スコア降順}"""
    races = defaultdict(list)
    for s, k, m in zip(scores, race_keys, meta):
        races[k].append((float(s), m))
    out = {}
    for k, mem in races.items():
        mem.sort(key=lambda x: -x[0])
        mx = mem[0][0]
        es = [math.exp((s - mx) / temp) for s, _ in mem]
        z = sum(es)
        out[k] = [(s, e / z, m) for (s, m), e in zip(mem, es)]
    return out


def report_calibration(prob_races, kmax=4, min_r=9):
    """予測カバレッジ (上位k頭の勝率和の平均) と実測カバレッジを比較"""
    pred_sum = [0.0] * kmax
    act_cnt = [0] * kmax
    n = 0
    for (date, place, r), mem in prob_races.items():
        if r is None or r < min_r or len(mem) < 8:
            continue
        wpos = next((i + 1 for i, (_, _, m) in enumerate(mem) if m["rank"] == 1), None)
        if wpos is None:
            continue
        n += 1
        cum = 0.0
        for k in range(kmax):
            if k < len(mem):
                cum += mem[k][1]
            pred_sum[k] += min(cum, 1.0)
            if wpos <= k + 1:
                act_cnt[k] += 1
    if not n:
        return
    print(f"\n===== 確率キャリブレーション (検証セット, n={n}) =====")
    print("  k | 予測カバレッジ | 実測カバレッジ")
    for k in range(kmax):
        print(f"  {k+1} | {100.0*pred_sum[k]/n:12.1f}% | {100.0*act_cnt[k]/n:12.1f}%")


def simulate_win5_compare(prob_races, upset_map, coverage, budgets, min_r=9):
    """土日・R降順5レース近似のWIN5で、荒れランク平均配分 vs レース固有確率配分を比較。
    軸固定は rank版=スコア1-2位差最大 / prob版=最大勝率レース を1点固定。"""
    from backtest_win5 import upset_rank_of

    by_day = defaultdict(list)
    for (date, place, r), mem in prob_races.items():
        if r is None or r < min_r or len(mem) < 8:
            continue
        try:
            if datetime.strptime(date, "%Y%m%d").weekday() not in (5, 6):
                continue
        except ValueError:
            continue
        by_day[date].append((r, place, mem))

    strategies = ["rank通常", "rank軸", "prob通常", "prob軸"]
    results = {b: {s: {"hits": 0, "pts": 0} for s in strategies} for b in budgets}
    day_records = {b: [] for b in budgets}  # prob通常の日別記録 (見送り基準分析用)
    n_days = 0

    for date, cands in sorted(by_day.items()):
        if len(cands) < 5:
            continue
        cands.sort(key=lambda x: (-x[0], x[1]))
        sel = cands[:5]
        winner_pos, ranks, prob_lists, margins, top1 = [], [], [], [], []
        winner_odds = []
        ok = True
        for r, place, mem in sel:
            wpos = next((i + 1 for i, (_, _, m) in enumerate(mem) if m["rank"] == 1), None)
            if wpos is None:
                ok = False
                break
            winner_pos.append(wpos)
            m0 = mem[0][2]
            ranks.append(upset_rank_of(upset_map, place, m0["track_type"], m0["distance"]))
            prob_lists.append([p for _, p, _ in mem])
            margins.append(mem[0][0] - mem[1][0] if len(mem) > 1 else 0.0)
            top1.append(mem[0][1])
            wmeta = next(m for _, _, m in mem if m["rank"] == 1)
            odds = parse_final_odds(wmeta.get("win_pay"), 1)
            winner_odds.append(odds)
        if not ok:
            continue
        n_days += 1
        axis_rank = {max(range(5), key=lambda i: margins[i])}
        axis_prob = {max(range(5), key=lambda i: top1[i])}
        odds_proxy = None
        if all(winner_odds):
            odds_proxy = 1.0
            for o in winner_odds:
                odds_proxy *= o

        for b in budgets:
            picks_p, est_p = scoring.allocate_picks_prob(prob_lists, b)
            allocs = {
                "rank通常": scoring.allocate_picks(ranks, coverage, b)[0],
                "rank軸": scoring.allocate_picks(ranks, coverage, b, fixed=axis_rank)[0],
                "prob通常": picks_p,
                "prob軸": scoring.allocate_picks_prob(prob_lists, b, fixed=axis_prob)[0],
            }
            for name, picks in allocs.items():
                results[b][name]["pts"] += scoring._pts_of(picks)
                if all(wp <= k for wp, k in zip(winner_pos, picks)):
                    results[b][name]["hits"] += 1
            day_records[b].append({
                "date": date, "est": est_p, "pts": scoring._pts_of(picks_p),
                "hit": all(wp <= k for wp, k in zip(winner_pos, picks_p)),
                "odds_proxy": odds_proxy,
            })

    print(f"\n===== WIN5配分比較 (検証セット, 土日R降順5R近似, {n_days}日) =====")
    print("点数  | " + " | ".join(f"{s:14s}" for s in strategies))
    for b in budgets:
        cells = []
        for s in strategies:
            rr = results[b][s]
            cells.append(f"{100.0*rr['hits']/n_days:5.1f}% /{rr['pts']/n_days:6.1f}点"
                         if n_days else "n/a")
        print(f"  {b:4d}| " + " | ".join(cells))
    return results, n_days, day_records


def report_skip_analysis(day_records, budgets):
    """見送り基準: 推定的中率が閾値未満の日をスキップした場合の成績。
    回収プロキシ = 的中日の勝ち馬単勝オッズ積 (WIN5配当の近似) × 100円 / 投入点数"""
    for b in budgets:
        recs = day_records.get(b, [])
        if not recs:
            continue
        ests = sorted(r["est"] for r in recs)
        print(f"\n===== 見送り基準スイープ ({b}点, 全{len(recs)}日, "
              f"推定的中率の分布 中央値{100*ests[len(ests)//2]:.1f}% / "
              f"上位25%点{100*ests[int(len(ests)*0.75)]:.1f}%) =====")
        print("  閾値   | 購入日数 | 的中  | 的中率  | 回収プロキシ")
        for th in (0.0, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12):
            buy = [r for r in recs if r["est"] >= th]
            if not buy:
                continue
            hits = [r for r in buy if r["hit"]]
            cost = sum(r["pts"] for r in buy) * 100.0
            ret = sum((r["odds_proxy"] or 0.0) * 100.0 for r in hits)
            roi = 100.0 * ret / cost if cost else 0.0
            print(f"  {100*th:5.1f}% | {len(buy):7d} | {len(hits):4d} | "
                  f"{100.0*len(hits)/len(buy):5.1f}% | {roi:6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="全期間で再学習してモデルJSONを出力")
    ap.add_argument("--lgbm", action="store_true",
                    help="LightGBM LambdaRank も学習して比較 (要 lightgbm)")
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
    Xz_tr = scaler.transform(X[tr])
    Xz_te = scaler.transform(X[te])
    tr_keys = [k for k, t in zip(race_keys, tr) if t]
    te_keys = [k for k, t in zip(race_keys, te) if t]
    te_meta = [m for m, t in zip(meta, te) if t]

    model = LogisticRegression(max_iter=2000, C=1.0)
    model.fit(Xz_tr, y[tr])
    w_cl = fit_conditional_logit(Xz_tr, y[tr], tr_keys)

    print("\n===== 学習された係数 (標準化後、|LR|順): LR vs conditional logit =====")
    coefs = sorted(zip(FEATURES, model.coef_[0], w_cl), key=lambda x: -abs(x[1]))
    for name, c_lr, c_cl in coefs:
        print(f"  {name:12s}: LR {c_lr:+.3f} / CL {c_cl:+.3f}")

    # ── 検証セットでのカバレッジ比較 ──
    proba = model.decision_function(Xz_te)
    scores_cl = Xz_te @ w_cl
    cov_ml, n_ml = coverage_from_scores(proba, te_keys, te_meta, upset_map)
    cov_cl, n_cl = coverage_from_scores(scores_cl, te_keys, te_meta, upset_map)

    # 現行の手調整スコア (同じ検証期間。履歴なし馬の扱いが僅かに異なる点は誤差)
    races_cur = score_all_runners(runs, TEST_FROM, cfg)
    cov_cur, n_cur = measure_coverage(races_cur, upset_map)

    # 人気順ベースライン (同一 runs / TEST_FROM フィルタ = 同一レース集団)
    races_ninki = popularity_races(runs, TEST_FROM)
    cov_ninki, n_ninki = measure_coverage(races_ninki, upset_map)

    print(f"\n===== 検証セット (2025年) の勝ち馬カバレッジ =====")
    print(f"{'model':26s} | k=1   k=2   k=3   k=4   (n)")
    for label, cov, n in (("人気順ベースライン", cov_ninki, n_ninki),
                          ("現行 (手調整)", cov_cur, n_cur),
                          ("ロジスティック回帰", cov_ml, n_ml),
                          ("conditional logit", cov_cl, n_cl)):
        d = cov.get("default", [])
        row = "  ".join(f"{c*100:4.1f}" for c in d[:4]) if d else "n/a"
        print(f"{label:26s} | {row}  ({n.get('default_all', 0)})")

    # ── 温度キャリブレーション (内部分割: 学習21-23 → 温度調整24。検証25は使わない) ──
    inner_tr = dates[tr] <= "20231231"
    w_inner = fit_conditional_logit(Xz_tr[inner_tr], y[tr][inner_tr],
                                    [k for k, t in zip(tr_keys, inner_tr) if t])
    cal = ~inner_tr
    temp = fit_temperature(Xz_tr[cal] @ w_inner, y[tr][cal],
                           [k for k, t in zip(tr_keys, cal) if t])
    print(f"\n温度キャリブレーション (21-23学習→24調整): τ = {temp:.2f} "
          f"({'過信を平滑化' if temp > 1 else '据え置き' if temp == 1 else '先鋭化'})")

    # ── 確率キャリブレーション + WIN5配分比較 (CLのsoftmax勝率, τ適用) ──
    prob_races = build_prob_races(scores_cl, te_keys, te_meta, temp=temp)
    report_calibration(prob_races)
    _res, _nd, day_records = simulate_win5_compare(
        prob_races, upset_map, cov_cl, budgets=[50, 100, 150, 200])
    report_skip_analysis(day_records, budgets=[100, 150, 200])

    # ── LightGBM LambdaRank (オプション比較) ──
    if args.lgbm:
        inner_keys = [k for k, t in zip(tr_keys, inner_tr) if t]
        cal_keys = [k for k, t in zip(tr_keys, cal) if t]
        booster, best_it = fit_lgbm_rank(
            Xz_tr[inner_tr], y[tr][inner_tr], inner_keys,
            Xz_tr[cal], y[tr][cal], cal_keys)
        print(f"\nLightGBM LambdaRank: best_iteration = {best_it}")
        scores_gb = booster.predict(Xz_te, num_iteration=best_it)
        cov_gb, n_gb = coverage_from_scores(scores_gb, te_keys, te_meta, upset_map)
        d_cl = cov_cl.get("default", [0] * 4)
        d_gb = cov_gb.get("default", [0] * 4)
        print("model                      | k=1   k=2   k=3   k=4")
        print(f"{'conditional logit':26s} | " + "  ".join(f"{c*100:4.1f}" for c in d_cl[:4]))
        print(f"{'LightGBM LambdaRank':26s} | " + "  ".join(f"{c*100:4.1f}" for c in d_gb[:4]))
        gain = sorted(zip(FEATURES, booster.feature_importance("gain")), key=lambda x: -x[1])
        print("特徴量重要度(gain上位8):", ", ".join(f"{n}={g:.0f}" for n, g in gain[:8]))
        # 温度 (rankスコアは非確率のためキャリブレーション必須)
        temp_gb = fit_temperature(booster.predict(Xz_tr[cal], num_iteration=best_it),
                                  y[tr][cal], cal_keys,
                                  grid=np.arange(0.2, 3.01, 0.05))
        print(f"LGBM 温度: τ = {temp_gb:.2f}")
        prob_races_gb = build_prob_races(scores_gb, te_keys, te_meta, temp=temp_gb)
        report_calibration(prob_races_gb)
        simulate_win5_compare(prob_races_gb, upset_map, cov_gb, budgets=[50, 100, 150, 200])

    # CL はキャリブレーション済み勝率を提供し、レース固有配分 (WIN5的中率+3pt級) の
    # 前提になるため、LR がカバレッジで 1pt 以上明確に勝る場合のみ LR を採用する
    mean_lr = sum(cov_ml.get("default", [0] * 4)[:4]) / 4
    mean_cl = sum(cov_cl.get("default", [0] * 4)[:4]) / 4
    best_obj = "logistic_regression" if mean_lr - mean_cl > 0.01 else "conditional_logit"
    print(f"\nOOS平均カバレッジ(k1-4): LR {mean_lr*100:.1f}% / CL {mean_cl*100:.1f}% → 採用候補: {best_obj}"
          f" (CLは勝率配分の前提のため1pt差までCL優先)")

    # ── 本番モデル出力 (全期間で再学習) ──
    if args.write:
        scaler_f = StandardScaler().fit(X)
        Xz_f = scaler_f.transform(X)
        if best_obj == "conditional_logit":
            coef_f = fit_conditional_logit(Xz_f, y, race_keys)
        else:
            model_f = LogisticRegression(max_iter=2000, C=1.0)
            model_f.fit(Xz_f, y)
            coef_f = model_f.coef_[0]
        out = {
            "meta": {
                "trained_at": datetime.now().strftime("%Y-%m-%d"),
                "train_period": f"{TRAIN_FROM}-{TEST_TO}",
                "n_samples": int(len(y)),
                "oos_note": f"時系列検証(学習21-24/検証25): CL k1-4 = "
                            f"{[round(c*100,1) for c in cov_cl.get('default', [])[:4]]} / LR "
                            f"{[round(c*100,1) for c in cov_ml.get('default', [])[:4]]} / "
                            f"手調整 {[round(c*100,1) for c in cov_cur.get('default', [])[:4]]}",
                "missing_fill": {"ln_odds": "ln(30)", "prev_rank": 10, "ln_interval": "ln(30)",
                                 "age": 4, "kinryo": 55.0, "weight": 470},
            },
            "objective": best_obj,
            "features": FEATURES,
            "mean": [round(float(v), 6) for v in scaler_f.mean_],
            "sd": [round(float(v), 6) for v in scaler_f.scale_],
            "coef": [round(float(v), 6) for v in coef_f],
            "display_scale": 10.0,
            "prob_temperature": temp,
        }
        with open(MODEL_PATH, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=1)
        print(f"\n[OK] 本番モデル ({best_obj}, 全期間再学習, n={len(y)}) -> {MODEL_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
