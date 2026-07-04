"""
血統辞典 買い/消しルールの集計レベル検証
==========================================
過去DBに種牡馬情報が無いため per-runner バックテストは不可。代わりに
db-keiba由来のコース別種牡馬統計 (data_files/<場>/factors/*.csv, 2021-2025) を使い、

  「ルールのコース条件 (芝ダ/距離/場/直線) に該当するコースでの当該種牡馬の
   複勝率乖離が、非該当コースでの乖離より 買い=高い / 消し=低い か」

を全ルール横断で検証する。

【限界 (レポートにも表示)】
  - 性別・母父・脚質・馬体重などの馬レベル条件は集計に反映できない (希釈される)
  - db-keibaは各コース複勝上位10種牡馬のみ掲載 → 対象コースが限られる
  - コース条件を持たないルール (馬レベル条件のみ) は検証対象外

【使い方】 python buysell_validation.py
"""
import csv
import json
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from scoring import VENUE_SLUG_MAP, _norm, straight_type  # noqa: E402

RULES_PATH = os.path.join(API_DIR, "data_files", "common", "sire_buysell_rules.json")
SLUG_TO_VENUE = {v: k for k, v in VENUE_SLUG_MAP.items()}

COURSE_KEYS = ("surface", "venue", "dist_min", "dist_max", "dist_list",
               "dist_min_alt", "straight")


