"""
2026年1〜4月に欠落した ability.db/runs を netkeiba から埋め戻す。

既存の (date, place, r) はレース単位で必ずスキップし、INSERTだけを行う。
Neonや2025年以前の行には触れない。

使い方:
  python backfill_runs_netkeiba.py --dry-run --limit 3
  python backfill_runs_netkeiba.py --from 20260101 --to 20260430
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
from backfill_fukusho_netkeiba import (  # noqa: E402
    UA,
    _split_br,
    fetch_race_ids,
)
from build_ability_db import parse_race_class  # noqa: E402
from extend_ability_from_neon import compute_pci  # noqa: E402


DB_PATH = os.path.join(BASE_DIR, "ability.db")
SLEEP_SEC = 1.2
VERIFY_FAILURE_LIMIT = 0.05
VERIFY_FAILURE_MIN_RACES = 20

RUN_COLUMNS = [
    "date", "place", "r", "race_name", "race_class", "horse", "sex", "age",
    "jockey", "kinryo", "total_horses", "umaban", "popularity", "rank",
    "track_type", "distance", "condition", "time_sec", "chakusa", "c4",
    "agari", "pci", "weight", "affi", "win_pay", "fukusho_pay",
]


class RacePageError(ValueError):
    """保存できないレースページ。kindで除外理由を区別する。"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _text(tag):
    return tag.get_text(" ", strip=True) if tag is not None else ""


def _number(value, converter=float):
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not match:
        return None
    try:
        return converter(match.group())
    except (TypeError, ValueError):
        return None


def _header_index(headers, *candidates, contains=False):
    normalized = [re.sub(r"\s+", "", value) for value in headers]
    for candidate in candidates:
        for index, value in enumerate(normalized):
            if value == candidate or (contains and candidate in value):
                return index
    return None


def _cell_text(cells, index):
    if index is None or index >= len(cells):
        return ""
    return _text(cells[index])


def _linked_text(cells, index):
    if index is None or index >= len(cells):
        return ""
    link = cells[index].find("a")
    return _text(link) if link is not None else _text(cells[index])


def _affiliation(value):
    text = str(value or "")
    if "[東]" in text or "［東］" in text or "美浦" in text:
        return "美浦"
    if "[西]" in text or "［西］" in text or "栗東" in text:
        return "栗東"
    if "[地]" in text or "［地］" in text:
        return "地方"
    if "[外]" in text or "［外］" in text:
        return "海外"
    return ""


def _parse_payouts(soup):
    tan, fukusho = {}, {}
    for table in soup.find_all("table", class_="pay_table_01"):
        for row in table.find_all("tr"):
            header = row.find("th")
            cells = row.find_all("td")
            if header is None or len(cells) < 2:
                continue
            label = _text(header).replace(" ", "")
            if label not in ("単勝", "複勝"):
                continue
            numbers = _split_br(cells[0])
            payments = _split_br(cells[1])
            target = tan if label == "単勝" else fukusho
            for horse_number, payment in zip(numbers, payments):
                try:
                    target[int(horse_number)] = float(payment)
                except (TypeError, ValueError):
                    continue
    return tan, fukusho


def _parse_race_header(soup):
    intro = soup.find("div", class_="data_intro")
    if intro is None:
        raise RacePageError("parse", "レースヘッダ(data_intro)がありません")
    heading = intro.find("h1") or soup.find("h1")
    race_name = _text(heading)
    intro_text = _text(intro)
    compact = re.sub(r"\s+", "", intro_text)

    if "障害" in compact or re.search(r"障\d{3,4}m", compact):
        raise RacePageError("obstacle", "障害レース")
    course = re.search(r"(芝|ダート|ダ)[^0-9/]{0,12}(\d{3,4})m", compact)
    if not course:
        raise RacePageError("parse", f"芝ダ・距離を解析できません: {intro_text}")
    surface = "ダート" if course.group(1) in ("ダ", "ダート") else "芝"
    distance = int(course.group(2))

    condition_match = re.search(
        r"(?:芝|ダート|ダ)[:：](良|稍重|重|不良)", compact
    )
    if not condition_match:
        raise RacePageError("parse", f"馬場状態を解析できません: {intro_text}")
    condition = {"稍重": "稍", "不良": "不"}.get(
        condition_match.group(1), condition_match.group(1)[:1]
    )
    return race_name, surface, distance, condition


