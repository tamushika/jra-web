"""
統合ローカルサーバー (T38: 3アプリの単一ポート統合)
====================================================
これまで別ポートで個別起動していた3つのローカル専用Flaskアプリを、
単一プロセス・単一ポートに統合するエントリポイント。

  - オッズ監視     (jra_ev.py, 従来 5003)  → /ev/
  - WIN5予想       (jra_win5.py, 従来 5002) → /win5/
  - 実績ダッシュボード (jra_perf.py, 従来 5004) → /perf/
  - `/` はポータル (3セクションへのリンク + バックグラウンドループの生存表示)

各アプリは Flask Blueprint 化のリファクタのみを行っており、通知の発火条件・
文言・送信経路 (jra_ev.py の refresh_and_alert / _send_line / _send_discord 等) は
一切変更していない。モジュールレベルの状態 (STATE、_LOCK、_WATCH_LOCK 等) と
処理関数は各モジュールに残したまま、ルートだけをこのプロセスに集約する。

scheduler_loop (ev) と _watch_loop (win5) は、このモジュールが所有し、
プロセス内でちょうど1回だけ起動する (start_background_loops)。

【起動】 python jra_suite.py  (または start_suite.bat)
【URL】  http://localhost:5005  (環境変数 JRA_SUITE_PORT で変更可)
"""
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.dispatcher import DispatcherMiddleware

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from api.port_guard import ensure_port_free, is_port_in_use  # noqa: E402

import jra_ev  # noqa: E402
import jra_win5  # noqa: E402
import jra_perf  # noqa: E402
import jra_graded  # noqa: E402

# jra_win5 が `from index import analyze_race_url, ...` で既に api/index.py を
# import 済み (sys.modules["index"]) のため、ここでの `import index` は
# 再実行されず同じモジュールオブジェクト (= 同じ index.app) を再利用する
# (SPEC-T73 §2.1: 二重importで別モジュールオブジェクトになることを避ける)。
import index  # noqa: E402

PORT = int(os.environ.get("JRA_SUITE_PORT", "5005"))

_SECTIONS = (
    {"prefix": "ev", "title": "オッズ監視", "desc": "期待値レース監視 (旧 jra_ev.py, port 5003)",
     "loop": "ev-scheduler-loop"},
    {"prefix": "win5", "title": "WIN5予想", "desc": "スコアリング + 荒れ度配分 (旧 jra_win5.py, port 5002)",
     "loop": "win5-watch-loop"},
    {"prefix": "perf", "title": "実績ダッシュボード", "desc": "予測実績の集計表示 (旧 jra_perf.py, port 5004)",
     "loop": None},
    {"prefix": "graded", "title": "重賞データ", "desc": "重賞の過去傾向 (ability.db 由来キャッシュ)",
     "loop": None},
    {"prefix": "race", "title": "レース詳細", "desc": "個別レース解析 (本番Webと同じ画面)",
     "loop": None},
)

# タブバー用ラベル (SPEC-T38 §5b: ループ生存バッジをタブバー内に統合)
_LOOP_BADGE_LABELS = {
    "ev-scheduler-loop": "オッズ監視ループ",
    "win5-watch-loop": "WIN5監視ループ",
}

_EV_LOOP_THREAD_NAME = "jra-suite-ev-scheduler-loop"
_WIN5_LOOP_THREAD_NAME = "jra-suite-win5-watch-loop"
_LOOP_THREAD_NAMES = {"ev-scheduler-loop": _EV_LOOP_THREAD_NAME,
                      "win5-watch-loop": _WIN5_LOOP_THREAD_NAME}


