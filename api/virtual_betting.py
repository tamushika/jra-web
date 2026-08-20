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

SPEC-T70b (2026-08-20、合意の正は docs/T70-pattern-discussion.md): 上のv1は
「稼働済み・凍結」の confirmatory primary として一切変更しない。これに加えて
**estimation-only** (実マネー移行資格なし) の2パターンを同一フックで評価する。
各パターンは独立財布 (policy_version単位で日次予算¥10,000を集計) なので、
P5/P3の追加はP1の決定行・冪等キー・予算判定に一切影響しない。

ポリシー p5-v1 (P5、全適格ベースライン):
  - 対象: race_confidence_snapshots の **selected値を問わず** status=ok相当の行
    (model_probs_json/market_odds_json あり・凍結manifest SHA一致)。9R未満等の
    failure行はmodel_probs/market_oddsが元から欠損しているため従来どおり対象外
  - 買い目: v1と同じ選択関数 (CL勝率1位馬) の複勝¥1,000。対照は同レース1番人気
    複勝¥1,000 (予算外)。日上限¥10,000 (独立財布)

ポリシー p3-v1 (P3、λ複勝EV):
  - 対象: p5-v1と同じ母集団のうち、同一レースの30分前 board_odds_snapshots
    (bet_type='place'。日本語の複勝=fukushoの意で、実際のDB列値は'place') が
    T62b cutoffと同一ループ由来 (stage='30'・cutoff_atに最も近いreceived_at、
    許容誤差15分以内) として取得できるもの。無ければ status='skipped_data' で
    1行記録 (でっち上げ禁止)。実測ログでは板取得がcutoff_atよりわずかに後に
    完了するため、厳密な「received_at<=cutoff_at」ではなく近傍一致を採用
    (_load_p3_board_odds のdocstring参照)
  - 複勝確率: cutoffの単勝オッズ (market_odds_json) から T59d本番と同一の
    導出関数 (api.board_market.market_probabilities + derive_probabilities、
    λ2固定) で計算する。独自実装はしない。7頭以下はskipped_data
  - 買い目: `odds_low × 複勝確率` が最大かつ1.00以上の1頭の複勝¥1,000
    (同値は馬番小)。1.00未満の日・レースは何も買わない (行を記録しない)。
    対照なし。日上限¥10,000 (独立財布)

P5/P3は推定専用 — 同一ゲート判定もBonferroni補正もしない。表示にもその旨を
明示する (jra_perf.py / index_perf.html)。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .logging_store import DEFAULT_DB_PATH, LoggingStore, utc_now
    from . import board_market
    from . import race_confidence
except ImportError:  # direct execution from api/
    from logging_store import DEFAULT_DB_PATH, LoggingStore, utc_now
    import board_market
    import race_confidence


# ─── ポリシー v1 定数 (変更にはPOLICY_VERSION更新+台帳記録が必要) ─────────────
POLICY_VERSION = "v1"
BET_TYPE_MAIN = "fukusho"
STAKE_YEN = 2000
DAILY_BUDGET_YEN = 10000

DISCLAIMER_JA = "仮想運用であり実購入・購入推奨ではありません"

# ─── SPEC-T70b: P5/P3 (estimation-only) 定数 ─────────────────────────────────
P5_POLICY_VERSION = "p5-v1"
P5_STAKE_YEN = 1000
P3_POLICY_VERSION = "p3-v1"
P3_STAKE_YEN = 1000
P3_EV_THRESHOLD = 1.00
P3_MIN_RUNNERS = 8  # 7頭以下は対象外 (skipped_data)。T62bの8頭ゲートと二重防御
P3_BOARD_BET_TYPE = "place"  # board_odds_snapshots.bet_type の実際の値 (複勝)
P3_BOARD_STAGE = "30"  # T62b cutoffと同一ループ由来 (30分前) の板取得のみ対象

