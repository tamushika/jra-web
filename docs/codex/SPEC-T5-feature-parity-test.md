# SPEC T5: 学習/本番の特徴量一致テスト

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T5。

## 背景
WIN5用MLモデル (conditional logit、21特徴量、`api/data_files/common/win5_ml_model.json`) は
学習時とライブ予測時で特徴量生成コードが別経路になっており、既知の不一致がある
(docs/prediction-accuracy-improvement-roadmap.md §3.2 の表)。
モデル改善の前に「どの特徴量がどれだけズレているか」を自動検出できるテストが必要。

## 対象コード (まずここを読む)
- 学習側の特徴量生成: `backtest_ml.py` の `build_dataset()` (ability.db の runs から生成)
- ライブ側の特徴量生成: `jra_win5.py` および `api/` 配下 (build_dataset 相当の処理を探す。
  `win5_ml_model.json` の `features` 配列が21特徴量の定義名)
- 既知の不一致: オッズ(確定vs直前)・pace_fit(ライブは常に0)・馬体重(ライブは前走値)・
  grade_pts(ルール定義差)

## 要求仕様
1. `tests/test_feature_parity.py` を作成 (pytest 形式。既存 `tests/` の流儀に合わせる)。
2. ability.db から直近の完了レースをN=20レース程度サンプルし、各レースについて:
   - 学習側経路で21特徴量ベクトルを生成
   - ライブ側経路で同じレースの21特徴量ベクトルを生成
   - 特徴量ごとに突き合わせ、`abs(diff) > 1e-6` の馬の割合を集計
3. 既知の不一致4項目 (オッズ/pace_fit/馬体重/grade_pts) は **expected failure として明示**
   (xfail マーカーまたは許容リストで区別)。それ以外の特徴量で不一致が出たらテスト失敗。
4. 実行はネットワーク不要 (ability.db のみで完結) にする。ライブ経路がJRAページ取得を
   前提にしている場合は、特徴量計算関数をデータ注入可能な形に**最小限**リファクタして良い
   (公開関数のシグネチャを変える場合は呼び出し元をすべて追随)。
5. レポート出力: テスト実行時に特徴量別の不一致率表を stdout に出す
   (どの特徴量が何%の馬でズレるか)。

## 受け入れ基準
- `python -m pytest tests/test_feature_parity.py -v` が通る
  (既知4項目は xfail/skip として表示され、他の17特徴量は一致)
- 不一致率表がレポートされる
- 既存テスト (`python -m pytest tests/`) が壊れていない

## やらないこと
- 不一致の「修正」はしない (pace_fit実装=T6、馬体重=T7 で別途)。本タスクは検出のみ
- モデルの再学習・win5_ml_model.json の変更
