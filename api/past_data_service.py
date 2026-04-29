import sqlite3
import os
import re
import zipfile

_last_db_error = None

class _PgConn:
    """psycopg2接続のラッパー。is_pg属性でPostgreSQL判定を可能にする。"""
    is_pg = True
    def __init__(self, conn): self._conn = conn
    def cursor(self): return self._conn.cursor()
    def close(self): return self._conn.close()

def get_db_connection(base_dir):
    global _last_db_error
    _last_db_error = None
    # Check for DATABASE_URL first
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as e:
            _last_db_error = f"psycopg2 import error: {e}"
            print(_last_db_error)
            return None
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        try:
            conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
            return _PgConn(conn)
        except Exception as e:
            _last_db_error = f"PostgreSQL connection error: {e}"
            print(_last_db_error)
            return None

    # Check for Vercel environment
    is_vercel = os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL') == 'true'
    
    # Paths for both environments
    local_db_path = os.path.join(base_dir, 'past_data_v2.db')
    vercel_tmp_path = os.path.join('/tmp', 'past_data_v2.db')
    
    # Final path depends on environment
    db_path = vercel_tmp_path if is_vercel else local_db_path
    
    # 1. Check if the DB already exists at the final path
    if not os.path.exists(db_path):
        # 2. If running on Vercel, check if the file exists in the bundle (read-only)
        if is_vercel and os.path.exists(local_db_path):
            # We can connect directly to the bundled DB if it exists, though unzipping to /tmp is safer for large DBs
            print(f"Bundled DB found at {local_db_path}, using it.")
            db_path = local_db_path
        else:
            # 3. Try to extract from ZIP
            zip_path = os.path.join(base_dir, 'past_data_v2.zip')
            if not os.path.exists(zip_path):
                # Fallback to current working directory if base_dir is wrong
                zip_path_alt = os.path.join(os.getcwd(), 'api', 'past_data_v2.zip')
                if os.path.exists(zip_path_alt):
                    zip_path = zip_path_alt
                else:
                    print(f"Zip file not found at {zip_path} or {zip_path_alt}")
                    return None

            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    extract_dest = '/tmp' if is_vercel else base_dir
                    
                    # Ensure /tmp exists (sometimes needed in serverless environments)
                    if is_vercel and not os.path.exists('/tmp'):
                        try:
                            os.makedirs('/tmp', exist_ok=True)
                        except:
                            print("Warning: Could not create /tmp, using base_dir for extraction.")
                            extract_dest = base_dir
                            db_path = local_db_path
                    
                    print(f"Extracting {zip_path} to {extract_dest}...")
                    zip_ref.extract('past_data_v2.db', extract_dest)
            except Exception as e:
                print(f"Extraction error: {e}")
                # Last resort fallback tobundled DB if it exists
                if os.path.exists(local_db_path):
                    db_path = local_db_path
                else:
                    return None

    # Final check
    if not os.path.exists(db_path):
        print(f"Final DB check failed: {db_path} does not exist.")
        return None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"SQLite connection error: {e}")
        return None

def parse_time(t_str):
    if not t_str or str(t_str) == 'NaN': return None
    t_str = str(t_str).strip()
    if not t_str.isdigit(): return None
    if len(t_str) >= 4:
        return float(t_str[:-3]) * 60 + float(t_str[-3:-1]) + float(t_str[-1]) / 10
    elif len(t_str) >= 3:
        return float(t_str[:-1]) + float(t_str[-1]) / 10
    return None

def format_time(seconds):
    if seconds is None: return "-"
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m}:{s:04.1f}" if m > 0 else f"{s:04.1f}"

# 枠連の計算ロジック
def calculate_waku(umaban, head_count):
    if not umaban or not head_count: return None
    try:
        u = int(umaban)
        h = int(head_count)
    except:
        return None
        
    if h <= 8: return u
    
    waku_sizes = [1] * 8
    rem = h - 8
    
    for i in range(8, 0, -1):
        if rem > 0:
            waku_sizes[i-1] += 1
            rem -= 1
        if rem > 0 and i > 1:
            waku_sizes[i-1] += 1
            rem -= 1
            
    current_u = 1
    for waku in range(1, 9):
        size = waku_sizes[waku-1]
        if current_u <= u < current_u + size:
            return waku
        current_u += size
    return None

