"""SQLite prediction logging based EV notification performance report."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path(os.environ.get("JRA_LOG_DB", BASE_DIR / "data" / "jra_logging.db"))


def normalize_since(value: str | None) -> str:
    raw = str(value or "").strip().replace("-", "")
    if not raw:
        return "00000000"
    if len(raw) == 6 and raw.isdigit():  # 旧CLIとの互換: YYMMDD
        raw = "20" + raw
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("--since は YYYYMMDD (または旧形式YYMMDD) で指定してください")
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("--since が実在する日付ではありません") from exc
    return raw


def open_readonly(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"ロギングDBがありません: {path}")
    uri = f"file:{path.as_posix()}?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _bucket():
    return {"alerts": 0, "settled": 0, "pending": 0, "win": 0, "top3": 0,
            "tan_ret": 0.0, "fuku_ret": 0.0, "drift": []}


def _add_result(bucket, row):
    bucket["alerts"] += 1
    position = row.get("finish_position")
    if position is None:
        bucket["pending"] += 1
        return
    position = int(position)
    bucket["settled"] += 1
    if position == 1:
        bucket["win"] += 1
        bucket["tan_ret"] += float(row.get("win_payout") or 0)
    if position <= 3:
        bucket["top3"] += 1
        bucket["fuku_ret"] += float(row.get("place_payout") or 0)
    try:
        final_odds = float(row.get("final_win_odds"))
        alert_odds = float(row.get("odds_alert"))
        if final_odds > 0 and alert_odds > 0:
            bucket["drift"].append(final_odds / alert_odds)
    except (TypeError, ValueError):
        pass


def load_report(conn: sqlite3.Connection, since: str = "") -> dict:
    compact = normalize_since(since)
    rows = [dict(row) for row in conn.execute("""
        SELECT e.ev_evaluation_id,e.race_id,e.horse_id,e.evaluated_at,
               e.win_probability,e.ev,e.reason_json,o.win_odds AS odds_alert,
               rr.finish_position,rr.final_win_odds,rr.win_payout,rr.place_payout
        FROM ev_evaluations e
        JOIN odds_snapshots o ON o.odds_snapshot_id=e.odds_snapshot_id
        LEFT JOIN race_results rr ON rr.race_id=e.race_id AND rr.horse_id=e.horse_id
        WHERE e.decision='alert' AND substr(e.race_id,1,8) >= ?
        ORDER BY e.race_id,e.evaluated_at,e.ev_evaluation_id
    """, (compact,))]

    notifications = [dict(row) for row in conn.execute("""
        SELECT n.ev_evaluation_id,n.channel,n.status
        FROM notifications n
        JOIN ev_evaluations e ON e.ev_evaluation_id=n.ev_evaluation_id
        WHERE e.decision='alert' AND substr(e.race_id,1,8) >= ?
        ORDER BY n.ev_evaluation_id,n.channel
    """, (compact,))]
    notification_map = defaultdict(list)
    channel_status = defaultdict(Counter)
    for item in notifications:
        notification_map[item["ev_evaluation_id"]].append(item)
        channel_status[item["channel"]][item["status"]] += 1

    sections = defaultdict(_bucket)
    channel_sections = defaultdict(_bucket)
    for row in rows:
        try:
            reason = json.loads(row.get("reason_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            reason = {}
        stage = reason.get("stage")
        _add_result(sections["全体"], row)
        if stage is not None:
            _add_result(sections[f"{stage}分前"], row)
        for notice in notification_map.get(row["ev_evaluation_id"], []):
            if notice["status"] == "sent":
                _add_result(channel_sections[notice["channel"]], row)

    return {
        "since": compact,
        "alerts": len(rows),
        "sections": dict(sections),
        "channels": dict(channel_sections),
        "channel_status": {channel: dict(counts) for channel, counts in channel_status.items()},
    }


def _rate(value, denominator):
    return 100.0 * value / denominator if denominator else 0.0


def _section_line(label, bucket):
    drift = sum(bucket["drift"]) / len(bucket["drift"]) if bucket["drift"] else None
    drift_text = f"×{drift:.2f} (n={len(bucket['drift'])})" if drift is not None else "n/a"
    return (f"  {label:9s}| {bucket['alerts']:6d} | {bucket['settled']:6d} | "
            f"{_rate(bucket['win'], bucket['settled']):6.1f}% | "
            f"{_rate(bucket['top3'], bucket['settled']):6.1f}% | "
            f"{bucket['tan_ret']/bucket['settled'] if bucket['settled'] else 0:6.1f}% | "
            f"{bucket['fuku_ret']/bucket['settled'] if bucket['settled'] else 0:6.1f}% | "
            f"{drift_text:>14s} | "
            f"{bucket['pending']:6d}")


def render_report(report: dict) -> str:
    lines = [
        f"===== 期待値監視 実測レポート (alert {report['alerts']}件 / since {report['since']}) =====",
        "区分       | alert  | 照合済 | 単勝的中 | 複勝的中 | 単勝回収 | 複勝回収 | オッズドリフト | 結果待ち",
    ]
    sections = report["sections"]
    stage_keys = sorted((key for key in sections if key.endswith("分前")),
                        key=lambda value: int(value[:-2]), reverse=True)
    for key in [*stage_keys, "全体"]:
        if key in sections:
            lines.append(_section_line(key, sections[key]))

    lines.extend(["", "===== チャネル別 =====",
                  "channel   | sent/予約 (suppressed,failed) | 照合済 | 単勝回収 | 複勝回収 | 結果待ち"])
    channel_names = sorted(set(report["channel_status"]) | set(report["channels"]))
    for channel in channel_names:
        statuses = report["channel_status"].get(channel, {})
        sent = statuses.get("sent", 0)
        reserved = sum(statuses.values())
        bucket = report["channels"].get(channel, _bucket())
        lines.append(
            f"  {channel:8s}| {sent:4d}/{reserved:<4d} "
            f"({statuses.get('suppressed', 0):3d},{statuses.get('failed', 0):3d})       | "
            f"{bucket['settled']:6d} | "
            f"{bucket['tan_ret']/bucket['settled'] if bucket['settled'] else 0:6.1f}% | "
            f"{bucket['fuku_ret']/bucket['settled'] if bucket['settled'] else 0:6.1f}% | "
            f"{bucket['pending']:6d}")
    lines.extend(["", "[見方] 回収率は各alert馬へ単勝/複勝100円を均等購入した実測値。",
                  "オッズドリフトは確定オッズ/通知時オッズ (1.0未満は通知後に低下)。",
                  "race_results未取得分は結果待ちとして残り、レポート処理は継続します。"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="", help="集計開始日 YYYYMMDD (旧YYMMDDも可)")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        with open_readonly(args.db) as conn:
            report = load_report(conn, args.since)
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
