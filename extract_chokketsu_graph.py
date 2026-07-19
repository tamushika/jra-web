"""SPEC-T58 Stage 0: extract the chapter-5 course-transfer tables locally.

The source is a user-provided, one-page-per-PDF scan.  Only source indices
135..220 (the 86 table pages) are sent to the local Ollama endpoint.  Raw
transcriptions and the resulting graph stay under data/t58/ (gitignored).
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_PDF_DIR = Path(r"C:\Users\owner\Downloads\vFlat\PDF\直結BP馬券術")
DEFAULT_OUT_DIR = ROOT / "data" / "t58"
FIRST_SOURCE_INDEX = 135
LAST_SOURCE_INDEX = 220
MODEL = "qwen3-vl:8b"
VENUES = ("東京", "中山", "京都", "阪神", "中京", "新潟", "福島", "小倉", "札幌", "函館")
CLASS_GROUPS = ("古馬重賞OP", "古馬条件未勝利", "2-3歳限定")

_COURSE_LAYOUT = {
    "東京": {"芝": (1400, 1600, 1800, 2000, 2300, 2400, 2500, 3400), "ダ": (1300, 1400, 1600, 2100)},
    "中山": {"芝": (1200, 1600, 1800, 2000, 2200, 2500, 3600), "ダ": (1200, 1800, 2400, 2500)},
    "京都": {"芝": (1200, 1400, 1600, 1800, 2000, 2200, 2400, 3000, 3200), "ダ": (1200, 1400, 1800, 1900)},
    "阪神": {"芝": (1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 3000), "ダ": (1200, 1400, 1800, 2000)},
    "中京": {"芝": (1200, 1400, 1600, 2000, 2200), "ダ": (1200, 1400, 1800, 1900)},
    "新潟": {"芝": (1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400), "ダ": (1200, 1800, 2500)},
    "福島": {"芝": (1200, 1800, 2000, 2600), "ダ": (1150, 1700, 2400)},
    "小倉": {"芝": (1200, 1800, 2000, 2600), "ダ": (1000, 1700, 2400)},
    "札幌": {"芝": (1200, 1500, 1800, 2000, 2600), "ダ": (1000, 1700, 2400)},
    "函館": {"芝": (1000, 1200, 1800, 2000, 2600), "ダ": (1000, 1700, 2400)},
}
VALID_COURSES = {
    f"{venue}{surface}{distance}"
    for venue, surfaces in _COURSE_LAYOUT.items()
    for surface, distances in surfaces.items()
    for distance in distances
}
for _course in (
    "京都芝1400内", "京都芝1400外", "京都芝1600内", "京都芝1600外",
    "新潟芝1600外", "新潟芝1800外", "新潟芝2000内", "新潟芝2000外",
    # Historical temporary course printed on book page 174 (visually verified).
    "中京芝3000",
):
    VALID_COURSES.add(_course)

# Page-limited corrections are deliberately explicit: an invalid vocabulary item
# must never be silently guessed or globally aliased.  The scan on book page 147
# clearly reads 京都芝1600内; qwen repeatedly confused 京都 with 中京 in that cell.
REVIEWED_OCR_CORRECTIONS = {
    (146, "中京芝1600内"): "京都芝1600内",
}

PROMPT = """この画像にある競馬の直結コース表を、全表・全セル正確に転写してください。
1ページに複数の対象コース表があればすべて返します。各表には1つ以上のクラス行があり、
存在しないクラスを補完してはいけません。各行の「直結コースSランク」と
「直結コースAランク」に縦に並ぶ全コース名を、上から順に読み取ってください。
クラス名は 古馬重賞OP / 古馬条件未勝利 / 2-3歳限定 のいずれかに正規化します。
コース名は場名+芝/ダ+距離（内外の記載があれば含む）とし、mだけ除去します。
空セルは空配列。説明、推測、省略は禁止です。
JSON形式: {"tables":[{"target_course":"東京芝2400","groups":[
{"class_group":"古馬重賞OP","s_rank":["東京芝2400"],"a_rank":[]}]}]}"""


class ValidationError(ValueError):
    """The OCR response cannot be accepted without human correction."""


def normalize_course(value: Any) -> str:
    raw = str(value or "").strip()
    text = raw.replace("ダート", "ダ").replace("ｍ", "m")
    text = text.replace("（外回り）", "外").replace("(外回り)", "外")
    text = text.replace("（内回り）", "内").replace("(内回り)", "内")
    text = re.sub(r"[\s・･,，:：/／]", "", text)
    text = text.replace("外回り", "外").replace("内回り", "内").replace("m", "")
    if not text:
        raise ValidationError("empty course item")
    if text not in VALID_COURSES:
        raise ValidationError(f"unknown course vocabulary: {raw!r} -> {text!r}")
    return text


def normalize_course_on_page(value: Any, source_index: int) -> str:
    raw = str(value or "")
    try:
        return normalize_course(raw)
    except ValidationError:
        comparable = re.sub(r"[\s・･,，:：/／]", "", raw.replace("ｍ", "m"))
        comparable = comparable.replace("ダート", "ダ").replace("m", "")
        correction = REVIEWED_OCR_CORRECTIONS.get((source_index, comparable))
        if correction is None:
            raise
        return normalize_course(correction)


def normalize_class_group(value: Any) -> str:
    raw = str(value or "").strip()
    text = re.sub(r"[\s・･]", "", raw)
    if text.startswith("古馬") and ("重賞" in text or "オープン" in text or text.endswith("OP")):
        return "古馬重賞OP"
    if text.startswith("古馬") and ("条件" in text or "未勝利" in text):
        return "古馬条件未勝利"
    if ("2歳" in text and "3歳" in text) or text.startswith("2-3歳"):
        return "2-3歳限定"
    if raw in CLASS_GROUPS:
        return raw
    raise ValidationError(f"unknown class vocabulary: {raw!r}")


def _json_from_model_text(text: str) -> dict[str, Any]:
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValidationError("model returned no JSON object")
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid model JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("model JSON root is not an object")
    return value


def normalize_page(payload: dict[str, Any], *, source_index: int, filename: str) -> list[dict[str, Any]]:
    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValidationError("page has no tables")
    records: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise ValidationError("table is not an object")
        target = normalize_course_on_page(table.get("target_course"), source_index)
        if target in seen_targets:
            raise ValidationError(f"duplicate target on page: {target}")
        seen_targets.add(target)
        groups = table.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ValidationError(f"{target}: no class groups")
        seen_groups: set[str] = set()
        for group in groups:
            if not isinstance(group, dict):
                raise ValidationError(f"{target}: class row is not an object")
            class_group = normalize_class_group(group.get("class_group"))
            if class_group in seen_groups:
                raise ValidationError(f"{target}: duplicate class group {class_group}")
            seen_groups.add(class_group)
            ranks: dict[str, list[str]] = {}
            for key in ("s_rank", "a_rank"):
                values = group.get(key, [])
                if not isinstance(values, list):
                    raise ValidationError(f"{target}/{class_group}: {key} is not a list")
                # Preserve printed duplicates.  Pages 149 and 180 contain them in
                # the source itself; coverage reports them as data-quality warnings.
                normalized = [normalize_course_on_page(item, source_index)
                              for item in values if str(item or "").strip()]
                ranks[key] = normalized
            records.append({
                "target_course": target,
                "class_group": class_group,
                "s_rank": ranks["s_rank"],
                "a_rank": ranks["a_rank"],
                "page": source_index + 1,
                "source_index": source_index,
                "source_file": filename,
            })
    return records


def _load_cached_pages(raw_dir: Path) -> dict[int, dict[str, Any]]:
    pages = {}
    for path in sorted(raw_dir.glob("page_*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            pages[int(rec["source_index"])] = rec
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
    return pages


def _call_ollama(pdf_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    import fitz
    import requests

    with fitz.open(pdf_path) as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
    image = base64.b64encode(pix.tobytes("jpeg", jpg_quality=92)).decode("ascii")
    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": MODEL, "prompt": PROMPT, "images": [image], "format": "json",
            "stream": False, "options": {"temperature": 0, "seed": 58, "num_predict": 1800},
        },
        timeout=300,
    )
    response.raise_for_status()
    body = response.json()
    # qwen3-vl may put a complete JSON answer in `thinking` and leave response empty.
    model_text = body.get("response") or body.get("thinking") or ""
    return _json_from_model_text(model_text), {
        "model": MODEL,
        "eval_count": body.get("eval_count"),
        "total_duration": body.get("total_duration"),
        "model_text": model_text,
    }


def extract(pdf_dir: Path, out_dir: Path, force_pages: set[int]) -> None:
    files = sorted(pdf_dir.glob("*.pdf"))
    if len(files) != 222:
        raise SystemExit(f"expected 222 source PDFs, found {len(files)}: {pdf_dir}")
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cached = _load_cached_pages(raw_dir)
    failures: list[dict[str, Any]] = []
    for source_index in range(FIRST_SOURCE_INDEX, LAST_SOURCE_INDEX + 1):
        path = files[source_index - 1]
        if source_index in cached and source_index not in force_pages:
            print(f"[{source_index}/{LAST_SOURCE_INDEX}] cached {path.name}", flush=True)
            continue
        last_error = ""
        for attempt in range(1, 3):
            try:
                payload, inference = _call_ollama(path)
                records = normalize_page(payload, source_index=source_index, filename=path.name)
                raw = {
                    "status": "ok", "source_index": source_index, "page": source_index + 1,
                    "source_file": path.name, "payload": payload, "records": records, **inference,
                }
                (raw_dir / f"page_{source_index:03d}.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                cached[source_index] = raw
                print(f"[{source_index}/{LAST_SOURCE_INDEX}] ok tables={len(payload['tables'])} rows={len(records)}", flush=True)
                break
            except Exception as exc:  # retain explicit page-level failures for review
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"[{source_index}/{LAST_SOURCE_INDEX}] attempt {attempt} failed: {last_error}", flush=True)
        else:
            failures.append({"source_index": source_index, "page": source_index + 1,
                             "source_file": path.name, "error": last_error})
    build(out_dir, failures)


def build(out_dir: Path, extraction_failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pages = _load_cached_pages(out_dir / "raw")
    records: list[dict[str, Any]] = []
    failures = list(extraction_failures or [])
    for source_index in range(FIRST_SOURCE_INDEX, LAST_SOURCE_INDEX + 1):
        page = pages.get(source_index)
        if not page or page.get("status") != "ok":
            if not any(item["source_index"] == source_index for item in failures):
                failures.append({"source_index": source_index, "page": source_index + 1,
                                 "error": "missing successful raw record"})
            continue
        try:
            normalized = normalize_page(page["payload"], source_index=source_index,
                                        filename=page["source_file"])
            records.extend(normalized)
        except ValidationError as exc:
            failures.append({"source_index": source_index, "page": source_index + 1,
                             "error": str(exc)})

    pairs = [(r["target_course"], r["class_group"]) for r in records]
    duplicate_pairs = sorted(f"{a}/{b}" for (a, b), n in Counter(pairs).items() if n > 1)
    duplicate_rank_items = []
    for record in records:
        for rank in ("s_rank", "a_rank"):
            for course, count in Counter(record[rank]).items():
                if count > 1:
                    duplicate_rank_items.append({
                        "page": record["page"], "source_index": record["source_index"],
                        "target_course": record["target_course"],
                        "class_group": record["class_group"], "rank": rank,
                        "course": course, "count": count,
                    })
    targets = sorted({r["target_course"] for r in records}, key=lambda c: (VENUES.index(next(v for v in VENUES if c.startswith(v))), c))
    missing = [
        {"target_course": target, "class_group": class_group}
        for target in targets for class_group in CLASS_GROUPS
        if (target, class_group) not in set(pairs)
    ]
    venue_summary = {}
    for venue in VENUES:
        venue_targets = [target for target in targets if target.startswith(venue)]
        venue_rows = [r for r in records if r["target_course"].startswith(venue)]
        venue_summary[venue] = {
            "courses": len(venue_targets), "class_rows": len(venue_rows),
            "possible_rows": len(venue_targets) * len(CLASS_GROUPS),
            "missing_rows": len(venue_targets) * len(CLASS_GROUPS) - len(venue_rows),
        }
    coverage = {
        "source_index_range": [FIRST_SOURCE_INDEX, LAST_SOURCE_INDEX],
        "expected_pages": LAST_SOURCE_INDEX - FIRST_SOURCE_INDEX + 1,
        "successful_pages": len({r["source_index"] for r in records}),
        "records": len(records), "courses": len(targets),
        "unknown_or_failed_pages": failures, "duplicate_course_class_pairs": duplicate_pairs,
        "printed_duplicate_rank_items": duplicate_rank_items,
        "reviewed_ocr_corrections": [
            {"source_index": source_index, "page": source_index + 1,
             "from": source, "to": target, "basis": "high-resolution visual review"}
            for (source_index, source), target in REVIEWED_OCR_CORRECTIONS.items()
        ],
        "venue_summary": venue_summary, "missing_course_class_rows": missing,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_records = [
        {key: record[key] for key in ("target_course", "class_group", "s_rank", "a_rank", "page")}
        for record in records
    ]
    (out_dir / "chokketsu_graph.json").write_text(
        json.dumps(graph_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(coverage, ensure_ascii=False, indent=2), flush=True)
    if failures or duplicate_pairs:
        raise SystemExit("coverage gate failed; see data/t58/coverage.json")
    return coverage


def render_qa(pdf_dir: Path, out_dir: Path) -> list[int]:
    import fitz

    files = sorted(pdf_dir.glob("*.pdf"))
    indices = sorted(random.Random(58).sample(range(FIRST_SOURCE_INDEX, LAST_SOURCE_INDEX + 1), 10))
    qa_dir = out_dir / "qa_pages"
    qa_dir.mkdir(parents=True, exist_ok=True)
    for source_index in indices:
        with fitz.open(files[source_index - 1]) as doc:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        pix.save(qa_dir / f"page_{source_index:03d}_book_{source_index + 1:03d}.png")
    sample = {"seed": 58, "source_indices": indices, "book_pages": [i + 1 for i in indices]}
    (out_dir / "qa_sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(sample, ensure_ascii=False), flush=True)
    return indices


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--render-qa", action="store_true")
    parser.add_argument("--force-page", type=int, action="append", default=[])
    args = parser.parse_args()
    if args.build_only:
        build(args.out_dir)
    else:
        extract(args.pdf_dir, args.out_dir, set(args.force_page))
    if args.render_qa:
        render_qa(args.pdf_dir, args.out_dir)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
