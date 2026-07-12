"""
予測実績ダッシュボード (ローカル専用)
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
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory

from api.logging_store import LoggingStore
from api.port_guard import ensure_port_free
from api.result_service import normalize_race_date, sync_results_for_date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "jra_logging.db")
PORT = 5004

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")


@app.after_request
def _no_cache_html(resp):
    # UI更新のたびにブラウザキャッシュで旧画面が出る事故の防止 (HTMLのみ。APIは元々動的)
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


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


def _iso_date(compact):
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}" if compact else None


_JST = timezone(timedelta(hours=9))


def _to_jst(iso_str):
    """UTC ISO 8601文字列 ("...Z" / "+00:00" / タイムゾーン無しnaive) を
    JSTの "YYYY-MM-DD HH:MM" 文字列に変換する。パース不能な場合は元の文字列を返す。"""
    if not iso_str:
        return iso_str
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str


def _win5_hit_flags(race_ids, selections, results):
    """WIN5の各レースについて「選択馬に勝ち馬が含まれるか」を判定する。
    レース未確定 (勝ち馬不明) の場合は None、確定済みならTrue/Falseを返す。"""
    hit_flags, settled_all = [], True
    for rid, sel in zip(race_ids, selections):
        winner = next((int(h.split(":")[-1]) for (r, h), res in results.items()
                       if r == rid and res["finish_position"] == 1), None)
        if winner is None:
            settled_all = False
            hit_flags.append(None)
        else:
            nums = sel if isinstance(sel, list) else sel.get("horse_numbers", sel.get("nums", []))
            hit_flags.append(winner in [int(x) for x in nums])
    return hit_flags, settled_all


def collect(race_date=None):
    compact_date = normalize_race_date(race_date) if race_date else None
    conn = _conn()
    cur = conn.cursor()

    available_dates = [row[0] for row in cur.execute("""
        SELECT race_date FROM (
            SELECT race_date FROM races
            UNION SELECT substr(race_id,1,8) FROM predictions
            UNION SELECT substr(race_id,1,8) FROM race_results
        ) WHERE length(race_date)=8 AND race_date GLOB '[0-9]*'
        ORDER BY race_date DESC
    """)]

    # 結果 (確定着順と払戻)
    results = {}
    result_sql = """SELECT race_id, horse_id, horse_name, finish_position,
                           win_payout, place_payout FROM race_results"""
    result_params = ()
    if compact_date:
        result_sql += " WHERE substr(race_id,1,8)=?"
        result_params = (compact_date,)
    for r in cur.execute(result_sql, result_params):
        results[(r["race_id"], r["horse_id"])] = dict(r)
    races_with_result = {k[0] for k in results}
    winners = {race_id: item for (race_id, _), item in results.items()
               if item["finish_position"] == 1}

    race_meta = {}
    meta_sql = "SELECT race_id,race_date,venue,race_no,race_name FROM races"
    meta_params = ()
    if compact_date:
        meta_sql += " WHERE race_date=?"
        meta_params = (compact_date,)
    for r in cur.execute(meta_sql, meta_params):
        race_meta[r["race_id"]] = dict(r)

    # 予測: レース×モデル群ごとに「最後のrun」だけ採用
    prediction_sql = """
        SELECT p.race_id, p.horse_id, p.predicted_at, p.ml_score, p.win5_score, p.web_score,
               p.calibrated_win_probability, p.feature_snapshot_json,
               r.prediction_run_id, r.app_name, r.model_name, r.model_version,
               substr(coalesce(r.config_hash,''),1,8) AS cfg, r.started_at
        FROM predictions p JOIN prediction_runs r ON r.prediction_run_id = p.prediction_run_id
    """
    prediction_params = ()
    if compact_date:
        prediction_sql += " WHERE substr(p.race_id,1,8)=?"
        prediction_params = (compact_date,)
    rows = list(cur.execute(prediction_sql, prediction_params))
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
    daily = defaultdict(lambda: {"races": 0, "settled": 0, "win": 0, "top3": 0,
                                 "tan_ret": 0, "fuku_ret": 0})
    race_details = []
    for (g, race_id), plist in by_race.items():
        date = race_id.split(":")[0]
        s = summary[g]
        s["races"] += 1
        s["date_min"] = min(s["date_min"], date)
        s["date_max"] = max(s["date_max"], date)
        d = daily[(g, date)]
        d["races"] += 1
        top = max(plist, key=lambda p: (_score_of(p) if _score_of(p) is not None else -1e9))
        res = results.get((race_id, top["horse_id"]))
        winner = winners.get(race_id)
        try:
            feature = json.loads(top["feature_snapshot_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            feature = {}
        meta = race_meta.get(race_id, {})
        score = _score_of(top)
        race_details.append({
            "date": date,
            "race_id": race_id,
            "venue": meta.get("venue") or race_id.split(":")[1],
            "race_no": meta.get("race_no") or int(race_id.split(":")[-1]),
            "race_name": meta.get("race_name"),
            "app": g[0], "model": g[1], "version": g[2], "config": g[3],
            "horse": top["horse_id"].split(":")[-1],
            "horse_name": feature.get("horse_name"),
            "score": round(float(score), 3) if score is not None else None,
            "result": res["finish_position"] if res else None,
            "win_payout": res["win_payout"] if res else None,
            "place_payout": res["place_payout"] if res else None,
            "winner": winner["horse_id"].split(":")[-1] if winner else None,
            "winner_name": winner.get("horse_name") if winner else None,
        })
        if res is None or res["finish_position"] is None:
            continue
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
    ev_sql = """
        SELECT e.race_id, e.horse_id, e.ev, e.win_probability, e.threshold, e.evaluated_at,
               o.win_odds, o.popularity
        FROM ev_evaluations e LEFT JOIN odds_snapshots o ON o.odds_snapshot_id = e.odds_snapshot_id
        WHERE e.decision = 'alert'
    """
    ev_params = ()
    if compact_date:
        ev_sql += " AND substr(e.race_id,1,8)=?"
        ev_params = (compact_date,)
    ev_sql += " ORDER BY e.evaluated_at DESC"
    # 日別EV実績 (T30): 同一馬の重複アラート (15分前/5分前など) は馬単位に集約する
    ev_by_date = defaultdict(lambda: {"n": 0, "settled": 0, "win": 0, "top3": 0,
                                       "tan_ret": 0, "fuku_ret": 0})
    ev_seen_horses = set()
    for e in cur.execute(ev_sql, ev_params):
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
        horse_key = (e["race_id"], e["horse_id"])
        if horse_key not in ev_seen_horses:
            ev_seen_horses.add(horse_key)
            edate = e["race_id"].split(":")[0]
            eb = ev_by_date[edate]
            eb["n"] += 1
            if settled:
                eb["settled"] += 1
                if res["finish_position"] == 1:
                    eb["win"] += 1
                    eb["tan_ret"] += res["win_payout"] or 0
                if res["finish_position"] <= 3:
                    eb["top3"] += 1
                    eb["fuku_ret"] += res["place_payout"] or 0
        if len(ev_rows) < 100:
            ev_rows.append({
                "race_id": e["race_id"], "horse": e["horse_id"].split(":")[-1],
                "ev": e["ev"], "prob": e["win_probability"], "odds": e["win_odds"],
                "pop": e["popularity"], "at": _to_jst(e["evaluated_at"]) or "",
                "result": (res["finish_position"] if settled else None),
                "win_payout": (res["win_payout"] if settled else None),
                "place_payout": (res["place_payout"] if settled else None),
            })

    # WIN5実績
    win5 = []
    if compact_date:
        win5_sql = """SELECT * FROM win5_predictions WHERE race_ids_json LIKE ?
                      ORDER BY created_at DESC"""
        win5_params = (f'%"{compact_date}:%',)
    else:
        win5_sql = "SELECT * FROM win5_predictions ORDER BY created_at DESC LIMIT 30"
        win5_params = ()
    for w in cur.execute(win5_sql, win5_params):
        race_ids = json.loads(w["race_ids_json"])
        if compact_date and not any(str(rid).startswith(f"{compact_date}:") for rid in race_ids):
            continue
        if len(win5) >= 30:
            break
        selections = json.loads(w["selections_json"])
        hit_flags, settled_all = _win5_hit_flags(race_ids, selections, results)
        win5.append({
            "created_at": _to_jst(w["created_at"]), "budget": w["budget"],
            "points": w["total_points"], "est": w["estimated_hit_rate"],
            "method": w["allocation_method"], "single_axis": bool(w["single_axis"]),
            "hits": hit_flags, "all_settled": settled_all,
            "win5_hit": settled_all and all(hit_flags),
        })

    # 日別WIN5実績 (T30): 日付ごとに最新プラン (created_at最大) の的中状況を採る
    win5_by_date = {}
    for w in cur.execute("SELECT * FROM win5_predictions ORDER BY created_at DESC"):
        race_ids = json.loads(w["race_ids_json"])
        if not race_ids:
            continue
        wdate = str(race_ids[0]).split(":")[0]
        if compact_date and wdate != compact_date:
            continue
        if wdate in win5_by_date:
            continue  # 既に採用済みの日 (created_at DESCなのでこれより新しいものは無い)
        selections = json.loads(w["selections_json"])
        hit_flags, settled_all = _win5_hit_flags(race_ids, selections, results)
        win5_by_date[wdate] = {
            "hits": sum(1 for h in hit_flags if h), "total": len(hit_flags),
            "all_settled": settled_all, "win5_hit": settled_all and all(hit_flags),
        }

    # 日別実績 (T30): モデル別 + EV通知 + WIN5 を日付単位にまとめる
    daily_by_date = defaultdict(list)
    for entry in out_daily:
        daily_by_date[entry["date"]].append({
            "app": entry["app"], "model": entry["model"], "version": entry["version"],
            "config": entry["config"], "races": entry["races"], "settled": entry["settled"],
            "win_rate": entry["win_rate"], "tan_roi": entry["tan_roi"], "fuku_roi": entry["fuku_roi"],
        })
    all_days = sorted(set(daily_by_date) | set(ev_by_date) | set(win5_by_date), reverse=True)
    days = []
    for date in all_days:
        eb = ev_by_date.get(date)
        ev_out = None
        if eb:
            n = eb["settled"]
            ev_out = {
                "n": eb["n"], "settled": n, "win": eb["win"], "top3": eb["top3"],
                "tan_roi": round(eb["tan_ret"] / n, 1) if n else None,
                "fuku_roi": round(eb["fuku_ret"] / n, 1) if n else None,
            }
        days.append({
            "date": _iso_date(date),
            "models": daily_by_date.get(date, []),
            "ev": ev_out,
            "win5": win5_by_date.get(date),
        })

    # 結果待ちレース数
    all_pred_races = {race_id for (_g, race_id) in by_race}
    pending = len(all_pred_races - races_with_result)

    if compact_date:
        race_details.sort(key=lambda item: (item["venue"], item["race_no"], item["app"], item["model"]))
    else:
        race_details.sort(key=lambda item: (-int(item["date"]), item["venue"], item["race_no"],
                                            item["app"], item["model"]))

    conn.close()
    return {"summary": out_summary, "daily": out_daily[:60],
            "ev": {"sum": ev_sum, "rows": ev_rows},
            "win5": win5, "race_details": race_details[:300],
            "pending_races": pending,
            "selected_date": _iso_date(compact_date),
            "available_dates": [_iso_date(value) for value in available_dates],
            "days": days[:90]}


@app.route("/api/perf")
def api_perf():
    try:
        return jsonify(collect(request.args.get("date")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/results/sync", methods=["POST"])
def api_sync_results():
    data = request.get_json(silent=True) or {}
    race_date = data.get("date")
    if not race_date:
        return jsonify({"error": "date is required"}), 400
    try:
        result = sync_results_for_date(race_date, store=LoggingStore(DB_PATH))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    ensure_port_free(PORT, "予測実績ダッシュボード")
    url = f"http://localhost:{PORT}"
    print("=" * 55)
    print("  予測実績ダッシュボード")
    print("=" * 55)
    print(f"  URL: {url}")
    print(f"  DB : {DB_PATH}")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
