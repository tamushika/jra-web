import os
import re
import math
import concurrent.futures
from datetime import datetime
from urllib.parse import urljoin

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# Local imports
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import analysis
import past_data_service

app = Flask(__name__, static_folder='../', static_url_path='/')
CORS(app)

@app.route('/')
def serve_index():
    return app.send_static_file('index.html')

VENUE_MAP = {"06": "中山", "08": "京都", "05": "東京", "09": "阪神", "07": "中京", "04": "新潟", "03": "福島", "10": "小倉", "01": "札幌", "02": "函館"}

def get_base_dir():
    return os.path.dirname(os.path.abspath(__file__))

def calculate_waku(n, total):
    if total <= 8: return n
    q, r = divmod(total, 8)
    counts = [q + (1 if i > 8 - r else 0) for i in range(1, 9)]
    cur = 0
    for i, c in enumerate(counts, 1):
        cur += c
        if n <= cur: return i
    return 8

def calculate_kyakushitsu(hist_list):
    valid_hist = []
    for h in hist_list:
        corners = h.get('corners', '-')
        total = h.get('total', '-')
        if corners == '-' or total == '-' or not str(total).isdigit(): continue
        c_list = corners.split('-')
        first_c = c_list[0].strip()
        if not first_c.isdigit(): continue
        pos = int(first_c)
        t_num = int(total)
        if t_num > 0: valid_hist.append(pos / t_num)
            
    if len(valid_hist) < 2: return "ー"
    avg_pos = sum(valid_hist) / len(valid_hist)
    if avg_pos <= 0.15: return "◀◁◁◁"
    elif avg_pos <= 0.25: return "◀◀◁◁"
    elif avg_pos <= 0.40: return "◁◀◁◁"
    elif avg_pos <= 0.55: return "◁◀◀◁"
    elif avg_pos <= 0.70: return "◁◁◀◁"
    elif avg_pos <= 0.85: return "◁◁◀◀"
    else: return "◁◁◁◀"

def get_agari_rank_from_url(url, target_name):
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        res.encoding = 'shift_jis'
        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table: return "-", "-", "-"
        rows = table.find_all("tr")
        header = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        n_idx = next((i for i, h in enumerate(header) if "馬名" in h), -1)
        a_idx = next((i for i, h in enumerate(header) if any(x in h for x in ["上り","3F"])), -1)
        time_idx = next((i for i, h in enumerate(header) if "タイム" in h), -1)
        pop_idx = next((i for i, h in enumerate(header) if "人気" in h), -1)
        
        if n_idx == -1 or a_idx == -1: return "-", "-", "-"
        
        times, t_val, t_time, t_pop = [], None, "-", "-"
        target = re.sub(r'[\s　]+', '', target_name)
        for row in rows[1:]:
            cols = row.find_all(["td", "th"])
            if len(cols) <= max(n_idx, a_idx): continue
            name = re.sub(r'[\s　]+', '', cols[n_idx].get_text(strip=True))
            m = re.search(r'(\d+\.\d+)', cols[a_idx].get_text(strip=True))
            if m:
                v = float(m.group(1))
                times.append(v)
                if target in name:
                    t_val = v
                    if time_idx != -1 and time_idx < len(cols): t_time = cols[time_idx].get_text(strip=True)
                    if pop_idx != -1 and pop_idx < len(cols): t_pop = cols[pop_idx].get_text(strip=True)
        agari = f"{sorted(times).index(t_val)+1}/{len(times)}" if t_val else "-"
        return agari, t_time, t_pop
    except Exception as e:
        print("get_agari error:", e)
        return "-", "-", "-"

