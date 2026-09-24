# T17a: 時点別オッズドリフト診断 (Stage A)

仕様: [SPEC-T17a v3](codex/SPEC-T17a-odds-drift-diagnostic.md) / [事前監査レビュー](T17a-pre-audit-review.md)

状態: **Stage Aのみ完了 (監査・隔離ハーネス・合成テスト・凍結抽出手順・登録案)。
実データでの係数推定・LogLoss・相関・機械判定は未実施。台帳への登録・Stage B・
裁定は未着手。** 本番・通知・EV・仮想購入・T63/T62b/T70への接続は行っていない。

## 1. 実装物

- `backtest_t17a_drift.py` — 隔離ハーネス。`--audit-only` / `--smoke` / `--run` の
  排他3モード、および別枠の `--freeze-extract` 手順。
- `tests/test_t17a_drift.py` — 合成データのみの38テスト (fixtureファイルは無く、
  すべてハーネス内の合成データ生成関数で自己完結)。
- `outputs/t17a/stage_a/audit.json` — 実DB (`data/jra_logging.db`, 読み取り専用)
  に対する監査結果。件数・除外理由・時点assert結果のみで、スコア・LogLoss・
  相関は一切含まない。
- `outputs/t17a/stage_a/manifest.json` — 実行環境・sha256・監査要約。
- `outputs/t17a/stage_a/registration.draft.json` — 正式登録前の登録案
  (`eval.ledger.validate_experiment` によるファイルI/O無しの検証済み、台帳へは
  未追記)。

## 2. Stage A 監査結果 (実 `data/jra_logging.db`、読み取り専用)

実行: `python -X utf8 backtest_t17a_drift.py --audit-only --out outputs/t17a/stage_a/audit.json`

母集団の再集計はSPEC v3 §3.1の機械判定をゼロから独立実装したものであり、
2026-09-24事前監査レビューの570R (取得品質側のみ) を流用・加工していない。
570という数値が今回の`capture_quality_pass_count`と偶然一致した (下表) ことは、
2つの独立実装が同じ取得品質定義に収束したという交差検証であって、570の再利用
ではない。

### 2.1 件数の推移 (排他的な段階)

| 段階 | 件数 |
|---|---:|
| DB内の全レース | 680 |
| うち JRA平地 (芝/ダート) | 680 |
| 平地かつ30/10/2分前いずれかの取得試行が全stage揃う (構造上の対象) | 616 |
| 平地だが30/10/2のいずれかのstageで取得試行が皆無 (対象外、構造上除外) | 64 |
| 対象のうち頭数8未満または頭数不明 (適用対象外、除外理由には数えない) | 21 (旧版22。§8.1参照) |
| 適用対象 (採用取得のheadcount>=8) | 595 (旧版594。§8.1参照) |
| **取得品質通過 (各stageで窓内・フラグ無し・オッズ完全な取得が1つ選べた)** | **570** |
| 取得品質通過のうち、結果/馬集合の完全一致チェックで追加除外 | 86 |
| **最終適格レース数 ((a)(b)(c)共通、時点assert通過)** | **484 (変化なし)** |
| 最終適格の開催日数 | 20 |

- `final_eligible_count = 484` < 800 につき、**Stage Bのゲート未到達**。
  `gate_800_shortfall = 316`。
- 570→484の86件減少は、旧監査が示していた「結果行数不一致74レース」とは別集計
  (v3の要求どおり、74と570の単純差分では算出していない)。86件には行数不一致に
  加え、頭数一致でも馬集合が食い違うケースや着順が{1..n}の厳密な順列でない
  ケースも含まれる。内訳は`audit.json`の`exclusion_primary_reason`
  (**v3 §3.6-1適用後**: `odds_incomplete: 11`, `outside_window: 25`,
  `result_incomplete: 75`。§8.1参照) と `exclusion_all_flags` を参照。
- 時点assert (`observed_at < scheduled_post_at` および
  `result_fetched_at > scheduled_post_at`) の違反は0件
  (`time_assert_violations` は空)。よって`machine_status = "OK"`
  (800未達はSPECのstop_rule上の理由として`invalid_reasons`に記録するが、
  データ異常によるINVALIDではない)。
- 最終適格レースの`race_id`集合のsha256:
  `2090d70aaa9c9c307d770fd1ac18cbf5d94f43e12f576ce06614b72456167b8b`
  (`audit.json`の`final_race_id_set_sha256`と同一。凍結抽出未作成のため、この
  sha256はまだ台帳に封入していない。)

### 2.2 候補(d)対応可能数

