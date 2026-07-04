"""
スコア重みバックテスト/チューナー
==================================
過去DB (api/past_data_v2.db, SQLite) の 2024-2025 平地レースに対して、
DBで検証可能なファクター (騎手/枠順/脚質) の部分スコアを再現計算し、
スコア分位バケット毎の複勝率・勝率・単勝回収率を検証する。

【使い方】
  python backtest_score.py                 # レポートのみ (現行重み)
  python backtest_score.py --from 2401 --to 2512
  python backtest_score.py --grid          # 重みグリッドサーチ (騎手/枠/脚質)
  python backtest_score.py --grid --write  # 最良重みを score_weights.json に書き戻し

【注意 (レポートにも出力)】
  1) 脚質は corner_4 (当該レースの実現4角位置) から導出しており楽観的なproxy。
     相対的な重み比較には有効だが絶対値は過大評価side。
  2) db-keibaファクター統計は2021-2025集計で評価期間と重複あり。
  3) DBの騎手名は4文字切り詰め (例: 佐々木大) → 前方一致で照合。
     照合率が80%未満の場合は正規化の見直しが先。
"""
import argparse
import itertools
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
import past_data_service as pds  # noqa: E402

WEIGHTS_PATH = os.path.join(API_DIR, "data_files", "common", "score_weights.json")
TUNABLE = ["jockey_w", "frame", "averunningstyle"]

# calculate_kyakushitsu (api/index.py:58-64) と同一の閾値
STYLE_BANDS = [
    (0.15, "◀◁◁◁"), (0.25, "◀◀◁◁"), (0.40, "◁◀◁◁"), (0.55, "◁◀◀◁"),
    (0.70, "◁◁◀◁"), (0.85, "◁◁◀◀"), (9.99, "◁◁◁◀"),
]


def derive_style_arrows(corner_4, total_horses):
    """4角位置比率 → 脚質矢印 (単走の実現値なので楽観proxy)"""
    m = re.search(r"\d+", str(corner_4 or ""))
    if not m or not total_horses:
        return None
    try:
        ratio = int(m.group()) / int(total_horses)
    except (ValueError, ZeroDivisionError):
        return None
    for th, arrows in STYLE_BANDS:
        if ratio <= th:
            return arrows
    return None


def parse_win_payout(odds_str, rank):
    """odds列: 1着=配当円 '260' / それ以外='(3.7)'。単勝回収額(100円賭け)を返す。"""
    if rank != 1:
        return 0.0
    s = str(odds_str or "").strip()
    if s.startswith("("):
        return 0.0
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_runners(conn, date_from, date_to):
    cur = conn.cursor()
    q = """SELECT date, place, track_type, distance, rank, horse_number,
                  corner_4, jockey, popularity, odds, total_horses, race_name
           FROM races
           WHERE date >= ? AND date <= ? AND rank IS NOT NULL"""
    if getattr(conn, "is_pg", False):
        q = q.replace("?", "%s")
    cur.execute(q, (date_from, date_to))
    runners = []
    for r in cur.fetchall():
        d = dict(r)
        if "障" in str(d.get("race_name", "")):
            continue
        try:
            d["rank"] = int(d["rank"])
            d["distance"] = int(d["distance"])
            d["total_horses"] = int(d["total_horses"])
        except (ValueError, TypeError):
            continue
        runners.append(d)
    return runners


class JockeyMatcher:
    """DB騎手名(4文字切詰) → db-keibaエンティティの前方一致キャッシュ照合"""

    def __init__(self):
        self.cache = {}

    def match(self, entity_map, db_name):
        key = (id(entity_map), db_name)
        if key in self.cache:
            return self.cache[key]
        name = scoring._norm(db_name)
        row = None
        if name:
            row = entity_map.get(name)
            if row is None and len(name) >= 3:
                hits = [v for ent, v in entity_map.items() if ent.startswith(name)]
                if len(hits) == 1:
                    row = hits[0]
        self.cache[key] = row
        return row


