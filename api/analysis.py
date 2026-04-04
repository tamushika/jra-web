import re
import pandas as pd
import os

def load_csv_criteria(venue_name, base_dir):
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 新しい構成: DATA/{会場}/好走条件/criteria.csv
    file_path = os.path.join(current_script_dir, "DATA", venue_name, "好走条件", "criteria.csv")
    
    if not os.path.exists(file_path):
        # 互換性のための旧パスチェック（念のため）
        old_filename_map = {"中山": "nakayama.csv", "京都": "kyoto.csv", "中京": "tyukyo.csv", "阪神": "hanshin.csv", "東京": "tokyo.csv", "小倉": "kokura.csv"}
        old_path = os.path.join(current_script_dir, "csv", old_filename_map.get(venue_name, "kyoto.csv"))
        if os.path.exists(old_path):
            file_path = old_path
        else:
            print(f"【警告】好走条件ファイルが見つかりません: {venue_name}")
            return []

    # 以下、読み込み処理（df = pd.read_csv...）
        
    try:
        df = pd.read_csv(file_path, header=None, encoding='utf-8-sig') 
        criteria = []
        for _, row in df.iterrows():
            dist_str = str(row[2])
            dist_range = re.findall(r'\d+', dist_str)
            if not dist_range: continue
            dist_min = int(dist_range[0])
            dist_max = int(dist_range[1]) if len(dist_range) > 1 else dist_min
            criteria.append({
                "id": row[0], "type": row[1], "dist_min": dist_min, "dist_max": dist_max, 
                "c1": str(row[3]), "c2": str(row[4]), "c3": str(row[5])
            })
        return criteria
    except: return []

def load_course_feature(venue, race_type, distance, base_dir):
    """
    指定された会場・条件のコース特徴テキストを読み込む
    新パス例: DATA/京都/コース情報/芝1200.txt
    """
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    filename = f"{race_type}{distance}.txt"
    
    file_path = os.path.join(current_script_dir, "DATA", venue, "コース情報", filename)
    
    if not os.path.exists(file_path):
        # 旧パスチェック（念のため）
        old_path = os.path.join(current_script_dir, "DATA", venue, filename)
        if os.path.exists(old_path):
            file_path = old_path
        else:
            return f"【情報】{venue} {race_type}{distance}m の特徴データは登録されていません。"

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            return f.read()
    except Exception as e:
        return f"エラー: ファイルの読み込みに失敗しました ({str(e)})"