- 最終適格484レースのうち、stage30取得時刻から±90秒以内に全馬分の
  `calibrated_win_probability`を持つ予測run が見つかったもの: **395件**。
- 対応不能89件の理由は全件`no_matching_full_field_probability_run`
  (90秒枠内に予測runが無い、または枠内runの馬集合が不完全)。
- (d)対応不能は(a)(b)(c)の母集団484を変更しない (SPECの要求どおり、別カウント)。

### 2.3 年月・場・頭数帯別 (適格率 = 最終適格 / 適用対象)

`audit.json`の`eligible_rate_by_year_month` / `eligible_rate_by_venue` /
`eligible_rate_by_surface` / `eligible_rate_by_field_band` に完全な分割表がある。
2026-07から2026-09 (9/13は欠測日としてそのまま欠測)、函館・小倉・福島・中京・
新潟・札幌・中山・阪神の8場、頭数帯8-11/12-15/16+で分割済み。値を本文に転記せず
JSONを一次資料とする (数値は監査再実行のたびに変わり得るため、レポート本文への
転記は乖離のリスクがある)。

### 2.4 800R到達見込み

484/800 (残316)。到達日・到達率の予測はSPEC§4.1の指示により**Stage Aの仕事では
ない** (「到達見込みは再集計まで未確定」→ 再集計後もdrift-reportでは実測件数の
報告に限定し、ペース仮定を用いた到達日算出はレビュー担当の判断に委ねる)。

## 3. Stage A 合成テスト (SPEC v3 §5)

実行: `PYTHONUTF8=1 python -X utf8 -m pytest tests/test_t17a_drift.py -q`
結果: **61 passed** (§3.6/§3.7追補分の23件を含む。旧版38件から増加。詳細は§8.5)。
実DBへは接続していない (すべて`:memory:`のsqlite3接続、またはDB接続を伴わない
`RaceRow`合成データ)。

### 3.1 SPEC §5必須テストの対応表

| # | SPEC §5の要求 | テスト関数 |
|---|---|---|
| (1) | drift無情報→β1≈0・ΔLL≈0 | `test_uninformative_drift_gives_near_zero_beta_and_delta` |
| (2) | 情報を仕込めば ΔLL<0 | `test_informative_drift_gives_negative_delta_log_loss` |
| (3) | 発走後取得の除外 | `test_post_race_capture_is_excluded` |
| (3) | 窓外の除外 | `test_out_of_window_capture_is_excluded` |
| (3) | 取消 (結果不完全) の除外 | `test_scratch_after_30min_excludes_race_via_result_mismatch` |
| (4) | 4機械判定: PASS | `test_judgment_pass_all_conditions_met` |
| (4) | 4機械判定: INVALID (最優先) | `test_judgment_invalid_takes_priority_over_everything` |
| (4) | 4機械判定: FAIL (top-kのみ不成立) | `test_judgment_fail_when_only_topk_condition_fails` |
| (4) | 4機械判定: FAIL (人気帯のみ不成立) | `test_judgment_fail_when_only_popularity_band_condition_fails` |
| (4) | 4機械判定: INCONCLUSIVE (CI境界=0) | `test_judgment_inconclusive_when_ci_high_exactly_zero` |
| (5) | block bootstrap再現性 (seed=17) | `test_bootstrap_is_reproducible_with_fixed_seed` |
| (6) | (d)対応付け規則 | `test_d_association_matches_closest_run_within_tolerance` ほか4件 |

### 3.2 v3必須追加テストの対応表

| v3の要求 | テスト関数 |
|---|---|
| 取得品質側800以上でも結果除外後799 (相当) なら実行不可 | `test_quality_pass_above_800_but_final_below_800_is_not_executable` |
| 重複snapshotを1Rと数える | `test_duplicate_snapshot_capture_counts_as_one_race` |
| 同頭数でも馬集合が違えば除外 | `test_same_headcount_different_horse_set_is_excluded` |
| (d)欠落が一次母集団を減らさない | `test_d_incompleteness_does_not_shrink_primary_population` |
| (d)対(a)の同一集合比較 | `test_d_vs_a_comparison_uses_the_same_restricted_race_set` |
| 判定: CIと安全条件の同時不成立→FAIL優先 | `test_judgment_fail_beats_inconclusive_when_both_conditions_unmet` |
| 判定: 人気帯0件→INCONCLUSIVE (0差扱いしない) | `test_judgment_inconclusive_when_a_popularity_band_has_zero_races` |
| 判定: 人気帯差が非有限→INVALID | `test_judgment_invalid_on_non_finite_band_diff_with_races_present` |
| historical / prospective_start_date=null の登録案検証 | `test_registration_draft_is_historical_with_null_prospective_start_date`, `test_registration_draft_validates_against_ledger_schema` |

