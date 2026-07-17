# SPEC T41: 低コスト特徴パック (A3馬体重変動 / A7ローテ / A8斤量差分 / X3タイム指数状態量)

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T41。
背景: [accuracy-ideas-20260717.md](../accuracy-ideas-20260717.md) A3/A7/A8/X3、
[RE-accuracy-ideas-20260717.md](RE-accuracy-ideas-20260717.md) §3/§4.3/§4.7/§4.8/§10-X3 (採用済み)。
起票: 2026-07-17 上位モデル。

**実装者の担当は、特徴量構築・診断スクリプト・テスト・数値報告まで。
採否判断・実験台帳への事前登録・コミットは上位モデルが行う。**

## 1. 目的

**新規データ取得ゼロ** (ability.db の保有列のみ) で構築できる4グループの特徴を、
現行 conditional logit へ追加した場合の増分を凍結分割で検証する。
現行モデルは市場同等〜劣後 (TASKS.md 完了ログ 2026-07-13) であり、
本パックは「保有データから本当に新しい差分だけを小さく試す」段 (RE §11)。

## 2. 実装方式 (T13 RELATIVE_FEATURES と同じ隔離パターン)

- **本番 `FEATURES` は変更しない**。`backtest_ml.py` の `RELATIVE_FEATURES` と
  同様に、パック特徴のリスト `PACK_FEATURES` を定義し、診断実行時だけ末尾に加える
- 実行入口は新スクリプト `backtest_feature_pack.py` (`backtest_ml.py` の
  `build_dataset`/`fit_conditional_logit` を再利用)。本番ファイルの変更は
  `build_dataset` への特徴追加コード (フラグでOFF時は完全に非活性) までを上限とし、
  既存呼び出しの出力が変わらないこと (決定論SHA一致) をテストで保証する
- 全特徴は**レース時点as-of** (当該レースおよび未来の行を一切使わない)。
  ability.db `runs` の date/place/r/umaban/rank/time_sec/agari_3f/kinryo/weight/
  jockey/class/distance/track_type 等の既存列のみ使用

## 3. 特徴量定義 (4グループ・各グループ独立にON/OFF可能)

### 3.1 A3 馬体重変動 (rawの当日馬体重 `weight` は既存 — 差分だけが新規)

- `weight_delta_prev`: 当日体重 − 前走体重 (前走なしは0 + missing flag列)
- `weight_delta_rate`: 上記 / 前走体重
- `weight_dev_expanding`: その馬の**過去走のみ**のexpanding中央値からの符号付き偏差、
  および絶対偏差 (MAD正規化。過去2走未満は0 + flag)
- `weight_delta_x_layoff`: `weight_delta_prev` × ln(休養日数)
- **禁止**: 全キャリア (未来を含む) からの「好走時最適体重」逆算は未来リーク。
  expanding統計は当該レースを含めない
- 当日体重欠損時は前走値fallback + flag (本番ライブとバックテストで同一規則)

### 3.2 A7 ローテーション (`ln_interval` は既存 — 以下が新規)

- `starts_28d` / `starts_56d`: 過去28/56日の出走数
- `run_after_layoff`: 休養 (90日以上) 明け何戦目か (上限5でクリップ)
- `class_delta`: 前走とのクラス差 (昇級+/降級−、初出走0)
- `dist_delta_log`: ln(今回距離/前走距離)、`surface_change`: 芝⇔ダ替わりflag
- `venue_change`: 前走と別競馬場flag。**名称は `transport_proxy` 系とし、
  実輸送の観測と誤解される命名をしない** (滞在競馬は区別できない)
- `jockey_change`: 前走と騎手が異なるflag

### 3.3 A8 斤量差分 (raw `kinryo` は既存。**単純なレース内相対斤量
`kinryo_dev` はT13で検証済み・再実装禁止**)

- `kinryo_delta_prev`: 今回斤量 − 前走斤量
- `kinryo_per_weight`: 斤量 / 当日馬体重 (体重欠損時は前走体重)
- `kinryo_delta_x_handicap`: `kinryo_delta_prev` × ハンデ戦flag
- 見習減量・重量種別はrunsから判定可能な範囲のみ (判定不能なら省略し報告)

### 3.4 X3 タイム指数の頑健状態量 (既存 `tfeat` = 直近4走max — 分布・推移が新規)

- `tfeat_rwmean`: recency加重平均 (半減期2走、直近8走)
- `tfeat_std`: 同ウィンドウの標準偏差 (2走未満は0 + flag)
- `tfeat_slope2`: 直近2走の傾き
- `tfeat_gap_from_max`: `tfeat_rwmean` − 現行tfeat (max) — 上振れ一発への頑健化
- `tfeat_post_layoff_delta`: 休養明け初戦だった前走の指数変化
- タイム指数の定義は現行 `tfeat` が使う指数と同一 (新指数を発明しない)

## 4. 評価 (T39プロトコル準拠)

1. **分割**: 学習2021-23 / 選抜2024 (L2とグループ選択のみ、事前固定の小グリッド) /
   2025・2026H1は**historical benchmark** (固定モデルで報告のみ、再調整禁止)
2. **ablation**: 現行CL (baseline) / +A3 / +A7 / +A8 / +X3 / +全部 の6構成。
   グループ内の特徴の出し入れによる探索はしない (グリッドはL2×グループ構成のみ)
3. **指標**: 一次 = 勝馬Log Loss (2024)。副 = Brier、top-1/2/3捕捉率、
   同一集団のWIN5開催日paired (T39 blocks使用)、市場top-kフロア
   (roadmap §8: 市場人気top-kを下回らないこと)
4. **coverage報告**: 特徴ごとの欠損率・flag率を学習/評価期間別に報告
5. 乱数seed固定・再実行SHA一致。実験は上位モデルが事前にT39台帳へ登録してから実行

## 5. テスト

1. **as-of健全性**: 合成馬履歴で、当該レース行・未来行が各特徴に影響しないこと
   (expanding統計が現在行を含まないこと、starts_28dが未来を数えないこと)
2. 前走なし/休養明け/体重欠損/初出走の境界値とflag
3. `kinryo_per_weight` の分母fallback規則
4. **本番非回帰**: パックOFFで `build_dataset` 出力が現行と決定論SHA一致
5. `python -m pytest tests/ -q` 全体パス

## 6. 成果物

- `backtest_feature_pack.py` + テスト
- 数値レポート: 6構成×(2024選抜 / 2025 / 2026H1)の全指標、coverage表、
  選抜されたL2とグループ構成
- 解釈と採否は上位モデル。**採否は「2024で選抜した固定構成」の
  historical benchmark と、その後のprospective確認で行う** (2025/26H1を見て
  構成を変えることは禁止)

## 7. スコープ外

- 本番 `FEATURES`・本番artifactの変更 (採用決定後に別途バンドル)
- 血統 (T19)、騎手/厩舎ローリング (T18)、ペース拡張 (A6)、調教 (T42)
- LightGBM等の非線形モデル (T20はパック採否後)
