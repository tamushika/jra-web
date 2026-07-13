"""Serialization helpers for ability.db-derived factor snapshots.

The production scorer must be able to consume a generated snapshot without
depending on SQLite or the offline aggregation code.  This module therefore
contains only the small, versioned JSON contract shared by the generator and
``api.scoring``.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "factor_snapshot.json"
SOURCE_NAME = "ability.db:runs"

_CACHE: dict[str, Any] = {
    "path": None,
    "signature": None,
    "snapshot": None,
}


def _date8(value: Any, label: str) -> str:
    value = str(value or "")
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError(f"{label} must be YYYYMMDD: {value!r}")
    return value


def _surface(value: Any) -> str | None:
    value = str(value or "")
    if value == "芝":
        return "芝"
    if value in ("ダ", "ダート"):
        return "ダート"
    return None


def course_key(venue: Any, race_type: Any, distance: Any):
    """Normalize one course identity to the scorer/provider convention."""
    surface = _surface(race_type)
    try:
        distance = int(distance)
    except (TypeError, ValueError):
        return None
    venue = str(venue or "").strip()
    if not venue or surface is None or distance <= 0:
        return None
    return venue, surface, distance


def _valid_rate(value: Any) -> bool:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and 0.0 <= value <= 100.0


def build_snapshot_payload(
    tables: Mapping[tuple[str, str, int], Mapping[str, Any]],
    *,
    as_of: str,
    stats_from: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable snapshot payload."""
    as_of = _date8(as_of, "as_of")
    stats_from = _date8(stats_from, "stats_from")
    if stats_from > as_of:
        raise ValueError("stats_from must not be after as_of")

    courses = []
    for raw_key, table in sorted(tables.items(), key=lambda item: item[0]):
        key = course_key(*raw_key)
        if key is None:
            raise ValueError(f"invalid course key: {raw_key!r}")
        if not isinstance(table, Mapping):
            raise ValueError(f"factor table must be an object: {raw_key!r}")
        venue, race_type, distance = key
        courses.append({
            "venue": venue,
            "race_type": race_type,
            "distance": distance,
            "table": dict(table),
        })

    return {
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE_NAME,
            "generated_at": str(generated_at),
            "stats_from": stats_from,
            "as_of": as_of,
            "window_years": int(as_of[:4]) - int(stats_from[:4]) + 1,
            "strict_as_of": True,
            "course_count": len(courses),
        },
        "courses": courses,
    }


def validate_snapshot_payload(payload: Any) -> dict[str, Any]:
    """Validate and index a decoded snapshot.

    A snapshot is all-or-nothing: malformed metadata or a malformed course
    invalidates the whole file.  The caller can then safely fall back to the
    legacy CSV source rather than mixing two sources silently.
    """
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be an object")
    meta = payload.get("_meta")
    courses = payload.get("courses")
    if not isinstance(meta, dict) or not isinstance(courses, list):
        raise ValueError("snapshot must contain _meta and courses")
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported factor snapshot schema")
    if meta.get("source") != SOURCE_NAME or meta.get("strict_as_of") is not True:
        raise ValueError("snapshot source metadata is invalid")
    as_of = _date8(meta.get("as_of"), "_meta.as_of")
    stats_from = _date8(meta.get("stats_from"), "_meta.stats_from")
    if stats_from > as_of:
        raise ValueError("snapshot stats_from is after as_of")
    if meta.get("course_count") != len(courses):
        raise ValueError("snapshot course_count does not match courses")
    if not courses:
        raise ValueError("snapshot must contain at least one course")

    tables: dict[tuple[str, str, int], dict[str, Any]] = {}
    for course in courses:
        if not isinstance(course, dict):
            raise ValueError("snapshot course must be an object")
        key = course_key(
            course.get("venue"), course.get("race_type"), course.get("distance"))
        table = course.get("table")
        if key is None or not isinstance(table, dict) or key in tables:
            raise ValueError("snapshot contains an invalid or duplicate course")
        baseline = table.get("baseline")
        if (not isinstance(baseline, dict)
                or not _valid_rate(baseline.get("win_rate"))
                or not _valid_rate(baseline.get("show_rate"))):
            raise ValueError(f"snapshot baseline is invalid: {key!r}")
        for factor_type, factor_rows in table.items():
            if factor_type in ("baseline", "_meta"):
                continue
            if not isinstance(factor_rows, dict):
                raise ValueError(
                    f"snapshot factor map is invalid: {key!r}/{factor_type}")
            for entity, row in factor_rows.items():
                if (not str(entity) or not isinstance(row, dict)
                        or not _valid_rate(row.get("win_rate"))
                        or not _valid_rate(row.get("show_rate"))):
                    raise ValueError(
                        f"snapshot factor row is invalid: "
                        f"{key!r}/{factor_type}/{entity!r}")
        tables[key] = table
    return {"meta": dict(meta), "tables": tables}


def reset_snapshot_cache() -> None:
    """Clear the process-local file cache (primarily useful for tests)."""
    _CACHE.update(path=None, signature=None, snapshot=None)


def load_factor_snapshot(path: str | os.PathLike[str]):
    """Load and validate a snapshot, returning ``None`` on any file error."""
    path = os.path.abspath(os.fspath(path))
    try:
        stat = os.stat(path)
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None

    if _CACHE["path"] == path and _CACHE["signature"] == signature:
        return _CACHE["snapshot"]

    snapshot = None
    if signature is not None:
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                snapshot = validate_snapshot_payload(json.load(fp))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            snapshot = None
    _CACHE.update(path=path, signature=signature, snapshot=snapshot)
    return snapshot


def write_factor_snapshot(
    path: str | os.PathLike[str], payload: Mapping[str, Any]
) -> None:
    """Validate and atomically write a snapshot in UTF-8."""
    validate_snapshot_payload(dict(payload))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
            fp.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
