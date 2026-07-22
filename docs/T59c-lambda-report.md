# T59c 着順段階別λ校正 — 実装報告

## 状態

2026-07-22時点で、隔離評価ハーネス `backtest_t59c_lambda.py` と数理テストを実装した。
本番の `api/combo_probs.py`、本番FEATURES、通知経路には変更を加えていない。

実評価は未実行である。SPEC §6–7の指示どおり、上位モデルがT39台帳へ複勝・ワイド・
馬連の3実験を登録するまで、CLIは評価開始前に停止する。必要な実験IDは次のとおり。

- `T59c-place-lambda-v1`
- `T59c-wide-lambda-v1`
- `T59c-umaren-lambda-v1`

各登録は ability.db SHA-256
`7ffcfe21618612b603053544cd888eec637b6fdf69192470c5751c5f89b00c79` を封印する必要がある。

## 実装済み契約

- `runs.win_pay` は既存 `backtest_win5.parse_final_odds()` のみで解釈し、逆数をレース内正規化。
- ブック合計 `[1.15, 1.45]`、8頭以上、単独1–3着が揃うレースへ固定。
- 2021–23でλ2/λ3を独立MLE、2024を一次評価、2025/2026H1を固定benchmark。
- 複勝・ワイド・馬連と参考三連複を同一λから導出。
- 開催日paired bootstrapは `eval/blocks.py` を使用。
- 条件付き確率、複勝確率、馬連・三連複確率の総和を実行時assert。
- JSONは時刻を含めずキー順・区切りを固定し、同一入力のSHAを決定論化。

## 未生成の成果物

`outputs/t59c_lambda_result.json` は意図的に未生成。T39登録後に次を実行し、数値欄を
この報告へ追記する。

```powershell
& 'C:\Users\owner\project\jra-runtime\Scripts\python.exe' backtest_t59c_lambda.py
```

登録前の実行は `T59c evaluation blocked` で停止するのが正常動作である。
