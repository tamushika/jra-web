# SPEC-T73b: オッズ監視の解析で全レースのレース詳細を自動生成する (T73 追補)

起票: 2026-09-07 (Fable 5)。ユーザー要望: 「オッズ監視の解析開始を押すと、自動でレース詳細も全レース解析を終える仕組みにしたい。レース詳細の解析開始ボタン・最新URL取得・簡易分析 (モード選択) は削除でよい」。

## 1. 設計
- オッズ監視 (`jra_ev.analyze_one`) は既に全レースを `analyze_race_url(url, "簡易")` で解析している (初回 + 30/15/10/5/2分前の再解析)。この `result` を **プロセス内キャッシュ** に保存し、レース詳細タブの `/race/api/scrape` はキャッシュがあればそれを即返す。追加のネットワーク負荷なし。
- 「詳細分析」モード (過去走ごとに結果ページを取得する重い処理) は統合版では廃止し、監視と同じ簡易に統一する。本番Web (Vercel、`/` 直下) の画面・挙動は変えない (埋め込み判定で分岐)。
- `api/index.py` は変更しない (Vercel と共用)。フックは `jra_suite.py` 側から登録する。

## 2. 変更仕様

### 2.1 `jra_ev.py`: 解析結果キャッシュ
1. モジュールレベルに `RACE_ANALYSIS_CACHE = {}` と `_RACE_ANALYSIS_CACHE_LOCK = threading.Lock()` を追加。値は `{url: {"result": <analyze_race_url の返却dict>, "cached_at": "HH:MM:SS", "stage": stage or None, "race_date": ...}}`。
2. `analyze_one()` で `result = analyze_race_url(url, "簡易")` の直後 (障害除外で `return None` する前でもよいが、`analysis_excluded` の結果は保存しない) に `RACE_ANALYSIS_CACHE[url] = {...}` を保存する。**通知関数 (`_send_line`/`_send_discord`/`refresh_and_alert`) には触れない**。
3. 解析開始 (STATE["races"] をリセットして全レースを解析し直す箇所) でキャッシュもクリアする。どこでリセットしているかは `STATE["races"] = {}` / `.clear()` を grep して特定する。
4. 公開ヘルパー `get_cached_analysis(url)` → dict or None。

### 2.2 `jra_suite.py`: `/race/api/scrape` のキャッシュ短絡
1. 既存の `_reject_denied_race_static_paths` (index.app の before_request) と同じ仕組みで、`request.path == "/api/scrape"` かつ POST のとき JSON body の `url` で `jra_ev.get_cached_analysis(url)` を引き、ヒットしたら `flask.jsonify({**result, "cached_from_monitor": cached_at, "monitor_stage": stage})` を返して短絡する (before_request が Response を返せば view は呼ばれない)。
2. body の `mode` が `"詳細"` のとき、または `force` が真のときは短絡せず素通し (index.app の scrape が実行される。ただし埋め込みUIからは送らない)。
3. 短絡した応答では予想ログ保存 (`log_race_prediction`) は行われない (監視側で既にログ済みのため二重記録を避ける)。素通し時は従来どおり。
4. before_request の登録は既存の1回限りガード (`_RACE_GUARD_ATTR`) に統合する。

### 2.3 `script.js` / `index.html`: 埋め込みモード
1. 埋め込み判定 `IS_EMBEDDED = window.location.pathname.startsWith('/race/')` を `script.js` 冒頭で定義。本番Web (`/`) では false。
2. `IS_EMBEDDED` のとき `DOMContentLoaded` で:
   - URL入力行 (`#urlInput`・`#modeSelect`・`#searchBtn`・`#getUrlBtn` を含む最初の `.input-group`) を `display:none` にする。**削除ではなく非表示** (本番Webでは従来どおり表示。既存のイベント登録はそのまま動く)。
   - 起動時の最新URL自動取得 (`autoFetchUrl(false)`) は呼ばない。
   - `?url=` があれば `#urlInput` に設定し、`mode = '簡易'` 固定で `startScraping()` を自動実行 (`auto=1` の有無に関わらず)。
   - `?url=` が無ければ `#raceInfo` に「オッズ監視でレースを選択してください」と表示し、`../ev/api/state` を取得して当日のレース一覧 (会場ごとに `NR 発走時刻 レース名(race_info)`、発走時刻順) をリンクとして `#raceInfo` の直下に描画する。クリックで `location.href = '?url=' + encodeURIComponent(url) + '&auto=1'`。状態が空 (未解析) なら「オッズ監視で「解析開始」を押すと全レースが解析され、ここに一覧が出ます」と表示。
3. `startScraping()` は `IS_EMBEDDED` のとき `mode` を `'簡易'` に固定する (`#modeSelect` の値を読まない)。
4. `applyScrapeData()` で `data.cached_from_monitor` があれば `#raceInfo` の末尾に小さく「(オッズ監視 HH:MM:SS 取得・N分前ステージ)」を付ける。stage が null なら「初回解析」。
5. 本番Web (非埋め込み) の DOM・挙動は変えない。

### 2.4 タブシェル (`jra_suite.py` ポータルJS)
- 変更不要 (T73 の postMessage → `/race/?url=…&auto=1` のまま)。

## 3. テスト (`tests/test_t73b_autofill.py`)
- `analyze_one` を `analyze_race_url` monkeypatch で実行 → `RACE_ANALYSIS_CACHE[url]` に result が入り、`cached_at` が付く。`analysis_excluded` の結果は入らない。
- 解析開始のリセットでキャッシュが空になる (該当関数を monkeypatch/直接呼び出しで確認。ネットワーク不要な形で)。
- suite test client: キャッシュ投入後の `POST /race/api/scrape {url}` が `cached_from_monitor` 付きで即返る (index の scrape が呼ばれないことを monkeypatch で確認)。`mode: "詳細"` と未キャッシュURLは素通し (index の scrape が呼ばれる)。
- T62b/T70/T71/jra_suite の既存テストが通ること。

## 4. 受け入れ (手動・ユーザー)
1. オッズ監視で「解析開始」→ 完了後にセルをクリック → レース詳細が待ち時間なく表示され、見出しに「(オッズ監視 HH:MM:SS 取得…)」が付く。
2. レース詳細タブを直接開くとレース一覧が出て、クリックで各レースに遷移できる。
3. 統合版のレース詳細に URL入力・モード選択・解析開始・最新URL取得が表示されない。本番Webでは従来どおり表示される。
