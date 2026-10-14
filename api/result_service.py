"""Parse official JRA result pages and reconcile them with prediction logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
_LOGGER = logging.getLogger(__name__)


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
    container = soup.select_one("#race_result")
    tables = container.select("table") if container else []
    if not tables:
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
    seen_horse_numbers: set[int] = set()
    duplicate_count = 0
    # #race_result wraps the runner-result table alongside unrelated tables
    # (lap time, corner-passage order) that share the container but carry no
    # td.num/td.horse cells. JRA has also been observed splitting the runner
    # rows themselves across more than one <tbody>, or more than one <table>,
    # under the same container (T84: rows past 5th place silently dropped
    # because only the first <table>'s rows were read). Every table and every
    # tbody under the container is therefore scanned; a <tr> is only treated
    # as a result row when a horse number AND a horse name can both be read
    # from it -- that alone is enough to skip header rows, empty rows, and
    # the unrelated tables, without depending on table/tbody position.
    for result_table in tables:
        for tr in result_table.select("tbody tr") or result_table.select("tr"):
            position_text = tr.select_one("td.place")
            number_text = tr.select_one("td.num")
            horse_text = tr.select_one("td.horse")
            horse_no = _integer(number_text.get_text(" ", strip=True) if number_text else None)
            horse_name = horse_text.get_text(" ", strip=True) if horse_text else None
            if horse_no is None or not horse_name:
                continue
            if horse_no in seen_horse_numbers:
                duplicate_count += 1
                continue
            seen_horse_numbers.add(horse_no)
            position = _integer(position_text.get_text(" ", strip=True) if position_text else None)
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
    if duplicate_count:
        _LOGGER.warning(
            "parse_result_html: race %s had %d duplicate horse row(s) collapsed to the first occurrence",
            race_id, duplicate_count,
        )
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


def _get(url: str, timeout: int, *, encoding: str = "cp932"):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    response.encoding = encoding
    return response


def _known_field_size(race_id: str, target: LoggingStore) -> int | None:
    """Best-effort lookup of the most recently observed odds_snapshots.field_size
    for a race (T84 §2 health check). Queried directly with sqlite3 -- same
    pattern already used by monitored_result_sources() below -- rather than
    adding a LoggingStore method, since this fix is scoped to this file only.
    """
    try:
        target.initialize()
        with sqlite3.connect(target.db_path) as conn:
            row = conn.execute(
                """SELECT field_size FROM odds_snapshots
                   WHERE race_id=? AND field_size IS NOT NULL
                   ORDER BY observed_at DESC LIMIT 1""",
                (race_id,),
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row and row[0] is not None else None


def _flag_partial_result_rows(parsed: dict[str, Any], target: LoggingStore) -> bool:
    """Mark every row with a 'partial_result_rows' quality flag when the row
    count about to be saved looks suspiciously low next to the field size
    recorded in odds_snapshots (T84 §2, e.g. 5 rows while field_size>=8). This
    never blocks the save: the payout figures already parsed from the page
    stay correct even when some runner rows are missing, so the flag only
    marks the race for a later re-fetch (T84 Stage C) rather than discarding
    anything.
    """
    rows = parsed["rows"]
    field_size = _known_field_size(parsed["race_id"], target)
    if field_size is None or field_size < 8 or len(rows) > field_size - 3:
        return False
    for row in rows:
        flags = row.setdefault("data_quality_flags", [])
        if "partial_result_rows" not in flags:
            flags.append("partial_result_rows")
    return True


def _save_parsed(parsed: dict[str, Any], target: LoggingStore) -> dict[str, Any]:
    parsed["partial_result_rows_flagged"] = _flag_partial_result_rows(parsed, target)
    parsed["save_stats"] = target.save_race_results(parsed["rows"])
    parsed["match_summary"] = target.result_match_summary(parsed["race_id"])
    return parsed


# ─── 部分取り込みレース結果の再同期 (T84b: T84 Stage A/B の後続) ───────────────
# T84 Stage A で確定した真因: 発走16分後では約5%のレースでJRAの全着順が
# まだ上位5頭しか出ておらず、結果は一度しか取得しないのでその5%が永久に
# 部分データのまま残る。ここでは決定ロジック・通知・仮想購入には一切触れず、
# 保存済みのsource_urlを後から1回だけ取り直して行を補う入口だけを追加する。

DAILY_RESYNC_KIND = "t84b_result_resync"


def find_incomplete_results(db_path: str | Path, *, max_age_days: int = 60) -> list[dict[str, Any]]:
    """race_results を読み、取り直す価値がある (欠落している可能性が高い)
    レースを列挙する (読み取り専用・SPEC-T84b §1-1)。

    対象にする条件 (いずれか):
      - 保存済み行の data_quality_flags_json に partial_result_rows を含む
      - 保存行数が同レースの odds_snapshots.field_size に対して明らかに少ない
        (field_size>=8 かつ rows<=field_size-3。T84 §2 の検知式と同一)
    source_url が保存されていないレース (取り直し先が特定できない) は対象外
    にし、件数だけ警告ログへ出す。max_age_days より古いレースは対象外 (日々の
    定期実行が無限に古いデータを掘り返さないようにする既定のガード)。
    """
    store = LoggingStore(db_path)
    store.initialize()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)) \
        .isoformat(timespec="microseconds").replace("+00:00", "Z")
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            """SELECT race_id, COUNT(*) AS n_rows, MAX(source_url) AS source_url,
                      MAX(result_fetched_at) AS result_fetched_at,
                      MAX(CASE WHEN data_quality_flags_json LIKE '%partial_result_rows%'
                               THEN 1 ELSE 0 END) AS has_flag
               FROM race_results
               GROUP BY race_id
               HAVING MAX(result_fetched_at) >= ?
               ORDER BY result_fetched_at""",
            (cutoff,),
        ).fetchall()
    targets = []
    skipped_no_source = 0
    for race_id, n_rows, source_url, result_fetched_at, has_flag in rows:
        field_size = _known_field_size(race_id, store)
        row_count_low = field_size is not None and field_size >= 8 and n_rows <= field_size - 3
        if not (has_flag or row_count_low):
            continue
        if not source_url:
            skipped_no_source += 1
            continue
        targets.append({
            "race_id": race_id, "rows": n_rows, "field_size": field_size,
            "source_url": source_url, "result_fetched_at": result_fetched_at,
        })
    if skipped_no_source:
        _LOGGER.warning(
            "find_incomplete_results: %d incomplete race(s) have no source_url on file "
            "and cannot be re-fetched", skipped_no_source,
        )
    return targets


def resync_result(race_id: str, *, db_path: str | Path, sleep_seconds: float = 1.5,
                  timeout: int = 20) -> dict[str, Any]:
    """1レースの保存済み source_url を1回だけ取り直し、行を upsert する
    (SPEC-T84b §1-2)。決定ロジック・通知・仮想購入には触れない。

    同一horse_idの既存行は上書き (着順・払戻が確定後の値に揃う)、無い行は
    追加する (save_race_results のON CONFLICT upsertに委ねる)。再取得後の
    行数が取り直し前と同じかそれ以下なら何も書き込まない ("no_change")。
    取得・解析に失敗した場合も何も書き込まない ("failed")。
    """
    target = LoggingStore(db_path)
    target.initialize()
    with sqlite3.connect(target.db_path) as conn:
        row = conn.execute(
            "SELECT source_url FROM race_results WHERE race_id=? AND source_url IS NOT NULL LIMIT 1",
            (race_id,),
        ).fetchone()
        existing_rows = conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE race_id=?", (race_id,)
        ).fetchone()[0]
    if not row or not row[0]:
        return {"status": "failed", "race_id": race_id, "error": "no source_url on file"}
    url = row[0]
    # netkeiba/JRAへの連続アクセスは1秒以上空ける (プロジェクト規約)。単体で
    # 呼ばれても間隔を守れるよう、この関数自身がリクエスト前にスリープする。
    time.sleep(max(sleep_seconds, 0))
    try:
        response = _get(url, timeout)
        parsed = parse_result_html(response.text, source_url=response.url)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "failed", "race_id": race_id, "error": f"{type(exc).__name__}: {exc}"}
    new_row_count = len(parsed["rows"])
    if new_row_count <= existing_rows:
        return {"status": "no_change", "race_id": race_id,
                "rows_before": existing_rows, "rows_after": new_row_count}
    saved = _save_parsed(parsed, target)
    return {"status": "resynced", "race_id": race_id, "rows_before": existing_rows,
            "rows_after": new_row_count, "save_stats": saved["save_stats"],
            "partial_result_rows_flagged": saved["partial_result_rows_flagged"]}


def resync_incomplete_results(db_path: str | Path, *, limit: int = 50, sleep_seconds: float = 1.5,
                              max_age_days: int = 60, timeout: int = 20) -> dict[str, Any]:
    """find_incomplete_results で対象を列挙し、古い順に最大 limit 件まで
    resync_result を実行する (SPEC-T84b §1-3)。各 resync_result 呼び出しが
    自前でリクエスト前に sleep_seconds 秒待つため、呼び出し間隔は自動的に
    最低 sleep_seconds 秒空く。
    """
    targets = find_incomplete_results(db_path, max_age_days=max_age_days)
    targets = sorted(targets, key=lambda item: item["result_fetched_at"] or "")
    targets = targets[:max(0, limit)]
    details = []
    counts = {"resynced": 0, "no_change": 0, "failed": 0}
    for item in targets:
        outcome = resync_result(item["race_id"], db_path=db_path, sleep_seconds=sleep_seconds, timeout=timeout)
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
        details.append(outcome)
    return {"targets": len(targets), "resynced": counts["resynced"], "no_change": counts["no_change"],
            "failed": counts["failed"], "details": details}


def resync_incomplete_results_daily(db_path: str | Path | None = None, *, kind: str = DAILY_RESYNC_KIND,
                                    limit: int = 20, sleep_seconds: float = 1.5,
                                    max_age_hours: float = 24.0) -> dict[str, Any]:
    """resync_incomplete_results を1日1回までに制限して呼び出す (SPEC-T84b §2)。
    直近実行時刻は resync_runs(kind) に記録する。max_age_hours 以内に実行済み
    なら何もせず {"skipped": True, ...} を返す (ネットワークアクセスなし)。
    viewer_mode() の判定は呼び出し元 (jra_ev.worker_analyze_all の完了フック)
    の責務で、この関数自体は行わない (単体/CLIからも使えるようにするため)。
    """
    target = LoggingStore(db_path) if db_path else LoggingStore()
    target.initialize()
    last_run_at = target.get_resync_last_run(kind)
    if last_run_at:
        try:
            last_dt = datetime.fromisoformat(str(last_run_at).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        except ValueError:
            elapsed_hours = max_age_hours  # 壊れた値は実行済みとみなさない (安全側)
        if elapsed_hours < max_age_hours:
            return {"skipped": True, "reason": "already run within max_age_hours",
                    "last_run_at": last_run_at}
    result = resync_incomplete_results(target.db_path, limit=limit, sleep_seconds=sleep_seconds)
    target.mark_resync_run(kind)
    result["skipped"] = False
    return result


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


# ─── WIN5 payout (T55) ──────────────────────────────────────────────────────
# WIN5 spans 5 races across up to 5 venues and has no payout page of its own on
# jra.go.jp per race, so the official race-result flow above cannot surface it.
# netkeiba's WIN5 corner (race.netkeiba.com/top/win5.html?date=YYYYMMDD) publishes
# the same figures JRA announces (cross-checked manually against jra.go.jp/news
# for 2026-07-12: payout 6,412,100 yen / 79 tickets) and is stable/scriptable,
# so it is used as the source here. One request per race day; see
# fetch_and_save_win5_result / sync_results_for_date for the call-site rules.
WIN5_RESULT_URL = "https://race.netkeiba.com/top/win5.html?date={date}"
_WIN5_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _yen_amount(text: str | None) -> int | None:
    """Parse a Japanese amount like "641万2100円" / "5億3990万5240円" / "-円" into yen.

    Returns None for the "-円" placeholder netkeiba shows when there is no payout
    (e.g. a carryover day with zero winning tickets).
    """
    text = (text or "").strip()
    if not text or text.startswith("-"):
        return None
    match = re.match(r"(?:(\d+)億)?(?:(\d+)万)?(\d+)?円", text)
    if not match or not any(match.groups()):
        return None
    oku, man, yen = (int(g) if g else 0 for g in match.groups())
    return oku * 100_000_000 + man * 10_000 + yen


def parse_win5_result_html(html: str, *, race_date: str | None = None, source_url: str = "") -> dict[str, Any]:
    """Parse netkeiba's WIN5 result page into the win5_results row shape (T55)."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".TitleHeadingWin")
    heading_text = heading.get_text(strip=True) if heading else ""
    date_match = re.search(r"(20\d{2})年(\d{2})月(\d{2})日", heading_text)
    if not race_date and date_match:
        race_date = f"{date_match.group(1)}{date_match.group(2)}{date_match.group(3)}"
    if not race_date:
        raise ValueError("win5 result date could not be determined")

    numbers = [_integer(li.get_text(strip=True)) for li in soup.select(".Win5_UmabanWrap li.w5")]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        raise ResultNotReady("win5 result page has no winning-number data yet")

    payout_yen = hit_ticket_count = None
    for row in soup.select(".WIN5_AllResult tr.Result"):
        th, td = row.select_one("th"), row.select_one("td")
        if th is None or td is None:
            continue
        label = th.get_text(strip=True)
        if label == "払戻金":
            payout_yen = _yen_amount(td.get_text(strip=True))
        elif label == "的中票数":
            hit_ticket_count = _integer(td.get_text(strip=True))

    carryover_amount = None
    carryover_table = soup.select_one('table[summary="結果"]')
    if carryover_table and "キャリーオーバー" in carryover_table.get_text():
        td = carryover_table.select_one("td")
        carryover_amount = _yen_amount(td.get_text(strip=True)) if td else None
    carryover_flag = bool(carryover_amount)

    source_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return {
        "race_date": race_date, "payout_yen": payout_yen, "hit_ticket_count": hit_ticket_count,
        "carryover_flag": carryover_flag, "carryover_amount": carryover_amount,
        "winning_numbers": numbers, "source_url": source_url, "source_hash": source_hash,
    }


