"""
好走条件 (criteria.csv) の有効性バックテスト
==============================================
本番と同一の analysis.evaluate_ultra / check_condition を、ability.db (馬名つき) から
再構築した馬データに適用し、◎/〇/△ 判定と該当数の予測力を検証する。

【使い方】
  python backtest_criteria.py                 # 2024-2025 レポート
  python backtest_criteria.py --from 20220101 --to 20231231
  python backtest_criteria.py --gen-weights   # ルール別重みを算出して
                                              # api/data_files/common/criteria_weights.json に保存

【制約 (レポートにも表示)】
  - 血統(父/母父/系)・生産者・減量記号の条件は ability.db に無いため、
    これらを含むルールは対象外 (全274ルール中 約51%が検証対象)
  - 実運用の◎は血統ルール込みのため、ここでの◎は「血統以外ルールの◎」
"""
import argparse
import csv as csv_mod
import os
import re
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import analysis  # noqa: E402
from backtest_ability import load_runs, parse_win_payout  # noqa: E402
from past_data_service import calculate_waku  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
UNEVALUABLE = re.compile(r"父|母父|系|産駒|生産者|減量")

VENUES = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"]


def load_filtered_criteria():
    """全場の criteria を読み、評価不能条件を含むルールを除外"""
    out = {}
    n_all, n_keep = 0, 0
    for venue in VENUES:
        rules = analysis.load_csv_criteria(venue, API_DIR)
        keep = []
        for rule in rules:
            n_all += 1
            conds = [rule[k] for k in ("c1", "c2", "c3") if analysis.is_valid_cond(rule[k])]
            if not conds or any(UNEVALUABLE.search(c) for c in conds):
                continue
            keep.append(rule)
            n_keep += 1
        out[venue] = keep
    print(f"検証対象ルール: {n_keep}/{n_all} (血統・生産者・減量条件を含むものは除外)")
    return out


def load_mawari():
    path = os.path.join(API_DIR, "data_files", "common", "mawari.csv")
    m = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv_mod.reader(f):
                if len(row) >= 2:
                    m[row[0].strip()] = row[1].strip()
    except Exception:
        pass
    return m


def attach_agari_rank(runs):
    """レース内の上がり順位 'n/N' を各行に付与"""
    by_race = defaultdict(list)
    for r in runs:
        by_race[(r["date"], r["place"], r["r"])].append(r)
    for members in by_race.values():
        with_agari = sorted((m for m in members if m["agari"]), key=lambda m: m["agari"])
        n = len(with_agari)
        for i, m in enumerate(with_agari, start=1):
            m["agari_rank_str"] = f"{i}/{n}"
        for m in members:
            m.setdefault("agari_rank_str", "-")


def build_h(cur, prev):
    """DB行 → 本番と同形式の馬dict (hist は前走1件)"""
    affi = "美浦" if "美" in str(cur.get("affi", "")) else ("栗東" if "栗" in str(cur.get("affi", "")) else "")
    h = {
        "name": cur["horse"], "num": cur["umaban"],
        "w_num": calculate_waku(cur["umaban"], cur["total_horses"]),
        "jock": cur["jockey"] or "", "kg": str(cur.get("kinryo") or ""),
        "sex_age": f"{cur.get('sex', '')}{cur.get('age', '')}",
        "affi": affi, "sire": "-", "bms": "-",
        "iv": "-", "hist": [],
    }
    if prev is not None:
        try:
            d1 = datetime.strptime(cur["date"], "%Y%m%d")
            d0 = datetime.strptime(prev["date"], "%Y%m%d")
            diff = (d1 - d0).days
            h["iv"] = "連闘" if diff <= 7 else f"中{diff // 7 - 1}週"
        except ValueError:
            pass
        surf = "芝" if prev["track_type"] == "芝" else "ダ"
        kin = prev.get("kinryo")
        raw = (f"2025年 1回{prev['place']}1日 {prev['rank']} 着 {prev['total_horses']} 頭 "
               f"{prev.get('popularity') or 9} 番人気 {prev['jockey'] or '-'} "
               f"{kin if kin else 55.0} kg {prev.get('race_name', '')}")
        h["hist"] = [{
            "raw": raw,
            "corners": str(prev.get("c4") or "-"),
            "agari_rank": prev.get("agari_rank_str", "-"),
            "course": f"{prev['distance']}{surf}",
            "weight": str(prev.get("weight") or ""),
            "race_name": prev.get("race_name", ""),
            "place": prev["place"],
        }]
    return h


WEIGHTS_PATH = os.path.join(API_DIR, "data_files", "common", "criteria_weights.json")


