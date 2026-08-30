"""WIN5監視予約の発走時刻補完 (2026-08-30): JRAのWIN5一覧ページは各レースセルに
発走時刻を含まない (発売締切時刻のみ) ため、time が空だと STEP4 の予約が
「発走時刻が取得できていません」で拒否される。サーバー側はEVモニタの start_time から補完する。"""
import jra_ev
import jra_win5


def test_fill_times_from_ev_monitor_matches_on_venue_and_race_num(monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "中京", "race_num": 6, "start_time": "15:01"},
        "r2": {"venue": "新潟", "race_num": 7, "start_time": "15:10"},
        "r3": {"venue": "札幌", "race_num": 11, "start_time": ""},
    })
    races = [
        {"idx": 0, "venue": "中京", "race_num": 6, "time": ""},
        {"idx": 1, "venue": "新潟", "race_num": 7, "time": "15:11"},   # 既存値は保持
        {"idx": 2, "venue": "札幌", "race_num": 11, "time": ""},       # EV側も空
        {"idx": 3, "venue": "中京", "race_num": 7, "time": ""},        # EV側に無い
    ]
    out = jra_win5._fill_times_from_ev_monitor(races)
    assert [r["time"] for r in out] == ["15:01", "15:11", "", ""]


def test_fill_times_is_noop_when_ev_monitor_not_analyzed(monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {})
    races = [{"idx": 0, "venue": "中京", "race_num": 6, "time": ""}]
    assert jra_win5._fill_times_from_ev_monitor(races)[0]["time"] == ""


def test_win5_races_endpoint_fills_time(monkeypatch):
    monkeypatch.setattr(jra_win5, "_scrape_win5_target", lambda: ({
        "date": "8月30日", "date_yyyymmdd": "20260830",
        "races": [{"idx": 0, "venue": "中京", "race_num": 6, "time": "", "label": "中京6R", "url": ""}],
    }, None))
    monkeypatch.setattr(jra_win5, "_find_win5_urls", lambda races, d: {0: "https://example/0"})
    monkeypatch.setattr(jra_win5, "_fill_missing_from_siblings", lambda races, m, d: m)
    monkeypatch.setitem(jra_ev.STATE, "races", {"r1": {"venue": "中京", "race_num": 6, "start_time": "15:01"}})
    client = jra_win5.app.test_client()
    data = client.get("/api/win5_races").get_json()
    assert data["success"] is True
    assert data["races"][0]["time"] == "15:01"
    assert data["races"][0]["url"] == "https://example/0"
