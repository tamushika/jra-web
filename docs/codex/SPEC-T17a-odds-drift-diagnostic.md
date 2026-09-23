# SPEC-T17a v2: 時点別オッズ (30/10/2 分前) のドリフト診断 (D3 先行分析・事前登録)

起票: 2026-09-24 v1 (Fable 5.1) → 同日 v2 (実行前監査 [T17a-pre-audit-review.md](../T17a-pre-audit-review.md) を反映)。
ロードマップ §4.5 / Phase C の「~800 レースで D3 先行、2,000 レースで勝馬増分」の前段。
**実行はゲート到達後。本 SPEC は分析定義を蓄積完了前に凍結するためのもの。**

## 0. 位置づけ
- 問い: 市場の時間変化 (30→2 分前) は **2 分前の市場確率だけでは説明できない勝馬情報** を持つか。
- 持たなければ T17 本体 (ドリフト特徴の CL への追加) は起票しない (負の結果)。持てば T17 本体を別 SPEC で起票する (学習は同じ発走前時間帯のデータのみ。確定オッズで学習しない。CL の ln_odds を同時点に差し替えて再採点する基盤が前提)。
- 本番変更なし。通知・EV・仮想購入・T63/T62b/T70 の契約に接続しない。
- 段階: Stage A (監査・隔離ハーネス・合成テスト・凍結抽出手順・登録案) → レビュー担当が登録 → Stage B (固定評価を 1 回) → 裁定。T81 と同じ規律。

## 1. ゲート (Stage B の実行条件)
- 厳密 clean レース (§3.1) が **800 以上**。2026-09-24 時点 570 (20 開催日)。到達見込み 10 月中〜下旬。
- 到達時に **凍結抽出** `outputs/t17a/extract_<YYYYMMDD>.sqlite` を作り (odds_snapshots の stage 30/10/2/15/5 行・race_results・predictions の stage 30 相当行・races、read-only 抽出、書込み先は新規ファイルのみ)、その sha256 を台帳に封入。以降の蓄積は混ぜない。

## 2. 事前登録 `T17a-odds-drift-diagnostic-v1`
- `benchmark_type`: `prospective` (2026-07-12 以降の蓄積。ability.db は使わない。ability.db の 2026H2 未使用制約とは別枠であることを notes に明記)。
- `candidate_count`: 3 ((b)(c)(d))。対照 (a) は係数固定で候補数に含めない。
- `primary_metric`: `win_logloss_delta_b_minus_a_validation` (検証期間、レース等重み、開催日 block bootstrap 2,000 回、seed 17)。
- `search_grid`: なし (L2 なし最尤 1 回。ハイパーパラメータ探索禁止)。
- `stop_rule`: 母集団 < 800 / 凍結抽出 sha 不一致 / 発走後取得の混入 / 結果集合不一致 / 非収束 → INVALID で停止。結果を見て候補・母集団・期間・指標を変えない。
- `data_hashes`: 凍結抽出・SPEC・ハーネス・manifest の sha256。
- `prospective_start_date`: 2026-07-12。

## 3. 定義

### 3.1 母集団と除外 (行単位で機械判定、優先順で主理由 1 つ + 全フラグ)
1. JRA 平地 (芝/ダート)、`field_size ≥ 8`、stage 30/10/2 の取得がある。
2. 各 stage につき「品質フラグ無し・`seconds_to_post ∈ [stage×60−60, stage×60+120]`・`valid_odds_count = field_size`」の取得を 1 つ採用 (複数なら発走に最も近いもの)。無ければレース除外。
3. `seconds_to_post ≤ 0` (発走後取得) は無条件除外。
4. 結果: 全馬に `finish_position` があり集合が {1..n}。結果行数 ≠ `field_size` (30 分前以降の取消・除外)、同着は除外。市場確率の再正規化による救済はしない。
5. 除外理由の優先順: 取得欠落 → 窓外 → フラグ → オッズ不完全 → 結果不完全。年月・場・頭数帯 (8-11 / 12-15 / 16+) 別の対象率を報告。

