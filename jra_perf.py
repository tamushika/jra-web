"""
予測実績ダッシュボード (ローカル専用・読み取り専用)
======================================================
data/jra_logging.db (予測run・予測値・オッズ・結果の追記ログ) を集計し、
「どのモデルバージョンで・どの程度 的中/回収できたか」を画面で確認する。

  - モデル別サマリ: model_name × model_version × 設定ハッシュごとに
    スコア1位馬の単勝/複勝の的中率・回収率 (100円賭け想定)
  - 日別内訳 / EV通知実績 / WIN5実績
  - レースごとに「最後の予測run」(締切に最も近い再計算) を採用して
    多重カウントを防ぐ
  - 結果 (race_results) は jra_ev.py がレース後に自動保存する。
    未確定分は「結果待ち」として件数表示

【起動】 python jra_perf.py  (または start_perf.bat)
【URL】  http://localhost:5004
"""
import json
import os
import sqlite3
import sys
import threading
import webbrowser
from collections import defaultdict

from flask import Flask, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "jra_logging.db")
PORT = 5004

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")


@app.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index_perf.html")


def _conn():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _score_of(p):
    """予測行の代表スコア (ml優先 → win5 → web)"""
    for k in ("ml_score", "win5_score", "web_score"):
        if p[k] is not None:
            return p[k]
    return None


