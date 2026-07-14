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
  - horse_pedigree のローカルキャッシュから父・母父を付与し、血統条件も候補化

【使い方】
  python mine_criteria.py                # 全コース (30-60分)
  python mine_criteria.py --venue 東京 --output tmp_tokyo_rules.csv
  python mine_criteria.py --discover-to 20231231 --select-from 20240101 --select-to 20241231
出力: mined_rules_v3.csv (本番 mined_rules.csv / v2 は変更しない)
"""
import argparse
import csv
import hashlib
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import analysis  # noqa: E402
import pedigree_store  # noqa: E402
import scoring  # noqa: E402
from backtest_ability import load_runs  # noqa: E402
from backtest_criteria import build_h, attach_agari_rank, load_mawari  # noqa: E402
from backtest_ability import parse_win_payout  # noqa: E402
from backtest_win5 import load_win5_cfg  # noqa: E402
from fold_stats import FoldFactorTableProvider  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")

DISCOVER_FROM, DISCOVER_TO = "20210101", "20231231"
SELECT_FROM, SELECT_TO = "20240101", "20241231"
EVAL_PERIODS = (
    ("2025", "20250101", "20251231"),
    ("2026H1", "20260101", "20260630"),
)
PRODUCTION_RULES_PATH = os.path.join(API_DIR, "data_files", "common", "mined_rules.csv")
V2_RULES_PATH = os.path.join(BASE_DIR, "mined_rules_v2.csv")
DEFAULT_OUTPUT_PATH = os.path.join(BASE_DIR, "mined_rules_v3.csv")
PEDIGREE_CACHE_PATH = os.path.join(BASE_DIR, "pedigree_cache.json")

MIN_DISCOVER_N = 30
MIN_SELECT_N = 15
DISCOVER_LIFT_BUY = 8.0    # 収縮後の複勝率乖離 (pt)
DISCOVER_LIFT_KILL = 6.0   # 消しは母集団が小さく閾値を緩和
SELECT_LIFT = 2.0          # 選抜期間での方向維持ライン (pt)
SHRINK_N0 = 50.0
BEAM = 12           # 各段で残す候補数
MAX_RULES = 3       # 買い/消しそれぞれコースあたり上限
PEDIGREE_TOP_N = 8
PEDIGREE_COND_RE = re.compile(r"父|母父|系")
SIRE_PTS_AS_OF = {"2025": "20241231", "2026H1": "20251231"}


def _pedigree_value(value):
    value = str(value or "").strip()
    return value if value and value != "-" else ""


def attach_pedigree(h, horse, pedigree):
    """Attach cached sire/bms to a mining horse without changing build_h."""
    ped = (pedigree or {}).get(str(horse or "").strip()) or {}
    sire = _pedigree_value(ped.get("sire"))
    bms = _pedigree_value(ped.get("bms"))
    if sire:
        h["sire"] = sire
    if bms:
        h["bms"] = bms
    return h


def pedigree_coverage(runs, pedigree):
    """Return unique-horse sire/bms coverage for a reproducible run universe."""
    horses = {str(r.get("horse") or "").strip() for r in runs if r.get("horse")}
    sire_n = bms_n = both_n = 0
    for horse in horses:
        ped = (pedigree or {}).get(horse) or {}
        has_sire = bool(_pedigree_value(ped.get("sire")))
        has_bms = bool(_pedigree_value(ped.get("bms")))
        sire_n += has_sire
        bms_n += has_bms
        both_n += has_sire and has_bms
    return {
        "horses": len(horses), "sire": sire_n, "bms": bms_n, "both": both_n,
    }


def is_pedigree_rule(rule):
    return any(PEDIGREE_COND_RE.search(str(cond or "")) for cond in rule.get("conds", ()))


def _matches_rule(rule, h, ctx, sire_lineage, mawari_map):
    try:
        return all(analysis.check_condition(c, h, ctx, sire_lineage, mawari_map)
                   for c in rule["conds"])
    except Exception:
        return False


def _matches_non_pedigree_context(rule, h, ctx, sire_lineage, mawari_map):
    """Match the same rule after removing only its pedigree predicates.

    This defines the comparison universe for pedigree increment: a horse that
    never met the rule's other conditions is not a valid control for whether
    adding the pedigree condition helped.
    """
    try:
        return all(
            analysis.check_condition(cond, h, ctx, sire_lineage, mawari_map)
            for cond in rule["conds"]
            if not PEDIGREE_COND_RE.search(str(cond or ""))
        )
    except Exception:
        return False


def candidate_conditions(course_runs, discover_from, discover_to, *,
                         pedigree=None, sire_lineage=None):
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

    # 血統候補の頻度集計も発見期間のみ。固定テスト期間の
    # 血統分布が候補集合に混入しないことが重要。
    sire_count = defaultdict(int)
    bms_count = defaultdict(int)
    lineage_count = defaultdict(int)
    pedigree = pedigree or {}
    sire_lineage = sire_lineage or {}
    for cur, _prev in course_runs:
        if not discover_from <= cur["date"] <= discover_to:
            continue
        ped = pedigree.get(str(cur.get("horse") or "").strip()) or {}
        sire = _pedigree_value(ped.get("sire"))
        bms = _pedigree_value(ped.get("bms"))
        if sire:
            sire_count[sire] += 1
            lineage = _pedigree_value(sire_lineage.get(sire))
            if lineage:
                # syuboba.csv の群名は通常「○○系」。条件文側で
                # 「系」を付けるため、二重の「系系」を避ける。
                lineage_count[re.sub(r"系$", "", lineage)] += 1
        if bms:
            bms_count[bms] += 1

    def top_names(counts):
        return [(name, n) for name, n in
                sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:PEDIGREE_TOP_N]
                if name and n >= MIN_DISCOVER_N]

    conds.extend(f"父が{name}" for name, _n in top_names(sire_count))
    conds.extend(f"母父が{name}" for name, _n in top_names(bms_count))
    # 系統は実数が少ないため、上位Nではなく件数下限を満たす全群。
    conds.extend(f"父が{name}系" for name, n in
                 sorted(lineage_count.items(), key=lambda item: (-item[1], item[0]))
                 if name and n >= MIN_DISCOVER_N)
    return conds


def shrunk_lift(top3, n, base):
    return (100.0 * top3 / n - base) * (n / (n + SHRINK_N0)) if n else 0.0


def mine_course(place, tt, dist, course_runs, mawari_map, report, *,
                discover_from=DISCOVER_FROM, discover_to=DISCOVER_TO,
                select_from=SELECT_FROM, select_to=SELECT_TO,
                pedigree=None, sire_lineage=None):
    """1コースをマイニング。course_runs: [(cur, prev), ...] 時系列属性つき
    戻り値: (buy_rules, kill_rules)。固定テスト期間は本関数の選抜に使わない。"""

    # h dict + マスク行列を構築
    hs, is_discover, is_select, top3, pay = [], [], [], [], []
    for cur, prev in course_runs:
        in_discover = discover_from <= cur["date"] <= discover_to
        in_select = select_from <= cur["date"] <= select_to
        if not (in_discover or in_select):
            continue  # 固定テストは特徴量構築・候補評価にも渡さない
        h = attach_pedigree(build_h(cur, prev), cur.get("horse"), pedigree)
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

    conds = candidate_conditions(
        course_runs, discover_from, discover_to,
        pedigree=pedigree, sire_lineage=sire_lineage)
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
                if analysis.check_condition(c, h, r_ctx, sire_lineage or {}, mawari_map):
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
                     select_from, select_to, pedigree=None, sire_lineage=None):
    report, output_rules = [], []
    ordered = sorted(courses.items(), key=lambda x: (-len(x[1]), x[0]))
    print(f"対象コース: {len(ordered)}")
    for idx, (key, course_runs) in enumerate(ordered):
        place, tt, dist = key
        buys, kills = mine_course(
            place, tt, dist, course_runs, mawari_map, report,
            discover_from=discover_from, discover_to=discover_to,
            select_from=select_from, select_to=select_to,
            pedigree=pedigree, sire_lineage=sire_lineage)
        for rule in buys + kills:
            output_rules.append({
                "place": place, "track_type": tt, "distance": dist,
                "conds": rule["conds"], "kind": rule["kind"],
                "points": _selection_points(rule),
                # CSVには書かず、発見→選抜→OOS方向安定性の報告にだけ使う。
                "discover_dev": rule["discover_rate"] - rule["discover_base"],
                "select_dev": rule["select_rate"] - rule["select_base"],
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


def evaluate_rules(rules, courses, mawari_map, period_name, date_from, date_to, *,
                   pedigree=None, sire_lineage=None):
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
            h = attach_pedigree(build_h(cur, prev), cur.get("horse"), pedigree)
            ctx = {"type": tt, "dist": dist, "venue": place,
                   "total_horses": cur["total_horses"], "class": cur["race_class"]}
            for kind in ("買い", "消し"):
                candidates = indexed.get((place, tt, dist, kind), ())
                matched = any(_matches_rule(rule, h, ctx, sire_lineage or {}, mawari_map)
                              for rule in candidates)
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


def build_sire_pts_provider(period_name, pedigree):
    """Build the leakage-free ability snapshot used for a fixed OOS period."""
    try:
        as_of = SIRE_PTS_AS_OF[period_name]
    except KeyError as exc:
        raise ValueError(f"sire_pts統計のas-ofが未定義です: {period_name}") from exc
    pedigree_by_horse = {
        str(horse).strip(): _pedigree_value((ped or {}).get("sire"))
        for horse, ped in (pedigree or {}).items()
        if _pedigree_value((ped or {}).get("sire"))
    }
    return FoldFactorTableProvider(
        DB_PATH, as_of,
        pedigree_by_horse=pedigree_by_horse,
        legacy_api_dir=API_DIR,
    )


def _sire_points(place, tt, dist, sire, factor_table_provider, cfg):
    """Return (sire_pts, available), matching the ML builder's zero fallback."""
    if not sire:
        return 0.0, False
    table = factor_table_provider(place, tt, dist)
    if not table:
        return 0.0, False
    baseline = (table.get("baseline") or {}).get("win_rate")
    if baseline is None:
        return 0.0, False
    row = scoring._match_entity(table.get("father_w"), sire)
    if row is None:
        return 0.0, False
    points, _note = scoring._factor_points(
        row, baseline, cfg["params"], 1.0, "win_rate")
    return (float(points), True) if points is not None else (0.0, False)