# ─── 静的配信ガード (SPEC-T38 §3.2) ────────────────────────────────────────
# 各Blueprintのprefix配下のみでBASE_DIR全体を配信する (現行の各アプリ本体は
# static_folder=BASE_DIR, static_url_path="/" のまま変更しない)。
# .env・*.db・backups/ は配信拒否し、パストラバーサルは send_from_directory
# (werkzeug safe_join) が拒否するのに加えてここでも明示的に弾く。
def _is_denied_static_path(rel_path):
    norm = rel_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        # ルート ("" / "/") は静的ファイル配信ではない (index.app の "/" 等) ので
        # 拒否しない。SPEC-T73 §2.1: この関数を index.app の before_request でも
        # 使うため、既存の prefix 静的配信 (filename は常に非空) の挙動は変えない。
        return False
    if ".." in parts:
        return True
    # SPEC §3.2の .env / *.db / backups/ に加え、レビューで .git / *.sqlite* /
    # *.log も拒否 (リポジトリ直下配信のためgit内部情報・database.sqlite・
    # 運用ログが露出するのを防ぐ)。
    if parts[0].lower() in ("backups", ".git"):
        return True
    last = parts[-1]
    lower = last.lower()
    if last == ".env" or last.endswith(".env"):
        return True
    if lower.endswith((".db", ".sqlite", ".sqlite3", ".log")):
        return True
    return False


def _serve_prefixed_static(filename):
    if _is_denied_static_path(filename):
        abort(404)
    return send_from_directory(BASE_DIR, filename)


# ─── /race/ マウント (SPEC-T73 §2.1) ────────────────────────────────────────
# api/index.py の Flask アプリ (index.app) をそのまま DispatcherMiddleware で
# /race にマウントする。index.app は static_folder='../' でリポジトリ直下を
# 配信するため、_is_denied_static_path と同等の拒否を before_request で効かせる
# (index.py 自体は変更しない = フックを外側から登録するだけ)。
_RACE_GUARD_ATTR = "_jra_suite_static_guard_installed"


def _install_race_static_guard():
    """index.app に静的配信ガード・オッズ監視キャッシュ短絡の before_request を
    1回だけ登録する。create_app() が複数回呼ばれても (index.app はモジュール
    単位のシングルトンなので) フックが重複登録されないようガードする
    (SPEC-T73b §2.2.4: 既存の1回限りガードにキャッシュ短絡フックも統合する)。"""
    if getattr(index.app, _RACE_GUARD_ATTR, False):
        return
    index.app.before_request(_reject_denied_race_static_paths)
    index.app.before_request(_short_circuit_race_scrape_with_cache)
    setattr(index.app, _RACE_GUARD_ATTR, True)


def _reject_denied_race_static_paths():
    # DispatcherMiddlewareでマウントされた index.app 内では request.path は
    # "/race" を含まない (SCRIPT_NAME="/race", PATH_INFO=残り) ので、
    # そのまま _is_denied_static_path に渡せる。
    if _is_denied_static_path(request.path):
        abort(404)


# ─── /race/api/scrape のオッズ監視キャッシュ短絡 (SPEC-T73b §2.2) ──────────
def _race_cache_snapshot(url, now=None):
    """監視キャッシュの読取専用API。未取得・別日でも外部取得には進まない。"""
    cached = jra_ev.get_cached_analysis(url) if url else None
    if cached is None:
        return {"available": False, "reason": "not_cached"}
    now = now or datetime.now(jra_ev.JST)
    try:
        captured = datetime.fromisoformat(cached.get("captured_at") or "")
        if captured.tzinfo is None or not cached.get("cache_version"):
            raise ValueError("timezone and cache version required")
        captured = captured.astimezone(jra_ev.JST)
    except (TypeError, ValueError):
        return {"available": False, "reason": "invalid_timestamp"}
    today = now.astimezone(jra_ev.JST).strftime("%Y%m%d")
    race_date = str(cached.get("race_date") or "").replace("-", "")
    if captured.strftime("%Y%m%d") != today or race_date != today:
        return {"available": False, "reason": "different_day"}
    if captured > now:
        return {"available": False, "reason": "invalid_timestamp"}
    age = max(0, int((now - captured).total_seconds()))
    result = {**cached["result"], "cached_from_monitor": cached.get("cached_at"),
              "monitor_stage": cached.get("stage"),
              "monitor_captured_at": captured.isoformat(),
              "monitor_cache_version": cached.get("cache_version")}
    return {"available": True, "reason": None, "result": result,
            "captured_at": captured.isoformat(), "cache_version": cached.get("cache_version"),
            "age_seconds": age, "stale": age > 180}


