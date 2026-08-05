# SPEC-T66: WIN5 500点シャドー実績のダッシュボード表示 (遡及対応)

作成: 2026-08-05 (Fable 5)。実装: jra-coder。レビュー・コミット: 上位モデル。

## 1. 目的

perfダッシュボード (jra_perf.py / index_perf.html、suite 5005 の /perf/) の「WIN5実績」に、
**「もし毎回500点 (prob通常配分・軸なし) で買っていたら」のシャドー実績**を併記する。
実購入 (現状は200点) と同一の予測スナップショットから導出するため、
**ログが存在する過去日 (2026-07-12以降) に自動で遡及適用**される。

背景: 2026-08-05 に WIN5買い目生成へ500点オプションを追加済み
(2025年OOS 101日: prob通常 12.9% / 平均483.1点。scoring-win5.md §3参照)。

## 2. 方式 — 導出表示 (DBに書き込まない)

シャドーは**表示時に毎回、ログ済みデータから再計算**する。理由:
- 入力 (predictions.ml_score, win5_predictions.prediction_run_ids_json) は不変ログなので
  再計算は決定的・再現可能
- バックフィルスクリプトや新テーブルが不要。過去日も同一コードパスで出る

**DBスキーマ変更・書き込みは一切しない**こと。

## 3. 計算仕様

### 3.1 対象行

`jra_perf.py` の WIN5実績で表示している各 `win5_predictions` 行 (最大30行) それぞれに、
シャドー配分を付与する。日別サマリー (§3.4) は **各日の created_at 最新行** を代表とする
(締切に最も近い=実購入と同条件のオッズ)。

### 3.2 シャドー配分の導出

各 `win5_predictions` 行について:

1. `prediction_run_ids_json` の5 run それぞれに対し
   `SELECT horse_id, ml_score FROM predictions WHERE prediction_run_id=?`
2. ml_score 降順にソートし、`scoring.win_probs_from_ml_scores([ml_score...])` で勝率化
   (jra_win5.build_kaime と同一。scoring は api/ 配下のものを import)
3. `scoring.allocate_picks_prob(prob_lists, 500, max_picks=8)` で k1..k5 を決定
   (max_picks は win5_weights.json の allocation.max_picks_per_race を読む。軸固定なし)
4. 各レースのシャドー選択馬番 = ml_score 上位k頭の `horse_id` 末尾2桁 (`int(horse_id.split(":")[-1])`)
5. 的中判定は既存 `_win5_hit_flags(race_ids, shadow_selections, results)` を再利用

予算は定数 `WIN5_SHADOW_BUDGETS = [500]` としてモジュール先頭に置く (将来の追加を容易に)。

### 3.3 フォールバック・欠損時

- いずれかの run で **ml_score が1頭でも NULL / 行が0件** → その行のシャドーは `None`
  (UIは「算出不可」表示)。**代替値のでっち上げをしない**
- レース結果未確定 (settled でない) → hit は未確定表示 (既存の実績表示と同じ扱い)

### 3.4 収支 (シャドーROIプロキシ)

- コスト = 総点数 × 100円
- 的中日の払戻 = `win5_results.payout_yen` (T55で取込済みの公式配当) をそのまま使う。
  **プロキシである旨をUIに明記** (自票によるパリミュチュエル希薄化は無視。
  的中票数が数千〜万オーダーのため誤差は小さい)
- 日別サマリー: 公式結果がある日のみ集計し、
  `対象日数 / 的中日数 / 投入計 / 払戻計 / ROI` を「500点シャドー累計」として表示

## 4. UI (index_perf.html)

- WIN5実績の各行 (既存の実購入プラン表示) の直下に1行追加:
  `500点シャドー: 5×5×4×5×4=xxx点 / 的中○ (または ✗・未確定・算出不可) / 払戻xx,xxx円`
- セクション末尾 (または先頭) に「500点シャドー累計 (2026-07-12以降・仮想)」ブロック
- **「シャドー=実購入ではない」ことをラベルで明示**。購入推奨の文言は書かない
  (WIN5はT59で凍結方針。これは記録・比較のみ)
- 既存のスタイル (style.css の既存クラス) に合わせ、新規CSSは最小限

## 5. 変更してよいファイル / 禁止事項

- 変更可: `jra_perf.py`, `index_perf.html`, `tests/test_jra_perf.py` (追記), 必要なら `style.css` 最小限
- **禁止**: logging_store.py のスキーマ変更、jra_win5.py / jra_ev.py / 通知系の変更、
  win5_weights.json の変更、DB書き込み、バックテストの実行
- CRLF注意: HTMLをPythonスクリプトで編集しない。Edit/Writeツールで直接編集

## 6. テスト (tests/test_jra_perf.py に追加)

一時SQLiteに最小フィクスチャ (5レース×各3頭程度の predictions + win5_predictions 1行 +
results + win5_results) を作り:
1. シャドー配分が決定的に再現される (総点数≤500、各レースk≥1、選択馬番がml_score上位)
2. 勝ち馬が全レースのシャドー選択に入るケース → hit=True、払戻=payout_yen が集計に載る
3. 1レースの ml_score を NULL にする → シャドー=None (算出不可)
4. 既存テストが全パス (`python -m pytest tests/ -q`)

## 7. 完了条件

- 上記テスト全パス+既存653テストにリグレッションなし
- ダッシュボードをローカル起動し、2026-07-12〜08-02 の過去7日ぶんに
  シャドー行が遡及表示されることをスクリーンショットまたはJSON出力で確認
- コミットはしない (上位モデルがレビュー後に行う)
