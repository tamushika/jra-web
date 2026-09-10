# SPEC-T76: コースレイアウト図に風向・風速のエフェクト (矢印オーバーレイ) を描く

起票: 2026-09-11 (Fable 5.1)。ユーザー要望: 「現在ローカルのアプリで風の影響も表示しているが、それをエフェクトで分かりやすく見える化したい。競馬場マップがあり、風の向きや強弱がわかるような矢印の図形が表示されるイメージ」。

対象は jra-web 解析画面 (`index.html` / `script.js` / `style.css`) の「コース」タブ。統合版の「レース詳細」タブ (`/race/`) も同じファイルなので自動的に反映される。本番 Web (Vercel) も同じ静的ファイルだが、外部 API (Open-Meteo, jra.go.jp 画像) は既に使っているので新規の依存は増えない。

## 0. 現状と前提 (調査済み・変更不可の事実)

- 風データ: `fetchWindData(venue)` (script.js) が Open-Meteo `current_weather` を取り、`#windDataDisplay` にテキストで「北西からの風 (326°), 微風 (3.9m/s)。向こう正面は…、直線は…」を出す。`COURSE_DIRECTION[venue].dir` は**ホームストレッチ (最後の直線) で馬が向かう方位** (東京 290=西北西、中山 140=南東 など)。`winddirection` は気象の慣例どおり**風が吹いてくる方位**。`checkWindEffectHtml` は両者の差が ≤45° で「直線は向かい風」、≥135° で「追い風」、それ以外「横風」。
- **バグ (本タスクで修正)**: Open-Meteo の `current_weather.windspeed` は既定 **km/h** (`current_weather_units.windspeed: "km/h"` を 2026-09-11 に実測)。現行コードは m/s と表記し、閾値 (10/15/20/30) も気象庁の m/s 階級なので、**表示値が約 3.6 倍過大**。リクエストに `&windspeed_unit=ms` を付ければ m/s で返る (実測済み)。
- コース図: `#courseLayoutImage` に JRA 公式の平面図 (`pic_course_heimenzu.gif` 等、`api/jra_images.json` 由来) を表示。画像は **スタンドが下・ホームストレッチが画像下部に水平** で統一されている (東京・中山の画像で確認)。ゴール位置: **左回り (東京・中京・新潟) は右端、右回り (中山・阪神・京都・函館・札幌・福島・小倉) は左端**。つまり画面上のホームストレッチの進行方向は左回り=右向き (+x)、右回り=左向き (−x)。
- 画像は cross-origin (jra.go.jp) なので、ピクセルを読む処理は不可。**画像の上に SVG を重ねる**だけにする。
- 画像が無い/読めないコース (`onerror` → `display:none`) もある。

## 1. 設計

### 1.1 角度の対応 (純関数・テスト対象)

画面角 θ は SVG 座標 (y 下向き) で **+x 方向から時計回り** に測る。方位 B (北=0、時計回り) との対応は、ホームストレッチの画面上の向き θ_s と実方位 `dir` を一致させる回転で決まる:

```
θ(B) = θ_s + (B − dir)        (mod 360)
θ_s  = 0   (左回り: 東京・中京・新潟)
θ_s  = 180 (右回り: それ以外の 7 場)
```

上から見た図なので鏡像反転は無い (方位も画面角もどちらも時計回り)。導出値:

- 風の**進行方向** (矢印の向き): `B_to = winddirection + 180` → `θ_to = θ(B_to)`
- 方位記号 N の向き: `θ_N = θ(0) = θ_s − dir`
- 直線/向こう正面の判定: 既存 `checkWindEffectHtml` と同じ差分ロジック (≤45 向かい風 / ≥135 追い風 / 横風) を **文字列を返さない純関数** に切り出して両者で共用する。

検算 (SPEC 執筆時): 東京 (θ_s=0, dir=290)、風向 290 (西北西から) → `θ_to = 0 + (470 − 290) = 180` → 矢印は左向き。馬は右向きに走るので向かい風。既存ロジックの「差 0 → 直線は向かい風」と一致。

`script.js` に以下の純関数を追加する (グローバル関数。`window.JRA_WIND = {...}` にも束ねて Node から評価できるようにする — §3 参照):

```js
const COURSE_HANDEDNESS = { "東京":"left", "中京":"left", "新潟":"left",
  "中山":"right", "阪神":"right", "京都":"right", "函館":"right", "札幌":"right", "福島":"right", "小倉":"right" };

function classifyWindVsCourse(windFromDeg, courseDir)  // → {diff, straight:'head'|'tail'|'cross', backstretch:'tail'|'head'|'cross'}
function windSpeedCategory(speedMs)                     // → {key:'calm'|'light'|'moderate'|'strong'|'very_strong'|'violent', label:'静穏'|'微風'|…, color:'#…'}
function computeWindScreenAngles(venue, windFromDeg)   // → {thetaTo, thetaNorth, handed:'left'|'right', straight, backstretch} または null (未知の venue)
```

