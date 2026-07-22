# T59d (P3) 確率・オッズボード実装報告

実装日: 2026-07-22（平日）。実装・テスト・fixture画面確認までを行い、suite再起動・
デプロイ・採否判断は行っていない。

## 実装結果

- 新規隔離モジュール `api/board_market.py` で市場単勝確率を正規化し、複勝・ワイドは
  λ2=`0.830185` / λ3=`0.720886`、馬連はλ=`1`から導出する。
- ブック合計 `[1.15, 1.45]` 外または単勝オッズ不完備なら「表示不可」とし、確率を
  生成しない。7頭以下の複勝は2着内へ切り替える。
- JRAカード中の公式オッズCNAMEを取得済みカード解析から受け渡し、既存30/10/2分前
  スナップショット処理の中で、単勝・複勝ページ、ワイド、馬連の3リクエストを行う。
  netkeibaおよび独立スケジューラは使用しない。
- 取得失敗時は前回値を参照せず、その時点のボードを「取得失敗」で置換・保存する。
- EV監視配下の `/ev/boards`（単独起動時は `/boards`）に独立表示ページを追加した。
  表示列はモデル確率、公正オッズ、実オッズ、乖離、オッズ取得時刻のみ。
- 本番 `api/combo_probs.py` と `win5_ml_model.json` は変更していない。

## P4向け保存スキーマ

`data/jra_logging.db` にmigration version 10として `board_odds_snapshots` を新設した。
主列は `date, place, r, race_id, bet_type, combo, odds, odds_low, odds_high,
model_probability, fair_odds, gap_ratio, requested_at, received_at, source, stage,
fetch_id, status, data_quality_flags_json`。1取得時点・券種・組合せをappend-onlyで保存し、
失敗とブック帯外も券種別sentinel行として残す。P4は `race_id + stage + bet_type + combo`
で結果・時点を結合し、確率校正、CLV、収益率を追加migrationなしで集計できる。

## 実データ表示確認

T59bで取得済みのJRA公式2026-07-19福島8R fixtureを使用した。ブック合計は
`1.261447`で帯内。複勝16行・ワイド120組・馬連120組を生成し、実オッズ欠損は全券種0。
具体値を `docs/T59d-board-preview.html` にHTMLダンプした。

例: 2番ロスパレドネスの複勝はモデル確率33.17%、公正3.01、実2.50–3.60、乖離0.99。
2-3ワイドはモデル確率6.32%、公正15.83、実16.10–17.50、乖離0.94。
表示文言を目視し、状態説明と指定5項目以外の判断文言がないことを確認した。

in-app browserは実行環境に利用可能なブラウザがなく接続できなかったため、SPEC §5が
許容するHTMLダンプで確認した。

## テスト

- T59d・logging・snapshot対象: 34件pass。
- 全体回帰: 502件pass / 2件skip。
- `test_extract_keiba_kagaku.py` は代替PythonにPyMuPDF (`fitz`) がないため収集対象外。
- 禁止語彙は専用HTML、正常JSON、取得失敗JSON、表示不可JSON、実API応答を全文走査。
- `_send_line` / `_send_discord` / `refresh_and_alert` はHEADとのAST一致。
- `api/combo_probs.py` / `win5_ml_model.json` は固定SHA一致。

## 上位モデルレビュー追補・修正 (2026-07-22, Fable 5)

レビューで発見: 7頭以下レースで `derive_probabilities(places=2)` が複勝を正しく
2着内ルールへ切り替える一方、**ワイドは常に「3着以内の組」定義のままλ3を適用**
していた。T59cの評価母集団は`field_under_8`を全件除外 (491件) しており、
ワイドの校正値 (λ3=0.720886) は8頭以上でしか検証されていない。実データ検証で
7頭の合成レースのワイド確率総和が3.0になること (真に2着以内定義なら1.0になる
はずの値) を確認し、未検証母集団への無断外挿と判定した。

**修正 (ユーザー承認のうえFable 5が実施)**:
- `derive_probabilities`: `places==2` のときワイドの計算自体を行わず `None` を返す
  (第3位条件付き確率の計算ループごと省略・効率化も兼ねる)。複勝・馬連は不変
- `build_board`: ワイドが `None` の場合、当該券種のみ `unavailable_kinds` に
  「7頭以下のため対象外 (T59cの検証母集団は8頭以上に限定)」を記録し、他券種
  (複勝・馬連) は通常どおり表示する (券種ごとの独立した対象外扱い)
- `snapshot_rows`: 券種別sentinel行の理由をboard全体のstatusではなく
  `unavailable_kinds` から取得するよう修正 (以前はboard status="ok"のとき
  ワイド不在行に空の理由が入るバグがあった)
- `index_boards.html`: 対象外券種は空テーブルでなく理由文を表示
- 新規テスト2件追加 (`test_seven_runner_wide_is_marked_unavailable_not_extrapolated`
  ・既存の7頭テストにワイド`None`の確認を追加)。禁止語彙テストにも7頭ケースを追加
- umaren (馬連) はλ=1固定で場サイズ依存の払戻ルールも無いため対象外扱いにしていない
  (T59cの除外対象だがフィット済みパラメータへの依存が無く外挿リスクはない)

509テスト全体パス。`combo_probs.py`/`win5_ml_model.json`のSHAは変更前後で不変を再確認。
