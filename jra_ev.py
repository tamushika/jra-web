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
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

import scoring  # noqa: E402
import board_market  # noqa: E402
from index import analyze_race_url, build_matrix_data  # noqa: E402
from combo_probs import wide_candidates  # noqa: E402
from api.port_guard import ensure_port_free  # noqa: E402
from result_service import ResultNotReady, fetch_and_save_result  # noqa: E402
try:
    from logging_store import LoggingStore, config_hash  # noqa: E402
except Exception as exc:
    LoggingStore = None
    print(f"[WARN] common logging unavailable: {type(exc).__name__}: {exc}")

PORT = 5003
RESULT_SYNC_VERSION = 2
JST = timezone(timedelta(hours=9))
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.jra.go.jp/"}

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")
CORS(app)
# T38: ルートはBlueprint (bp) に定義し、末尾で app.register_blueprint(bp) して
# standalone実行 (このapp) では従来どおりprefix無しで動く。統合エントリ
# (jra_suite.py) は同じbpを "/ev" prefixでマウントする。処理関数・STATE等の
# モジュールレベル状態はこのモジュールに残したまま (SPEC-T38 §3.1)。
bp = Blueprint("ev", __name__)

_LOCK = threading.RLock()
_ALERT_SEQ = itertools.count(1)
STATE = {
    "status": "idle",            # idle | analyzing | ready | error
    "error": "",
    "progress": {"done": 0, "total": 0},
    "started_at": "",
    "warning": "",
    "races": {},                 # rid -> race record
    "alerts": [],                # {id, ts, stage, rid, label, picks}
    "params": {"ev_threshold": 1.3, "max_odds": 50.0, "min_prob": 0.02,
               # ワイドは2026-07-13の再検証で運用停止 (2026H1フル期間で回収66.6%。
               # 採用根拠だった110%は4-6月サブセット+2025年統計リークの見かけと判明)。
               # 再開する場合は数値を戻す前に docs/TASKS.md の再検証記録を確認すること。
               "wide_overlay": None},
}
_SCHEDULER_STARTED = [False]


@bp.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index_ev.html")


@bp.route("/boards")
def serve_boards():
    return send_from_directory(BASE_DIR, "index_boards.html")


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


def _parse_start_time(race_info, base_date=None):
    """'【東京 11R】芝1600m 15:40発走　…' → datetime (base_date、省略時は本日)"""
    m = re.search(r"(\d{1,2}):(\d{2})発走", str(race_info))
    if not m:
        return None
    base = base_date or datetime.now()
    return base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                        second=0, microsecond=0)


def _parse_race_num(race_info):
    m = re.search(r"【[^ ]+ (\d+)R】", str(race_info))
    return int(m.group(1)) if m else None


def _to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _valid_win_odds(value):
    """JRAカード上で発売済みと判断できる単勝オッズならfloatを返す。"""
    odds = _to_float(value)
    if odds is None or not math.isfinite(odds) or not 1.0 < odds < 999.0:
        return None
    return odds


