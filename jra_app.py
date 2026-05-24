"""
JRA レース結果 CSV出力ツール
=============================
【起動方法】
  python jra_app.py

起動するとブラウザが自動で開きます。
URL欄にJRAレース結果ページのURLを入力して「結果を取得」を押してください。

【必要ライブラリ】
  pip install requests beautifulsoup4
"""

import sys
import json
import csv
import io
import threading
import webbrowser
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

try:
    import pandas as pd
    from sqlalchemy import create_engine
    from sqlalchemy.sql import text as sa_text
    from dotenv import load_dotenv
    _DB_LIBS_OK = True
except ImportError:
    _DB_LIBS_OK = False

# ─────────────────────────────────────────
PORT = 8765

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.jra.go.jp/",
}

# ── DB接続（jra-web/.env の DATABASE_URL を使用）──
engine = None
if _DB_LIBS_OK:
    load_dotenv(os.path.join(os.path.dirname(__file__), "jra-web", ".env"))
    _db_url = os.environ.get("DATABASE_URL", "")
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    if _db_url:
        try:
            engine = create_engine(_db_url)
        except Exception as _e:
            print(f"DB Engine error: {_e}")

PLACE_MAP = {
    "札幌": "札幌", "函館": "函館", "福島": "福島", "新潟": "新潟",
    "東京": "東京", "中山": "中山", "中京": "中京", "京都": "京都",
    "阪神": "阪神", "小倉": "小倉",
}

# ─────────────────────────────────────────
# スクレイピング処理
# ─────────────────────────────────────────

def parse_payouts(soup: BeautifulSoup) -> tuple[dict, dict]:
    """
    単勝・複勝の払戻金を取得する。
    HTML構造（table/dl/li など）に依存せず、ページ全体の
    テキストストリームを順に読んで解析する方式。
    戻り値: (tansho, fukusho)
      tansho  = {馬番文字列: "530円"}
      fukusho = {馬番文字列: "150円", ...}
    """
    tansho: dict[str, str] = {}
    fukusho: dict[str, str] = {}

    # ページ全テキストトークンを空白除去して列挙
    all_tokens = [t.strip() for t in soup.strings if t.strip()]

    # 他の券種ラベル（複勝ブロック終端の検出に使用）
    OTHER_TYPES = {"枠連", "馬連", "ワイド", "馬単", "3連複", "3連単", "WIN5"}

    def is_banum(s: str) -> bool:
        """馬番かどうか（1〜18 の整数に限定し、配当金額との混同を防ぐ）"""
        return s.isdigit() and 1 <= int(s) <= 18

    def get_amount(idx: int) -> tuple[str, int]:
        """
        idx 以降から金額文字列を取得する。
        ケース1: "530円" や "1,530円" が1トークンの場合 → (金額, 1)
        ケース2: "530" と "円" が別トークンに分かれている場合 → ("530円", 2)
        取得できなければ ("", 0) を返す。
        """
        if idx >= len(all_tokens):
            return "", 0
        t = all_tokens[idx]
        # "530円" / "1,530円" のように数字+円が同一トークン
        m = re.match(r'^([\d,]+)円', t)
        if m:
            return m.group(0), 1
        # "530" / "1,530" の次トークンが単独の "円"
        if re.match(r'^[\d,]+$', t) and idx + 1 < len(all_tokens) and all_tokens[idx + 1] == "円":
            return t + "円", 2
        return "", 0

    mode = None
    fukusho_count = 0
    i = 0

    while i < len(all_tokens):
        token = all_tokens[i]

        # ── 券種ラベルの検出 ──
        if token == "単勝":
            mode = "単勝"
            i += 1
            continue

        if token == "複勝":
            mode = "複勝"
            fukusho_count = 0
            i += 1
            continue

        if token in OTHER_TYPES:
            mode = None
            i += 1
            continue

        # ── 単勝: 馬番→金額の1セットだけ取得 ──
        if mode == "単勝" and is_banum(token):
            amount, consumed = get_amount(i + 1)
            if amount:
                tansho[token] = amount
                i += 1 + consumed
                mode = None  # 単勝は1件のみ
                continue

        # ── 複勝: 馬番→金額を最大3セット取得 ──
        if mode == "複勝" and fukusho_count < 3 and is_banum(token):
            amount, consumed = get_amount(i + 1)
            if amount:
                fukusho[token] = amount
                fukusho_count += 1
                i += 1 + consumed
                continue

        i += 1

    return tansho, fukusho


