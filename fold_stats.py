"""Leak-free factor tables for time-series backtests.

The production scorer reads course factor CSV files aggregated by db-keiba over
2021-2025.  Reusing those files in a 2025 holdout leaks the holdout outcomes
into jockey/frame/etc. rates.  This module rebuilds the subset of that table
used by the WIN5 backtests from ``ability.db`` and stops strictly at ``as_of``.

The table shape intentionally matches :func:`api.scoring.load_factor_table` so
callers can inject ``FoldFactorTableProvider`` without changing scoring rules.
No files are written and the SQLite database is opened read-only.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from contextlib import closing
from pathlib import Path


CURRENT_STATS_FROM = "20210101"
DEFAULT_WINDOW_YEARS = 5
TOP_ENTITY_LIMIT = 10
VENUE_SLUG_MAP = {
    "札幌": "sapporo", "函館": "hakodate", "福島": "fukushima",
    "新潟": "niigata", "東京": "tokyo", "中山": "nakayama",
    "中京": "chukyo", "京都": "kyoto", "阪神": "hanshin", "小倉": "kokura",
}


def fold_as_of_for(date_from: str) -> str:
    """Return the last day before the evaluation year.

    Examples: a 2025 fold uses data through 2024-12-31; a 2026 fold uses data
    through 2025-12-31.
    """
    _validate_date8(date_from, "date_from")
    return f"{int(date_from[:4]) - 1:04d}1231"


def rolling_stats_from(as_of: str, years: int = DEFAULT_WINDOW_YEARS) -> str:
    """First day of the rolling calendar-year window ending at ``as_of``."""
    _validate_date8(as_of, "as_of")
    if years <= 0:
        raise ValueError("years must be positive")
    return f"{int(as_of[:4]) - years + 1:04d}0101"


def load_pedigree_cache(path: str | Path) -> dict[str, str]:
    """Load the local, static horse -> sire mapping without network access."""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, ValueError, TypeError):
        return {}
    out = {}
    for horse, value in raw.items():
        sire = value.get("sire") if isinstance(value, dict) else None
        if horse and sire and sire != "-":
            out[str(horse).strip()] = str(sire).strip()
    return out


def discover_legacy_courses(api_dir: str | Path) -> set[tuple[str, str, int]]:
    """Return the exact course universe represented by current factor CSVs."""
    courses = set()
    root = Path(api_dir) / "data_files"
    for venue, slug in VENUE_SLUG_MAP.items():
        factor_dir = root / slug / "factors"
        if not factor_dir.is_dir():
            continue
        for path in factor_dir.glob("*.csv"):
            match = re.fullmatch(r"(芝|ダート)(\d+)\.csv", path.name)
            if match:
                courses.add((venue, match.group(1), int(match.group(2))))
    return courses


def _validate_date8(value: str, label: str) -> None:
    if not re.fullmatch(r"\d{8}", str(value or "")):
        raise ValueError(f"{label} must be YYYYMMDD: {value!r}")


def _norm(value) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s\.・．☆★▲△◇]", "", value).strip()


def _surface(value) -> str | None:
    value = str(value or "")
    if value == "芝":
        return "芝"
    if value in ("ダ", "ダート"):
        return "ダート"
    return None


def _surface_short(value) -> str | None:
    surface = _surface(value)
    return "芝" if surface == "芝" else ("ダ" if surface == "ダート" else None)


def _distance_label(current, previous) -> str | None:
    """Same seven distance bands as api.scoring._distance_label."""
    try:
        diff = int(current) - int(previous)
    except (TypeError, ValueError):
        return None
    if diff == 0:
        return "距離変更なし"
    absolute = abs(diff)
    if absolute >= 500:
        band = "500m以上"
    elif absolute >= 250:
        band = "300m-400m"
    else:
        band = "100m-200m"
    return band + ("延長" if diff > 0 else "短縮")


def _calculate_waku(umaban, head_count) -> int | None:
    """Reconstruct the official frame number represented by db-keiba rows.

    Extra horses are distributed one per outer frame on each pass (10 runners
    => frames 7 and 8 have two horses).  ``past_data_service.calculate_waku``
    currently puts both extras into frame 8; the scorer retains that existing
    behavior in T14, but the *statistics* must still use db-keiba's actual-frame
    definition so legacy-vs-fold changes only the aggregation period.
    """
    try:
        horse_no, total = int(umaban), int(head_count)
    except (TypeError, ValueError):
        return None
    if horse_no <= 0 or total <= 0 or horse_no > total:
        return None
    if total <= 8:
        return horse_no
    sizes = [1] * 8
    remaining = total - 8
    while remaining > 0:
        for i in range(7, -1, -1):
            if remaining <= 0:
                break
            sizes[i] += 1
            remaining -= 1
    first = 1
    for frame, size in enumerate(sizes, start=1):
        if first <= horse_no < first + size:
            return frame
        first += size
    return None


def _payout(value) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.startswith("("):
        return None
    try:
        payout = float(text)
    except ValueError:
        return None
    return payout if math.isfinite(payout) and payout >= 0 else None


class _Accumulator:
    __slots__ = (
        "n1", "n2", "n3", "out", "starts", "win_return", "win_expected",
        "win_known", "show_return", "show_expected", "show_known",
    )

    def __init__(self) -> None:
        self.n1 = self.n2 = self.n3 = self.out = self.starts = 0
        self.win_return = self.show_return = 0.0
        self.win_expected = self.win_known = 0
        self.show_expected = self.show_known = 0

    def add(self, rank: int, win_pay, show_pay) -> None:
        self.starts += 1
        if rank == 1:
            self.n1 += 1
            self.win_expected += 1
            payout = _payout(win_pay)
            if payout is not None:
                self.win_return += payout
                self.win_known += 1
        elif rank == 2:
            self.n2 += 1
        elif rank == 3:
            self.n3 += 1
        else:
            self.out += 1
        if rank <= 3:
            self.show_expected += 1
            payout = _payout(show_pay)
            if payout is not None:
                self.show_return += payout
                self.show_known += 1

    def row(self, entity: str) -> dict:
        starts = self.starts

        def rate(numerator: int) -> float:
            return round(100.0 * numerator / starts, 1) if starts else 0.0

        # Missing payout data must not look like a genuine 0% return.  The
        # scorer treats None as "no ROI bonus", which is the conservative path.
        win_roi = (round(self.win_return / starts, 1)
                   if starts and self.win_known == self.win_expected else None)
        show_roi = (round(self.show_return / starts, 1)
                    if starts and self.show_known == self.show_expected else None)
        return {
            "entity": entity,
            "n1": self.n1,
            "n2": self.n2,
            "n3": self.n3,
            "out": self.out,
            "starts": starts,
            "win_rate": rate(self.n1),
            "quinella_rate": rate(self.n1 + self.n2),
            "show_rate": rate(self.n1 + self.n2 + self.n3),
            "win_roi": win_roi,
            "show_roi": show_roi,
        }


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def build_fold_factor_tables(
    db_path: str | Path,
    as_of: str,
    *,
    stats_from: str | None = None,
    pedigree_by_horse: dict[str, str] | None = None,
    top_entity_limit: int = TOP_ENTITY_LIMIT,
    allowed_courses: set[tuple[str, str, int]] | None = None,
) -> dict[tuple[str, str, int], dict]:
    """Aggregate course factor tables from completed runs through ``as_of``.

    By default ``stats_from`` is the first day of the rolling five-year window
    ending at ``as_of`` (2021-01-01 for a 2025-12-31 snapshot), matching
    db-keiba's ``year5`` definition.  Rows before ``stats_from``
    are read only to establish each horse's immediately preceding surface and
    distance.  Sire is static reference data absent from ``runs``; when the
    local cache is supplied it is aggregated with the same course/rate rules.
    """
    _validate_date8(as_of, "as_of")
    stats_from = stats_from or rolling_stats_from(as_of)
    _validate_date8(stats_from, "stats_from")
    if as_of < stats_from:
        return {}
    if top_entity_limit < 0:
        raise ValueError("top_entity_limit must be >= 0")

    pedigree_by_horse = pedigree_by_horse or {}
    # build_ability_db.load_runs uses a three-year history window; matching it
    # keeps distance/surface definitions aligned while avoiding a full DB scan.
    history_from = f"{int(stats_from[:4]) - 3:04d}{stats_from[4:]}"
    acc = defaultdict(lambda: defaultdict(lambda: defaultdict(_Accumulator)))
    with closing(_read_only_connection(db_path)) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runs)")}
        show_expr = "fukusho_pay" if "fukusho_pay" in columns else "NULL"
        query = f"""
            SELECT date, place, r, horse, jockey, total_horses, umaban, rank,
                   track_type, distance, affi, win_pay, {show_expr} AS show_pay
              FROM runs
             WHERE date >= ? AND date <= ? AND rank IS NOT NULL
             ORDER BY horse, date, place, r
        """
        previous_by_horse = {}
        for row in conn.execute(query, (history_from, as_of)):
            (date8, place, _race_no, horse, jockey, total_horses, umaban, rank,
             track_type, distance, affi, win_pay, show_pay) = row
            surface = _surface(track_type)
            try:
                rank = int(rank)
                distance = int(distance)
            except (TypeError, ValueError):
                continue
            if not horse or not place or surface is None or rank <= 0:
                continue
            previous = previous_by_horse.get(horse)
            previous_by_horse[horse] = (distance, surface)
            if date8 < stats_from:
                continue

            course = (str(place), surface, distance)
            if allowed_courses is not None and course not in allowed_courses:
                continue

            def add(factor_type: str, entity) -> None:
                entity = _norm(entity)
                if entity:
                    acc[course][factor_type][entity].add(rank, win_pay, show_pay)

            add("baseline", "ALL")
            add("jockey_w", jockey)
            frame = _calculate_waku(umaban, total_horses)
            if frame:
                add("frame", f"{frame}枠")
            affiliation = ("美浦" if "美" in str(affi or "") else
                           ("栗東" if "栗" in str(affi or "") else None))
            add("stable_trainer", affiliation)
            sire = pedigree_by_horse.get(str(horse).strip())
            add("father_w", sire)
            if previous is not None:
                prev_distance, prev_surface = previous
                add("distance", _distance_label(distance, prev_distance))
                add("surface", f"{_surface_short(prev_surface)}→{_surface_short(surface)}")

    tables = {}
    top_types = {"jockey_w", "father_w"}
    for course, factors in acc.items():
        baseline_acc = factors.get("baseline", {}).get("ALL")
        if baseline_acc is None or not baseline_acc.starts:
            continue
        table = {
            "baseline": baseline_acc.row("ALL"),
            "_meta": {
                "source": "ability.db:runs",
                "stats_from": stats_from,
                "as_of": as_of,
                "strict_as_of": True,
            },
        }
        for factor_type, entities in factors.items():
            if factor_type == "baseline":
                continue
            items = list(entities.items())
            if factor_type in top_types:
                items.sort(key=lambda item: (-item[1].n1, -item[1].starts, item[0]))
                items = items[:top_entity_limit]
            table[factor_type] = {
                entity: bucket.row(entity) for entity, bucket in items
            }
        tables[course] = table

    return dict(tables)


class FoldFactorTableProvider:
    """Callable factor-table provider with one eager, immutable fold snapshot."""

    def __init__(
        self,
        db_path: str | Path,
        as_of: str,
        *,
        stats_from: str | None = None,
        pedigree_by_horse: dict[str, str] | None = None,
        pedigree_cache_path: str | Path | None = None,
        legacy_api_dir: str | Path | None = None,
    ) -> None:
        if pedigree_by_horse is None and pedigree_cache_path is not None:
            pedigree_by_horse = load_pedigree_cache(pedigree_cache_path)
        self.db_path = str(db_path)
        self.as_of = as_of
        self.explicit_stats_from = stats_from
        self.stats_from = stats_from or rolling_stats_from(as_of)
        self.pedigree_by_horse = pedigree_by_horse or {}
        self.legacy_api_dir = str(legacy_api_dir) if legacy_api_dir is not None else None
        allowed_courses = (discover_legacy_courses(legacy_api_dir)
                           if legacy_api_dir is not None else None)
        self.tables = build_fold_factor_tables(
            db_path,
            as_of,
            stats_from=self.stats_from,
            pedigree_by_horse=self.pedigree_by_horse,
            allowed_courses=allowed_courses,
        )

    def __call__(self, place: str, track_type: str, distance: int):
        surface = _surface(track_type)
        try:
            distance = int(distance)
        except (TypeError, ValueError):
            return None
        return self.tables.get((str(place), surface, distance))

    @property
    def course_count(self) -> int:
        return len(self.tables)

    def for_as_of(self, as_of: str) -> "FoldFactorTableProvider":
        """Create another strict snapshot with identical source configuration."""
        if as_of == self.as_of:
            return self
        return FoldFactorTableProvider(
            self.db_path,
            as_of,
            stats_from=self.explicit_stats_from,
            pedigree_by_horse=self.pedigree_by_horse,
            legacy_api_dir=self.legacy_api_dir,
        )


def compare_with_legacy(fold_tables, legacy_loader) -> dict:
    """Summarize rate differences against current factor CSV tables.

    This is a diagnostic for the same-end-date check requested by SPEC-T14.
    Exact equality is not expected because db-keiba and ability.db are separate
    sources; entity overlap and mean absolute percentage-point differences make
    that source difference explicit instead of silently treating it as leakage.
    """
    accumulators = defaultdict(lambda: {
        "fold_rows": 0, "legacy_rows": 0, "matched_rows": 0,
        "abs_win_rate_pp": 0.0, "abs_show_rate_pp": 0.0,
        "start_ratio_sum": 0.0, "start_ratio_n": 0,
    })
    common_courses = 0
    for (place, surface, distance), fold_table in fold_tables.items():
        legacy = legacy_loader(place, surface, distance)
        if not legacy:
            continue
        common_courses += 1
        factor_types = (set(fold_table) | set(legacy)) - {"_meta"}
        for factor_type in factor_types:
            fold_rows = (fold_table.get(factor_type) or {})
            legacy_rows = (legacy.get(factor_type) or {})
            if factor_type == "baseline":
                fold_rows = {"ALL": fold_rows} if fold_rows else {}
                legacy_rows = {"ALL": legacy_rows} if legacy_rows else {}
            metric = accumulators[factor_type]
            metric["fold_rows"] += len(fold_rows)
            metric["legacy_rows"] += len(legacy_rows)
            for entity in set(fold_rows) & set(legacy_rows):
                fold_row, legacy_row = fold_rows[entity], legacy_rows[entity]
                try:
                    fold_win = float(fold_row["win_rate"])
                    legacy_win = float(legacy_row["win_rate"])
                    fold_show = float(fold_row["show_rate"])
                    legacy_show = float(legacy_row["show_rate"])
                except (KeyError, TypeError, ValueError):
                    continue
                metric["matched_rows"] += 1
                metric["abs_win_rate_pp"] += abs(fold_win - legacy_win)
                metric["abs_show_rate_pp"] += abs(fold_show - legacy_show)
                legacy_starts = float(legacy_row.get("starts") or 0)
                if legacy_starts > 0:
                    metric["start_ratio_sum"] += (
                        float(fold_row.get("starts") or 0) / legacy_starts)
                    metric["start_ratio_n"] += 1

    factors = {}
    for factor_type, metric in sorted(accumulators.items()):
        matched = metric["matched_rows"]
        factors[factor_type] = {
            "fold_rows": metric["fold_rows"],
            "legacy_rows": metric["legacy_rows"],
            "matched_rows": matched,
            "mean_abs_win_rate_pp": (
                metric["abs_win_rate_pp"] / matched if matched else None),
            "mean_abs_show_rate_pp": (
                metric["abs_show_rate_pp"] / matched if matched else None),
            "mean_start_ratio": (
                metric["start_ratio_sum"] / metric["start_ratio_n"]
                if metric["start_ratio_n"] else None),
        }
    return {
        "fold_courses": len(fold_tables),
        "common_courses": common_courses,
        "factors": factors,
    }


def _main(argv=None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ability.db fold統計の確認")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "ability.db"))
    parser.add_argument("--as-of", required=True, metavar="YYYYMMDD")
    parser.add_argument("--from", dest="stats_from", default=None,
                        metavar="YYYYMMDD")
    parser.add_argument("--compare-current", action="store_true",
                        help="同じ集計終了日の現行CSVと率・行数を比較")
    args = parser.parse_args(argv)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    provider = FoldFactorTableProvider(
        args.db, args.as_of, stats_from=args.stats_from,
        pedigree_cache_path=os.path.join(base_dir, "pedigree_cache.json"),
        legacy_api_dir=os.path.join(base_dir, "api"))
    print(f"fold統計: {provider.stats_from}-{args.as_of} / {provider.course_count}コース")
    if not args.compare_current:
        return
    api_dir = os.path.join(base_dir, "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    import scoring

    report = compare_with_legacy(
        provider.tables,
        lambda place, surface, distance: scoring.load_legacy_factor_table(
            place, surface, distance, api_dir),
    )
    print(f"現行CSVとの共通コース: {report['common_courses']}/{report['fold_courses']}")
    print("factor          | fold/current/match | |勝率差|pp | |複勝率差|pp | starts比")
    for factor_type, row in report["factors"].items():
        win = row["mean_abs_win_rate_pp"]
        show = row["mean_abs_show_rate_pp"]
        ratio = row["mean_start_ratio"]
        print(f"{factor_type:15s} | {row['fold_rows']:5d}/{row['legacy_rows']:5d}/"
              f"{row['matched_rows']:5d} | "
              f"{win:10.3f} | {show:12.3f} | {ratio:8.3f}"
              if win is not None and show is not None and ratio is not None else
              f"{factor_type:15s} | {row['fold_rows']:5d}/{row['legacy_rows']:5d}/"
              f"{row['matched_rows']:5d} | n/a")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _main()
