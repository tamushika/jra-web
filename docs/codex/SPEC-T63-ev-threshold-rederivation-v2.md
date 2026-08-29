# SPEC-T63 v2: EV通知閾値の再導出 — 裁定ゲート要件の改訂 (supersede登録)

改訂: 2026-08-29 (Fable 5)。v1: [SPEC-T63](SPEC-T63-ev-threshold-rederivation.md)
(2026-07-25起票、台帳 `T63-ev-threshold-rederivation-v1`)。
実装: jra-coder。レビュー・台帳登録・コミット: 上位モデル。

## 0. 改訂の理由 (記録のために明示)

v1の裁定ゲート②「12開催日 ∧ 最有力閾値で反実仮想通知300件」は、起票時点の見込み
(旧モデル時代は1開催日に27〜88頭がEV≥1.3) を前提にした件数であった。
T45新モデルのEV分布 (ゲート①通過時に読了: p50=0.765 / p90=0.923 / p99=1.067、
日次最大1.03〜1.18) では、2026-08-29時点の**件数カウントのみ** (ハーネスの
`--leading-threshold` 指定時の `counterfactual_notifications`、仕様で許可された読み取り)
が以下で、300件は最も緩い1.00でも2027年1〜2月、それ以外は事実上到達不能:

| 候補閾値 | 反実仮想通知 (11開催日/374R) |
|---|---:|
| 1.00 | 63 |
| 1.05 | 29 |
| 1.10 | 12 |
| 1.15 | 4 |
| 1.20 | 1 |
| 1.30 | 0 |

**閾値別の校正比・回収率・その他の指標は一切読んでいない**。本改訂はゲート件数の
前提崩れ (分布シフト) に対する手続き上の是正であり、結果を見た基準変更ではない。

## 1. v1から変更する点 (これのみ)

- 裁定ゲート②: 「12開催日 ∧ 最有力閾値で反実仮想通知 **100件** 以上」に改める
  (300→100)。開催日要件12は不変
- 最有力閾値 (leading threshold) を **1.00** に指定する (上位モデル指定。件数から
  唯一現実的に到達し得る候補であり、他候補の指標は裁定時に同時出力される)
- 台帳: `T63-ev-threshold-rederivation-v2` を新規登録し、`superseded_by` で v1 を参照
  (prospective登録の改訂。v1行は不変・append-only)
- ハーネス: `EXPERIMENT_ID` / `ADJUDICATION_NOTIFICATIONS` / `SPEC_SHA256`
  (本v2ファイルのSHA) / `CONTRACT_SHA256` / `DEFAULT_SPEC` を v2 に更新

## 2. v1から変更しない点 (明示)

- データ源・cutoff定義・母集団params (max_odds 50 / min_prob 0.02)
- 候補グリッド {1.00, 1.05, 1.10, 1.15, 1.20, 1.30}
- 一次指標 (校正比) と二次指標・安全指標一式
- ゲート① (4開催日+200R、通過済み 2026-08-15)
- **蓄積データはリセットしない** (本番モデルSHA `8687f9…` は不変。prospective開始日
  2026-07-25 も不変)
- 全DB読み取り専用。本番の通知条件は本SPECの範囲外 (→ SPEC-T71 で別途扱う)
- ゲート②到達前に校正比・回収率を出力しないfail-closed構造

## 3. T71 (暫定通知閾値) との関係

SPEC-T71 で本番の通知閾値を暫定 1.1 に下げるが、本実験の入力は `board_odds_snapshots`
の全馬反実仮想再構成であり、本番閾値・本番通知の有無に依存しない。したがって
T71 は本実験の蓄積・指標を汚染しない。裁定時、参考報告として「暫定閾値1.1で
実際に送られた通知件数/月」を併記する (LINE無料枠制約の実測値として)。

## 4. 実装 (jra-coder)

1. `backtest_t63_ev_threshold.py`:
   - `EXPERIMENT_ID = "T63-ev-threshold-rederivation-v2"`
   - `ADJUDICATION_NOTIFICATIONS = 100`
   - `DEFAULT_SPEC` を本v2ファイルに変更、`SPEC_SHA256` を本ファイルのSHA256に更新
   - `CONTRACT_SHA256`: v1登録時と同一の算出規則で再算出する。算出規則は
     v1登録コミット `d5345da` (`git show d5345da --stat` で登録スクリプト/手順を確認)
     に従う。規則が特定できない場合は実装を止めて上位モデルに報告する (推測で埋めない)
   - 他の定数・ロジックは変更しない
2. `eval/experiments.jsonl` に v2 行を **append** (v1行は不変):
   - v1行をベースに `experiment_id`=v2、`superseded_by`="T63-ev-threshold-rederivation-v1"、
     `registered_at_utc`=登録時刻、`commit_sha`=HEAD、
     `search_grid.gates.adjudication.min_counterfactual_notifications_at_leading_threshold`=100、
     `data_hashes.spec_sha256`/`t63_contract_sha256` を更新 (`production_model_sha256` 不変)
   - `features` に LIMITATION を1行追加: 「v2改訂 (2026-08-29): 裁定ゲート②の件数300→100。
     理由=T45分布下で300は到達不能 (件数カウントのみ読了、閾値別指標は未読)。
     最有力閾値=1.00を上位モデルが指定」
   - `stop_rule` の「300件」相当の記述を100件に改める
   - `python -m eval.ledger verify` 相当の検証 (既存の検証手順) を通す
3. `tests/test_t63_ev_threshold.py`: 契約テストが v2 行・v2 SPEC を参照して通ること。
   ゲート②の件数テストがあれば 100 に追随
4. `docs/T63-ev-threshold-report.md` に「2026-08-29 v2改訂」節を追記 (§0の表と理由)
5. `python -m pytest tests/ -q` 全体パス

## 5. 見込み

1.00で1開催日≈5.7件 → 100件到達はあと約7開催日 (**2026年10月上旬〜中旬**)。
到達後にハーネスを `--leading-threshold 1.00` で実行し、上位モデルが裁定する。