def load_sire_lineage(base_dir):
    """syuboba.csv を読み込む (DATA/共通/ フォルダ内)"""
    lineage_map = {}
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    
    file_path = os.path.join(current_script_dir, "DATA", "共通", "syuboba.csv")
    
    if not os.path.exists(file_path):
        # 旧パスチェック
        old_path = os.path.join(base_dir, ".venv", "csv", "syuboba.csv")
        if not os.path.exists(old_path):
            old_path = os.path.join(current_script_dir, "csv", "syuboba.csv")
        
        if os.path.exists(old_path):
            file_path = old_path
        else:
            print("【警告】syuboba.csv が見つかりません")
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
    """
    指定された会場・条件の注目産駒データ（種牡馬ランキング）を読み込む
    """
    filename = f"{race_type}{distance}.csv"
    # base_dir は api/ フォルダを指している
    file_path = os.path.join(base_dir, "DATA", venue, "注目産駒", filename)

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

        # 5. 血統判定 (父/母父)
        if "父" in cond:
            # 置換対象を増やして「父・母父が」などにも対応
            target = cond
            for prefix in ["父が", "父or母父が", "父・母父が", "系", "種牡馬", "以外"]:
                target = target.replace(prefix, "")
            target = target.strip()
            
            match = (target == h['sire']) or (target in sire_lineage.get(h['sire'], ""))
            if "母父" in cond:
                match = match or (target == h.get('bms', "")) or (target in sire_lineage.get(h.get('bms', ""), ""))
            
            if ("以外" in cond and match) or ("以外" not in cond and not match): return False
            return True

        # 6. 前走場所・コース・距離
        if "前走" in cond:
            raw_hist = h['hist'][0]['raw'] if h['hist'] else ""
            prev_venue = re.sub(r'\d+回|\d+日', '', raw_hist.split()[1]) if len(raw_hist.split()) > 1 else ""
            curr_venue = r.get('venue', "")

            if "同コース" in cond:
                prev_course = h['hist'][0].get('course', "")
                actual_dist_m = re.search(r'(\d+)', prev_course)
                # 競馬場チェック
                if prev_venue != curr_venue: return False
                # 距離チェック
                if not actual_dist_m or int(actual_dist_m.group(1)) != r.get('dist', 0): return False
                # 芝/ダート チェック
                prev_type = "芝" if "芝" in prev_course else "ダート" if "ダ" in prev_course else ""
                if prev_type != r.get('type', ""): return False
                return True

            if "同競馬場" in cond and prev_venue != curr_venue: return False
            if "別競馬場" in cond and prev_venue == curr_venue: return False
            if "中央場所" in cond and prev_venue not in ["中山", "東京", "京都", "阪神"]: return False
            if "距離" in cond or "m" in cond:
                prev_course = h['hist'][0].get('course', "")
                actual_dist_m = re.search(r'(\d+)', prev_course)
                target_dist_m = re.search(r'(\d+)', cond)
                if actual_dist_m:
                    actual = int(actual_dist_m.group(1))
                    target = int(target_dist_m.group(1)) if target_dist_m else r.get('dist', 0)
                    if "同距離超" in cond and actual <= target: return False
                    if "同距離以上" in cond and actual < target: return False
                    if "同距離" in cond and "以上" not in cond and "超" not in cond and actual != target: return False
            venues = ["中山","京都","東京","阪神","中京","新潟","福島","小倉","札幌","函館"]
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

        # 8. 負担重量・減量 (実判定ロジックに修正)
        if "負担重量" in cond or "減量" in cond or "軽量" in cond:
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
                
            return True # それ以外の条件（単に「負担重量」のみ等）は現状スルー

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

        # 10. 性別・年齢・所属
        if "性別" in cond or "牝馬" in cond or "牡馬・セン" in cond:
            if "牝馬" in cond and "牝" not in h['sex_age']: return False
            if "牡馬・セン" in cond and not any(x in h['sex_age'] for x in ["牡", "セ"]): return False
        if "歳" in cond:
            target_m, actual_m = re.search(r'(\d+)', cond), re.search(r'(\d+)', h['sex_age'])
            if target_m and actual_m:
                t, a = int(target_m.group(1)), int(actual_m.group(1))
                if "以上" in cond and a < t: return False
                if "以下" in cond and a > t: return False
            return True
        if "所属" in cond or "関東馬" in cond or "関西馬" in cond:
            if ("美浦" in cond or "関東" in cond) and h['affi'] != "美浦": return False
            if ("栗東" in cond or "関西" in cond) and h['affi'] != "栗東": return False

        # 11. 自由記述 (騎手名)
        keywords = ["回り", "着", "角", "上がり", "歳", "牝", "牡", "父", "kg", "枠", "番", "距離", "頭", "週", "斤量", "ダート", "体重", "場所", "所属", "クラス", "条件", "馬齢", "性別", "間隔", "通過順", "負担重量", "順位"]
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
    """馬1頭に対してすべての好走条件をチェックし、結果を返す"""
    best_grade, details = "", []
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
            details.append(f"項番{rule['id']}: {grade} ({rule['c1']} | {rule['c2']} | {rule['c3']})")
            if grade == "◎" or (grade == "〇" and best_grade != "◎") or (grade == "△" and not best_grade):
                best_grade = grade
    return best_grade, details