def parse_race_html(html, race_date, place, race_number):
    """netkeiba成績HTMLをrunsの26列タプルへ変換し、払戻照合も行う。"""
    soup = BeautifulSoup(html, "html.parser")
    race_name, surface, distance, condition = _parse_race_header(soup)
    table = soup.find("table", class_="race_table_01")
    if table is None:
        raise RacePageError("parse", "成績テーブル(race_table_01)がありません")

    table_rows = table.find_all("tr")
    header_row = next((row for row in table_rows if row.find_all("th")), None)
    if header_row is None:
        raise RacePageError("parse", "成績テーブルの見出しがありません")
    headers = [_text(tag) for tag in header_row.find_all("th")]
    indexes = {
        "rank": _header_index(headers, "着順"),
        "umaban": _header_index(headers, "馬番"),
        "horse": _header_index(headers, "馬名"),
        "sex_age": _header_index(headers, "性齢"),
        "kinryo": _header_index(headers, "斤量"),
        "jockey": _header_index(headers, "騎手"),
        "time": _header_index(headers, "タイム"),
        "passing": _header_index(headers, "通過", contains=True),
        "agari": _header_index(headers, "上り", "上がり", contains=True),
        "odds": _header_index(headers, "単勝"),
        "popularity": _header_index(headers, "人気"),
        "weight": _header_index(headers, "馬体重"),
        "trainer": _header_index(headers, "調教師", contains=True),
    }
    missing = [name for name, index in indexes.items() if index is None]
    if missing:
        raise RacePageError(
            "parse", f"成績テーブルの必須列がありません: {missing} / headers={headers}"
        )

    parsed = []
    for table_row in table_rows:
        cells = table_row.find_all("td")
        if not cells:
            continue
        rank_text = _cell_text(cells, indexes["rank"])
        if any(status in rank_text for status in ("取消", "除外", "中止", "失格")):
            continue
        rank = _number(rank_text, int)
        if rank is None or rank >= 90:
            continue

        umaban = _number(_cell_text(cells, indexes["umaban"]), int)
        horse = _linked_text(cells, indexes["horse"]).strip()
        sex_age = _cell_text(cells, indexes["sex_age"])
        sex_match = re.search(r"[牡牝セ]", sex_age)
        age = _number(sex_age, int)
        odds = _number(_cell_text(cells, indexes["odds"]), float)
        if not umaban or not horse or odds is None or odds <= 0:
            raise RacePageError(
                "parse", f"馬番・馬名・単勝オッズを解析できません: rank={rank}"
            )

        passing_numbers = re.findall(
            r"\d+", _cell_text(cells, indexes["passing"])
        )
        weight = _number(_cell_text(cells, indexes["weight"]), int)
        time_sec = scoring.parse_run_time(_cell_text(cells, indexes["time"]))
        agari = _number(_cell_text(cells, indexes["agari"]), float)
        parsed.append({
            "rank": rank,
            "umaban": umaban,
            "horse": horse,
            "sex": sex_match.group() if sex_match else "",
            "age": age,
            "jockey": _linked_text(cells, indexes["jockey"]).strip(),
            "kinryo": _number(_cell_text(cells, indexes["kinryo"]), float),
            "time_sec": time_sec,
            "c4": int(passing_numbers[-1]) if passing_numbers else None,
            "agari": agari,
            "popularity": _number(
                _cell_text(cells, indexes["popularity"]), int
            ),
            "weight": weight,
            "affi": _affiliation(_cell_text(cells, indexes["trainer"])),
            "odds": odds,
        })

    if not parsed:
        raise RacePageError("parse", "有効な完走馬がありません")
    winners = [runner for runner in parsed if runner["rank"] == 1]
    if len(winners) != 1:
        raise RacePageError("parse", f"勝ち馬が一意ではありません: {len(winners)}頭")

    tan, fukusho = _parse_payouts(soup)
    winner = winners[0]
    winner_payment = tan.get(winner["umaban"])
    if winner_payment is None:
        raise RacePageError("verify", "単勝払戻に勝ち馬がありません")
    if abs(winner["odds"] * 100.0 - winner_payment) > 10.0:
        raise RacePageError(
            "verify",
            f"単勝照合不一致: {winner['umaban']}番 "
            f"odds={winner['odds']} / pay={winner_payment}",
        )

    total_horses = len(parsed)
    race_class = parse_race_class(race_name)
    output = []
    for runner in parsed:
        if runner["rank"] == 1:
            win_pay = f"{winner_payment:g}"
        else:
            win_pay = f"({runner['odds']:g})"
        output.append((
            race_date,
            place,
            int(race_number),
            race_name,
            race_class,
            runner["horse"],
            runner["sex"],
            runner["age"],
            runner["jockey"],
            runner["kinryo"],
            total_horses,
            runner["umaban"],
            runner["popularity"],
            runner["rank"],
            surface,
            distance,
            condition,
            runner["time_sec"],
            None,
            runner["c4"],
            runner["agari"],
            compute_pci(runner["time_sec"], runner["agari"], distance),
            runner["weight"],
            runner["affi"],
            win_pay,
            fukusho.get(runner["umaban"]),
        ))
    return output


