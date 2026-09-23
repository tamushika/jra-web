# SPEC-T80b: 枠割当修正後の CL 再学習と凍結分割ゲート (事前登録)

起票: 2026-09-24 (Fable 5.1)。T80 (2026-09-19) で `past_data_service.calculate_waku` の誤りを修正した結果、**学習・採掘側の枠特徴が本番 (`api/index.py`) と 2021〜2025 の 53.6% の行でずれていた**ことが判明した。現行本番モデル (`api/data_files/common/win5_ml_model.json`, sha256 `8687f9bf…`, T45 candidate 2026-07-19) は誤った枠で学習され、ライブでは正しい枠で採点されている (学習/本番スキュー)。本タスクは **バグ修正の影響を凍結分割で計測し、修正後の枠で学習した候補への差し替え可否を裁定する**。新特徴の探索ではない。

## 0. 不変条件
- ability.db は読み取り専用 (`file:...?mode=ro`)。封印 sha256 = `bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a`、最終日 20260704 (2026H2 は完全未使用のまま)。
- 本番 artifact (`win5_ml_model.json`, `factor_snapshot.json`, `mined_rules_v2.csv`)・`jra_ev.py`・通知関数・weights は変更しない。差し替えは裁定後に上位モデルが行う。
- 候補は **1 つ** (修正後の枠で学習した現行 21 FEATURES の conditional logit)。ハイパーパラメータ探索・特徴追加なし。温度は T45 と同じ手順 (2021-23 学習 → 2024 調整)。
- 稼働中の統合サーバー (5005) に触らない。`git checkout/stash/reset` 禁止。`docs/TASKS.md` は触らない。コミットしない。

## 1. 事前登録 (実装前に台帳へ追加すること)
`python -m eval.ledger register` で以下を登録 (`eval/experiments.jsonl` はバイナリ追記・改行 `\r\n`、`python -m eval.ledger verify --no-update-state` が OK であること):
- `experiment_id`: `T80b-waku-fix-retrain-v1`
- `benchmark_type`: `historical`、`candidate_count`: 1、`commit_sha`: 実装時 HEAD
- `data_hashes`: `ability_db_sha256` (上記)、`spec_sha256` (本ファイル)、`production_model_sha256` = `8687f9bfa2278ed1dcafd9f13c90b08fa6b6d58f993a2c139fd39c9edbf34527`
- `features`: 「現行 21 FEATURES 不変。変更点は枠特徴 (frame 因子ポイント) の枠番算出のみ: T80 修正済み `calculate_waku` (JRA 規則) vs 旧実装 (余りを外枠 2 頭ずつ)」
- `primary_metric`: `win_logloss_paired_diff_fixed_minus_legacy_2025` (固定テスト 2025・全レース母集団・開催日 block bootstrap)
- `selection`: 2024 (調整のみ・候補 1 つなので選抜なし)。`stop_rule`: 2025 で primary が有意に悪化 (paired bootstrap 95% 上側 < 0 でない かつ 点推定 > +0.001) なら 2026H1 を読まずに不採用
- `gate` (all_or_nothing): (a) 2025 と 2026H1 の両方で `LL_fixed ≤ LL_legacy + 0.0005` (同等以上)、(b) 市場 top-k フロア (k=1..3、WIN5 母集団) が legacy 比で悪化しない (差 ≥ −0.3pt)、(c) WIN5 母集団の paired LL 差が両期間で符号一致 (悪化方向でない)、(d) 本番モデル (skewed 評価) に対しても 2026H1 で LL 同等以上
- `notes`: 「バグ修正の確認実験。市場超えの主張はしない。採用 = 学習/本番の枠定義一致の回復」

## 2. 実装 `backtest_t80b_waku_retrain.py`
1. `backtest_t45_candidate.py` の関数 (`fit_candidate`, `compare_period`, `same_population_metrics`, `build_feature_dataset`) を再利用し、**枠実装を差し替え可能**にする: `backtest_ml.calculate_waku` を (a) `legacy` (T80 で置換された旧実装をこのスクリプト内に `_legacy_calculate_waku` としてコピー) と (b) `fixed` (`past_data_service.calculate_waku`) で monkeypatch して同じ手順を 2 回走らせる (特徴データセットは各実装で生成)。
2. **ローリングオリジン** (凍結分割): origin A = 2021-23 学習 → 2024 評価 (調整・温度)、origin B = 2021-24 学習 → 2025 評価 (固定テスト)、origin C = 2021-25 学習 → 2026H1 評価 (確認)。各 origin で legacy/fixed を学習し、**同一母集団** (全レース / WIN5 = 9R以降・8頭以上・オッズあり) で `win_logloss`, 市場 LL, top-k (k=1..4), paired 開催日 block bootstrap (2000 回) の差 (fixed − legacy) と 95% CI を出す。
3. **本番モデルとの比較**: 現行本番 artifact をロードし、fixed 特徴 (= ライブと同じ枠) で 2025 / 2026H1 を採点した LL・top-k を併記 (T45 報告と同じ `compare_period` 形式)。
4. **候補バンドル**: origin C 相当の最終学習 (2021-2025・fixed 枠) で `win5_ml_model.json` 候補と `factor_snapshot.json` (2025-12-31 断面) を `outputs/t80b/` に生成し、seed=45 で 2 回生成して sha256 一致を確認 (T45 と同じ)。
5. 出力: `outputs/t80b/report.json` (全数値) と `docs/T80b-waku-retrain-report.md` (表: origin × 母集団 × {legacy, fixed, market, production} の LL/top-k、paired 差と CI、ゲート判定の機械適用結果 `gate_pass: true/false` と根拠)。**採否の記述はしない** (上位モデルが裁定)。
6. 台帳へ `T80b-waku-fix-retrain-v1-result` 行を追記 (数値要約・report sha256)。adjudication 行は上位モデル。
7. テスト `tests/test_t80b_waku_retrain.py`: 小さな合成データで legacy/fixed の枠が異なる行が存在すること、両実装で market baseline が一致すること (母集団同一)、ゲート判定関数の境界。

## 3. 報告
- 表 (§2-5) と `gate_pass`、実行時間、候補 sha256×2 回一致、台帳 verify OK、テスト結果。
- 逸脱があれば理由。**ability.db の sha256 が実行前後で不変**であることを明記。
