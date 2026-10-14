"""SPEC-T70c: 仮想運用 (T70) の移行ゲート集計ハーネス。

事前登録済み5条件 (SPEC-T70 §5, 本SPEC-T70c §0で引用・変更不可) をP1
(policy_version='v1') についてだけ機械的に判定する。P5 (p5-v1) / P3 (p3-v1)
は estimation-only・移行資格なしの参考値のみ出力する (検定を装わない)。

read-only: `data/jra_logging.db` の `virtual_bets` テーブルを
`mode=ro` + `PRAGMA query_only=ON` で開く以外の入出力は一切行わない。
ability.db・Neon・ネットワーク・既存の台帳 (`eval/experiments.jsonl`) /
`docs/TASKS.md` は参照も変更もしない。本スクリプトはfail-closed: 保留行は
ROIから除外した上で件数・理由を別掲し、除外した結果n条件を満たさなければ
INVALIDで停止する (採否判定はしない。`machine_judgment` と
`reviewer_adjudication: null` は別フィールド)。

GATE_PASSであっても実マネー移行の承認では**ない**。移行は別途ユーザー承認
+ 規約リスク調査 (T70 P4) を要する。
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# eval/blocks.py は変更しない (import のみ)。対照とのROI差の paired block
# bootstrap (SPEC §3「対照との差」) に使う。
from eval.blocks import paired_block_bootstrap

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "jra_logging.db"
DEFAULT_OUTPUT = ROOT / "outputs" / "t70c" / "gate_report.json"
DEFAULT_POLICY = "v1"

# SPEC-T70 §5 (本SPEC §0で引用・凍結)。変更禁止。
N_EVENT_DAYS_MIN = 12
N_MAIN_BETS_MIN = 30
ROI_MIN = 1.00
BOOTSTRAP_LOWER5_MIN = 0.85
MAX_PAYOUT_EXCLUDED_ROI_MIN = 0.90
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 70
PENDING_SHARE_WARN = 0.20

# estimation-only (移行資格なし)。判定は行わず参考値のみ出力する。
REFERENCE_ONLY_POLICIES = ("p5-v1", "p3-v1")

ALLOWED_COLUMNS = (
    "policy_version", "race_id", "date", "bet_type", "horse_number",
    "stake_yen", "decision_odds", "decision_prob", "is_control", "status",
    "payout_yen", "settled_at",
)

# ROIの分子・分母に入れる状態 (SPEC §2-2)。保留 (pending) はROIから除外して
# 別掲 (§2-3)。skipped_budget/skipped_data は投資していないので母集団外
# (§2-4)。refundedは払戻が確定しているがstakeが全額返還される押し (push) で
# あり、SPEC §2-2が明示する「ROIの分子・分母」の対象をsettledのみに限定して
# いるため、settledと同じ『実際に賭けて確定した』扱いにはしつつROI計算からは
# 外す。本スクリプト独自の解釈点であり、レビュー時に要確認 (報告に明記)。
ROI_ELIGIBLE_STATUSES = frozenset({"settled"})
# n (件数) 条件は「pending as 実ベット」+ settled のみを数える。skipped_*は
# 投資していないので対象外、refundedは実際には賭けたが全額返還されるため
# ここでは対象外としたが、v1の実データには該当行が無く判定に影響しない
# (報告の『逸脱』節に明記)。
N_ELIGIBLE_STATUSES = frozenset({"pending", "settled"})


class GateError(RuntimeError):
    """fail-closed: データ不整合や計算不能を示す例外。"""


@dataclass(frozen=True)
class BetRow:
    policy_version: str
    race_id: str
    date: str
    bet_type: str
    horse_number: int
    stake_yen: int
    decision_odds: float | None
    decision_prob: float | None
    is_control: int
    status: str
    payout_yen: int | None
    settled_at: str | None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection, policy_version: str) -> list[BetRow]:
    columns = ", ".join(ALLOWED_COLUMNS)
    cur = conn.execute(
        f"SELECT {columns} FROM virtual_bets WHERE policy_version=? ORDER BY date, race_id, is_control",
        (policy_version,),
    )
    return [BetRow(**{k: row[k] for k in ALLOWED_COLUMNS}) for row in cur.fetchall()]


def load_global_max_date(conn: sqlite3.Connection) -> str | None:
    """全ポリシー・全行のうち最新の date (保留行の『as-of』基準)。virtual_bets
    の `date` 列以外は使わない (SPEC §1 の許可列のみ)。"""

    row = conn.execute("SELECT MAX(date) AS max_date FROM virtual_bets").fetchone()
    return row["max_date"] if row else None


def _split_by_status(rows: Sequence[BetRow]) -> dict[str, list[BetRow]]:
    grouped: dict[str, list[BetRow]] = defaultdict(list)
    for row in rows:
        grouped[row.status].append(row)
    return dict(grouped)


def _roi(rows: Sequence[BetRow]) -> dict[str, Any]:
    stake = sum(r.stake_yen for r in rows)
    payout_rows = [r for r in rows if r.payout_yen is not None]
    if len(payout_rows) != len(rows):
        raise GateError(
            "settled row(s) with payout_yen=NULL encountered "
            f"({len(rows) - len(payout_rows)} of {len(rows)})"
        )
    payout = sum(r.payout_yen for r in rows)
    if stake <= 0:
        return {"n": len(rows), "stake_yen": stake, "payout_yen": payout, "roi": None}
    return {"n": len(rows), "stake_yen": stake, "payout_yen": payout, "roi": payout / stake}


def _date_blocks(rows: Sequence[BetRow]) -> dict[str, list[BetRow]]:
    blocks: dict[str, list[BetRow]] = defaultdict(list)
    for row in rows:
        blocks[row.date].append(row)
    return dict(blocks)


def _roi_of_rows(rows: Sequence[BetRow]) -> float:
    stake = sum(r.stake_yen for r in rows)
    if stake <= 0:
        raise GateError("bootstrap resample has zero total stake (cannot compute ROI)")
    payout = sum(r.payout_yen for r in rows)
    return payout / stake


def block_bootstrap_lower_quantile(
    rows: Sequence[BetRow], *, n_resamples: int, seed: int, quantile: float = 0.05,
) -> dict[str, Any]:
    """開催日 (date) ブロック単位の復元抽出bootstrap (SPEC §3)。

    各リサンプルで開催日ブロックを len(blocks) 回、復元抽出し、その日の
    ベットをまとめて1ブロックとして動かす (backtest_t63_ev_threshold.py の
    `_bootstrap_roi` と同じ方式)。
    """

    blocks = _date_blocks(rows)
    keys = sorted(blocks)
    if not keys:
        raise GateError("bootstrap requires at least one event-day block")
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        sample: list[BetRow] = []
        for _ in keys:
            sample.extend(blocks[rng.choice(keys)])
        values.append(_roi_of_rows(sample))
    if not all(math.isfinite(v) for v in values):
        raise GateError("bootstrap produced a non-finite ROI value")
    ordered = sorted(values)
    return {
        "n_blocks": len(keys),
        "n_resamples": n_resamples,
        "seed": seed,
        "lower_quantile": quantile,
        "lower_quantile_value": _quantile(ordered, quantile),
        "ci95": [_quantile(ordered, 0.025), _quantile(ordered, 0.975)],
    }


def _quantile(ordered: Sequence[float], probability: float) -> float:
    if not ordered:
        raise GateError("cannot compute a quantile of an empty sample")
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def exclude_max_payout(rows: Sequence[BetRow]) -> tuple[list[BetRow], BetRow | None]:
    """payout_yen が最大の1行を母集団から除外する (SPEC §3)。タイは
    (date, race_id, horse_number) 昇順で最初の1件を除外し、決定的にする。"""

    if not rows:
        return list(rows), None
    max_payout = max(r.payout_yen or 0 for r in rows)
    tied = [r for r in rows if (r.payout_yen or 0) == max_payout]
    tied.sort(key=lambda r: (r.date, r.race_id, r.horse_number))
    excluded = tied[0]
    remaining = [r for r in rows if r is not excluded]
    return remaining, excluded


def pair_control_rows(
    main_settled: Sequence[BetRow], control_settled: Sequence[BetRow],
) -> dict[str, Any]:
    """本体settledベットと対照settledベットを (race_id, date, bet_type) で
    突合する (SPEC §5 テスト6「対照の突合」)。どちらか一方にしか無い行は
    警告として報告する (判定のROIは各群の独立集計のまま、SPEC §3の点推定に
    従う)。"""

    def _key(row: BetRow) -> tuple[str, str, str]:
        return (row.race_id, row.date, row.bet_type)

    main_keys = {_key(r) for r in main_settled}
    control_keys = {_key(r) for r in control_settled}
    missing_control = sorted(" ".join(k) for k in main_keys - control_keys)
    missing_main = sorted(" ".join(k) for k in control_keys - main_keys)
    return {
        "paired": len(main_keys & control_keys),
        "main_missing_control": missing_control,
        "control_missing_main": missing_main,
        "fully_paired": not missing_control and not missing_main,
    }


def categorize_pending(rows: Sequence[BetRow], as_of_date: str | None) -> list[dict[str, Any]]:
    """保留行の理由を virtual_bets の `date` 列のみから分類する (SPEC §1の
    許可列のみ使用。decided_at等の追加調査は本ハーネスのスコープ外)。"""

    out = []
    for row in rows:
        gap_days = None
        reason = "unknown_as_of_unavailable"
        if as_of_date is not None:
            try:
                gap_days = (
                    date(int(as_of_date[:4]), int(as_of_date[4:6]), int(as_of_date[6:8]))
                    - date(int(row.date[:4]), int(row.date[4:6]), int(row.date[6:8]))
                ).days
            except ValueError:
                gap_days = None
            if gap_days is not None:
                reason = (
                    "sync_pending_recent"
                    if gap_days <= 3
                    else "stale_pending_unexplained"
                )
        out.append({
            "race_id": row.race_id,
            "date": row.date,
            "bet_type": row.bet_type,
            "horse_number": row.horse_number,
            "is_control": bool(row.is_control),
            "gap_days_from_as_of": gap_days,
            "reason": reason,
            "reason_note": (
                "直近 (3日以内) のため結果同期待ちの可能性 (virtual_betsの`date`列"
                "のみから推定。確定診断にはrace_results等の追加照会が必要=本ハー"
                "ネスのスコープ外)"
                if reason == "sync_pending_recent"
                else (
                    "同期後も長期間保留 (取消・除外等による返還待ち、または精算"
                    "ロジックの異常の可能性。virtual_betsのみでは断定不可=要人力調査)"
                    if reason == "stale_pending_unexplained"
                    else "as_of日付が取得できず分類不能"
                )
            ),
        })
    return out


def _auxiliary_report(settled_main: Sequence[BetRow]) -> dict[str, Any]:
    """補助報告 (判定には使わない。SPEC §3)。"""

    if not settled_main:
        return {"n": 0}
    hits = sum(1 for r in settled_main if (r.payout_yen or 0) > 0)
    odds = [r.decision_odds for r in settled_main if r.decision_odds is not None]
    by_day: dict[str, list[BetRow]] = defaultdict(list)
    for r in settled_main:
        by_day[r.date].append(r)
    daily = []
    cum_pnl = 0
    peak = 0
    max_drawdown = 0
    for day in sorted(by_day):
        day_rows = by_day[day]
        stake = sum(r.stake_yen for r in day_rows)
        payout = sum(r.payout_yen or 0 for r in day_rows)
        cum_pnl += payout - stake
        peak = max(peak, cum_pnl)
        max_drawdown = min(max_drawdown, cum_pnl - peak)
        daily.append({
            "date": day, "n": len(day_rows), "stake_yen": stake, "payout_yen": payout,
            "cumulative_pnl_yen": cum_pnl,
        })
    # 人気帯別: decision_odds 昇順順位で代用 (人気の実値ではない。明記)。
    ranked = sorted(settled_main, key=lambda r: (r.decision_odds is None, r.decision_odds))
    bands = {"1-3": [], "4-8": [], "9+": []}
    for idx, row in enumerate(ranked, start=1):
        key = "1-3" if idx <= 3 else ("4-8" if idx <= 8 else "9+")
        bands[key].append(row)
    band_report = {
        name: {"n": len(rows), "hits": sum(1 for r in rows if (r.payout_yen or 0) > 0)}
        for name, rows in bands.items()
    }
    return {
        "n": len(settled_main),
        "hit_rate": hits / len(settled_main),
        "avg_decision_odds": sum(odds) / len(odds) if odds else None,
        "popularity_bands_note": (
            "人気の実値ではなく decision_odds 昇順順位で代用 (SPEC §3の代用規定)"
        ),
        "popularity_bands_by_odds_rank": band_report,
        "max_drawdown_yen": max_drawdown,
        "daily": daily,
    }


def _reference_only_summary(conn: sqlite3.Connection, policy_version: str) -> dict[str, Any]:
    rows = load_rows(conn, policy_version)
    main = [r for r in rows if not r.is_control]
    control = [r for r in rows if r.is_control]
    by_status_main = _split_by_status(main)
    by_status_control = _split_by_status(control)
    main_settled = by_status_main.get("settled", [])
    control_settled = by_status_control.get("settled", [])
    try:
        roi_main = _roi(main_settled)
    except GateError as exc:
        roi_main = {"error": str(exc)}
    try:
        roi_control = _roi(control_settled)
    except GateError as exc:
        roi_control = {"error": str(exc)}
    return {
        "policy_version": policy_version,
        "estimation_only": True,
        "migration_eligible": False,
        "disclaimer": "検定ではなく参考値 (estimation-only)。実マネー移行の資格はない (SPEC-T70b)。",
        "population_main_by_status": {k: len(v) for k, v in by_status_main.items()},
        "population_control_by_status": {k: len(v) for k, v in by_status_control.items()},
        "main_settled_roi": roi_main,
        "control_settled_roi": roi_control,
    }


def evaluate_conditions_passed(
    *,
    n_passed: bool,
    roi_main: float | None,
    bootstrap_lower5: float | None,
    roi_control: float | None,
    roi_excl_max: float | None,
) -> dict[int, bool]:
    """SPEC-T70 §5 の5条件の成否を、既に計算済みの数値から純粋に判定する
    (計算そのものはしない)。境界値テスト用に副作用のない比較だけを切り出した
    もの。各比較は閾値を含む (>=)。条件4のみ厳密な「上回る」(>) で不等号。"""

    return {
        1: bool(n_passed),
        2: roi_main is not None and roi_main >= ROI_MIN,
        3: bootstrap_lower5 is not None and bootstrap_lower5 >= BOOTSTRAP_LOWER5_MIN,
        4: (
            roi_main is not None and roi_control is not None and roi_main > roi_control
        ),
        5: roi_excl_max is not None and roi_excl_max >= MAX_PAYOUT_EXCLUDED_ROI_MIN,
    }


def compute_gate_report(conn: sqlite3.Connection, *, policy_version: str = DEFAULT_POLICY) -> dict[str, Any]:
    as_of_date = load_global_max_date(conn)
    rows = load_rows(conn, policy_version)
    main_rows = [r for r in rows if not r.is_control]
    control_rows = [r for r in rows if r.is_control]
    by_status_main = _split_by_status(main_rows)
    by_status_control = _split_by_status(control_rows)

    pending_main = by_status_main.get("pending", [])
    pending_control = by_status_control.get("pending", [])
    pending_rows_detail = categorize_pending(list(pending_main) + list(pending_control), as_of_date)

    n_eligible_main_pre = [r for r in main_rows if r.status in N_ELIGIBLE_STATUSES]
    n_pre = {
        "event_days": len({r.date for r in n_eligible_main_pre}),
        "main_bets": len(n_eligible_main_pre),
    }

    main_settled = by_status_main.get("settled", [])
    control_settled = by_status_control.get("settled", [])

    n_post = {
        "event_days": len({r.date for r in main_settled}),
        "main_bets": len(main_settled),
    }

    population_summary = {
        "main_by_status": {k: len(v) for k, v in by_status_main.items()},
        "control_by_status": {k: len(v) for k, v in by_status_control.items()},
    }

    main_population_for_share = len(n_eligible_main_pre)
    pending_share = (
        len(pending_main) / main_population_for_share if main_population_for_share else None
    )

    # INVALID (即時停止): settled行なのにpayout_yenがNULL (schema CHECKで原則
    # 防がれるがfail-closedで二重確認)。データ破損の疑いがあるため、以降の
    # 数値は一切信頼できず計算もしない。
    blocking_reasons: list[str] = []
    for label, group in (("main", main_settled), ("control", control_settled)):
        for row in group:
            if row.payout_yen is None:
                blocking_reasons.append(
                    f"status='settled' row with payout_yen=NULL in {label} "
                    f"(race_id={row.race_id}, date={row.date})"
                )

    report: dict[str, Any] = {
        "policy_version": policy_version,
        "as_of_date": as_of_date,
        "population": population_summary,
        "pending_detail": {
            "count_main": len(pending_main),
            "count_control": len(pending_control),
            "share_of_main_population_pre_exclusion": pending_share,
            "warning_pending_share_high": (
                pending_share is not None and pending_share > PENDING_SHARE_WARN
            ),
            "rows": pending_rows_detail,
        },
        "n_check": {
            "pre_exclusion": n_pre,
            "post_exclusion_settled_only": n_post,
            "threshold": {
                "event_days_min": N_EVENT_DAYS_MIN, "main_bets_min": N_MAIN_BETS_MIN,
            },
            "passed": (
                n_post["event_days"] >= N_EVENT_DAYS_MIN
                and n_post["main_bets"] >= N_MAIN_BETS_MIN
            ),
        },
    }

    report["migration_approval_disclaimer"] = (
        "GATE_PASSであっても実マネー移行の承認ではない。移行は別途ユーザー承認"
        "+規約リスク調査 (T70 P4) を要する。"
    )

    if blocking_reasons:
        report["conditions"] = None
        report["machine_judgment"] = "INVALID"
        report["invalid_reasons"] = blocking_reasons
        report["reviewer_adjudication"] = None
        return report

    # 以降は、INVALIDになる場合でも「どの数値がどう不成立だったか」を報告する
    # ため5条件を診断的に計算する (SPEC §6「5条件それぞれの数値と成否」)。
    # 最終 machine_judgment は fail-closed のINVALID規則が優先する。
    invalid_reasons: list[str] = []

    # INVALID: 精算済みだけでn条件を満たさない (SPEC §2-3)。
    if n_post["event_days"] < N_EVENT_DAYS_MIN or n_post["main_bets"] < N_MAIN_BETS_MIN:
        invalid_reasons.append(
            "settled-only population does not meet the n condition after excluding "
            f"pending rows (event_days={n_post['event_days']} < {N_EVENT_DAYS_MIN} "
            f"and/or main_bets={n_post['main_bets']} < {N_MAIN_BETS_MIN}; "
            f"pre-exclusion was event_days={n_pre['event_days']}, main_bets={n_pre['main_bets']})"
        )

    # INVALID: 投資額0。
    main_stake_sum = sum(r.stake_yen for r in main_settled)
    if main_settled and main_stake_sum <= 0:
        invalid_reasons.append("settled main population has total stake_yen <= 0")

    roi_main = _roi(main_settled)
    roi_control = _roi(control_settled)

    bootstrap: dict[str, Any] | None
    try:
        bootstrap = block_bootstrap_lower_quantile(
            main_settled, n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED,
        )
    except GateError as exc:
        bootstrap = None
        invalid_reasons.append(f"single-arm bootstrap could not be computed: {exc}")

    pairing = pair_control_rows(main_settled, control_settled)

    paired_diff: dict[str, Any] | None
    try:
        blocks_main = _date_blocks(main_settled)
        blocks_control = _date_blocks(control_settled)
        if not blocks_main or set(blocks_main) != set(blocks_control):
            raise GateError(
                "main and control settled populations do not share the same "
                "event-day blocks for the paired bootstrap "
                f"(main_only={sorted(set(blocks_main) - set(blocks_control))}, "
                f"control_only={sorted(set(blocks_control) - set(blocks_main))})"
            )
        result = paired_block_bootstrap(
            _roi_of_rows, blocks_main, blocks_control,
            n_resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED,
        )
        paired_diff = {
            "observed_difference": result.observed_difference,
            "ci95": [result.ci_low, result.ci_high],
            "n_resamples": result.n_resamples,
            "n_blocks": result.n_blocks,
            "seed": result.seed,
        }
    except GateError as exc:
        paired_diff = None
        invalid_reasons.append(f"paired bootstrap (main vs control) could not be computed: {exc}")

    excluded_rows, excluded_row = exclude_max_payout(main_settled)
    roi_excl_max = _roi(excluded_rows) if main_settled else {"n": 0, "stake_yen": 0, "payout_yen": 0, "roi": None}

    passed = evaluate_conditions_passed(
        n_passed=report["n_check"]["passed"],
        roi_main=roi_main["roi"],
        bootstrap_lower5=bootstrap["lower_quantile_value"] if bootstrap is not None else None,
        roi_control=roi_control["roi"],
        roi_excl_max=roi_excl_max["roi"],
    )
    cond1_passed, cond2_passed, cond3_passed, cond4_passed, cond5_passed = (
        passed[1], passed[2], passed[3], passed[4], passed[5],
    )

    conditions = [
        {
            "id": 1, "name": "n: >=12 event days and >=30 main bets (settled-only)",
            "observed": n_post, "threshold": report["n_check"]["threshold"],
            "passed": cond1_passed,
        },
        {
            "id": 2, "name": "main fukusho ROI point estimate >= 100%",
            "observed_roi": roi_main["roi"], "threshold": ROI_MIN,
            "main_settled_n": roi_main["n"], "main_stake_yen": roi_main["stake_yen"],
            "main_payout_yen": roi_main["payout_yen"], "passed": cond2_passed,
        },
        {
            "id": 3, "name": "event-day block bootstrap (2000 resamples, seed=70): "
                             "lower 5th percentile of ROI >= 85%",
            "bootstrap": bootstrap, "threshold": BOOTSTRAP_LOWER5_MIN, "passed": cond3_passed,
        },
        {
            "id": 4, "name": "main ROI exceeds control ROI (selected-race favorite fukusho)",
            "roi_main": roi_main["roi"], "roi_control": roi_control["roi"],
            "control_settled_n": roi_control["n"],
            "paired_bootstrap_difference": paired_diff,
            "control_pairing": pairing,
            "passed": cond4_passed,
        },
        {
            "id": 5, "name": "excluding the single largest payout, ROI >= 90%",
            "excluded_row": (
                {
                    "race_id": excluded_row.race_id, "date": excluded_row.date,
                    "horse_number": excluded_row.horse_number,
                    "payout_yen": excluded_row.payout_yen, "stake_yen": excluded_row.stake_yen,
                }
                if excluded_row is not None else None
            ),
            "observed_roi": roi_excl_max["roi"], "threshold": MAX_PAYOUT_EXCLUDED_ROI_MIN,
            "passed": cond5_passed,
        },
    ]
    report["conditions"] = conditions
    report["reviewer_adjudication"] = None

    if invalid_reasons:
        report["machine_judgment"] = "INVALID"
        report["invalid_reasons"] = invalid_reasons
        report["conditions_note"] = (
            "上記conditionsはINVALID判定の根拠を示す診断値であり、移行ゲートの"
            "成否判定そのものではない (fail-closedのINVALID規則が優先)。"
        )
        return report

    all_passed = all(c["passed"] for c in conditions)
    report["machine_judgment"] = "GATE_PASS" if all_passed else "GATE_FAIL"
    report["failed_condition_ids"] = [c["id"] for c in conditions if not c["passed"]]
    report["auxiliary_metrics_not_used_for_judgment"] = _auxiliary_report(main_settled)
    return report


def build_full_report(db_path: Path, *, policy_version: str = DEFAULT_POLICY) -> dict[str, Any]:
    started = time.monotonic()
    conn = _connect_readonly(db_path)
    try:
        gate = compute_gate_report(conn, policy_version=policy_version)
        reference_only = {
            policy: _reference_only_summary(conn, policy) for policy in REFERENCE_ONLY_POLICIES
        }
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generator": "backtest_t70c_gate.py",
        "spec": "docs/codex/SPEC-T70c-gate-aggregation.md",
        "db_path": str(db_path),
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "pending_share_warn_threshold": PENDING_SHARE_WARN,
        "elapsed_seconds": elapsed,
        "gate": gate,
        "reference_only": reference_only,
        "note": (
            "本ファイルはT70c集計ハーネスの出力。採否・実マネー移行の是非の裁定は"
            "行っていない (machine_judgmentのみ機械的に算出。reviewer_adjudication"
            "は常にnull)。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--policy", type=str, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)

    report = build_full_report(args.db, policy_version=args.policy)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    import json
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    print(f"wrote {args.output} (machine_judgment={report['gate']['machine_judgment']}, "
          f"elapsed={report['elapsed_seconds']:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
