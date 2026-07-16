"""T34b: safely plan and (only when explicitly approved) repair 2025 races duplicates.

The command is a dry-run by default.  It deliberately has no import-time database
side effects.  ``--apply`` is fail-closed and requires hashes for both the exact
approved manifest file and its plan; production execution is an operator
responsibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


NATURAL_KEY = ("date", "place", "race_num", "horse_number")
ROW_ID = "__ctid__"
PREDICATE_SQL = (
    "date IS NOT NULL AND place IS NOT NULL AND race_num IS NOT NULL "
    "AND horse_number IS NOT NULL"
)
MANDATORY_FIELDS = (
    "date", "place", "horse", "horse_number", "rank", "time",
    "track_type", "distance", "total_horses",
)
VETO_FIELDS = ("agari_3f", "corner_4", "weight", "condition")
CLASSIFICATION_NAMES = (
    "KEEP_CURRENT", "MOVE_CANDIDATE", "DELETE_EXACT_DUPLICATE",
    "DELETE_REDUNDANT_ERROR", "NON_RUNNER_KEEP", "UNVERIFIED_KEEP",
    "UNRESOLVED",
)
UNVERIFIED_REASON_CODES = (
    "TRUTH_ZERO", "TRUTH_MULTI", "PAYLOAD_MISMATCH",
    "UMABAN_MISMATCH", "INVALID_VALUE",
)
CANDIDATE_REASON_CODES = ("a", "b", "c")
PLAN_STATUSES = ("APPLICABLE", "NO_MUTATIONS_NEEDED", "NOT_APPLICABLE")
TRUTH_ROW_LIMIT = 200_000
CLOSURE_ROW_LIMIT = 100_000

# §9 gate.  These rows were manually corrected outside this tool and must be
# present, exactly once, both in the approved snapshot and again under the
# apply transaction's table lock.
MANUAL_FIX_EXPECTED_ROWS = (
    {"date": "250208", "place": "小倉", "race_num": 5,
     "horse_number": 3, "horse": "ミグラテール"},
    {"date": "250208", "place": "小倉", "race_num": 5,
     "horse_number": 5, "horse": "トーアマリシテン"},
    {"date": "250208", "place": "小倉", "race_num": 5,
     "horse_number": 7, "horse": "アルカンサス"},
    {"date": "250412", "place": "中山", "race_num": 3,
     "horse_number": 9, "horse": "スターコンパス"},
    {"date": "250412", "place": "中山", "race_num": 1,
     "horse_number": 9, "horse": "モーニングマジック"},
    {"date": "251116", "place": "福島", "race_num": 5,
     "horse_number": 9, "horse": "チンプンカンプン"},
)


class SafetyError(RuntimeError):
    """Raised when a T34b safety invariant is not satisfied."""


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"\s+", "", normalized).strip()
    return normalized or None


def normalize_neon_date(value: Any) -> str | None:
    """Convert a real 2025 YYMMDD value to YYYYMMDD, otherwise return None."""
    raw = normalize_text(value)
    if raw is None or not re.fullmatch(r"\d{6}", raw):
        return None
    candidate = "20" + raw
    try:
        parsed = datetime.strptime(candidate, "%Y%m%d").date()
    except ValueError:
        return None
    return candidate if parsed.year == 2025 else None


def strict_int(value: Any, minimum: int | None = None,
               maximum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal):
        if value != value.to_integral_value():
            return None
        result = int(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        result = int(value)
    else:
        raw = unicodedata.normalize("NFKC", str(value)).strip()
        if not re.fullmatch(r"[+-]?\d+", raw):
            return None
        result = int(raw)
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def to_deciseconds(value: Any) -> int | None:
    """Parse seconds or M:SS.d exactly; precision finer than 0.1s is invalid."""
    if value is None or isinstance(value, bool):
        return None
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if not raw:
        return None
    try:
        if ":" in raw:
            match = re.fullmatch(r"(\d+):([0-5]?\d(?:\.\d)?)", raw)
            if not match:
                return None
            seconds = Decimal(match.group(1)) * 60 + Decimal(match.group(2))
        else:
            seconds = Decimal(raw)
        scaled = seconds * 10
        if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled < 0:
            return None
        return int(scaled)
    except InvalidOperation:
        return None


def to_neon_time_deciseconds(value: Any) -> int | None:
    """Parse Neon ``races.time`` without changing generic seconds semantics.

    Digit-only text is the source-specific compact representation ``MSSd`` or
    ``MMSSd``.  A decimal point or colon makes the representation explicit and
    delegates to :func:`to_deciseconds`.  Numeric Python values are seconds,
    matching the ability.db contract.  Ambiguous or non-canonical digit text is
    rejected instead of being guessed.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        return to_deciseconds(value)

    raw = unicodedata.normalize("NFKC", value).strip()
    if not raw:
        return None
    if ":" in raw or "." in raw:
        return to_deciseconds(raw)
    if re.fullmatch(r"\d{4,5}", raw) is None:
        return None
    if len(raw) == 5 and raw.startswith("0"):
        return None

    minutes = int(raw[:-3])
    seconds = int(raw[-3:-1])
    tenths = int(raw[-1])
    if seconds >= 60:
        return None
    result = (minutes * 60 + seconds) * 10 + tenths
    return result if result > 0 else None


def canonical_track_type(value: Any) -> str | None:
    raw = normalize_text(value)
    if raw in {"ダ", "ダート"}:
        return "ダート"
    return raw if raw in {"芝"} else None


def canonical_condition(value: Any) -> str | None:
    raw = normalize_text(value)
    if raw in {"良"}:
        return "良"
    if raw in {"稍", "稍重"}:
        return "稍"
    if raw in {"重"}:
        return "重"
    if raw in {"不", "不良"}:
        return "不"
    return None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SafetyError("non-finite float cannot be serialized")
        return {"__t34b_type__": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SafetyError("non-finite decimal cannot be serialized")
        return {"__t34b_type__": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"__t34b_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__t34b_type__": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"__t34b_type__": "bytes", "value": value.hex()}
    raise SafetyError(f"unsupported backup value type: {type(value).__name__}")


def _canonical(value: Any, *, omit_ctid: bool = True) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item, omit_ctid=omit_ctid)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not (omit_ctid and str(key) in {ROW_ID, "raw_ctid"})
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item, omit_ctid=omit_ctid) for item in value]
    return _json_value(value)


def canonical_json_bytes(value: Any, *, omit_ctid: bool = True) -> bytes:
    return json.dumps(
        _canonical(value, omit_ctid=omit_ctid), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def logical_row_fingerprint(row: Mapping[str, Any]) -> str:
    logical = {key: value for key, value in row.items() if not str(key).startswith("__")}
    return hashlib.sha256(canonical_json_bytes(logical, omit_ctid=True)).hexdigest()


def canonical_plan_sha256(plan: Mapping[str, Any]) -> str:
    logical = dict(plan)
    logical.pop("plan_sha256", None)
    # These collections are logical sets/multisets.  Their presentation order
    # must not turn an otherwise identical fresh plan into false data drift.
    for key in ("logical_rows", "classifications", "operations", "reasons"):
        if isinstance(logical.get(key), list):
            logical[key] = sorted(
                logical[key], key=lambda item: canonical_json_bytes(item, omit_ctid=True)
            )
    return hashlib.sha256(canonical_json_bytes(logical, omit_ctid=True)).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise SafetyError(f"{label} must be exactly 64 hexadecimal characters")
    return value.casefold()


def _load_strict_json(raw: bytes, label: str) -> Any:
    def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SafetyError(f"{label} is not strict UTF-8 JSON") from exc


def ability_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    resolved = Path(path).resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved), "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns, "sha256": file_sha256(resolved),
    }


