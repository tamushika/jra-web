import requests
from bs4 import BeautifulSoup
import json
import time
import re

venues = {"札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata", "東京": "tokyo", "中山": "nakayama", "中京": "chukyo", "京都": "kyoto", "阪神": "hanshin", "小倉": "kokura"}
img_map = {}

for v_ja, v_en in venues.items():
    url = f"https://www.jra.go.jp/facilities/race/{v_en}/course/"
    print("Fetching", url)
    r = requests.get(url, timeout=5)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    
    img_map[v_ja] = {}
    for a in soup.find_all('a'):
        src = a.get('data-src') or a.get('data-image')
        # sometimes it's class btn, or just a link 
        text = a.get_text(strip=True)
        if hasattr(a, 'onclick') and 'img' in str(a.get('onclick')): pass # Some older pages use onclick="showImg('img/...')
        
        # Look for 芝 or ダート
        if ('芝' in text or 'ダート' in text or 'ダ' in text or '障害' in text) and src:
            type_str = "芝" if "芝" in text else ("ダート" if "ダ" in text else "障害")
            dist_m = re.search(r'(\d+)m', text)
            if dist_m:
                dist = dist_m.group(1)
                full_src = f"https://www.jra.go.jp/facilities/race/{v_en}/course/{src}"
                key = f"{type_str}{dist}"
                img_map[v_ja][key] = full_src
                
        # Handle JRA new course page format where images are directly in div blocks!
        # JRA changed course page layout recently.
    
    # Look for all imgs if the above failed
    if not img_map[v_ja]:
        for div in soup.find_all('div', class_=re.compile('course')):
            # fallback scraper here if needed
            pass

    time.sleep(1)

with open('jra_images.json', 'w', encoding='utf-8') as f:
    json.dump(img_map, f, ensure_ascii=False, indent=2)
print("Done mapping!", img_map)
