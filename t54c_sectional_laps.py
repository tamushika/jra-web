"""T54c: cache, parse, and store official JRA sectional-lap result data.

This module deliberately stops at collection infrastructure.  It does not
construct model features or connect the new tables to live prediction code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.jra.go.jp/JRADB/accessS.html"
INDEX_CNAME = "pw01skl00999999/B3"
PARSER_VERSION = "t54c-v1"
DEFAULT_CACHE_ROOT = Path("data/t54c/raw")
DEFAULT_MANIFEST_PATH = Path("data/t54c/manifest.sqlite")
DEFAULT_LOCK_PATH = Path("data/t54c/scrape.lock")
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36 "
        "(T54c read-only archive)"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.jra.go.jp/",
}
VENUES = ("札幌", "函館", "福島", "新潟", "東京",
          "中山", "中京", "京都", "阪神", "小倉")
JUMP_NAME_RE = re.compile(r"障害|ジャンプ|J[・･\s]?G(?:I{1,3}|[123])", re.I)
NON_RUNNER_STATUSES = ("取消", "除外", "中止", "失格")


class T54cError(RuntimeError):
    """Base error for the T54c collection pipeline."""


class ParseError(T54cError):
    """The official page is present but violates the expected result contract."""


class ConsecutiveFetchError(T54cError):
    """Fetching stopped after the configured number of consecutive failures."""


class ScrapeLockError(T54cError):
    """Another JRA scraper holds the shared lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_cname_url(cname: str) -> str:
    return f"{BASE_URL}?CNAME={quote(cname, safe='')}"


def cname_from_url(url: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get("CNAME")
    return unquote(values[0]) if values else None


def race_date_from_url(url: str) -> str | None:
    cname = cname_from_url(url) or ""
    match = re.search(r"(20\d{6})/[0-9A-Fa-f]{2}$", cname)
    return match.group(1) if match else None


def _safe_cache_name(cname: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", cname).strip("._")
    digest = hashlib.sha256(cname.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:120]}-{digest}.html"


def cache_path_for(url: str, cache_root: Path) -> Path:
    cname = cname_from_url(url)
    if not cname:
        raise ValueError(f"CNAMEのないURLはキャッシュできません: {url}")
    date = race_date_from_url(url)
    if date:
        year = date[:4]
    else:
        year_match = re.search(r"20\d{2}", cname)
        year = year_match.group() if year_match else "index"
    return cache_root / year / _safe_cache_name(cname)


def init_manifest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_manifest (
                url TEXT PRIMARY KEY,
                cache_path TEXT,
                fetched_at TEXT NOT NULL,
                http_status INTEGER,
                sha256 TEXT,
                parser_version TEXT NOT NULL,
                error TEXT
            )
            """
        )


def _record_manifest(
    path: Path,
    *,
    url: str,
    cache_path: Path | None,
    fetched_at: str,
    status: int | None,
    sha256: str | None,
    error: str | None,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO fetch_manifest
                (url, cache_path, fetched_at, http_status, sha256,
                 parser_version, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                cache_path=excluded.cache_path,
                fetched_at=excluded.fetched_at,
                http_status=excluded.http_status,
                sha256=excluded.sha256,
                parser_version=excluded.parser_version,
                error=excluded.error
            """,
            (
                url,
                str(cache_path) if cache_path else None,
                fetched_at,
                status,
                sha256,
                PARSER_VERSION,
                error,
            ),
        )


@dataclass(frozen=True)
class CachedHtml:
    body: bytes
    source_url: str
    fetched_at: str
    sha256: str
    cache_path: Path
    cache_hit: bool
    http_status: int