def open_ability_read_only(path: str | os.PathLike[str]) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def load_ability_truth(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    connection = open_ability_read_only(path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM runs WHERE date BETWEEN '20250101' AND '20251231' "
            "ORDER BY date, place, horse, r, umaban LIMIT ?",
            (TRUTH_ROW_LIMIT + 1,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > TRUTH_ROW_LIMIT:
        raise SafetyError(
            f"ability truth exceeds the fail-closed limit ({TRUTH_ROW_LIMIT})"
        )
    return [dict(row) for row in rows]


def build_truth_index(truth_rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_slot: set[tuple[str, str, int, int]] = set()
    for original in truth_rows:
        row = dict(original)
        day = normalize_text(row.get("date"))
        place = normalize_text(row.get("place"))
        horse = normalize_text(row.get("horse"))
        race = strict_int(row.get("r"), 1, 12)
        number = strict_int(row.get("umaban"), 1, 18)
        try:
            valid_truth_day = (
                bool(day) and bool(re.fullmatch(r"2025\d{4}", day))
                and datetime.strptime(day, "%Y%m%d").year == 2025
            )
        except (TypeError, ValueError):
            valid_truth_day = False
        if not valid_truth_day or not place or not horse or race is None or number is None:
            raise SafetyError("ability truth has an invalid 2025 identity field")
        slot = (day, place, race, number)
        if slot in seen_slot:
            raise SafetyError(f"ability truth natural key is duplicated: {slot!r}")
        seen_slot.add(slot)
        result[(day, place, horse)].append(row)
    return {
        key: sorted(values, key=lambda item: canonical_json_bytes(item))
        for key, values in result.items()
    }


def build_truth_slot_index(
        truth_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, int, int], dict[str, Any]]:
    """Index frozen truth by its complete natural key, validating identities."""
    result: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for original in truth_rows:
        row = dict(original)
        day = normalize_text(row.get("date"))
        place = normalize_text(row.get("place"))
        horse = normalize_text(row.get("horse"))
        race = strict_int(row.get("r"), 1, 12)
        number = strict_int(row.get("umaban"), 1, 18)
        try:
            valid_day = (
                bool(day) and bool(re.fullmatch(r"2025\d{4}", day))
                and datetime.strptime(day, "%Y%m%d").year == 2025
            )
        except (TypeError, ValueError):
            valid_day = False
        if not valid_day or not place or not horse or race is None or number is None:
            raise SafetyError("ability truth has an invalid 2025 identity field")
        key = (day, place, race, number)
        if key in result:
            raise SafetyError(f"ability truth natural key is duplicated: {key!r}")
        result[key] = row
    return result


def _normalized_pair(field: str, row: Mapping[str, Any], truth: Mapping[str, Any]) -> tuple[Any, Any]:
    if field == "date":
        return normalize_neon_date(row.get("date")), normalize_text(truth.get("date"))
    if field == "place":
        return normalize_text(row.get("place")), normalize_text(truth.get("place"))
    if field == "horse":
        return normalize_text(row.get("馬名", row.get("horse"))), normalize_text(truth.get("horse"))
    if field == "horse_number":
        return strict_int(row.get("horse_number"), 1, 18), strict_int(truth.get("umaban"), 1, 18)
    if field == "rank":
        return strict_int(row.get("rank"), 1, 18), strict_int(truth.get("rank"), 1, 18)
    if field == "time":
        return to_neon_time_deciseconds(row.get("time")), to_deciseconds(truth.get("time_sec"))
    if field == "track_type":
        return canonical_track_type(row.get("track_type")), canonical_track_type(truth.get("track_type"))
    if field == "distance":
        return strict_int(row.get("distance"), 1), strict_int(truth.get("distance"), 1)
    if field == "total_horses":
        return strict_int(row.get("total_horses"), 1, 18), strict_int(truth.get("total_horses"), 1, 18)
    if field == "agari_3f":
        return to_deciseconds(row.get("agari_3f")), to_deciseconds(truth.get("agari"))
    if field == "corner_4":
        return strict_int(row.get("corner_4"), 1, 18), strict_int(truth.get("c4"), 1, 18)
    if field == "weight":
        return strict_int(row.get("weight"), 100, 800), strict_int(truth.get("weight"), 100, 800)
    if field == "condition":
        return canonical_condition(row.get("condition")), canonical_condition(truth.get("condition"))
    raise KeyError(field)


def _raw_payload_pair(field: str, row: Mapping[str, Any],
                      truth: Mapping[str, Any]) -> tuple[Any, Any]:
    keys = {
        "date": ("date", "date"), "place": ("place", "place"),
        "horse": ("馬名", "horse"), "horse_number": ("horse_number", "umaban"),
        "rank": ("rank", "rank"), "time": ("time", "time_sec"),
        "track_type": ("track_type", "track_type"), "distance": ("distance", "distance"),
        "total_horses": ("total_horses", "total_horses"),
        "agari_3f": ("agari_3f", "agari"), "corner_4": ("corner_4", "c4"),
        "weight": ("weight", "weight"), "condition": ("condition", "condition"),
    }
    row_key, truth_key = keys[field]
    row_value = row.get(row_key)
    if field == "horse" and row_key not in row:
        row_value = row.get("horse")
    return row_value, truth.get(truth_key)


def _raw_absent(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def compare_payload(row: Mapping[str, Any], truth: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    mandatory_ok = True
    veto_ok = True
    for field in MANDATORY_FIELDS + VETO_FIELDS:
        left, right = _normalized_pair(field, row, truth)
        raw_left, raw_right = _raw_payload_pair(field, row, truth)
        role = "mandatory" if field in MANDATORY_FIELDS else "veto"
        if role == "mandatory":
            if _raw_absent(raw_left) or _raw_absent(raw_right):
                status = "null"
            elif left is None or right is None:
                status = "invalid"
            else:
                status = "match" if left == right else "mismatch"
            mandatory_ok = mandatory_ok and status == "match"
        else:
            if _raw_absent(raw_left) or _raw_absent(raw_right):
                status = "not_compared"
            elif left is None or right is None:
                status = "invalid"
            else:
                status = "match" if left == right else "mismatch"
            veto_ok = veto_ok and status not in {"mismatch", "invalid"}
        fields[field] = {
            "role": role, "row_raw": raw_left, "truth_raw": raw_right,
            "row": left, "truth": right, "status": status,
        }
    for field, row_key, truth_key in (
        ("jockey", "jockey", "jockey"), ("race_name", "race_name", "race_name")
    ):
        left, right = normalize_text(row.get(row_key)), normalize_text(truth.get(truth_key))
        fields[field] = {
            "role": "auxiliary", "row": left, "truth": right,
            "status": "not_compared" if left is None or right is None else ("match" if left == right else "mismatch"),
        }
    return {"mandatory_ok": mandatory_ok, "veto_ok": veto_ok, "fields": fields}


def _truth_identity(truth: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "date": normalize_text(truth.get("date")), "place": normalize_text(truth.get("place")),
        "r": strict_int(truth.get("r"), 1, 12), "umaban": strict_int(truth.get("umaban"), 1, 18),
        "horse": normalize_text(truth.get("horse")),
    }


def _source_identity(row: Mapping[str, Any], occurrence: int) -> dict[str, Any]:
    return {"fingerprint": logical_row_fingerprint(row), "occurrence": occurrence}


def evaluate_manual_fix_gate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate the six-row §9 prerequisite without trusting physical row ids."""
    results: list[dict[str, Any]] = []
    for expected_raw in MANUAL_FIX_EXPECTED_ROWS:
        expected = dict(expected_raw)
        matching_key = [
            row for row in rows
            if normalize_text(row.get("date")) == expected["date"]
            and normalize_text(row.get("place")) == normalize_text(expected["place"])
            and strict_int(row.get("race_num"), 1, 12) == expected["race_num"]
            and strict_int(row.get("horse_number"), 1, 18) == expected["horse_number"]
        ]
        matching_horse = [
            row for row in matching_key
            if normalize_text(row.get("馬名", row.get("horse")))
            == normalize_text(expected["horse"])
        ]
        row_result = {
            "expected": expected,
            "observed_count": len(matching_key),
            "matching_horse_count": len(matching_horse),
            "observed_fingerprints": sorted(
                logical_row_fingerprint(row) for row in matching_key
            ),
            # v2.3.5: the gate protects the manually fixed horse row itself.
            # Other rows sharing the key are ordinary cleanup candidates; the
            # final-collision simulation and postcheck prove they converge to
            # one row, so their presence must not deadlock the gate.
            "ok": len(matching_horse) == 1,
        }
        results.append(row_result)
    return {"ok": all(item["ok"] for item in results), "rows": results}


def _verify_manual_fix_gate(
        rows: Sequence[Mapping[str, Any]], approved_gate: Mapping[str, Any],
) -> dict[str, Any]:
    current = evaluate_manual_fix_gate(rows)
    if not current["ok"]:
        raise SafetyError("manual fix gate is not satisfied under the apply lock")
    if canonical_json_bytes(current) != canonical_json_bytes(approved_gate):
        raise SafetyError("manual fix gate differs from approved manifest")
    return current


def _prepare_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared = [dict(row) for row in rows]
    prepared.sort(key=lambda row: (logical_row_fingerprint(row), str(row.get(ROW_ID, ""))))
    occurrences: Counter[str] = Counter()
    for row in prepared:
        fingerprint = logical_row_fingerprint(row)
        row["__fingerprint__"] = fingerprint
        row["__occurrence__"] = occurrences[fingerprint]
        occurrences[fingerprint] += 1
    return prepared


def _validated_candidate_reasons(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise SafetyError("candidate reasons must be a sequence of a/b/c codes")
    if len(set(value)) != len(value) or any(item not in CANDIDATE_REASON_CODES for item in value):
        raise SafetyError("candidate reason contains an unknown or duplicate code")
    return [code for code in CANDIDATE_REASON_CODES if code in value]


def _attach_candidate_reasons(
        rows: Sequence[Mapping[str, Any]],
        candidate_reasons: Mapping[int, Sequence[str]] | Sequence[Sequence[str]] | None,
) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    if candidate_reasons is not None and not isinstance(candidate_reasons, (Mapping, list, tuple)):
        raise SafetyError("candidate reasons must be indexed by input row")
    for index, original in enumerate(rows):
        row = dict(original)
        provided: Any = row.get("__candidate_reasons__", [])
        if isinstance(candidate_reasons, Mapping):
            provided = candidate_reasons.get(index, provided)
        elif candidate_reasons is not None:
            provided = candidate_reasons[index] if index < len(candidate_reasons) else provided
        row["__candidate_reasons__"] = _validated_candidate_reasons(provided)
        attached.append(row)
    if isinstance(candidate_reasons, Mapping):
        extra = set(candidate_reasons) - set(range(len(rows)))
        if extra:
            raise SafetyError("candidate reason references an unknown input row")
    return attached


def _truth_key(identity: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
    if not identity:
        return None
    return (
        identity.get("date"), identity.get("place"), identity.get("r"),
        identity.get("umaban"), identity.get("horse"),
    )


def _is_non_runner_marker(row: Mapping[str, Any]) -> bool:
    raw_time = row.get("time")
    normalized_time = normalize_text(raw_time)
    return row.get("rank") is None and (raw_time is None or normalized_time == "----")


def _payload_has_invalid_value(comparison: Mapping[str, Any]) -> bool:
    fields = comparison.get("fields") or {}
    payload_fields = tuple(field for field in MANDATORY_FIELDS if field not in {
        "date", "place", "horse", "horse_number",
    }) + VETO_FIELDS
    return any((fields.get(field) or {}).get("status") == "invalid" for field in payload_fields)


def _redundancy_is_proven(result: Mapping[str, Any]) -> bool:
    """Return true only for the narrowly-scoped §3.2 DELETE exception."""
    if result.get("truth_candidate_count") != 1 or not result.get("identity_valid"):
        return False
    comparison = result.get("comparison") or {}
    fields = comparison.get("fields") or {}
    # date/place/horse establish that the bad row and the complete keeper refer
    # to the same unique truth identity.  Require the classification-specific
    # comparison evidence as well; merely sharing a horse name is insufficient.
    if not all((fields.get(field) or {}).get("status") == "match"
               for field in ("date", "place", "horse")):
        return False
    classification = result.get("classification")
    if classification == "MOVE_CANDIDATE":
        source = result.get("source_natural_key") or {}
        truth = result.get("truth") or {}
        return (
            comparison.get("mandatory_ok") is True
            and comparison.get("veto_ok") is True
            and source.get("race_num") != truth.get("r")
        )
    if classification != "UNVERIFIED_KEEP":
        return False
    reason_code = result.get("reason_code")
    if reason_code == "UMABAN_MISMATCH":
        return (fields.get("horse_number") or {}).get("status") == "mismatch"
    if reason_code == "INVALID_VALUE":
        return result.get("invalid_scope") == "payload" and _payload_has_invalid_value(comparison)
    if reason_code == "PAYLOAD_MISMATCH":
        mandatory_difference = any(
            (fields.get(field) or {}).get("status") in {"null", "mismatch"}
            for field in MANDATORY_FIELDS
        )
        veto_difference = any(
            (fields.get(field) or {}).get("status") in {"invalid", "mismatch"}
            for field in VETO_FIELDS
        )
        return mandatory_difference or veto_difference
    return False


def classify_rows(
        rows: Sequence[Mapping[str, Any]],
        truth_rows: Sequence[Mapping[str, Any]],
        candidate_reasons: Mapping[int, Sequence[str]] | Sequence[Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply §6.2 stages 1 and 2; final collision promotion is planned later."""
    truth_index = build_truth_index(truth_rows)
    prepared = _prepare_rows(_attach_candidate_reasons(rows, candidate_reasons))
    results: list[dict[str, Any]] = []

    for row in prepared:
        source = _source_identity(row, row["__occurrence__"])
        day = normalize_neon_date(row.get("date"))
        place = normalize_text(row.get("place"))
        horse = normalize_text(row.get("馬名", row.get("horse")))
        race_num = strict_int(row.get("race_num"), 1, 12)
        horse_number = strict_int(row.get("horse_number"), 1, 18)
        identity_valid = all(value is not None for value in (
            day, place, horse, race_num, horse_number,
        ))
        candidates = (
            truth_index.get((day, place, horse), []) if identity_valid else []
        )
        truth_identities = [_truth_identity(candidate) for candidate in candidates]
        base: dict[str, Any] = {
            "classification": "UNVERIFIED_KEEP",
            "reason": "identity value cannot be normalized",
            "reason_code": "INVALID_VALUE",
            "candidate_reasons": list(row.get("__candidate_reasons__", [])),
            "source": source, "truth": None, "truth_candidates": truth_identities,
            "truth_candidate_count": len(candidates), "target": None,
            "keeper": None, "comparison": None,
            "identity_valid": identity_valid, "invalid_scope": "identity",
            "source_natural_key": {
                "date": row.get("date"), "place": row.get("place"),
                "race_num": row.get("race_num"),
                "horse_number": row.get("horse_number"),
            },
            "raw_ctid": row.get(ROW_ID),
        }
        # Decision table rows 1-4.  The non-runner marker is checked before any
        # payload conversion, but only after a valid identity permits truth lookup.
        if not identity_valid:
            results.append(base)
            continue
        if len(candidates) == 0:
            if _is_non_runner_marker(row):
                base.update(
                    classification="NON_RUNNER_KEEP",
                    reason="truth has no row and source is a non-runner marker",
                    reason_code=None, invalid_scope=None,
                )
            else:
                base.update(
                    reason="truth has no matching horse identity",
                    reason_code="TRUTH_ZERO", invalid_scope=None,
                )
            results.append(base)
            continue
        if len(candidates) >= 2:
            base.update(
                reason="truth has multiple horse identities",
                reason_code="TRUTH_MULTI", invalid_scope=None,
            )
            results.append(base)
            continue

        truth = candidates[0]
        truth_id = truth_identities[0]
        comparison = compare_payload(row, truth)
        base.update(truth=truth_id, comparison=comparison, invalid_scope=None)
        # Decision table row 5.
        if _payload_has_invalid_value(comparison):
            base.update(
                reason="payload value cannot be normalized",
                reason_code="INVALID_VALUE", invalid_scope="payload",
            )
            results.append(base)
            continue
        # Decision table rows 6-7.
        if comparison["mandatory_ok"] and comparison["veto_ok"]:
            if race_num == truth_id["r"]:
                base.update(
                    classification="KEEP_CURRENT",
                    reason="complete payload matches current truth slot",
                    reason_code=None,
                )
            else:
                base.update(
                    classification="MOVE_CANDIDATE",
                    reason="complete payload matches a different truth race",
                    reason_code=None,
                    target={
                        "date": row.get("date"), "place": row.get("place"),
                        "race_num": truth_id["r"],
                        "horse_number": truth_id["umaban"],
                    },
                )
            results.append(base)
            continue
        # Decision table row 8: every mandatory field except umaban matches.
        fields = comparison["fields"]
        except_umaban_ok = all(
            fields[field]["status"] == "match"
            for field in MANDATORY_FIELDS if field != "horse_number"
        )
        umaban_mismatch = fields["horse_number"]["status"] == "mismatch"
        if except_umaban_ok and umaban_mismatch and comparison["veto_ok"]:
            base.update(
                reason="truth umaban differs while all other required payload matches",
                reason_code="UMABAN_MISMATCH",
            )
        else:
            base.update(
                reason="mandatory payload is missing/mismatched or veto mismatches",
                reason_code="PAYLOAD_MISMATCH",
            )
        results.append(base)

    # Stage 2a: deterministically retain one of each fully identical truth row.
    current_by_truth: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, result in enumerate(results):
        if result["classification"] == "KEEP_CURRENT":
            current_by_truth[_truth_key(result.get("truth"))].append(index)
    for indexes in current_by_truth.values():
        by_fingerprint: dict[str, list[int]] = defaultdict(list)
        for index in indexes:
            by_fingerprint[results[index]["source"]["fingerprint"]].append(index)
        for identical in by_fingerprint.values():
            ordered = sorted(identical, key=lambda i: results[i]["source"]["occurrence"])
            for index in ordered[1:]:
                results[index].update(
                    classification="DELETE_EXACT_DUPLICATE",
                    reason="identical complete duplicate",
                    reason_code=None, target=None,
                    keeper=results[ordered[0]]["source"],
                )

    # Stage 2b: a keeper is evidence only when exactly one complete current row
    # remains for that truth identity.  Truth-zero/multi and identity-invalid
    # rows can never enter the DELETE exception.
    keeper_by_truth: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, result in enumerate(results):
        if result["classification"] == "KEEP_CURRENT":
            keeper_by_truth[_truth_key(result.get("truth"))].append(index)
    for result in results:
        if result["classification"] not in {"MOVE_CANDIDATE", "UNVERIFIED_KEEP"}:
            continue
        if result["classification"] == "UNVERIFIED_KEEP" and result.get("reason_code") in {
                "TRUTH_ZERO", "TRUTH_MULTI"}:
            continue
        keepers = keeper_by_truth.get(_truth_key(result.get("truth")), [])
        if len(keepers) == 1 and _redundancy_is_proven(result):
            result.update(
                classification="DELETE_REDUNDANT_ERROR",
                reason="a unique complete keeper proves this erroneous row redundant",
                reason_code=None, target=None,
                keeper=results[keepers[0]]["source"],
            )
    return results


def _natural_key(row: Mapping[str, Any], race_override: int | None = None) -> tuple[Any, ...]:
    return (
        row.get("date"), row.get("place"),
        race_override if race_override is not None else row.get("race_num"),
        row.get("horse_number"),
    )


def build_operation_plan(
        rows: Sequence[Mapping[str, Any]],
        truth_rows: Sequence[Mapping[str, Any]],
        candidate_reasons: Mapping[int, Sequence[str]] | Sequence[Sequence[str]] | None = None,
        *, manual_fix_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attached = _attach_candidate_reasons(rows, candidate_reasons)
    prepared = _prepare_rows(attached)
    classifications = classify_rows(prepared, truth_rows)
    row_by_source = {
        (row["__fingerprint__"], row["__occurrence__"]): row for row in prepared
    }
    blocking_reasons: list[str] = []

    # Stage 3.  A verified chain/cycle is permitted.  Only actual final-state
    # collisions promote MOVE or non-mutation rows to UNRESOLVED.  Iterate
    # because cancelling one MOVE can expose a collision at its source slot.
    while True:
        occupants: dict[tuple[Any, ...], list[int]] = defaultdict(list)
        for index, result in enumerate(classifications):
            if result["classification"].startswith("DELETE_"):
                continue
            source_key = (result["source"]["fingerprint"], result["source"]["occurrence"])
            row = row_by_source[source_key]
            if result["classification"] == "MOVE_CANDIDATE":
                key = _natural_key(row, strict_int((result.get("target") or {}).get("race_num"), 1, 12))
            else:
                key = _natural_key(row)
            occupants[key].append(index)
        promoted = False
        for indexes in occupants.values():
            if len(indexes) < 2:
                continue
            for index in indexes:
                result = classifications[index]
                if result["classification"] in {
                    "MOVE_CANDIDATE", "NON_RUNNER_KEEP", "UNVERIFIED_KEEP",
                }:
                    result.update(
                        classification="UNRESOLVED",
                        reason="simulated final natural-key collision",
                        reason_code=None,
                    )
                    promoted = True
        if not promoted:
            final_collisions = [key for key, indexes in occupants.items() if len(indexes) > 1]
            break

    if final_collisions:
        blocking_reasons.append(
            f"simulated final natural-key collisions: {len(final_collisions)}"
        )
    if any(result["classification"] == "UNRESOLVED" for result in classifications):
        blocking_reasons.extend(
            result["reason"] for result in classifications
            if result["classification"] == "UNRESOLVED"
        )

    operations: list[dict[str, Any]] = []
    for result in classifications:
        common = {
            "source": result["source"],
            "source_natural_key": result["source_natural_key"],
            "truth": result.get("truth"),
            "candidate_reasons": result["candidate_reasons"],
            "reason_code": result.get("reason_code"),
        }
        if result["classification"] == "MOVE_CANDIDATE":
            operations.append({"action": "UPDATE_RACE_NUM", **common,
                               "target": result["target"]})
        elif result["classification"].startswith("DELETE_"):
            operations.append({"action": result["classification"], **common,
                               "keeper": result.get("keeper")})

    gate = dict(manual_fix_gate or {"ok": True, "rows": []})
    if not isinstance(gate.get("ok"), bool) or not isinstance(gate.get("rows"), list):
        raise SafetyError("manual fix gate has an invalid structure")
    if not gate["ok"]:
        blocking_reasons.append("manual fix gate is not satisfied")

    classification_counts = {
        name: sum(item["classification"] == name for item in classifications)
        for name in CLASSIFICATION_NAMES
    }
    unverified_reason_counts = {
        code: sum(
            item["classification"] == "UNVERIFIED_KEEP"
            and item.get("reason_code") == code
            for item in classifications
        ) for code in UNVERIFIED_REASON_CODES
    }
    candidate_reason_counts = {
        code: sum(code in item["candidate_reasons"] for item in classifications)
        for code in CANDIDATE_REASON_CODES
    }
    mutation_count = sum(
        item["classification"] == "MOVE_CANDIDATE"
        or item["classification"].startswith("DELETE_")
        for item in classifications
    )
    if blocking_reasons:
        status = "NOT_APPLICABLE"
    elif mutation_count:
        status = "APPLICABLE"
    else:
        status = "NO_MUTATIONS_NEEDED"

    logical_rows = Counter(logical_row_fingerprint(row) for row in attached)
    logical_rows_list = sorted(
        ({"fingerprint": key, "occurrences": value}
         for key, value in logical_rows.items()),
        key=lambda item: item["fingerprint"],
    )
    plan: dict[str, Any] = {
        "version": 3, "status": status,
        "logical_rows": logical_rows_list,
        "logical_rows_sha256": hashlib.sha256(
            canonical_json_bytes(logical_rows_list)
        ).hexdigest(),
        "classifications": classifications, "operations": operations,
        "classification_counts": classification_counts,
        "unverified_reason_counts": unverified_reason_counts,
        "candidate_reason_counts": candidate_reason_counts,
        "manual_fix_gate": gate,
        "counts": {
            "rows": len(rows),
            "keep": classification_counts["KEEP_CURRENT"],
            "update": sum(o["action"] == "UPDATE_RACE_NUM" for o in operations),
            "delete": sum(o["action"].startswith("DELETE_") for o in operations),
            "non_mutation": (
                classification_counts["NON_RUNNER_KEEP"]
                + classification_counts["UNVERIFIED_KEEP"]
            ),
            "unresolved": classification_counts["UNRESOLVED"],
            "final_row_delta": -sum(
                o["action"].startswith("DELETE_") for o in operations
            ),
        },
        "reasons": sorted(set(blocking_reasons)),
    }
    plan["plan_sha256"] = canonical_plan_sha256(plan)
    return plan


def mark_plan_not_applicable(plan: Mapping[str, Any], reason: str) -> dict[str, Any]:
    marked = dict(plan)
    marked["status"] = "NOT_APPLICABLE"
    marked["reasons"] = sorted(set(list(marked.get("reasons", [])) + [reason]))
    marked.pop("plan_sha256", None)
    marked["plan_sha256"] = canonical_plan_sha256(marked)
    return marked


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_canonical(value, omit_ctid=False), ensure_ascii=False,
                   sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]],
               columns: Sequence[str] | None = None) -> None:
    if columns is None:
        columns = sorted({str(key) for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            rendered = {}
            for key in columns:
                value = row.get(key)
                if isinstance(value, (Mapping, list, tuple)):
                    rendered[key] = canonical_json_bytes(value, omit_ctid=False).decode("utf-8")
                elif value is None:
                    rendered[key] = ""
                else:
                    converted = _json_value(value)
                    rendered[key] = (
                        canonical_json_bytes(converted, omit_ctid=False).decode("utf-8")
                        if isinstance(converted, Mapping) else converted
                    )
            writer.writerow(rendered)


def _comparison_counts(classifications: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for item in classifications:
        comparison = item.get("comparison") or {}
        for field, detail in (comparison.get("fields") or {}).items():
            result[field][str(detail.get("status", "unknown"))] += 1
    return {field: dict(sorted(counts.items())) for field, counts in sorted(result.items())}


def _verified_jsonl_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                raise SafetyError(f"truncated JSONL record: {path}")
            json.loads(line)
            count += 1
    return count


def _verified_csv_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SafetyError(f"CSV header is missing: {path}")
        return sum(1 for _ in reader)


_MANIFEST_KEYS = {
    "format_version", "mode", "created_at_utc", "database", "snapshot",
    "ability", "counts", "comparison_counts", "applicable", "plan_sha256",
    "plan_status", "classification_counts", "unverified_reason_counts",
    "candidate_reason_counts", "manual_fix_gate", "artifacts",
}
_ARTIFACT_NAMES = {
    "classification.csv", "candidate_rows.csv", "destination_rows.csv",
    "rows.jsonl", "plan.json",
}
_COUNT_KEYS = {
    "candidate_rows", "destination_rows", "rows", "keep", "update",
    "delete", "non_mutation", "unresolved", "final_row_delta",
}
_DATABASE_KEYS = {
    "database_name", "schema_name", "url_backend", "url_host", "url_port",
    "url_database",
}
_ABILITY_KEYS = {"path", "size", "mtime_ns", "sha256"}


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise SafetyError(f"{label} does not have the exact required schema")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SafetyError(f"{label} must be a non-empty string")
    return value


def _require_utc_timestamp(value: Any, label: str) -> None:
    raw = _require_nonempty_string(value, label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SafetyError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise SafetyError(f"{label} must include an explicit UTC offset")


def _validate_manual_gate_schema(value: Any) -> dict[str, Any]:
    gate = _require_exact_keys(value, {"ok", "rows"}, "manual_fix_gate")
    if not isinstance(gate["ok"], bool) or not isinstance(gate["rows"], list):
        raise SafetyError("manual_fix_gate has invalid field types")
    if len(gate["rows"]) != len(MANUAL_FIX_EXPECTED_ROWS):
        raise SafetyError("manual_fix_gate must contain all six expected rows")
    for index, row in enumerate(gate["rows"]):
        row = _require_exact_keys(row, {
            "expected", "observed_count", "matching_horse_count",
            "observed_fingerprints", "ok",
        }, f"manual_fix_gate.rows[{index}]")
        expected = _require_exact_keys(row["expected"], {
            "date", "place", "race_num", "horse_number", "horse",
        }, f"manual_fix_gate.rows[{index}].expected")
        if expected != dict(MANUAL_FIX_EXPECTED_ROWS[index]):
            raise SafetyError("manual_fix_gate expected row set is invalid")
        for key in ("observed_count", "matching_horse_count"):
            if not _is_plain_int(row[key]) or row[key] < 0:
                raise SafetyError(f"manual_fix_gate {key} is invalid")
        if not isinstance(row["ok"], bool) or not isinstance(row["observed_fingerprints"], list):
            raise SafetyError("manual_fix_gate row has invalid field types")
        for fingerprint in row["observed_fingerprints"]:
            _validated_sha256(fingerprint, "manual_fix_gate observed fingerprint")
        calculated_ok = row["matching_horse_count"] == 1
        if row["ok"] != calculated_ok:
            raise SafetyError("manual_fix_gate row ok flag is inconsistent")
    if gate["ok"] != all(row["ok"] for row in gate["rows"]):
        raise SafetyError("manual_fix_gate ok flag is inconsistent")
    return gate


def _validate_manifest_schema(manifest: Any) -> dict[str, Any]:
    """Reject unknown, missing, or mistyped approval-critical metadata."""
    manifest = _require_exact_keys(manifest, _MANIFEST_KEYS, "manifest")
    if not _is_plain_int(manifest["format_version"]) or manifest["format_version"] != 3:
        raise SafetyError("unsupported manifest format_version")
    mode = manifest["mode"]
    if mode not in {"dry-run", "apply-backup", "apply-noop"}:
        raise SafetyError("unsupported manifest mode")
    _require_utc_timestamp(manifest["created_at_utc"], "manifest created_at_utc")
    if not isinstance(manifest["applicable"], bool):
        raise SafetyError("manifest applicable must be boolean")
    if manifest["plan_status"] not in PLAN_STATUSES:
        raise SafetyError("manifest plan_status is invalid")
    if manifest["applicable"] != (manifest["plan_status"] == "APPLICABLE"):
        raise SafetyError("manifest applicable flag disagrees with plan_status")
    _validated_sha256(manifest["plan_sha256"], "manifest plan_sha256")

    database = _require_exact_keys(manifest["database"], _DATABASE_KEYS, "manifest database")
    for key in _DATABASE_KEYS - {"url_port"}:
        _require_nonempty_string(database[key], f"manifest database.{key}")
    if database["url_backend"] != "postgresql":
        raise SafetyError("manifest database.url_backend must be postgresql")
    if (not _is_plain_int(database["url_port"])
            or not 1 <= database["url_port"] <= 65535):
        raise SafetyError("manifest database.url_port is invalid")

    ability = _require_exact_keys(manifest["ability"], _ABILITY_KEYS, "manifest ability")
    _require_nonempty_string(ability["path"], "manifest ability.path")
    _validated_sha256(ability["sha256"], "manifest ability.sha256")
    for key in ("size", "mtime_ns"):
        if not _is_plain_int(ability[key]) or ability[key] < 0:
            raise SafetyError(f"manifest ability.{key} must be a non-negative integer")

    counts = _require_exact_keys(manifest["counts"], _COUNT_KEYS, "manifest counts")
    for key in _COUNT_KEYS - {"final_row_delta"}:
        if not _is_plain_int(counts[key]) or counts[key] < 0:
            raise SafetyError(f"manifest counts.{key} must be a non-negative integer")
    if not _is_plain_int(counts["final_row_delta"]):
        raise SafetyError("manifest counts.final_row_delta must be an integer")
    if counts["candidate_rows"] + counts["destination_rows"] != counts["rows"]:
        raise SafetyError("manifest row counts are internally inconsistent")
    if (counts["keep"] + counts["update"] + counts["delete"]
            + counts["non_mutation"] + counts["unresolved"] != counts["rows"]):
        raise SafetyError("manifest classification counts are internally inconsistent")
    if counts["final_row_delta"] != -counts["delete"]:
        raise SafetyError("manifest final_row_delta is internally inconsistent")

    classification_counts = _require_exact_keys(
        manifest["classification_counts"], set(CLASSIFICATION_NAMES),
        "manifest classification_counts",
    )
    unverified_reason_counts = _require_exact_keys(
        manifest["unverified_reason_counts"], set(UNVERIFIED_REASON_CODES),
        "manifest unverified_reason_counts",
    )
    candidate_reason_counts = _require_exact_keys(
        manifest["candidate_reason_counts"], set(CANDIDATE_REASON_CODES),
        "manifest candidate_reason_counts",
    )
    for label, values in (
        ("classification_counts", classification_counts),
        ("unverified_reason_counts", unverified_reason_counts),
        ("candidate_reason_counts", candidate_reason_counts),
    ):
        for count in values.values():
            if not _is_plain_int(count) or count < 0:
                raise SafetyError(f"manifest {label} values must be non-negative integers")
    if sum(classification_counts.values()) != counts["rows"]:
        raise SafetyError("classification_counts do not cover every row")
    if classification_counts["KEEP_CURRENT"] != counts["keep"]:
        raise SafetyError("classification_counts KEEP_CURRENT disagrees with counts")
    if classification_counts["MOVE_CANDIDATE"] != counts["update"]:
        raise SafetyError("classification_counts MOVE_CANDIDATE disagrees with counts")
    if (classification_counts["DELETE_EXACT_DUPLICATE"]
            + classification_counts["DELETE_REDUNDANT_ERROR"] != counts["delete"]):
        raise SafetyError("classification_counts DELETE disagrees with counts")
    if (classification_counts["NON_RUNNER_KEEP"]
            + classification_counts["UNVERIFIED_KEEP"] != counts["non_mutation"]):
        raise SafetyError("classification_counts non-mutation disagrees with counts")
    if classification_counts["UNRESOLVED"] != counts["unresolved"]:
        raise SafetyError("classification_counts UNRESOLVED disagrees with counts")
    if sum(unverified_reason_counts.values()) != classification_counts["UNVERIFIED_KEEP"]:
        raise SafetyError("unverified reason counts are internally inconsistent")
    if any(count > counts["candidate_rows"] for count in candidate_reason_counts.values()):
        raise SafetyError("candidate reason count exceeds candidate row count")
    mutations = counts["update"] + counts["delete"]
    if manifest["plan_status"] == "APPLICABLE" and (
            mutations < 1 or counts["unresolved"] != 0):
        raise SafetyError("APPLICABLE status is inconsistent with mutation counts")
    if manifest["plan_status"] == "NO_MUTATIONS_NEEDED" and (
            mutations != 0 or counts["unresolved"] != 0):
        raise SafetyError("NO_MUTATIONS_NEEDED status is inconsistent with counts")
    _validate_manual_gate_schema(manifest["manual_fix_gate"])

    comparison_counts = manifest["comparison_counts"]
    if not isinstance(comparison_counts, dict):
        raise SafetyError("manifest comparison_counts must be an object")
    for field, statuses in comparison_counts.items():
        _require_nonempty_string(field, "manifest comparison_counts field")
        if not isinstance(statuses, dict):
            raise SafetyError("manifest comparison_counts status set must be an object")
        for status, count in statuses.items():
            _require_nonempty_string(status, "manifest comparison_counts status")
            if not _is_plain_int(count) or count < 0:
                raise SafetyError("manifest comparison_counts value must be a non-negative integer")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != _ARTIFACT_NAMES:
        raise SafetyError("manifest does not contain the exact required artifact set")
    for name, metadata in artifacts.items():
        metadata = _require_exact_keys(metadata, {"size", "sha256"}, f"manifest artifact {name}")
        if not _is_plain_int(metadata["size"]) or metadata["size"] < 0:
            raise SafetyError(f"manifest artifact {name} size is invalid")
        _validated_sha256(metadata["sha256"], f"manifest artifact {name} sha256")

    snapshot = manifest["snapshot"]
    if mode == "dry-run":
        snapshot = _require_exact_keys(snapshot, {
            "transaction_time", "transaction_snapshot", "scoped_duplicate_groups",
            "candidate_2025_duplicate_groups",
        }, "dry-run snapshot")
        _require_nonempty_string(snapshot["transaction_time"], "dry-run transaction_time")
        _require_nonempty_string(snapshot["transaction_snapshot"], "dry-run transaction_snapshot")
        for key in ("scoped_duplicate_groups", "candidate_2025_duplicate_groups"):
            if not _is_plain_int(snapshot[key]) or snapshot[key] < 0:
                raise SafetyError(f"dry-run snapshot {key} must be a non-negative integer")
    elif mode == "apply-backup":
        snapshot = _require_exact_keys(snapshot, {
            "locked_at_utc", "approved_manifest_sha256", "approved_plan_sha256",
        }, "apply-backup snapshot")
        _require_utc_timestamp(snapshot["locked_at_utc"], "apply-backup locked_at_utc")
        _validated_sha256(snapshot["approved_manifest_sha256"], "approved manifest sha256")
        _validated_sha256(snapshot["approved_plan_sha256"], "approved plan sha256")
    else:
        snapshot = _require_exact_keys(snapshot, {
            "locked_at_utc", "result", "reason", "approved_manifest_sha256",
            "approved_plan_sha256",
        }, "apply-noop snapshot")
        _require_utc_timestamp(snapshot["locked_at_utc"], "apply-noop locked_at_utc")
        if snapshot["result"] != "no_mutations_needed":
            raise SafetyError("apply-noop result is invalid")
        _require_nonempty_string(snapshot["reason"], "apply-noop reason")
        _validated_sha256(snapshot["approved_manifest_sha256"], "approved manifest sha256")
        _validated_sha256(snapshot["approved_plan_sha256"], "approved plan sha256")
    return manifest


def write_bundle(output_dir: str | os.PathLike[str], *,
                 candidate_rows: Sequence[Mapping[str, Any]],
                 destination_rows: Sequence[Mapping[str, Any]],
                 plan: Mapping[str, Any], ability: Mapping[str, Any],
                 database: Mapping[str, Any], snapshot: Mapping[str, Any],
                 mode: str = "dry-run") -> dict[str, Any]:
    """Write, re-read, and hash a complete audit bundle."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    classifications = list(plan.get("classifications", []))
    if plan.get("version") != 3 or plan.get("status") not in PLAN_STATUSES:
        raise SafetyError("only a format v3 plan can be written")

    def raw_database_row(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(key): value for key, value in row.items()
            if str(key) == ROW_ID or not str(key).startswith("__")
        }

    candidate = [raw_database_row(row) for row in candidate_rows]
    destination = [raw_database_row(row) for row in destination_rows]
    all_rows = candidate + destination

    csv_classifications = []
    for item in classifications:
        rendered = dict(item)
        rendered["candidate_reasons"] = "|".join(
            _validated_candidate_reasons(item.get("candidate_reasons", []))
        )
        rendered["reason_code"] = item.get("reason_code")
        csv_classifications.append(rendered)
    _write_csv(root / "classification.csv", csv_classifications,
               ("classification", "reason", "candidate_reasons", "reason_code",
                "source", "source_natural_key", "truth", "truth_candidates",
                "target", "keeper", "comparison", "raw_ctid"))
    all_columns = [ROW_ID] + sorted({key for row in all_rows for key in row if key != ROW_ID})
    _write_csv(root / "candidate_rows.csv", candidate, all_columns)
    _write_csv(root / "destination_rows.csv", destination, all_columns)
    with (root / "rows.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for kind, rows_for_kind in (("candidate", candidate), ("destination", destination)):
            for row in rows_for_kind:
                record = {"kind": kind, "row": _canonical(row, omit_ctid=False)}
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False) + "\n")
    plan_copy = dict(plan)
    expected_plan_hash = canonical_plan_sha256(plan_copy)
    if plan_copy.get("plan_sha256") != expected_plan_hash:
        raise SafetyError("plan_sha256 is not canonical")
    _write_json(root / "plan.json", plan_copy)

    if _verified_jsonl_count(root / "rows.jsonl") != len(all_rows):
        raise SafetyError("rows.jsonl count mismatch")
    expected_csv_counts = {
        "classification.csv": len(classifications),
        "candidate_rows.csv": len(candidate),
        "destination_rows.csv": len(destination),
    }
    for name, expected_count in expected_csv_counts.items():
        if _verified_csv_count(root / name) != expected_count:
            raise SafetyError(f"CSV {name} count mismatch")
    reloaded_plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    if canonical_plan_sha256(reloaded_plan) != expected_plan_hash:
        raise SafetyError("plan.json failed canonical hash verification")
    artifact_names = tuple(sorted(_ARTIFACT_NAMES))
    artifacts = {
        name: {"size": (root / name).stat().st_size, "sha256": file_sha256(root / name)}
        for name in artifact_names
    }
    manifest: dict[str, Any] = {
        "format_version": 3, "mode": mode,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": dict(database), "snapshot": dict(snapshot), "ability": dict(ability),
        "counts": {
            "candidate_rows": len(candidate), "destination_rows": len(destination),
            **dict(plan.get("counts", {})),
        },
        "comparison_counts": _comparison_counts(classifications),
        "plan_status": plan.get("status"),
        "applicable": plan.get("status") == "APPLICABLE",
        "classification_counts": dict(plan.get("classification_counts", {})),
        "unverified_reason_counts": dict(plan.get("unverified_reason_counts", {})),
        "candidate_reason_counts": dict(plan.get("candidate_reason_counts", {})),
        "manual_fix_gate": dict(plan.get("manual_fix_gate", {})),
        "plan_sha256": expected_plan_hash, "artifacts": artifacts,
    }
    _write_json(root / "manifest.json", manifest)
    # Validate the exact bytes that the operator will approve.  The hash cannot
    # be embedded in the manifest itself, so it is returned as runtime metadata.
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    persisted_manifest = _validate_manifest_schema(
        _load_strict_json(manifest_bytes, "manifest.json")
    )
    result = dict(persisted_manifest)
    result["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    return result


def validate_bundle(
        manifest_path: str | os.PathLike[str], *,
        approved_manifest_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(manifest_path).resolve(strict=True)
    manifest_bytes = path.read_bytes()
    if approved_manifest_sha256 is not None:
        expected_manifest_sha256 = _validated_sha256(
            approved_manifest_sha256, "approved manifest sha256",
        )
        actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_manifest_sha256 != expected_manifest_sha256:
            raise SafetyError("explicit approved manifest hash does not match manifest file bytes")
    manifest = _validate_manifest_schema(_load_strict_json(manifest_bytes, "manifest.json"))
    root = path.parent.resolve()
    artifacts = manifest.get("artifacts")
    for name, expected in artifacts.items():
        artifact = (root / name).resolve(strict=True)
        if artifact.parent != root:
            raise SafetyError("manifest artifact escapes its bundle directory")
        if artifact.stat().st_size != expected.get("size") or file_sha256(artifact) != expected.get("sha256"):
            raise SafetyError(f"bundle artifact verification failed: {name}")
    plan_path = (root / "plan.json").resolve(strict=True)
    if plan_path.parent != root:
        raise SafetyError("plan.json escapes its bundle directory")
    plan = _load_strict_json(plan_path.read_bytes(), "plan.json")
    if not isinstance(plan, dict):
        raise SafetyError("plan.json root must be an object")
    if plan.get("version") != 3:
        raise SafetyError("plan.json version must be 3")
    if plan.get("status") not in PLAN_STATUSES:
        raise SafetyError("plan.json status is invalid")
    actual_hash = canonical_plan_sha256(plan)
    if actual_hash != plan.get("plan_sha256") or actual_hash != manifest.get("plan_sha256"):
        raise SafetyError("approved plan hash verification failed")
    if manifest.get("plan_status") != plan.get("status"):
        raise SafetyError("manifest plan_status disagrees with plan")
    if bool(manifest.get("applicable")) != (plan.get("status") == "APPLICABLE"):
        raise SafetyError("manifest applicable flag disagrees with plan status")
    for key in (
        "classification_counts", "unverified_reason_counts",
        "candidate_reason_counts", "manual_fix_gate",
    ):
        if manifest.get(key) != plan.get(key):
            raise SafetyError(f"manifest {key} disagrees with plan")
    for item in plan.get("classifications", []):
        classification = item.get("classification")
        if classification not in CLASSIFICATION_NAMES:
            raise SafetyError("plan has an unknown classification")
        reasons = _validated_candidate_reasons(item.get("candidate_reasons"))
        if reasons != item.get("candidate_reasons"):
            raise SafetyError("plan candidate reasons are not canonical")
        reason_code = item.get("reason_code")
        if classification == "UNVERIFIED_KEEP":
            if reason_code not in UNVERIFIED_REASON_CODES:
                raise SafetyError("UNVERIFIED_KEEP requires an enumerated reason_code")
        elif reason_code is not None:
            raise SafetyError("reason_code must be null outside UNVERIFIED_KEEP")
    for operation in plan.get("operations", []):
        _validated_candidate_reasons(operation.get("candidate_reasons"))
        if operation.get("reason_code") is not None:
            raise SafetyError("mutation operation reason_code must be null")
    manifest_counts = manifest.get("counts") or {}
    for key, value in (plan.get("counts") or {}).items():
        if manifest_counts.get(key) != value:
            raise SafetyError(f"manifest count disagrees with plan: {key}")
    candidate_count = _verified_csv_count(root / "candidate_rows.csv")
    destination_count = _verified_csv_count(root / "destination_rows.csv")
    classification_count = _verified_csv_count(root / "classification.csv")
    if candidate_count != manifest_counts.get("candidate_rows"):
        raise SafetyError("manifest candidate row count mismatch")
    if destination_count != manifest_counts.get("destination_rows"):
        raise SafetyError("manifest destination row count mismatch")
    if classification_count != (plan.get("counts") or {}).get("rows"):
        raise SafetyError("classification row count mismatch")
    if _verified_jsonl_count(root / "rows.jsonl") != candidate_count + destination_count:
        raise SafetyError("backup JSONL row count mismatch")
    return manifest, plan


def _database_identity(connection: Any) -> dict[str, Any]:
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT current_database() AS database_name, current_schema() AS schema_name"
    )).mappings().one()
    url = connection.engine.url
    host = (url.host or "").casefold()
    first, separator, remainder = host.partition(".")
    if first.endswith("-pooler"):
        first = first[:-len("-pooler")]
    stable_host = first + (separator + remainder if separator else "")
    return {
        **dict(row), "url_backend": url.get_backend_name(), "url_host": stable_host,
        "url_port": url.port or 5432, "url_database": (url.database or "").strip("/"),
    }


def _fetch_race_columns(connection: Any) -> list[str]:
    from sqlalchemy import text

    rows = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='races' ORDER BY ordinal_position"
    )).scalars().all()
    if not rows or not all(column in rows for column in (*NATURAL_KEY, "馬名")):
        raise SafetyError("public.races is missing required T34b columns")
    return list(rows)


def _fetch_2025_complete_rows(connection: Any) -> list[dict[str, Any]]:
    """Read the generalized-candidate scan in the caller's single snapshot."""
    from sqlalchemy import text

    result = connection.execute(text(f"""
        SELECT r.ctid::text AS "{ROW_ID}", r.*
        FROM public.races AS r
        WHERE {PREDICATE_SQL}
          AND r.date BETWEEN '250101' AND '251231'
        ORDER BY r.date, r.place, r.race_num, r.horse_number, r.ctid
        LIMIT :row_limit
    """), {"row_limit": CLOSURE_ROW_LIMIT + 1})
    rows = [dict(row) for row in result.mappings().all()]
    if len(rows) > CLOSURE_ROW_LIMIT:
        raise SafetyError(
            f"2025 complete-key snapshot exceeds the fail-closed limit ({CLOSURE_ROW_LIMIT})"
        )
    return rows


def _select_generalized_candidates(
        scoped_rows: Sequence[Mapping[str, Any]],
        truth_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select §6.1 candidates (a)/(b)/(c) from one in-memory snapshot."""
    truth_slots = build_truth_slot_index(truth_rows)
    raw_key_counts = Counter(
        tuple(row.get(column) for column in NATURAL_KEY) for row in scoped_rows
    )
    candidates: list[dict[str, Any]] = []
    for original in scoped_rows:
        row = dict(original)
        raw_key = tuple(row.get(column) for column in NATURAL_KEY)
        reasons: list[str] = []
        if raw_key_counts[raw_key] > 1:
            reasons.append("a")
        slot = (
            normalize_neon_date(row.get("date")), normalize_text(row.get("place")),
            strict_int(row.get("race_num"), 1, 12),
            strict_int(row.get("horse_number"), 1, 18),
        )
        truth = truth_slots.get(slot) if all(value is not None for value in slot) else None
        if truth is None:
            reasons.append("c")
        elif normalize_text(row.get("馬名", row.get("horse"))) != normalize_text(truth.get("horse")):
            reasons.append("b")
        if reasons:
            row["__candidate_reasons__"] = _validated_candidate_reasons(reasons)
            candidates.append(row)
    return candidates


def _expected_truth_targets(rows: Sequence[Mapping[str, Any]],
                            truth_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]]) -> set[tuple[str, str, int, int]]:
    targets: set[tuple[str, str, int, int]] = set()
    for row in rows:
        day8 = normalize_neon_date(row.get("date"))
        place = normalize_text(row.get("place"))
        horse = normalize_text(row.get("馬名", row.get("horse")))
        candidates = truth_index.get((day8 or "", place or "", horse or ""), [])
        if len(candidates) != 1:
            continue
        truth = candidates[0]
        race = strict_int(truth.get("r"), 1, 12)
        number = strict_int(truth.get("umaban"), 1, 18)
        raw_place = row.get("place")
        if day8 and raw_place is not None and race is not None and number is not None:
            targets.add((day8[2:], str(raw_place), race, number))
    return targets


def _fetch_target_rows(connection: Any, targets: Sequence[tuple[str, str, int, int]]) -> list[dict[str, Any]]:
    if not targets:
        return []
    from sqlalchemy import text
    fetched: list[dict[str, Any]] = []
    # Stay below PostgreSQL's bind-parameter ceiling even if the safety bound
    # is approached by a synthetic or non-production fixture.
    for start in range(0, len(targets), 5_000):
        batch = targets[start:start + 5_000]
        values, params = [], {}
        for index, (day, place, race, number) in enumerate(batch):
            values.append(f"(:d{index}, :p{index}, :r{index}, :h{index})")
            params.update({f"d{index}": day, f"p{index}": place,
                           f"r{index}": race, f"h{index}": number})
        result = connection.execute(text(f"""
            WITH wanted(date, place, race_num, horse_number) AS
                 (VALUES {', '.join(values)})
            SELECT r.ctid::text AS "{ROW_ID}", r.*
            FROM public.races AS r
            JOIN wanted AS w
              ON r.date = w.date AND r.place = w.place
             AND r.race_num = w.race_num AND r.horse_number = w.horse_number
            ORDER BY r.date, r.place, r.race_num, r.horse_number, r.ctid
        """), params)
        fetched.extend(dict(row) for row in result.mappings().all())
    return fetched


def _fetch_manual_fix_gate_rows(connection: Any) -> list[dict[str, Any]]:
    targets = [
        (row["date"], row["place"], row["race_num"], row["horse_number"])
        for row in MANUAL_FIX_EXPECTED_ROWS
    ]
    return _fetch_target_rows(connection, targets)


def fetch_target_closure(connection: Any, truth_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scoped_rows = _fetch_2025_complete_rows(connection)
    candidates = _select_generalized_candidates(scoped_rows, truth_rows)
    if not candidates:
        return [], []
    truth_index = build_truth_index(truth_rows)
    all_rows = list(candidates)
    seen_ctids = {str(row[ROW_ID]) for row in all_rows}
    queried: set[tuple[str, str, int, int]] = set()
    while True:
        wanted = _expected_truth_targets(all_rows, truth_index) - queried
        if not wanted:
            break
        queried.update(wanted)
        fetched = _fetch_target_rows(connection, sorted(wanted))
        new_rows = [row for row in fetched if str(row[ROW_ID]) not in seen_ctids]
        if not new_rows:
            continue
        all_rows.extend(new_rows)
        if len(all_rows) > CLOSURE_ROW_LIMIT:
            raise SafetyError(
                f"candidate closure exceeds the fail-closed limit ({CLOSURE_ROW_LIMIT})"
            )
        seen_ctids.update(str(row[ROW_ID]) for row in new_rows)
    candidate_ctids = {str(row[ROW_ID]) for row in candidates}
    destinations = []
    for original in all_rows:
        if str(original[ROW_ID]) in candidate_ctids:
            continue
        row = dict(original)
        row["__candidate_reasons__"] = []
        destinations.append(row)
    return candidates, destinations


def _count_table_rows(connection: Any) -> int:
    from sqlalchemy import text
    return int(connection.execute(text("SELECT COUNT(*) FROM public.races")).scalar_one())


def _count_scoped_duplicate_groups(connection: Any) -> int:
    from sqlalchemy import text
    return int(connection.execute(text(f"""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM public.races WHERE {PREDICATE_SQL}
            GROUP BY date, place, race_num, horse_number HAVING COUNT(*) > 1
        ) AS duplicate_keys
    """)).scalar_one())


def _assert_no_covering_natural_key_unique(connection: Any) -> None:
    """Fail before mutation if an existing unique object can constrain swaps."""
    from sqlalchemy import text

    rows = connection.execute(text("""
        SELECT idx.relname AS index_name, i.indisunique,
               array_agg(att.attname ORDER BY key.ordinality)
                   FILTER (WHERE key.ordinality <= i.indnkeyatts) AS key_columns
          FROM pg_catalog.pg_index AS i
          JOIN pg_catalog.pg_class AS tbl ON tbl.oid = i.indrelid
          JOIN pg_catalog.pg_namespace AS ns ON ns.oid = tbl.relnamespace
          JOIN pg_catalog.pg_class AS idx ON idx.oid = i.indexrelid
          LEFT JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY
               AS key(attnum, ordinality) ON TRUE
          LEFT JOIN pg_catalog.pg_attribute AS att
                 ON att.attrelid = tbl.oid AND att.attnum = key.attnum
         WHERE ns.nspname = 'public' AND tbl.relname = 'races'
         GROUP BY idx.relname, i.indisunique, i.indnkeyatts
    """)).mappings().all()
    required = set(NATURAL_KEY)
    for row in rows:
        name = str(row.get("index_name") or "")
        columns = {str(column) for column in (row.get("key_columns") or []) if column}
        if name == "uq_races_natural_key" or (
                bool(row.get("indisunique")) and required.issubset(columns)):
            raise SafetyError(
                "a races natural-key unique index/constraint already exists; "
                "cleanup mutation must run before migration"
            )


def _verify_truth_poststate(connection: Any, plan: Mapping[str, Any],
                            truth_rows: Sequence[Mapping[str, Any]]) -> None:
    truth_lookup: dict[tuple[str, str, int, int, str], Mapping[str, Any]] = {}
    for truth in truth_rows:
        identity = _truth_identity(truth)
        key = (identity["date"], identity["place"], identity["r"],
               identity["umaban"], identity["horse"])
        truth_lookup[key] = truth
    classifications = list(plan.get("classifications", []))
    by_source = {
        (item["source"]["fingerprint"], item["source"]["occurrence"]): item
        for item in classifications
    }
    expected: dict[tuple[Any, Any, Any, Any], Mapping[str, Any]] = {}
    expected_truth_keys: dict[tuple[Any, Any, Any, Any], tuple[Any, ...]] = {}
    for item in classifications:
        classification = item.get("classification")
        if classification not in {
            "KEEP_CURRENT", "MOVE_CANDIDATE", "DELETE_EXACT_DUPLICATE",
            "DELETE_REDUNDANT_ERROR",
        }:
            # NON_RUNNER/UNVERIFIED are intentionally not truth-checked.  A
            # DELETE target may itself be wrong; only its referenced keeper is.
            continue
        if classification.startswith("DELETE_") and not item.get("keeper"):
            raise SafetyError("postcheck DELETE classification has no keeper")
        retained = item
        if classification.startswith("DELETE_"):
            keeper = item["keeper"]
            retained = by_source.get((keeper.get("fingerprint"), keeper.get("occurrence")))
            if not retained or retained.get("classification") != "KEEP_CURRENT":
                raise SafetyError("postcheck DELETE keeper is not the unique KEEP_CURRENT row")
        identity = item.get("truth")
        if not identity:
            raise SafetyError("postcheck verified classification has no truth identity")
        key = (identity.get("date"), identity.get("place"), identity.get("r"),
               identity.get("umaban"), identity.get("horse"))
        if key not in truth_lookup:
            raise SafetyError("postcheck truth identity disappeared")
        if retained.get("classification") == "MOVE_CANDIDATE":
            raw_target = retained.get("target") or {}
        else:
            raw_target = retained.get("source_natural_key") or {}
        target = tuple(raw_target.get(column) for column in NATURAL_KEY)
        if any(value is None for value in target):
            raise SafetyError("postcheck retained row has an incomplete raw natural key")
        if target in expected_truth_keys and expected_truth_keys[target] != key:
            raise SafetyError("postcheck target maps to multiple truth identities")
        expected_truth_keys[target] = key
        expected[target] = truth_lookup[key]
    targets = sorted(expected)
    actual_rows = _fetch_target_rows(connection, targets)
    grouped: dict[tuple[Any, Any, Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for row in actual_rows:
        grouped[tuple(row.get(column) for column in NATURAL_KEY)].append(row)
    for target, truth in expected.items():
        matching = grouped.get(target, [])
        if len(matching) != 1:
            raise SafetyError("postcheck did not find exactly one retained truth row")
        comparison = compare_payload(matching[0], truth)
        if not comparison["mandatory_ok"] or not comparison["veto_ok"]:
            raise SafetyError("postcheck retained row no longer matches truth")


def _verify_nonmutation_poststate(connection: Any, plan: Mapping[str, Any]) -> None:
    """Prove non-mutation rows kept their key, fingerprint, and multiplicity."""
    expected: dict[tuple[str, str, int, int], Counter[str]] = defaultdict(Counter)
    for item in plan.get("classifications", []):
        if item.get("classification") not in {"NON_RUNNER_KEEP", "UNVERIFIED_KEEP"}:
            continue
        raw = item.get("source_natural_key") or {}
        day = normalize_text(raw.get("date"))
        place = raw.get("place")
        race = raw.get("race_num")
        number = raw.get("horse_number")
        if day is None or place is None or race is None or number is None:
            raise SafetyError("non-mutation plan row has an invalid natural key")
        expected[(day, str(place), race, number)][item["source"]["fingerprint"]] += 1
    if not expected:
        return
    actual_rows = _fetch_target_rows(connection, sorted(expected))
    actual: dict[tuple[str, str, int, int], Counter[str]] = defaultdict(Counter)
    for row in actual_rows:
        key = (
            normalize_text(row.get("date")) or "", str(row.get("place")),
            row.get("race_num"), row.get("horse_number"),
        )
        actual[key][logical_row_fingerprint(row)] += 1
    if actual != expected:
        raise SafetyError(
            "non-mutation row key/fingerprint/occurrence changed during apply"
        )


def _timestamped_dir(base: str | os.PathLike[str], label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path(base) / f"{stamp}_{label}"


def run_dry_run(engine: Any, ability_path: str | os.PathLike[str],
                output_base: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
    from sqlalchemy import text

    ability_before = ability_identity(ability_path)
    truth_rows = load_ability_truth(ability_path)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            database = _database_identity(connection)
            _fetch_race_columns(connection)
            snapshot_row = connection.execute(text(
                "SELECT transaction_timestamp()::text AS transaction_time, "
                "txid_current_snapshot()::text AS transaction_snapshot"
            )).mappings().one()
            candidates, destinations = fetch_target_closure(connection, truth_rows)
            manual_fix_gate = evaluate_manual_fix_gate(
                _fetch_manual_fix_gate_rows(connection)
            )
            plan = build_operation_plan(
                candidates + destinations, truth_rows,
                manual_fix_gate=manual_fix_gate,
            )
            snapshot = dict(snapshot_row)
            global_duplicate_groups = _count_scoped_duplicate_groups(connection)
            candidate_groups = {
                _natural_key(row) for row in candidates
                if "a" in row.get("__candidate_reasons__", [])
            }
            snapshot["scoped_duplicate_groups"] = global_duplicate_groups
            snapshot["candidate_2025_duplicate_groups"] = len(candidate_groups)
            if global_duplicate_groups != len(candidate_groups):
                plan = mark_plan_not_applicable(
                    plan, "duplicate groups outside the 2025 cleanup scope exist",
                )
            bundle = _timestamped_dir(output_base, "dry_run")
            manifest = write_bundle(
                bundle, candidate_rows=candidates, destination_rows=destinations,
                plan=plan, ability=ability_before, database=database,
                snapshot=snapshot, mode="dry-run",
            )
            transaction.commit()
        except BaseException:
            transaction.rollback()
            raise
    if ability_identity(ability_path) != ability_before:
        raise SafetyError("ability.db changed during dry-run")
    return bundle, manifest


def _row_for_source(rows: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    prepared = _prepare_rows(rows)
    matches = [row for row in prepared
               if row["__fingerprint__"] == source.get("fingerprint")
               and row["__occurrence__"] == source.get("occurrence")]
    if len(matches) != 1:
        raise SafetyError("logical source did not resolve to exactly one fresh row")
    return matches[0]


def _guard_sql(row: Mapping[str, Any], columns: Sequence[str]) -> tuple[str, dict[str, Any]]:
    guards, params = ["ctid = CAST(:guard_ctid AS tid)"], {"guard_ctid": row.get(ROW_ID)}
    for index, column in enumerate(columns):
        quoted = '"' + column.replace('"', '""') + '"'
        name = f"guard_{index}"
        guards.append(f"{quoted} IS NOT DISTINCT FROM :{name}")
        params[name] = row.get(column)
    return " AND ".join(guards), params


def _apply_operations(connection: Any, rows: Sequence[Mapping[str, Any]],
                      plan: Mapping[str, Any], columns: Sequence[str]) -> tuple[int, int]:
    from sqlalchemy import text

    updates = deletes = 0
    ordered = sorted(plan.get("operations", []), key=lambda op: 0 if str(op["action"]).startswith("DELETE_") else 1)
    for operation in ordered:
        source_row = _row_for_source(rows, operation["source"])
        guard, params = _guard_sql(source_row, columns)
        if str(operation["action"]).startswith("DELETE_"):
            sql = f'DELETE FROM public.races WHERE {guard} RETURNING ctid::text AS "{ROW_ID}", *'
            returned = [dict(row) for row in connection.execute(text(sql), params).mappings().all()]
            if len(returned) != 1 or logical_row_fingerprint(returned[0]) != operation["source"]["fingerprint"]:
                raise SafetyError("DELETE guard/RETURNING mismatch")
            deletes += 1
        else:
            new_race = strict_int((operation.get("target") or {}).get("race_num"), 1, 12)
            if new_race is None:
                raise SafetyError("UPDATE operation has an invalid target race_num")
            params["new_race_num"] = new_race
            sql = f'UPDATE public.races SET race_num=:new_race_num WHERE {guard} RETURNING ctid::text AS "{ROW_ID}", *'
            returned = [dict(row) for row in connection.execute(text(sql), params).mappings().all()]
            expected_row = dict(source_row)
            expected_row["race_num"] = new_race
            if (len(returned) != 1
                    or strict_int(returned[0].get("race_num"), 1, 12) != new_race
                    or logical_row_fingerprint(returned[0]) != logical_row_fingerprint(expected_row)):
                raise SafetyError("UPDATE guard/RETURNING mismatch")
            updates += 1
    return updates, deletes


def run_apply(engine: Any, ability_path: str | os.PathLike[str],
              output_base: str | os.PathLike[str], manifest_path: str | os.PathLike[str],
              approved_manifest_sha256: str, approved_plan_sha256: str, *,
              writer_stopped: bool = False,
              lock_timeout_ms: int = 5000) -> tuple[Path, dict[str, Any]]:
    """Apply an approved plan atomically; every discrepancy rolls back."""
    if not isinstance(lock_timeout_ms, int) or isinstance(lock_timeout_ms, bool) or lock_timeout_ms < 1:
        raise SafetyError("lock_timeout_ms must be a positive integer")
    if not writer_stopped:
        raise SafetyError("--writer-stopped acknowledgement is required")
    approved_manifest_sha256 = _validated_sha256(
        approved_manifest_sha256, "approved manifest sha256",
    )
    approved_plan_sha256 = _validated_sha256(
        approved_plan_sha256, "approved plan sha256",
    )
    approved_manifest, approved_plan = validate_bundle(
        manifest_path, approved_manifest_sha256=approved_manifest_sha256,
    )
    if approved_plan_sha256 != approved_manifest.get("plan_sha256"):
        raise SafetyError("explicit approved plan hash does not match manifest")
    approved_status = approved_plan.get("status")
    if approved_status not in {"APPLICABLE", "NO_MUTATIONS_NEEDED"}:
        raise SafetyError("approved plan is not APPLICABLE or NO_MUTATIONS_NEEDED")
    if approved_manifest.get("plan_status") != approved_status:
        raise SafetyError("approved plan status differs from manifest")
    if approved_manifest.get("mode") != "dry-run":
        raise SafetyError("--apply accepts only an approved dry-run manifest")
    ability_before = ability_identity(ability_path)
    if ability_before.get("sha256") != (approved_manifest.get("ability") or {}).get("sha256"):
        raise SafetyError("ability.db hash differs from approved manifest")

    from sqlalchemy import text
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{lock_timeout_ms}ms"},
            )
            connection.execute(text("LOCK TABLE public.races IN SHARE ROW EXCLUSIVE MODE"))
            database = _database_identity(connection)
            if database != approved_manifest.get("database"):
                raise SafetyError("database identity differs from approved dry-run")
            columns = _fetch_race_columns(connection)
            approved_mutations = (
                int((approved_plan.get("counts") or {}).get("update", 0))
                + int((approved_plan.get("counts") or {}).get("delete", 0))
            )
            if approved_mutations:
                _assert_no_covering_natural_key_unique(connection)

            # Reopen and reload frozen truth only after the races lock has been
            # acquired, then recreate the complete snapshot/plan under it.
            if ability_identity(ability_path).get("sha256") != ability_before["sha256"]:
                raise SafetyError("ability.db hash changed before locked reclassification")
            truth_rows = load_ability_truth(ability_path)
            total_before = _count_table_rows(connection)
            candidates, destinations = fetch_target_closure(connection, truth_rows)
            current_gate = _verify_manual_fix_gate(
                _fetch_manual_fix_gate_rows(connection),
                approved_manifest.get("manual_fix_gate") or {},
            )
            global_duplicate_groups = _count_scoped_duplicate_groups(connection)
            candidate_groups = {
                _natural_key(row) for row in candidates
                if "a" in row.get("__candidate_reasons__", [])
            }
            if global_duplicate_groups != len(candidate_groups):
                raise SafetyError("duplicate groups outside the approved 2025 cleanup scope exist")
            fresh_plan = build_operation_plan(
                candidates + destinations, truth_rows,
                manual_fix_gate=current_gate,
            )
            if fresh_plan.get("plan_sha256") != approved_plan_sha256:
                raise SafetyError("fresh plan differs from approved plan (data drift)")
            if fresh_plan.get("status") != approved_status:
                raise SafetyError("fresh plan status differs from approved plan")

            if approved_status == "NO_MUTATIONS_NEEDED":
                if fresh_plan["counts"]["update"] or fresh_plan["counts"]["delete"]:
                    raise SafetyError("NO_MUTATIONS_NEEDED plan unexpectedly contains mutations")
                _verify_nonmutation_poststate(connection, fresh_plan)
                if ability_identity(ability_path) != ability_before:
                    raise SafetyError("ability.db changed during no-op apply")
                apply_bundle = _timestamped_dir(output_base, "apply_noop")
                apply_manifest = write_bundle(
                    apply_bundle, candidate_rows=candidates,
                    destination_rows=destinations, plan=fresh_plan,
                    ability=ability_before, database=database,
                    snapshot={
                        "locked_at_utc": datetime.now(timezone.utc).isoformat(),
                        "result": "no_mutations_needed",
                        "reason": "approved fresh plan has no mutations",
                        "approved_manifest_sha256": approved_manifest_sha256,
                        "approved_plan_sha256": approved_plan_sha256,
                    }, mode="apply-noop",
                )
                if ability_identity(ability_path) != ability_before:
                    raise SafetyError("ability.db changed while writing no-op bundle")
                transaction.commit()
                return apply_bundle, apply_manifest

            apply_bundle = _timestamped_dir(output_base, "apply_backup")
            apply_manifest = write_bundle(
                apply_bundle, candidate_rows=candidates, destination_rows=destinations,
                plan=fresh_plan, ability=ability_before, database=database,
                snapshot={"locked_at_utc": datetime.now(timezone.utc).isoformat(),
                          "approved_manifest_sha256": approved_manifest_sha256,
                          "approved_plan_sha256": approved_plan_sha256},
                mode="apply-backup",
            )
            updates, deletes = _apply_operations(
                connection, candidates + destinations, fresh_plan, columns,
            )
            expected = fresh_plan["counts"]
            if updates != expected["update"] or deletes != expected["delete"]:
                raise SafetyError("actual mutation counts differ from plan")
            total_after = _count_table_rows(connection)
            if total_before - total_after != deletes:
                raise SafetyError("table row-count delta differs from DELETE count")
            if _count_scoped_duplicate_groups(connection) != 0:
                raise SafetyError("scoped duplicate groups remain after mutation")
            _verify_truth_poststate(connection, fresh_plan, truth_rows)
            _verify_nonmutation_poststate(connection, fresh_plan)
            if ability_identity(ability_path) != ability_before:
                raise SafetyError("ability.db changed during apply")
            transaction.commit()
        except BaseException:
            transaction.rollback()
            raise
    return apply_bundle, apply_manifest


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="explicit dry-run (also the default)")
    mode.add_argument("--apply", action="store_true", help="apply an explicitly approved plan")
    parser.add_argument("--ability-db", default=str(Path(__file__).with_name("ability.db")))
    parser.add_argument("--output-dir", default=str(Path(__file__).with_name("outputs") / "t34b"))
    parser.add_argument("--database-url-env", default="DATABASE_URL",
                        help="environment variable containing the PostgreSQL URL; URL is never logged")
    parser.add_argument("--approved-manifest")
    parser.add_argument("--approved-manifest-sha256")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--writer-stopped", action="store_true")
    parser.add_argument("--lock-timeout-ms", type=int, default=5000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_argument_parser().parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        print(f"[ERROR] environment variable {args.database_url_env!r} is not set", file=sys.stderr)
        return 2
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://", "postgres://")):
        print("[ERROR] T34b cleanup supports PostgreSQL only", file=sys.stderr)
        return 2
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    engine = None
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        if args.apply:
            if (not args.approved_manifest or not args.approved_manifest_sha256
                    or not args.approved_plan_sha256):
                raise SafetyError(
                    "--apply requires --approved-manifest, "
                    "--approved-manifest-sha256, and --approved-plan-sha256"
                )
            if args.lock_timeout_ms < 1:
                raise SafetyError("--lock-timeout-ms must be positive")
            bundle, manifest = run_apply(
                engine, args.ability_db, args.output_dir, args.approved_manifest,
                args.approved_manifest_sha256, args.approved_plan_sha256,
                writer_stopped=args.writer_stopped,
                lock_timeout_ms=args.lock_timeout_ms,
            )
        else:
            bundle, manifest = run_dry_run(engine, args.ability_db, args.output_dir)
        print(f"bundle: {bundle}")
        print(f"manifest_sha256: {manifest['manifest_sha256']}")
        print(f"plan_sha256: {manifest['plan_sha256']}")
        print(f"plan_status: {manifest['plan_status']}")
        print(f"applicable: {manifest['applicable']}")
        # A generated report is still useful when it is NOT_APPLICABLE, but a
        # non-zero status prevents automation from chaining into migration.
        return 0 if manifest["plan_status"] in {
            "APPLICABLE", "NO_MUTATIONS_NEEDED",
        } else 3
    except (SafetyError, OSError, ValueError) as exc:
        print(f"[SAFE STOP] {exc}", file=sys.stderr)
        return 3
    except SQLAlchemyError as exc:
        # DBAPI messages can embed connection parameters.  Report only the
        # exception class and leave detailed inspection to protected logs.
        print(f"[SAFE STOP] database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 3
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
