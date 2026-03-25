import requests

urls = [
    "https://cdn.netkeiba.com/img.race/course/course_base/06_2_1200.png",
    "https://cdn.netkeiba.com/img.race/course/06_2_1200.png",
    "https://race.sp.netkeiba.com/img/course/06_2_1200.png",
    "https://www.keibalab.jp/db/race/course/image/tokyo_t1600.png",
    "https://www.keibalab.jp/img/race/course/tokyo_t1600.png",
    "https://www.keibalab.jp/img/race/course/nakayama_d1200.png"
]
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.keibalab.jp/"
}

for u in urls:
    try:
        r = requests.head(u, headers=headers, timeout=3)
        print(f"{r.status_code} {u}")
    except Exception as e:
        print(f"Error {u}: {e}")