def fetch_and_parse(url: str) -> dict:
    """JRAのレース結果ページを取得して解析する"""
    resp = requests.get(url, headers=FETCH_HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "cp932"
    soup = BeautifulSoup(resp.text, "html.parser")

    # ── レース基本情報 ──
    info = {}

    # レース情報: span.opt に "2026年4月25日（土曜）2回東京1日 1レース" が入る
    opt = soup.find("span", class_="opt")
    if opt:
        info["レース情報"] = opt.get_text(strip=True)

    # レース名: ナビゲーション系 h2 をスキップして最初の有意な h2
    _SKIP_H2 = {"検索ウィンドウ", "緊急情報", "払戻金", "JRAからのお知らせ"}
    for h2 in soup.find_all("h2"):
        t = h2.get_text(strip=True)
        if t and t not in _SKIP_H2:
            info["レース名"] = t
            break

    # コース: div.course に "コース：1,600メートル（ダート・左）" が入る
    course_div = soup.find("div", class_="course")
    if course_div:
        info["コース"] = course_div.get_text(strip=True)
    else:
        for tag in soup.find_all(string=re.compile(r"メートル")):
            p = tag.parent
            for _ in range(4):
                full = p.get_text(strip=True) if p else ""
                if re.search(r'\d', full):
                    info["コース"] = full
                    break
                p = getattr(p, "parent", None)
            if "コース" in info:
                break

    # 馬場: div.date_line に天候・馬場状態が含まれる
    date_line = soup.find("div", class_="date_line")
    if date_line:
        info["馬場"] = date_line.get_text(strip=True)

    # ── 払戻金（単勝・複勝）──
    tansho, fukusho = parse_payouts(soup)

    # ── レース結果テーブル ──
    target_table = None
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        if any(k in ths for k in ["馬名", "タイム", "騎手", "着順"]):
            target_table = table
            break

    columns = []
    rows = []

    if target_table:
        header_row = target_table.find("tr")
        columns = [th.get_text(strip=True) for th in header_row.find_all("th")]
        # 単勝配当・複勝配当列を追加
        columns = columns + ["単勝配当", "複勝配当"]

        for tr in target_table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue

            base_cols = [c for c in columns if c not in ("単勝配当", "複勝配当")]
            row = {base_cols[i]: cells[i] for i in range(min(len(base_cols), len(cells)))}

            # 着順を数値化（"1"〜"3" のみ払戻対象）
            rank_str = row.get("着順", "").strip()
            try:
                rank = int(rank_str)
            except ValueError:
                rank = 99  # 失格・除外等

            banum = row.get("馬番", "").strip()

            # 単勝配当: 1着馬のみ
            row["単勝配当"] = tansho.get(banum, "") if rank == 1 else ""
            # 複勝配当: 1〜3着馬のみ（着外は空欄）
            row["複勝配当"] = fukusho.get(banum, "") if rank <= 3 else ""

            rows.append(row)

    return {"info": info, "columns": columns, "rows": rows}


def build_csv(data: dict) -> str:
    """辞書データからBOM付きUTF-8 CSV文字列を生成する"""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["【レース情報】"])
    for k, v in data["info"].items():
        writer.writerow([k, v])
    writer.writerow([])

    writer.writerow(["【レース結果】"])
    writer.writerow(data["columns"])
    for row in data["rows"]:
        writer.writerow([row.get(c, "") for c in data["columns"]])

    return "\ufeff" + buf.getvalue()   # BOM付き


# ─────────────────────────────────────────



# ─────────────────────────────────────────
# DB登録
# ─────────────────────────────────────────

def _clean(val: str) -> str:
    import unicodedata
    return unicodedata.normalize('NFKC', str(val).replace(' ', '').replace('　', '')) if val else ''


def _fmt_time(s: str) -> str:
    return s if s and ':' in s else s


