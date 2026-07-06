"""
ability.db に複勝配当 (fukusho_pay) を netkeiba から埋め戻す
==============================================================
TARGET由来の ability.db には単勝払戻 (win_pay) しか無く、複勝回収率の
バックテストができない。netkeiba の成績ページ払戻テーブルから複勝配当を
取得し、runs テーブルに fukusho_pay 列 (3着内馬のみ、100円あたり円) を追加する。

  - 日別レース一覧 (db.netkeiba.com/race/list/YYYYMMDD/) から race_id を解決
    (kaisai情報が不要なため ability.db 全期間に適用可能)
  - 検証: 勝ち馬の netkeiba 単勝払戻が runs.win_pay と一致しないレースはスキップ
  - 冪等: 3着内馬の fukusho_pay が充填済みのレースはスキップ (再実行安全)

【使い方】
  python backfill_fukusho_netkeiba.py --from 20250101 --to 20260630
  python backfill_fukusho_netkeiba.py --dry-run --limit 3
"""
import argparse
import os
import re
import sqlite3
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ability.db")

NK_PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
            "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SLEEP_SEC = 1.0


def _split_br(td):
    """<td>11<br>3<br>13</td> → ['11', '3', '13']"""
    parts = re.split(r"<br\s*/?>", td.decode_contents())
    return [re.sub(r"<[^>]+>", "", p).replace(",", "").strip() for p in parts if
            re.sub(r"<[^>]+>", "", p).strip()]


def fetch_race_ids(date8, session):
    """日別レース一覧 → {(場, R): race_id}"""
    res = session.get(f"https://db.netkeiba.com/race/list/{date8}/", headers=UA, timeout=15)
    res.raise_for_status()
    res.encoding = "EUC-JP"
    out = {}
    for a in BeautifulSoup(res.text, "html.parser").find_all("a", href=True):
        m = re.match(r"^/race/(\d{12})/$", a["href"])
        if not m:
            continue
        rid = m.group(1)
        place = NK_PLACE.get(rid[4:6])
        if place:
            out[(place, int(rid[10:12]))] = rid
    return out


def fetch_payouts(race_id, session):
    """成績ページ払戻 → (単勝{馬番:円}, 複勝{馬番:円})"""
    res = session.get(f"https://db.netkeiba.com/race/{race_id}/", headers=UA, timeout=15)
    res.raise_for_status()
    res.encoding = "EUC-JP"
    tan, fuku = {}, {}
    for tbl in BeautifulSoup(res.text, "html.parser").find_all("table", class_="pay_table_01"):
        for tr in tbl.find_all("tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if th is None or len(tds) < 2:
                continue
            label = th.get_text(strip=True)
            if label not in ("単勝", "複勝"):
                continue
            nums, pays = _split_br(tds[0]), _split_br(tds[1])
            target = tan if label == "単勝" else fuku
            for n, p in zip(nums, pays):
                try:
                    target[int(n)] = float(p)
                except ValueError:
                    continue
    return tan, fuku


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20250101")
    ap.add_argument("--to", dest="date_to", default="20260630")
    ap.add_argument("--limit", type=int, default=0, help="処理レース数上限 (0=無制限)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE runs ADD COLUMN fukusho_pay REAL")
        print("fukusho_pay 列を追加")
    except sqlite3.OperationalError:
        pass  # 既存

    # 対象: 3着内馬の fukusho_pay が未充填のレース
    cur.execute("""
        SELECT date, place, r,
               SUM(CASE WHEN rank <= 3 THEN 1 ELSE 0 END) AS n3,
               SUM(CASE WHEN rank <= 3 AND fukusho_pay IS NOT NULL THEN 1 ELSE 0 END) AS filled
        FROM runs WHERE date >= ? AND date <= ? AND r IS NOT NULL
        GROUP BY date, place, r""", (args.date_from, args.date_to))
    todo = [(d, p, r) for d, p, r, n3, filled in cur.fetchall() if n3 and filled < n3]
    dates = sorted({d for d, _, _ in todo})
    print(f"対象: {len(todo)}レース / {len(dates)}日")
    if args.limit:
        todo = todo[:args.limit]
    todo_set = {(d, p, r) for d, p, r in todo}

    session = requests.Session()
    n_ok = n_skip = n_fail = 0
    fails = []
    done = 0
    for date8 in dates:
        day_races = [(d, p, r) for (d, p, r) in todo_set if d == date8]
        if not day_races:
            continue
        try:
            rid_map = fetch_race_ids(date8, session)
        except requests.RequestException as e:
            n_fail += len(day_races)
            fails.append(f"{date8}: 一覧取得失敗 {e}")
            continue
        time.sleep(SLEEP_SEC)

        for d, place, rn in sorted(day_races):
            done += 1
            rid = rid_map.get((place, rn))
            label = f"{d} {place}{rn}R"
            if not rid:
                n_fail += 1
                fails.append(f"{label}: race_id未解決")
                continue
            try:
                tan, fuku = fetch_payouts(rid, session)
            except requests.RequestException as e:
                n_fail += 1
                fails.append(f"{label}: 取得失敗 {e}")
                time.sleep(SLEEP_SEC)
                continue

            # 検証: 勝ち馬の単勝払戻が win_pay と一致するか
            cur.execute("""SELECT umaban, win_pay FROM runs
                           WHERE date = ? AND place = ? AND r = ? AND rank = 1""",
                        (d, place, rn))
            verified = False
            for umaban, win_pay in cur.fetchall():
                try:
                    if umaban in tan and abs(tan[umaban] - float(str(win_pay).replace(",", ""))) < 1.0:
                        verified = True
                        break
                except (ValueError, TypeError):
                    continue
            if not verified or not fuku:
                n_skip += 1
                fails.append(f"{label}: 払戻照合失敗 → スキップ (race_id={rid})")
                time.sleep(SLEEP_SEC)
                continue

            if not args.dry_run:
                for umaban, pay in fuku.items():
                    cur.execute("""UPDATE runs SET fukusho_pay = ?
                                   WHERE date = ? AND place = ? AND r = ? AND umaban = ?""",
                                (pay, d, place, rn, umaban))
                conn.commit()
            else:
                print(f"[DRY] {label}: 複勝{len(fuku)}頭 {fuku} (race_id={rid})")
            n_ok += 1
            if done % 100 == 0:
                print(f"  ... {done}/{len(todo)} 処理済み (成功{n_ok})")
            time.sleep(SLEEP_SEC)

    conn.close()
    print(f"\n完了: 成功{n_ok} / 照合スキップ{n_skip} / 失敗{n_fail}")
    for f in fails[:15]:
        print("  -", f)
    if len(fails) > 15:
        print(f"  ... 他{len(fails) - 15}件")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