その他: 品質フラグのみ不成立・オッズ不完全のみ不成立・頭数8未満の適用対象外
分離・読み取り専用接続の書込み拒否・`--run`ゲートの登録無し拒否・凍結抽出の
往復と上書き拒否・時点assert違反時のINVALID化、をそれぞれ個別テストで確認。

## 4. 登録案 (`registration.draft.json`) 要約

- `experiment_id`: `T17a-odds-drift-diagnostic-v1`
- `benchmark_type`: `historical`、`prospective_start_date`: `null`
- `candidate_count`: 3 ((b)(c)(d)。対照(a)は係数固定で候補数に含めない)
- `primary_metric`: `win_logloss_delta_b_minus_a_validation`
- `search_grid`: `{}` (L2なし最尤1回、探索無し)
- `stop_rule`: SPEC v3 §2のINVALID条件 (母集団<800、凍結抽出sha不一致、
  発走後取得の混入、結果/馬集合不一致、非収束、指標/CI数値不正) を明記。
  通常の除外(§3.1)はそれ単独でINVALIDにしないことも明記。
- `data_hashes`: `spec` / `harness_source` / `tests_source` / `manifest_sha256`
  を封入済み。`frozen_extract_sha256`は`null` (Stage Aでは凍結抽出を作成しない
  ため)。
- `notes`: data_start_date=2026-07-12の記録、候補式・分割・bootstrap・機械判定の
  優先順、「歴史的screen通過は将来確認・本番採用ではない」を明記。

### 4.1 登録前バリデーション結果

`eval.ledger.validate_experiment` (ファイルI/O無し、台帳への追記なし) を、
`registered_at_utc`のみプレースホルダのUTC時刻に置換したコピーに対して実行:

```
{'schema_valid': True, 'error': None,
 'note': 'validated a copy with a placeholder registered_at_utc for the '
         'schema-conformance check only; the draft file itself keeps '
         'registered_at_utc=null until a reviewer performs the real '
         'registration.'}
```

`registration.draft.json`ファイル自体の`registered_at_utc`は`null`のまま
(実登録時にレビュー担当が確定する)。

## 5. manifest.json 主要SHA (第3ラウンド反映後に再生成。§9参照)

| 対象 | sha256 |
|---|---|
| SPEC-T17a-odds-drift-diagnostic.md | e3317d868ff28b4414bd98960c548fb40726b7a72d79f8faca2ddf0b25aec565 |
| backtest_t17a_drift.py | 2e60e1bbc19fbe02b57b24d1da007eadaf1f89c45c5bdb02fc7912d8782c0772 |
| tests/test_t17a_drift.py | 2b879e641b91acb2c12e51ea125693c8c1fccade51fa52fb87ce9dd70c3f02c0 |
| manifest.json (自己hash) | 18f7aa6c4f7d530fbf5dcd35ad0d94235dcd4931164a7b606e5bc60fcdfda485 |
| evaluation_conditions_sha256 | a31ac1d905a639855f990c4eb3dfde0a18c1e6c360eee7da2a85e41c23d6d1a9 |

(この表は2026-09-24の第3ラウンド修正後に`--audit-only`と手動の
`build_manifest`/`build_registration_draft`呼び出しで再生成した値。
`evaluation_conditions_sha256`は§9.4の`leave_one_event_date_out`→
`per_event_day_paired_delta`名称統一により、旧値
(`e0ee9699b05a12a2cba8ccaed77ea2debddca1bf34886b8312737631bf6e5143`)から
変化している。SPEC・harness・testsのsha256も§3.6/§3.7追補後の版に対する
値に変わっている。)

git head (実行時点、未コミット変更あり): `c97b0e021759d4d819b95c858cececf3745e4e43`
(第2ラウンドのコミット。第3ラウンドの本追補はこのコミットの後にワークツリー上へ
実装しており、現在のHEADもコミットしていない。表のsha256は現在のワークツリー
内容に対する値。`registration.draft.json`/`result_record.draft.json`の
`commit_sha`は`resolve_commit_sha()`が実測した同じHEAD値。)

## 6. 登録前にレビュー担当が判断すべき未決事項 (実装担当の解釈)

**以下5項目はいずれもSPEC v3 §3.6でレビュー担当により確定済み
(2026-09-24)。項目1のみ実装変更が必要で、§8.1のとおり修正した
(採用取得のfield_sizeのみで判定、field_size不一致は独立した
`odds_incomplete`除外)。項目2〜5は元の実装が既に§3.6の確定内容と一致して
いたため、コード変更は不要だった。登録案`notes`には§3.6の5項目を原文相当で
封入した (§8.3)。以下は経緯記録として旧文のまま残す。**

