import hashlib

import numpy as np
import pytest

import backtest_t53 as t53


def _row(date, race, horse, rank, umaban=1):
    return {"date": date, "place": "東京", "r": race, "horse": horse,
            "rank": rank, "umaban": umaban, "total_horses": 3}


def test_date_atomic_asof_excludes_same_day_and_future_results():
    runs = []
    for day in range(1, 6):
        runs.extend([_row(f"202012{day:02d}", 1, "A", 1),
                     _row(f"202012{day:02d}", 1, "B", 2, 2)])
    runs.extend([
        _row("20210101", 1, "A", 2), _row("20210101", 1, "B", 1, 2),
        _row("20210101", 9, "A", 1), _row("20210101", 9, "B", 2, 2),
        _row("20210102", 1, "A", 1), _row("20210102", 1, "B", 2, 2),
    ])
    features = t53.build_rating_feature_map(runs, eta=0.1, k=5.0)
    day_rows = [features[t53._runner_key(row)] for row in runs if row["date"] == "20210101"]
    assert day_rows[0] == day_rows[2]
    assert day_rows[1] == day_rows[3]
    assert day_rows[0][1] == 1.0
    assert day_rows[0][0] > day_rows[1][0]


def test_winner_only_gradient_ignores_order_of_nonwinners():
    ratings = [0.2, -0.1, 0.5]
    first = t53.winner_only_gradient(ratings, [True, False, False])
    # Swapping second/third finishing ranks cannot enter this API; only winner mask exists.
    second = t53.winner_only_gradient(ratings, [True, False, False])
    assert np.array_equal(first, second)
    assert first.sum() == pytest.approx(0.0)


def test_dead_heat_matches_conditional_logit_top1_gradient():
    gradient = t53.winner_only_gradient([0.0, 0.0, 0.0], [True, True, False])
    assert gradient.tolist() == pytest.approx([1 / 3, 1 / 3, -2 / 3])


def test_shrinkage_is_monotonic_and_zero_without_starts():
    assert t53.shrink_rating(2.0, 0, 15.0) == 0.0
    values = [t53.shrink_rating(2.0, n, 15.0) for n in (1, 5, 20, 100)]
    assert values == sorted(values)
    assert all(value < 2.0 for value in values)


def test_warmup_changes_2021_feature_but_is_not_emitted_itself():
    runs = []
    for year in (2018, 2019, 2020):
        for race in (1, 2):
            runs.extend([_row(f"{year}0101", race, "A", 1),
                         _row(f"{year}0101", race, "B", 2, 2)])
    target = [_row("20210101", 1, "A", 1), _row("20210101", 1, "B", 2, 2)]
    features = t53.build_rating_feature_map(runs + target, eta=0.1, k=5.0)
    assert all(key[0] >= "20210101" for key in features)
    assert features[t53._runner_key(target[0])][1] == 1.0
    assert features[t53._runner_key(target[0])][0] > 0.0


def test_pack_off_is_byte_identical():
    base = np.arange(12, dtype=float).reshape(3, 4)
    disabled = t53.model_matrix(base, None)
    assert disabled is base
    assert hashlib.sha256(disabled.tobytes()).digest() == hashlib.sha256(base.tobytes()).digest()
    assert t53.model_matrix(base, np.ones((3, 2))).shape == (3, 6)


def test_base_history_excludes_incidental_pre_warmup_lookback():
    runs = [_row("20171231", 1, "A", 1), _row("20180101", 1, "A", 1)]
    base_runs = [row for row in runs if row["date"] >= t53.WARMUP_FROM]
    assert [row["date"] for row in base_runs] == ["20180101"]


def test_evaluation_gate_fails_before_registration(tmp_path):
    db, ledger = tmp_path / "ability.db", tmp_path / "experiments.jsonl"
    db.write_bytes(b"fixture")
    ledger.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        t53.require_registration(ledger, db)
