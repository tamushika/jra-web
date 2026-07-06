"""
血統 (父・母父) の netkeiba バックフィル — 2021年以降の全出走馬
================================================================
ability.db には父・母父が無く血統をML特徴量にできない。netkeiba から
2段階で収集し、Neon の horse_pedigree テーブル (馬名→父/母父/母) に蓄積する。

  Phase A: レース成績ページから 馬名→horse_id を収集 (名前の曖昧性なし)
           進捗は ability.db の nk_harvest_done / nk_horse_ids テーブルに記録
  Phase B: 馬ページ (db.netkeiba.com/horse/<id>) の血統表から 父/母父/母 を取得
           → pedigree_store.upsert_horses で Neon へ

規模: レースページ約1.9万 + 馬ページ約3.2万 ≈ 5万リクエスト。
**必ず --limit で区切って数晩に分けて実行すること** (連続失敗10回で
ブロックと判断して自動停止。再実行で続きから再開する冪等設計)。

【使い方】
  python backfill_pedigree_netkeiba.py --phase a --limit 3000   # ID収集を3000リクエスト分
  python backfill_pedigree_netkeiba.py --phase b --limit 3000   # 血統取得を3000頭分
  python backfill_pedigree_netkeiba.py --status                 # 進捗確認のみ
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
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

DB_PATH = os.path.join(BASE_DIR, "ability.db")
NK_PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
            "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DATE_FROM = "20210101"  # ML学習ウィンドウ
MAX_CONSEC_FAIL = 10


def ensure_work_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS nk_horse_ids
                    (horse_id TEXT PRIMARY KEY, horse TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nk_harvest_done (date TEXT PRIMARY KEY)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nk_horse ON nk_horse_ids(horse)")
    conn.commit()


class Fetcher:
    """連続失敗でブロックと判断して例外を投げる薄いラッパー"""
    def __init__(self, sleep_sec):
        self.session = requests.Session()
        self.sleep_sec = sleep_sec
        self.consec_fail = 0
        self.count = 0

    def get_soup(self, url):
        time.sleep(self.sleep_sec)
        self.count += 1
        try:
            res = self.session.get(url, headers=UA, timeout=15)
            res.raise_for_status()
            res.encoding = "EUC-JP"
            self.consec_fail = 0
            return BeautifulSoup(res.text, "html.parser")
        except requests.RequestException as e:
            self.consec_fail += 1
            if self.consec_fail >= MAX_CONSEC_FAIL:
                raise RuntimeError(
                    f"連続{MAX_CONSEC_FAIL}回失敗 — ブロックの可能性。数時間置いて再実行してください") from e
            return None


def phase_a(conn, fetcher, limit):
    """レース成績ページから horse_id を収集 (日付単位で進捗管理)"""
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT date FROM runs WHERE date >= ?
                   AND date NOT IN (SELECT date FROM nk_harvest_done)
                   ORDER BY date DESC""", (DATE_FROM,))
    dates = [r[0] for r in cur.fetchall()]
    print(f"Phase A: 未収集 {len(dates)}日 (このセッションの上限 {limit}リクエスト)")

    n_ids = 0
    for date8 in dates:
        if fetcher.count >= limit:
            break
        soup = fetcher.get_soup(f"https://db.netkeiba.com/race/list/{date8}/")
        if soup is None:
            continue
        rids = sorted({m.group(1) for a in soup.find_all("a", href=True)
                       for m in [re.match(r"^/race/(\d{12})/$", a["href"])] if m
                       and a["href"] and m.group(1)[4:6] in NK_PLACE})
        day_ok = True
        for rid in rids:
            if fetcher.count >= limit:
                day_ok = False
                break
            rsoup = fetcher.get_soup(f"https://db.netkeiba.com/race/{rid}/")
            if rsoup is None:
                day_ok = False
                continue
            tbl = rsoup.find("table", class_="race_table_01")
            if tbl is None:
                day_ok = False
                continue
            rows = []
            for a in tbl.find_all("a", href=True):
                m = re.match(r"^/horse/(\w+)/?$", a["href"])
                name = a.get_text(strip=True)
                if m and name:
                    rows.append((m.group(1), name))
            cur.executemany("INSERT OR REPLACE INTO nk_horse_ids VALUES (?, ?)", rows)
            n_ids += len(rows)
        if day_ok:
            cur.execute("INSERT OR IGNORE INTO nk_harvest_done VALUES (?)", (date8,))
        conn.commit()
        print(f"  {date8}: {len(rids)}レース {'完了' if day_ok else '(一部のみ・再実行で継続)'}")
    print(f"Phase A: 今回 {n_ids}件のIDを登録 (リクエスト {fetcher.count})")