以下はSPECに明示が無く、実装担当が解釈で補った点。採否・修正はレビュー担当判断。

1. **頭数の適用可否判定の基準時点。** SPEC §3.1項1は「JRA平地・field_size≥8・
   30/10/2の取得がある」を候補条件とするが、`field_size`は取得(capture)ごとの
   列であり、スクラッチ等で経時変化しうる。本実装は
   「stage 30/10/2のいずれかの取得row群に現れた`field_size`の最大値」で
   適用可否を判定した (`_max_field_size`)。最大値ではなく「採用した
   stage-2取得の`field_size`」を基準にする、あるいは3stage全ての一致を要求する
   といった別解釈もあり得る。現行実装では、適用対象と判定された後に
   §3.1項4で3stage間の`field_size`不一致が検出されればそのレースは
   `result_incomplete`として除外されるため、最終適格集合への影響は無いが、
   `not_applicable_headcount_or_unknown` (22件) の内訳がこの解釈に依存する。
2. **時点assertの標準時刻 (`scheduled_post_at`) の選び方。** 結果行の
   `result_fetched_at > scheduled_post_at`のassertに、本実装はstage2取得の
   `scheduled_post_at`を正としている(発走に最も近い取得のため代表性が高いと
   判断)。SPECはどのstageを正とするか明記していない。3stageの
   `scheduled_post_at`が食い違うケースを別途検出するassertは実装していない
   (実データでは0件の不整合だった)。
3. **候補(d)の予測run選定規則。** 「取得時刻差≤90秒」はSPEC通りだが、
   複数runが窓内にある場合は「stage30取得時刻に最も近いrun」を採用し、
   さらに「全馬の`calibrated_win_probability`が非null・有限・正」であることを
   追加要件とした。`raw_win_probability`は実データで全行`NULL`だったため、
   `calibrated_win_probability`のみを使用した。SPEC本文は「p_model,30」の
   具体的な列を明示していない。
4. **「取得欠落」の集計範囲。** SPEC優先順の最上位「取得欠落」は、本実装では
   構造上の候補選定 (stage 30/10/2の取得試行が1件も無いレースは
   `candidate_structural_count`に入れない) で吸収され、`exclusion_primary_reason`
   の集計には現れない設計とした。実行結果でも`acquisition_missing`は
   0件だった。SPEC §3.1項5の「除外理由の優先順」表に`acquisition_missing`が
   含まれない可能性をレビュー担当に確認してほしい (audit.jsonには
   `structural_excluded_nonflat_or_missing_a_primary_stage_attempt: 64`として
   別掲済み)。
5. **manifest.jsonの`manifest_sha256`自己参照。** T81の先例と同様、
   manifest.json確定後にそのファイル自体のsha256を計算し、
   registration.draft.jsonの`data_hashes.manifest_sha256`に埋め込んでいる
   (manifest.json自身は自己hashを含まない、循環を避けるため)。

## 7. やらなかったこと / 実施できなかった項目

- 実データでの(a)(b)(c)(d)係数推定・LogLoss・block bootstrap・機械判定は
  実施していない (SPEC §6・依頼文の指示どおり)。`--smoke`は合成データのみで
  同じパイプラインを検証済み。
- 凍結抽出 (`outputs/t17a/extract_<YYYYMMDD>.sqlite`) の実データからの作成は
  実施していない (800Rゲート未到達のため。手順の実装・合成データでの往復
  テストのみ完了)。
- `--run`モードは実行していない。台帳に`T17a-odds-drift-diagnostic-v1`が
  未登録であることを確認済みで、`check_run_gate`は現状必ず拒否する
  (`test_run_gate_refuses_without_ledger_entry`で確認)。
- 台帳 (`eval/experiments.jsonl`) への追記・`docs/TASKS.md`の更新・
  本番/通知/DBの変更・コミットは行っていない。

## 8. v3 §3.6/§3.7 追加実装 (本追補、実データでの係数推定は今回も未実施)

SPEC v3への追補 (§3.6 仕様補完・§3.7 `--run`ゲートと評価本体) を受けて以下を実施した。
実データでのスコア計算・LogLoss・相関・機械判定は今回も一切行っていない
(`--audit-only`の再監査のみ実データに接続、`--run`の評価本体は合成データの
`--smoke`経由でのみ検証)。

### 8.1 頭数判定の境界修正 (§3.6-1)

