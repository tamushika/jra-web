"""
ability.db を Neon から延伸する (TARGET不要の再学習データパス)
================================================================
ability.db (1980.csv由来, 〜2025-12-28で凍結) に、Neonに毎週蓄積される
馬名つきレース結果 (2026年〜) を追記する。これによりMLモデルの再学習・
バックテストが TARGET のエクスポートなしで継続できる。

  - 対象: Neonの 馬名 IS NOT NULL 行のうち ability.db の最終日より後
  - PCI は 走破タイム/上がり3F/距離 から厳密式で計算 (相関1.0000で検証済み)
  - 冪等: 対象期間を DELETE してから INSERT (再実行安全)

【使い方】 python extend_ability_from_neon.py
【前提】  C:\\project\\.env に DATABASE_URL (Neon)
"""
import argparse
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
sys.path.insert(0, API_DIR)

import scoring  # noqa: E402
import past_data_service as pds  # noqa: E402
from build_ability_db import parse_race_class  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "ability.db")
RUN_COLS = [
    "date", "place", "r", "race_name", "race_class", "horse", "sex", "age",
    "jockey", "kinryo", "total_horses", "umaban", "popularity", "rank",
    "track_type", "distance", "condition", "time_sec", "chakusa", "c4",
    "agari", "pci", "weight", "affi", "win_pay",
]


def compute_pci(time_sec, agari, dist):
    """PCI = 100×(道中600m換算ペース)/上がり3F − 50 (実データで相関1.0000を確認済み)"""
    if not time_sec or not agari or not dist or dist <= 600 or agari <= 0:
        return None
    try:
        return round(100.0 * ((time_sec - agari) / (dist - 600)) / (agari / 600.0) - 50.0, 1)
    except ZeroDivisionError:
        return None


def to_float(v):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def restore_supplemental_fields(cursor, saved_fukusho, saved_kinryo):
    """Restore local backfills after the Neon delete/reinsert sync.

    Neon remains authoritative when it has a weight.  T51's local netkeiba
    value is restored only when the reinserted Neon row still has NULL kinryo.
    """
    cursor.executemany(
        "UPDATE runs SET fukusho_pay = ? "
        "WHERE date = ? AND place = ? AND r = ? AND umaban = ?",
        saved_fukusho,
    )
    cursor.executemany(
        "UPDATE runs SET kinryo = ? "
        "WHERE date = ? AND place = ? AND r = ? AND umaban = ? "
        "AND kinryo IS NULL",
        saved_kinryo,
    )


