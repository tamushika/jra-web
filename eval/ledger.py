"""Append-only JSONL experiment ledger and integrity verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


DEFAULT_LEDGER_PATH = Path(__file__).with_name("experiments.jsonl")
STATE_VERSION = 1
REQUIRED_KEYS = frozenset(
    {
        "experiment_id",
        "registered_at_utc",
        "commit_sha",
        "data_hashes",
        "features",
        "primary_metric",
        "safety_metrics",
        "search_grid",
        "candidate_count",
        "stop_rule",
        "benchmark_type",
        "prospective_start_date",
        "result_summary",
        "adjudication",
    }
)
RESULT_IMMUTABLE_FIELDS = (
    "commit_sha",
    "features",
    "primary_metric",
    "safety_metrics",
    "search_grid",
    "candidate_count",
    "stop_rule",
    "benchmark_type",
    "prospective_start_date",
)


class LedgerError(ValueError):
    """Base error for invalid or non-append-only ledgers."""


class LedgerValidationError(LedgerError):
    """Raised when one JSONL entry violates the experiment schema."""


class LedgerIntegrityError(LedgerError):
    """Raised when a previously verified byte prefix has changed."""


@dataclass(frozen=True)
class LedgerVerification(Mapping[str, Any]):
    line_count: int
    byte_count: int
    sha256: str
    state_path: Path
    checkpoint_updated: bool

    def _mapping(self) -> dict[str, Any]:
        return {
            "line_count": self.line_count,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "state_path": self.state_path,
            "checkpoint_updated": self.checkpoint_updated,
        }

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())


def _state_path(ledger_path: Path, state_path: str | Path | None) -> Path:
    if state_path is not None:
        return Path(state_path)
    return ledger_path.with_suffix(ledger_path.suffix + ".verify-state.json")


def _utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"{field} must be a non-empty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerValidationError(f"{field} is not an ISO timestamp") from exc
    if parsed.utcoffset() is None or parsed.astimezone(timezone.utc).utcoffset() != timezone.utc.utcoffset(None):
        raise LedgerValidationError(f"{field} must include a UTC offset")
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise LedgerValidationError(f"{field} must be expressed in UTC")
    return parsed


def _iso_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str):
        raise LedgerValidationError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerValidationError(f"{field} must be an ISO date") from exc


def validate_experiment(record: Any) -> dict[str, Any]:
    """Validate and return a shallow copy of one experiment registration."""

    if not isinstance(record, dict):
        raise LedgerValidationError("each ledger line must be a JSON object")
    missing = sorted(REQUIRED_KEYS - set(record))
    if missing:
        raise LedgerValidationError(f"missing required keys: {', '.join(missing)}")

    experiment_id = record["experiment_id"]
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise LedgerValidationError("experiment_id must be a non-empty string")
    if any(character.isspace() for character in experiment_id):
        raise LedgerValidationError("experiment_id must not contain whitespace")
    superseded_by = record.get("superseded_by")
    if superseded_by is not None:
        if not isinstance(superseded_by, str) or not superseded_by.strip():
            raise LedgerValidationError("superseded_by must be a non-empty experiment_id")
        if superseded_by == experiment_id:
            raise LedgerValidationError("superseded_by must not reference the same experiment")
    _utc_timestamp(record["registered_at_utc"], field="registered_at_utc")
    if not isinstance(record["commit_sha"], str) or not record["commit_sha"].strip():
        raise LedgerValidationError("commit_sha must be a non-empty string")
    if not isinstance(record["data_hashes"], dict):
        raise LedgerValidationError("data_hashes must be an object")
    if not isinstance(record["features"], list) or not all(
        isinstance(feature, str) and feature.strip() for feature in record["features"]
    ):
        raise LedgerValidationError("features must be a list of non-empty descriptions")
    if not isinstance(record["primary_metric"], str) or not record["primary_metric"].strip():
        raise LedgerValidationError("primary_metric must be exactly one non-empty string")
    if not isinstance(record["safety_metrics"], list) or not all(
        isinstance(metric, str) and metric.strip() for metric in record["safety_metrics"]
    ):
        raise LedgerValidationError("safety_metrics must be a list of strings")
    if not isinstance(record["search_grid"], dict):
        raise LedgerValidationError("search_grid must be an object")
    candidate_count = record["candidate_count"]
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 1:
        raise LedgerValidationError("candidate_count must be a positive integer")
    if not isinstance(record["stop_rule"], str) or not record["stop_rule"].strip():
        raise LedgerValidationError("stop_rule must be a non-empty string")

    benchmark_type = record["benchmark_type"]
    if benchmark_type not in {"historical", "prospective"}:
        raise LedgerValidationError("benchmark_type must be historical or prospective")
    prospective_start = record["prospective_start_date"]
    if benchmark_type == "prospective":
        # commit_sha is the immutable freeze commit for a prospective row.
        _iso_date(prospective_start, field="prospective_start_date")
    elif prospective_start is not None:
        _iso_date(prospective_start, field="prospective_start_date")
    has_result = record["result_summary"] is not None
    has_adjudication = record["adjudication"] is not None
    if (has_result or has_adjudication) and superseded_by is None:
        raise LedgerValidationError(
            "result/adjudication rows must reference the prior row with superseded_by"
        )
    if has_adjudication and not has_result:
        raise LedgerValidationError(
            "an adjudication row must carry forward a non-null result_summary"
        )
    try:
        # Reject non-JSON Python objects and non-standard NaN/Infinity before
        # they can enter an append-only record.
        json.dumps(record, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("experiment contains a non-JSON value") from exc
    return dict(record)


def _validate_linked_record(
    record: Mapping[str, Any],
    referenced: Mapping[str, Any],
    *,
    context: str,
) -> None:
    """Keep a result/adjudication row tied to its preregistered contract."""

    if record["result_summary"] is None and record["adjudication"] is None:
        # A preregistration may supersede an earlier specification. Its new
        # contract is allowed to change, but still requires a new unique ID.
        return
    changed = [
        field
        for field in RESULT_IMMUTABLE_FIELDS
        if record[field] != referenced[field]
    ]
    if changed:
        raise LedgerValidationError(
            f"{context}: result/adjudication changed immutable fields: "
            + ", ".join(changed)
        )
    for key, value in referenced["data_hashes"].items():
        if key not in record["data_hashes"] or record["data_hashes"][key] != value:
            raise LedgerValidationError(
                f"{context}: result/adjudication must preserve "
                f"data_hashes[{key!r}]"
            )
    if record["adjudication"] is not None:
        if referenced["result_summary"] is None:
            raise LedgerValidationError(
                f"{context}: adjudication must reference a prior result row"
            )
        if record["result_summary"] != referenced["result_summary"]:
            raise LedgerValidationError(
                f"{context}: adjudication must preserve result_summary"
            )


def _decode_ledger(data: bytes) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerValidationError("ledger must be UTF-8") from exc
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise LedgerValidationError(f"line {line_number}: blank lines are not allowed")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerValidationError(
                f"line {line_number}: invalid JSON: {exc.msg}"
            ) from exc
        try:
            record = validate_experiment(raw)
        except LedgerValidationError as exc:
            raise LedgerValidationError(f"line {line_number}: {exc}") from exc
        experiment_id = record["experiment_id"]
        if experiment_id in seen:
            raise LedgerValidationError(
                f"line {line_number}: duplicate experiment_id {experiment_id!r}"
            )
        superseded_by = record.get("superseded_by")
        if superseded_by is not None and superseded_by not in seen:
            raise LedgerValidationError(
                f"line {line_number}: superseded_by must reference an earlier "
                f"experiment_id, got {superseded_by!r}"
            )
        if superseded_by is not None:
            _validate_linked_record(
                record,
                seen_records[superseded_by],
                context=f"line {line_number}",
            )
        seen.add(experiment_id)
        seen_records[experiment_id] = record
        records.append(record)
    return records


def load_ledger(path: str | Path = DEFAULT_LEDGER_PATH) -> list[dict[str, Any]]:
    ledger_path = Path(path)
    data = ledger_path.read_bytes() if ledger_path.exists() else b""
    return _decode_ledger(data)


def _read_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerIntegrityError(f"invalid verification checkpoint: {path}") from exc
    required = {"version", "byte_count", "line_count", "prefix_sha256"}
    if not isinstance(checkpoint, dict) or not required.issubset(checkpoint):
        raise LedgerIntegrityError(f"invalid verification checkpoint: {path}")
    if checkpoint["version"] != STATE_VERSION:
        raise LedgerIntegrityError("unsupported verification checkpoint version")
    if (
        isinstance(checkpoint["byte_count"], bool)
        or not isinstance(checkpoint["byte_count"], int)
        or checkpoint["byte_count"] < 0
        or isinstance(checkpoint["line_count"], bool)
        or not isinstance(checkpoint["line_count"], int)
        or checkpoint["line_count"] < 0
        or not isinstance(checkpoint["prefix_sha256"], str)
    ):
        raise LedgerIntegrityError(f"invalid verification checkpoint: {path}")
    return checkpoint


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_ledger(
    path: str | Path = DEFAULT_LEDGER_PATH,
    *,
    state_path: str | Path | None = None,
    update_state: bool = True,
) -> LedgerVerification:
    """Validate schema, uniqueness, and the last verified append-only prefix."""

    ledger_path = Path(path)
    data = ledger_path.read_bytes() if ledger_path.exists() else b""
    records = _decode_ledger(data)
    checkpoint_path = _state_path(ledger_path, state_path)
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint is not None:
        previous_bytes = checkpoint["byte_count"]
        if len(data) < previous_bytes:
            raise LedgerIntegrityError("ledger was truncated after its last verification")
        prefix_hash = hashlib.sha256(data[:previous_bytes]).hexdigest()
        if prefix_hash != checkpoint["prefix_sha256"]:
            raise LedgerIntegrityError("previously verified ledger rows were modified")
        prefix_records = _decode_ledger(data[:previous_bytes])
        if len(prefix_records) != checkpoint["line_count"]:
            raise LedgerIntegrityError("verified ledger row count no longer matches")

    digest = hashlib.sha256(data).hexdigest()
    if update_state:
        _write_checkpoint(
            checkpoint_path,
            {
                "version": STATE_VERSION,
                "byte_count": len(data),
                "line_count": len(records),
                "prefix_sha256": digest,
            },
        )
    return LedgerVerification(
        line_count=len(records),
        byte_count=len(data),
        sha256=digest,
        state_path=checkpoint_path,
        checkpoint_updated=update_state,
    )


def append_experiment(
    record: Mapping[str, Any],
    path: str | Path = DEFAULT_LEDGER_PATH,
    *,
    state_path: str | Path | None = None,
) -> LedgerVerification:
    """Validate and atomically append one registration before checkpointing it."""

    ledger_path = Path(path)
    normalised = validate_experiment(dict(record))
    existing = load_ledger(ledger_path)
    verify_ledger(ledger_path, state_path=state_path, update_state=False)
    existing_ids = {item["experiment_id"] for item in existing}
    if normalised["experiment_id"] in existing_ids:
        raise LedgerValidationError(
            f"duplicate experiment_id {normalised['experiment_id']!r}"
        )
    superseded_by = normalised.get("superseded_by")
    if superseded_by is not None and superseded_by not in existing_ids:
        raise LedgerValidationError(
            "superseded_by must reference an earlier experiment_id, "
            f"got {superseded_by!r}"
        )
    if superseded_by is not None:
        referenced = next(
            item for item in existing if item["experiment_id"] == superseded_by
        )
        _validate_linked_record(
            normalised,
            referenced,
            context=f"experiment {normalised['experiment_id']!r}",
        )

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    previous = ledger_path.read_bytes() if ledger_path.exists() else b""
    separator = b"" if not previous or previous.endswith((b"\n", b"\r")) else b"\n"
    line = json.dumps(
        normalised,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    descriptor = os.open(
        ledger_path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, separator + line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return verify_ledger(ledger_path, state_path=state_path, update_state=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="validate ledger and append-only checkpoint")
    verify.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    verify.add_argument("--state", type=Path)
    verify.add_argument("--no-update-state", action="store_true")
    register = subparsers.add_parser("register", help="append one JSON experiment registration")
    register.add_argument("record", help="UTF-8 JSON file, or - for stdin")
    register.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    register.add_argument("--state", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_ledger(
                args.ledger,
                state_path=args.state,
                update_state=not args.no_update_state,
            )
        else:
            text = sys.stdin.read() if args.record == "-" else Path(args.record).read_text(encoding="utf-8")
            record = json.loads(text)
            result = append_experiment(record, args.ledger, state_path=args.state)
    except (LedgerError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: {result.line_count} experiments, {result.byte_count} bytes, "
        f"sha256={result.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