`_max_field_size`(全stage・非採用取得を含む最大値)による事前判定を廃止し、
**採用した30/10/2取得のfield_sizeのみ**で適用可否と3stage間一致を判定するよう
`audit_one_race`を修正した。非採用取得の頭数は一切参照しない。3stage間で
不一致なら「オッズ不完全 (`field_size_mismatch_across_stages`)」として単独の
除外理由に分離し (従来は`result_incomplete`に混在)、境界(7/8頭、採用と非採用で
頭数が異なるケース)をテストで固定した
(`test_adopted_headcount_7_with_non_adopted_8_is_not_applicable_no_exception`,
`test_adopted_headcount_exactly_8_is_applicable_and_eligible`,
`test_field_size_mismatch_across_adopted_stages_is_odds_incomplete`)。

再監査 (実DB、読み取り専用) の結果、**最終適格数は484のまま変化なし**
(`final_race_id_set_sha256`も同一)。内訳のみ変化:
`not_applicable_headcount_or_unknown` 22→21、`applicable_count` 594→595、
`exclusion_primary_reason`が `outside_window:24, result_incomplete:86` から
`odds_incomplete:11, outside_window:25, result_incomplete:75` へ再分類された
(旧`result_incomplete`86件のうち11件が`field_size_mismatch_across_stages`として
独立し、非採用取得の頭数を誤って参照していたレース1件が
`not_applicable`から`applicable`(結果は`outside_window`除外)へ移動)。
最終適格レース集合そのものは1件も変わっていない。

### 8.2 `--run`評価本体とゲート拡張 (§3.7)

- `evaluation_conditions()` / `evaluation_conditions_sha256()`: 母集団定義・
  候補式・指標・分割・bootstrap設定・機械判定優先順を構造化してハッシュ化。
  `manifest.json`に埋め込み、登録案`data_hashes.evaluation_conditions_sha256`
  にも格納する。
- `check_run_gate`を拡張し、`experiment_id`・凍結抽出sha256に加え、
  SPEC・ハーネス・テスト・manifestのsha256、および上記`evaluation_conditions_sha256`
  (manifest内の値と台帳`data_hashes`の値の両方)を照合。加えて
  `benchmark_type=historical`・`prospective_start_date=null`・`primary_metric`・
  `candidate_count=3`・`search_grid={}`を台帳行から直接検証する。いずれか1つでも
  不一致ならINVALID相当で`allowed=False`を返す。tmp_path上の合成台帳で、
  全一致時のみ許可・各不一致(凍結抽出/manifest/spec/harness/tests/
  evaluation_conditions/candidate_count/search_grid/benchmark_type)で個別に
  拒否することをテストで確認した。
- `run_evaluation()`: `_build_race_audits`で得た最終適格集合から
  `RaceRow`を構築し、(a)固定・(b)drift・(c)drift+dp+rank_change・
  (d)ln(p_model,30)-ln(p_30)を推定 (L-BFGS-B、収束必須)。開催日60/40分割
  (訓練で係数推定、検証で固定係数評価)、一次指標(b-a のLogLoss差・
  `eval.blocks.paired_block_bootstrap` 2000回 seed17)、二次指標(c/d差・
  top-k(1..3)・人気帯別差・市場30/10/2記述統計・`SD_day`・開催日ごとの
  paired差)、§3.5の機械判定までを実装した。
- 成果物: `result.json` / `race_metrics.jsonl` / `predictions.jsonl` /
  `result_record.draft.json` (T81のStage B実行で欠けていた4点セットを
  最初から出力する。`docs/T81-rank-display-report.md`の`artifact_gap`を参照)。
- `run_smoke_end_to_end()`: 40開催日×6R(頭数8〜16、driftに情報を持たせた
  合成データ)で上記パイプラインをエンドツーエンドに実行し、
  `outputs/t17a/smoke/`に4成果物を書き出す。`enforce_min_population=False`
  (800件ゲートは意図的に適用しない、合成データはそもそも800件に満たない)。
  `--smoke --out-dir outputs/t17a/smoke`(CLI経由)と
  `test_run_smoke_end_to_end_produces_all_4_artifacts`(pytest経由)の双方で
  生成を確認済み。既存の`run_smoke()`(合成データのみのモデリング自己診断、
  9項目)は変更していない。
- `--run`のCLIは`--extract`と`--manifest`を必須にし、ゲート通過後に
  `read_only_connect(extract)` → `run_evaluation(enforce_min_population=True)`
  → (`--out-dir`指定時)`write_run_artifacts`まで実装した。台帳に一致する
  登録が無いため、Stage Aでは従来どおり必ずゲートで拒否され、実データへは
  到達しない (`test_cli_run_mode_refuses_without_extract_flag`ほかで確認)。

