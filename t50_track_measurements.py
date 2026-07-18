"""T50 Stage 1: collect JRA moisture/cushion archives into local SQLite.

This is an isolated research data path.  It does not change production features.
The official archive has two PDF layouts: 2021-24 (legacy grouped columns) and
2025+ (one measurement date per row).  Friday pre-measurements are retained for
provenance but marked ``is_race_day=0``; later feature work must use race-day
rows only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
ARCHIVE_URL = "https://www.jra.go.jp/keiba/baba/archive/{year}.html"
USER_AGENT = "Mozilla/5.0 (compatible; jra-web T50 research collector)"
VENUES = {
    "sapporo": "札幌", "hakodate": "函館", "fukushima": "福島",
    "niigata": "新潟", "tokyo": "東京", "nakayama": "中山",
    "chukyo": "中京", "kyoto": "京都", "hanshin": "阪神",
    "kokura": "小倉",
}
IMAGE_ONLY_TRANSCRIPTS = {
    # 2021 第6回中京 is an image-only official PDF.  The SHA lock prevents a
    # transcription from being applied if JRA replaces the source document.
    "a255b27b72b40a7282964f01a32ce0989035935bcd6ab8666b8522f3b8ea2d55":
        Path(__file__).parent / "docs" / "codex" / "fixtures" /
        "T50-2021-chukyo06-transcription.txt",
}


class CollectionHalted(RuntimeError):
    """Raised after the configured number of consecutive HTTP failures."""


class ParseError(ValueError):
    """Raised when an official PDF does not match either approved layout."""


@dataclass(frozen=True)
class Measurement:
    date: str
    venue: str
    is_race_day: int
    turf_moisture_goal: float | None = None
    turf_moisture_4c: float | None = None
    dirt_moisture_goal: float | None = None
    dirt_moisture_4c: float | None = None
    cushion: float | None = None
    published_at: str | None = None
    cushion_measured_at: str | None = None
    moisture_measured_at: str | None = None
    source_url: str = ""
    fetched_at: str = ""
    format_version: str = ""
    pdf_sha256: str = ""


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = (value.replace("\u3000", " ").replace("−", "-")
             .replace("–", "-").replace("～", "~"))
    # One 2022 text layer contains ``8..8`` although the visible cell is 8.8.
    return re.sub(r"(?<=\d)\.{2,}(?=\d)", ".", value)


def _decimals(value: str) -> list[float]:
    return [float(v) for v in re.findall(r"(?<!\d)(\d{1,3}\.\d)(?!\d)", value)]


def _iso_day(year: int, month: int, day: int) -> str:
    return date(year, month, day).isoformat()


def _iso_measured(day: str, hhmm: str | None) -> str | None:
    if not hhmm:
        return None
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, tzinfo=JST).isoformat(timespec="seconds")


def _legacy_headers(text: str) -> list[re.Match[str]]:
    pattern = re.compile(
        r"(?P<days>第\s*\d+\s*日(?:\s*・\s*第\s*\d+\s*日)*)"
        r".*?[（(]\s*(?P<year>\d{4})年\s*(?P<m1>\d{1,2})月\s*(?P<d1>\d{1,2})日?"
        r"\s*[~\-]\s*(?:(?P<m2>\d{1,2})月\s*)?(?P<d2>\d{1,2})日?\s*[）)]",
        re.DOTALL,
    )
    return list(pattern.finditer(text))


def _legacy_rows(block: str) -> dict[str, list[float]]:
    """Read the five numeric rows from a layout-preserving legacy text block."""
    rows: dict[str, list[float]] = {}
    surface: str | None = None
    pending: str | None = None
    for raw in block.splitlines():
        line = _norm(raw).strip()
        if not line:
            continue
        if "ダート" in line:
            surface = "dirt"
        elif "芝コース" in line or re.search(r"(?:^|\s)芝(?:\s|$)", line):
            surface = "turf"

        key = None
        if "クッション値" in line:
            key = "cushion"
        elif "ゴール前" in line and surface:
            key = f"{surface}_moisture_goal"
        elif re.search(r"4\s*コーナー", line) and surface:
            key = f"{surface}_moisture_4c"

        values = _decimals(line)
        if key and values:
            rows[key] = values
            pending = None
        elif key:
            pending = key
        elif pending and values:
            rows[pending] = values
            pending = None
        elif values and "cushion" not in rows and surface is None:
            # Default pypdf extraction moves the repeated cushion label to the
            # page footer, leaving its first numeric row after the date header.
            rows["cushion"] = values
    return rows


def parse_legacy_text(text: str, venue: str, *, source_url: str = "",
                      fetched_at: str = "", pdf_sha256: str = "") -> list[Measurement]:
    text = _norm(text)
    headers = _legacy_headers(text)
    if not headers:
        raise ParseError("legacy meeting/date headers not found")
    output: list[Measurement] = []
    for index, header in enumerate(headers):
        block = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
        year, m1, d1 = (int(header.group(name)) for name in ("year", "m1", "d1"))
        m2 = int(header.group("m2") or m1)
        d2 = int(header.group("d2"))
        start, end = date(year, m1, d1), date(year, m2, d2)
        if end < start or (end - start).days > 7:
            raise ParseError(f"invalid legacy date range: {start}..{end}")
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        race_count = len(re.findall(r"第\s*\d+\s*日", header.group("days")))
        rows = _legacy_rows(block)
        required = {"cushion", "turf_moisture_goal", "turf_moisture_4c",
                    "dirt_moisture_goal", "dirt_moisture_4c"}
        if not required.issubset(rows):
            raise ParseError(f"legacy rows missing: {sorted(required - rows.keys())}")

        # Official legacy tables normally contain Friday + each race day.  If a
        # PDF omits Friday, align its columns to the final race_count dates.
        column_count = max(len(values) for values in rows.values())
        mapped_days = days[-column_count:]
        if len(mapped_days) != column_count:
            raise ParseError("legacy value columns exceed date range")
        race_days = set(days[-race_count:])
        for column, measured_day in enumerate(mapped_days):
            values = {key: (row[column] if column < len(row) else None)
                      for key, row in rows.items()}
            output.append(Measurement(
                date=measured_day.isoformat(), venue=venue,
                is_race_day=int(measured_day in race_days),
                turf_moisture_goal=values.get("turf_moisture_goal"),
                turf_moisture_4c=values.get("turf_moisture_4c"),
                dirt_moisture_goal=values.get("dirt_moisture_goal"),
                dirt_moisture_4c=values.get("dirt_moisture_4c"),
                cushion=values.get("cushion"), published_at=None,
                source_url=source_url, fetched_at=fetched_at,
                format_version="legacy-2021-2024", pdf_sha256=pdf_sha256,
            ))
    return output


def _modern_chunks(text: str) -> Iterable[tuple[re.Match[str], str, int]]:
    date_pattern = re.compile(r"(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日")
    matches = list(date_pattern.finditer(text))
    for index, match in enumerate(matches):
        # Include the same line prefix because it contains 開催日次.
        line_start = text.rfind("\n", 0, match.start()) + 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield match, text[line_start:end], match.start() - line_start


def parse_modern_text(text: str, venue: str, *, source_url: str = "",
                      fetched_at: str = "", pdf_sha256: str = "") -> list[Measurement]:
    text = _norm(text)
    year_match = re.search(r"(?P<year>20\d{2})年", text)
    if not year_match:
        raise ParseError("modern year not found")
    year = int(year_match.group("year"))
    output: list[Measurement] = []
    for match, chunk, local_date_start in _modern_chunks(text):
        # Header examples and prose can also contain dates; a data row has five
        # decimal measurements (cushion + four moisture readings).
        after_date = chunk[local_date_start + len(match.group(0)):]
        values = _decimals(after_date)
        times = re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)", after_date)
        if len(values) < 5 or len(times) < 2:
            continue
        day = _iso_day(year, int(match.group("month")), int(match.group("day")))
        prefix = chunk[:local_date_start]
        # An unnumbered pre-measurement can be on Saturday before a
        # Sunday/Monday holiday meeting.  開催日次, not weekday, is authoritative.
        is_race_day = bool(re.search(r"第\s*\d+\s*日", prefix))
        output.append(Measurement(
            date=day, venue=venue, is_race_day=int(is_race_day),
            cushion=values[0], turf_moisture_goal=values[1],
            turf_moisture_4c=values[2], dirt_moisture_goal=values[3],
            dirt_moisture_4c=values[4], published_at=None,
            cushion_measured_at=_iso_measured(day, times[0]),
            moisture_measured_at=_iso_measured(day, times[1]),
            source_url=source_url, fetched_at=fetched_at,
            format_version="modern-2025+", pdf_sha256=pdf_sha256,
        ))
    if not output:
        raise ParseError("modern measurement rows not found")
    return output


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise RuntimeError("pypdf is required: pip install -r requirements.txt") from exc
    reader = PdfReader(str(path))
    # Default extraction preserves logical cell order in legacy PDFs. Layout
    # mode can visually interleave adjacent turf/dirt cells (e.g. ``10.84.2``).
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def parse_pdf(path: Path, venue: str, *, year: int, source_url: str,
              fetched_at: str, pdf_sha256: str) -> list[Measurement]:
    text = extract_pdf_text(path)
    if not text.strip() and pdf_sha256 in IMAGE_ONLY_TRANSCRIPTS:
        text = IMAGE_ONLY_TRANSCRIPTS[pdf_sha256].read_text(encoding="utf-8")
    parser = parse_modern_text if year >= 2025 else parse_legacy_text
    return parser(text, venue, source_url=source_url, fetched_at=fetched_at,
                  pdf_sha256=pdf_sha256)


SCHEMA = """
CREATE TABLE IF NOT EXISTS track_measurements (
    date TEXT NOT NULL, venue TEXT NOT NULL, is_race_day INTEGER NOT NULL,
    turf_moisture_goal REAL, turf_moisture_4c REAL,
    dirt_moisture_goal REAL, dirt_moisture_4c REAL, cushion REAL,
    published_at TEXT, cushion_measured_at TEXT, moisture_measured_at TEXT,
    source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
    format_version TEXT NOT NULL, pdf_sha256 TEXT NOT NULL,
    PRIMARY KEY (date, venue)
);
CREATE TABLE IF NOT EXISTS download_manifest (
    source_url TEXT PRIMARY KEY, year INTEGER NOT NULL, venue TEXT NOT NULL,
    meeting_no INTEGER NOT NULL, local_path TEXT NOT NULL,
    pdf_sha256 TEXT, fetched_at TEXT, status TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_t50_race_day ON track_measurements(is_race_day, date, venue);
"""


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def save_measurements(connection: sqlite3.Connection,
                      rows: Iterable[Measurement]) -> int:
    columns = list(Measurement.__dataclass_fields__)
    sql = (f"INSERT INTO track_measurements ({','.join(columns)}) "
           f"VALUES ({','.join('?' for _ in columns)}) "
           "ON CONFLICT(date,venue) DO UPDATE SET " +
           ",".join(f"{column}=excluded.{column}" for column in columns[2:]))
    payload = [tuple(asdict(row)[column] for column in columns) for row in rows]
    connection.executemany(sql, payload)
    connection.commit()
    return len(payload)


def reconcile_race_day_flags(t50_db: Path, race_calendar_db: Path) -> dict:
    """Reconcile legacy date columns against actual JRA venue/date racing.

    Legacy holiday blocks may alternate pre-measurement and race dates, while
    their PDF header only lists a date range and meeting-day count.  ability.db
    supplies only the JRA calendar here; every measurement value still comes
    exclusively from the official archive PDF.
    """
    with closing(sqlite3.connect(race_calendar_db)) as calendar:
        race_days = {(str(day), venue) for day, venue in calendar.execute(
            """SELECT DISTINCT date, place FROM runs
                 WHERE date BETWEEN '20210101' AND '20261231'
                   AND rank IS NOT NULL""")}
    with closing(sqlite3.connect(t50_db)) as connection:
        rows = connection.execute(
            "SELECT date, venue, is_race_day FROM track_measurements").fetchall()
        updates = []
        for day, venue, old_flag in rows:
            new_flag = int((day.replace("-", ""), venue) in race_days)
            if new_flag != int(old_flag):
                updates.append((new_flag, day, venue))
        connection.executemany(
            "UPDATE track_measurements SET is_race_day=? WHERE date=? AND venue=?",
            updates)
        connection.commit()
        race_rows = connection.execute(
            "SELECT COUNT(*) FROM track_measurements WHERE is_race_day=1").fetchone()[0]
    return {"changed_flags": len(updates), "race_day_rows": race_rows,
            "calendar_days": len(race_days)}


class Fetcher:
    def __init__(self, *, session=None, min_interval: float = 1.0,
                 max_consecutive_failures: int = 10,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Referer": "https://www.jra.go.jp/"})
        self.min_interval = max(1.0, float(min_interval))
        self.max_consecutive_failures = max_consecutive_failures
        self.sleep, self.clock = sleep, clock
        self.last_request_at: float | None = None
        self.consecutive_failures = 0

    def get(self, url: str) -> bytes:
        if self.last_request_at is not None:
            self.sleep(max(0.0, self.min_interval - (self.clock() - self.last_request_at)))
        self.last_request_at = self.clock()
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_consecutive_failures:
                raise CollectionHalted(
                    f"stopped after {self.consecutive_failures} consecutive failures") from exc
            raise
        self.consecutive_failures = 0
        return response.content


def archive_links(html: bytes, year: int) -> list[tuple[str, str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    pattern = re.compile(rf"/archive/{year}pdf/([a-z]+)(\d{{2}})\.pdf$", re.I)
    for anchor in soup.find_all("a", href=True):
        url = urljoin(ARCHIVE_URL.format(year=year), anchor["href"])
        match = pattern.search(url)
        if match and match.group(1).lower() in VENUES:
            found[url] = (VENUES[match.group(1).lower()], int(match.group(2)))
    return [(url, venue, meeting) for url, (venue, meeting) in sorted(found.items())]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_if_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(payload)


def collect(*, years: Iterable[int], data_dir: Path, db_path: Path,
            refresh: bool = False, limit: int | None = None,
            fetcher: Fetcher | None = None) -> dict:
    fetcher = fetcher or Fetcher()
    connection = open_store(db_path)
    summary = {"pdf_discovered": 0, "pdf_downloaded": 0, "pdf_reused": 0,
               "pdf_attempted": 0, "pdf_parsed": 0, "rows_saved": 0,
               "parse_failures": []}
    try:
        for year in years:
            index_path = data_dir / "raw" / str(year) / "index.html"
            if index_path.exists() and not refresh:
                index_html = index_path.read_bytes()
            else:
                index_html = fetcher.get(ARCHIVE_URL.format(year=year))
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_bytes(index_html)
            links = archive_links(index_html, year)
            summary["pdf_discovered"] += len(links)
            for source_url, venue, meeting_no in links:
                if limit is not None and summary["pdf_attempted"] >= limit:
                    break
                summary["pdf_attempted"] += 1
                stem = re.search(r"/([^/]+)\.pdf$", source_url).group(1)
                pdf_path = data_dir / "raw" / str(year) / f"{stem}.pdf"
                digest = fetched_at = None
                try:
                    if pdf_path.exists() and pdf_path.stat().st_size > 0 and not refresh:
                        payload = pdf_path.read_bytes()
                        summary["pdf_reused"] += 1
                        fetched_at = datetime.fromtimestamp(
                            pdf_path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                    else:
                        payload = fetcher.get(source_url)
                        if not payload.startswith(b"%PDF"):
                            raise ParseError("response is not a PDF")
                        pdf_path.parent.mkdir(parents=True, exist_ok=True)
                        pdf_path.write_bytes(payload)
                        summary["pdf_downloaded"] += 1
                        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    digest = _sha256(payload)
                    rows = parse_pdf(pdf_path, venue, year=year, source_url=source_url,
                                     fetched_at=fetched_at, pdf_sha256=digest)
                    summary["rows_saved"] += save_measurements(connection, rows)
                    summary["pdf_parsed"] += 1
                    status, error = "parsed", None
                except CollectionHalted:
                    raise
                except Exception as exc:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"
                    summary["parse_failures"].append({"url": source_url, "error": error})
                connection.execute(
                    """INSERT INTO download_manifest
                       (source_url,year,venue,meeting_no,local_path,pdf_sha256,fetched_at,status,error)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_url) DO UPDATE SET
                       pdf_sha256=excluded.pdf_sha256,fetched_at=excluded.fetched_at,
                       status=excluded.status,error=excluded.error""",
                    (source_url, year, venue, meeting_no, str(pdf_path),
                     digest, fetched_at, status, error))
                connection.commit()
            if limit is not None and summary["pdf_attempted"] >= limit:
                break
    finally:
        connection.close()
    report_path = data_dir / "coverage.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-year", type=int, default=2021)
    parser.add_argument("--to-year", type=int, default=2026)
    parser.add_argument("--data-dir", type=Path, default=Path("data/t50"))
    parser.add_argument("--db", type=Path, default=Path("data/t50/track_measurements.sqlite"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--limit", type=int, help="parse at most N PDFs (smoke test)")
    parser.add_argument("--race-calendar-db", type=Path, default=Path("ability.db"),
                        help="JRA venue/date calendar for legacy holiday reconciliation")
    args = parser.parse_args()
    if args.from_year > args.to_year:
        parser.error("--from-year must be <= --to-year")
    result = collect(years=range(args.from_year, args.to_year + 1),
                     data_dir=args.data_dir, db_path=args.db,
                     refresh=args.refresh, limit=args.limit)
    if args.limit is None and args.race_calendar_db.exists():
        result["race_day_reconciliation"] = reconcile_race_day_flags(
            args.db, args.race_calendar_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["parse_failures"]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
