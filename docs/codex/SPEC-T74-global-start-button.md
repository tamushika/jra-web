# SPEC-T74: 解析開始をタブシェル左上の専用ボタンに集約し、WIN5 対象レース取得も同時実行する

起票: 2026-09-08 (Fable 5)。ユーザー要望: 「解析開始はオッズ監視に置くのではなく別出しにして、左上に特別なアイコンを載せたい。押すと WIN5 のレース取得も実施したい」。あわせて「非開催日に解析開始を押すとエラーになった」→ 状態をボタン横に分かりやすく表示する。

## 1. 設計
- 統合版 `jra_suite.py` のタブバー (`_PORTAL_TEMPLATE`) の**左端** (タブより前) に専用ボタン `#globalStart` (🏇 アイコン + 「解析開始」) と状態表示 `#globalStatus` を置く。
- クリックで (1) オッズ監視の解析開始 `POST /ev/api/analyze_start` (body `{}` = サーバー側の現在の閾値をそのまま使う) と (2) WIN5 の対象レース取得を同時に開始する。レース詳細は T73b により監視の解析結果から自動生成されるので追加処理なし。
- オッズ監視ページ (`index_ev.html`) の「解析開始 (本日の全レース)」ボタンは、タブシェル内 (iframe) では非表示にする (スタンドアロン `/ev/` では従来どおり表示)。EV閾値の選択・通知許可・進捗表示・警告バナーは従来どおり (状態は `api/state` のポーリングで反映される)。
- 通知関数・監視ロジック・WIN5 のスコア計算/買い目/予約は変更しない。

## 2. 変更仕様

### 2.1 `jra_suite.py` タブシェル
1. マークアップ: `#tabbar` の先頭に
   ```html
   <button id="globalStart" class="global-start" title="本日の全レースを解析し、WIN5対象レースも取得します">🏇 解析開始</button>
   <span id="globalStatus" class="global-status"></span>
   ```
   CSS: ボタンは目立つ配色 (例: 背景 #f5c842・文字 #222・角丸・太字・高さ 32px)、`disabled` 時は不透明度 0.6。`#globalStatus` は 0.85em・`#ddd`、エラー時は `.error` クラスで `#ffb347`。
2. JS (`(function(){...})()` 内に追加):
   - `startAll()`:
     a. ボタンを disabled にし、status に「解析開始中…」。
     b. `fetch('/ev/api/analyze_start', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})`。409 (解析実行中) は「解析実行中です」と表示して継続。
     c. WIN5: `frame-win5` の iframe が未ロード (`src` 未設定) なら `src` を `/win5/?autofetch=1` に設定 (WIN5 ページ側が起動時に取得を実行)。ロード済みなら `iframe.contentWindow.postMessage({type:'jra-win5-fetch'}, window.location.origin)`。
     d. EV 状態のポーリング開始 (`pollGlobalStatus()`、3秒間隔)。
   - `pollGlobalStatus()`: `GET /ev/api/state` を読み、`status`/`progress`/`error`/`warning`/`races.length` から status 文言を作る:
     - `analyzing`: `解析中 {progress}` (progress は既存の文字列をそのまま)。
     - `done`/`idle` でレースあり: `解析完了: {N}レース (HH:MM:SS)`。`warning` があれば末尾に ` ⚠ {warning}`。
     - `error` あり: `.error` クラスで `{error}` (例: 「本日の出馬表が見つかりません (開催日にお試しください)」)。
     - 解析中でなくなったらポーリングを止め、ボタンを有効に戻す。
   - WIN5 側の結果は `frame-win5` からの `postMessage({type:'jra-win5-fetched', ok, message})` を受けて status 末尾に ` / WIN5: {message}` を追記する (origin チェック必須)。
   - ページ読み込み時にも `pollGlobalStatus()` を1回呼び、既に解析済み/解析中なら状態を表示する (解析中なら継続ポーリング)。
3. 既存の `activateTab`/`message` ハンドラ (T73 `jra-open-race`) は不変。`message` ハンドラは `type` で分岐させる。

### 2.2 `index_ev.html`
1. `window.parent !== window` (タブシェル内) のとき、`#startBtn` を `display:none` にする (削除しない)。それ以外は不変。
2. `startAnalyze()` 自体は残す (スタンドアロン用)。

### 2.3 `index_win5.html`
1. ページ初期化時に `URLSearchParams(location.search).get('autofetch') === '1'` なら `loadRaces()` を自動実行。
2. `window.addEventListener('message', ...)` で同一オリジンからの `{type:'jra-win5-fetch'}` を受けたら `loadRaces()` を実行 (取得中 = `#loadBtn.disabled` なら無視)。
3. `loadRaces()` の完了時 (成功/失敗とも) に、`window.parent !== window` なら `window.parent.postMessage({type:'jra-win5-fetched', ok: <success>, message: <対象日 or エラー文言>}, window.location.origin)` を送る。
4. 「対象レースを取得」ボタンは残す (WIN5 単体での再取得用)。

## 3. テスト (`tests/test_t74_global_start.py`)
- ポータル `/` の HTML に `id="globalStart"`・`id="globalStatus"` が含まれ、`data-tab="ev"` より前に出現する。
- `POST /ev/api/analyze_start` に body `{}` を送っても 200 で `STATE["params"]["ev_threshold"]` が変わらない (`worker_analyze_all` は monkeypatch)。
- `index_ev.html` に `startBtn` の埋め込み時非表示ロジックが含まれる (文字列検査でよい)。`index_win5.html` に `autofetch` と `jra-win5-fetch` の処理が含まれる。
- 既存: `tests/test_jra_suite.py` `tests/test_t73_race_tab.py` `tests/test_t73b_autofill.py` `tests/test_win5_start_time_fill.py` `tests/test_t62b_shadow.py` `tests/test_t70_virtual_betting.py` `tests/test_t71_interim_threshold.py` が通ること。

## 4. 受け入れ (手動・ユーザー)
1. 統合版を開くと左上に「🏇 解析開始」がある。押すと状態が「解析開始中…」→「解析中 …」→「解析完了: Nレース」と変わり、オッズ監視タブに結果、WIN5 タブに対象レースが入っている。
2. 非開催日 (火曜など) は「本日の出馬表が見つかりません (開催日にお試しください)」がボタン横に出て、ボタンが再度押せる状態に戻る。
3. オッズ監視タブ内には解析開始ボタンが無い (スタンドアロン /ev/ では従来どおりある)。