### 8.3 登録案notesへの§3.6条文封入

`build_registration_draft`のnotesに、レビュー担当が確定した§3.6の5項目
(頭数判定基準・時点assertの基準stage・候補(d)の確率列選定規則・
「取得欠落」の集計範囲・manifest自己ハッシュ方式)を`SECTION_3_6_CLARIFICATIONS_TEXT`
として原文相当で封入するようにした
(`test_registration_draft_notes_embed_section_3_6_clarifications`)。

### 8.4 T81回帰テストの修正

`tests/test_rank_display.py::test_run_historical_stops_without_a_matching_ledger_registration`
は、T81が正式登録された後は実台帳(`eval/experiments.jsonl`)に
`T81-rank-display-pl3-v1`が実在するため失敗していた。テストのみを修正し、
`monkeypatch.setattr(rd, "LEDGER_PATH", ...)`で無関係な1行だけを持つ
tmp_path台帳に差し替えるようにした。`backtest_rank_display.py`は
`LEDGER_PATH`を呼び出し時に参照するモジュールグローバルとして既に
monkeypatch可能だったため、本体コードの変更は不要だった
(挙動は不変)。

### 8.5 テスト・回帰結果

- `tests/test_t17a_drift.py`: 38→**61 passed** (境界テスト3件、`--run`ゲート
  照合テスト12件、`build_race_row`拡張テスト3件、`--smoke`エンドツーエンド
  テスト2件、登録案notesテスト3件などを追加)。
- `tests/test_rank_display.py`: **49 passed** (該当テストの修正後、全件成功)。
- 全体回帰 (`pytest -q`、リポジトリ全体): **1318 passed, 2 skipped, 3 failed**。
  失敗3件は`tests/test_jra_suite.py::test_legacy_entrypoint_direct_run_exits_nonzero_with_guidance`
  (`jra_ev.py`/`jra_win5.py`/`jra_perf.py`。子プロセス出力のUTF-8デコード
  失敗によるもので、今回の変更と無関係な既知の環境依存失敗)。

### 8.6 今回も実施していないこと

- `--run`モードの実データ実行、凍結抽出の実データからの作成、台帳への
  正式登録・追記。
- (解消済み・経緯記録として旧文のまま残す) `evaluation_conditions()`の
  「開催日leave-one-outのpaired差」の解釈: SPECは具体的な算出手順を
  明記していないため、本実装では検証期間の開催日ごとの平均(LL_b-LL_a)
  (`paired_metric_by_block`の開催日別平均、`SD_day`の算出でも使う中間値)
  をそのまま報告する設計とした。開催日ごとに再学習するfull
  leave-one-day-out CVは実装していない。**2026-09-24のレビューで
  この設計 (再学習なしの開催日別paired差) が確定し (SPEC v3 §3.3)、
  「leave-one-out」という呼称自体が誤解を招くとして`per_event_day_paired_delta`
  へ統一された (§9.4)。以下は当時のオープンクエスチョンの記録であり、
  現在はクローズ済み。**

## 9. 第3ラウンド レビュー対応 (2026-09-24、本追補)

レビュー担当が§3.6/§3.7実装 (第2ラウンド) の監査で見つけた4件の不足・
1件の整理事項に対応した。実データでの係数推定・LogLoss・相関は今回も
実施していない (`--audit-only`の再監査のみ実データに読み取り専用接続)。

### 9.1 800R未満での学習関数呼び出しを停止 (必須修正1)

`run_evaluation()`は従来、`enforce_min_population=True`かつ最終適格数が
800未満のとき、不足を`quality_violations`に記録するだけで
`build_race_rows_from_audits`→`fit_conditional_logit`まで進み、
事後の`machine_judgment`でINVALIDと判定していた。これは「800未満なら
係数推定に進まない」というSPECのstop_ruleの趣旨(実データが無い/信頼できない
状態でモデルを学習しない)に反する可能性があったため、不足を検出した
直後にINVALID相当の結果を構築して即returnするよう変更した。
`build_race_rows_from_audits`も`fit_conditional_logit`も、この分岐からは
一切呼ばれない。

`enforce_min_population=False`(`--smoke`が使う緩和)の経路は変更していない
(合成データはそもそも800件に満たないことが前提のため、従来どおり
最後までパイプラインを実行する)。

テストで確認:
- `test_run_evaluation_below_gate_never_calls_the_fitting_function`
  (合成3Rで`fit_conditional_logit`と`build_race_rows_from_audits`を
  monkeypatchし、呼ばれたら`AssertionError`を送出するようにした上で、
  `enforce_min_population=True`実行時にどちらも呼ばれないことを確認)。
