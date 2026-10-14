"""SPEC-T70c: 仮想運用ゲート集計ハーネスのテスト。

合成データのみを使用する (本番DB・ability.db・ネットワークは一切使わない)。
`backtest_t70c_gate.py` が読み取り専用で `virtual_bets` を集計し、事前登録済み
5条件 (SPEC-T70 §5) を機械的に判定することを検証する。本テストは採否・移行の
是非には関与しない (reviewer_adjudicationは常にnullであることのみ確認)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import backtest_t70c_gate as gate
from api.logging_store import LoggingStore


# ─── fixtures ──────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "logging.db"
    LoggingStore(db_path).initialize()
    return db_path


def _insert(conn: sqlite3.Connection, *, seq: int, policy_version: str, race_id: str,
            date: str, horse_number: int, stake_yen: int, status: str,
            is_control: int = 0, payout_yen: int | None = None,
            bet_type: str = "fukusho", decision_odds: float | None = 2.0,
            decision_prob: float | None = 0.4) -> None:
    settled_at = "2026-01-01T00:00:00Z" if status in ("settled", "refunded") else None
    conn.execute(
        "INSERT INTO virtual_bets (idempotency_key, policy_version, race_id, date, "
        "bet_type, horse_number, stake_yen, decided_at, cutoff_source, decision_odds, "
        "decision_prob, is_control, status, payout_yen, settled_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{policy_version}:{race_id}:{bet_type}:{seq}:{'control' if is_control else 'main'}",
            policy_version, race_id, date, bet_type, horse_number, stake_yen,
            "2026-01-01T00:00:00Z", None, decision_odds, decision_prob, is_control,
            status, payout_yen, settled_at, "2026-01-01T00:00:00Z",
        ),
    )


def _seed_flat_population(conn: sqlite3.Connection, *, n_dates: int = 15,
                           bets_per_date: int = 2, main_ratio: float = 1.0,
                           control_ratio: float = 0.9, stake: int = 1000,
                           policy_version: str = "v1") -> None:
    """全ベットが同一payout/stake比になる母集団 (bootstrapの分散が0になるため
    下側5%点が点推定と一致し、boundary の数値検証に向く)。"""

    seq = 0
    for day in range(n_dates):
        race_date = f"2026{(day % 12) + 1:02d}{(day % 27) + 1:02d}"
        for b in range(bets_per_date):
            seq += 1
            race_id = f"T70C:{race_date}:{seq}"
            _insert(
                conn, seq=seq, policy_version=policy_version, race_id=race_id,
                date=race_date, horse_number=1, stake_yen=stake, status="settled",
                is_control=0, payout_yen=round(stake * main_ratio),
            )
            _insert(
                conn, seq=seq, policy_version=policy_version, race_id=race_id,
                date=race_date, horse_number=2, stake_yen=stake, status="settled",
                is_control=1, payout_yen=round(stake * control_ratio),
            )
    conn.commit()


def _report(db_path: Path, policy_version: str = "v1") -> dict:
    conn = gate._connect_readonly(db_path)
    try:
        return gate.compute_gate_report(conn, policy_version=policy_version)
    finally:
        conn.close()


# ─── (1) 5条件の各境界 ───────────────────────────────────────────────────────

def test_all_five_conditions_pass_at_or_above_threshold(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        # 15開催日 x 2件 = 30件。main ROI=100%ちょうど、control=90%、
        # payout/stake比が全ベットで一定なのでbootstrap下側5%もちょうど100%。
        _seed_flat_population(conn, main_ratio=1.00, control_ratio=0.90)

    report = _report(db_path)
    assert report["machine_judgment"] == "GATE_PASS"
    assert report["reviewer_adjudication"] is None
    assert [c["passed"] for c in report["conditions"]] == [True, True, True, True, True]
    assert report["conditions"][1]["observed_roi"] == pytest.approx(1.00)
    # 最大払戻除外後も全ベットの比が同一のため90%以上を維持
    assert report["conditions"][4]["observed_roi"] == pytest.approx(1.00)


def test_condition2_roi_boundary_999_fails_1000_passes():
    # 条件2単体を純粋関数で検証: 閾値はROI>=100%。99.9%は不成立、100%はちょうど成立。
    passed_999 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=0.999, bootstrap_lower5=0.90, roi_control=0.80,
        roi_excl_max=0.95,
    )
    passed_1000 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=1.000, bootstrap_lower5=0.90, roi_control=0.80,
        roi_excl_max=0.95,
    )
    assert passed_999[2] is False
    assert passed_1000[2] is True
    # 他の条件は変化しない (単一条件の境界だけを動かしたことの確認)
    for cond_id in (1, 3, 4, 5):
        assert passed_999[cond_id] == passed_1000[cond_id] is True


def test_condition3_bootstrap_lower5_boundary_849_fails_850_passes():
    passed_849 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=1.10, bootstrap_lower5=0.849, roi_control=0.80,
        roi_excl_max=0.95,
    )
    passed_850 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=1.10, bootstrap_lower5=0.850, roi_control=0.80,
        roi_excl_max=0.95,
    )
    assert passed_849[3] is False
    assert passed_850[3] is True
    for cond_id in (1, 2, 4, 5):
        assert passed_849[cond_id] == passed_850[cond_id] is True


def test_condition4_control_tie_fails_strictly_exceeding_passes():
    tied = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=0.90, bootstrap_lower5=0.90, roi_control=0.90,
        roi_excl_max=0.95,
    )
    exceeding = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=0.9001, bootstrap_lower5=0.90, roi_control=0.90,
        roi_excl_max=0.95,
    )
    assert tied[4] is False  # 同値は「上回る」を満たさない
    assert exceeding[4] is True


def test_condition5_exclude_max_payout_roi_boundary_899_fails_900_passes():
    passed_899 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=1.10, bootstrap_lower5=0.90, roi_control=0.80,
        roi_excl_max=0.899,
    )
    passed_900 = gate.evaluate_conditions_passed(
        n_passed=True, roi_main=1.10, bootstrap_lower5=0.90, roi_control=0.80,
        roi_excl_max=0.900,
    )
    assert passed_899[5] is False
    assert passed_900[5] is True


def test_end_to_end_gate_fail_lists_failing_condition_without_invalidating_others(tmp_path):
    """n条件は満たすが本体ROIが100%未満の場合、INVALIDにはならずGATE_FAILで
    不成立の条件 (2) が列挙される。"""

    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, main_ratio=0.95, control_ratio=0.80)
    report = _report(db_path)
    assert report["machine_judgment"] == "GATE_FAIL"
    assert 2 in report["failed_condition_ids"]
    assert report["conditions"][0]["passed"] is True  # n条件は成立
    assert report["conditions"][3]["passed"] is True  # 対照(80%)は上回っている


# ─── (2) 保留の除外とn条件の再判定でINVALID ──────────────────────────────────

def test_pending_exclusion_drops_below_n_threshold_is_invalid(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        # settledは12開催日・30件のちょうど閾値を満たすが、うち数件をpendingに
        # すると settled-only では閾値を割り込む。
        _seed_flat_population(conn, n_dates=12, bets_per_date=3, main_ratio=1.0,
                               control_ratio=0.9)
        # 1件をpendingへ変更 (1開催日分の片方のmainベットのみ)。
        conn.execute(
            "UPDATE virtual_bets SET status='pending', payout_yen=NULL, settled_at=NULL "
            "WHERE is_control=0 AND date=(SELECT MIN(date) FROM virtual_bets)"
        )
        conn.commit()

    report = _report(db_path)
    assert report["machine_judgment"] == "INVALID"
    assert any("n condition" in r for r in report["invalid_reasons"])
    assert report["n_check"]["post_exclusion_settled_only"]["main_bets"] < 36
    assert report["pending_detail"]["count_main"] >= 1
    # 診断用に5条件の数値自体は出力される (判定の根拠ではないことを明記)
    assert report["conditions"] is not None
    assert report["conditions_note"] is not None


def test_pending_share_high_warning_flag(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        # 30件中、十分な余裕を持ってn条件を満たしたまま3割をpendingにする。
        _seed_flat_population(conn, n_dates=20, bets_per_date=3, main_ratio=1.0,
                               control_ratio=0.9)
        conn.execute(
            "UPDATE virtual_bets SET status='pending', payout_yen=NULL, settled_at=NULL "
            "WHERE is_control=0 AND date IN ("
            "  SELECT DISTINCT date FROM virtual_bets WHERE is_control=0 ORDER BY date LIMIT 7"
            ")"
        )
        conn.commit()

    report = _report(db_path)
    # 60件中 7*3=21件がpending -> 35% > 20%
    assert report["pending_detail"]["warning_pending_share_high"] is True


# ─── (3) settledなのにpayout_yenがNULLならINVALID ────────────────────────────

def test_settled_row_with_null_payout_is_invalid(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, main_ratio=1.0, control_ratio=0.9)
        # CHECK制約を一時的に回避してNULL payoutのsettled行を作る
        # (schemaのCHECKはsettled/refunded以外の値遷移を縛るが、ここでは直接
        # UPDATEで意図的にデータ破損を再現する。SQLiteのCHECK制約は書き込み
        # 時に評価されるため、INSERT時点では通常防がれる異常系)。
        conn.execute("PRAGMA ignore_check_constraints = 1")
        conn.execute(
            "UPDATE virtual_bets SET payout_yen=NULL WHERE is_control=0 "
            "AND status='settled' AND date=(SELECT MIN(date) FROM virtual_bets)"
        )
        conn.commit()

    report = _report(db_path)
    assert report["machine_judgment"] == "INVALID"
    assert report["conditions"] is None
    assert any("payout_yen=NULL" in r for r in report["invalid_reasons"])


# ─── (4) bootstrapの再現性 (seed=70) ─────────────────────────────────────────

def test_bootstrap_is_reproducible_with_fixed_seed(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, main_ratio=1.0, control_ratio=0.9)
    report1 = _report(db_path)
    report2 = _report(db_path)
    b1 = report1["conditions"][2]["bootstrap"]
    b2 = report2["conditions"][2]["bootstrap"]
    assert b1 == b2
    assert b1["seed"] == 70
    assert b1["n_resamples"] == 2000


def test_bootstrap_lower_quantile_direct_reproducibility():
    rows = [
        gate.BetRow(
            policy_version="v1", race_id=f"r{i}", date=f"2026010{i % 9 + 1}",
            bet_type="fukusho", horse_number=1, stake_yen=1000, decision_odds=2.0,
            decision_prob=0.4, is_control=0, status="settled",
            payout_yen=1000 if i % 2 == 0 else 500, settled_at="2026-01-01T00:00:00Z",
        )
        for i in range(20)
    ]
    r1 = gate.block_bootstrap_lower_quantile(rows, n_resamples=2000, seed=70)
    r2 = gate.block_bootstrap_lower_quantile(rows, n_resamples=2000, seed=70)
    assert r1 == r2


# ─── (5) P5/P3は判定対象外で参考値のみ ───────────────────────────────────────

def test_p5_p3_are_reference_only_and_not_gated(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, main_ratio=1.0, control_ratio=0.9, policy_version="v1")
        _seed_flat_population(conn, n_dates=12, bets_per_date=2, main_ratio=1.2,
                               control_ratio=1.1, stake=500, policy_version="p5-v1")
        _seed_flat_population(conn, n_dates=5, bets_per_date=1, main_ratio=0.5,
                               control_ratio=0.5, stake=500, policy_version="p3-v1")
        conn.commit()

    full = gate.build_full_report(db_path, policy_version="v1")
    assert full["gate"]["policy_version"] == "v1"
    assert "machine_judgment" in full["gate"]
    for policy in ("p5-v1", "p3-v1"):
        ref = full["reference_only"][policy]
        assert ref["estimation_only"] is True
        assert ref["migration_eligible"] is False
        assert "machine_judgment" not in ref
        assert "conditions" not in ref


# ─── (6) 対照の突合 (同一レース・同一日) ─────────────────────────────────────

def test_control_pairing_matches_same_race_and_date(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, main_ratio=1.0, control_ratio=0.9)
    report = _report(db_path)
    pairing = report["conditions"][3]["control_pairing"]
    assert pairing["fully_paired"] is True
    assert pairing["main_missing_control"] == []
    assert pairing["control_missing_main"] == []
    assert pairing["paired"] == report["conditions"][1]["main_settled_n"]


def test_control_pairing_reports_mismatch_when_control_row_missing(tmp_path):
    db_path = _make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        _seed_flat_population(conn, n_dates=12, bets_per_date=3, main_ratio=1.0,
                               control_ratio=0.9)
        # 1件の対照だけpendingにして、settled集団から外す (本体は精算済みのまま)。
        conn.execute(
            "UPDATE virtual_bets SET status='pending', payout_yen=NULL, settled_at=NULL "
            "WHERE is_control=1 AND date=(SELECT MIN(date) FROM virtual_bets) "
            "AND race_id=(SELECT race_id FROM virtual_bets WHERE is_control=1 "
            "ORDER BY date, race_id LIMIT 1)"
        )
        conn.commit()

    report = _report(db_path)
    # 日ブロック自体は残る (同じ開催日に他のcontrol settled行がある) ため
    # 開催日ブロックbootstrap自体は計算できるが、レース単位の突合は崩れる。
    pairing = report["conditions"][3]["control_pairing"]
    assert pairing["fully_paired"] is False
    assert len(pairing["main_missing_control"]) == 1
