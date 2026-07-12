"""Operational performance reports for the shared JRA logging database."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from .logging_store import DEFAULT_DB_PATH, LoggingStore
except ImportError:
    from logging_store import DEFAULT_DB_PATH, LoggingStore


@dataclass(frozen=True)
class ReportFilters:
    date_from: str | None = None
    date_to: str | None = None
    app_name: str | None = None
    model_version: str | None = None
    threshold: float | None = None
    venue: str | None = None
    distance_m: int | None = None


def _where(filters: ReportFilters, *, include_ev: bool = False) -> tuple[str, list[Any]]:
    clauses, params = [], []
    for expression, value in (
        ("r.race_date >= ?", filters.date_from), ("r.race_date <= ?", filters.date_to),
        ("pr.app_name = ?", filters.app_name), ("pr.model_version = ?", filters.model_version),
        ("r.venue = ?", filters.venue), ("r.distance_m = ?", filters.distance_m),
    ):
        if value is not None:
            clauses.append(expression)
            params.append(value)
    if include_ev and filters.threshold is not None:
        clauses.append("e.threshold = ?")
        params.append(filters.threshold)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _one(conn: sqlite3.Connection, sql: str, params=()) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _rate(numerator: float | int | None, denominator: float | int | None) -> float | None:
    return round(100.0 * float(numerator or 0) / float(denominator), 2) if denominator else None


def generate_report(db_path: str | Path = DEFAULT_DB_PATH, *, filters: ReportFilters | None = None,
                    period: str = "day") -> dict[str, Any]:
    filters = filters or ReportFilters()
    LoggingStore(db_path).initialize()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        base_where, base_params = _where(filters)
        ev_where, ev_params = _where(filters, include_ev=True)
        summary = _one(conn, f"""SELECT COUNT(DISTINCT pr.prediction_run_id) AS runs,
            COUNT(DISTINCT CASE WHEN pr.status='failed' THEN pr.prediction_run_id END) AS failed_runs,
            COUNT(DISTINCT p.race_id || '|' || p.horse_id) AS predicted_runners,
            COUNT(DISTINCT CASE WHEN rr.horse_id IS NOT NULL THEN p.race_id || '|' || p.horse_id END) AS matched_runners,
            COUNT(DISTINCT p.race_id) AS races
            FROM prediction_runs pr JOIN predictions p ON p.prediction_run_id=pr.prediction_run_id
            LEFT JOIN races r ON r.race_id=p.race_id
            LEFT JOIN race_results rr ON rr.race_id=p.race_id AND rr.horse_id=p.horse_id
            {base_where}""", base_params)
        summary["unmatched_runners"] = summary.get("predicted_runners", 0) - summary.get("matched_runners", 0)
        summary["match_rate_pct"] = _rate(summary.get("matched_runners"), summary.get("predicted_runners"))

        ev = _one(conn, f"""SELECT COUNT(*) AS evaluations,
            SUM(CASE WHEN e.decision='alert' THEN 1 ELSE 0 END) AS alerts,
            SUM(CASE WHEN e.decision='alert' AND rr.horse_id IS NOT NULL THEN 1 ELSE 0 END) AS settled_alerts,
            SUM(CASE WHEN e.decision='alert' AND rr.finish_position=1 THEN 1 ELSE 0 END) AS win_hits,
            SUM(CASE WHEN e.decision='alert' AND rr.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS place_hits,
            SUM(CASE WHEN e.decision='alert' THEN COALESCE(rr.win_payout,0) ELSE 0 END) AS win_return,
            SUM(CASE WHEN e.decision='alert' THEN COALESCE(rr.place_payout,0) ELSE 0 END) AS place_return
            FROM ev_evaluations e JOIN prediction_runs pr ON pr.prediction_run_id=e.prediction_run_id
            LEFT JOIN races r ON r.race_id=e.race_id
            LEFT JOIN race_results rr ON rr.race_id=e.race_id AND rr.horse_id=e.horse_id
            {ev_where}""", ev_params)
        ev["win_hit_rate_pct"] = _rate(ev.get("win_hits"), ev.get("settled_alerts"))
        ev["place_hit_rate_pct"] = _rate(ev.get("place_hits"), ev.get("settled_alerts"))
        ev["win_roi_pct"] = _rate(ev.get("win_return"), (ev.get("settled_alerts") or 0) * 100)
        ev["place_roi_pct"] = _rate(ev.get("place_return"), (ev.get("settled_alerts") or 0) * 100)

        notification = _one(conn, f"""SELECT COUNT(*) AS total,
            SUM(CASE WHEN n.status='sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN n.status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN n.status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN n.status='suppressed' THEN 1 ELSE 0 END) AS suppressed
            FROM notifications n JOIN ev_evaluations e ON e.ev_evaluation_id=n.ev_evaluation_id
            JOIN prediction_runs pr ON pr.prediction_run_id=e.prediction_run_id
            LEFT JOIN races r ON r.race_id=e.race_id {ev_where}""", ev_params)
        notification["failure_rate_pct"] = _rate(notification.get("failed"), notification.get("total"))
        quality = _one(conn, f"""SELECT COUNT(*) AS odds_snapshots,
            SUM(CASE WHEN win_odds IS NULL THEN 1 ELSE 0 END) AS missing_win_odds,
            SUM(CASE WHEN is_stale=1 THEN 1 ELSE 0 END) AS stale_odds FROM odds_snapshots o
            LEFT JOIN prediction_runs pr ON pr.prediction_run_id=o.fetch_id
            LEFT JOIN races r ON r.race_id=o.race_id {base_where}""", base_params)
        quality["missing_odds_rate_pct"] = _rate(quality.get("missing_win_odds"), quality.get("odds_snapshots"))

        period_expr = {
            "day": "r.race_date", "week": "strftime('%Y-W%W', substr(r.race_date,1,4)||'-'||substr(r.race_date,5,2)||'-'||substr(r.race_date,7,2))",
            "month": "substr(r.race_date,1,6)",
        }[period]
        period_rows = [dict(row) for row in conn.execute(f"""SELECT {period_expr} AS period,
            COUNT(CASE WHEN e.decision='alert' AND rr.horse_id IS NOT NULL THEN 1 END) AS settled_alerts,
            SUM(CASE WHEN e.decision='alert' AND rr.finish_position=1 THEN 1 ELSE 0 END) AS win_hits,
            SUM(CASE WHEN e.decision='alert' THEN COALESCE(rr.win_payout,0) ELSE 0 END) AS win_return,
            ROUND(AVG(CASE WHEN e.decision='alert' THEN e.ev END),3) AS average_ev
            FROM ev_evaluations e JOIN prediction_runs pr ON pr.prediction_run_id=e.prediction_run_id
            LEFT JOIN races r ON r.race_id=e.race_id
            LEFT JOIN race_results rr ON rr.race_id=e.race_id AND rr.horse_id=e.horse_id
            {ev_where} GROUP BY {period_expr} HAVING period IS NOT NULL ORDER BY period""", ev_params)]
        for row in period_rows:
            row["win_hit_rate_pct"] = _rate(row["win_hits"], row["settled_alerts"])
            row["win_roi_pct"] = _rate(row["win_return"], row["settled_alerts"] * 100)

        calibration = [dict(row) for row in conn.execute(f"""SELECT
            MIN(90, CAST(COALESCE(p.calibrated_win_probability,p.raw_win_probability)*10 AS INTEGER)*10) AS probability_band_pct,
            COUNT(*) AS samples, ROUND(AVG(COALESCE(p.calibrated_win_probability,p.raw_win_probability))*100,2) AS predicted_pct,
            ROUND(AVG(CASE WHEN rr.finish_position=1 THEN 1.0 ELSE 0.0 END)*100,2) AS actual_win_pct
            FROM predictions p JOIN prediction_runs pr ON pr.prediction_run_id=p.prediction_run_id
            LEFT JOIN races r ON r.race_id=p.race_id
            JOIN race_results rr ON rr.race_id=p.race_id AND rr.horse_id=p.horse_id
            {base_where + (' AND ' if base_where else ' WHERE ') + 'COALESCE(p.calibrated_win_probability,p.raw_win_probability) IS NOT NULL'}
            GROUP BY probability_band_pct ORDER BY probability_band_pct""", base_params)]

        drift = _one(conn, f"""SELECT COUNT(*) AS samples,
            ROUND(AVG(rr.final_win_odds / NULLIF(o.win_odds,0)),3) AS average_final_to_observed_ratio
            FROM ev_evaluations e JOIN prediction_runs pr ON pr.prediction_run_id=e.prediction_run_id
            JOIN odds_snapshots o ON o.odds_snapshot_id=e.odds_snapshot_id
            LEFT JOIN races r ON r.race_id=e.race_id
            JOIN race_results rr ON rr.race_id=e.race_id AND rr.horse_id=e.horse_id
            {ev_where + (' AND ' if ev_where else ' WHERE ') + 'e.decision=\'alert\' AND rr.final_win_odds IS NOT NULL AND o.win_odds IS NOT NULL'}""", ev_params)
        drift["note"] = "JRA結果ページで最終オッズを復元できる勝ち馬のみの参考値"

        model_rows = [dict(row) for row in conn.execute(f"""SELECT pr.app_name,pr.model_version,
            COUNT(DISTINCT pr.prediction_run_id) AS runs,COUNT(DISTINCT p.race_id) AS races,
            COUNT(DISTINCT CASE WHEN rr.horse_id IS NOT NULL THEN p.race_id||'|'||p.horse_id END) AS matched_runners
            FROM prediction_runs pr JOIN predictions p ON p.prediction_run_id=pr.prediction_run_id
            LEFT JOIN races r ON r.race_id=p.race_id
            LEFT JOIN race_results rr ON rr.race_id=p.race_id AND rr.horse_id=p.horse_id
            {base_where} GROUP BY pr.app_name,pr.model_version ORDER BY pr.app_name,pr.model_version""", base_params)]
        return {"filters": asdict(filters), "period": period, "summary": summary, "ev": ev,
                "notifications": notification, "data_quality": quality, "odds_drift": drift,
                "calibration": calibration, "by_period": period_rows, "by_model": model_rows}
    finally:
        conn.close()


def format_text(report: dict[str, Any]) -> str:
    s, e, n, q = report["summary"], report["ev"], report["notifications"], report["data_quality"]
    pct = lambda value: "n/a" if value is None else f"{value}%"
    lines = ["JRA 共通ログ 成績レポート",
             f"予測: {s.get('races') or 0}レース / {s.get('predicted_runners') or 0}頭 / 照合率 {pct(s.get('match_rate_pct'))}",
             f"EV通知: 照合済み{e.get('settled_alerts') or 0}件 / 単勝的中率 {pct(e.get('win_hit_rate_pct'))} / 単勝回収率 {pct(e.get('win_roi_pct'))}",
             f"通知: 成功{n.get('sent') or 0} / 失敗{n.get('failed') or 0} / 保留{n.get('pending') or 0}",
             f"品質: オッズ欠損 {q.get('missing_win_odds') or 0}/{q.get('odds_snapshots') or 0} ({pct(q.get('missing_odds_rate_pct'))})", ""]
    lines.append("期間 | 購入数 | 単勝的中 | 的中率 | 単勝回収率 | 平均EV")
    for row in report["by_period"]:
        lines.append(f"{row['period']} | {row['settled_alerts']} | {row['win_hits']} | {row['win_hit_rate_pct']}% | {row['win_roi_pct']}% | {row['average_ev']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report JRA prediction, EV, result, and notification performance")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH)); parser.add_argument("--period", choices=("day","week","month"), default="day")
    parser.add_argument("--from", dest="date_from"); parser.add_argument("--to", dest="date_to")
    parser.add_argument("--app"); parser.add_argument("--model-version"); parser.add_argument("--threshold", type=float)
    parser.add_argument("--venue"); parser.add_argument("--distance", type=int)
    parser.add_argument("--format", choices=("text","json","csv"), default="text"); parser.add_argument("--output")
    args = parser.parse_args()
    filters = ReportFilters(args.date_from, args.date_to, args.app, args.model_version,
                            args.threshold, args.venue, args.distance)
    report = generate_report(args.db, filters=filters, period=args.period)
    if args.format == "json":
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
    elif args.format == "csv":
        from io import StringIO
        buf = StringIO(); fields = ("period","settled_alerts","win_hits","win_hit_rate_pct","win_return","win_roi_pct","average_ev")
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(report["by_period"]); rendered = buf.getvalue()
    else:
        rendered = format_text(report)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8-sig" if args.format == "csv" else "utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
