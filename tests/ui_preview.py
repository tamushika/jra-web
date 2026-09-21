"""Offline UI-only preview: python -B tests/ui_preview.py --port 5006.

All displayed records are invented fixtures, not racing results or predictions.
No scheduler, restore, production endpoint, database, or outgoing connection runs.
This helper is deliberately separate from production entry points.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sqlite3
import sys
from urllib.parse import urlsplit

from werkzeug.wrappers import Request, Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DAY = "20260919"
STAMP = "2026-09-19T09:12:00+09:00"

# Intentionally schematic; this drawing is a local UI fixture, not a real track map.
SAMPLE_COURSE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="520"
    viewBox="0 0 900 520" role="img" aria-label="SAMPLE course layout fixture">
  <rect width="900" height="520" rx="20" fill="#152333"/>
  <text x="38" y="46" fill="#f0bc67" font-size="22" font-family="sans-serif">SAMPLE / UI TEST ONLY</text>
  <path d="M225 130 H665 C805 130 810 375 665 375 H225 C70 375 70 130 225 130Z"
        fill="none" stroke="#4ed6b0" stroke-width="50"/>
  <path d="M225 130 H665 C805 130 810 375 665 375 H225 C70 375 70 130 225 130Z"
        fill="none" stroke="#d7faef" stroke-width="3" stroke-dasharray="12 10"/>
  <path d="M592 348 V404" stroke="#fff" stroke-width="8"/>
  <text x="560" y="440" fill="#fff" font-size="25" font-family="sans-serif">GOAL</text>
  <circle cx="325" cy="375" r="16" fill="#f0bc67"/>
  <text x="275" y="440" fill="#f0bc67" font-size="25" font-family="sans-serif">START</text>
  <text x="290" y="242" fill="#edf3fa" font-size="30" font-family="sans-serif">SAMPLE COURSE</text>
  <text x="340" y="282" fill="#9aabc0" font-size="22" font-family="sans-serif">NOT TO SCALE</text>
  <path d="M438 376 H486 M469 360 L487 376 L469 392" fill="none" stroke="#0b111b" stroke-width="6"/>
</svg>'''

# Browser-only weather mock for this development preview. CSP still forbids all
# external connections; only this exact endpoint receives a synthetic response.
SAMPLE_WIND_SCRIPT = '''<script id="sample-preview-weather">
(() => {
    const originalFetch = window.fetch.bind(window);
    const missing = new URLSearchParams(window.location.search).get('sample_wind') === 'missing';
    window.fetch = function(input, init) {
        let url;
        try {
            url = new URL(input && typeof input.url === 'string' ? input.url : String(input), window.location.href);
        } catch (_) {
            return originalFetch(input, init);
        }
        if (url.origin === 'https://api.open-meteo.com' && url.pathname === '/v1/forecast') {
            const payload = missing
                ? {source: 'SAMPLE', current_weather: null}
                : {source: 'SAMPLE', timezone: 'Asia/Tokyo', current_weather: {
                    winddirection: 21, windspeed: 1.93, time: '2026-09-19T10:15'}};
            return Promise.resolve(new Response(JSON.stringify(payload), {
                status: 200, headers: {'Content-Type': 'application/json', 'X-Preview-Source': 'SAMPLE'}
            }));
        }
        return originalFetch(input, init);
    };
})();
</script>'''


def _blocked_io(*_args, **_kwargs):
    raise RuntimeError("SAMPLE preview forbids databases and outgoing connections")


def _quality(n, ok=True):
    return {"ok": ok, "scored": n if ok else 0, "total": n,
            "missing": [] if ok else [{"num": i, "reason": "初出走"}
                                      for i in range(1, n + 1)]}


def _horses(n, ok=True):
    return [{"num": i, "name": f"サンプルホース{i:02d}", "jock": "サンプル騎手",
             "odds": round(3.1 + i * 1.2, 1), "pop": i, "grade": "○" if i == 1 else "",
             "ml_score": 2.0 - i / 10 if ok else None,
             "score": round(2.0 - i / 10, 2) if ok else None,
             "score_source": "ml" if ok else "unavailable",
             "score_failure_reason": None if ok else "初出走", "score_details": [],
             "win_prob": round(1 / n, 3) if ok else None,
             "ev": 1.12 if ok and i == 1 else (0.72 if ok else None),
             "picked": bool(ok and i == 1), "web_value": False, "scratched": False}
            for i in range(1, n + 1)]


def state_fixture():
    races = []
    for venue_index, venue in enumerate(("阪神", "中山")):
        for race in range(1, 13):
            if venue_index == 0 and race == 4:
                continue
            field = 16 if race in (4, 5, 6) else 8 + race % 7
            ok = race not in (4, 5, 6)
            minutes = 9 * 60 + 45 + (race - 1) * 30 + venue_index * 15
            races.append({"rid": f"{DAY}:{venue}:{race}", "venue": venue,
                          "race_num": race, "race_info": f"【{venue} {race}R】サンプルレース",
                          "start_time": f"{minutes // 60:02d}:{minutes % 60:02d}",
                          "horses": _horses(field, ok), "n_picked": 1 if ok else 0,
                          "wide_picks": [], "checked15": False, "checked5": False,
                          "finished": False, "last_update": "09:12:00", "odds_ok": True,
                          "day_label": "9月19日・SAMPLE", "url": f"/sample/race/{venue_index}/{race}",
                          "ml_coverage": {"scored": field if ok else 0, "total": field, "ok": ok},
                          "probability_quality": _quality(field, ok)})
    return {"status": "ready", "error": "", "warning": "", "started_at": "09:10:00",
            "progress": {"done": 24, "total": 24}, "params": {"ev_threshold": 1.1},
            "races": races, "health": {"date": DAY,
                "analysis": {"total": 24, "succeeded": 23, "failed": 0, "excluded": 1,
                             "completed_at": STAMP},
                "collection": {"last_fetch_at": STAMP, "last_odds_at": STAMP,
                               "last_save_at": STAMP, "fetch_errors": 0,
                               "save_errors": 0, "error_types": []},
                "stages": {str(stage): {"saved": 0, "waiting": 23, "missing": 0,
                                        "unknown": 0, "quality_failed": 0}
                           for stage in (30, 10, 2)},
                "monitor": {"last_iteration_at": STAMP}}}


def win5_fixture():
    return {"success": True, "date": "9月19日・SAMPLE", "date8": DAY,
            "races": [{"idx": i, "venue": "阪神" if i % 2 == 0 else "中山",
                       "race_num": 10 + i // 2, "time": f"15:{i * 10:02d}",
                       "field_size": [14, 16, 12, None, 18][i],
                       "field_size_source": "ev_monitor" if i != 3 else None,
                       "url": f"/sample/win5/{i}"} for i in range(5)]}


def race_fixture(url="/sample/race/0/2"):
    """Complete race-page data, including six dates/venues and twelve race buttons."""
    parts = urlsplit(str(url)).path.strip("/").split("/")
    venue_index = 0
    race_num = 2
    if len(parts) >= 4 and parts[:2] == ["sample", "race"]:
        try:
            venue_index = int(parts[2]) % 2
            race_num = max(1, min(12, int(parts[3])))
        except ValueError:
            pass
    venue = ("阪神", "中山")[venue_index]
    horses = [{**horse, "waku": (horse["num"] + 1) // 2,
               "score_ml": horse["ml_score"], "score_ml_details": ["SAMPLE: 架空スコア"],
               "place_prob": 0.2, "place_prob_details": ["SAMPLE: 架空データ"],
               "iv": "中3週", "dist_diff": "同距離", "sex_age": "牡3", "kg": 57,
               "kyakushitsu": "先行", "affi": "栗東", "sire": "サンプル父",
               "bms": "サンプル母父", "ultra_details": [], "hist": [], "sire_buysell": []}
              for horse in _horses(16)]
    matrix = [{"text": f"9/{19 + day}({'土日月'[day]}) 4回{place}{5 + day}日",
               "races": [{"r": race, "url": f"/sample/race/{idx}/{race}?day={19 + day}"}
                         for race in range(1, 13)]}
              for day in range(3) for idx, place in enumerate(("阪神", "中山"))]
    return {"success": True, "race_info": f"【{venue} {race_num}R】SAMPLE 芝2000m 16頭",
            "venue": venue, "race_num": race_num, "race_type": "芝", "dist_val": 2000,
            "race_date": DAY, "race_class": "3歳未勝利", "horses": horses,
            "baba_info": "SAMPLE 馬場情報 — 芝: 良 / ダート: 良", "course_record": "",
            "criteria_lines": ["SAMPLE: 実際の好走条件・予想ではありません。"],
            "has_double_circle": False, "matrix_data": matrix, "harab_index": "SAMPLE",
            "feature_text": "SAMPLE 表示確認用の架空データです。\nコース図はUI検証専用の模式図であり、実際の形状・距離を表しません。",
            "course_image": "/sample/course-layout.svg", "notable_sires": [],
            "cached_from_monitor": "09:12:00", "monitor_captured_at": STAMP,
            "monitor_cache_version": "1900000000000000001", "monitor_stage": None}


def perf_fixture(date=None):
    dates = ["2026-09-19", "2026-09-13", "2026-09-12", "2026-09-06", "2026-09-05"]
    model = {"app": "ev", "model": "sample_model", "version": "SAMPLE",
             "config": "demo0001", "period": "20260905〜20260919", "races": 120,
             "settled": 114, "win_rate": 26.3, "top3_rate": 58.8,
             "tan_roi": 82.4, "fuku_roi": 86.8}
    daily = [{**model, "date": value.replace("-", ""), "races": 24,
              "settled": 18 if i == 0 else 24, "win_rate": [27.8, 25, 29.2, 20.8, 29.2][i],
              "tan_roi": [80, 91, 72, 78, 90][i], "fuku_roi": [88, 92, 75, 87, 92][i]}
             for i, value in enumerate(dates)]
    selected = date if date in dates else None
    shown = selected or dates[0]
    rows = [{"date": shown.replace("-", ""), "race_id": f"{shown}:阪神:{i}",
             "venue": "阪神", "race_no": i, "race_name": "サンプル特別",
             "app": "ev", "model": "sample_model", "version": "SAMPLE", "config": "demo0001",
             "horse": i + 1, "horse_name": f"サンプルホース{i + 1:02d}", "score": 1.9 - i / 10,
             "winner": i + 1 if i % 3 == 0 else i + 2, "winner_name": "サンプル勝馬",
             "result": [1, 3, 6, 2, None, 8][i - 1], "win_payout": 350 if i == 1 else 0,
             "place_payout": 140 if i in (1, 2, 4) else 0} for i in range(1, 7)]
    ev_sum = {"n": 24, "settled": 22, "win": 5, "top3": 11, "tan_ret": 1740, "fuku_ret": 1910}
    bucket = {"n_bets": 28, "n_pending": 2, "n_skipped_budget": 3, "n_skipped_data": 2,
              "stake_yen": 2800, "payout_yen": 2310, "roi_pct": 82.5}
    win5_rows = [{"venue": "阪神" if i % 2 == 0 else "中山", "race_no": 10 + i // 2,
                  "race_name": "SAMPLE特別", "settled": True, "hit": i != 3,
                  "picks": [{"no": 1, "pop": 1, "hit": i != 3}, {"no": 2, "pop": 3, "hit": False}],
                  "winner": {"no": 1 if i != 3 else 5, "name": "サンプル勝馬", "pop": 1 if i != 3 else 5}}
                 for i in range(5)]
    win5 = {"created_at": f"{shown} 14:00", "points": 32, "budget": 50, "est": 0.04,
            "method": "sample", "single_axis": False, "hits": [True, True, True, False, True],
            "all_settled": True, "win5_hit": False, "races": win5_rows,
            "payout": {"fetched": True, "payout_yen": 123400, "hit_ticket_count": 12,
                       "carryover_flag": False, "carryover_amount": 0, "mismatch": False},
            "shadow_500": None}
    return {"summary": [{**model, **({"races": 24, "settled": 18} if selected else {})}],
            "daily": [d for d in daily if not selected or d["date"] == selected.replace("-", "")],
            "pending_races": 6, "selected_date": selected, "available_dates": dates,
            "race_details": rows,
            "race_dates": [{"date": value, "races": 24, "settled": 18 if i == 0 else 24,
                            "win": 5, "top3": 12, "tan_roi": 82.4, "fuku_roi": 86.8, "models": 1}
                           for i, value in enumerate(dates)],
            "ev": {"sum": ev_sum, "rows": [{"at": f"{shown} 14:45", "race_id": f"{DAY}:阪神:10",
                     "horse": 3, "ev": 1.12, "prob": 0.2, "odds": 5.6, "pop": 4,
                     "result": 3, "win_payout": 0, "place_payout": 180}]},
            "ev_dates": [{"date": value, **ev_sum, "tan_roi": 79.1, "fuku_roi": 86.8} for value in dates],
            "win5": [win5], "win5_dates": [{"date": value, "hits": 4, "total": 5,
                "all_settled": True, "win5_hit": False, "payout_fetched": True,
                "payout_yen": 123400, "shadow_hit": None, "shadow_points": None} for value in dates],
            "win5_shadow_summary": {"budget": 500,
                "target_days": 0, "hit_days": 0, "unavailable_days": 0, "cost_yen": 0,
                "payout_yen": 0, "roi_pct": None, "since": None, "until": None},
            "days": [{"date": value, "models": [daily[i]], "ev": {**ev_sum,
                       "tan_roi": 79.1, "fuku_roi": 86.8}, "win5": {**win5, "hits": 4, "total": 5}}
                     for i, value in enumerate(dates)],
            "series": {"models": [{**model, "points": [{**d, "date": dates[i]}
                         for i, d in reversed(list(enumerate(daily)))]}],
                       "ev": {"points": [{"date": value, "settled": 22, "win": 5,
                              "tan_roi": 79.1, "fuku_roi": 86.8} for value in reversed(dates)]},
                       "win5": {"points": [{"date": value, "hits": 4, "total": 5, "win5_hit": False}
                                            for value in reversed(dates)]}},
            "virtual_betting": {"disclaimer": "SAMPLE 仮想運用。実購入・購入推奨ではありません。",
                "labels": {"v1": "サンプル単勝", "p5-v1": "サンプル5頭", "p3-v1": "サンプル複勝"},
                "patterns": {policy: {"cumulative": {"main": bucket, "control": bucket},
                    "max_drawdown_yen": 900, "days": [{"date": value, "main": bucket, "control": bucket}
                                                       for value in dates]}
                             for policy in ("v1", "p5-v1", "p3-v1")}, "p3_calibration": None}}


def _fixture_response(req):
    path = req.path
    if req.method == "GET":
        if path == "/ev/api/state":
            return state_fixture(), 200
        if path == "/ev/api/alerts":
            return {"alerts": []}, 200
        if path == "/ev/api/boards":
            return {"races": []}, 200
        if path == "/perf/api/perf":
            return perf_fixture(req.args.get("date")), 200
        if path == "/win5/api/win5_races":
            return win5_fixture(), 200
        if path == "/win5/api/win5_watch":
            return {"status": "idle", "result": None}, 200
        if path == "/race/api/cache":
            return {"available": True, "result": race_fixture(req.args.get("url", "")),
                    "cache_version": "1900000000000000001", "captured_at": STAMP,
                    "age_seconds": 12}, 200
        if path == "/race/api/latest_url":
            return {"url": "/sample/race/0/2"}, 200
        if path == "/race/api/track_bias":
            return {"error": "No recent races: SAMPLE preview contains no actual track-bias data"}, 200
    if req.method == "POST":
        if path == "/race/api/scrape":
            data = req.get_json(silent=True) or {}
            return race_fixture(data.get("url", "")), 200
        if path == "/ev/api/analyze_start":
            return {"success": True, "sample": True}, 200
        if path == "/win5/api/win5_analyze_one":
            data = req.get_json(silent=True) or {}
            idx = data.get("idx", 0)
            if not isinstance(idx, int) or not 0 <= idx < 5:
                return {"error": "Invalid SAMPLE race index"}, 400
            race = win5_fixture()["races"][idx]
            n = race["field_size"] or 16
            return {"success": True, "idx": idx, "race_date": DAY, "venue": race["venue"],
                    "race_num": race["race_num"], "race_type": "芝", "dist_val": 1600,
                    "race_info": f"【{race['venue']} {race['race_num']}R】SAMPLE 芝1600m {race['time']}発走",
                    "horses": _horses(n), "field_size": n, "field_size_source": "analysis",
                    "upset_rank": "B", "upset_score": 42, "probability_quality": _quality(n)}, 200
    return {"error": "SAMPLE preview: this operation is blocked; no production API was called"}, 403


class FixtureMiddleware:
    """Intercept before the suite's /race DispatcherMiddleware as well as Flask."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/sample/course-layout.svg" and environ.get("REQUEST_METHOD") in ("GET", "HEAD"):
            response = Response(SAMPLE_COURSE_SVG, content_type="image/svg+xml; charset=utf-8")
            response.headers["Cache-Control"] = "no-store"
            return response(environ, start_response)
        if "/api/" in path or path.endswith("/api") or environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            import json
            payload, status = _fixture_response(Request(environ))
            response = Response(json.dumps(payload, ensure_ascii=False), status=status,
                                content_type="application/json; charset=utf-8")
            response.headers["Cache-Control"] = "no-store"
            return response(environ, start_response)
        return self.app(environ, start_response)