class RateLimitedFetcher:
    """Fetch CNAME pages sequentially with an immutable on-disk cache."""

    def __init__(
        self,
        *,
        cache_root: Path = DEFAULT_CACHE_ROOT,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        interval: float = 1.05,
        max_retries: int = 3,
        failure_threshold: int = 5,
        session: requests.Session | None = None,
    ) -> None:
        if interval < 1.0:
            raise ValueError("JRA取得間隔は1秒以上でなければなりません")
        self.cache_root = Path(cache_root)
        self.manifest_path = Path(manifest_path)
        self.interval = interval
        self.max_retries = max_retries
        self.failure_threshold = failure_threshold
        self.session = session or requests.Session()
        self.session.headers.update(FETCH_HEADERS)
        self._last_request_started: float | None = None
        self._consecutive_failures = 0
        self.network_requests = 0
        self.cache_hits = 0
        init_manifest(self.manifest_path)

    def _cached(self, url: str, target: Path) -> CachedHtml | None:
        if not target.exists():
            return None
        with sqlite3.connect(self.manifest_path) as connection:
            row = connection.execute(
                """
                SELECT fetched_at, http_status, sha256, error
                FROM fetch_manifest WHERE url=?
                """,
                (url,),
            ).fetchone()
            if row is None:
                legacy = connection.execute(
                    """
                    SELECT url, fetched_at, http_status, sha256, error
                    FROM fetch_manifest WHERE cache_path=?
                    """,
                    (str(target),),
                ).fetchone()
                if legacy is not None:
                    # v1の初回バッチはJRA hrefの生"/"をmanifestキーにした。
                    # 同じcache_path/SHAを確認できる行だけCNAME正規形へ移行する。
                    connection.execute(
                        "UPDATE fetch_manifest SET url=? WHERE url=?",
                        (url, legacy[0]),
                    )
                    row = legacy[1:]
        if not row or row[3] or row[1] != 200 or not row[2]:
            raise T54cError(f"manifestと対応しない孤立キャッシュです: {target}")
        body = target.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual != row[2]:
            raise T54cError(f"immutableキャッシュのSHA-256不一致です: {target}")
        self.cache_hits += 1
        return CachedHtml(body, url, row[0], actual, target, True, row[1])

    def _wait(self) -> None:
        if self._last_request_started is None:
            return
        remaining = self.interval - (time.monotonic() - self._last_request_started)
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, url: str) -> requests.Response:
        cname = cname_from_url(url)
        if not cname:
            raise ValueError(f"CNAMEを特定できません: {url}")
        self._wait()
        self._last_request_started = time.monotonic()
        self.network_requests += 1
        if "pw01sde" in cname:
            return self.session.get(url, timeout=30)
        return self.session.post(BASE_URL, data={"cname": cname}, timeout=30)

    def fetch(self, url: str) -> CachedHtml:
        cname = cname_from_url(url)
        if not cname:
            raise ValueError(f"CNAMEを特定できません: {url}")
        # JRAのhrefはスラッシュを生で返す場合と%2Fで返す場合がある。
        # 同一CNAMEを必ず1つのmanifestキーへ寄せ、二重取得や孤立判定を防ぐ。
        manifest_url = canonical_cname_url(cname)
        target = cache_path_for(url, self.cache_root)
        cached = self._cached(manifest_url, target)
        if cached is not None:
            self._consecutive_failures = 0
            return CachedHtml(
                cached.body,
                url,
                cached.fetched_at,
                cached.sha256,
                cached.cache_path,
                True,
                cached.http_status,
            )

        last_error: Exception | None = None
        last_status: int | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._request(url)
                last_status = response.status_code
                response.raise_for_status()
                body = response.content
                digest = hashlib.sha256(body).hexdigest()
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise T54cError(f"immutableキャッシュへの上書きを拒否しました: {target}")
                temporary = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(body)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    temporary.unlink()
                    raise T54cError("キャッシュ書き込み直後のSHA-256検証に失敗しました")
                os.replace(temporary, target)
                fetched_at = utc_now()
                _record_manifest(
                    self.manifest_path,
                    url=manifest_url,
                    cache_path=target,
                    fetched_at=fetched_at,
                    status=response.status_code,
                    sha256=digest,
                    error=None,
                )
                self._consecutive_failures = 0
                return CachedHtml(
                    body, url, fetched_at, digest, target, False,
                    response.status_code,
                )
            except Exception as exc:  # requests and cache integrity failures
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)

        _record_manifest(
            self.manifest_path,
            url=manifest_url,
            cache_path=None,
            fetched_at=utc_now(),
            status=last_status,
            sha256=None,
            error=str(last_error),
        )
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            raise ConsecutiveFetchError(
                f"{self._consecutive_failures}件連続で取得に失敗したため停止しました"
            ) from last_error
        raise T54cError(f"取得に失敗しました: {url}: {last_error}") from last_error


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _race_heading(soup: BeautifulSoup) -> str:
    for heading in soup.find_all("h1"):
        text = heading.get_text(" ", strip=True)
        if re.search(r"20\d{2}年\d{1,2}月\d{1,2}日", text):
            return text
    opt = soup.select_one("span.opt")
    return opt.get_text(" ", strip=True) if opt else ""


