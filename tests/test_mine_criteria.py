import sqlite3

import pytest

import mine_criteria as mc


def _run(date, rank=4, *, flag=False, win_pay=None, place_pay=None):
    return {
        "date": date,
        "place": "東京",
        "r": 1,
        "race_class": "未勝利",
        "horse": f"horse-{date}-{rank}-{flag}",
        "total_horses": 12,
        "rank": rank,
        "track_type": "芝",
        "distance": 1600,
        "jockey": "テスト騎手",
        "umaban": 1,
        "win_pay": win_pay,
        "fukusho_pay": place_pay,
        "flag": flag,
    }


def test_default_windows_are_clean_and_fixed_test_is_rejected():
    mc.validate_windows(mc.DISCOVER_FROM, mc.DISCOVER_TO,
                        mc.SELECT_FROM, mc.SELECT_TO)
    with pytest.raises(ValueError, match="固定テスト"):
        mc.validate_windows("20210101", "20231231", "20240101", "20250101")
    with pytest.raises(ValueError, match="時系列順"):
        mc.validate_windows("20210101", "20241231", "20240101", "20241231")


def test_candidate_jockeys_use_discovery_period_only():
    discovery = [(_run("20220101"), None) for _ in range(mc.MIN_DISCOVER_N)]
    for cur, _ in discovery:
        cur["jockey"] = "発見騎手"
    fixed_test = [(_run("20250101"), None) for _ in range(mc.MIN_DISCOVER_N + 10)]
    for cur, _ in fixed_test:
        cur["jockey"] = "固定テスト騎手"

    conds = mc.candidate_conditions(discovery + fixed_test, "20210101", "20231231")

    assert "騎手が発見騎手" in conds
    assert "騎手が固定テスト騎手" not in conds


def test_mining_does_not_build_or_read_fixed_test_rows(monkeypatch):
    discovery = []
    for i in range(200):
        discovery.append((_run(
            "20220101", rank=1 if i < 50 else 4, flag=i < 100), None))
    selection = []
    for i in range(30):
        selection.append((_run(
            "20240101", rank=1 if i < 15 else 4, flag=i < 15), None))
    fixed_test = [(_run("20250101", rank=1, flag=True), None) for _ in range(20)]

    def fake_build_h(cur, _prev):
        if cur["date"] >= "20250101":
            raise AssertionError("fixed-test row reached mining")
        return cur

    def fake_check(condition, h, *_args):
        return condition == "性別が牝馬" and h["flag"]

    monkeypatch.setattr(mc, "build_h", fake_build_h)
    monkeypatch.setattr(mc.analysis, "check_condition", fake_check)

    buys, kills = mc.mine_course(
        "東京", "芝", 1600, discovery + selection + fixed_test, {}, [],
        discover_from="20210101", discover_to="20231231",
        select_from="20240101", select_to="20241231")

    assert kills == []
    assert len(buys) == 1
    assert buys[0]["conds"] == ["性別が牝馬"]


def test_rule_csv_is_deterministic_and_production_path_is_guarded(tmp_path):
    rules = [{
        "place": "東京", "track_type": "芝", "distance": 1600,
        "conds": ["馬齢が3歳以下"], "kind": "買い", "points": 1.25,
    }]
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    mc.write_rules(first, rules)
    mc.write_rules(second, rules)

    assert first.read_bytes() == second.read_bytes()
    assert mc.load_rules(first) == rules
    with pytest.raises(ValueError, match="上書き"):
        mc.write_rules(mc.PRODUCTION_RULES_PATH, rules)


def test_evaluation_counts_each_runner_once_and_reports_both_rois(monkeypatch):
    winner = _run("20250101", rank=1, flag=True, win_pay="250", place_pay=120)
    loser = _run("20250102", rank=4, flag=True)
    ignored = _run("20250103", rank=2, flag=False, place_pay=130)
    courses = {("東京", "芝", 1600): [(winner, None), (loser, None), (ignored, None)]}
    rules = [
        {"place": "東京", "track_type": "芝", "distance": 1600,
         "conds": ["match-a"], "kind": "買い", "points": 1.0},
        {"place": "東京", "track_type": "芝", "distance": 1600,
         "conds": ["match-b"], "kind": "買い", "points": 1.0},
    ]
    monkeypatch.setattr(mc, "build_h", lambda cur, _prev: cur)
    monkeypatch.setattr(
        mc.analysis, "check_condition",
        lambda condition, h, *_args: condition in ("match-a", "match-b") and h["flag"])

    buy, kill = mc.evaluate_rules(rules, courses, {}, "2025", "20250101", "20251231")

    assert buy["rules"] == 2
    assert buy["matches"] == 2  # 2ルールに重複該当しても馬単位で1件
    assert buy["show_rate"] == pytest.approx(50.0)
    assert buy["win_roi"] == pytest.approx(125.0)
    assert buy["place_roi"] == pytest.approx(60.0)
    assert buy["place_missing"] == 0
    assert kill["matches"] == 0


def test_place_payouts_fall_back_to_race_payouts():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE race_payouts (
        date TEXT, place TEXT, r INTEGER, bet_type TEXT, combo TEXT, pay REAL)""")
    conn.execute(
        "INSERT INTO race_payouts VALUES (?,?,?,?,?,?)",
        ("20260101", "東京", 1, "複勝", "3", 180.0))
    runs = [{"date": "20260101", "place": "東京", "r": 1, "umaban": 3}]

    mc.attach_place_payouts(conn, runs, "20260101", "20260630")

    assert runs[0]["fukusho_pay"] == 180.0


def test_selection_points_keep_existing_formula_and_bounds():
    assert mc._selection_points({"select_rate": 30.0, "select_base": 28.0}) == 0.5
    assert mc._selection_points({"select_rate": 38.0, "select_base": 30.0}) == 2.0
    assert mc._selection_points({"select_rate": 55.0, "select_base": 20.0}) == 3.0