def _short_circuit_race_scrape_with_cache():
    """オッズ監視 (jra_ev.analyze_one) が既に同じURLを解析済みなら、
    jra_ev.RACE_ANALYSIS_CACHE の結果を即返して index.app の scrape() (実URL
    再取得) を呼ばせない。mode="詳細" または force 指定時、キャッシュ未ヒット
    時は None を返して素通しする (before_request はNoneならviewが呼ばれる)。"""
    if request.path == "/api/cache":
        if request.method != "GET":
            return jsonify({"error": "GETのみ対応"}), 405
        response = jsonify(_race_cache_snapshot(request.args.get("url")))
        response.headers["Cache-Control"] = "no-store"
        return response
    if request.path != "/api/scrape" or request.method != "POST":
        return None
    data = request.get_json(silent=True) or {}
    if data.get("mode") == "詳細" or data.get("force"):
        return None
    url = data.get("url")
    if not url:
        return None
    snapshot = _race_cache_snapshot(url)
    if not snapshot["available"]:
        return None
    response = jsonify(snapshot["result"])
    response.headers["Cache-Control"] = "no-store"
    return response


# ─── バックグラウンドループの一元管理 (SPEC-T38 §3.3) ──────────────────────
_LOOPS_STARTED = threading.Event()
_LOOPS_LOCK = threading.Lock()


def start_background_loops():
    """scheduler_loop (ev) と _watch_loop (win5) をプロセス内でちょうど1回だけ起動する。
    2回目以降の呼び出しはno-op (Falseを返す)。各モジュール既存の再入ガード
    (jra_ev._SCHEDULER_STARTED / jra_win5._WATCH_THREAD) はそのまま維持し、
    ここではその起動を統合エントリに一元化するだけ (起動主体を1箇所に絞る)。"""
    with _LOOPS_LOCK:
        if _LOOPS_STARTED.is_set():
            return False
        _LOOPS_STARTED.set()

    with jra_ev._LOCK:
        if not jra_ev._SCHEDULER_STARTED[0]:
            jra_ev._SCHEDULER_STARTED[0] = True
            threading.Thread(target=jra_ev.scheduler_loop, daemon=True,
                             name=_EV_LOOP_THREAD_NAME).start()

    with jra_win5._WATCH_LOCK:
        if not jra_win5._WATCH_THREAD[0]:
            jra_win5._WATCH_THREAD[0] = True
            threading.Thread(target=jra_win5._watch_loop, daemon=True,
                             name=_WIN5_LOOP_THREAD_NAME).start()

    # 既存の監視状態・未送信通知の復元 (jra_ev.py の旧__main__が起動時に行っていたのと同じ)。
    # 内部で_ensure_scheduler()を呼ぶが、上でガードを立てた後なのでno-op。
    jra_ev._restore_phase2_state()
    return True


def _loop_alive(thread_name):
    return any(t.name == thread_name and t.is_alive() for t in threading.enumerate())


