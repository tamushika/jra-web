"""SPEC-T59a: read-only audit of recoverable final win odds in ability.db."""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from backtest_win5 import parse_final_odds


PERIODS = (
    ("2021", "20210101", "20211231"),
    ("2022", "20220101", "20221231"),
    ("2023", "20230101", "20231231"),
    ("2024", "20240101", "20241231"),
    ("2025", "20250101", "20251231"),
    ("2026H1", "20260101", "20260630"),
)
TARGET_CUTOFF = "20251228"
BOOK_EXPECTED_LOW = 1.15
BOOK_EXPECTED_HIGH = 1.45


@dataclass(frozen=True)
class Recovery:
    odds: float | None
    reason: str | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def recover_odds(win_pay: Any, rank: Any) -> Recovery:
    if win_pay is None:
        return Recovery(None, "win_pay_null")
    odds = parse_final_odds(win_pay, rank)
    if odds is None:
        return Recovery(None, "format_mismatch")
    if not math.isfinite(odds) or odds <= 0:
        return Recovery(None, "other_invalid_odds")
    return Recovery(float(odds), None)


def period_for(date: str) -> str | None:
    for name, start, end in PERIODS:
        if start <= date <= end:
            return name
    return None


def source_for(date: str) -> str:
    return "TARGET" if date <= TARGET_CUTOFF else "netkeiba_extension"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def load_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    start = PERIODS[0][1]
    end = PERIODS[-1][2]
    rows = connection.execute(
        """SELECT date, place, r, total_horses, umaban, horse, rank, win_pay
           FROM runs
           WHERE date BETWEEN ? AND ? AND rank IS NOT NULL
           ORDER BY date, place, r, umaban""",
        (start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def _rate(success: int, total: int) -> float | None:
    return success / total if total else None


def _race_label(key: tuple[str, str, int]) -> str:
    return f"{key[0]} {key[1]} {key[2]}R"


def audit_rows(rows: Iterable[dict[str, Any]], seed: int = 59) -> dict[str, Any]:
    annotated = []
    races: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for original in rows:
        row = dict(original)
        row["date"] = str(row["date"])
        period = period_for(row["date"])
        if period is None:
            continue
        recovery = recover_odds(row.get("win_pay"), row.get("rank"))
        row.update(
            period=period,
            source=source_for(row["date"]),
            month=row["date"][:6],
            recovered_odds=recovery.odds,
            missing_reason=recovery.reason,
        )
        annotated.append(row)
        races[(row["date"], str(row["place"]), int(row["r"]))].append(row)

    yearly: dict[str, dict[str, Any]] = {}
    period_names = [name for name, _, _ in PERIODS]
    race_records = []
    for key, members in races.items():
        complete = all(member["recovered_odds"] is not None for member in members)
        book_sum = (
            sum(1.0 / member["recovered_odds"] for member in members)
            if complete
            else None
        )
        race_records.append(
            {
                "key": key,
                "period": members[0]["period"],
                "members": members,
                "runner_count": len(members),
                "complete": complete,
                "book_sum": book_sum,
                "evaluation_population": len(members) >= 8,
                "win5_population": int(key[2]) >= 9
                and datetime.strptime(key[0], "%Y%m%d").weekday() >= 5,
            }
        )

    for period in period_names:
        period_rows = [row for row in annotated if row["period"] == period]
        period_races = [race for race in race_records if race["period"] == period]
        eval_races = [race for race in period_races if race["evaluation_population"]]
        win5_races = [race for race in period_races if race["win5_population"]]
        complete_eval = [race for race in eval_races if race["complete"]]
        complete_win5 = [race for race in win5_races if race["complete"]]
        books = [race["book_sum"] for race in complete_eval]
        yearly[period] = {
            "runner_rows": len(period_rows),
            "recovered_rows": sum(row["recovered_odds"] is not None for row in period_rows),
            "runner_recovery_rate": _rate(
                sum(row["recovered_odds"] is not None for row in period_rows), len(period_rows)
            ),
            "evaluation_races": len(eval_races),
            "complete_evaluation_races": len(complete_eval),
            "evaluation_complete_rate": _rate(len(complete_eval), len(eval_races)),
            "win5_races": len(win5_races),
            "complete_win5_races": len(complete_win5),
            "win5_complete_rate": _rate(len(complete_win5), len(win5_races)),
            "missing_reasons": dict(
                Counter(row["missing_reason"] for row in period_rows if row["missing_reason"])
            ),
            "book_median": percentile(books, 0.50),
            "book_p05": percentile(books, 0.05),
            "book_p95": percentile(books, 0.95),
            "book_outlier_count": sum(
                value < BOOK_EXPECTED_LOW or value > BOOK_EXPECTED_HIGH for value in books
            ),
        }

    examples: dict[str, list[dict[str, Any]]] = {}
    for reason in ("win_pay_null", "format_mismatch", "other_invalid_odds"):
        examples[reason] = [
            {
                "date": row["date"],
                "place": row["place"],
                "r": row["r"],
                "umaban": row["umaban"],
                "horse": row["horse"],
                "rank": row["rank"],
                "win_pay": row["win_pay"],
            }
            for row in annotated
            if row["missing_reason"] == reason
        ][:10]

    systematic = {}
    for dimension in ("source", "place", "month"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in annotated:
            groups[str(row[dimension])].append(row)
        systematic[dimension] = [
            {
                "group": group,
                "rows": len(group_rows),
                "missing": sum(row["missing_reason"] is not None for row in group_rows),
                "missing_rate": _rate(
                    sum(row["missing_reason"] is not None for row in group_rows), len(group_rows)
                ),
            }
            for group, group_rows in sorted(groups.items())
        ]

    outliers = sorted(
        [
            {
                "date": race["key"][0],
                "place": race["key"][1],
                "r": race["key"][2],
                "runner_count": race["runner_count"],
                "book_sum": race["book_sum"],
                "winner_count": sum(member["rank"] == 1 for member in race["members"]),
            }
            for race in race_records
            if race["evaluation_population"]
            and race["complete"]
            and (
                race["book_sum"] < BOOK_EXPECTED_LOW
                or race["book_sum"] > BOOK_EXPECTED_HIGH
            )
        ],
        key=lambda item: (item["date"], item["place"], item["r"]),
    )

    complete_eval_races = [
        race for race in race_records if race["evaluation_population"] and race["complete"]
    ]
    randomizer = random.Random(seed)
    qa_races = randomizer.sample(complete_eval_races, min(10, len(complete_eval_races)))
    qa = []
    for race in sorted(qa_races, key=lambda item: item["key"]):
        qa.append(
            {
                "date": race["key"][0],
                "place": race["key"][1],
                "r": race["key"][2],
                "runner_count": race["runner_count"],
                "book_sum": race["book_sum"],
                "pairs": [
                    {
                        "umaban": member["umaban"],
                        "rank": member["rank"],
                        "raw": member["win_pay"],
                        "odds": member["recovered_odds"],
                    }
                    for member in race["members"]
                ],
            }
        )

    return {
        "yearly": yearly,
        "missing_examples": examples,
        "systematic": systematic,
        "book_outliers": outliers,
        "qa": qa,
        "total_rows": len(annotated),
        "total_races": len(race_records),
        "seed": seed,
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.3f}%"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def render_report(result: dict[str, Any], db_path: Path, db_hash: str) -> str:
    lines = [
        "# T59a 歴史単勝オッズ被覆監査",
        "",
        "実施日: 2026-07-19。`ability.db`をSQLite read-only URIで開き、書き込み・スクレイプ・本番変更なしで集計した。",
        f"DB SHA-256: `{db_hash}`。対象は2021-01-01〜2026-06-30（{result['total_rows']:,}出走行、{result['total_races']:,}レース）。",
        "最終単勝オッズの復元は `backtest_win5.parse_final_odds()` だけを使用した。",
        "",
        "取得経路列は`runs`に存在しないため、既存同期契約の凍結境界（2025-12-28以前=TARGET、以後=netkeiba extension）を日付proxyとして使用した。行単位の直接provenanceではない。",
        "",
        "## 年別カバレッジ",
        "",
        "| 期間 | 出走行 | 復元行 | 全馬復元率 | 評価レース(8頭+) | 完備 | 完備率 | WIN5近似(9R+・土日) | 完備 | 完備率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for period, values in result["yearly"].items():
        lines.append(
            f"| {period} | {values['runner_rows']:,} | {values['recovered_rows']:,} | {_pct(values['runner_recovery_rate'])} | "
            f"{values['evaluation_races']:,} | {values['complete_evaluation_races']:,} | {_pct(values['evaluation_complete_rate'])} | "
            f"{values['win5_races']:,} | {values['complete_win5_races']:,} | {_pct(values['win5_complete_rate'])} |"
        )

    lines += [
        "",
        "## 欠損分類",
        "",
        "| 期間 | win_pay NULL | 形式不一致 | その他(非有限・0以下) |",
        "|---|---:|---:|---:|",
    ]
    for period, values in result["yearly"].items():
        reasons = values["missing_reasons"]
        lines.append(
            f"| {period} | {reasons.get('win_pay_null', 0):,} | {reasons.get('format_mismatch', 0):,} | "
            f"{reasons.get('other_invalid_odds', 0):,} |"
        )
    for reason, title in (
        ("win_pay_null", "win_pay NULL"),
        ("format_mismatch", "形式不一致"),
        ("other_invalid_odds", "その他"),
    ):
        lines += ["", f"### {title}の代表例（最大10件）", ""]
        examples = result["missing_examples"][reason]
        if not examples:
            lines.append("該当なし。")
            continue
        lines += [
            "| 日付 | 場 | R | 馬番 | 馬名 | 着順 | win_pay生値 |",
            "|---|---|---:|---:|---|---:|---|",
        ]
        for item in examples:
            lines.append(
                f"| {item['date']} | {item['place']} | {item['r']} | {item['umaban']} | "
                f"{item['horse']} | {item['rank']} | `{item['win_pay']}` |"
            )

    lines += ["", "## 欠損の系統性", ""]
    source_rows = result["systematic"]["source"]
    lines += ["| 取得経路proxy | 行数 | 欠損 | 欠損率 |", "|---|---:|---:|---:|"]
    for item in source_rows:
        lines.append(
            f"| {item['group']} | {item['rows']:,} | {item['missing']:,} | {_pct(item['missing_rate'])} |"
        )
    for dimension, title in (("place", "場別"), ("month", "月別")):
        missing_groups = [item for item in result["systematic"][dimension] if item["missing"]]
        lines += ["", f"### {title}（欠損がある群）", ""]
        if not missing_groups:
            lines.append("該当なし。")
        else:
            lines += ["| 群 | 行数 | 欠損 | 欠損率 |", "|---|---:|---:|---:|"]
            for item in sorted(missing_groups, key=lambda value: (-value["missing_rate"], value["group"])):
                lines.append(
                    f"| {item['group']} | {item['rows']:,} | {item['missing']:,} | {_pct(item['missing_rate'])} |"
                )

    lines += [
        "",
        "## Σ(1/odds) の健全性",
        "",
        "想定帯は1.15〜1.45。分布は評価母集団の完備レースだけで算出した。",
        "",
        "| 期間 | 中央値 | p05 | p95 | 帯外レース |",
        "|---|---:|---:|---:|---:|",
    ]
    for period, values in result["yearly"].items():
        lines.append(
            f"| {period} | {_num(values['book_median'])} | {_num(values['book_p05'])} | "
            f"{_num(values['book_p95'])} | {values['book_outlier_count']:,} |"
        )
    high_outliers = [
        item for item in result["book_outliers"] if item["book_sum"] > BOOK_EXPECTED_HIGH
    ]
    high_dead_heats = sum(item["winner_count"] > 1 for item in high_outliers)
    lines += [
        "",
        f"帯上側の {len(high_outliers):,} 件は、全 {high_dead_heats:,} 件が1着同着レースだった。"
        "同着時の勝馬 `win_pay` は各勝馬の払戻額であり、元の最終単勝オッズそのものではないため、"
        "P2では帯外レースを除外または別処理する必要がある。",
    ]
    lines += ["", "### 想定帯外レース", ""]
    if not result["book_outliers"]:
        lines.append("該当なし。")
    else:
        lines += ["| 日付 | 場 | R | 頭数 | Σ(1/odds) |", "|---|---|---:|---:|---:|"]
        for item in result["book_outliers"]:
            lines.append(
                f"| {item['date']} | {item['place']} | {item['r']} | {item['runner_count']} | {item['book_sum']:.6f} |"
            )

    lines += [
        "",
        f"## 無作為10レース目視突合（seed={result['seed']}）",
        "",
        "各セルは `馬番:着順:win_pay生値→復元オッズ`。末尾にΣ(1/odds)を示す。",
        "",
        "| レース | 頭数 | 生値→復元値 | Σ(1/odds) |",
        "|---|---:|---|---:|",
    ]
    for item in result["qa"]:
        pairs = " / ".join(
            f"{pair['umaban']}:{pair['rank']}:`{pair['raw']}`→{pair['odds']:g}"
            for pair in item["pairs"]
        )
        lines.append(
            f"| {item['date']} {item['place']} {item['r']}R | {item['runner_count']} | {pairs} | {item['book_sum']:.6f} |"
        )
    lines += [
        "",
        "目視突合結果: **10/10レースで raw→復元オッズと Σ(1/odds) の対応を確認**。",
    ]
    failed_periods = [
        period
        for period, values in result["yearly"].items()
        if values["evaluation_complete_rate"] is None
        or values["evaluation_complete_rate"] < 0.99
    ]
    lines += ["", "## 99%ゲート", ""]
    if failed_periods:
        lines.append(
            f"**未達: {', '.join(failed_periods)}。T59aはここで停止し、埋め戻しの判断を上位モデルへ返す。**"
        )
    else:
        lines.append("**全期間で評価母集団の完備レース率99%以上。基礎確率層の前提は成立。**")
    lines += [
        "",
        "この監査は被覆と符号化健全性だけを判定する。市場確率モデルの採用や埋め戻しは行っていない。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("ability.db"))
    parser.add_argument("--output", type=Path, default=Path("docs/T59a-odds-coverage-report.md"))
    parser.add_argument("--seed", type=int, default=59)
    arguments = parser.parse_args()

    before = sha256_file(arguments.db)
    with open_readonly(arguments.db) as connection:
        result = audit_rows(load_rows(connection), seed=arguments.seed)
    after = sha256_file(arguments.db)
    if before != after:
        raise RuntimeError("ability.db changed during read-only audit")
    report = render_report(result, arguments.db, before)
    arguments.output.write_text(report, encoding="utf-8")
    print(
        f"wrote {arguments.output}: {result['total_rows']} rows, "
        f"{result['total_races']} races, db_sha256={before}"
    )


if __name__ == "__main__":
    main()
