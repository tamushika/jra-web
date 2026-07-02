"""
JRA解析ツール 簡易版 (AI機能・DB機能なし)
==========================================
必要: pip install flask flask-cors requests beautifulsoup4
起動: python jra_lite.py
"""
import csv
import json
import os
import re
import sys
import threading
import webbrowser
import concurrent.futures
from datetime import datetime
from urllib.parse import urljoin

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests as req_lib
from bs4 import BeautifulSoup

# ─── 設定 ────────────────────────────────────────────────────────────────────
PORT = 5001
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "api", "data_files")

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="/")
CORS(app)

VENUE_MAP = {
    "06": "中山", "08": "京都", "05": "東京", "09": "阪神", "07": "中京",
    "04": "新潟", "03": "福島", "10": "小倉", "01": "札幌", "02": "函館",
}
VENUE_SLUG_MAP = {
    "中山": "nakayama", "阪神": "hanshin", "東京": "tokyo", "京都": "kyoto",
    "中京": "chukyo", "新潟": "niigata", "福島": "fukushima", "小倉": "kokura",
    "札幌": "sapporo", "函館": "hakodate", "共通": "common",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── 静的ファイル配信 ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index_lite.html")


# ─── CSV ユーティリティ (pandas不使用) ────────────────────────────────────────
def _read_csv_rows(filepath, encoding="utf-8-sig"):
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding=encoding, newline="") as f:
            return [[str(v).strip() for v in row] for row in csv.reader(f)]
    except Exception:
        return []


def _read_csv_dicts(filepath, encoding="utf-8-sig"):
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding=encoding, newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except Exception:
        return []


# ─── 分析関数 (analysis.py 相当、pandas不使用) ─────────────────────────────────
def get_class_rank(cls_str):
    if not cls_str:
        return 0
    if "GⅠ" in cls_str or "G1" in cls_str: return 90
    if "GⅡ" in cls_str or "G2" in cls_str: return 80
    if "GⅢ" in cls_str or "G3" in cls_str: return 70
    if any(k in cls_str for k in ["OP", "オープン", "(L)", "リステッド"]): return 60
    if "3勝" in cls_str or "1600万" in cls_str: return 50
    if "2勝" in cls_str or "1000万" in cls_str: return 40
    if "1勝" in cls_str or "500万" in cls_str: return 30
    if "未勝利" in cls_str: return 20
    if "新馬" in cls_str: return 10
    return 0


def is_valid_cond(c):
    return c and str(c).strip() not in ["nan", "-", "", "None"]


def load_csv_criteria(venue_name):
    slug = VENUE_SLUG_MAP.get(venue_name, "kyoto")
    path = os.path.join(DATA_DIR, slug, "criteria", "criteria.csv")
    criteria = []
    for row in _read_csv_rows(path):
        if len(row) < 6:
            continue
        nums = re.findall(r"\d+", row[2])
        if not nums:
            continue
        criteria.append({
            "id": row[0], "type": row[1],
            "dist_min": int(nums[0]), "dist_max": int(nums[1]) if len(nums) > 1 else int(nums[0]),
            "c1": row[3], "c2": row[4], "c3": row[5],
        })
    return criteria


def load_sire_lineage():
    path = os.path.join(DATA_DIR, "common", "syuboba.csv")
    lineage = {}
    if not os.path.exists(path):
        return lineage
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if not parts:
                    continue
                group = parts[0].replace("種牡馬", "")
                for sire in parts[1:]:
                    lineage[sire] = group
    except Exception:
        pass
    return lineage


def load_mawari_map():
    path = os.path.join(DATA_DIR, "common", "mawari.csv")
    rows = _read_csv_rows(path)
    return {row[0]: row[1] for row in rows if len(row) >= 2}


def load_course_feature(venue, race_type, distance):
    slug = VENUE_SLUG_MAP.get(venue, "kyoto")
    path = os.path.join(DATA_DIR, slug, "course_info", f"{race_type}{distance}.txt")
    if not os.path.exists(path):
        return f"【情報】{venue} {race_type}{distance}m の特徴データは登録されていません。"
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except Exception as e:
        return f"エラー: {e}"


def load_notable_sires(venue, race_type, distance):
    slug = VENUE_SLUG_MAP.get(venue, "kyoto")
    path = os.path.join(DATA_DIR, slug, "sire", f"{race_type}{distance}.csv")
    debug = {"attempted_path": path, "exists": os.path.exists(path)}
    if not debug["exists"]:
        return {"sires": [], "debug": debug}
    try:
        sires = []
        for i, row in enumerate(_read_csv_rows(path)):
            if len(row) < 4:
                continue
            sires.append({
                "rank": i + 1, "name": row[0],
                "win_rate": f"{float(row[1]):.1f}%",
                "quinella_rate": f"{float(row[2]):.1f}%",
                "show_rate": f"{float(row[3]):.1f}%",
            })
        return {"sires": sires, "debug": debug}
    except Exception as e:
        debug["error"] = str(e)
        return {"sires": [], "debug": debug}


def load_konochichi_criteria(venue_name, race_type, distance):
    slug = VENUE_SLUG_MAP.get(venue_name, "kyoto")
    path = os.path.join(DATA_DIR, slug, "criteria", "criteria_konochichi.csv")
    lines = []
    for row in _read_csv_rows(path):
        if len(row) < 6:
            continue
        if row[1] != race_type:
            continue
        nums = re.findall(r"\d+", row[2])
        if not nums:
            continue
        d_min, d_max = int(nums[0]), int(nums[1]) if len(nums) > 1 else int(nums[0])
        if not (d_min <= distance <= d_max):
            continue
        conds = [row[k] for k in (3, 4, 5) if k < len(row) and is_valid_cond(row[k])]
        if conds:
            suffix = "(要確認)" if len(row) > 6 and row[6] == "flagged" else ""
            lines.append(f"・{'×'.join(conds)}{suffix}")
    return lines


