import json

import pytest

import extract_keiba_kagaku as subject


def test_target_pages_cover_only_spec_ranges():
    pages = subject.target_pages()
    assert len(pages) == 64
    assert len(set(pages)) == 64
    assert pages[:2] == [15, 16]
    assert pages[-2:] == [203, 204]
    assert 53 not in pages
    assert 156 not in pages
    assert subject.chapter_for_page(157).number == 9
    assert subject.source_index_for_page(31) == 29


def test_parse_model_json_accepts_fenced_json():
    parsed = subject.parse_model_json(
        '```json\n{"transcription":"本文123", "uncertain_fragments":["[判読不能: 図]"]}\n```'
    )
    assert parsed == {"transcription": "本文123", "uncertain_fragments": ["[判読不能: 図]"]}


def test_parse_model_json_rejects_truncated_output():
    with pytest.raises(ValueError, match="did not contain a JSON object"):
        subject.parse_model_json('{"transcription":"末尾なし')


def test_numeric_line_count_counts_lines_not_digits():
    assert subject.numeric_line_count("数字12と34\n本文\n割合5%") == 2


def test_compile_outputs_requires_every_page(tmp_path):
    with pytest.raises(FileNotFoundError, match="page_015"):
        subject.compile_outputs(tmp_path)


def test_compile_outputs_writes_numbered_plaintext_and_coverage(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for page in subject.target_pages():
        (raw / f"page_{page:03d}.json").write_text(
            json.dumps(
                {
                    "source_file": f"scan-{page}.pdf",
                    "transcription": f"ページ本文 {page}%",
                    "uncertain_fragments": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    subject.compile_outputs(tmp_path)
    chapter = (tmp_path / "chapter_01_p015-032.txt").read_text(encoding="utf-8")
    assert "===== p015 =====" in chapter
    assert "===== p032 =====" in chapter
    coverage = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["expected_pages"] == coverage["successful_pages"] == 64
    assert coverage["pages_with_numeric_lines"] == 64
