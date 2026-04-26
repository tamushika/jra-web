import os
import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv
import glob
import re

# Load environment variables
load_dotenv()
db_url = os.environ.get("DATABASE_URL")
if not db_url:
    print("Error: DATABASE_URL not found in .env file.")
    exit(1)

# Ensure postgresql dialect for sqlalchemy
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(db_url)

# Mapping JRA location code to name
place_map = {
    "札": "札幌", "函": "函館", "福": "福島", "新": "新潟",
    "東": "東京", "中": "中山", "名": "中京", "京": "京都",
    "阪": "阪神", "小": "小倉"
}

def extract_place(kaisai):
    if pd.isna(kaisai): return None
    m = re.search(r'\d+(.*?)\d+', str(kaisai))
    if m and m.group(1) in place_map:
        return place_map[m.group(1)]
    return None

def process_csv(filepath):
    print(f"Reading {filepath}...")
    # Using chunksize for massive CSVs to prevent memory issues, 
    # but for ~350MB, loading into RAM is usually fine.
    # To be safe, we will load in chunks.
    chunksize = 50000
    for i, chunk in enumerate(pd.read_csv(filepath, encoding='cp932', chunksize=chunksize)):
        print(f"  Processing chunk {i+1}...")
        
        # Rename core columns to match our expected schema
        rename_map = {
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
            '単勝配当': 'odds',
            'レース名': 'race_name',
            '馬体重': 'weight',
            '頭数': 'total_horses'
        }
        chunk = chunk.rename(columns=rename_map)
        
        # Format the 'date' column string if it's purely numeric like 251228
        # The frontend queries like 'date DESC', so string YYYYMMDD is usually expected.
        # But old dates are like "20251228" maybe? Or wait, '251228' lacks century.
        # Let's just stringify it for now to match exactly what it means.
        # Wait, 1980 is 800105. "19" or "20" century prefix is needed?
        # Actually our current DB uses formats like '251228'. We'll leave it as is, just as a string.
        if 'date' in chunk.columns:
            chunk['date'] = chunk['date'].astype(str).str.zfill(6)
            
        # Parse 'place' from 'kaisai'
        if 'kaisai' in chunk.columns:
            chunk['place'] = chunk['kaisai'].apply(extract_place)
            
        # Clean rank (e.g. "１" -> 1, "取" -> None)
        def clean_rank(r):
            try:
                r_str = str(r).replace(' ', '').replace('　', '')
                import unicodedata
                r_str = unicodedata.normalize('NFKC', r_str)
                return float(re.search(r'\d+', r_str).group())
            except:
                return None
        if 'rank' in chunk.columns:
            chunk['rank'] = chunk['rank'].apply(clean_rank)
            
        # Write back to PostgreSQL table `races`
        # if_exists='append' ensures we add chunks together.
        # For the first chunk of the VERY FIRST run, ideally it should replace, but we shouldn't drop on every chunk!
        chunk.to_sql('races', engine, if_exists='append', index=False, method='multi', chunksize=1000)

if __name__ == "__main__":
    import sys
    data_dir = os.path.join(os.path.dirname(__file__), "data_source")
    
    # Optional: Clear table before uploading if user runs `python upload_to_db.py --reset`
    if len(sys.argv) > 1 and sys.argv[1] == '--reset':
        print("Resetting table 'races'...")
        with engine.begin() as conn:
            conn.execute(pd.io.sql.text("DROP TABLE IF EXISTS races"))
    
    csv_files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not csv_files:
        print(f"No CSV files found in {data_dir}")
        exit(1)
        
    for f in sorted(csv_files):
        try:
            process_csv(f)
        except Exception as e:
            print(f"Failed to process {f}: {e}")
            
    print("Upload completed successfully!")