# ─── ポータル (SPEC-T38 §5b: iframeタブシェル) ──────────────────────────────
# 「リンク集」から「タブシェル」へ変更 (2026-07-18 追補)。
#   - 上部固定タブバー (3タブ + ループ生存バッジ)
#   - 下部に same-origin iframe を3枚。切替は display (CSSクラス) のみで
#     行い、iframeはアンマウントしない → 各アプリのクライアント側状態
#     (WIN5解析結果グリッド等) が切替後も保持される
#   - iframeは初回アクティブ化時に data-src → src へ差し替えて遅延ロードする
#   - アクティブタブは URLハッシュ (#ev/#win5/#perf) + localStorage に永続化
#   - 各アプリのHTML/JS/Pythonロジックは変更しない (シェルのみで実現)
_PORTAL_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JRA予想スイート</title>
<link rel="stylesheet" href="/ev/style.css">
<style>
  html, body { display: block; margin: 0; padding: 0; height: 100%; overflow: hidden;
               background: var(--bg-color); color: var(--text-main); }
  #tabbar { display: grid; grid-template-columns: auto 1fr auto;
            grid-template-areas: "brand status actions" "tabs tabs loops";
            align-items: center; gap: 0 24px; background: #101925; padding: 12px 24px 0;
            border-bottom: 1px solid var(--border-color); flex: 0 0 auto; }
  .suite-brand { grid-area: brand; font-size: 17px; font-weight: 750; letter-spacing: -.03em; white-space: nowrap; }
  .suite-brand span { display: inline-block; font-size: 9px; color: var(--primary); letter-spacing: .15em; margin-left: 9px; }
  .suite-actions { grid-area: actions; justify-self: end; }
  .suite-nav { grid-area: tabs; display: flex; gap: 4px; overflow-x: auto; min-width: 0; margin-top: 9px;
               scrollbar-width: thin; scrollbar-color: #40546b transparent; }
  #tabbar .tab { cursor: pointer; padding: 12px 18px; color: var(--text-muted); font-size: 13px;
                 white-space: nowrap; background: transparent; border-radius: 7px 7px 0 0; border-bottom: 2px solid transparent; }
  #tabbar .tab.active { color: var(--primary); background: #18302c; border-bottom-color: var(--primary); }
  .suite-loops { grid-area: loops; display: flex; gap: 8px; justify-content: flex-end; }
  #tabbar .loop-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 10px; color: var(--text-muted); white-space: nowrap; }
  .loop-badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: #718097; }
  .loop-badge.alive::before { background: var(--primary); }
  .loop-badge.dead::before { background: var(--accent); }
  #tabbar .global-start { cursor: pointer; min-height: 36px; padding: 0 18px;
                           border: none; border-radius: 8px; background: var(--primary); color: #08241c;
                           font-weight: 750; font-size: 12px; white-space: nowrap; }
  #tabbar .global-start:disabled { opacity: 0.6; cursor: default; }
  #tabbar .global-status { grid-area: status; font-size: 11px; color: var(--text-muted); min-width: 0;
                           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #tabbar .global-status.error { color: #ffb347; }
  #shell { display: flex; flex-direction: column; height: 100%; }
  .collection-panel { flex: 0 0 auto; border-bottom: 1px solid var(--border-color); background: #0d1722; }
  .collection-panel > summary { cursor: pointer; padding: 9px 24px; font-size: 11px; color: var(--text-muted);
                               list-style-position: inside; overflow-wrap: anywhere; }
  .collection-panel.warning > summary { color: var(--accent); }
  .health-label { color: var(--text-main); margin-right: 12px; font-weight: 650; }
  #collectionHealth { padding: 8px 24px 18px; font-size: 12px; line-height: 1.9;
                       overflow-wrap: anywhere; white-space: pre-line; columns: 2; column-gap: 36px; }
  #collectionHealth.warning { color: #ffd69b; }
  #frames { position: relative; flex: 1 1 auto; min-height: 0; }
  #frames iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                    border: none; display: none; }
  #frames iframe.active { display: block; }
  @media(max-width:900px) {
    #tabbar { gap: 0 12px; padding: 10px 14px 0; grid-template-columns: 1fr auto;
              grid-template-areas: "brand actions" "status status" "tabs tabs"; }
    .suite-loops { display: none; } .global-status { margin-top: 8px; }
    #tabbar .tab { padding: 10px 14px; font-size: 12px; }
    .collection-panel > summary { padding: 8px 14px; }
    #collectionHealth { padding: 8px 14px 14px; columns: 1; max-height: 32vh; overflow-y: auto; }
  }
