"""Generate the production-shaped factor snapshot from ``ability.db``.

The command is intentionally separate from live scoring.  Running it is the
only operation that writes ``factor_snapshot.json``; importing scoring never
creates or updates the file.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from factor_snapshot import (SNAPSHOT_FILENAME, build_snapshot_payload,
                             write_factor_snapshot)  # noqa: E402
from fold_stats import (FoldFactorTableProvider,
                        discover_legacy_courses)  # noqa: E402


DEFAULT_DB_PATH = os.path.join(BASE_DIR, "ability.db")
DEFAULT_OUTPUT_PATH = os.path.join(
    API_DIR, "data_files", "common", SNAPSHOT_FILENAME)


def _format_courses(courses) -> str:
    return ", ".join(
        f"{venue}/{surface}/{distance}"
        for venue, surface, distance in sorted(courses)
    ) or "(none)"


def _validate_course_coverage(generated_courses, legacy_courses) -> None:
    """Reject a snapshot that would silently disable any legacy course.

    A valid snapshot is authoritative in live scoring, so falling back only for
    missing courses would mix statistics from different periods.  Generation
    is therefore all-or-nothing against the course universe represented by the
    currently deployed legacy factor CSVs.
    """
    generated = set(generated_courses)
    expected = set(legacy_courses)
    if not expected:
        raise ValueError("legacy course universe is empty")

    missing = expected - generated
    extra = generated - expected
    if missing or extra:
        raise ValueError(
            "factor snapshot course coverage mismatch: "
            f"missing=[{_format_courses(missing)}]; "
            f"extra=[{_format_courses(extra)}]"
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="ability.dbからローリング5年のfactor snapshotを生成")
    parser.add_argument(
        "--as-of", default=date.today().strftime("%Y%m%d"), metavar="YYYYMMDD",
        help="集計締切日 (既定: 今日)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH,
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def generate_snapshot(db_path: str, output_path: str, as_of: str) -> dict:
    """Build and write one strict rolling-window snapshot."""
    try:
        as_of_date = datetime.strptime(as_of, "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--as-of must be YYYYMMDD: {as_of!r}") from exc
    if as_of_date > date.today():
        raise ValueError("--as-of cannot be in the future")
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"ability database not found: {db_path}")

    provider = FoldFactorTableProvider(
        db_path,
        as_of,
        pedigree_cache_path=os.path.join(BASE_DIR, "pedigree_cache.json"),
        legacy_api_dir=API_DIR,
    )
    legacy_courses = discover_legacy_courses(API_DIR)
    _validate_course_coverage(provider.tables, legacy_courses)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = build_snapshot_payload(
        provider.tables,
        as_of=provider.as_of,
        stats_from=provider.stats_from,
        generated_at=generated_at,
    )
    write_factor_snapshot(output_path, payload)
    return payload


def main(argv=None) -> None:
    args = parse_args(argv)
    payload = generate_snapshot(args.db, args.output, args.as_of)
    meta = payload["_meta"]
    print(f"factor snapshot生成: {meta['stats_from']}-{meta['as_of']} / "
          f"{meta['course_count']}コース")
    print(f"保存先: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
