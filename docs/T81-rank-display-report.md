# T81: 着順表示専用モデルの限定検証 (Stage A / Stage B)

仕様: [SPEC-T81](codex/SPEC-T81-rank-display-limited-validation.md)

状態: **Stage A (監査・隔離実装・合成テスト・登録案) とStage B (歴史評価の1回実行)
が完了。裁定はまだ (`reviewer_adjudication=null`)。** Stage Bは既閲覧の
historical benchmark (2025年・2026年上期) であり、本番採用・三連単精度・
収益改善を証明するものではない。表示・通知・購入への接続は行っていない。

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

## レポート必須の5問 (SPEC SS14) — Stage A時点

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

---

# Stage B: 歴史評価の1回実行

台帳登録: `T81-rank-display-pl3-v1` (`registered_at_utc=2026-09-23T18:16:22+00:00`、
承認manifest = `outputs/t81_rank_display/stage_a/manifest.json`、HEAD
`931c6e1966054c05ad1f76f4946f68d8823e455c` で生成・封印)。実行前の
`python -X utf8 -m eval.ledger verify --no-update-state` は
`OK: 68 experiments, 272956 bytes, sha256=051a13216ed540869ff3ebf0704a9e41cc478f7dc7134eeb40ad7c4f85e6a80d`。

実行コマンド (1回のみ):

```
python -X utf8 backtest_rank_display.py --run-historical \
  --manifest outputs/t81_rank_display/stage_a/manifest.json \
  --registration-id T81-rank-display-pl3-v1 \
  --output-dir outputs/t81_rank_display/run_001
```

開始 `2026-09-23T18:17:38Z` 〜 終了 `2026-09-23T18:19:31Z`、**所要時間 113秒**、
exit code `0`。標準出力に fold ごとの sire カバレッジ (95.5%〜98.9%、いずれも
閾値70%以上で有効) と最終JSONステータス行のみ、標準エラー出力は空。

**ability.db sha256 (実行前後で不変):**
`bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a`。
保護対象資産も実行前後で不変を確認 (`win5_ml_model.json`=`8687f9bf...4527`,
`score_weights.json`=`4ba97650...f318`, `mined_rules.csv`=`c8406af9...71bfb`)。
git HEAD は実行前後で `4bd11cac8c6c47604ea0ea4815e9a32b653089de` のまま
(追跡ファイルの変更なし、`git status --short` で本ハーネス以外の差分なし)。

生成物: `outputs/t81_rank_display/run_001/result.json` (sha256
`9ec3d571d0b9b0891cf2724dc35b112d7e0277be5887953607361ea5abb4439a`)。
台帳向け結果行案は `outputs/t81_rank_display/run_001/result_record.draft.json`
(`superseded_by=T81-rank-display-pl3-v1`、`adjudication=null`、
`registered_at_utc=null`)。

### L2選択 (fold1-3、レース数重み付け race_mean_spearman)

候補 (PL@3, objective=pl_top3): 選択 L2=**0.3** (加重平均 0.545032;
L2=1.0は0.544994、L2=3.0は0.545011で僅差、最大との差は1e-12を超えるため
0.3を採用)。
対照 (再学習top-1, objective=pl_top1): 選択 L2=**3.0** (加重平均 0.535183;
L2=0.3は0.535128、L2=1.0は0.535128)。

| fold | 学習 | 評価 | 適格レース数 | 候補 pl_top3 race_mean_spearman (選択L2=0.3) | 対照 pl_top1 race_mean_spearman (選択L2=3.0) |
|---|---|---|---:|---:|---:|
| fold1 | 2021 | 2022 | 2,368 | 0.551392 | 0.540674 |
| fold2 | 2021-2022 | 2023 | 2,283 | 0.537829 | 0.526582 |
| fold3 | 2021-2023 | 2024 | 2,248 | 0.545646 | 0.538135 |
| 加重平均 (3fold, race数重み) | — | — | 6,899 | **0.545032** | **0.535183** |

全同値レース (all_tied_races) は候補・対照ともに各foldで0件。
sire_pts fold gate は fold1-3ともcoverage 100%・閾値70%で有効。

最終fit: 両方式とも2021-01-01〜2024-12-31 (適格6,899レース) で固定係数・
標準化器をfit。sire gate final coverage 99.996% (閾値70%、有効、対象馬26,044頭)。
以降、2025年→2026年上期の順に、この固定係数を再学習せずに評価。

