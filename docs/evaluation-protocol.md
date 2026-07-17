# 予測実験の評価プロトコル

本書は、T41以降の予測実験を同一条件で比較し、as-ofリークと事後的な評価指標の
変更を防ぐための運用契約である。2025年と2026年上期は、既に複数回の判断に利用
されているため historical benchmark とする。最終採否に使う prospective 評価は、
仕様をfreezeした後の次開催日以降だけで行う。

## 1. 共通cutoff

市場、現行モデル、候補モデルには、購入可能時点に実在した情報だけを同じcutoffで
与える。

- WIN5では全5レース共通で、第1対象レース発走時刻の5分前を既定cutoffとする。
  `eval.cutoff.win5_cutoff()`と`WIN5_CUTOFF_MARGIN_MIN`がこの規約を実装する。
  後半レースの直前オッズや直前更新情報を使ってはならない。
- 単レースEVでは、通知判断に実際に使ったスナップショットの`observed_at`をcutoffと
  する。
- WIN5用と単レースEV用の特徴テーブルを混在させない。実験コードと台帳のfeatures欄
  に用途、cutoff、変換、欠損処理、利用可能時刻を明記する。

実装APIは次のとおりである。

```python
def select_snapshots(connection, race_ids, *, cutoff: datetime,
                     purpose: Literal["win5", "single_race_ev"],
                     require_quality: bool = True) -> list[SnapshotRow]: ...
```

元SPEC §3.2のコード例には`purpose`が無いが、§3.1の用途明示・混在禁止を機械的に
保証するため、実装では必須keywordとして追加している。省略または未知の値は拒否する。
WIN5特徴には`purpose="win5"`、単レースEV特徴には
`purpose="single_race_ev"`を指定する。戻り値にも選択したpurposeを保持する。

この関数はcutoff以下の最後の行を`(race_id, horse_id)`ごとに返す。cutoffと同時刻の
行は含み、直後の行、
`observed_at`欠損、timezone情報を持たないnaiveな観測時刻は含まない。品質除外を
行ってから最後の行を決めるため、最新行が不良でも、それ以前にcleanな行があれば
その行を利用する。

`cutoff`と`win5_cutoff()`へ渡す第1対象レース発走時刻は、必ずtimezone-awareな
`datetime`とする。naiveな値はJSTかUTCかを安全に判定できないため、暗黙補完せず
`ValueError`で即時停止する。DBの`observed_at`もtimezone-awareな行だけをUTCへ正規化
して比較し、naiveな行は`missing_or_ambiguous_observed_at`除外件数へ計上する。

戻り値はlist互換の`SnapshotSelection`である。呼び出し側は必ず
`excluded_quality_count`または`excluded_counts`を評価結果に併記する。
`require_quality=True`では次を除外する。

- `insufficient_odds`
- `catchup_burst`
- `post_time_changed`
- `is_stale=1`
- 品質フラグJSONが壊れている行

`late_capture`と`scheduler_restart`は取得状況の監査情報であり、実際の`observed_at`が
共通cutoffを満たすだけでは品質不良としない。必要な実験では台帳登録時に追加除外を
事前指定する。

## 2. 開催日ブロックbootstrap

paired比較のリサンプリング単位は、通常評価では開催日、WIN5ではWIN5開催日とする。
本システムのrace_idは先頭8桁がJSTの`YYYYMMDD`なので、
`eval.blocks.race_date_block()`または`group_by_race_date()`でブロック化する。
先頭8桁は数字であるだけでなく、暦上実在する日付でなければならない。例えば
`20260230`や`20261301`は入力誤りとして拒否する。
同じ日の行を分割するレース行単位のbootstrapは禁止する。同日内の馬場、天候、市場
などの相関を無視して不確実性を過小評価するためである。

`paired_block_bootstrap(metric_fn, blocks_a, blocks_b, n_resamples, seed)`は、A/Bで
同じ開催日のブロックを同時に復元抽出し、`metric(A) - metric(B)`の分布を返す。
mappingを渡す場合、A/Bのブロックキーは完全一致しなければならない。返り値には、
全ブロックでの差、差の分布、95% percentile CI、両側p値、seed、抽出回数、
ブロック数を含む。p値には有限回の再標本化で0を返さないadd-one補正を使う。
seedは台帳へ登録し、比較途中で変更しない。

毎週結果を見る逐次評価を行う場合は、その頻度と停止条件を実験登録の`stop_rule`に
事前記載する。bootstrapヘルパー自体は逐次検定を実装しない。

## 3. 実験台帳

台帳は`eval/experiments.jsonl`に置き、UTF-8 JSONLの1行を1実験とする。登録は実験
実行前に上位モデルが行い、追記だけを許す。必要なキーは次のとおり。

```json
{
  "experiment_id": "T41-weightpack-v1",
  "registered_at_utc": "2026-07-17T00:00:00Z",
  "commit_sha": "freeze対象のcommit SHA",
  "data_hashes": {"ability_db": "sha256:...", "logging_db_rows": 123456},
  "features": ["変換・欠損処理・利用可能時刻を含む特徴記述"],
  "primary_metric": "win_logloss_2124_cv",
  "safety_metrics": ["market_topk_floor", "win5_day_paired"],
  "search_grid": {"l2": [0.3, 1, 3]},
  "candidate_count": 3,
  "stop_rule": "grid全評価後に固定し、逐次確認しない",
  "benchmark_type": "historical",
  "prospective_start_date": null,
  "result_summary": null,
  "adjudication": null
}
```

