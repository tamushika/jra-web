"""T59d display-only market probability and JRA board-odds helpers.

This module is intentionally independent from ``combo_probs`` and every EV,
notification, and production win-probability path.
"""

from __future__ import annotations

import itertools
import math
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


LAMBDA2 = 0.830185
LAMBDA3 = 0.720886
BOOK_MIN = 1.15
BOOK_MAX = 1.45
JRA_ODDS_URL = "https://www.jra.go.jp/JRADB/accessO.html"
SOURCE = "jra_official"
BET_TYPES = ("place", "wide", "umaren")
_CNAME_RE = re.compile(r"['\"](pw151ou[^'\"]+Z/[0-9A-Fa-f]{2})['\"]")
_BOARD_CNAME_RE = re.compile(r"['\"](pw15([45])ou[^'\"]+/[0-9A-Fa-f]{2})['\"]")


def extract_entry_cname(html_or_soup) -> str | None:
    text = str(html_or_soup or "")
    match = _CNAME_RE.search(text)
    return match.group(1) if match else None


def _number(text):
    try:
        value = float(str(text).replace(",", "").strip())
        return value if math.isfinite(value) and value > 0.0 else None
    except (TypeError, ValueError):
        return None


def _range(text):
    values = [_number(value) for value in re.findall(r"\d+(?:\.\d+)?", str(text))]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return (values[0], values[-1])


def parse_place_board(html: str) -> dict[tuple[int], tuple[float, float]]:
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for row in soup.select("table tr"):
        number = row.select_one("td.num")
        odds = row.select_one("td.odds_fuku")
        parsed = _range(odds.get_text(" ", strip=True) if odds else "")
        try:
            key = (int(number.get_text(strip=True)),) if number else None
        except ValueError:
            key = None
        if key and parsed:
            result[key] = parsed
    return result


def parse_pair_board(html: str, css_class: str) -> dict[tuple[int, int], tuple[float, float]]:
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for table in soup.select(f"table.{css_class}"):
        caption = table.select_one("caption")
        try:
            first = int(caption.get_text(strip=True))
        except (AttributeError, ValueError):
            continue
        for row in table.select("tr"):
            second_node = row.select_one("th")
            odds_node = row.select_one("td")
            try:
                second = int(second_node.get_text(strip=True))
            except (AttributeError, ValueError):
                continue
            parsed = _range(odds_node.get_text(" ", strip=True) if odds_node else "")
            if parsed:
                result[tuple(sorted((first, second)))] = parsed
    return result


def _decode(response) -> str:
    response.raise_for_status()
    response.encoding = "cp932"
    return response.text


def fetch_jra_odds(entry_cname: str, *, session=requests,
                   now=lambda: datetime.now(timezone.utc)) -> dict:
    """Fetch place entry plus wide/umaren boards; never reuse old values."""
    requested_at = now().isoformat()
    entry_html = _decode(session.post(
        JRA_ODDS_URL, data={"cname": entry_cname},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.jra.go.jp/"},
        timeout=20,
    ))
    cnames = {}
    for cname, kind in _BOARD_CNAME_RE.findall(entry_html):
        cnames[{"4": "umaren", "5": "wide"}[kind]] = cname
    if set(cnames) != {"wide", "umaren"}:
        raise ValueError("JRA odds navigation did not expose wide/umaren boards")
    umaren_html = _decode(session.post(
        JRA_ODDS_URL, data={"cname": cnames["umaren"]},
        headers={"User-Agent": "Mozilla/5.0", "Referer": JRA_ODDS_URL}, timeout=20))
    wide_html = _decode(session.post(
        JRA_ODDS_URL, data={"cname": cnames["wide"]},
        headers={"User-Agent": "Mozilla/5.0", "Referer": JRA_ODDS_URL}, timeout=20))
    received_at = now().isoformat()
    return {
        "requested_at": requested_at,
        "received_at": received_at,
        "source": SOURCE,
        "odds": {
            "place": parse_place_board(entry_html),
            "wide": parse_pair_board(wide_html, "wide"),
            "umaren": parse_pair_board(umaren_html, "umaren"),
        },
    }


def market_probabilities(win_odds) -> tuple[tuple[float, ...], float] | None:
    odds = [_number(value) for value in win_odds]
    if any(value is None or value <= 1.0 or value >= 999.0 for value in odds):
        return None
    inverse = [1.0 / value for value in odds]
    book_sum = sum(inverse)
    if not BOOK_MIN <= book_sum <= BOOK_MAX:
        return None
    probabilities = tuple(value / book_sum for value in inverse)
    assert math.isclose(sum(probabilities), 1.0, abs_tol=1e-12)
    return probabilities, book_sum


def _conditional(probabilities, excluded, exponent):
    weights = [0.0 if index in excluded else value ** exponent
               for index, value in enumerate(probabilities)]
    denominator = sum(weights)
    result = tuple(value / denominator for value in weights)
    assert math.isclose(sum(result), 1.0, abs_tol=1e-12)
    return result


