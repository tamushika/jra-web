"""Leakage-safe selection of odds snapshots at a common evaluation cutoff.

This module deliberately depends only on the DB-API connection passed by the
caller.  It does not initialise or mutate the logging database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterator, Literal, Mapping


WIN5_CUTOFF_MARGIN_MIN = 5
SnapshotPurpose = Literal["win5", "single_race_ev"]
SNAPSHOT_PURPOSES = frozenset({"single_race_ev", "win5"})

# ``late_capture`` and ``scheduler_restart`` describe how a row was captured,
# but do not invalidate an actually observed timestamp.  The three flags below
# are the T40 conditions that make an odds row unsuitable for model evaluation.
DISQUALIFYING_QUALITY_FLAGS = frozenset(
    {"insufficient_odds", "catchup_burst", "post_time_changed"}
)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _require_aware_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _comparable_datetime(value: datetime) -> datetime | None:
    """Normalise an aware value, failing closed for naive DB timestamps."""

    if value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _quality_flags(value: Any) -> tuple[tuple[str, ...], bool]:
    """Decode a flag column and report whether its representation was valid."""

    if value is None or value == "":
        return (), True
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return (), False
    if not isinstance(value, (list, tuple, set)):
        return (), False
    flags: list[str] = []
    for flag in value:
        if not isinstance(flag, str) or not flag:
            return (), False
        flags.append(flag)
    return tuple(sorted(set(flags))), True


@dataclass(frozen=True)
class SnapshotRow(Mapping[str, Any]):
    """Immutable mapping with attribute access for one selected DB row.

    ``observed_at`` is normalised to a :class:`datetime` and
    ``data_quality_flags`` is a tuple.  The original JSON text remains
    available as ``data_quality_flags_json``.
    """

    _values: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        observed_at: datetime,
        quality_flags: tuple[str, ...],
    ) -> "SnapshotRow":
        normalised = dict(values)
        normalised["observed_at"] = observed_at
        normalised["data_quality_flags"] = quality_flags
        return cls(MappingProxyType(normalised))

    @property
    def race_id(self) -> str:
        return str(self._values["race_id"])

    @property
    def horse_id(self) -> str:
        return str(self._values["horse_id"])

    @property
    def observed_at(self) -> datetime:
        return self._values["observed_at"]

    @property
    def quality_flags(self) -> tuple[str, ...]:
        return self._values["data_quality_flags"]

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class SnapshotSelection(list[SnapshotRow]):
    """List-compatible result carrying mandatory exclusion accounting."""

    def __init__(
        self,
        rows: list[SnapshotRow] | None = None,
        *,
        purpose: SnapshotPurpose,
        excluded_counts: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(rows or [])
        self.purpose = purpose
        self.excluded_counts = MappingProxyType(dict(excluded_counts or {}))

    @property
    def rows(self) -> list[SnapshotRow]:
        return list(self)

    @property
    def excluded_quality_count(self) -> int:
        return int(self.excluded_counts.get("quality", 0))

    @property
    def excluded_count(self) -> int:
        """Compatibility alias for the quality-exclusion count."""

        return self.excluded_quality_count


def win5_cutoff(first_leg_post_time: datetime) -> datetime:
    """Return the conservative common WIN5 information cutoff."""

    first_leg_post_time = _require_aware_datetime(
        first_leg_post_time, field="first_leg_post_time"
    )
    return first_leg_post_time - timedelta(minutes=WIN5_CUTOFF_MARGIN_MIN)


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def select_snapshots(
    connection: Any,
    race_ids: Any,
    *,
    cutoff: datetime,
    purpose: SnapshotPurpose,
    require_quality: bool = True,
) -> list[SnapshotRow]:
    """Select the last eligible row per ``(race_id, horse_id)`` at *cutoff*.

    The concrete return object is a list-compatible :class:`SnapshotSelection`.
    Callers must report ``excluded_quality_count`` (or the more detailed
    ``excluded_counts``) with their evaluation.  Rows with missing/ambiguous
    timestamps fail closed and can never pass the cutoff.

    Quality filtering happens before latest-row selection.  Consequently, a
    bad latest capture does not hide an earlier clean snapshot for that horse.
    The function only reads the supplied connection.
    """

    if purpose not in SNAPSHOT_PURPOSES:
        allowed = ", ".join(sorted(SNAPSHOT_PURPOSES))
        raise ValueError(f"purpose must be one of: {allowed}")
    cutoff = _require_aware_datetime(cutoff, field="cutoff")
    ids = list(dict.fromkeys(str(race_id) for race_id in race_ids))
    counts = {
        "quality": 0,
        "missing_or_ambiguous_observed_at": 0,
        "after_cutoff": 0,
    }
    if not ids:
        return SnapshotSelection(purpose=purpose, excluded_counts=counts)

    placeholders = ",".join("?" for _ in ids)
    cursor = connection.execute(
        f"SELECT * FROM odds_snapshots WHERE race_id IN ({placeholders})",
        ids,
    )
    cutoff_value = cutoff.astimezone(timezone.utc)
    selected: dict[tuple[str, str], tuple[datetime, int, SnapshotRow]] = {}

    for ordinal, raw_row in enumerate(cursor):
        values = _row_mapping(cursor, raw_row)
        observed = _parse_datetime(values.get("observed_at"))
        if observed is None:
            counts["missing_or_ambiguous_observed_at"] += 1
            continue
        comparable = _comparable_datetime(observed)
        if comparable is None:
            counts["missing_or_ambiguous_observed_at"] += 1
            continue
        if comparable > cutoff_value:
            counts["after_cutoff"] += 1
            continue

        flags, flags_valid = _quality_flags(values.get("data_quality_flags_json"))
        stale = bool(values.get("is_stale", 0))
        invalid_quality = (
            not flags_valid
            or stale
            or bool(DISQUALIFYING_QUALITY_FLAGS.intersection(flags))
        )
        if require_quality and invalid_quality:
            counts["quality"] += 1
            continue

        row = SnapshotRow.from_mapping(
            values, observed_at=observed, quality_flags=flags
        )
        key = (row.race_id, row.horse_id)
        raw_id = values.get("odds_snapshot_id")
        try:
            tie_breaker = int(raw_id)
        except (TypeError, ValueError):
            tie_breaker = ordinal
        previous = selected.get(key)
        candidate = (comparable, tie_breaker, row)
        if previous is None or candidate[:2] > previous[:2]:
            selected[key] = candidate

    rows = [value[2] for _, value in sorted(selected.items())]
    return SnapshotSelection(rows, purpose=purpose, excluded_counts=counts)
