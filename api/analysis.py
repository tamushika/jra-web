import json
import re
import pandas as pd
import os

def get_class_rank(cls_str):
    if not cls_str: return 0
    if "GⅠ" in cls_str or "G1" in cls_str: return 90
    if "GⅡ" in cls_str or "G2" in cls_str: return 80
    if "GⅢ" in cls_str or "G3" in cls_str: return 70
    if "OP" in cls_str or "オープン" in cls_str or "L" in cls_str or "リステッド" in cls_str: return 60
    if "3勝" in cls_str or "1600万" in cls_str: return 50
    if "2勝" in cls_str or "1000万" in cls_str: return 40
    if "1勝" in cls_str or "500万" in cls_str: return 30
    if "未勝利" in cls_str: return 20
    if "新馬" in cls_str: return 10
    return 0

VENUE_SLUG_MAP = {
    "中山": "nakayama", "阪神": "hanshin", "東京": "tokyo", "京都": "kyoto",
    "中京": "chukyo", "新潟": "niigata", "福島": "fukushima", "小倉": "kokura",
    "札幌": "sapporo", "函館": "hakodate", "共通": "common"
}

_criteria_weights_cache = None

def _clean_rule_id(raw):
    """pandas経由のid (29, 29.0, '29') を '29' に正規化"""
    s = str(raw).strip()
    return s[:-2] if s.endswith(".0") else s