def gen_weights(by_rule, by_venue, args):
    """ルール別複勝率のベースライン乖離から重みを算出し JSON 保存。
    weight = clamp(1 + k × shrunk_dev, wmin, wmax)
    shrunk_dev = (ルール複勝率 − 場複勝率) × n / (n + n0)   ※小サンプル収縮"""
    import json
    weights, report = {}, []
    for (venue, rid), b in sorted(by_rule.items()):
        vb = by_venue.get(venue)
        if not vb or not vb["n"] or not b["n"]:
            continue
        base = vb["top3"] / vb["n"]
        rate = b["top3"] / b["n"]
        shrunk = (rate - base) * (b["n"] / (b["n"] + args.n0))
        w = round(min(args.wmax, max(args.wmin, 1.0 + args.k * shrunk)), 2)
        weights.setdefault(venue, {})[rid] = w
        report.append((w, venue, rid, b["n"], 100.0 * rate, 100.0 * base))

    payload = {
        "_meta": {
            "generated": datetime.now().strftime("%Y-%m-%d"),
            "period": f"{args.date_from}-{args.date_to}",
            "formula": "1 + k*(rule_top3 - venue_top3)*n/(n+n0)",
            "params": {"k": args.k, "n0": args.n0, "wmin": args.wmin, "wmax": args.wmax},
            "note": "血統・生産者・減量条件を含むルールは検証不能のため未収録 (重み1.0扱い)。CSV7列目の手動重みが優先される。",
        },
    }
    payload.update(weights)
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    n_rules = sum(len(v) for v in weights.values())
    print(f"\n重みを保存: {WEIGHTS_PATH} ({n_rules}ルール)")

    report.sort(reverse=True)
    print("\n===== ルール別重み 上位/下位 (重み | 場 項番 | 該当n | 複勝率 vs 場平均) =====")
    for w, venue, rid, n, rate, base in report[:10]:
        print(f"  {w:.2f} | {venue} 項番{rid:4s}| n={n:5d} | {rate:5.1f}% vs {base:4.1f}%")
    print("  ...")
    for w, venue, rid, n, rate, base in report[-10:]:
        print(f"  {w:.2f} | {venue} 項番{rid:4s}| n={n:5d} | {rate:5.1f}% vs {base:4.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20240101")
    ap.add_argument("--to", dest="date_to", default="20251231")
    ap.add_argument("--gen-weights", action="store_true",
                    help="ルール別重みを算出して criteria_weights.json に保存")
    ap.add_argument("--n0", type=float, default=20.0, help="収縮パラメータ (デフォルト20)")
    ap.add_argument("--k", type=float, default=4.0, help="乖離→重み変換係数 (デフォルト4)")
    ap.add_argument("--wmin", type=float, default=0.5)
    ap.add_argument("--wmax", type=float, default=1.5)
    args = ap.parse_args()

    criteria_map = load_filtered_criteria()
    mawari_map = load_mawari()

    conn = sqlite3.connect(DB_PATH)
    runs = load_runs(conn, args.date_from, args.date_to)
    conn.close()
    attach_agari_rank(runs)
    print(f"ロード: {len(runs)}行")

    by_horse = defaultdict(list)
    for r in runs:
        if r["horse"]:
            by_horse[r["horse"]].append(r)
    for lst in by_horse.values():
        lst.sort(key=lambda x: x["date"])

    # 集計器
    def bucket():
        return {"n": 0, "win": 0, "top3": 0, "payout": 0.0}

    by_grade = defaultdict(bucket)
    by_count = defaultdict(bucket)
    by_rule = defaultdict(bucket)   # (場, 項番) 別 — 重み算出用
    by_venue = defaultdict(bucket)  # 場別ベースライン
    n_eval = 0

    for horse, lst in by_horse.items():
        for i, cur in enumerate(lst):
            if cur["date"] < args.date_from or cur["rank"] is None:
                continue
            crits = criteria_map.get(cur["place"], [])
            if not crits:
                continue
            prev = lst[i - 1] if i > 0 else None
            h = build_h(cur, prev)
            r_ctx = {"type": cur["track_type"], "dist": cur["distance"],
                     "venue": cur["place"], "total_horses": cur["total_horses"],
                     "class": cur["race_class"]}
            grade, details, matches = analysis.evaluate_ultra(h, r_ctx, crits, {}, mawari_map)
            n_eval += 1

            pay = parse_win_payout(cur["win_pay"], cur["rank"])
            is_win = 1 if cur["rank"] == 1 else 0
            is_top3 = 1 if cur["rank"] <= 3 else 0
            buckets = [by_grade[grade or "無印"], by_count[min(len(details), 3)],
                       by_venue[cur["place"]]]
            buckets.extend(by_rule[(cur["place"], str(m["id"]))] for m in matches)
            for b in buckets:
                b["n"] += 1
                b["win"] += is_win
                b["top3"] += is_top3
                b["payout"] += pay

    print(f"評価出走: {n_eval}")

    def show(table, order, label):
        print(f"\n===== {label} =====")
        print("  区分   | 出走    | 勝率   | 複勝率 | 単回収")
        for k in order:
            b = table.get(k)
            if not b or b["n"] == 0:
                continue
            print(f"  {str(k):6s}| {b['n']:7d} | {100.0 * b['win'] / b['n']:5.1f}% | "
                  f"{100.0 * b['top3'] / b['n']:5.1f}% | {100.0 * b['payout'] / (100.0 * b['n']):5.1f}%")

    show(by_grade, ["◎", "〇", "△", "無印"], "判定グレード別 (血統以外ルールのみ)")
    show(by_count, [0, 1, 2, 3], "該当ルール数別 (3=3件以上)")

    if args.gen_weights:
        gen_weights(by_rule, by_venue, args)

    print("\n[注意] 血統・生産者・減量条件を含むルールは対象外 (実運用の◎はこれらも含む) / "
          "騎手名はDB側4文字切詰のため部分一致で判定")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
