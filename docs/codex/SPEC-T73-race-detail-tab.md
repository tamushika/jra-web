# SPEC-T73: オッズ監視 → レース詳細 (jra-web 解析画面) への遷移

起票: 2026-09-07 (Fable 5)。ユーザー要望: 「レースの個別予想を行いたいとき、オッズ監視の画面から各レースの情報に遷移したい。1レースを選択→画面が遷移し、そのレース全体の情報 (少なくとも jra-web の情報) が見える」。

## 1. 設計

統合版 `jra_suite.py` (port 5005) に4つ目のタブ **「レース詳細」 (prefix `race`)** を追加する。中身は本番Web (jra-web.vercel.app) と同じ `index.html` + `script.js` + `style.css` を、`api/index.py` の Flask アプリ (解析API `/api/scrape` `/api/track_bias` `/api/past_data` `/api/ai_predict` `/api/latest_url`) ごと `/race/` 配下にマウントしたもの。オッズ監視のセル (レース) をクリックすると、タブシェルが「レース詳細」タブに切り替わり、そのレースURLで解析が自動実行される。

- 本番Web (Vercel、`/` 直下) と統合版 (`/race/` 配下) の両方で同じ `index.html`/`script.js` が動くこと (相対パス化)。
- jra_ev / jra_win5 / jra_perf のロジック・通知関数 (`_send_line`/`_send_discord`/`refresh_and_alert`、T62b凍結) は変更しない。

## 2. 変更仕様

### 2.1 `jra_suite.py`: `/race/` マウントと4つ目のタブ
1. `_SECTIONS` に `{"prefix": "race", "title": "レース詳細", "desc": "個別レース解析 (本番Webと同じ画面)", "loop": None}` を追加 (末尾)。
2. `api/index.py` の `app` (Flask) を `werkzeug.middleware.dispatcher.DispatcherMiddleware` で `/race` にマウントする:
   `app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/race": index_app.wsgi_app})`。
   `index_app` は `from api.index import app as index_app` ではなく、jra_win5 と同じ import 経路 (`sys.path` に `api/` を追加済みなら `import index` → `index.app`) を使う。二重 import で別モジュールオブジェクトにならないよう、jra_win5 が既に import した `index` を再利用すること。
   `index.app` は `static_folder='../'` (リポジトリ直下) を配信するため、`/race/index.html` `/race/script.js` `/race/style.css` はそのまま解決される。`_is_denied_static_path` と同等の拒否 (.env / *.db / *.log / backups / .git) を `/race/` にも効かせるため、`index.app` の `before_request` で `request.path` を検査して該当なら 404 にするフック関数を `jra_suite.py` 側から登録する (index.py 自体は変更しない)。
3. ポータル (タブシェル) の JS に **親→iframe のレース起動** を追加:
   - `window.addEventListener("message", ...)` で `{type: "jra-open-race", url: <JRA accessD URL>}` を受け取ったら、`frame-race` の `src` を `/race/?url=<encodeURIComponent(url)>&auto=1` に設定 (既に同じURLならそのまま) し、`activateTab("race")` を呼ぶ。
   - 受け付けるのは同一オリジン (`event.origin === window.location.origin`) かつ `url` が `https://www.jra.go.jp/JRADB/accessD.html?CNAME=` で始まる文字列のみ。
4. `/race/` タブは遅延ロード (既存どおり初回アクティブ化時に `data-src="/race/"`)。

### 2.2 `index_ev.html`: セルからの遷移
1. `cellHtml(r)` のヘッダー (`.ev-cell-head`、発走時刻の部分) をクリック可能にする (`cursor:pointer`、`title="レース詳細を開く"`)。表示は `会場 NR 発走時刻` が分かるようにする (現状は時刻のみ。同じ行に `${r.venue}${r.race_num}R` を小さく付ける)。
2. クリック時 `openRace(r)`:
   - `window.parent !== window` (タブシェル内) なら `window.parent.postMessage({type: "jra-open-race", url: r.url}, window.location.origin)`。
   - そうでなければ (スタンドアロン `/ev/` 表示) `window.open("../race/?url=" + encodeURIComponent(r.url) + "&auto=1", "_blank")`。
