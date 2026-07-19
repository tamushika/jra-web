from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extract_chokketsu_graph import (
    ValidationError, normalize_course, normalize_course_on_page, normalize_page,
)


def test_course_normalization_is_fixed_and_handles_book_spelling():
    assert normalize_course(" 東京・芝2400m ") == "東京芝2400"
    assert normalize_course("新潟芝2000m（外回り）") == "新潟芝2000外"
    assert normalize_course("京都ダート1800m") == "京都ダ1800"
    with pytest.raises(ValidationError, match="unknown course vocabulary"):
        normalize_course("東京芝1750m")
    assert normalize_course("中京芝3000m") == "中京芝3000"


def test_visual_correction_is_page_limited():
    assert normalize_course_on_page("中京芝1600m内", 146) == "京都芝1600内"
    with pytest.raises(ValidationError, match="unknown course vocabulary"):
        normalize_course_on_page("中京芝1600m内", 145)


def test_page_can_hold_multiple_tables_and_only_printed_classes():
    payload = {
        "tables": [
            {"target_course": "東京芝2500m", "groups": [{
                "class_group": "古馬 重賞・オープン",
                "s_rank": ["東京芝2500m", "札幌芝2600m"],
                "a_rank": ["中京芝2000m"],
            }]},
            {"target_course": "東京芝3400m", "groups": [{
                "class_group": "古馬重賞OP",
                "s_rank": ["東京芝3400m"], "a_rank": ["京都芝3200m"],
            }]},
        ]
    }
    rows = normalize_page(payload, source_index=140, filename="scan.pdf")
    assert [(r["target_course"], r["class_group"], r["page"]) for r in rows] == [
        ("東京芝2500", "古馬重賞OP", 141),
        ("東京芝3400", "古馬重賞OP", 141),
    ]


def test_unknown_rank_item_fails_the_whole_page():
    payload = {"tables": [{"target_course": "東京芝1400", "groups": [{
        "class_group": "2歳・3歳限定戦", "s_rank": ["謎芝1400"], "a_rank": [],
    }]}]}
    with pytest.raises(ValidationError, match="unknown course vocabulary"):
        normalize_page(payload, source_index=135, filename="scan.pdf")


def test_printed_rank_duplicates_are_preserved_for_audit():
    payload = {"tables": [{"target_course": "中京芝1400", "groups": [{
        "class_group": "古馬条件未勝利",
        "s_rank": ["中京芝1400"],
        "a_rank": ["阪神芝1600", "阪神芝1600"],
    }]}]}
    rows = normalize_page(payload, source_index=179, filename="scan.pdf")
    assert rows[0]["a_rank"] == ["阪神芝1600", "阪神芝1600"]