def load_criteria_weights():
    """criteria_weights.json (backtest_criteria.py --gen-weights で生成) を読む。
    無ければ空dict = 全ルール重み1.0。"""
    global _criteria_weights_cache
    if _criteria_weights_cache is not None:
        return _criteria_weights_cache
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(current_script_dir, "data_files", "common", "criteria_weights.json")
    weights = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        weights = {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        pass
    _criteria_weights_cache = weights
    return weights

def load_csv_criteria(venue_name, base_dir):
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    venue_slug = VENUE_SLUG_MAP.get(venue_name, "kyoto")

    # 同階層の data_files を参照 (Zero Config 最適化)
    file_path = os.path.join(current_script_dir, "data_files", venue_slug, "criteria", "criteria.csv")

    if not os.path.exists(file_path):
        return []

    auto_weights = load_criteria_weights().get(venue_name) or {}
    try:
        df = pd.read_csv(file_path, header=None, encoding='utf-8-sig')
        criteria = []
        for _, row in df.iterrows():
            dist_str = str(row[2])
            dist_range = re.findall(r'\d+', dist_str)
            if not dist_range: continue
            dist_min = int(dist_range[0])
            dist_max = int(dist_range[1]) if len(dist_range) > 1 else dist_min
            rid = _clean_rule_id(row[0])
            # 重み: CSV7列目 (手動) > criteria_weights.json (バックテスト自動算出) > 1.0
            weight = None
            try:
                cell = row.get(6)
                if cell is not None and pd.notna(cell) and str(cell).strip() != "":
                    weight = float(cell)
            except (ValueError, TypeError):
                pass
            if weight is None:
                weight = float(auto_weights.get(rid, 1.0))
            criteria.append({
                "id": rid, "type": row[1], "dist_min": dist_min, "dist_max": dist_max,
                "c1": str(row[3]), "c2": str(row[4]), "c3": str(row[5]), "weight": weight
            })
        return criteria
    except: return []

def load_course_feature(venue, race_type, distance, base_dir):
    """ 指定された会場・条件のコース特徴テキストを読み込む """
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    venue_slug = VENUE_SLUG_MAP.get(venue, "kyoto")
    filename = f"{race_type}{distance}.txt"
    
    # 同階層の data_files を参照
    file_path = os.path.join(current_script_dir, "data_files", venue_slug, "course_info", filename)
    
    if not os.path.exists(file_path):
        return f"【情報】{venue} {race_type}{distance}m の特徴データは登録されていません。"

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            return f.read()
    except Exception as e:
        return f"エラー: ファイルの読み込みに失敗しました ({str(e)})"

def load_sire_lineage(base_dir):
    """syuboba.csv を読み込む (data_files/common/ フォルダ内)"""
    lineage_map = {}
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 同階層の data_files を参照
    file_path = os.path.join(current_script_dir, "data_files", "common", "syuboba.csv")
    
    if not os.path.exists(file_path):
        return {}
        
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                parts = [p.strip() for p in line.split(',') if p.strip()]
                if not parts: continue
                group_name = parts[0].replace("種牡馬", "")
                for sire in parts[1:]: lineage_map[sire] = group_name
    except: pass
    return lineage_map

def load_notable_sires(venue, race_type, distance, base_dir):
    """ 指定された会場・条件の注目産駒データ（種牡馬ランキング）を読み込む """
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    venue_slug = VENUE_SLUG_MAP.get(venue, "kyoto")
    filename = f"{race_type}{distance}.csv"
    
    # 同階層の data_files を参照
    file_path = os.path.join(current_script_dir, "data_files", venue_slug, "sire", filename)

    debug_info = {
        "attempted_path": file_path,
        "exists": os.path.exists(file_path),
        "error": None
    }

    if not debug_info["exists"]:
        return {"sires": [], "debug": debug_info}

    try:
        df = pd.read_csv(file_path, header=None, encoding='utf-8-sig')
        sires = []
        for i, row in df.iterrows():
            if len(row) < 4: continue
            sires.append({
                "rank": i + 1,
                "name": str(row[0]).strip(),
                "win_rate": f"{float(row[1]):.1f}%",
                "quinella_rate": f"{float(row[2]):.1f}%",
                "show_rate": f"{float(row[3]):.1f}%"
            })
        return {"sires": sires, "debug": debug_info}
    except Exception as e:
        debug_info["error"] = str(e)
        return {"sires": [], "debug": debug_info}

def load_konochichi_criteria(venue_name, race_type, distance, base_dir):
    """「この父このテキこの鞍上」の好走条件(criteria_konochichi.csv)を読み込む。

    既存のcriteria.csv(評価ロジックで使用)とは別ファイルで、画面表示専用。
    馬主/調教師の条件はstatus='flagged'のものも含むため参考情報として扱う。
    """
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    venue_slug = VENUE_SLUG_MAP.get(venue_name, "kyoto")
    file_path = os.path.join(current_script_dir, "data_files", venue_slug, "criteria", "criteria_konochichi.csv")

    if not os.path.exists(file_path):
        return []

    try:
        df = pd.read_csv(file_path, header=None, encoding='utf-8-sig')
        lines = []
        for _, row in df.iterrows():
            row_type = str(row[1]).strip()
            dist_range = re.findall(r'\d+', str(row[2]))
            if not dist_range or row_type != race_type:
                continue
            dist_min = int(dist_range[0])
            dist_max = int(dist_range[1]) if len(dist_range) > 1 else dist_min
            if not (dist_min <= distance <= dist_max):
                continue
            conds = [str(row[k]) for k in (3, 4, 5) if is_valid_cond(row[k])]
            if conds:
                status = str(row[6]).strip() if len(row) > 6 else ""
                suffix = "(要確認)" if status == "flagged" else ""
                lines.append(f"・{'×'.join(conds)}{suffix}")
        return lines
    except Exception:
        return []


def load_course_tips(venue_name, race_type, distance, base_dir):
    """「コース辞典」のこのコースの狙い方!(course_tips.csv)を読み込む。"""
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_script_dir, "data_files", "common", "course_tips.csv")

    if not os.path.exists(file_path):
        return []

    try:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        tips = []
        for _, row in df.iterrows():
            if str(row.get('競馬場', '')).strip() != venue_name:
                continue
            row_type = str(row.get('芝ダート', '')).strip()
            if row_type and row_type != race_type:
                continue
            dist_nums = re.findall(r'\d+', str(row.get('距離', '')))
            if dist_nums and int(dist_nums[0]) != distance:
                continue
            hint = str(row.get('狙い方ヒント', '')).strip()
            if hint:
                tips.append(hint)
        return tips
    except Exception:
        return []


_race_conditions_cache = None
_RACE_GRADE_RE = re.compile(r"第\d+回|[GgＧ][ⅠⅡⅢ123一二三]+")
_RACE_PAREN_RE = re.compile(r"[（(]([^）)]*)[）)]")


