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

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

import scoring  # noqa: E402
from index import analyze_race_url, build_matrix_data  # noqa: E402
from combo_probs import wide_candidates  # noqa: E402
from result_service import ResultNotReady, fetch_and_save_result  # noqa: E402
try:
    from logging_store import LoggingStore, config_hash  # noqa: E402
except Exception as exc:
    LoggingStore = None
    print(f"[WARN] common logging unavailable: {type(exc).__name__}: {exc}")

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
    "warning": "",
    "races": {},                 # rid -> race record
    "alerts": [],                # {id, ts, stage, rid, label, picks}
    "params": {"ev_threshold": 1.3, "max_odds": 50.0, "min_prob": 0.02,
               "wide_overlay": 1.6},
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
    backtest_combo検証: overlay>=1.6 全買いで 2025年110.1% / 2026年110.0%"""
    try:
        cands = wide_candidates(
            [h.get("win_prob") for h in horses],
            [_to_float(h.get("odds")) for h in horses],
            overlay_min=params.get("wide_overlay", 1.6))
    except Exception as e:
        print(f"[WARN] wide計算失敗: {e}")
        return []
    return [{"nums": sorted([horses[i]["num"], horses[j]["num"]]),
             "names": [horses[i]["name"], horses[j]["name"]],
             "overlay": ov, "prob": mp}
            for i, j, ov, mp in cands]


def analyze_one(url, params, base_date=None, day_label=""):
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

    log_context = {}
    # Logging is deliberately best-effort: observability must not stop monitoring.
    if LoggingStore is not None:
        try:
            observed_at = datetime.now().astimezone()
            race_day = (base_date or observed_at).strftime("%Y%m%d")
            race_no = _parse_race_num(result.get("race_info")) or 0
            race_id = f"{race_day}:{venue or 'unknown'}:{race_no:02d}"
            store = LoggingStore()
            store.save_race(
                race_id=race_id, race_date=race_day, venue=venue or "unknown", race_no=race_no,
                surface=race_type, distance_m=dist_val, race_class=result.get("race_class"),
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
                    "feature_snapshot": {"horse_no": h.get("num"), "horse_name": h.get("name")},
                    "data_quality_flags": [] if h.get("win_prob") is not None else ["win_probability_unavailable"],
                })
                odds = _to_float(h.get("odds"))
                odds_rows.append({
                    "race_id": race_id, "horse_id": horse_id, "observed_at": observed_at,
                    "win_odds": odds if odds and odds > 1.0 else None,
                    "popularity": int(float(h["pop"])) if _to_float(h.get("pop")) else None,
                    "source": "jra", "fetch_id": run_id,
                    "idempotency_key": f"{run_id}:{race_id}:{horse_id}",
                    "data_quality_flags": [] if odds and odds > 1.0 else ["win_odds_unavailable"],
                })
            store.save_predictions(run_id, prediction_rows)
            store.save_odds(odds_rows)
            store.finish_run(run_id)
            log_context = {"prediction_run_id": run_id, "race_id": race_id}
        except Exception as exc:
            print(f"[WARN] common logging failed: {type(exc).__name__}: {exc}")

    # オッズが有効な馬の数 (発売前はカードにオッズが載らず全馬None → EV判定不能)
    n_odds = sum(1 for h in horses
                 if (_to_float(h.get("odds")) or 0) > 1.0)
    odds_ok = bool(horses) and n_odds >= max(1, len(horses) // 2)

    start_dt = _parse_start_time(result.get("race_info"), base_date)
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
        "last_update": datetime.now().strftime("%H:%M:%S"),
        "_log_context": log_context,
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


# ─── 通知の外部連携 (実測ログ / Discord) ─────────────────────────────────────

def _log_alert_to_neon(alert):
    """通知時点のオッズ・勝率・EVを Neon ev_alert_log へ記録 (実運用回収率の検証用)。
    後日 ev_log_report.py が races テーブルの確定結果と突き合わせる。失敗しても無視。"""
    try:
        import past_data_service as pds
        conn = pds.get_db_connection(API_DIR)
        if conn is None or not getattr(conn, "is_pg", False):
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ev_alert_log (
                    id SERIAL PRIMARY KEY,
                    date TEXT, venue TEXT, race_num INTEGER, stage INTEGER,
                    horse_num INTEGER, horse_name TEXT,
                    odds_alert DOUBLE PRECISION, win_prob DOUBLE PRECISION,
                    ev DOUBLE PRECISION, web_value BOOLEAN,
                    created_at TIMESTAMP DEFAULT NOW())""")
            date6 = datetime.now().strftime("%y%m%d")
            venue = alert["rid"].split("_")[0]
            race_num = int(alert["rid"].split("_")[1])
            for p in alert["picks"]:
                cur.execute("""INSERT INTO ev_alert_log
                    (date, venue, race_num, stage, horse_num, horse_name,
                     odds_alert, win_prob, ev, web_value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (date6, venue, race_num, alert["stage"], p["num"],
                             p["name"], _to_float(p["odds"]), p["win_prob"],
                             p["ev"], bool(p["web_value"])))
            conn._conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] ev_alert_log 記録失敗: {e}")


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
        new_rec = analyze_one(rec["url"], params)
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
        _log_alert_to_neon(alert)
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


def scheduler_loop():
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
            if rec.get("_start_dt") and rec["_start_dt"] > datetime.now() - timedelta(minutes=1):
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
        print(f"[result] {result['race_id']} saved: {result['save_stats']}")
    except ResultNotReady as exc:
        delays = (5, 10, 20, 30, 60, 120, 240, 480)
        rec["_next_result_retry"] = (datetime.now() + timedelta(minutes=delays[attempts])).isoformat()
        print(f"[WARN] result not ready ({_rid(rec)}, attempt {attempts + 1}): {exc}")
    except Exception as exc:
        rec["_next_result_retry"] = (datetime.now() + timedelta(minutes=30)).isoformat()
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
        STATE["warning"] = ""
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


if __name__ == "__main__":
    if "--test-line" in sys.argv:
        # LINE設定後の疎通確認: python jra_ev.py --test-line
        stage = int(os.environ.get("EV_LINE_STAGE", "5"))
        _send_line({"stage": stage, "label": "テスト送信 (疎通確認)",
                    "picks": [{"num": 7, "name": "テスト馬", "ev": 9.9,
                               "win_prob": 0.5, "odds": "19.8", "web_value": False}]})
        print("LINEテスト送信を試行しました。スマホで受信を確認してください "
              "(届かない場合は EV_LINE_CHANNEL_TOKEN と友だち追加を確認)")
        sys.exit(0)

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
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if "--auto-start" in sys.argv:
        threading.Timer(6.0, _auto_start).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
