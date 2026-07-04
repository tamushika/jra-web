"""
WIN5予想 ローカル専用アプリ
============================
スコアリング (勝率ベース) + コース荒れ度 (course_upset.csv) で
WIN5の買い目を点数指定 (50/100/150/200) で機械的に組むローカルアプリ。

【起動】 python jra_win5.py  (または start_win5.bat)
【URL】  http://localhost:5002

api/index.py の解析パイプライン (analyze_race_url) と
api/scoring.py (win5_weights.json / allocate_picks) を再利用する。
"""
import csv
import json
import os
import re
import sys
import threading
import webbrowser

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from scoring import VENUE_SLUG_MAP  # noqa: E402
from index import analyze_race_url, _scrape_win5_target, _find_win5_urls  # noqa: E402

PORT = 5002

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")
CORS(app)


@app.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index_win5.html")


# ─── 荒れ度 (course_upset.csv) ───────────────────────────────────────────────

_UPSET_CACHE = {"map": None}


def load_upset_map():
    """全場の course_upset.csv → {(場,芝ダ,距離): (rank, score)} + 場全体 (場,'*',0)"""
    if _UPSET_CACHE["map"] is not None:
        return _UPSET_CACHE["map"]
    upset = {}
    root = os.path.join(API_DIR, "data_files")
    for venue, slug in VENUE_SLUG_MAP.items():
        if slug == "common":
            continue
        path = os.path.join(root, slug, "course_upset.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    name = re.sub(r"（[^）]*）", "", str(row.get("course_name", ""))).strip()
                    try:
                        score = float(row["upset_score"])
                        rank = str(row["upset_rank"]).strip()
                    except (KeyError, ValueError):
                        continue
                    m = re.match(rf"^{venue}(芝|ダート)(\d+)m?$", name)
                    if m:
                        key = (venue, m.group(1), int(m.group(2)))
                    elif name == venue:
                        key = (venue, "*", 0)
                    else:
                        continue
                    if key not in upset or score > upset[key][1]:
                        upset[key] = (rank, score)
        except Exception:
            continue
    _UPSET_CACHE["map"] = upset
    return upset


def get_upset(venue, race_type, dist):
    """(rank, score) を返す。
    コース行 → 同場同surfaceの最近距離コース → 場全体行 → None の順にフォールバック。"""
    upset = load_upset_map()
    hit = upset.get((venue, race_type, dist))
    if hit:
        return hit[0], hit[1]
    # 同場・同surface で距離が最も近いコース (例: 小倉ダ1800 → 小倉ダ1700)
    candidates = [(abs(d - dist), v) for (ven, rt, d), v in upset.items()
                  if ven == venue and rt == race_type and d > 0]
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1][0], candidates[0][1][1]
    hit = upset.get((venue, "*", 0))
    if hit:
        return hit[0], hit[1]
    return None, None


# ─── API ─────────────────────────────────────────────────────────────────────

@app.route("/api/win5_races", methods=["GET"])
def win5_races():
    """WIN5対象5レースの取得 + カードURL解決 (web版と同形状)"""
    try:
        target, err = _scrape_win5_target()
        if not target:
            return jsonify({"success": False,
                            "error": err or "WIN5対象レース情報を取得できませんでした (発表前の可能性)。URLを手入力してください。"})
        url_map = _find_win5_urls(target["races"], target.get("date_yyyymmdd", ""))
        for r in target["races"]:
            r["url"] = url_map.get(r["idx"], "")
        return jsonify({"success": True, **target})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/win5_analyze_one", methods=["POST"])