def _aware_local(value):
    """datetimeをJSTのaware値へ正規化する。naive値はJSTとして扱う。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(JST)


def _same_instant(left, right):
    left_dt = _aware_local(left)
    right_dt = _aware_local(right)
    return bool(left_dt and right_dt and left_dt == right_dt)


def _snapshot_runner(horse):
    """取消・除外馬をPhase Cのfield_size/有効オッズ判定から除く。"""
    if any(bool(horse.get(key)) for key in
           ("scratched", "cancelled", "withdrawn", "is_scratched")):
        return False
    status = " ".join(str(horse.get(key) or "") for key in
                      ("status", "race_status", "entry_status"))
    return not re.search(r"取消|除外|scratched|cancelled|withdrawn", status, re.I)


def _snapshot_quality(stage, scheduled_post_at, observed_at, fetch_duration_ms, horses,
                      previous_snapshots=(), scheduler_restart=False):
    """1回のPhase C取得について、保存用メタデータと品質フラグを返す。"""
    observed = _aware_local(observed_at)
    scheduled = _aware_local(scheduled_post_at)
    stage_value = int(stage)
    runners = [horse for horse in horses if _snapshot_runner(horse)]
    valid_odds_count = sum(
        1 for horse in runners if _valid_win_odds(horse.get("odds")) is not None)
    field_size = len(runners)
    threshold = max(4, field_size // 2)
    seconds_to_post = (
        (scheduled - observed).total_seconds() if scheduled and observed else None)

    flags = []
    if (seconds_to_post is None
            or abs(seconds_to_post - stage_value * 60) > 60):
        flags.append("late_capture")

    catchup_burst = False
    for previous in previous_snapshots or ():
        previous_stage = previous.get("stage")
        previous_observed = _aware_local(previous.get("observed_at"))
        if (previous_stage is not None and int(previous_stage) != stage_value
                and observed and previous_observed):
            elapsed = (observed - previous_observed).total_seconds()
            if 0 <= elapsed <= 120:
                catchup_burst = True
    previous_scheduled = (
        previous_snapshots[-1].get("scheduled_post_at")
        if previous_snapshots else None)
    post_time_changed = (
        previous_scheduled is not None and scheduled is not None
        and not _same_instant(previous_scheduled, scheduled))
    if catchup_burst:
        flags.append("catchup_burst")
    if valid_odds_count < threshold:
        flags.append("insufficient_odds")
    if post_time_changed:
        flags.append("post_time_changed")
    if scheduler_restart:
        flags.append("scheduler_restart")

    return {
        "scheduled_post_at": scheduled.isoformat() if scheduled else None,
        "seconds_to_post": seconds_to_post,
        "fetch_duration_ms": max(0, int(fetch_duration_ms)),
        "valid_odds_count": valid_odds_count,
        "field_size": field_size,
        "data_quality_flags": flags,
        "observed_at": observed.isoformat() if observed else None,
    }


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


def compute_wide_picks(horses, params):
    """ワイド候補 (overlay >= wide_overlay の全組み合わせ)。
    2026-07-13 運用停止 (params["wide_overlay"]=None): 2026H1フル再検証で回収66.6%。
    当初の採用根拠 (2025年110.1%/2026年110.0%) は統計リークとApr-Junサブセットの見かけだった"""
    if params.get("wide_overlay") is None:
        return []
    try:
        cands = wide_candidates(
            [h.get("win_prob") for h in horses],
            [_to_float(h.get("odds")) for h in horses],
            overlay_min=params["wide_overlay"])
    except Exception as e:
        print(f"[WARN] wide計算失敗: {e}")
        return []
    return [{"nums": sorted([horses[i]["num"], horses[j]["num"]]),
             "names": [horses[i]["name"], horses[j]["name"]],
             "overlay": ov, "prob": mp}
            for i, j, ov, mp in cands]


def analyze_one(url, params, base_date=None, day_label="", stage=None,
                snapshot_context=None):
    """1レースを解析して監視レコードを返す。stage は発走何分前の取得かの識別子
    (30/15/10/5/2。通常スキャン時はNone) で、odds_snapshotsに記録される
    (時点別オッズ特徴量 Phase C 用)"""
    fetch_started = time.perf_counter()
    result = analyze_race_url(url, "簡易")
    venue = result.get("venue")
    if result.get("analysis_excluded") or result.get("race_type") == "障害":
        race_no = result.get("race_num") or _parse_race_num(result.get("race_info")) or "?"
        print(f"[INFO] 障害レースのため監視対象外: {venue or '不明'}{race_no}R")
        return None
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
        if ml is None and scoring.is_debut_horse(h):
            print(f"[INFO] 初出走のためML/EV対象外: {h.get('name') or '馬名不明'}")
        horses.append({
            "num": h.get("num"), "name": h.get("name"), "jock": h.get("jock"),
            "odds": h.get("odds"), "pop": h.get("pop"), "grade": h.get("grade", ""),
            "current_weight": h.get("current_weight"), "weight_change": h.get("weight_change"),
            "weight_source": h.get("weight_source"),
            "weight_fallback": h.get("weight_source") not in ("jra_live", "jra_live_cache"),
            "pace_fit": h.get("_pace_fit"), "pace_fit_source": h.get("pace_fit_source"),
            "status": h.get("status") or h.get("race_status") or h.get("entry_status"),
            "scratched": bool(h.get("scratched") or h.get("cancelled")
                               or h.get("withdrawn") or h.get("is_scratched")),
            "web_score": h.get("score"), "ml_score": ml,
        })
    n_picked = compute_picks(horses, params)

    # 既存の監視/通知経路が参照するodds_okは、T40以前の判定を維持する。
    # Phase C用の厳格な有効頭数・不足判定は_snapshot_quality内だけで算出する。
    n_odds = sum(1 for h in horses
                 if (_to_float(h.get("odds")) or 0) > 1.0)
    odds_ok = bool(horses) and n_odds >= max(1, len(horses) // 2)
    start_dt = _parse_start_time(result.get("race_info"), base_date)
    observed_at = datetime.now().astimezone()
    fetch_duration_ms = round((time.perf_counter() - fetch_started) * 1000)
    snapshot_quality = None
    snapshot_persisted = None if snapshot_context is None else False
    if snapshot_context is not None:
        snapshot_quality = _snapshot_quality(
            stage=stage,
            scheduled_post_at=start_dt or snapshot_context.get("scheduled_post_at"),
            observed_at=observed_at,
            fetch_duration_ms=fetch_duration_ms,
            horses=horses,
            previous_snapshots=snapshot_context.get("previous_snapshots", ()),
            scheduler_restart=bool(snapshot_context.get("scheduler_restart")),
        )

    log_context = {}
    # Logging is deliberately best-effort: observability must not stop monitoring.
    if LoggingStore is not None:
        try:
            race_day = (base_date or observed_at).strftime("%Y%m%d")
            race_no = _parse_race_num(result.get("race_info")) or 0
            race_id = f"{race_day}:{venue or 'unknown'}:{race_no:02d}"
            store = LoggingStore()
            store.save_race(
                race_id=race_id, race_date=race_day, venue=venue or "unknown", race_no=race_no,
                surface=race_type, distance_m=dist_val, race_class=result.get("race_class"),
                start_time=start_dt,
            )
            run_id = store.start_run(
                app_name="ev_monitor", trigger_type="scheduler",
                model_name="win5_conditional_logit",
                model_version=cfg5.get("version", "unknown"),
                config={"score": cfg5, "ev": params},
                data_version=_data_version(),
            )
            prediction_rows = []
            odds_rows = []
            for h in horses:
                horse_no = int(float(h["num"])) if _to_float(h.get("num")) is not None else 0
                horse_id = f"{race_id}:{horse_no:02d}"
                h["_horse_id"] = horse_id
                prediction_rows.append({
                    "race_id": race_id, "horse_id": horse_id, "predicted_at": observed_at,
                    "web_score": h.get("web_score"), "ml_score": h.get("ml_score"),
                    "calibrated_win_probability": h.get("win_prob"),
                    "score_details": {"picked": h.get("picked"), "ev": h.get("ev")},
                    "feature_snapshot": {
                        "horse_no": h.get("num"), "horse_name": h.get("name"),
                        "current_weight": h.get("current_weight"),
                        "weight_source": h.get("weight_source"),
                        "pace_fit": h.get("pace_fit"),
                    },
                    "data_quality_flags": [] if h.get("win_prob") is not None else ["win_probability_unavailable"],
                })
                if snapshot_quality is not None:
                    odds = _valid_win_odds(h.get("odds"))
                else:
                    raw_odds = _to_float(h.get("odds"))
                    odds = raw_odds if raw_odds and raw_odds > 1.0 else None
                odds_flags = list(
                    (snapshot_quality or {}).get("data_quality_flags", []))
                if odds is None:
                    odds_flags.append("win_odds_unavailable")
                odds_rows.append({
                    "race_id": race_id, "horse_id": horse_id, "observed_at": observed_at,
                    "win_odds": odds,
                    "popularity": int(float(h["pop"])) if _to_float(h.get("pop")) else None,
                    "source": "jra", "fetch_id": run_id, "stage": stage,
                    "idempotency_key": f"{run_id}:{race_id}:{horse_id}",
                    "scheduled_post_at": (snapshot_quality or {}).get("scheduled_post_at"),
                    "seconds_to_post": (snapshot_quality or {}).get("seconds_to_post"),
                    "fetch_duration_ms": (snapshot_quality or {}).get("fetch_duration_ms"),
                    "valid_odds_count": (snapshot_quality or {}).get("valid_odds_count"),
                    "field_size": (snapshot_quality or {}).get("field_size"),
                    "data_quality_flags": odds_flags,
                })
            store.save_predictions(run_id, prediction_rows)
            saved_odds = store.save_odds(odds_rows)
            if snapshot_quality is not None:
                snapshot_persisted = (
                    bool(odds_rows) and saved_odds == len(odds_rows))
            store.finish_run(run_id)
            log_context = {"prediction_run_id": run_id, "race_id": race_id}
        except Exception as exc:
            print(f"[WARN] common logging failed: {type(exc).__name__}: {exc}")

    return {
        "url": url, "venue": venue,
        "race_num": _parse_race_num(result.get("race_info")),
        "race_info": result.get("race_info", ""),
        "race_type": race_type, "dist": dist_val,
        "start_time": start_dt.strftime("%H:%M") if start_dt else "",
        "_start_dt": start_dt,
        "day_label": day_label,
        "odds_ok": odds_ok, "n_odds": n_odds,
        "horses": horses, "n_picked": n_picked,
        "wide_picks": compute_wide_picks(horses, params),
        "checked15": False, "checked5": False, "finished": False,
        # 通知を伴わないスナップショット専用ステージ (時点別オッズ特徴量 Phase C 用蓄積)。
        # checked15/checked5 の通知パスとは完全に独立しており、通知条件には影響しない。
        "checked30": False, "checked10": False, "checked2": False,
        "last_update": datetime.now().strftime("%H:%M:%S"),
        "_log_context": log_context,
        "_snapshot_quality": snapshot_quality,
        "_snapshot_persisted": snapshot_persisted,
        "_board_odds_entry_cname": result.get("_board_odds_entry_cname"),
        "race_date": result.get("race_date") or (
            (base_date or observed_at).strftime("%Y%m%d")),
    }


def _data_version():
    """Return a non-sensitive, stable identifier for the local input data."""
    candidates = [os.path.join(API_DIR, "past_data_v2.db"), os.path.join(BASE_DIR, "ability.db")]
    parts = []
    for path in candidates:
        try:
            stat = os.stat(path)
            parts.append(f"{os.path.basename(path)}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    return "|".join(parts) or None


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
        # 本日分のみ (matrix には土日両日が混在し得る → 日付ラベルが今日のもの優先)。
        # 本日開催が無い日 (金曜等) は直近の開催日を対象にし、その日付で発走時刻を解釈する
        now = datetime.now()
        today_md = f"{now.month}/{now.day}("

        def _venue_date(text):
            m = re.match(r"(\d{1,2})/(\d{1,2})\(", str(text))
            if not m:
                return None, ""
            try:
                d = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                                hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                return None, ""
            if d.date() < now.date():  # 年跨ぎ (12月に1月の開催を見た場合)
                d = d.replace(year=d.year + 1)
            return d, m.group(0).rstrip("(") + str(text)[m.end() - 1:m.end() + 2]

        today_venues = [v for v in matrix if today_md in v.get("text", "")]
        warning = ""
        if today_venues:
            venues = today_venues
        else:
            # 本日開催なし → 直近の開催日のみ対象 (土日両方を混ぜると
            # 同一場・同一R番号でレースIDが衝突するため1日に絞る)
            dated = [(v, _venue_date(v.get("text", ""))[0]) for v in matrix]
            dated = [(v, d) for v, d in dated if d is not None]
            if dated:
                min_d = min(d for _, d in dated).date()
                venues = [v for v, d in dated if d.date() == min_d]
                warning = (f"本日の開催はありません。{min_d.month}/{min_d.day} の出馬表を"
                           "解析します (オッズは発売前の可能性があります)")
            else:
                venues = matrix
                warning = "本日の開催はありません。直近の出馬表を解析します"
        targets = []
        for v in venues:
            vdate, _ = _venue_date(v.get("text", ""))
            dlabel = str(v.get("text", "")).split(" ")[0] if v.get("text") else ""
            for r in v["races"]:
                targets.append((v["text"], r["url"], vdate, dlabel))
        with _LOCK:
            STATE["progress"] = {"done": 0, "total": len(targets)}
            STATE["warning"] = warning

        def run(t):
            label, url, vdate, dlabel = t
            try:
                rec = analyze_one(url, params, base_date=vdate, day_label=dlabel)
                if rec is None:
                    return
                with _LOCK:
                    STATE["races"][_rid(rec)] = rec
                _persist_monitor(rec)
            except Exception as e:
                print(f"[WARN] 解析失敗 {label}: {e}")
            finally:
                with _LOCK:
                    STATE["progress"]["done"] += 1

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(run, targets))
        with _LOCK:
            # 全レースでオッズが取れていない場合は明示的に警告 (発売前が典型)
            recs = list(STATE["races"].values())
            n_no_odds = sum(1 for r in recs if not r.get("odds_ok"))
            if recs and n_no_odds == len(recs):
                STATE["warning"] = ((STATE["warning"] + " / ") if STATE["warning"] else "") + \
                    "⚠️ 全レースでオッズが取得できていません (発売前の可能性)。" \
                    "EV判定にはオッズが必須のため該当馬は出ません。発売開始後に再度「解析開始」を押してください"
            elif n_no_odds:
                STATE["warning"] = ((STATE["warning"] + " / ") if STATE["warning"] else "") + \
                    f"⚠️ {n_no_odds}/{len(recs)}レースでオッズ未取得"
            STATE["status"] = "ready"
        _ensure_scheduler()
    except Exception as e:
        with _LOCK:
            STATE["status"] = "error"
            STATE["error"] = str(e)


# ─── 通知の外部連携 (SQLite実測ログ / Discord) ────────────────────────────────


def _send_discord(alert):
    """.env の EV_DISCORD_WEBHOOK が設定されていればスマホへも通知"""
    url = os.environ.get("EV_DISCORD_WEBHOOK", "").strip()
    if not url:
        return "suppressed", None, None
    try:
        lines = [f"⏰ **{alert['stage']}分前 期待値あり: {alert['label']}**"]
        for p in alert["picks"]:
            lines.append(f"{p['num']}番 {p['name']} EV{p['ev']} "
                         f"(勝率{round((p['win_prob'] or 0) * 100)}%×{p['odds']}倍)"
                         + (" [Web妙味]" if p.get("web_value") else ""))
        for w in alert.get("wide", []):
            lines.append(f"ワイド {w['nums'][0]}-{w['nums'][1]} ({w['names'][0]}×{w['names'][1]}) "
                         f"overlay{w['overlay']}")
        lines.append("-# 単勝+複勝の併用が堅い / ワイドはoverlay1.6以上全買いで検証上110%")
        res = requests.post(url, json={"content": "\n".join(lines)}, timeout=10)
        if 200 <= res.status_code < 300:
            return "sent", res.status_code, None
        return "failed", res.status_code, f"HTTP {res.status_code}"
    except Exception as e:
        print(f"[WARN] Discord通知失敗: {e}")
        return "failed", None, f"{type(e).__name__}: {e}"


def _send_line(alert):
    """.env の EV_LINE_CHANNEL_TOKEN 設定時、「5分前 × EV>=1.3」の通知をLINEへ送る。
    LINE Notify は2025年に終了したため Messaging API のブロードキャストを使用
    (自分だけを友だちにしたボット宛て。無料枠200通/月 → 5分前のみなら十分収まる)。
    ステージ/閾値は EV_LINE_STAGE / EV_LINE_MIN_EV で変更可 (既定 5 / 1.3)。"""
    token = os.environ.get("EV_LINE_CHANNEL_TOKEN", "").strip()
    if not token:
        return "suppressed", None, None
    try:
        stage_req = int(os.environ.get("EV_LINE_STAGE", "5"))
        min_ev = float(os.environ.get("EV_LINE_MIN_EV", "1.3"))
    except ValueError:
        stage_req, min_ev = 5, 1.3
    if alert["stage"] != stage_req:
        return "suppressed", None, None
    picks = [p for p in alert["picks"] if (p.get("ev") or 0) >= min_ev]
    if not picks:
        return "suppressed", None, None
    lines = [f"⏰ {alert['stage']}分前 期待値あり: {alert['label']}"]
    for p in picks:
        lines.append(f"{p['num']}番 {p['name']} EV{p['ev']} "
                     f"(勝率{round((p['win_prob'] or 0) * 100)}%×{p['odds']}倍)"
                     + (" [Web妙味]" if p.get("web_value") else ""))
    for w in alert.get("wide", []):
        lines.append(f"ワイド {w['nums'][0]}-{w['nums'][1]} overlay{w['overlay']}")
    lines.append("※単勝+複勝の併用が堅い / ワイドは1.6以上全買いで110%")
    try:
        res = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": "\n".join(lines)}]},
            timeout=10)
        if res.status_code != 200:
            print(f"[WARN] LINE通知失敗: HTTP {res.status_code} {res.text[:120]}")
            return "failed", res.status_code, f"HTTP {res.status_code}"
        return "sent", res.status_code, None
    except Exception as e:
        print(f"[WARN] LINE通知失敗: {e}")
        return "failed", None, f"{type(e).__name__}: {e}"


# ─── スケジューラ (15分前/5分前チェック) ─────────────────────────────────────

def refresh_and_alert(rec, stage):
    """オッズを再取得して再判定。期待値馬がいれば通知を作成"""
    params = STATE["params"]
    try:
        new_rec = analyze_one(rec["url"], params, stage=stage)
    except Exception as e:
        print(f"[WARN] 再取得失敗 {_rid(rec)}: {e}")
        return False
    alert = None
    notification_ids = {}
    with _LOCK:
        for k in ("horses", "n_picked", "wide_picks", "last_update"):
            rec[k] = new_rec.get(k, rec.get(k))
        picks = [h for h in rec["horses"] if h["picked"]]
        if picks:
            alert = {
                "id": next(_ALERT_SEQ),
                "ts": datetime.now().strftime("%H:%M:%S"),
                "stage": stage,
                "rid": _rid(rec),
                "label": f"{rec['venue']}{rec['race_num']}R {rec['start_time']}発走",
                "picks": [{"num": h["num"], "name": h["name"], "odds": h["odds"],
                           "win_prob": h["win_prob"], "ev": h["ev"],
                           "web_value": h["web_value"], "_horse_id": h.get("_horse_id")}
                          for h in picks],
                "wide": (rec.get("wide_picks") or [])[:3],
            }
    if alert and LoggingStore is not None and new_rec.get("_log_context"):
        try:
            store = LoggingStore()
            context = new_rec["_log_context"]
            threshold_version = config_hash(params)[:12]
            evaluation_ids = {}
            for horse in new_rec["horses"]:
                horse_id = horse.get("_horse_id")
                if not horse_id:
                    continue
                evaluation_ids[horse_id] = store.save_ev_evaluation(
                    prediction_run_id=context["prediction_run_id"], race_id=context["race_id"],
                    horse_id=horse_id, win_probability=horse.get("win_prob"), ev=horse.get("ev"),
                    threshold=params["ev_threshold"], decision="alert" if horse.get("picked") else "skip",
                    reason={"stage": stage, "max_odds": params["max_odds"], "min_prob": params["min_prob"]},
                    idempotency_key=f"{context['prediction_run_id']}:{horse_id}:{stage}:{threshold_version}",
                )
            for channel in ("browser", "discord", "line"):
                notification_ids[channel] = []
                for pick in alert["picks"]:
                    horse_id = pick.get("_horse_id")
                    if horse_id not in evaluation_ids:
                        continue
                    single_payload = {**alert, "picks": [{k: v for k, v in pick.items() if not k.startswith("_")}]}
                    notification_id, created = store.reserve_notification(
                        ev_evaluation_id=evaluation_ids[horse_id], channel=channel,
                        dedupe_key=f"{context['race_id']}:{horse_id}:{stage}:{channel}:{threshold_version}",
                        payload=single_payload,
                    )
                    if created:
                        notification_ids[channel].append(notification_id)
            if not any(notification_ids.values()):
                alert = None
        except Exception as exc:
            print(f"[WARN] EV/notification logging failed: {type(exc).__name__}: {exc}")
            notification_ids = {}
    if alert:
        with _LOCK:
            STATE["alerts"].append(alert)
        store = LoggingStore() if LoggingStore is not None else None
        if store and notification_ids:
            for notification_id in notification_ids.get("browser", []):
                store.mark_notification(notification_id, status="sent")
        for channel, sender in (("discord", _send_discord), ("line", _send_line)):
            status, response_code, error = sender(alert)
            if store and notification_ids:
                for notification_id in notification_ids.get(channel, []):
                    store.mark_notification(notification_id, status=status,
                                            response_code=response_code, error_message=error)
    return True


# 通知を伴わないオッズスナップショット専用ステージ (分前, ウィンドウ秒, 断念秒)。
# 断念秒は「これ以上待たずスナップショットは諦めてフラグを立てる」境界で、
# checked15/checked5 の断念ロジック (6分/0分) と同じ考え方で、次のより重要な
# ステージ (15分/5分の通知付き判定) を絶対にブロックしないよう手前で切り上げる。
_SNAPSHOT_STAGES = ((30, 30 * 60, 20 * 60), (10, 10 * 60, 6 * 60), (2, 2 * 60, 0))


def _capture_board_snapshot(rec, stage):
    """Capture the display-only T59d boards on the existing snapshot stage."""
    parsed = board_market.market_probabilities(
        [horse.get("odds") for horse in rec.get("horses", [])])
    if parsed is None:
        board = board_market.unavailable_board(
            "表示不可", stage=stage, status="book_outside")
    else:
        try:
            cname = rec.get("_board_odds_entry_cname")
            if not cname:
                raise ValueError("JRA odds CNAME unavailable")
            fetched = board_market.fetch_jra_odds(cname)
            board = board_market.build_board(rec.get("horses", []), fetched, stage=stage)
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            board = board_market.unavailable_board(
                "取得失敗", stage=stage, status="fetch_failed")
            board["requested_at"] = now
            board["received_at"] = now
            board["error_type"] = type(exc).__name__
            print(f"[WARN] 確率ボード取得失敗 {_rid(rec)} ({stage}分前): "
                  f"{type(exc).__name__}: {exc}")
    context = rec.get("_log_context") or {}
    if LoggingStore is not None and context.get("race_id"):
        try:
            rows = board_market.snapshot_rows(
                board, race_id=context["race_id"],
                race_date=rec.get("race_date") or context["race_id"][:8],
                place=rec.get("venue") or "unknown", race_no=rec.get("race_num") or 0,
                fetch_id=context.get("prediction_run_id"),
            )
            LoggingStore().save_board_odds(rows)
        except Exception as exc:
            board["storage_warning"] = type(exc).__name__
            print(f"[WARN] 確率ボード保存失敗 {_rid(rec)}: {type(exc).__name__}: {exc}")
    return board


def snapshot_odds(rec, stage, *, scheduler_restart=False):
    """通知を伴わずオッズだけ再取得して記録する (時点別オッズ特徴量 Phase C 用のデータ蓄積)。
    refresh_and_alert とは完全に独立しており、アラート生成・LINE/Discord/ブラウザ通知の
    いずれも一切行わない (STATE["alerts"] にも追加しない)。戻り値は、保存済みかつ
    十分=True、保存済みだが不足=False、取得/保存失敗=None のtri-state。"""
    params = STATE["params"]
    previous_snapshots = list(rec.get("_snapshot_history") or ())
    try:
        new_rec = analyze_one(
            rec["url"], params, base_date=rec.get("_start_dt"), stage=stage,
            snapshot_context={
                "scheduled_post_at": rec.get("_start_dt"),
                "previous_snapshots": previous_snapshots,
                "scheduler_restart": scheduler_restart,
            },
        )
    except Exception as e:
        print(f"[WARN] スナップショット取得失敗 {_rid(rec)} ({stage}分前): {e}")
        return None
    quality = new_rec.get("_snapshot_quality") or {}
    persisted = new_rec.get("_snapshot_persisted") is True
    board = _capture_board_snapshot(new_rec, stage)
    with _LOCK:
        for k in ("horses", "n_picked", "wide_picks", "last_update",
                  "odds_ok", "n_odds"):
            rec[k] = new_rec.get(k, rec.get(k))
        rec["board"] = board
        if persisted:
            rec.setdefault("_snapshot_history", []).append({
                "stage": stage,
                "observed_at": quality.get("observed_at"),
                "scheduled_post_at": quality.get("scheduled_post_at"),
            })
            # 品質判定に必要なのは直近の少数件だけ。monitor_stateの肥大化を避ける。
            rec["_snapshot_history"] = rec["_snapshot_history"][-10:]
    if not persisted:
        return None
    return "insufficient_odds" not in quality.get("data_quality_flags", ())


def _process_snapshot_stages(rec, remain, *, scheduler_restart=False):
    """1レース分のPhase Cステージを処理する (通知ステージとは独立)。"""
    for snap_stage, window_sec, giveup_sec in _SNAPSHOT_STAGES:
        flag = f"checked{snap_stage}"
        if not rec.get(flag) and remain <= window_sec:
            capture_state = snapshot_odds(
                rec, snap_stage, scheduler_restart=scheduler_restart)
            # giveup境界を過ぎたら取得/保存失敗(None)でも打ち切る (T40レビュー判断):
            # LoggingStore不全等の持続故障時、通知は無事なままsnapshotだけが
            # 全レース×20秒間隔で終日リトライし続けるsilent failureを防ぐ。
            # 窓内(giveup前)はNone/Falseとも次サイクルで再試行する。
            if capture_state is True or remain <= giveup_sec:
                rec[flag] = True
            # insufficient再試行中も履歴を永続化し、再起動後のcatchup/変更検知に使う。
            _persist_monitor(rec)


def _notification_due_and_pending(rec, remain):
    """現在の残り時間で処理すべき15/5分通知が未完了ならTrue。"""
    return ((not rec.get("checked15", False) and remain <= 15 * 60)
            or (not rec.get("checked5", False) and remain <= 5 * 60))


def scheduler_loop():
    first_scan_cycle = True
    while True:
        time.sleep(20)
        now = datetime.now()
        with _LOCK:
            recs = list(STATE["races"].values())
        for rec in recs:
            start = rec.get("_start_dt")
            if start is None:
                continue
            if rec["finished"]:
                _maybe_sync_result(rec)
                continue
            remain = (start - now).total_seconds()
            if remain < -60:
                rec["finished"] = True
                _maybe_sync_result(rec)
                _persist_monitor(rec)
                continue
            if not rec["checked15"] and remain <= 15 * 60:
                if refresh_and_alert(rec, 15) or remain <= 6 * 60:
                    rec["checked15"] = True
                    _persist_monitor(rec)
            elif not rec["checked5"] and remain <= 5 * 60:
                if refresh_and_alert(rec, 5) or remain <= 0:
                    rec["checked5"] = True
                    _persist_monitor(rec)
            # 30/10/2分前の同期取得は通知判定の後に行い、遅い取得が15/5分前通知を
            # 先にブロックしないようにする。通知側のif/elif本文・条件は変更しない。
            # 15分通知を処理した時点ですでに5分窓内なら、5分通知は次周期の
            # if/elifで先に処理する。通知失敗でpendingの場合もsnapshotを挟まない。
            if _notification_due_and_pending(rec, remain):
                continue
            _process_snapshot_stages(
                rec, remain, scheduler_restart=first_scan_cycle)
        first_scan_cycle = False


def _persist_monitor(rec):
    if LoggingStore is None:
        return
    try:
        context = rec.get("_log_context") or {}
        LoggingStore().save_monitor_state(
            _rid(rec), rec, race_id=context.get("race_id"), start_time=rec.get("_start_dt"),
            checked15=rec.get("checked15", False), checked5=rec.get("checked5", False),
            finished=rec.get("_result_synced", False),
        )
    except Exception as exc:
        print(f"[WARN] monitor state logging failed: {type(exc).__name__}: {exc}")


def _restore_phase2_state():
    """Restore active monitors and retry notifications without storing credentials."""
    if LoggingStore is None:
        return
    store = LoggingStore()
    try:
        for item in store.active_monitors():
            rec = item["payload"]
            if item.get("start_time"):
                start = datetime.fromisoformat(item["start_time"].replace("Z", "+00:00"))
                rec["_start_dt"] = start.astimezone().replace(tzinfo=None)
            rec["checked15"] = item["checked15"]
            rec["checked5"] = item["checked5"]
            rec["finished"] = bool(rec.get("finished", False))
            # 結果未取得の当日・直近開催分も再起動後に復元する。従来は発走1分後を
            # 過ぎると復元対象外となり、結果取得に一度失敗したレースが永久に残った。
            if rec.get("_start_dt") and rec["_start_dt"] > datetime.now() - timedelta(days=2):
                with _LOCK:
                    STATE["races"][item["monitor_key"]] = rec
        if STATE["races"]:
            STATE["status"] = "ready"
            _ensure_scheduler()
        for pending in store.retryable_notifications():
            payload = pending["payload"]
            if pending["channel"] == "browser":
                with _LOCK:
                    payload["id"] = next(_ALERT_SEQ)
                    STATE["alerts"].append(payload)
                result = ("sent", None, None)
            elif pending["channel"] == "discord":
                result = _send_discord(payload)
            elif pending["channel"] == "line":
                result = _send_line(payload)
            else:
                result = ("suppressed", None, "unknown channel")
            store.mark_notification(pending["notification_id"], status=result[0],
                                    response_code=result[1], error_message=result[2])
    except Exception as exc:
        print(f"[WARN] phase2 state restore failed: {type(exc).__name__}: {exc}")


def _maybe_sync_result(rec):
    """Retry official-result reconciliation with bounded, persisted backoff."""
    if rec.get("_result_synced"):
        return
    if rec.get("_result_sync_version") != RESULT_SYNC_VERSION:
        # URL解決方式などが更新された場合は、旧方式で使い切った試行回数を一度だけ
        # リセットする。バージョンは監視状態に永続化されるため再起動のたびには戻らない。
        rec["_result_sync_version"] = RESULT_SYNC_VERSION
        rec["_result_attempts"] = 0
        rec.pop("_next_result_retry", None)
        rec.pop("_result_error", None)
    attempts = int(rec.get("_result_attempts", 0))
    if attempts >= 8:
        return
    next_retry = rec.get("_next_result_retry")
    if next_retry:
        try:
            if datetime.fromisoformat(next_retry) > datetime.now():
                return
        except ValueError:
            pass
    rec["_result_attempts"] = attempts + 1
    try:
        result = fetch_and_save_result(rec["url"])
        rec["_result_synced"] = True
        rec["_result_match_summary"] = result.get("match_summary", {})
        rec.pop("_next_result_retry", None)
        rec.pop("_result_error", None)
        print(f"[result] {result['race_id']} saved: {result['save_stats']}")
    except ResultNotReady as exc:
        delays = (5, 10, 20, 30, 60, 120, 240, 480)
        rec["_next_result_retry"] = (datetime.now() + timedelta(minutes=delays[attempts])).isoformat()
        rec["_result_error"] = str(exc)
        print(f"[WARN] result not ready ({_rid(rec)}, attempt {attempts + 1}): {exc}")
    except Exception as exc:
        rec["_next_result_retry"] = (datetime.now() + timedelta(minutes=30)).isoformat()
        rec["_result_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[WARN] result sync failed ({_rid(rec)}): {type(exc).__name__}: {exc}")
    finally:
        _persist_monitor(rec)


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
        races.append({k: rec.get(k) for k in
                      ("venue", "race_num", "race_info", "start_time", "horses",
                       "n_picked", "wide_picks", "checked15", "checked5", "finished", "last_update",
                       "odds_ok", "day_label")}
                     | {"rid": rid})
    races.sort(key=lambda r: (r["start_time"] or "99:99", r["venue"]))
    return {"status": STATE["status"], "error": STATE["error"],
            "warning": STATE.get("warning", ""),
            "progress": STATE["progress"], "started_at": STATE["started_at"],
            "params": STATE["params"], "races": races}


@bp.route("/api/analyze_start", methods=["POST"])
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
        STATE["warning"] = ""
        STATE["races"] = {}
        STATE["alerts"] = []
        STATE["started_at"] = datetime.now().strftime("%H:%M:%S")
    threading.Thread(target=worker_analyze_all,
                     args=(STATE["params"],), daemon=True).start()
    return jsonify({"success": True})


@bp.route("/api/state")
def api_state():
    with _LOCK:
        return jsonify(_slim_state())


@bp.route("/api/boards")
def api_boards():
    with _LOCK:
        races = []
        for rid, rec in STATE["races"].items():
            board = rec.get("board") or board_market.unavailable_board("取得前")
            races.append({
                "rid": rid, "venue": rec.get("venue"),
                "race_num": rec.get("race_num"), "start_time": rec.get("start_time"),
                "board": board,
            })
        races.sort(key=lambda row: (
            row.get("start_time") or "99:99", row.get("venue") or ""))
        return jsonify({"generated_at": datetime.now(timezone.utc).isoformat(),
                        "races": races})


@bp.route("/api/alerts")
def api_alerts():
    after = request.args.get("after", 0, type=int)
    with _LOCK:
        return jsonify({"alerts": [a for a in STATE["alerts"] if a["id"] > after]})


def _auto_start():
    """--auto-start 指定時: 起動6秒後に解析を自動開始 (週末タスクスケジューラ用)"""
    with _LOCK:
        if STATE["status"] == "analyzing":
            return
        STATE["status"] = "analyzing"
        STATE["error"] = ""
        STATE["warning"] = ""
        STATE["races"] = {}
        STATE["alerts"] = []
        STATE["started_at"] = datetime.now().strftime("%H:%M:%S")
    threading.Thread(target=worker_analyze_all, args=(STATE["params"],), daemon=True).start()
    print("  [auto-start] 解析を自動開始しました")


# standalone実行時 (このモジュールを直接importしてapp.run()する場合や、テストで
# app.test_client()を使う場合) に従来どおりprefix無しでルートが解決できるよう、
# bpをこのモジュール自身のappにも登録しておく (SPEC-T38 §3.1)。
app.register_blueprint(bp)


if __name__ == "__main__":
    # T38: 統合版 jra_suite.py (port 5005) に統合されたため、引数なしの直接起動は
    # 案内のみ表示して終了する (通知の二重送信・監視の二重実行を防ぐため)。
    #
    # ⚠️ 移行ブリッジ (運用切替=SPEC-T38 §5 完了までの暫定措置):
    # 週末タスクスケジューラの start_ev_auto.bat は `jra_ev.py --auto-start` を
    # 呼ぶため、この経路だけは従来どおり単独起動できるまま残す。ここを塞ぐと
    # スケジューラ登録を差し替えるまでレース日の監視・通知が全損する。
    # --test-line (LINE疎通診断) も同様に残す。
    # §5の切替 (タスクスケジューラを start_suite_auto.bat へ差し替え、1開催の
    # 並行監視確認) が完了したら、このブリッジを削除して案内終了だけにする。
    if "--test-line" in sys.argv:
        # LINE設定後の疎通確認: python jra_ev.py --test-line
        stage = int(os.environ.get("EV_LINE_STAGE", "5"))
        _send_line({"stage": stage, "label": "テスト送信 (疎通確認)",
                    "picks": [{"num": 7, "name": "テスト馬", "ev": 9.9,
                               "win_prob": 0.5, "odds": "19.8", "web_value": False}]})
        print("LINEテスト送信を試行しました。スマホで受信を確認してください "
              "(届かない場合は EV_LINE_CHANNEL_TOKEN と友だち追加を確認)")
        sys.exit(0)

    if "--auto-start" in sys.argv:
        print("[注意] 旧単独起動 (--auto-start) です。統合版 jra_suite.py への"
              "切替後はこの経路を使わないでください (通知二重送信の危険)。",
              file=sys.stderr)
        ensure_port_free(PORT, "期待値レース監視サーバー")
        url = f"http://localhost:{PORT}"
        print("=" * 55)
        print("  期待値レース監視 (Webスコア × MLスコア)")
        print("=" * 55)
        print(f"  URL: {url}")
        print("  ブラウザのタブを開いたままにすると 15分前/5分前に通知します")
        if os.environ.get("EV_DISCORD_WEBHOOK", "").strip():
            print("  Discord通知: 有効")
        if os.environ.get("EV_LINE_CHANNEL_TOKEN", "").strip():
            print(f"  LINE通知: 有効 ({os.environ.get('EV_LINE_STAGE', '5')}分前 × "
                  f"EV>={os.environ.get('EV_LINE_MIN_EV', '1.3')})")
        print("  終了: Ctrl+C")
        print("-" * 55)
        _restore_phase2_state()
        threading.Timer(6.0, _auto_start).start()
        app.run(host="127.0.0.1", port=PORT, debug=False)
        sys.exit(0)

    print(
        "[案内] jra_ev.py は統合版 jra_suite.py (port 5005) に統合されました。\n"
        "  python jra_suite.py で起動してください (または start_suite.bat)。\n"
        "  このファイルを直接起動すると通知の二重送信が起きる可能性があるため停止しました。",
        file=sys.stderr,
    )
    sys.exit(1)
