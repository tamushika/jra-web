# 血統特徴量の検証手順 (引き継ぎ用)

作成: 2026-07-07。血統バックフィル完了後に、どのセッション/モデルでも
この手順だけで検証〜採用判断ができるように書いてある。
セッション横断の全タスク・人手作業の一覧は [HANDOFF.md](HANDOFF.md) を参照。

## 現状 (2026-07-07時点)

- ability.db (ML学習データ) には父・母父が無い — これが最大のデータ制約
- 対策として Neon の `horse_pedigree` テーブル (馬名→父/母父/母) に蓄積中:
  1. **受動的蓄積**: 出馬表スクレイプのたびに自動 upsert (`api/pedigree_store.py`)
  2. **バックフィル**: `run_pedigree_backfill.bat` を1日1回実行 (netkeiba日次上限
     ~4,000リクエストのため分割。進捗は `python backfill_pedigree_netkeiba.py --status`)
     - Phase A (馬名→netkeiba馬ID): 2026-07-07時点で 113/556日・12,836頭
     - Phase B (馬ページ→父/母父): 未着手。**直近出走馬から優先取得**する設計
- **検証コードは実装済み**: backtest_ml.py に `sire_pts` 特徴量 (db-keiba father_w
  バイアス点) が入っており、血統カバレッジ >= 70% (`PEDIGREE_MIN_COVERAGE`) で
  自動有効化される。それまでは全0の定数列として無害
- ライブ推論 (`scoring._ml_features`) の sire_pts は実装・動作確認済み
  (出馬表に父が載るため、ライブは蓄積と無関係に常時計算できる)

## 検証手順 (バックフィル完了後)

```
# 0. カバレッジ確認 (backtest_ml が起動時に表示する)
python backfill_pedigree_netkeiba.py --status

# 1. Neon → ローカルキャッシュを更新 (pedigree_cache.json, gitignore対象)
python -c "import sys; sys.path.insert(0,'api'); import pedigree_store; print(len(pedigree_store.load_all(refresh=True)))"

# 2. OOS検証 (学習21-24 → 検証25)。起動ログに「血統カバレッジ: X% → sire_pts 有効」が出ること
python backtest_ml.py

# 3. 採用判断 → 本番反映
python backtest_ml.py --write
```

## 採用判断の基準 (これまでの前例に合わせる)

- 検証セット(2025年)のカバレッジ表で **k=1/k=2 が現行 (31.3/51.0) から改善**するか、
  同等でも sire_pts の係数が明確 (|coef| >= 0.03 目安) なら採用
- 悪化するなら `FEATURES` から "sire_pts" を外して --write し直す (定数列でも実害は
  ないが、モデルJSONを綺麗に保つ)
- LightGBM再評価 (`--lgbm`) も血統投入後にやる価値がある (線形で拾えない
  血統×コース交互作用が本命。2026-07時点の線形20特徴量では LGBM は不採用だった)

## 併せてやると良いこと (優先度順)

1. **血統ありの criteria 再マイニング**: `mine_criteria.py` の候補条件に
   「父が◯◯」を追加 (コースごとの father_w 上位種牡馬から生成)。
   check_condition は「父が…」「父・母父が…系」を解釈できる。
   系統ルールには `analysis.load_sire_lineage` の系統マップを evaluate に渡すこと
2. **backtest_criteria の血統ルール解禁**: `load_filtered_criteria` の UNEVALUABLE
   正規表現 (父|母父|系|産駒|生産者|減量) から血統を外し、build_h に pedigree を
   注入すれば、書籍274ルールの残り半分が検証可能になり criteria_weights.json の
   カバー率が上がる (backtest_ml.build_dataset には注入コード実装済み)
3. **血統辞典 (sire_buysell_rules.json) のML特徴量化**: eval_sire_buysell の
   買い/消し判定を bs_pts 特徴量に

## 注意 (ハマりどころ)

- **netkeiba は1日 ~4,000リクエストでブロック** (自動停止する。翌日再開)
- pedigree_cache.json は Neon の写し。古いと感じたら refresh=True で再取得
- 発掘条件 (mined_rules.csv) は選抜に 2025-26 データを使用済みのため、
  **ML特徴量に入れて25年で検証するとリーク** (再マイニングで期間を組み直すこと)
- 再学習の前に必ず `backfill_odds_netkeiba.py` (retrain_all.bat の Step1) —
  オッズ欠損のまま学習すると ln_odds がリークしてモデルが壊れる (2026-07に実証済み)
- モデルの温度 (prob_temperature) は再学習時に自動再調整される
- 血統名の表記: netkeiba とJRA出馬表で同一馬名は一致するが、種牡馬名の照合は
  `scoring._match_entity` (NFKC正規化・部分一致) に任せること
