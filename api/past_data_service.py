import sqlite3
import os
import re
import zipfile

def get_db_connection(base_dir):
    # Check for Vercel /tmp or local fallback
    db_path = os.path.join('/tmp', 'past_data.db') if os.environ.get('VERCEL') else os.path.join(base_dir, 'past_data.db')
    
    if not os.path.exists(db_path):
        # 展開元ZIPのパス
        zip_path = os.path.join(base_dir, 'past_data.zip')
        if os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # /tmp に展開するか、localならbase_dirに展開
                extract_path = '/tmp' if os.environ.get('VERCEL') else base_dir
                zip_ref.extract('past_data.db', extract_path)
        else:
            return None

    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

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

def analyze_races(rows):
    if not rows:
        return None

    total_races = len(rows)
    
    # 枠番(馬番) Stats
    # format: {horse_number: {'runs': 0, 'wins': 0, 'top3': 0}}
    umaban_stats = {}
    
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
        return {"error": "Database not found"}
        
    cursor = conn.cursor()

    query = '''
        SELECT rank, horse_number, corner_4, jockey, time, agari_3f, weight
        FROM races
        WHERE place = ? AND track_type = ? AND distance = ?
    '''
    params = [place, track_type, distance]

    if condition and condition != "null" and condition != "":
        query += " AND condition = ?"
        params.append(condition)

    if race_class and race_class != "null" and race_class != "":
        query += " AND race_class = ?"
        params.append(race_class)

    cursor.execute(query, tuple(params))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    res = analyze_races(rows)

    return {
        "success": True,
        "results": res
    }