ESTIMATION_ONLY_POLICIES = (P5_POLICY_VERSION, P3_POLICY_VERSION)
ESTIMATION_DISCLAIMER_JA = "推定専用 (実運用移行資格なし)"
CONFIRMATORY_LABEL_JA = "移行ゲートあり"
POLICY_LABELS_JA = {
    POLICY_VERSION: CONFIRMATORY_LABEL_JA,
    P5_POLICY_VERSION: ESTIMATION_DISCLAIMER_JA,
    P3_POLICY_VERSION: ESTIMATION_DISCLAIMER_JA,
}

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


def _main_control_rows(snapshot: Mapping[str, Any], *, policy_version: str, stake_yen: int,
                       main_budget_used_yen: int) -> list[dict[str, Any]]:
    """純関数 (P1/P5共通): 凍結manifest一致・model_probs/market_odds有りの
    race_confidence_snapshots 行 → 0/2件の virtual_bets 行案 (本体=CL勝率1位馬、
    対照=cutoffオッズ最小の1番人気)。呼び出し側が対象母集団のゲート (P1=
    selected==1、P5=ゲートなし) を先に判定してからこの関数を呼ぶ。DB I/Oはしない。

    予算超過時は本体行の stake_yen=0・status='skipped_budget' (対照は予算外なので
    常に stake_yen=stake_yen固定)。SPEC-T70 §1 (P1) / SPEC-T70b §2 (P5) 共通ロジック。
    """
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
    within_budget = main_budget_used_yen + stake_yen <= DAILY_BUDGET_YEN

    def _row(*, horse_number, stake, is_control, status, odds_map, prob_map, suffix):
        return {
            "idempotency_key": f"{policy_version}:{race_id}:{BET_TYPE_MAIN}:{suffix}",
            "policy_version": policy_version, "race_id": race_id, "date": date,
            "bet_type": BET_TYPE_MAIN, "horse_number": horse_number, "stake_yen": stake,
            "decided_at": decided_at, "cutoff_source": cutoff_source,
            "decision_odds": odds_map.get(horse_number), "decision_prob": prob_map.get(horse_number),
            "is_control": int(is_control), "status": status,
            "payout_yen": None, "settled_at": None,
        }

    return [
        _row(horse_number=main_num, stake=stake_yen if within_budget else 0, is_control=False,
             status="pending" if within_budget else "skipped_budget",
             odds_map=market_odds, prob_map=model_probs, suffix="main"),
        _row(horse_number=control_num, stake=stake_yen, is_control=True, status="pending",
             odds_map=market_odds, prob_map=model_probs, suffix="control"),
    ]


