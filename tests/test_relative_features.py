import numpy as np

import backtest_ml


def _base_row(**values):
    row = np.zeros(len(backtest_ml.FEATURES), dtype=float)
    for name, value in values.items():
        row[backtest_ml.FEATURES.index(name)] = value
    return row


def _mask(**values):
    names = ("tfeat", "j_pts", "f_pts", "sire_pts", "kinryo", "n_prior")
    return {name: bool(values.get(name, True)) for name in names}


def test_relative_features_two_races_ties_missing_and_centering():
    rows = np.asarray([
        _base_row(tfeat=10, j_pts=2, f_pts=3, sire_pts=1, kinryo=57, n_prior=4),
        _base_row(tfeat=10, j_pts=1, f_pts=2, sire_pts=2, kinryo=55, n_prior=2),
        _base_row(tfeat=4, j_pts=0, f_pts=1, sire_pts=3, kinryo=53, n_prior=3),
        _base_row(tfeat=8, j_pts=4, f_pts=9, sire_pts=0, kinryo=56, n_prior=1),
        # tfeat/j_pts/kinryo は既存モデルの補完値が入っていてもmaskで欠損扱いにする。
        _base_row(tfeat=0, j_pts=0, f_pts=7, sire_pts=0, kinryo=55, n_prior=3),
    ])
    keys = [("20250101", "東京", 9)] * 3 + [("20250101", "東京", 10)] * 2
    masks = [
        _mask(), _mask(), _mask(),
        _mask(sire_pts=False),
        _mask(tfeat=False, j_pts=False, sire_pts=False, kinryo=False),
    ]

    extended = backtest_ml._append_relative_features(rows, keys, masks)
    rel = extended[:, len(backtest_ml.FEATURES):]

    assert extended.shape == (5, len(backtest_ml.FEATURES) + 8)
    assert np.array_equal(extended[:, :len(backtest_ml.FEATURES)], rows)
    assert np.allclose(rel[:3, 0], [0.75, 0.75, 0.0])  # 同着は平均順位
    assert np.allclose(rel[:3, 1], [0.0, 0.0, -6.0])
    assert np.allclose(rel[:3, 2], [0.0, 0.0, -6.0])
    assert rel[3, 0] == 1.0  # 取得可能な馬が1頭なら最良順位1
    assert np.allclose(rel[3:, 1:3], 0.0)  # 1頭のみ・欠損馬はいずれも差0
    assert rel[4, 3] == 0.0
    assert rel[4, 6] == 0.0

    # 各偏差列は、取得可能な馬についてレース内合計0になる。
    assert np.allclose(rel[:3, 3:].sum(axis=0), 0.0)
    assert np.isclose(rel[3:, 4].sum(), 0.0)
    assert np.isclose(rel[3:, 7].sum(), 0.0)


def test_default_feature_contract_and_matrix_selection_are_unchanged():
    assert backtest_ml.active_feature_names() == backtest_ml.FEATURES
    assert len(backtest_ml.FEATURES) == 21

    base = np.arange(2 * len(backtest_ml.FEATURES), dtype=float).reshape(
        2, len(backtest_ml.FEATURES)
    )
    selected = backtest_ml.feature_matrix(base, ["ln_odds", "tfeat"])
    assert np.array_equal(selected, base[:, [5, 2]])

    names = backtest_ml.active_feature_names(relative=True)
    extended = np.hstack((base, np.ones((2, len(backtest_ml.RELATIVE_FEATURES)))))
    selected_relative = backtest_ml.feature_matrix(
        extended, ["tfeat", "tfeat_rank", "n_prior_dev"]
    )
    assert names[-8:] == backtest_ml.RELATIVE_FEATURES
    assert np.array_equal(selected_relative[:, 0], base[:, 2])
    assert np.array_equal(selected_relative[:, 1:], np.ones((2, 2)))


def test_tfeat_rank_uses_official_field_denominator_when_values_are_missing():
    rows = np.asarray([
        _base_row(tfeat=10),
        _base_row(tfeat=5),
        *[_base_row(tfeat=0) for _ in range(6)],
    ])
    key = ("20250101", "東京", 9)
    masks = [
        _mask(), _mask(),
        *[_mask(tfeat=False) for _ in range(6)],
    ]

    extended = backtest_ml._append_relative_features(rows, [key] * 8, masks)
    rank = extended[:, len(backtest_ml.FEATURES)]

    assert rank[0] == 1.0
    assert rank[1] == 6.0 / 7.0
    assert np.array_equal(rank[2:], np.zeros(6))