def insert_new_rows_only(cursor, batch, columns=RUN_COLS):
    """T45 safe sync: preserve every existing row and insert unseen keys only."""
    key_positions = tuple(columns.index(name) for name in ("date", "place", "r", "umaban"))
    incoming_keys = [tuple(row[index] for index in key_positions) for row in batch]
    if len(incoming_keys) != len(set(incoming_keys)):
        raise RuntimeError("Neon batch contains duplicate natural keys")
    existing = set(cursor.execute(
        "SELECT date, place, r, umaban FROM runs WHERE date > ?",
        ("20251228",),
    ))
    additions = [row for row, key in zip(batch, incoming_keys) if key not in existing]
    cursor.executemany(
        f"INSERT INTO runs ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
        additions,
    )
    return len(additions), len(batch) - len(additions)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-safe-sync", action="store_true",
        help="T45専用: 既存行を変更・削除せず、新しい自然キーだけ追加",
    )
    args = parser.parse_args(argv)
    # 凍結境界: 1980.csv (TARGET) 由来データの最終日。これ以降は常にNeonから再同期する
    FROZEN_END = "20251228"
    conn_sq = sqlite3.connect(DB_PATH)
    cur_sq = conn_sq.cursor()
    cur_sq.execute("SELECT MAX(date) FROM runs")
    print(f"ability.db 現在の最終日: {cur_sq.fetchone()[0]} (凍結境界 {FROZEN_END} 以降を再同期)")
    cutoff_neon = FROZEN_END[2:]  # Neonは YYMMDD

    conn_pg = pds.get_db_connection(API_DIR)
    if conn_pg is None or not getattr(conn_pg, "is_pg", False):
        print("[ERROR] Neon未接続 (DATABASE_URL を確認)")
        sys.exit(1)
    cur_pg = conn_pg.cursor()
    cur_pg.execute('''
        SELECT date, place, race_num, race_name, "馬名" AS horse, sex_age,
               jockey, "斤量" AS kinryo, total_horses, horse_number, popularity,
               rank, track_type, distance, condition, time, corner_4, agari_3f,
               weight, "所属" AS affi, odds, horse_odds
        FROM races
        WHERE "馬名" IS NOT NULL AND date > %s AND race_name NOT LIKE %s
    ''', (cutoff_neon, "%障%"))
    rows = cur_pg.fetchall()
    conn_pg.close()
    print(f"Neonから取得: {len(rows)}行 (date > {cutoff_neon}, 平地のみ)")
    if not rows:
        print("追記対象なし")
        return

    batch = []
    n_pci = 0
    for r in rows:
        d = dict(r)
        rank = to_float(d.get("rank"))
        if rank is None or rank >= 90:  # 取消/除外
            continue
        track = "ダート" if d.get("track_type") in ("ダ", "ダート") else "芝"
        dist = int(to_float(d.get("distance")) or 0)
        if not dist:
            continue
        date8 = "20" + str(d["date"])
        cond = str(d.get("condition") or "")[:1]
        tsec = scoring.parse_run_time(d.get("time") or "")
        agari = to_float(d.get("agari_3f"))
        pci = compute_pci(tsec, agari, dist)
        if pci is not None:
            n_pci += 1
        sex_age = str(d.get("sex_age") or "")
        sex = sex_age[:1] if sex_age[:1] in ("牡", "牝", "セ") else ""
        age_m = re.search(r"(\d+)", sex_age)
        c4_m = re.search(r"\d+", str(d.get("corner_4") or ""))
        pop_m = re.search(r"\d+", str(d.get("popularity") or ""))
        # win_pay: TARGET互換形式。勝ち馬=払戻円 '260' / 他馬=確定オッズ '(3.7)'
        # (horse_odds は backfill_odds_netkeiba.py / 将来のupdater改修で充填)
        if int(rank) == 1:
            win_pay = str(d.get("odds") or "")
        else:
            ho = to_float(d.get("horse_odds"))
            win_pay = f"({ho:g})" if ho else ""
        batch.append((
            date8, d.get("place"), int(d["race_num"]) if d.get("race_num") else None,
            d.get("race_name") or "", parse_race_class(d.get("race_name") or ""),
            (d.get("horse") or "").strip(), sex,
            int(age_m.group(1)) if age_m else None,
            (d.get("jockey") or "").strip(), to_float(d.get("kinryo")),
            int(to_float(d.get("total_horses")) or 0) or None,
            int(to_float(d.get("horse_number")) or 0) or None,
            int(pop_m.group()) if pop_m else None, int(rank),
            track, dist, cond,
            tsec, None,  # chakusa は取得不能
            int(c4_m.group()) if c4_m else None,
            agari, pci,
            int(to_float(d.get("weight")) or 0) or None,
            (d.get("affi") or "").strip(), win_pay,
        ))

    # 別スクリプトが埋め戻す列はDELETE/再INSERTで消えるため退避して復元する。
    # kinryoはNeon側がNULLの場合だけT51補完値を戻し、Neonの非NULL値を優先する。
    COLS = RUN_COLS
    if args.candidate_safe_sync:
        added, retained = insert_new_rows_only(cur_sq, batch, COLS)
        conn_sq.commit()
        cur_sq.execute("SELECT MAX(date), COUNT(*) FROM runs")
        new_max, total = cur_sq.fetchone()
        conn_sq.close()
        print(f"candidate-safe同期: 追加{added}行 / 既存維持{retained}行 / 削除・更新0")
        print(f"ability.db: 計{total}行, 最終日 {new_max}")
        return
    saved_fukusho = list(cur_sq.execute(
        "SELECT fukusho_pay, date, place, r, umaban FROM runs "
        "WHERE date > ? AND fukusho_pay IS NOT NULL AND fukusho_pay != ''",
        ("20" + cutoff_neon,)))
    saved_kinryo = list(cur_sq.execute(
        "SELECT kinryo, date, place, r, umaban FROM runs "
        "WHERE date > ? AND kinryo IS NOT NULL",
        ("20" + cutoff_neon,)))

    # 冪等: 対象期間を消してから挿入 (再実行時は前回追記分を置き換え)
    cur_sq.execute("DELETE FROM runs WHERE date > ?", ("20" + cutoff_neon,))
    deleted = cur_sq.rowcount
    cur_sq.executemany(
        f"INSERT INTO runs ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", batch)
    restore_supplemental_fields(cur_sq, saved_fukusho, saved_kinryo)
    conn_sq.commit()
    print(f"fukusho_pay 復元: {len(saved_fukusho)}行")
    print(f"kinryo 保護候補: {len(saved_kinryo)}行 (Neon NULL行だけ復元)")

    cur_sq.execute("SELECT MAX(date), COUNT(*) FROM runs")
    new_max, total = cur_sq.fetchone()
    conn_sq.close()
    n_odds = sum(1 for b in batch if b[-1])
    print(f"追記: {len(batch)}行 (置換削除 {deleted}行, PCI計算済み {n_pci}行)")
    print(f"win_pay(オッズ)充填: {n_odds}/{len(batch)}行 ({100.0 * n_odds / len(batch):.0f}%)")
    if n_odds < len(batch) * 0.9:
        print("[WARN] オッズ欠損が多い → 先に python backfill_odds_netkeiba.py を実行してください"
              " (欠損のままML再学習すると ln_odds 特徴量が壊れます)")
    print(f"ability.db: 計{total}行, 最終日 {new_max}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
