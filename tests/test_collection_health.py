"""Display-only collection health; synthetic state and mocked I/O exclusively."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import jra_ev


NOW = datetime(2026, 9, 19, 11, 0, tzinfo=jra_ev.JST)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(jra_ev, "STATE", deepcopy(jra_ev.STATE))
    jra_ev.STATE.update(races={}, excluded_races={}, status="idle", error="", warning="")
    jra_ev.STATE.pop("_analysis_health", None)
    monkeypatch.setattr(jra_ev, "_COLLECTION_HEALTH", {})
    monkeypatch.setattr(jra_ev, "RACE_ANALYSIS_CACHE", {})
    monkeypatch.setattr(jra_ev, "LoggingStore", None)


def record(start=NOW, *, observed=True, day="20260919", url="race"):
    return {"url": url, "venue": "東京", "race_num": 1, "race_date": day,
            "_start_dt": start, "horses": [],
            "_collection_health": {"observed": True} if observed else {}}


def test_stage_counts_are_today_only_and_do_not_trust_checked_flags():
    new = record(NOW - timedelta(minutes=1))
    new["checked30"] = True
    old = record(NOW - timedelta(minutes=1), observed=False, url="restored")
    old["checked30"] = True
    future = record(NOW + timedelta(days=1), day="20260920", url="tomorrow")
    previous = record(NOW - timedelta(days=1), day="20260918", url="yesterday")
    jra_ev.STATE["races"] = dict(enumerate([new, old, future, previous]))
    jra_ev.STATE["excluded_races"] = {"阪神_4": {
        "url": "jump", "venue": "阪神", "race_num": 4, "race_date": "20260919",
        "analysis_excluded": True, "excluded_reason": "jump_race",
    }}
    health = jra_ev._health_summary(NOW)
    assert health["analysis"]["succeeded"] == 2
    assert health["analysis"]["total"] is None
    for stage in health["stages"].values():
        assert stage == {"waiting": 0, "saved": 0, "quality_failed": 0,
                         "missing": 1, "unknown": 1}


@pytest.mark.parametrize("stage,remaining,bucket", [
    ("30", 1201, "waiting"), ("30", 1200, "missing"),
    ("10", 361, "waiting"), ("10", 360, "missing"),
    ("2", 1, "waiting"), ("2", 0, "missing"),
])
def test_missing_only_after_existing_giveup_boundary(stage, remaining, bucket):
    jra_ev.STATE["races"] = {"r": record(NOW + timedelta(seconds=remaining))}
    assert jra_ev._health_summary(NOW)["stages"][stage][bucket] == 1


def test_saved_and_saved_with_quality_flags_are_distinct():
    rec = record()
    rec["_collection_health"]["stages"] = {
        "30": {"persisted": True, "quality_failed": False},
        "10": {"persisted": True, "quality_failed": True},
        "2": {"persisted": False, "quality_failed": False},
    }
    jra_ev.STATE["races"] = {"r": rec}
    stages = jra_ev._health_summary(NOW)["stages"]
    assert stages["30"]["saved"] == 1
    assert stages["10"]["quality_failed"] == 1
    assert stages["2"]["missing"] == 1


def test_wrapper_reports_safe_failure_and_clears_it_on_success(monkeypatch):
    def fail(*args):
        raise TimeoutError("secret URL / private token")
    monkeypatch.setattr(jra_ev, "_analyze_one_impl", fail)
    with pytest.raises(TimeoutError):
        jra_ev.analyze_one("race", {}, base_date=NOW)
    current = jra_ev._health_summary(NOW)["collection"]
    assert current["fetch_errors"] == 1
    assert current["error_types"] == ["TimeoutError"]
    rec = record()
    rec["_collection_health"].update(last_fetch_at=NOW.isoformat(), fetch_error=None)
    monkeypatch.setattr(jra_ev, "_analyze_one_impl", lambda *args: rec)
    jra_ev.analyze_one("race", {}, base_date=NOW)
    current = jra_ev._health_summary(NOW)["collection"]
    assert current["fetch_errors"] == 0
    assert current["last_fetch_at"] == NOW.isoformat()


def test_failed_attempt_preserves_previous_successful_timestamps():
    health = {"last_fetch_at": NOW.isoformat(), "last_save_at": NOW.isoformat()}
    jra_ev._merge_collection_health(health, {"last_save_at": None, "save_error": "IncompleteSave"})
    assert health["last_save_at"] == NOW.isoformat()
    assert health["save_error"] == "IncompleteSave"


@pytest.fixture
def analysis_inputs(monkeypatch):
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda *args: {
        "venue": "東京", "race_type": "芝", "dist_val": 1600,
        "race_info": "【東京 1R】芝1600m 11:00発走", "race_date": "20260919",
        "horses": [{"num": 1, "name": "A", "odds": 2.0},
                   {"num": 2, "name": "B", "odds": 3.0}],
    })
    monkeypatch.setattr(jra_ev.scoring, "load_score_weights", lambda *args: {})
    monkeypatch.setattr(jra_ev.scoring, "load_factor_table", lambda *args: {})
    monkeypatch.setattr(jra_ev.scoring, "assess_ml_score", lambda *args:
                        {"score": 1.0, "source": "ml", "failure_reason": None})
    monkeypatch.setattr(jra_ev.scoring, "win_probs_from_ml_scores", lambda scores: [0.5, 0.5])
    monkeypatch.setattr(jra_ev, "_data_version", lambda: "synthetic")


class Store:
    def save_race(self, **kwargs):
        pass

    def start_run(self, **kwargs):
        return "synthetic"

    def save_predictions(self, run_id, rows):
        return len(rows)

    def save_odds(self, rows):
        return len(rows)

    def finish_run(self, run_id):
        pass


@pytest.mark.parametrize("short_method", ["save_predictions", "save_odds"])
def test_short_save_count_is_not_success(analysis_inputs, monkeypatch, short_method):
    store = Store()
    monkeypatch.setattr(store, short_method, lambda *args: 1)
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)
    rec = jra_ev.analyze_one("race", jra_ev.STATE["params"], base_date=NOW)
    assert rec["_collection_health"]["save_error"] == "IncompleteSave"
    assert rec["_collection_health"].get("last_save_at") is None


def test_successful_save_and_unavailable_store(analysis_inputs, monkeypatch):
    rec = jra_ev.analyze_one("race", jra_ev.STATE["params"], base_date=NOW)
    assert rec["_collection_health"]["save_error"] == "LoggingUnavailable"
    monkeypatch.setattr(jra_ev, "LoggingStore", Store)
    rec = jra_ev.analyze_one("race", jra_ev.STATE["params"], base_date=NOW)
    assert rec["_collection_health"]["save_error"] is None
    assert rec["_collection_health"]["last_save_at"]


def test_monitor_save_error_clears_without_masking_odds_save_error(monkeypatch):
    rec = record()
    rec["_collection_health"]["save_error"] = "IncompleteSave"
    jra_ev._persist_monitor(rec)
    assert rec["_collection_health"]["monitor_save_error"] == "LoggingUnavailable"
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: SimpleNamespace(save_monitor_state=lambda *a, **kw: None))
    jra_ev._persist_monitor(rec)
    assert rec["_collection_health"]["monitor_save_error"] is None
    assert rec["_collection_health"]["save_error"] == "IncompleteSave"


def test_worker_distinguishes_success_failure_and_exclusion(monkeypatch):
    now = datetime.now()
    monkeypatch.setattr(jra_ev, "find_entry_url", lambda: "synthetic")
    monkeypatch.setattr(jra_ev.requests, "get", lambda *a, **kw: SimpleNamespace(text=""))
    monkeypatch.setattr(jra_ev, "build_matrix_data", lambda *args: [{
        "text": f"{now.month}/{now.day}(土) 東京", "races": [{"url": item} for item in ("ok", "fail", "excluded")],
    }])
    def analyze(url, *args, **kwargs):
        if url == "fail":
            raise ValueError("synthetic")
        if url == "excluded":
            return {
                "analysis_excluded": True,
                "excluded_reason": "jump_race",
                "excluded_reason_label": "障害レースのため予測対象外",
                "race_type": "障害", "venue": "阪神", "race_num": 4,
                "race_info": "【阪神 4R】障害2970m", "race_date": now.strftime("%Y%m%d"),
                "start_time": "11:20", "day_label": f"{now.month}/{now.day}(土)",
                "url": url, "horses": [], "n_picked": 0, "wide_picks": [],
            }
        return record(url=url)
    monkeypatch.setattr(jra_ev, "analyze_one", analyze)
    persisted = []
    monkeypatch.setattr(jra_ev, "_persist_monitor", lambda rec: persisted.append(rec["url"]))
    monkeypatch.setattr(jra_ev, "_ensure_scheduler", lambda: None)
    # SPEC-T82: 解析完了フックが実ネットワークへ問い合わせないようにする
    # (このテストの関心はworker_analyze_all本体の集計であり、使用コース取得ではない)。
    monkeypatch.setattr(jra_ev, "_t82_update_course_usage_hook", lambda: None)
    jra_ev.worker_analyze_all(jra_ev.STATE["params"])
    health = jra_ev.STATE["_analysis_health"]
    assert {key: health[key] for key in ("total", "succeeded", "failed", "excluded")} == {
        "total": 3, "succeeded": 1, "failed": 1, "excluded": 1}
    assert health["started_at"] and health["completed_at"]
    assert [rec["url"] for rec in jra_ev.STATE["races"].values()] == ["ok"]
    assert persisted == ["ok"]

    excluded = list(jra_ev.STATE["excluded_races"].values())
    assert len(excluded) == 1
    assert excluded[0]["url"] == "excluded"
    assert excluded[0]["excluded_reason"] == "jump_race"

    response = jra_ev.app.test_client().get("/api/state")
    assert response.status_code == 200
    payload = response.get_json()
    assert [rec["url"] for rec in payload["races"]] == ["ok"]
    assert payload["excluded_races"] == [{
        "analysis_excluded": True,
        "excluded_reason": "jump_race",
        "excluded_reason_label": "障害レースのため予測対象外",
        "race_type": "障害",
        "venue": "阪神",
        "race_num": 4,
        "race_info": "【阪神 4R】障害2970m",
        "start_time": "11:20",
        "day_label": f"{now.month}/{now.day}(土)",
        "url": "excluded",
        "horses": [],
        "n_picked": 0,
        "wide_picks": [],
        "rid": "阪神_4",
    }]


def test_slim_state_is_read_only_and_recomputes_probability_quality(monkeypatch):
    rec = record()
    rec.update(ml_coverage={"sufficient": True}, probability_quality={"eligible": True})
    jra_ev.STATE["races"] = {"r": rec}
    def forbidden(*args, **kwargs):
        raise AssertionError("health must not access network or SQLite")
    monkeypatch.setattr(jra_ev, "LoggingStore", forbidden)
    monkeypatch.setattr(jra_ev.requests, "get", forbidden)
    state = jra_ev._slim_state()
    assert "health" in state
    assert state["races"][0]["ml_coverage"] == jra_ev._ml_coverage([])


def test_restore_clears_unproven_legacy_probabilities(monkeypatch):
    now = datetime.now()
    rec = record(start=now, day=now.strftime("%Y%m%d"))
    rec["horses"] = [{"num": 1, "name": "A", "ml_score": 2.0,
                      "win_prob": 1.0, "ev": 8.0, "picked": True}]
    store = SimpleNamespace(
        active_monitors=lambda: [{"payload": rec, "start_time": now.isoformat(),
                                  "checked15": False, "checked5": False, "monitor_key": "r"}],
        retryable_notifications=lambda: [],
    )
    monkeypatch.setattr(jra_ev, "LoggingStore", lambda: store)
    monkeypatch.setattr(jra_ev, "_ensure_scheduler", lambda: None)
    jra_ev._restore_phase2_state()
    restored = jra_ev.STATE["races"]["r"]["horses"][0]
    assert restored["win_prob"] is None
    assert restored["ev"] is None
    assert restored["picked"] is False


def test_restored_success_timestamp_survives_first_failed_refresh(monkeypatch):
    rec = record()
    rec["_collection_health"]["last_fetch_at"] = NOW.isoformat()
    jra_ev._record_collection_health(rec, {})
    def fail(*args):
        raise TimeoutError("synthetic")
    monkeypatch.setattr(jra_ev, "_analyze_one_impl", fail)
    with pytest.raises(TimeoutError):
        jra_ev.analyze_one("race", {}, base_date=NOW)
    current = jra_ev._health_summary(NOW)["collection"]
    assert current["fetch_errors"] == 1
    assert current["last_fetch_at"] == NOW.isoformat()
