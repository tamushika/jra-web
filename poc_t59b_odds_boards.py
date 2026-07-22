"""Capture a tiny, rate-limited T59b odds-board PoC fixture set.

This script is intentionally not a collector.  It is fixed to one race day/race,
writes only gitignored research fixtures, and enforces the SPEC request ceiling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


OUT_DIR = Path("data/t59")
MIN_INTERVAL_SECONDS = 1.05
MAX_REQUESTS_PER_SOURCE = 30
USER_AGENT = "Mozilla/5.0 (compatible; JRA-Web-T59b-PoC/1.0)"

# 2026-07-19, Fukushima 8R (race_id=202603020808), selected so both
# sources describe exactly the same completed race.
TARGETS = (
    ("jra", "robots", "GET", "https://www.jra.go.jp/robots.txt", None),
    (
        "jra",
        "fukushima_08_odds_entry",
        "POST",
        "https://www.jra.go.jp/JRADB/accessO.html",
        {"cname": "pw151ouS303202602080820260719Z/15"},
    ),
    *tuple(
        (
            "jra",
            name,
            "POST",
            "https://www.jra.go.jp/JRADB/accessO.html",
            {"cname": cname},
        )
        for name, cname in (
            ("board_wakuren", "pw153ouS303202602080820260719Z/1D"),
            ("board_umaren", "pw154ouS303202602080820260719Z/A1"),
            ("board_wide", "pw155ouS303202602080820260719Z/25"),
            ("board_sanrenpuku", "pw157ouS303202602080820260719Z99/8F"),
        )
    ),
    ("netkeiba", "robots", "GET", "https://www.netkeiba.com/robots.txt", None),
    (
        "netkeiba",
        "fukushima_08_index",
        "GET",
        "https://race.netkeiba.com/odds/index.html?race_id=202603020808",
        None,
    ),
    (
        "netkeiba",
        "fukushima_08_win_place",
        "GET",
        "https://race.netkeiba.com/odds/index.html?race_id=202603020808&type=b1",
        None,
    ),
    (
        "netkeiba",
        "fukushima_08_wakuren",
        "GET",
        "https://race.netkeiba.com/odds/index.html?race_id=202603020808&type=b3",
        None,
    ),
    (
        "netkeiba",
        "fukushima_08_umaren",
        "GET",
        "https://race.netkeiba.com/odds/index.html?race_id=202603020808&type=b4",
        None,
    ),
    (
        "netkeiba",
        "fukushima_08_wide",
        "GET",
        "https://race.netkeiba.com/odds/index.html?race_id=202603020808&type=b5",
        None,
    ),
    *tuple(
        (
            "netkeiba",
            f"api_fukushima_08_type_{odds_type}",
            "GET",
            "https://race.netkeiba.com/api/api_get_jra_odds.html"
            "?pid=api_get_jra_odds&input=UTF-8&output=jsonp"
            f"&race_id=202603020808&type={odds_type}"
            "&action=init&sort=odds&compress=0&callback=t59b",
            None,
        )
        for odds_type in (1, 3, 4, 5)
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("jra", "netkeiba"))
    parser.add_argument("--name-prefix")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    counts: dict[str, int] = {}
    last_request: dict[str, float] = {}
    manifest: list[dict[str, object]] = []

    for source, name, method, url, data in TARGETS:
        if args.source and source != args.source:
            continue
        if args.name_prefix and not name.startswith(args.name_prefix):
            continue
        counts[source] = counts.get(source, 0) + 1
        if counts[source] > MAX_REQUESTS_PER_SOURCE:
            raise RuntimeError(f"{source}: request ceiling exceeded")

        if source in last_request:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_request[source])
            if wait > 0:
                time.sleep(wait)

        started = datetime.now(timezone.utc)
        response = session.request(method, url, data=data, timeout=30)
        last_request[source] = time.monotonic()
        body = response.content
        suffix = ".txt" if urlparse(url).path.endswith("robots.txt") else ".html"
        path = OUT_DIR / f"{source}_{name}{suffix}"
        path.write_bytes(body)
        manifest.append(
            {
                "source": source,
                "name": name,
                "requested_url": url,
                "method": method,
                "form_fields": sorted(data) if data else [],
                "final_url": response.url,
                "retrieved_at_utc": started.isoformat(),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "fixture": path.as_posix(),
            }
        )
        print(source, name, response.status_code, len(body), response.url)

    run_suffix = ""
    if args.source or args.name_prefix:
        parts = [part for part in (args.source, args.name_prefix) if part]
        safe_parts = [part.strip("_").replace("_", "-") for part in parts]
        run_suffix = "_" + "_".join(safe_parts)
    manifest_path = OUT_DIR / f"manifest{run_suffix}.json"
    manifest_path.write_text(
        json.dumps(
            {
                "purpose": "SPEC-T59b Stage 0 feasibility PoC only",
                "request_interval_seconds": MIN_INTERVAL_SECONDS,
                "request_ceiling_per_source": MAX_REQUESTS_PER_SOURCE,
                "requests_this_capture": counts,
                "responses": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