def load_course_tips(venue_name, race_type, distance):
    path = os.path.join(DATA_DIR, "common", "course_tips.csv")
    tips = []
    for row in _read_csv_dicts(path):
        if row.get("競馬場", "").strip() != venue_name:
            continue
        rt = row.get("芝ダート", "").strip()
        if rt and rt != race_type:
            continue
        nums = re.findall(r"\d+", row.get("距離", ""))
        if nums and int(nums[0]) != distance:
            continue
        hint = row.get("狙い方ヒント", "").strip()
        if hint:
            tips.append(hint)
    return tips


_RACE_GRADE_RE = re.compile(r"第\d+回|[GgＧ][ⅠⅡⅢ123一二三]+")
_RACE_PAREN_RE = re.compile(r"[（(]([^）)]*)[）)]")
_race_conditions_cache = None


def _race_name_candidates(name):
    base = _RACE_GRADE_RE.sub("", name)
    alias = _RACE_PAREN_RE.search(base)
    main = _RACE_PAREN_RE.sub("", base).strip()
    cands = {main} if main else set()
    if alias:
        cands.add(alias.group(1).strip())
    return {c for c in cands if c}


def load_race_conditions(race_name):
    global _race_conditions_cache
    if not race_name:
        return []
    if _race_conditions_cache is None:
        path = os.path.join(DATA_DIR, "common", "race_conditions.csv")
        _race_conditions_cache = _read_csv_dicts(path)
    targets = _race_name_candidates(race_name)
    if not targets:
        return []
    matched = []
    for row in _race_conditions_cache:
        book = _race_name_candidates(str(row.get("レース名", "")))
        if any(b in t or t in b for b in book for t in targets):
            matched.append({
                "kubun": row.get("区分", ""),
                "header": row.get("小見出し", ""),
                "conclusion": row.get("結論", ""),
            })
    return matched


_sire_buysell_cache = None


def load_sire_buysell(sire_name):
    global _sire_buysell_cache
    if not sire_name or sire_name == "-":
        return []
    if _sire_buysell_cache is None:
        path = os.path.join(DATA_DIR, "common", "sire_buysell.csv")
        mapping = {}
        for row in _read_csv_dicts(path):
            name = row.get("種牡馬名", "").strip()
            mapping.setdefault(name, []).append({
                "kubun": row.get("区分", "").strip(),
                "condition": row.get("条件文", "").strip(),
            })
        _sire_buysell_cache = mapping
    return _sire_buysell_cache.get(sire_name, [])


