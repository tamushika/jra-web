"""
Neon races テーブルの horse_odds (全馬の確定単勝オッズ) を netkeiba から埋め戻す
=================================================================================
背景: jra_db_updater が読む JRA成績ページには勝ち馬の払戻しか無く、
      勝ち馬以外のオッズが欠損している (ML再学習の ln_odds 特徴量に必須)。
netkeiba の成績ページ (db.netkeiba.com/race/<race_id>/) は全馬の単勝オッズを
持つため、これを取得して horse_odds 列を更新する。

  - 対象: date > 251228 (Neon延伸期間) のうち horse_odds が未充填のレース
  - 冪等: 全馬充填済みのレースはスキップ (再実行安全)
  - 検証: 勝ち馬の netkeiba オッズ×100 が Neon の単勝払戻と一致しないレースは
    照合失敗として更新せずスキップ (race_id の組み立てミス防止)

【使い方】
  python backfill_odds_netkeiba.py --dry-run --limit 3   # 動作確認
  python backfill_odds_netkeiba.py                       # 全量実行 (1.2秒/レース)
【前提】 .env に DATABASE_URL (Neon)
"""
import argparse
import re
import sys
import time
import os

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import past_data_service as pds  # noqa: E402

PLACE_CODE = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
    "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10",
}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SLEEP_SEC = 1.2


def build_race_id(date6, place, kaisai, race_num):
    """'260425', '京都', '…3回京都1日…', 11 → '202608030111'"""
    code = PLACE_CODE.get(place)
    m = re.search(r"(\d+)回\S*?(\d+)日", str(kaisai or ""))
    if not code or not m:
        return None
    return f"20{date6[:2]}{code}{int(m.group(1)):02d}{int(m.group(2)):02d}{int(race_num):02d}"


def fetch_netkeiba_odds(race_id, session):
    """netkeiba成績ページ → {馬番(int): 単勝オッズ(float)}。取消馬('---')は含めない"""
    url = f"https://db.netkeiba.com/race/{race_id}/"
    res = session.get(url, headers=UA, timeout=15)
    res.raise_for_status()
    res.encoding = "EUC-JP"
    table = BeautifulSoup(res.text, "html.parser").find("table", class_="race_table_01")
    if table is None:
        return None
    rows = table.find_all("tr")
    header = [th.get_text(strip=True) for th in rows[0].find_all("th")]
    try:
        i_uma = header.index("馬番")
        i_odds = header.index("単勝")
    except ValueError:
        return None
    odds_map = {}
    for tr in rows[1:]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) <= max(i_uma, i_odds):
            continue
        try:
            odds_map[int(tds[i_uma])] = float(tds[i_odds])
        except ValueError:
            continue  # 取消・除外 ('---') 等
    return odds_map or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="251228", help="対象開始日 (Neon形式 YYMMDD, これより後)")
    ap.add_argument("--limit", type=int, default=0, help="処理レース数上限 (0=無制限)")
    ap.add_argument("--dry-run", action="store_true", help="更新せず取得・照合のみ")
    args = ap.parse_args()

    conn = pds.get_db_connection(API_DIR)
    if conn is None or not getattr(conn, "is_pg", False):
        print("[ERROR] Neon未接続 (DATABASE_URL を確認)")
        sys.exit(1)
    cur = conn.cursor()
    cur.execute('''
        SELECT date, place, race_num, MIN(kaisai) AS kaisai,
               COUNT(*) AS n, COUNT(horse_odds) AS n_filled
        FROM races
        WHERE "馬名" IS NOT NULL AND date > %s AND race_num IS NOT NULL
        GROUP BY date, place, race_num
        ORDER BY date, place, race_num
    ''', (args.since,))
    races = [dict(r) for r in cur.fetchall()]
    todo = [r for r in races if r["n_filled"] < r["n"]]
    print(f"対象レース: {len(todo)}/{len(races)} (充填済みスキップ {len(races) - len(todo)})")
    if args.limit:
        todo = todo[:args.limit]

    session = requests.Session()
    n_ok = n_skip = n_fail = 0
    fails = []
    for i, race in enumerate(todo):
        date6, place, rn = race["date"], race["place"], int(race["race_num"])
        label = f"{date6} {place}{rn}R"
        race_id = build_race_id(date6, place, race["kaisai"], rn)
        if not race_id:
            n_fail += 1
            fails.append(f"{label}: race_id組み立て失敗 (kaisai='{race['kaisai']}')")
            continue
        try:
            odds_map = fetch_netkeiba_odds(race_id, session)
        except requests.RequestException as e:
            odds_map = None
            fails.append(f"{label}: 取得エラー {e}")
        if not odds_map:
            n_fail += 1
            if not fails or not fails[-1].startswith(label):
                fails.append(f"{label}: テーブル取得失敗 (race_id={race_id})")
            time.sleep(SLEEP_SEC)
            continue

        # 照合: 勝ち馬の Neon 払戻 = netkeiba オッズ×100
        cur.execute('''SELECT horse_number, rank, odds FROM races
                       WHERE "馬名" IS NOT NULL AND date = %s AND place = %s AND race_num = %s''',
                    (date6, place, race["race_num"]))
        runners = [dict(r) for r in cur.fetchall()]
        winner = next((r for r in runners if str(r["rank"]).startswith("1") and float(r["rank"]) == 1.0), None)
        verified = False
        if winner:
            try:
                pay = float(str(winner["odds"]).replace(",", "").replace("円", ""))
                w_uma = int(float(winner["horse_number"]))
                nk = odds_map.get(w_uma)
                verified = nk is not None and abs(nk * 100.0 - pay) < 1.0
            except (ValueError, TypeError):
                pass
        if not verified:
            n_skip += 1
            fails.append(f"{label}: 勝ち馬オッズ照合失敗 → 更新スキップ (race_id={race_id})")
            time.sleep(SLEEP_SEC)
            continue

        if args.dry_run:
            print(f"[DRY] {label}: {len(odds_map)}頭分取得・照合OK (race_id={race_id})")
        else:
            for r in runners:
                try:
                    uma = int(float(r["horse_number"]))
                except (ValueError, TypeError):
                    continue
                if uma in odds_map:
                    cur.execute('''UPDATE races SET horse_odds = %s
                                   WHERE "馬名" IS NOT NULL AND date = %s AND place = %s
                                     AND race_num = %s AND horse_number = %s''',
                                (odds_map[uma], date6, place, race["race_num"], r["horse_number"]))
            conn._conn.commit()  # _PgConn ラッパーは commit 未公開
        n_ok += 1
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(todo)} 処理済み (成功{n_ok})")
        time.sleep(SLEEP_SEC)

    conn.close()
    print(f"\n完了: 成功{n_ok} / 照合スキップ{n_skip} / 失敗{n_fail}")
    for f in fails[:20]:
        print("  -", f)
    if len(fails) > 20:
        print(f"  ... 他{len(fails) - 20}件")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
