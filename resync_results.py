"""SPEC-T84b: 部分取り込み (上位数頭で着順が止まった) レース結果の再同期 CLI。

T84 Stage A/B で確定した真因: 発走16分後の時点では約5%のレースでJRAの全着順が
まだ上位5頭しか出ておらず、結果は一度しか取得しないのでその5%が永久に部分
データのまま race_results に残ってしまう。このスクリプトは `race_results` を
読み (`find_incomplete_results`)、保存済みの `source_url` を1回だけ取り直して
行を補う (`resync_incomplete_results`) 入口。決定ロジック・通知・仮想購入には
一切触れない (SPEC-T84b §5)。

【使い方】
  python -X utf8 resync_results.py --dry-run    # 対象一覧を表示するだけ (取得しない)
  python -X utf8 resync_results.py --limit 50   # 古い順に最大50件まで再同期 (既定)
  python -X utf8 resync_results.py --db path\\to.db  # 対象DBを明示 (既定: data/jra_logging.db)

リクエストは1件ごとに最低1.5秒間隔を空ける (netkeiba/JRAへの連続アクセスは
1秒以上空けるプロジェクト規約。resync_result自身がリクエスト前にスリープする)。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from api.result_service import find_incomplete_results, resync_incomplete_results  # noqa: E402

DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "jra_logging.db")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-sync partially captured JRA race results (SPEC-T84b)")
    parser.add_argument("--limit", type=int, default=50, help="再同期する最大件数 (古い順、既定50)")
    parser.add_argument("--dry-run", action="store_true", help="対象一覧を表示するだけで取得しない")
    parser.add_argument("--db", default=None, help="対象DBのパス (省略時は data/jra_logging.db)")
    parser.add_argument("--max-age-days", type=int, default=60,
                        help="何日前までのレースを対象にするか (既定60。過去分の穴埋めでは広げる)")
    args = parser.parse_args()

    db_path = args.db or DEFAULT_DB_PATH

    if args.dry_run:
        targets = find_incomplete_results(db_path, max_age_days=args.max_age_days)
        targets = sorted(targets, key=lambda item: item["result_fetched_at"] or "")
        print(f"対象 {len(targets)} 件 (--limit {args.limit} 件まで実行予定。--dry-run のため取得はしません)")
        for item in targets[:args.limit]:
            print(f"  {item['race_id']}: rows={item['rows']} field_size={item['field_size']} "
                  f"fetched_at={item['result_fetched_at']}")
        return 0

    result = resync_incomplete_results(db_path, limit=args.limit, max_age_days=args.max_age_days)
    print(f"targets={result['targets']} resynced={result['resynced']} "
          f"no_change={result['no_change']} failed={result['failed']}")
    for item in result["details"]:
        rows = f"{item.get('rows_before')}->{item.get('rows_after')}" if "rows_after" in item else ""
        error = f" error={item['error']}" if item.get("error") else ""
        print(f"  [{item['status']}] {item['race_id']} {rows}{error}")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