def insert_into_db(data: dict, url: str) -> dict:
    """
    fetch_and_parse() の戻り値を DB の races テーブルへ登録する。
    列名は jra_app.py の fetch_and_parse が返す名前（騎手名・コーナー通過順位・推定上り等）に合わせている。
    """
    if not engine:
        return {"error": "DATABASE_URL が未設定です（jra-web/.env を確認してください）"}

    info = data.get("info", {})
    rows = data.get("rows", [])
    if not rows:
        return {"error": "登録するレース結果がありません"}

    r_info = info.get("レース情報", "")
    r_name = info.get("レース名", "")
    course = info.get("コース", "")
    baba   = info.get("馬場", "")

    date_val = None
    m = re.search(r"20(\d{2})年(\d{1,2})月(\d{1,2})日", r_info)
    if m:
        date_val = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    else:
        hits = re.findall(r"20(\d{6})", url)
        if hits:
            date_val = hits[-1]
    if not date_val:
        return {"error": f"日付を特定できません: {r_info}"}

    place_val = next((p for p in PLACE_MAP if p in r_info), "")
    if not place_val:
        return {"error": f"競馬場を特定できません: {r_info}"}

    # コースと馬場の両方からトラック種別を判定
    _src = course + " " + baba
    track_type = "芝" if "芝" in _src else ("ダート" if "ダート" in _src else "")
    # 距離: カンマ付き数字も考慮 ("1,600メートル" → 1600)
    m_dist = re.search(r"([\d,]+)メートル", course)
    distance = int(m_dist.group(1).replace(",", "")) if m_dist else None

    condition = ""
    if "不良" in baba or "不" in baba:   condition = "不"
    elif "稍重" in baba or "稍" in baba: condition = "稍"
    elif "重" in baba:                    condition = "重"
    elif "良" in baba:                    condition = "良"

    try:
        with engine.begin() as conn:
            # kaisai はレース番号まで含む一意な文字列のため重複キーとして使用
            # 例: "2026年4月25日（土曜）2回東京1日 1レース"
            cnt = conn.execute(
                sa_text("SELECT COUNT(*) FROM races WHERE kaisai=:k"),
                {"k": r_info},
            ).scalar()
            if cnt and cnt > 0:
                return {"skipped": True, "message": f"{r_info} は既に登録済みです"}
    except Exception as e:
        return {"error": f"重複チェック失敗: {e}"}

    total = len(rows)
    df_rows = []
    for r in rows:
        # コーナー通過順位: _clean()を使わずスペース/ハイフン区切りで分割して最終コーナーのみ取得
        # （_cleanはスペースを除去するため "12 12 11 11" → "12121111" になってしまう）
        c4_raw = r.get("コーナー通過順位", "").strip()
        parts  = re.split(r"[\s\-]+", c4_raw)
        m_c4   = re.search(r"\d+", parts[-1]) if parts else None
        c4_val = int(m_c4.group()) if m_c4 else None

        m_w   = re.search(r"(\d+)", r.get("馬体重（増減）", ""))
        w_val = float(m_w.group(1)) if m_w else None

        m_r  = re.search(r"\d+", _clean(r.get("着順", "")))
        rank = float(m_r.group()) if m_r else 99.0

        # 空文字は None に変換（double precision 型カラムへの空文字渡しを防ぐ）
        odds_raw = r.get("単勝配当", "").replace("円", "").replace(",", "").strip()
        odds_val = odds_raw or None

        agari_raw = _clean(r.get("推定上り", ""))
        agari_val = agari_raw or None

        pop_raw = (r.get("単勝人気") or r.get("人気") or "").strip()
        pop_val = pop_raw or None

        horse_odds_raw = (r.get("単勝") or r.get("単勝オッズ") or r.get("オッズ") or "").strip()
        try:
            horse_odds_val = float(horse_odds_raw) if horse_odds_raw else None
        except ValueError:
            horse_odds_val = None

        df_rows.append({
            "date":         date_val,
            "place":        place_val,
            "kaisai":       r_info,
            "track_type":   track_type,
            "distance":     distance,
            "condition":    condition,
            "race_name":    r_name,
            "total_horses": total,
            "horse_number": r.get("馬番"),
            "rank":         rank,
            "corner_4":     c4_val,
            "jockey":       r.get("騎手名"),
            "time":         _fmt_time(r.get("タイム", "")) or None,
            "agari_3f":     agari_val,
            "popularity":   pop_val,
            "odds":         odds_val,
            "horse_odds":   horse_odds_val,
            "weight":       w_val,
        })

    try:
        pd.DataFrame(df_rows).to_sql("races", engine, if_exists="append", index=False, method="multi")
        return {"success": True, "message": f"{total}頭分のデータを登録しました"}
    except Exception as e:
        return {"error": str(e)}

def discover_weekend_races(seed_url: str) -> tuple[list[dict], list[str]]:
    """
    入力されたレース結果URLをシードとして、同週の全レースURLを発見する。
    レース結果ページには全会場/日のナビリンクが掲載されているため、
    そこから会場/日別の代表URLを収集し、さらに各会場の全レースを辿る。
    """
    races: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    # Step1: シードURL（入力済みレース結果ページ）から会場/日別リンクを収集
    try:
        resp = requests.get(seed_url, headers=FETCH_HEADERS, timeout=20)
        resp.encoding = 'cp932'
        seed_soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        return [], [f'ページ取得失敗: {e}']

    venue_day_map: dict[str, str] = {}
    for a in seed_soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']
        if text and 'accessS.html' in href and 'CNAME=pw01' in href and text not in venue_day_map:
            full = ('https://www.jra.go.jp' + href) if href.startswith('/') else href
            venue_day_map[text] = full

    if not venue_day_map:
        return [], ['このページから週間レース一覧が見つかりませんでした（JRAレース結果URLを入力してください）']

    # Step2: 各会場/日ページから全レースナビリンクを収集
    for label, vd_url in venue_day_map.items():
        try:
            time.sleep(0.3)
            resp2 = requests.get(vd_url, headers=FETCH_HEADERS, timeout=20)
            resp2.encoding = 'cp932'
            soup2 = BeautifulSoup(resp2.text, 'html.parser')
        except Exception as e:
            errors.append(f'{label} 取得失敗: {e}')
            continue
        for a in soup2.find_all('a', href=True):
            href = a['href']
            if 'accessS.html' in href and 'CNAME=pw01' in href:
                full = ('https://www.jra.go.jp' + href) if href.startswith('/') else href
                if full not in seen:
                    seen.add(full)
                    races.append({'venue_day': label, 'url': full})

    return races, errors


