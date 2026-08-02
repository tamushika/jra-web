"""SPEC-T42b Stage A: offline parser for the immutable T42 training cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, SoupStrainer


PARSER_VERSION = "t42b-stage-a-v2"
ROOT = Path(__file__).resolve().parent
DEFAULT_RAW = ROOT / "data" / "t42" / "raw"
DEFAULT_MANIFEST = ROOT / "data" / "t42" / "manifest.sqlite"
DEFAULT_OUTPUT = ROOT / "data" / "t42" / "t42_training.sqlite"
DEFAULT_ABILITY = ROOT / "ability.db"
DEFAULT_SUMMARY = ROOT / "outputs" / "t42b_stage_a.json"

PLACE_BY_CODE = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
COURSE_NORMALIZATION = {
    "美坂": "美浦坂路", "栗坂": "栗東坂路",
    "美Ｗ": "美浦W", "美南Ｗ": "美浦W", "美南W": "美浦W",
    "南Ｗ": "美浦W", "栗ＣＷ": "栗東CW", "栗CW": "栗東CW", "ＣＷ": "栗東CW",
    "美ダ": "美浦ダート", "美南Ｄ": "美浦ダート", "南Ｄ": "美浦ダート",
    "南ダ": "美浦ダート", "北Ｃ": "美浦C",
    "栗Ｂ": "栗東B", "栗Ｐ": "栗東P", "美Ｐ": "美浦P",
    "栗Ｅ": "栗東E", "ＤＰ": "栗東DP",
    "札ダ": "札幌ダート", "札芝": "札幌芝", "函Ｗ": "函館W",
    "函ダ": "函館ダート", "小ダ": "小倉ダート", "小芝": "小倉芝",
}
INTENSITY_NORMALIZATION = {
    "馬也": "馬也", "馬なり": "馬也", "一杯": "一杯", "強め": "強め",
    "直強め": "強め", "末強め": "強め", "Ｇ前強め": "強め",
    "直一杯": "一杯", "末一杯": "一杯", "Ｇ前一杯": "一杯",
    "直線追う": "直線", "直線一杯": "一杯", "直線強め": "強め",
    "仕掛": "強め", "Ｇ強": "強め", "Ｇ一": "一杯",
    "直強": "強め", "直一": "一杯",
}
DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
RACE_ID_RE = re.compile(r"/race/(\d{12})(?:/|\?|$)")
HORSE_ID_RE = re.compile(r"/horse/([A-Za-z0-9]+)")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


SCHEMA = """
CREATE TABLE race_training_rows (
    race_id TEXT NOT NULL, horse_id TEXT NOT NULL, horse_name TEXT NOT NULL,
    row_index INTEGER NOT NULL, training_date TEXT, course_raw TEXT, course_norm TEXT,
    baba TEXT, rider TEXT, times_json TEXT NOT NULL, laps_json TEXT NOT NULL,
    lap_count INTEGER NOT NULL, position TEXT, intensity_raw TEXT, intensity_norm TEXT,
    evaluation TEXT, comment TEXT, partner_text TEXT, source_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    PRIMARY KEY (race_id, horse_id, row_index)
);
CREATE INDEX ix_t42b_race_horse ON race_training_rows(race_id, horse_name);
CREATE INDEX ix_t42b_race_date_course ON race_training_rows(training_date, course_norm);
CREATE TABLE horse_training_rows (
    horse_id TEXT NOT NULL, group_index INTEGER NOT NULL, row_index INTEGER NOT NULL,
    training_date TEXT, course_raw TEXT, course_norm TEXT, baba TEXT, rider TEXT,
    times_json TEXT NOT NULL, laps_json TEXT NOT NULL, lap_count INTEGER NOT NULL,
    position TEXT, intensity_raw TEXT, intensity_norm TEXT, evaluation TEXT,
    comment TEXT, source_sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
    PRIMARY KEY (horse_id, group_index, row_index)
);
CREATE TABLE race_id_map (
    race_id TEXT PRIMARY KEY, date8 TEXT NOT NULL, place TEXT NOT NULL, race_no INTEGER NOT NULL
);
CREATE TABLE parse_audit (
    source_path TEXT PRIMARY KEY, category TEXT NOT NULL, rows_parsed INTEGER NOT NULL,
    rows_skipped INTEGER NOT NULL, skip_reasons_json TEXT NOT NULL
);
CREATE TABLE vocab_audit (
    field TEXT NOT NULL, raw_value TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (field, raw_value)
);
"""


@dataclass(frozen=True)
class ManifestEntry:
    category: str
    key: str
    path: Path | None
    sha256: str | None
    error_code: str | None


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_date(text: str) -> str | None:
    match = DATE_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return datetime(*(int(value) for value in match.groups())).date().isoformat()
    except ValueError:
        return None


def normalize_course(raw: str) -> str:
    value = str(raw or "").strip()
    if value.endswith(" 一番時計"):
        value = value.removesuffix(" 一番時計").strip()
    return COURSE_NORMALIZATION.get(value, value)


def normalize_intensity(raw: str) -> str:
    value = str(raw or "").strip()
    return INTENSITY_NORMALIZATION.get(value, value)


def _direct_text(node) -> str:
    return " ".join(str(part).strip() for part in node.find_all(string=True, recursive=False)
                    if str(part).strip())


def parse_times(cell) -> tuple[list[float], list[float]]:
    times, laps = [], []
    if cell is None:
        return times, laps
    timing_list = cell.select_one(".TrainingTimeDataList") or cell
    items = timing_list.find_all("li", recursive=False)
    if not items:
        items = [timing_list]
    for item in items:
        main = _direct_text(item)
        match = NUMBER_RE.search(main)
        if match:
            times.append(float(match.group(0)))
        for lap in item.select(".RapTime"):
            lap_match = NUMBER_RE.search(lap.get_text(" ", strip=True))
            if lap_match:
                laps.append(float(lap_match.group(0)))
    return times, laps


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def parse_race_page(body: bytes, race_id: str, source_sha256: str):
    soup = BeautifulSoup(body, "html.parser", parse_only=SoupStrainer("table"))
    rows, skipped, reasons = [], 0, Counter()
    indexes = defaultdict(int)
    for table in soup.select("table"):
        current_id = current_name = None
        for tr in table.select("tr"):
            horse_cell = tr.select_one("td.Horse_Info")
            if horse_cell is not None:
                anchor = horse_cell.find("a", href=True)
                match = HORSE_ID_RE.search(str(anchor.get("href"))) if anchor else None
                current_id = match.group(1) if match else None
                current_name = _text(anchor) or _text(horse_cell).removesuffix("前走").strip()
            day = tr.select_one("td.Training_Day, .Training_Day")
            if day is None:
                continue
            if not current_id or not current_name:
                skipped += 1
                reasons["horse_identity_missing"] += 1
                continue
            training_date = normalize_date(_text(day))
            if training_date is None and _text(day):
                skipped += 1
                reasons["training_date_invalid"] += 1
                continue
            course = day.find_next_sibling("td")
            baba = course.find_next_sibling("td") if course else None
            rider = baba.find_next_sibling("td") if baba else None
            timing = tr.select_one("td.TrainingTimeData")
            position = timing.find_next_sibling("td") if timing else None
            intensity = tr.select_one("td.TrainingLoad")
            critic = tr.select_one("td.Training_Critic")
            evaluation = tr.select_one("td[class*='Rank_']")
            times, laps = parse_times(timing)
            course_raw, intensity_raw = _text(course), _text(intensity)
            indexes[current_id] += 1
            partner = timing.select_one(".Comment_Cell") if timing else None
            rows.append((
                race_id, current_id, current_name, indexes[current_id], training_date,
                course_raw, normalize_course(course_raw), _text(baba), _text(rider),
                stable_json(times), stable_json(laps), len(times), _text(position),
                intensity_raw, normalize_intensity(intensity_raw), _text(evaluation),
                _text(critic), _text(partner), source_sha256, PARSER_VERSION,
            ))
    return rows, skipped, reasons


def parse_horse_page(body: bytes, horse_id: str, source_sha256: str):
    soup = BeautifulSoup(body, "html.parser", parse_only=SoupStrainer("table"))
    rows, skipped, reasons = [], 0, Counter()
    for group_index, table in enumerate(soup.select("table.race_table_01"), 1):
        row_index = 0
        for tr in table.select("tr"):
            cells = tr.find_all("td", recursive=False)
            if not cells or "日付" in _text(cells[0]):
                continue
            if len(cells) < 8:
                skipped += 1
                reasons["columns_missing"] += 1
                continue
            training_date = normalize_date(_text(cells[0]))
            if training_date is None:
                skipped += 1
                reasons["training_date_invalid"] += 1
                continue
            row_index += 1
            timing = cells[4]
            times, laps = parse_times(timing)
            course_raw, intensity_raw = _text(cells[1]), _text(cells[6])
            rows.append((
                horse_id, group_index, row_index, training_date, course_raw,
                normalize_course(course_raw), _text(cells[2]), _text(cells[3]),
                stable_json(times), stable_json(laps), len(times), _text(cells[5]),
                intensity_raw, normalize_intensity(intensity_raw),
                _text(cells[8]) if len(cells) > 8 else "", _text(cells[7]),
                source_sha256, PARSER_VERSION,
            ))
    return rows, skipped, reasons


def parse_race_index(body: bytes, date8: str):
    soup = BeautifulSoup(body, "html.parser", parse_only=SoupStrainer("a"))
    values = {}
    for anchor in soup.find_all("a", href=True):
        match = RACE_ID_RE.search(str(anchor.get("href")))
        if not match:
            continue
        race_id = match.group(1)
        place = PLACE_BY_CODE.get(race_id[4:6], race_id[4:6])
        values[race_id] = (race_id, date8, place, int(race_id[-2:]))
    return list(values.values())


def load_manifest(path: Path) -> list[ManifestEntry]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        raw = connection.execute("""SELECT category,target_key,cache_path,sha256,error_code
            FROM fetch_manifest ORDER BY category,target_key""").fetchall()
    finally:
        connection.close()
    entries = []
    for category, key, cache_path, digest, error in raw:
        resolved = None
        if cache_path:
            candidate = Path(cache_path)
            resolved = candidate if candidate.is_absolute() else ROOT / candidate
        entries.append(ManifestEntry(category, str(key), resolved, digest, error))
    return entries


def _read_verified(entry: ManifestEntry) -> bytes:
    if entry.path is None or entry.sha256 is None:
        raise FileNotFoundError(entry.error_code or "manifest_has_no_cache")
    body = entry.path.read_bytes()
    if hashlib.sha256(body).hexdigest() != entry.sha256:
        raise ValueError("source_sha256_mismatch")
    return body


def _parse_manifest_entry(entry: ManifestEntry):
    """Worker-safe parse with structured failures and no writes."""
    try:
        body = _read_verified(entry)
        if entry.category == "race_index":
            return entry, parse_race_index(body, entry.key), 0, {}
        if entry.category == "race":
            values, skipped, reasons = parse_race_page(body, entry.key, entry.sha256)
            return entry, values, skipped, dict(reasons)
        if entry.category == "horse":
            values, skipped, reasons = parse_horse_page(body, entry.key, entry.sha256)
            return entry, values, skipped, dict(reasons)
        return entry, [], 1, {"unknown_category": 1}
    except (FileNotFoundError, ValueError) as exc:
        return entry, [], 1, {str(exc): 1}


def rebuild_store(manifest_path=DEFAULT_MANIFEST, output_path=DEFAULT_OUTPUT,
                  ability_path=DEFAULT_ABILITY, progress_every=500, workers=1,
                  resume=False) -> dict:
    entries = load_manifest(Path(manifest_path))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not resume:
        output_path.unlink()
    connection = sqlite3.connect(output_path, uri=True)
    if not resume:
        connection.executescript(SCHEMA)
    elif not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parse_audit'").fetchone():
        connection.close()
        raise ValueError("--resume requires an existing T42b Stage A database")
    audit_rows, vocab = [], Counter()
    totals, race_ids_in_map = Counter(), set()
    successful_race_ids = {entry.key for entry in entries
                           if entry.category == "race" and entry.sha256}
    try:
        ordered = sorted(entries, key=lambda entry: (
            {"race_index": 0, "race": 1, "horse": 2}.get(entry.category, 9), entry.key))
        if resume:
            processed = {row[0] for row in connection.execute("SELECT source_path FROM parse_audit")}
            ordered = [entry for entry in ordered if str(entry.path or entry.key) not in processed]
            for category, pages, parsed, skipped in connection.execute("""SELECT category,COUNT(*),
                    SUM(rows_parsed),SUM(rows_skipped) FROM parse_audit GROUP BY category"""):
                totals[f"{category}_pages"] = pages
                totals[f"{category}_rows"] = parsed
                totals[f"{category}_skipped"] = skipped
            race_ids_in_map.update(row[0] for row in connection.execute("SELECT race_id FROM race_id_map"))
            for field, table, column in (("course", "race_training_rows", "course_raw"),
                                         ("intensity", "race_training_rows", "intensity_raw"),
                                         ("course", "horse_training_rows", "course_raw"),
                                         ("intensity", "horse_training_rows", "intensity_raw")):
                for raw, count in connection.execute(
                        f"SELECT {column},COUNT(*) FROM {table} GROUP BY {column}"):
                    vocab[(field, raw)] += count
            print(f"resuming with {len(processed)} completed pages; {len(ordered)} remaining",
                  flush=True)
        executor = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        parsed_entries = (executor.map(_parse_manifest_entry, ordered, chunksize=8)
                          if executor else map(_parse_manifest_entry, ordered))
        for page_number, (entry, values, skipped, reasons) in enumerate(parsed_entries, 1):
            if entry.category == "race_index":
                connection.executemany(
                    "INSERT OR REPLACE INTO race_id_map(race_id,date8,place,race_no) VALUES(?,?,?,?)",
                    values)
                race_ids_in_map.update(value[0] for value in values)
            elif entry.category == "race":
                connection.executemany("""INSERT INTO race_training_rows
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                for row in values:
                    vocab[("course", row[5])] += 1
                    vocab[("intensity", row[13])] += 1
            elif entry.category == "horse":
                connection.executemany("""INSERT INTO horse_training_rows
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                for row in values:
                    vocab[("course", row[4])] += 1
                    vocab[("intensity", row[12])] += 1
            parsed = len(values)
            totals[f"{entry.category}_pages"] += 1
            totals[f"{entry.category}_rows"] += parsed
            totals[f"{entry.category}_skipped"] += skipped
            audit_rows.append((str(entry.path or entry.key), entry.category, parsed,
                               skipped, stable_json(dict(reasons))))
            if page_number % progress_every == 0:
                connection.executemany(
                    "INSERT INTO parse_audit VALUES(?,?,?,?,?)", audit_rows)
                audit_rows.clear()
                connection.commit()
                print(f"parsed {page_number}/{len(ordered)} remaining pages", flush=True)
        if executor:
            executor.shutdown()
        if audit_rows:
            connection.executemany("INSERT INTO parse_audit VALUES(?,?,?,?,?)", audit_rows)
        connection.execute("DELETE FROM vocab_audit")
        connection.executemany("INSERT INTO vocab_audit VALUES(?,?,?)",
                               [(field, raw, count) for (field, raw), count in sorted(vocab.items())])
        connection.commit()
        summary = build_quality_summary(
            connection, Path(ability_path), totals,
            successful_race_ids - race_ids_in_map, vocab)
    finally:
        connection.close()
    return summary


def refine_existing_store(manifest_path=DEFAULT_MANIFEST, output_path=DEFAULT_OUTPUT,
                          ability_path=DEFAULT_ABILITY) -> dict:
    """Apply Stage A parser refinements without rereading unaffected source pages."""
    entries = load_manifest(Path(manifest_path))
    by_source = {str(entry.path or entry.key): entry for entry in entries}
    connection = sqlite3.connect(Path(output_path), uri=True)
    try:
        affected = [row[0] for row in connection.execute(
            "SELECT source_path FROM parse_audit WHERE category='race' "
            "AND skip_reasons_json LIKE '%training_date_invalid%'")]
        for index, source in enumerate(affected, 1):
            entry = by_source[source]
            _entry, values, skipped, reasons = _parse_manifest_entry(entry)
            connection.execute("DELETE FROM race_training_rows WHERE race_id=?", (entry.key,))
            connection.executemany("INSERT INTO race_training_rows VALUES(" +
                                   ",".join("?" * 20) + ")", values)
            connection.execute("""UPDATE parse_audit SET rows_parsed=?,rows_skipped=?,
                skip_reasons_json=? WHERE source_path=?""",
                (len(values), skipped, stable_json(reasons), source))
            if index % 100 == 0:
                connection.commit()
                print(f"refined {index}/{len(affected)} pages", flush=True)
        for table in ("race_training_rows", "horse_training_rows"):
            courses = [row[0] for row in connection.execute(
                f"SELECT DISTINCT course_raw FROM {table}")]
            intensities = [row[0] for row in connection.execute(
                f"SELECT DISTINCT intensity_raw FROM {table}")]
            connection.executemany(
                f"UPDATE {table} SET course_norm=? WHERE course_raw=?",
                [(normalize_course(raw), raw) for raw in courses])
            connection.executemany(
                f"UPDATE {table} SET intensity_norm=? WHERE intensity_raw=?",
                [(normalize_intensity(raw), raw) for raw in intensities])
            connection.execute(f"UPDATE {table} SET parser_version=?", (PARSER_VERSION,))
        connection.execute("DELETE FROM vocab_audit")
        vocab = Counter()
        for field, table, column in (("course", "race_training_rows", "course_raw"),
                                     ("intensity", "race_training_rows", "intensity_raw"),
                                     ("course", "horse_training_rows", "course_raw"),
                                     ("intensity", "horse_training_rows", "intensity_raw")):
            for raw, count in connection.execute(
                    f"SELECT {column},COUNT(*) FROM {table} GROUP BY {column}"):
                vocab[(field, raw)] += count
        connection.executemany("INSERT INTO vocab_audit VALUES(?,?,?)",
                               [(field, raw, count) for (field, raw), count in sorted(vocab.items())])
        connection.commit()
        totals = Counter()
        for category, pages, parsed, skipped in connection.execute("""SELECT category,COUNT(*),
                SUM(rows_parsed),SUM(rows_skipped) FROM parse_audit GROUP BY category"""):
            totals[f"{category}_pages"] = pages
            totals[f"{category}_rows"] = parsed
            totals[f"{category}_skipped"] = skipped
        mapped = {row[0] for row in connection.execute("SELECT race_id FROM race_id_map")}
        successful = {entry.key for entry in entries if entry.category == "race" and entry.sha256}
        return build_quality_summary(connection, Path(ability_path), totals,
                                     successful - mapped, vocab)
    finally:
        connection.close()


def _percentile(values, probability):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[low]
    fraction = position - low
    return values[low] * (1 - fraction) + values[high] * fraction


def count_future_training_rows(connection) -> int:
    return int(connection.execute("""SELECT COUNT(*) FROM race_training_rows t
        JOIN race_id_map m ON m.race_id=t.race_id
        WHERE replace(t.training_date,'-','') > m.date8""").fetchone()[0])


def build_quality_summary(connection, ability_path: Path, totals: Counter,
                          missing_map: set[str], vocab: Counter):
    future_rows = count_future_training_rows(connection)
    timing = defaultdict(list)
    for course, payload in connection.execute(
            "SELECT course_norm,times_json FROM race_training_rows WHERE lap_count>0"):
        values = json.loads(payload)
        if values:
            timing[course].append(float(values[0]))
    timing_summary = {course: {"count": len(values), "p1": _percentile(values, .01),
                               "p99": _percentile(values, .99)}
                      for course, values in sorted(timing.items())}
    connection.execute("ATTACH DATABASE ? AS ability",
                       (ability_path.resolve().as_uri() + "?mode=ro",))
    coverage_rows = connection.execute("""WITH eligible AS (
          SELECT DISTINCT date,place,r,horse FROM ability.runs
          WHERE date BETWEEN '20210101' AND '20260630'
        ), trained AS (
          SELECT DISTINCT race_id,horse_name FROM race_training_rows
        )
        SELECT substr(e.date,1,4),COUNT(*),SUM(CASE WHEN t.horse_name IS NOT NULL THEN 1 ELSE 0 END)
        FROM eligible e LEFT JOIN race_id_map m
          ON m.date8=e.date AND m.place=e.place AND m.race_no=e.r
        LEFT JOIN trained t ON t.race_id=m.race_id AND t.horse_name=e.horse
        GROUP BY substr(e.date,1,4) ORDER BY 1""").fetchall()
    connection.execute("DETACH DATABASE ability")
    coverage = {year: {"runners": total, "covered": covered,
                       "rate": covered / total if total else None}
                for year, total, covered in coverage_rows}
    known_course = set(COURSE_NORMALIZATION)
    known_intensity = set(INTENSITY_NORMALIZATION)
    vocab_summary = {}
    for field, known in (("course", known_course), ("intensity", known_intensity)):
        rows = [(raw, count) for (name, raw), count in vocab.items() if name == field]
        rows.sort(key=lambda item: (-item[1], item[0]))
        def is_known(raw):
            if not raw:
                return True  # recorded as missing, not an unknown token
            if raw in known:
                return True
            return (field == "course" and raw.endswith(" 一番時計")
                    and raw.removesuffix(" 一番時計").strip() in known)
        vocab_summary[field] = {
            "top": rows[:30], "unknown": [(raw, count) for raw, count in rows if not is_known(raw)],
            "missing_count": sum(count for raw, count in rows if not raw),
        }
    return {
        "parser_version": PARSER_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": dict(sorted(totals.items())),
        "race_id_map_missing_count": len(missing_map),
        "race_id_map_missing": sorted(missing_map),
        "future_training_rows": int(future_rows),
        "coverage": coverage,
        "timing_by_course": timing_summary,
        "vocab": vocab_summary,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ability", type=Path, default=DEFAULT_ABILITY)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refine-existing", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    if args.resume and args.refine_existing:
        parser.error("--resume and --refine-existing are mutually exclusive")
    if args.refine_existing:
        summary = refine_existing_store(args.manifest, args.output, args.ability)
    else:
        summary = rebuild_store(args.manifest, args.output, args.ability, workers=args.workers,
                                resume=args.resume)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(stable_json({"output": str(args.output), "summary": str(args.summary),
                       "future_training_rows": summary["future_training_rows"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
