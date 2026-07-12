"""Parse official JRA result pages and reconcile them with prediction logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

try:
    from .logging_store import LoggingStore
except ImportError:  # direct execution from api/
    from logging_store import LoggingStore

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://www.jra.go.jp/"}
VENUES = ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉")


class ResultNotReady(ValueError):
    pass


def _integer(text: str | None) -> int | None:
    match = re.search(r"\d+", (text or "").replace(",", ""))
    return int(match.group()) if match else None


def _payouts(soup: BeautifulSoup, css_class: str) -> dict[int, int]:
    result = {}
    for line in soup.select(f".refund_unit li.{css_class} .line"):
        number = _integer(line.select_one(".num").get_text(" ", strip=True) if line.select_one(".num") else None)
        amount = _integer(line.select_one(".yen").get_text(" ", strip=True) if line.select_one(".yen") else None)
        if number is not None and amount is not None:
            result[number] = amount
    return result


def parse_result_html(html: str, *, source_url: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#race_result table")
    if table is None:
        raise ResultNotReady("official race result table is not available")
    opt = soup.select_one("span.opt")
    info = opt.get_text(" ", strip=True) if opt else soup.get_text(" ", strip=True)[:500]
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", info)
    race_match = re.search(r"(\d{1,2})レース", info)
    venue = next((item for item in VENUES if item in info), None)
    if not (date_match and race_match and venue):
        raise ValueError(f"race identity could not be parsed: {info[:120]}")
    race_day = f"{date_match.group(1)}{int(date_match.group(2)):02d}{int(date_match.group(3)):02d}"
    race_no = int(race_match.group(1))
    race_id = f"{race_day}:{venue}:{race_no:02d}"
    page_text = soup.get_text(" ", strip=True)
    official_status = "corrected" if "訂正" in page_text else "official"
    win = _payouts(soup, "win")
    place = _payouts(soup, "place")
    source_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    fetched_at = datetime.now().astimezone()
    rows = []
    for tr in table.select("tbody tr") or table.select("tr")[1:]:
        position_text = tr.select_one("td.place")
        number_text = tr.select_one("td.num")
        horse_text = tr.select_one("td.horse")
        horse_no = _integer(number_text.get_text(" ", strip=True) if number_text else None)
        if horse_no is None:
            continue
        position = _integer(position_text.get_text(" ", strip=True) if position_text else None)
        horse_name = horse_text.get_text(" ", strip=True) if horse_text else None
        flags = []
        if position is None:
            flags.append("non_numeric_finish_status")
        final_odds = win.get(horse_no) / 100.0 if horse_no in win else None
        if final_odds is None:
            flags.append("final_win_odds_unavailable")
        rows.append({
            "race_id": race_id, "horse_id": f"{race_id}:{horse_no:02d}", "horse_name": horse_name,
            "finish_position": position, "official_status": official_status,
            "final_win_odds": final_odds, "win_payout": win.get(horse_no),
            "place_payout": place.get(horse_no), "result_fetched_at": fetched_at,
            "source_url": source_url, "source_hash": source_hash,
            "data_quality_flags": flags,
        })
    if not rows:
        raise ResultNotReady("official result contains no runners")
    return {"race_id": race_id, "official_status": official_status, "rows": rows,
            "source_hash": source_hash}


def _cname(url: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get("CNAME")
    return unquote(values[0]) if values else None


def _cname_race_identity(url: str) -> str | None:
    """Return the checksum-independent portion of a JRA race-page CNAME."""
    value = _cname(url)
    if not value:
        return None
    match = re.match(r"[a-z]{2}\d+(?:dde|sde)([^/]+)", value, re.IGNORECASE)
    return match.group(1) if match else None


def result_url_candidates(url: str, *, card_html: str | None = None,
                          base_url: str | None = None) -> list[str]:
    """Resolve official result links embedded in a JRA race-card page.

    The two hexadecimal characters after the slash in CNAME are a page-specific
    validation value.  Replacing ``accessD/dde`` with ``accessS/sde`` therefore
    creates an invalid URL.  The card page itself contains the valid result URL,
    so candidates are selected from its links by the checksum-independent race
    identity.
    """
    if "accessS.html" in url:
        return [url]
    if not card_html:
        return []

    expected = _cname_race_identity(url)
    candidates = []
    soup = BeautifulSoup(card_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "accessS.html" not in href or "CNAME=" not in href:
            continue
        candidate = urljoin(base_url or url, href)
        if expected and _cname_race_identity(candidate) != expected:
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _get(url: str, timeout: int):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    response.encoding = "cp932"
    return response


def _save_parsed(parsed: dict[str, Any], target: LoggingStore) -> dict[str, Any]:
    parsed["save_stats"] = target.save_race_results(parsed["rows"])
    parsed["match_summary"] = target.result_match_summary(parsed["race_id"])
    return parsed


def fetch_and_save_result(url: str, *, store: LoggingStore | None = None,
                          timeout: int = 20) -> dict[str, Any]:
    target = store or LoggingStore()
    errors = []
    try:
        entry = _get(url, timeout)
    except requests.RequestException as exc:
        raise ResultNotReady(f"{url}: {type(exc).__name__}: {exc}") from exc

    # A caller may already supply a result URL.  Also accept a response that
    # directly contains results even if its path is non-standard.
    try:
        parsed = parse_result_html(entry.text, source_url=entry.url)
        return _save_parsed(parsed, target)
    except ValueError as exc:
        errors.append(f"{entry.url}: {type(exc).__name__}: {exc}")

    candidates = result_url_candidates(url, card_html=entry.text, base_url=entry.url)
    if not candidates:
        raise ResultNotReady("; ".join(errors + [
            f"{entry.url}: official result link is not available"
        ]))

    for candidate in candidates:
        try:
            response = _get(candidate, timeout)
            parsed = parse_result_html(response.text, source_url=candidate)
            return _save_parsed(parsed, target)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
    raise ResultNotReady("; ".join(errors))


def normalize_race_date(value: str) -> str:
    compact = str(value or "").strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("date must be YYYY-MM-DD or YYYYMMDD")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("date is not a valid calendar date") from exc
    return compact


def monitored_result_sources(race_date: str, *, store: LoggingStore | None = None) -> list[dict[str, str]]:
    """Return persisted race-card URLs for one locally monitored race day."""
    compact = normalize_race_date(race_date)
    target = store or LoggingStore()
    target.initialize()
    sources: dict[str, str] = {}
    with sqlite3.connect(target.db_path) as conn:
        rows = conn.execute(
            """SELECT race_id,payload_json FROM monitored_races
               WHERE substr(coalesce(race_id,''),1,8)=? ORDER BY race_id""",
            (compact,),
        ).fetchall()
    for race_id, payload_json in rows:
        try:
            url = json.loads(payload_json).get("url")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if race_id and url:
            sources[str(race_id)] = str(url)
    return [{"race_id": race_id, "url": url} for race_id, url in sorted(sources.items())]


def sync_results_for_date(race_date: str, *, store: LoggingStore | None = None,
                          timeout: int = 20, max_workers: int = 4) -> dict[str, Any]:
    """Fetch all persisted official result pages for a monitored race day."""
    compact = normalize_race_date(race_date)
    target = store or LoggingStore()
    sources = monitored_result_sources(compact, store=target)
    synced = []
    failed = []

    def fetch(source: dict[str, str]) -> tuple[dict[str, str], dict[str, Any]]:
        return source, fetch_and_save_result(source["url"], store=target, timeout=timeout)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as pool:
        futures = {pool.submit(fetch, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                _, parsed = future.result()
                synced.append({
                    "race_id": parsed["race_id"],
                    "saved": parsed.get("save_stats", {}),
                })
            except Exception as exc:
                failed.append({
                    "race_id": source["race_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

    return {
        "date": compact,
        "sources": len(sources),
        "synced": len(synced),
        "failed": len(failed),
        "details": sorted(synced, key=lambda item: item["race_id"]),
        "errors": sorted(failed, key=lambda item: item["race_id"]),
    }


def save_result_html(path: str | Path, *, source_url: str = "fixture://local",
                     store: LoggingStore | None = None) -> dict[str, Any]:
    html = Path(path).read_text(encoding="utf-8")
    parsed = parse_result_html(html, source_url=source_url)
    target = store or LoggingStore()
    parsed["save_stats"] = target.save_race_results(parsed["rows"])
    parsed["match_summary"] = target.result_match_summary(parsed["race_id"])
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and reconcile an official JRA race result")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url")
    group.add_argument("--html")
    group.add_argument("--date", help="sync every monitored race for YYYY-MM-DD")
    parser.add_argument("--db")
    args = parser.parse_args()
    store = LoggingStore(args.db) if args.db else LoggingStore()
    if args.url:
        result = fetch_and_save_result(args.url, store=store)
    elif args.html:
        result = save_result_html(args.html, store=store)
    else:
        result = sync_results_for_date(args.date, store=store)
    print({key: value for key, value in result.items() if key != "rows"})


if __name__ == "__main__":
    main()
