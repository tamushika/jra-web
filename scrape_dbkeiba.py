"""
db-keiba.com コース別ファクター統計スクレイパー
=================================================
各コースページ (例: https://db-keiba.com/sapporo-turf-1200/) から
year5_* (2021-2025集計) のファクター統計テーブルを取得し、
api/data_files/<venue_slug>/factors/<芝|ダート><距離>.csv に保存する。

【使い方】
  python scrape_dbkeiba.py                # 未取得コースのみ全取得
  python scrape_dbkeiba.py --only sapporo # スラッグ部分一致で対象を絞る
  python scrape_dbkeiba.py --force        # 既存CSVも上書き
  python scrape_dbkeiba.py --delay 2.0    # リクエスト間隔(秒)

【必要】 pip install requests beautifulsoup4
"""
import argparse
import csv
import os
import re
import sys
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://db-keiba.com/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "api", "data_files")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# db-keiba.com/main/ から抽出した実在スラッグ一覧 (2026-07確認・103コース)
SLUGS = [
    "sapporo-turf-1200", "sapporo-turf-1500", "sapporo-turf-1800", "sapporo-turf-2000",
    "sapporo-turf-2600", "sapporo-dirt-1000", "sapporo-dirt-1700", "sapporo-dirt-2400",
    "hakodate-turf-1000", "hakodate-turf-1200", "hakodate-turf-1800", "hakodate-turf-2000",
    "hakodate-turf-2600", "hakodate-dirt-1000", "hakodate-dirt-1700", "hakodate-dirt-2400",
    "fukushima-turf-1200", "fukushima-turf-1800", "fukushima-turf-2000", "fukushima-turf-2600",
    "fukushima-dirt-1150", "fukushima-dirt-1700", "fukushima-dirt-2400",
    "niigata-turf-1000", "niigata-turf-1200", "niigata-turf-1400", "niigata-turf-1600",
    "niigata-turf-1800", "niigata-turf-2000u", "niigata-turf-2000s", "niigata-turf-2200",
    "niigata-turf-2400", "niigata-dirt-1200", "niigata-dirt-1800", "niigata-dirt-2500",
    "tokyo-turf-1400", "tokyo-turf-1600", "tokyo-turf-1800", "tokyo-turf-2000",
    "tokyo-turf-2300", "tokyo-turf-2400", "tokyo-turf-2500", "tokyo-turf-3400",
    "tokyo-dirt-1300", "tokyo-dirt-1400", "tokyo-dirt-1600", "tokyo-dirt-2100",
    "nakayama-turf-1200", "nakayama-turf-1600", "nakayama-turf-1800", "nakayama-turf-2000",
    "nakayama-turf-2200", "nakayama-turf-2500", "nakayama-turf-3600",
    "nakayama-dirt-1200", "nakayama-dirt-1800", "nakayama-dirt-2400", "nakayama-dirt-2500",
    "chukyo-turf-1200", "chukyo-turf-1400", "chukyo-turf-1600", "chukyo-turf-2000",
    "chukyo-turf-2200", "chukyo-dirt-1200", "chukyo-dirt-1400", "chukyo-dirt-1800",
    "chukyo-dirt-1900",
    "kyoto-turf-1200", "kyoto-turf-1400u", "kyoto-turf-1400s", "kyoto-turf-1600u",
    "kyoto-turf-1600s", "kyoto-turf-1800", "kyoto-turf-2000", "kyoto-turf-2200",
    "kyoto-turf-2400", "kyoto-turf-3000", "kyoto-turf-3200",
    "kyoto-dirt-1200", "kyoto-dirt-1400", "kyoto-dirt-1800", "kyoto-dirt-1900",
    "hanshin-turf-1200", "hanshin-turf-1400", "hanshin-turf-1600", "hanshin-turf-1800",
    "hanshin-turf-2000", "hanshin-turf-2200", "hanshin-turf-2400", "hanshin-turf-2600",
    "hanshin-turf-3000",
    "hanshin-dirt-1200", "hanshin-dirt-1400", "hanshin-dirt-1800", "hanshin-dirt-2000",
    "kokura-turf-1200", "kokura-turf-1800", "kokura-turf-2000", "kokura-turf-2600",
    "kokura-dirt-1000", "kokura-dirt-1700", "kokura-dirt-2400",
]