def check_condition(cond, h, r, sire_lineage, mawari_map):
    DEFAULT_MAWARI = {
        "中山": "右", "東京": "左", "京都": "右", "阪神": "右", "中京": "左",
        "新潟": "左", "福島": "右", "小倉": "右", "札幌": "右", "函館": "右",
    }
    if not is_valid_cond(cond):
        return True
    try:
        if "騎手" in cond:
            target_jock = cond.replace("騎手が", "").strip()
            clean_cond = re.sub(r"[▲△☆★◇\s　kgkｇ]", "", target_jock)
            clean_jock = re.sub(r"[▲△☆★◇\s　kgkｇ]", "", h["jock"])
            if "乗り替わり" not in target_jock:
                return (clean_cond in clean_jock) or (clean_jock in clean_cond)

        if "回り" in cond:
            target = "右" if "右" in cond else "左"
            raw_hist = h["hist"][0]["raw"] if h["hist"] else ""
            venues_pattern = "|".join(DEFAULT_MAWARI.keys())
            match = re.search(f"({venues_pattern})", raw_hist)
            if match:
                pv = match.group(1)
                actual = mawari_map.get(pv) or DEFAULT_MAWARI.get(pv, "")
                if target not in actual:
                    return False
            else:
                return False
            return True

        if "週" in cond or "連闘" in cond:
            actual_iv = h.get("iv", "")
            if actual_iv == "-" or not actual_iv:
                return False
            if "週" in cond:
                tm, am = re.search(r"(\d+)", cond), re.search(r"(\d+)", actual_iv)
                if tm and am:
                    if int(am.group(1)) > int(tm.group(1)):
                        return False
                elif "連闘" not in actual_iv:
                    return False
            if "連闘" in cond and "連闘" not in actual_iv:
                return False
            return True

        if "着" in cond:
            actual_raw = h["hist"][0]["raw"] if h["hist"] else ""
            rm = re.search(r"(\d+)\s*着", actual_raw)
            if not rm:
                return False
            actual = int(rm.group(1))
            tm = re.search(r"(\d+)", cond)
            if tm:
                target = int(tm.group(1))
                if "以内" in cond and actual > target: return False
                if "以下" in cond and actual < target: return False
                if "以内" not in cond and "以下" not in cond and actual != target: return False
            return True

        if "角" in cond or "通過順" in cond:
            passing = h["hist"][0].get("corners") or h["hist"][0].get("passing", "") if h["hist"] else ""
            if not passing or passing in ["---", "-", ""]:
                return False
            try:
                raw_last = passing.split("-")[-1]
                am = re.search(r"(\d+)", raw_last)
                if not am:
                    return False
                actual = int(am.group(1))
            except Exception:
                return False
            nums_in_cond = re.findall(r"(\d+)", cond)
            if nums_in_cond:
                target = int(nums_in_cond[-1])
                if ("以内" in cond or "以下" in cond) and actual > target: return False
                if "番手" in cond and "以内" not in cond and "以下" not in cond and actual != target: return False
            return True

        if "上がり" in cond:
            actual_str = h["hist"][0].get("agari_rank", "-") if h["hist"] else "-"
            if not actual_str or actual_str == "-":
                return False
            m_rank = re.search(r"(\d+)", actual_str)
            if not m_rank:
                return False
            actual = int(m_rank.group(1))
            tm = re.search(r"(\d+)", cond)
            if tm:
                target = int(tm.group(1))
                if "以内" in cond and actual > target: return False
                if "以下" in cond and actual < target: return False
                if "位" in cond and "以内" not in cond and "以下" not in cond and actual != target: return False
            return True

        if "頭" in cond:
            curr_total = r.get("total_horses", 0)
            tm = re.search(r"(\d+)", cond)
            is_prev = "前走" in cond or "出走頭数" in cond
            if is_prev:
                prev_raw = h["hist"][0]["raw"] if h["hist"] else ""
                ptm = re.search(r"着\s*(\d+)\s*頭", prev_raw) or re.search(r"(\d+)\s*頭", prev_raw)
                if not ptm:
                    return False
                actual = int(ptm.group(1))
            else:
                actual = curr_total
            if "今回より多い" in cond and actual <= curr_total: return False
            elif "同頭数以上" in cond and actual < curr_total: return False
            elif tm:
                target = int(tm.group(1))
                if "以上" in cond and actual < target: return False
                if ("以下" in cond or "以内" in cond) and actual > target: return False
            return True

        if "生産者" in cond:
            target = cond.replace("生産者が", "").strip()
            producer = h.get("producer", "")
            if not producer:
                return False
            return target in producer or producer in target

        if "父" in cond:
            target = cond
            for prefix in ["父or母父が", "父・母父が", "父か母父が", "父が", "系", "以外の", "以外", "種牡馬"]:
                target = target.replace(prefix, "")
            target = re.sub(r"\s+", "", target).strip()
            match = (target == h["sire"]) or (target in sire_lineage.get(h["sire"], ""))
            if "母父" in cond:
                match = match or (target == h.get("bms", "")) or (target in sire_lineage.get(h.get("bms", ""), ""))
            if ("以外" in cond and match) or ("以外" not in cond and not match):
                return False
            return True

        venues = ["中山", "京都", "東京", "阪神", "中京", "新潟", "福島", "小倉", "札幌", "函館"]
        is_course_cond = any(k in cond for k in ["条件", "コース", "距離", "m", "場所", "ダート", "芝"]) or any(v in cond for v in venues)
        if "前走" in cond and is_course_cond:
            raw_hist = h["hist"][0]["raw"] if h["hist"] else ""
            curr_venue = r.get("venue", "")
            prev_course = h["hist"][0].get("course", "") if h["hist"] else ""
            adm = re.search(r"(\d+)", prev_course)
            actual_dist = int(adm.group(1)) if adm else 0
            prev_type = "芝" if "芝" in prev_course else "ダート" if "ダ" in prev_course else ""
            if "ダート" in cond and prev_type != "ダート": return False
            if "芝" in cond and "ダート" not in cond and prev_type != "芝": return False
            if "同条件" in cond or "同コース" in cond:
                vm = re.search("|".join(venues), raw_hist)
                pv = vm.group(0) if vm else ""
                if pv != curr_venue: return False
                if actual_dist != r.get("dist", 0): return False
                if prev_type != r.get("type", ""): return False
                return True
            vm = re.search("|".join(venues), raw_hist)
            pv = vm.group(0) if vm else ""
            if "同競馬場" in cond and pv != curr_venue: return False
            if "別競馬場" in cond and pv == curr_venue: return False
            if "中央場所" in cond and pv not in ["中山", "東京", "京都", "阪神"]: return False
            if "別距離" in cond:
                if actual_dist == r.get("dist", 0): return False
            elif "距離" in cond or "m" in cond:
                range_m = re.search(r"(\d+)\s*[~〜～]\s*(\d+)", cond)
                if range_m:
                    if not (int(range_m.group(1)) <= actual_dist <= int(range_m.group(2))): return False
                else:
                    tdm = re.search(r"(\d+)", cond)
                    target = int(tdm.group(1)) if tdm else r.get("dist", 0)
                    if "同距離超" in cond or "同距離越" in cond:
                        if actual_dist <= target: return False
                    elif "以上" in cond and tdm:
                        if actual_dist < target: return False
                    elif "以下" in cond and tdm:
                        if actual_dist > target: return False
                    else:
                        if actual_dist != target: return False
            target_v = next((v for v in venues if v in cond), None)
            if target_v and target_v != pv: return False
            return True

        if "枠" in cond or "番" in cond:
            if "最内枠" in cond: return h.get("w_num") == 1 or h.get("num") == 1
            if "大外枠" in cond: return h.get("num") == r.get("total_horses")
            nums = re.findall(r"\d+", cond)
            val = h.get("w_num") if "枠" in cond else h.get("num")
            if val is None: return False
            if len(nums) == 2:
                if not (int(nums[0]) <= val <= int(nums[1])): return False
            elif len(nums) == 1:
                target = int(nums[0])
                if "以内" in cond and val > target: return False
                elif "以内" not in cond and val != target: return False
            return True

        if any(k in cond for k in ["負担重量", "減量", "軽量", "斤量"]):
            syms = ["▲", "△", "☆", "★", "◇"]
            has_red = any(s in h.get("jock", "") for s in syms)
            if "無し" in cond: return not has_red
            if "有り" in cond: return has_red
            try:
                actual_kg = float(str(h.get("kg", "0")))
            except ValueError:
                actual_kg = 0.0
            tdm = re.search(r"(\d+(?:\.\d+)?)", cond)
            if tdm:
                target_kg = float(tdm.group(1))
                if "以上" in cond and actual_kg < target_kg: return False
                if ("以下" in cond or "以内" in cond) and actual_kg > target_kg: return False
                if "未満" in cond and actual_kg >= target_kg: return False
                if not any(k in cond for k in ["以上", "以下", "以内", "未満"]) and actual_kg != target_kg: return False
            return True

        if "馬体重" in cond:
            raw_w = h["hist"][0].get("weight", "") if h["hist"] else ""
            am = re.search(r"(\d{3})", str(raw_w))
            tm = re.search(r"(\d+)", cond)
            if not am or not tm: return False
            actual, target = int(am.group(1)), int(tm.group(1))
            if "以上" in cond and actual < target: return False
            if ("以下" in cond or "以内" in cond) and actual > target: return False
            return True

        if "クラス" in cond:
            if "前走クラスが今回以上" in cond:
                curr_rank = get_class_rank(r.get("class", ""))
                prev_raw = h["hist"][0]["raw"] if h["hist"] else ""
                prev_name = h["hist"][0].get("race_name", "") if h["hist"] else ""
                prev_rank = max(get_class_rank(prev_raw), get_class_rank(prev_name))
                if curr_rank == 0 or prev_rank == 0: return False
                return prev_rank >= curr_rank
            return True

        if "性別" in cond or "牝馬" in cond or "牡馬・セン" in cond:
            if "牝馬" in cond and "牝" not in h["sex_age"]: return False
            if "牡馬・セン" in cond and not any(x in h["sex_age"] for x in ["牡", "セ"]): return False
        if "歳" in cond:
            tm, am = re.search(r"(\d+)", cond), re.search(r"(\d+)", h["sex_age"])
            if tm and am:
                t, a = int(tm.group(1)), int(am.group(1))
                if "以上" in cond and a < t: return False
                if "以下" in cond and a > t: return False
                if "以上" not in cond and "以下" not in cond and t != a: return False
            return True
        if "所属" in cond or "関東馬" in cond or "関西馬" in cond:
            if ("美浦" in cond or "関東" in cond) and h["affi"] != "美浦": return False
            if ("栗東" in cond or "関西" in cond) and h["affi"] != "栗東": return False

        keywords = ["回り", "着", "角", "上がり", "歳", "牝", "牡", "父", "kg", "枠", "番",
                    "距離", "頭", "週", "斤量", "ダート", "体重", "場所", "所属", "クラス",
                    "条件", "馬齢", "性別", "間隔", "通過順", "負担重量", "順位", "生産者"]
        if not any(kw in cond for kw in keywords):
            target_jock = cond.replace("騎手が", "").strip()
            cc = re.sub(r"[▲△☆★◇\s　kgkｇ]", "", target_jock)
            cj = re.sub(r"[▲△☆★◇\s　kgkｇ]", "", h["jock"])
            if not cj or (cc not in cj and cj not in cc): return False

        if "乗り替わり" in cond:
            cur_j = re.sub(r"[▲△☆★◇\s　]", "", h["jock"])
            prev_raw = h["hist"][0]["raw"] if h["hist"] else ""
            pjm = re.search(r"人気\s+([^\s]+?)\s+\d+\.\d+\s*kg", prev_raw)
            if pjm:
                pj = re.sub(r"[▲△☆★◇\s　]", "", pjm.group(1))
                changed = (cur_j != pj)
                if "無し" in cond or "以外" in cond: return not changed
                return changed
            return False

        return True
    except Exception:
        return False


