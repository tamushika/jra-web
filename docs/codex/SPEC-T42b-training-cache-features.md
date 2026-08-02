# SPEC-T42b: 調教キャッシュのパーサー精錬 + 調教特徴の凍結分割検証

起票: 2026-08-02 (Fable 5)。T42a完遂 (immutableキャッシュ33,229ファイル・SHA監査済み) を受けた
次工程。**ネットワークアクセスは一切禁止 — 入力は `data/t42/raw/` のキャッシュのみ**
(トライアルは解約済み。キャッシュが失われたら再取得できないため、読み取り専用で扱う)。

**2段構成。Stage A (パーサー・構造化・品質報告) を先に納品し、上位モデル受理と
T39台帳登録を経てから Stage B (特徴量評価) を実行する** (T58と同じ運び)。

## 0. 動かせない前提

- キャッシュ (`data/t42/raw/`, `manifest.sqlite`) への書き込み・移動・削除は禁止
- ability.db は読み取りのみ。本番コード (scoring/jra_ev/jra_win5/combo_probs) に触れない
- Stage B の候補特徴は本SPECで固定。**データの集計値を見てから候補を増減しない**
- 評価は凍結分割 (学習2021-23 / 選抜2024 / テスト2025-26H1)。採否ゲートは
  T41/T54dと同一: 一次指標=選抜期間のpaired勝馬LogLoss差 (開催日block bootstrap)、
  かつ**市場top-kフロア (k=1..4) がbaseline比で悪化しないこと**

## 1. Stage A: パーサーと構造化ストア (1-2日)

### 1-1. 出力: `data/t42/t42_training.sqlite` (gitignore)

```
race_training_rows(      -- レース調教ページ由来 (as-of主系)
  race_id, horse_id, horse_name, row_index,
  training_date,         -- YYYY-MM-DD (曜日suffix除去)
  course_raw, course_norm, baba, rider,
  times_json,            -- 例 [85.6, 68.3, 53.1, 39.0, 12.4] 外→内の順で生値
  lap_count, position, intensity_raw, intensity_norm,  -- 一杯/強め/馬也/直線 等
  evaluation, comment, partner_text,
  source_sha256, parser_version)

horse_training_rows(     -- 馬別ページ由来 (休養中調教の補完・全キャリア混在)
  horse_id, group_index, row_index, training_date,
  course_raw, course_norm, baba, rider, times_json, lap_count,
  position, intensity_raw, intensity_norm, evaluation, comment,
  source_sha256, parser_version)

race_id_map(race_id, date8, place, race_no)   -- キャッシュ済みrace_indexページから構築
parse_audit(source_path, category, rows_parsed, rows_skipped, skip_reasons_json)
vocab_audit(field, raw_value, count)          -- course/intensity の全語彙と頻度
```

### 1-2. パース要件

- レースページDOM (`Training_Day`/`TrainingTimeDataList` 系) と馬別ページDOM
  (`race_table_01`) の両方 (実DOMはStage 0/Phase 2で確認済み。fixtureは
  キャッシュから最小断片を切り出して合成し、テストにコミットする — 全ページの
  コミットは不可)
- 時計セルの括弧内ラップ (例 `85.6 (17.3) 68.3 (15.2)`) は主時計とラップを分離
- `-` のみの行 (時計なし) は skip せず lap_count=0 で保存 (調教量ゼロも情報)
- **course_norm / intensity_norm の正規化辞書**を実装内に固定し、未知語彙は
  正規化せず vocab_audit に記録 (T22の会場語彙ゲートと同じ思想。**未知語彙で
  落とさない** — 後から辞書を拡張して再パースできるのがrawキャッシュの利点)
- 冪等: 再実行は全テーブルをDROP→再構築 (追記ではない)。source_sha256 は
  manifest の値を転記

### 1-3. 品質報告 `docs/T42b-stage-a-report.md`

- パース率 (ページ数・行数・skip内訳)、course/intensity語彙の頻度表 (上位+未知)
- **カバレッジ照合**: ability.db の 2021-2026H1 出走馬のうち、当該レースの
  race_training_rows が1行以上ある割合 (年別)。80%を下回る年があれば原因を調査
- race_id_map の欠落 (race_idが日次インデックスに無い等) の件数
- 数値サニティ: 主時計の分布 (コース別p1/p99)、training_date > race日 の行数
  (**0件であるべき** — レースページの公開時刻ポリシー上、レース後の調教が
  混入していたらas-of違反なので即報告)

## 2. Stage B: 特徴量評価 (Stage A受理+台帳登録後、1日)

### 2-1. 候補特徴 (6個固定・レースページ由来のみ)

各出走馬×レースについて、そのレースのrace_training_rowsから:

1. **F1 final_time_z**: 最終追切 (training_date最大の行) の主時計を、同コース
   (course_norm) ×同調教日の全馬集合で z標準化した値 (同日同コース標準化 —
   馬場差を吸収。集合が5未満なら同コース×同月で代替)
2. **F2 finish_bite**: 最終追切のラスト1F − その追切の平均1F (負=終い加速)
3. **F3 workout_count**: 掲載追切本数 (lap_count>0 の行数)
4. **F4 days_since_final**: レース日 − 最終追切日
5. **F5 intensity_share**: intensity_norm が「一杯/強め」の行の割合
6. **F6 zero_workout**: 掲載行が全て lap_count=0 (時計なし) のフラグ

- 欠測 (調教行なし・時計なし) は F1/F2=集合平均、F3/F5=0、F4=中央値で補完し、
  **欠測フラグ列を必ず併走**させる (T41と同じ扱い)
- horse_training_rows はStage Bでは**使わない** (全キャリア混在のas-of規律が別課題。
  休養明け特徴は結果を見てから別SPECで検討)

### 2-2. 評価

- 既存 `backtest_feature_pack.py` の様式 (T41ハーネス) で、現行CL baseline に
  各候補を単独追加 + 全候補同時の7構成を評価
- 凍結分割・paired勝馬LogLoss (開催日block bootstrap 2000回)・市場top-kフロア。
  ability.db は T51是正後の封印SHA `7ffcfe21…0c79` を使用
- **上位モデルが評価前にT39へ登録する** (experiment_id `T42b-training-features-v1`、
  候補7構成・ゲート・キャッシュmanifest DBのSHA・本SPECのSHAを封入)。
  Codexは登録完了の連絡を受けてから評価を実行し、数値の解釈をせず報告のみ行う

## 3. テスト (両Stage)

1. パーサー: 実DOM断片fixtureで全列抽出・時計/ラップ分離・`-`行の lap_count=0
2. 正規化辞書: 既知語彙の変換・未知語彙の素通し+audit記録
3. race_id_map: インデックスfixtureからの構築と欠落記録
4. as-ofサニティ: training_date > race日 の行を検出するテスト (検出ロジック自体の検証)
5. Stage B: 特徴計算の決定論・欠測補完とフラグ・T39台帳照合fail-closed (T62/T63と同型)
6. `python -m pytest tests/ -q` 全体パス

## 4. 成果物

- Stage A: `t42b_parse_training.py`・`tests/test_t42b_parse.py`・品質報告
- Stage B: `backtest_t42b_features.py` (または feature_pack 拡張)・
  `tests/test_t42b_features.py`・結果JSON (outputs/)・報告追記
- 採否判断・T20 (LightGBM再評価) への接続判断は上位モデル

想定: Stage A 1-2日 → 上位モデル受理・登録 → Stage B 1日 → 上位モデル裁定。
