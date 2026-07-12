import sqlite3

import numpy as np
import pytest

from fold_stats import (FoldFactorTableProvider, build_fold_factor_tables,
                        compare_with_legacy, discover_legacy_courses,
                        fold_as_of_for, rolling_stats_from, _calculate_waku)
from backtest_win5 import score_all_runners
from backtest_fold_stats import build_feature_dataset, conditional_log_loss


RUN_COLUMNS = """
    date TEXT, place TEXT, r INTEGER, horse TEXT, jockey TEXT,
    total_horses INTEGER, umaban INTEGER, rank INTEGER, track_type TEXT,
    distance INTEGER, affi TEXT, win_pay TEXT
"""


def _database(path, *, with_show=True):
    conn = sqlite3.connect(path)
    show_column = ", fukusho_pay REAL" if with_show else ""
    conn.execute(f"CREATE TABLE runs ({RUN_COLUMNS}{show_column})")
    return conn


def _insert(conn, values, *, with_show=True):
    placeholders = ",".join("?" for _ in range(13 if with_show else 12))
    conn.execute(f"INSERT INTO runs VALUES ({placeholders})", values)


def test_fold_table_strict_cutoff_rates_and_feature_entities(tmp_path):
    db = tmp_path / "ability.db"
    conn = _database(db)
    # A's pre-window run is used only to derive distance/surface change.
    _insert(conn, ("20191201", "東京", 1, "A", "旧騎手", 10, 1, 4,
                   "ダート", 1400, "美浦", "(20.0)", None))
    _insert(conn, ("20210110", "東京", 2, "A", "J1", 10, 1, 1,
                   "芝", 1600, "美浦", "250", 120))
    _insert(conn, ("20210110", "東京", 2, "B", "J2", 10, 2, 2,
                   "芝", 1600, "栗東", "(4.0)", 110))
    _insert(conn, ("20210110", "東京", 2, "C", "J1", 10, 3, 3,
                   "芝", 1600, "美浦", "(8.0)", 130))
    _insert(conn, ("20210110", "東京", 2, "D", "J3", 10, 10, 4,
                   "芝", 1600, "栗東", "(12.0)", None))
    # This outcome is in the evaluation year and must never enter the fold.
    _insert(conn, ("20250105", "東京", 3, "A", "FUTURE", 10, 1, 1,
                   "芝", 1600, "美浦", "180", 100))
    conn.commit()
    conn.close()

    tables = build_fold_factor_tables(
        db, "20241231", pedigree_by_horse={"A": "Sire One"})
    table = tables[("東京", "芝", 1600)]
    baseline = table["baseline"]
    assert (baseline["starts"], baseline["n1"], baseline["n2"],
            baseline["n3"], baseline["out"]) == (4, 1, 1, 1, 1)
    assert baseline["win_rate"] == 25.0
    assert baseline["quinella_rate"] == 50.0
    assert baseline["show_rate"] == 75.0
    assert baseline["win_roi"] == 62.5
    assert baseline["show_roi"] == 90.0
    assert "FUTURE" not in table["jockey_w"]
    assert table["jockey_w"]["J1"]["starts"] == 2
    assert table["frame"]["8枠"]["starts"] == 1
    assert table["stable_trainer"]["美浦"]["starts"] == 2
    assert table["father_w"]["SireOne"]["starts"] == 1
    assert table["distance"]["100m-200m延長"]["starts"] == 1
    assert table["surface"]["ダ→芝"]["starts"] == 1
    assert table["_meta"]["as_of"] == "20241231"
    assert table["_meta"]["strict_as_of"] is True


