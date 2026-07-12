# SPEC T28: サーバー起動時のポート二重バインド防止

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T28。

## 背景
Windows + Python の Flask 開発サーバーは SO_REUSEADDR の挙動により、既に別プロセスが
LISTEN しているポートに二重バインドできてしまう。実害が2回発生している:
- port 5003 (EV監視): 旧コードのプロセスが残ったまま新プロセスを起動 → 旧が応答し続けた
- port 5004 (実績ダッシュボード): ウィンドウなしの旧 pythonw プロセスが残り、
  再起動しても修正前コードが応答し続けた (原因究明に時間を浪費)

## 要求仕様
1. `api/port_guard.py` を新規作成:
   ```
   def ensure_port_free(port, name="server"):
       """既にLISTENしているプロセスがあれば、わかるメッセージを出して sys.exit(1)"""
   ```
   - 判定は 127.0.0.1:port への TCP 接続試行 (connect成功=使用中) が簡便で確実
   - 使用中の場合のエラーメッセージに: ポート番号、想定される原因 (前回プロセスの残存)、
     対処 (タスクマネージャで python/pythonw を終了するか `Get-NetTCPConnection -LocalPort <port>`
     で PID 特定) を日本語で表示
2. 以下の4サーバーの起動直前 (`app.run` の前、`if __name__ == "__main__":` ブロック内) に組み込む:
   - `jra_app.py` (port 5001)
   - `jra_win5.py` (port 5002)
   - `jra_ev.py` (port 5003)
   - `jra_perf.py` (port 5004)
   ※ 各ファイルの PORT 定数を使うこと。組み込みは各2行以内 (import + 呼び出し) に収める
3. テスト: tests/test_port_guard.py — (a) 空きポートでは何も起きない、
   (b) テスト内でソケットをbind+listenした状態で ensure_port_free が SystemExit を出す、の2件

## 受け入れ基準
- `python -m pytest tests/test_port_guard.py -q` パス、既存テストが壊れていない
- 4サーバーすべてに組み込み済み (`python -m py_compile` OK)
- 動作確認: いずれか1サーバーを起動した状態で同じサーバーをもう一度起動すると、
  即座に日本語エラーで終了する (実行中サーバーが無い環境なら jra_perf.py で確認し、
  確認後に起動したプロセスは自分で終了してよい — **自分が起動したもの以外は殺さない**)

## やらないこと
- 既存プロセスの自動killや自動リトライ (誤爆リスクの方が大きい。検出とエラー表示のみ)
- SO_EXCLUSIVEADDRUSE などソケットオプションの変更 (Flask内部への介入はしない)
