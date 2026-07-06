"""
コース辞典PDF → 枠×使用コース(A/B/C)別成績の抽出
====================================================
スキャン書籍「コース辞典」の各ページを Gemini Vision で分類・抽出し、
「◯コース使用時の枠番別成績」表を waku_rail_bias.json にまとめる。

  - ページはスキャン順 = 書籍順なので、コースタイトルページ (例: 札幌芝1200m) を
    状態として保持し、後続の表ページに紐付ける
  - 進捗は waku_rail_raw.jsonl に追記 (再実行時は処理済みをスキップ)

【使い方】
  python extract_waku_rail.py            # 抽出 (再開可能)
  python extract_waku_rail.py --build    # raw から waku_rail_bias.json を組み立てのみ
【前提】 jra-web/.env に GEMINI_API_KEY / pip install pymupdf pillow google-generativeai
"""
import argparse
import glob
import io
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = r"C:\Users\owner\Downloads\vFlat\PDF\コース辞典"
RAW_PATH = os.path.join(BASE_DIR, "waku_rail_raw.jsonl")
OUT_PATH = os.path.join(BASE_DIR, "api", "data_files", "common", "waku_rail_bias.json")

VENUES = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"]

PROMPT = """
競馬書籍「コース辞典」のスキャンページです。以下のいずれかに分類し、JSONのみを出力してください。

1) コースのタイトルページ (「札幌芝1200m」のようなコース名の大見出し+コース図があるページ):
   {"page_type": "course_title", "venue": "札幌", "course": "芝1200"}
   ※course は「芝」or「ダート」+距離数字。外回り/内回りは「芝2000外」「芝2000内」のように付記

2) 「◯コース使用時の枠番別成績」という表があるページ:
   {"page_type": "waku_rail", "venue": "札幌",
    "tables": [
      {"rail": "A", "rows": [
        {"waku": "1-3", "win": 6.4, "show": 23.4},
        {"waku": "6-8", "win": 9.3, "show": 23.1}]},
      {"rail": "C", "rows": [...]}]}
   ※venue はページ右端のタブ (黒背景の会場名) や英字ヘッダーから判断
   ※rail は表タイトルの「Aコース使用時」の A/B/C
   ※waku は表の枠番グループ表記のまま ("1-3", "4-5", "6-8" 等。~ は - に)
   ※win=勝率, show=複勝率 (％記号を除いた数値)

3) どちらでもない (会場概要・血統解説・目次など):
   {"page_type": "other"}

JSON以外の文字は一切出力しないこと。
"""


def load_processed():
    done = {}
    if os.path.exists(RAW_PATH):
        with open(RAW_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    done[rec["file"]] = rec
                except json.JSONDecodeError:
                    continue
    return done


def extract(args):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    import fitz
    from PIL import Image
    import google.generativeai as genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[ERROR] GEMINI_API_KEY がありません")
        sys.exit(1)
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    files = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
    done = load_processed()
    todo = [p for p in files if os.path.basename(p) not in done]
    print(f"対象 {len(files)}ファイル / 処理済み {len(done)} / 残り {len(todo)}")

    rate_limit_hits = 0
    with open(RAW_PATH, "a", encoding="utf-8") as out:
        for i, path in enumerate(todo):
            name = os.path.basename(path)
            try:
                doc = fitz.open(path)
                page = doc.load_page(0)
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                doc.close()
                res = model.generate_content([PROMPT, image])
                text = res.text.strip().replace("```json", "").replace("```", "").strip()
                data = json.loads(text)
                rate_limit_hits = 0
            except Exception as e:
                msg = str(e)
                if "429" in msg or "quota" in msg.lower() or "exhaust" in msg.lower():
                    rate_limit_hits += 1
                    if rate_limit_hits >= 10:
                        print(f"\n[STOP] レート制限が{rate_limit_hits}回連続 — 日次クォータ切れの可能性。"
                              "時間を置いて再実行してください (処理済み分から再開します)")
                        return
                    print(f"  [{name}] レート制限 → 60秒待機して再試行 ({rate_limit_hits}/10)")
                    time.sleep(60)
                    todo.append(path)
                    continue
                data = {"page_type": "error", "error": msg[:200]}
            rec = {"file": name, **data}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            label = data.get("page_type")
            extra = data.get("course") or (f"{len(data.get('tables', []))}表" if label == "waku_rail" else "")
            print(f"  [{i+1}/{len(todo)}] {name}: {label} {data.get('venue', '')} {extra}")
            time.sleep(1.0)


def build():
    """raw jsonl (書籍順) → waku_rail_bias.json"""
    done = load_processed()
    files = sorted(done.keys())
    out = {}
    cur_venue, cur_course = None, None
    n_tables, n_orphan = 0, 0
    for name in files:
        rec = done[name]
        pt = rec.get("page_type")
        if pt == "course_title" and rec.get("venue") in VENUES and rec.get("course"):
            cur_venue, cur_course = rec["venue"], str(rec["course"]).replace("m", "")
        elif pt == "waku_rail":
            venue = rec.get("venue") if rec.get("venue") in VENUES else cur_venue
            # 直前のタイトルと会場が食い違う場合はタイトル側を破棄 (章跨ぎ)
            if venue != cur_venue or not cur_course:
                n_orphan += 1
                print(f"  [WARN] {name}: コース不明の表 (venue={venue}, 直前タイトル={cur_venue}{cur_course})")
                continue
            course_map = out.setdefault(venue, {}).setdefault(cur_course, {})
            for tbl in rec.get("tables", []):
                rail = str(tbl.get("rail", "")).strip().upper()
                if rail not in ("A", "B", "C", "D"):
                    continue
                rows = {}
                for row in tbl.get("rows", []):
                    waku = str(row.get("waku", "")).replace("~", "-").replace("〜", "-").strip()
                    try:
                        rows[waku] = {"win": float(row["win"]), "show": float(row["show"])}
                    except (KeyError, ValueError, TypeError):
                        continue
                if rows:
                    course_map[rail] = rows
                    n_tables += 1
    payload = {
        "_meta": {
            "source": "コース辞典 (スキャンPDF, Gemini抽出)",
            "note": "使用コース(A/B/C)別の枠番グループ成績 (win=勝率%, show=複勝率%)。"
                    "書籍集計値のためバックテスト不能 → scoring では控えめな重みで使用",
        },
    }
    payload.update(out)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    n_courses = sum(len(v) for k, v in out.items())
    print(f"\n[OK] {OUT_PATH}")
    print(f"  会場 {len(out)} / コース {n_courses} / 表 {n_tables} / 紐付け不能 {n_orphan}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="抽出せず raw から JSON 組み立てのみ")
    args = ap.parse_args()
    if not args.build:
        extract(args)
    build()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