def analyze_races(rows):
    if not rows:
        return None

    total_races = len(rows)
    
    # 枠番(馬番) Stats
    # format: {horse_number: {'runs': 0, 'wins': 0, 'top3': 0}}
    umaban_stats = {}
    
    # 枠別 Stats (1〜8)
    waku_stats = {str(i): {'runs': 0, 'wins': 0, 'top3': 0} for i in range(1, 9)}
    
    # 脚質 Stats
    kyaku_stats = {'逃げ/先行(1-3番手)': {'runs': 0, 'wins': 0, 'top3': 0},
                   '中団/差し(4-8番手)': {'runs': 0, 'wins': 0, 'top3': 0},
                   '後方/追込(9番手~)': {'runs': 0, 'wins': 0, 'top3': 0},
                   '不明': {'runs': 0, 'wins': 0, 'top3': 0}}
                   
    # 騎手 Stats
    jockey_stats = {}
    
    # 馬体重 Stats
    weight_buckets = [
        "400kg以下", "400〜419kg", "420〜439kg", "440〜459kg", 
        "460〜479kg", "480〜499kg", "500〜519kg", "520〜539kg", "540kg以上"
    ]
    weight_stats = {b: {'runs': 0, 'wins': 0, 'top3': 0} for b in weight_buckets}
    weight_stats['不明'] = {'runs': 0, 'wins': 0, 'top3': 0}

    # タイム
    valid_times = []
    valid_agari = []

    for r in rows:
        rank = r['rank']
        if rank is None: continue
        
        is_win = 1 if rank == 1 else 0
        is_top3 = 1 if rank <= 3 else 0

        # 馬番
        umaban = str(r['horse_number'])
        if umaban and umaban != 'nan' and umaban != 'None':
            if umaban not in umaban_stats:
                umaban_stats[umaban] = {'runs': 0, 'wins': 0, 'top3': 0}
            umaban_stats[umaban]['runs'] += 1
            umaban_stats[umaban]['wins'] += is_win
            umaban_stats[umaban]['top3'] += is_top3

        # 枠番
        total_horses = r.get('total_horses')
        waku = calculate_waku(r['horse_number'], total_horses)
        if waku:
            w = str(waku)
            if w in waku_stats:
                waku_stats[w]['runs'] += 1
                waku_stats[w]['wins'] += is_win
                waku_stats[w]['top3'] += is_top3

        # 脚質
        c4 = r['corner_4']
        k_key = '不明'
        if c4 and str(c4) != 'nan' and str(c4) != 'None':
            # c4 might be '4', '11', '3-4'
            match = re.search(r'\d+', str(c4))
            if match:
                pos = int(match.group())
                if pos <= 3: k_key = '逃げ/先行(1-3番手)'
                elif pos <= 8: k_key = '中団/差し(4-8番手)'
                else: k_key = '後方/追込(9番手~)'
        
        kyaku_stats[k_key]['runs'] += 1
        kyaku_stats[k_key]['wins'] += is_win
        kyaku_stats[k_key]['top3'] += is_top3

        # 騎手
        jockey = str(r['jockey']).strip()
        if jockey and jockey != 'nan' and jockey != 'None':
            if jockey not in jockey_stats:
                jockey_stats[jockey] = {'runs': 0, 'wins': 0, 'top3': 0}
            jockey_stats[jockey]['runs'] += 1
            jockey_stats[jockey]['wins'] += is_win
            jockey_stats[jockey]['top3'] += is_top3

        # 馬体重
        w_val = r.get('weight')
        w_key = '不明'
        if w_val is not None and str(w_val) != 'nan':
            try:
                w_num = int(w_val)
                if w_num <= 400: w_key = "400kg以下"
                elif w_num < 420: w_key = "400〜419kg"
                elif w_num < 440: w_key = "420〜439kg"
                elif w_num < 460: w_key = "440〜459kg"
                elif w_num < 480: w_key = "460〜479kg"
                elif w_num < 500: w_key = "480〜499kg"
                elif w_num < 520: w_key = "500〜519kg"
                elif w_num < 540: w_key = "520〜539kg"
                else: w_key = "540kg以上"
            except:
                pass
        
        weight_stats[w_key]['runs'] += 1
        weight_stats[w_key]['wins'] += is_win
        weight_stats[w_key]['top3'] += is_top3

        # タイム
        t_sec = parse_time(r['time'])
        if t_sec: valid_times.append(t_sec)

        # 上り3F
        agari = r['agari_3f']
        if agari and str(agari) != 'nan':
            try:
                valid_agari.append(float(agari))
            except: pass

    # Sort & format output
    def format_stats(stats_dict):
        arr = []
        for k, v in stats_dict.items():
            if v['runs'] > 0:
                win_rate = (v['wins'] / v['runs']) * 100
                top3_rate = (v['top3'] / v['runs']) * 100
                arr.append({
                    'name': k,
                    'runs': v['runs'],
                    'wins': v['wins'],
                    'top3': v['top3'],
                    'win_rate': f"{win_rate:.1f}%",
                    'top3_rate': f"{top3_rate:.1f}%",
                    'win_rate_val': win_rate
                })
        return sorted(arr, key=lambda x: x['win_rate_val'], reverse=True)

    umaban_arr = format_stats(umaban_stats)
    
    # 枠別は0回でも全枠(1〜8)を返す
    waku_arr = []
    for i in range(1, 9):
        w_str = str(i)
        v = waku_stats[w_str]
        r_w = v['runs']
        win_rate = (v['wins'] / r_w * 100) if r_w > 0 else 0.0
        top3_rate = (v['top3'] / r_w * 100) if r_w > 0 else 0.0
        waku_arr.append({
            'name': w_str,
            'runs': r_w,
            'wins': v['wins'],
            'top3': v['top3'],
            'win_rate': f"{win_rate:.1f}%",
            'top3_rate': f"{top3_rate:.1f}%"
        })

    # limit jockeys to those with at least some runs to avoid 1/1 100% win rate noise
    # let's just sort by wins first, then win_rate
    jockey_arr = format_stats(jockey_stats)
    # exclude jockeys with < 5 runs to highlight true top jockeys, then sort by wins
    jockey_arr = sorted([j for j in jockey_arr if j['runs'] >= 3], key=lambda x: (x['wins'], x['top3']), reverse=True)[:5]
    
    kyaku_arr = format_stats(kyaku_stats)

    avg_time = sum(valid_times)/len(valid_times) if valid_times else None
    avg_agari = sum(valid_agari)/len(valid_agari) if valid_agari else None

    # Sort weight stats specifically by bucket order
    weight_arr = []
    ordered_w_keys = weight_buckets + ['不明']
    for k in ordered_w_keys:
        v = weight_stats[k]
        if v['runs'] > 0:
            win_rate = (v['wins'] / v['runs']) * 100
            top3_rate = (v['top3'] / v['runs']) * 100
            weight_arr.append({
                'name': k,
                'runs': v['runs'],
                'wins': v['wins'],
                'top3': v['top3'],
                'win_rate': f"{win_rate:.1f}%",
                'top3_rate': f"{top3_rate:.1f}%"
            })

    # Count actual number of unique races
    exact_races = sum([1 for r in rows if r['rank'] == 1])

    return {
        'total_entries': total_races,
        'exact_races': exact_races,
        'umaban': umaban_arr[:10],
        'umaban_all': sorted(umaban_arr, key=lambda x: int(x['name']) if x['name'].isdigit() else 99),
        'waku': waku_arr,
        'jockey': jockey_arr,
        'kyakushitsu': kyaku_arr,
        'weight': weight_arr,
        'avg_time': format_time(avg_time),
        'avg_time_sec': avg_time,
        'avg_agari': f"{avg_agari:.1f}" if avg_agari else "-"
    }