### 主指標: race_mean_spearman 差 (candidate − comparator)、開催日block paired bootstrap (seed=81, 10,000回)

| 期間 | 適格レース数 | 開催日数 | 対市場 差 (点推定) | 対市場 95% CI | 対市場 p | 対top-1 差 (点推定) | 対top-1 95% CI | 対top-1 p |
|---|---:|---:|---:|---|---:|---:|---|---:|
| 2025 | 2,214 | 109 | +0.005584 | [+0.003855, +0.007263] | 0.0002 | +0.007941 | [+0.006289, +0.009638] | 0.0002 |
| 2026H1 | 1,086 | 53 | +0.003471 | [+0.000782, +0.006175] | 0.0128 | +0.008280 | [+0.005997, +0.010591] | 0.0002 |

いずれも点推定は正、2025年はCI下限も0超、2026年上期もCI下限が0超 (参考、
ゲートには使わない規約)。**Spearmanのゲート条件は両期間・両対照で成立。**

### 上位馬取りこぼし補助指標 (candidate − comparator)

| 期間 | top3_set_recall差 対市場 | top3_set_recall差 対top-1 | actual_top3_rank_mae差 対市場 | actual_top3_rank_mae差 対top-1 |
|---|---:|---:|---:|---:|
| 2025 | +0.000753 [-0.003228, +0.004800] | +0.001506 [-0.002387, +0.005270] | -0.000876 [-0.001973, +0.000216] | -0.000680 [-0.001660, +0.000290] |
| 2026H1 | **-0.002455** [-0.009120, +0.004345] | **-0.000921** [-0.006938, +0.005071] | **+0.000572** [-0.001143, +0.002295] | **+0.000137** [-0.001284, +0.001528] |

2025年は4指標すべて条件を満たす (recall差 ≥0、mae差 ≤0)。
**2026年上期は4指標すべて条件不成立** (recall差が両対照で点推定 <0、
mae差が両対照で点推定 >0)。SPEC SS9はこの4条件を点推定のみで判定し
(CIはゲートに使わない規約)、2026年上期のこの逆転が主判定を不通過に
している。top3_order_exact (参考指標、ゲート対象外) は今回result.jsonに
未集計 (result_summaryのスキーマにこの参考値は含まれない。詳細は
「Stage Bで判明した逸脱」参照)。

### 勝馬側参考指標 (候補 / top-1対照 / 市場)

**全適格レース**

| 期間 | 方式 | n | winner_in_top1 | winner_in_top2 | winner_in_top3 | winner_logloss |
|---|---|---:|---:|---:|---:|---:|
| 2025 | 候補 pl_top3 | 2,214 | 0.32746 | 0.52981 | 0.65583 | 1.95911 |
| 2025 | top-1対照 | 2,214 | 0.32791 | 0.52891 | 0.65763 | 1.94156 |
| 2025 | 市場 | 2,214 | 0.33243 | 0.53162 | 0.66396 | 1.94229 |
| 2026H1 | 候補 pl_top3 | 1,086 | 0.32781 | 0.50000 | 0.63076 | 1.97170 |
| 2026H1 | top-1対照 | 1,086 | 0.33333 | 0.50368 | 0.62983 | 1.95362 |
| 2026H1 | 市場 | 1,086 | 0.32689 | 0.50460 | 0.63444 | 1.95402 |

**9R以降部分集合 (参考、主母集団のWIN5条件を暗黙適用しない旨の別分母)**

| 期間 | 方式 | n | winner_in_top1 | winner_in_top2 | winner_in_top3 | winner_logloss |
|---|---|---:|---:|---:|---:|---:|
| 2025 | 候補 pl_top3 | 945 | 0.30159 | 0.50370 | 0.62222 | 2.03953 |
| 2025 | top-1対照 | 945 | 0.30899 | 0.50476 | 0.62116 | 2.02620 |
| 2025 | 市場 | 945 | 0.30794 | 0.50159 | 0.63069 | 2.02693 |
| 2026H1 | 候補 pl_top3 | 492 | 0.29268 | 0.47561 | 0.59756 | 2.03489 |
| 2026H1 | top-1対照 | 492 | 0.31098 | 0.48171 | 0.60366 | 2.01290 |
| 2026H1 | 市場 | 492 | 0.31301 | 0.48577 | 0.59959 | 2.01693 |

