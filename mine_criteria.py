"""
好走条件・消し条件のルールマイニング
======================================
ability.db から criteria.csv 形式の条件 (最大3条件の組み合わせ) を自動発見する。

  - 述語エンジンに本番の analysis.check_condition をそのまま使用
    → 発見されたルールは構文的に必ず本番パイプラインで動く
  - 多重検定対策の二段構え:
      発見: 2021-2023 (該当n>=30, 収縮lift 買い+8pt/消し-6pt 以上)
      選抜: 2024 (該当n>=15, 方向維持 ±2pt 以上)
    選抜を通過したルールのみ出力する
  - 2025 / 2026H1 は固定テスト。候補生成・選抜には一切参照しない
  - 買い (コース平均より複勝率が高い) と 消し (低い) の両方を探索
  - 血統条件は horse_pedigree 蓄積完了後に追加予定 (現状DBに父が無い)

【使い方】
  python mine_criteria.py                # 全コース (30-60分)
  python mine_criteria.py --venue 東京 --output tmp_tokyo_rules.csv
  python mine_criteria.py --discover-to 20231231 --select-from 20240101 --select-to 20241231
出力: mined_rules_v2.csv (本番 mined_rules.csv は変更しない)
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import analysis  # noqa: E402
from backtest_ability import load_runs  # noqa: E402
from backtest_criteria import build_h, attach_agari_rank, load_mawari  # noqa: E402
from backtest_ability import parse_win_payout  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

DISCOVER_FROM, DISCOVER_TO = "20210101", "20231231"
SELECT_FROM, SELECT_TO = "20240101", "20241231"
EVAL_PERIODS = (
    ("2025", "20250101", "20251231"),
    ("2026H1", "20260101", "20260630"),
)
PRODUCTION_RULES_PATH = os.path.join(API_DIR, "data_files", "common", "mined_rules.csv")
DEFAULT_OUTPUT_PATH = os.path.join(BASE_DIR, "mined_rules_v2.csv")

MIN_DISCOVER_N = 30
MIN_SELECT_N = 15
DISCOVER_LIFT_BUY = 8.0    # 収縮後の複勝率乖離 (pt)
DISCOVER_LIFT_KILL = 6.0   # 消しは母集団が小さく閾値を緩和
SELECT_LIFT = 2.0          # 選抜期間での方向維持ライン (pt)
SHRINK_N0 = 50.0
BEAM = 12           # 各段で残す候補数
MAX_RULES = 3       # 買い/消しそれぞれコースあたり上限


def candidate_conditions(course_runs, discover_from, discover_to):
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
    # このコースで騎乗数の多い騎手 (発見期間のみ)
    jcount = defaultdict(int)
    for cur, _prev in course_runs:
        if discover_from <= cur["date"] <= discover_to and cur.get("jockey"):
            jcount[cur["jockey"].strip()] += 1
    for j, n in sorted(jcount.items(), key=lambda x: (-x[1], x[0]))[:8]:
        if n >= MIN_DISCOVER_N and j:
            conds.append(f"騎手が{j}")
    return conds


def shrunk_lift(top3, n, base):
    return (100.0 * top3 / n - base) * (n / (n + SHRINK_N0)) if n else 0.0


def mine_course(place, tt, dist, course_runs, mawari_map, report, *,
                discover_from=DISCOVER_FROM, discover_to=DISCOVER_TO,
                select_from=SELECT_FROM, select_to=SELECT_TO):
    """1コースをマイニング。course_runs: [(cur, prev), ...] 時系列属性つき
    戻り値: (buy_rules, kill_rules)。固定テスト期間は本関数の選抜に使わない。"""

    # h dict + マスク行列を構築
    hs, is_discover, is_select, top3, pay = [], [], [], [], []
    for cur, prev in course_runs:
        in_discover = discover_from <= cur["date"] <= discover_to
        in_select = select_from <= cur["date"] <= select_to
        if not (in_discover or in_select):
            continue  # 固定テストは特徴量構築・候補評価にも渡さない
        h = build_h(cur, prev)
        hs.append((h, {"type": tt, "dist": dist, "venue": place,
                       "total_horses": cur["total_horses"], "class": cur["race_class"]}))
        is_discover.append(in_discover)
        is_select.append(in_select)
        top3.append(1 if cur["rank"] <= 3 else 0)
        pay.append(parse_win_payout(cur.get("win_pay"), cur["rank"]))
    n_all = len(hs)
    if sum(is_discover) < 200:
        return [], []

    discover_idx = [i for i in range(n_all) if is_discover[i]]
    select_idx = [i for i in range(n_all) if is_select[i]]
    if not select_idx:
        return [], []

    conds = candidate_conditions(course_runs, discover_from, discover_to)
    # 条件ごとの真偽ベクトル (check_condition を1条件×1走ごとに評価)。
    # ほぼ全馬に該当/非該当の条件はパーサー未対応 (常にTrue) の疑いがあるため除外
    # 候補の除外判定と重複判定も発見期間のみで行う。
    # 2025/2026H1 の値をここで見ると固定テストがリークする。
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
        discover_values = bytes(v[i] for i in discover_idx)
        rate = sum(discover_values) / len(discover_values)
        key = discover_values
        if rate >= 0.97 or rate <= 0.005 or key in seen_vecs:
            continue
        seen_vecs.add(key)
        vec[c] = v
    conds = list(vec.keys())

    base_discover = 100.0 * sum(top3[i] for i in discover_idx) / len(discover_idx)
    base_select = 100.0 * sum(top3[i] for i in select_idx) / len(select_idx)

    def stats(cset, idx):
        hit = [i for i in idx if all(vec[c][i] for c in cset)]
        if not hit:
            return 0, 0.0, 0.0
        t3 = sum(top3[i] for i in hit)
        roi = sum(pay[i] for i in hit) / len(hit)
        return len(hit), 100.0 * t3 / len(hit), roi

    def discover_score(cset):
        n, rate, _roi = stats(cset, discover_idx)
        if n < MIN_DISCOVER_N:
            return None
        return shrunk_lift(
            sum(top3[i] for i in discover_idx if all(vec[c][i] for c in cset)),
            n, base_discover), n, rate

    # ── ビーム探索 (1条件 → 2条件 → 3条件) ──
    evaluated = {}
    def eval_set(cset):
        key = tuple(sorted(cset))
        if key not in evaluated:
            evaluated[key] = discover_score(cset)
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

    # ── 候補を集めて2024年で選抜 ──
    all_cands = beams[1] + list(beams[2]) + list(beams[3])
    survivors = []
    for cset, lift in sorted(all_cands, key=lambda x: -abs(x[1])):
        if lift >= DISCOVER_LIFT_BUY:
            pass  # 買い候補
        elif lift <= -DISCOVER_LIFT_KILL:
            pass  # 消し候補
        else:
            continue  # lift=0 付近 (無差別条件のフォールスルー対策)
        n_discover, rate_discover, roi_discover = stats(cset, discover_idx)
        n_select, rate_select, roi_select = stats(cset, select_idx)
        if n_select < MIN_SELECT_N:
            continue
        select_dev = rate_select - base_select
        if lift > 0 and select_dev < SELECT_LIFT:
            continue
        if lift < 0 and select_dev > -SELECT_LIFT:
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
            "discover_n": n_discover, "discover_rate": rate_discover,
            "discover_base": base_discover,
            "select_n": n_select, "select_rate": rate_select,
            "select_base": base_select, "lift": lift,
            "roi_discover": roi_discover, "roi_select": roi_select,
        })

    buys = [s for s in survivors if s["kind"] == "買い"][:MAX_RULES]
    kills = [s for s in survivors if s["kind"] == "消し"][:MAX_RULES]
    for s in buys + kills:
        report.append(
            f"{place}{tt}{dist} [{s['kind']}] {' | '.join(s['conds'])}\n"
            f"    発見21-23: 複勝{s['discover_rate']:.1f}% "
            f"(基準{s['discover_base']:.1f}%, n={s['discover_n']}) / "
            f"選抜24: 複勝{s['select_rate']:.1f}% "
            f"(基準{s['select_base']:.1f}%, n={s['select_n']}) / "
            f"単回収 発見{s['roi_discover']:.0f}%→選抜{s['roi_select']:.0f}%")
    return buys, kills


def validate_windows(discover_from, discover_to, select_from, select_to):
    """発見/選抜期間が固定テストより前で、互いに重ならないことを保証。"""
    values = (discover_from, discover_to, select_from, select_to)
    for value in values:
        try:
            datetime.strptime(value, "%Y%m%d")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"日付はYYYYMMDD形式で指定してください: {value}") from exc
    if not discover_from <= discover_to < select_from <= select_to:
        raise ValueError("発見期間と選抜期間は、重複なしの時系列順で指定してください")
    if select_to >= EVAL_PERIODS[0][1]:
        raise ValueError("2025年以降は固定テストのため選抜に使用できません")


def build_course_runs(runs, date_from, date_to, venue=None):
    """各出走を直前走と結合し、(場, 芝ダ, 距離) 別に返す。"""
    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    courses = defaultdict(list)
    for lst in by_horse.values():
        for i, cur in enumerate(lst):
            if (cur["date"] < date_from or cur["date"] > date_to or cur["rank"] is None
                    or not cur.get("total_horses") or cur["total_horses"] < 8):
                continue
            if venue and cur["place"] != venue:
                continue
            prev = lst[i - 1] if i > 0 else None
            courses[(cur["place"], cur["track_type"], cur["distance"])].append((cur, prev))
    return courses


def _selection_points(rule):
    """現行と同じ「選抜期複勝率乖離×0.25」を0.5〜3.0点にクランプ。"""
    dev = abs(rule["select_rate"] - rule["select_base"])
    return round(max(0.5, min(3.0, dev * 0.25)), 2)


def mine_all_courses(courses, mawari_map, *, discover_from, discover_to,
                     select_from, select_to):
    report, output_rules = [], []
    ordered = sorted(courses.items(), key=lambda x: (-len(x[1]), x[0]))
    print(f"対象コース: {len(ordered)}")
    for idx, (key, course_runs) in enumerate(ordered):
        place, tt, dist = key
        buys, kills = mine_course(
            place, tt, dist, course_runs, mawari_map, report,
            discover_from=discover_from, discover_to=discover_to,
            select_from=select_from, select_to=select_to)
        for rule in buys + kills:
            output_rules.append({
                "place": place, "track_type": tt, "distance": dist,
                "conds": rule["conds"], "kind": rule["kind"],
                "points": _selection_points(rule),
            })
        if (idx + 1) % 10 == 0:
            n_buy = sum(r["kind"] == "買い" for r in output_rules)
            n_kill = len(output_rules) - n_buy
            print(f"  ... {idx + 1}/{len(ordered)}コース "
                  f"(採用 買い{n_buy}/消し{n_kill})")
    return output_rules, report


def write_rules(path, rules):
    output_path = os.path.abspath(path)
    if os.path.normcase(output_path) == os.path.normcase(os.path.abspath(PRODUCTION_RULES_PATH)):
        raise ValueError("本番 mined_rules.csv は上書きできません")
    header = ["場", "芝ダ", "距離", "条件1", "条件2", "条件3", "種別", "点数"]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for rule in rules:
            conds = (rule["conds"] + ["", ""])[:3]
            writer.writerow([
                rule["place"], rule["track_type"], rule["distance"], *conds,
                rule["kind"], rule["points"],
            ])


def load_rules(path):
    rules = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                rules.append({
                    "place": row["場"], "track_type": row["芝ダ"],
                    "distance": int(row["距離"]),
                    "conds": [c for c in (row.get("条件1"), row.get("条件2"),
                                             row.get("条件3")) if c and c.strip()],
                    "kind": row["種別"], "points": float(row["点数"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
    return rules


def attach_place_payouts(conn, runs, date_from, date_to):
    """runs.fukusho_pay を付与し、欠損はrace_payoutsの複勝で補完する。"""
    try:
        rows = conn.execute(
            """SELECT date,place,r,umaban,fukusho_pay FROM runs
               WHERE date >= ? AND date <= ?""", (date_from, date_to)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    payout_map = {(d, p, r, int(u)): value for d, p, r, u, value in rows
                  if u is not None and value is not None}
    try:
        fallback_rows = conn.execute(
            """SELECT date,place,r,combo,pay FROM race_payouts
               WHERE date >= ? AND date <= ? AND bet_type = '複勝'""",
            (date_from, date_to)).fetchall()
    except sqlite3.OperationalError:
        fallback_rows = []
    for date, place, race_no, combo, value in fallback_rows:
        try:
            key = (date, place, race_no, int(str(combo).strip()))
        except (TypeError, ValueError):
            continue
        if value is not None:
            payout_map.setdefault(key, value)
    for run in runs:
        try:
            umaban = int(run.get("umaban"))
        except (TypeError, ValueError):
            umaban = None
        run["fukusho_pay"] = payout_map.get(
            (run.get("date"), run.get("place"), run.get("r"), umaban))


def evaluate_rules(rules, courses, mawari_map, period_name, date_from, date_to):
    """各種別で「1つ以上のルールに該当した馬」を1頭1件として集計する。"""
    indexed = defaultdict(list)
    rule_counts = defaultdict(int)
    for rule in rules:
        key = (rule["place"], rule["track_type"], rule["distance"], rule["kind"])
        indexed[key].append(rule)
        rule_counts[rule["kind"]] += 1

    accum = {
        kind: {"period": period_name, "kind": kind, "rules": rule_counts[kind],
               "matches": 0, "top3": 0, "wins": 0, "win_return": 0.0,
               "place_return": 0.0, "place_missing": 0}
        for kind in ("買い", "消し")
    }
    for (place, tt, dist), course_runs in courses.items():
        for cur, prev in course_runs:
            if not date_from <= cur["date"] <= date_to:
                continue
            h = build_h(cur, prev)
            ctx = {"type": tt, "dist": dist, "venue": place,
                   "total_horses": cur["total_horses"], "class": cur["race_class"]}
            for kind in ("買い", "消し"):
                candidates = indexed.get((place, tt, dist, kind), ())
                matched = False
                for rule in candidates:
                    try:
                        if all(analysis.check_condition(c, h, ctx, {}, mawari_map)
                               for c in rule["conds"]):
                            matched = True
                            break
                    except Exception:
                        continue
                if not matched:
                    continue
                row = accum[kind]
                row["matches"] += 1
                if cur["rank"] <= 3:
                    row["top3"] += 1
                    place_pay = cur.get("fukusho_pay")
                    if place_pay is None or str(place_pay).strip() == "":
                        row["place_missing"] += 1
                    else:
                        try:
                            row["place_return"] += float(place_pay)
                        except (TypeError, ValueError):
                            row["place_missing"] += 1
                if cur["rank"] == 1:
                    row["wins"] += 1
                row["win_return"] += parse_win_payout(cur.get("win_pay"), cur["rank"])

    for row in accum.values():
        n = row["matches"]
        row["show_rate"] = 100.0 * row["top3"] / n if n else 0.0
        row["win_roi"] = row["win_return"] / n if n else 0.0
        row["place_roi"] = row["place_return"] / n if n else 0.0
    return [accum["買い"], accum["消し"]]


def print_evaluation(rule_sets, courses, mawari_map):
    print("\n固定テスト比較 (各種別で複数ルール該当馬は1頭1件):")
    print("期間     ルール       種別  本数    該当n  複勝率  単回収  複回収  複勝配当欠損")
    for period_name, date_from, date_to in EVAL_PERIODS:
        for set_name, rules in rule_sets:
            for row in evaluate_rules(rules, courses, mawari_map,
                                      period_name, date_from, date_to):
                print(f"{period_name:<8} {set_name:<12} {row['kind']:<3} "
                      f"{row['rules']:>4} {row['matches']:>8} "
                      f"{row['show_rate']:>6.1f}% {row['win_roi']:>6.1f}% "
                      f"{row['place_roi']:>6.1f}% {row['place_missing']:>10}")
    print("注: 現行ルールの2025年値は選抜に使用済みで、固定テストではありません。"
          "真の比較は2026H1です。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default=None)
    ap.add_argument("--discover-from", default=DISCOVER_FROM)
    ap.add_argument("--discover-to", default=DISCOVER_TO)
    ap.add_argument("--select-from", default=SELECT_FROM)
    ap.add_argument("--select-to", default=SELECT_TO)
    ap.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    ap.add_argument("--skip-evaluation", action="store_true",
                    help="再採掘のみ実行し、2025/2026H1比較を省略")
    args = ap.parse_args()

    validate_windows(args.discover_from, args.discover_to,
                     args.select_from, args.select_to)
    eval_to = EVAL_PERIODS[-1][2]
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.discover_from, eval_to)
    attach_place_payouts(conn, runs, EVAL_PERIODS[0][1], eval_to)
    conn.close()
    print(f"ロード: {len(runs)}行")
    print(f"発見: {args.discover_from}-{args.discover_to} "
          f"(n>={MIN_DISCOVER_N}, 収縮lift 買い+{DISCOVER_LIFT_BUY:g}pt/"
          f"消し-{DISCOVER_LIFT_KILL:g}pt) / "
          f"選抜: {args.select_from}-{args.select_to} "
          f"(n>={MIN_SELECT_N}, 方向維持±{SELECT_LIFT:g}pt)")
    attach_agari_rank(runs)
    mawari_map = load_mawari()
    courses = build_course_runs(runs, args.discover_from, eval_to, args.venue)
    rules_v2, _report = mine_all_courses(
        courses, mawari_map, discover_from=args.discover_from,
        discover_to=args.discover_to, select_from=args.select_from,
        select_to=args.select_to)
    write_rules(args.output, rules_v2)
    n_buy = sum(r["kind"] == "買い" for r in rules_v2)
    n_kill = len(rules_v2) - n_buy
    print(f"\n[OK] 買い{n_buy}本 / 消し{n_kill}本 -> {os.path.abspath(args.output)}")

    if not args.skip_evaluation:
        current_rules = load_rules(PRODUCTION_RULES_PATH)
        print(f"ルール数: 現行{len(current_rules)} -> v2 {len(rules_v2)}")
        print_evaluation((("現行(リークあり)", current_rules), ("v2(クリーン)", rules_v2)),
                         courses, mawari_map)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
