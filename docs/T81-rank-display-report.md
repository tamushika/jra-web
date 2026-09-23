# T81: 着順表示専用モデルの限定検証 (Stage A)

仕様: [SPEC-T81](codex/SPEC-T81-rank-display-limited-validation.md)

状態: **Stage A (監査・隔離実装・合成テスト・登録案) のみ完了。本データでの学習・
性能評価はまだ実施していない。** 本節の数値は監査・テスト結果であり、
歴史足切り (`HISTORICAL_SCREEN_PASS` 等) の判定材料ではない。

2026-09-24、レビュー担当 (Fable 5.1) がハーネス・68件のテスト・監査を受理し、
実装担当が提示した4つの解釈判断 (sire_pts保守的AND・除外7段階順序・INCONCLUSIVE
のCI適用範囲2025限定・top3_recall/mae厳密比較) と `--audit-only` の所要時間を承認
した。承認に伴い `registration.draft.json` に `notes` (候補内訳・fold定義・L2選択
規則・tie規約・bootstrap・SS9ゲート条件・as-of制約要約・「歴史反証であり完全リー
クフリー/未見OOSではない」の明記) と、`data_hashes` への
`manifest_sha256` / `venue_standard_times_json` / `track_variants_json` /
`score_weights_json` / `criteria_csv_bundle` / `production_model_sha256` を追加した
(本節末尾の監査結果は追加後に再実行して更新済み)。台帳への登録・コミットは未実施。

## 実装物

- `backtest_rank_display.py` — 隔離ハーネス (`--audit-only` / `--smoke` / `--run-historical`)
- `tests/test_rank_display.py` + `tests/fixtures/t81_rank_display/raw_population_cases.json`
- `outputs/t81_rank_display/stage_a/audit.json`, `manifest.json`, `registration.draft.json`
- `outputs/t81_rank_display/smoke/smoke_result.json` (合成データのみ、実DB非接続)

## Stage A 監査結果 (実 ability.db・読み取り専用)

実行: `python -X utf8 backtest_rank_display.py --audit-only --db ability.db --output-dir outputs/t81_rank_display/stage_a`

- ability.db sha256 (実行前後で不変を確認): `bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a`
- pedigree_cache.json sha256: `452a322ec6ec2a92f8dd9e1da3f7c05f4801780987ad2ff35c1f9aa3a95afb50`
- 対象期間: 2021-01-01 〜 2026-06-30 (raw行 402,034 / 期間内レース候補 18,905)
- **完全フィールド適格レース: 12,558 (適格率 66.4%)**

### 年別適格率

| 年 | 適格 | 全体 | 適格率 |
|---|---:|---:|---:|
| 2021 | 2,359 | 3,456 | 68.3% |
| 2022 | 2,368 | 3,456 | 68.5% |
| 2023 | 2,283 | 3,456 | 66.1% |
| 2024 | 2,248 | 3,454 | 65.1% |
| 2025 | 2,214 | 3,455 | 64.1% |
| 2026 (上期) | 1,086 | 1,628 | 66.7% |

年別の傾き（68%→64%台へのわずかな低下）は主に近年の出走頭数増減・新馬混入率の
変動によるもので、除外理由の内訳（下表）に大きな年次シフトはない。

### 除外の主理由 (排他的、SS6の優先順)

| 主理由 | 件数 |
|---|---:|
| feature_row_mismatch (特徴が全馬揃わない: 新馬等の履歴なし馬を含む) | 3,732 |
| raw_duplicate_or_row_mismatch (raw行数≠total_horses、馬番/馬名重複) | 1,766 |
| headcount_invalid (n<8 または total_horses不整合) | 435 |
| rank_not_strict_1_to_n (同着・取消・除外・着順欠番) | 414 |

### 全理由フラグ (主理由以外も含む、重複計上あり)

| フラグ | 件数 |
|---|---:|
| feature_row_mismatch | 5,681 |
| raw_duplicate_or_row_mismatch | 2,201 |
| rank_not_strict_1_to_n | 2,182 |
| missing_or_invalid_odds | 1,529 |
| headcount_invalid | 435 |

`missing_or_invalid_odds` が主理由になった件数は0（他理由に必ず先取りされる）だが、
全理由フラグでは1,529件で単独にも該当しうる。venue/surface別・頭数帯別の分割表は
`outputs/t81_rank_display/stage_a/audit.json` の `eligible_rate_by_venue` /
`eligible_rate_by_surface` / `eligible_rate_by_field_band` を参照。

