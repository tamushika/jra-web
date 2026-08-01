# T62b シャドー測定セットアップ報告

作成日: 2026-08-01

## 実装範囲

- T62の凍結manifestを `eval/t62_freeze_manifest.json` に固定し、内部manifest SHA-256
  `db8df644fc5025f91d1ae6a7c67fe394a02f690a54f73f8b6aeb4d445055b1f3` をロード時に検証する。
  不一致・特徴契約不一致では表示を停止し、再fit・再調整は行わない。
- 既存T17ループの30分前cutoffで、本番T45勝率、全馬の単勝オッズ、ライブ走歴から固定11特徴と
  スコアを計算する。対象は9R以降・平地・8頭以上・全馬オッズありに限定した。
- 既存ボードに「本日の勝負レース度」を表示専用で追加した。通知関数、EV判定、
  `combo_probs.py`、`win5_ml_model.json` は変更していない。
- `T62b-race-selection-shadow-v1` を評価前にT39台帳へ登録し、manifestファイル、評価ハーネス、
  SPEC、評価契約のSHA-256を封印した。採否は行わない。

## 追記スキーマ

`race_confidence_snapshots` は `date/place/r/cutoff_at/snapshot_source`、モデル確率、cutoff単勝オッズ、
11特徴、凍結スコア・閾値・選定フラグ・manifest SHA・作成時刻だけを追記する。勝馬、着順、
確定オッズ列は持たず、評価時だけ既存 `race_results` と結合する。cutoff取得失敗は確率・特徴・
スコアをNULLにした新規行として記録し、以前の値を流用しない。

## ゲート状態

2026-08-01の実装時点は **0開催日 / 4開催日、0レース / 200レース**。未達のため評価ハーネスは
件数だけを先読みして停止する。したがって、prospective評価数値はまだ存在しない。

## 変更ファイル

- `eval/t62_freeze_manifest.json`
- `api/race_confidence.py`
- `api/logging_store.py`
- `jra_ev.py`
- `index_boards.html`
- `backtest_t62b_shadow.py`
- `eval/experiments.jsonl`
- `tests/test_t62b_shadow.py`
- `tests/test_logging_store.py`

## 検証

- T62b専用 + logging store: 26 passed
- 台帳検証: 48 experiments、append-only整合性OK
- 全体テスト: 632 passed、2 skipped（2026-08-01、29.52秒）

デプロイ・suite再起動は未実施。レース非開催日にユーザー承認を得て別途実施する。

## 上位モデルレビュー: 受理+ハードニング2件 (2026-08-01, Fable 5)

**受理**。manifest二重封印 (定数SHA+canonical再計算) のfail-closed・母集団ガード・
台帳事前登録 (canonical SHAとファイルSHAの両封入)・スキーマの結果列拒否・
禁止語彙テスト・通知3関数の無変更 (git diffで独立確認) をすべて確認。全633テストパス。

ハードニング (上位モデル直接修正):

1. **取消馬の扱い**: 取消馬が1頭でもいるとレース全体が「全馬オッズ必須」検査で対象外に
   なり、歴史母集団 (実出走馬のみ) と定義がずれるバグ。取消馬を除外してから検査・採点
   するよう修正 (選定率ドリフト計測の汚染防止)。回帰テスト追加
2. **AST不変性テストの脆さ**: ast.dumpのハッシュ固定はPythonバージョンでダンプ形式が
   変わると誤検知する (レビュー環境で実際に失敗)。通知3関数の**ソース断片SHA-256固定**
   方式へ置換 (バージョン非依存・コメント含む一切の編集で失敗する凍結契約仕様)

デプロイ (suite再起動) はレース非開催タイミングでユーザーが実施。蓄積はデプロイ後の
開催日の30分前cutoffから自動開始する。
