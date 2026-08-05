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
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Flask, jsonify, request, send_from_directory

from api import scoring
from api.logging_store import LoggingStore
from api.port_guard import ensure_port_free
from api.result_service import normalize_race_date, sync_results_for_date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "jra_logging.db")
ABILITY_DB_PATH = os.path.join(BASE_DIR, "ability.db")
PORT = 5004

# T66: WIN5「500点シャドー」(もし毎回500点prob配分で買っていたら) の予算リスト。
# DBには一切書き込まず、predictions.ml_score + win5_predictions.prediction_run_ids_json
# から表示のたびに再計算する導出値 (jra_win5.build_kaime と同一ロジック)。
WIN5_SHADOW_BUDGETS = [500]

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")
# T38: ルートはBlueprint (bp) に定義し、末尾で app.register_blueprint(bp) して
# standalone実行では従来どおりprefix無しで動く (jra_perf.app.test_client()を使う
# 既存テストも無修正で動作する)。統合エントリ (jra_suite.py) は同じbpを "/perf"
# prefixでマウントする (SPEC-T38 §3.1)。
bp = Blueprint("perf", __name__)


@bp.after_request
def _no_cache_html(resp):
    # UI更新のたびにブラウザキャッシュで旧画面が出る事故の防止 (HTMLのみ。APIは元々動的)
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@bp.route("/")
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


def _selection_numbers(sel):
    nums = sel if isinstance(sel, list) else sel.get("horse_numbers", sel.get("nums", []))
    return [int(x) for x in nums]


def _popularity_by_race(results):
    """race_id -> {馬番(int): 確定人気(int) or None}。
    race_results.final_win_odds をレース内で昇順順位化して人気を導出する (T55)。
    同値はmin rank (通常の競技順位)。**全馬分のオッズが揃うレースのみ順位化する**:
    現実装の結果取得では勝ち馬しか final_win_odds を持たないため、部分順位化すると
    「勝ち馬が常に1人気」という誤表示になる (実データで確認)。揃わない場合は全馬 None。"""
    by_race = defaultdict(list)
    for (race_id, horse_id), row in results.items():
        try:
            horse_no = int(horse_id.split(":")[-1])
        except (TypeError, ValueError):
            continue
        by_race[race_id].append((horse_no, row.get("final_win_odds")))
    out = {}
    for race_id, entries in by_race.items():
        if len(entries) < 2 or any(odds is None for _no, odds in entries):
            out[race_id] = {no: None for no, _odds in entries}
            continue
        valid = sorted((odds, no) for no, odds in entries if odds is not None)
        ranks, prev_odds, prev_rank = {}, None, 0
        for idx, (odds, no) in enumerate(valid, start=1):
            if odds != prev_odds:
                prev_rank, prev_odds = idx, odds
            ranks[no] = prev_rank
        out[race_id] = {no: ranks.get(no) for no, _odds in entries}
    return out


def _ability_popularity_for(race_ids, db_path=None):
    """race_id集合 -> {race_id: {馬番(int): 確定人気(int)}} を ability.db の
    runs.popularity (全馬分の確定人気) から引く (T55)。
    race_results.final_win_odds は勝ち馬分しか無いため、こちらを第一ソースにする。
    ability.db が無い / 該当日が未同期 / 形式不一致は空dictへfail-soft
    (呼び出し側が final_win_odds 順位化へフォールバック)。書き込みはしない (mode=ro)。"""
    path = db_path or ABILITY_DB_PATH
    triples = []
    for rid in race_ids:
        parts = str(rid).split(":")
        if len(parts) != 3:
            continue
        try:
            triples.append((rid, parts[0].strip(), parts[1].strip(), int(parts[2])))
        except (TypeError, ValueError):
            continue
    if not triples or not os.path.exists(path):
        return {}
    out = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for rid, date, place, r in triples:
                pops = {}
                for umaban, pop in conn.execute(
                        "SELECT umaban, popularity FROM runs WHERE date=? AND place=? AND r=?",
                        (date, place, r)):
                    try:
                        if pop is not None:
                            pops[int(umaban)] = int(pop)
                    except (TypeError, ValueError):
                        continue
                if pops:
                    out[rid] = pops
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return out


