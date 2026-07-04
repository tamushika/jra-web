"""
WIN5スコアのバックテスト (勝ち馬カバレッジ計測 + WIN5シミュレーション)
=======================================================================
ability.db (馬名・R番号つき) を使い、WIN5用スコア (勝率ベースbias + 1着偏重能力) で

  1) 「勝ち馬がスコア上位k頭に入る確率 (カバレッジ)」を荒れランク(S/A/B/C)別に計測
     → --write で win5_weights.json の allocation.coverage に書き込み
  2) WIN5シミュレーション: 各日曜の「9R以降で発走が遅い5レース(R降順)」を対象近似とし、
     点数別(50/100/150/200)に全5レース的中率を計測。荒れ度考慮配分 vs 一律配分を比較

【使い方】
  python backtest_win5.py                 # 2024-2025 レポート
  python backtest_win5.py --write         # カバレッジ実測値を win5_weights.json へ

【注意 (レポートにも表示)】
  - 実際のWIN5対象はJRA指定。R番号降順5レースは近似
  - WIN5配当データが無いため回収は勝ち馬単勝オッズ積のプロキシ
  - バックテストのスコアは jockey/frame(勝率ベース) + 能力のみ (種牡馬・判定◎等は
    ability.db に父が無いため再現不能 → 実運用スコアの主成分での検証)
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from backtest_ability import load_runs, RECENCY, TIME_DIFF_CLIP, CLASS_RANK, parse_win_payout  # noqa: E402
from backtest_score import JockeyMatcher  # noqa: E402
from scoring import VENUE_SLUG_MAP  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
WEIGHTS_PATH = os.path.join(API_DIR, "data_files", "common", "win5_weights.json")

UPSET_RANKS = ["S", "A", "B", "C"]


def load_win5_cfg():
    with open(WEIGHTS_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def load_upset_map():
    """全場の course_upset.csv → {(場, 芝ダ, 距離): (upset_rank, upset_score)}
    内外回り等の重複は upset_score が高い方 (荒れ想定側) を採用。場全体行は ('*',) キー。"""
    upset = {}
    root = os.path.join(API_DIR, "data_files")
    for slug, venue in [(v, k) for k, v in VENUE_SLUG_MAP.items() if v != "common"]:
        path = os.path.join(root, slug, "course_upset.csv")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                name = re.sub(r"（[^）]*）", "", str(row.get("course_name", ""))).strip()
                try:
                    score = float(row["upset_score"])
                    rank = str(row["upset_rank"]).strip()
                except (KeyError, ValueError):
                    continue
                m = re.match(rf"^{venue}(芝|ダート)(\d+)m?$", name)
                if m:
                    key = (venue, m.group(1), int(m.group(2)))
                elif name == venue:
                    key = (venue, "*", 0)  # 場全体フォールバック
                else:
                    continue
                if key not in upset or score > upset[key][1]:
                    upset[key] = (rank, score)
    return upset


def upset_rank_of(upset_map, place, track, dist):
    hit = upset_map.get((place, track, dist))
    if hit:
        return hit[0]
    hit = upset_map.get((place, "*", 0))
    return hit[0] if hit else None


def parse_final_odds(win_pay, rank):
    """win_pay列から確定単勝オッズを復元。勝ち馬='260'(配当円)→2.6 / 他='(3.7)'→3.7"""
    s = str(win_pay or "").strip().replace(",", "")
    if not s:
        return None
    if s.startswith("("):
        try:
            return float(s.strip("()"))
        except ValueError:
            return None
    if rank == 1:
        try:
            return float(s) / 100.0
        except ValueError:
            return None
    return None


def score_all_runners(runs, date_from, cfg, rate_key="win_rate"):
    """
    評価期間の全出走に WIN5スコア (bias: jockey/frame + 能力 + 市場) を付与。
    rate_key: バイアス統計の参照率 ("win_rate" / "show_rate")
    戻り値: {(date, place, r): [(score, runner_dict), ...]}
    """
    import math
    params = cfg["params"]
    weights = cfg["weights"]
    mk = params.get("market", {})
    odds_k = mk.get("odds_k", 0.0)
    odds_ref = mk.get("odds_ref", 10.0)
    odds_clamp = mk.get("clamp", 10.0)
    ab = params["ability"]
    pos_factor = {int(k): v for k, v in ab["pos_factor"].items()}
    time_k, class_k = ab["time_k"], ab["class_k"]
    agari_ratio, agari_bonus = ab["agari_best_ratio"], ab["agari_bonus"]

    # 馬ごとの時系列
    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # ファクター表キャッシュ (勝率ベースで参照)
    ftables = {}

    def get_ftable(place, track, dist):
        key = (place, track, dist)
        if key not in ftables:
            ftables[key] = scoring.load_factor_table(place, track, dist, API_DIR)
        return ftables[key]

    jmatcher = JockeyMatcher()
    races = defaultdict(list)

    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < date_from or cur["rank"] is None:
                continue
            total = 0.0

            # ── bias (騎手/枠, 勝率ベース) ──
            table = get_ftable(cur["place"], cur["track_type"], cur["distance"])
            if table:
                baseline = table["baseline"].get(rate_key)
                if baseline is not None:
                    row = jmatcher.match(table.get("jockey_w") or {}, cur["jockey"])
                    if row is not None:
                        p, _ = scoring._factor_points(row, baseline, params,
                                                      weights.get("jockey_w", 0), rate_key)
                        if p is not None:
                            total += p
                    from past_data_service import calculate_waku
                    waku = calculate_waku(cur["umaban"], cur["total_horses"])
                    if waku:
                        row = (table.get("frame") or {}).get(f"{waku}枠")
                        if row is not None:
                            p, _ = scoring._factor_points(row, baseline, params,
                                                          weights.get("frame", 0), rate_key)
                            if p is not None:
                                total += p

            # ── 能力 (直近4走) ──
            prior = lst[max(0, i - 4):i][::-1]
            tbest, cbest, amin = None, None, None
            for j, pr in enumerate(prior):
                rw = RECENCY[j] if j < len(RECENCY) else RECENCY[-1]
                if pr["time_sec"] and pr["condition"]:
                    base = scoring.std_time(pr["place"], pr["track_type"], pr["distance"],
                                            pr["race_class"], pr["condition"])
                    if base is not None:
                        tv = scoring.track_variant(pr["date"], pr["place"], pr["track_type"]) \
                            if ab.get("track_variant") else 0.0
                        diff = max(-TIME_DIFF_CLIP,
                                   min(TIME_DIFF_CLIP, base + tv - pr["time_sec"]))
                        v = diff * rw
                        tbest = v if tbest is None else max(tbest, v)
                pf = pos_factor.get(pr["rank"], 0.0)
                if pf:
                    v = CLASS_RANK.get(pr["race_class"], 0) * pf * rw
                    cbest = v if cbest is None else max(cbest, v)
                if pr.get("agari_ratio") is not None:
                    amin = pr["agari_ratio"] if amin is None else min(amin, pr["agari_ratio"])
            if tbest is not None:
                total += tbest * time_k
            if cbest is not None:
                total += cbest * class_k
            if amin is not None and amin <= agari_ratio:
                total += agari_bonus

            # ── 市場 (確定単勝オッズ) ──
            if odds_k:
                odds = parse_final_odds(cur.get("win_pay"), cur["rank"])
                if odds and odds > 1.0:
                    total += max(-odds_clamp, min(odds_clamp, odds_k * math.log(odds_ref / odds)))

            races[(cur["date"], cur["place"], cur["r"])].append((total, cur))
    return races


def measure_coverage(races, upset_map, kmax=8, min_r=9):
    """r>=min_r のレースで勝ち馬のスコア順位を集計 → ランク別カバレッジ"""
    pos_counts = defaultdict(lambda: [0] * (kmax + 1))  # rank -> [k=1..kmax超過含む]
    n_races = defaultdict(int)
    for (date, place, r), members in races.items():
        if r is None or r < min_r or len(members) < 8:
            continue
        members.sort(key=lambda x: -x[0])
        winner_pos = next((i + 1 for i, (_, m) in enumerate(members) if m["rank"] == 1), None)
        if winner_pos is None:
            continue
        track = members[0][1]["track_type"]
        dist = members[0][1]["distance"]
        rank = upset_rank_of(upset_map, place, track, dist) or "default"
        n_races[rank] += 1
        n_races["default_all"] += 1
        idx = min(winner_pos, kmax + 1) - 1
        pos_counts[rank][idx] += 1
        pos_counts["default_all"][idx] += 1

    coverage = {}
    for rank in UPSET_RANKS + ["default_all"]:
        n = n_races.get(rank, 0)
        if n < 30:
            continue
        cum, curve = 0, []
        for k in range(kmax):
            cum += pos_counts[rank][k]
            curve.append(round(cum / n, 4))
        coverage["default" if rank == "default_all" else rank] = curve
    return coverage, dict(n_races)


def simulate_win5(races, upset_map, coverage, budgets, min_r=9):
    """土日のR降順5レースを擬似WIN5日としてシミュレーション。
    通常配分 / 一律配分(荒れ度無視) / 軸1頭固定(スコア1-2位マージン最大レースを1点) を比較。"""
    by_sunday = defaultdict(list)
    for (date, place, r), members in races.items():
        if r is None or r < min_r or len(members) < 8:
            continue
        try:
            if datetime.strptime(date, "%Y%m%d").weekday() not in (5, 6):  # 土日
                continue
        except ValueError:
            continue
        by_sunday[date].append((r, place, members))

    results = {b: {"hits": 0, "days": 0, "pts": 0, "odds_proxy": []} for b in budgets}
    results_uni = {b: {"hits": 0, "pts": 0} for b in budgets}
    results_axis = {b: {"hits": 0, "pts": 0, "odds_proxy": []} for b in budgets}
    axis_stat = {"n": 0, "axis_won": 0}

    for date, cands in sorted(by_sunday.items()):
        if len(cands) < 5:
            continue
        cands.sort(key=lambda x: (-x[0], x[1]))
        sel = cands[:5]
        ranks, winner_pos, winner_odds, margins = [], [], [], []
        ok = True
        for r, place, members in sel:
            members.sort(key=lambda x: -x[0])
            wpos = next((i + 1 for i, (_, m) in enumerate(members) if m["rank"] == 1), None)
            if wpos is None:
                ok = False
                break
            track, dist = members[0][1]["track_type"], members[0][1]["distance"]
            ranks.append(upset_rank_of(upset_map, place, track, dist))
            winner_pos.append(wpos)
            margins.append(members[0][0] - members[1][0] if len(members) > 1 else 0.0)
            wp = parse_win_payout(next(m["win_pay"] for _, m in members if m["rank"] == 1), 1)
            winner_odds.append(wp / 100.0 if wp else None)
        if not ok:
            continue

        # 軸レース = スコア1位と2位の差が最大のレース
        axis_idx = max(range(5), key=lambda i: margins[i])
        axis_stat["n"] += 1
        if winner_pos[axis_idx] == 1:
            axis_stat["axis_won"] += 1

        def odds_prod():
            if all(o for o in winner_odds):
                p = 1.0
                for o in winner_odds:
                    p *= o
                return p
            return None

        for b in budgets:
            picks, _ = scoring.allocate_picks(ranks, coverage, b)
            results[b]["days"] += 1
            results[b]["pts"] += scoring._pts_of(picks)
            if all(wp <= k for wp, k in zip(winner_pos, picks)):
                results[b]["hits"] += 1
                op = odds_prod()
                if op:
                    results[b]["odds_proxy"].append(op)
            # 一律配分 (荒れ度無視 = 全レースdefaultカーブ)
            picks_u, _ = scoring.allocate_picks([None] * 5, coverage, b)
            results_uni[b]["pts"] += scoring._pts_of(picks_u)
            if all(wp <= k for wp, k in zip(winner_pos, picks_u)):
                results_uni[b]["hits"] += 1
            # 軸1頭固定
            picks_a, _ = scoring.allocate_picks(ranks, coverage, b, fixed={axis_idx})
            results_axis[b]["pts"] += scoring._pts_of(picks_a)
            if all(wp <= k for wp, k in zip(winner_pos, picks_a)):
                results_axis[b]["hits"] += 1
                op = odds_prod()
                if op:
                    results_axis[b]["odds_proxy"].append(op)
    return results, results_uni, results_axis, axis_stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20240101")
    ap.add_argument("--to", dest="date_to", default="20251231")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--ml", action="store_true",
                    help="win5_ml_model.json のMLスコアで評価 (デフォルトは手調整スコア)")
    args = ap.parse_args()

    cfg = load_win5_cfg()
    upset_map = load_upset_map()
    print(f"course_upset: {len(upset_map)}コース分ロード")

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.date_from, args.date_to)
    conn.close()
    print(f"ロード: {len(runs)}行 / スコア付与中...")

    if args.ml:
        import numpy as np
        from backtest_ml import build_dataset, MODEL_PATH
        with open(MODEL_PATH, "r", encoding="utf-8") as f:
            model = json.load(f)
        X, _y, race_keys, meta = build_dataset(runs, args.date_from, cfg)
        z = (X - np.array(model["mean"])) / np.array(model["sd"])
        scores = z @ np.array(model["coef"]) * model.get("display_scale", 10.0)
        races = defaultdict(list)
        for s, key, m in zip(scores, race_keys, meta):
            races[key].append((float(s), m))
        print(f"MLスコア使用 (win5_ml_model.json, {model['meta']['trained_at']})")
    else:
        races = score_all_runners(runs, args.date_from, cfg)
    print(f"評価レース: {len(races)}")

    coverage, n_races = measure_coverage(races, upset_map)
    print("\n===== 勝ち馬カバレッジ (9R以降・8頭以上, P(勝ち馬がスコア上位k頭)) =====")
    print("ランク | n     | k=1   k=2   k=3   k=4   k=5   k=6   k=7   k=8")
    for rank in UPSET_RANKS + ["default"]:
        curve = coverage.get(rank)
        if not curve:
            print(f"  {rank}   | サンプル不足")
            continue
        n = n_races.get("default_all" if rank == "default" else rank, 0)
        print(f"  {rank:7s}| {n:5d} | " + "  ".join(f"{c*100:4.1f}" for c in curve))

    budgets = [50, 100, 150, 200]
    results, results_uni, results_axis, axis_stat = simulate_win5(races, upset_map, coverage, budgets)
    print("\n===== WIN5シミュレーション (土日・R降順5レース近似) =====")
    if axis_stat["n"]:
        print(f"軸馬 (スコア1-2位差最大レースの1位馬) の勝率: "
              f"{100.0 * axis_stat['axis_won'] / axis_stat['n']:.1f}% ({axis_stat['axis_won']}/{axis_stat['n']})")
    print("点数  | 対象日 | 通常配分        | 軸1頭固定       | 一律配分")
    print("      |       | 的中率/平均点数  | 的中率/平均点数  | 的中率/平均点数")
    for b in budgets:
        r, u, a = results[b], results_uni[b], results_axis[b]
        if r["days"] == 0:
            continue
        d = r["days"]
        print(f"  {b:4d}| {d:5d} | {100.0*r['hits']/d:5.1f}% /{r['pts']/d:6.1f}点 | "
              f"{100.0*a['hits']/d:5.1f}% /{a['pts']/d:6.1f}点 | "
              f"{100.0*u['hits']/d:5.1f}% /{u['pts']/d:6.1f}点")
        for label, rr in (("通常", r), ("軸1頭", a)):
            if rr["odds_proxy"]:
                avg_prod = sum(rr["odds_proxy"]) / len(rr["odds_proxy"])
                print(f"        {label}的中日の勝ち馬オッズ積(平均): {avg_prod:,.0f}倍")

    print("\n[注意] WIN5対象はR番号降順5レースの近似 / 配当はオッズ積プロキシ / "
          "スコアは騎手・枠(勝率)+能力のみで実運用スコアの主成分検証")

    if args.write:
        cfg["allocation"]["coverage"] = {k: coverage[k] for k in coverage}
        cfg["backtest_meta"] = {
            "tuned_factors": ["allocation.coverage"],
            "tuned_at": datetime.now().strftime("%Y-%m-%d"),
            "note": (f"backtest_win5 {args.date_from}-{args.date_to}: カバレッジ実測 "
                     f"(9R以降, ランク別n={ {k: v for k, v in n_races.items() if k != 'default_all'} }). "
                     "WIN5シミュレーションは日曜R降順5レース近似"),
        }
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] {WEIGHTS_PATH} にカバレッジ実測値を反映")
    else:
        print("\n(--write で win5_weights.json に反映)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