def test_top_entities_follow_current_top_win_count_shape(tmp_path):
    db = tmp_path / "top.db"
    conn = _database(db)
    for number, (jockey, rank) in enumerate(
            (("J0", 3), ("J1", 1), ("J2", 2)), start=1):
        _insert(conn, ("20220101", "中山", 1, f"H{number}", jockey, 8,
                       number, rank, "ダート", 1200, "美浦", "200" if rank == 1
                       else "(5.0)", 120 if rank <= 3 else None))
    conn.commit()
    conn.close()

    table = build_fold_factor_tables(
        db, "20241231", top_entity_limit=1)[("中山", "ダート", 1200)]
    assert list(table["jockey_w"]) == ["J1"]


def test_missing_show_payout_is_unknown_not_zero_roi(tmp_path):
    db = tmp_path / "no_show.db"
    conn = _database(db, with_show=False)
    _insert(conn, ("20220101", "京都", 1, "A", "J", 8, 1, 1,
                   "芝", 1400, "栗東", "300"), with_show=False)
    conn.commit()
    conn.close()

    baseline = build_fold_factor_tables(db, "20241231")[
        ("京都", "芝", 1400)]["baseline"]
    assert baseline["win_roi"] == 300.0
    assert baseline["show_roi"] is None


def test_fold_date_and_provider_lookup(tmp_path):
    assert fold_as_of_for("20250101") == "20241231"
    assert fold_as_of_for("20260630") == "20251231"
    assert rolling_stats_from("20241231") == "20200101"
    assert rolling_stats_from("20251231") == "20210101"
    with pytest.raises(ValueError):
        fold_as_of_for("2025-01-01")
    assert _calculate_waku(8, 10) == 7
    assert _calculate_waku(9, 10) == 8
    assert _calculate_waku(17, 18) == 8

    db = tmp_path / "provider.db"
    conn = _database(db)
    _insert(conn, ("20220101", "阪神", 1, "A", "J", 8, 1, 1,
                   "ダート", 1800, "栗東", "200", 110))
    conn.commit()
    conn.close()
    provider = FoldFactorTableProvider(db, "20241231")
    assert provider.course_count == 1
    assert provider("阪神", "ダ", "1800")["baseline"]["starts"] == 1
    assert provider("東京", "芝", 1600) is None
    earlier = provider.for_as_of("20211231")
    assert earlier.as_of == "20211231"
    assert earlier("阪神", "ダート", 1800) is None


def test_legacy_course_discovery_and_filter(tmp_path):
    factor_dir = tmp_path / "api" / "data_files" / "tokyo" / "factors"
    factor_dir.mkdir(parents=True)
    (factor_dir / "芝1600.csv").write_text("factor_type,entity\n", encoding="utf-8")
    (factor_dir / "README.txt").write_text("ignored", encoding="utf-8")
    assert discover_legacy_courses(tmp_path / "api") == {("東京", "芝", 1600)}


def test_legacy_comparison_reports_entity_overlap():
    fold = {
        ("東京", "芝", 1600): {
            "baseline": {"starts": 80, "win_rate": 10.0, "show_rate": 30.0},
            "jockey_w": {
                "J": {"starts": 8, "win_rate": 12.5, "show_rate": 37.5},
            },
        },
    }
    legacy = {
        "baseline": {"starts": 100, "win_rate": 9.0, "show_rate": 28.0},
        "jockey_w": {
            "J": {"starts": 10, "win_rate": 10.0, "show_rate": 30.0},
            "K": {"starts": 10, "win_rate": 0.0, "show_rate": 20.0},
        },
    }
    report = compare_with_legacy(fold, lambda *_args: legacy)
    assert report["common_courses"] == 1
    assert report["factors"]["baseline"]["mean_abs_win_rate_pp"] == 1.0
    jockey = report["factors"]["jockey_w"]
    assert jockey["matched_rows"] == 1
    assert jockey["legacy_rows"] == 2
    assert jockey["mean_abs_show_rate_pp"] == 7.5
    assert jockey["mean_start_ratio"] == 0.8


