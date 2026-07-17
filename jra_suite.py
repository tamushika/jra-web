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
import webbrowser

from flask import Flask, abort, render_template_string, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

from api.port_guard import ensure_port_free  # noqa: E402

import jra_ev  # noqa: E402
import jra_win5  # noqa: E402
import jra_perf  # noqa: E402

PORT = int(os.environ.get("JRA_SUITE_PORT", "5005"))

_SECTIONS = (
    {"prefix": "ev", "title": "オッズ監視", "desc": "期待値レース監視 (旧 jra_ev.py, port 5003)",
     "loop": "ev-scheduler-loop"},
    {"prefix": "win5", "title": "WIN5予想", "desc": "スコアリング + 荒れ度配分 (旧 jra_win5.py, port 5002)",
     "loop": "win5-watch-loop"},
    {"prefix": "perf", "title": "実績ダッシュボード", "desc": "予測実績の集計表示 (旧 jra_perf.py, port 5004)",
     "loop": None},
)

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
    if not parts or ".." in parts:
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


# ─── ポータル ────────────────────────────────────────────────────────────────
_PORTAL_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>JRA予想スイート</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "Hiragino Sans", sans-serif;
         max-width: 720px; margin: 40px auto; padding: 0 16px; color: #222; }
  h1 { font-size: 1.4em; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin: 12px 0; }
  .card a { font-size: 1.15em; font-weight: bold; text-decoration: none; color: #0a5; }
  .card p { margin: 6px 0 0; color: #555; }
  .loop { display: inline-block; margin-top: 8px; font-size: 0.85em; padding: 2px 8px;
          border-radius: 10px; }
  .loop.alive { background: #e6f7ea; color: #1a7a34; }
  .loop.dead { background: #f7e6e6; color: #a31a1a; }
  footer { margin-top: 24px; font-size: 0.85em; color: #888; }
</style>
</head>
<body>
<h1>JRA予想スイート (統合版, port {{ port }})</h1>
<p>3アプリを単一プロセスで動かしています。旧ポート (5002/5003/5004) は使用しません。</p>
{% for s in sections %}
<div class="card">
  <a href="/{{ s.prefix }}/">{{ s.title }}</a>
  <p>{{ s.desc }}</p>
  {% if s.loop %}
  <span class="loop {{ 'alive' if s.alive else 'dead' }}">
    バックグラウンドループ: {{ '稼働中' if s.alive else '未起動 (該当機能を使うと自動起動)' }}
  </span>
  {% endif %}
</div>
{% endfor %}
<footer>SPEC-T38 (jra-web/docs/codex/SPEC-T38-app-consolidation.md)</footer>
</body>
</html>
"""


def _portal_sections():
    out = []
    for s in _SECTIONS:
        alive = _loop_alive(_LOOP_THREAD_NAMES[s["loop"]]) if s["loop"] else None
        out.append({**s, "alive": alive})
    return out


# ─── アプリ生成 ──────────────────────────────────────────────────────────────

def create_app():
    """3アプリをBlueprintとしてマウントした統合Flaskアプリを返す (SPEC-T38 §3.1)。"""
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(jra_ev.bp, url_prefix="/ev")
    app.register_blueprint(jra_win5.bp, url_prefix="/win5")
    app.register_blueprint(jra_perf.bp, url_prefix="/perf")

    for prefix in ("ev", "win5", "perf"):
        app.add_url_rule(f"/{prefix}/<path:filename>",
                         endpoint=f"{prefix}_static",
                         view_func=_serve_prefixed_static)

    @app.route("/")
    def portal():
        return render_template_string(_PORTAL_TEMPLATE, port=PORT,
                                      sections=_portal_sections())

    return app


if __name__ == "__main__":
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
    if os.environ.get("EV_DISCORD_WEBHOOK", "").strip():
        print("  Discord通知: 有効")
    if os.environ.get("EV_LINE_CHANNEL_TOKEN", "").strip():
        print(f"  LINE通知: 有効 ({os.environ.get('EV_LINE_STAGE', '5')}分前 × "
              f"EV>={os.environ.get('EV_LINE_MIN_EV', '1.3')})")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
