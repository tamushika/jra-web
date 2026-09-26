# SPEC-T83: 閲覧モード (監視ループ・通知を抑止して起動する)

起票: 2026-09-26 (Opus 5)。ユーザー要望: 「アプリ内に監視、通知を抑止するモードを作成できる？ 着手をお願いします。閲覧モードも用意してください」。
2 台目の PC (ノート) で同じアプリを使うため ([laptop-setup.md](../laptop-setup.md))、**監視・通知・prospective 書き込みを一切行わない起動モード**を追加する。

## 0. 背景 (調査済み)

`.env` の `EV_LINE_CHANNEL_TOKEN` を空にするだけでは通知しか止まらず、定期オッズ保存・
確信度スナップショット・仮想購入の決定は動き続ける。監視が始まる経路は 4 つある。

| # | 経路 | 起きること |
|---|---|---|
| 1 | `jra_suite.start_background_loops()` | `jra_ev.scheduler_loop` と `jra_win5._watch_loop` をスレッド起動 |
| 2 | `jra_ev._ensure_scheduler()` (jra_ev.py:818 解析完了時 / :1306 起動復元時) | 同じ `scheduler_loop` を別経路で起動 |
| 3 | `jra_ev.scheduler_loop` の本体 | 30/15/10/5/2 分前の `snapshot_odds` (T17/T40)・`_capture_race_confidence` (T62b)・`virtual_betting.record_decision_for_snapshot_id` (T70)・`refresh_and_alert` による 15/5 分前通知 |
| 4 | `jra_ev._restore_phase2_state()` (jra_ev.py:1307-1321) | 起動時にログ DB の未送信通知を **再送** する。デスクトップの `data/jra_logging.db` をコピーしたノート PC で通知が飛ぶ |

**凍結制約**: `_send_line` / `_send_discord` / `refresh_and_alert` の 3 関数はソース sha256 が
`tests/test_t62b_shadow.py` で固定されている。**本体を 1 文字も変更してはならない**。
抑止はすべて呼び出し側に置く。

## 1. 設計

- 起動時に決まる 2 値のモード。実行中に切り替えない (誤操作で監視が中途半端に始まるのを防ぐ)。
- 入口は環境変数 `JRA_VIEWER_MODE`。`1` / `true` / `on` / `yes` (大文字小文字不問・前後空白可) を真とする。
  それ以外・未設定は通常モード。`jra_suite.py --viewer` を付けるとプロセス先頭で同変数を `1` に設定する。
- 判定は新規モジュール `api/run_mode.py` の `viewer_mode() -> bool` に集約し、**呼ばれるたびに環境変数を読む**
  (import 時にキャッシュしない。テストで monkeypatch できるようにするため)。理由文字列を返す
  `viewer_mode_reason()` も用意し、ログ・API・画面で同じ文言を使う。

### 1.1 抑止する動作 (閲覧モード時)

| 対象 | 実装箇所 | 挙動 |
|---|---|---|
| EV 監視ループ | `jra_suite.start_background_loops()` | 起動しない。1 行ログ「閲覧モード: 監視ループを起動しません」 |
| WIN5 監視ループ | 同上 | 起動しない |
| スケジューラの別経路起動 | `jra_ev._ensure_scheduler()` 冒頭 | 即 return (`_SCHEDULER_STARTED` も立てない) |
| スケジューラ本体 | `jra_ev.scheduler_loop()` 冒頭 | 1 行ログを出して即 return (多重防御) |
| 未送信通知の再送 | `jra_ev._restore_phase2_state()` の `for pending in store.retryable_notifications():` ブロック | **ブロックごとスキップ**。DB の行は触らない (pending のまま残し、デスクトップ側で送れるようにする) |
| WIN5 締切前監視 | `jra_win5._watch_loop()` 冒頭 | 即 return |
| WIN5 確信度通知 (T68) | `jra_win5.py:600` の `_notify_win5_confidence(...)` 呼び出し | 呼ばない (多重防御) |
| 疎通テスト送信 | `jra_ev.py` の `--test-line` 分岐 | 送らずに「閲覧モードでは送信できません」と表示して exit 1 |

### 1.2 抑止しない動作 (閲覧モードでも使える)