def test_win5_scorer_uses_injected_provider(monkeypatch):
    monkeypatch.setattr(
        "backtest_win5.scoring.load_factor_table",
        lambda *_args: pytest.fail("legacy table loader must not be called"),
    )
    table = {
        "baseline": {"win_rate": 10.0},
        "jockey_w": {
            "J": {"starts": 20, "win_rate": 20.0,
                  "win_roi": None, "show_roi": None},
        },
        "frame": {},
    }
    calls = []

    def provider(place, surface, distance):
        calls.append((place, surface, distance))
        return table

    prior = {
        "horse": "A", "date": "20240101", "r": 1, "rank": 4, "place": "中山",
        "track_type": "芝", "distance": 1600, "race_class": "1勝クラス",
        "time_sec": None, "condition": "良", "agari_ratio": None,
    }
    current = {
        "horse": "A", "date": "20250101", "r": 1, "rank": 1, "place": "中山",
        "track_type": "芝", "distance": 1600, "race_class": "1勝クラス",
        "time_sec": None, "condition": "良", "agari_ratio": None,
        "jockey": "J", "umaban": 1, "total_horses": 8, "win_pay": "200",
    }
    cfg = {
        "params": {
            "scale_k": 100, "clamp_min": -8.0, "clamp_max": 12.0,
            "shrinkage_n0": 20, "min_starts": 5,
            "roi_bonus": {"show_roi_threshold": 999, "win_roi_threshold": 999,
                          "points": 0},
            "market": {"odds_k": 0},
            "ability": {
                "pos_factor": {"1": 1.0}, "time_k": 1.0, "class_k": 0.0,
                "agari_best_ratio": 0.3, "agari_bonus": 0.0,
            },
        },
        "weights": {"jockey_w": 1.0, "frame": 0.0},
    }
    races = score_all_runners(
        [prior, current], "20250101", cfg,
        factor_table_provider=provider)
    assert calls == [("中山", "芝", 1600)]
    assert ("20250101", "中山", 1) in races
    assert races[("20250101", "中山", 1)][0][0] > 0


def test_logloss_uses_same_late_race_population_as_coverage():
    early_key = ("20250101", "中山", 1)
    target_key = ("20250101", "中山", 9)
    keys = [early_key] * 8 + [target_key] * 8
    scores = np.zeros(16)
    labels = np.array([1] + [0] * 7 + [1] + [0] * 7)
    assert conditional_log_loss(scores, labels, keys) == pytest.approx(np.log(8))


def test_fold_training_features_use_previous_year_snapshots(monkeypatch):
    snapshot_calls = []

    class Provider:
        def __init__(self, as_of):
            self.as_of = as_of

        def for_as_of(self, as_of):
            snapshot_calls.append(as_of)
            return Provider(as_of)

        def __call__(self, *_args):
            return self.as_of

    runs = [
        {"date": f"{year}0101", "place": "東京", "r": 9, "rank": 1}
        for year in range(2021, 2026)
    ]

    def fake_build_dataset(source_runs, date_from, _cfg):
        import backtest_fold_stats as module
        snapshot = module.scoring.load_factor_table("東京", "芝", 1600, None)
        selected = [row for row in source_runs if row["date"] >= date_from]
        features = np.array([[float(snapshot[:4])] for _ in selected])
        labels = np.ones(len(selected), dtype=int)
        keys = [(row["date"], row["place"], row["r"]) for row in selected]
        return features, labels, keys, selected

    monkeypatch.setattr("backtest_fold_stats.build_dataset", fake_build_dataset)
    features, _labels, keys, _meta = build_feature_dataset(
        runs, {}, "20251231", Provider("20241231"))
    assert snapshot_calls == [
        "20201231", "20211231", "20221231", "20231231", "20241231"]
    assert [key[0][:4] for key in keys] == ["2021", "2022", "2023", "2024", "2025"]
    assert features[:, 0].tolist() == [2020.0, 2021.0, 2022.0, 2023.0, 2024.0]
