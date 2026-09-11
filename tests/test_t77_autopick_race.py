"""SPEC-T77 (「解析開始」完了時にレース詳細タブへ次のレースを自動読み込み) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T77-global-start-autopick-race.md §3
  - script.js の `// ---- T77 race pick pure functions (begin/end) ----`
    マーカー区間だけを抜き出し、Node (window/document 非依存であること込み) で
    評価して pickNextRace の数値を検算する (§1・§3-1 の検算値どおり)。
  - Node が無い環境では該当テストを skip する。
  - jra_suite.py に autopick=1 と jra-analysis-done が含まれる。
  - script.js に autopick と jra-analysis-done の処理が含まれる。
  - 既存 T73/T73b/T74/T76 のテストは別途 pytest 実行で確認する (このファイルは
    追加した文字列/純関数の検証のみを担う)。
"""
import json
import os
import shutil
import subprocess

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)

BEGIN_MARKER = "// ---- T77 race pick pure functions (begin) ----"
END_MARKER = "// ---- T77 race pick pure functions (end) ----"

NODE_AVAILABLE = shutil.which("node") is not None


def _read(relpath, base=REPO_DIR):
    path = os.path.join(base, relpath)
    with open(path, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


def _extract_pure_block():
    src = _read("script.js")
    b = src.find(BEGIN_MARKER)
    e = src.find(END_MARKER)
    assert b != -1, "T77 begin marker not found in script.js"
    assert e != -1, "T77 end marker not found in script.js"
    assert b < e, "T77 markers are out of order"
    return src[b + len(BEGIN_MARKER):e]


def _run_node(js_expr_lines, tmp_path):
    """マーカー区間 + 追加のJS式 (各行 console.log(JSON.stringify(...))) をNodeで評価する。"""
    snippet = _extract_pure_block()
    driver = "\n".join(js_expr_lines)
    script = snippet + "\n" + driver + "\n"
    script_path = tmp_path / "t77_pure.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


RACES_JSON = json.dumps([
    {"start_time": "09:45", "url": "a"},
    {"start_time": "10:10", "url": "b"},
    {"start_time": "10:40", "url": "c"},
])


# ─── §3.1: pickNextRace の純関数検算 ────────────────────────────────────────

@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pure_block_has_no_window_or_document_reference():
    snippet = _extract_pure_block()
    assert "window" not in snippet
    assert "document" not in snippet


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_picks_first_race_after_now(tmp_path):
    out = _run_node([
        f"console.log(JSON.stringify(pickNextRace({RACES_JSON}, '10:00')));",
    ], tmp_path)
    (picked,) = out
    assert picked["url"] == "b"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_same_start_time_is_included(tmp_path):
    out = _run_node([
        f"console.log(JSON.stringify(pickNextRace({RACES_JSON}, '10:10')));",
    ], tmp_path)
    (picked,) = out
    assert picked["url"] == "b"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_falls_back_to_first_when_all_past(tmp_path):
    out = _run_node([
        f"console.log(JSON.stringify(pickNextRace({RACES_JSON}, '17:00')));",
    ], tmp_path)
    (picked,) = out
    assert picked["url"] == "a"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_before_first_returns_first(tmp_path):
    out = _run_node([
        f"console.log(JSON.stringify(pickNextRace({RACES_JSON}, '09:00')));",
    ], tmp_path)
    (picked,) = out
    assert picked["url"] == "a"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_ignores_races_without_url(tmp_path):
    races = json.dumps([
        {"start_time": "09:00"},
        {"start_time": "09:30", "url": None},
    ])
    out = _run_node([
        f"console.log(JSON.stringify(pickNextRace({races}, '08:00')));",
    ], tmp_path)
    (picked,) = out
    assert picked is None


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pick_next_race_empty_array_returns_null(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(pickNextRace([], '10:00')));",
    ], tmp_path)
    (picked,) = out
    assert picked is None


# ─── §3.2: ファイル内の文字列検査 ───────────────────────────────────────────

def test_jra_suite_py_has_autopick_query_param():
    assert "autopick=1" in _read("jra_suite.py")


def test_jra_suite_py_sends_analysis_done_message():
    assert "jra-analysis-done" in _read("jra_suite.py")


def test_script_js_handles_autopick_query_param():
    js = _read("script.js")
    assert "autopick" in js
    assert "autoPickRaceFromEvState" in js


def test_script_js_listens_for_analysis_done_message():
    js = _read("script.js")
    assert "jra-analysis-done" in js
    assert "addEventListener('message'" in js


def test_script_js_exposes_pick_next_race_on_window():
    js = _read("script.js")
    assert "window.JRA_RACE_PICK" in js
