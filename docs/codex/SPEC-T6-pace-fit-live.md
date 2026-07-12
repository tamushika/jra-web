# SPEC T6: pace_fit のライブ計算実装

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T6。依存: T5 (検証に使う)。

## 背景
学習時の特徴量 `pace_fit` は `backtest_ml.py` 内で馬ごとに計算されるが、ライブ予測
(WIN5アプリ / EV監視) では常に0が入っている。conditional logit はレース内softmaxのため
全馬一律0でも確率は歪まないが、pace_fit の情報が捨てられている。

## 要求仕様
1. `backtest_ml.py` の pace_fit 定義 (計算式・使用データ列) を正確に把握する。
2. ライブ側の特徴量生成 (jra_win5.py / api/ 配下) に同一定義の pace_fit 計算を実装する。
   - 必要な過去走データ (PCI等) がライブ側で参照している past data ソースに存在するか確認し、
     欠損時は学習時と同じ欠損処理 (build_dataset と同じデフォルト値) に合わせる
3. 定義は学習側と1つのコードパスに共通化できるなら共通化する (api/ 配下の共有モジュール化)。
   共通化が大改造になる場合は、同一ロジックの複製+相互参照コメントでも可。

## 受け入れ基準
- T5 の parity テストで pace_fit が xfail から「一致」に移動する
  (tests/test_feature_parity.py の許容リストから pace_fit を外してテストが通る)
- `python -m py_compile` 対象ファイル全部OK、既存テストが壊れていない
- ライブ画面 (WIN5アプリ) の予測が例外なく動く (起動して1レース分の予測が返ること)

## やらないこと
- モデル係数の変更・再学習 (pace_fit が正しく入るようになった後の再学習は別タスク)
- 他の不一致 (馬体重・grade_pts) への波及修正