def evaluate_ultra(h, r, criteria, sire_lineage, mawari_map):
    best_grade, details = "", []
    for rule in criteria:
        if rule["type"] != r["type"] or not (rule["dist_min"] <= r["dist"] <= rule["dist_max"]):
            continue
        v2, v3 = is_valid_cond(rule["c2"]), is_valid_cond(rule["c3"])
        res1 = check_condition(rule["c1"], h, r, sire_lineage, mawari_map)
        res2 = check_condition(rule["c2"], h, r, sire_lineage, mawari_map) if res1 else False
        res3 = check_condition(rule["c3"], h, r, sire_lineage, mawari_map) if (res1 and res2) else False
        grade = ""
        if v3:
            if res1 and res2 and res3: grade = "◎"
            elif res1 and res2: grade = "〇"
            elif res1: grade = "△"
        elif v2:
            if res1 and res2: grade = "〇"
            elif res1: grade = "△"
        else:
            if res1: grade = "△"
        if grade:
            details.append(f"項番{rule['id']}: {grade} ({rule['c1']} | {rule['c2']} | {rule['c3']})")
            if grade == "◎" or (grade == "〇" and best_grade != "◎") or (grade == "△" and not best_grade):
                best_grade = grade
    return best_grade, details


# ─── スクレイピングヘルパー ───────────────────────────────────────────────────
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
    valid = []
    for h in hist_list:
        corners = h.get("corners", "-")
        total = h.get("total", "-")
        if corners == "-" or total == "-" or not str(total).isdigit():
            continue
        parts = corners.split("-")
        fc = parts[0].strip()
        if not fc.isdigit():
            continue
        t_num = int(total)
        if t_num > 0:
            valid.append(int(fc) / t_num)
    if len(valid) < 2:
        return "ー"
    avg = sum(valid) / len(valid)
    if avg <= 0.15: return "◀◁◁◁"
    elif avg <= 0.25: return "◀◀◁◁"
    elif avg <= 0.40: return "◁◀◁◁"
    elif avg <= 0.55: return "◁◀◀◁"
    elif avg <= 0.70: return "◁◁◀◁"
    elif avg <= 0.85: return "◁◁◀◀"
    else: return "◁◁◁◀"


