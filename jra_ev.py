"""
期待値レース監視 ローカル専用アプリ
====================================
期待値が高いレースを見逃さないための監視システム。

  1. 「解析開始」でその日の全レースを解析し、
     - WIN5 MLスコア (conditional logit) → レース内勝率 (的中率重視)
     - Web版スコア (オッズ不使用の複勝率ベース評価 = 回収率重視)
     の両面から「期待値の高い馬」をピックアップして一覧表示
  2. 各レースの発走15分前にオッズを自動再取得し、期待値馬がいれば1回目通知
  3. 発走5分前に再度オッズを更新し、期待値馬が残っていれば2回目通知
     (通知はブラウザ通知 + サウンド。ページを開いたままにしておくこと)

期待値の定義:
  EV = ML推定勝率 × 単勝オッズ (閾値以上でピックアップ、既定1.3)
  さらに「Web妙味」= Web版スコア上位なのに人気が低い馬 (市場の見落とし) をバッジ表示

バックテスト (backtest_ev.py, 確定オッズ・21-24学習CL):
  EV>=1.3: 2025年 単勝回収158.6% (499賭け) / 2026年OOS 100.1% (266賭け)、
           複勝回収は両年とも102-103%。EV>=1.2 は 119.6% / 90.2%

【起動】 python jra_ev.py  (または start_ev.bat)
【URL】  http://localhost:5003
"""
import itertools
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from index import analyze_race_url, build_matrix_data  # noqa: E402

PORT = 5003
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.jra.go.jp/"}

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")
CORS(app)

_LOCK = threading.RLock()
_ALERT_SEQ = itertools.count(1)
STATE = {
    "status": "idle",            # idle | analyzing | ready | error
    "error": "",
    "progress": {"done": 0, "total": 0},
    "started_at": "",
    "races": {},                 # rid -> race record
    "alerts": [],                # {id, ts, stage, rid, label, picks}
    "params": {"ev_threshold": 1.3, "max_odds": 50.0, "min_prob": 0.02},
}
_SCHEDULER_STARTED = [False]


@app.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index_ev.html")


# ─── 発見・解析 ──────────────────────────────────────────────────────────────

def find_entry_url():
    """JRAトップ/今週ページから任意のレースカードURLを1つ見つける"""
    for candidate in ("https://www.jra.go.jp/", "https://www.jra.go.jp/keiba/thisweek/"):
        try:
            res = requests.get(candidate, headers=HDRS, timeout=10)
            res.encoding = "cp932"
            soup = BeautifulSoup(res.text, "html.parser")
            fallback = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "accessD.html" not in href or "CNAME=" not in href:
                    continue
                from urllib.parse import urljoin
                full = urljoin("https://www.jra.go.jp/", href)
                if "dde" in href:
                    return full
                fallback = fallback or full
            if fallback:
                return fallback
        except Exception:
            continue
    return None


def _parse_start_time(race_info):
    """'【東京 11R】芝1600m 15:40発走　…' → datetime (本日)"""
    m = re.search(r"(\d{1,2}):(\d{2})発走", str(race_info))
    if not m:
        return None
    now = datetime.now()
    return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                       second=0, microsecond=0)


def _parse_race_num(race_info):
    m = re.search(r"【[^ ]+ (\d+)R】", str(race_info))
    return int(m.group(1)) if m else None


