"""SPEC-T76 (コースレイアウト図への風向・風速オーバーレイ) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T76-wind-map-overlay.md §3
  - script.js の `// ---- T76 wind pure functions (begin/end) ----` マーカー
    区間だけを抜き出し、Node (window/document 非依存であること込み) で評価して
    computeWindScreenAngles / classifyWindVsCourse / windSpeedCategory の
    数値を検算する (§1.1・§3 の検算値どおり)。
  - Node が無い環境では該当テストを skip する。
  - script.js / index.html / style.css / win5_predictor/venue_info.py に
    必要な文字列 (windspeed_unit=ms, id="windOverlay" など) が含まれること。
"""
import json
import os
import shutil
import subprocess

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
PARENT_DIR = os.path.dirname(REPO_DIR)

BEGIN_MARKER = "// ---- T76 wind pure functions (begin) ----"
END_MARKER = "// ---- T76 wind pure functions (end) ----"

NODE_AVAILABLE = shutil.which("node") is not None


def _read(relpath, base=REPO_DIR):
    path = os.path.join(base, relpath)
    with open(path, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


def _extract_pure_block():
    src = _read("script.js")
    b = src.find(BEGIN_MARKER)
    e = src.find(END_MARKER)
    assert b != -1, "T76 begin marker not found in script.js"
    assert e != -1, "T76 end marker not found in script.js"
    assert b < e, "T76 markers are out of order"
    return src[b + len(BEGIN_MARKER):e]


def _run_node(js_expr_lines, tmp_path):
    """マーカー区間 + 追加のJS式 (各行 console.log(JSON.stringify(...))) をNodeで評価する。"""
    snippet = _extract_pure_block()
    driver = "\n".join(js_expr_lines)
    script = snippet + "\n" + driver + "\n"
    script_path = tmp_path / "t76_pure.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ─── §3.1〜4: computeWindScreenAngles / classifyWindVsCourse ────────────────

@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_pure_block_has_no_window_or_document_reference():
    snippet = _extract_pure_block()
    assert "window" not in snippet
    assert "document" not in snippet


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_compute_wind_screen_angles_tokyo(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(computeWindScreenAngles('東京', 290)));",
        "console.log(JSON.stringify(computeWindScreenAngles('東京', 110)));",
    ], tmp_path)
    tokyo_head, tokyo_tail = out
    assert tokyo_head["thetaTo"] == 180
    assert tokyo_head["straight"] == "head"
    assert tokyo_head["handed"] == "left"
    assert tokyo_tail["thetaTo"] == 0
    assert tokyo_tail["straight"] == "tail"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_compute_wind_screen_angles_nakayama_right_handed(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(computeWindScreenAngles('中山', 140)));",
    ], tmp_path)
    (nakayama,) = out
    assert nakayama["thetaTo"] == 0
    assert nakayama["straight"] == "head"
    assert nakayama["handed"] == "right"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_compute_wind_screen_angles_north_and_unknown_venue(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(computeWindScreenAngles('東京', 0)));",
        "console.log(JSON.stringify(computeWindScreenAngles('存在しない競馬場', 0)));",
    ], tmp_path)
    tokyo, unknown = out
    assert tokyo["thetaNorth"] == 70
    assert unknown is None


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_classify_wind_vs_course_boundaries(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(classifyWindVsCourse(45, 0)));",
        "console.log(JSON.stringify(classifyWindVsCourse(46, 0)));",
        "console.log(JSON.stringify(classifyWindVsCourse(134, 0)));",
        "console.log(JSON.stringify(classifyWindVsCourse(135, 0)));",
        "console.log(JSON.stringify(classifyWindVsCourse(355, 5)));",
    ], tmp_path)
    diff45, diff46, diff134, diff135, wrap10 = out
    assert diff45["straight"] == "head"
    assert diff46["straight"] == "cross"
    assert diff134["straight"] == "cross"
    assert diff135["straight"] == "tail"
    assert wrap10["diff"] == 10
    assert wrap10["straight"] == "head"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node not available")
def test_wind_speed_category_boundaries(tmp_path):
    out = _run_node([
        "console.log(JSON.stringify(windSpeedCategory(0.2)));",
        "console.log(JSON.stringify(windSpeedCategory(3.9)));",
        "console.log(JSON.stringify(windSpeedCategory(10)));",
        "console.log(JSON.stringify(windSpeedCategory(15)));",
        "console.log(JSON.stringify(windSpeedCategory(20)));",
        "console.log(JSON.stringify(windSpeedCategory(30)));",
    ], tmp_path)
    calm, light, moderate, strong, very_strong, violent = out
    assert calm["label"] == "静穏"
    assert light["label"] == "微風"
    assert moderate["label"] == "やや強い風"
    assert strong["label"] == "強い風"
    assert very_strong["label"] == "非常に強い風"
    assert violent["label"] == "猛烈な風"


# ─── §3.6: ファイル内の文字列検査 ───────────────────────────────────────────

def test_script_js_uses_windspeed_unit_ms():
    assert "windspeed_unit=ms" in _read("script.js")


def test_index_html_has_wind_overlay_markup():
    html = _read("index.html")
    assert 'id="windOverlay"' in html
    assert 'class="course-map-wrap"' in html

    wrap_start = html.index('class="course-map-wrap"')
    wrap_open_tag_start = html.rindex("<div", 0, wrap_start)
    # 対応する </div> を簡易に探す (直後の最初の </div> がwrapの閉じタグである
    # ことは、img/svgのみを子に持つ現在のマークアップ構造から成立する)。
    wrap_end = html.index("</div>", wrap_start)
    img_pos = html.index('id="courseLayoutImage"')
    assert wrap_open_tag_start < img_pos < wrap_end


def test_style_css_has_reduced_motion_and_wind_flow():
    css = _read("style.css")
    assert "prefers-reduced-motion" in css
    assert "wind-flow" in css


def test_venue_info_py_has_windspeed_unit_ms():
    content = _read("venue_info.py", base=PARENT_DIR + os.sep + "win5_predictor")
    assert '"windspeed_unit": "ms"' in content


# ─── レビュー指摘分 (2026-09-11): 画像404フォールバックの表示崩れ・
#     バッジ/チップのテキストはみ出し・矢頭の視認性 ─────────────────────────

def test_style_css_has_course_map_wrap_fallback_class():
    # .course-map-wrap.fallback が幅/高さを持つことで、画像404時に
    # inline-blockのラッパーが0x0に潰れてSVGが消える問題を防ぐ。
    css = _read("style.css")
    assert "course-map-wrap.fallback" in css


def test_index_html_course_map_wrap_can_receive_fallback_class():
    # JSがcourseImage.closest('.course-map-wrap')でラッパーを取得して
    # classList.add('fallback') する前提のマークアップであること。
    html = _read("index.html")
    assert 'class="course-map-wrap"' in html


def test_script_js_uses_get_computed_text_length_for_chip_sizing():
    js = _read("script.js")
    assert "getComputedTextLength" in js


def test_script_js_toggles_fallback_class_on_image_load_state():
    js = _read("script.js")
    assert "courseMapWrap" in js
    assert "classList.add('fallback')" in js
    assert "classList.remove('fallback')" in js