手動の「🏇 解析開始」(`/ev/api/analyze_start` → `worker_analyze_all`)、WIN5 の対象レース取得と
買い目生成、レース詳細、重賞データ、実績ダッシュボードの閲覧、T82 開催カレンダー・T79c 週末重賞の
取得。`prediction_runs` / `predictions` へのローカル記録も従来どおり残す
(T63/T70/T62b/T17 の prospective 契約はいずれも `odds_snapshots` / `virtual_bets` /
`race_confidence_snapshots` 側で成立しており、`predictions` の記録は契約に影響しない)。

### 1.3 画面表示

- `/ev/api/state` の応答に `viewer_mode: true|false` を追加 (既存キーは変更しない)。
- 統合版タブシェル (`jra_suite._PORTAL_TEMPLATE`): 閲覧モードのとき、タブバーの下に帯を出す。
  文言「閲覧モード — 監視ループと通知は停止しています (このPCでは記録・通知を行いません)」。
  背景は警告色 (`#f5c842` 系) ではなく落ち着いた青系 (`#2d4a6b`) にし、エラーと区別する。
- ループ稼働バッジ (`_portal_loop_badges`) は閲覧モードのとき「停止中 (閲覧モード)」を表示する。
- オッズ監視ページ (`index_ev.html`) の上部にも同じ帯を出す (`/ev/api/state` の `viewer_mode` を見る)。

### 1.4 起動スクリプト

新規 `start_suite_viewer.bat`: `start_suite.bat` と同じ Python 解決 (リポジトリ同梱 venv 優先) で
`set "JRA_VIEWER_MODE=1"` を設定してから `jra_suite.py` を起動する。タイトルに「(閲覧モード)」を含める。
`--auto-start` は付けない。既存の `start_suite.bat` は変更しない (通常モードのまま)。

## 2. 制約

- `_send_line` / `_send_discord` / `refresh_and_alert` の本体は変更禁止 (T62b のハッシュ固定)。
- 通常モード (環境変数なし) の挙動・通知条件・ログ出力は完全に不変であること。
- `api/index.py`・本番 Web (Vercel) は変更しない。DB スキーマ変更なし。
- 稼働中の統合サーバー (port 5005) に接続・再起動しない。コミットしない。

## 3. テスト (`tests/test_t83_viewer_mode.py`)

1. `viewer_mode()` の真偽判定 (`1/true/TRUE/on/yes/ ` 前後空白 → 真、`0/false/空/未設定/other` → 偽)。
2. 閲覧モード: `start_background_loops()` がスレッドを 1 つも起動しない (`threading.Thread` を monkeypatch して呼び出し回数 0)。`_SCHEDULER_STARTED` / `_WATCH_THREAD` が立たない。
3. 閲覧モード: `_ensure_scheduler()` が即 return し `_SCHEDULER_STARTED` を立てない。
4. 閲覧モード: `scheduler_loop()` が即 return する (内部で `snapshot_odds` を monkeypatch して呼ばれないことを確認)。
5. 閲覧モード: `_restore_phase2_state()` が `_send_line` / `_send_discord` を呼ばない (両方を「呼ばれたら例外」に monkeypatch)。`store.retryable_notifications()` の行が pending のまま残る (`mark_notification` が呼ばれない)。
6. 閲覧モード: `jra_win5._watch_loop()` が即 return する。
7. 通常モード: 上記 2〜6 がすべて従来どおり動く (スレッド起動・送信関数呼び出しが行われる)。
8. `/ev/api/state` に `viewer_mode` が入り、値がモードと一致する。
9. ポータル HTML に閲覧モードの帯が出る / 通常モードでは出ない。`index_ev.html` に `viewer_mode` を読む処理がある (文字列検査)。
10. `start_suite_viewer.bat` が `JRA_VIEWER_MODE=1` を設定し、リポジトリ同梱 venv を優先している (文字列検査)。
11. 既存の凍結ハッシュテスト (`tests/test_t62b_shadow.py`) が通る。
12. 全体回帰。

## 4. 受け入れ (手動・ユーザー)

1. ノート PC で `start_suite_viewer.bat` を実行すると、画面上部に閲覧モードの帯が出て、稼働バッジが「停止中 (閲覧モード)」になる。
2. その状態で「🏇 解析開始」を押すと解析は走り、レース詳細・重賞データ・WIN5 の買い目は使える。LINE 通知は来ない。
3. デスクトップで `start_suite.bat` を実行したときは従来どおり監視と通知が動く。