`primary_metric`は非空文字列1個だけとし、配列や複合指標へ実行後に変更しない。
`benchmark_type`は`historical`または`prospective`である。prospective登録では、
`commit_sha`をfreeze commitとして扱い、次開催日である`prospective_start_date`を必須と
する。宣言後に特徴、探索範囲、指標、停止条件などを変える場合は、新しい
`experiment_id`で追記する。その新しい置換行に`"superseded_by": "旧ID"`を付け、
置換対象を明示する。このフィールドは必ず既に登録済みのIDを参照し、自己参照は許さ
ない。名称は台帳schemaとの互換性のため`superseded_by`とするが、append-onlyを守る
ため参照は新行から旧行へ記録する。既存行を編集してはならない。

### 3.1 結果・裁定の追記

事前登録行の`result_summary`と`adjudication`を後から直接更新してはならない。結果は
次のように、全必須キーを持つ新しい一意IDの行として追記し、`superseded_by`から元の
事前登録IDを参照する。

```json
{
  "experiment_id": "T41-weightpack-v1-result-20260801",
  "registered_at_utc": "2026-08-01T00:00:00Z",
  "superseded_by": "T41-weightpack-v1",
  "commit_sha": "freeze対象のcommit SHA",
  "data_hashes": {
    "ability_db": "sha256:...",
    "logging_db_rows": 123456,
    "prospective_eval_sha256": "sha256:...",
    "prospective_eval_rows": 250
  },
  "features": ["事前登録行と同一の特徴記述"],
  "primary_metric": "win_logloss_2124_cv",
  "safety_metrics": ["market_topk_floor", "win5_day_paired"],
  "search_grid": {"l2": [0.3, 1, 3]},
  "candidate_count": 3,
  "stop_rule": "grid全評価後に固定し、逐次確認しない",
  "benchmark_type": "prospective",
  "prospective_start_date": "2026-07-18",
  "result_summary": {
    "primary_metric_value": 0.412,
    "excluded_quality_rows": 3
  },
  "adjudication": null
}
```

結果・裁定行では、参照先の`commit_sha`、`features`、`primary_metric`、
`safety_metrics`、`search_grid`、`candidate_count`、`stop_rule`、`benchmark_type`、
`prospective_start_date`をそのままコピーし、変更してはならない。参照先の
`data_hashes`も既存キーを削除・上書きせず、実評価データは例の
`prospective_eval_*`のような新しいキーで追加する。validatorはこれらを検査する。

裁定を後日記録する場合も、結果行を直接変更しない。さらに新しいID、例えば
`T41-weightpack-v1-adjudication-20260802`を追記し、`superseded_by`で直前の結果行を
参照する。設計フィールド、data hashes、`result_summary`を引き継いだ上で、
`adjudication`へ採用・棄却と理由を記録する。`result_summary`または`adjudication`が
非nullの行は必ず先行行を参照し、裁定行は非nullの`result_summary`も引き継ぐ。

仕様そのものを実行前に改定する置換行では、`result_summary`と`adjudication`をnullに
保ち、新しい契約として変更点を記述できる。この場合も新IDと先行ID参照が必要であり、
再度実行前に登録する。

登録例:

```powershell
python -m eval.ledger register registration.json
```

標準入力を使う場合はファイル名を`-`にする。登録ツールはスキーマとID重複を検査し、
append後に整合性チェックポイントを更新する。

## 4. append-only検証

次を実行すると、JSONL構文、必須キー、型、`experiment_id`一意性、前回検証済みの
byte prefixを検証する。

```powershell
python -m eval.ledger verify
```

初回verify時には台帳隣に`experiments.jsonl.verify-state.json`を作る。このローカル
チェックポイントは前回のbyte数、行数、prefix SHA-256を持つ。次回verifyでは、以前
検証済みの範囲の短縮・編集・差し替えを拒否し、末尾への追記だけを許す。検証失敗時は
チェックポイントを更新しない。チェックポイント自体を削除・差し替えれば過去の
基準を失うため、評価証跡とともに保全する。Gitで台帳を管理する場合は、台帳追記と
チェックポイント更新を同じ変更としてレビュー・保存する。チェックポイントを
`.gitignore`へ追加しない。

## 5. 採否までの流れ

1. 実装・データ・cutoff・主指標・安全指標・探索範囲・候補数・seed・停止条件を決める。
2. 実行前に台帳へ登録し、`verify`を通す。
3. historical benchmarkは探索・比較用として明記し、最終採否の根拠と混同しない。
4. prospective候補はfreeze commitと次開催日を登録し、それ以後のデータだけを同じ
   cutoffで蓄積する。
5. 開催日単位のpaired比較を行い、主指標と事前登録済み安全指標を報告する。cutoff・
   品質による除外数も必ず報告する。
6. 結果と裁定は§3.1の新ID行として追記する。既存行は修正しない。
7. 仕様変更、追加探索、指標変更が必要なら新IDを事前登録する。

freeze宣言、実験登録、結果の採否判断は上位モデルが行う。本ヘルパーの存在だけで
prospective採用条件を満たしたことにはならない。
