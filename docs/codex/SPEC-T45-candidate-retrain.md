# SPEC T45: 定期再学習 candidate bundle + baseline再固定 (E1)

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T45。
背景: [RE-accuracy-ideas-20260717.md](RE-accuracy-ideas-20260717.md) §8.1、T33完了ログ。
起票: 2026-07-18 上位モデル。

**⚠️ 実行順序の制約 (最重要)**: 本タスクは `extend_ability_from_neon` 再同期で
**ability.db を更新する**。T41/T43等の進行中実験は現在の ability.db SHA-256 を
台帳に封印して実行中のため、**それらの実行が完了するまで本タスクを開始しない**。
再同期後は新しいSHAを以後の台帳登録に使う。

## 1. 目的

T34b完了でNeon→ability.db再同期が安全になった。T33で「評価標準として採用」した
ability as-of統計を使うcandidate bundleを作成し、現行本番artifactと同一集団で
比較する。**自動デプロイ禁止** — 採用ゲート通過を上位モデルが確認した場合のみ
本番切替を別途実施する。あわせて、この比較結果を**T39のprospective評価の
baseline固定** (freeze宣言) に使う。

## 2. 手順 (下位モデル実行)

1. 進行中実験の完了を上位モデルに確認
2. `extend_ability_from_neon` で2026年分を再同期 (T34b後の初回。
   fukusho_pay退避・復元の既知配慮を確認)。実行前に ability.db をバックアップ
   (コピー) し、再同期前後の行数差分・重複0を検証して報告
3. 再学習パイプライン (retrain_all.bat 相当の構成要素) を **candidate出力先**
   (本番artifactを上書きしないパス) で実行: ability as-of統計 (T33) +
  mined_rules_v2 + 現行FEATURES
4. 同一集団比較: candidate vs 現行本番artifact を、WIN5評価母集団
   (9R以降・8頭以上・オッズあり) と全レース集団の両方で、
   2025 / 2026H1 / 2026-07以降 (取得可能分) について並記。
   市場ベースライン (人気順) も同一集団で併記 (T4標準)
5. 数値を上位モデルへ報告 (解釈・切替判断はしない)

## 3. 検証・安全条件

- 本番artifact・jra_ev.py・Webサーバー設定に一切触れない (candidateはスクラッチ領域)
- バックテストに `--write` を付けない
- 再同期の差分検証: 追加行はすべて2026年、既存行の変更0、
  自然キー重複0 (T34bのUNIQUE indexが機能していることの実地確認になる)
- 乱数seed固定・candidate生成の決定論SHA報告

## 4. 上位モデル側の後続 (参考)

比較結果を確認後、①切替可否の判断 ②T39台帳へのbaseline freeze宣言
(prospective_start_date = 次開催日) ③新ability.db SHAの封印、を行う。