def build_bet_decisions(snapshot: Mapping[str, Any], *, main_budget_used_yen: int) -> list[dict[str, Any]]:
    """純関数: 1件の race_confidence_snapshots 行 → 0/2件の virtual_bets 行案 (P1/v1)。

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
    return _main_control_rows(snapshot, policy_version=POLICY_VERSION, stake_yen=STAKE_YEN,
                              main_budget_used_yen=main_budget_used_yen)


def build_bet_decisions_p5(snapshot: Mapping[str, Any], *, main_budget_used_yen: int) -> list[dict[str, Any]]:
    """純関数: P5 (全適格ベースライン、estimation-only) の決定 (SPEC-T70b §2)。

    P1と異なり selected値を問わない (status=ok相当、すなわち model_probs_json/
    market_odds_json が揃い凍結manifestが一致する行なら対象)。買い目の選択関数・
    賭け金構造はP1と同一で、stake_yenのみP5_STAKE_YENに変わる (独立財布)。
    """
    return _main_control_rows(snapshot, policy_version=P5_POLICY_VERSION, stake_yen=P5_STAKE_YEN,
                              main_budget_used_yen=main_budget_used_yen)


# ─── 純関数: P3 (λ複勝EV、estimation-only) ───────────────────────────────────

def compute_p3_place_probabilities(market_odds: Mapping[int, float]) -> dict[int, float] | None:
    """P3専用の純関数: cutoffの単勝オッズ (race_confidence_snapshots.
    market_odds_json を馬番→オッズに変換済みのもの) から、T59d本番と同一の
    導出関数 (api.board_market.market_probabilities + derive_probabilities、
    λ2固定) で馬番→複勝確率を計算する。独自実装はしない (既存モジュールを呼ぶ)。

    7頭以下、またはbookサム (逆オッズ合計) が正常レンジ外 (取消混入等) の場合は
    None を返す (呼び出し側で status='skipped_data' として扱う)。
    """
    numbers = sorted(market_odds)
    if len(numbers) < P3_MIN_RUNNERS:
        return None
    odds_list = [market_odds[number] for number in numbers]
    parsed = board_market.market_probabilities(odds_list)
    if parsed is None:
        return None
    probabilities, _book_sum = parsed
    derived = board_market.derive_probabilities(probabilities, places=3)
    place_probs = derived["place"]
    return {number: place_probs[index] for index, number in enumerate(numbers)}


def select_p3_ev_horse(place_probs: Mapping[int, float], odds_low: Mapping[int, float]
                       ) -> tuple[int, float] | None:
    """P3専用の純関数: `odds_low × 複勝確率` (期待値) が最大かつ P3_EV_THRESHOLD
    (1.00) 以上の1頭を (馬番, 期待値) で返す (同値は馬番小)。odds_lowに存在しない
    馬は候補から除外する。該当なしは None (1.00未満の日・レースは何も買わない)。
    """
    best_num, best_ev = None, None
    for number, prob in place_probs.items():
        low = odds_low.get(number)
        if low is None:
            continue
        ev = low * prob
        if ev < P3_EV_THRESHOLD:
            continue
        if best_num is None or ev > best_ev or (ev == best_ev and number < best_num):
            best_num, best_ev = number, ev
    return (best_num, best_ev) if best_num is not None else None


def build_bet_decisions_p3(snapshot: Mapping[str, Any], *, main_budget_used_yen: int,
                           board_odds_low: Mapping[int, float] | None) -> list[dict[str, Any]]:
    """純関数: P3 (λ複勝EV、estimation-only) の決定 (SPEC-T70b §3)。DB I/Oはしない。
    board_odds_lowは呼び出し側 (record_decision_p3) が board_odds_snapshots から
    読み取って渡す (この関数自体はDBに触れない、build_bet_decisions系と同じ設計)。

    対象母集団はP5と同じ (凍結manifest一致・model_probs/market_odds有り)。
    7頭以下、またはboard_odds_lowが None (30分前板が未取得/book外れ等) の場合は
    status='skipped_data' の1行を記録する (でっち上げ禁止、対照なし)。1.00未満で
    誰も条件を満たさない場合は何も買わない (行を記録しない、SPEC-T70b §3)。
    """
    if snapshot.get("manifest_sha256") != race_confidence.EXPECTED_MANIFEST_SHA256:
        return []
    model_probs = _parse_number_map(snapshot.get("model_probs_json"))
    market_odds = _parse_number_map(snapshot.get("market_odds_json"))
    if not model_probs or not market_odds:
        return []

    date = str(snapshot["date"])
    place = str(snapshot["place"])
    race_no = int(snapshot["r"])
    race_id = f"{date}:{place}:{race_no:02d}"
    decided_at = snapshot.get("cutoff_at") or utc_now()
    snapshot_id = snapshot.get("race_confidence_snapshot_id")
    cutoff_source = f"race_confidence_snapshot:{snapshot_id}" if snapshot_id is not None \
        else "race_confidence_snapshot:unknown"
    idempotency_key = f"{P3_POLICY_VERSION}:{race_id}:{BET_TYPE_MAIN}:main"

    def _row(*, horse_number, stake_yen, status, decision_odds, decision_prob):
        return {
            "idempotency_key": idempotency_key, "policy_version": P3_POLICY_VERSION,
            "race_id": race_id, "date": date, "bet_type": BET_TYPE_MAIN,
            "horse_number": horse_number, "stake_yen": stake_yen,
            "decided_at": decided_at, "cutoff_source": cutoff_source,
            "decision_odds": decision_odds, "decision_prob": decision_prob,
            "is_control": 0, "status": status, "payout_yen": None, "settled_at": None,
        }

    place_probs = compute_p3_place_probabilities(market_odds)
    if place_probs is None or board_odds_low is None:
        # データが揃わずEVを計算できない (7頭以下・板未取得・book外れ)。何も
        # 買わずに1行だけ記録する (でっち上げ禁止)。参照用にCL1位馬の馬番を
        # 記録するが、これはP3自身の選択結果ではない (decision_odds/probは空)。
        reference_num = select_top_probability_horse(model_probs)
        return [_row(horse_number=reference_num, stake_yen=0, status="skipped_data",
                     decision_odds=None, decision_prob=None)]

    chosen = select_p3_ev_horse(place_probs, board_odds_low)
    if chosen is None:
        return []  # 1.00未満: 何も買わない (ベット0の日は正常、行も記録しない)
    horse_number, _ev = chosen
    within_budget = main_budget_used_yen + P3_STAKE_YEN <= DAILY_BUDGET_YEN
    return [_row(
        horse_number=horse_number, stake_yen=P3_STAKE_YEN if within_budget else 0,
        status="pending" if within_budget else "skipped_budget",
        decision_odds=board_odds_low.get(horse_number), decision_prob=place_probs.get(horse_number),
    )]


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

def _budget_used_yen(conn: sqlite3.Connection, *, date: str, policy_version: str) -> int:
    """指定日・指定policyの本体stake合計 (対照は含まない)。policy_version単位で
    独立財布なので、P5/P3の追加はP1の予算判定に一切影響しない (SPEC-T70b §1)。"""
    used = conn.execute(
        "SELECT COALESCE(SUM(stake_yen),0) FROM virtual_bets "
        "WHERE date=? AND is_control=0 AND policy_version=?",
        (date, policy_version),
    ).fetchone()[0]
    return int(used or 0)


def _insert_decision_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]
                          ) -> list[dict[str, Any]]:
    """決定行 (0件以上) を冪等にvirtual_betsへ書き込む共通I/O。idempotency_keyが
    policy_version×race_id×bet_type×suffixで決まるため、同一決定への複数回
    呼び出しは安全 (INSERT OR IGNORE)。record_decision/record_decision_p5/
    record_decision_p3 の共通処理 (T70b: P1既存の書き込みロジックを抽出しただけで、
    P1の外部から見える挙動は変えていない)。"""
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


def record_decision(snapshot: Mapping[str, Any], *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """P1 (v1) の決定を冪等に永続化する薄いI/Oエントリ。純関数 build_bet_decisions
    を使い、その日 (snapshot["date"]) に既に積み上がった本体stakeをDBから読んで
    予算判定してから、virtual_bets へ INSERT OR IGNORE する (idempotency_keyが
    race_id×bet_type×本体/対照で決まるため、同一レースへの複数回呼び出しは安全)。

    SPEC-T70b: このP1専用関数のシグネチャ・挙動は一切変更していない (凍結済み
    confirmatory primary)。P5/P3はそれぞれ独立の record_decision_p5/
    record_decision_p3 を持ち、フック (record_decision_for_snapshot_id) がこの
    3つをまとめて呼ぶ。"""
    LoggingStore(db_path).initialize()
    date = str(snapshot.get("date") or "")

    def op(conn: sqlite3.Connection):
        used = _budget_used_yen(conn, date=date, policy_version=POLICY_VERSION)
        rows = build_bet_decisions(snapshot, main_budget_used_yen=used)
        return _insert_decision_rows(conn, rows)

    return _write(db_path, op)


def record_decision_p5(snapshot: Mapping[str, Any], *, db_path: str | Path | None = None
                       ) -> list[dict[str, Any]]:
    """P5 (p5-v1、estimation-only) の決定を冪等に永続化するI/Oエントリ。record_
    decision (P1) と同じ構造だが、独立財布 (policy_version='p5-v1') で予算判定する。
    """
    LoggingStore(db_path).initialize()
    date = str(snapshot.get("date") or "")

    def op(conn: sqlite3.Connection):
        used = _budget_used_yen(conn, date=date, policy_version=P5_POLICY_VERSION)
        rows = build_bet_decisions_p5(snapshot, main_budget_used_yen=used)
        return _insert_decision_rows(conn, rows)

    return _write(db_path, op)


_P3_BOARD_MATCH_TOLERANCE_SECONDS = 15 * 60  # T62b cutoffと同一ループ由来のみ許容


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_p3_board_odds(conn: sqlite3.Connection, snapshot: Mapping[str, Any]
                        ) -> dict[int, float] | None:
    """P3専用の読み取り専用ヘルパー: 対象レースの複勝板 (board_odds_snapshots.
    bet_type='place'、T62bと同一ループ由来のstage='30') から馬番→odds_low の辞書
    を返す。未取得・book外れ・fetch失敗 (combo='*'の代表行) はNone。呼び出し側
    (build_bet_decisions_p3) でstatus='skipped_data'として扱う。

    結合はcutoff_atに最も近いreceived_atの1バッチを採用する (許容誤差
    _P3_BOARD_MATCH_TOLERANCE_SECONDS以内のみ)。単純な「received_at<=cutoff_at」
    ではない: 実運用ログ実測 (2026-08-15/16) で、板取得 (board_market.
    fetch_jra_odds、JRA公式へ複数回HTTPを重ねる) はT62bのcutoff_at (=主要オッズ
    取得のobserved_at) より数秒後に完了して記録される。同一ループ由来である
    ことに変わりはなく、この数秒差でas-of保証を崩すものではないため、厳密な
    前方一致ではなく近傍一致にした (SPEC-T70b §3の「同一ループ由来」の意図を
    優先し、字面どおりの<=だと実データで常にskipped_dataになってしまうため)。
    """
    date = str(snapshot.get("date") or "")
    place = str(snapshot.get("place") or "")
    race_no = int(snapshot.get("r") or 0)
    race_id = f"{date}:{place}:{race_no:02d}"
    candidates = [row[0] for row in conn.execute(
        "SELECT DISTINCT received_at FROM board_odds_snapshots "
        "WHERE race_id=? AND bet_type=? AND stage=?",
        (race_id, P3_BOARD_BET_TYPE, P3_BOARD_STAGE),
    ).fetchall()]
    if not candidates:
        return None

    target = _parse_iso_datetime(snapshot.get("cutoff_at"))
    if target is None:
        chosen_received_at = max(candidates)  # cutoff_at不明時は最新を採用
    else:
        best_value, best_diff = None, None
        for value in candidates:
            parsed = _parse_iso_datetime(value)
            if parsed is None:
                continue
            diff = abs((parsed - target).total_seconds())
            if diff > _P3_BOARD_MATCH_TOLERANCE_SECONDS:
                continue
            if best_diff is None or diff < best_diff:
                best_value, best_diff = value, diff
        if best_value is None:
            return None
        chosen_received_at = best_value

    rows = conn.execute(
        "SELECT combo, odds_low, status FROM board_odds_snapshots "
        "WHERE race_id=? AND bet_type=? AND stage=? AND received_at=?",
        (race_id, P3_BOARD_BET_TYPE, P3_BOARD_STAGE, chosen_received_at),
    ).fetchall()
    odds_low: dict[int, float] = {}
    for combo, low, status in rows:
        if status != "ok" or combo == "*" or low is None:
            continue
        try:
            odds_low[int(combo)] = float(low)
        except (TypeError, ValueError):
            continue
    return odds_low or None


def record_decision_p3(snapshot: Mapping[str, Any], *, db_path: str | Path | None = None
                       ) -> list[dict[str, Any]]:
    """P3 (p3-v1、estimation-only) の決定を冪等に永続化するI/Oエントリ。
    board_odds_snapshotsから板を読み取り (_load_p3_board_odds)、純関数
    build_bet_decisions_p3 に渡して決定してから、独立財布 (policy_version=
    'p3-v1') で予算判定してvirtual_betsへ書き込む。"""
    LoggingStore(db_path).initialize()
    date = str(snapshot.get("date") or "")

    def op(conn: sqlite3.Connection):
        used = _budget_used_yen(conn, date=date, policy_version=P3_POLICY_VERSION)
        board_odds_low = _load_p3_board_odds(conn, snapshot)
        rows = build_bet_decisions_p3(snapshot, main_budget_used_yen=used, board_odds_low=board_odds_low)
        return _insert_decision_rows(conn, rows)

    return _write(db_path, op)


def record_decision_for_snapshot_id(snapshot_id: int | None, *, db_path: str | Path | None = None
                                    ) -> list[dict[str, Any]]:
    """suiteフック用のエントリ (jra_ev.py._capture_race_confidence が呼ぶ唯一の
    T70フック)。T62bスナップショット書込 (LoggingStore.save_race_confidence が
    返す race_confidence_snapshot_id) の直後に呼ぶ。保存済みの当該行をそのまま
    読み戻して P1 (record_decision)・P5 (record_decision_p5)・P3
    (record_decision_p3) の3つをこの1箇所で評価する (SPEC-T70b §1)。

    P1の呼び出しは他パターンの失敗から隔離しない (従来通り、失敗時は例外を
    そのまま呼び出し元 jra_ev.py の既存try/exceptに委ねる)。P5/P3はestimation-
    onlyなので、どちらかが例外を投げても他方・P1の記録を妨げないようfail-softに
    捕捉する (P3のboard読み取り失敗等がP1/P5の記録をブロックしてはならない)。
    """
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

    inserted: list[dict[str, Any]] = list(record_decision(snapshot, db_path=db_path))
    for estimation_entry in (record_decision_p5, record_decision_p3):
        try:
            inserted += estimation_entry(snapshot, db_path=db_path)
        except Exception as exc:
            print(f"[WARN] T70b {estimation_entry.__name__} failed: {type(exc).__name__}: {exc}")
    return inserted


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
            "n_skipped_budget": 0, "n_skipped_data": 0, "stake_yen": 0, "payout_yen": 0}


def _accumulate(bucket: dict[str, int], bet: Mapping[str, Any]) -> None:
    bucket["n_bets"] += 1
    status = bet["status"]
    if status == "skipped_budget":
        bucket["n_skipped_budget"] += 1
        return
    if status == "skipped_data":
        # T70b: P3の板未取得・7頭以下 (でっち上げ禁止の記録のみの行)。stake=0
        # なので集計に加算するものはないが、内訳として可視化する。
        bucket["n_skipped_data"] += 1
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


def summarize(bets: Iterable[Mapping[str, Any]], *, policy_version: str = POLICY_VERSION) -> dict[str, Any]:
    """純関数: virtual_betsの行リスト → perfダッシュボード用の日別・累計サマリ。
    本体 (is_control=0) と対照 (is_control=1) を分けて集計し、本体の最大DDを
    決定順 (date, decided_at, virtual_bet_id) の確定済み損益系列から算出する。

    渡す bets は呼び出し側が対象ポリシーで絞り込み済みであること (このモジュールに
    複数ポリシーが同居するT70b以降、summarize自身はフィルタしない)。policy_version
    は出力ラベル用 (既定はPOLICY_VERSION="v1"、後方互換)。"""
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
        "policy_version": policy_version,
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
    """perfダッシュボード用のI/Oエントリ (P1/v1専用、後方互換のため挙動は変更
    していない)。lazy精算 (settle=True既定) してから表示用集計を返す (SPEC-T70
    §4-2)。settle_pending_bets が失敗しても表示自体はfail-softで継続する
    (ダッシュボード表示を精算の成否に依存させない)。

    T70b以降、virtual_betsにはP5/P3の行も同居するため、この関数は呼び出し側で
    ポリシーを絞り込まない限り複数ポリシーを混ぜて集計してしまう。perfダッシュ
    ボード本体 (jra_perf.collect) はパターン別ブロック表示のため
    multi_policy_dashboard_payload を使う (SPEC-T70b §5)。この関数自体は既存の
    直接呼び出し・テストとの後方互換のために残す。"""
    if settle:
        try:
            settle_pending_bets(date=date, db_path=db_path)
        except Exception:
            pass
    return summarize(load_bets(date=date, db_path=db_path))


def p3_calibration_gap(bets: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """純関数: P3の精算済み本体ベットについて、λ校正の予測複勝確率 (decision_prob
    の平均) と実現複勝率 (的中件数/件数) の乖離を計算する (SPEC-T70b §5)。
    settled行が0件ならNone (でっち上げ禁止)。bet_type='fukusho'なので payout_yen>0
    が的中を表す (compute_settlementの仕様どおり、4着以下はpayout_yen=0で
    'settled'になる。refundedは対象外: 取消は複勝的中/不的中のどちらでもない)。"""
    settled = [bet for bet in bets
              if bet.get("policy_version") == P3_POLICY_VERSION
              and not bet.get("is_control") and bet.get("status") == "settled"]
    n = len(settled)
    if n == 0:
        return None
    predicted_sum = sum(float(bet.get("decision_prob") or 0.0) for bet in settled)
    hits = sum(1 for bet in settled if int(bet.get("payout_yen") or 0) > 0)
    predicted_rate = predicted_sum / n
    realized_rate = hits / n
    return {
        "n": n, "predicted_place_rate": predicted_rate, "realized_place_rate": realized_rate,
        "gap": realized_rate - predicted_rate,
    }


def multi_policy_dashboard_payload(*, date: str | None = None, db_path: str | Path | None = None,
                                   settle: bool = True) -> dict[str, Any]:
    """perfダッシュボード用のI/Oエントリ (SPEC-T70b §5、P1/P5/P3をパターン別
    ブロックとして返す)。lazy精算はvirtual_bets全体に対して1回だけ行う
    (settle_pending_bets自体がpolicy_versionを問わず全pendingを対象にしているため、
    ポリシーごとに繰り返す必要はない)。settle失敗時もfail-softで表示を継続する。"""
    if settle:
        try:
            settle_pending_bets(date=date, db_path=db_path)
        except Exception:
            pass
    bets = load_bets(date=date, db_path=db_path)
    by_policy: dict[str, list[dict[str, Any]]] = {}
    for bet in bets:
        by_policy.setdefault(bet["policy_version"], []).append(bet)

    patterns = {
        policy_version: summarize(by_policy.get(policy_version, []), policy_version=policy_version)
        for policy_version in (POLICY_VERSION, P5_POLICY_VERSION, P3_POLICY_VERSION)
    }
    return {
        "patterns": patterns,
        "labels": dict(POLICY_LABELS_JA),
        "estimation_only_policies": list(ESTIMATION_ONLY_POLICIES),
        "estimation_disclaimer": ESTIMATION_DISCLAIMER_JA,
        "disclaimer": DISCLAIMER_JA,
        "p3_calibration": p3_calibration_gap(bets),
    }


# ─── ドライラン (実DB読み取り専用。書き込みしない) ───────────────────────────

_DRY_RUN_POLICIES = (POLICY_VERSION, P5_POLICY_VERSION, P3_POLICY_VERSION)


def dry_run(date: str, *, policy: str = POLICY_VERSION, db_path: str | Path | None = None
           ) -> list[dict[str, Any]]:
    """稼働前の検証用: 実DB (既定で data/jra_logging.db) を読み取り専用で開き、
    指定日のrace_confidence_snapshotsを cutoff_at 昇順に見ていったとき、もし
    指定ポリシーが稼働していたら何をベットしていたかを純関数だけで再現する。
    virtual_betsテーブルへの書き込みは一切しない。同一レースに複数回の
    スナップショット書込 (実運用ログで観測済み: 同一date/place/rが2行) がある
    場合は、cutoff_atが最初の行のみを採用する (record_decisionの
    idempotency_keyがrace_id単位のため、二重計上を避けるのと同じ挙動)。

    policy='v1' (既定、SPEC-T70): T62b選定レース (selected=1) のみが対象。
    policy='p5-v1'/'p3-v1' (SPEC-T70b、estimation-only): selected値を問わず
    model_probs_json/market_odds_json が揃う全適格行が対象。p3-v1はさらに
    board_odds_snapshotsを読み (読み取り専用)、λ複勝EVで決定する。"""
    if policy not in _DRY_RUN_POLICIES:
        raise ValueError(f"unknown policy: {policy}")
    path = _db_path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if policy == POLICY_VERSION:
            where = "date=? AND selected=1"
        else:
            where = "date=? AND model_probs_json IS NOT NULL AND market_odds_json IS NOT NULL"
        rows = conn.execute(
            f"SELECT * FROM race_confidence_snapshots WHERE {where} "
            "ORDER BY cutoff_at ASC, race_confidence_snapshot_id ASC",
            (date,),
        ).fetchall()

        seen_races: set[str] = set()
        main_budget_used = 0
        decisions: list[dict[str, Any]] = []
        for row in rows:
            snapshot = dict(row)
            race_key = f"{snapshot['date']}:{snapshot['place']}:{int(snapshot['r']):02d}"
            if race_key in seen_races:
                continue  # 同一レースの2回目以降のスナップショット行 (idempotencyと同じ扱い)
            seen_races.add(race_key)
            if policy == POLICY_VERSION:
                bet_rows = build_bet_decisions(snapshot, main_budget_used_yen=main_budget_used)
            elif policy == P5_POLICY_VERSION:
                bet_rows = build_bet_decisions_p5(snapshot, main_budget_used_yen=main_budget_used)
            else:  # P3_POLICY_VERSION
                board_odds_low = _load_p3_board_odds(conn, snapshot)
                bet_rows = build_bet_decisions_p3(
                    snapshot, main_budget_used_yen=main_budget_used, board_odds_low=board_odds_low)
            for bet_row in bet_rows:
                if not bet_row["is_control"]:
                    main_budget_used += bet_row["stake_yen"]
            decisions.extend(bet_rows)
        return decisions
    finally:
        conn.close()


def _print_dry_run(date: str, db_path: str | Path | None = None, *, policy: str = POLICY_VERSION) -> None:
    decisions = dry_run(date, policy=policy, db_path=db_path)
    if not decisions:
        print(f"{date} [{policy}]: 対象レースなし、または未稼働のためベットなし")
        return
    for row in decisions:
        leg = "対照" if row["is_control"] else "本体"
        print(
            f"{row['date']} {row['race_id']} [{policy}/{leg}] {row['bet_type']} {row['horse_number']}番 "
            f"stake=¥{row['stake_yen']} status={row['status']} "
            f"odds={row['decision_odds']} prob={row['decision_prob']} "
            f"cutoff={row['decided_at']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-T70 virtual betting harness (paper trading)")
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="実DB読み取り専用のドライラン (書き込みなし)")
    dry.add_argument("--date", required=True, help="YYYYMMDD")
    dry.add_argument("--policy", default=POLICY_VERSION, choices=list(_DRY_RUN_POLICIES),
                     help="v1 (既定・P1) / p5-v1 (P5) / p3-v1 (P3)")
    dry.add_argument("--db")

    settle = sub.add_parser("settle", help="pendingなvirtual_betsを冪等に精算する")
    settle.add_argument("--date")
    settle.add_argument("--db")

    args = parser.parse_args()
    if args.command == "dry-run":
        _print_dry_run(args.date, db_path=args.db, policy=args.policy)
    elif args.command == "settle":
        result = settle_pending_bets(date=args.date, db_path=args.db)
        print(result)


if __name__ == "__main__":
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