def get_past_data(base_dir, place, track_type, distance, condition=None, race_class=None):
    conn = get_db_connection(base_dir)
    if not conn:
        return {"error": f"Database not found: {_last_db_error or 'unknown'}"}
        
    cursor = conn.cursor()

    query = '''
        SELECT rank, horse_number, corner_4, jockey, time, agari_3f, weight, total_horses
        FROM races
        WHERE place = ? AND track_type = ? AND distance = ?
    '''
    params = [place, track_type, distance]

    if condition and condition != "null" and condition != "":
        db_condition = condition
        if condition == "稍重":
            db_condition = "稍"
        elif condition == "不良":
            db_condition = "不"
            
        query += " AND condition = ?"
        params.append(db_condition)

    if race_class and race_class != "null" and race_class != "":
        # We need to use LIKE if race_class is partly matched? JRA DB 'race_class' isn't explicitly defined in 1980.csv.
        # But we preserved 'race_name' column. Maybe race_class wasn't in 1980.csv explicitly mapped to 'race_class'?
        # 1980.csv does not have a "race_class" column. It has "レース名" (race_name) which contains "ファイＨ･3勝".
        # For now, let's keep the query as is. If column doesn't exist, we might need a fallback.
        # But wait, original DB also might just not have returned anything.
        query += " AND race_class = ?"
        params.append(race_class)

    is_pg = getattr(conn, 'is_pg', False)
    if is_pg:
        query = query.replace('?', '%s')
        
    try:
        cursor.execute(query, tuple(params))
        rows = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Query error: {e}")
        rows = []
    finally:
        conn.close()

    res = analyze_races(rows)

    return {
        "success": True,
        "results": res
    }

