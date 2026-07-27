"""SPEC-T42a: authenticated immutable cache for netkeiba training pages.

The command is fail-closed.  Network access is disabled unless both --live and
--acknowledge-trial-active are supplied.  Cached HTML is the primary artifact;
parsing can always be repeated offline.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from t54c_sectional_laps import ScrapeLockError, scrape_lock


PARSER_VERSION = "t42a-v1"
DEFAULT_ROOT = Path("data/t42/raw")
DEFAULT_MANIFEST = Path("data/t42/manifest.sqlite")
DEFAULT_COOKIES = Path("data/t42/netkeiba_cookies.json")
DEFAULT_LOCK = Path("data/t54c/scrape.lock")
DEFAULT_DB = Path("ability.db")
INTERVAL = 1.05
DAILY_LIMIT = 8_000
PLACE_CODES = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
    "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36 (T42a archive)"
    ),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}
PAYWALL_SELECTORS = (
    ".Premium_Regist_Msg", ".Premium_Regist_Box", ".Premium_Registration",
)
# A trial/free tier may render the training-table skeleton but mask the actual
# lap values.  The immutable cache cannot be re-fetched after the trial ends, so
# the sentinel additionally requires at least one real numeric time (e.g. 54.4)
# inside a timing cell before an unattended bulk write is allowed.
TIMING_VALUE_RE = re.compile(r"\d+\.\d")


class T42Error(RuntimeError):
    pass


class CookieConfigError(T42Error):
    pass


class CacheIntegrityError(T42Error):
    pass


class MemberContentError(T42Error):
    pass


class PermanentPageGap(T42Error):
    """The remote site permanently rejects this target's URL (HTTP 400)."""


class StopAfterFailures(T42Error):
    pass


class DailyLimitReached(T42Error):
    pass


class LiveGateError(T42Error):
    pass


@dataclass(frozen=True)
class Target:
    category: str
    key: str
    url: str
    expected_horse_ids: tuple[str, ...] = ()
    expected_horse_count: int | None = None


@dataclass(frozen=True)
class CachedPage:
    body: bytes
    target: Target
    path: Path
    sha256: str
    fetched_at: str
    cache_hit: bool
    http_status: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jst_day() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()


def race_target(
    race_id: str, expected_horse_ids: Iterable[str] = (),
    expected_horse_count: int | None = None,
) -> Target:
    if not re.fullmatch(r"\d{12}", race_id):
        raise ValueError(f"invalid race_id: {race_id}")
    query = urlencode({"race_id": race_id, "type": "1"})
    return Target(
        "race", race_id,
        f"https://race.netkeiba.com/race/oikiri.html?{query}",
        tuple(sorted(set(expected_horse_ids))),
        expected_horse_count,
    )


def horse_target(horse_id: str) -> Target:
    if not re.fullmatch(r"[A-Za-z0-9]+", horse_id):
        raise ValueError(f"invalid horse_id: {horse_id}")
    query = urlencode({"pid": "horse_training", "id": horse_id})
    return Target(
        "horse", horse_id, f"https://db.netkeiba.com/?{query}", (horse_id,),
    )


def index_target(date8: str) -> Target:
    if not re.fullmatch(r"\d{8}", date8):
        raise ValueError(f"invalid date: {date8}")
    return Target(
        "race_index", date8, f"https://db.netkeiba.com/race/list/{date8}/",
    )


