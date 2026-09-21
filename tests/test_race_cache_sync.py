"""Cache-only detail updates must never trigger acquisition or use another day."""
from datetime import datetime, timedelta

import pytest
import requests

import jra_ev
import jra_suite
import index


URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=cache-sync-fixture"
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=jra_ev.JST)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(jra_ev, "RACE_ANALYSIS_CACHE", {})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cache-only lookup must not fetch or write")

    monkeypatch.setattr(index, "analyze_race_url", forbidden)
    monkeypatch.setattr(jra_ev, "analyze_race_url", forbidden)
    monkeypatch.setattr(jra_ev, "LoggingStore", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def seed(captured=NOW, race_date="20260919", version="v1"):
    entry = {
        "result": {"race_info": "東京 1R", "horses": [{"num": 1, "odds": 4.0}]},
        "cached_at": captured.strftime("%H:%M:%S"), "stage": 10,
        "captured_at": captured.isoformat(), "race_date": race_date,
        "cache_version": version,
    }
    jra_ev.RACE_ANALYSIS_CACHE[URL] = entry
    return entry


def test_cache_snapshot_contains_version_age_and_result_metadata():
    original = seed(NOW - timedelta(seconds=90))
    snapshot = jra_suite._race_cache_snapshot(URL, now=NOW)
    assert snapshot["available"] is True
    assert snapshot["cache_version"] == "v1"
    assert snapshot["age_seconds"] == 90
    assert snapshot["stale"] is False
    assert snapshot["result"]["monitor_captured_at"] == snapshot["captured_at"]
    assert snapshot["result"]["monitor_cache_version"] == "v1"
    assert snapshot["result"]["monitor_stage"] == 10
    assert "monitor_cache_version" not in original["result"]


def test_same_day_old_data_is_marked_stale_not_implicitly_refetched():
    seed(NOW - timedelta(minutes=10))
    snapshot = jra_suite._race_cache_snapshot(URL, now=NOW)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["age_seconds"] == 600


@pytest.mark.parametrize("captured,race_date", [
    (NOW - timedelta(days=1), "20260919"),
    (NOW, "20260918"),
    (NOW, "20260920"),
    (NOW, None),
])
def test_other_day_or_unknown_day_is_not_returned(captured, race_date):
    seed(captured, race_date)
    snapshot = jra_suite._race_cache_snapshot(URL, now=NOW)
    assert snapshot == {"available": False, "reason": "different_day"}


@pytest.mark.parametrize("timestamp", [None, "12:00:00", "bad", "2026-09-19T12:00:00"])
def test_missing_or_naive_timestamp_is_rejected(timestamp):
    seed()["captured_at"] = timestamp
    assert jra_suite._race_cache_snapshot(URL, now=NOW) == {
        "available": False, "reason": "invalid_timestamp"}


def test_future_timestamp_rejected():
    seed(NOW + timedelta(minutes=1))
    assert not jra_suite._race_cache_snapshot(URL, now=NOW)["available"]


def test_missing_version_cannot_be_treated_as_synchronizable():
    seed()["cache_version"] = None
    assert jra_suite._race_cache_snapshot(URL, now=NOW) == {
        "available": False, "reason": "invalid_timestamp"}


def test_jst_day_is_used_when_capture_timestamp_is_utc():
    seed()["captured_at"] = "2026-09-18T16:00:00+00:00"
    assert jra_suite._race_cache_snapshot(URL, now=NOW)["available"]


def test_get_endpoint_hit_and_miss_never_invoke_scrape():
    now = datetime.now(jra_ev.JST)
    seed(now, now.strftime("%Y%m%d"))
    client = jra_suite.create_app().test_client()
    response = client.get("/race/api/cache", query_string={"url": URL})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["available"]
    jra_ev.RACE_ANALYSIS_CACHE.clear()
    response = client.get("/race/api/cache", query_string={"url": URL})
    assert response.status_code == 200
    assert response.get_json() == {"available": False, "reason": "not_cached"}


def test_get_endpoint_returns_new_version_without_modifying_cache():
    now = datetime.now(jra_ev.JST)
    seed(now, now.strftime("%Y%m%d"))
    client = jra_suite.create_app().test_client()
    assert client.get("/race/api/cache", query_string={"url": URL}).get_json()["cache_version"] == "v1"
    seed(now, now.strftime("%Y%m%d"), version="v2")
    assert client.get("/race/api/cache", query_string={"url": URL}).get_json()["cache_version"] == "v2"


def test_cache_endpoint_rejects_post_without_fetch():
    response = jra_suite.create_app().test_client().post("/race/api/cache", json={"url": URL})
    assert response.status_code == 405
