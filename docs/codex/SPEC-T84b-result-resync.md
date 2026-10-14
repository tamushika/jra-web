# SPEC-T84b: 部分取り込みのレース結果を後から取り直す (re-sync)

起票: 2026-10-15 (Opus 5)。T84 Stage A で根本原因が確定したことを受けた本命の修正。

## 0. 確定した事実 (T84 Stage A・実測)

- 結果ページの構造は原因ではない。影響レースを**今取り直すと全馬取れる** (修正前のパーサでも)。
- 取得タイミングも原因ではない。影響レース 38 件と正常レース 741 件で、発走から取得までの
  中央値はともに **16 分** (影響: 最小 11 / 最大 17、正常: 最小 12 / 最大 702)。
- したがって真因は「**発走 16 分後の時点では約 5% のレースで JRA の全着順がまだ上位 5 頭しか出ておらず、
  結果は一度しか取得しないので、その 5% が永久に部分データのまま残る**」。
- 現状: 839 レース中 41 件が 5 行 (うち 1 件は実際に 5 頭立てで正常、**40 件が欠落**)。
  T84 で `partial_result_rows` フラグによる**検知**は入ったが、取り直しは無い。

## 1. 変更 (re-sync の仕組み)

`api/result_service.py` に再同期の入口を追加する。**決定ロジック・通知・仮想購入には触れない。**

1. `find_incomplete_results(db_path, *, max_age_days=60) -> list[dict]`:
   `race_results` を読み、次のいずれかに該当するレースを返す (読み取り専用)。
   - `data_quality_flags_json` に `partial_result_rows` を含む
   - または、保存行数が同レースの `odds_snapshots.field_size` に対し明らかに少ない
     (`field_size >= 8` かつ `rows <= field_size - 3`。T84 の判定式と同じ)
   返す各要素に `race_id`, `rows`, `field_size`, `source_url`, `result_fetched_at` を含める。
   **`source_url` が無いレースは対象外** (取り直し先が特定できないため。件数は報告する)。
2. `resync_result(race_id, *, db_path, sleep_seconds=1.5) -> dict`:
   保存済みの `source_url` を 1 回だけ取得し、`parse_result_html` で解析して**行を upsert** する。
   - 既存行と同じ `horse_id` は上書き (着順・払戻が確定後の値に揃う)、無い行は追加。
   - **再取得で行数が増えなかった場合は何も書かず** `{"status": "no_change"}` を返す (無駄な更新を避ける)。
   - 取得後の行数が依然として不十分なら `partial_result_rows` フラグは残したままにする (次回また対象になる)。
   - 取得失敗 (HTTP エラー・解析不能) は書き込まず `{"status": "failed", "error": …}`。
3. `resync_incomplete_results(db_path, *, limit=50, sleep_seconds=1.5) -> dict`:
   1 を呼んで対象を列挙し、古い順に最大 `limit` 件まで 2 を実行する。
   **リクエスト間隔は最低 1.5 秒**。結果を `{"targets": n, "resynced": n, "no_change": n, "failed": n, "details": [...]}` で返す。

## 2. 定期実行への接続

- `jra_ev.worker_analyze_all` の完了フック (T82/T79c と同じ場所) に、**1 日 1 回まで**の再同期を追加する。
  - 直近の実行時刻をロギング DB の `meta` 相当 (無ければ新規の小テーブル `resync_runs(kind TEXT PK, last_run_at TEXT)`) に記録し、
    24 時間以内に実行済みならスキップ。
  - 1 回あたり `limit=20` に制限 (最大 30 秒程度)。失敗しても解析は止めない (警告ログのみ)。
  - **閲覧モード (T83) では実行しない** (`api.run_mode.viewer_mode()` を見る。ノート PC から二重に取りに行かない)。
- CLI も用意する: `python -X utf8 resync_results.py [--limit N] [--dry-run]`。`--dry-run` は対象一覧だけ出す。

## 3. 過去 40 件の穴埋め (Stage C 相当)

実装後、`resync_results.py --limit 50` を **1 回だけ**実行して既存の 40 件を埋める。
- リクエストは最大 40 回・間隔 1.5 秒 (所要 約 1 分)。取得先は既に保存済みの `source_url` のみ。
- 実行前後で `race_results` の総行数・5 行レースの件数を報告する。
- **この実行は本 SPEC の範囲内** (ユーザー承認済みの作業の一部)。それ以上の再取得はしない。

## 4. 影響の確認 (報告のみ)

穴埋めによって値が変わり得る箇所を確認して報告する。**修正はしない。**
- `virtual_bets`: T70d で「外れ」として精算済みの 6 件は、行が増えても外れのまま (複勝圏外は確定) — 変わらないことを確認する。
- 実績ダッシュボードのレース別明細: 6 着以下が表示されるようになる (改善)。
- T62b / T63 / T17a: 勝ち馬 (1 着) は元から取れているため影響なし — 1 着行が変化しないことを確認する。

## 5. 制約

- `race_results` への書き込みは §1-2 の upsert と §3 の 1 回のみ。他テーブルは `resync_runs` の作成のみ。
- `ability.db`・本番モデル・通知関数 3 つ・`virtual_betting` の決定/精算ロジックは無変更。
- 稼働中サーバー (5005) に接続・再起動しない。`git checkout/stash/reset/commit/push` 禁止。
  `docs/TASKS.md`・`eval/` は触らない。コミットしない。

## 6. テスト (`tests/test_t84b_resync.py`)

1. `find_incomplete_results`: フラグ付き / 行数不足 / 正常 (5 頭立て) / `source_url` 無し の判定。
2. `resync_result`: 行が増えるケースで upsert される / 増えないケースで書き込まない / 取得失敗で書き込まない。
3. 既存行の着順・払戻が確定値で上書きされる (同一 `horse_id`)。
4. `resync_incomplete_results`: `limit` と間隔が守られる (`time.sleep` を monkeypatch して呼び出し回数を確認)。
5. 日次ガード: 24 時間以内の再実行がスキップされる。
6. 閲覧モードでは実行されない。
7. ネットワークは monkeypatch し、テストから実アクセスしない。
8. 既存 `tests/test_result_service.py` `tests/test_t84_result_parser.py` `tests/test_t70d_settlement.py` が通る。

## 7. 報告

変更ファイル、テスト結果、§3 の実行結果 (対象件数・取り直せた件数・失敗・実行前後の 5 行レース件数)、
§4 の確認結果、SPEC からの逸脱。
