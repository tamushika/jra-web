"""
道悪適性 (当日馬場状態) のバックテスト
========================================
「当日が道悪 (稍重/重/不良) のとき、直近4走の道悪実績は着順を予測するか」を検証する。
本番のスコア計算が参照できるのは直近4走のみのため、それに合わせる。

--rule group (既定・現行不変):
  道悪好走 : 道悪での出走があり、いずれか3着以内
  道悪凡走 : 道悪での出走はあるが全て4着以下
  道悪未経験: 直近4走に道悪出走なし

--rule gap (T72): 本番 (api/scoring.py eval_wet_aptitude) と同じ「道悪ベスト着順 vs 良ベスト
着順」の gap ルールで better/worse/neutral/no_signal に分類する。--same-surface を付けると
当日と同じ芝/ダートの走だけを prior4 の集計対象にする (T72: ダート道悪と芝道悪は質が異なるため)。

プラセボ検証: 同じグループ分けを「当日良馬場」のレースにも適用。
道悪適性が本物なら道悪日だけ差が出るはず (良馬場でも差が出るなら単なる能力の代理)。

【使い方】
  python backtest_wet.py [--from 20240101 --to 20251231]
  python backtest_wet.py --rule gap [--same-surface] [--json outputs/t72/result.json]
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from backtest_ability import load_runs, parse_win_payout  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
WET = {"稍", "重", "不"}
GAP_GROUPS = ("better", "worse", "neutral", "no_signal")


def classify(prior4):
    """直近4走から道悪適性グループを判定 (--rule group)"""
    wet_runs = [p for p in prior4 if p["condition"] in WET]
    if not wet_runs:
        return "道悪未経験"
    if any(p["rank"] is not None and p["rank"] <= 3 for p in wet_runs):
        return "道悪好走"
    return "道悪凡走"


def classify_gap(prior4, cur_track, same_surface):
    """
    prior4 の各走を condition (1文字: 良/稍/重/不) で wet/dry に分け、本番と同じ gap ルールで判定する。
    same_surface=True のときは cur_track ('芝'/'ダート') と同じ track_type の走のみを対象にする。
    rank が None の走は除外。wet/dry の両方が揃わなければ "no_signal"。
    """
    wet_ranks, dry_ranks = [], []
    for p in prior4:
        if not p or p.get("rank") is None:
            continue
        cond = p.get("condition")
        if cond not in WET and cond != "良":
            continue
        if same_surface and p.get("track_type") != cur_track:
            continue
        (wet_ranks if cond in WET else dry_ranks).append(p["rank"])
    if not wet_ranks or not dry_ranks:
        return "no_signal"
    gap = min(wet_ranks) - min(dry_ranks)  # 負 = 道悪の方が着順が良い
    if gap <= -2:
        return "better"
    if gap >= 2:
        return "worse"
    return "neutral"


def bucket():
    return {"n": 0, "win": 0, "top3": 0, "payout": 0.0}


def add_to_bucket(b, cur):
    b["n"] += 1
    b["win"] += 1 if cur["rank"] == 1 else 0
    b["top3"] += 1 if cur["rank"] <= 3 else 0
    b["payout"] += parse_win_payout(cur["win_pay"], cur["rank"])


def bucket_summary(b):
    n = b["n"]
    if not n:
        return {"n": 0, "win_rate": 0.0, "show_rate": 0.0, "win_payout": 0.0}
    return {
        "n": n,
        "win_rate": 100.0 * b["win"] / n,
        "show_rate": 100.0 * b["top3"] / n,
        "win_payout": 100.0 * b["payout"] / (100.0 * n),
    }


def run_group_rule(by_horse, args):
    """既存の group 分類モード。出力・数値は変更しない。"""
    tables = {"道悪日 (稍/重/不)": defaultdict(bucket), "良馬場日 (プラセボ)": defaultdict(bucket)}

    for lst in by_horse.values():
        for i, cur in enumerate(lst):
            if cur["date"] < args.date_from or cur["rank"] is None or not cur["condition"]:
                continue
            prior4 = lst[max(0, i - 4):i]
            if not prior4:
                continue
            group = classify(prior4)
            day = "道悪日 (稍/重/不)" if cur["condition"] in WET else "良馬場日 (プラセボ)"
            add_to_bucket(tables[day][group], cur)

    for day, table in tables.items():
        total = bucket()
        for b in table.values():
            for k in total:
                total[k] += b[k]
        print(f"\n===== {day} (全体: n={total['n']}, "
              f"勝率{100.0*total['win']/max(total['n'],1):.1f}%, "
              f"複勝率{100.0*total['top3']/max(total['n'],1):.1f}%) =====")
        print("  グループ      | 出走    | 勝率   | 複勝率 | 単回収")
        for g in ("道悪好走", "道悪凡走", "道悪未経験"):
            b = table.get(g)
            if not b or b["n"] == 0:
                continue
            print(f"  {g:8s}| {b['n']:7d} | {100.0*b['win']/b['n']:5.1f}% | "
                  f"{100.0*b['top3']/b['n']:5.1f}% | {100.0*b['payout']/(100.0*b['n']):5.1f}%")

    print("\n[読み方] 道悪日にグループ間の差があり、良馬場日には差が小さい場合のみ"
          "「道悪適性」として有効 (良馬場でも同じ差が出るなら能力の代理に過ぎない)")


def run_gap_rule(by_horse, args):
    """T72: 本番と同じ gap ルール (better/worse/neutral/no_signal)。"""
    tables = {"道悪日 (稍/重/不)": defaultdict(bucket), "良馬場日 (プラセボ)": defaultdict(bucket)}
    by_surface = {"芝": defaultdict(bucket), "ダート": defaultdict(bucket)}

    for lst in by_horse.values():
        for i, cur in enumerate(lst):
            if cur["date"] < args.date_from or cur["rank"] is None or not cur["condition"]:
                continue
            prior4 = lst[max(0, i - 4):i]
            if not prior4:
                continue
            group = classify_gap(prior4, cur["track_type"], args.same_surface)
            is_wet_day = cur["condition"] in WET
            day = "道悪日 (稍/重/不)" if is_wet_day else "良馬場日 (プラセボ)"
            add_to_bucket(tables[day][group], cur)
            if is_wet_day and cur["track_type"] in by_surface:
                add_to_bucket(by_surface[cur["track_type"]][group], cur)

    result = {
        "params": {
            "rule": "gap",
            "same_surface": args.same_surface,
            "from": args.date_from,
            "to": args.date_to,
        },
        "wet_day": {},
        "dry_day": {},
        "wet_day_by_surface": {"芝": {}, "ダート": {}},
    }

    print(f"\n[--rule gap] same_surface={args.same_surface}")
    for day, table in tables.items():
        key = "wet_day" if day.startswith("道悪日") else "dry_day"
        total = bucket()
        for b in table.values():
            for k in total:
                total[k] += b[k]
        print(f"\n===== {day} (全体: n={total['n']}, "
              f"勝率{100.0*total['win']/max(total['n'],1):.1f}%, "
              f"複勝率{100.0*total['top3']/max(total['n'],1):.1f}%) =====")
        print("  グループ    | 出走    | 勝率   | 複勝率 | 単回収")
        for g in GAP_GROUPS:
            summary = bucket_summary(table.get(g, bucket()))
            result[key][g] = summary
            if summary["n"] == 0:
                continue
            print(f"  {g:10s}| {summary['n']:7d} | {summary['win_rate']:5.1f}% | "
                  f"{summary['show_rate']:5.1f}% | {summary['win_payout']:5.1f}%")

    print("\n----- 道悪日 内訳 (当日 芝/ダート) -----")
    for surface in ("芝", "ダート"):
        table = by_surface[surface]
        total = bucket()
        for b in table.values():
            for k in total:
                total[k] += b[k]
        print(f"\n  [{surface}] (全体: n={total['n']}, "
              f"勝率{100.0*total['win']/max(total['n'],1):.1f}%, "
              f"複勝率{100.0*total['top3']/max(total['n'],1):.1f}%)")
        for g in GAP_GROUPS:
            summary = bucket_summary(table.get(g, bucket()))
            result["wet_day_by_surface"][surface][g] = summary
            if summary["n"] == 0:
                continue
            print(f"    {g:10s}| {summary['n']:7d} | {summary['win_rate']:5.1f}% | "
                  f"{summary['show_rate']:5.1f}% | {summary['win_payout']:5.1f}%")

    print("\n[読み方] 道悪日で better/worse の単回収差が大きく、良馬場日 (プラセボ) では"
          "差が小さい場合のみ「道悪適性」として有効")

    if args.json_path:
        out_dir = os.path.dirname(args.json_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[json] 保存: {args.json_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20240101")
    ap.add_argument("--to", dest="date_to", default="20251231")
    ap.add_argument("--rule", choices=("group", "gap"), default="group",
                     help="group=直近4走の道悪好走/凡走/未経験 (既定・現行不変), "
                          "gap=本番と同じ道悪ベストvs良ベストのgap判定 (T72)")
    ap.add_argument("--same-surface", action="store_true",
                     help="--rule gap 専用: prior4 の集計を当日と同じ芝/ダートの走に限定する")
    ap.add_argument("--json", dest="json_path", default=None,
                     help="--rule gap 専用: 集計結果をJSONで保存するパス")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.date_from, args.date_to)
    conn.close()
    print(f"ロード: {len(runs)}行")

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    if args.rule == "gap":
        run_gap_rule(by_horse, args)
    else:
        run_group_rule(by_horse, args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