</style>
</head>
<body>
<div id="shell">
  <div id="tabbar">
    <div class="suite-brand">JRA 予想スイート <span>LOCAL ANALYTICS</span></div>
    <div class="suite-actions"><button id="globalStart" class="global-start" title="本日の全レースを解析し、WIN5対象レースも取得します">解析開始</button></div>
    <span id="globalStatus" class="global-status"></span>
    <nav class="suite-nav" role="tablist" aria-label="アプリ切り替え">
    {% for t in tabs %}
    <button type="button" class="tab" data-tab="{{ t.prefix }}" role="tab" aria-controls="frame-{{ t.prefix }}">{{ t.title }}</button>
    {% endfor %}
    </nav>
    <div class="suite-loops">
    {% for b in loop_badges %}
    <span class="loop-badge {{ 'alive' if b.alive else 'dead' }}" data-loop="{{ b.loop }}">
      {{ b.label }}: {{ '稼働中' if b.alive else '未起動' }}
    </span>
    {% endfor %}
    </div>
  </div>
  <details class="collection-panel" id="healthPanel">
    <summary><span class="health-label">収集状況</span><span id="healthSummary" role="status">確認中…</span></summary>
    <div id="collectionHealth">収集状態を確認中…</div>
  </details>
  <div id="frames">
    {% for t in tabs %}
    <iframe id="frame-{{ t.prefix }}" data-tab="{{ t.prefix }}" data-src="/{{ t.prefix }}/" title="{{ t.title }}"></iframe>
    {% endfor %}
  </div>
