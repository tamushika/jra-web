# SPEC-T77: 「🏇 解析開始」完了時にレース詳細タブへ次のレースを自動で読み込む

起票: 2026-09-11 (Fable 5.1)。ユーザー要望: 「解析開始を押した際に、(レース詳細の) URL を取得できませんか?」。
現状、統合版 (jra_suite.py, port 5005) の「レース詳細」タブは `?url=` 無しで開くと「オッズ監視でレースを選択してください」+ レース一覧 (T73b) を出すだけで、ユーザーが 1 件クリックするまで何も解析されない。埋め込みモードでは「最新URL取得」も無効化されている (T73b §2.3-2)。

## 1. 設計
- タブシェルの「🏇 解析開始」(T74 `startAll`) で始めた解析が **完了した時点** で、レース詳細タブに「次に発走するレース」を自動で読み込む。
- 「次に発走するレース」= オッズ監視の状態 `races` (start_time "HH:MM" 昇順・venue 順にソート済み) のうち `url` を持つもので、`start_time >= 現在時刻 (HH:MM)` の最初の 1 件。該当なし (全レース発走済み) なら **先頭のレース**。
- 自動読み込みは **レース詳細タブが一覧状態 (URL 未指定) のときだけ**。既にユーザーがレースを開いて見ている場合は上書きしない。
- タブの自動切替はしない (ユーザーが見ているタブはそのまま)。
- 本番 Web (Vercel, `/race/` 以外) には影響しない (埋め込みモード限定)。

## 2. 変更仕様

### 2.1 `jra_suite.py` タブシェル JS
1. `startAll()` で `globalAwaitingRacePick = true` を立てる。
2. `pollGlobalStatus()` で、`analyzing` でなく `st.races.length > 0` かつ `globalAwaitingRacePick` のとき、フラグを下ろしてレース詳細 iframe (`#frame-race`) に通知する:
   - iframe に `src` が無い (未ロード) → `src` を `/race/?autopick=1` にする。
   - ロード済み → `iframe.contentWindow.postMessage({type: "jra-analysis-done"}, window.location.origin)`。
3. 既存の `jra-open-race` / `jra-win5-fetched` の処理は不変。

### 2.2 `script.js` (埋め込みモード)
1. 純関数 `pickNextRace(races, nowHHMM)` を追加し、`// ---- T77 race pick pure functions (begin) ----` / `(end)` マーカーで囲む (区間内は `window`/`document` 非参照)。
   - `races` から `url` があるものだけを対象、`start_time` (文字列 "HH:MM") 昇順で安定ソート。
   - `start_time >= nowHHMM` の最初の要素を返す。無ければ先頭要素。対象が空なら `null`。
   - `window.JRA_RACE_PICK = { pickNextRace }` にも束ねる。
2. `autoPickRaceFromEvState()`: `../ev/api/state` を取得し `pickNextRace(state.races, 現在のHH:MM)` で 1 件選び、`location.href = '?url=' + encodeURIComponent(r.url) + '&auto=1'` で遷移する (T73b のリンクと同じ遷移)。選べなければ `renderEmbeddedRaceList()` を呼ぶだけ。遷移前に `#raceInfo` に「次のレースを自動選択中...」を表示。
3. `DOMContentLoaded` の埋め込み分岐: `?url` 無し かつ `?autopick=1` なら `renderEmbeddedRaceList()` の代わりに `autoPickRaceFromEvState()`。
4. `window.addEventListener('message', ...)`: 同一オリジンからの `{type:'jra-analysis-done'}` を受けたら、**`queryRaceUrl` が無い (一覧状態) 場合のみ** `autoPickRaceFromEvState()`。レース表示中なら無視。
5. 既存の `?url=...&auto=1` の挙動・T73b の一覧描画は不変。

## 3. テスト (`tests/test_t77_autopick_race.py`)
1. Node でマーカー区間を評価: `pickNextRace` に `[{start_time:"09:45",url:"a"},{start_time:"10:10",url:"b"},{start_time:"10:40",url:"c"}]`:
   - now "10:00" → b、now "10:10" → b (同時刻は対象)、now "17:00" → a (先頭)、now "09:00" → a。
   - `url` 無しの要素は無視される。空配列 → null。
2. 文字列検査: `jra_suite.py` に `autopick=1` と `jra-analysis-done` が含まれる。`script.js` に `autopick` と `jra-analysis-done` の処理が含まれる。
3. 既存 `tests/test_t73_race_tab.py` `tests/test_t73b_autofill.py` `tests/test_t74_global_start.py` `tests/test_t76_wind_overlay.py` が通る。

## 4. 受け入れ (手動・ユーザー)
1. 統合版で「🏇 解析開始」を押し、完了後にレース詳細タブを開くと、次に発走するレースが解析済みで表示されている (レース一覧ではない)。
2. 既にレース詳細で別のレースを見ている状態で「解析開始」を押しても、そのレースは上書きされない。
3. 開催のない日は従来どおりエラー表示のみでレース詳細は変わらない。