def fetch_and_save_win5_result(race_date: str, *, store: LoggingStore | None = None,
                               timeout: int = 20, force: bool = False) -> dict[str, Any]:
    """Fetch and persist one day's official WIN5 payout (fail-soft; T55).

    At most one HTTP request per race day: if a row already exists for the date
    (a prior fetch already succeeded), this returns it without a network call
    unless ``force`` is set. Any failure (network error, unparsable/not-yet-
    published page) raises ResultNotReady/ValueError and leaves no row behind,
    so the dashboard keeps showing "未取得" until a later sync retries it.
    """
    compact = normalize_race_date(race_date)
    target = store or LoggingStore()
    if not force:
        cached = target.get_win5_result(compact)
        if cached is not None:
            return dict(cached, already_stored=True)
    url = WIN5_RESULT_URL.format(date=compact)
    time.sleep(1)  # netkeibaへのアクセスは1リクエスト1秒以上のスリープを挟む (厳守事項)
    try:
        response = requests.get(url, headers=_WIN5_HEADERS, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
    except requests.RequestException as exc:
        raise ResultNotReady(f"{url}: {type(exc).__name__}: {exc}") from exc
    parsed = parse_win5_result_html(response.text, race_date=compact, source_url=url)
    target.save_win5_result(
        race_date=parsed["race_date"], payout_yen=parsed["payout_yen"],
        hit_ticket_count=parsed["hit_ticket_count"], carryover_flag=parsed["carryover_flag"],
        carryover_amount=parsed["carryover_amount"], winning_numbers=parsed["winning_numbers"],
        source_url=parsed["source_url"], source_hash=parsed["source_hash"],
    )
    return dict(parsed, already_stored=False)


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

    # WIN5 payout (T55): fail-soft best-effort, at most one netkeiba request (see
    # fetch_and_save_win5_result). A missing/failed fetch must not affect the
    # official race-result sync above, so failures are swallowed here.
    win5_result: dict[str, Any] = {"fetched": False}
    try:
        parsed = fetch_and_save_win5_result(compact, store=target, timeout=timeout)
        win5_result = {
            "fetched": True, "already_stored": parsed.get("already_stored", False),
            "payout_yen": parsed.get("payout_yen"), "hit_ticket_count": parsed.get("hit_ticket_count"),
            "carryover_flag": parsed.get("carryover_flag"),
        }
    except Exception as exc:
        win5_result = {"fetched": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "date": compact,
        "sources": len(sources),
        "synced": len(synced),
        "failed": len(failed),
        "details": sorted(synced, key=lambda item: item["race_id"]),
        "errors": sorted(failed, key=lambda item: item["race_id"]),
        "win5_result": win5_result,
    }


def save_result_html(path: str | Path, *, source_url: str = "fixture://local",
                     store: LoggingStore | None = None) -> dict[str, Any]:
    html = Path(path).read_text(encoding="utf-8")
    parsed = parse_result_html(html, source_url=source_url)
    target = store or LoggingStore()
    return _save_parsed(parsed, target)


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