def build_preview_app():
    # Fail closed even if a future production route/import gains a side effect.
    sqlite3.connect = _blocked_io
    socket.create_connection = _blocked_io
    socket.socket.connect = _blocked_io
    import jra_suite

    app = jra_suite.create_app()
    app.wsgi_app = FixtureMiddleware(app.wsgi_app)

    def label_sample(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' data:; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'")
        if response.mimetype == "text/html":
            response.direct_passthrough = False
            banner = ('<div id="sample-preview-label" style="position:fixed;right:12px;bottom:12px;'
                      'z-index:2147483647;background:#6f2b12;color:#fff;border:1px solid #ffb071;'
                      'padding:7px 12px;border-radius:8px;font:600 12px system-ui;pointer-events:none">'
                      'SAMPLE · 架空データ / 外部取得・DB操作なし</div>')
            html = response.get_data(as_text=True)
            # Keep the mock confined to the preview's race document, before its
            # own application script; production files are not modified.
            html = html.replace('<script src="script.js"></script>',
                                SAMPLE_WIND_SCRIPT + '\n<script src="script.js"></script>')
            response.set_data(html.replace("</body>", banner + "</body>"))
        return response

    app.after_request(label_sample)
    jra_suite.index.app.after_request(label_sample)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5006)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 5005:
        parser.error("Use an unprivileged preview port other than production port 5005")
    app = build_preview_app()
    print(f"SAMPLE preview only: http://127.0.0.1:{args.port}; no production I/O", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
