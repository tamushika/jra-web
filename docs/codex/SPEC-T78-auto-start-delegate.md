# SPEC-T78: 週末自動起動時にスイートが既に稼働中なら、起動をあきらめず稼働中プロセスへ解析開始を依頼する

起票: 2026-09-13 (Fable 5.1)。

## 0. 発生した事象 (2026-09-13、開催日のデータ欠落)

- 9/11 (金) 19:14 にユーザーが `start_suite.bat` で手動起動 (`--auto-start` 無し)。9/11 夜に 9/12 の出馬表を手動で解析 → 9/12 (土) の監視・スナップショット・仮想購入 (20件) は正常に記録された。
- 9/12・9/13 とも 8:30 にタスクスケジューラ `JRA_Suite_Monitor` (`launch_suite_hidden.vbs` → `start_suite_auto.bat` → `jra_suite.py --auto-start`) は起動したが、**9/11 の手動プロセスがポート 5005 を占有していたため `ensure_port_free` で exit 1** (`suite_monitor.log` 末尾に ERROR が 2 回)。
- 手動起動プロセスには日次の自動解析が無いため、9/13 (日) の出馬表は誰も解析せず、**9/13 の板オッズスナップショット・仮想購入 (T70 P1/P5/P3)・レース確信度スナップショット・WIN5 シャドーが全て欠落** (`data/jra_logging.db`: `board_odds_snapshots`/`virtual_bets`/`race_confidence_snapshots` に 20260913 の行が無い)。実時間データのため事後復元は不可能。
- ユーザーは 9/13 23:41 に再度手動起動しており、**同じ構図が 9/19 (土) 8:30 に再発する**。

根本原因: ポートガードは「二重起動の防止」としては正しいが、`--auto-start` の目的 (開催日の朝に解析を必ず始める) がプロセスの有無に依存してしまっている。

## 1. 設計
- `jra_suite.py --auto-start` で起動したとき、ポート 5005 が既に LISTEN 中なら、**新プロセスの起動はあきらめ、稼働中プロセスに HTTP で解析開始を依頼して exit 0** する。
  - 依頼先: `POST http://127.0.0.1:5005/ev/api/analyze_start` (body `{}` = サーバー側の現在の閾値をそのまま使う。T74 と同じ)。
  - 200 → 「[auto-start] 既に稼働中の統合サーバーに解析開始を依頼しました」を出力して exit 0。
  - 409 (解析実行中) → 「既に解析実行中」を出力して exit 0 (成功扱い)。
  - 接続失敗・その他の HTTP エラー (5005 を別物が占有している等) → 従来どおりポートガードの案内を出して exit 1。
- `--auto-start` 無し (手動起動) の挙動は不変 (従来どおり案内して exit 1。二重起動防止はそのまま)。
- WIN5 側の取得 (`/win5/api/win5_races` → 各レース解析 → 買い目) はブラウザ主導の多段フローなのでサーバー側からは依頼しない (現状どおりユーザーが「🏇 解析開始」を押す。本 SPEC の範囲外として記録)。
- 新しい依存は追加しない (`urllib.request` を使う)。

## 2. 変更仕様

### 2.1 `api/port_guard.py`
1. `is_port_in_use(port) -> bool` を追加 (現在 `ensure_port_free` の中にある接続試行を切り出す)。`ensure_port_free` はこれを使う形にリファクタし、挙動 (案内文・`SystemExit(1)`) は不変。

### 2.2 `jra_suite.py`
1. `delegate_auto_start_to_running_server(port, timeout=5.0) -> tuple[bool, str]` を追加。
   - `urllib.request.Request(f"http://127.0.0.1:{port}/ev/api/analyze_start", data=b"{}", method="POST", headers={"Content-Type": "application/json"})` を `urlopen` する。
   - 200 系 → `(True, "既に稼働中の統合サーバーに解析開始を依頼しました")`。`HTTPError` で code 409 → `(True, "既に解析実行中です")`。その他の `HTTPError` / `URLError` / 例外 → `(False, str(e))`。
2. `__main__`:
   ```python
   if "--auto-start" in sys.argv and is_port_in_use(PORT):
       ok, msg = delegate_auto_start_to_running_server(PORT)
       print(f"  [auto-start] {msg}")
       if ok:
           sys.exit(0)
       # 依頼できなければ従来どおりポートガードの案内で終了 (別プロセスが5005を占有)
   ensure_port_free(PORT, "統合サーバー (jra_suite)")
   ...
   ```
   出力は `suite_monitor.log` に残るので、日付が分かるよう `datetime.now().strftime('%Y-%m-%d %H:%M:%S')` を先頭に付ける。
3. それ以外 (create_app / start_background_loops / 6 秒後の `_auto_start`) は不変。

### 2.3 `start_suite_auto.bat` / `launch_suite_hidden.vbs`
変更不要 (exit 0 で静かに終わる)。コメントに「稼働中なら解析開始を依頼して終了する (SPEC-T78)」を 1 行足す。

## 3. テスト (`tests/test_t78_auto_start_delegate.py`)
1. `is_port_in_use`: 空きポートで False、`socket` で LISTEN したポートで True (`tests/test_port_guard.py` の作法に合わせる)。`ensure_port_free` の既存テストが通る。
2. `delegate_auto_start_to_running_server`: `urllib.request.urlopen` を monkeypatch し、
   - 200 を返すダミー → `(True, …)`、渡された Request の URL が `/ev/api/analyze_start`・method `POST`・body `b"{}"`。
   - `urllib.error.HTTPError(code=409)` を投げる → `(True, "既に解析実行中です")`。
   - `urllib.error.URLError` を投げる → `(False, …)`。
3. 実サーバー経路 1 本: `jra_suite.create_app()` を `werkzeug.serving.make_server` で空きポートに立て (別スレッド)、`jra_ev.worker_analyze_all` を monkeypatch した上で `delegate_auto_start_to_running_server(その port)` が `(True, …)` を返し、`jra_ev.STATE["status"]` が `analyzing` になること。終了時に `server.shutdown()`。
4. 文字列検査: `jra_suite.py` の `__main__` ブロックで `is_port_in_use` の判定が `ensure_port_free` より前にある。
5. 既存 `tests/test_jra_suite.py` `tests/test_port_guard.py` `tests/test_t74_global_start.py` が通る。

## 4. 受け入れ (手動・ユーザー)
1. スイートを手動起動したまま `start_suite_auto.bat` を実行すると、二重起動せずに稼働中の画面で解析が始まり、`suite_monitor.log` に日時付きで「解析開始を依頼しました」が残る。
2. 何も起動していない状態で `start_suite_auto.bat` を実行すると従来どおり起動して 6 秒後に解析が始まる。
3. 9/19 (土) 8:30 の `JRA_Suite_Monitor` で、手動プロセスの有無にかかわらず解析が始まる。

## 5. 記録
- 9/13 の欠落は T70 仮想運用 (prospective) の「未記録日」として TASKS.md に残す (損失ではなく欠測。ゲート集計の分母には入れない)。
