# SPEC-T80c: 枠割当修正後の採掘ルール再採掘と v2 との比較

起票: 2026-09-24 (Fable 5.1)。T80 で修正した `calculate_waku` は `backtest_criteria.py` (ルール適用の枠) と `mine_criteria.py` 経由の採掘に使われており、本番 `mined_rules_v2.csv` (330 本、うち枠番条件 24 本) は誤った枠で採掘された。本番のライブ適用 (`api/index.py`) は正しい枠なので、24 本は採掘時と異なる母集団に当たっている。**mined rules は Web 表示スコア (◎○△) のみに使われ、CL モデル・EV・仮想購入には無関係** (T33 注記) — 影響は表示側に限られる。

## 0. 不変条件
- ability.db 読み取り専用 (封印 `bbee39cc…990860a`)。本番 `mined_rules_v2.csv` は変更しない (差し替えは上位モデル裁定後)。5005 に触らない。`git checkout/stash/reset` 禁止。`docs/TASKS.md` は触らない。コミットしない。
- T15 と同じ手順: 発見 2021-2023 / 選抜 2024 (`mine_criteria.py` の既定 `--discover-from/--discover-to/--select-from/--select-to`)。パラメータ変更・探索なし。

## 1. 手順
1. `python -X utf8 mine_criteria.py --output outputs/t80c/mined_rules_v4.csv` を修正後の枠で実行 (2 回実行し sha256 一致を確認)。
2. 差分レポート: v2 と v4 の本数 (買/消)、完全一致ルール数、v2 のみ・v4 のみ (特に「枠番」条件を含むもの)、枠番条件ルールの本数の変化。
3. 評価: `backtest_criteria.py` で v2 と v4 を **同一母集団・同一期間** (固定テスト 2025 / 確認 2026H1、ability.db) に適用し、買いルール該当馬の 複勝率・単勝回収・複勝回収・件数、消しルール該当馬の同指標を表にする (T15/T23 の報告と同じ形式)。2024 (選抜期間) はリークのため参考値として別掲。
4. 出力: `outputs/t80c/report.json`、`docs/T80c-waku-rules-report.md` (表と差分一覧)。**採否の記述はしない**。
5. 台帳: `T80c-waku-rules-remine-v1` を事前登録 (primary_metric = 2025 買いルール複勝率 v4−v2、gate = 2025・2026H1 とも複勝率/複回収が v2 比で悪化しない、all_or_nothing)、実行後に `-result` 行を追記。`verify --no-update-state` OK を確認。

## 2. 報告
- 上記の表・差分・sha256 一致・台帳 verify・実行時間。ability.db sha256 が実行前後で不変であること。
