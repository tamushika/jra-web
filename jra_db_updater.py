"""
JRA レース結果 DB登録アプリ
=============================
【起動方法】
  python jra_db_updater.py

起動するとブラウザが自動で開きます。
URL欄にJRAレース結果ページのURLを入力して「データベースに登録」を押してください。
"""

import sys
import json
import csv
import io
import threading
import webbrowser
import re
import os
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), 'jra-web', '.env'))
DB_URL = os.environ.get('DATABASE_URL')
if DB_URL and DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql://', 1)

engine = None
if DB_URL:
    try:
        engine = create_engine(DB_URL)
    except Exception as e:
        print('DB Engine creation error:', e)

PORT = 8766

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.jra.go.jp/",
}

PLACE_MAP = {
    "札幌": "札幌", "函館": "函館", "福島": "福島", "新潟": "新潟",
    "東京": "東京", "中山": "中山", "中京": "中京", "京都": "京都",
    "阪神": "阪神", "小倉": "小倉"
}

def parse_payouts(soup: BeautifulSoup) -> tuple[dict, dict]:
    tansho: dict[str, str] = {}
    fukusho: dict[str, str] = {}

    all_tokens = [t.strip() for t in soup.strings if t.strip()]

    OTHER_TYPES = {"枠連", "馬連", "ワイド", "馬単", "3連複", "3連単", "WIN5"}

    def is_banum(s: str) -> bool:
        return s.isdigit() and 1 <= int(s) <= 18

    def get_amount(idx: int) -> tuple[str, int]:
        if idx >= len(all_tokens):
            return "", 0
        t = all_tokens[idx]
        m = re.match(r'^([\d,]+)円', t)
        if m:
            return m.group(0), 1
        if re.match(r'^[\d,]+$', t) and idx + 1 < len(all_tokens) and all_tokens[idx + 1] == "円":
            return t + "円", 2
        return "", 0

    mode = None
    fukusho_count = 0
    i = 0

    while i < len(all_tokens):
        token = all_tokens[i]

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

        if mode == "単勝" and is_banum(token):
            amount, consumed = get_amount(i + 1)
            if amount:
                tansho[token] = amount
                i += 1 + consumed
                mode = None
                continue

        if mode == "複勝" and fukusho_count < 3 and is_banum(token):
            amount, consumed = get_amount(i + 1)
            if amount:
                fukusho[token] = amount
                fukusho_count += 1
                i += 1 + consumed
                continue

        i += 1

    return tansho, fukusho

def clean_obj(val):
    if not val: return ''
    v_str = str(val).replace(' ','').replace('　','')
    import unicodedata
    return unicodedata.normalize('NFKC', v_str)

def format_time_str(time_str):
    if not time_str: return ''
    if ':' in time_str: return time_str
    return time_str

