"""Audit Phase C odds snapshots without modifying the logging database.

The source database is always opened in SQLite read-only/query-only mode.  The
generated Markdown and CSV artifacts are written to ``outputs/t40`` by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(os.environ.get("JRA_LOG_DB", BASE_DIR / "data" / "jra_logging.db"))
DEFAULT_OUTPUT_DIR = BASE_DIR / "outputs" / "t40"
TARGET_STAGES = ("30", "10", "2")
JST = timezone(timedelta(hours=9))
DIRTY_FLAGS = {
    "late_capture",
    "catchup_burst",
    "insufficient_odds",
    "post_time_changed",
    "invalid_quality_flags_json",
}


def open_readonly(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open an existing SQLite file with writes disabled at both URI and PRAGMA level."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"ロギングDBがありません: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]


def _race_date(race_id: str) -> str:
    prefix = str(race_id or "")[:8]
    if len(prefix) == 8 and prefix.isdigit():
        try:
            return datetime.strptime(prefix, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return "unknown"


def _parse_time(value: Any, *, race_id: str = "") -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            # Old monitor payloads can contain only HH:MM.
            try:
                clock = datetime.strptime(raw, "%H:%M").time()
                date_text = _race_date(race_id)
                if date_text == "unknown":
                    return None
                parsed = datetime.combine(datetime.fromisoformat(date_text).date(), clock)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(JST).isoformat(timespec="seconds")


def _stage(value: Any) -> str | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    result = str(int(numeric))
    return result if result in TARGET_STAGES else None


def _flags(value: Any) -> set[str]:
    if value is None:
        return set()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"invalid_quality_flags_json"}
    if isinstance(parsed, dict):
        return {str(key) for key, enabled in parsed.items() if enabled}
    if isinstance(parsed, (list, tuple, set)):
        return {str(item) for item in parsed}
    return {str(parsed)}


def _payload_post_time(payload_json: Any, race_id: str) -> datetime | None:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("scheduled_post_at", "_start_dt", "start_time"):
        parsed = _parse_time(payload.get(key), race_id=race_id)
        if parsed is not None:
            return parsed
    return None


def _post_time_fallbacks(conn: sqlite3.Connection) -> tuple[dict[str, datetime], dict[str, str], set[str]]:
    times: dict[str, datetime] = {}
    sources: dict[str, str] = {}
    candidate_races: set[str] = set()

    # A monitor value is closer to what the scheduler knew than the races catalog.
    for row in _table_rows(conn, "monitored_races"):
        race_id = str(row.get("race_id") or "")
        if not race_id:
            continue
        candidate_races.add(race_id)
        parsed = _parse_time(row.get("start_time"), race_id=race_id)
        if parsed is None:
            parsed = _payload_post_time(row.get("payload_json"), race_id)
        if parsed is not None:
            times[race_id] = parsed
            sources[race_id] = "monitored_races"

    for row in _table_rows(conn, "races"):
        race_id = str(row.get("race_id") or "")
        if not race_id:
            continue
        candidate_races.add(race_id)
        parsed = _parse_time(row.get("start_time"), race_id=race_id)
        if parsed is not None and race_id not in times:
            times[race_id] = parsed
            sources[race_id] = "races"
    return times, sources, candidate_races


def _first_number(values: Iterable[Any], cast) -> Any:
    for value in values:
        if value is None:
            continue
        try:
            return cast(value)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _valid_win_odds(value: Any) -> bool:
    try:
        odds = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(odds) and 1.0 < odds < 999.0


def _collapse_snapshots(
    rows: list[dict[str, Any]],
    fallback_times: dict[str, datetime],
    fallback_sources: dict[str, str],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        stage = _stage(row.get("stage"))
        if stage is None:
            continue
        race_id = str(row.get("race_id") or "")
        observed = str(row.get("observed_at") or "")
        fetch_key = str(row.get("fetch_id") or f"legacy:{observed}")
        groups[(race_id, stage, fetch_key)].append(row)

    snapshots: list[dict[str, Any]] = []
    for (race_id, stage, fetch_key), group in groups.items():
        observed = next(
            (_parse_time(row.get("observed_at"), race_id=race_id) for row in group
             if _parse_time(row.get("observed_at"), race_id=race_id) is not None),
            None,
        )
        direct_posts = [
            parsed
            for row in group
            if (parsed := _parse_time(row.get("scheduled_post_at"), race_id=race_id)) is not None
        ]
        scheduled = direct_posts[0] if direct_posts else fallback_times.get(race_id)
        post_source = "snapshot" if direct_posts else fallback_sources.get(race_id, "unrecoverable")
        seconds = _first_number((row.get("seconds_to_post") for row in group), float)
        if seconds is None and observed is not None and scheduled is not None:
            seconds = (scheduled - observed).total_seconds()

        explicit_valid = _first_number((row.get("valid_odds_count") for row in group), int)
        explicit_field = _first_number((row.get("field_size") for row in group), int)
        horse_ids = {str(row.get("horse_id")) for row in group if row.get("horse_id") is not None}
        valid_from_rows = sum(
            1 for row in group if _valid_win_odds(row.get("win_odds"))
        )
        valid_count = explicit_valid if explicit_valid is not None else valid_from_rows
        field_size = explicit_field if explicit_field is not None else len(horse_ids)
        all_flags: set[str] = set()
        for row in group:
            all_flags.update(_flags(row.get("data_quality_flags_json")))

        expected_seconds = int(stage) * 60
        within_window = seconds is not None and abs(seconds - expected_seconds) <= 60.0
        insufficient = valid_count < max(4, field_size // 2)
        if seconds is not None and not within_window:
            all_flags.add("late_capture")
        if insufficient:
            all_flags.add("insufficient_odds")

        snapshots.append({
            "race_id": race_id,
            "race_date": _race_date(race_id),
            "stage": stage,
            "fetch_key": fetch_key,
            "observed_dt": observed,
            "observed_at": _iso(observed),
            "scheduled_dt": scheduled,
            "scheduled_post_at": _iso(scheduled),
            "scheduled_post_source": post_source,
            "seconds_to_post": seconds,
            "minutes_to_post": seconds / 60.0 if seconds is not None else None,
            "timing_error_seconds": seconds - expected_seconds if seconds is not None else None,
            "within_window": within_window,
            "fetch_duration_ms": _first_number(
                (row.get("fetch_duration_ms") for row in group), int),
            "valid_odds_count": valid_count,
            "field_size": field_size,
            "flags": all_flags,
            "row_count": len(group),
        })

    snapshots.sort(key=lambda item: (
        item["race_id"], item["observed_dt"] or datetime.min.replace(tzinfo=JST),
        -int(item["stage"]),
    ))
    return snapshots


def _detect_catchup(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bursts: list[dict[str, Any]] = []
    by_race: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        by_race[snapshot["race_id"]].append(snapshot)
    for race_id, items in by_race.items():
        dated = [item for item in items if item["observed_dt"] is not None]
        dated.sort(key=lambda item: item["observed_dt"])
        for later_index, later in enumerate(dated):
            for earlier in reversed(dated[:later_index]):
                delta = (later["observed_dt"] - earlier["observed_dt"]).total_seconds()
                if delta > 120:
                    break
                if earlier["stage"] == later["stage"]:
                    continue
                later["flags"].add("catchup_burst")
                bursts.append({
                    "race_id": race_id,
                    "race_date": later["race_date"],
                    "earlier_stage": earlier["stage"],
                    "later_stage": later["stage"],
                    "earlier_observed_at": earlier["observed_at"],
                    "later_observed_at": later["observed_at"],
                    "gap_seconds": delta,
                })
    return bursts


def _detect_post_time_changes(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    by_race: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot["scheduled_post_source"] == "snapshot" and snapshot["scheduled_dt"] is not None:
            by_race[snapshot["race_id"]].append(snapshot)
    for race_id, items in by_race.items():
        items.sort(key=lambda item: item["observed_dt"] or item["scheduled_dt"])
        previous = items[0]
        for current in items[1:]:
            if current["scheduled_dt"] != previous["scheduled_dt"]:
                current["flags"].add("post_time_changed")
                changes.append({
                    "race_id": race_id,
                    "race_date": current["race_date"],
                    "previous_stage": previous["stage"],
                    "current_stage": current["stage"],
                    "previous_scheduled_post_at": previous["scheduled_post_at"],
                    "current_scheduled_post_at": current["scheduled_post_at"],
                    "change_seconds": (
                        current["scheduled_dt"] - previous["scheduled_dt"]
                    ).total_seconds(),
                    "observed_at": current["observed_at"],
                })
            previous = current
    return changes


def _timing_bucket(error_seconds: float | None) -> str:
    if error_seconds is None:
        return "unrecoverable"
    value = abs(error_seconds)
    if value <= 60:
        return "within_1m"
    if value <= 180:
        return "1m_to_3m"
    if value <= 300:
        return "3m_to_5m"
    if value <= 600:
        return "5m_to_10m"
    return "over_10m"


def _candidate_races(
    snapshots: list[dict[str, Any]], catalog_races: set[str]
) -> list[str]:
    snapshot_races = {item["race_id"] for item in snapshots}
    candidates = snapshot_races | catalog_races
    dated = sorted(item["race_date"] for item in snapshots if item["race_date"] != "unknown")
    if not dated:
        return sorted(candidates)
    first_date, last_date = dated[0], dated[-1]
    return sorted(
        race_id for race_id in candidates
        if _race_date(race_id) == "unknown" or first_date <= _race_date(race_id) <= last_date
    )


def audit_snapshots(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return audit aggregates while executing SELECT/PRAGMA statements only."""
    fallback_times, fallback_sources, catalog_races = _post_time_fallbacks(conn)
    odds_rows = _table_rows(conn, "odds_snapshots")
    snapshots = _collapse_snapshots(odds_rows, fallback_times, fallback_sources)
    catchup_bursts = _detect_catchup(snapshots)
    post_time_changes = _detect_post_time_changes(snapshots)
    for snapshot in snapshots:
        snapshot["clean"] = (
            snapshot["within_window"] and not (snapshot["flags"] & DIRTY_FLAGS)
        )

    candidates = _candidate_races(snapshots, catalog_races)
    by_race_stage: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        by_race_stage[(snapshot["race_id"], snapshot["stage"])].append(snapshot)

    coverage: list[dict[str, Any]] = []
    for race_id in candidates:
        for stage in TARGET_STAGES:
            items = by_race_stage.get((race_id, stage), [])
            coverage.append({
                "race_id": race_id,
                "race_date": _race_date(race_id),
                "stage": stage,
                "captured": int(bool(items)),
                "fetch_count": len(items),
                "clean_fetch_count": sum(int(item["clean"]) for item in items),
                "within_window_fetch_count": sum(int(item["within_window"]) for item in items),
                "max_valid_odds_count": max(
                    (item["valid_odds_count"] for item in items), default=None),
                "field_size": max((item["field_size"] for item in items), default=None),
            })

    stage_summary: list[dict[str, Any]] = []
    for stage in TARGET_STAGES:
        items = [item for item in snapshots if item["stage"] == stage]
        covered = {item["race_id"] for item in items}
        recoverable = [item for item in items if item["seconds_to_post"] is not None]
        clean_races = {item["race_id"] for item in items if item["clean"]}
        expected = len(candidates)
        valid_counts = [item["valid_odds_count"] for item in items]
        stage_summary.append({
            "stage": stage,
            "expected_races": expected,
            "captured_races": len(covered),
            "missing_races": max(0, expected - len(covered)),
            "missing_rate_pct": (100.0 * (expected - len(covered)) / expected) if expected else 0.0,
            "snapshot_fetches": len(items),
            "reconstructable_fetches": len(recoverable),
            "unrecoverable_fetches": len(items) - len(recoverable),
            "within_window_fetches": sum(int(item["within_window"]) for item in recoverable),
            "within_window_rate_pct": (
                100.0 * sum(int(item["within_window"]) for item in recoverable) / len(recoverable)
                if recoverable else 0.0
            ),
            "clean_fetches": sum(int(item["clean"]) for item in items),
            "clean_races": len(clean_races),
            "valid_odds_min": min(valid_counts) if valid_counts else None,
            "valid_odds_mean": sum(valid_counts) / len(valid_counts) if valid_counts else None,
            "valid_odds_max": max(valid_counts) if valid_counts else None,
        })

    timing_counter = Counter(
        (item["stage"], _timing_bucket(item["timing_error_seconds"])) for item in snapshots
    )
    timing_histogram = [
        {"stage": stage, "divergence_bucket": bucket, "snapshot_fetches": count}
        for (stage, bucket), count in sorted(
            timing_counter.items(), key=lambda pair: (TARGET_STAGES.index(pair[0][0]), pair[0][1]))
    ]
    odds_counter = Counter(
        (item["stage"], item["valid_odds_count"]) for item in snapshots
    )
    valid_odds_distribution = [
        {"stage": stage, "valid_odds_count": valid, "snapshot_fetches": count}
        for (stage, valid), count in sorted(
            odds_counter.items(), key=lambda pair: (TARGET_STAGES.index(pair[0][0]), pair[0][1]))
    ]
    daily_counter = Counter(item["race_date"] for item in catchup_bursts)
    daily_catchup = [
        {"race_date": race_date, "catchup_pairs": count}
        for race_date, count in sorted(daily_counter.items())
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_rows": len(odds_rows),
        "snapshot_fetches": len(snapshots),
        "candidate_races": len(candidates),
        "stage_summary": stage_summary,
        "coverage": coverage,
        "timing_histogram": timing_histogram,
        "valid_odds_distribution": valid_odds_distribution,
        "catchup_bursts": catchup_bursts,
        "daily_catchup": daily_catchup,
        "post_time_changes": post_time_changes,
        "snapshots": snapshots,
    }


