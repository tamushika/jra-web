"""SPEC-T70: virtual betting harness (paper trading, not real purchases).

配管は実運用と同一 (締切前自動決定 → 記録 → 精算) の prospective 計測装置だが、
これは **購入推奨・実購入ではない**。仮想成績が実運用移行ゲート (SPEC-T70 §5、
上位モデルのみが判定) を通過した戦略だけが実マネー段階 (P4、別SPEC) に進む。
LINE/Discord通知条件 (5分前×EV≥1.3等) はこのモジュールでは一切変更しない
(jra_ev.py の通知関数3つはテストでAST不変を検証している。tests/test_t70_*.py)。

ポリシー v1 (2026-08-19 事前登録・数値を見る前に固定。SPEC-T70 §1):
  - 対象: T62b選定レース (race_confidence_snapshots.selected=1 かつ凍結manifest
    SHA一致) のみ。日2件前後の想定
  - 買い目: 選定レースの30分前cutoffにおけるCL勝率1位馬 (model_probs_json 最大値。
    同率は馬番小) の複勝
  - 賭け金: 1ベット¥2,000固定。日上限¥10,000 (本体ベットの合計stakeで判定。
    6件目以降の選定は本体をベットせず status='skipped_budget' として記録する。
    予算は上限であり全額消化を目標としない)
  - 対照群 (無料・予算外): 同じ選定レースの1番人気 (cutoffオッズ最小。同オッズは
    馬番小) の複勝¥2,000。is_control=1。予算判定の対象外・実運用対象外
  - 単勝EV枠 (v2, 予約): T63裁定で新閾値が確定するまでは0円 (未実装)

これらのパラメータ (¥2,000/¥10,000/複勝/1位馬の定義) を変更する場合は、
POLICY_VERSION を更新し、T39台帳に記録すること (SPEC-T70 §6)。

このモジュールは決定・精算ロジックの「本体」を持つ (SPEC-T70 §6: 変更してよい
ファイル一覧)。api/logging_store.py 側の変更は virtual_bets テーブル追加のみに
とどめ、読み書きはここに閉じる。Vercel関数化を防ぐため .vercelignore に
api/virtual_betting.py を追記済み (T69の教訓)。ローカルsuite専用、
api/index.py には一切組み込まない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .logging_store import DEFAULT_DB_PATH, LoggingStore, utc_now
    from . import race_confidence
except ImportError:  # direct execution from api/
    from logging_store import DEFAULT_DB_PATH, LoggingStore, utc_now
    import race_confidence


# ─── ポリシー v1 定数 (変更にはPOLICY_VERSION更新+台帳記録が必要) ─────────────
POLICY_VERSION = "v1"
BET_TYPE_MAIN = "fukusho"
STAKE_YEN = 2000
DAILY_BUDGET_YEN = 10000

DISCLAIMER_JA = "仮想運用であり実購入・購入推奨ではありません"

# 決定 (decide) の結果に混入してはならない結果由来フィールド。schema CHECK と
# 二重に、コード側でも decided 時点での混入を拒否する (T62b save_race_confidence
# と同じ設計)。
_FORBIDDEN_DECISION_FIELDS = ("payout_yen", "settled_at")


# ─── 純関数: 決定 (snapshot行 → ベット行) ────────────────────────────────────

def _parse_number_map(raw: Any) -> dict[int, float]:
    """{"1": 0.12, ...} 形式のJSON文字列 or dict を {馬番(int): 値(float)} へ。"""
    if raw is None:
        return {}
    data = json.loads(raw) if isinstance(raw, str) else raw
    out: dict[int, float] = {}
    for key, value in (data or {}).items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def select_top_probability_horse(model_probs: Mapping[Any, Any]) -> int | None:
    """CL勝率1位馬の馬番を返す (同率は馬番小)。"""
    best_num, best_prob = None, None
    for key, value in model_probs.items():
        try:
            num, prob = int(key), float(value)
        except (TypeError, ValueError):
            continue
        if best_num is None or prob > best_prob or (prob == best_prob and num < best_num):
            best_num, best_prob = num, prob
    return best_num


def select_favorite_horse(market_odds: Mapping[Any, Any]) -> int | None:
    """cutoffオッズ最小 (=1番人気) の馬番を返す (同オッズは馬番小)。"""
    best_num, best_odds = None, None
    for key, value in market_odds.items():
        try:
            num, odds = int(key), float(value)
        except (TypeError, ValueError):
            continue
        if best_num is None or odds < best_odds or (odds == best_odds and num < best_num):
            best_num, best_odds = num, odds
    return best_num


def build_bet_decisions(snapshot: Mapping[str, Any], *, main_budget_used_yen: int) -> list[dict[str, Any]]:
    """純関数: 1件の race_confidence_snapshots 行 → 0/2件の virtual_bets 行案。

    snapshot はDBの行そのもの (dict化したsqlite3.Row等) を想定する。参照する列は
    決定に使ったas-of値のみ (date/place/r/cutoff_at/model_probs_json/
    market_odds_json/selected/manifest_sha256/race_confidence_snapshot_id)。
    結果情報は一切参照しない。DB I/Oはしない (呼び出し側 record_decision が
    冪等に永続化する)。

    非選定・凍結manifest SHA不一致・model_probs/market_odds欠損の場合は
    空リストを返す (ベットしない)。予算超過時は本体行の stake_yen=0・
    status='skipped_budget' (対照は予算外なので常に stake_yen=STAKE_YEN)。
    """
    if int(snapshot.get("selected") or 0) != 1:
        return []
    if snapshot.get("manifest_sha256") != race_confidence.EXPECTED_MANIFEST_SHA256:
        return []
    model_probs = _parse_number_map(snapshot.get("model_probs_json"))
    market_odds = _parse_number_map(snapshot.get("market_odds_json"))
    if not model_probs or not market_odds:
        return []
    main_num = select_top_probability_horse(model_probs)
    control_num = select_favorite_horse(market_odds)
    if main_num is None or control_num is None:
        return []

    date = str(snapshot["date"])
    place = str(snapshot["place"])
    race_no = int(snapshot["r"])
    race_id = f"{date}:{place}:{race_no:02d}"
    decided_at = snapshot.get("cutoff_at") or utc_now()
    snapshot_id = snapshot.get("race_confidence_snapshot_id")
    cutoff_source = f"race_confidence_snapshot:{snapshot_id}" if snapshot_id is not None \
        else "race_confidence_snapshot:unknown"
    within_budget = main_budget_used_yen + STAKE_YEN <= DAILY_BUDGET_YEN

    def _row(*, horse_number, stake_yen, is_control, status, odds_map, prob_map, suffix):
        return {
            "idempotency_key": f"{POLICY_VERSION}:{race_id}:{BET_TYPE_MAIN}:{suffix}",
            "policy_version": POLICY_VERSION, "race_id": race_id, "date": date,
            "bet_type": BET_TYPE_MAIN, "horse_number": horse_number, "stake_yen": stake_yen,
            "decided_at": decided_at, "cutoff_source": cutoff_source,
            "decision_odds": odds_map.get(horse_number), "decision_prob": prob_map.get(horse_number),
            "is_control": int(is_control), "status": status,
            "payout_yen": None, "settled_at": None,
        }

    return [
        _row(horse_number=main_num, stake_yen=STAKE_YEN if within_budget else 0, is_control=False,
             status="pending" if within_budget else "skipped_budget",
             odds_map=market_odds, prob_map=model_probs, suffix="main"),
        _row(horse_number=control_num, stake_yen=STAKE_YEN, is_control=True, status="pending",
             odds_map=market_odds, prob_map=model_probs, suffix="control"),
    ]


# ─── 純関数: 精算 (race_results → payout) ───────────────────────────────────

def compute_settlement(bet: Mapping[str, Any], result_row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """純関数: pendingなvirtual_bets 1行 + race_resultsの該当馬1行 (無ければNone)
    → 更新後 status/payout_yen。未確定 (結果未取得・複勝払戻未取得) はNoneを返し
    据え置く (pendingのまま。呼び出し側は次回精算で再試行する)。

    取消・除外馬 (finish_position が非数値 = None) は返還 (refunded・
    payout_yen=stake_yen)。着順3着以内なら公式複勝払戻 (100円あたり) から
    payout = stake/100*place_payout。4着以下は的中なし (settled・payout=0)。
    パリミュチュエル自票影響は無視 (SPEC-T70 §2、¥2,000規模)。
    """
    if result_row is None:
        return None
    official_status = result_row.get("official_status")
    if official_status not in ("official", "corrected"):
        return None  # 'unofficial' はまだ確定していない
    finish_position = result_row.get("finish_position")
    if finish_position is None:
        return {"status": "refunded", "payout_yen": int(bet["stake_yen"])}
    if bet.get("bet_type") != "fukusho":
        raise ValueError("policy v1 only settles fukusho (place) bets")
    if int(finish_position) > 3:
        return {"status": "settled", "payout_yen": 0}
    place_payout = result_row.get("place_payout")
    if place_payout is None:
        return None  # 3着以内だが複勝払戻が未取得 → 保留、次回精算で再試行
    payout = int(round(bet["stake_yen"] / 100.0 * place_payout))
    return {"status": "settled", "payout_yen": payout}


# ─── I/O: DB接続 (logging_store.pyの内部実装には依存しない自前の薄い接続) ────

def _db_path(db_path: str | Path | None) -> Path:
    return Path(db_path) if db_path else DEFAULT_DB_PATH


def _connect(db_path: str | Path | None) -> sqlite3.Connection:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=1.5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=1500")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _write(db_path: str | Path | None, callback, *, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            with _connect(db_path) as conn:
                return callback(conn)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= retries:
                raise
            time.sleep(0.05 * (attempt + 1))


# ─── I/O: 決定の薄いエントリ (suiteフック用) ────────────────────────────────

def record_decision(snapshot: Mapping[str, Any], *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """決定を冪等に永続化する薄いI/Oエントリ。純関数 build_bet_decisions を使い、
    その日 (snapshot["date"]) に既に積み上がった本体stakeをDBから読んで予算判定
    してから、virtual_bets へ INSERT OR IGNORE する (idempotency_keyがrace_id×
    bet_type×本体/対照で決まるため、同一レースへの複数回呼び出しは安全)。"""
    LoggingStore(db_path).initialize()
    date = str(snapshot.get("date") or "")

    def op(conn: sqlite3.Connection):
        used = conn.execute(
            "SELECT COALESCE(SUM(stake_yen),0) FROM virtual_bets "
            "WHERE date=? AND is_control=0 AND policy_version=?",
            (date, POLICY_VERSION),
        ).fetchone()[0]
        rows = build_bet_decisions(snapshot, main_budget_used_yen=int(used or 0))
        inserted = []
        for row in rows:
            if row.get("payout_yen") is not None or row.get("settled_at") is not None:
                # decided時点でpayout/settled_atが埋まっているのはバグ (T70 §3)。
                raise ValueError(
                    f"decision rows must not carry settlement fields: "
                    f"{[k for k in _FORBIDDEN_DECISION_FIELDS if row.get(k) is not None]}")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO virtual_bets
                   (idempotency_key,policy_version,race_id,date,bet_type,horse_number,
                    stake_yen,decided_at,cutoff_source,decision_odds,decision_prob,
                    is_control,status,payout_yen,settled_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["idempotency_key"], row["policy_version"], row["race_id"], row["date"],
                 row["bet_type"], row["horse_number"], row["stake_yen"], row["decided_at"],
                 row["cutoff_source"], row["decision_odds"], row["decision_prob"],
                 row["is_control"], row["status"], None, None, utc_now()),
            )
            if cursor.rowcount:
                inserted.append(row)
        return inserted

    return _write(db_path, op)


def record_decision_for_snapshot_id(snapshot_id: int | None, *, db_path: str | Path | None = None
                                    ) -> list[dict[str, Any]]:
    """suiteフック用のエントリ。T62bスナップショット書込 (LoggingStore.
    save_race_confidence が返す race_confidence_snapshot_id) の直後に呼ぶ。
    保存済みの当該行をそのまま読み戻して record_decision に渡す (呼び出し側で
    行の形を組み立て直す必要がなく、スキーマとの乖離が起きない)。"""
    if snapshot_id is None:
        return []
    LoggingStore(db_path).initialize()

    def read(conn: sqlite3.Connection):
        row = conn.execute(
            "SELECT * FROM race_confidence_snapshots WHERE race_confidence_snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    with _connect(db_path) as conn:
        snapshot = read(conn)
    if snapshot is None:
        return []
    return record_decision(snapshot, db_path=db_path)


# ─── I/O: 精算の薄いエントリ ─────────────────────────────────────────────────

def settle_pending_bets(*, date: str | None = None, db_path: str | Path | None = None) -> dict[str, int]:
    """既存の結果同期 (result_service.sync_results_for_date) 完了後、または
    perfダッシュボード表示時のlazy精算として呼ぶ冪等な精算エントリ (SPEC-T70
    §4-2、実装が単純な方=lazy精算を採用)。書き込みは virtual_bets の UPDATEの
    みで race_results は読み取り専用。status='pending' の行のみ対象にし、
    UPDATEもWHERE status='pending'を維持するため、再実行しても二重精算しない。"""
    LoggingStore(db_path).initialize()

    def op(conn: sqlite3.Connection):
        sql = "SELECT * FROM virtual_bets WHERE status='pending'"
        params: tuple = ()
        if date:
            sql += " AND date=?"
            params = (date,)
        pending = [dict(row) for row in conn.execute(sql, params).fetchall()]
        settled = refunded = still_pending = 0
        for bet in pending:
            race_id = bet["race_id"]
            horse_id = f"{race_id}:{int(bet['horse_number']):02d}"
            result = conn.execute(
                "SELECT finish_position, official_status, place_payout FROM race_results "
                "WHERE race_id=? AND horse_id=?", (race_id, horse_id),
            ).fetchone()
            outcome = compute_settlement(bet, dict(result) if result is not None else None)
            if outcome is None:
                still_pending += 1
                continue
            updated = conn.execute(
                "UPDATE virtual_bets SET status=?, payout_yen=?, settled_at=? "
                "WHERE virtual_bet_id=? AND status='pending'",
                (outcome["status"], outcome["payout_yen"], utc_now(), bet["virtual_bet_id"]),
            )
            if updated.rowcount:
                if outcome["status"] == "settled":
                    settled += 1
                else:
                    refunded += 1
        return {"settled": settled, "refunded": refunded, "still_pending": still_pending}

    return _write(db_path, op)


# ─── I/O: 表示用の読み取り専用ロード + 純関数の集計 (perfダッシュボード用) ──

def load_bets(*, date: str | None = None, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """virtual_bets を読み取り専用で取得する (書き込みなし)。"""
    LoggingStore(db_path).initialize()
    path = _db_path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM virtual_bets"
        params: tuple = ()
        if date:
            sql += " WHERE date=?"
            params = (date,)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _empty_bucket() -> dict[str, int]:
    return {"n_bets": 0, "n_settled": 0, "n_refunded": 0, "n_pending": 0,
            "n_skipped_budget": 0, "stake_yen": 0, "payout_yen": 0}


def _accumulate(bucket: dict[str, int], bet: Mapping[str, Any]) -> None:
    bucket["n_bets"] += 1
    status = bet["status"]
    if status == "skipped_budget":
        bucket["n_skipped_budget"] += 1
        return
    bucket["stake_yen"] += int(bet["stake_yen"])
    if status == "pending":
        bucket["n_pending"] += 1
    elif status == "settled":
        bucket["n_settled"] += 1
        bucket["payout_yen"] += int(bet.get("payout_yen") or 0)
    elif status == "refunded":
        bucket["n_refunded"] += 1
        bucket["payout_yen"] += int(bet.get("payout_yen") or 0)


def _max_drawdown_yen(ordered_net_yen: Iterable[int]) -> int:
    """本体 (is_control=0) の確定済ベットを決定順に並べた損益系列から、
    累積損益の最大ドローダウン (円、0以下) を計算する純関数。"""
    peak = cumulative = 0
    max_dd = 0
    for net in ordered_net_yen:
        cumulative += net
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return max_dd


def summarize(bets: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """純関数: virtual_betsの行リスト → perfダッシュボード用の日別・累計サマリ。
    本体 (is_control=0) と対照 (is_control=1) を分けて集計し、本体の最大DDを
    決定順 (date, decided_at, virtual_bet_id) の確定済み損益系列から算出する。"""
    ordered = sorted(bets, key=lambda b: (b["date"], b.get("decided_at") or "", b["virtual_bet_id"]))
    by_date: dict[str, dict[str, dict[str, int]]] = {}
    by_date_confirmed_stake: dict[str, dict[str, int]] = {}
    cumulative = {"main": _empty_bucket(), "control": _empty_bucket()}
    cumulative_confirmed_stake = {"main": 0, "control": 0}
    main_net_series: list[int] = []

    for bet in ordered:
        leg = "control" if bet["is_control"] else "main"
        day = by_date.setdefault(bet["date"], {"main": _empty_bucket(), "control": _empty_bucket()})
        day_stake = by_date_confirmed_stake.setdefault(bet["date"], {"main": 0, "control": 0})
        _accumulate(day[leg], bet)
        _accumulate(cumulative[leg], bet)
        if bet["status"] in ("settled", "refunded"):
            day_stake[leg] += int(bet["stake_yen"])
            cumulative_confirmed_stake[leg] += int(bet["stake_yen"])
            if leg == "main":
                main_net_series.append(int(bet.get("payout_yen") or 0) - int(bet["stake_yen"]))

    def _finalize(bucket: dict[str, int], confirmed_stake: int) -> dict[str, Any]:
        view = dict(bucket)
        view["roi_pct"] = round(100.0 * bucket["payout_yen"] / confirmed_stake, 1) if confirmed_stake else None
        return view

    days = []
    for date in sorted(by_date, reverse=True):
        stakes = by_date_confirmed_stake[date]
        days.append({
            "date": date,
            "main": _finalize(by_date[date]["main"], stakes["main"]),
            "control": _finalize(by_date[date]["control"], stakes["control"]),
        })

    return {
        "policy_version": POLICY_VERSION,
        "disclaimer": DISCLAIMER_JA,
        "days": days,
        "cumulative": {
            "main": _finalize(cumulative["main"], cumulative_confirmed_stake["main"]),
            "control": _finalize(cumulative["control"], cumulative_confirmed_stake["control"]),
        },
        "max_drawdown_yen": _max_drawdown_yen(main_net_series),
    }


def dashboard_payload(*, date: str | None = None, db_path: str | Path | None = None,
                      settle: bool = True) -> dict[str, Any]:
    """perfダッシュボード用のI/Oエントリ。lazy精算 (settle=True既定) してから
    表示用集計を返す (SPEC-T70 §4-2)。settle_pending_bets が失敗しても表示自体は
    fail-softで継続する (ダッシュボード表示を精算の成否に依存させない)。"""
    if settle:
        try:
            settle_pending_bets(date=date, db_path=db_path)
        except Exception:
            pass
    return summarize(load_bets(date=date, db_path=db_path))


# ─── ドライラン (実DB読み取り専用。書き込みしない) ───────────────────────────

def dry_run(date: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """稼働前の検証用: 実DB (既定で data/jra_logging.db) を読み取り専用で開き、
    指定日の選定レース (selected=1) を cutoff_at 昇順に見ていったとき、もし
    ポリシーv1が稼働していたら何をベットしていたかを純関数だけで再現する。
    virtual_betsテーブルへの書き込みは一切しない。同一レースに複数回の
    スナップショット書込 (実運用ログで観測済み: 同一date/place/rが2行) がある
    場合は、cutoff_atが最初の行のみを採用する (record_decisionの
    idempotency_keyがrace_id単位のため、二重計上を避けるのと同じ挙動)。"""
    path = _db_path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM race_confidence_snapshots WHERE date=? AND selected=1 "
            "ORDER BY cutoff_at ASC, race_confidence_snapshot_id ASC",
            (date,),
        ).fetchall()
    finally:
        conn.close()

    seen_races: set[str] = set()
    main_budget_used = 0
    decisions: list[dict[str, Any]] = []
    for row in rows:
        snapshot = dict(row)
        race_key = f"{snapshot['date']}:{snapshot['place']}:{int(snapshot['r']):02d}"
        if race_key in seen_races:
            continue  # 同一レースの2回目以降のスナップショット行 (idempotencyと同じ扱い)
        seen_races.add(race_key)
        bet_rows = build_bet_decisions(snapshot, main_budget_used_yen=main_budget_used)
        for bet_row in bet_rows:
            if not bet_row["is_control"]:
                main_budget_used += bet_row["stake_yen"]
        decisions.extend(bet_rows)
    return decisions


def _print_dry_run(date: str, db_path: str | Path | None = None) -> None:
    decisions = dry_run(date, db_path=db_path)
    if not decisions:
        print(f"{date}: 選定レースなし、またはT62b未稼働のためベットなし")
        return
    for row in decisions:
        leg = "対照" if row["is_control"] else "本体"
        print(
            f"{row['date']} {row['race_id']} [{leg}] {row['bet_type']} {row['horse_number']}番 "
            f"stake=¥{row['stake_yen']} status={row['status']} "
            f"odds={row['decision_odds']} prob={row['decision_prob']} "
            f"cutoff={row['decided_at']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-T70 virtual betting harness (paper trading)")
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="実DB読み取り専用のドライラン (書き込みなし)")
    dry.add_argument("--date", required=True, help="YYYYMMDD")
    dry.add_argument("--db")

    settle = sub.add_parser("settle", help="pendingなvirtual_betsを冪等に精算する")
    settle.add_argument("--date")
    settle.add_argument("--db")

    args = parser.parse_args()
    if args.command == "dry-run":
        _print_dry_run(args.date, db_path=args.db)
    elif args.command == "settle":
        result = settle_pending_bets(date=args.date, db_path=args.db)
        print(result)


if __name__ == "__main__":
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
