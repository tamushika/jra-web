import hashlib
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest_t65_cell_audit as t65


def _meta(**kwargs):
    base = {
        "date": "20240107", "place": "中山", "race_no": 5, "track_type": "芝",
        "condition": "良", "distance": 1600, "race_class": "1勝クラス",
        "race_name": "1勝クラス", "field_size": 12, "is_handicap": False,
        "is_fillies_only": False, "max_age": 4,
    }
    base.update(kwargs)
    return base


def test_cell_classification_is_deterministic_and_matches_spec():
    assert t65.classify_cells(_meta(condition="重")) == ("C1",)
    assert t65.classify_cells(
        _meta(track_type="ダート", condition="不", distance=1400)) == ("C2",)
    assert t65.classify_cells(
        _meta(place="小倉", race_class="2勝クラス")) == ("C3",)
    assert t65.classify_cells(_meta(
        race_class="3勝クラス", field_size=14, is_handicap=True,
        race_name="ファイＨ･3勝")) == ("C4",)
    assert t65.classify_cells(_meta(
        date="20241215", is_fillies_only=True, race_name="1勝クラス・牝",
    )) == ("C5",)
    assert t65.classify_cells(
        _meta(date="20240203", race_class="未勝利", max_age=3)) == ("C6",)
    assert t65.classify_cells(_meta(
        date="20240707", place="新潟", race_class="新馬", max_age=2)) == ("C7",)
    # 重複許容: ローカル2勝かつ道悪芝
    cells = t65.classify_cells(_meta(
        place="福島", race_class="2勝クラス", condition="不"))
    assert set(cells) == {"C1", "C3"}
    # 対象外
    assert t65.classify_cells(_meta()) == ()


def test_d_definition_matches_market_minus_model_winner_logloss():
    rows = []
    for i in range(8):
        rows.append({
            "date": "20240107", "place": "中山", "r": 9, "umaban": i + 1,
            "horse": f"馬{i}", "total_horses": 8, "popularity": i + 1,
            "rank": 1 if i == 0 else i + 1, "track_type": "芝",
            "condition": "良", "distance": 1600, "race_class": "1勝クラス",
            "race_name": "1勝クラス",
            "win_pay": "320" if i == 0 else f"({2.0 + i:.1f})",
            "age": 4,
        })
    key = ("20240107", "中山", 9)
    scores = np.linspace(1.0, 0.3, 8)
    labels = np.asarray([1] + [0] * 7)
    obs = t65.build_observations(
        scores, labels, [key] * 8, rows, temperature=1.0)
    assert len(obs) == 1
    model = t65._softmax(scores, 1.0)
    odds = [t65.parse_final_odds(row["win_pay"], row["rank"]) for row in rows]
    market = np.asarray([1.0 / float(o) for o in odds])
    market /= market.sum()
    expected = (-math.log(market[0])) - (-math.log(model[0]))
    assert obs[0]["d"] == pytest.approx(expected, rel=1e-12)


def _synthetic_observations(effect: float, n_days: int = 60):
    rng = np.random.default_rng(7)
    observations = []
    for day in range(n_days):
        date = f"202401{day % 28 + 1:02d}" if day < 28 else f"202502{day % 28 + 1:02d}"
        period = "2024" if day < 28 else "2025"
        for race in range(6):
            in_family = race < 2
            cells = ()
            if in_family:
                cells = ("C1",)
            elif race == 2:
                cells = ("C6",)  # 経験浅family (効果なし)
            elif race == 3:
                cells = ("C3",)  # ローカルfamily (効果なし)
            d = float(rng.normal(effect if in_family else 0.0, 0.05))
            observations.append({
                "key": (date, "中山", race + 1), "date": date, "period": period,
                "d": d, "cells": cells,
                "winner_popularity": 1 + race % 9,
                "model_win_in_topk": (True, True, True, True),
                "market_win_in_topk": (False, True, True, True),
                "meta": _meta(date=date),
            })
    # 2026H1側にも同構造を複製 (3期間そろえる)
    extra = []
    for obs in observations[:120]:
        clone = dict(obs)
        clone["date"] = "2026" + obs["date"][4:]
        clone["period"] = "2026H1"
        extra.append(clone)
    return observations + extra


def test_gate_passes_large_effect_and_rejects_null():
    strong = t65.gate_statistics(
        _synthetic_observations(0.30), resamples=400, seed=1)
    assert strong["道悪"]["pass_dual_maxt"] is True
    null = t65.gate_statistics(
        _synthetic_observations(0.0), resamples=400, seed=1)
    assert null["道悪"]["pass_dual_maxt"] is False
    # 他familyはメンバー0でエラーになるべきではなく、そもそも空 → T65Error
    # (_synthetic_observationsはC1のみ使うため経験浅/ローカルfamilyは空)


def test_gate_statistics_is_deterministic_under_seed():
    observations = _synthetic_observations(0.05)
    a = t65.gate_statistics(observations, resamples=300, seed=42)
    b = t65.gate_statistics(observations, resamples=300, seed=42)
    assert a == b


def test_registration_gate_fails_closed_on_spec_tamper(tmp_path):
    spec_copy = tmp_path / "spec.md"
    spec_copy.write_bytes(
        (ROOT / "docs" / "codex" / "SPEC-T65-condition-cell-audit.md").read_bytes()
        + b"\ntampered\n")
    with pytest.raises(t65.T65Error, match="spec_changed"):
        t65.require_registration(spec_path=spec_copy)
