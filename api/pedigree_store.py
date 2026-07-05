"""
血統データの蓄積 (将来のML血統特徴量用)
==========================================
ability.db (TARGET由来) には父・母父が無く、血統をML特徴量にできないのが
現状最大のデータ制約。出馬表スクレイプのたびに (馬名, 父, 母父, 母) を
Neon の horse_pedigree テーブルへ upsert して蓄積する。

- 呼び出し: api/index.py analyze_race_url (ベストエフォート・失敗しても解析継続)
           collect_pedigree.py (週末全カード一括収集)
- 蓄積が学習期間の出走馬を十分カバーした時点で、backtest_ml の特徴量に
  父×コース適性 (db-keiba factors の father_w と照合) 等を追加できる。
"""
import os
import sys
from datetime import date

_API_DIR = os.path.dirname(os.path.abspath(__file__))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)


def _get_conn():
    import past_data_service as pds
    conn = pds.get_db_connection(_API_DIR)
    if conn is None or not getattr(conn, "is_pg", False):
        return None
    return conn


def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS horse_pedigree (
            horse      TEXT PRIMARY KEY,
            sire       TEXT,
            bms        TEXT,
            dam        TEXT,
            updated_at DATE
        )
    """)
    conn._conn.commit()


def upsert_horses(horses):
    """horses: dictリスト (name/sire/bms/dam キーを持つ)。
    有効な血統情報がある馬のみ upsert。戻り値: 登録件数 (接続不可なら -1)。"""
    rows = []
    today = date.today()
    for h in horses:
        name = str(h.get("name") or "").strip()
        sire = str(h.get("sire") or "").strip()
        bms = str(h.get("bms") or "").strip()
        dam = str(h.get("dam") or "").strip()
        if not name or name == "-" or (not sire or sire == "-"):
            continue
        rows.append((name, sire, bms if bms != "-" else "",
                     dam if dam != "-" else "", today))
    if not rows:
        return 0

    conn = _get_conn()
    if conn is None:
        return -1
    try:
        ensure_table(conn)
        cur = conn.cursor()
        cur.executemany("""
            INSERT INTO horse_pedigree (horse, sire, bms, dam, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (horse) DO UPDATE SET
                sire = EXCLUDED.sire, bms = EXCLUDED.bms,
                dam = EXCLUDED.dam, updated_at = EXCLUDED.updated_at
        """, rows)
        conn._conn.commit()
        return len(rows)
    finally:
        conn.close()


def coverage_stats():
    """蓄積状況: (登録馬数, 最終更新日)。接続不可なら None。"""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS latest FROM horse_pedigree")
        row = cur.fetchone()
        return (row["n"], str(row["latest"])) if row else None
    except Exception:
        return None
    finally:
        conn.close()