3. そのために `jra_ev._slim_state()` の races 要素に `"url"` を追加する (キー追加のみ。他のキー・順序・通知は不変)。

### 2.3 `script.js` / `index.html`: `/race/` 配下でも動くようにする + 自動実行
1. `fetch('/api/...')` を相対パス `'api/...'` に変更する (5箇所: `latest_url`, `scrape`, `ai_predict`, `_buildPastDataUrl` の `/api/past_data`, `track_bias`)。本番Web (`/`) では `api/...` は `/api/...` に解決され、統合版 (`/race/`) では `/race/api/...` に解決される。**末尾スラッシュ無しの `/race` は `/race/` にリダイレクトされること** (DispatcherMiddleware + Flask の既定で `/race` → index app の `/` になるはず。ならない場合は suite 側に `/race` → `/race/` の 308 リダイレクトを追加)。
2. `DOMContentLoaded` で `URLSearchParams(location.search)` の `url` を読み、あれば `#urlInput` に設定。`auto=1` なら `startScraping()` を自動実行 (mode は `#modeSelect` の既定値=簡易)。
3. 同一ページで別レースを開き直せるよう、`window.addEventListener("message")` は不要 (親が iframe の src を差し替える)。ただし `src` 差し替えでページが再ロードされるため `apiCache` は失われてよい。
4. `index.html` の `<title>` はそのまま。`/race/` 配下で `style.css` `script.js` の相対参照が効くことを確認。

### 2.4 レース詳細ページに「オッズ監視の評価」を併記 (統合版のみ)
1. `script.js` `applyScrapeData()` の末尾で `fetch('../ev/api/state')` を試み (失敗・404 は無視。本番Webでは存在しないので静かにスキップ)、`venue` と `race_num` が一致するレースを探す。
2. 見つかれば、`#raceInfo` の直下に小さなテーブル `#evSummary` (無ければ動的に作成) を表示: 馬番・馬名・単勝・CL勝率 (`win_prob`)・EV・EV対象 (`picked` に ★)・複勝率β (`place_prob` があれば)。CL勝率降順。`ml_coverage.ok === false` のときは「MLスコア付き馬が不足のためEV対象外 (k/n頭)」と1行表示。
3. 見つからない (未解析) 場合は何も表示しない。

## 3. テスト
- `tests/test_t73_race_tab.py`:
  - suite の Flask test client で `/race/` が 200 で `index.html` の内容 (`id="urlInput"`) を返す、`/race/script.js` が 200、`/race/.env` `/race/ability.db` `/race/backups/x` が 404。
  - `/race/api/scrape` に `analyze_race_url` を monkeypatch した POST で JSON が返る (index app のルートが `/race` 配下で動く)。
  - `/` (ポータル) に `data-tab="race"` のタブと `frame-race` の iframe が含まれる。
  - `jra_ev._slim_state()` の各 race に `url` キーがある (既存キーは不変)。
- `tests/test_t62b_shadow.py` `tests/test_t70_virtual_betting.py` `tests/test_t71_interim_threshold.py` `tests/test_jra_suite.py` が通ること (通知関数・凍結ハッシュ不変)。

## 4. 受け入れ基準 (手動)
1. `jra_suite.py` 起動 → オッズ監視で「解析開始」→ 任意のセルのヘッダーをクリック → 「レース詳細」タブに切り替わり、そのレースの解析結果 (jra-web と同じ画面) が自動表示される。
2. 同ページ上部に「オッズ監視の評価」 (CL勝率/EV) が出る。
3. 別のセルをクリックすると別レースに切り替わる。オッズ監視タブに戻っても監視表示は保持されている。
4. 本番Web (jra-web.vercel.app) の解析が従来どおり動く (相対パス化の回帰なし)。