def _to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def compute_picks(horses, params):
    """slim馬リストに win_prob / ev / picked / web_value を付与し、pick数を返す"""
    scored = [h for h in horses if h.get("ml_score") is not None]
    probs = scoring.win_probs_from_ml_scores([h["ml_score"] for h in scored]) if scored else None

    # Web版スコアの順位 (降順)
    by_web = sorted([h for h in horses if h.get("web_score") is not None],
                    key=lambda h: -h["web_score"])
    web_rank = {id(h): i + 1 for i, h in enumerate(by_web)}

    n_picked = 0
    for h in horses:
        h["win_prob"] = None
        h["ev"] = None
        h["picked"] = False
        h["web_value"] = False
        odds = _to_float(h.get("odds"))
        pop = _to_float(h.get("pop"))
        wr = web_rank.get(id(h))
        if wr and pop and wr <= 3 and wr < pop:
            h["web_value"] = True
    if probs:
        for h, p in zip(scored, probs):
            h["win_prob"] = round(p, 4)
            odds = _to_float(h.get("odds"))
            if odds and odds > 1.0:
                h["ev"] = round(p * odds, 2)
                if (h["ev"] >= params["ev_threshold"] and odds <= params["max_odds"]
                        and p >= params["min_prob"]):
                    h["picked"] = True
                    n_picked += 1
    return n_picked


def analyze_one(url, params):
    """1レースを解析して監視レコードを返す"""
    result = analyze_race_url(url, "簡易")
    venue = result.get("venue")
    race_type = result.get("race_type")
    dist_val = result.get("dist_val")
    cfg5 = scoring.load_score_weights(API_DIR, "win5_weights.json")
    factor_table = scoring.load_factor_table(venue, race_type, dist_val, API_DIR)
    rc = {"type": race_type, "dist": dist_val, "venue": venue,
          "race_class": result.get("race_class", ""), "age_cond": "",
          "baba_cond": result.get("baba_cond", "")}

    horses = []
    for h in result.get("horses", []):
        ml, _det = scoring.compute_score_ml(h, rc, factor_table, cfg5)
        horses.append({
            "num": h.get("num"), "name": h.get("name"), "jock": h.get("jock"),
            "odds": h.get("odds"), "pop": h.get("pop"), "grade": h.get("grade", ""),
            "web_score": h.get("score"), "ml_score": ml,
        })
    n_picked = compute_picks(horses, params)

    start_dt = _parse_start_time(result.get("race_info"))
    return {
        "url": url, "venue": venue,
        "race_num": _parse_race_num(result.get("race_info")),
        "race_info": result.get("race_info", ""),
        "race_type": race_type, "dist": dist_val,
        "start_time": start_dt.strftime("%H:%M") if start_dt else "",
        "_start_dt": start_dt,
        "horses": horses, "n_picked": n_picked,
        "checked15": False, "checked5": False, "finished": False,
        "last_update": datetime.now().strftime("%H:%M:%S"),
    }


def _rid(rec):
    return f"{rec['venue']}_{rec['race_num']}"


def worker_analyze_all(params):
    try:
        entry = find_entry_url()
        if not entry:
            with _LOCK:
                STATE["status"] = "error"
                STATE["error"] = "本日の出馬表が見つかりません (開催日にお試しください)"
            return
        res = requests.get(entry, headers=HDRS, timeout=15)
        res.encoding = "shift_jis"
        matrix = build_matrix_data(BeautifulSoup(res.text, "html.parser"))
        # 本日分のみ (matrix には土日両日が混在し得る → 日付ラベルが今日のもの優先。
        # 判別不能ならすべて対象にし、発走時刻の過ぎたレースはスケジューラが無視する)
        today_md = f"{datetime.now().month}/{datetime.now().day}("
        venues = [v for v in matrix if today_md in v.get("text", "")] or matrix
        targets = [(v["text"], r["url"]) for v in venues for r in v["races"]]
        with _LOCK:
            STATE["progress"] = {"done": 0, "total": len(targets)}

        def run(t):
            label, url = t
            try:
                rec = analyze_one(url, params)
                with _LOCK:
                    STATE["races"][_rid(rec)] = rec
            except Exception as e:
                print(f"[WARN] 解析失敗 {label}: {e}")
            finally:
                with _LOCK:
                    STATE["progress"]["done"] += 1

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(run, targets))
        with _LOCK:
            STATE["status"] = "ready"
        _ensure_scheduler()
    except Exception as e:
        with _LOCK:
            STATE["status"] = "error"
            STATE["error"] = str(e)