def get_agari_rank_from_url(url, target_name):
    try:
        res = req_lib.get(url, headers=HEADERS, timeout=5)
        res.encoding = "shift_jis"
        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return "-", "-", "-"
        rows = table.find_all("tr")
        header = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        n_idx = next((i for i, h in enumerate(header) if "馬名" in h), -1)
        a_idx = next((i for i, h in enumerate(header) if any(x in h for x in ["上り", "3F"])), -1)
        time_idx = next((i for i, h in enumerate(header) if "タイム" in h), -1)
        pop_idx = next((i for i, h in enumerate(header) if "人気" in h), -1)
        if n_idx == -1 or a_idx == -1:
            return "-", "-", "-"
        times, t_val, t_time, t_pop = [], None, "-", "-"
        target = re.sub(r"[\s　]+", "", target_name)
        for row in rows[1:]:
            cols = row.find_all(["td", "th"])
            if len(cols) <= max(n_idx, a_idx):
                continue
            name = re.sub(r"[\s　]+", "", cols[n_idx].get_text(strip=True))
            m = re.search(r"(\d+\.\d+)", cols[a_idx].get_text(strip=True))
            if m:
                v = float(m.group(1))
                times.append(v)
                if target in name:
                    t_val = v
                    if time_idx != -1 and time_idx < len(cols): t_time = cols[time_idx].get_text(strip=True)
                    if pop_idx != -1 and pop_idx < len(cols): t_pop = cols[pop_idx].get_text(strip=True)
        agari = f"{sorted(times).index(t_val)+1}/{len(times)}" if t_val else "-"
        return agari, t_time, t_pop
    except Exception:
        return "-", "-", "-"


def fetch_baba_info(venue):
    if not venue:
        return None
    try:
        cushion_val = "-"
        res_c = req_lib.get("https://www.jra.go.jp/keiba/baba/_data_cushion.html", headers=HEADERS, timeout=5)
        if res_c.status_code == 200:
            res_c.encoding = "shift_jis"
            sc = BeautifulSoup(res_c.text, "html.parser")
            vd = sc.find("div", title=venue)
            if vd:
                unit = vd.find("div", class_="unit")
                if unit and unit.find("div", class_="cushion"):
                    cushion_val = unit.find("div", class_="cushion").get_text(strip=True)

        turf_cond, dirt_cond = "良", "良"
        turf_g, turf_4c, dirt_g, dirt_4c = "-", "-", "-", "-"
        res_m = req_lib.get("https://www.jra.go.jp/keiba/baba/_data_moist.html", headers=HEADERS, timeout=5)
        if res_m.status_code == 200:
            res_m.encoding = "shift_jis"
            sm = BeautifulSoup(res_m.text, "html.parser")
            vdm = sm.find("div", title=venue)
            if vdm:
                um = vdm.find("div", class_="unit")
                if um:
                    td = um.find("div", class_="turf")
                    if td:
                        gs = td.find("span", class_="mg")
                        c4s = td.find("span", class_="m4c")
                        if gs: turf_g = gs.get_text(strip=True)
                        if c4s: turf_4c = c4s.get_text(strip=True)
                        cond_map = {"hard": "良", "wet": "稍重", "soft": "重", "heavy": "不良"}
                        if gs: turf_cond = cond_map.get(gs.get("data-condition", ""), "良")
                    dd = um.find("div", class_="dirt")
                    if dd:
                        gsd = dd.find("span", class_="mg")
                        c4sd = dd.find("span", class_="m4c")
                        if gsd: dirt_g = gsd.get_text(strip=True)
                        if c4sd: dirt_4c = c4sd.get_text(strip=True)
                        cond_map2 = {"hard": "良", "wet": "稍重", "soft": "重", "heavy": "不良"}
                        if gsd: dirt_cond = cond_map2.get(gsd.get("data-condition", ""), "良")

        return (f"馬場情報 [{venue}] "
                f"芝: {turf_cond} (水分: G前{turf_g}% 4角{turf_4c}% クッション値: {cushion_val})"
                f" / ダート: {dirt_cond} (水分: G前{dirt_g}% 4角{dirt_4c}%)")
    except Exception:
        return f"馬場情報 [{venue}]：取得失敗"


