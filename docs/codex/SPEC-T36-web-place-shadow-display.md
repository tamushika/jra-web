# SPEC T36: Web複勝モデルのシャドー表示接続

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T36。
前提: T16 (Web複勝確率モデル) は **2026-07-14 に上位モデルが採用**。市場独立
(オッズ・人気不使用の20特徴) の複勝確率モデルが、両凍結期間で現行Web主成分スコアを
全指標上回り、良好に校正されていることを確認済み。本タスクはその**採用済みモデルを
Web画面に「シャドー(参考)列」として接続する**もの。

**重要な性格づけ (逸脱厳禁)**: これは**表示・ロギング専用**であり **馬券シグナルではない**。
- 比較対象は市場でなく現行Web表示スコア。複勝ROIは全人気帯で<100%。
- したがって place_prob を **EV通知/LINE/Discord/WIN5配分/購入導線のいずれにも一切流さない**。
  閾値判定・アラート発火も作らない。純粋に画面表示 + 予測ログへの追記のみ。

**Codexは実装と数値・スクショ報告まで。採否・コミットは上位モデル。**
`codex/T36` ブランチか未コミットの作業ツリーで作業し、main へ直接 push しないこと。

---

## 背景: 既存の接続点 (調査済み)

ライブWeb予測は Flask アプリ `api/index.py`。1レース分の処理は `analyze_race_url(url, mode)`:
- `scoring.attach_live_pace_features(scraped_data)` (index.py L499) でライブ特徴を付与
- 各馬 `h` に既に2つのスコアが付く:
  - `h['score'], h['score_details'] = scoring.compute_score(...)` (index.py L575) … 現行Web主成分 (回収スコア)
  - `h['score_ml'], h['score_ml_details'] = scoring.compute_score_ml(...)` (index.py L578) … MLスコア (的中スコア)
- `compute_score_ml` (scoring.py L826) は内部で **`_ml_features(h, race_context, factor_table, cfg)`** を呼び、
  特徴名→値の dict `f` を作ってから `win5_ml_model.json` の線形結合を取る。
  → **この `f` に複勝モデルが必要な20特徴 (NO_MARKET_FEATURES) が全て含まれる**。複勝モデルは
  同じ `f` の部分集合を使うだけでよい (特徴生成を二重実装しない)。
- 本番MLモデルは `load_ml_model()` (scoring.py L573) が `api/data_files/common/win5_ml_model.json`
  (utf-8-sig, キャッシュ付き) から読む。**本番モデル成果物はこのディレクトリにコミットする慣行**
  (win5_ml_model.json は git 追跡されている)。
- フロントは静的 `index.html` + `script.js`。スコア列は script.js L415
  `const fields = { '回収スコア': 'score', '的中スコア': 'score_ml' };`、行描画は L470
  `(h.score ?? '-'), (h.score_ml ?? '-')`、ツールチップは L529/L542 (`score_details`/`score_ml_details`)。
- 予測ログは index.py L709 `log_race_prediction(result, ...)`。

オフラインの検証用実装は `backtest_place_model.py`:
- `NO_MARKET_FEATURES` (20特徴、ln_odds除外)、`fit_place_model` (2021-23 L2ロジ + 2024 Platt校正)、
  `predict_place_probability(model, X)` (標準化→線形→Platt)。
- 既定出力は **スクラッチの `web_place_model.json` (リポジトリ直下、gitignore済み・本番未接続)**。
  `validate_model_output_path` が api 配下への出力を禁止している (offline衛生ガード)。

---

## 要求仕様

### A. 本番モデル成果物の生成とコミット
1. `backtest_place_model.py` に **`--production` モード** (または明示フラグ) を追加する。
   このモードは同一レシピ (同じ NO_MARKET_FEATURES・L2ロジ・Platt校正・ability年次as-of統計) で、
   **本番用の分割**で refit し、`api/data_files/common/web_place_model.json` に保存する:
   - 学習: 2021-01-01 〜 2024-12-31 (凍結検証の3年に1年足した4年。より多くのデータを使う)
   - 校正: 2025 (直近の完全な1年、ホールドアウト校正)
   - 2026H1 は本番モデルにも**投入しない** (継続してライブ・シャドー実測に使うため未使用のまま)
   - 既定 (フラグ無し) の凍結OOS検証ロジック・スクラッチ出力・`validate_model_output_path`
     の挙動は**一切変えない**。`--production` の時だけ、固定の本番パスへ書くことを許可し、
     「本番モデルを上書きする」旨の日本語警告を stdout に出す。
2. **`.gitignore` の修正が必須**: 現状 L29-30 の `web_place_model.json` は**アンカー無しで
   どこでも無視**するため、本番パスに置いてもコミットされない。ルート直下のスクラッチだけを
   無視するよう `/web_place_model.json` (先頭スラッシュでルート限定) に変更し、
   `api/data_files/common/web_place_model.json` を **git 追跡・コミット対象**にする。
   スクラッチ (リポジトリ直下) は引き続き無視されること・本番コピーは追跡されることを
   `git check-ignore` で両方確認して報告する。
3. 生成した本番モデルの `meta` に用途と分割 (`purpose: "web_shadow_display"`,
   train/calibration期間, n_train, n_calibration) を残す。

