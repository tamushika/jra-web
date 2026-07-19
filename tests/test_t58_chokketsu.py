import hashlib

import numpy as np
import pytest

import backtest_chokketsu as t58


def _run(date, course, perf, *, age=4, race_class="1勝クラス", rank=2, r=1):
    venue, rest = course[:2], course[2:]
    surface = "ダート" if rest.startswith("ダ") else "芝"
    distance = int(rest[1:].removesuffix("内").removesuffix("外"))
    return {"date": date, "place": venue, "r": r, "horse": "H", "umaban": 1,
            "track_type": surface, "distance": distance, "age": age,
            "race_class": race_class, "rank": rank, "perf": perf}


def _perf(row, _use_variant):
    return row.get("perf")


def test_graph_reference_uses_target_course_and_class_group():
    graph = {
        ("東京芝1600", "古馬条件未勝利"): {"s": {"中山芝1600"}, "a": set()},
        ("東京芝1600", "2-3歳限定"): {"s": {"阪神芝1600"}, "a": set()},
    }
    current = _run("20240102", "東京芝1600", 0)
    prior = [_run("20240101", "中山芝1600", 2.0)]
    adult, available = t58.chokketsu_values(
        prior, current, "古馬条件未勝利", graph, performance_fn=_perf)
    young, _ = t58.chokketsu_values(
        prior, current, "2-3歳限定", graph, performance_fn=_perf)
    assert available == 1.0
    assert adult[0] == 2.0
    assert young[0] == 0.0 and young[2] == 2.0


def test_good_runs_split_into_s_a_nonconnect_and_same_course():
    graph = {("東京芝1600", "古馬条件未勝利"):
             {"s": {"中山芝1600"}, "a": {"阪神芝1600"}}}
    prior = [
        _run("20240101", "中山芝1600", 1.0),
        _run("20240102", "阪神芝1600", 2.0),
        _run("20240103", "京都芝1600", 3.0),
        _run("20240104", "東京芝1600", 4.0),
    ]
    values, available = t58.chokketsu_values(
        prior, _run("20240105", "東京芝1600", 0), "古馬条件未勝利",
        graph, performance_fn=_perf)
    assert available == 1.0
    assert values == pytest.approx([0.55, 1.4, 2.55, 4.0, 1.0, 1.0])


def test_asof_excludes_current_and_future_performance():
    graph = {("東京芝1600", "古馬条件未勝利"):
             {"s": {"中山芝1600"}, "a": set()}}
    past = _run("20240101", "中山芝1600", 1.0)
    current = _run("20240102", "東京芝1600", 50.0)
    future = _run("20240103", "中山芝1600", 99.0)
    before, _ = t58.chokketsu_values(
        [past], current, "古馬条件未勝利", graph, performance_fn=_perf)
    # build_chokketsu_features must locate current in a full history but use [:i].
    matrix, _, _, _ = t58.build_chokketsu_features(
        [current], [past, current, future], graph,
        performance_fn=_perf)
    np.testing.assert_array_equal(matrix[0], before)
    assert matrix[0, 0] == 1.0


def test_race_class_mapping_uses_field_age_not_individual_three_year_old():
    young = [_run("20240101", "東京芝1600", 0, age=3),
             {**_run("20240101", "東京芝1600", 0, age=3), "horse": "B"}]
    mixed_open = [_run("20240601", "東京芝1600", 0, age=3, race_class="重賞"),
                  {**_run("20240601", "東京芝1600", 0, age=5, race_class="重賞"), "horse": "B"}]
    assert t58.race_class_group(young) == "2-3歳限定"
    assert t58.race_class_group(mixed_open) == "古馬重賞OP"


def test_inner_outer_projection_is_explicit_and_deterministic():
    assert t58.project_course_name("京都芝1600外") == "京都芝1600"
    assert t58.project_course_name("京都芝1600内") == "京都芝1600"
    assert t58.project_course_name("新潟芝2000外") == "新潟芝2000"


def test_pack_off_keeps_baseline_sha_identical():
    baseline = np.arange(42, dtype=np.float64).reshape(2, 21)
    extra = np.ones((2, 6), dtype=np.float64)
    before = hashlib.sha256(baseline.tobytes()).hexdigest()
    off = t58.candidate_features(baseline, extra, enabled=False)
    assert hashlib.sha256(off.tobytes()).hexdigest() == before
    assert off is baseline
