"""SPEC-T82: JRA馬場情報ページ / アーカイブPDFから使用コース(A/B/C/D)を取得し、
ローカルの `data/jra_logging.db` (meeting_course_usage) に蓄積する。

進行中の開催はアーカイブPDFが未公開のため、毎週 (統合サーバーの解析完了時・
起動時) に馬場情報ページから当週の使用コースを自前で記録しておく。開催終了後は
アーカイブPDFで日別の値を補完できる (--archive)。

【使い方】
  python fetch_course_usage.py                 # 現在の馬場情報ページ (index*.html)
                                                # から全場ぶんを取得・記録
  python fetch_course_usage.py --date 20260921  # (--dateは記録・ログ用の注記のみ。
                                                # 取得対象はJRA側が「現在」示す内容)
  python fetch_course_usage.py --archive 2026 nakayama 03
                                                # アーカイブPDFから日別の使用コースを
                                                # 週単位に畳んで記録 (既存週は上書きしない)

このモジュールは実際のHTTP取得・DB書き込みを行う「入口」で、解析ロジック自体は
`api/meeting_calendar.py` の純関数に委ねる。jra_ev.worker_analyze_all の完了時
フックと jra_suite.py の起動時フックは、いずれもこのモジュールの
`fetch_and_store_current_week_usage()` を呼ぶだけの薄いラッパーになっている。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import requests  # noqa: E402

from api import meeting_calendar as mc  # noqa: E402
from api.logging_store import LoggingStore  # noqa: E402

JST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; jra-web T82 course usage collector)"}
REQUEST_TIMEOUT = 10
SLEEP_SECONDS = 1.2  # netkeiba/JRAへの連続アクセスは1秒以上空ける (プロジェクト規約)


def fetch_baba_page(path, *, timeout=REQUEST_TIMEOUT):
    res = requests.get(mc.BABA_BASE_URL + path, headers=HEADERS, timeout=timeout)
    res.raise_for_status()
    res.encoding = "shift_jis"
    return res.text


def fetch_and_store_current_week_usage(store=None, *, sleep_seconds=SLEEP_SECONDS,
                                        fetch=fetch_baba_page):
    """https://www.jra.go.jp/keiba/baba/ の index.html が示すタブ一覧 (今週開催中の
    全場) を辿り、各場の使用コースを meeting_course_usage に記録する。

    index3.html 等、タブに載っていないページは前開催のキャッシュが残っていることが
    実データ確認で分かっている (SPEC-T82 §0) ため、index.html 自身のタブ一覧に
    載っているページだけを対象にする (index.html は必ずタブに自分自身を含む)。

    戻り値: {"saved": [...], "skipped": [...], "errors": [...]}"""
    store = store or LoggingStore()
    store.initialize()
    saved, skipped, errors = [], [], []

    try:
        index_html = fetch("index.html")
    except Exception as e:
        return {"saved": saved, "skipped": skipped, "errors": [f"index.html: {e}"]}

    parsed_index = mc.parse_baba_index_page(index_html)
    pages = [("index.html", parsed_index)]
    seen_hrefs = {"index.html"}
    for tab in parsed_index.get("tabs") or []:
        href = tab.get("href")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        time.sleep(sleep_seconds)
        try:
            html = fetch(href)
        except Exception as e:
            errors.append(f"{href}: {e}")
            continue
        pages.append((href, mc.parse_baba_index_page(html)))

    now_jst = datetime.now(JST)
    fetched_at = now_jst.isoformat()
    # SPEC-T82: 週の帰属先はページ内の日付ではなく取得実行時点の実日付で決める
    # (mc.current_course_week_start の docstring 参照)。
    week_start = mc.current_course_week_start(now_jst.date()).isoformat()
    for href, page in pages:
        venue = page.get("venue")
        page_date = page.get("date")
        if not venue or not page_date:
            errors.append(f"{href}: 開催情報を解析できませんでした")
            continue
        source_url = mc.BABA_BASE_URL + href
        try:
            is_new = store.save_course_usage(
                venue=venue, week_start=week_start, meeting_no=page.get("meeting_no"),
                course=page.get("course"), note=page.get("course_note"),
                source_url=source_url, fetched_at=fetched_at)
        except Exception as e:
            errors.append(f"{venue}: 保存失敗 {e}")
            continue
        record = {"venue": venue, "week_start": week_start, "course": page.get("course")}
        (saved if is_new else skipped).append(record)
    return {"saved": saved, "skipped": skipped, "errors": errors}


def _download_archive_pdf(year, venue_en, meeting_no, *, dest_dir=None):
    """T50が既にローカルに保存済みなら再利用し (data/t50/raw/<year>/)、無ければ
    アーカイブURLから取得してキャッシュする。"""
    local_dir = dest_dir or os.path.join(BASE_DIR, "data", "t50", "raw", str(year))
    local_path = os.path.join(local_dir, f"{venue_en}{int(meeting_no):02d}.pdf")
    url = mc.ARCHIVE_URL_TEMPLATE.format(year=year, venue_en=venue_en, meeting_no=int(meeting_no))
    if os.path.exists(local_path):
        return local_path, url
    os.makedirs(local_dir, exist_ok=True)
    res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    res.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(res.content)
    return local_path, url


def import_archive(year, venue_en, meeting_no, *, store=None, dest_dir=None):
    """アーカイブPDF (venue_en+meeting_no) から日別の使用コースを読み取り、週単位に
    畳んで meeting_course_usage に記録する (append-only。既存週は変更しない)。"""
    import fitz  # PyMuPDF (SPEC-T82: PDF解析は既存のfitzを使う)

    store = store or LoggingStore()
    store.initialize()
    venue_jp = next((jp for jp, en in mc.VENUE_EN.items() if en == venue_en), None)
    if venue_jp is None:
        raise ValueError(f"不明な venue_en: {venue_en}")

    local_path, source_url = _download_archive_pdf(year, venue_en, meeting_no, dest_dir=dest_dir)
    doc = fitz.open(local_path)
    text = "\n".join(page.get_text() for page in doc)
    rows = mc.parse_archive_pdf_text(text)

    fetched_at = datetime.now(JST).isoformat()
    saved, skipped = [], []
    for row in rows:
        if row.get("weekday") == "金":
            # 金曜は測定日 (pre-measurement) で実際の開催日ではないため週次記録には
            # 畳まない (直後の土日と同じ使用コースになるのが通例。T50のis_race_day=0と
            # 同じ扱い)。week_start_for_date で丸めると前週側の週バケツに入ってしまい、
            # 実在しない開催週の記録を作ってしまうのを避ける。
            continue
        week_start = mc.week_start_for_date(row["date"]).isoformat()
        note = f"アーカイブPDF ({row['date'].isoformat()} 時点) の使用コース"
        is_new = store.save_course_usage(
            venue=venue_jp, week_start=week_start, meeting_no=int(meeting_no),
            course=row.get("course"), note=note, source_url=source_url, fetched_at=fetched_at)
        record = {"venue": venue_jp, "week_start": week_start, "course": row.get("course"),
                  "date": row["date"].isoformat()}
        (saved if is_new else skipped).append(record)
    return {"saved": saved, "skipped": skipped, "source_url": source_url, "local_path": local_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None,
                        help="記録用の注記に使う対象日 (YYYYMMDD)。取得内容自体はJRA側が"
                             "「現在」示すものになる (指定しても取得対象は変わらない)")
    parser.add_argument("--archive", nargs=3, metavar=("YEAR", "VENUE_EN", "MEETING_NO"),
                        default=None,
                        help="アーカイブPDFから日別の使用コースを取り込む "
                             "(例: --archive 2026 nakayama 03)")
    args = parser.parse_args()

    if args.archive:
        year, venue_en, meeting_no = args.archive
        result = import_archive(int(year), venue_en, int(meeting_no))
        print(f"[archive] 保存 {len(result['saved'])}件 / 既存 {len(result['skipped'])}件"
              f" ({result['source_url']})")
        for r in result["saved"]:
            print(f"  + {r['venue']} {r['week_start']} {r['course']} (from {r['date']})")
        return 0

    if args.date:
        print(f"[note] 対象日指定: {args.date} (取得内容はJRA側の「現在」の値)")
    result = fetch_and_store_current_week_usage()
    print(f"保存 {len(result['saved'])}件 / 既存 {len(result['skipped'])}件"
          f" / エラー {len(result['errors'])}件")
    for r in result["saved"]:
        print(f"  + {r['venue']} {r['week_start']} {r['course']}")
    for e in result["errors"]:
        print(f"  ! {e}")
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