候補pl_top3は両期間・両分母で市場・top-1対照よりわずかにwinner_logloss
が高い (勝馬確率としては劣る)。winner_in_top1も候補が両対照より低い
場合が多い (2025年top-1対照比 -0.00045pt、2026H1市場比 -0.00092pt、
top-1対照比 -0.00552pt)。**winner_in_top3はtop3_set_recallと異なる指標で
あり、PL@3のsoftmaxを校正済み勝率として本番へ渡さない (SPEC SS8)。**

### 機械判定

**`HISTORICAL_SCREEN_FAIL`** (result.json記載、本ハーネスの
`adjudicate_*`ロジックで算出。手計算でも再確認)。

根拠: SPEC SS9固定ゲート条件は全条件ANDで、2026年上期の
top3_set_recall差 (対市場 -0.002455 <0、対top-1 -0.000921 <0) と
actual_top3_rank_mae差 (対市場 +0.000572 >0、対top-1 +0.000137 >0) が
必要な点推定方向 (recall差≥0、mae差≤0) を満たさない。これは
「必要な点推定の方向が不成立」に該当し、`HISTORICAL_SCREEN_FAIL`
(`INCONCLUSIVE`ではない。`INCONCLUSIVE`は2025年のCIが0を跨ぐ場合限定の
規約で、2025年のCIはいずれも0を跨いでいない)。品質エラー・封印不一致・
非収束はなく`INVALID`にも該当しない。

### 最適化の収束

候補 (pl_top3) ・対照 (pl_top1) とも、L2 3水準 × fold3本 (計6+6=12) と
最終fit2本 (2021-2024) の合計14回のPL fitがすべて完了し、result.jsonに
非収束エラーは記録されていない。本ハーネスは `scipy.optimize.minimize`
の `result.success` が偽、または係数が非有限の場合に `QualityError` を
発生させ非zero exitで停止する実装 (`backtest_rank_display.py` 671-679行)
であり、今回はexit code 0でresult.jsonが完全に書き出されたため、
**全fitが収束 (result.success=True、有限係数) したことをコードパスから
確認**した (result.json自体には`success`フラグの明示フィールドはない)。

### 再現性 (seed 81)

不確実性推定は `eval.blocks.paired_block_bootstrap` を開催日block
(同日全場を1 block)・10,000回・**seed=81**で実行し、各比較のCI出力に
`"seed": 81` が記録されている。SPEC SS3の「実行は1回のみ」の規律に従い、
本Stage Bの `--run-historical` を再実行して結果の再現一致は確認して
いない (再実行するとStage B自体の一度性の規律に反するため)。同じ
コードパスの決定性は、Stage Aの合成`--smoke`再実行テスト
(`test_smoke_rerun_matches_within_tight_tolerance`, 許容誤差1e-10) で
既に検証済み。

### レポート必須5問への回答 (SPEC SS14、Stage B)

1. **全頭順位は市場・再学習top-1の両方より近づいたか。**
   Yes、両期間・両対照で race_mean_spearman の点推定は候補が上回った
   (2025: 対市場+0.005584・対top-1+0.007941。2026H1: 対市場+0.003471・
   対top-1+0.008280)。2025年はCI下限も0超。2026年上期もCI下限が0超だが
   SS9の規約でこの期間のCIはゲートに使わない。
2. **改善が下位馬だけに偏り、上位3頭を外すようになっていないか。**
   2025年は偏っていない (top3_set_recall差・mae差とも両対照で改善方向
   ≥0/≤0)。**2026年上期は上位3頭側が悪化方向に転じている**
   (top3_set_recall差が両対照で点推定<0、actual_top3_rank_mae差が両対照
   で点推定>0)。すなわち2026年上期に限り、全体順位の改善が上位3頭の
   犠牲と引き換えになっている可能性を示すデータであり、SS9のゲートが
   これを検出して不通過にしている。
3. **勝馬を当てる能力とのトレードオフはどの程度か。**
   候補pl_top3のwinner_logloss は両期間・両分母で市場・top-1対照より
   悪化 (例: 2025年全適格 1.95911 vs top-1対照1.94156・市場1.94229;
   2026H1全適格1.97170 vs top-1対照1.95362・市場1.95402)。winner_in_top1
   も概ね劣る。悪化幅は小さいが一貫して候補が最下位。T43の「勝馬用途に
   は不採用」判断は本結果でも裏付けられ、着順表示専用に用途を限定する
   前提から逸脱していない。
