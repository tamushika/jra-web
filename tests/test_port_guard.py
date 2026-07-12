import socket

import pytest

from api.port_guard import ensure_port_free


def test_free_port_does_not_raise():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    ensure_port_free(port, "テストサーバー")


def test_listening_port_exits_with_japanese_guidance(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        with pytest.raises(SystemExit) as exc_info:
            ensure_port_free(port, "テストサーバー")

    assert exc_info.value.code == 1
    message = capsys.readouterr().err
    assert str(port) in message
    assert "既に使用中" in message
    assert f"Get-NetTCPConnection -LocalPort {port}" in message
