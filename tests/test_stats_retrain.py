from __future__ import annotations

import numpy as np
import pytest

import backtest_ml
import backtest_stats_retrain as t33
from backtest_fold_stats import same_population_metrics


def _rules(label="rules"):
    return t33.RuleSetMetadata(
        label=label, path=f"{label}.csv", sha256="a" * 64,
        rules=1, buys=1, kills=0)


def test_production_rule_files_validate_and_are_fingerprinted():
    current = t33.validate_rules_file(t33.CURRENT_RULES_PATH)
    v2 = t33.validate_rules_file(t33.V2_RULES_PATH)
    assert (current.rules, current.buys + current.kills) == (359, 359)
    assert (v2.rules, v2.buys + v2.kills) == (330, 330)
    assert current.label == "current rules"
    assert v2.label == "v2 rules"
    assert current.sha256 != v2.sha256


def test_rules_validation_rejects_wrong_schema_and_bad_rows(tmp_path):
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("place,type\n東京,芝\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        t33.validate_rules_file(wrong)

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "場,芝ダ,距離,条件1,条件2,条件3,種別,点数\n"
        "東京,芝,1600,馬齢が4歳以下,,,不明,1.0\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        t33.validate_rules_file(bad)


def test_consistent_feature_helper_selects_explicit_source(monkeypatch):
    calls = []

    def fake_builder(runs, cfg, date_to, provider=None, *, dataset_kwargs=None):
        calls.append((runs, cfg, date_to, provider, dataset_kwargs))
        return "X", "y", "keys", "meta"

    monkeypatch.setattr(t33, "build_feature_dataset", fake_builder)
    expected = t33.build_consistent_feature_dataset(
        ["run"], {"cfg": 1}, "20251231", stats_source="current")
    assert expected == ("X", "y", "keys", "meta")
    assert calls[-1][3] is None

    provider = object()
    expected = t33.build_consistent_feature_dataset(
        ["run"], {"cfg": 1}, "20251231", stats_source="ability",
        factor_provider=provider)
    assert expected == ("X", "y", "keys", "meta")
    assert calls[-1][3] is provider

    t33.build_consistent_feature_dataset(
        ["run"], {"cfg": 1}, "20251231", stats_source="ability",
        factor_provider=provider, dataset_kwargs={"relative": True})
    assert calls[-1][4] == {"relative": True}

    with pytest.raises(ValueError, match="stats_source"):
        t33.build_consistent_feature_dataset([], {}, "20251231", stats_source="bad")


def test_current_dataset_is_legacy_fixed_even_when_auto_loader_is_snapshot(
        monkeypatch):
    import backtest_fold_stats as fold

    monkeypatch.setattr(fold.scoring, "load_factor_table", lambda *_args: "snapshot")
    monkeypatch.setattr(fold.scoring, "load_legacy_factor_table",
                        lambda *_args: "legacy")

    def fake_build(_runs, _date_from, _cfg, **_kwargs):
        selected = fold.scoring.load_factor_table("東京", "芝", 1600, None)
        return selected, None, [], []

    monkeypatch.setattr(fold, "build_dataset", fake_build)
    features, _labels, _keys, _meta = fold.build_feature_dataset(
        [], {}, "20251231", provider=None)
    assert features == "legacy"


def test_three_requested_rows_mark_rules_na_but_remain_stats_sensitive(monkeypatch):
    def fake_dataset(_runs, _cfg, _to, *, stats_source, **_kwargs):
        marker = 1.0 if stats_source == "current" else 2.0
        return np.array([[marker]]), np.array([1]), [("20250101", "東京", 9)], [{}]

    def fake_evaluate(features, _labels, _keys, _meta, _upset, _from, _to):
        marker = float(features[0, 0])
        model = {
            "coverage": [marker / 10] * 4,
            "logloss": marker,
            "brier": marker / 100,
        }
        market = {
            "coverage": [0.3, 0.5, 0.6, 0.7],
            "logloss": 2.0,
            "brier": 0.05,
        }
        return {
            "temperature": 1.0,
            "comparison": {
                "races": 1, "horses": 8,
                "models": {"model": model, "market": market},
                "sample_signature": (("same",),), "skipped": {},
            },
        }

    monkeypatch.setattr(t33, "build_consistent_feature_dataset", fake_dataset)
    monkeypatch.setattr(t33, "evaluate_feature_dataset", fake_evaluate)
    report = t33.evaluate_quadrants(
        [], {}, {}, [_rules("current"), _rules("v2")],
        periods={"2025": ("20250101", "20251231")})
    assert len(report["rows"]) == 3
    current = [row for row in report["rows"] if row["stats_source"] == "current"]
    ability = [row for row in report["rows"] if row["stats_source"] == "ability"]
    assert len(current) == 1
    assert len(ability) == 2
    assert ability[0]["metrics"] == ability[1]["metrics"]
    assert current[0]["metrics"] != ability[0]["metrics"]
    assert all(row["rules_effect"].startswith("N/A:") for row in report["rows"])
    assert report["rules_effect"].startswith("N/A:")


def test_same_population_metrics_uses_identical_complete_races():
    key = ("20250101", "東京", 9)
    meta = []
    for index in range(8):
        rank = 1 if index == 0 else index + 1
        odds = "200" if rank == 1 else f"({2 + index:.1f})"
        meta.append({
            "horse": f"H{index}", "umaban": index + 1, "rank": rank,
            "popularity": index + 1, "total_horses": 8, "win_pay": odds,
        })
    scores = np.arange(8, 0, -1, dtype=float)
    labels = np.array([1] + [0] * 7)
    result = same_population_metrics(
        scores, labels, [key] * 8, meta, temperature=1.0)
    assert result["races"] == 1
    assert result["horses"] == 8
    assert result["models"]["model"]["coverage"] == [1.0] * 4
    assert result["models"]["market"]["coverage"] == [1.0] * 4
    assert result["models"]["model"]["logloss"] > 0
    assert result["models"]["model"]["brier"] > 0


def test_same_population_metrics_rejects_incomplete_feature_field():
    key = ("20250101", "東京", 9)
    meta = [
        {"horse": f"H{i}", "umaban": i + 1, "rank": 1 if i == 0 else i + 1,
         "popularity": i + 1, "total_horses": 9,
         "win_pay": "200" if i == 0 else "(5.0)"}
        for i in range(8)
    ]
    result = same_population_metrics(
        np.zeros(8), np.array([1] + [0] * 7), [key] * 8, meta)
    assert result["races"] == 0
    assert result["skipped"] == {"incomplete_features": 1}


def test_same_population_metrics_counts_every_dead_heat_winner():
    key = ("20250101", "東京", 9)
    meta = [
        {
            "horse": f"H{i}", "umaban": i + 1,
            "rank": 1 if i < 2 else i,
            "popularity": i + 1, "total_horses": 8,
            "win_pay": "200" if i == 0 else "300" if i == 1 else "(10.0)",
        }
        for i in range(8)
    ]
    # The second dead-heat winner is model rank 1, while the first winner is
    # deliberately last.  Using only the first label==1 row would miss top-1.
    scores = np.asarray([-10.0, 10.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0])
    labels = np.asarray([1, 1, 0, 0, 0, 0, 0, 0])

    result = same_population_metrics(scores, labels, [key] * 8, meta)

    assert result["models"]["model"]["coverage"][0] == 1.0
    probabilities = np.exp(scores - scores.max())
    probabilities /= probabilities.sum()
    expected_logloss = -(
        np.log(probabilities[0]) + np.log(probabilities[1])) / 2.0
    assert result["models"]["model"]["logloss"] == pytest.approx(
        expected_logloss)


@pytest.mark.parametrize("argv", [
    ["--stats-snapshot", "--write"],
    ["--stats-snapshot", "--no-market"],
    ["--stats-snapshot", "--lgbm"],
    ["--rules-file", "rules.csv"],
])
def test_backtest_ml_rejects_unsafe_stats_cli_combinations(argv):
    with pytest.raises(SystemExit) as exc_info:
        backtest_ml.parse_cli_args(argv)
    assert exc_info.value.code == 2


def test_backtest_ml_delegates_snapshot_evaluation_without_loading_writer(
        monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sys.argv",
        ["backtest_ml.py", "--stats-snapshot", "--rules-file", "clean.csv"])
    monkeypatch.setattr(t33, "main", lambda argv=None: calls.append(argv))
    backtest_ml.main()
    assert calls == [["--stats-source", "all", "--rules-file", "clean.csv"]]