def fetch_baba_info(venue):
    if not venue: return None
    try:
        cushion_val = "-"
        res_c = requests.get("https://www.jra.go.jp/keiba/baba/_data_cushion.html", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res_c.status_code == 200:
            res_c.encoding = 'shift_jis'
            soup_c = BeautifulSoup(res_c.text, "html.parser")
            venue_div = soup_c.find("div", title=venue)
            if venue_div:
                unit = venue_div.find("div", class_="unit")
                if unit and unit.find("div", class_="cushion"):
                    cushion_val = unit.find("div", class_="cushion").get_text(strip=True)

        turf_cond_str, dirt_cond_str = "良", "良"
        turf_g, turf_4c, dirt_g, dirt_4c = "-", "-", "-", "-"
        
        res_m = requests.get("https://www.jra.go.jp/keiba/baba/_data_moist.html", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res_m.status_code == 200:
            res_m.encoding = 'shift_jis'
            soup_m = BeautifulSoup(res_m.text, "html.parser")
            venue_div_m = soup_m.find("div", title=venue)
            if venue_div_m:
                unit_m = venue_div_m.find("div", class_="unit")
                if unit_m:
                    turf_div = unit_m.find("div", class_="turf")
                    if turf_div:
                        g_span = turf_div.find("span", class_="mg")
                        c4_span = turf_div.find("span", class_="m4c")
                        if g_span: turf_g = g_span.get_text(strip=True)
                        if c4_span: turf_4c = c4_span.get_text(strip=True)
                        cond = g_span.get("data-condition") if g_span else None
                        if cond == "hard": turf_cond_str = "良"
                        elif cond == "wet": turf_cond_str = "稍重"
                        elif cond == "soft": turf_cond_str = "重"
                        elif cond == "heavy": turf_cond_str = "不良"

                    dirt_div = unit_m.find("div", class_="dirt")
                    if dirt_div:
                        g_span_d = dirt_div.find("span", class_="mg")
                        c4_span_d = dirt_div.find("span", class_="m4c")
                        if g_span_d: dirt_g = g_span_d.get_text(strip=True)
                        if c4_span_d: dirt_4c = c4_span_d.get_text(strip=True)
                        cond_d = g_span_d.get("data-condition") if g_span_d else None
                        if cond_d == "hard": dirt_cond_str = "良"
                        elif cond_d == "wet": dirt_cond_str = "稍重"
                        elif cond_d == "soft": dirt_cond_str = "重"
                        elif cond_d == "heavy": dirt_cond_str = "不良"

        base_info = f"馬場情報 [{venue}] "
        turf_info = f"芝: {turf_cond_str} (水分: G前{turf_g}% 4角{turf_4c}% クッション値: {cushion_val})" 
        dirt_info = f" / ダート: {dirt_cond_str} (水分: G前{dirt_g}% 4角{dirt_4c}%)"
        return base_info + turf_info + dirt_info
    except:
        return f"馬場情報 [{venue}]：取得失敗"

def fetch_history_data(c, url, name, mode, is_first):
    raw_text = c.get_text(" ", strip=True)
    cond_tag = c.find("span", class_="condition")
    condition = cond_tag.get_text(strip=True) if cond_tag else "-"
    
    corners_list = [li.get_text(strip=True) for li in c.find_all("li", title=re.compile(r"コーナー通過順位"))]
    if not corners_list:
        m_corners = re.search(r'(\d{1,2}(?:\s*-\s*\d{1,2})+)', raw_text)
        if m_corners: corners_list = [x.strip() for x in m_corners.group(1).split('-')]
    
    agari_rank, run_time, pop_rank = "-", "-", "-"
    link = c.find("a")
    # TBD: タイムアウト対策で一時的に全て並列化または簡易化
    if (mode == "詳細" or is_first) and link:
        agari_rank, run_time, pop_rank = get_agari_rank_from_url(urljoin("https://www.jra.go.jp/JRADB/", link['href']), name)
    
    weight_tag, kinryo_tag = c.find("p", class_="h_weight"), c.find("div", class_="weight")
    course_m = re.search(r'(\d{3,4}\s*(?:芝|ダ|障))', raw_text)
    
    place_m = re.search(r'(中山|東京|京都|阪神|中京|札幌|函館|福島|新潟|小倉)', raw_text)
    place = place_m.group(1) if place_m else "-"
    
    race_name_tag = c.find("a")
    race_name = race_name_tag.get_text(strip=True) if race_name_tag else "-"
    
    rank_m = re.search(r'(\d{1,2})\s*着', raw_text)
    rank = rank_m.group(1) if rank_m else "-"
    total_m = re.search(r'(\d{1,2})\s*頭', raw_text)
    total = total_m.group(1) if total_m else "-"

    jockey = "-"
    if kinryo_tag:
        kinryo_val = re.sub(r'[^\d.]', '', kinryo_tag.get_text(strip=True))
        if kinryo_val:
            j_m = re.search(r'人気\s+([^\d]+?)\s+' + re.escape(kinryo_val), raw_text)
            if not j_m: j_m = re.search(r'番\s+([^\d]+?)\s+' + re.escape(kinryo_val), raw_text)
            if j_m:
                jockey = j_m.group(1).strip()
                jockey = re.sub(r'.*人気\s*', '', jockey).strip()

    return {
        "raw": raw_text, 
        "condition": condition, 
        "corners": "-".join(corners_list) if corners_list else "-", 
        "agari_rank": agari_rank, 
        "course": course_m.group(0) if course_m else "-", 
        "weight": weight_tag.get_text(strip=True) if weight_tag else "-", 
        "kinryo": kinryo_tag.get_text(strip=True) if kinryo_tag else "-",
        "place": place, "race_name": race_name, "rank": rank, "total": total,
        "run_time": run_time, "pop_rank": pop_rank, "jockey": jockey
    }


def parse_horse_row(row, url, curr_date_fmt, mode):
    cells = row.find_all(["td", "th"])
    off = 1 if "着" in cells[0].get_text() or (len(cells) > 2 and cells[2].get_text().isdigit()) else 0
    try:
        num = int(cells[1+off].get_text(strip=True))
        h_cell = cells[2+off].get_text(" ", strip=True)
        name = h_cell.split()[0]
        
        sire_m = re.search(r'父：\s*([^\s]+)', h_cell)
        sire = sire_m.group(1) if "父" in h_cell and sire_m else "-"
        dam_m = re.search(r'母：\s*([^\s]+)', h_cell)
        dam = dam_m.group(1) if "母" in h_cell and dam_m else "-"
        bms_m = re.search(r'母の父：\s*([^ \)]+)', h_cell)
        bms = bms_m.group(1) if "母の父" in h_cell and bms_m else "-"
        
        jock_raw = cells[3+off].get_text(" ", strip=True).split()
        sex_age, kg = jock_raw[0], jock_raw[1]
        jock = re.sub(r'[\s　kgkｇ0-9.]', '', "".join(jock_raw[2:]))
        
        o_val = 999.0
        for cell in cells:
            odds_div = cell.find("div", class_="odds_line")
            if odds_div and odds_div.find("strong"):
                o_val = float(odds_div.find("strong").get_text(strip=True))
                break

        hist_cells = [c for c in cells if "202" in c.get_text()]
        hist_list = []
        
        # Parallel fetch for history to avoid Vercel timeouts (optional speedup)
        # Using a thread pool
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            for i, c in enumerate(hist_cells[:4]):
                futures.append(executor.submit(fetch_history_data, c, url, name, mode, i==0))
            for f in futures:
                hist_list.append(f.result())

        iv = "-"
        if hist_list:
            p_date_m = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)', hist_list[0]['raw'])
            if p_date_m and curr_date_fmt:
                diff = (datetime.strptime(curr_date_fmt, "%Y年%m月%d日") - datetime.strptime(p_date_m.group(1), "%Y年%m月%d日")).days
                iv = f"中{(diff//7)-1}週" if diff > 7 else "連闘"

        return {"num": num, "odds": o_val, "iv": iv, "name": name, "sex_age": sex_age, 
                "kyakushitsu": calculate_kyakushitsu(hist_list), "kg": kg, "jock": jock, 
                "affi": "栗東" if "栗東" in h_cell else "美浦", "sire": sire, "dam": dam, "bms": bms, "hist": hist_list}
    except Exception as e:
        print("Row parse error:", e)
        return None

def fetch_races_for_venue(text, url):
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        races_map = {}
        for a in soup.find_all("a", href=True):
            if "CNAME=" in a['href']:
                m = re.search(r'CNAME=pw\d+dde\d+(\d{2})\d{8}', a['href'])
                if m and 1 <= int(m.group(1)) <= 12:
                    races_map[int(m.group(1))] = urljoin("https://www.jra.go.jp/JRADB/", a['href'])
        races = [{"r": k, "url": v} for k, v in sorted(races_map.items())]
        return {"text": text, "url": url, "races": races}
    except:
        return {"text": text, "url": url, "races": []}

def build_matrix_data(soup):
    nav_lists = soup.find_all("ul", class_="data_line_list")
    seen_urls = set()
    venue_links = []
    
    for ul in nav_lists:
        for a in ul.find_all("a", href=True):
            href = urljoin("https://www.jra.go.jp/JRADB/", a['href'])
            if href in seen_urls: continue
            
            date_text = ""
            matches = re.findall(r'(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', href)
            if matches:
                try:
                    y, mo, d = matches[-1]
                    wd = ["月", "火", "水", "木", "金", "土", "日"][datetime(int(y), int(mo), int(d)).weekday()]
                    date_text = f"{int(mo)}/{int(d)}({wd}) "
                except: pass
                
            text = date_text + a.get_text(strip=True)
            seen_urls.add(href)
            venue_links.append((text, href))
            
    matrix_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_races_for_venue, text, url) for text, url in venue_links]
        for f in futures:
            res = f.result()
            if res and res["races"]:
                matrix_data.append(res)
                
    return matrix_data