def _race_name(soup: BeautifulSoup) -> str:
    ignored = {"検索ウィンドウ", "緊急情報", "払戻金", "JRAからのお知らせ"}
    for heading in soup.find_all("h2"):
        text = heading.get_text(" ", strip=True)
        if text and text not in ignored:
            return text
    return ""


def _result_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        labels = {_compact(th.get_text(" ", strip=True)) for th in table.find_all("th")}
        if "馬番" in labels and "コーナー通過順位" in labels:
            return table
    return None


def _table_rows(table) -> list[dict[str, Any]]:
    header = table.find("tr")
    if header is None:
        raise ParseError("結果テーブルに見出し行がありません")
    headers = [_compact(th.get_text(" ", strip=True))
               for th in header.find_all("th", recursive=False)]
    try:
        number_index = headers.index("馬番")
        corner_index = headers.index("コーナー通過順位")
        status_index = headers.index("着順")
    except ValueError as exc:
        raise ParseError(f"結果テーブルの必須見出しがありません: {headers}") from exc

    parsed = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td", recursive=False)
        if not cells or len(cells) != len(headers):
            continue
        number_match = re.search(r"\d+", cells[number_index].get_text(" ", strip=True))
        if not number_match:
            continue
        corner_cell = cells[corner_index]
        corner_items = corner_cell.find_all(
            "li", title=re.compile(r"[1-4]コーナー通過順位")
        )
        positions = []
        corner_numbers = []
        for item in corner_items:
            value_match = re.search(r"\d+", item.get_text(" ", strip=True))
            title_match = re.search(r"([1-4])コーナー", item.get("title", ""))
            if value_match:
                positions.append(int(value_match.group()))
                corner_numbers.append(int(title_match.group(1)) if title_match else None)
        if not corner_items:
            positions = [
                int(value) for value in re.findall(
                    r"\d+", corner_cell.get_text(" ", strip=True)
                )
            ]
            corner_numbers = (
                list(range(5 - len(positions), 5)) if positions else []
            )
        parsed.append({
            "umaban": int(number_match.group()),
            "status": cells[status_index].get_text(" ", strip=True),
            "corner_numbers": corner_numbers,
            "corner_positions": positions,
            "c4": positions[-1] if positions else None,
        })
    if not parsed:
        raise ParseError("結果テーブルから馬番を持つ行を取得できません")
    return parsed