def derive_probabilities(probabilities, *, places=3) -> dict[str, dict | tuple | None]:
    """Apply accepted lambdas to place/wide and lambda=1 to umaren.

    Wide is left as ``None`` when ``places == 2`` (7-or-fewer-runner fields):
    T59c's evaluation population excluded every field-under-8 race entirely,
    so lambda3's fitted top-3 wide formula was never validated there and
    must not be extrapolated. Place switches to its own validated top-2
    rule instead. Umaren is unconditional (lambda=1, no fitted parameter,
    no field-size-dependent payout rule) so it is unaffected.
    """
    n = len(probabilities)
    if places not in (2, 3) or places > n:
        raise ValueError("places must be 2 or 3")
    place = [0.0] * n
    wide = ({pair: 0.0 for pair in itertools.combinations(range(n), 2)}
            if places == 3 else None)
    umaren = {pair: 0.0 for pair in itertools.combinations(range(n), 2)}
    for first in range(n):
        calibrated_second = _conditional(probabilities, (first,), LAMBDA2)
        baseline_second = _conditional(probabilities, (first,), 1.0)
        for second in range(n):
            if second == first:
                continue
            umaren[tuple(sorted((first, second)))] += (
                probabilities[first] * baseline_second[second])
            p12 = probabilities[first] * calibrated_second[second]
            if places == 2:
                place[first] += p12
                place[second] += p12
                continue
            calibrated_third = _conditional(
                probabilities, (first, second), LAMBDA3)
            for third in range(n):
                if third in (first, second):
                    continue
                value = p12 * calibrated_third[third]
                place[first] += value
                place[second] += value
                place[third] += value
                top3 = tuple(sorted((first, second, third)))
                for pair in itertools.combinations(top3, 2):
                    wide[pair] += value
    assert math.isclose(sum(place), float(places), abs_tol=1e-10)
    assert math.isclose(sum(umaren.values()), 1.0, abs_tol=1e-10)
    return {"place": tuple(place), "wide": wide, "umaren": umaren}


def unavailable_board(message: str, *, stage=None, status="unavailable") -> dict:
    return {
        "status": status, "message": message, "stage": stage,
        "source": SOURCE, "requested_at": None, "received_at": None,
        "book_sum": None, "rows": {kind: [] for kind in BET_TYPES},
    }


def build_board(horses, fetched: dict, *, stage=None) -> dict:
    parsed = market_probabilities([horse.get("odds") for horse in horses])
    if parsed is None:
        return unavailable_board("表示不可", stage=stage, status="book_outside")
    probabilities, book_sum = parsed
    derived = derive_probabilities(probabilities, places=2 if len(horses) <= 7 else 3)
    numbers = [int(float(horse["num"])) for horse in horses]
    names = [str(horse.get("name") or "") for horse in horses]
    rows = {kind: [] for kind in BET_TYPES}
    unavailable_kinds = {}
    for kind in BET_TYPES:
        values = derived[kind]
        if values is None:
            unavailable_kinds[kind] = (
                "7頭以下のため対象外 (T59cの検証母集団は8頭以上に限定)")
            continue
        iterator = enumerate(values) if kind == "place" else values.items()
        for indexes, probability in iterator:
            indexes = (indexes,) if kind == "place" else indexes
            combo_numbers = tuple(numbers[index] for index in indexes)
            combo = "-".join(str(number) for number in combo_numbers)
            actual = fetched["odds"].get(kind, {}).get(tuple(sorted(combo_numbers)))
            low, high = actual if actual else (None, None)
            representative = ((low + high) / 2.0) if low is not None else None
            fair = 1.0 / probability
            rows[kind].append({
                "bet_type": kind, "combo": combo,
                "names": [names[index] for index in indexes],
                "model_probability": probability, "fair_odds": fair,
                "odds": representative, "odds_low": low, "odds_high": high,
                "gap_ratio": fair / representative if representative else None,
                "odds_status": "ok" if actual else "fetch_failed",
            })
    return {
        "status": "ok", "message": "", "stage": stage,
        "source": fetched["source"],
        "requested_at": fetched["requested_at"],
        "received_at": fetched["received_at"],
        "book_sum": book_sum, "rows": rows,
        "unavailable_kinds": unavailable_kinds,
    }


def snapshot_rows(board, *, race_id, race_date, place, race_no,
                  fetch_id=None) -> list[dict]:
    common = {
        "race_id": race_id, "date": race_date, "place": place, "r": race_no,
        "requested_at": board.get("requested_at"),
        "received_at": board.get("received_at"), "source": board.get("source", SOURCE),
        "stage": board.get("stage"), "fetch_id": fetch_id,
    }
    unavailable_kinds = board.get("unavailable_kinds") or {}
    result = []
    for kind in BET_TYPES:
        kind_rows = board.get("rows", {}).get(kind, [])
        if not kind_rows:
            if kind in unavailable_kinds:
                status, reason = "unavailable", unavailable_kinds[kind]
            else:
                status = board.get("status", "unavailable")
                reason = board.get("message") or "unavailable"
            result.append({**common, "bet_type": kind, "combo": "*",
                           "status": status, "data_quality_flags": [reason]})
            continue
        for row in kind_rows:
            result.append({
                **common, **{key: row.get(key) for key in (
                    "bet_type", "combo", "model_probability", "fair_odds",
                    "odds", "odds_low", "odds_high", "gap_ratio")},
                "status": row.get("odds_status", "ok"),
                "data_quality_flags": ([] if row.get("odds_status") == "ok"
                                       else ["odds_unavailable"]),
            })
    return result