def _snapshot_popularity_for(cur, race_ids):
    """race_id -> {馬番(int): 人気(int)} を odds_snapshots の最終スナップショット
    (popularity列・全馬分) から引く (T55)。確定人気ではなく最終取得時点
    (通常発走2〜5分前) の人気。ability.db が未同期の直近開催日のフォールバック。"""
    out = {}
    for rid in race_ids:
        try:
            rows = cur.execute(
                "SELECT horse_id, popularity FROM odds_snapshots WHERE race_id=? "
                "AND observed_at=(SELECT MAX(observed_at) FROM odds_snapshots WHERE race_id=?)",
                (rid, rid)).fetchall()
        except sqlite3.OperationalError:
            return out
        pops = {}
        for row in rows:
            try:
                if row["popularity"] is not None:
                    pops[int(str(row["horse_id"]).split(":")[-1])] = int(row["popularity"])
            except (TypeError, ValueError):
                continue
        if pops:
            out[rid] = pops
    return out


def _merge_popularity(odds_pop, ability_pop):
    """確定人気のマージ (T55): ability.db由来 (全馬分) を優先し、
    足りない馬番だけ final_win_odds 順位化 (odds_pop) で補完する。"""
    merged = {rid: dict(m) for rid, m in odds_pop.items()}
    for rid, pops in ability_pop.items():
        base = merged.setdefault(rid, {})
        for no, pop in pops.items():
            base[no] = pop
    return merged


def _win5_race_details(race_ids, selections, results, race_meta, pop_by_race, hit_flags):
    """WIN5詳細表示 (T55): レースごとの買い目×確定人気、的中選択馬の判定、
    不的中レースの勝ち馬 (馬番・馬名・人気) を返す。"""
    rows = []
    for rid, sel, hit in zip(race_ids, selections, hit_flags):
        nums = _selection_numbers(sel)
        pop_map = pop_by_race.get(rid, {})
        winner_row = next((res for (r, h), res in results.items()
                           if r == rid and res["finish_position"] == 1), None)
        winner_no = int(winner_row["horse_id"].split(":")[-1]) if winner_row else None
        meta = race_meta.get(rid, {})
        picks = [{"no": n, "pop": pop_map.get(n), "hit": winner_no is not None and n == winner_no}
                 for n in nums]
        winner = None
        if winner_row is not None:
            winner = {"no": winner_no, "name": winner_row.get("horse_name"), "pop": pop_map.get(winner_no)}
        rows.append({
            "race_id": rid, "venue": meta.get("venue") or rid.split(":")[1],
            "race_no": meta.get("race_no") or int(rid.split(":")[-1]),
            "race_name": meta.get("race_name"),
            "picks": picks, "settled": hit is not None, "hit": hit, "winner": winner,
        })
    return rows


def _load_win5_results(cur, compact_date):
    """win5_results テーブル (T55; 結果取り込み経路のみが書き込む) を日付→行のdictで返す。
    migration未適用の古いDBではテーブルが無いので、その場合は空dictにfail-soft。"""
    try:
        sql = "SELECT * FROM win5_results"
        params = ()
        if compact_date:
            sql += " WHERE race_date=?"
            params = (compact_date,)
        return {row["race_date"]: dict(row) for row in cur.execute(sql, params)}
    except sqlite3.OperationalError:
        return {}