def partial_score(runner, table, weights, params, style_map, jmatcher, stats):
    baseline = table["baseline"]["show_rate"]
    total = 0.0
    scored_any = False

    # 騎手
    jmap = table.get("jockey_w") or {}
    stats["jockey_total"] += 1
    row = jmatcher.match(jmap, runner["jockey"])
    if row is not None:
        stats["jockey_hit"] += 1
        pts, _ = scoring._factor_points(row, baseline, params, weights.get("jockey_w", 0))
        if pts is not None:
            total += pts
            scored_any = True

    # 枠順
    waku = pds.calculate_waku(runner["horse_number"], runner["total_horses"])
    if waku:
        row = (table.get("frame") or {}).get(f"{waku}枠")
        if row is not None:
            pts, _ = scoring._factor_points(row, baseline, params, weights.get("frame", 0))
            if pts is not None:
                total += pts
                scored_any = True

    # 脚質
    arrows = derive_style_arrows(runner["corner_4"], runner["total_horses"])
    styles = style_map.get(arrows or "", [])
    if styles:
        smap = table.get("averunningstyle") or {}
        pts_list = []
        for s in styles:
            row = smap.get(scoring._norm(s))
            if row is not None:
                p, _ = scoring._factor_points(row, baseline, params, weights.get("averunningstyle", 0))
                if p is not None:
                    pts_list.append(p)
        if pts_list:
            total += sum(pts_list) / len(pts_list)
            scored_any = True

    return total if scored_any else None