def _fmt_pct(value: Any) -> str:
    return f"{float(value):.1f}%" if value is not None else "n/a"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# T40 オッズスナップショット品質監査",
        "",
        f"- 生成時刻 (UTC): `{report['generated_at']}`",
        f"- 元の馬行: {report['source_rows']}",
        f"- 取得単位: {report['snapshot_fetches']}",
        f"- カバレッジ候補レース: {report['candidate_races']}",
        "- DBは read-only/query-only で開き、監査中の書き込みは行っていない。",
        "",
        "## stage別サマリ",
        "",
        "| stage | 取得レース/期待 | 欠損率 | 実時刻復元 | 窓内取得率 | cleanレース | 有効オッズ頭数 min/mean/max |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["stage_summary"]:
        mean = row["valid_odds_mean"]
        valid_text = (
            f"{row['valid_odds_min']}/{mean:.1f}/{row['valid_odds_max']}"
            if mean is not None else "n/a"
        )
        lines.append(
            f"| {row['stage']} | {row['captured_races']}/{row['expected_races']} | "
            f"{_fmt_pct(row['missing_rate_pct'])} | {row['reconstructable_fetches']}/"
            f"{row['snapshot_fetches']} | {_fmt_pct(row['within_window_rate_pct'])} | "
            f"{row['clean_races']} | {valid_text} |"
        )

    lines.extend([
        "",
        "## stageラベルと実残り時間の乖離",
        "",
        "| stage | 乖離バケット | 取得数 |",
        "|---:|---|---:|",
    ])
    for row in report["timing_histogram"]:
        lines.append(
            f"| {row['stage']} | {row['divergence_bucket']} | {row['snapshot_fetches']} |"
        )
    if not report["timing_histogram"]:
        lines.append("| - | 対象なし | 0 |")

    lines.extend([
        "",
        "## 近接した別stage取得",
        "",
        f"catchup pair: **{len(report['catchup_bursts'])}** 件",
        "",
        "| 日付 | pair数 |",
        "|---|---:|",
    ])
    for row in report["daily_catchup"]:
        lines.append(f"| {row['race_date']} | {row['catchup_pairs']} |")
    if not report["daily_catchup"]:
        lines.append("| - | 0 |")

    lines.extend([
        "",
        "## 発走時刻変更の疑い",
        "",
        f"該当遷移: **{len(report['post_time_changes'])}** 件",
        "",
        "| race_id | 前回予定 | 今回予定 | 変化(秒) |",
        "|---|---|---|---:|",
    ])
    for row in report["post_time_changes"]:
        lines.append(
            f"| {row['race_id']} | {row['previous_scheduled_post_at']} | "
            f"{row['current_scheduled_post_at']} | {row['change_seconds']:.0f} |"
        )
    if not report["post_time_changes"]:
        lines.append("| - | - | - | 0 |")

    lines.extend([
        "",
        "## clean subsetの定義",
        "",
        "stage分×60秒と実残り時間の差が±60秒以内で、"
        "`late_capture` / `catchup_burst` / `insufficient_odds` / "
        "`post_time_changed` / `invalid_quality_flags_json` のいずれも無い取得を "
        "clean とした。",
        "復元不能な旧データは窓内率の分母から除き、件数を別途明示した。",
        "カバレッジ候補は対象snapshotの日付範囲内にある `races` / "
        "`monitored_races` / snapshot の和集合である。",
        "",
    ])
    return "\n".join(lines)