</div>
<script>
(function () {
  "use strict";
  var TABS = [{% for t in tabs %}"{{ t.prefix }}"{{ ", " if not loop.last else "" }}{% endfor %}];
  var DEFAULT_TAB = "ev";
  var STORAGE_KEY = "jra_suite_active_tab";

  function readInitialTab() {
    var hashTab = (window.location.hash || "").replace(/^#/, "");
    if (TABS.indexOf(hashTab) !== -1) { return hashTab; }
    var stored = null;
    try { stored = window.localStorage.getItem(STORAGE_KEY); } catch (e) { stored = null; }
    if (TABS.indexOf(stored) !== -1) { return stored; }
    return DEFAULT_TAB;
  }

  function activateTab(name) {
    if (TABS.indexOf(name) === -1) { name = DEFAULT_TAB; }
    TABS.forEach(function (prefix) {
      var iframe = document.getElementById("frame-" + prefix);
      var tabEl = document.querySelector('.tab[data-tab="' + prefix + '"]');
      var isActive = (prefix === name);
      if (iframe) {
        // 遅延ロード: 初めてアクティブになった時だけ data-src を src に反映する。
        if (isActive && !iframe.getAttribute("src")) {
          iframe.setAttribute("src", iframe.getAttribute("data-src"));
        }
        // アンマウントせず display 切替のみ (クライアント状態を保持するため)。
        iframe.classList.toggle("active", isActive);
      }
      if (tabEl) { tabEl.classList.toggle("active", isActive); tabEl.setAttribute("aria-selected", String(isActive)); }
    });
    try { window.localStorage.setItem(STORAGE_KEY, name); } catch (e) { /* noop */ }
    if ((window.location.hash || "").replace(/^#/, "") !== name) {
      window.location.hash = name;
    }
  }

  document.querySelectorAll("#tabbar .tab").forEach(function (tabEl) {
    tabEl.addEventListener("click", function () {
      activateTab(tabEl.getAttribute("data-tab"));
    });
  });

  window.addEventListener("hashchange", function () {
    activateTab((window.location.hash || "").replace(/^#/, ""));
  });

  // SPEC-T74: タブバー左端の統合「解析開始」ボタン。オッズ監視の解析開始と
  // WIN5対象レース取得を同時に行い、/ev/api/state のポーリングで状態を表示する。
  var globalPollTimer = null;
  var globalPollInFlight = false;
  var globalWin5Suffix = "";
  // SPEC-T77: startAll() で立て、解析完了 (analyzing=false かつ races あり) を
  // 検知した最初の pollGlobalStatus() でレース詳細タブへ自動選択を通知する。
  var globalAwaitingRacePick = false;

  function setGlobalStatus(text, isError) {
    var el = document.getElementById("globalStatus");
    if (!el) { return; }
    el.textContent = text;
    el.classList.toggle("error", !!isError);
  }

  // ---- collection health rendering (begin) ----
  function setHealthSummary(text, warning) {
    var summary = document.getElementById("healthSummary");
    var panel = document.getElementById("healthPanel");
    if (summary) { summary.textContent = text; }
    if (panel) { panel.classList.toggle("warning", !!warning); }
  }
  function healthTime(value) {
    if (!value) { return "未確認"; }
    var date = new Date(value);
    return isNaN(date.getTime()) ? "未確認" : date.toLocaleTimeString("ja-JP", {hour12: false});
  }

  function renderCollectionHealth(health) {
    var el = document.getElementById("collectionHealth");
    if (!el) { return; }
    if (!health) {
      setHealthSummary("収集状態は未確認です", true);
      el.textContent = "収集状態は未確認です（サーバー再起動後に確認できます）";
      el.classList.toggle("warning", true);
      return;
    }
    var a = health.analysis || {};
    var c = health.collection || {};
    var stages = health.stages || {};
    var warning = !c.last_save_at || c.fetch_errors > 0 || c.save_errors > 0 || a.failed > 0;
    var summary = ["本日 " + (health.date || ""),
      "解析成功 " + (a.succeeded || 0) + "/" + (a.total == null ? "未確認" : a.total) + "R" +
      "（失敗 " + (a.failed == null ? "未確認" : a.failed) + "・対象外 " + (a.excluded == null ? "未確認" : a.excluded) + "）",
      "取得 " + healthTime(c.last_fetch_at), "有効オッズ " + healthTime(c.last_odds_at),
      "保存 " + healthTime(c.last_save_at),
      "現在の取得/保存エラー " + (c.fetch_errors || 0) + "/" + (c.save_errors || 0)];
    ["30", "10", "2"].forEach(function (stage) {
      var s = stages[stage] || {};
      warning = warning || s.missing > 0 || s.quality_failed > 0 || s.unknown > 0;
      summary.push(stage + "分前: 保存 " + (s.saved || 0) + "・待機 " + (s.waiting || 0) +
        "・品質不足 " + (s.quality_failed || 0) + "・未取得 " + (s.missing || 0) + "・未確認 " + (s.unknown || 0));
    });
    summary.push("監視巡回 " + healthTime((health.monitor || {}).last_iteration_at));
    el.textContent = summary.join("\\n");
    el.classList.toggle("warning", !!warning);
    var missed = ["30", "10", "2"].reduce(function (n, stage) { return n + ((stages[stage] || {}).missing || 0); }, 0);
    setHealthSummary("解析 " + (a.succeeded || 0) + "/" + (a.total == null ? "未確認" : a.total) +
      "R · 最終保存 " + healthTime(c.last_save_at) + " · 未取得 " + missed +
      " · 取得/保存エラー " + (c.fetch_errors || 0) + "/" + (c.save_errors || 0), warning);
  }
  // ---- collection health rendering (end) ----

  function pollGlobalStatus() {
    clearTimeout(globalPollTimer);
    if (globalPollInFlight) { return; }
    globalPollInFlight = true;
    var nextPollMs = 15000;
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 10000);
    fetch("/ev/api/state", {cache: "no-store", signal: controller.signal}).then(function (res) {
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      return res.json();
    }).then(function (st) {
      renderCollectionHealth(st.health);
      var btn = document.getElementById("globalStart");
      var analyzing = st.status === "analyzing";
      var text = "";
      var isError = false;
      if (analyzing) {
        var done = (st.progress && st.progress.done) || 0;
        var total = (st.progress && st.progress.total) || 0;
        text = "解析中 " + done + "/" + total;
      } else if (st.error) {
        text = st.error;
        isError = true;
      } else if (st.races && st.races.length) {
        text = "解析完了: " + st.races.length + "レース" +
               (st.started_at ? " (" + st.started_at + ")" : "");
        if (st.warning) { text += " ⚠ " + st.warning; }
      }
      // SPEC-T77: 解析完了を検知したらレース詳細タブへ「次に発走するレース」の
      // 自動選択を促す (一度だけ)。
      if (!analyzing && st.races && st.races.length > 0 && globalAwaitingRacePick) {
        globalAwaitingRacePick = false;
        var raceFrame = document.getElementById("frame-race");
        if (raceFrame) {
          if (!raceFrame.getAttribute("src")) {
            raceFrame.setAttribute("src", "/race/?autopick=1");
          } else {
            try {
              raceFrame.contentWindow.postMessage({ type: "jra-analysis-done" }, window.location.origin);
            } catch (e) { /* noop */ }
          }
        }
      }
      if (globalWin5Suffix) { text += globalWin5Suffix; }
      setGlobalStatus(text, isError);
      if (btn) { btn.disabled = analyzing; }
      if (analyzing) {
        nextPollMs = 3000;
      }
    }).catch(function () {
      setHealthSummary("接続を確認できません — 再接続中", true);
      setGlobalStatus("サーバーとの通信失敗・再接続待ち", true);
      var el = document.getElementById("collectionHealth");
      if (el) {
        el.textContent = "通信失敗のため収集状態を確認できません。15秒後に再確認します。";
        el.classList.toggle("warning", true);
      }
      var btn = document.getElementById("globalStart");
      if (btn) { btn.disabled = false; }
    }).finally(function () {
      clearTimeout(timeout);
      globalPollInFlight = false;
      globalPollTimer = setTimeout(pollGlobalStatus, nextPollMs);
    });
  }

  function startAll() {
    var btn = document.getElementById("globalStart");
    if (btn) { btn.disabled = true; }
    globalWin5Suffix = "";
    globalAwaitingRacePick = true; // SPEC-T77: 完了後にレース詳細タブへ自動選択を通知する
    setGlobalStatus("解析開始中...", false);

    // WIN5対象レース取得: 未ロードならautofetch付きでロード、ロード済みなら
    // postMessageで取得を依頼する (オッズ監視の解析開始と同時に実行)。
    var win5Frame = document.getElementById("frame-win5");
    if (win5Frame) {
      if (!win5Frame.getAttribute("src")) {
        win5Frame.setAttribute("src", "/win5/?autofetch=1");
      } else {
        try {
          win5Frame.contentWindow.postMessage({ type: "jra-win5-fetch" }, window.location.origin);
        } catch (e) { /* noop */ }
      }
    }

    fetch("/ev/api/analyze_start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (res) {
      if (res.status === 409) {
        setGlobalStatus("解析実行中です", false);
      }
      pollGlobalStatus();
    }).catch(function () {
      pollGlobalStatus();
    });
  }

  var globalStartBtn = document.getElementById("globalStart");
  if (globalStartBtn) {
    globalStartBtn.addEventListener("click", startAll);
  }

  // SPEC-T73 §2.1-3: オッズ監視セルのクリック (子iframeからのpostMessage) を
  // 受けてレース詳細タブへ切り替え、そのレースURLで自動解析させる。
  // SPEC-T74: WIN5取得完了の通知 (jra-win5-fetched) もここで type 分岐して扱う。
  var RACE_URL_PREFIX = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=";
  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin) { return; }
    var data = event.data;
    if (!data || typeof data.type !== "string") { return; }
    if (data.type === "jra-open-race") {
      var url = data.url;
      if (typeof url !== "string" || url.indexOf(RACE_URL_PREFIX) !== 0) { return; }
      var iframe = document.getElementById("frame-race");
      if (!iframe) { return; }
      var nextSrc = "/race/?url=" + encodeURIComponent(url) + "&auto=1";
      if (iframe.getAttribute("src") !== nextSrc) {
        iframe.setAttribute("src", nextSrc);
      }
      activateTab("race");
    } else if (data.type === "jra-win5-fetched") {
      var msg = data.message != null ? data.message : (data.ok ? "取得完了" : "取得失敗");
      globalWin5Suffix = " / WIN5: " + msg;
      pollGlobalStatus();
    }
  });

  activateTab(readInitialTab());
  pollGlobalStatus();
})();
</script>
</body>
</html>
"""


def _portal_tabs():
    return [{"prefix": s["prefix"], "title": s["title"]} for s in _SECTIONS]


def _portal_loop_badges():
    out = []
    for s in _SECTIONS:
        if not s["loop"]:
            continue
        alive = _loop_alive(_LOOP_THREAD_NAMES[s["loop"]])
        out.append({"loop": s["loop"], "label": _LOOP_BADGE_LABELS[s["loop"]], "alive": alive})
    return out


# ─── アプリ生成 ──────────────────────────────────────────────────────────────

def create_app():
    """3アプリをBlueprintとしてマウントした統合Flaskアプリを返す (SPEC-T38 §3.1)。"""
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(jra_ev.bp, url_prefix="/ev")
    app.register_blueprint(jra_win5.bp, url_prefix="/win5")
    app.register_blueprint(jra_perf.bp, url_prefix="/perf")
    app.register_blueprint(jra_graded.bp, url_prefix="/graded")

    for prefix in ("ev", "win5", "perf", "graded"):
        app.add_url_rule(f"/{prefix}/<path:filename>",
                         endpoint=f"{prefix}_static",
                         view_func=_serve_prefixed_static)

    # /race/ : api/index.py の Flask アプリ (本番Webと同じ画面) を丸ごとマウント
    # (SPEC-T73 §2.1)。jra_ev/jra_win5/jra_perf の Blueprint 化とは異なり、
    # index.py 自体は変更禁止のため DispatcherMiddleware でサブアプリとして
    # 接続する。/race (末尾スラッシュ無し) は werkzeug の既定動作で index.app の
    # "/" ルールへの 308 リダイレクトになる (SPEC §2.3-1 実測確認済み)。
    _install_race_static_guard()
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/race": index.app.wsgi_app})

    @app.route("/")
    def portal():
        return render_template_string(_PORTAL_TEMPLATE, port=PORT,
                                      tabs=_portal_tabs(),
                                      loop_badges=_portal_loop_badges())

    return app


def delegate_auto_start_to_running_server(port, timeout=5.0):
    """既に稼働中の統合サーバーへ解析開始を依頼する (SPEC-T78)。

    戻り値: (成功したか, メッセージ)。
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/ev/api/analyze_start",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True, "既に稼働中の統合サーバーに解析開始を依頼しました"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return True, "既に解析実行中です"
        return False, str(e)
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    # 週末タスクスケジューラ (--auto-start) で、既に手動起動プロセスがポートを
    # 占有している場合は新規起動をあきらめ、稼働中プロセスへ解析開始を依頼する
    # (SPEC-T78: ポートガードでexit 1すると開催日の解析が誰も始めない事故になる)。
    if "--auto-start" in sys.argv and is_port_in_use(PORT):
        ok, msg = delegate_auto_start_to_running_server(PORT)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [auto-start] {msg}")
        if ok:
            sys.exit(0)
        # 依頼できなければ従来どおりポートガードの案内で終了 (別プロセスが5005を占有)

    ensure_port_free(PORT, "統合サーバー (jra_suite)")
    app = create_app()
    start_background_loops()

    # 週末タスクスケジューラ用 (旧 jra_ev.py --auto-start と同じ挙動):
    # 起動6秒後にオッズ監視の解析を自動開始する。
    if "--auto-start" in sys.argv:
        threading.Timer(6.0, jra_ev._auto_start).start()

    url = f"http://localhost:{PORT}/"
    print("=" * 55)
    print("  JRA予想スイート (統合版)")
    print("=" * 55)
    print(f"  URL: {url}")
    print(f"    オッズ監視           : {url}ev/")
    print(f"    WIN5予想             : {url}win5/")
    print(f"    実績ダッシュボード   : {url}perf/")
    print("  ブラウザのタブを開いたままにすると 15分前/5分前に通知します (オッズ監視)")
    _th = jra_ev.STATE["params"]["ev_threshold"]
    print(f"  EV閾値: {_th}" + (" (暫定・T63裁定まで)" if _th < 1.3 else ""))
    if os.environ.get("EV_DISCORD_WEBHOOK", "").strip():
        print("  Discord通知: 有効")
    if os.environ.get("EV_LINE_CHANNEL_TOKEN", "").strip():
        print(f"  LINE通知: 有効 ({os.environ.get('EV_LINE_STAGE', '5')}分前 × "
              f"EV>={jra_ev._effective_line_min_ev()})")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
