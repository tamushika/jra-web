# SPEC T33: 統計ソース刷新 + 再学習 (最優先・他タスクの土台)

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T33。
背景: 2026-07-13のフル再検証 (TASKS.md 完了ログ) で、fold検証 (backtest_fold_stats.py) の
ability.db由来統計が db-keiba由来の現行統計よりクリーンな2026H1で優位と判明
(CL k=1: 29.2% vs 27.6%)。現状モデルは市場劣後のため、これが唯一エビデンスのある改善方向。

**Codexは実装と数値報告まで。本番反映 (--write・ライブ切替) は絶対に行わない。採否は上位モデル。**

## 要求仕様

### Part A: 本番用統計スナップショット生成
1. `fold_stats.py` の集計ロジック (FoldFactorTableProvider) を再利用し、
   `gen_factor_snapshot.py` を新規作成: `--as-of YYYYMMDD` (既定=今日) で
   ability.db からローリング5年窓の factor table 一式を生成し、
   `api/data_files/common/factor_snapshot.json` (1ファイル、メタデータつき) に保存する。
2. ライブ側 (`api/scoring.py` の load_factor_table 経路) に「snapshotがあればそれを使い、
   無ければ従来のdb-keiba CSVにフォールバック」の読込を実装する。
   **ただしsnapshotファイルはコミットしない/生成物を置かない状態でPRする**
   (ライブ挙動は採否判断まで従来のまま — フォールバックが働くこと)。

### Part B: 評価 (凍結分割・フォールバック無しの一貫経路)
3. `backtest_ml.py` に `--stats-snapshot` 相当の経路を追加 (backtest_fold_stats.py の
   既存機構を流用してよい): 学習も検証も ability.db 由来統計で一貫させ、
   統計as-ofは各期間の前年末 (学習21-24は2020年末〜各時点、実装は fold_stats の流儀)。
4. mined_rules_v2.csv を使う評価経路も用意 (現行 mined_rules.csv とのA/B)。
5. 比較表を報告 (すべて同一集団・人気ベースライン併記):
   - (i) 現行統計+現行rules (=現行モデル相当)
   - (ii) ability統計+現行rules
   - (iii) ability統計+v2 rules
   期間: 2025固定テスト (fold as-of 2024末) と 2026H1 (as-of 2025末)。
   指標: top-k (k=1..4)、レースLogLoss、Brier。
6. 学習/本番一致: parityテスト (tests/test_feature_parity.py) が新経路でも通ること
   (統計ソースを差し替えた場合の学習側・ライブ側の一致を確認する形に拡張)。

## 受け入れ基準
- 比較表 (i)(ii)(iii) × 2期間が数値で報告される
- ライブの既定挙動が不変 (snapshot未生成ならフォールバック)
- 全テストパス。--write は使用しない (フラグ併用はエラーにする)

## やらないこと
- win5_ml_model.json / mined_rules.csv / criteria_weights.json の書き換え
- ライブへのsnapshot配置 (採否後に上位モデルが指示する)