# ─── スケジューラ (15分前/5分前チェック) ─────────────────────────────────────

def refresh_and_alert(rec, stage):
    """オッズを再取得して再判定。期待値馬がいれば通知を作成"""
    params = STATE["params"]
    try:
        new_rec = analyze_one(rec["url"], params)
    except Exception as e:
        print(f"[WARN] 再取得失敗 {_rid(rec)}: {e}")
        return False
    with _LOCK:
        for k in ("horses", "n_picked", "last_update"):
            rec[k] = new_rec[k]
        picks = [h for h in rec["horses"] if h["picked"]]
        if picks:
            STATE["alerts"].append({
                "id": next(_ALERT_SEQ),
                "ts": datetime.now().strftime("%H:%M:%S"),
                "stage": stage,
                "rid": _rid(rec),
                "label": f"{rec['venue']}{rec['race_num']}R {rec['start_time']}発走",
                "picks": [{"num": h["num"], "name": h["name"], "odds": h["odds"],
                           "win_prob": h["win_prob"], "ev": h["ev"],
                           "web_value": h["web_value"]} for h in picks],
            })
    return True


def scheduler_loop():
    while True:
        time.sleep(20)
        now = datetime.now()
        with _LOCK:
            recs = list(STATE["races"].values())
        for rec in recs:
            start = rec.get("_start_dt")
            if start is None or rec["finished"]:
                continue
            remain = (start - now).total_seconds()
            if remain < -60:
                rec["finished"] = True
                continue
            if not rec["checked15"] and remain <= 15 * 60:
                if refresh_and_alert(rec, 15) or remain <= 6 * 60:
                    rec["checked15"] = True
            elif not rec["checked5"] and remain <= 5 * 60:
                if refresh_and_alert(rec, 5) or remain <= 0:
                    rec["checked5"] = True


def _ensure_scheduler():
    with _LOCK:
        if _SCHEDULER_STARTED[0]:
            return
        _SCHEDULER_STARTED[0] = True
    threading.Thread(target=scheduler_loop, daemon=True).start()


# ─── API ─────────────────────────────────────────────────────────────────────

def _slim_state():
    races = []
    for rid, rec in STATE["races"].items():
        races.append({k: rec[k] for k in
                      ("venue", "race_num", "race_info", "start_time", "horses",
                       "n_picked", "checked15", "checked5", "finished", "last_update")}
                     | {"rid": rid})
    races.sort(key=lambda r: (r["start_time"] or "99:99", r["venue"]))
    return {"status": STATE["status"], "error": STATE["error"],
            "progress": STATE["progress"], "started_at": STATE["started_at"],
            "params": STATE["params"], "races": races}


@app.route("/api/analyze_start", methods=["POST"])
def api_analyze_start():
    data = request.json or {}
    with _LOCK:
        if STATE["status"] == "analyzing":
            return jsonify({"error": "解析実行中です"}), 409
        try:
            th = float(data.get("ev_threshold", STATE["params"]["ev_threshold"]))
            STATE["params"]["ev_threshold"] = max(0.8, min(3.0, th))
        except (ValueError, TypeError):
            pass
        STATE["status"] = "analyzing"
        STATE["error"] = ""
        STATE["races"] = {}
        STATE["alerts"] = []
        STATE["started_at"] = datetime.now().strftime("%H:%M:%S")
    threading.Thread(target=worker_analyze_all,
                     args=(STATE["params"],), daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/state")
def api_state():
    with _LOCK:
        return jsonify(_slim_state())


@app.route("/api/alerts")
def api_alerts():
    after = request.args.get("after", 0, type=int)
    with _LOCK:
        return jsonify({"alerts": [a for a in STATE["alerts"] if a["id"] > after]})


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print("=" * 55)
    print("  期待値レース監視 (Webスコア × MLスコア)")
    print("=" * 55)
    print(f"  URL: {url}")
    print("  ブラウザのタブを開いたままにすると 15分前/5分前に通知します")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