def _win5_payout_view(win5_result_row, race_rows):
    """win5_results の1行 + レース別勝ち馬情報 → ダッシュボード表示用の配当dict (T55)。
    winning_numbers_json (照合用の冗長記録) と race_results 由来の勝ち馬が食い違う場合は
    mismatch=True (表示側で警告)。"""
    if not win5_result_row:
        return {"fetched": False}
    mismatch = False
    try:
        stored_numbers = json.loads(win5_result_row.get("winning_numbers_json") or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_numbers = None
    if stored_numbers:
        for idx, row in enumerate(race_rows):
            winner = row.get("winner")
            if winner is None or idx >= len(stored_numbers):
                continue
            try:
                if int(stored_numbers[idx]) != int(winner["no"]):
                    mismatch = True
            except (TypeError, ValueError):
                continue
    return {
        "fetched": True,
        "payout_yen": win5_result_row.get("payout_yen"),
        "hit_ticket_count": win5_result_row.get("hit_ticket_count"),
        "carryover_flag": bool(win5_result_row.get("carryover_flag")),
        "carryover_amount": win5_result_row.get("carryover_amount"),
        "mismatch": mismatch,
    }


def _win5_shadow_max_picks():
    """win5_weights.json の allocation.max_picks_per_race を読む (T66)。
    jra_win5.build_kaime と同一ソース。読めなければ既定値8 (build_kaime既定と同じ)。"""
    try:
        cfg5 = scoring.load_score_weights(BASE_DIR, "win5_weights.json")
        return int((cfg5.get("allocation") or {}).get("max_picks_per_race", 8))
    except Exception:
        return 8


def _win5_shadow_allocation(cur, prediction_run_ids, budget, max_picks):
    """T66: 5 run分の predictions.ml_score から500点シャドー配分を導出する
    (scoring.win_probs_from_ml_scores + scoring.allocate_picks_prob。
    jra_win5.build_kaime のprob配分パスと同一ロジック・軸固定なし)。
    いずれかのrunで行が0件 / ml_scoreが1頭でもNULL / prob化不能(非conditional_logitモデル等)
    なら None (算出不可。代替値のでっち上げをしない)。
    戻り値: (selections[レース][馬番int...], picks[int...], est_hit_rate) または None。"""
    sorted_ids_per_race = []
    prob_lists = []
    for run_id in prediction_run_ids:
        rows = cur.execute(
            "SELECT horse_id, ml_score FROM predictions WHERE prediction_run_id=? "
            "ORDER BY horse_id ASC", (run_id,)).fetchall()
        if not rows or any(r["ml_score"] is None for r in rows):
            return None
        # ml_score降順 (同値はhorse_id昇順を維持: 決定的な再現性のため安定ソート)
        rows_sorted = sorted(rows, key=lambda r: -r["ml_score"])
        sorted_ids_per_race.append([r["horse_id"] for r in rows_sorted])
        probs = scoring.win_probs_from_ml_scores([r["ml_score"] for r in rows_sorted])
        if not probs:
            return None
        prob_lists.append(probs)
    picks, est = scoring.allocate_picks_prob(prob_lists, budget, max_picks)
    selections = []
    for horse_ids, k in zip(sorted_ids_per_race, picks):
        kk = min(k, len(horse_ids))
        selections.append([int(h.split(":")[-1]) for h in horse_ids[:kk]])
    return selections, picks, est


def _win5_shadow_view(cur, race_ids, prediction_run_ids, budget, max_picks,
                      results, race_meta, pop_by_race, win5_results_by_date):
    """T66: win5_predictions 1行分の「500点シャドー」表示用dictを返す (算出不可なら None)。
    的中判定は既存 _win5_hit_flags を再利用。払戻は win5_results.payout_yen をそのまま
    プロキシとして使う (自票によるパリミュチュエル希薄化は無視。UI側でプロキシ表記する)。"""
    if len(race_ids) != 5 or len(prediction_run_ids) != 5:
        return None
    alloc = _win5_shadow_allocation(cur, prediction_run_ids, budget, max_picks)
    if alloc is None:
        return None
    selections, picks, est = alloc
    clipped_k = [len(sel) for sel in selections]
    total_points = 1
    for k in clipped_k:
        total_points *= max(k, 1)
    formula = "×".join(str(max(k, 1)) for k in clipped_k) + f"={total_points}点"
    hit_flags, settled_all = _win5_hit_flags(race_ids, selections, results)
    race_rows = _win5_race_details(race_ids, selections, results, race_meta, pop_by_race, hit_flags)
    plan_date = str(race_ids[0]).split(":")[0] if race_ids else None
    return {
        "budget": budget,
        "points": total_points,
        "formula": formula,
        "est": round(est, 5) if est is not None else None,
        "hits": hit_flags,
        "all_settled": settled_all,
        "win5_hit": settled_all and all(hit_flags),
        "races": race_rows,
        "payout": _win5_payout_view(win5_results_by_date.get(plan_date), race_rows),
        "cost_yen": total_points * 100,
    }


def _win5_shadow_summary(win5_by_date, win5_results_by_date, budget):
    """T66: 「500点シャドー累計」ブロック。公式結果 (win5_results) がある日のみ集計する。
    シャドーが算出不可 (None) の日は投入・払戻に含めず unavailable_days でカウントする
    (代替値のでっち上げをしない)。"""
    target_days = hit_days = unavailable_days = 0
    cost_total = payout_total = 0
    dates = []
    for wdate, entry in win5_by_date.items():
        if not win5_results_by_date.get(wdate):
            continue  # 公式結果がある日のみ集計
        shadow = entry.get("shadow_500")
        if shadow is None:
            unavailable_days += 1
            continue
        target_days += 1
        dates.append(wdate)
        cost_total += shadow["cost_yen"]
        if shadow["win5_hit"]:
            hit_days += 1
            payout_view = shadow["payout"]
            if payout_view.get("fetched"):
                payout_total += payout_view.get("payout_yen") or 0
    return {
        "budget": budget,
        "target_days": target_days,
        "hit_days": hit_days,
        "unavailable_days": unavailable_days,
        "cost_yen": cost_total,
        "payout_yen": payout_total,
        "roi_pct": round(100.0 * payout_total / cost_total, 1) if cost_total else None,
        "since": _iso_date(min(dates)) if dates else None,
        "until": _iso_date(max(dates)) if dates else None,
    }


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
                           final_win_odds, win_payout, place_payout FROM race_results"""
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
    pop_by_race = _popularity_by_race(results)
    win5_results_by_date = _load_win5_results(cur, compact_date)
    shadow_budget = WIN5_SHADOW_BUDGETS[0]
    shadow_max_picks = _win5_shadow_max_picks()
    win5 = []
    if compact_date:
        win5_sql = """SELECT * FROM win5_predictions WHERE race_ids_json LIKE ?
                      ORDER BY created_at DESC"""
        win5_params = (f'%"{compact_date}:%',)
    else:
        win5_sql = "SELECT * FROM win5_predictions ORDER BY created_at DESC LIMIT 30"
        win5_params = ()
    win5_rows = list(cur.execute(win5_sql, win5_params))
    win5_daily_rows = list(cur.execute(
        "SELECT * FROM win5_predictions ORDER BY created_at DESC"))
    # T55: WIN5対象レースの人気は3段ソース。優先度は
    # ability.db確定人気 > 最終スナップショット人気 (2〜5分前) > final_win_odds順位化
    win5_race_ids = set()
    for w in win5_rows + win5_daily_rows:
        try:
            win5_race_ids.update(str(r) for r in json.loads(w["race_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    pop_by_race = _merge_popularity(pop_by_race, _snapshot_popularity_for(cur, win5_race_ids))
    pop_by_race = _merge_popularity(pop_by_race, _ability_popularity_for(win5_race_ids))
    for w in win5_rows:
        race_ids = json.loads(w["race_ids_json"])
        if compact_date and not any(str(rid).startswith(f"{compact_date}:") for rid in race_ids):
            continue
        if len(win5) >= 30:
            break
        selections = json.loads(w["selections_json"])
        hit_flags, settled_all = _win5_hit_flags(race_ids, selections, results)
        plan_date = str(race_ids[0]).split(":")[0] if race_ids else None
        race_rows = _win5_race_details(race_ids, selections, results, race_meta, pop_by_race, hit_flags)
        try:
            prediction_run_ids = json.loads(w["prediction_run_ids_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            prediction_run_ids = []
        win5.append({
            "created_at": _to_jst(w["created_at"]), "budget": w["budget"],
            "points": w["total_points"], "est": w["estimated_hit_rate"],
            "method": w["allocation_method"], "single_axis": bool(w["single_axis"]),
            "hits": hit_flags, "all_settled": settled_all,
            "win5_hit": settled_all and all(hit_flags),
            "races": race_rows,
            "payout": _win5_payout_view(win5_results_by_date.get(plan_date), race_rows),
            "shadow_500": _win5_shadow_view(
                cur, race_ids, prediction_run_ids, shadow_budget, shadow_max_picks,
                results, race_meta, pop_by_race, win5_results_by_date),
        })

    # 日別WIN5実績 (T30): 日付ごとに最新プラン (created_at最大) の的中状況を採る
    win5_by_date = {}
    for w in win5_daily_rows:
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
        race_rows = _win5_race_details(race_ids, selections, results, race_meta, pop_by_race, hit_flags)
        try:
            prediction_run_ids = json.loads(w["prediction_run_ids_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            prediction_run_ids = []
        win5_by_date[wdate] = {
            "hits": sum(1 for h in hit_flags if h), "total": len(hit_flags),
            "all_settled": settled_all, "win5_hit": settled_all and all(hit_flags),
            "races": race_rows,
            "payout": _win5_payout_view(win5_results_by_date.get(wdate), race_rows),
            "shadow_500": _win5_shadow_view(
                cur, race_ids, prediction_run_ids, shadow_budget, shadow_max_picks,
                results, race_meta, pop_by_race, win5_results_by_date),
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

    payload = {"summary": out_summary, "daily": out_daily[:60],
               "ev": {"sum": ev_sum, "rows": ev_rows},
               "win5": win5, "race_details": race_details[:300],
               "pending_races": pending,
               "selected_date": _iso_date(compact_date),
               "available_dates": [_iso_date(value) for value in available_dates],
               "days": days[:90],
               "win5_shadow_summary": _win5_shadow_summary(
                   win5_by_date, win5_results_by_date, shadow_budget)}

    # 日付指定時はT56以前の応答を完全に維持する。以下の全期間向けデータは、
    # 既存の集計結果を表示用に組み替えるだけで、新しいSQLや母集団は持たない。
    if compact_date:
        conn.close()
        return payload

    model_series = {}
    for entry in out_daily:
        group = (entry["app"], entry["model"], entry["version"], entry["config"])
        series_entry = model_series.setdefault(group, {
            "app": entry["app"], "model": entry["model"],
            "version": entry["version"], "config": entry["config"], "points": [],
        })
        series_entry["points"].append({
            "date": _iso_date(entry["date"]), "settled": entry["settled"],
            "win_rate": entry["win_rate"], "tan_roi": entry["tan_roi"],
            "fuku_roi": entry["fuku_roi"],
        })
    for entry in model_series.values():
        entry["points"].sort(key=lambda point: point["date"])

    ev_points = []
    for date, bucket in sorted(ev_by_date.items()):
        settled = bucket["settled"]
        ev_points.append({
            "date": _iso_date(date), "settled": settled, "win": bucket["win"],
            "tan_roi": round(bucket["tan_ret"] / settled, 1) if settled else None,
            "fuku_roi": round(bucket["fuku_ret"] / settled, 1) if settled else None,
        })
    win5_points = [{
        "date": _iso_date(date), "hits": bucket["hits"], "total": bucket["total"],
        "win5_hit": bucket["win5_hit"],
    } for date, bucket in sorted(win5_by_date.items())]

    race_date_buckets = defaultdict(lambda: {
        "races": 0, "settled": 0, "win": 0, "top3": 0,
        "tan_ret": 0, "fuku_ret": 0, "models": 0,
    })
    for (_group, date), bucket in daily.items():
        target = race_date_buckets[date]
        target["models"] += 1
        for key in ("races", "settled", "win", "top3", "tan_ret", "fuku_ret"):
            target[key] += bucket[key]
    race_dates = []
    for date, bucket in sorted(race_date_buckets.items(), reverse=True):
        settled = bucket["settled"]
        race_dates.append({
            "date": _iso_date(date), "races": bucket["races"], "settled": settled,
            "win": bucket["win"], "top3": bucket["top3"],
            "tan_roi": round(bucket["tan_ret"] / settled, 1) if settled else None,
            "fuku_roi": round(bucket["fuku_ret"] / settled, 1) if settled else None,
            "models": bucket["models"],
        })

    latest_date = race_details[0]["date"] if race_details else None
    payload["race_details"] = [row for row in race_details if row["date"] == latest_date]
    payload["race_dates"] = race_dates
    payload["series"] = {
        "models": [model_series[group] for group in sorted(model_series)],
        "ev": {"points": ev_points},
        "win5": {"points": win5_points},
    }
    conn.close()
    return payload


@bp.route("/api/perf")
def api_perf():
    try:
        return jsonify(collect(request.args.get("date")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@bp.route("/api/results/sync", methods=["POST"])
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


# standalone実行時 (このモジュールを直接importしてapp.run()する場合や、テストで
# app.test_client()を使う場合) に従来どおりprefix無しでルートが解決できるよう、
# bpをこのモジュール自身のappにも登録しておく (SPEC-T38 §3.1)。
app.register_blueprint(bp)


if __name__ == "__main__":
    # T38: 統合版 jra_suite.py (port 5005) に統合されたため、直接起動は案内のみ表示して
    # 終了する (二重起動によるポート競合を防ぐため)。関数群はjra_suite.pyから
    # importして使われる。
    print(
        "[案内] jra_perf.py は統合版 jra_suite.py (port 5005) に統合されました。\n"
        "  python jra_suite.py で起動してください (または start_suite.bat)。",
        file=sys.stderr,
    )
    sys.exit(1)