# 取得カテゴリ (allall は baseline 用)
CATEGORIES = [
    "allall", "father_w", "jockey_w", "frame", "averunningstyle",
    "distance", "surface", "stable_trainer",
]

# CSV 列: テーブルヘッダ日本語 → キー
COL_MAP = {
    "1着": "n1", "2着": "n2", "3着": "n3", "着外": "out", "出走": "starts",
    "勝率": "win_rate", "連対": "quinella_rate", "複勝": "show_rate",
    "単回": "win_roi", "複回": "show_roi",
}
CSV_FIELDS = ["factor_type", "entity", "n1", "n2", "n3", "out", "starts",
              "win_rate", "quinella_rate", "show_rate", "win_roi", "show_roi"]

COUNT_KEYS = ["n1", "n2", "n3", "out", "starts"]
ROI_KEYS = ["win_roi", "show_roi"]


def norm_text(s):
    """NFKC正規化 + 空白除去"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s))).strip()


def parse_slug(slug):
    """スラッグ → (venue_slug, race_type, dist, variant)"""
    m = re.match(r"^([a-z]+)-(turf|dirt)-(\d+)([us]?)$", slug)
    if not m:
        raise ValueError(f"invalid slug: {slug}")
    venue, td, dist, var = m.groups()
    race_type = "芝" if td == "turf" else "ダート"
    return venue, race_type, int(dist), var or None


def fetch_page(slug, session, retries=2):
    url = BASE_URL + slug + "/"
    for attempt in range(retries + 1):
        try:
            res = session.get(url, headers=HEADERS, timeout=20)
            if res.status_code == 404:
                return None
            res.raise_for_status()
            return res.text
        except Exception as e:
            if attempt == retries:
                print(f"    [NG] fetch failed: {url} ({e})")
                return None
            time.sleep(2.0)


def _to_num(val, as_int):
    v = str(val).replace(",", "").replace("%", "").strip()
    if v in ("", "-", "ー"):
        return None
    try:
        return int(v) if as_int else float(v)
    except ValueError:
        return None


def parse_factor_tables(html, year="year5"):
    """ページHTML → {category: [row_dict, ...]} (year: 'year5'=2021-2025集計 / 'year1'=当年単年)"""
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for cat in CATEGORIES:
        table = soup.find("table", id=re.compile(rf"json-{year}_{cat}$"))
        if table is None:
            continue
        trs = table.find_all("tr")
        if len(trs) < 2:
            continue
        # 1行目 = ヘッダ (column-1 は空、以降がラベル)
        header_cells = [norm_text(td.get_text()) for td in trs[0].find_all("td")]
        col_keys = [COL_MAP.get(h) for h in header_cells]  # index0はNone(entity列)
        rows = []
        for tr in trs[1:]:
            tds = tr.find_all("td")
            if len(tds) != len(header_cells):
                continue
            entity = norm_text(tds[0].get_text())
            if not entity:
                continue
            row = {"entity": entity}
            for i, td in enumerate(tds[1:], start=1):
                key = col_keys[i] if i < len(col_keys) else None
                if key:
                    row[key] = _to_num(td.get_text(), as_int=key in COUNT_KEYS)
            rows.append(row)
        if rows:
            result[cat] = rows
    return result


def sanity_check(slug, tables):
    warns = []
    if "allall" not in tables:
        warns.append("allall(基準値)テーブルなし")
    frame_rows = tables.get("frame", [])
    if frame_rows and len(frame_rows) != 8:
        warns.append(f"frame行数={len(frame_rows)} (期待8)")
    for cat in CATEGORIES:
        if cat not in tables:
            warns.append(f"{cat}なし")
    for w in warns:
        print(f"    [WARN] {slug}: {w}")
    return "allall" in tables  # baselineが無ければ不合格


def merge_tables(table_list):
    """内外回りバリアントを合算。件数は加算、率は再計算、ROIは出走数加重平均。"""
    if len(table_list) == 1:
        return table_list[0]
    merged = {}
    for cat in CATEGORIES:
        acc = {}  # entity -> {counts..., roi加重合計用}
        order = []
        for tables in table_list:
            for row in tables.get(cat, []):
                ent = row["entity"]
                if ent not in acc:
                    acc[ent] = {k: 0 for k in COUNT_KEYS}
                    acc[ent].update({f"_{k}_wsum": 0.0 for k in ROI_KEYS})
                    acc[ent].update({f"_{k}_w": 0 for k in ROI_KEYS})
                    order.append(ent)
                a = acc[ent]
                for k in COUNT_KEYS:
                    a[k] += row.get(k) or 0
                starts = row.get("starts") or 0
                for k in ROI_KEYS:
                    if row.get(k) is not None and starts > 0:
                        a[f"_{k}_wsum"] += row[k] * starts
                        a[f"_{k}_w"] += starts
        rows = []
        for ent in order:
            a = acc[ent]
            starts = a["starts"]
            row = {"entity": ent}
            row.update({k: a[k] for k in COUNT_KEYS})
            if starts > 0:
                top1 = a["n1"]
                top2 = a["n1"] + a["n2"]
                top3 = top2 + a["n3"]
                row["win_rate"] = round(100.0 * top1 / starts, 1)
                row["quinella_rate"] = round(100.0 * top2 / starts, 1)
                row["show_rate"] = round(100.0 * top3 / starts, 1)
            else:
                row["win_rate"] = row["quinella_rate"] = row["show_rate"] = None
            for k in ROI_KEYS:
                w = a[f"_{k}_w"]
                row[k] = round(a[f"_{k}_wsum"] / w) if w > 0 else None
        # 注: バリアント合算のROIは出走数加重平均による近似値
            rows.append(row)
        if rows:
            merged[cat] = rows
    return merged


def write_course_csv(venue_slug, race_type, dist, tables):
    out_dir = os.path.join(OUT_ROOT, venue_slug, "factors")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{race_type}{dist}.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        # baseline行 (allallのALL行)
        for row in tables.get("allall", []):
            out = {"factor_type": "baseline", "entity": "ALL"}
            out.update({k: row.get(k, "") for k in CSV_FIELDS[2:]})
            writer.writerow(_blank_none(out))
        for cat in CATEGORIES:
            if cat == "allall":
                continue
            for row in tables.get(cat, []):
                out = {"factor_type": cat, "entity": row["entity"]}
                out.update({k: row.get(k, "") for k in CSV_FIELDS[2:]})
                writer.writerow(_blank_none(out))
    return out_path


def _blank_none(d):
    return {k: ("" if v is None else v) for k, v in d.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="既存CSVも上書き")
    ap.add_argument("--only", default="", help="スラッグ部分一致フィルタ")
    ap.add_argument("--delay", type=float, default=1.5, help="リクエスト間隔(秒)")
    args = ap.parse_args()

    # (venue, race_type, dist) でグループ化 (内外回りバリアントを同一コースに)
    groups = {}
    for slug in SLUGS:
        if args.only and args.only not in slug:
            continue
        venue, race_type, dist, _var = parse_slug(slug)
        groups.setdefault((venue, race_type, dist), []).append(slug)

    if not groups:
        print("対象コースがありません (--only の指定を確認)")
        return

    session = requests.Session()
    ok, skipped, failed = [], [], []

    for (venue, race_type, dist), slugs in sorted(groups.items()):
        course_label = f"{venue} {race_type}{dist}"
        out_path = os.path.join(OUT_ROOT, venue, "factors", f"{race_type}{dist}.csv")
        if os.path.exists(out_path) and not args.force:
            skipped.append(course_label)
            continue

        table_list = []
        for slug in slugs:
            print(f"  fetching {slug} ...")
            html = fetch_page(slug, session)
            time.sleep(args.delay)
            if html is None:
                print(f"    [NG] {slug}: 取得失敗(404等)")
                continue
            tables = parse_factor_tables(html)
            if sanity_check(slug, tables):
                table_list.append(tables)

        if not table_list:
            failed.append(course_label)
            continue

        merged = merge_tables(table_list)
        path = write_course_csv(venue, race_type, dist, merged)
        n_rows = sum(len(v) for v in merged.values())
        print(f"    [OK] {course_label} -> {path} ({n_rows}行)")
        ok.append(course_label)

    print("\n===== サマリ =====")
    print(f"  OK:      {len(ok)}")
    print(f"  スキップ: {len(skipped)} (既存CSV。--forceで上書き)")
    print(f"  失敗:    {len(failed)}")
    for c in failed:
        print(f"    - {c}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