@app.route('/api/scrape', methods=['POST'])
def scrape():
    data = request.json or {}
    url = data.get('url')
    mode = data.get('mode', '簡易')
    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        res.encoding = 'shift_jis'
        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text()

        v_code_match = re.search(r'CNAME=pw\d+dde\d{2}(\d{2})', url)
        v_code = v_code_match.group(1) if v_code_match else "00"
        venue = VENUE_MAP.get(v_code, "会場不明")

        curr_date_m = re.search(r'(\d+)月(\d+)日', page_text)
        curr_date = f"{curr_date_m.group(1)}/{curr_date_m.group(2)}" if curr_date_m else "??/??"
        
        year_match = re.search(r'dde\d{2}\d{2}(\d{4})', url)
        year_val = year_match.group(1) if year_match else str(datetime.now().year)
        curr_date_fmt = f"{year_val}年{curr_date_m.group(1)}月{curr_date_m.group(2)}日" if curr_date_m else None

        race_num_m = re.search(r'(\d+)\s*レース', page_text)
        race_idx = int(race_num_m.group(1)) if race_num_m else 1

        dist_match = re.search(r'(\d,?\d{2,3})メートル', page_text)
        dist_val = int(dist_match.group(1).replace(",", "")) if dist_match else 0
        race_type = "ダート" if "ダート" in page_text else "芝"
        
        race_name_tag = soup.find(class_="race_name")
        race_name = race_name_tag.get_text(strip=True) if race_name_tag else "レース名不明"
        race_label = f"【{venue} {race_idx}R】{race_type}{dist_val}m　{race_name}"

        def get_race_class(name):
            name_str = str(name).upper()
            if "G1" in name_str or "G2" in name_str or "G3" in name_str: return "重賞"
            if "JG1" in name_str or "JG2" in name_str or "JG3" in name_str: return "重賞"
            if "(L)" in name_str or "オープ" in name_str or "OP" in name_str: return "オープン"
            if "3勝" in name_str or "３勝" in name_str: return "3勝クラス"
            if "2勝" in name_str or "２勝" in name_str: return "2勝クラス"
            if "1勝" in name_str or "１勝" in name_str: return "1勝クラス"
            if "未勝利" in name_str: return "未勝利"
            if "新馬" in name_str: return "新馬"
            return "オープン"
        
        race_class = get_race_class(race_name)

        baba_info = fetch_baba_info(venue)
        matrix = build_matrix_data(soup)

        target_table = next((t for t in soup.find_all("table") if "馬名" in t.get_text()), None)
        rows = [r for r in target_table.find_all("tr") if len(r.find_all(["td", "th"])) >= 5 and "馬名" not in r.get_text()] if target_table else []

        # Parse horses using threads for speed
        scraped_data = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(parse_horse_row, r, url, curr_date_fmt, mode) for r in rows]
            for f in futures:
                res_horse = f.result()
                if res_horse: scraped_data.append(res_horse)

        scraped_data.sort(key=lambda x: x['odds'])
        for i, h in enumerate(scraped_data): h['pop'] = str(i + 1)
        scraped_data.sort(key=lambda x: x['num'])

        # Analysis
        base_dir = get_base_dir()
        sire_lineage = analysis.load_sire_lineage(base_dir)
        criteria = analysis.load_csv_criteria(venue, base_dir)
        
        # Load mawari data
        mawari_map = {}
        try:
            import pandas as pd
            # 新構成: DATA/共通/mawari.csv
            mawari_path = os.path.join(base_dir, "DATA", "共通", "mawari.csv")
            if not os.path.exists(mawari_path):
                # 旧構成
                mawari_path = os.path.join(base_dir, "csv", "mawari.csv")
            
            df = pd.read_csv(mawari_path, header=None, encoding='utf-8-sig')
            mawari_map = {str(row[0]).strip(): str(row[1]).strip() for _, row in df.iterrows()}
        except: pass

        race_context = {"type": race_type, "dist": dist_val, "total_horses": len(scraped_data), "venue": venue}
        
        criteria_lines = []
        for c in criteria:
            if c['type'] == race_type and c['dist_min'] <= dist_val <= c['dist_max']:
                conds = [c[k] for k in ['c1', 'c2', 'c3'] if analysis.is_valid_cond(c[k])]
                criteria_lines.append(f"・{'×'.join(conds)}")

        has_double_circle = False
        horses_out = []
        for h in scraped_data:
            h['w_num'] = calculate_waku(h['num'], len(scraped_data))
            grade, det = analysis.evaluate_ultra(h, race_context, criteria, sire_lineage, mawari_map)
            h['grade'] = grade
            h['ultra_details'] = det
            if grade == "◎": has_double_circle = True
            horses_out.append(h)

        # Feature Course Memo
        feature_text = analysis.load_course_feature(venue, race_type, dist_val, base_dir)
        
        # Notable Sires (NEW)
        sire_result = analysis.load_notable_sires(venue, race_type, dist_val, base_dir)
        notable_sires = sire_result.get("sires", [])
        debug_sire_path = sire_result.get("debug", {})
  
        try:
            target_file = debug_sire_path.get("attempted_path", "")
            if not os.path.exists(target_file):
                # 親ディレクトリの中身を確認
                parent_dir = os.path.dirname(target_file)
                if os.path.exists(parent_dir):
                    debug_sire_path["dir_contents"] = os.listdir(parent_dir)
                else:
                    debug_sire_path["api_root_contents"] = os.listdir(base_dir)
        except: pass

        sire_ranking_map = {s['name']: s['rank'] for s in notable_sires}

        # 更新された馬データ（ランク付与）
        for h in horses_out:
            s_name = h.get('sire', '-')
            if s_name in sire_ranking_map:
                h['sire_rank'] = sire_ranking_map[s_name]

        harab_index = "-"
        if feature_text:
            harab_m = re.search(r'【?波乱指数】?[:：\s]*([^\n]+)', feature_text)
            if harab_m:
                harab_index = harab_m.group(1).strip()
                feature_text = feature_text.replace(harab_m.group(0), "").strip()
            
            first_line_m = re.match(r'^(.*?[芝ダ障害]\d+m\s*)\n', feature_text)
            if first_line_m:
                feature_text = feature_text[len(first_line_m.group(0)):].strip()

        # Course record extraction
        course_record_text = ""
        record_elem = soup.find("div", class_=re.compile("record"))
        if record_elem and "コースレコード" in record_elem.get_text():
            course_record_text = record_elem.get_text(separator=' ', strip=True)
        else:
            r_str = soup.find(string=re.compile(r'コースレコード'))
            if r_str:
                r_parent = r_str.find_parent(class_=re.compile("record"))
                if r_parent:
                    course_record_text = r_parent.get_text(separator=' ', strip=True)
                else:
                    course_record_text = r_str.parent.get_text(separator=' ', strip=True)
                    if course_record_text == "コースレコード" and r_str.parent.parent:
                        course_record_text = r_str.parent.parent.get_text(separator=' ', strip=True)
        
        course_record_text = re.sub(r'\s+', ' ', course_record_text).strip()
        if course_record_text == "コースレコード":
            cr_m = re.search(r'コースレコード[\s\S]{0,30}?(\d{1,2}:\d{2}\.\d)', soup.get_text(separator=' '))
            if cr_m: course_record_text = "コースレコード " + cr_m.group(1)

        course_image = ""
        try:
            import json
            with open(os.path.join(base_dir, 'jra_images.json'), 'r', encoding='utf-8') as f:
                img_map = json.load(f)
                key = f"{race_type}{dist_val}"
                course_image = img_map.get(venue, {}).get(key, "")
        except: pass

        return jsonify({
            "success": True,
            "race_info": race_label,
            "venue": venue,
            "race_type": race_type,
            "dist_val": dist_val,
            "race_class": race_class,
            "baba_info": baba_info,
            "course_record": course_record_text,
            "course_image": course_image,
            "criteria_lines": criteria_lines,
            "harab_index": harab_index,
            "feature_text": feature_text,
            "notable_sires": notable_sires,
            "debug_sire": debug_sire_path,
            "matrix_data": matrix,
            "horses": horses_out,
            "has_double_circle": has_double_circle,
            "race_type": race_type,
            "dist_val": dist_val,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route('/api/latest_url', methods=['GET'])
def latest_url():
    day = request.args.get('day', type=int)
    try:
        # Javascript getDay(): 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
        if day in [1, 2, 3, 4]:
            return jsonify({"error": "出馬表が公開されていないため、取得に失敗しました。"}), 400
        else:
            # 金・土・日の場合、JRAの今週の重賞ページから出馬表を探す
            res = requests.get('https://www.jra.go.jp/keiba/thisweek/', headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            res.encoding = 'shift_jis'
            soup = BeautifulSoup(res.text, "html.parser")
            
            # /keiba/thisweek/YYYY/MMDD_X/horse.html のような重賞ページを探す
            horse_link = soup.find("a", href=re.compile(r'/keiba/thisweek/\d{4}/\d{4}_\d+/horse\.html'))
            if horse_link:
                horse_url = urljoin("https://www.jra.go.jp/", horse_link['href'])
                r2 = requests.get(horse_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                r2.encoding = 'shift_jis'
                soup2 = BeautifulSoup(r2.text, "html.parser")
                a = soup2.find("a", href=re.compile(r'CNAME=pw01dde'))
                if a: 
                    return jsonify({"url": urljoin("https://www.jra.go.jp/JRADB/", a['href'])})
            
            # 万が一horse.htmlが見つからなかった場合のフォールバック（トップから直接アクセスDを探す）
            for a in soup.find_all("a", href=True):
                if 'accessD.html' in a['href']:
                    return jsonify({"url": urljoin("https://www.jra.go.jp/", a['href'])})
                
        return jsonify({"error": "最新のURLが見つかりませんでした。"}), 404
    except Exception as e:
        return jsonify({"error": f"URL取得エラー: {str(e)}"}), 500

@app.route('/api/past_data', methods=['GET'])
def get_past_data_api():
    place = request.args.get('place')
    track_type = request.args.get('track_type')
    distance = request.args.get('distance')
    condition = request.args.get('condition')
    race_class = request.args.get('race_class')

    if not all([place, track_type, distance]):
        return jsonify({"error": "Missing parameters (place, track_type, distance)"}), 400

    try:
        from past_data_service import get_past_data
        base_dir = get_base_dir()
        result = get_past_data(base_dir, place, track_type, distance, condition, race_class)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route('/api/ai_predict', methods=['POST'])
def ai_predict():
    data = request.json or {}
    if data.get('password') != 'oso18':
        return jsonify({"error": "パスワードが間違っています。AI予想の実行権限がありません。"}), 401
        
    api_key = data.get('api_key') or os.environ.get("GEMINI_API_KEY")
    if not api_key: return jsonify({"error": "GEMINI_API_KEY is missing."}), 400
    
    prompt = data.get('prompt')
    if not prompt: return jsonify({"error": "No prompt provided."}), 400

    try:
        genai.configure(api_key=api_key)
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt, safety_settings=safety_settings)
        
        try:
            result_text = response.text
        except ValueError:
            result_text = "AIから有効な回答が得られませんでした。フィードバックを確認してください。"
            
        return jsonify({"success": True, "result": result_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
