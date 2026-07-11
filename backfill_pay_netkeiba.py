"""
組み合わせ馬券払戻 (馬連/ワイド/三連複等) の埋め戻し
======================================================
組み合わせ馬券のEVバックテスト用に、netkeiba 払戻テーブルから全券種の
払戻を取得し ability.db の race_payouts テーブルへ保存する。

  race_payouts(date, place, r, bet_type, combo, pay)
    - combo は "3-11" (馬連/ワイド/三連複: 昇順) / "11>3" (馬単/三連単: 着順)
    - ワイドは1レース3行、複勝は最大3行
  - 検証: 単勝払戻が runs.win_pay と一致しないレースはスキップ
  - 冪等: 馬連行が既にあるレースはスキップ (再実行安全)

【使い方】
  python backfill_pay_netkeiba.py --from 20250101 --to 20260630
  python backfill_pay_netkeiba.py --dry-run --limit 3
"""
import argparse
import os
import sqlite3
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ability.db")
SLEEP_SEC = 1.0

from backfill_fukusho_netkeiba import fetch_race_ids, _split_br, NK_PLACE, UA  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

SORTED_TYPES = {"複勝": False, "枠連": True, "馬連": True, "ワイド": True, "三連複": True}
ORDERED_TYPES = {"馬単", "三連単"}


def fetch_all_payouts(race_id, session):
    """成績ページ → [(bet_type, combo, pay)] (単勝含む全券種)"""
    res = session.get(f"https://db.netkeiba.com/race/{race_id}/", headers=UA, timeout=15)
    res.raise_for_status()
    res.encoding = "EUC-JP"
    out = []
    for tbl in BeautifulSoup(res.text, "html.parser").find_all("table", class_="pay_table_01"):
        for tr in tbl.find_all("tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if th is None or len(tds) < 2:
                continue
            label = th.get_text(strip=True)
            combos, pays = _split_br(tds[0]), _split_br(tds[1])
            for c, p in zip(combos, pays):
                try:
                    pay = float(p)
                except ValueError:
                    continue
                if label in ORDERED_TYPES:
                    combo = ">".join(x.strip() for x in c.split("→"))
                elif "-" in c:
                    nums = sorted(int(x) for x in c.split("-"))
                    combo = "-".join(str(x) for x in nums)
                else:
                    combo = c.strip()
                out.append((label, combo, pay))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="20250101")
    ap.add_argument("--to", dest="date_to", default="20260630")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS race_payouts (
        date TEXT, place TEXT, r INTEGER, bet_type TEXT, combo TEXT, pay REAL,
        PRIMARY KEY (date, place, r, bet_type, combo))""")

    # 対象: 馬連行が無いレース
    cur.execute("""
        SELECT date, place, r FROM runs
        WHERE date >= ? AND date <= ? AND r IS NOT NULL
        GROUP BY date, place, r""", (args.date_from, args.date_to))
    all_races = cur.fetchall()
    cur.execute("SELECT DISTINCT date, place, r FROM race_payouts WHERE bet_type = '馬連'")
    have = set(cur.fetchall())
    todo = [x for x in all_races if x not in have]
    print(f"対象: {len(todo)}/{len(all_races)}レース")
    if args.limit:
        todo = todo[:args.limit]

    by_date = {}
    for d, p, r in todo:
        by_date.setdefault(d, []).append((p, r))

    session = requests.Session()
    n_ok = n_skip = n_fail = 0
    fails = []
    done = 0
    for date8 in sorted(by_date):
        try:
            rid_map = fetch_race_ids(date8, session)
        except requests.RequestException as e:
            n_fail += len(by_date[date8])
            fails.append(f"{date8}: 一覧取得失敗 {e}")
            continue
        time.sleep(SLEEP_SEC)
        for place, rn in sorted(by_date[date8]):
            done += 1
            label = f"{date8} {place}{rn}R"
            rid = rid_map.get((place, int(rn)))
            if not rid:
                n_fail += 1
                fails.append(f"{label}: race_id未解決")
                continue
            try:
                payouts = fetch_all_payouts(rid, session)
            except requests.RequestException as e:
                n_fail += 1
                fails.append(f"{label}: 取得失敗 {e}")
                time.sleep(SLEEP_SEC)
                continue

            # 照合: 単勝払戻 = runs.win_pay
            tan = {c: p for t, c, p in payouts if t == "単勝"}
            cur.execute("""SELECT umaban, win_pay FROM runs
                           WHERE date = ? AND place = ? AND r = ? AND rank = 1""",
                        (date8, place, rn))
            verified = False
            for umaban, win_pay in cur.fetchall():
                try:
                    if str(umaban) in tan and abs(tan[str(umaban)] - float(str(win_pay).replace(",", ""))) < 1.0:
                        verified = True
                        break
                except (ValueError, TypeError):
                    continue
            if not verified or not any(t == "馬連" for t, _c, _p in payouts):
                n_skip += 1
                fails.append(f"{label}: 照合失敗/馬連なし → スキップ (race_id={rid})")
                time.sleep(SLEEP_SEC)
                continue

            if args.dry_run:
                print(f"[DRY] {label}: {[(t, c, p) for t, c, p in payouts if t in ('馬連', 'ワイド')]}")
            else:
                for t, c, p in payouts:
                    cur.execute("""INSERT OR REPLACE INTO race_payouts VALUES (?,?,?,?,?,?)""",
                                (date8, place, rn, t, c, p))
                conn.commit()
            n_ok += 1
            if done % 200 == 0:
                print(f"  ... {done}/{len(todo)} (成功{n_ok})")
            time.sleep(SLEEP_SEC)

    conn.close()
    print(f"\n完了: 成功{n_ok} / スキップ{n_skip} / 失敗{n_fail}")
    for f in fails[:10]:
        print("  -", f)
    if len(fails) > 10:
        print(f"  ... 他{len(fails) - 10}件")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