def parse_blood(soup):
    """馬ページの blood_table → (父, 母, 母父)。
    tdの並び: [父, 父父, 父母, 母, 母父, 母母]"""
    tbl = soup.find("table", class_="blood_table")
    if tbl is None:
        return None
    tds = [td.get_text(strip=True).split("\n")[0].strip() for td in tbl.find_all("td")]
    if len(tds) < 5:
        return None
    clean = [re.sub(r"\s*\d{4}.*$", "", t) for t in tds]  # 生年等の付記を除去
    return clean[0], clean[3], clean[4]


def phase_b(conn, fetcher, limit):
    """血統未収集の馬 (直近出走順) の馬ページから 父/母父 を取得 → Neonへ"""
    import pedigree_store
    have = set()
    pconn = pedigree_store._get_conn()
    if pconn is None:
        print("[ERROR] Neon未接続 (DATABASE_URL を確認)")
        return
    try:
        pedigree_store.ensure_table(pconn)
        pcur = pconn.cursor()
        pcur.execute("SELECT horse FROM horse_pedigree")
        have = {r["horse"] for r in pcur.fetchall()}
    finally:
        pconn.close()

    cur = conn.cursor()
    cur.execute("""
        SELECT r.horse, i.horse_id, MAX(r.date) AS last_run
        FROM runs r JOIN nk_horse_ids i ON i.horse = r.horse
        WHERE r.date >= ? GROUP BY r.horse ORDER BY last_run DESC""", (DATE_FROM,))
    targets = [(h, hid) for h, hid, _ in cur.fetchall() if h not in have]
    cur.execute("SELECT COUNT(DISTINCT horse) FROM runs WHERE date >= ?", (DATE_FROM,))
    total = cur.fetchone()[0]
    print(f"Phase B: 対象 {total}頭 / ID解決済み残り {len(targets)}頭 / 取得済み {len(have)}頭")

    batch, n_ok, n_ng = [], 0, 0
    for horse, hid in targets:
        if fetcher.count >= limit:
            break
        soup = fetcher.get_soup(f"https://db.netkeiba.com/horse/{hid}/")
        blood = parse_blood(soup) if soup else None
        if blood is None:
            n_ng += 1
            continue
        sire, dam, bms = blood
        batch.append({"name": horse, "sire": sire, "bms": bms, "dam": dam})
        n_ok += 1
        if len(batch) >= 50:
            pedigree_store.upsert_horses(batch)
            batch = []
            print(f"  ... {n_ok}頭取得済み (リクエスト {fetcher.count})")
    if batch:
        pedigree_store.upsert_horses(batch)
    print(f"Phase B: 今回 {n_ok}頭登録 / 失敗 {n_ng} (リクエスト {fetcher.count})")


def show_status(conn):
    import pedigree_store
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM nk_harvest_done")
    days_done = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT date) FROM runs WHERE date >= ?", (DATE_FROM,))
    days_all = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM nk_horse_ids")
    ids = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT horse) FROM runs WHERE date >= ?", (DATE_FROM,))
    total = cur.fetchone()[0]
    stats = pedigree_store.coverage_stats()
    print(f"Phase A (ID収集): {days_done}/{days_all}日 完了, {ids}件のID")
    print(f"Phase B (血統):   Neon horse_pedigree {stats[0] if stats else '?'}頭 "
          f"/ 対象{total}頭 (2021年以降出走馬)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["a", "b", "all"], default="all")
    ap.add_argument("--limit", type=int, default=3000, help="このセッションのリクエスト上限")
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--status", action="store_true", help="進捗表示のみ")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_work_tables(conn)
    if args.status:
        show_status(conn)
        conn.close()
        return

    fetcher = Fetcher(args.sleep)
    try:
        if args.phase in ("a", "all"):
            phase_a(conn, fetcher, args.limit)
        if args.phase in ("b", "all") and fetcher.count < args.limit:
            phase_b(conn, fetcher, args.limit)
    except RuntimeError as e:
        print(f"\n[STOP] {e}")
    finally:
        show_status(conn)
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