def _fetch_race_safe(race_info: dict) -> dict:
    try:
        data = fetch_and_parse(race_info['url'])
        data['venue_day'] = race_info['venue_day']
        return data
    except Exception as e:
        return {'venue_day': race_info['venue_day'], 'url': race_info['url'],
                'error': str(e), 'info': {}, 'columns': [], 'rows': []}


def fetch_all_races_batch(race_infos: list[dict], max_workers: int = 3) -> list[dict]:
    results: list = [None] * len(race_infos)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        fmap = {executor.submit(_fetch_race_safe, ri): i for i, ri in enumerate(race_infos)}
        for future in as_completed(fmap):
            results[fmap[future]] = future.result()
    return results

# 組み込みHTML（GUI）
# ─────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JRA レース結果 CSV出力ツール</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f7f5f0;--surf:#fff;--surf2:#f2efe9;
  --bdr:rgba(0,0,0,.10);--bdr2:rgba(0,0,0,.18);
  --txt:#1a1a1a;--txt2:#555;--txt3:#999;
  --acc:#1a1a2e;--gold:#f5c842;--gold-d:#c8970a;
  --ok-bg:#eaf3de;--ok-txt:#3b6d11;
  --ng-bg:#fcebeb;--ng-txt:#a32d2d;
  --ld-bg:#fef9e7;--ld-txt:#7d5a00;
  --r:8px;--rl:12px;
}
body{font-family:"Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic UI","Meiryo",sans-serif;
     background:var(--bg);color:var(--txt);min-height:100vh}

/* トップバー */
.bar{background:var(--acc);color:#fff;height:52px;padding:0 1.5rem;
     display:flex;align-items:center;gap:10px}
.bar-ico{width:28px;height:28px;background:var(--gold);border-radius:6px;
         display:flex;align-items:center;justify-content:center;font-size:16px}
.bar h1{font-size:15px;font-weight:600}
.bar p{font-size:11px;opacity:.55;margin-left:auto}

/* メイン */
.main{max-width:1040px;margin:0 auto;padding:2rem 1.5rem}
.card{background:var(--surf);border:1px solid var(--bdr);
      border-radius:var(--rl);padding:1.5rem;margin-bottom:1.2rem}

/* フォーム */
.lbl{display:block;font-size:11px;font-weight:600;color:var(--txt2);
     letter-spacing:.06em;text-transform:uppercase;margin-bottom:7px}
.row{display:flex;gap:8px}
.row input{flex:1;height:40px;border:1px solid var(--bdr2);border-radius:var(--r);
           padding:0 12px;font-size:13px;background:var(--surf);color:var(--txt);outline:none}
.row input:focus{border-color:var(--acc);box-shadow:0 0 0 2px rgba(26,26,46,.12)}
.hint{font-size:11px;color:var(--txt3);margin-top:6px}

/* ボタン */
.btn-main{height:40px;padding:0 22px;background:var(--acc);color:var(--gold);
          border:none;border-radius:var(--r);font-size:13px;font-weight:600;
          cursor:pointer;white-space:nowrap;transition:opacity .15s,transform .1s}
.btn-main:hover:not(:disabled){opacity:.88}
.btn-main:active:not(:disabled){transform:scale(.97)}
.btn-main:disabled{opacity:.35;cursor:not-allowed}
.btn-dl{height:36px;padding:0 14px;background:transparent;color:var(--txt);
        border:1px solid var(--bdr2);border-radius:var(--r);font-size:12px;font-weight:500;
        cursor:pointer;display:flex;align-items:center;gap:5px;transition:background .15s}
.btn-dl:hover{background:var(--surf2)}
.btn-dl svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.5}

/* ステータス */
.stat{display:flex;align-items:center;gap:8px;font-size:12px;padding:9px 14px;
      border-radius:var(--r);margin-bottom:1.2rem;border:1px solid var(--bdr);
      background:var(--surf2);color:var(--txt2);transition:background .2s,color .2s}