def fetch_and_parse(url: str) -> dict:
    resp = requests.get(url, headers=FETCH_HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "cp932"
    soup = BeautifulSoup(resp.text, "html.parser")

    info = {}
    h1 = soup.find("h1")
    if h1: info["レース情報"] = h1.get_text(strip=True)
    h2 = soup.find("h2")
    if h2: info["レース名"] = h2.get_text(strip=True)

    for tag in soup.find_all(string=re.compile(r"メートル")):
        txt = tag.strip()
        if txt: info["コース"] = txt; break
        
    for cell in soup.find_all("div", class_="cell"):
        txt = cell.get_text(strip=True)
        if "芝" in txt or "ダート" in txt or "馬場" in txt or "良" in txt or "重" in txt:
            if "馬場" not in info: info["馬場"] = ""
            info["馬場"] += " " + txt

    tansho, fukusho = parse_payouts(soup)

    target_table = None
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        if any(k in ths for k in ["馬名", "タイム", "騎手", "着順"]):
            target_table = table; break

    columns = []
    rows = []

    if target_table:
        header_row = target_table.find("tr")
        columns = [th.get_text(strip=True) for th in header_row.find_all("th")]
        columns += ["単勝配当", "複勝配当"]

        for tr in target_table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells: continue
            base_cols = [c for c in columns if c not in ("単勝配当", "複勝配当")]
            row = {base_cols[i]: cells[i] for i in range(min(len(base_cols), len(cells)))}

            rank_str = clean_obj(row.get("着順", ""))
            m_rank = re.search(r'\d+', rank_str)
            rank = int(m_rank.group()) if m_rank else 99

            banum = clean_obj(row.get("馬番", ""))
            row["単勝配当"] = tansho.get(banum, "") if rank == 1 else ""
            row["複勝配当"] = fukusho.get(banum, "") if rank <= 3 else ""
            rows.append(row)

    return {"info": info, "columns": columns, "rows": rows}

def insert_into_db(data: dict, url: str) -> dict:
    if not engine:
        return {"error": "データベース接続設定(DATABASE_URL)が見つかりません"}
        
    info = data.get("info", {})
    rows = data.get("rows", [])
    if not rows:
        return {"error": "登録するレース結果がありません"}

    r_info = info.get("レース情報", "")
    r_name = info.get("レース名", "")
    course_txt = info.get("コース", "")
    baba_txt = info.get("馬場", "")
    
    dates_in_url = re.findall(r'20(\d{6})', url)
    date_val = None
    if dates_in_url:
        date_val = dates_in_url[-1]

    # レース番号 (CNAME: pw01sde <開催回> <場コード> <年> <回日> <R番号> <日付8桁>)
    race_num_val = None
    m_rn = re.search(r'pw01sde\d{2}\d{2}\d{4}\d{4}(\d{2})20\d{6}', url)
    if m_rn:
        race_num_val = int(m_rn.group(1))
    
    m_date = re.search(r'20(\d{2})年(\d{1,2})月(\d{1,2})日', r_info)
    if m_date:
        date_val = f"{m_date.group(1)}{int(m_date.group(2)):02d}{int(m_date.group(3)):02d}"
        
    if not date_val:
        return {"error": "URLまたは本文から日付(YYMMDD)を特定できませんでした。"}
        
    place_val = ""
    for p in PLACE_MAP:
        if p in r_info:
            place_val = p; break
            
    if not place_val:
        return {"error": "競馬場を特定できませんでした。"}
        
    track_type = ""
    if "芝" in course_txt: track_type = "芝"
    elif "ダート" in course_txt: track_type = "ダート"
    
    distance = None
    m_dist = re.search(r'(\d+)メートル', course_txt)
    if m_dist: distance = int(m_dist.group(1))
    
    condition = ""
    if "良" in baba_txt: condition = "良"
    elif "稍重" in baba_txt or "稍" in baba_txt: condition = "稍"
    elif "不良" in baba_txt or "不" in baba_txt: condition = "不"
    elif "重" in baba_txt: condition = "重"

    try:
        with engine.begin() as conn:
            check_q = text("SELECT COUNT(*) FROM races WHERE date=:d AND place=:p AND race_name=:rn")
            cnt = conn.execute(check_q, {"d": date_val, "p": place_val, "rn": r_name}).scalar()
            if cnt > 0:
                return {"error": f"このレース({place_val} {r_name})は既にデータベースに登録されています！"}
    except Exception as e:
        print("Duplicate check error:", e)

    df_rows = []
    total_horses = len(rows)
    for r in rows:
        c4 = r.get("通過", "")
        m_c4 = re.search(r'\d+', c4.split('-')[-1]) if '-' in c4 else re.search(r'\d+', c4)
        c4_val = int(m_c4.group()) if m_c4 else None
        
        weight_str = r.get("馬体重", "")
        m_w = re.search(r'^\s*(\d+)', weight_str)
        w_val = float(m_w.group(1)) if m_w else None
        
        rank_str = clean_obj(r.get("着順", ""))
        m_r = re.search(r'\d+', rank_str)
        
        tansho_str = r.get("単勝配当", "")
        tansho_str = tansho_str.replace('円', '').replace(',', '').strip()

        horse_odds_raw = (r.get("単勝") or r.get("単勝オッズ") or r.get("オッズ") or "").strip()
        try:
            horse_odds_val = float(horse_odds_raw) if horse_odds_raw else None
        except ValueError:
            horse_odds_val = None

        # 斤量 (負担重量) を数値化
        kinryo_raw = (r.get("斤量") or r.get("負担重量") or "").strip()
        m_kin = re.search(r'(\d+(?:\.\d+)?)', kinryo_raw)

        row_dict = {
            "date": date_val,
            "place": place_val,
            "kaisai": r_info,
            "track_type": track_type,
            "distance": distance,
            "condition": condition,
            "race_name": r_name,
            "total_horses": total_horses,
            "horse_number": r.get("馬番"),
            "rank": float(m_r.group()) if m_r else 99.0,
            "corner_4": c4_val,
            "jockey": r.get("騎手"),
            "time": format_time_str(r.get("タイム", "")),
            "agari_3f": r.get("上り", "").replace(' ', '').replace('　', ''),
            "popularity": r.get("人気") or r.get("単勝人気"),
            "odds": tansho_str,
            "horse_odds": horse_odds_val,
            "weight": w_val,
            "race_num": race_num_val,
            # ML再学習用: 馬名で履歴連結できるよう馬情報も保存 (2026-07追加, 列型はすべてtext)
            "馬名": (r.get("馬名") or "").strip() or None,
            "sex_age": (r.get("性齢") or "").strip() or None,
            "斤量": m_kin.group(1) if m_kin else None,
            "所属": (r.get("調教師名") or r.get("調教師") or r.get("厩舎") or r.get("所属") or "").strip() or None,
        }
        df_rows.append(row_dict)
        
    df = pd.DataFrame(df_rows)
    
    try:
        df.to_sql('races', engine, if_exists='append', index=False, method='multi')
        return {"success": True, "message": f"{total_horses}頭分のデータをPostgreSQLに追加登録しました！"}
    except Exception as e:
        return {"error": str(e)}

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JRA レース結果 DB登録アプリ</title>
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

.bar{background:var(--acc);color:#fff;height:52px;padding:0 1.5rem;
     display:flex;align-items:center;gap:10px}
.bar-ico{width:28px;height:28px;background:var(--gold);border-radius:6px;
         display:flex;align-items:center;justify-content:center;font-size:16px}
.bar h1{font-size:15px;font-weight:600}

.main{max-width:1040px;margin:0 auto;padding:2rem 1.5rem}
.card{background:var(--surf);border:1px solid var(--bdr);
      border-radius:var(--rl);padding:1.5rem;margin-bottom:1.2rem}

.lbl{display:block;font-size:11px;font-weight:600;color:var(--txt2);
     letter-spacing:.06em;text-transform:uppercase;margin-bottom:7px}
.row{display:flex;gap:8px}
.row input{flex:1;height:40px;border:1px solid var(--bdr2);border-radius:var(--r);
           padding:0 12px;font-size:13px;background:var(--surf);color:var(--txt);outline:none}
.row input:focus{border-color:var(--acc);box-shadow:0 0 0 2px rgba(26,26,46,.12)}
.hint{font-size:11px;color:var(--txt3);margin-top:6px}

.btn-main{height:40px;padding:0 22px;background:var(--acc);color:var(--gold);
          border:none;border-radius:var(--r);font-size:13px;font-weight:600;
          cursor:pointer;white-space:nowrap;transition:opacity .15s,transform .1s}
.btn-main:hover:not(:disabled){opacity:.88}
.btn-main:active:not(:disabled){transform:scale(.97)}
.btn-main:disabled{opacity:.35;cursor:not-allowed}

.btn-dl{height:36px;padding:0 14px;background:#1a1a2e;color:var(--gold);
        border:none;border-radius:var(--r);font-size:13px;font-weight:600;
        cursor:pointer;display:flex;align-items:center;gap:5px;transition:background .15s}
.btn-dl:hover:not(:disabled){opacity:.88}
.btn-dl:disabled{opacity:.35;cursor:not-allowed}

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

.empty{text-align:center;padding:4rem 1rem;color:var(--txt3);font-size:13px}
.empty-ico{font-size:3rem;margin-bottom:12px;opacity:.3}
</style>
</head>
<body>

<div class="bar">
  <div class="bar-ico">🏇</div>
  <h1>JRA レース結果 PostgreSQL 自動登録ツール</h1>
  <p>ローカルサーバー稼働中 (port PORTNUM)</p>
</div>

<div class="main">
  <div class="card">
    <label class="lbl" for="urlInput">レース結果 URL</label>
    <div class="row">
      <input id="urlInput" type="text"
        placeholder="https://www.jra.go.jp/JRADB/accessS.html?CNAME=..." />
      <button class="btn-main" id="fetchBtn" onclick="doFetch()">プレビューを表示</button>
    </div>
    <p class="hint">JRAレース結果ページのURLをそのまま貼り付けて開始してください</p>
  </div>

  <div class="stat" id="stat">
    <div class="spin"></div><div class="dot"></div>
    <span id="statTxt">URLを入力してプレビューを表示してください</span>
  </div>

  <div id="res">
    <div class="res-hd">
      <h2 id="raceTitle">レース結果プレビュー</h2>
      <button class="btn-dl" id="insertBtn" onclick="doInsert()">
        データベースへ追加登録 (INSERT)
      </button>
    </div>
    <div id="chips" class="chips"></div>
    <div class="tbl-wrap">
      <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
    </div>
  </div>

  <div class="empty" id="empty">
    <div class="empty-ico">🏇</div>
    <div>取得結果と登録ボタンがここに表示されます</div>
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
  document.getElementById("insertBtn").disabled = true;
  setStat("ld","JRAサイトからデータを取得中...");

  try{
    const r = await fetch("/api/fetch?url=" + encodeURIComponent(url));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || "取得失敗");
    if(!j.rows || !j.rows.length) throw new Error("レース結果テーブルが見つかりません");

    gData = j;
    render(j);
    setStat("ok", `取得完了 — ${j.rows.length}頭分のデータをプレビューに表示しています`);
    document.getElementById("insertBtn").disabled = false;
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
  ["レース名","コース","馬場"].forEach(k=>{
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

async function doInsert(){
  if(!gData) return;
  setStat("ld","PostgreSQLに登録中...");
  const btn = document.getElementById("insertBtn");
  btn.disabled = true;
  
  try{
    const url = document.getElementById("urlInput").value.trim();
    const r = await fetch("/api/insert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, data: gData })
    });
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || "DBへの追加に失敗しました");
    setStat("ok", j.message || "DBへの追加が成功しました！");
    alert("データベースへ登録が完了しました！");
  }catch(e){
    setStat("ng","登録エラー: " + e.message);
  }finally{
    btn.disabled = false;
  }
}

document.getElementById("urlInput")
  .addEventListener("keydown", e=>{ if(e.key==="Enter") doFetch(); });
</script>
</body>
</html>
""".replace("PORTNUM", str(PORT))

class Handler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_html(HTML)

        elif path == "/api/fetch":
            qs = parse_qs(parsed.query)
            urls = qs.get("url", [])
            url = unquote(urls[0]) if urls else None
            
            if not url:
                self._send_json({"error": "urlパラメータが必要です"}, 400)
                return
            try:
                data = fetch_and_parse(url)
                self._send_json(data)
            except requests.exceptions.HTTPError as e:
                self._send_json({"error": f"HTTPエラー: {e.response.status_code}"}, 502)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == "/api/insert":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                req_json = json.loads(post_data.decode('utf-8'))
                res = insert_into_db(req_json['data'], req_json['url'])
                if "error" in res:
                    self._send_json(res, 400)
                else:
                    self._send_json(res, 200)
            except Exception as e:
                import traceback
                print(traceback.format_exc())
                self._send_json({"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

def main():
    if not engine:
        print("\n[警告] .envファイルにDATABASE_URLが設定されていないか、接続できません。")
        print("この状態でも取得はできますが、DBへの追加(INSERT)は機能しません。\n")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"

    print("=" * 50)
    print("  🏇 JRA レース結果 DB登録アプリ")
    print("=" * 50)
    print(f"  サーバー起動: {url}")
    print("  ブラウザが自動で開きます...")
    print("  終了するには Ctrl+C を押してください")
    print("-" * 50)

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  サーバーを停止しました。")

if __name__ == "__main__":
    main()
