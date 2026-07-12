# SPEC T25: ev_log_report.py を SQLite ロギング基盤に付け替え

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T25。

## 背景
`ev_log_report.py` (EV通知の実測回収レポート) は Neon の `ev_alert_log` テーブルを参照して
いるが、このテーブルへの書き込み (`jra_ev.py` の `_log_alert_to_neon()`) は失敗をWARNで
握りつぶす設計で、実際には全期間0件のまま機能していない。一方、同じアラートは SQLite の
ロギング基盤 (`data/jra_logging.db`、`api/logging_store.py`) の `ev_evaluations` /
`notifications` に正しく記録されている。レポートをSQLite側に付け替えて一本化する。

## 要求仕様
1. `ev_log_report.py` のデータソースを `data/jra_logging.db` に変更する:
   - アラート: `ev_evaluations` (decision='alert') + `notifications` (channel/status)
   - 結果: `race_results` (finish_position, win_payout 等。`api/result_service.py` が充填)
   - レポート内容は現行を踏襲: 通知件数、的中数、100円均等買いの実測回収率
     (単勝・複勝)、通知時オッズ→確定オッズのドリフト。チャネル別 (LINE/browser) 集計を追加
2. DBは読み取り専用で開く (`file:...?mode=ro&immutable=0` 等)。書き込みしない。
3. Neon の `ev_alert_log` 参照と `jra_ev.py` の `_log_alert_to_neon()` は削除する
   (SQLiteに一本化。呼び出し箇所も除去し、他に参照が無いことを確認)。
4. `--since YYYYMMDD` オプションは維持。
5. docs/HANDOFF.md の定例表にある `python ev_log_report.py` の説明が変わる場合は追記修正。

## 受け入れ基準
- `python ev_log_report.py --since 20260712` が 2026-07-12 の実データ
  (alert 31件・LINE送信15件) を表示する
- race_results が未充填のレースは「結果待ち」として件数表示 (エラーにしない)
- jra_ev.py から ev_alert_log への参照が消え、`python -m py_compile jra_ev.py ev_log_report.py` OK
- 監視サーバーの通知動作に影響なし (SQLite記録経路は無変更)

## やらないこと
- Neon 側テーブルの削除 (DBスキーマは触らない。コード参照だけ外す)
- result_service の取得ロジック変更