def fetch_history_data(c, url, name, mode, is_first):
    raw_text = c.get_text(" ", strip=True)
    cond_tag = c.find("span", class_="condition")
    condition = cond_tag.get_text(strip=True) if cond_tag else "-"
    corners_list = [li.get_text(strip=True) for li in c.find_all("li", title=re.compile(r"コーナー通過順位"))]
    if not corners_list:
        m_corners = re.search(r"(\d{1,2}(?:\s*-\s*\d{1,2})+)", raw_text)
        if m_corners:
            corners_list = [x.strip() for x in m_corners.group(1).split("-")]
    agari_rank, run_time, pop_rank = "-", "-", "-"
    link = c.find("a")
    if (mode == "詳細" or is_first) and link:
        agari_rank, run_time, pop_rank = get_agari_rank_from_url(
            urljoin("https://www.jra.go.jp/JRADB/", link["href"]), name
        )
    weight_tag = c.find("p", class_="h_weight")
    kinryo_tag = c.find("div", class_="weight")
    course_m = re.search(r"(\d{3,4}\s*(?:芝|ダ|障))", raw_text)
    place_m = re.search(r"(中山|東京|京都|阪神|中京|札幌|函館|福島|新潟|小倉)", raw_text)
    place = place_m.group(1) if place_m else "-"
    race_name_tag = c.find("a")
    race_name = race_name_tag.get_text(strip=True) if race_name_tag else "-"
    rank_m = re.search(r"(\d{1,2})\s*着", raw_text)
    rank = rank_m.group(1) if rank_m else "-"
    total_m = re.search(r"(\d{1,2})\s*頭", raw_text)
    total = total_m.group(1) if total_m else "-"
    jockey = "-"
    if kinryo_tag:
        kv = re.sub(r"[^\d.]", "", kinryo_tag.get_text(strip=True))
        if kv:
            jm = re.search(r"人気\s+([^\d]+?)\s+" + re.escape(kv), raw_text)
            if not jm: jm = re.search(r"番\s+([^\d]+?)\s+" + re.escape(kv), raw_text)
            if jm:
                jockey = jm.group(1).strip()
                jockey = re.sub(r".*人気\s*", "", jockey).strip()
    return {
        "raw": raw_text, "condition": condition,
        "corners": "-".join(corners_list) if corners_list else "-",
        "agari_rank": agari_rank, "course": course_m.group(0) if course_m else "-",
        "weight": weight_tag.get_text(strip=True) if weight_tag else "-",
        "kinryo": kinryo_tag.get_text(strip=True) if kinryo_tag else "-",
        "place": place, "race_name": race_name, "rank": rank, "total": total,
        "run_time": run_time, "pop_rank": pop_rank, "jockey": jockey,
    }


def parse_horse_row(row, url, curr_date_fmt, mode):
    cells = row.find_all(["td", "th"])
    off = 1 if "着" in cells[0].get_text() or (len(cells) > 2 and cells[2].get_text().isdigit()) else 0
    try:
        num = int(cells[1 + off].get_text(strip=True))
        h_cell = cells[2 + off].get_text(" ", strip=True)
        name = h_cell.split()[0]
        sire_m = re.search(r"父：\s*([^\s]+)", h_cell)
        sire = sire_m.group(1) if "父" in h_cell and sire_m else "-"
        dam_m = re.search(r"母：\s*([^\s]+)", h_cell)
        dam = dam_m.group(1) if "母" in h_cell and dam_m else "-"
        bms_m = re.search(r"母の父：\s*([^ \)]+)", h_cell)
        bms = bms_m.group(1) if "母の父" in h_cell and bms_m else "-"
        jock_raw = cells[3 + off].get_text(" ", strip=True).split()
        sex_age = jock_raw[0] if jock_raw else "-"
        kg = jock_raw[1] if len(jock_raw) > 1 else "-"
        jock = re.sub(r"[\s　kgkｇ0-9.]", "", "".join(jock_raw[2:])) if len(jock_raw) > 2 else "-"
        o_val = 999.0
        for cell in cells:
            od = cell.find("div", class_="odds_line")
            if od and od.find("strong"):
                try: o_val = float(od.find("strong").get_text(strip=True))
                except ValueError: pass
                break
        hist_cells = [c for c in cells if "202" in c.get_text()]
        hist_list = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fetch_history_data, c, url, name, mode, i == 0) for i, c in enumerate(hist_cells[:4])]
            for f in futs:
                hist_list.append(f.result())
        iv = "-"
        if hist_list:
            pdm = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", hist_list[0]["raw"])
            if pdm and curr_date_fmt:
                diff = (datetime.strptime(curr_date_fmt, "%Y年%m月%d日") - datetime.strptime(pdm.group(1), "%Y年%m月%d日")).days
                iv = f"中{(diff//7)-1}週" if diff > 7 else "連闘"
        return {
            "num": num, "odds": o_val, "iv": iv, "name": name, "sex_age": sex_age,
            "kyakushitsu": calculate_kyakushitsu(hist_list), "kg": kg, "jock": jock,
            "affi": "栗東" if "栗東" in h_cell else "美浦",
            "sire": sire, "dam": dam, "bms": bms, "hist": hist_list,
        }
    except Exception as e:
        print("Row parse error:", e)
        return None


def fetch_races_for_venue(text, url):
    try:
        res = req_lib.get(url, headers=HEADERS, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        expected_date = None
        dm = re.search(r"(\d+)/(\d+)", text)
        if dm:
            year = datetime.now().year
            expected_date = f"{year}{int(dm.group(1)):02d}{int(dm.group(2)):02d}"
        races_map = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "CNAME=" not in href: continue
            rn_m = re.search(r"CNAME=pw\d+dde\d+(\d{2})\d{8}", href)
            if not rn_m or not (1 <= int(rn_m.group(1)) <= 12): continue
            if expected_date:
                date_m = re.search(r"(\d{8})/[0-9A-Fa-f]+$", href)
                if date_m and date_m.group(1) != expected_date: continue
            races_map[int(rn_m.group(1))] = urljoin("https://www.jra.go.jp/JRADB/", href)
        races = [{"r": k, "url": v} for k, v in sorted(races_map.items())]
        return {"text": text, "url": url, "races": races}
    except Exception:
        return {"text": text, "url": url, "races": []}


def build_matrix_data(soup):
    nav_lists = soup.find_all("ul", class_="data_line_list")
    seen_urls = set()
    venue_links = []
    for ul in nav_lists:
        for a in ul.find_all("a", href=True):
            href = urljoin("https://www.jra.go.jp/JRADB/", a["href"])
            if href in seen_urls: continue
            date_text = ""
            matches = re.findall(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", href)
            if matches:
                try:
                    y, mo, d = matches[-1]
                    wd = ["月", "火", "水", "木", "金", "土", "日"][datetime(int(y), int(mo), int(d)).weekday()]
                    date_text = f"{int(mo)}/{int(d)}({wd}) "
                except Exception:
                    pass
            text = date_text + a.get_text(strip=True)
            seen_urls.add(href)
            venue_links.append((text, href))
    matrix_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_races_for_venue, t, u) for t, u in venue_links]
        for f in futs:
            res = f.result()
            if res and res["races"]:
                matrix_data.append(res)
    return matrix_data


