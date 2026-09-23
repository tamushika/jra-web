"""
SPEC-T80c: mined_rules_v2.csv と mined_rules_v4.csv (枠割当修正後の再採掘) を
同一母集団・同一期間で比較するラッパースクリプト。

backtest_criteria.py はルールCSVのパスをCLIから切り替えられない
(analysis.load_csv_criteria が api/data_files/<場>/criteria/criteria.csv を
固定で読む) ため、mine_criteria.py が内部で使っている評価器
evaluate_rules (= backtest_criteria.py の build_h / analysis.check_condition /
past_data_service.calculate_waku をそのまま使う集計器) を re-export して
v2/v4 を切り替え適用する。mine_criteria.py 自体は変更しない。

出力: outputs/t80c/report.json, docs/T80c-waku-rules-report.md

【使い方】
  python -X utf8 backtest_t80c_rules_compare.py \
      --v2 mined_rules_v2.csv --v4 outputs/t80c/mined_rules_v4.csv \
      --json-out outputs/t80c/report.json --md-out docs/T80c-waku-rules-report.md
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import analysis  # noqa: E402
import pedigree_store  # noqa: E402
from backtest_ability import load_runs  # noqa: E402

import mine_criteria as mc  # noqa: E402  (re-uses evaluate_rules; not modified)

DB_PATH = mc.DB_PATH
DISCOVER_FROM = mc.DISCOVER_FROM  # "20210101"
PEDIGREE_CACHE_PATH = mc.PEDIGREE_CACHE_PATH

# 2024 (選抜期間) はリークがあるため参考値。primary_metric/gate の対象は
# EVAL_PERIODS (2025, 2026H1) のみ。
REFERENCE_PERIOD = ("2024(参考・リーク)", "20240101", "20241231")
EVAL_PERIODS = mc.EVAL_PERIODS  # (("2025", ...), ("2026H1", ...))
ALL_PERIODS = (REFERENCE_PERIOD,) + EVAL_PERIODS


def _sha256(path):
    if not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_rows(path):
    """CSVの生データ行 (ヘッダ除く) を文字列タプルとして返す (完全一致判定用)。"""
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if row:
                rows.append(tuple(row))
    return rows


def _content_key(rule):
    """点数を除いた実体キー (場/芝ダ/距離/種別/条件集合)。条件の順序は無視。"""
    return (rule["place"], rule["track_type"], rule["distance"], rule["kind"],
            frozenset(rule["conds"]))


def _has_waku_cond(rule_or_row_conds):
    return any("枠番" in str(c) for c in rule_or_row_conds)


def diff_rulesets(v2_path, v4_path):
    v2_raw = _raw_rows(v2_path)
    v4_raw = _raw_rows(v4_path)
    v2_raw_set = set(v2_raw)
    v4_raw_set = set(v4_raw)

    exact_match = v2_raw_set & v4_raw_set
    v2_only_raw = v2_raw_set - v4_raw_set
    v4_only_raw = v4_raw_set - v2_raw_set

    v2_rules = mc.load_rules(v2_path)
    v4_rules = mc.load_rules(v4_path)
    v2_content = {_content_key(r) for r in v2_rules}
    v4_content = {_content_key(r) for r in v4_rules}
    content_match = v2_content & v4_content
    v2_only_content = v2_content - v4_content
    v4_only_content = v4_content - v2_content

    def waku_count(rules):
        return sum(1 for r in rules if _has_waku_cond(r["conds"]))

    def waku_count_rows(rows, cond_idx=(3, 4, 5)):
        return sum(1 for row in rows if any("枠番" in row[i] for i in cond_idx if i < len(row)))

    def kind_count(rules, kind):
        return sum(1 for r in rules if r["kind"] == kind)

    return {
        "v2_total": len(v2_rules), "v4_total": len(v4_rules),
        "v2_buy": kind_count(v2_rules, "買い"), "v2_kill": kind_count(v2_rules, "消し"),
        "v4_buy": kind_count(v4_rules, "買い"), "v4_kill": kind_count(v4_rules, "消し"),
        "v2_waku_rules": waku_count(v2_rules), "v4_waku_rules": waku_count(v4_rules),
        "row_exact_match_n": len(exact_match),
        "row_v2_only_n": len(v2_only_raw),
        "row_v4_only_n": len(v4_only_raw),
        "row_v2_only_waku_n": waku_count_rows(v2_only_raw),
        "row_v4_only_waku_n": waku_count_rows(v4_only_raw),
        "content_match_n": len(content_match),
        "content_v2_only_n": len(v2_only_content),
        "content_v4_only_n": len(v4_only_content),
        "content_v2_only_waku_n": sum(1 for k in v2_only_content if any("枠番" in c for c in k[4])),
        "content_v4_only_waku_n": sum(1 for k in v4_only_content if any("枠番" in c for c in k[4])),
        "v2_only_rows_waku_examples": sorted(r for r in v2_only_raw if any("枠番" in r[i] for i in (3, 4, 5) if i < len(r)))[:20],
        "v4_only_rows_waku_examples": sorted(r for r in v4_only_raw if any("枠番" in r[i] for i in (3, 4, 5) if i < len(r)))[:20],
    }


def evaluate_ruleset(rules, courses, mawari_map, pedigree, sire_lineage):
    out = {}
    for period_name, date_from, date_to in ALL_PERIODS:
        rows = mc.evaluate_rules(rules, courses, mawari_map, period_name,
                                 date_from, date_to, pedigree=pedigree,
                                 sire_lineage=sire_lineage)
        out[period_name] = {row["kind"]: row for row in rows}
    return out


def build_markdown(report):
    lines = []
    lines.append("# T80c: 枠割当修正後の再採掘ルール (v4) と v2 の比較")
    lines.append("")
    lines.append(f"生成: {report['meta']['generated_at_utc']}")
    lines.append("")
    lines.append("採否の記述はしない (本レポートは事実の記録のみ、判定は上位モデル)。")
    lines.append("")
    lines.append("## 0. 不変条件の確認")
    lines.append("")
    lines.append(f"- ability.db sha256 (実行前): `{report['meta']['ability_db_sha256_before']}`")
    lines.append(f"- ability.db sha256 (実行後): `{report['meta']['ability_db_sha256_after']}`")
    lines.append(f"- mined_rules_v2.csv sha256: `{report['meta']['v2_sha256']}` (不変)")
    lines.append(f"- mined_rules_v4.csv sha256 (run1): `{report['meta']['v4_sha256_run1']}`")
    lines.append(f"- mined_rules_v4.csv sha256 (run2): `{report['meta']['v4_sha256_run2']}`")
    lines.append(f"- v4 再現性 (2回実行のsha256一致): **{report['meta']['v4_reproducible']}**")
    lines.append("")
    lines.append("## 1. 本数・差分")
    lines.append("")
    d = report["diff"]
    lines.append("| 項目 | v2 | v4 |")
    lines.append("|---|---|---|")
    lines.append(f"| 総本数 | {d['v2_total']} | {d['v4_total']} |")
    lines.append(f"| 買い | {d['v2_buy']} | {d['v4_buy']} |")
    lines.append(f"| 消し | {d['v2_kill']} | {d['v4_kill']} |")
    lines.append(f"| 枠番条件を含むルール数 | {d['v2_waku_rules']} | {d['v4_waku_rules']} |")
    lines.append("")
    lines.append("CSV行 (場,芝ダ,距離,条件1,条件2,条件3,種別,点数) の完全一致による比較:")
    lines.append("")
    lines.append(f"- 完全一致: {d['row_exact_match_n']} 本")
    lines.append(f"- v2のみ: {d['row_v2_only_n']} 本 (うち枠番条件を含む: {d['row_v2_only_waku_n']})")
    lines.append(f"- v4のみ: {d['row_v4_only_n']} 本 (うち枠番条件を含む: {d['row_v4_only_waku_n']})")
    lines.append("")
    lines.append("点数 (スコア) を除いた実体一致 (場,芝ダ,距離,種別,条件集合) による比較:")
    lines.append("")
    lines.append(f"- 実体一致 (点数は異なる場合を含む): {d['content_match_n']} 本")
    lines.append(f"- v2のみ (実体): {d['content_v2_only_n']} 本 (うち枠番条件を含む: {d['content_v2_only_waku_n']})")
    lines.append(f"- v4のみ (実体): {d['content_v4_only_n']} 本 (うち枠番条件を含む: {d['content_v4_only_waku_n']})")
    lines.append("")
    if d["v2_only_rows_waku_examples"] or d["v4_only_rows_waku_examples"]:
        lines.append("### 枠番条件を含む差分ルール (行完全一致基準、最大20件ずつ)")
        lines.append("")
        lines.append("v2のみ:")
        lines.append("")
        for row in d["v2_only_rows_waku_examples"]:
            lines.append(f"- {','.join(row)}")
        lines.append("")
        lines.append("v4のみ:")
        lines.append("")
        for row in d["v4_only_rows_waku_examples"]:
            lines.append(f"- {','.join(row)}")
        lines.append("")

    lines.append("## 2. 評価 (2025固定テスト / 2026H1確認、2024は参考値)")
    lines.append("")
    lines.append("各種別で1つ以上のルールに該当した馬を1頭1件として集計 (mine_criteria.evaluate_rules、"
                 "backtest_criteria.py と同じ build_h/analysis.check_condition/calculate_waku を使用)。")
    lines.append("")
    lines.append("| 期間 | ルール | 種別 | 本数 | 該当n | 複勝率 | 単回収 | 複回収 | 複勝配当欠損 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for period_name, _, _ in ALL_PERIODS:
        for set_name in ("v2", "v4"):
            for kind in ("買い", "消し"):
                row = report["eval"][set_name][period_name][kind]
                lines.append(
                    f"| {period_name} | {set_name} | {kind} | {row['rules']} | "
                    f"{row['matches']} | {row['show_rate']:.1f}% | {row['win_roi']:.1f}% | "
                    f"{row['place_roi']:.1f}% | {row['place_missing']} |")
    lines.append("")
    ref_period_name = REFERENCE_PERIOD[0]
    ref_rows = [report["eval"][s][ref_period_name][k]
               for s in ("v2", "v4") for k in ("買い", "消し")]
    if any(r["top3"] and r["place_missing"] >= r["top3"] for r in ref_rows):
        lines.append(
            "**注 (2024複勝回収について)**: ability.db の `race_payouts` (複勝) / "
            "`runs.fukusho_pay` は2024年分が0件 (直接確認済み: "
            "`race_payouts WHERE date LIKE '2024%' AND bet_type='複勝'` → 0行、"
            "2025年は10,276行)。そのため2024行の複勝回収0.0%は実測値ではなく"
            "データ欠損によるもの (複勝配当欠損 ≈ 該当複勝的中数)。2024の単勝回収・"
            "複勝率は参照可能だが、複勝回収は2024では評価不能。")
        lines.append("")
    lines.append("## 3. primary_metric / safety_metrics (登録値そのまま、判定はしない)")
    lines.append("")
    pm = report["primary_metric"]
    lines.append(f"- primary_metric (2025 買いルール複勝率 v4-v2): "
                 f"{pm['v4_show_rate']:.1f}% - {pm['v2_show_rate']:.1f}% = {pm['delta_pt']:+.1f}pt")
    lines.append("")
    lines.append("## 4. 実行時間")
    lines.append("")
    for k, v in report["timing_seconds"].items():
        lines.append(f"- {k}: {v:.1f}秒")
    lines.append("")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v2", default=mc.V2_RULES_PATH)
    ap.add_argument("--v4", default=os.path.join(BASE_DIR, "outputs", "t80c", "mined_rules_v4.csv"))
    ap.add_argument("--v4-sha-run1", default=None, help="1回目実行時のv4 sha256 (再現性報告用)")
    ap.add_argument("--v4-sha-run2", default=None, help="2回目実行時のv4 sha256 (再現性報告用)")
    ap.add_argument("--ability-sha-before", default=None)
    ap.add_argument("--json-out", default=os.path.join(BASE_DIR, "outputs", "t80c", "report.json"))
    ap.add_argument("--md-out", default=os.path.join(BASE_DIR, "docs", "T80c-waku-rules-report.md"))
    args = ap.parse_args()

    if not os.path.exists(PEDIGREE_CACHE_PATH):
        raise RuntimeError("pedigree_cache.json がありません。ネット再取得は行いません")

    t0 = time.time()
    ability_before = args.ability_sha_before or _sha256(DB_PATH)

    diff = diff_rulesets(args.v2, args.v4)

    pedigree = pedigree_store.load_all(use_cache=True)
    sire_lineage = analysis.load_sire_lineage(API_DIR)
    mawari_map = mc.load_mawari()

    t_load0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    eval_to = EVAL_PERIODS[-1][2]
    runs = load_runs(conn, DISCOVER_FROM, eval_to)
    mc.attach_place_payouts(conn, runs, REFERENCE_PERIOD[1], eval_to)
    conn.close()
    mc.attach_agari_rank(runs)
    courses = mc.build_course_runs(runs, DISCOVER_FROM, eval_to)
    t_load1 = time.time()

    v2_rules = mc.load_rules(args.v2)
    v4_rules = mc.load_rules(args.v4)

    t_eval0 = time.time()
    eval_v2 = evaluate_ruleset(v2_rules, courses, mawari_map, pedigree, sire_lineage)
    eval_v4 = evaluate_ruleset(v4_rules, courses, mawari_map, pedigree, sire_lineage)
    t_eval1 = time.time()

    ability_after = _sha256(DB_PATH)
    if ability_after != ability_before:
        raise RuntimeError("ability.db が実行中に変更されました (read-only違反)")

    v2_show = eval_v2["2025"]["買い"]["show_rate"]
    v4_show = eval_v4["2025"]["買い"]["show_rate"]

    report = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ability_db_sha256_before": ability_before,
            "ability_db_sha256_after": ability_after,
            "v2_path": os.path.abspath(args.v2),
            "v4_path": os.path.abspath(args.v4),
            "v2_sha256": _sha256(args.v2),
            "v4_sha256_run1": args.v4_sha_run1,
            "v4_sha256_run2": args.v4_sha_run2,
            "v4_reproducible": (args.v4_sha_run1 is not None and
                                args.v4_sha_run1 == args.v4_sha_run2),
        },
        "diff": diff,
        "eval": {"v2": eval_v2, "v4": eval_v4},
        "notes": {
            "2024_place_return_caveat": (
                "ability.db has 0 rows of 複勝(place) payout data for 2024 "
                "(race_payouts WHERE date LIKE '2024%' AND bet_type='複勝' -> 0; "
                "2025 -> 10276). The 2024 place_roi of 0.0% in this report is a "
                "data-absence artifact, not a measured value; place_missing for "
                "2024 rows is approximately equal to the top3 count."
            ),
        },
        "primary_metric": {
            "definition": "2025年の買いルール該当馬複勝率 (v4 - v2)",
            "v2_show_rate": v2_show, "v4_show_rate": v4_show,
            "delta_pt": v4_show - v2_show,
        },
        "timing_seconds": {
            "diff_rulesets": t_load0 - t0,
            "load_runs_and_courses": t_load1 - t_load0,
            "evaluate_rules_v2_and_v4": t_eval1 - t_eval0,
            "total": time.time() - t0,
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] {args.json_out}")

    md = build_markdown(report)
    with open(args.md_out, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print(f"[OK] {args.md_out}")

    print(json.dumps(diff, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