def _race_name_candidates(name):
    base = _RACE_GRADE_RE.sub("", name)
    alias = _RACE_PAREN_RE.search(base)
    main = _RACE_PAREN_RE.sub("", base).strip()
    candidates = {main} if main else set()
    if alias:
        candidates.add(alias.group(1).strip())
    return {c for c in candidates if c}


def load_race_conditions(race_name, base_dir):
    """「重賞ナビ2026」の好走条件(race_conditions.csv)をレース名から引く。

    本のレース名表記(例:「オークス（優駿牝馬）」)と実際のレース名表記の
    揺れに対応するため、第◯回・グレード表記を除いた正規化文字列で照合する。
    """
    global _race_conditions_cache
    if not race_name:
        return []

    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_script_dir, "data_files", "common", "race_conditions.csv")

    if _race_conditions_cache is None:
        rows = []
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path, encoding='utf-8-sig')
                rows = df.to_dict('records')
            except Exception:
                rows = []
        _race_conditions_cache = rows

    targets = _race_name_candidates(race_name)
    if not targets:
        return []

    matched = []
    for row in _race_conditions_cache:
        book_candidates = _race_name_candidates(str(row.get('レース名', '')))
        if any(b in t or t in b for b in book_candidates for t in targets):
            matched.append({
                "kubun": row.get('区分', ''),
                "header": row.get('小見出し', ''),
                "conclusion": row.get('結論', ''),
            })
    return matched


_sire_buysell_cache = None


def load_sire_buysell(sire_name, base_dir):
    """「亀谷敬正の競馬血統辞典2」の買い&消しポイント(sire_buysell.csv)を父名から引く。"""
    global _sire_buysell_cache
    if not sire_name or sire_name == "-":
        return []

    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_script_dir, "data_files", "common", "sire_buysell.csv")

    if _sire_buysell_cache is None:
        mapping = {}
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path, encoding='utf-8-sig')
                for _, row in df.iterrows():
                    name = str(row.get('種牡馬名', '')).strip()
                    mapping.setdefault(name, []).append({
                        "kubun": str(row.get('区分', '')).strip(),
                        "condition": str(row.get('条件文', '')).strip(),
                    })
            except Exception:
                mapping = {}
        _sire_buysell_cache = mapping

    return _sire_buysell_cache.get(sire_name, [])


def is_valid_cond(c):
    return c and str(c).strip() not in ["nan", "-", "", "None"]

