import os
import glob
import time
import google.generativeai as genai

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("No GEMINI_API_KEY found.")
    exit(1)

genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

data_dirs = [
    r"c:\Users\owner\project\.venv\DATA",
    r"c:\Users\owner\project\.venv\jra-web\DATA",
    r"c:\Users\owner\project\.venv\jra-web\api\DATA"
]

txt_files = []
for d in data_dirs:
    txt_files.extend(glob.glob(os.path.join(d, "**", "*.txt"), recursive=True))

txt_files = list(set(txt_files))
print(f"Found {len(txt_files)} text files in DATA directories.")

count = 0
for filepath in txt_files:
    if "course" in filepath.lower() or "mawari" in filepath.lower(): continue
    filename = os.path.basename(filepath)
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read().strip()
    except:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as e:
            print(f"Read error {filename}: {e}")
            continue
    
    if "波乱指数" not in content and "【" not in content:
        continue

    # Skip if already expanded
    if len(content) > 250:
        continue

    print(f"Enhancing {filepath}...")
    prompt = f"""
以下のJRA競馬場のコース特徴テキストを、スタートからゴールまでの起伏、直線の長さ、有利な脚質、枠順の傾向などを踏まえて、プロの競馬ライター視点で約400文字から500文字の詳細なコース解説に増強してください。
元のテキストに含まれる【波乱指数】の部分は、数値を「一切変更せず」そのままの表記で出力の冒頭含めてください。
その他、ユーザーが読むだけでコースの特徴が生き生きと伝わるような文章にしてください。

＜元のテキスト＞
{content}
    """
    
    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        new_text = response.text.strip()
        
        if "波乱指数" in new_text:
            with open(filepath, "w", encoding="utf-8-sig") as f:
                f.write(new_text)
            count += 1
            print(f"  -> Success! Wrote {len(new_text)} chars.")
            time.sleep(4)
        else:
            print(f"  -> Failed: Target keyword missing in response. Response was: {new_text[:50]}...")
    except Exception as e:
        print(f"  -> Error API: {e}")
        time.sleep(10)

print(f"\nEnhancement complete! Enhanced {count} files.")
