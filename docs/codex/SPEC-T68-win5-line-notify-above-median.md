# SPEC-T68: WIN5推定的中率が中央値超のときLINE通知

作成: 2026-08-05 (Fable 5)。実装: jra-coder。レビュー・コミット: 上位モデル。

## 1. 目的

ユーザー要望 (2026-08-05):「win5の的中率が中央値を超える場合、LINEに通知をしてほしい」。

WIN5の**締切前監視** (jra_win5.py `_watch_loop`、発走15分前の自動再スコア) が買い目を
再計算したとき、推定的中率 (`est_hit_rate`) がその点数の2025年実測中央値
(`win5_weights.json` の `allocation.est_reference[str(points)][0]`) を**上回った場合のみ**、
LINEへ通知する。UIの「今週の確信度」と同じ判定のプッシュ版。

## 2. トリガーと条件

- **トリガーは締切前監視の再計算完了時のみ** (`_watch_loop` 内で `build_kaime` 成功後)。
  手動のSTEP3買い目生成 (`/api/win5_kaime`) では通知しない (クリックごとのスパム防止)
- 条件: `alloc_method == "prob"` かつ `est_reference[str(points)]` が存在し、
  `est_hit_rate > median` (median = est_reference[0]。「超える」なので strict greater)
- 監視1回の実行 (armed→done) につき通知は最大1通。再armしたら再判定してよい
- 条件を満たさない場合は何も送らない (「中央値未満でした」の類の通知はしない)

## 3. LINE送信

- **jra_ev.py は変更しない**。jra_win5.py に独立の送信ヘルパーを新設する
  (`_send_line_win5(text)` 等)。送信方法は jra_ev._send_line と同じ
  Messaging API broadcast (`https://api.line.me/v2/bot/message/broadcast`、
  `Bearer EV_LINE_CHANNEL_TOKEN`、timeout=10)。コードはコピーでよい (共通化のための
  jra_ev.py リファクタ禁止)
- ゲート: `EV_LINE_CHANNEL_TOKEN` 未設定なら送らない。加えて環境変数
  `WIN5_LINE_NOTIFY=0` で無効化できる (既定=有効)
- 送信失敗は WARN print のみで監視ループを壊さない (try/except)
- 送信結果 (sent/suppressed/failed) を WATCH dict に `line_notify` キーで記録し、
  `/api/win5_watch` の状態レスポンスに含める (UIでの確認用。UI変更は任意)

## 4. メッセージ文言 (プレーンテキスト)

```
🎯 WIN5確信度: 高め (中央値超え)
{日付} {点数}点 {formula}
①{場}: {選択馬番カンマ区切り}{ (軸) if is_axis}
②〜⑤ 同上 (kaime["picks"] 由来。2026-08-09 ユーザー要望で追加)
推定的中率 {est*100:.2f}% > 2025年中央値 {median*100:.1f}%
(上位25%点 {p75*100:.1f}%{、上位25%圏 if est >= p75})
※参考情報です (購入推奨ではありません)
```

- est >= p75 のときは1行目を「高め (上位25%圏)」にする
- **購入推奨の文言を入れない** (プロジェクト方針: 採用ゲート通過まで購入推奨をしない。
  UIの確信度表示と同じ「参考情報」の位置づけ)

## 5. 変更してよいファイル / 禁止事項

- 変更可: `jra_win5.py`, `tests/` (新規テストファイル可), `docs/scoring-win5.md` (§5運用上の注意に1項追記)
- **禁止**: `jra_ev.py`・EV通知条件 (LINE=5分前×EV≥1.3) の一切の変更、
  `win5_weights.json` の変更、`build_kaime` の計算ロジック変更 (通知判定の追加のみ)、
  DBスキーマ変更
- `_watch_loop` の既存フロー (armed→running→done/error、20秒poll、15分前判定) を変えない

## 6. テスト

新規 or 既存テストファイルに追加。requests.post はモックする (実送信しない):
1. est > median → 送信される (broadcast URL・トークンヘッダ・本文に est/median を含む)
2. est <= median → 送信されない
3. est >= p75 → 文言が「上位25%圏」になる
4. est_reference に該当点数が無い / alloc_method != "prob" / トークン未設定 /
   WIN5_LINE_NOTIFY=0 → 送信されない
5. requests.post が例外 → 監視ループが落ちず line_notify="failed" が記録される
6. 既存テスト全パス (`python -m pytest tests/ -q`、venv python)

## 7. 完了条件

- テスト全パス+リグレッションなし
- `python -m pytest tests/ -q` の件数を報告
- jra_ev.py に diff が無いことを `git diff --stat` で確認して報告
- コミットはしない (上位モデルがレビュー後に行う)
