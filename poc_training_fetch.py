"""
T42 PoC: netkeiba 調教データ取得可能性の実地検証スクリプト
================================================================
docs/codex/SPEC-T42-training-data-poc.md の調査用。**本収集はしない。**
このスクリプトは「1レース分の調教ページを取得し構造化する」方法の
再現用サンプル (30馬×直近2走程度に留めること)。

【確認できた取得方法】
  1. db.netkeiba.com/race/list/<date8>/ でその日の race_id 一覧を取得
     (backfill_pedigree_netkeiba.py の Phase A と同じ手法を流用)
  2. race.netkeiba.com/race/oikiri.html?race_id=<race_id>&type=1 で
     そのレース全馬の調教履歴 (複数日分) を1ページで取得できる
     (馬ごとに個別リクエストする必要がない = ページ数はレース数と同じ)
  3. 無料アクセスでは1レースにつき先頭3頭分 (netkeiba編集部のピック
     アップ馬と推測) しか type=1 の詳細 (日付・コース・馬場・ラップ・
     併せ馬コメント・調教強度・評価) が見えない。それ以外の馬は
     「スーパープレミアムコース」(月額1,390円、年払いで月あたり1,159円)
     以上の会員登録が必要 (ページ内に明記されている)。
     — 詳細は docs/T42-training-poc-report.md 参照。

【使い方 (サンプル取得のみ・本収集禁止)】
  python poc_training_fetch.py --date 20260704 --place 福島 --race 12
"""
import argparse
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SLEEP_SEC = 1.2
NK_PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
            "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


def get(url, sleep=SLEEP_SEC):
    time.sleep(sleep)
    res = requests.get(url, headers=UA, timeout=15)
    res.raise_for_status()
    res.encoding = "EUC-JP" if "db.netkeiba.com" in url else "UTF-8"
    return BeautifulSoup(res.text, "html.parser")


def resolve_race_id(date8, place_name, race_no):
    """db.netkeiba.com/race/list/<date8>/ からそのレースの race_id を1件解決する。"""
    soup = get(f"https://db.netkeiba.com/race/list/{date8}/")
    place_code = next((c for c, n in NK_PLACE.items() if n == place_name), None)
    if place_code is None:
        raise ValueError(f"unknown place: {place_name}")
    target_suffix = f"{race_no:02d}"
    for a in soup.find_all("a", href=True):
        m = re.match(rf"^/race/(\d{{4}}{place_code}\d{{4}}{target_suffix})/$", a["href"])
        if m:
            return m.group(1)
    raise ValueError(f"race_id not found for {date8} {place_name} {race_no}R")


def parse_training_records(soup, race_id):
    records = []
    for horse_block in soup.select("td.Horse_Info"):
        name_a = horse_block.select_one(".Horse_Name a")
        if not name_a:
            continue
        horse_id = re.search(r"/horse/(\w+)", name_a["href"]).group(1)
        horse_name = name_a.get_text(strip=True)
        row = horse_block.find_parent("tr")
        # rowspan で束ねられた最初の行以降、同じ馬の追加調教日が続く tr が並ぶ
        block_rows = [row]
        sib = row.find_next_sibling("tr")
        while sib is not None and sib.find("td", class_="Horse_Info") is None \
                and sib.find("td", class_="Waku1") is None and sib.find(class_=re.compile("^Waku")) is None:
            block_rows.append(sib)
            sib = sib.find_next_sibling("tr")
        for r in block_rows:
            day_td = r.find("td", class_="Training_Day")
            if day_td is None:
                continue
            course_td = day_td.find_next_sibling("td")
            baba_td = course_td.find_next_sibling("td") if course_td else None
            times_td = r.find("td", class_="TrainingTimeData")
            comment = times_td.select_one(".Comment_Cell p") if times_td else None
            load_td = r.find("td", class_="TrainingLoad")
            critic_td = r.find("td", class_="Training_Critic")
            rank_td = r.find("td", class_=re.compile(r"^Rank_"))
            records.append({
                "race_id": race_id,
                "horse_id": horse_id,
                "horse_name": horse_name,
                "training_day": day_td.get_text(strip=True),
                "course": course_td.get_text(strip=True) if course_td else None,
                "baba": baba_td.get_text(strip=True) if baba_td else None,
                "laps": [li.get_text(" ", strip=True) for li in times_td.select("li")] if times_td else [],
                "comment": comment.get_text(strip=True) if comment else None,
                "load": load_td.get_text(strip=True) if load_td else None,
                "critic": critic_td.get_text(strip=True) if critic_td else None,
                "rank": rank_td.get_text(strip=True) if rank_td else None,
            })
    return records


def fetch_training_records(race_id):
    """race.netkeiba.com/race/oikiri.html?race_id=...&type=1 を構造化する。
    無料枠では先頭3頭分のみ詳細が取れる (それ以外は評価のみ or 非表示)。
    """
    soup = get(f"https://race.netkeiba.com/race/oikiri.html?race_id={race_id}&type=1")
    return parse_training_records(soup, race_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD")
    ap.add_argument("--place", help="例: 福島")
    ap.add_argument("--race", type=int, help="レース番号")
    ap.add_argument("--from-file", help="ネットに出ず、保存済みHTMLを再パースするだけのオフラインモード")
    ap.add_argument("--race-id", help="--from-file 使用時に付与する race_id (任意)")
    args = ap.parse_args()

    import json
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        soup = BeautifulSoup(html, "html.parser")
        recs = parse_training_records(soup, args.race_id or "")
    else:
        if not (args.date and args.place and args.race):
            ap.error("--date/--place/--race か --from-file のいずれかが必要")
        rid = resolve_race_id(args.date, args.place, args.race)
        print(f"race_id={rid}", file=sys.stderr)
        recs = fetch_training_records(rid)
    print(json.dumps(recs, ensure_ascii=False, indent=2))
