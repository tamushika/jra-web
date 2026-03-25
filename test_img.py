import requests
from bs4 import BeautifulSoup
import re

venues = {"中山": "nakayama", "東京": "tokyo"}
for v, v_en in venues.items():
    url = f"https://www.jra.go.jp/facilities/race/{v_en}/course/index.html"
    r = requests.get(url)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    for a in soup.find_all('a', href=True):
        if 'javascript' in a['href'] and 'course' in a.get('class', []):
            pass # old JRA
    # Lets just print course images
    images = []
    for img in soup.find_all('img', src=re.compile(r'course')):
        images.append((img.get('alt', ''), img.get('src')))
    print(v, images[:5])