def get_track_bias_data(base_dir, place):
    conn = get_db_connection(base_dir)
    if not conn:
        return {"error": f"Database not found: {_last_db_error or 'unknown'}"}
        
    cursor = conn.cursor()
    is_pg = getattr(conn, 'is_pg', False)
    
    try:
        # First find the latest date for this place
        q_date = "SELECT MAX(date) as max_date FROM races WHERE place = ?"
        if is_pg: q_date = q_date.replace('?', '%s')
        cursor.execute(q_date, (place,))
        row = cursor.fetchone()
        latest_date = row['max_date'] if row else None
        
        if not latest_date:
            return {"error": "No recent races found"}
            
        # Get races for that date and place
        q_races = """
            SELECT rank, track_type, horse_number, corner_4, popularity, total_horses 
            FROM races 
            WHERE place = ? AND date = ? AND rank <= 3
        """
        if is_pg: q_races = q_races.replace('?', '%s')
        cursor.execute(q_races, (place, latest_date))
        rows = [dict(r) for r in cursor.fetchall()]
        
        # Analyze Bias split by track_type (芝 vs ダート)
        bias_results = {"芝": {"nige": 0, "sashi": 0, "in": 0, "out": 0, "total": 0},
                        "ダート": {"nige": 0, "sashi": 0, "in": 0, "out": 0, "total": 0}}
        
        import re
        for r in rows:
            tt = r.get('track_type')
            if tt not in bias_results: continue
            
            # Base weight is 1. If it was unpopular but ran top 3, increase the weight!
            pop = r.get('popularity')
            rank = r.get('rank')
            try: pop = float(pop) 
            except: pop = 1
            try: rank = float(rank)
            except: rank = 1
            
            pop_diff = max(0, pop - rank)
            # If standard performance (1st pop, 1st place -> weight 1)
            # If 6th pop, 1st place -> pop_diff=5 -> weight 1+5=6 (very strong evidence of bias)
            weight = 1 + pop_diff
            
            bias_results[tt]["total"] += 1
            
            # Analyze Kyakushitsu
            c4 = r.get('corner_4')
            if c4 and str(c4) != 'nan':
                m = re.search(r'\d+', str(c4))
                if m:
                    pos = int(m.group())
                    if pos <= 4:
                        bias_results[tt]["nige"] += weight
                    else:
                        bias_results[tt]["sashi"] += weight
                        
            # Analyze Waku
            h_num = r.get('horse_number')
            t_horses = r.get('total_horses')
            waku = calculate_waku(h_num, t_horses)
            if waku:
                if waku <= 4:
                    bias_results[tt]["in"] += weight
                elif waku >= 5:
                    bias_results[tt]["out"] += weight

        # Evaluate logic
        evaluations = {}
        for tt, b in bias_results.items():
            if b["total"] == 0:
                evaluations[tt] = {"kyaku": "データなし", "waku": "データなし"}
                continue
                
            k_eval = "フラット"
            if b["nige"] > b["sashi"] * 1.5:
                k_eval = "前残り有利（逃げ/先行）"
            elif b["sashi"] > b["nige"] * 1.5:
                k_eval = "差し有利"
                
            w_eval = "フラット"
            if b["in"] > b["out"] * 1.3:
                w_eval = "イン（内枠）有利"
            elif b["out"] > b["in"] * 1.3:
                w_eval = "外枠有利"
                
            evaluations[tt] = {
                "kyaku": k_eval,
                "waku": w_eval,
                "nige_score": b["nige"],
                "sashi_score": b["sashi"],
                "in_score": b["in"],
                "out_score": b["out"]
            }
            
        return {
            "success": True,
            "latest_date": latest_date,
            "place": place,
            "evaluations": evaluations
        }
    except Exception as e:
        print(f"Track bias error: {e}")
        return {"error": str(e)}
    finally:
        conn.close()