- `test_run_evaluation_smoke_style_below_gate_without_enforcement_still_fits`
  (同じ合成3Rで`enforce_min_population=False`のときは
  `fit_conditional_logit`が呼ばれることを確認し、上の変更が
  `--smoke`の緩和を壊していないことを確認)。
- 既存の`test_run_evaluation_reports_invalid_when_population_below_gate`は
  無変更で成功 (最終的な`machine_judgment.verdict == INVALID`という
  外部から見た挙動は変わらない)。

### 9.2 `--run`に`--out-dir`必須化と実行前チェック (必須修正2)

`--run`は従来`--out-dir`が省略されても(登録ゲートで拒否されない限り)
成果物を残さず実行できた。新設した`validate_run_out_dir()`を
登録ゲート(`check_run_gate`、ひいては台帳/凍結抽出/manifestの参照)より
前に呼び、以下のいずれかで非0終了するようにした:

1. `--out-dir`未指定 → エラー (「a run must not execute without
   persisting its 4 artifacts」)。
2. 指定パスがディレクトリでないファイルとして存在 → エラー。
3. 指定ディレクトリに既存の成果物(`result.json` / `race_metrics.jsonl` /
   `predictions.jsonl` / `result_record.draft.json`のいずれか)が
   既に存在 → 上書き拒否のエラー。
4. 書込み不可 (probe fileの作成に失敗) → エラー。

テストで確認 (すべて`tests/test_t17a_drift.py`):
- `test_validate_run_out_dir_requires_a_path`
- `test_validate_run_out_dir_refuses_existing_artifacts`
- `test_validate_run_out_dir_refuses_when_path_is_a_file`
- `test_validate_run_out_dir_accepts_new_empty_directory`
- `test_cli_run_mode_refuses_without_out_dir_before_touching_extract_or_manifest`
  (`--extract`/`--manifest`も未指定の状態で、エラーメッセージが
  out-dir由来であり「--extract」由来でないことまで確認し、
  チェック順序が登録ゲートより前であることを担保)
- `test_cli_run_mode_refuses_when_out_dir_already_has_artifacts`
  (`--extract`/`--manifest`が存在しないパスでも、out-dir側の拒否が先に
  発生することを確認)
- `test_cli_run_end_to_end_with_valid_out_dir_persists_all_4_artifacts`
  (`main(["--run", ...])`をそのまま実行するend-to-endテスト。
  実DB/実台帳には一切触れず、`eval.ledger.load_ledger`と
  `check_run_gate`をmonkeypatchしてhermeticな登録案を用意し、
  800件以上の合成レースを持つ実ファイルsqlite凍結抽出もどきを
  用意した上で、4成果物すべてがディスクに残ることを確認)。

### 9.3 `result_record.draft.json`の`commit_sha` (整理3)

`write_run_artifacts()`の`commit_sha`引数が省略された場合(従来の
`--run`呼び出しがこれに該当)、常に文字列`"unknown"`が
`result_record.draft.json`へそのまま書き込まれていた。新設した
`resolve_commit_sha()`が実行中チェックアウトの`git rev-parse HEAD`を
実測し、成功すれば実sha (40文字16進、gitの短縮無し完全形) を返す。
git実行不可・非gitリポジトリ等で取得できない場合のみ、`commit_sha`に
`"unavailable:<理由>"`という理由付き文字列を入れ(`null`にはしない)、
`commit_sha_fallback`にこのharnessファイル自身のsha256
(`sha256:<hex>`形式)を代替identifierとして入れる。取得できた場合
`commit_sha_fallback`は`None`。

`write_run_artifacts(commit_sha=None)`(新しいデフォルト)のときのみ
自動解決する。`--smoke`(`run_smoke_end_to_end`)は従来どおり明示的に
`"smoke-end-to-end"`を渡すため、この自動解決の対象外(既存の
`--smoke`結果への影響なし)。`--run`のCLI呼び出しは`commit_sha`引数を
渡していなかったため、変更後は自動的に実sha解決の対象になる。

実測 (本ラウンドの再生成時): `outputs/t17a/smoke/result_record.draft.json`の
`commit_sha` = `c97b0e021759d4d819b95c858cececf3745e4e43` (実行時点のHEAD、
未コミット変更あり)。

### 9.4 名称統一: `leave_one_event_date_out` → `per_event_day_paired_delta` (整理4)

検証期間の開催日別paired差(訓練期間で固定した係数をそのまま検証期間の
各開催日に当てはめた平均差)は、開催日ごとに再学習するleave-one-out CVでは
ない (SPEC v3 §3.3、2026-09-24のレビューで確定)。この誤解を招く名称を
以下のとおり統一し、`leave_one_out`/`leave-one-out`系の名称をコード内の
出力キー・notes文言から除去した:

- `result.json`の`secondary_metrics`: キー
  `leave_one_event_date_out_paired_delta_b_minus_a_by_day` →
  `per_event_day_paired_delta_b_minus_a_by_day`。
- `evaluation_conditions()`(→`manifest.evaluation_conditions`、
  `evaluation_conditions_sha256`の入力): `split`内のキー
  `leave_one_event_date_out_paired_diffs_reported` →
  `per_event_day_paired_delta_reported`。
- `SAFETY_METRICS`(登録案`safety_metrics`): 要素
  `leave_one_event_date_out_paired_delta_b_minus_a` →
  `per_event_day_paired_delta_b_minus_a`。
- 登録案notesの「(c)(d)の好成績やleave-one-outの結果で、一次候補の
  不通過を救済しない」という文言 → 「the per-event-day paired delta」に
  変更。
- 本レポート§8.6の記述に「解消済み」の注記を追加 (SPEC本文§3.3/§3.4の
  「leave-one-out CVは行わない」という対比としての用法は、設計上の
  否定形の説明であって出力の名称ではないため、SPEC.md自体は変更していない)。

この名称変更により、`evaluation_conditions_sha256`は旧値
(`e0ee9699b05a12a2cba8ccaed77ea2debddca1bf34886b8312737631bf6e5143`)から
新値(`a31ac1d905a639855f990c4eb3dfde0a18c1e6c360eee7da2a85e41c23d6d1a9`)
へ変わった。これは想定内の変化であり、`manifest.json`・
`registration.draft.json`も本ラウンドで再生成して追随させた
(§9.5)。母集団や機械判定の実装ロジックは変更していない。

### 9.5 manifest・登録案の再生成 (封印前の整理5)

`--audit-only`を実DB(読み取り専用)に対して再実行し、監査件数が
不変であることを確認した:

| 指標 | 値 (変化なし) |
|---|---:|
| capture_quality_pass_count | 570 |
| final_eligible_count | 484 |
| final_eligible_event_dates | 20 |
| gate_800_reached | False |
| gate_800_shortfall | 316 |
| d_compatible_count | 395 |
| final_race_id_set_sha256 | `2090d70aaa9c9c307d770fd1ac18cbf5d94f43e12f576ce06614b72456167b8b` |

`outputs/t17a/stage_a/manifest.json`と`outputs/t17a/stage_a/registration.draft.json`
をこの監査結果・現在のSPEC/harness/tests/manifestファイル内容から再生成した
(§5のSHA表を参照)。`registration.draft.json`の`commit_sha`は
`resolve_commit_sha()`による実測値(`c97b0e021759d4d819b95c858cececf3745e4e43`)。

`outputs/t17a/smoke/`も`--smoke --out-dir outputs/t17a/smoke --seed 17`で
再実行し、4成果物 (`result.json` / `race_metrics.jsonl` /
`predictions.jsonl` / `result_record.draft.json`) の再生成、
`quality_violations == []`、`per_event_day_paired_delta_b_minus_a_by_day`
キーの存在、`result_record.draft.json`の`commit_sha`が実HEAD値であること
(「unknown」でないこと)を確認した。

### 9.6 テスト・回帰結果 (第3ラウンド)

- `tests/test_t17a_drift.py`: 61→**70 passed** (本ラウンドで9件追加: §9.1の
  学習関数呼び出し検証2件、§9.2の`--out-dir`検証6件+end-to-end1件)。
- `tests/test_rank_display.py`: **49 passed** (無変更)。
- 全体回帰 (`pytest -q`、リポジトリ全体): **1327 passed, 2 skipped, 3 failed**。
  失敗3件は前ラウンドと同一の既知の環境依存失敗
  (`tests/test_jra_suite.py::test_legacy_entrypoint_direct_run_exits_nonzero_with_guidance`、
  `jra_ev.py`/`jra_win5.py`/`jra_perf.py`の子プロセスUTF-8デコード失敗。
  本ラウンドの変更と無関係)。

### 9.7 今回も実施していないこと

- `--run`モードの実データ実行、凍結抽出の実データからの作成、台帳への
  正式登録・追記 (800Rゲート未到達のため引き続き未着手)。
- SPEC-T17a-odds-drift-diagnostic.md本文への変更 (§3.3/§3.4の
  「leave-one-out CVは行わない」という説明文はレビュー担当が確定した
  文言であり、実装担当からは変更していない。§9.4参照)。