`getWindSpeedTerm` / `checkWindEffectHtml` は上記を呼ぶ形に書き換える (出力文字列は不変)。

### 1.2 オーバーレイの見た目

`#courseLayoutImage` を `<div class="course-map-wrap">` (position:relative; display:inline-block; max-width:100%) で包み、その中に `<svg id="windOverlay" class="wind-overlay">` を絶対配置 (top/left 0, width/height 100%, pointer-events:none) で重ねる。`viewBox` は画像ロード時に `0 0 naturalWidth naturalHeight` に設定する (アスペクト比が一致するので角度が歪まない。`preserveAspectRatio="none"` は使わない)。

描画要素 (すべて JS で生成、`renderWindOverlay(venue, windFromDeg, windSpeedMs)` が一括で描く):

1. **風の流線 (メイン)**: 画像中心を回転中心として θ_to 方向に走る平行な矢印群。
   - 本数: 静穏 0 / 微風 5 / やや強い 7 / 強い 9 / 非常に強い・猛烈 11。間隔は画像高の 1/(本数+1)。長さは画像の対角線 (回転しても画像を横切る)。`<g transform="rotate(θ_to cx cy)">` の中に水平線を並べ、`clipPath` で画像矩形に切る。
   - 線: `stroke-linecap:round`、太さ 静穏− / 微風 2 / やや強い 3 / 強い 4 / それ以上 5 (viewBox 単位、画像幅 ~570 前提)。色は `windSpeedCategory().color`: 微風 `#4fc3f7`、やや強い `#ffd54f`、強い `#ff9800`、非常に強い `#f44336`、猛烈 `#d500f9`。不透明度 0.55〜0.8。
   - 矢頭: `<marker>` で線の終端 (と中間 1 か所) に三角形。
   - **動き**: `stroke-dasharray: 18 14` と `stroke-dashoffset` の `@keyframes wind-flow` (負方向に流す) で「風が流れる」アニメーション。周期は `max(0.6, 4 − speed×0.25)` 秒 (速いほど短い)。`@media (prefers-reduced-motion: reduce)` ではアニメーション無し。
   - 静穏 (<0.3 m/s) のときは流線を描かず、中央に「静穏」チップだけ出す。
2. **方位記号**: 右上に半径 ~22 の円 + 「N」矢印を θ_N に回転。円は `rgba(0,0,0,.55)` 塗り・白縁。
3. **直線 / 向こう正面バッジ**: 画像内の相対位置に 2 つのチップを置く。
   - ホームストレッチ: (x=50%, y=71%)、文言 `直線: 向かい風 / 追い風 / 横風`。
   - 向こう正面: (x=50%, y=9%)、文言 `向こう正面: …`。
   - 色: 向かい風 = `#ff9800` 系 (逃げ・先行有利)、追い風 = `#4fc3f7` 系 (差し・追込有利)、横風 = `#bdbdbd`。チップは `rect(rx=10)` + `text` (font-size 13, 太字, 白)。チップ内の左端に小さな ▶/◀/▲ 風向アイコンは不要 (文言で足りる)。
4. **風速チップ**: 左上に `🌬 北西 3.9 m/s 微風` (方位16分割・小数1桁・階級)。背景は流線の色を 20% で敷く。
5. **凡例**: 画像の直下 (SVG の外、`.course-map-wrap` の後ろ) に 1 行 `矢印 = 風の吹いていく向き / 本数と色 = 強さ / N = 北` を `font-size:11px; color:var(--text-sub)` で出す。

画像が無い場合 (`onerror`) の**フォールバック**: `.course-map-wrap` を表示したまま、SVG の viewBox を `0 0 570 400` にし、簡略楕円 (`<ellipse>` 2 本で芝コース風のリング + 下部に「スタンド」矩形 + 左右回りに応じたゴール▲) を先に描いてから同じ流線・バッジを重ねる。「コース図なし (簡略図)」を凡例の先頭に付ける。

### 1.3 データフロー / 再描画

