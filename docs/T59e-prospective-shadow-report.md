# T59e (P4) prospectiveシャドー測定 — 実装報告

実装日: 2026-07-22。集計設計・read-onlyハーネス・テストを実装した。
蓄積ゲート未達のため、評価値の読込み・結果JSON生成・採否判断は行っていない。

## 現在の蓄積ゲート

| 対象 | 現在 | 必要 | 状態 |
|---|---:|---:|---|
| `stage=30 AND status='ok'` の開催日 | 0 | 4 | 未達 |
| 同レース | 0 | 200 | 未達 |

参考として `race_results` 自体には3開催日・108レースが存在するが、T59dの
`board_odds_snapshots` は次開催から蓄積するため、これらをT59eへ遡及流用しない。

`python backtest_t59e_shadow.py` の実DB実行は次で停止することを確認した。

```text
T59e evaluation blocked: 0 races / 200 required; 0 event dates / 4 required
```

`outputs/t59e_shadow.json` と日別CSVは生成されていない。

## 事前固定した集計定義

- ゲート確認では開催日数・レース数だけを読み、未達ならモデル確率・オッズ・結果を
  SELECTする前に停止する。
- 各stageで最初に成功した取得を採用し、後続値を見て取得時点を選ばない。
- 一次評価は30分前。単勝オッズは同じ`race_id + stage + fetch_id`の
  `odds_snapshots`から読み、全出走馬が揃う8頭以上・ブック帯`[1.15,1.45]`だけを採用。
- 確定結果は単独1–3着が成立するレースだけを採用。同着はレース単位で除外し、
  取消行は着順母集団から除外する。
- 複勝・ワイドは全候補のBernoulli log lossをレース内平均。馬連は実現した1–2着組の
  `-log(P)`。baselineは同時点の単勝オッズからλ2=λ3=1で導出する。
- 複勝・ワイドのcandidate-baseline差は開催日単位5,000回paired bootstrap。
  馬連はcandidate自体がλ=1のため、予測質の継続記録として扱う。
- 校正曲線は券種・candidate/baseline別の確率10分位。

## CLV・参考ROIの固定定義

CLVは30分前・10分前の各組合せについて、
`x=log(公正オッズ_stage / 実オッズ_stage)` と
`y=log(実オッズ_2分前 / 実オッズ_stage)` のPearson相関を券種別に報告する。
2分前をクロージングとし、対応組合せが欠ける行は除外する。

参考ROIはSPECどおり各stageで `公正オッズ > 実オッズ` の組合せへ100円ずつ、結果を
見ずに機械選択する。現行`race_results`にはワイド・馬連の公式組合せ払戻が無いため、
3券種を同一定義で比較できるよう、的中時払戻は2分前実オッズ×100円のnear-final proxy
に事前固定した。この制約をJSONの`reference_roi_definition`にも必ず出力する。
表示・通知・ライブ判断には接続しない。

## 成果物

- `backtest_t59e_shadow.py`: read-only集計、ゲート、一次指標、校正、CLV、参考ROI。
- ゲート到達後のみ `outputs/t59e_shadow.json` と
  `outputs/t59e_shadow_daily.csv` を決定論的に生成する。
- 日別CSVは`date, ticket, races, candidate_logloss, baseline_logloss`の固定列。

## テスト

- T59e専用9件pass: 199/200境界、4開催日、log loss閉形式、ブック帯、field欠損、
  同着・取消、最初の成功取得、CLV相関符号、ライブ非接続、決定論出力。
- 全体回帰: 512件pass / 2件skip。
- `test_extract_keiba_kagaku.py`は代替PythonにPyMuPDF (`fitz`) が無いため収集対象外。
- `git diff --check` pass。

初回数値報告と採否は、両方の蓄積ゲート到達後にのみ行う。
