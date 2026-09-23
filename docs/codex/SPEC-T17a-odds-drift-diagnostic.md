# SPEC-T17a: 時点別オッズ (30/10/2 分前) のドリフト診断 (D3 先行分析・事前登録)

起票: 2026-09-24 (Fable 5.1)。ロードマップ §4.5 / Phase C の「~800 レースで D3 先行」に対応。**実行はゲート到達後** (下記 §1)。この SPEC は分析定義を蓄積完了前に凍結するためのもの。

## 0. 位置づけ
- 目的: 市場の時間変化 (30→10→2 分前) が **2 分前の市場確率だけでは説明できない勝馬情報** を持つかを診断する。持たなければ T17 の特徴化 (odds_drift 等) は起票しない (負の結果として記録)。持つなら T17 本体 (学習は「同じ発走前時間帯」のデータのみ、確定オッズで学習しない) を起票する。
- 本番変更なし。通知・EV・仮想購入に接続しない。

## 1. ゲート (実行条件)
- `audit_odds_snapshots.py` の clean レース (stage 30/10/2 とも窓内・品質フラグなし) が **800 以上**、かつ結果照合 (race_results) が 95% 以上。2026-09-24 時点: clean 603〜605 / 候補 644、22 開催日。到達見込み 10 月中旬。
- 到達時に **凍結抽出** `outputs/t17a/extract_<YYYYMMDD>.sqlite` (odds_snapshots の stage 30/10/2 行 + race_results + predictions の該当行のみ、read-only 抽出) を作り、その sha256 を台帳に封入する (jra_logging.db は成長するため直接は封印しない)。

## 2. 事前登録 `T17a-odds-drift-diagnostic-v1`
- `benchmark_type`: `prospective` (Phase C 蓄積データ)。`candidate_count`: 3 (下記 (b)(c)(d))。
- 母集団: clean レースのうち 30/10/2 の全 stage が取れ、取消馬を除く全馬に有効オッズがあるもの。人気帯分解 (1-3 / 4-8 / 9+) を安全指標として必須。
- 定義 (馬 i, レース r): `p_s(i) = (1/odds_s(i)) / Σ_j (1/odds_s(j))` (stage s ∈ {30,10,2})。`drift(i) = log(odds_2(i) / odds_30(i))`、`dp(i) = p_2(i) − p_30(i)`、`rank_change(i) = pop_2(i) − pop_30(i)`。
- モデル (レース内 conditional logit、係数はレース横断で共通):
  (a) baseline: `logit ∝ log p_2` (2 分前市場のみ、係数 1 固定 = 市場そのもの)
  (b) `log p_2 + β1·drift`
  (c) `log p_2 + β1·drift + β2·dp + β3·rank_change`
  (d) `log p_2 + β4·(log p_model − log p_30)` — 本番 CL の勝率 (predictions テーブル, 30 分前 cutoff 相当) と 30 分前市場の乖離が「その後の市場の動きと結果」を先読みするか (T63/T62b と同じ問題意識)
- 分割: 開催日を時系列で前 60% (学習) / 後 40% (検証)、加えて開催日 leave-one-out の paired 差。**同一母集団**で (a) との paired 勝馬 LogLoss 差 (開催日 block bootstrap 2000 回、95% CI)。
- 一次指標: 検証期間の `win_logloss_(b) − win_logloss_(a)`。二次: (c)(d) の同差、top-k (k=1..3) が (a) を下回らないこと、人気帯別の符号一致。
- ゲート (all_or_nothing): 一次指標 < 0 で 95% CI 上側 < 0、top-k 非悪化、人気帯 3 帯のうち 2 帯以上で同符号。通過しても **採用ではなく T17 本体の起票根拠**にとどめる。
- stop rule: 母集団が 800 未満、または (a) の市場 LL が同期間の ability.db 確定オッズ市場 LL と 0.02 以上乖離 (データ品質疑い) なら中止。

## 3. 実装 (ゲート到達後に jra-runner/jra-coder)
- `backtest_t17a_drift.py` (read-only、凍結抽出を入力、`outputs/t17a/report.json` + `docs/T17a-drift-report.md`)。既存 `backtest_fold_stats.same_population_metrics` の paired 集計を再利用。
- テスト: 合成データで drift 無情報のとき β1≈0・LL 差≈0、drift に情報を仕込んだとき LL 差 < 0。
- 採否 (T17 本体起票の可否) は上位モデル。
