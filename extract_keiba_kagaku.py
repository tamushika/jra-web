"""Local, resumable OCR for the page ranges approved in SPEC-T57 Stage 0."""

from __future__ import annotations

import argparse
import base64
import io
import json
import random
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image


MODELS = ("qwen3-vl:8b", "qwen2.5vl:7b", "qwen3-vl:4b", "qwen3.6:latest")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    first_page: int
    last_page: int

    @property
    def pages(self) -> range:
        return range(self.first_page, self.last_page + 1)


CHAPTERS = (
    Chapter(1, "データ馬券", 15, 32),
    Chapter(2, "季節性", 33, 52),
    Chapter(9, "暑さ", 157, 164),
    Chapter(11, "数学と物理", 187, 204),
)

PROMPT = """/no_think
この日本語書籍の1ページを忠実に転写してください。
要約・解説・推測はせず、見える本文、見出し、注記、表、数式、数字をページ上の順序で残してください。
縦書きは自然な日本語の文順に直してください。表は行ごとのプレーンテキストで構いません。
読めない箇所を勝手に補わず [判読不能] としてください。
特に数字、単位、範囲、割合、符号を慎重に読み取ってください。
転写本文だけを出力し、前置きや説明は書かないでください。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "transcription": {"type": "string"},
        "uncertain_fragments": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["transcription", "uncertain_fragments"],
}


def source_files(source: Path) -> list[Path]:
    files = sorted(source.glob("*.pdf"))
    if len(files) != 222:
        raise ValueError(f"expected 222 one-page PDFs, found {len(files)} in {source}")
    return files


def target_pages() -> list[int]:
    return [page for chapter in CHAPTERS for page in chapter.pages]


def chapter_for_page(page: int) -> Chapter:
    return next(chapter for chapter in CHAPTERS if page in chapter.pages)


def source_index_for_page(page: int) -> int:
    """Map the printed book page to the zero-based scan index (front matter offset = 2)."""
    return page - 2


def render_page(pdf_path: Path, scale: float = 1.65) -> Image.Image:
    document = fitz.open(pdf_path)
    try:
        page = document[0]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        document.close()
    return image


def image_jpeg_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=91, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("model output did not contain a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict) or not isinstance(value.get("transcription"), str):
        raise ValueError("model JSON lacked a string transcription")
    uncertainties = value.get("uncertain_fragments", [])
    if not isinstance(uncertainties, list):
        uncertainties = [str(uncertainties)]
    value["uncertain_fragments"] = [
        str(item) for item in uncertainties if "判読不能" in str(item)
    ]
    return value


def call_ollama(
    image: Image.Image, model: str = MODELS[0], timeout: int = 600
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [image_jpeg_base64(image)],
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "num_predict": 3072, "seed": 57},
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        envelope = json.loads(response.read().decode("utf-8"))
    candidate = (envelope.get("response") or envelope.get("thinking") or "").strip()
    return parse_model_json(candidate), envelope


def raw_path(output: Path, page: int) -> Path:
    return output / "raw" / f"page_{page:03d}.json"


def extract(
    source: Path,
    output: Path,
    force: bool = False,
    only_pages: list[int] | None = None,
    preferred_model: str | None = None,
) -> None:
    files = source_files(source)
    (output / "raw").mkdir(parents=True, exist_ok=True)
    pages = only_pages or target_pages()
    invalid_pages = sorted(set(pages) - set(target_pages()))
    if invalid_pages:
        raise ValueError(f"pages outside SPEC-T57 ranges: {invalid_pages}")
    preferred_by_chapter: dict[int, str] = {
        chapter.number: preferred_model for chapter in CHAPTERS if preferred_model
    }
    for position, page in enumerate(pages, start=1):
        chapter = chapter_for_page(page)
        destination = raw_path(output, page)
        if destination.exists() and not force:
            try:
                cached = json.loads(destination.read_text(encoding="utf-8"))
                cached_text = cached.get("transcription", "").strip()
                if (
                    cached_text
                    and len(cached_text) <= 5000
                    and not cached_text.startswith("<think>")
                    and cached.get("model") in MODELS
                    and cached.get("source_index") == source_index_for_page(page)
                ):
                    preferred_by_chapter[chapter.number] = cached["model"]
                    print(f"[{position:02d}/{len(pages)}] p{page:03d} cached", flush=True)
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        source_index = source_index_for_page(page)
        source_file = files[source_index]
        print(f"[{position:02d}/{len(pages)}] p{page:03d} extracting", flush=True)
        image = render_page(source_file)
        last_error: Exception | None = None
        selected_model = ""
        preferred = preferred_by_chapter.get(chapter.number, MODELS[0])
        model_order = (preferred,) + tuple(model for model in MODELS if model != preferred)
        for attempt, model in enumerate(model_order, start=1):
            try:
                parsed, envelope = call_ollama(image, model=model)
                selected_model = model
                break
            except (ValueError, json.JSONDecodeError) as error:
                last_error = error
                print(f"  p{page:03d} incomplete {model}, fallback {attempt}/{len(model_order)}", flush=True)
        else:
            raise RuntimeError(f"p{page}: all local models returned invalid output") from last_error
        transcription = parsed["transcription"].strip()
        record = {
            "book_page": page,
            "source_index": source_index,
            "source_file": source_file.name,
            "model": selected_model,
            "transcription": transcription or "[空白ページ]",
            "blank_page": not transcription,
            "uncertain_fragments": parsed["uncertain_fragments"],
            "metrics": {
                "total_duration": envelope.get("total_duration"),
                "eval_count": envelope.get("eval_count"),
            },
        }
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        preferred_by_chapter[chapter.number] = selected_model
    compile_outputs(output)


def load_record(output: Path, page: int) -> dict[str, Any]:
    path = raw_path(output, page)
    if not path.exists():
        raise FileNotFoundError(f"missing OCR result: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not record.get("transcription", "").strip():
        raise ValueError(f"empty OCR result: {path}")
    record["uncertain_fragments"] = [
        str(item) for item in record.get("uncertain_fragments", []) if "判読不能" in str(item)
    ]
    corrections_path = output / "reviewer_corrections.json"
    if corrections_path.exists():
        record = dict(record)
        for correction in json.loads(corrections_path.read_text(encoding="utf-8")).get("corrections", []):
            if correction["book_page"] != page:
                continue
            old = correction["old"]
            if record["transcription"].count(old) != 1:
                raise ValueError(f"p{page}: reviewer correction source was not unique: {old!r}")
            record["transcription"] = record["transcription"].replace(old, correction["new"])
    return record


def numeric_line_count(text: str) -> int:
    return sum(bool(re.search(r"\d", line)) for line in text.splitlines())


def compile_outputs(output: Path) -> None:
    coverage_pages = []
    for chapter in CHAPTERS:
        sections = []
        for page in chapter.pages:
            record = load_record(output, page)
            transcription = record["transcription"].strip()
            uncertain = record.get("uncertain_fragments", [])
            sections.append(
                f"===== p{page:03d} =====\n"
                f"source: {record['source_file']}\n\n{transcription}\n"
                + (f"\n[判読不確実: {' / '.join(uncertain)}]\n" if uncertain else "")
            )
            coverage_pages.append(
                {
                    "book_page": page,
                    "chapter": chapter.number,
                    "characters": len(transcription),
                    "numeric_lines": numeric_line_count(transcription),
                    "uncertain_fragments": len(uncertain),
                }
            )
        filename = f"chapter_{chapter.number:02d}_p{chapter.first_page:03d}-{chapter.last_page:03d}.txt"
        (output / filename).write_text("\n".join(sections), encoding="utf-8")
    corrections_path = output / "reviewer_corrections.json"
    correction_count = 0
    if corrections_path.exists():
        correction_count = len(json.loads(corrections_path.read_text(encoding="utf-8")).get("corrections", []))
    summary = {
        "models": list(MODELS),
        "expected_pages": len(target_pages()),
        "successful_pages": len(coverage_pages),
        "missing_pages": [],
        "total_characters": sum(item["characters"] for item in coverage_pages),
        "pages_with_numeric_lines": sum(item["numeric_lines"] > 0 for item in coverage_pages),
        "total_uncertain_fragments": sum(item["uncertain_fragments"] for item in coverage_pages),
        "reviewer_correction_count": correction_count,
        "short_pages_under_20_characters": [
            item["book_page"] for item in coverage_pages if item["characters"] < 20
        ],
        "pages": coverage_pages,
    }
    (output / "coverage.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def render_qa(source: Path, output: Path) -> None:
    files = source_files(source)
    randomizer = random.Random(57)
    selections = []
    qa_directory = output / "qa_pages"
    qa_directory.mkdir(parents=True, exist_ok=True)
    for chapter in CHAPTERS:
        records = [(page, load_record(output, page)) for page in chapter.pages]
        numeric_page = max(records, key=lambda item: (numeric_line_count(item[1]["transcription"]), -item[0]))[0]
        candidates = [page for page in chapter.pages if page != numeric_page]
        random_page = randomizer.choice(candidates)
        for page, reason in ((random_page, "seeded_random"), (numeric_page, "numeric_dense")):
            filename = f"chapter_{chapter.number:02d}_p{page:03d}.png"
            render_page(files[source_index_for_page(page)], scale=1.7).save(qa_directory / filename)
            selections.append(
                {
                    "chapter": chapter.number,
                    "book_page": page,
                    "reason": reason,
                    "numeric_lines": numeric_line_count(load_record(output, page)["transcription"]),
                    "image": str(Path("qa_pages") / filename),
                }
            )
    (output / "qa_sample.json").write_text(json.dumps(selections, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(selections, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/t57"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--compile-only", action="store_true")
    parser.add_argument("--render-qa", action="store_true")
    parser.add_argument("--pages", help="comma-separated subset of the approved book pages")
    parser.add_argument("--preferred-model", choices=MODELS)
    arguments = parser.parse_args()
    if arguments.compile_only:
        compile_outputs(arguments.output)
    else:
        selected_pages = [int(item) for item in arguments.pages.split(",")] if arguments.pages else None
        extract(
            arguments.source,
            arguments.output,
            force=arguments.force,
            only_pages=selected_pages,
            preferred_model=arguments.preferred_model,
        )
    if arguments.render_qa:
        render_qa(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
