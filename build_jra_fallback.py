import json
import os

venues = {"札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata", "東京": "tokyo", "中山": "nakayama", "中京": "chukyo", "京都": "kyoto", "阪神": "hanshin", "小倉": "kokura"}
img_map = {}
types = ["芝", "ダート", "障害"]
dists = [str(d) for d in range(1000, 4200, 100)] + ["1150"]

for v_ja, v_en in venues.items():
    img_map[v_ja] = {}
    url = f"https://www.jra.go.jp/facilities/race/{v_en}/course/img/pic_course_heimenzu.gif"
    for t in types:
        for d in dists:
            key = f"{t}{d}"
            img_map[v_ja][key] = url

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api', 'jra_images.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(img_map, f, ensure_ascii=False, indent=2)
print("Fallback mapping created at", out_path)