def _tertile_cuts(values):
    """Deterministic observation-weighted terciles; equal scores stay together."""
    ordered = sorted(values)
    if not ordered:
        return None
    last = len(ordered) - 1
    return ordered[last // 3], ordered[(2 * last) // 3]


def _sire_band(value, cuts):
    low_cut, high_cut = cuts
    if value <= low_cut:
        return "低"
    if value <= high_cut:
        return "中"
    return "高"


def _group_comparison_rates(matched, control):
    """Return rates/increment, using None when a comparison is unobserved."""
    matched_rate = (100.0 * sum(row["top3"] for row in matched) / len(matched)
                    if matched else None)
    control_rate = (100.0 * sum(row["top3"] for row in control) / len(control)
                    if control else None)
    increment = (matched_rate - control_rate
                 if matched_rate is not None and control_rate is not None
                 else None)
    return matched_rate, control_rate, increment


def evaluate_pedigree_increment(rules, courses, mawari_map, period_name,
                                date_from, date_to, *, pedigree,
                                sire_lineage, factor_table_provider=None):
    """Compare pedigree-rule hits with non-hits inside the same sire_pts band.

    The comparison universe is limited to horses satisfying the non-pedigree
    predicates of at least one selected pedigree rule of the same course/kind.
    Thus a composite rule such as ``previous top5 AND sire X`` compares sire X
    with other sires among previous-top5 horses, rather than with the whole
    course.  Multiple rules are unioned so each runner remains one observation.
    """
    indexed = defaultdict(list)
    for rule in rules:
        if is_pedigree_rule(rule):
            indexed[(rule["place"], rule["track_type"], rule["distance"],
                     rule["kind"])].append(rule)

    # 2025に2025年成績を、2026H1に2026年成績を混ぜない。
    # T14/T33と同じability.db年次as-of統計とWIN5設定を使う。
    factor_table_provider = (factor_table_provider or
                             build_sire_pts_provider(period_name, pedigree))
    cfg = load_win5_cfg()
    rows_by_kind = defaultdict(list)
    unavailable = defaultdict(int)
    for (place, tt, dist), course_runs in courses.items():
        for kind in ("買い", "消し"):
            candidates = indexed.get((place, tt, dist, kind), ())
            if not candidates:
                continue
            for cur, prev in course_runs:
                if not date_from <= cur["date"] <= date_to:
                    continue
                h = attach_pedigree(build_h(cur, prev), cur.get("horse"), pedigree)
                ctx = {"type": tt, "dist": dist, "venue": place,
                       "total_horses": cur["total_horses"], "class": cur["race_class"]}
                eligible = any(
                    _matches_non_pedigree_context(
                        rule, h, ctx, sire_lineage, mawari_map)
                    for rule in candidates
                )
                if not eligible:
                    continue
                pts, pts_available = _sire_points(
                    place, tt, dist, _pedigree_value(h.get("sire")),
                    factor_table_provider, cfg)
                if not pts_available:
                    unavailable[kind] += 1
                matched = any(_matches_rule(rule, h, ctx, sire_lineage, mawari_map)
                              for rule in candidates)
                rows_by_kind[kind].append({
                    "points": float(pts), "matched": matched,
                    "available": bool(pts_available),
                    "top3": int(cur["rank"] <= 3),
                })

    output = []
    for kind in ("買い", "消し"):
        rows = rows_by_kind[kind]
        # MLでは統計行なしを0点へfallbackするが、層別評価まで同じ箱へ
        # 混ぜると欠損が多数のとき三分位が全て0になる。欠損を独立帯にし、
        # 実際にsire_ptsを算出できた馬だけで低/中/高を定義する。
        cuts = _tertile_cuts([
            row["points"] for row in rows if row["available"]
        ])
        if cuts is None:
            matched = [row for row in rows if row["matched"]]
            control = [row for row in rows if not row["matched"]]
            matched_rate, control_rate, increment = _group_comparison_rates(
                matched, control)
            output.append({
                "period": period_name, "kind": kind, "band": "欠損",
                "matched_n": len(matched), "matched_rate": matched_rate,
                "control_n": len(control), "control_rate": control_rate,
                "increment": increment,
                "overlap_top": 0.0, "unavailable": unavailable[kind],
                "low_cut": None, "high_cut": None,
            })
            continue
        matched_total = sum(row["matched"] for row in rows)
        matched_high = sum(
            row["matched"] and row["available"]
            and _sire_band(row["points"], cuts) == "高"
            for row in rows
        )
        overlap = 100.0 * matched_high / matched_total if matched_total else 0.0
        for band in ("欠損", "低", "中", "高"):
            group = [
                row for row in rows
                if ((not row["available"] and band == "欠損")
                    or (row["available"] and band != "欠損"
                        and _sire_band(row["points"], cuts) == band))
            ]
            matched = [row for row in group if row["matched"]]
            control = [row for row in group if not row["matched"]]
            matched_rate, control_rate, increment = _group_comparison_rates(
                matched, control)
            output.append({
                "period": period_name, "kind": kind, "band": band,
                "matched_n": len(matched), "matched_rate": matched_rate,
                "control_n": len(control), "control_rate": control_rate,
                "increment": increment,
                "overlap_top": overlap, "unavailable": unavailable[kind],
                "low_cut": (None if band == "欠損" else cuts[0]),
                "high_cut": (None if band == "欠損" else cuts[1]),
            })
    return output


def evaluate_direction_stability(rules, courses, mawari_map, period_name,
                                 date_from, date_to, *, pedigree,
                                 sire_lineage):
    """Count v3 rules whose discover/select/fixed-test deviations agree."""
    stats = {
        "all": {"rules": 0, "evaluable": 0, "consistent": 0},
        "pedigree": {"rules": 0, "evaluable": 0, "consistent": 0},
    }
    for rule in rules:
        groups = ["all"] + (["pedigree"] if is_pedigree_rule(rule) else [])
        for group in groups:
            stats[group]["rules"] += 1
        course_runs = courses.get(
            (rule["place"], rule["track_type"], rule["distance"]), ())
        base_n = base_top3 = matched_n = matched_top3 = 0
        for cur, prev in course_runs:
            if not date_from <= cur["date"] <= date_to:
                continue
            base_n += 1
            base_top3 += cur["rank"] <= 3
            h = attach_pedigree(build_h(cur, prev), cur.get("horse"), pedigree)
            ctx = {"type": rule["track_type"], "dist": rule["distance"],
                   "venue": rule["place"], "total_horses": cur["total_horses"],
                   "class": cur["race_class"]}
            if _matches_rule(rule, h, ctx, sire_lineage, mawari_map):
                matched_n += 1
                matched_top3 += cur["rank"] <= 3
        if not base_n or not matched_n:
            continue
        fixed_dev = 100.0 * matched_top3 / matched_n - 100.0 * base_top3 / base_n
        expected = 1.0 if rule["kind"] == "買い" else -1.0
        devs = (rule.get("discover_dev"), rule.get("select_dev"), fixed_dev)
        if any(dev is None for dev in devs):
            continue
        consistent = all(expected * dev > 0 for dev in devs)
        for group in groups:
            stats[group]["evaluable"] += 1
            stats[group]["consistent"] += consistent
    for row in stats.values():
        row["period"] = period_name
        row["rate"] = (100.0 * row["consistent"] / row["evaluable"]
                       if row["evaluable"] else 0.0)
    return stats


def print_pedigree_increment(rules, courses, mawari_map, *, pedigree,
                             sire_lineage):
    print("\nsire_pts層別の血統ルール増分 "
          "(同種別の血統ルールがあるコース内):")
    print("期間     種別  層  該当n 該当複勝  非該当n 非該当複勝  差      "
          "上位層重複  sire_pts境界")
    for period_name, date_from, date_to in EVAL_PERIODS:
        rows = evaluate_pedigree_increment(
            rules, courses, mawari_map, period_name, date_from, date_to,
            pedigree=pedigree, sire_lineage=sire_lineage)
        for row in rows:
            cuts = ("-" if row["low_cut"] is None else
                    f"<={row['low_cut']:.2f}/<={row['high_cut']:.2f}/high")
            matched_rate = ("N/A" if row["matched_rate"] is None else
                            f"{row['matched_rate']:.1f}%")
            control_rate = ("N/A" if row["control_rate"] is None else
                            f"{row['control_rate']:.1f}%")
            increment = ("N/A" if row["increment"] is None else
                         f"{row['increment']:+.1f}pt")
            print(f"{period_name:<8} {row['kind']:<3} {row['band']:<2} "
                  f"{row['matched_n']:>6} {matched_rate:>9} "
                  f"{row['control_n']:>8} {control_rate:>11} "
                  f"{increment:>8} {row['overlap_top']:>9.1f}%  {cuts}")
        if rows:
            print(f"  sire_pts統計行なし(0点フォールバック): "
                  f"買い{rows[0]['unavailable']}", end="")
            kill = next((row for row in rows if row["kind"] == "消し"), None)
            print(f" / 消し{kill['unavailable'] if kill else 0}")

    print("\n方向安定性 (発見21-23→選抜24→固定テスト):")
    for period_name, date_from, date_to in EVAL_PERIODS:
        stats = evaluate_direction_stability(
            rules, courses, mawari_map, period_name, date_from, date_to,
            pedigree=pedigree, sire_lineage=sire_lineage)
        for group in ("all", "pedigree"):
            row = stats[group]
            print(f"{period_name:<8} {group:<8} {row['consistent']}/{row['evaluable']} "
                  f"({row['rate']:.1f}%, ルール全数{row['rules']})")


def _file_sha256(path):
    if not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def print_evaluation(rule_sets, courses, mawari_map, *, pedigree=None,
                     sire_lineage=None):
    print("\n固定テスト比較 (各種別で複数ルール該当馬は1頭1件):")
    print("期間     ルール       種別  本数    該当n  複勝率  単回収  複回収  複勝配当欠損")
    for period_name, date_from, date_to in EVAL_PERIODS:
        for set_name, rules in rule_sets:
            for row in evaluate_rules(rules, courses, mawari_map,
                                      period_name, date_from, date_to,
                                      pedigree=pedigree,
                                      sire_lineage=sire_lineage):
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
    if os.path.normcase(os.path.abspath(args.output)) == os.path.normcase(
            os.path.abspath(V2_RULES_PATH)):
        raise ValueError("T23では既存 mined_rules_v2.csv を上書きできません")
    protected_hashes = {
        PRODUCTION_RULES_PATH: _file_sha256(PRODUCTION_RULES_PATH),
        V2_RULES_PATH: _file_sha256(V2_RULES_PATH),
    }
    if not os.path.exists(PEDIGREE_CACHE_PATH):
        raise RuntimeError(
            "pedigree_cache.json がありません。T23はネット再取得を行いません")
    # use_cache=True + キャッシュ存在ガードにより、Neon/外部サイトへ
    # 接続せず、既存のローカルデータだけを使う。
    pedigree = pedigree_store.load_all(use_cache=True)
    sire_lineage = analysis.load_sire_lineage(API_DIR)
    eval_to = EVAL_PERIODS[-1][2]
    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.discover_from, eval_to)
    attach_place_payouts(conn, runs, EVAL_PERIODS[0][1], eval_to)
    conn.close()
    print(f"ロード: {len(runs)}行")
    coverage = pedigree_coverage(
        (r for r in runs if r["date"] >= args.discover_from), pedigree)
    total = coverage["horses"]
    pct = lambda n: 100.0 * n / total if total else 0.0
    print("血統カバレッジ(対象期間の固有馬): "
          f"父 {coverage['sire']}/{total} ({pct(coverage['sire']):.1f}%) / "
          f"母父 {coverage['bms']}/{total} ({pct(coverage['bms']):.1f}%) / "
          f"両方 {coverage['both']}/{total} ({pct(coverage['both']):.1f}%)")
    print(f"系統マップ: {len(sire_lineage)}種牡馬")
    print(f"発見: {args.discover_from}-{args.discover_to} "
          f"(n>={MIN_DISCOVER_N}, 収縮lift 買い+{DISCOVER_LIFT_BUY:g}pt/"
          f"消し-{DISCOVER_LIFT_KILL:g}pt) / "
          f"選抜: {args.select_from}-{args.select_to} "
          f"(n>={MIN_SELECT_N}, 方向維持±{SELECT_LIFT:g}pt)")
    attach_agari_rank(runs)
    mawari_map = load_mawari()
    courses = build_course_runs(runs, args.discover_from, eval_to, args.venue)
    rules_v3, _report = mine_all_courses(
        courses, mawari_map, discover_from=args.discover_from,
        discover_to=args.discover_to, select_from=args.select_from,
        select_to=args.select_to, pedigree=pedigree,
        sire_lineage=sire_lineage)
    write_rules(args.output, rules_v3)
    n_buy = sum(r["kind"] == "買い" for r in rules_v3)
    n_kill = len(rules_v3) - n_buy
    n_pedigree = sum(is_pedigree_rule(rule) for rule in rules_v3)
    n_broad_exclusion = sum(
        any("以外" in cond for cond in rule["conds"]) for rule in rules_v3)
    print(f"\n[OK] 買い{n_buy}本 / 消し{n_kill}本 -> {os.path.abspath(args.output)}")
    print(f"血統条件を含むルール: {n_pedigree}本 / "
          f"「以外」型: {n_broad_exclusion}本")

    if not args.skip_evaluation:
        current_rules = load_rules(PRODUCTION_RULES_PATH)
        v2_rules = load_rules(V2_RULES_PATH) if os.path.exists(V2_RULES_PATH) else []
        print(f"ルール数: 現行{len(current_rules)} / v2 {len(v2_rules)} / "
              f"v3 {len(rules_v3)}")
        print_evaluation(
            (("現行(リークあり)", current_rules),
             ("v2(クリーン)", v2_rules),
             ("v3(血統込み)", rules_v3)),
            courses, mawari_map, pedigree=pedigree,
            sire_lineage=sire_lineage)
        print_pedigree_increment(
            rules_v3, courses, mawari_map, pedigree=pedigree,
            sire_lineage=sire_lineage)

    changed = [path for path, before in protected_hashes.items()
               if _file_sha256(path) != before]
    if changed:
        raise RuntimeError("保護対象のルールCSVが変更されました: "
                           + ", ".join(changed))
    print("保護CSV SHA-256: 本番/v2 ともに不変")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