def fetch_race_html(race_id, session):
    response = session.get(
        f"https://db.netkeiba.com/race/{race_id}/", headers=UA, timeout=20
    )
    response.raise_for_status()
    response.encoding = "EUC-JP"
    return response.text


def _date_range(date_from, date_to):
    start = datetime.strptime(date_from, "%Y%m%d").date()
    end = datetime.strptime(date_to, "%Y%m%d").date()
    if start > end:
        raise ValueError("--from は --to 以下にしてください")
    while start <= end:
        yield start.strftime("%Y%m%d")
        start += timedelta(days=1)


def _existing_races(connection, date_from, date_to):
    return set(connection.execute(
        "SELECT DISTINCT date, place, r FROM runs WHERE date >= ? AND date <= ?",
        (date_from, date_to),
    ))


def insert_race(connection, rows):
    """レース単位でINSERTしてcommitする。既存レースなら何もしない。"""
    if not rows:
        return False
    date_index = RUN_COLUMNS.index("date")
    place_index = RUN_COLUMNS.index("place")
    race_index = RUN_COLUMNS.index("r")
    key = (rows[0][date_index], rows[0][place_index], rows[0][race_index])
    exists = connection.execute(
        "SELECT 1 FROM runs WHERE date = ? AND place = ? AND r = ? LIMIT 1", key
    ).fetchone()
    if exists:
        return False
    placeholders = ",".join("?" for _ in RUN_COLUMNS)
    columns = ",".join(RUN_COLUMNS)
    with connection:
        connection.executemany(
            f"INSERT INTO runs ({columns}) VALUES ({placeholders})", rows
        )
    return True