def parse_sectional_html(
    body: bytes | str,
    *,
    source_url: str,
    fetched_at: str,
    sha256: str | None = None,
) -> dict[str, Any]:
    """Parse one official result page without any position-based lap selector."""
    if isinstance(body, bytes):
        raw = body
        html = body.decode("cp932", errors="strict")
    else:
        html = body
        raw = body.encode("cp932", errors="replace")
    digest = sha256 or hashlib.sha256(raw).hexdigest()
    soup = BeautifulSoup(html, "html.parser")
    heading = _race_heading(soup)
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", heading)
    race_match = re.search(r"(\d{1,2})レース", heading)
    place = next((venue for venue in VENUES if venue in heading), None)
    if not (date_match and race_match and place):
        raise ParseError(f"レース識別子を本文から特定できません: {heading[:120]}")
    date = (
        f"{date_match.group(1)}{int(date_match.group(2)):02d}"
        f"{int(date_match.group(3)):02d}"
    )
    race_no = int(race_match.group(1))

    page_text = soup.get_text(" ", strip=True)
    course_match = re.search(
        r"コース\s*：?\s*(\d[\d,]*)\s*メートル\s*([^本]{0,80})",
        page_text,
    )
    course_text = course_match.group(0) if course_match else ""
    distance_match = re.search(r"(\d[\d,]*)\s*メートル", course_text)
    if not distance_match:
        raise ParseError("コース距離を特定できません")
    distance = int(distance_match.group(1).replace(",", ""))
    race_name = _race_name(soup)
    is_obstacle = bool(
        "障害" in course_text
        or JUMP_NAME_RE.search(race_name)
        or JUMP_NAME_RE.search(heading)
    )
    is_straight = "直" in course_text

    table = _result_table(soup)
    if table is None:
        raise ParseError("公式結果テーブルがありません")
    runners = _table_rows(table)

    # Exact header equality is intentional.  Never infer this row by position:
    # the adjacent "上り" row is a different aggregate.
    lap_rows = []
    for th in soup.find_all("th"):
        if _compact(th.get_text(" ", strip=True)) == "ハロンタイム":
            td = th.find_next_sibling("td")
            if td is not None:
                lap_rows.append(td.get_text(" ", strip=True))
    if len(lap_rows) > 1:
        raise ParseError("ハロンタイム行が複数あり一意に決定できません")
    laps = [
        float(value) for value in re.findall(r"\d+(?:\.\d+)?", lap_rows[0])
    ] if lap_rows else []

    warnings = []
    if is_obstacle:
        excluded_reason = "obstacle"
    else:
        excluded_reason = None
        if not laps:
            raise ParseError("平地レースにハロンタイムがありません")
        if abs(len(laps) * 200 - distance) > 100:
            raise ParseError(
                f"距離とハロン数が不整合です: distance={distance}, laps={len(laps)}"
            )

    for runner in runners:
        if runner["corner_positions"]:
            continue
        if is_straight:
            continue
        if not any(status in runner["status"] for status in NON_RUNNER_STATUSES):
            warnings.append(
                f"馬番{runner['umaban']}は通常行ですが通過順が空です"
            )

    return {
        "date": date,
        "place": place,
        "r": race_no,
        "distance": distance,
        "race_name": race_name,
        "lap_sequence": laps,
        "runners": runners,
        "excluded_reason": excluded_reason,
        "is_straight": is_straight,
        "warnings": warnings,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "sha256": digest,
        "parser_version": PARSER_VERSION,
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runner_corners (
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    umaban INTEGER NOT NULL,
    corner_positions_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (date, place, r, umaban)
);
CREATE TABLE IF NOT EXISTS race_laps (
    date TEXT NOT NULL,
    place TEXT NOT NULL,
    r INTEGER NOT NULL,
    lap_sequence_json TEXT NOT NULL,
    distance INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (date, place, r)
);
"""


def init_sectional_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


def c4_compatibility(
    connection: sqlite3.Connection, parsed: dict[str, Any]
) -> dict[str, int]:
    checked = 0
    missing = 0
    mismatches = 0
    legacy_sequence_encoded = 0
    for runner in parsed["runners"]:
        row = connection.execute(
            """
            SELECT c4 FROM runs
            WHERE date=? AND place=? AND r=? AND umaban=?
            """,
            (parsed["date"], parsed["place"], parsed["r"], runner["umaban"]),
        ).fetchone()
        if row is None:
            missing += 1
            continue
        if row[0] is None or runner["c4"] is None:
            continue
        checked += 1
        existing = int(row[0])
        if existing == runner["c4"]:
            continue
        concatenated = int("".join(
            str(value) for value in runner["corner_positions"]
        ))
        if existing == concatenated:
            # Some 2026 ability rows encoded the full sequence into the c4
            # INTEGER (e.g. [13,12,8,6] -> 131286).  It is not a valid final
            # corner value, but it independently confirms every parsed item.
            # Keep runs.c4 immutable and report this legacy defect separately.
            legacy_sequence_encoded += 1
        else:
            mismatches += 1
    return {
        "checked": checked,
        "missing": missing,
        "mismatches": mismatches,
        "legacy_sequence_encoded": legacy_sequence_encoded,
    }


def save_sectional_race(
    connection: sqlite3.Connection, parsed: dict[str, Any]
) -> dict[str, int]:
    """Save with explicit columns, leaving the existing runs table untouched."""
    if parsed["excluded_reason"]:
        return {"runner_rows": 0, "lap_rows": 0}
    compatibility = c4_compatibility(connection, parsed)
    if compatibility["mismatches"]:
        raise ParseError(
            f"既存runs.c4との不一致が{compatibility['mismatches']}行あります"
        )
    runner_values = [
        (
            parsed["date"],
            parsed["place"],
            parsed["r"],
            runner["umaban"],
            json.dumps(runner["corner_positions"], ensure_ascii=False),
            parsed["source_url"],
            parsed["fetched_at"],
            parsed["sha256"],
        )
        for runner in parsed["runners"]
    ]
    with connection:
        connection.executemany(
            """
            INSERT INTO runner_corners
                (date, place, r, umaban, corner_positions_json,
                 source_url, fetched_at, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, place, r, umaban) DO UPDATE SET
                corner_positions_json=excluded.corner_positions_json,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at,
                sha256=excluded.sha256
            """,
            runner_values,
        )
        connection.execute(
            """
            INSERT INTO race_laps
                (date, place, r, lap_sequence_json, distance,
                 source_url, fetched_at, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, place, r) DO UPDATE SET
                lap_sequence_json=excluded.lap_sequence_json,
                distance=excluded.distance,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at,
                sha256=excluded.sha256
            """,
            (
                parsed["date"],
                parsed["place"],
                parsed["r"],
                json.dumps(parsed["lap_sequence"], ensure_ascii=False),
                parsed["distance"],
                parsed["source_url"],
                parsed["fetched_at"],
                parsed["sha256"],
            ),
        )
    return {"runner_rows": len(runner_values), "lap_rows": 1, **compatibility}


def _onclick_cnames(html: str, marker: str) -> list[str]:
    values = re.findall(
        r"doAction\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]",
        html,
    )
    return [value for value in values if marker in value]


def month_checksums(fetcher: RateLimitedFetcher) -> dict[str, str]:
    page = fetcher.fetch(canonical_cname_url(INDEX_CNAME))
    html = page.body.decode("cp932", errors="strict")
    pairs = re.findall(
        r'objParam\["(\d{4})"\]\s*=\s*"([0-9A-Fa-f]{2})"', html
    )
    if not pairs:
        raise ParseError("月別検索ページのチェック値一覧を取得できません")
    return dict(pairs)


def discover_result_urls(
    fetcher: RateLimitedFetcher,
    *,
    years: Iterable[int],
    end_date: str | None = None,
) -> list[str]:
    checksums = month_checksums(fetcher)
    venue_day_cnames: set[str] = set()
    for year in sorted(set(years)):
        last_month = 12
        if end_date and str(year) == end_date[:4]:
            last_month = int(end_date[4:6])
        for month in range(1, last_month + 1):
            yyyymm = f"{year}{month:02d}"
            key = yyyymm[2:]
            if key not in checksums:
                raise ParseError(f"{yyyymm}の月別検索チェック値がありません")
            cname = f"pw01skl10{yyyymm}/{checksums[key]}"
            page = fetcher.fetch(canonical_cname_url(cname))
            html = page.body.decode("cp932", errors="strict")
            venue_day_cnames.update(_onclick_cnames(html, "pw01srl"))

    urls: set[str] = set()
    for cname in sorted(venue_day_cnames):
        page = fetcher.fetch(canonical_cname_url(cname))
        soup = BeautifulSoup(page.body.decode("cp932", errors="strict"), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if "accessS.html" not in href or "CNAME=pw01sde" not in href:
                continue
            url = urljoin(BASE_URL, href)
            date = race_date_from_url(url)
            if not date or int(date[:4]) not in set(years):
                continue
            if end_date and date > end_date:
                continue
            urls.add(url)
    return sorted(urls, key=lambda value: (
        race_date_from_url(value) or "", cname_from_url(value) or ""
    ))


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def scrape_lock(path: Path = DEFAULT_LOCK_PATH):
    """Shared lock used by T54c and the existing weekly result updater."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
            break
        except FileExistsError:
            try:
                owner = path.read_text(encoding="utf-8").strip()
                owner_pid = int(owner.split(":", 1)[0])
            except (OSError, ValueError):
                raise ScrapeLockError(f"判定不能なスクレイプロックがあります: {path}")
            if _pid_is_alive(owner_pid):
                raise ScrapeLockError(
                    f"別のJRAスクレイパー(pid={owner_pid})が実行中です"
                )
            path.unlink()
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8").strip() == token:
                path.unlink()
        except FileNotFoundError:
            pass


def run_batch(
    *,
    db_path: Path,
    years: list[int],
    end_date: str | None,
    fetcher: RateLimitedFetcher,
    progress_every: int = 50,
) -> dict[str, Any]:
    started = time.monotonic()
    summary: dict[str, Any] = {
        "started_at": utc_now(),
        "years": years,
        "end_date": end_date,
        "discovered": 0,
        "parsed": 0,
        "saved_races": 0,
        "saved_runner_rows": 0,
        "excluded_obstacle": 0,
        "parse_failures": 0,
        "fetch_failures": 0,
        "warnings": 0,
        "c4_checked": 0,
        "c4_missing": 0,
        "c4_mismatches": 0,
        "c4_legacy_sequence_encoded": 0,
        "errors": [],
    }
    urls = discover_result_urls(fetcher, years=years, end_date=end_date)
    summary["discovered"] = len(urls)
    consecutive_pipeline_failures = 0
    with sqlite3.connect(db_path) as connection:
        init_sectional_schema(connection)
        for index, url in enumerate(urls, start=1):
            try:
                cached = fetcher.fetch(url)
                parsed = parse_sectional_html(
                    cached.body,
                    source_url=url,
                    fetched_at=cached.fetched_at,
                    sha256=cached.sha256,
                )
                summary["parsed"] += 1
                summary["warnings"] += len(parsed["warnings"])
                if parsed["excluded_reason"] == "obstacle":
                    summary["excluded_obstacle"] += 1
                else:
                    saved = save_sectional_race(connection, parsed)
                    summary["saved_races"] += saved["lap_rows"]
                    summary["saved_runner_rows"] += saved["runner_rows"]
                    summary["c4_checked"] += saved["checked"]
                    summary["c4_missing"] += saved["missing"]
                    summary["c4_mismatches"] += saved["mismatches"]
                    summary["c4_legacy_sequence_encoded"] += saved[
                        "legacy_sequence_encoded"
                    ]
                consecutive_pipeline_failures = 0
            except ConsecutiveFetchError:
                raise
            except T54cError as exc:
                is_fetch = "取得に失敗" in str(exc)
                summary["fetch_failures" if is_fetch else "parse_failures"] += 1
                if len(summary["errors"]) < 100:
                    summary["errors"].append({"url": url, "error": str(exc)})
                consecutive_pipeline_failures += 1
                if consecutive_pipeline_failures >= fetcher.failure_threshold:
                    raise ConsecutiveFetchError(
                        f"パースを含む{consecutive_pipeline_failures}件連続失敗で停止"
                    ) from exc
            if progress_every and index % progress_every == 0:
                print(
                    f"[T54c] {index}/{len(urls)} parsed={summary['parsed']} "
                    f"saved={summary['saved_races']} "
                    f"failed={summary['parse_failures'] + summary['fetch_failures']}",
                    flush=True,
                )
    summary["network_requests"] = fetcher.network_requests
    summary["cache_hits"] = fetcher.cache_hits
    total_cache_lookups = fetcher.network_requests + fetcher.cache_hits
    summary["cache_hit_rate"] = (
        fetcher.cache_hits / total_cache_lookups if total_cache_lookups else 0.0
    )
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    summary["finished_at"] = utc_now()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("ability.db"))
    parser.add_argument("--years", type=int, nargs="+", default=[2018, 2026])
    parser.add_argument("--end-date", default="20260630")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--interval", type=float, default=1.05)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--failure-threshold", type=int, default=5)
    parser.add_argument(
        "--summary", type=Path, default=Path("outputs/t54c_stage1.json")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fetcher = RateLimitedFetcher(
        cache_root=args.cache_root,
        manifest_path=args.manifest,
        interval=args.interval,
        max_retries=args.max_retries,
        failure_threshold=args.failure_threshold,
    )
    try:
        with scrape_lock(args.lock):
            summary = run_batch(
                db_path=args.db,
                years=args.years,
                end_date=args.end_date,
                fetcher=fetcher,
            )
    except ScrapeLockError as exc:
        print(f"[T54c] {exc}")
        return 2
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