# ─── APIルート ────────────────────────────────────────────────────────────────
@app.route("/api/scrape", methods=["POST"])
def scrape():
    data = request.json or {}
    url = data.get("url")
    mode = data.get("mode", "簡易")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        res = req_lib.get(url, headers=HEADERS, timeout=15)
        res.encoding = "shift_jis"
        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text()

        v_code_match = re.search(r"CNAME=pw\d+dde\d{2}(\d{2})", url)
        v_code = v_code_match.group(1) if v_code_match else "00"
        venue = VENUE_MAP.get(v_code, "会場不明")

        curr_date_m = re.search(r"(\d+)月(\d+)日", page_text)
        curr_date = f"{curr_date_m.group(1)}/{curr_date_m.group(2)}" if curr_date_m else "??/??"
        year_match = re.search(r"dde\d{2}\d{2}(\d{4})", url)
        year_val = year_match.group(1) if year_match else str(datetime.now().year)
        curr_date_fmt = f"{year_val}年{curr_date_m.group(1)}月{curr_date_m.group(2)}日" if curr_date_m else None

        race_num_m = re.search(r"(\d+)\s*レース", page_text)
        race_idx = int(race_num_m.group(1)) if race_num_m else 1

        dist_match = re.search(r"(\d,?\d{2,3})メートル", page_text)
        dist_val = int(dist_match.group(1).replace(",", "")) if dist_match else 0
        race_type = "ダート" if "ダート" in page_text else "芝"

        race_name_tag = soup.find(class_="race_name")
        race_name = race_name_tag.get_text(strip=True) if race_name_tag else "レース名不明"

        race_time = ""
        for t_tag in soup.find_all(class_=re.compile(r"time", re.I)):
            if t_tag and "発走" in t_tag.get_text():
                m = re.search(r"(\d{1,2})[時:](\d{2})", t_tag.get_text())
                if m:
                    race_time = f"{m.group(1)}:{m.group(2)}"
                    break
        if not race_time:
            m = re.search(r"発走(?:時刻)?[\s：:]*(\d{1,2})[時:](\d{2})", page_text)
            if not m:
                m = re.search(r"(\d{1,2})[時:](\d{2})[\s]*(?:分)?[\s]*(?:発走|発走時刻)", page_text)
            if m: race_time = f"{m.group(1)}:{m.group(2)}"

        time_str = f" {race_time}発走" if race_time else ""
        race_label = f"【{venue} {race_idx}R】{race_type}{dist_val}m{time_str}　{race_name}"

        def get_race_class(name):
            n = str(name).upper()
            if any(g in n for g in ["G1", "G2", "G3", "JG1", "JG2", "JG3"]): return "重賞"
            if any(g in n for g in ["(L)", "オープ", "OP"]): return "オープン"
            if "3勝" in n or "３勝" in n: return "3勝クラス"
            if "2勝" in n or "２勝" in n: return "2勝クラス"
            if "1勝" in n or "１勝" in n: return "1勝クラス"
            if "未勝利" in n: return "未勝利"
            if "新馬" in n: return "新馬"
            return "オープン"

        race_class = get_race_class(race_name)
        baba_info = fetch_baba_info(venue)
        matrix = build_matrix_data(soup)

        target_table = next((t for t in soup.find_all("table") if "馬名" in t.get_text()), None)
        rows = (
            [r for r in target_table.find_all("tr")
             if len(r.find_all(["td", "th"])) >= 5 and "馬名" not in r.get_text()]
            if target_table else []
        )

        scraped_data = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(parse_horse_row, r, url, curr_date_fmt, mode) for r in rows]
            for f in futs:
                h = f.result()
                if h: scraped_data.append(h)

        scraped_data.sort(key=lambda x: x["odds"])
        for i, h in enumerate(scraped_data): h["pop"] = str(i + 1)
        scraped_data.sort(key=lambda x: x["num"])

        sire_lineage = load_sire_lineage()
        criteria = load_csv_criteria(venue)
        mawari_map = load_mawari_map()

        race_context = {"type": race_type, "dist": dist_val, "total_horses": len(scraped_data), "venue": venue}

        criteria_lines = []
        for c in criteria:
            if c["type"] == race_type and c["dist_min"] <= dist_val <= c["dist_max"]:
                conds = [c[k] for k in ["c1", "c2", "c3"] if is_valid_cond(c[k])]
                criteria_lines.append(f"・{'×'.join(conds)}")

        has_double_circle = False
        horses_out = []
        for h in scraped_data:
            h["w_num"] = calculate_waku(h["num"], len(scraped_data))
            grade, det = evaluate_ultra(h, race_context, criteria, sire_lineage, mawari_map)
            h["grade"] = grade
            h["ultra_details"] = det
            if grade == "◎": has_double_circle = True
            h["dist_diff"] = "-"
            if h.get("hist") and h["hist"][0] and h["hist"][0].get("course"):
                prev_dist_m = re.search(r"(\d+)", h["hist"][0]["course"])
                if prev_dist_m:
                    pd = int(prev_dist_m.group(1))
                    h["dist_diff"] = "延長" if dist_val > pd else "短縮" if dist_val < pd else "同"
            horses_out.append(h)

        feature_text = load_course_feature(venue, race_type, dist_val)
        konochichi_lines = load_konochichi_criteria(venue, race_type, dist_val)
        course_tips = load_course_tips(venue, race_type, dist_val)
        race_conditions = load_race_conditions(race_name)
        for h in horses_out:
            h["sire_buysell"] = load_sire_buysell(h.get("sire"))

        sire_result = load_notable_sires(venue, race_type, dist_val)
        notable_sires = sire_result.get("sires", [])
        sire_ranking_map = {s["name"]: s["rank"] for s in notable_sires}
        for h in horses_out:
            if h.get("sire") in sire_ranking_map:
                h["sire_rank"] = sire_ranking_map[h["sire"]]

        harab_index = "-"
        if feature_text:
            hm = re.search(r"【?波乱指数】?[:：\s]*([^\n]+)", feature_text)
            if hm:
                harab_index = hm.group(1).strip()
                feature_text = feature_text.replace(hm.group(0), "").strip()
            flm = re.match(r"^(.*?[芝ダ障害]\d+m\s*)\n", feature_text)
            if flm:
                feature_text = feature_text[len(flm.group(0)):].strip()

        course_record_text = ""
        record_elem = soup.find("div", class_=re.compile("record"))
        if record_elem and "コースレコード" in record_elem.get_text():
            course_record_text = record_elem.get_text(separator=" ", strip=True)
        else:
            r_str = soup.find(string=re.compile(r"コースレコード"))
            if r_str:
                rp = r_str.find_parent(class_=re.compile("record"))
                course_record_text = rp.get_text(separator=" ", strip=True) if rp else r_str.parent.get_text(separator=" ", strip=True)
        course_record_text = re.sub(r"\s+", " ", course_record_text).strip()

        course_image = ""
        try:
            img_path = os.path.join(BASE_DIR, "api", "jra_images.json")
            if not os.path.exists(img_path):
                img_path = os.path.join(BASE_DIR, "jra_images.json")
            with open(img_path, "r", encoding="utf-8") as f:
                img_map = json.load(f)
                course_image = img_map.get(venue, {}).get(f"{race_type}{dist_val}", "")
        except Exception:
            pass

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
            "konochichi_lines": konochichi_lines,
            "course_tips": course_tips,
            "race_conditions": race_conditions,
            "harab_index": harab_index,
            "feature_text": feature_text,
            "notable_sires": notable_sires,
            "debug_sire": sire_result.get("debug", {}),
            "matrix_data": matrix,
            "horses": horses_out,
            "has_double_circle": has_double_circle,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/latest_url", methods=["GET"])