def check_condition(cond, h, r, sire_lineage, mawari_map):
    """「XXXがXXX」形式に対応した条件判定ロジック。"""
    # デフォルトの回りデータ（CSVが読み込めなかった時のバックアップ）
    DEFAULT_MAWARI = {
        "中山": "右", "東京": "左", "京都": "右", "阪神": "右", "中京": "左",
        "新潟": "左", "福島": "右", "小倉": "右", "札幌": "右", "函館": "右"
    }

    if not is_valid_cond(cond): return True
    try:
        # 0. 騎手名判定 (最優先)
        # 「角田大和」の「角」が「4角」と誤判定されるのを防ぐため、騎手条件は最初に処理する
        if "騎手" in cond:
            target_jock = cond.replace("騎手が", "").strip()
            # 記号を除去して比較
            clean_cond = re.sub(r'[▲△☆★◇\s　kgkｇ]', '', target_jock)
            clean_jock = re.sub(r'[▲△☆★◇\s　kgkｇ]', '', h['jock'])
            
            if "乗り替わり" in cond:
                 # 乗り替わり判定は後続のロジック(10.5)に任せるか、ここでやるか。
                 # ここでやると後続の「乗り替わり」ロジックが呼ばれない。
                 # 「騎手が乗り替わり」という条件式ならここでFalseを返すべきではない（乗り替わりロジックへ通すべき）。
                 # しかし「騎手が角田大和」ならここで判定してreturnすべき。
                 if "乗り替わり" not in target_jock:
                     return (clean_cond in clean_jock) or (clean_jock in clean_cond)
            else:
                 return (clean_cond in clean_jock) or (clean_jock in clean_cond)

        # 1. 回り判定 (修正版)
        if "回り" in cond:
            target = "右" if "右" in cond else "左"
            raw_hist = h['hist'][0]['raw'] if h['hist'] else ""
            
            # 競馬場名を抽出 (「中山」「京都」などの2文字を正規表現で探す)
            venues_pattern = "|".join(DEFAULT_MAWARI.keys())
            match = re.search(f"({venues_pattern})", raw_hist)
            
            if match:
                prev_venue = match.group(1)
                # 引数の mawari_map を優先し、なければ DEFAULT_MAWARI を使う
                actual_mawari = mawari_map.get(prev_venue) or DEFAULT_MAWARI.get(prev_venue, "")
                
                # デバッグ用 (判定が怪しい馬の名前を指定)
                if "シェーラ" in h['name'] or "ミスターエメラルド" in h['name']:
                    print(f"--- 【回りデバッグ】 {h['name']} ---")
                    print(f"前走場所: {prev_venue}, 判定データ: {actual_mawari}, 目標: {target}")

                if target not in actual_mawari: return False
            else:
                if "シェーラ" in h['name']: print(f"⚠️ {h['name']}: 前走の競馬場名が見つかりません (raw: {raw_hist[:30]}...)")
                return False
            return True

        # 2. 間隔判定
        if "週" in cond or "連闘" in cond:
            actual_iv = h.get('iv', "")
            if actual_iv == "-" or not actual_iv: return False
            if "週" in cond:
                target_m, actual_m = re.search(r'(\d+)', cond), re.search(r'(\d+)', actual_iv)
                if target_m and actual_m:
                    if int(actual_m.group(1)) > int(target_m.group(1)): return False
                elif "連闘" not in actual_iv: return False
            if "連闘" in cond and "連闘" not in actual_iv: return False
            return True

        # 3. 着順判定 (修正版)
        if "着" in cond:
            actual_raw = h['hist'][0]['raw'] if h['hist'] else ""
            
            # 「3 着」のように数字と「着」の間にスペースや文字があっても、その直前の数字を取得
            rank_m = re.search(r'(\d+)\s*着', actual_raw)
            
            if rank_m:
                actual = int(rank_m.group(1))
            else:
                return False

            target_m = re.search(r'(\d+)', cond)
            if target_m:
                target = int(target_m.group(1))
                if "以内" in cond and actual > target: return False
                if "以下" in cond and actual < target: return False
                if "以内" not in cond and "以下" not in cond and actual != target: return False
            return True
        
       # 3.5. 4角通過順判定 (修正版)
        if "角" in cond or "通過順" in cond:
            # キー名が 'corners' か 'passing' のどちらでも取得
            passing_order = h['hist'][0].get('corners') or h['hist'][0].get('passing', "")
            
            if not passing_order or passing_order in ["---", "-", ""]:
                return False
            
            try:
                # ハイフンで分割して最後（4角）を取得
                raw_last = passing_order.split('-')[-1]
                actual_m = re.search(r'(\d+)', raw_last)
                if not actual_m: return False
                actual = int(actual_m.group(1))
            except:
                return False
            
            # --- 修正ポイント：条件文から「最後」の数字を抽出 ---
            # 「4角10番手」のような場合、4ではなく10を取得するために findall を使用
            nums_in_cond = re.findall(r'(\d+)', cond)
            if nums_in_cond:
                target = int(nums_in_cond[-1]) # 最後の数字（10）を取得
                
                if ("以内" in cond or "以下" in cond) and actual > target: return False
                if "番手" in cond and "以内" not in cond and "以下" not in cond and actual != target: return False
            
            return True
        
        # 3.7. 上がり順位判定
        if "上がり" in cond:
            # scraping.pyで保存した "3/16" などの形式から数値を取得
            actual_str = h['hist'][0].get('agari_rank', "-")
            if not actual_str or actual_str == "-":
                return False
            
            # "3/16" のような形式から最初の数字（順位）を抽出
            m_rank = re.search(r'(\d+)', actual_str)
            if not m_rank: return False
            actual = int(m_rank.group(1))
            
            target_m = re.search(r'(\d+)', cond)
            if target_m:
                target = int(target_m.group(1))
                if "以内" in cond and actual > target: return False
                if "以下" in cond and actual < target: return False
                if "位" in cond and "以内" not in cond and "以下" not in cond and actual != target: return False
            return True
        
        # 4. 頭数判定 (修正版：'3 着 16 頭' の形式に対応)
        if "頭" in cond:
            curr_total = r.get('total_horses', 0)
            target_m = re.search(r'(\d+)', cond)
            is_prev = "前走" in cond or "出走頭数" in cond
            
            if is_prev:
                prev_raw = h['hist'][0]['raw'] if h['hist'] else ""
                # '着'の次に来る数字を「前走の頭数」として取得
                prev_total_m = re.search(r'着\s*(\d+)\s*頭', prev_raw)
                if not prev_total_m:
                    # 見つからない場合は単純に '頭' の前の数字を探す
                    prev_total_m = re.search(r'(\d+)\s*頭', prev_raw)
                
                if not prev_total_m: return False
                actual = int(prev_total_m.group(1))
            else: 
                actual = curr_total

            if "今回より多い" in cond and actual <= curr_total: return False
            elif "同頭数以上" in cond and actual < curr_total: return False
            elif target_m:
                target = int(target_m.group(1))
                if "以上" in cond and actual < target: return False
                if ("以下" in cond or "以内" in cond) and actual > target: return False
            return True

        # 4.5. 生産者判定 (馬の個別プロフィールページ遷移が必要なため、
        # h['producer'] が無い場合は判定不可としてFalseを返す。jra-web側はこのキーを
        # 設定していないため、ここを通っても常にFalseとなり負荷増にはならない)
        if "生産者" in cond:
            target = cond.replace("生産者が", "").strip()
            producer = h.get("producer", "")
            if not producer:
                return False
            return target in producer or producer in target

        # 5. 血統判定 (父/母父)
        if "父" in cond:
            # 置換対象を増やして「父か母父が」などにも対応
            target = cond
            # 「父が」は他のprefixの部分文字列でもあるため、より長い(複合)prefixを先に
            # 除去しないと「父か母父が」等が中途半端に切れてしまう(例:「父か母」が残る)。
            # 同様に「以外の種牡馬」も「以外」より先に「以外の」を除去しないと「の」が残る。
            for prefix in ["父or母父が", "父・母父が", "父か母父が", "父が", "系", "以外の", "以外", "種牡馬"]:
                target = target.replace(prefix, "")
            target = re.sub(r"\s+", "", target).strip()
            
            match = (target == h['sire']) or (target in sire_lineage.get(h['sire'], ""))
            if "母父" in cond:
                match = match or (target == h.get('bms', "")) or (target in sire_lineage.get(h.get('bms', ""), ""))
            
            if ("以外" in cond and match) or ("以外" not in cond and not match): return False
            return True

        # 6. 前走場所・コース・距離
        venues = ["中山","京都","東京","阪神","中京","新潟","福島","小倉","札幌","函館"]
        is_course_cond = any(k in cond for k in ["条件", "コース", "距離", "m", "場所", "ダート", "芝"]) or any(v in cond for v in venues)
        if "前走" in cond and is_course_cond:
            raw_hist = h['hist'][0]['raw'] if h['hist'] else ""
            prev_venue = re.sub(r'\d+回|\d+日', '', raw_hist.split()[1]) if len(raw_hist.split()) > 1 else ""
            curr_venue = r.get('venue', "")

            prev_course = h['hist'][0].get('course', "")
            actual_dist_m = re.search(r'(\d+)', prev_course)
            actual_dist = int(actual_dist_m.group(1)) if actual_dist_m else 0
            
            prev_type = "芝" if "芝" in prev_course else "ダート" if "ダ" in prev_course else ""
            
            # --- 馬場状態（芝/ダート）直接指定 ---
            if "ダート" in cond and prev_type != "ダート": return False
            if "芝" in cond and "ダート" not in cond and prev_type != "芝": return False

            # --- 同条件判定 ---
            if "同条件" in cond or "同コース" in cond:
                if prev_venue != curr_venue: return False
                if actual_dist != r.get('dist', 0): return False
                if prev_type != r.get('type', ""): return False
                return True

            if "同競馬場" in cond and prev_venue != curr_venue: return False
            if "別競馬場" in cond and prev_venue == curr_venue: return False
            if "中央場所" in cond and prev_venue not in ["中山", "東京", "京都", "阪神"]: return False
            
            # --- 距離判定 ---
            if "別距離" in cond:
                if actual_dist == r.get('dist', 0): return False
            elif "距離" in cond or "m" in cond:
                range_m = re.search(r'(\d+)\s*[~〜～]\s*(\d+)', cond)
                if range_m:
                    min_d, max_d = int(range_m.group(1)), int(range_m.group(2))
                    if not (min_d <= actual_dist <= max_d): return False
                else:
                    target_dist_m = re.search(r'(\d+)', cond)
                    target = int(target_dist_m.group(1)) if target_dist_m else r.get('dist', 0)
                    if "同距離超" in cond or "同距離越" in cond:
                        if actual_dist <= target: return False
                    elif "同距離以上" in cond or "m以上" in cond or ("以上" in cond and target_dist_m):
                        if actual_dist < target: return False
                    elif "以下" in cond and target_dist_m:
                        if actual_dist > target: return False
                    else:
                        if actual_dist != target: return False
            
            # 競馬場名の指定があればチェック
            target_v = next((v for v in venues if v in cond), None)
            if target_v and target_v != prev_venue: return False
            
            return True

        # 7. 枠・番
        if "枠" in cond or "番" in cond:
            if "最内枠" in cond: return h.get('w_num') == 1 or h.get('num') == 1
            if "大外枠" in cond: return h.get('num') == r.get('total_horses') # 出走頭数と同じ番号＝大外
            
            nums = re.findall(r'\d+', cond)
            # ...以下、既存の数字範囲判定...
            val = h.get('w_num') if "枠" in cond else h.get('num')
            if val is None: return False
            if len(nums) == 2:
                if not (int(nums[0]) <= val <= int(nums[1])): return False
            elif len(nums) == 1:
                target = int(nums[0])
                if "以内" in cond and val > target: return False
                elif "以内" not in cond and val != target: return False
            return True

        # 8. 負担重量・減量・斤量 (実判定ロジックに修正)
        if "負担重量" in cond or "減量" in cond or "軽量" in cond or "斤量" in cond:
            # 騎手名（h['jock']）に含まれる減量記号（▲△☆★◇）の有無をチェック
            reduction_symbols = ["▲", "△", "☆", "★", "◇"]
            # 騎手名に記号がいずれか含まれているか
            has_reduction = any(s in h.get('jock', '') for s in reduction_symbols)
            
            if "無し" in cond:
                # 「減量無し」が条件の場合、記号がない馬だけを True にする
                return not has_reduction
            elif "有り" in cond:
                # 「減量有り」が条件の場合、記号がある馬だけを True にする
                return has_reduction
                
            # 斤量の数値比較ロジック
            actual_kg_str = str(h.get('kg', "0"))
            try:
                actual_kg = float(actual_kg_str)
            except ValueError:
                actual_kg = 0.0
                
            target_m = re.search(r'(\d+(?:\.\d+)?)', cond)
            if target_m:
                target_kg = float(target_m.group(1))
                if "以上" in cond and actual_kg < target_kg: return False
                if ("以下" in cond or "以内" in cond) and actual_kg > target_kg: return False
                if "未満" in cond and actual_kg >= target_kg: return False
                if "以上" not in cond and "以下" not in cond and "以内" not in cond and "未満" not in cond and actual_kg != target_kg: return False
                
            return True # それ以外の条件はスルー

        # 9. 馬体重 (厳密比較)
        if "馬体重" in cond:
            raw_w = h['hist'][0].get('weight', "") if h['hist'] else ""
            actual_m = re.search(r'(\d{3})', str(raw_w))
            target_m = re.search(r'(\d+)', cond)
            if not actual_m or not target_m: return False
            actual, target = int(actual_m.group(1)), int(target_m.group(1))
            if "以上" in cond and actual < target: return False
            if ("以下" in cond or "以内" in cond) and actual > target: return False
            return True

        # 10. 性別・年齢・所属・クラス
        if "クラス" in cond:
            if "前走クラスが今回以上" in cond:
                curr_class = r.get("class", "クラス不明")
                curr_rank = get_class_rank(curr_class)
                
                prev_raw = h['hist'][0]['raw'] if h['hist'] else ""
                prev_race_name = h['hist'][0].get('race_name', "")
                prev_rank = max(get_class_rank(prev_raw), get_class_rank(prev_race_name))
                
                if curr_rank == 0 or prev_rank == 0: return False
                return prev_rank >= curr_rank
            return True

        if "性別" in cond or "牝馬" in cond or "牡馬・セン" in cond:
            if "牝馬" in cond and "牝" not in h['sex_age']: return False
            if "牡馬・セン" in cond and not any(x in h['sex_age'] for x in ["牡", "セ"]): return False
        if "歳" in cond:
            target_m, actual_m = re.search(r'(\d+)', cond), re.search(r'(\d+)', h['sex_age'])
            if target_m and actual_m:
                t, a = int(target_m.group(1)), int(actual_m.group(1))
                if "以上" in cond and a < t: return False
                if "以下" in cond and a > t: return False
                # 「以上」「以下」なし → 完全一致が必要（例:「馬齢が2歳」= 2歳のみ）
                if "以上" not in cond and "以下" not in cond and t != a: return False
            return True
        if "所属" in cond or "関東馬" in cond or "関西馬" in cond:
            if ("美浦" in cond or "関東" in cond) and h['affi'] != "美浦": return False
            if ("栗東" in cond or "関西" in cond) and h['affi'] != "栗東": return False

        # 11. 自由記述 (騎手名)
        keywords = ["回り", "着", "角", "上がり", "歳", "牝", "牡", "父", "kg", "枠", "番", "距離", "頭", "週", "斤量", "ダート", "体重", "場所", "所属", "クラス", "条件", "馬齢", "性別", "間隔", "通過順", "負担重量", "順位", "生産者"]
        if not any(kw in cond for kw in keywords):
            # 騎手名の場合は「騎手が」を外して比較
            target_jock = cond.replace("騎手が", "").strip()
            clean_cond = re.sub(r'[▲△☆★◇\s　kgkｇ]', '', target_jock)
            clean_jock = re.sub(r'[▲△☆★◇\s　kgkｇ]', '', h['jock'])
            if not clean_jock or (clean_cond not in clean_jock and clean_jock not in clean_cond): return False
        # 10.5. 乗り替わり判定
        if "乗り替わり" in cond:
            # 今回の騎手名（記号を除去）
            current_jock = re.sub(r'[▲△☆★◇\s　]', '', h['jock'])
            
            # 前走の生データから騎手名を抽出
            prev_raw = h['hist'][0]['raw'] if h['hist'] else ""
            # JRAの形式 "8 番人気 田口 貫太 56.0 kg" から名前を抽出
            prev_jock_match = re.search(r'人気\s+([^\s]+?)\s+\d+\.\d+\s*kg', prev_raw)
            
            if prev_jock_match:
                prev_jock = re.sub(r'[▲△☆★◇\s　]', '', prev_jock_match.group(1))
                is_changed = (current_jock != prev_jock)
                
                # 「乗り替わり無し（継続騎乗）」にも対応
                if "無し" in cond or "以外" in cond:
                    return not is_changed
                return is_changed
            return False

        return True
    

    
    except: return False

