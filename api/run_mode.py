"""SPEC-T83: 閲覧モード (監視ループ・通知を抑止して起動する) の判定。

環境変数 JRA_VIEWER_MODE を読み、監視ループ・通知・prospective書き込みを
抑止する「閲覧モード」かどうかを判定する。2台目PC (ノート) で本番PCと同時に
アプリを動かしても、監視・通知が二重に走らないようにするための入口。

判定は呼ばれるたびに環境変数を読み直す (import時にキャッシュしない)。
起動時に決まる2値のモードとして扱い、実行中の切り替えは想定しない
(呼び出し側で一度だけ判定し、以降はその結果を使うことを想定しているが、
 このモジュール自体はいつ呼ばれても同じ規則で判定する)。
"""

import os

_TRUE_VALUES = {"1", "true", "on", "yes"}


def viewer_mode():
    """JRA_VIEWER_MODE が真値ならTrue。呼ばれるたびに環境変数を読む。"""
    raw = os.environ.get("JRA_VIEWER_MODE", "")
    return raw.strip().lower() in _TRUE_VALUES


def viewer_mode_reason():
    """ログ・API・画面表示で共通に使う理由文言。"""
    return "閲覧モード — 監視ループと通知は停止しています (このPCでは記録・通知を行いません)"