def latest_url():
    try:
        def _find_access_d(soup_obj):
            fallback = None
            for a in soup_obj.find_all("a", href=True):
                href = a["href"]
                if "accessD.html" not in href or "CNAME=" not in href:
                    continue
                full = urljoin("https://www.jra.go.jp/", href)
                if "dde" in href:
                    return full
                if fallback is None:
                    fallback = full
            return fallback

        for candidate in ["https://www.jra.go.jp/", "https://www.jra.go.jp/keiba/thisweek/"]:
            try:
                res = req_lib.get(candidate, headers=HEADERS, timeout=10)
                res.encoding = "cp932"
                soup = BeautifulSoup(res.text, "html.parser")
                url = _find_access_d(soup)
                if url:
                    return jsonify({"url": url})
            except Exception:
                continue
        return jsonify({"error": "最新の出馬表URLが見つかりませんでした。レース開催日(木〜日)にお試しください。"}), 404
    except Exception as e:
        return jsonify({"error": f"URL取得エラー: {str(e)}"}), 500


@app.route("/api/save_course_manual", methods=["POST"])
def save_course_manual():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Invalid request"}), 400
        place_code = data.get("place")
        place_map = {
            "sapporo": "札幌", "hakodate": "函館", "fukushima": "福島", "niigata": "新潟",
            "tokyo": "東京", "nakayama": "中山", "chukyo": "中京", "kyoto": "京都",
            "hanshin": "阪神", "kokura": "小倉",
        }
        place_name = place_map.get(place_code, place_code)
        track = data.get("track", "")
        distance = data.get("distance", "")
        course_name = f"{place_name}{track}{distance}m"
        row_dict = {
            "course_name": course_name,
            "favorable_running_style": data.get("runningStyle", ""),
            "course_features": data.get("features", ""),
            "favorable_frame": data.get("frame", ""),
            "notes": data.get("notes", ""),
            "standard_time_new": data.get("timeNew", ""),
            "standard_time_1_2": data.get("time12", ""),
            "standard_time_3": data.get("time3", ""),
            "wind_bias": data.get("wind", ""),
        }
        fieldnames = list(row_dict.keys())
        target_dir = os.path.join(DATA_DIR, place_code)
        os.makedirs(target_dir, exist_ok=True)
        csv_path = os.path.join(target_dir, "course_dictionary.csv")
        existing = []
        if os.path.exists(csv_path):
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                existing = list(csv.DictReader(f))
        updated = False
        for r in existing:
            if r.get("course_name") == course_name:
                r.update(row_dict)
                updated = True
                break
        if not updated:
            existing.append(row_dict)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in existing:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        return jsonify({"success": True, "course_name": course_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── 起動 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  JRA解析ツール 簡易版 (AI機能・DB機能なし)")
    print("=" * 55)
    print(f"  URL: http://localhost:{PORT}")
    print("  ブラウザが自動で開きます...")
    print("  終了: Ctrl+C")
    print("-" * 55)
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
