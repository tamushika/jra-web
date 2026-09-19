"""SPEC-T80 (past_data_service.calculate_waku の枠割当修正) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T80-waku-allocation-fix.md §1-2
  - api/past_data_service.py:calculate_waku, api/index.py:calculate_waku,
    fold_stats._calculate_waku の3実装が、9頭以上のレースで全馬番について
    一致すること (JRAの枠番規則: h<=8なら枠=馬番。h>8なら各枠 h//8 頭を基本とし、
    余り h%8 頭を外枠から1頭ずつ追加する)。
  - 境界値 (umaban<=0 / head_count<=0 / 非数 / umaban>head_count) で None を返すこと。
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_DIR = os.path.join(REPO_ROOT, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import past_data_service as pds  # noqa: E402
import index as idx  # noqa: E402
import fold_stats  # noqa: E402


HEAD_COUNTS = [9, 10, 12, 14, 16, 17, 18]


@pytest.mark.parametrize("head_count", HEAD_COUNTS)
def test_three_implementations_agree_for_all_umaban(head_count):
    """9〜18頭の各頭数について、全馬番で3実装の結果が一致すること。"""
    for umaban in range(1, head_count + 1):
        a = pds.calculate_waku(umaban, head_count)
        b = idx.calculate_waku(umaban, head_count)
        c = fold_stats._calculate_waku(umaban, head_count)
        assert a == b == c, (
            f"mismatch at umaban={umaban}, head_count={head_count}: "
            f"past_data_service={a}, index={b}, fold_stats={c}"
        )
        assert a is not None
        assert 1 <= a <= 8


# ─── JRA規則の具体例 (SPEC §0) ────────────────────────────────────────────────

@pytest.mark.parametrize("head_count,expected_sizes", [
    (16, [2, 2, 2, 2, 2, 2, 2, 2]),
    (18, [2, 2, 2, 2, 2, 2, 3, 3]),
    (12, [1, 1, 1, 1, 2, 2, 2, 2]),
    (10, [1, 1, 1, 1, 1, 1, 2, 2]),
])
def test_past_data_service_matches_jra_frame_sizes(head_count, expected_sizes):
    """past_data_service.calculate_waku が仕様書 §0 の具体例どおりの枠割当になること。"""
    counts = [0] * 8
    for umaban in range(1, head_count + 1):
        waku = pds.calculate_waku(umaban, head_count)
        assert waku is not None
        counts[waku - 1] += 1
    assert counts == expected_sizes


@pytest.mark.parametrize("head_count", [1, 5, 8])
def test_small_fields_frame_equals_umaban(head_count):
    """8頭以下では枠=馬番であること (3実装共通)。"""
    for umaban in range(1, head_count + 1):
        assert pds.calculate_waku(umaban, head_count) == umaban
        assert idx.calculate_waku(umaban, head_count) == umaban
        assert fold_stats._calculate_waku(umaban, head_count) == umaban


# ─── past_data_service.calculate_waku の境界値 ────────────────────────────────

@pytest.mark.parametrize("umaban,head_count", [
    (0, 16),
    (-1, 16),
    (16, 0),
    (16, -1),
    (None, 16),
    (16, None),
    ("abc", 16),
    (16, "abc"),
    (17, 16),  # umaban > head_count
])
def test_calculate_waku_returns_none_on_invalid_input(umaban, head_count):
    assert pds.calculate_waku(umaban, head_count) is None
