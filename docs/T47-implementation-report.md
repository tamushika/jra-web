# T47 取消・除外ステータス配線 — 実装報告

実装日: 2026-07-19。デプロイ・suite再起動は未実施。

- `api/index.py` に位置ベースの `_current_entry_status()` を追加。
- 戻り値は `normal` / `scratched` / `excluded`。判定例外は `normal` へフェイルセーフ。
- 過去走セルより後は検査せず、馬名の部分一致も検査しない。
- `parse_horse_row()` は `status` と `scratched` を常に返す。
- `_active_runners()` を人気順、確率正規化、採点の直前に適用。T40の
  `_snapshot_runner()` と合わせ、取消・除外馬をfield sizeから除外する。
- 保存fixtureで通常・取消・発走除外・過去走取消・壊れた行を検証した。

通知時刻・EV閾値・通知送信条件には変更なし。
全体回帰: **448 passed / 2 skipped**。
