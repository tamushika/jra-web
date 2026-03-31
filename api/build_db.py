import pandas as pd
import sqlite3
import os

# パスの設定
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, 'csv', 'data_2020-2025.csv')
DB_PATH = os.path.join(BASE_DIR, 'past_data.db')

def build_database():
    print(f"Loading CSV from {CSV_PATH}...")
    try:
        # cp932で読み込み
        df = pd.read_csv(CSV_PATH, encoding='cp932', dtype=str)
    except Exception as e:
        print(f"Error loading CSV: {e}")
        return

    print(f"Loaded {len(df)} rows. Processing...")

    # 必要なカラムの抽出とリネーム
    # ['日付', '開催', '着順', '芝・ダ', '距離', '馬場状態', '馬番', '4角', '騎手', '走破タイム', '上り3F', '人気', '単勝配当']
    cols_to_keep = {
        '日付': 'date',
        '開催': 'kaisai',
        '着順': 'rank',
        '芝・ダ': 'track_type',
        '距離': 'distance',
        '馬場状態': 'condition',
        '馬番': 'horse_number',
        '4角': 'corner_4',
        '騎手': 'jockey',
        '走破タイム': 'time',
        '上り3F': 'agari_3f',
        '人気': 'popularity',
        '単勝配当': 'odds'
    }

    # 存在するカラムだけを抽出
    available_cols = [c for c in cols_to_keep.keys() if c in df.columns]
    keep_df = df[available_cols].rename(columns={c: cols_to_keep[c] for c in available_cols})

    # 「場所」を「開催」から抽出（例: '5中8' -> '中山'）
    # 開催の文字列に含まれる漢字１文字で判定
    place_map = {
        '札': '札幌', '函': '函館', '福': '福島', '新': '新潟',
        '東': '東京', '中': '中山', '名': '中京', '京': '京都',
        '阪': '阪神', '小': '小倉'
    }
    
    def extract_place(kaisai_str):
        if pd.isna(kaisai_str): return None
        for k, v in place_map.items():
            if k in kaisai_str:
                return v
        return None

    keep_df['place'] = keep_df['kaisai'].apply(extract_place)

    # 芝・ダ のフォーマット統一 ('ダ' -> 'ダート')
    keep_df['track_type'] = keep_df['track_type'].replace({'ダ': 'ダート'})

    # 距離を数値に変換（変換できないものはNaN）
    keep_df['distance'] = pd.to_numeric(keep_df['distance'], errors='coerce')

    # 着順を数値に（'１' -> 1, '取消' -> NaN）
    import re
    def parse_rank(rank_str):
        if pd.isna(rank_str): return None
        match = re.search(r'\d+', str(rank_str).translate(str.maketrans('１２３４５６７８９０', '1234567890')))
        if match: return int(match.group())
        return None
    
    keep_df['rank'] = keep_df['rank'].apply(parse_rank)
    
    # NaNの除外（着順がないものは未出走なので不要）
    keep_df = keep_df.dropna(subset=['rank'])

    # SQLiteDBへの保存
    print(f"Saving to SQLite database: {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    keep_df.to_sql('races', conn, if_exists='replace', index=False)
    
    # インデックスの作成 (検索を高速化するため)
    cursor = conn.cursor()
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_race_search ON races(place, track_type, distance, condition)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_race_search_nocond ON races(place, track_type, distance)')
    conn.commit()
    conn.close()

    print(f"Database built successfully. Total valid records: {len(keep_df)}")

if __name__ == "__main__":
    build_database()