def evaluate_ultra(h, r, criteria, sire_lineage, mawari_map):
    """馬1頭に対してすべての好走条件をチェックし、
    (最良判定, 詳細文リスト, 該当ルールリスト[{id, grade, weight}]) を返す"""
    best_grade, details, matches = "", [], []
    for rule in criteria:
        if rule['type'] != r['type'] or not (rule['dist_min'] <= r['dist'] <= rule['dist_max']): continue
        v2, v3 = is_valid_cond(rule['c2']), is_valid_cond(rule['c3'])
        res1 = check_condition(rule['c1'], h, r, sire_lineage, mawari_map)
        res2 = check_condition(rule['c2'], h, r, sire_lineage, mawari_map) if res1 else False
        res3 = check_condition(rule['c3'], h, r, sire_lineage, mawari_map) if (res1 and res2) else False
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
            weight = float(rule.get('weight', 1.0))
            w_txt = f" [重み{weight:g}]" if abs(weight - 1.0) >= 0.005 else ""
            details.append(f"項番{rule['id']}: {grade} ({rule['c1']} | {rule['c2']} | {rule['c3']}){w_txt}")
            matches.append({"id": rule['id'], "grade": grade, "weight": weight})
            if grade == "◎" or (grade == "〇" and best_grade != "◎") or (grade == "△" and not best_grade):
                best_grade = grade
    return best_grade, details, matches