### 同日重複出走監査 (SPEC SS5)

`same_day_duplicate_horse_history_violations`: **0件** (同一馬が同日に複数行を持つ
ケースは検出されなかった)。

### sire_pts fold gate (SPEC SS5、厳格・fold限定の再計算)

| fold | 学習期間 | coverage | 閾値 | 有効 | 対象馬数 |
|---|---|---:|---:|---|---:|
| fold1 | 2021 | 100.0% | 70% | 有効 | 11,526 |
| fold2 | 2021-2022 | 100.0% | 70% | 有効 | 16,322 |
| fold3 | 2021-2023 | 100.0% | 70% | 有効 | 21,133 |
| final | 2021-2024 | 99.996% | 70% | 有効 | 26,044 |

pedigree_cache.json のカバレッジは監査時点で非常に高く、全foldで有効判定。
実装は `backtest_ml.build_dataset` 自身が計算する（リークし得る）年次gateとの
**保守的AND** でsire_ptsを0に落とす（詳細は「登録前にレビューが判断すべき事項」）。

### as-of 制約一覧 (`as_of_limitations`)

`audit.json.as_of_limitations` に機械可読形式で格納。要点:

1. `venue_standard_times.json` の集計期間は2016-2023固定。2022/2023 foldの
   学習期間終了後の情報を含む既知の制約（本SPEC対象外、是正は別SPEC）。
2. `track_variant` 補正も同じ固定基準タイムに依存。
3. `grade_pts` (criteria.csv) は現行ルール条件を全期間に適用。自動重み
   (`analysis._criteria_weights_cache`) は無効化済みだが、ルール条件自体の
   制定時点as-ofは未保証。
4. sire_pts gate は上記の保守的AND方式（本ハーネス側の解釈、レビュー要）。
5. pedigree_cache.json 自体の鮮度（評価日時点でのas-of）は追跡していない。

### 依存関係・副作用監査

`audit.json.dependency_and_side_effect_audit` に記録。要点:

- `pedigree_store.load_all` は本ハーネス実行中、検証済みローカルJSONを返す
  fail-closedアダプタに置き換え。Neonフォールバック
  (`past_data_service.get_db_connection` / psycopg2 / `DATABASE_URL`) への
  到達経路はテストで到達不能を確認済み（`test_isolated_pedigree_adapter_never_touches_network`）。
- `fold_stats.FoldFactorTableProvider` は `mode=ro` URIで内部接続（`PRAGMA query_only`
  は付与されていないが、`mode=ro` 自体がSQLite VFS層で書込みを拒否するため実害なし。
  共通コードは変更していない・観察のみ）。
- 本ハーネス自身の ability.db 接続は `mode=ro` + `PRAGMA query_only=ON`。
  書込み試行が実際に拒否されることをテストで確認 (`test_readonly_connection_rejects_writes`)。
- 保護対象資産 (`win5_ml_model.json`, `score_weights.json`, `mined_rules.csv`,
  ability.db, pedigree_cache.json) のSHA-256は実行前後で不変を確認済み。

## テスト結果

実行:

```
PYTHONUTF8=1 python -X utf8 -m pytest tests/test_rank_display.py tests/test_rank_objective.py -q
  -> 68 passed
PYTHONUTF8=1 python -X utf8 -m pytest tests -q --ignore=tests/fixtures
  -> 1260 passed, 2 skipped
```

既存の失敗・skipなし（環境メモにある既知cp932失敗3件は `PYTHONUTF8=1` で解消済み、
本実行でも再現しなかった）。全体回帰にネットワーク・本番DB書込みを行う既存テストは
確認されず、隔離の必要はなかった。

### SPEC SS13 必須テスト対応表

