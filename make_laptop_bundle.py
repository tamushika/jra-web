"""ノート PC へ持ち出す「git に入っていないが実行に要るファイル」を 1 つの zip にまとめる。

コード自体は GitHub にあるので、このスクリプトが集めるのは .gitignore 済みの
成果物・キャッシュ・(任意で) 大容量 DB だけ。手順は docs/laptop-setup.md。

    python -X utf8 make_laptop_bundle.py --list              # 何を入れるか確認だけ
    python -X utf8 make_laptop_bundle.py                     # 既定 (小さい成果物のみ)
    python -X utf8 make_laptop_bundle.py --with-db           # 巨大DBも同梱 (約1.2GB)
    python -X utf8 make_laptop_bundle.py --include-env       # .env も同梱 (秘密情報)

出力先の既定は outputs/laptop_bundle_<YYYYMMDD>.zip。リポジトリ内の他のファイルは
一切変更しない。ネットワークアクセスもしない。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# (リポジトリ相対パス, 説明, 再生成手段)
CORE_FILES = [
    ("api/data_files/common/factor_snapshot.json",
     "as-of コース統計 (T45 で本番採用)。無いと採点が CSV 統計にフォールバックし結果が変わる",
     "retrain_all.bat (要 ability.db)"),
    ("web_place_model.json",
     "Web 複勝率β (T16/T36)",
     "backtest_place_model.py (要 ability.db)"),
    ("pedigree_cache.json",
     "血統キャッシュ (sire_pts・父/母父表示)",
     "collect_pedigree.py / Neon から再生成"),
    ("data/graded_cache.sqlite",
     "重賞データタブの表示用キャッシュ (T79)",
     "build_graded_cache.py (要 ability.db・約5秒)"),
]

BIG_FILES = [
    ("ability.db",
     "学習用 1986-2026 (バックテスト・再学習・重賞キャッシュ生成に必要)",
     "再取得は現実的でない。必ずコピーする"),
    ("api/past_data_v2.db",
     "過去データ分析のローカル版。DATABASE_URL (Neon) があれば不要",
     "Neon で代替可"),
    ("data/jra_logging.db",
     "予測・オッズ・通知・仮想運用の履歴 (実績ダッシュボードの中身)",
     "コピーしない場合ノート PC では空から始まる"),
]

ENV_FILE = (".env", "DATABASE_URL / LINE トークン", "docs/laptop-setup.md 参照")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024.0
    return f"{size:.1f}GB"


def collect(with_db: bool, include_env: bool):
    items = list(CORE_FILES)
    if with_db:
        items += BIG_FILES
    if include_env:
        items.append(ENV_FILE)
    found, missing = [], []
    for rel, why, regen in items:
        path = BASE_DIR / rel
        (found if path.exists() else missing).append((rel, why, regen, path))
    return found, missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with-db", action="store_true",
                        help="ability.db / past_data_v2.db / jra_logging.db も同梱 (約1.2GB)")
    parser.add_argument("--include-env", action="store_true",
                        help=".env (DATABASE_URL・LINEトークン) も同梱する。取り扱い注意")
    parser.add_argument("--list", action="store_true", help="作成せず内容だけ表示")
    parser.add_argument("--out", default=None, help="出力 zip のパス")
    args = parser.parse_args(argv)

    found, missing = collect(args.with_db, args.include_env)

    print("=== 同梱するファイル ===")
    total = 0
    for rel, why, _regen, path in found:
        size = path.stat().st_size
        total += size
        print(f"  {rel:<46} {human(size):>10}  {why}")
    if missing:
        print("\n=== 見つからない (この PC に無い) ===")
        for rel, why, regen, _ in missing:
            print(f"  {rel:<46} {'':>10}  {why} / 再生成: {regen}")
    print(f"\n合計 {human(total)} / {len(found)} ファイル")

    if not args.include_env:
        print("\n注意: .env は同梱していません。ノート PC では .env.example をコピーして"
              "値を手で入れるか、--include-env で同梱してください (秘密情報)。")
    else:
        print("\n警告: .env を同梱します。DATABASE_URL と LINE トークンが zip に入ります。"
              "共有ストレージや公開場所に置かないでください。")

    if args.list:
        return 0
    if not found:
        print("同梱できるファイルがありません。", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else (
        BASE_DIR / "outputs" / f"laptop_bundle_{datetime.now():%Y%m%d}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"既に存在します (上書きしません): {out}", file=sys.stderr)
        return 1

    manifest = [f"# jra-web laptop bundle {datetime.now():%Y-%m-%d %H:%M}",
                "# 展開先: クローンした jra-web ディレクトリ直下 (同じ相対パスに戻す)", ""]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, _why, _regen, path in found:
            zf.write(path, rel)
            manifest.append(f"{rel}\t{path.stat().st_size}\t{sha256_file(path)}")
            print(f"  + {rel}")
        zf.writestr("BUNDLE_MANIFEST.txt", "\n".join(manifest) + "\n")

    print(f"\n作成: {out}  ({human(out.stat().st_size)})")
    print("展開: ノート PC のクローン先 jra-web/ 直下で unzip (同じ相対パスに戻る)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