def load_all_courses():
    """全 factors CSV → {(場, 芝ダ, 距離): {"baseline": float, "sires": {正規化名: (show, starts, 表示名)}}}"""
    courses = {}
    root = os.path.join(API_DIR, "data_files")
    for slug in os.listdir(root):
        fdir = os.path.join(root, slug, "factors")
        venue = SLUG_TO_VENUE.get(slug)
        if venue is None or not os.path.isdir(fdir):
            continue
        for fname in os.listdir(fdir):
            if not fname.endswith(".csv"):
                continue
            race_type = "芝" if fname.startswith("芝") else "ダート"
            dist = int("".join(c for c in fname if c.isdigit()))
            baseline = None
            sires = {}
            with open(os.path.join(fdir, fname), "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        show = float(row["show_rate"])
                        starts = int(float(row["starts"]))
                    except (ValueError, KeyError):
                        continue
                    if row["factor_type"] == "baseline":
                        baseline = show
                    elif row["factor_type"] == "father_w":
                        sires[_norm(row["entity"])] = (show, starts, row["entity"])
            if baseline is not None and sires:
                courses[(venue, race_type, dist)] = {"baseline": baseline, "sires": sires}
    return courses


def segment_course_match(seg, venue, race_type, dist):
    """セグメントのコース条件のみ評価。コース条件が無ければ None (判別力なし)。"""
    checks = []
    if seg.get("surface"):
        checks.append(seg["surface"] == race_type)
    if seg.get("venue"):
        checks.append(seg["venue"] == venue)
    if seg.get("dist_list") is not None:
        checks.append(dist in seg["dist_list"] or
                      (seg.get("dist_min_alt") and dist >= seg["dist_min_alt"]))
    else:
        if seg.get("dist_min") is not None:
            checks.append(dist >= seg["dist_min"])
        if seg.get("dist_max") is not None:
            checks.append(dist <= seg["dist_max"])
    if seg.get("straight"):
        st = straight_type(venue, race_type, dist)
        if st is None:
            checks.append(False)
        else:
            checks.append(st == seg["straight"])
    if not checks:
        return None
    return all(bool(c) for c in checks)


def rule_course_match(rule, venue, race_type, dist):
    """いずれかのセグメントのコース条件が成立すれば該当。判別力ゼロなら None。"""
    any_disc = False
    for seg in rule.get("segments", []):
        m = segment_course_match(seg, venue, race_type, dist)
        if m is None:
            continue
        any_disc = True
        if m:
            return True
    return False if any_disc else None


def wmean(pairs):
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None


def main():
    with open(RULES_PATH, "r", encoding="utf-8-sig") as f:
        rules = json.load(f)["rules"]
    courses = load_all_courses()
    print(f"コース統計: {len(courses)}コース / ルール: {len(rules)}件")

    results = []
    skipped_no_course_cond = 0
    skipped_no_data = 0
    pooled = defaultdict(list)  # kubun -> [(dev, starts)] 該当セル / "neutral" 非該当セル

    for rule in rules:
        sire_n = _norm(rule["sire"])
        match_cells, other_cells = [], []
        for (venue, rt, dist), cd in courses.items():
            if sire_n not in cd["sires"]:
                continue
            show, starts, _disp = cd["sires"][sire_n]
            dev = show - cd["baseline"]
            m = rule_course_match(rule, venue, rt, dist)
            if m is None:
                continue
            (match_cells if m else other_cells).append((dev, starts))
        if all(rule_course_match(rule, v, r, d) is None for (v, r, d) in courses.keys()):
            skipped_no_course_cond += 1
            continue
        if len(match_cells) < 2 or len(other_cells) < 2:
            skipped_no_data += 1
            continue

        dev_m, dev_o = wmean(match_cells), wmean(other_cells)
        gap = dev_m - dev_o
        expected = gap > 0 if rule["kubun"] == "買い" else gap < 0
        results.append({
            "sire": rule["sire"], "kubun": rule["kubun"], "text": rule["text"],
            "n_match": len(match_cells), "n_other": len(other_cells),
            "starts_match": sum(w for _, w in match_cells),
            "dev_match": dev_m, "dev_other": dev_o, "gap": gap, "ok": expected,
        })
        for c in match_cells:
            pooled[rule["kubun"]].append(c)
        for c in other_cells:
            pooled["neutral"].append(c)

    print(f"検証可能ルール: {len(results)} / コース条件なしスキップ: {skipped_no_course_cond} / "
          f"データ不足スキップ: {skipped_no_data}")
    if not results:
        return

    n_ok = sum(1 for r in results if r["ok"])
    buy = [r for r in results if r["kubun"] == "買い"]
    sell = [r for r in results if r["kubun"] == "消し"]
    print(f"\n===== ルール単位の方向一致 =====")
    print(f"全体: {n_ok}/{len(results)} ({100.0 * n_ok / len(results):.0f}%) が本の主張と同方向")
    for label, grp in (("買い", buy), ("消し", sell)):
        if grp:
            ok = sum(1 for r in grp if r["ok"])
            gaps = [r["gap"] if label == "買い" else -r["gap"] for r in grp]
            print(f"  {label}: {ok}/{len(grp)} ({100.0 * ok / len(grp):.0f}%) / "
                  f"平均ギャップ(期待方向) {sum(gaps) / len(gaps):+.1f}pt")

    print(f"\n===== プール集計 (種牡馬×コースセル、出走数加重の複勝率乖離) =====")
    for key, label in (("買い", "買い該当コース"), ("消し", "消し該当コース"), ("neutral", "非該当コース")):
        cells = pooled.get(key, [])
        if cells:
            print(f"  {label:12s}: {wmean(cells):+.1f}pt (セル{len(cells)}, 出走{sum(w for _, w in cells)})")

    print("\n===== ルール別明細 (ギャップ = 該当コース乖離 − 非該当コース乖離) =====")
    print("  判定 | 区分 | 種牡馬                     | 該当n | ギャップ | 条件")
    for r in sorted(results, key=lambda x: (x["kubun"], -abs(x["gap"]))):
        mark = "○" if r["ok"] else "×"
        print(f"  {mark} | {r['kubun']} | {r['sire']:<14s} | {r['n_match']:3d} | "
              f"{r['gap']:+6.1f}pt | {r['text'][:38]}")

    print("\n[限界] 性別・母父・脚質等の馬レベル条件は集計に反映されず希釈される / "
          "db-keibaは各コース上位10種牡馬のみ掲載 / 検証はin-sample(2021-2025)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