def win5_analyze_one():
    """1レースを解析し WIN5用スコアを付与して返す (フロントが5回呼ぶ)"""
    data = request.json or {}
    idx = data.get("idx", 0)
    url = data.get("url")
    if not url:
        return jsonify({"error": "urlが必要です"}), 400
    try:
        result = analyze_race_url(url, "簡易")

        # WIN5用スコア (勝率ベース) で上書き計算
        cfg5 = scoring.load_score_weights(API_DIR, "win5_weights.json")
        venue = result.get("venue")
        race_type = result.get("race_type")
        dist_val = result.get("dist_val")
        factor_table = scoring.load_factor_table(venue, race_type, dist_val, API_DIR)
        rc = {"type": race_type, "dist": dist_val, "venue": venue,
              "race_class": result.get("race_class", ""), "age_cond": "",
              "baba_cond": result.get("baba_cond", "")}
        for h in result.get("horses", []):
            h["score"], h["score_details"] = scoring.compute_score(h, rc, factor_table, cfg5)

        rank, score = get_upset(venue, race_type, dist_val)
        horses = [{
            "num": h.get("num"), "name": h.get("name"), "jock": h.get("jock"),
            "sire": h.get("sire"), "odds": h.get("odds"), "pop": h.get("pop"),
            "grade": h.get("grade", ""),
            "score": h.get("score"), "score_details": h.get("score_details", []),
        } for h in result.get("horses", [])]

        return jsonify({
            "success": True, "idx": idx,
            "race_info": result.get("race_info"),
            "venue": venue, "race_type": race_type, "dist_val": dist_val,
            "race_class": result.get("race_class"),
            "upset_rank": rank, "upset_score": score,
            "horses": horses,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/win5_kaime", methods=["POST"])
def win5_kaime():
    """5レースのスコア + 荒れランク + 点数上限 → 買い目配分。
    single_axis=true で「スコア1-2位差が最大のレース」を1頭固定(軸)にする。"""
    data = request.json or {}
    races = data.get("races", [])
    points = int(data.get("points", 100))
    single_axis = bool(data.get("single_axis"))
    if len(races) != 5:
        return jsonify({"error": "5レース分のデータが必要です"}), 400

    try:
        cfg5 = scoring.load_score_weights(API_DIR, "win5_weights.json")
        alloc = cfg5.get("allocation", {})
        coverage = alloc.get("coverage", {})
        max_picks = alloc.get("max_picks_per_race", 8)

        ranks = [r.get("upset_rank") for r in races]

        # 軸レース選定: スコア1位と2位の差 (マージン) が最大のレース
        axis_idx, axis_margin = None, None
        if single_axis:
            margins = []
            for race in races:
                hs = sorted([h["score"] for h in race.get("horses", [])
                             if h.get("score") is not None], reverse=True)
                margins.append(hs[0] - hs[1] if len(hs) >= 2 else -999)
            axis_idx = max(range(5), key=lambda i: margins[i])
            axis_margin = round(margins[axis_idx], 1)

        picks, est = scoring.allocate_picks(
            ranks, coverage, points, max_picks,
            fixed={axis_idx} if axis_idx is not None else None)

        out = []
        for i, race in enumerate(races):
            horses = [h for h in race.get("horses", []) if h.get("score") is not None]
            horses.sort(key=lambda h: -h["score"])
            k = min(picks[i], len(horses)) if horses else 0
            sel = horses[:k]
            curve = coverage.get(ranks[i]) or coverage.get("default") or []
            cov = curve[k - 1] if 0 < k <= len(curve) else None
            out.append({
                "idx": i, "venue": race.get("venue"),
                "race_info": race.get("race_info", ""),
                "upset_rank": ranks[i], "k": k,
                "coverage": cov,
                "is_axis": i == axis_idx,
                "horses": [{"num": h["num"], "name": h["name"], "score": h["score"],
                            "odds": h.get("odds"), "pop": h.get("pop")} for h in sel],
            })

        total = 1
        for r in out:
            total *= max(r["k"], 1)
        formula = "×".join(str(max(r["k"], 1)) for r in out) + f"={total}点"
        return jsonify({
            "success": True, "picks": out,
            "total_points": total, "formula": formula,
            "est_hit_rate": round(est, 5),
            "budget": points,
            "single_axis": single_axis,
            "axis_idx": axis_idx, "axis_margin": axis_margin,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ─── 起動 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print("=" * 55)
    print("  WIN5予想 (スコアリング + 荒れ度配分)")
    print("=" * 55)
    print(f"  URL: {url}")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=PORT, debug=False)