| # | 要件 | テスト |
|---|---|---|
| 1 | Spearman: 正順=1・逆順=-1・同値=平均順位・全同値=0+件数 | `test_spearman_forward_and_reverse_orderings`, `test_spearman_ties_use_average_rank`, `test_spearman_all_tied_scores_returns_zero_with_flag` |
| 2 | winner_in_top3 と top3_set_recall の別fixture、MAE手計算一致 | `test_winner_in_top3_and_top3_set_recall_differ`, `test_actual_top3_rank_mae_matches_hand_calculation`, `test_top3_order_exact_requires_full_order_match` |
| 3 | 同点予測順位=馬番決定、行順不変 | `test_display_order_ties_broken_by_umaban_and_is_row_order_invariant` |
| 4 | raw段階の同着・中止・取消・重複・非整数rank・連番欠け・頭数不整合を除外 | `test_classify_raw_race_flags_each_bad_case` (8ケース parametrize), `test_classify_raw_race_accepts_the_good_case` |
| 5 | 勝馬はいるが他馬特徴欠損 → 完全フィールドとして通さない | `test_incomplete_feature_field_is_excluded_even_with_a_winner_present` |
| 6 | オッズ欠損/1.0/NaN除外、人気列とオッズ順位が違う場合はオッズ優先 | `test_missing_or_degenerate_odds_are_excluded`, `test_market_order_uses_odds_not_popularity_when_they_disagree` |
| 7 | PL top-1/PL@3の数値勾配一致 (1e-5)、stage重み合計1 | `test_pl_gradient_matches_central_difference`, `test_stage_weights_sum_to_one` |
| 8 | scalerに評価行を含めない、将来行/gate追加が過去に影響しない | `test_fit_pl_model_scaler_uses_only_the_rows_it_is_given`, `test_strict_sire_gate_ignores_horses_outside_its_own_train_window`, `test_apply_sire_gate_only_ever_removes_information` |
| 9 | 全指標が同一レース/馬集合、1-8Rが主指標・9R以降は別表 | `test_race1_through_8_are_in_the_primary_population_not_only_9_plus`, `test_compare_period_requires_identical_race_populations` |
| 10 | 開催日block対応、レース等重み、同seed再現性 | `test_blocks_by_date_groups_all_venues_of_one_day_together`, `test_paired_metric_diff_is_race_equal_weighted_and_seed_reproducible`, `test_paired_metric_diff_raises_when_populations_differ` |
| 11 | 非収束・manifest/台帳不一致・ネットワークfallback・保護書込みがfail-closed | `test_fit_pl_model_raises_on_non_convergence`, `test_run_historical_stops_without_a_matching_ledger_registration`, `test_run_historical_stops_on_manifest_sha_mismatch`, `test_run_historical_stops_on_ledger_manifest_design_mismatch`, `test_isolated_pedigree_adapter_never_touches_network`, `test_validate_pedigree_cache_fails_closed_on_missing_or_invalid_file`, `test_readonly_connection_rejects_writes`, `test_assert_connection_is_readonly_detects_a_writable_connection` |
| 12 | 合成smoke再実行が1e-10以内で一致 | `test_smoke_rerun_matches_within_tight_tolerance` |
| 13 | quality/direction/CI条件の4機械判定が正しい | `test_adjudicate_invalid_overrides_everything`, `test_adjudicate_pass_when_all_points_and_2025_ci_are_positive`, `test_adjudicate_fail_when_a_point_direction_is_wrong`, `test_adjudicate_inconclusive_when_2025_ci_crosses_zero`, `test_adjudicate_top3_recall_or_mae_violation_fails` |

合成smokeの機械判定は `HISTORICAL_SCREEN_FAIL`（意図的に混合結果を出す合成データの
ため。パイプラインの結線確認のみが目的で、性能主張ではない）。

## レポート必須の5問 (SPEC SS14) — Stage Aでは未評価

1. 全頭順位は市場・再学習top-1の両方より近づいたか — **未評価 (Stage B待ち)**
2. 改善が下位馬だけに偏り、上位3頭を外すようになっていないか — **未評価 (Stage B待ち)**
3. 勝馬を当てる能力とのトレードオフはどの程度か — **未評価 (Stage B待ち)**
4. どの母集団にしか適用できないか — 上記「完全フィールド適格率」参照。JRA中央競馬・
   芝ダ平地・1R〜12R・8頭以上・全馬特徴/確定単勝オッズが揃うレースに限定され、
   新馬混在レース・取消/除外/同着を含むレース・頭数7以下のレースは対象外。
5. 歴史足切りは通ったか — **未実施 (Stage B待ち)**。本節の数値はすべて監査・テスト
   結果であり、歴史足切りの判定材料ではない。

## 登録前にレビューが判断すべき事項

実装担当としての報告メッセージ本文（SubagentHandbackで返却）に、SPECが「レビューへ
返す」と定めた点・仕様の曖昧点・自分が置いた解釈判断をまとめて記載した。