4. **どの母集団にしか適用できないか。**
   Stage A監査のとおり、JRA中央競馬・芝ダ平地・1R〜12R・8頭以上・
   全馬特徴量が生成でき確定単勝オッズが全馬揃うレースに限定 (完全フィー
   ルド適格率 64-69%、年別に大差なし)。新馬混在・取消/除外/同着を含む
   レース・頭数7以下は対象外。歴史反証フィルタとして確定オッズを使用
   しており、発走前に購入可能だった条件の評価ではない。
5. **歴史足切りは通ったか。**
   **通っていない (`HISTORICAL_SCREEN_FAIL`)。** 2026年上期の上位3頭
   補助指標の点推定方向が不成立のため。これは実装・数値再現の失敗では
   なく、SPEC SS9が事前に固定した歴史足切り条件そのものの不成立であり、
   本番採用・三連単精度・収益改善の証明ではない (そもそも通過しても
   将来検証の起票候補にとどまる契約)。

## Stage Bで判明した逸脱・懸念点 (レビュー担当への報告事項)

1. **`--run-historical`はSPEC SS14が列挙する成果物のうちresult.jsonのみ
   を出力する。** `race_metrics.jsonl`・`predictions.jsonl`・
   `selected_candidate.json`・`selected_top1_control.json` は、
   `backtest_rank_display.py`の`main()`の`run-historical`分岐
   (該当行約1697-1707) が`result.json`以外を書き出さない実装のため
   生成されなかった。これはStage Aで審査・受理済みのハーネス実装その
   ものの範囲であり、Stage B実行によって初めて表面化した仕様と実装の
   ギャップである。result.jsonには集計後の数値 (fold別・期間別の
   スカラー) のみが含まれ、レース別・馬別の生データや学習済み係数・
   標準化器は含まれないため、これら4ファイルをresult.jsonから事後的に
   再構成することはできない。「実行は1回のみ・候補やハーネスを結果を
   見て変更しない」という規律のもと、ハーネスへの追記や再実行はせず、
   欠落をそのまま報告する。対応 (ハーネス修正・再実行の要否) はレビュー
   担当の判断を待つ。
2. `top3_order_exact` (SPEC SS8の参考指標、ゲート対象外) はresult.jsonの
   `comparisons.*`スキーマに含まれず、今回報告できない。上記1と同根の
   スキーマ範囲の限界。
3. §6の除外理由内訳・年別/頭数帯別適格率は、Stage A監査 (`--audit-only`、
   対象2021-01-01〜2026-06-30) の数値を上記「Stage A監査結果」節で既に
   報告済みで、Stage Bの`run-historical`はこれを再集計・再出力しない
   (`result.json`にはn_races/n_day_blocksのみ)。2025年・2026年上期の
   適格レース数 (2,214 / 1,086) はStage A監査の年別表と一致する。
4. Stage Bは登録済み台帳の内容と実行時のmanifest/入力SHAの不一致検出で
   停止することなく、正常終了した。台帳・manifest・入力資産・保護対象
   資産のいずれにも不整合はなかった。


## レビュー担当の裁定 (2026-09-24, Fable 5.1)

- 裁定: **REJECTED (歴史足切り不通過)**。machine_judgment=HISTORICAL_SCREEN_FAIL を事前登録規則どおり採用。
- 全頭の並び (race_mean_spearman) は両期間・両対照で有意に改善 (2025 対市場 +0.0056, 対top1 +0.0079; 2026H1 対市場 +0.0035, 対top1 +0.0083、95% CI 下限 > 0)。
- 上位3頭指標は 2026H1 で点推定が僅かに悪化 (recall 対市場 −0.0025 / 対top1 −0.0009、MAE +0.0006 / +0.0001、CI は 0 を跨ぐ) → AND 条件不成立。事後の許容誤差は設けない。
- 勝馬側は候補が一貫して劣り、T43 の勝率用途不採用を再確認。
- 実装ギャップ (per-race / predictions / selected_* の未出力) は裁定に影響しないが、再実行案件では先に修正・再登録が必要。
- 本番・表示・通知・購入への接続なし。再訪は上位3頭の非劣性マージンを事前定義した別 SPEC・別登録のみ。