- `fetchWindData(venue)`: URL に `&windspeed_unit=ms` を追加。成功時に `window.lastWindData = {venue, dir: w.winddirection, speed: w.windspeed, time: w.time}` を保存し、既存テキスト表示を更新したのち `renderWindOverlay(venue, dir, speed)` を呼ぶ。失敗時は `clearWindOverlay()` (SVG を空にし、凡例に「風データ取得失敗」)。
- `courseImage.onload`: viewBox を natural サイズに更新し、`window.lastWindData` があれば `renderWindOverlay` を再実行 (画像と風のどちらが先に届いても最終状態が同じになるように)。`onerror`: 画像 hide + フォールバック描画。
- 解析のたびに `fetchWindData` が呼ばれる現行フローは不変。前回の SVG 内容は描画前に必ず全消去する。
- `windDataDisplay` のテキストは現行どおり残す (数値の単位表記 m/s は今回正しくなる)。

### 1.4 win5_predictor 側の単位バグも直す

親リポジトリ `win5_predictor/venue_info.py` の `fetch_wind_info` は同じ Open-Meteo 呼び出しで同じ m/s 誤表記なので、`params` に `"windspeed_unit": "ms"` を追加する (それ以外は不変)。親リポジトリは別 git なのでコミットは分ける (コミットは上位モデルが行う)。

## 2. 変更ファイル

- `index.html`: コースタブ右ペインのマークアップ (`.course-map-wrap` + `#windOverlay` + 凡例 `#windLegend`)。
- `script.js`: §1.1 の純関数、`renderWindOverlay` / `clearWindOverlay` / フォールバック描画、`fetchWindData` の単位修正と保存、`courseImage.onload/onerror` の拡張。
- `style.css`: `.course-map-wrap` / `.wind-overlay` / `@keyframes wind-flow` / reduced-motion。
- `../win5_predictor/venue_info.py`: 単位パラメータ追加。
- `tests/test_t76_wind_overlay.py`: §3。

## 3. テスト (`tests/test_t76_wind_overlay.py`)

Node (v24 が入っている) で `script.js` の純関数を評価する。`script.js` は読み込み時に DOM を触るので、テストは **ファイル先頭から純関数の定義部分だけを切り出す** のではなく、`script.js` 内の対象関数を `// ---- T76 wind pure functions (begin) ----` / `(end)` のマーカーで囲み、テストがその区間だけを抽出して `node -e` に食わせる (`window`/`document` 非依存であること自体が検証になる)。Node が無い環境では `pytest.skip`。

1. `computeWindScreenAngles("東京", 290)` → `thetaTo == 180`、`straight == "head"`、`handed == "left"`。`computeWindScreenAngles("東京", 110)` → `thetaTo == 0`、`straight == "tail"`。
2. `computeWindScreenAngles("中山", 140)` → `thetaTo == 0` (右回りは θ_s=180 なので 180+180=360→0)、`straight == "head"`。
3. `computeWindScreenAngles("東京", 0).thetaNorth == 70` (`0 − 290 → 70`)、未知の venue は `null`。
4. `classifyWindVsCourse` の境界: diff 45 → head、46 → cross、134 → cross、135 → tail、355 と 5 の差 (wrap) → 10 → head。
5. `windSpeedCategory`: 0.2→静穏、3.9→微風、10→やや強い風、15→強い風、20→非常に強い風、30→猛烈な風 (既存 `getWindSpeedTerm` と同じ境界)。
6. 文字列検査: `script.js` に `windspeed_unit=ms` が含まれる。`index.html` に `id="windOverlay"` と `class="course-map-wrap"` が含まれ、`#courseLayoutImage` がその中にある。`style.css` に `prefers-reduced-motion` と `wind-flow` が含まれる。`win5_predictor/venue_info.py` に `"windspeed_unit": "ms"` が含まれる。
7. 既存テストが通ること (`tests/test_t73_race_tab.py`, `tests/test_t73b_autofill.py`, `tests/test_t74_global_start.py`, `tests/test_place_shadow.py` など index.html/script.js を検査するもの)。

## 4. 受け入れ (手動・ユーザー)

1. 解析後にコースタブを開くと、コース図の上に風向きの矢印が流れて見える。矢印の向き = 風が吹いていく向き。図の右上の N が北を指す。
2. 直線・向こう正面のバッジの文言が `#windDataDisplay` のテキストと一致する。
3. 表示される風速 (m/s) が気象庁/天気アプリの現地値と同程度 (従来より約 1/3.6 になる。従来値が誤りだった)。
4. 強い風の日は本数・太さ・色・流れる速さが増す。静穏の日は矢印が出ず「静穏」チップだけ出る。
5. コース図が無いレース (画像 404) でも簡略楕円の上に同じ表示が出る。

## 5. やらないこと

- 風によるスコア/予測への反映 (表示のみ。予測への組み込みは検証を通す別タスク)。
- 予報 (時間別) の表示・履歴保存。
- 各競馬場ごとの直線位置の精密なマッピング (画像内の固定相対座標で十分)。
