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
from logging_store import LoggingStore, config_hash  # noqa: E402
from port_guard import ensure_port_free  # noqa: E402
from prediction_logging import log_race_prediction  # noqa: E402

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

VENUE_CODE = {"札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
              "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10"}


def _fill_missing_from_siblings(races, url_map, date8):
    """URL未解決のレースを、同場・同日の取得済みカードページのレースナビから補完する。"""
    import requests as _rq
    for r in races:
        if url_map.get(r["idx"]):
            continue
        v_code = VENUE_CODE.get(r["venue"])
        if not v_code:
            continue
        sibling = next((url_map[o["idx"]] for o in races
                        if o["venue"] == r["venue"] and url_map.get(o["idx"])), None)
        if not sibling:
            continue
        try:
            res = _rq.get(sibling, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            res.encoding = "shift_jis"
            pat = (rf'accessD\.html\?CNAME=(pw01dde\d{{2}}{v_code}\d{{8}}'
                   rf'{int(r["race_num"]):02d}{date8}/\w+)')
            m = re.search(pat, res.text)
            if m:
                url_map[r["idx"]] = f"https://www.jra.go.jp/JRADB/accessD.html?CNAME={m.group(1)}"
        except Exception:
            continue
    return url_map


@app.route("/api/win5_races", methods=["GET"])
def win5_races():
    """WIN5対象5レースの取得 + カードURL解決 (web版と同形状 + 兄弟レース補完)"""
    try:
        target, err = _scrape_win5_target()
        if not target:
            return jsonify({"success": False,
                            "error": err or "WIN5対象レース情報を取得できませんでした (発表前の可能性)。URLを手入力してください。"})
        date8 = target.get("date_yyyymmdd", "")
        url_map = _find_win5_urls(target["races"], date8)
        url_map = _fill_missing_from_siblings(target["races"], url_map, date8)
        for r in target["races"]:
            r["url"] = url_map.get(r["idx"], "")
        return jsonify({"success": True, **target})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def analyze_one_core(idx, url):
    """1レースを解析し WIN5用スコアを付与した dict を返す (エンドポイント/監視共用)"""
    result = analyze_race_url(url, "簡易")
    return _analyze_one_body(idx, url, result)


@app.route("/api/win5_analyze_one", methods=["POST"])
def win5_analyze_one():
    """1レースを解析し WIN5用スコアを付与して返す (フロントが5回呼ぶ)"""
    data = request.json or {}
    idx = data.get("idx", 0)
    url = data.get("url")
    if not url:
        return jsonify({"error": "urlが必要です"}), 400
    try:
        return jsonify(analyze_one_core(idx, url))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


def _analyze_one_body(idx, url, result):
    if True:  # 旧エンドポイント本体 (インデント維持のため)

        # WIN5用スコアで上書き計算 (use_ml=true かつモデル配置済みならML、なければ手調整)
        cfg5 = scoring.load_score_weights(API_DIR, "win5_weights.json")
        venue = result.get("venue")
        race_type = result.get("race_type")
        dist_val = result.get("dist_val")
        factor_table = scoring.load_factor_table(venue, race_type, dist_val, API_DIR)
        rc = {"type": race_type, "dist": dist_val, "venue": venue,
              "race_class": result.get("race_class", ""), "age_cond": "",
              "baba_cond": result.get("baba_cond", "")}
        use_ml = cfg5.get("use_ml", False)
        for h in result.get("horses", []):
            if use_ml:
                h["score"], h["score_details"] = scoring.compute_score_ml(h, rc, factor_table, cfg5)
            else:
                h["score"], h["score_details"] = scoring.compute_score(h, rc, factor_table, cfg5)

        rank, score = get_upset(venue, race_type, dist_val)
        horses = [{
            "num": h.get("num"), "name": h.get("name"), "jock": h.get("jock"),
            "sire": h.get("sire"), "odds": h.get("odds"), "pop": h.get("pop"),
            "grade": h.get("grade", ""),
            "current_weight": h.get("current_weight"), "weight_change": h.get("weight_change"),
            "weight_source": h.get("weight_source"),
            "weight_fallback": h.get("weight_source") not in ("jra_live", "jra_live_cache"),
            "pace_fit": h.get("_pace_fit"), "pace_fit_source": h.get("pace_fit_source"),
            "score": h.get("score"), "score_details": h.get("score_details", []),
        } for h in result.get("horses", [])]

        output = {
            "success": True, "idx": idx,
            "race_info": result.get("race_info"),
            "race_date": result.get("race_date"), "race_num": result.get("race_num"),
            "venue": venue, "race_type": race_type, "dist_val": dist_val,
            "race_class": result.get("race_class"),
            "upset_rank": rank, "upset_score": score,
            "horses": horses,
        }
        try:
            log_race_prediction(
                output, app_name="win5", config=cfg5, model_name="win5_score",
                model_version=cfg5.get("version", "unknown"), trigger_type="manual",
                base_dir=API_DIR,
            )
        except Exception as log_exc:
            output["logging_warning"] = f"{type(log_exc).__name__}: {log_exc}"
            print(f"[WARN] WIN5 prediction logging failed: {output['logging_warning']}")
        return output


@app.route("/api/win5_kaime", methods=["POST"])
def win5_kaime():
    """5レースのスコア + 荒れランク + 点数上限 → 買い目配分。
    MLモデルが conditional logit の場合は「レース固有のsoftmax勝率」で配分
    (2025年OOSで荒れランク平均配分の的中率5.0%→7.9% (150点))。
    それ以外は従来の荒れランク別カバレッジ平均で配分。
    single_axis=true で軸レースを1頭固定 (prob配分では最大勝率レース /
    rank配分ではスコア1-2位差最大レース)。"""
    data = request.json or {}
    races = data.get("races", [])
    points = int(data.get("points", 100))
    single_axis = bool(data.get("single_axis"))
    if len(races) != 5:
        return jsonify({"error": "5レース分のデータが必要です"}), 400
    try:
        return jsonify(build_kaime(races, points, single_axis))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


def build_kaime(races, points, single_axis):
    """買い目配分の本体 (エンドポイント/締切前監視の共用)"""
    if True:
        cfg5 = scoring.load_score_weights(API_DIR, "win5_weights.json")
        alloc = cfg5.get("allocation", {})
        coverage = alloc.get("coverage", {})
        max_picks = alloc.get("max_picks_per_race", 8)

        ranks = [r.get("upset_rank") for r in races]
        sorted_horses = []
        for race in races:
            horses = [h for h in race.get("horses", []) if h.get("score") is not None]
            horses.sort(key=lambda h: -h["score"])
            sorted_horses.append(horses)

        # レース固有勝率 (conditional logit モデル時のみ算出可能)
        prob_lists = [scoring.win_probs_from_ml_scores([h["score"] for h in hs]) if hs else None
                      for hs in sorted_horses]
        use_prob = all(p for p in prob_lists)
        alloc_method = "prob" if use_prob else "rank"

        # 軸レース選定
        axis_idx, axis_margin = None, None
        if single_axis:
            if use_prob:
                axis_idx = max(range(5), key=lambda i: prob_lists[i][0])
                axis_margin = round(prob_lists[axis_idx][0] * 100, 1)  # 勝率%
            else:
                margins = []
                for hs in sorted_horses:
                    margins.append(hs[0]["score"] - hs[1]["score"] if len(hs) >= 2 else -999)
                axis_idx = max(range(5), key=lambda i: margins[i])
                axis_margin = round(margins[axis_idx], 1)

        fixed = {axis_idx} if axis_idx is not None else None
        if use_prob:
            picks, est = scoring.allocate_picks_prob(prob_lists, points, max_picks, fixed=fixed)
        else:
            picks, est = scoring.allocate_picks(ranks, coverage, points, max_picks, fixed=fixed)

        out = []
        for i, race in enumerate(races):
            horses = sorted_horses[i]
            k = min(picks[i], len(horses)) if horses else 0
            sel = horses[:k]
            if use_prob:
                cov = round(sum(prob_lists[i][:k]), 4) if k else None
            else:
                curve = coverage.get(ranks[i]) or coverage.get("default") or []
                cov = curve[k - 1] if 0 < k <= len(curve) else None
            out.append({
                "idx": i, "venue": race.get("venue"),
                "race_info": race.get("race_info", ""),
                "upset_rank": ranks[i], "k": k,
                "coverage": cov,
                "is_axis": i == axis_idx,
                "horses": [{"num": h["num"], "name": h["name"], "score": h["score"],
                            "odds": h.get("odds"), "pop": h.get("pop"),
                            "win_prob": (round(prob_lists[i][j], 4) if use_prob else None)}
                           for j, h in enumerate(sel)],
            })

        total = 1
        for r in out:
            total *= max(r["k"], 1)
        formula = "×".join(str(max(r["k"], 1)) for r in out) + f"={total}点"

        # 見送り基準 (オプトイン): skip_threshold 設定時のみ見送り推奨を表示。
        # 2025年検証では閾値導入で回収プロキシが悪化 (堅い週ほど配当が安い) → デフォルト無効
        skip_th = alloc.get("skip_threshold") if use_prob else None
        skip_recommended = bool(skip_th) and est < skip_th
        est_ref = (alloc.get("est_reference") or {}).get(str(points)) if use_prob else None

        result = {
            "success": True, "picks": out,
            "total_points": total, "formula": formula,
            "est_hit_rate": round(est, 5),
            "est_reference": est_ref,  # [2025年実測の中央値, 上位25%点]
            "budget": points,
            "single_axis": single_axis,
            "axis_idx": axis_idx, "axis_margin": axis_margin,
            "alloc_method": alloc_method,
            "skip_threshold": skip_th,
            "skip_recommended": skip_recommended,
        }
        try:
            run_ids = [str((race.get("_logging") or {}).get("prediction_run_id") or "") for race in races]
            race_ids = [str((race.get("_logging") or {}).get("race_id") or "") for race in races]
            if not all(run_ids) or not all(race_ids):
                raise ValueError("all five races must have common logging context")
            idempotency_key = config_hash({
                "prediction_run_ids": run_ids, "budget": points, "single_axis": single_axis,
                "selections": [[h["num"] for h in race["horses"]] for race in out],
            })
            result["win5_prediction_id"] = LoggingStore().save_win5_prediction(
                prediction_run_ids=run_ids, race_ids=race_ids, budget=points, total_points=total,
                selections=[{"race_id": race_ids[i], "horse_numbers": [h["num"] for h in r["horses"]]}
                            for i, r in enumerate(out)],
                coverage=[r.get("coverage") for r in out], estimated_hit_rate=result["est_hit_rate"],
                allocation_method=alloc_method, allocation_version=cfg5.get("version", "unknown"),
                single_axis=single_axis, axis_index=axis_idx, skipped=skip_recommended,
                idempotency_key=idempotency_key,
            )
        except Exception as log_exc:
            result["logging_warning"] = f"{type(log_exc).__name__}: {log_exc}"
            print(f"[WARN] WIN5 plan logging failed: {result['logging_warning']}")
        return result


# ─── 締切前の自動再スコア (発走15分前にオッズ込みで再計算) ────────────────────

WATCH = {"status": "idle", "deadline": "", "first_time": "", "urls": [],
         "points": 100, "single_axis": False, "result": None, "races": None,
         "error": "", "updated_at": ""}
_WATCH_LOCK = threading.Lock()
_WATCH_THREAD = [False]


def _watch_loop():
    from datetime import datetime, timedelta
    import time as _time
    while True:
        _time.sleep(20)
        with _WATCH_LOCK:
            armed = WATCH["status"] == "armed"
            first_time = WATCH["first_time"]
            urls = list(WATCH["urls"])
            points, single_axis = WATCH["points"], WATCH["single_axis"]
        if not armed or not first_time:
            continue
        try:
            hh, mm = first_time.split(":")
            start = datetime.now().replace(hour=int(hh), minute=int(mm),
                                           second=0, microsecond=0)
        except ValueError:
            with _WATCH_LOCK:
                WATCH["status"] = "error"
                WATCH["error"] = f"発走時刻を解釈できません: {first_time}"
            continue
        if datetime.now() < start - timedelta(minutes=15):
            continue
        with _WATCH_LOCK:
            WATCH["status"] = "running"
        try:
            races = [analyze_one_core(i, u) for i, u in enumerate(urls)]
            kaime = build_kaime(races, points, single_axis)
            slim = [{"idx": r["idx"], "venue": r["venue"],
                     "race_info": r["race_info"], "upset_rank": r["upset_rank"],
                     "horses": [{k: h.get(k) for k in
                                 ("num", "name", "score", "odds", "pop", "grade")}
                                for h in r["horses"]]} for r in races]
            with _WATCH_LOCK:
                WATCH["status"] = "done"
                WATCH["result"] = kaime
                WATCH["races"] = slim
                WATCH["updated_at"] = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            with _WATCH_LOCK:
                WATCH["status"] = "error"
                WATCH["error"] = str(e)


@app.route("/api/win5_watch", methods=["POST"])
def win5_watch_arm():
    """締切15分前の自動再計算を予約。body: {urls[5], first_time "HH:MM", points, single_axis}"""
    data = request.json or {}
    urls = data.get("urls", [])
    first_time = str(data.get("first_time", "")).strip()
    if len(urls) != 5 or not all(urls):
        return jsonify({"error": "5レース分のURLが必要です"}), 400
    if not first_time:
        return jsonify({"error": "1レース目の発走時刻 (HH:MM) が必要です"}), 400
    with _WATCH_LOCK:
        WATCH.update({
            "status": "armed", "first_time": first_time, "urls": urls,
            "points": int(data.get("points", 100)),
            "single_axis": bool(data.get("single_axis")),
            "result": None, "races": None, "error": "", "updated_at": "",
        })
        if not _WATCH_THREAD[0]:
            _WATCH_THREAD[0] = True
            threading.Thread(target=_watch_loop, daemon=True).start()
    return jsonify({"success": True, "first_time": first_time})


@app.route("/api/win5_watch")
def win5_watch_state():
    with _WATCH_LOCK:
        return jsonify({k: WATCH[k] for k in
                        ("status", "first_time", "result", "races", "error", "updated_at")})


# ─── 起動 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_port_free(PORT, "WIN5予想サーバー")
    url = f"http://localhost:{PORT}"
    print("=" * 55)
    print("  WIN5予想 (スコアリング + 荒れ度配分)")
    print("=" * 55)
    print(f"  URL: {url}")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=PORT, debug=False)