def evaluate(runners, tables, weights, params, style_map, verbose=False):
    """全出走にスコア付与 → 5分位バケット集計"""
    jmatcher = JockeyMatcher()
    stats = defaultdict(int)
    scored = []
    for r in runners:
        table = tables.get((r["place"], r["track_type"], r["distance"]))
        if table is None:
            stats["no_table"] += 1
            continue
        s = partial_score(r, table, weights, params, style_map, jmatcher, stats)
        if s is not None:
            scored.append((s, r))

    if len(scored) < 100:
        return None, stats

    scored.sort(key=lambda x: x[0])
    n = len(scored)
    buckets = []
    for i in range(5):
        chunk = scored[i * n // 5:(i + 1) * n // 5]
        runs = len(chunk)
        top3 = sum(1 for _, r in chunk if r["rank"] <= 3)
        wins = sum(1 for _, r in chunk if r["rank"] == 1)
        payout = sum(parse_win_payout(r["odds"], r["rank"]) for _, r in chunk)
        buckets.append({
            "bucket": i + 1,
            "runs": runs,
            "score_min": round(chunk[0][0], 1),
            "score_max": round(chunk[-1][0], 1),
            "top3_rate": round(100.0 * top3 / runs, 1),
            "win_rate": round(100.0 * wins / runs, 1),
            "win_roi": round(100.0 * payout / (100.0 * runs), 1),
        })

    result = {"buckets": buckets, "n_scored": n, "scored": scored if verbose else None}
    return result, stats


def objective(buckets):
    """単調性 (隣接バケットの複勝率増加数) + 上下バケットのリフト"""
    rates = [b["top3_rate"] for b in buckets]
    mono = sum(1 for a, b in zip(rates, rates[1:]) if b > a)
    lift = rates[-1] - rates[0]
    return mono * 10 + lift, buckets[-1]["win_roi"]


def popularity_crosstab(scored):
    """人気帯 × スコア上位/下位半分 のクロス集計 (人気を超えるリフト確認)"""
    n = len(scored)
    half = n // 2
    lower, upper = scored[:half], scored[half:]
    bands = [("1-3人気", 1, 3), ("4-8人気", 4, 8), ("9人気以下", 9, 99)]
    out = []
    for label, lo, hi in bands:
        row = {"band": label}
        for part_label, part in (("スコア下位半分", lower), ("スコア上位半分", upper)):
            sel = []
            for _, r in part:
                m = re.search(r"\d+", str(r.get("popularity") or ""))
                if m and lo <= int(m.group()) <= hi:
                    sel.append(r)
            if sel:
                t3 = sum(1 for r in sel if r["rank"] <= 3)
                row[part_label] = f"複{100.0 * t3 / len(sel):.1f}% (n={len(sel)})"
            else:
                row[part_label] = "-"
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2401", help="開始 (YYMM or YYMMDD)")
    ap.add_argument("--to", dest="date_to", default="2512", help="終了")
    ap.add_argument("--grid", action="store_true", help="重みグリッドサーチ")
    ap.add_argument("--write", action="store_true", help="最良重みをscore_weights.jsonへ書き戻し")
    ap.add_argument("--sqlite", action="store_true",
                    help="ローカルSQLite(past_data_v2)を強制使用 (デフォルトは.envのNeonを優先)")
    args = ap.parse_args()
    if args.sqlite:
        os.environ.pop("DATABASE_URL", None)
    date_from = args.date_from.ljust(6, "0")
    date_to = args.date_to.ljust(6, "9")

    cfg = scoring.load_score_weights(API_DIR)
    params = cfg["params"]
    style_map = cfg["style_map"]
    base_weights = {k: cfg["weights"].get(k, 0) for k in TUNABLE}

    print(f"期間: {date_from} - {date_to} / 現行重み: {base_weights}")
    print("DB接続中...")
    conn = pds.get_db_connection(API_DIR)
    if conn is None:
        print("[ERROR] DB接続失敗")
        return
    runners = load_runners(conn, date_from, date_to)
    conn.close()
    print(f"対象出走: {len(runners)}件 (平地のみ)")

    # ファクターテーブルを (場, 芝ダ, 距離) 毎にプリロード
    tables = {}
    combos = {(r["place"], r["track_type"], r["distance"]) for r in runners}
    for place, tt, dist in combos:
        tables[(place, tt, dist)] = scoring.load_factor_table(place, tt, dist, API_DIR)
    n_table = sum(1 for v in tables.values() if v)
    print(f"ファクター表: {n_table}/{len(tables)} コース分ロード")

    # ── 現行重みレポート ──
    result, stats = evaluate(runners, tables, base_weights, params, style_map, verbose=True)
    if result is None:
        print("[ERROR] スコア付与できた出走が少なすぎます")
        return

    cov = 100.0 * stats["jockey_hit"] / max(stats["jockey_total"], 1)
    print(f"\n騎手照合率: {cov:.1f}% ({stats['jockey_hit']}/{stats['jockey_total']})")
    print("  ※db-keibaは各コース上位10騎手のみ掲載のため25〜35%程度が上限 (照合ロジック自体は切詰名でも前方一致でヒット確認済)")
    if cov < 15:
        print("  [WARN] 照合率が想定下限(15%)未満 — 正規化ロジックの見直しを推奨。")
    print(f"ファクター表なしでスキップ: {stats['no_table']}件")

    print(f"\n===== 現行重みのバケット別成績 (n={result['n_scored']}) =====")
    print("バケット | スコア範囲     | 出走   | 複勝率 | 勝率  | 単回収")
    for b in result["buckets"]:
        print(f"  {b['bucket']} (低→高) | {b['score_min']:6.1f}〜{b['score_max']:6.1f} | {b['runs']:6d} | "
              f"{b['top3_rate']:5.1f}% | {b['win_rate']:4.1f}% | {b['win_roi']:5.1f}%")

    print("\n===== 人気帯クロス集計 (人気を超えるリフトの確認) =====")
    for row in popularity_crosstab(result["scored"]):
        print(f"  {row['band']:8s} | 下位半分: {row['スコア下位半分']:22s} | 上位半分: {row['スコア上位半分']}")

    print("\n[注意] 脚質はcorner_4由来の楽観proxy / db-keiba統計(2021-2025)と期間重複あり")

    # ── グリッドサーチ ──
    if not args.grid:
        return
    print("\n===== グリッドサーチ (騎手/枠/脚質 × {0, 0.5, 1.0, 1.5, 2.0}) =====")
    grid_vals = [0.0, 0.5, 1.0, 1.5, 2.0]
    best = None
    for jw, fw, sw in itertools.product(grid_vals, repeat=3):
        w = {"jockey_w": jw, "frame": fw, "averunningstyle": sw}
        if all(v == 0 for v in w.values()):
            continue
        res, _ = evaluate(runners, tables, w, params, style_map)
        if res is None:
            continue
        score = objective(res["buckets"])
        if best is None or score > best[0]:
            best = (score, w, res["buckets"])
            print(f"  改善: {w} -> 単調性+リフト={score[0]:.1f}, 上位バケット単回収={score[1]:.1f}%")

    if best is None:
        print("[ERROR] グリッドサーチ失敗")
        return

    _best_score, best_w, best_buckets = best
    print(f"\n最良重み: {best_w}")
    print("バケット | 複勝率 | 勝率  | 単回収")
    for b in best_buckets:
        print(f"  {b['bucket']} | {b['top3_rate']:5.1f}% | {b['win_rate']:4.1f}% | {b['win_roi']:5.1f}%")

    if args.write:
        with open(WEIGHTS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        data["weights"].update(best_w)
        data["backtest_meta"] = {
            "tuned_factors": TUNABLE,
            "tuned_at": datetime.now().strftime("%Y-%m-%d"),
            "note": f"backtest {date_from}-{date_to}, 騎手照合率{cov:.0f}%, corner_4脚質proxy使用",
        }
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] {WEIGHTS_PATH} に書き戻しました")
    else:
        print("\n(--write 指定で score_weights.json に反映)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