def _csv_value(value: Any) -> Any:
    if isinstance(value, set):
        return "|".join(sorted(value))
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def write_report(report: dict[str, Any], output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output / "snapshot_quality_report.md",
        "stage_summary": output / "stage_summary.csv",
        "coverage": output / "race_stage_coverage.csv",
        "timing_histogram": output / "timing_histogram.csv",
        "valid_odds_distribution": output / "valid_odds_distribution.csv",
        "catchup_bursts": output / "catchup_bursts.csv",
        "daily_catchup": output / "daily_catchup_bursts.csv",
        "post_time_changes": output / "suspected_post_time_changes.csv",
    }
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    _write_csv(paths["stage_summary"], report["stage_summary"], list(report["stage_summary"][0]))
    _write_csv(paths["coverage"], report["coverage"], [
        "race_id", "race_date", "stage", "captured", "fetch_count", "clean_fetch_count",
        "within_window_fetch_count", "max_valid_odds_count", "field_size",
    ])
    _write_csv(paths["timing_histogram"], report["timing_histogram"], [
        "stage", "divergence_bucket", "snapshot_fetches",
    ])
    _write_csv(paths["valid_odds_distribution"], report["valid_odds_distribution"], [
        "stage", "valid_odds_count", "snapshot_fetches",
    ])
    _write_csv(paths["catchup_bursts"], report["catchup_bursts"], [
        "race_id", "race_date", "earlier_stage", "later_stage",
        "earlier_observed_at", "later_observed_at", "gap_seconds",
    ])
    _write_csv(paths["daily_catchup"], report["daily_catchup"], [
        "race_date", "catchup_pairs",
    ])
    _write_csv(paths["post_time_changes"], report["post_time_changes"], [
        "race_id", "race_date", "previous_stage", "current_stage",
        "previous_scheduled_post_at", "current_scheduled_post_at", "change_seconds", "observed_at",
    ])
    return paths


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Phase C オッズスナップショット品質監査")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="ロギングSQLite DB")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Markdown/CSV出力先")
    args = parser.parse_args()
    try:
        with open_readonly(args.db) as conn:
            report = audit_snapshots(conn)
        paths = write_report(report, args.output_dir)
    except (FileNotFoundError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(
        f"[OK] snapshot_fetches={report['snapshot_fetches']} "
        f"candidate_races={report['candidate_races']} output={paths['markdown']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