def run(date_from, date_to, db_path=DB_PATH, dry_run=False, limit=0):
    connection = sqlite3.connect(db_path)
    schema_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(runs)")
    }
    missing_columns = [column for column in RUN_COLUMNS if column not in schema_columns]
    if missing_columns:
        connection.close()
        raise RuntimeError(f"runsに必要な列がありません: {missing_columns}")

    existing = _existing_races(connection, date_from, date_to)
    print(
        f"期間: {date_from}-{date_to} / 既存{len(existing)}レースは保護してスキップ"
    )
    if dry_run:
        print("DRY-RUN: ability.dbへの書き込みは行いません")

    session = requests.Session()
    stats = {
        "list_days": 0,
        "race_pages": 0,
        "inserted_races": 0,
        "inserted_rows": 0,
        "existing_skips": 0,
        "obstacle_skips": 0,
        "verify_failures": 0,
        "parse_failures": 0,
        "network_failures": 0,
    }
    failures = []
    stop = False

    for date8 in _date_range(date_from, date_to):
        if stop:
            break
        try:
            race_ids = fetch_race_ids(date8, session)
            stats["list_days"] += 1
        except requests.RequestException as exc:
            stats["network_failures"] += 1
            failures.append(f"{date8}: 一覧取得失敗 {exc}")
            time.sleep(SLEEP_SEC)
            continue
        time.sleep(SLEEP_SEC)

        for (place, race_number), race_id in sorted(race_ids.items()):
            key = (date8, place, int(race_number))
            if key in existing:
                stats["existing_skips"] += 1
                continue
            if limit and stats["race_pages"] >= limit:
                stop = True
                break

            label = f"{date8} {place}{race_number}R"
            stats["race_pages"] += 1
            try:
                html = fetch_race_html(race_id, session)
            except requests.RequestException as exc:
                stats["network_failures"] += 1
                failures.append(f"{label}: 成績取得失敗 {exc}")
                time.sleep(SLEEP_SEC)
                continue
            time.sleep(SLEEP_SEC)

            try:
                rows = parse_race_html(html, date8, place, race_number)
            except RacePageError as exc:
                if exc.kind == "obstacle":
                    stats["obstacle_skips"] += 1
                elif exc.kind == "verify":
                    stats["verify_failures"] += 1
                    failures.append(f"{label}: 払戻照合失敗 → 保存せず ({exc})")
                else:
                    stats["parse_failures"] += 1
                    failures.append(f"{label}: 解析失敗 → 保存せず ({exc})")
                continue

            if dry_run:
                sample = rows[0]
                print(
                    f"[DRY] {label}: {len(rows)}頭 / {sample[3]} / "
                    f"{sample[14]}{sample[15]}m {sample[16]} / 勝ち馬={sample[5]}"
                )
                stats["inserted_races"] += 1
                stats["inserted_rows"] += len(rows)
            elif insert_race(connection, rows):
                existing.add(key)
                stats["inserted_races"] += 1
                stats["inserted_rows"] += len(rows)
            else:
                stats["existing_skips"] += 1

            verified = stats["inserted_races"] + stats["verify_failures"]
            if (verified >= VERIFY_FAILURE_MIN_RACES
                    and stats["verify_failures"] / verified > VERIFY_FAILURE_LIMIT):
                failures.append(
                    "照合失敗率が5%を超えたため安全停止: "
                    f"{stats['verify_failures']}/{verified}"
                )
                stop = True
                break

            if stats["race_pages"] % 100 == 0:
                print(
                    f"  ... 成績{stats['race_pages']}件 / "
                    f"保存{stats['inserted_races']}R・{stats['inserted_rows']}行 / "
                    f"照合失敗{stats['verify_failures']}"
                )

    connection.close()
    verified = stats["inserted_races"] + stats["verify_failures"]
    failure_rate = (
        stats["verify_failures"] / verified if verified else 0.0
    )
    print("\n完了:")
    print(
        f"  一覧{stats['list_days']}日 / 成績{stats['race_pages']}ページ / "
        f"保存{stats['inserted_races']}レース・{stats['inserted_rows']}行"
    )
    print(
        f"  既存スキップ{stats['existing_skips']} / 障害{stats['obstacle_skips']} / "
        f"照合失敗{stats['verify_failures']} ({100 * failure_rate:.2f}%) / "
        f"解析失敗{stats['parse_failures']} / 通信失敗{stats['network_failures']}"
    )
    for failure in failures[:20]:
        print("  -", failure)
    if len(failures) > 20:
        print(f"  ... 他{len(failures) - 20}件")
    if stop and failure_rate > VERIFY_FAILURE_LIMIT:
        raise RuntimeError("照合失敗率超過により安全停止しました")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", default="20260101")
    parser.add_argument("--to", dest="date_to", default="20260430")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--limit", type=int, default=0, help="成績ページ処理上限 (0=無制限)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.date_from < "20260101" or args.date_to > "20260430":
        parser.error("T31の対象は20260101〜20260430だけです")
    if args.limit < 0:
        parser.error("--limit は0以上にしてください")
    run(args.date_from, args.date_to, args.db, args.dry_run, args.limit)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