def collect():
    conn = _conn()
    cur = conn.cursor()

    # 結果 (確定着順と払戻)
    results = {}
    for r in cur.execute("SELECT race_id, horse_id, finish_position, win_payout, place_payout FROM race_results"):
        results[(r["race_id"], r["horse_id"])] = dict(r)
    races_with_result = {k[0] for k in results}

    # 予測: レース×モデル群ごとに「最後のrun」だけ採用
    rows = list(cur.execute("""
        SELECT p.race_id, p.horse_id, p.predicted_at, p.ml_score, p.win5_score, p.web_score,
               p.calibrated_win_probability,
               r.prediction_run_id, r.app_name, r.model_name, r.model_version,
               substr(coalesce(r.config_hash,''),1,8) AS cfg, r.started_at
        FROM predictions p JOIN prediction_runs r ON r.prediction_run_id = p.prediction_run_id
    """))
    # (group, race) -> 最新run
    latest_run = {}
    for p in rows:
        g = (p["app_name"], p["model_name"] or "-", p["model_version"] or "-", p["cfg"])
        key = (g, p["race_id"])
        cur_best = latest_run.get(key)
        if cur_best is None or p["started_at"] > cur_best:
            latest_run[key] = p["started_at"]

    # レースごとのスコア1位馬 (最新runのみ)
    by_race = defaultdict(list)
    for p in rows:
        g = (p["app_name"], p["model_name"] or "-", p["model_version"] or "-", p["cfg"])
        if p["started_at"] != latest_run[(g, p["race_id"])]:
            continue
        by_race[(g, p["race_id"])].append(p)

    summary = defaultdict(lambda: {"races": 0, "settled": 0, "win": 0, "top3": 0,
                                   "tan_ret": 0, "fuku_ret": 0,
                                   "date_min": "9999", "date_max": "0000"})
    daily = defaultdict(lambda: {"settled": 0, "win": 0, "top3": 0, "tan_ret": 0, "fuku_ret": 0})
    for (g, race_id), plist in by_race.items():
        date = race_id.split(":")[0]
        s = summary[g]
        s["races"] += 1
        s["date_min"] = min(s["date_min"], date)
        s["date_max"] = max(s["date_max"], date)
        top = max(plist, key=lambda p: (_score_of(p) if _score_of(p) is not None else -1e9))
        res = results.get((race_id, top["horse_id"]))
        if res is None or res["finish_position"] is None:
            continue
        d = daily[(g, date)]
        for b in (s, d):
            b["settled"] += 1
            if res["finish_position"] == 1:
                b["win"] += 1
                b["tan_ret"] += res["win_payout"] or 0
            if res["finish_position"] <= 3:
                b["top3"] += 1
                b["fuku_ret"] += res["place_payout"] or 0

    def fmt(g, b):
        n = b["settled"]
        return {
            "app": g[0], "model": g[1], "version": g[2], "config": g[3],
            "period": f"{b.get('date_min','')}〜{b.get('date_max','')}" if "date_min" in b else "",
            "races": b.get("races", 0), "settled": n,
            "win_rate": round(100.0 * b["win"] / n, 1) if n else None,
            "top3_rate": round(100.0 * b["top3"] / n, 1) if n else None,
            "tan_roi": round(b["tan_ret"] / n, 1) if n else None,
            "fuku_roi": round(b["fuku_ret"] / n, 1) if n else None,
        }

    out_summary = [fmt(g, b) for g, b in sorted(summary.items())]
    out_daily = [dict(fmt(g, b), date=date) for (g, date), b in sorted(daily.items(), reverse=True)]

    # EV通知実績 (decision='alert' × 結果)
    ev_rows = []
    ev_sum = {"n": 0, "settled": 0, "win": 0, "top3": 0, "tan_ret": 0, "fuku_ret": 0}
    for e in cur.execute("""
        SELECT e.race_id, e.horse_id, e.ev, e.win_probability, e.threshold, e.evaluated_at,
               o.win_odds, o.popularity
        FROM ev_evaluations e LEFT JOIN odds_snapshots o ON o.odds_snapshot_id = e.odds_snapshot_id
        WHERE e.decision = 'alert' ORDER BY e.evaluated_at DESC"""):
        res = results.get((e["race_id"], e["horse_id"]))
        settled = res is not None and res["finish_position"] is not None
        ev_sum["n"] += 1
        if settled:
            ev_sum["settled"] += 1
            if res["finish_position"] == 1:
                ev_sum["win"] += 1
                ev_sum["tan_ret"] += res["win_payout"] or 0
            if res["finish_position"] <= 3:
                ev_sum["top3"] += 1
                ev_sum["fuku_ret"] += res["place_payout"] or 0
        ev_rows.append({
            "race_id": e["race_id"], "horse": e["horse_id"].split(":")[-1],
            "ev": e["ev"], "prob": e["win_probability"], "odds": e["win_odds"],
            "pop": e["popularity"], "at": (e["evaluated_at"] or "")[:16],
            "result": (res["finish_position"] if settled else None),
            "win_payout": (res["win_payout"] if settled else None),
            "place_payout": (res["place_payout"] if settled else None),
        })

    # WIN5実績
    win5 = []
    for w in cur.execute("""SELECT * FROM win5_predictions ORDER BY created_at DESC LIMIT 30"""):
        race_ids = json.loads(w["race_ids_json"])
        selections = json.loads(w["selections_json"])
        hit_flags, settled_all = [], True
        for rid, sel in zip(race_ids, selections):
            winner = next((int(h.split(":")[-1]) for (r, h), res in results.items()
                           if r == rid and res["finish_position"] == 1), None)
            if winner is None:
                settled_all = False
                hit_flags.append(None)
            else:
                nums = sel if isinstance(sel, list) else sel.get("nums", [])
                hit_flags.append(winner in [int(x) for x in nums])
        win5.append({
            "created_at": w["created_at"][:16], "budget": w["budget"],
            "points": w["total_points"], "est": w["estimated_hit_rate"],
            "method": w["allocation_method"], "single_axis": bool(w["single_axis"]),
            "hits": hit_flags, "all_settled": settled_all,
            "win5_hit": settled_all and all(hit_flags),
        })

    # 結果待ちレース数
    all_pred_races = {race_id for (_g, race_id) in by_race}
    pending = len(all_pred_races - races_with_result)

    conn.close()
    return {"summary": out_summary, "daily": out_daily[:60],
            "ev": {"sum": ev_sum, "rows": ev_rows[:100]},
            "win5": win5, "pending_races": pending}


@app.route("/api/perf")
def api_perf():
    try:
        return jsonify(collect())
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print("=" * 55)
    print("  予測実績ダッシュボード (読み取り専用)")
    print("=" * 55)
    print(f"  URL: {url}")
    print(f"  DB : {DB_PATH}")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