def cache_path_for(target: Target, root: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", target.key)
    return Path(root) / target.category / f"{safe}.html"


def init_manifest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS fetch_manifest (
                url TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                target_key TEXT NOT NULL,
                cache_path TEXT,
                fetched_at TEXT NOT NULL,
                http_status INTEGER,
                sha256 TEXT,
                parser_version TEXT NOT NULL,
                error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_t42_target
                ON fetch_manifest(category, target_key);
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requested_at TEXT NOT NULL,
                local_day TEXT NOT NULL,
                category TEXT NOT NULL,
                target_key TEXT NOT NULL,
                http_status INTEGER
            );
            """
        )


def _record_manifest(
    path: Path, target: Target, *, cache_path: Path | None,
    status: int | None, digest: str | None, error_code: str | None,
) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            """
            INSERT INTO fetch_manifest
              (url, category, target_key, cache_path, fetched_at, http_status,
               sha256, parser_version, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              category=excluded.category, target_key=excluded.target_key,
              cache_path=excluded.cache_path, fetched_at=excluded.fetched_at,
              http_status=excluded.http_status, sha256=excluded.sha256,
              parser_version=excluded.parser_version,
              error_code=excluded.error_code
            """,
            (
                target.url, target.category, target.key,
                str(cache_path) if cache_path else None, utc_now(), status,
                digest, PARSER_VERSION, error_code,
            ),
        )


def _load_cookies(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise CookieConfigError(
            f"cookie file is missing: {path} (contents were not read)"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CookieConfigError(
            f"cookie file is unreadable or invalid JSON: {path}"
        ) from exc
    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list) or not payload:
        raise CookieConfigError(f"cookie file has no cookie list: {path}")
    result = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("name") or "value" not in item:
            raise CookieConfigError(f"cookie file has an invalid entry: {path}")
        result.append(item)
    return result


def _soup(body: bytes) -> BeautifulSoup:
    return BeautifulSoup(body, "html.parser")


def detailed_horse_ids(soup: BeautifulSoup) -> set[str]:
    ids: set[str] = set()
    for block in soup.select("td.Horse_Info"):
        anchor = block.select_one(".Horse_Name a[href]") or block.find("a", href=True)
        if not anchor:
            continue
        match = re.search(r"/horse/([A-Za-z0-9]+)", str(anchor.get("href")))
        if match:
            ids.add(match.group(1))
    return ids


def validate_member_content(body: bytes, target: Target) -> dict[str, Any]:
    """Validate paid raw fields before the immutable write occurs."""
    soup = _soup(body)
    if any(soup.select_one(selector) for selector in PAYWALL_SELECTORS):
        raise MemberContentError("member_content_missing")
    days = soup.select("td.Training_Day, .Training_Day")
    timings = soup.select(".TrainingTimeDataList")
    if target.category == "horse":
        # db.netkeiba.com horse_training pages use a different DOM from the
        # race oikiri pages: plain race_table_01 tables with a 調教タイム
        # header column (confirmed live 2026-07-26, Stage 0).
        tables = [
            table
            for table in soup.select("table.race_table_01")
            if any("調教タイム" in th.get_text() for th in table.select("th"))
        ]
        if not tables:
            raise MemberContentError("training_sentinel_missing")
        if not any(
            TIMING_VALUE_RE.search(table.get_text(" ", strip=True))
            for table in tables
        ):
            raise MemberContentError("training_values_masked")
        if target.key not in str(soup):
            raise MemberContentError("horse_id_sentinel_missing")
        # Pagination summary such as 18件中1〜10件目 — recorded so the later
        # phase-2 budget decision can use real pages-per-horse numbers.
        pager = re.search(
            r"(\d+)件中(\d+)〜(\d+)件目", soup.get_text(" ", strip=True)
        )
        return {
            "training_tables": len(tables),
            "pager": pager.group(0) if pager else None,
        }
    if target.category == "race_index":
        race_ids = {
            match.group(1)
            for anchor in soup.find_all("a", href=True)
            if (match := re.search(r"/race/(\d{12})/?", str(anchor["href"])))
        }
        if not race_ids:
            raise MemberContentError("race_index_contract_missing")
        return {"race_ids": sorted(race_ids)}
    if not days or not timings:
        raise MemberContentError("training_sentinel_missing")
    if not any(TIMING_VALUE_RE.search(timing.get_text(" ", strip=True)) for timing in timings):
        # Skeleton present but every lap value is masked/blank -> not real
        # member data.  Refuse rather than cache a value-less page forever.
        raise MemberContentError("training_values_masked")
    horse_ids = detailed_horse_ids(soup)
    if target.category == "race" and not horse_ids:
        raise MemberContentError("race_horse_sentinel_missing")
    if target.expected_horse_ids:
        missing = set(target.expected_horse_ids) - horse_ids
        if missing:
            raise MemberContentError("expected_horse_coverage_missing")
    if (
        target.expected_horse_count is not None
        and len(horse_ids) < target.expected_horse_count
    ):
        raise MemberContentError("expected_horse_count_missing")
    return {
        "training_days": len(days),
        "timing_rows": len(timings),
        "horse_ids": sorted(horse_ids),
    }


class TrainingFetcher:
    def __init__(
        self, *, cache_root: Path = DEFAULT_ROOT,
        manifest_path: Path = DEFAULT_MANIFEST,
        cookies_path: Path = DEFAULT_COOKIES, interval: float = INTERVAL,
        daily_limit: int = DAILY_LIMIT, max_retries: int = 3,
        failure_threshold: int = 5, paywall_threshold: int = 3,
        live: bool = False, trial_acknowledged: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        if interval < 1.05:
            raise ValueError("netkeiba interval must be at least 1.05 seconds")
        if daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        self.cache_root = Path(cache_root)
        self.manifest_path = Path(manifest_path)
        self.cookies_path = Path(cookies_path)
        self.interval = interval
        self.daily_limit = daily_limit
        self.max_retries = max_retries
        self.failure_threshold = failure_threshold
        self.paywall_threshold = paywall_threshold
        self.live = live
        self.trial_acknowledged = trial_acknowledged
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self._cookies_loaded = False
        self._last_started: float | None = None
        self._failures = 0
        self._paywall_failures = 0
        self.network_requests = 0
        self.cache_hits = 0
        init_manifest(self.manifest_path)

    def _cached(self, target: Target, path: Path) -> CachedPage | None:
        if not path.exists():
            return None
        with sqlite3.connect(self.manifest_path) as db:
            row = db.execute(
                """
                SELECT fetched_at, http_status, sha256, error_code, cache_path
                FROM fetch_manifest WHERE url=?
                """,
                (target.url,),
            ).fetchone()
        if not row or row[1] != 200 or not row[2] or row[3] or row[4] != str(path):
            raise CacheIntegrityError(f"orphaned cache: {path}")
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != row[2]:
            raise CacheIntegrityError(f"immutable cache SHA-256 mismatch: {path}")
        validate_member_content(body, target)
        self.cache_hits += 1
        return CachedPage(body, target, path, digest, row[0], True, row[1])

    def _enable_network(self) -> None:
        if not self.live or not self.trial_acknowledged:
            raise LiveGateError(
                "network disabled; require --live and --acknowledge-trial-active"
            )
        if self._cookies_loaded:
            return
        for cookie in _load_cookies(self.cookies_path):
            kwargs = {}
            if cookie.get("domain"):
                kwargs["domain"] = cookie["domain"]
            if cookie.get("path"):
                kwargs["path"] = cookie["path"]
            self.session.cookies.set(cookie["name"], cookie["value"], **kwargs)
        self._cookies_loaded = True

    def _daily_count(self) -> int:
        with sqlite3.connect(self.manifest_path) as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM request_log WHERE local_day=?", (jst_day(),)
            ).fetchone()[0])

    def _request(self, target: Target) -> requests.Response:
        self._enable_network()
        if self._daily_count() >= self.daily_limit:
            raise DailyLimitReached(
                f"daily request limit reached ({self.daily_limit})"
            )
        if self._last_started is not None:
            wait = self.interval - (time.monotonic() - self._last_started)
            if wait > 0:
                time.sleep(wait)
        self._last_started = time.monotonic()
        response: requests.Response | None = None
        try:
            response = self.session.get(target.url, timeout=30)
            return response
        finally:
            self.network_requests += 1
            with sqlite3.connect(self.manifest_path) as db:
                db.execute(
                    """
                    INSERT INTO request_log
                      (requested_at, local_day, category, target_key, http_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(), jst_day(), target.category, target.key,
                        response.status_code if response is not None else None,
                    ),
                )

    def fetch(self, target: Target) -> CachedPage:
        path = cache_path_for(target, self.cache_root)
        cached = self._cached(target, path)
        if cached:
            self._failures = self._paywall_failures = 0
            return cached
        last_error: Exception | None = None
        status: int | None = None
        body: bytes | None = None
        permanent_client_error = False
        for attempt in range(self.max_retries):
            try:
                response = self._request(target)
                status = response.status_code
                if status == 400 and target.category == "race":
                    # race.netkeiba answers HTTP 400 for race_ids that exist
                    # on db.netkeiba but not there (2022 rescheduled meetings
                    # renumber kai/nichi; verified stable across runs).  This
                    # is permanent for the URL: do not retry, and do not let a
                    # block of such races trip the consecutive-failure stop
                    # (restart deadlock).  db.netkeiba (race_index) also
                    # returns transient 400s under load (observed 2026-07-27
                    # 02:00, 200 on retry), so other categories keep the
                    # normal retry + failure-count path.
                    permanent_client_error = True
                    last_error = requests.HTTPError(f"400 for {target.url}")
                    break
                response.raise_for_status()
                body = response.content
                validate_member_content(body, target)
                break
            except MemberContentError as exc:
                last_error = exc
                body = None
                break
            except (CookieConfigError, LiveGateError, DailyLimitReached):
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
        if body is None:
            if permanent_client_error:
                _record_manifest(
                    self.manifest_path, target, cache_path=None, status=status,
                    digest=None, error_code="http_400_id_not_on_race_site",
                )
                # Not a health signal: reset nothing, count nothing.
                raise PermanentPageGap(
                    f"race site rejects {target.category}/{target.key}"
                ) from last_error
            code = (
                str(last_error)
                if isinstance(last_error, MemberContentError)
                else "http_or_network_failure"
            )
            _record_manifest(
                self.manifest_path, target, cache_path=None, status=status,
                digest=None, error_code=code,
            )
            self._failures += 1
            if isinstance(last_error, MemberContentError):
                self._paywall_failures += 1
                if self._paywall_failures >= self.paywall_threshold:
                    raise StopAfterFailures(
                        f"{self._paywall_failures} consecutive member-content "
                        "failures; refresh the login session"
                    ) from last_error
            else:
                self._paywall_failures = 0
            if self._failures >= self.failure_threshold:
                raise StopAfterFailures(
                    f"{self._failures} consecutive fetch failures"
                ) from last_error
            raise T42Error(f"fetch rejected for {target.category}/{target.key}") from last_error
        digest = hashlib.sha256(body).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise CacheIntegrityError(f"immutable overwrite refused: {path}")
        temporary = path.with_suffix(f".html.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(body)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise CacheIntegrityError("post-write SHA-256 verification failed")
        os.replace(temporary, path)
        _record_manifest(
            self.manifest_path, target, cache_path=path, status=status,
            digest=digest, error_code=None,
        )
        self._failures = self._paywall_failures = 0
        return CachedPage(body, target, path, digest, utc_now(), False, status or 200)


def parse_training_records(body: bytes, *, source_key: str) -> list[dict[str, Any]]:
    soup = _soup(body)
    records: list[dict[str, Any]] = []
    for horse_block in soup.select("td.Horse_Info"):
        name_a = horse_block.select_one(".Horse_Name a[href]")
        if not name_a:
            continue
        match = re.search(r"/horse/([A-Za-z0-9]+)", str(name_a["href"]))
        if not match:
            continue
        horse_id, horse_name = match.group(1), name_a.get_text(strip=True)
        row = horse_block.find_parent("tr")
        rows = [row]
        sibling = row.find_next_sibling("tr") if row else None
        while sibling is not None and not sibling.find("td", class_="Horse_Info"):
            if sibling.find(class_=re.compile(r"^Waku")):
                break
            rows.append(sibling)
            sibling = sibling.find_next_sibling("tr")
        for item in rows:
            day = item.find("td", class_="Training_Day")
            if not day:
                continue
            course = day.find_next_sibling("td")
            going = course.find_next_sibling("td") if course else None
            timing = item.find("td", class_="TrainingTimeData")
            comment = timing.select_one(".Comment_Cell p") if timing else None
            load = item.find("td", class_="TrainingLoad")
            critic = item.find("td", class_="Training_Critic")
            rank = item.find("td", class_=re.compile(r"^Rank_"))
            records.append({
                "source_key": source_key, "horse_id": horse_id,
                "horse_name": horse_name, "training_day": day.get_text(strip=True),
                "course": course.get_text(strip=True) if course else None,
                "baba": going.get_text(strip=True) if going else None,
                "laps": (
                    [li.get_text(" ", strip=True) for li in timing.select("li")]
                    if timing else []
                ),
                "comment": comment.get_text(strip=True) if comment else None,
                "load": load.get_text(strip=True) if load else None,
                "critic": critic.get_text(strip=True) if critic else None,
                "rank": rank.get_text(strip=True) if rank else None,
            })
    return records


def _normalise_place(value: str) -> str:
    for place in PLACE_CODES:
        if place in value:
            return place
    return value


def race_rows(db_path: Path, start: str, end: str) -> list[tuple[str, str, int]]:
    with sqlite3.connect(db_path) as db:
        return [
            (date, _normalise_place(place), int(race_no))
            for date, place, race_no in db.execute(
                """
                SELECT DISTINCT date, place, r FROM runs
                WHERE date BETWEEN ? AND ? ORDER BY date, place, r
                """,
                (start, end),
            )
        ]


def horse_ids(db_path: Path, start: str, end: str) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [
            row[0] for row in db.execute(
                """
                SELECT DISTINCT i.horse_id
                FROM nk_horse_ids i JOIN runs r ON r.horse=i.horse
                WHERE r.date BETWEEN ? AND ?
                ORDER BY i.horse_id
                """,
                (start, end),
            )
        ]


def horse_ids_from_race_cache(root: Path, years: set[int]) -> list[str]:
    """Enumerate horses from paid race pages, avoiding an incomplete name join."""
    directory = Path(root) / "race"
    if not directory.is_dir():
        return []
    result: set[str] = set()
    for path in directory.glob("*.html"):
        if not re.fullmatch(r"\d{12}\.html", path.name):
            continue
        if int(path.name[:4]) not in years:
            continue
        result.update(detailed_horse_ids(_soup(path.read_bytes())))
    return sorted(result)


def resolve_day(fetcher: TrainingFetcher, date8: str) -> dict[tuple[str, int], str]:
    page = fetcher.fetch(index_target(date8))
    soup = _soup(page.body)
    result: dict[tuple[str, int], str] = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/race/(\d{12})/?", str(anchor["href"]))
        if not match:
            continue
        race_id = match.group(1)
        code, race_no = race_id[4:6], int(race_id[-2:])
        place = next((name for name, value in PLACE_CODES.items() if value == code), "")
        if place:
            result[(place, race_no)] = race_id
    return result


def iter_race_targets(
    fetcher: TrainingFetcher, db_path: Path, start: str, end: str,
) -> Iterable[Target]:
    rows = race_rows(db_path, start, end)
    by_date: dict[str, list[tuple[str, int]]] = {}
    for date8, place, race_no in rows:
        by_date.setdefault(date8, []).append((place, race_no))
    with sqlite3.connect(db_path) as db:
        for date8, races in by_date.items():
            try:
                resolved = resolve_day(fetcher, date8)
            except (
                CookieConfigError, LiveGateError,
                DailyLimitReached, StopAfterFailures,
            ):
                raise
            except T42Error:
                # 一時的なindex失敗 (db.netkeibaの間欠400等) で無人バックフィル
                # 全体を落とさない。1回だけ間隔を置いて再試行し、それでも
                # 駄目なら再送出 (プロセス停止・再開可能)
                time.sleep(120)
                resolved = resolve_day(fetcher, date8)
            for place, race_no in races:
                race_id = resolved.get((place, race_no))
                if not race_id:
                    raise T42Error(
                        f"race_id unresolved: {date8} {place} {race_no}R"
                    )
                expected = [
                    row[0] for row in db.execute(
                        """
                        SELECT DISTINCT i.horse_id
                        FROM runs r LEFT JOIN nk_horse_ids i ON i.horse=r.horse
                        WHERE r.date=? AND r.place=? AND r.r=? AND i.horse_id IS NOT NULL
                        """,
                        (date8, place, race_no),
                    )
                ]
                expected_count = int(db.execute(
                    """
                    SELECT COUNT(*) FROM runs
                    WHERE date=? AND place=? AND r=?
                    """,
                    (date8, place, race_no),
                ).fetchone()[0])
                yield race_target(race_id, expected, expected_count)


def planned_targets(
    fetcher: TrainingFetcher, db_path: Path,
    phases: Iterable[int],
) -> Iterable[Target]:
    for phase in phases:
        if phase == 1:
            yield from iter_race_targets(fetcher, db_path, "20210101", "20260630")
        elif phase == 2:
            values = horse_ids_from_race_cache(
                fetcher.cache_root, set(range(2021, 2027))
            )
            if not values:
                raise T42Error("phase 2 requires completed phase 1 race caches")
            yield from (horse_target(value) for value in values)
        elif phase == 3:
            yield from iter_race_targets(fetcher, db_path, "20180101", "20201231")
        elif phase == 4:
            values = horse_ids_from_race_cache(
                fetcher.cache_root, {2018, 2019, 2020}
            )
            if not values:
                raise T42Error("phase 4 requires completed phase 3 race caches")
            yield from (horse_target(value) for value in values)
        else:
            raise ValueError(f"unknown phase: {phase}")


def run_targets(
    fetcher: TrainingFetcher, targets: Iterable[Target], *,
    request_budget: int | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "started_at": utc_now(), "processed": 0, "cached": 0, "fetched": 0,
        "parsed_records": 0, "failures": 0, "permanent_gaps": 0,
        "stopped": None,
    }
    start_requests = fetcher.network_requests
    try:
        for target in targets:
            if (
                request_budget is not None
                and fetcher.network_requests - start_requests >= request_budget
            ):
                summary["stopped"] = "request_budget"
                break
            try:
                page = fetcher.fetch(target)
                summary["processed"] += 1
                summary["cached" if page.cache_hit else "fetched"] += 1
                if target.category in {"race", "horse"}:
                    summary["parsed_records"] += len(parse_training_records(
                        page.body, source_key=target.key
                    ))
            except (
                CookieConfigError, LiveGateError,
                DailyLimitReached, StopAfterFailures,
            ):
                raise
            except PermanentPageGap:
                summary["permanent_gaps"] += 1
            except T42Error:
                summary["failures"] += 1
    except (DailyLimitReached, StopAfterFailures) as exc:
        summary["stopped"] = type(exc).__name__
    summary["network_requests"] = fetcher.network_requests - start_requests
    summary["finished_at"] = utc_now()
    return summary


def manifest_status(path: Path) -> dict[str, Any]:
    init_manifest(path)
    with sqlite3.connect(path) as db:
        categories = dict(db.execute(
            """
            SELECT category, COUNT(*) FROM fetch_manifest
            WHERE error_code IS NULL AND cache_path IS NOT NULL GROUP BY category
            """
        ))
        failures = int(db.execute(
            "SELECT COUNT(*) FROM fetch_manifest WHERE error_code IS NOT NULL"
        ).fetchone()[0])
        today = int(db.execute(
            "SELECT COUNT(*) FROM request_log WHERE local_day=?", (jst_day(),)
        ).fetchone()[0])
    return {"cached_by_category": categories, "failures": failures,
            "requests_today_jst": today}


def _explicit_target(value: str) -> Target:
    category, separator, key = value.partition(":")
    if not separator:
        raise argparse.ArgumentTypeError("target must be race:ID or horse:ID")
    if category not in {"race", "horse"}:
        raise argparse.ArgumentTypeError("target must be race:ID or horse:ID")
    try:
        if category == "race":
            race_id, count_separator, count_text = key.partition(":")
            count = int(count_text) if count_separator else None
            if count is not None and count < 1:
                raise ValueError("expected horse count must be positive")
            return race_target(race_id, expected_horse_count=count)
        if ":" in key:
            raise ValueError("horse target does not accept a count")
        return horse_target(key)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--interval", type=float, default=INTERVAL)
    parser.add_argument("--daily-limit", type=int, default=DAILY_LIMIT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-trial-active", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stage0", action="store_true")
    parser.add_argument("--target", action="append", type=_explicit_target, default=[])
    parser.add_argument("--phases", type=int, nargs="+", choices=(1, 2, 3, 4))
    parser.add_argument("--summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        print(json.dumps(manifest_status(args.manifest), ensure_ascii=False, indent=2))
        return 0
    if args.stage0 and (not args.target or len(args.target) > 100):
        raise SystemExit("Stage 0 requires 1..100 explicit --target values")
    if not args.stage0 and not args.phases:
        raise SystemExit("choose --stage0 or --phases")
    fetcher = TrainingFetcher(
        cache_root=args.cache_root, manifest_path=args.manifest,
        cookies_path=args.cookies, interval=args.interval,
        daily_limit=args.daily_limit, live=args.live,
        trial_acknowledged=args.acknowledge_trial_active,
    )
    targets = args.target if args.stage0 else planned_targets(
        fetcher, args.db, args.phases
    )
    try:
        with scrape_lock(args.lock):
            summary = run_targets(
                fetcher, targets, request_budget=100 if args.stage0 else None,
            )
    except ScrapeLockError as exc:
        print(f"[T42a] {exc}")
        return 2
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