### B. ライブ推論 (api/scoring.py) — 特徴生成は再利用
1. `load_place_model()` を追加 (`load_ml_model` L573 と同型: キャッシュ、utf-8-sig、
   `api/data_files/common/web_place_model.json`、欠損・例外時は None を返す)。
2. `compute_place_prob(h, race_context, factor_table, cfg)` を追加。戻り値 `(prob, details)`:
   - `model = load_place_model()`。None なら **`(None, [])` を返す** (欠損時は列に `-` を出す。
     他スコアへフォールバックしない — 複勝確率と主成分スコアは単位が違う)。
   - `f = _ml_features(h, race_context, factor_table, cfg)` を**再利用**して特徴 dict を得る。
   - `model["features"]` (= NO_MARKET_FEATURES) の順で `f` から値を取り、`predict_place_probability`
     と**数値的に一致する**手順で確率を計算する: `(値 - mean)/scale · coef + intercept` → Platt
     (`slope*z + intercept` の sigmoid)。実装は `backtest_place_model.predict_place_probability` の
     単一馬版。可能なら backtest_place_model の関数/係数適用を import して単一ソース化する。
   - `details`: 寄与上位の特徴を `compute_score_ml` と同様に数本 (例 "複勝: 前走着順 +0.42" 等)、
     ツールチップ用に返す。
   - **市場独立契約の assert**: `"ln_odds" not in model["features"]` と、popularity/odds系の名前を
     一切参照しないこと (compute_score_ml と違い ln_odds 分岐を持たない)。例外時は `(None, [])`。

### C. 画面接続 (api/index.py, index.html, script.js) — 追加のみ・既存不変
1. `analyze_race_url()` 内、score_ml 計算 (L578) の直後に
   `h['place_prob'], h['place_prob_details'] = scoring.compute_place_prob(h, race_context, factor_table, score_cfg)`
   を追加。返り値の `place_prob` は 0〜1 の確率 (または None)。
2. `index.html` のスコア表ヘッダに列 **「複勝率β」** を追加 (回収スコア・的中スコアの隣)。
   `script.js` の `fields` マップ (L415) に `'複勝率β': 'place_prob'` を追加してソート可能にし、
   行描画 (L470周辺) に `(h.place_prob != null ? (h.place_prob*100).toFixed(1)+'%' : '-')` を追加。
   ツールチップは `place_prob_details` を既存2列と同じ仕組みで表示。
3. **参考・非馬券であることを画面に明示**: 列見出しに β バッジ、または表下に一行凡例
   「複勝率β = 市場非依存の参考指標 (校正済み複勝確率)。馬券判断には用いない」を出す。
   既存の 回収スコア/的中スコア 列・◎〇△等の表示は**一切変えない** (HANDOFF §5-6)。
4. **予測ロギングへの追記**: index.py L709 `log_race_prediction` に渡る各馬レコードに
   `place_prob` を**加算フィールドとして含める** (既存フィールドは変更しない)。後日ライブ・
   シャドーの複勝的中/校正を評価するため。ロギング側スキーマ変更が要るなら追加列/キーは
   nullable・後方互換で。

### D. 特徴量パリティの担保
- 複勝モデルの校正はライブ特徴とオフライン特徴の一致を前提にする。T5 のパリティ・ハーネスを
  再利用し、**NO_MARKET_FEATURES の20特徴について**ライブ (`_ml_features` 経由) と
  オフライン (`build_consistent_feature_dataset`) が T5 と同じ許容誤差内で一致することを
  1テストで確認する (grade_pts の既知差は T8 で解消済み前提)。

---

## テスト (最低4件)
1. `compute_place_prob` が `backtest_place_model.predict_place_probability` と、合成した特徴・
   保存JSON構造で数値一致 (round-trip)。
2. モデル欠損時に `compute_place_prob` が `(None, [])` を返し、行が `-` 描画になる (フォールバック
   で他スコアに化けない)。
3. 市場独立契約: 使用特徴に ln_odds/popularity/odds が一切含まれない (assert)。
4. パリティ: 上記 D のライブ/オフライン20特徴一致。
5. (可能なら) `log_race_prediction` のペイロードに `place_prob` が入り、既存フィールドが不変。

## 受け入れ基準
- 実レース1本 (または取り込み済みキャッシュ) で β列が妥当な校正済み%を表示するスクショ/JSON。
  2-3頭について place_prob と score/score_ml を並べて報告。
- パリティテスト・全テストパス。`git check-ignore` でスクラッチ無視/本番追跡の両立を確認。
- **EV通知経路が不変**: `jra_ev.py` の diff がゼロであることを示す (place_prob は通知に非接続)。
- 本番モデル生成コマンドの出力 (n_train/n_calibration/保存先) を報告。

## やらないこと
- place_prob を EV/LINE/Discord/WIN5/購入・アラートのいずれかに接続する (表示+ログ専用)。
- 現行スコア・◎〇△表示・EV通知条件 (LINE=5分前×EV>=1.3) の変更。
- 複勝モデルへオッズ・人気特徴を追加する (市場独立が価値の源泉)。
- 特徴生成の二重実装 (`_ml_features` を再利用する)。