.stat.ld{background:var(--ld-bg);color:var(--ld-txt);border-color:#f5c84250}
.stat.ok{background:var(--ok-bg);color:var(--ok-txt);border-color:#63992250}
.stat.ng{background:var(--ng-bg);color:var(--ng-txt);border-color:#a32d2d50}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--txt3)}
.stat.ok .dot{background:var(--ok-txt)}
.stat.ng .dot{background:var(--ng-txt)}
.spin{width:14px;height:14px;border:2px solid transparent;border-top-color:var(--gold-d);
      border-radius:50%;animation:sp .6s linear infinite;display:none}
.stat.ld .spin{display:block}.stat.ld .dot{display:none}
@keyframes sp{to{transform:rotate(360deg)}}

/* 結果 */
#res{display:none}
.res-hd{display:flex;align-items:center;justify-content:space-between;
        flex-wrap:wrap;gap:8px;margin-bottom:1rem}
.res-hd h2{font-size:15px;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:1.2rem}
.chip{background:var(--surf2);border:1px solid var(--bdr);border-radius:20px;
      padding:4px 12px;font-size:12px;color:var(--txt2)}
.chip strong{color:var(--txt);font-weight:600;margin-left:4px}

.tbl-wrap{overflow-x:auto;border-radius:var(--rl);border:1px solid var(--bdr)}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:700px}
thead th{background:var(--surf2);color:var(--txt2);font-weight:600;font-size:11px;
         padding:8px 12px;text-align:left;border-bottom:1px solid var(--bdr);
         white-space:nowrap;position:sticky;top:0}
tbody td{padding:8px 12px;border-bottom:1px solid var(--bdr);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--surf2)}
.r1 td:first-child{color:var(--gold-d);font-weight:700;font-size:14px}
.r2 td:first-child{color:#888;font-weight:600}
.r3 td:first-child{color:#8b6914;font-weight:600}

/* 空状態 */
.empty{text-align:center;padding:4rem 1rem;color:var(--txt3);font-size:13px}
.empty-ico{font-size:3rem;margin-bottom:12px;opacity:.3}

/* 一括取得 */
.batch-meta{font-size:12px;color:var(--txt2);margin-bottom:8px}
.prog-wrap{margin-top:10px;display:none}
.prog-wrap.active{display:block}
.prog-bar{height:5px;background:var(--bdr2);border-radius:3px;overflow:hidden}
.prog-inner{height:100%;background:var(--acc);border-radius:3px;width:0;transition:width .25s}
.prog-lbl{font-size:11px;color:var(--txt2);margin-top:4px}
</style>
</head>
<body>

<div class="bar">
  <div class="bar-ico">🏇</div>
  <h1>JRA レース結果 CSV出力ツール</h1>
  <p>ローカルサーバー稼働中 (port PORTNUM)</p>
</div>

<div class="main">
  <div class="card">
    <label class="lbl" for="urlInput">レース結果 URL</label>
    <div class="row">
      <input id="urlInput" type="text"
        placeholder="https://www.jra.go.jp/JRADB/accessS.html?CNAME=..." />
      <button class="btn-main" id="fetchBtn" onclick="doFetch()">結果を取得</button>
    </div>
    <p class="hint">JRAレース結果ページのURLをそのまま貼り付けて「結果を取得」を押してください</p>
  </div>

  <div class="card">
    <div class="res-hd" style="margin-bottom:.6rem">
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:3px">今週の全レース一括取得</div>
        <div style="font-size:11px;color:var(--txt3)">上のURL欄に任意の1レースのURLを入力→「レース一覧を確認」で同週の全レースを一括取得できます</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn-main" id="discoverBtn" onclick="doDiscover()" style="height:36px;font-size:12px">レース一覧を確認</button>
        <button class="btn-dl" id="batchDlBtn" onclick="doBatchDownload()" style="display:none">
          <svg viewBox="0 0 16 16"><path d="M8 2v8M5 7l3 3 3-3M2 12v1a1 1 0 001 1h10a1 1 0 001-1v-1"/></svg>
          全レース CSV
        </button>
        <button class="btn-dl" id="batchDbBtn" onclick="doBatchInsert()" style="display:none;background:#1a1a2e;color:#f5c842;border:none">
          <svg viewBox="0 0 16 16" style="stroke:#f5c842"><path d="M2 4h12M2 8h12M2 12h12M10 2v12"/></svg>
          全データをDBに登録
        </button>
      </div>
    </div>
    <div id="batchInfo" style="display:none">
      <div id="batchMeta" class="batch-meta"></div>
      <div id="batchChips" class="chips"></div>
      <div class="prog-wrap" id="progWrap">
        <div class="prog-bar"><div class="prog-inner" id="progInner"></div></div>
        <div class="prog-lbl" id="progLbl"></div>
      </div>
    </div>
  </div>

  <div class="stat" id="stat">
    <div class="spin"></div><div class="dot"></div>
    <span id="statTxt">URLを入力して「結果を取得」を押してください</span>
  </div>

  <div id="res">
    <div class="res-hd">
      <h2 id="raceTitle">レース結果</h2>
      <button class="btn-dl" onclick="doDownload()">
        <svg viewBox="0 0 16 16"><path d="M8 2v8M5 7l3 3 3-3M2 12v1a1 1 0 001 1h10a1 1 0 001-1v-1"/></svg>
        CSVをダウンロード
      </button>
    </div>
    <div id="chips" class="chips"></div>
    <div class="tbl-wrap">
      <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
    </div>
  </div>

  <div class="empty" id="empty">
    <div class="empty-ico">🏇</div>
    <div>取得結果がここに表示されます</div>
  </div>
</div>

<script>
let gData = null;

function setStat(type, txt){
  const el = document.getElementById("stat");
  el.className = "stat " + (type||"");
  document.getElementById("statTxt").textContent = txt;
}

async function doFetch(){
  const url = document.getElementById("urlInput").value.trim();
  if(!url){ setStat("ng","URLを入力してください"); return; }

  document.getElementById("fetchBtn").disabled = true;
  document.getElementById("res").style.display = "none";
  document.getElementById("empty").style.display = "block";
  setStat("ld","JRAサイトからデータを取得中...");

  try{
    const r = await fetch("/api/fetch?url=" + encodeURIComponent(url));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || "取得失敗");
    if(!j.rows || !j.rows.length) throw new Error("レース結果テーブルが見つかりません");

    gData = j;
    render(j);
    setStat("ok", `取得完了 — ${j.rows.length}頭分のデータを取得しました`);
  }catch(e){
    setStat("ng", "エラー: " + e.message);
  }finally{
    document.getElementById("fetchBtn").disabled = false;
  }
}

function render(d){
  document.getElementById("raceTitle").textContent =
    (d.info && d.info["レース情報"]) || "レース結果";

  const chips = document.getElementById("chips");
  chips.innerHTML = "";
  ["レース名","コース"].forEach(k=>{
    if(d.info && d.info[k])
      chips.innerHTML += `<div class="chip">${k}<strong>${d.info[k]}</strong></div>`;
  });

  document.getElementById("thead").innerHTML =
    "<tr>" + d.columns.map(c=>`<th>${c}</th>`).join("") + "</tr>";

  const PAYOUT_COLS = new Set(["単勝配当","複勝配当"]);
  document.getElementById("tbody").innerHTML = d.rows.map(row=>{
    const rk = row["着順"]||"";
    const cls = rk==="1"?"r1":rk==="2"?"r2":rk==="3"?"r3":"";
    const cells = d.columns.map(c=>{
      const val = row[c]||"";
      if(PAYOUT_COLS.has(c) && val){
        const bg = c==="単勝配当"?"#fff8e1":"#f1f8e9";
        const color = c==="単勝配当"?"#c8970a":"#3b6d11";
        return `<td style="background:${bg};color:${color};font-weight:600">${val}</td>`;
      }
      return `<td>${val}</td>`;
    }).join("");
    return `<tr class="${cls}">${cells}</tr>`;
  }).join("");

  document.getElementById("res").style.display = "block";
  document.getElementById("empty").style.display = "none";
}

async function doDownload(){
  if(!gData) return;
  setStat("ld","CSVを生成中...");
  try{
    const url = document.getElementById("urlInput").value.trim();
    const r = await fetch("/api/csv?url=" + encodeURIComponent(url));
    if(!r.ok){ const j=await r.json(); throw new Error(j.error); }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const ts = new Date().toISOString().slice(0,16).replace(/[T:]/g,"-");
    a.download = `jra_result_${ts}.csv`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setStat("ok","CSVをダウンロードしました");
  }catch(e){
    setStat("ng","CSV生成エラー: " + e.message);
  }
}

document.getElementById("urlInput")
  .addEventListener("keydown", e=>{ if(e.key==="Enter") doFetch(); });

// ── 一括取得 ──────────────────────────────
let gDiscoveredRaces = [];

async function doDiscover(){
  const seedUrl = document.getElementById("urlInput").value.trim();
  if(!seedUrl){ setStat("ng","上のURL欄にJRAレース結果URLを入力してから押してください"); return; }
  const btn = document.getElementById("discoverBtn");
  btn.disabled = true;
  document.getElementById("batchDlBtn").style.display = "none";
  document.getElementById("batchDbBtn").style.display = "none";
  document.getElementById("batchInfo").style.display = "none";
  document.getElementById("progWrap").classList.remove("active");
  setStat("ld", "同週の全レース一覧を確認中...");
  try{
    const r = await fetch("/api/discover?url=" + encodeURIComponent(seedUrl));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || "取得失敗");
    gDiscoveredRaces = j.races || [];
    if(!gDiscoveredRaces.length) throw new Error((j.errors||[])[0] || "レースが見つかりません");

    const groups = {};
    gDiscoveredRaces.forEach(rc => { groups[rc.venue_day] = (groups[rc.venue_day]||0) + 1; });

    document.getElementById("batchMeta").textContent =
      `${gDiscoveredRaces.length}レース / ${Object.keys(groups).length}会場・日が見つかりました`;
    document.getElementById("batchChips").innerHTML =
      Object.entries(groups).map(([vd, cnt]) =>
        `<div class="chip">${vd}<strong style="margin-left:5px">${cnt}R</strong></div>`
      ).join("");
    document.getElementById("batchInfo").style.display = "block";
    document.getElementById("batchDlBtn").style.display = "flex";
    document.getElementById("batchDbBtn").style.display = "flex";
    setStat("ok", `${gDiscoveredRaces.length}レース分のデータが取得可能です`);
  }catch(e){
    setStat("ng", "エラー: " + e.message);
  }finally{
    btn.disabled = false;
  }
}

async function doBatchDownload(){
  if(!gDiscoveredRaces.length){ setStat("ng","先に「レース一覧を確認」を押してください"); return; }
  const dlBtn = document.getElementById("batchDlBtn");
  const discBtn = document.getElementById("discoverBtn");
  dlBtn.disabled = true; discBtn.disabled = true;
  const progWrap = document.getElementById("progWrap");
  const progInner = document.getElementById("progInner");
  const progLbl = document.getElementById("progLbl");
  progWrap.classList.add("active");

  const total = gDiscoveredRaces.length;
  let done = 0;
  const allResults = [];

  setStat("ld", `全レースデータ取得中... (0/${total})`);
  try{
    const CONCURRENCY = 3;
    for(let i = 0; i < gDiscoveredRaces.length; i += CONCURRENCY){
      const chunk = gDiscoveredRaces.slice(i, i + CONCURRENCY);
      const fetched = await Promise.all(chunk.map(rc =>
        fetch("/api/fetch?url=" + encodeURIComponent(rc.url))
          .then(r => r.json())
          .then(data => ({ venue_day: rc.venue_day, data }))
          .catch(e => ({ venue_day: rc.venue_day, data: { error: e.message, rows: [] } }))
      ));
      fetched.forEach(r => { allResults.push(r); done++; });
      const pct = Math.round(done / total * 100);
      progInner.style.width = pct + "%";
      progLbl.textContent = `${done} / ${total} レース取得完了`;
      setStat("ld", `全レースデータ取得中... (${done}/${total})`);
    }

    const csvText = buildBatchCsv(allResults);
    const blob = new Blob([csvText], {type:"text/csv;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `jra_batch_${new Date().toISOString().slice(0,10)}.csv`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setStat("ok", `全${done}レースのCSVをダウンロードしました`);
  }catch(e){
    setStat("ng", "一括取得エラー: " + e.message);
  }finally{
    dlBtn.disabled = false; discBtn.disabled = false;
    progWrap.classList.remove("active");
  }
}

async function doBatchInsert(){
  if(!gDiscoveredRaces.length){ setStat("ng","先に「レース一覧を確認」を押してください"); return; }
  const dlBtn   = document.getElementById("batchDlBtn");
  const dbBtn   = document.getElementById("batchDbBtn");
  const discBtn = document.getElementById("discoverBtn");
  dlBtn.disabled = true; dbBtn.disabled = true; discBtn.disabled = true;
  const progWrap  = document.getElementById("progWrap");
  const progInner = document.getElementById("progInner");
  const progLbl   = document.getElementById("progLbl");
  progWrap.classList.add("active");

  const total = gDiscoveredRaces.length;
  let done=0, ok=0, skipped=0, errored=0;

  setStat("ld", `DB登録中... (0/${total})`);
  try{
    const CONCURRENCY = 3;
    for(let i=0; i<gDiscoveredRaces.length; i+=CONCURRENCY){
      const chunk = gDiscoveredRaces.slice(i, i+CONCURRENCY);
      await Promise.all(chunk.map(async rc => {
        try{
          // 1. レースデータ取得
          const fr = await fetch("/api/fetch?url=" + encodeURIComponent(rc.url));
          const fd = await fr.json();
          if(!fr.ok || fd.error) throw new Error(fd.error || "fetch失敗");

          // 2. DB登録
          const ir = await fetch("/api/insert", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({data: fd, url: rc.url})
          });
          const id = await ir.json();
          if(id.skipped)      skipped++;
          else if(id.success) ok++;
          else                errored++;
        }catch(e){ errored++; }
        done++;
      }));
      const pct = Math.round(done/total*100);
      progInner.style.width = pct + "%";
      progLbl.textContent = `${done}/${total} 完了 — 登録:${ok} スキップ:${skipped} エラー:${errored}`;
      setStat("ld", `DB登録中... (${done}/${total})`);
    }
    setStat(errored===0?"ok":"ng",
      `DB登録完了 — 登録:${ok}件 スキップ:${skipped}件 エラー:${errored}件`);
  }catch(e){
    setStat("ng","DB登録エラー: " + e.message);
  }finally{
    dlBtn.disabled=false; dbBtn.disabled=false; discBtn.disabled=false;
    progWrap.classList.remove("active");
  }
}

function buildBatchCsv(allResults){
  const EXTRA = ["開催","レース情報","レース名","コース"];
  const rows = [];
  let baseCols = null;
  for(const {venue_day, data} of allResults){
    if(data.error || !data.rows || !data.rows.length) continue;
    if(!baseCols){ baseCols = data.columns; rows.push([...EXTRA, ...baseCols]); }
    const info = data.info || {};
    const extra = [venue_day, info["レース情報"]||"", info["レース名"]||"", info["コース"]||""];
    for(const row of data.rows)
      rows.push([...extra, ...baseCols.map(c => row[c]||"")]);
  }
  if(!rows.length) rows.push(["取得できたデータがありません"]);
  const esc = v => { v = String(v); return (v.includes(",")||v.includes('"')||v.includes("\n")) ? '"'+v.replace(/"/g,'""')+'"' : v; };
  return "﻿" + rows.map(r => r.map(esc).join(",")).join("\r\n");
}
</script>
</body>
</html>
""".replace("PORTNUM", str(PORT))


# ─────────────────────────────────────────
# HTTPリクエストハンドラ
# ─────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # アクセスログを整形して表示
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] {args[0]} {args[1]}")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, csv_text: str, filename: str):
        body = csv_text.encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_url_param(self) -> str | None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        urls = qs.get("url", [])
        return unquote(urls[0]) if urls else None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── トップページ ──
        if path in ("/", "/index.html"):
            self._send_html(HTML)

        # ── API: データ取得 ──
        elif path == "/api/fetch":
            url = self._get_url_param()
            if not url:
                self._send_json({"error": "urlパラメータが必要です"}, 400)
                return
            try:
                data = fetch_and_parse(url)
                self._send_json(data)
            except requests.exceptions.HTTPError as e:
                self._send_json({"error": f"HTTPエラー: {e.response.status_code}"}, 502)
            except requests.exceptions.Timeout:
                self._send_json({"error": "タイムアウト（JRAサイトへの接続に時間がかかりすぎました）"}, 504)
            except requests.exceptions.ConnectionError:
                self._send_json({"error": "接続エラー（インターネット接続を確認してください）"}, 503)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        # ── API: CSVダウンロード ──
        elif path == "/api/csv":
            url = self._get_url_param()
            if not url:
                self._send_json({"error": "urlパラメータが必要です"}, 400)
                return
            try:
                data = fetch_and_parse(url)
                csv_text = build_csv(data)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self._send_csv(csv_text, f"jra_result_{ts}.csv")
            except requests.exceptions.HTTPError as e:
                self._send_json({"error": f"HTTPエラー: {e.response.status_code}"}, 502)
            except requests.exceptions.Timeout:
                self._send_json({"error": "タイムアウト"}, 504)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        # ── API: 全レース一覧発見（シードURLから同週の全レースを探索）──
        elif path == "/api/discover":
            url = self._get_url_param()
            if not url:
                self._send_json({"error": "urlパラメータが必要です（上のURL欄に単一レースのURLを入力してください）"}, 400)
                return
            try:
                races, errors = discover_weekend_races(url)
                self._send_json({"races": races, "errors": errors})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── API: 1レースをDBに登録 ──
        if path == "/api/insert":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                req = json.loads(body.decode("utf-8"))
                result = insert_into_db(req["data"], req.get("url", ""))
                status = 200 if ("success" in result or "skipped" in result) else 400
                self._send_json(result, status)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────
# 起動
# ─────────────────────────────────────────

def main():
    # ライブラリチェック
    try:
        import requests       # noqa: F401
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError as e:
        print(f"[ERROR] 必要なライブラリが見つかりません: {e}")
        print("   次のコマンドでインストールしてください:")
        print("   pip install requests beautifulsoup4")
        sys.exit(1)

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"

    print("=" * 50)
    print("  JRA レース結果 CSV出力ツール")
    print("=" * 50)
    print(f"  サーバー起動: {url}")
    print("  ブラウザが自動で開きます...")
    print("  終了するには Ctrl+C を押してください")
    print("-" * 50)

    # 少し待ってからブラウザを開く
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  サーバーを停止しました。")


if __name__ == "__main__":
    main()
