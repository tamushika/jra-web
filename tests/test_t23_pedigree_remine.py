from pathlib import Path

import pytest

import mine_criteria as mc


def _run(date, horse="A"):
    return {
        "date": date, "horse": horse, "jockey": "J", "rank": 4,
        "place": "東京", "track_type": "芝", "distance": 1600,
        "total_horses": 12, "race_class": "未勝利", "r": 1,
    }


def test_cached_pedigree_is_attached_without_changing_missing_defaults():
    known = {"Known": {"sire": "Sire A", "bms": "BMS A"}}

    h = mc.attach_pedigree(
        {"sire": "-", "bms": "-"}, "Known", known)
    missing = mc.attach_pedigree(
        {"sire": "-", "bms": "-"}, "Missing", known)

    assert h["sire"] == "Sire A"
    assert h["bms"] == "BMS A"
    assert missing == {"sire": "-", "bms": "-"}


def test_pedigree_predicates_distinguish_sire_bms_and_lineage():
    h = {"sire": "Sire A", "bms": "BMS A"}
    lineage = {"Sire A": "Northern", "BMS A": "Native"}

    assert mc.analysis.check_condition("父がNorthern系", h, {}, lineage, {})
    assert mc.analysis.check_condition("母父がBMS A", h, {}, lineage, {})
    assert mc.analysis.check_condition("母父がNative系", h, {}, lineage, {})
    assert not mc.analysis.check_condition("母父がSire A", h, {}, lineage, {})
    assert not mc.analysis.check_condition("父がBMS A", h, {}, lineage, {})
    assert mc.analysis.check_condition("父or母父がBMS A", h, {}, lineage, {})


def test_exact_sire_name_does_not_match_an_offspring_in_that_lineage():
    h = {"sire": "キズナ", "bms": "リアルスティール"}
    lineage = {
        "キズナ": "ディープインパクト系",
        "リアルスティール": "ディープインパクト系",
    }

    assert not mc.analysis.check_condition(
        "父がディープインパクト", h, {}, lineage, {})
    assert not mc.analysis.check_condition(
        "母父がディープインパクト", h, {}, lineage, {})
    assert mc.analysis.check_condition(
        "父がディープインパクト系", h, {}, lineage, {})
    assert mc.analysis.check_condition(
        "母父がディープインパクト系", h, {}, lineage, {})

    ancestor = {"sire": "ディープインパクト", "bms": "ステイゴールド"}
    assert mc.analysis.check_condition(
        "父がディープインパクト系", ancestor, {}, {}, {})
    assert mc.analysis.check_condition(
        "母父がステイゴールド系", ancestor, {}, {}, {})


def test_pedigree_increment_control_keeps_non_pedigree_conditions_fixed():
    rule = {"conds": ["馬齢が3歳以下", "父がSire A"]}
    context = {}
    lineage = {}

    assert not mc._matches_non_pedigree_context(
        rule, {"sex_age": "牡5", "sire": "Sire A"}, context, lineage, {})
    assert mc._matches_non_pedigree_context(
        rule, {"sex_age": "牡3", "sire": "Other"}, context, lineage, {})
    assert not mc._matches_rule(
        rule, {"sex_age": "牡3", "sire": "Other"}, context, lineage, {})
    assert mc._matches_rule(
        rule, {"sex_age": "牡3", "sire": "Sire A"}, context, lineage, {})


def test_candidate_pedigrees_use_discovery_period_only():
    discovery = [(_run("20220101", f"D{i}"), None)
                 for i in range(mc.MIN_DISCOVER_N)]
    fixed = [(_run("20250101", f"F{i}"), None)
             for i in range(mc.MIN_DISCOVER_N + 10)]
    pedigree = {
        **{f"D{i}": {"sire": "Discovery Sire", "bms": "Discovery BMS"}
           for i in range(mc.MIN_DISCOVER_N)},
        **{f"F{i}": {"sire": "Fixed Sire", "bms": "Fixed BMS"}
           for i in range(mc.MIN_DISCOVER_N + 10)},
    }
    lineage = {"Discovery Sire": "Discovery Line系", "Fixed Sire": "Fixed Line系"}

    conds = mc.candidate_conditions(
        discovery + fixed, "20210101", "20231231",
        pedigree=pedigree, sire_lineage=lineage)

    assert "父がDiscovery Sire" in conds
    assert "母父がDiscovery BMS" in conds
    assert "父がDiscovery Line系" in conds
    assert not any("系系" in cond for cond in conds)
    assert not any("Fixed" in cond for cond in conds)