### 3.2 特徴とモデル
- `p_s(i) = (1/odds_s(i)) / Σ_j (1/odds_s(j))`, s ∈ {30, 10, 2}。odds ≤ 1.0・非有限は品質エラー。
- `drift(i) = log(odds_2(i)/odds_30(i))`, `dp(i) = p_2(i) − p_30(i)`, `rank_change(i) = pop_2(i) − pop_30(i)`。`drift_late = log(odds_2/odds_10)` と 15/5 分前は記述統計のみ。
- レース内 conditional logit (係数は全レース共通、L-BFGS-B、収束必須):
  (a) `log p_2` 係数 1 固定 (対照)
  (b) `log p_2 + β1·drift`
  (c) `log p_2 + β1·drift + β2·dp + β3·rank_change`
  (d) `log p_2 + β4·(log p_model,30 − log p_30)` — `p_model,30` は stage 30 の再解析 (predictions) の勝率。予測時刻と stage 30 取得時刻の差 ≤ 90 秒で対応付け。対応不能レースは (d) の母集団からのみ外し件数を報告。**CL は ln_odds を含むため市場独立ではない。(d) は「同時刻オッズを見たモデルの残差が、その後の市場の動きと結果を先読みするか」の診断に限定**。

### 3.3 情報時点
- 特徴は stage 取得値のみ。`observed_at < scheduled_post_at` を行単位 assert。ラベルは `result_fetched_at > scheduled_post_at` を assert。
- 分割: 開催日を時系列で前 60% (係数推定) / 後 40% (検証)。検証期間の係数固定。加えて開催日 leave-one-out の paired 差を報告。

### 3.4 指標
- 一次: 検証期間 `win_logloss_(b) − win_logloss_(a)`、95% CI、p。
- 二次: (c)(d) の同差、top-k (k=1..3) の (a) 比 (非悪化)、人気帯 (1-3 / 4-8 / 9+) 別の差の符号、`win_logloss_(市場30) − (市場10) − (市場2)` の時間推移 (記述)、**日次平均 ΔLL の標準偏差 `SD_day`** (T17 本体の件数設計用)。

### 3.5 機械判定
- `HISTORICAL_SCREEN_PASS`: 一次指標 < 0 かつ 95% CI 上側 < 0、top-k 非悪化、人気帯 3 帯中 2 帯以上で同符号。→ T17 本体の起票根拠 (採用ではない)。
- `HISTORICAL_SCREEN_FAIL`: 一次指標の点推定 ≥ 0。
- `INCONCLUSIVE`: 点推定 < 0 だが CI が 0 を含む。
- `INVALID`: §2 stop_rule。
- 機械判定とレビュー担当の裁定は別フィールド。

## 4. 必要件数
- D3: 800 clean レース (≈ 25〜28 開催日)。最小実用差は置かない (探索診断)。
- T17 本体: `N_days ≥ (1.96 × SD_day / (MDE/2))^2`、`MDE` は D3 の結果を見る前に決める (候補 0.005)。ロードマップの 2,000 レースは仮置きで、D3 の `SD_day` 実測で置き換える。現在のペース (10 月以降 週 70 レース) では 2027 年 2〜3 月。

## 5. 実装 (Stage A、ゲート到達前に着手可)
- `backtest_t17a_drift.py`: `--audit-only` (件数・除外・時点 assert・sha、実データでスコア計算なし) / `--smoke` (合成データのみ) / `--run` (登録 ID と凍結抽出 sha の一致が無ければ即停止) の排他 3 モード。read-only 接続、出力は指定ディレクトリのみ。
- `tests/test_t17a_drift.py`: 合成データで (1) drift 無情報なら β1≈0・ΔLL≈0、(2) 情報を仕込めば ΔLL<0、(3) 発走後取得・窓外・取消の除外、(4) 4 機械判定、(5) block bootstrap の再現性 (seed 17)、(6) (d) の対応付け規則。
- 成果物: `outputs/t17a/stage_a/{audit.json, manifest.json, registration.draft.json}`、`docs/T17a-drift-report.md`。
- 採否 (T17 本体起票の可否) は上位モデル。

## 6. やらないこと
- ゲート到達前の実データでの係数推定・LL・相関 (smoke 含む)。台帳の正式登録 (Stage A は登録案まで)。本番・通知・他契約の変更。2,000R 段階の設計確定 (D3 の SD_day 実測後)。
