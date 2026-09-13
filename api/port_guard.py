"""ローカルサーバーのポート重複起動を検出する。"""

import socket
import sys


def is_port_in_use(port):
    """指定ポートに127.0.0.1で接続できればTrue (既にLISTEN中)。"""
    port = int(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def ensure_port_free(port, name="server"):
    """既にLISTEN中のプロセスがあれば、案内を表示して終了する。"""
    port = int(port)
    if not is_port_in_use(port):
        return

    print(
        f"[ERROR] {name}を起動できません: ポート {port} は既に使用中です。\n"
        "前回起動したプロセスが残っている可能性があります。\n"
        "タスク マネージャーで python.exe / pythonw.exe を終了するか、\n"
        f"PowerShellで Get-NetTCPConnection -LocalPort {port} を実行してPIDを確認してください。",
        file=sys.stderr,
    )
    raise SystemExit(1)