def test_windows_still_reject_fixed_test_selection():
    mc.validate_windows("20210101", "20231231", "20240101", "20241231")
    with pytest.raises(ValueError, match="固定テスト"):
        mc.validate_windows("20210101", "20231231", "20240101", "20250101")


def test_v3_is_writable_but_production_rules_are_guarded(tmp_path):
    rule = {
        "place": "東京", "track_type": "芝", "distance": 1600,
        "conds": ["父がSire A"], "kind": "買い", "points": 1.0,
    }
    v2 = tmp_path / "mined_rules_v2.csv"
    v3 = tmp_path / "mined_rules_v3.csv"
    mc.write_rules(v2, [rule])
    mc.write_rules(v3, [rule])

    assert mc.load_rules(v2) == [rule]
    assert mc.load_rules(v3) == [rule]
    with pytest.raises(ValueError, match="上書き"):
        mc.write_rules(Path(mc.PRODUCTION_RULES_PATH), [rule])


def test_missing_pedigree_is_false_not_an_exception():
    missing = {"sire": "-", "bms": "-"}

    assert not mc.analysis.check_condition("父がUnknown", missing, {}, {}, {})
    assert not mc.analysis.check_condition("母父がUnknown", missing, {}, {}, {})


def test_pedigree_coverage_counts_unique_horses_and_each_field():
    runs = [_run("20220101", "A"), _run("20220102", "A"), _run("20220101", "B")]
    pedigree = {"A": {"sire": "S", "bms": "B"}, "B": {"sire": "S2", "bms": ""}}

    assert mc.pedigree_coverage(runs, pedigree) == {
        "horses": 2, "sire": 2, "bms": 1, "both": 1,
    }


@pytest.mark.parametrize(
    ("period", "expected_as_of"),
    [("2025", "20241231"), ("2026H1", "20251231")],
)
def test_sire_pts_provider_uses_prior_year_ability_snapshot(
        monkeypatch, period, expected_as_of):
    captured = {}

    class FakeProvider:
        def __init__(self, db_path, as_of, **kwargs):
            captured.update(db_path=str(db_path), as_of=as_of, **kwargs)

    monkeypatch.setattr(mc, "FoldFactorTableProvider", FakeProvider)
    provider = mc.build_sire_pts_provider(
        period, {"A": {"sire": "S", "bms": "B"}, "Missing": {"sire": ""}})

    assert isinstance(provider, FakeProvider)
    assert captured["as_of"] == expected_as_of
    assert captured["pedigree_by_horse"] == {"A": "S"}
    assert captured["legacy_api_dir"] == mc.API_DIR


def test_unknown_sire_pts_period_is_rejected():
    with pytest.raises(ValueError, match="as-of"):
        mc.build_sire_pts_provider("2027", {})


def test_sire_pts_matches_model_zero_fallback_when_factor_row_is_missing():
    cfg = {"params": {"min_starts": 5}}
    no_course = lambda *_args: None
    no_sire_row = lambda *_args: {
        "baseline": {"win_rate": 10.0}, "father_w": {},
    }

    assert mc._sire_points("東京", "芝", 1600, "S", no_course, cfg) == (0.0, False)
    assert mc._sire_points("東京", "芝", 1600, "S", no_sire_row, cfg) == (0.0, False)


def test_sire_pts_tertiles_are_weighted_by_observations_not_unique_values():
    # 90頭の0点を「1つの値」として軽く扱わず、実際の観測分布で境界を決める。
    values = [0.0] * 90 + [1.0] * 5 + [2.0] * 5

    assert mc._tertile_cuts(values) == (0.0, 0.0)


def test_unobserved_sire_band_increment_is_not_reported_as_zero_effect():
    matched_rate, control_rate, increment = mc._group_comparison_rates(
        [], [{"top3": 1}, {"top3": 0}])

    assert matched_rate is None
    assert control_rate == 50.0
    assert increment is None
