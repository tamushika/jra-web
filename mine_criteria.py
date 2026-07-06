"""
好走条件・消し条件のルールマイニング
======================================
ability.db から criteria.csv 形式の条件 (最大3条件の組み合わせ) を自動発見する。

  - 述語エンジンに本番の analysis.check_condition をそのまま使用
    → 発見されたルールは構文的に必ず本番パイプラインで動く
  - 多重検定対策の二段構え:
      発見: 2021-2024 (該当n>=30, 収縮lift |8pt| 以上)
      検証: 2025-2026 OOS (該当n>=15, 方向維持 ±2pt 以上)
    OOSを通過したルールのみ出力する
  - 買い (コース平均より複勝率が高い) と 消し (低い) の両方を探索
  - 血統条件は horse_pedigree 蓄積完了後に追加予定 (現状DBに父が無い)

【使い方】
  python mine_criteria.py                # 全コース (30-60分)
  python mine_criteria.py --venue 東京   # 1場のみ
出力: mined_criteria_report.txt / mined_buy_rules.csv / mined_kill_rules.csv
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from itertools import combinations

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import analysis  # noqa: E402
from backtest_ability import load_runs  # noqa: E402
from backtest_criteria import build_h, attach_agari_rank, load_mawari  # noqa: E402
from backtest_ability import parse_win_payout  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

TRAIN_FROM, TRAIN_TO = "20210101", "20241231"
TEST_FROM, TEST_TO = "20250101", "20260630"

MIN_TRAIN_N = 30
MIN_TEST_N = 15
TRAIN_LIFT_BUY = 8.0    # 収縮後の複勝率乖離 (pt)
TRAIN_LIFT_KILL = 6.0   # 消しは母集団が小さく閾値を緩和
TEST_LIFT = 2.0     # OOSでの方向維持ライン (pt)
SHRINK_N0 = 50.0
BEAM = 12           # 各段で残す候補数
MAX_RULES = 3       # 買い/消しそれぞれコースあたり上限


def candidate_conditions(course_runs):
    """コースの候補条件文リスト (check_condition が解釈できる語彙のみ)"""
    conds = [
        "馬齢が3歳以下", "馬齢が4歳以下", "馬齢が5歳以上", "馬齢が6歳以上",
        "性別が牡馬・セン", "性別が牝馬",
        "枠番が1~3枠", "枠番が6~8枠",
        "出走頭数が12頭以下", "出走頭数が14頭以下",
        "前走着順が3着以内", "前走着順が5着以内", "前走着順が9着以内",
        "前走との間隔が中4週以内", "前走との間隔が中8週以上",
        "前走の馬体重が460kg以上", "前走の馬体重が500kg以上",
        "前走の4コーナー通過順が4番手以内",
        "調教師の所属が美浦", "調教師の所属が栗東",
        "前走中央場所",
    ]
    # このコースで騎乗数の多い騎手 (学習期間)
    jcount = defaultdict(int)
    for cur, _prev in course_runs:
        if cur["date"] <= TRAIN_TO and cur.get("jockey"):
            jcount[cur["jockey"].strip()] += 1
    for j, n in sorted(jcount.items(), key=lambda x: -x[1])[:8]:
        if n >= MIN_TRAIN_N and j:
            conds.append(f"騎手が{j}")
    return conds


def shrunk_lift(top3, n, base):
    return (100.0 * top3 / n - base) * (n / (n + SHRINK_N0)) if n else 0.0


def mine_course(place, tt, dist, course_runs, mawari_map, report):
    """1コースをマイニング。course_runs: [(cur, prev), ...] 時系列属性つき
    戻り値: (buy_rules, kill_rules)  各 [{conds, train, test}]"""
    r_ctx_base = {"type": tt, "dist": dist, "venue": place}

    # h dict + マスク行列を構築
    hs, is_train, is_test, top3, pay = [], [], [], [], []
    for cur, prev in course_runs:
        h = build_h(cur, prev)
        hs.append((h, {"type": tt, "dist": dist, "venue": place,
                       "total_horses": cur["total_horses"], "class": cur["race_class"]}))
        is_train.append(cur["date"] <= TRAIN_TO)
        is_test.append(cur["date"] >= TEST_FROM)
        top3.append(1 if cur["rank"] <= 3 else 0)
        pay.append(parse_win_payout(cur.get("win_pay"), cur["rank"]))
    n_all = len(hs)
    if sum(is_train) < 200:
        return [], []

    conds = candidate_conditions(course_runs)
    # 条件ごとの真偽ベクトル (check_condition を1条件×1走ごとに評価)。
    # ほぼ全馬に該当/非該当の条件はパーサー未対応 (常にTrue) の疑いがあるため除外
    vec = {}
    seen_vecs = set()
    for c in conds:
        v = bytearray(n_all)
        for i, (h, r_ctx) in enumerate(hs):
            try:
                if analysis.check_condition(c, h, r_ctx, {}, mawari_map):
                    v[i] = 1
            except Exception:
                pass
        rate = sum(v) / n_all
        key = bytes(v)
        if rate >= 0.97 or rate <= 0.005 or key in seen_vecs:
            continue
        seen_vecs.add(key)
        vec[c] = v
    conds = list(vec.keys())

    tr_idx = [i for i in range(n_all) if is_train[i]]
    te_idx = [i for i in range(n_all) if is_test[i]]
    base_tr = 100.0 * sum(top3[i] for i in tr_idx) / len(tr_idx)
    base_te = (100.0 * sum(top3[i] for i in te_idx) / len(te_idx)) if te_idx else None

    def stats(cset, idx):
        hit = [i for i in idx if all(vec[c][i] for c in cset)]
        if not hit:
            return 0, 0.0, 0.0
        t3 = sum(top3[i] for i in hit)
        roi = sum(pay[i] for i in hit) / len(hit)
        return len(hit), 100.0 * t3 / len(hit), roi

    def train_score(cset):
        n, rate, _roi = stats(cset, tr_idx)
        if n < MIN_TRAIN_N:
            return None
        return shrunk_lift(sum(top3[i] for i in tr_idx if all(vec[c][i] for c in cset)),
                           n, base_tr), n, rate

    # ── ビーム探索 (1条件 → 2条件 → 3条件) ──
    evaluated = {}
    def eval_set(cset):
        key = tuple(sorted(cset))
        if key not in evaluated:
            evaluated[key] = train_score(cset)
        return evaluated[key]

    beams = {1: [], 2: [], 3: []}
    for c in conds:
        s = eval_set((c,))
        if s:
            beams[1].append(((c,), s[0]))
    beams[1].sort(key=lambda x: -abs(x[1]))
    seeds = beams[1][:BEAM]
    for depth in (2, 3):
        cands = []
        for cset, _ in seeds:
            for c in conds:
                if c in cset:
                    continue
                new = tuple(sorted(cset + (c,)))
                s = eval_set(new)
                if s:
                    cands.append((new, s[0]))
        uniq = {}
        for cset, lift in cands:
            uniq[cset] = lift
        beams[depth] = sorted(uniq.items(), key=lambda x: -abs(x[1]))[:BEAM]
        seeds = beams[depth]

    # ── 候補を集めてOOS検証。入れ子は「上位が下位を2pt以上上回る場合のみ」採用 ──
    all_cands = beams[1] + list(beams[2]) + list(beams[3])
    survivors = []
    for cset, lift in sorted(all_cands, key=lambda x: -abs(x[1])):
        if lift >= TRAIN_LIFT_BUY:
            pass  # 買い候補
        elif lift <= -TRAIN_LIFT_KILL:
            pass  # 消し候補
        else:
            continue  # lift=0 付近 (無差別条件のフォールスルー対策)
        n_tr, rate_tr, roi_tr = stats(cset, tr_idx)
        n_te, rate_te, roi_te = stats(cset, te_idx)
        if n_te < MIN_TEST_N or base_te is None:
            continue
        oos_dev = rate_te - base_te
        if lift > 0 and oos_dev < TEST_LIFT:
            continue
        if lift < 0 and oos_dev > -TEST_LIFT:
            continue
        # 既採用ルールの部分集合/上位集合との冗長排除
        redundant = False
        for s in survivors:
            ss = set(s["conds"])
            if ss.issubset(cset) or ss.issuperset(cset):
                redundant = True
                break
        if redundant:
            continue
        survivors.append({
            "conds": list(cset), "kind": "買い" if lift > 0 else "消し",
            "train_n": n_tr, "train_rate": rate_tr, "train_base": base_tr,
            "test_n": n_te, "test_rate": rate_te, "test_base": base_te,
            "lift": lift, "roi_tr": roi_tr, "roi_te": roi_te,
        })

    buys = [s for s in survivors if s["kind"] == "買い"][:MAX_RULES]
    kills = [s for s in survivors if s["kind"] == "消し"][:MAX_RULES]
    for s in buys + kills:
        report.append(
            f"{place}{tt}{dist} [{s['kind']}] {' | '.join(s['conds'])}\n"
            f"    学習21-24: 複勝{s['train_rate']:.1f}% (基準{s['train_base']:.1f}%, n={s['train_n']}) / "
            f"OOS25-26: 複勝{s['test_rate']:.1f}% (基準{s['test_base']:.1f}%, n={s['test_n']}) / "
            f"単回収 学習{s['roi_tr']:.0f}%→OOS{s['roi_te']:.0f}%")
    return buys, kills


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, TRAIN_FROM, TEST_TO)
    conn.close()
    print(f"ロード: {len(runs)}行")
    attach_agari_rank(runs)
    mawari_map = load_mawari()

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # (場, 芝ダ, 距離) ごとの (cur, prev) リスト
    courses = defaultdict(list)
    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if (cur["date"] < TRAIN_FROM or cur["rank"] is None
                    or not cur.get("total_horses") or cur["total_horses"] < 8):
                continue
            if args.venue and cur["place"] != args.venue:
                continue
            prev = lst[i - 1] if i > 0 else None
            courses[(cur["place"], cur["track_type"], cur["distance"])].append((cur, prev))

    print(f"対象コース: {len(courses)}")
    report, buy_rows, kill_rows = [], [], []
    for idx, (key, course_runs) in enumerate(sorted(courses.items(),
                                                    key=lambda x: -len(x[1]))):
        place, tt, dist = key
        buys, kills = mine_course(place, tt, dist, course_runs, mawari_map, report)
        for s in buys + kills:
            conds = (s["conds"] + ["", ""])[:3]
            row = [place, tt, dist] + conds + [
                s["kind"], s["train_n"], round(s["train_rate"], 1),
                round(s["train_base"], 1), s["test_n"], round(s["test_rate"], 1),
                round(s["test_base"], 1), round(s["roi_tr"]), round(s["roi_te"])]
            (buy_rows if s["kind"] == "買い" else kill_rows).append(row)
        if (idx + 1) % 10 == 0:
            print(f"  ... {idx + 1}/{len(courses)}コース (採用 買い{len(buy_rows)}/消し{len(kill_rows)})")

    header = ["場", "芝ダ", "距離", "条件1", "条件2", "条件3", "種別",
              "学習n", "学習複勝率", "学習基準", "OOSn", "OOS複勝率", "OOS基準",
              "学習単回収", "OOS単回収"]
    for path, rows in (("mined_buy_rules.csv", buy_rows), ("mined_kill_rules.csv", kill_rows)):
        with open(os.path.join(BASE_DIR, path), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    with open(os.path.join(BASE_DIR, "mined_criteria_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"\n[OK] 買い{len(buy_rows)}本 / 消し{len(kill_rows)}本 "
          f"(mined_buy_rules.csv / mined_kill_rules.csv / mined_criteria_report.txt)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
