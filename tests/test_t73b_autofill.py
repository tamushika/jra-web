"""SPEC-T73b (オッズ監視の解析結果をレース詳細タブへ自動反映) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T73b-race-detail-autofill.md §3
  - jra_ev.analyze_one() が analyze_race_url() の結果を RACE_ANALYSIS_CACHE に
    保存する (cached_at 付き)。analysis_excluded / 障害の結果は保存しない。
  - 解析開始 (api_analyze_start / _auto_start) のリセットでキャッシュが空になる。
  - suite の /race/api/scrape が、キャッシュヒット時は index.app の scrape() を
    呼ばずに cached_from_monitor 付きで即返る。mode="詳細"・force・未キャッシュ
    URLは素通しする (index.app の scrape() が呼ばれる)。
"""
import pytest

import jra_ev
import jra_suite
# jra_suite が sys.path に api/ を追加した上で `index` を import 済みなので、
# ここでの `import index` は同じモジュールオブジェクト (index.app) を再利用する。
import index


@pytest.fixture(autouse=True)
def _clear_race_analysis_cache():
    jra_ev.RACE_ANALYSIS_CACHE.clear()
    yield
    jra_ev.RACE_ANALYSIS_CACHE.clear()


@pytest.fixture()
def suite_client():
    app = jra_suite.create_app()
    return app.test_client()


def _patch_scoring_noop(monkeypatch):
    monkeypatch.setattr(jra_ev.scoring, "load_score_weights", lambda *_a: {})
    monkeypatch.setattr(jra_ev.scoring, "load_factor_table", lambda *_a: {})
    monkeypatch.setattr(jra_ev.scoring, "compute_score_ml", lambda horse, *_a: (None, []))
    monkeypatch.setattr(jra_ev, "LoggingStore", None)


# ─── jra_ev.analyze_one(): RACE_ANALYSIS_CACHE への保存 ─────────────────────

def test_analyze_one_caches_result_with_cached_at_and_stage(monkeypatch):
    _patch_scoring_noop(monkeypatch)
    url = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=t73b-cache"
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda u, mode: {
        "url": u, "venue": "東京", "race_type": "芝", "dist_val": 1600,
        "race_class": "未勝利", "race_info": "【東京 1R】芝1600m 10:00発走",
        "baba_cond": "良", "race_date": "20260907", "horses": [],
    })

    rec = jra_ev.analyze_one(url, dict(jra_ev.STATE["params"]), stage=5)

    assert rec is not None
    entry = jra_ev.get_cached_analysis(url)
    assert entry is not None
    assert entry["stage"] == 5
    assert entry["race_date"] == "20260907"
    assert entry["cached_at"]  # "HH:MM:SS" 形式の非空文字列
    assert entry["result"]["race_info"] == "【東京 1R】芝1600m 10:00発走"


def test_analyze_one_does_not_cache_analysis_excluded_or_jump(monkeypatch):
    monkeypatch.setattr(jra_ev, "analyze_race_url", lambda u, mode: {
        "analysis_excluded": True, "race_type": "障害", "venue": "小倉",
        "race_num": 1, "race_info": "【小倉 1R】障害2860m",
    })
    url = "fixture://jump"

    result = jra_ev.analyze_one(url, dict(jra_ev.STATE["params"]))

    assert result is None
    assert jra_ev.get_cached_analysis(url) is None


def test_get_cached_analysis_missing_url_returns_none():
    assert jra_ev.get_cached_analysis("https://no-such-url.example/") is None


# ─── 解析開始のリセットでキャッシュがクリアされる ───────────────────────────

def test_analyze_start_endpoint_clears_race_analysis_cache(monkeypatch):
    monkeypatch.setattr(jra_ev, "worker_analyze_all", lambda params: None)
    monkeypatch.setitem(jra_ev.STATE, "status", "idle")
    jra_ev.RACE_ANALYSIS_CACHE["dummy"] = {
        "result": {}, "cached_at": "00:00:00", "stage": None, "race_date": None,
    }

    resp = jra_ev.app.test_client().post("/api/analyze_start", json={})

    assert resp.status_code == 200
    assert jra_ev.RACE_ANALYSIS_CACHE == {}


def test_auto_start_clears_race_analysis_cache(monkeypatch):
    monkeypatch.setattr(jra_ev, "worker_analyze_all", lambda params: None)
    monkeypatch.setitem(jra_ev.STATE, "status", "idle")
    jra_ev.RACE_ANALYSIS_CACHE["dummy"] = {
        "result": {}, "cached_at": "00:00:00", "stage": None, "race_date": None,
    }

    jra_ev._auto_start()

    assert jra_ev.RACE_ANALYSIS_CACHE == {}


# ─── suite: /race/api/scrape のキャッシュ短絡 ──────────────────────────────

def test_race_scrape_returns_cached_result_without_calling_index_scrape(
        suite_client, monkeypatch):
    url = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=t73b-suite-hit"
    jra_ev.RACE_ANALYSIS_CACHE[url] = {
        "result": {"race_info": "キャッシュ 5R", "race_num": 5, "venue": "東京", "horses": []},
        "cached_at": "12:34:56", "stage": 5, "race_date": "20260907",
    }

    def _boom(*_args, **_kwargs):
        raise AssertionError("cache hit時はindex.analyze_race_urlを呼ばないはず")
    monkeypatch.setattr(index, "analyze_race_url", _boom)

    resp = suite_client.post("/race/api/scrape", json={"url": url})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race_info"] == "キャッシュ 5R"
    assert data["cached_from_monitor"] == "12:34:56"
    assert data["monitor_stage"] == 5


def test_race_scrape_mode_detail_bypasses_cache(suite_client, monkeypatch):
    url = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=t73b-suite-detail"
    jra_ev.RACE_ANALYSIS_CACHE[url] = {
        "result": {"race_info": "キャッシュ 5R", "race_num": 5, "venue": "東京", "horses": []},
        "cached_at": "12:34:56", "stage": 5, "race_date": "20260907",
    }
    called = {}

    def _fake(u, mode):
        called["url"], called["mode"] = u, mode
        return {"race_info": "実取得 5R", "race_num": 5, "venue": "東京", "horses": []}
    monkeypatch.setattr(index, "analyze_race_url", _fake)

    resp = suite_client.post("/race/api/scrape", json={"url": url, "mode": "詳細"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race_info"] == "実取得 5R"
    assert "cached_from_monitor" not in data
    assert called == {"url": url, "mode": "詳細"}


def test_race_scrape_force_bypasses_cache(suite_client, monkeypatch):
    url = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=t73b-suite-force"
    jra_ev.RACE_ANALYSIS_CACHE[url] = {
        "result": {"race_info": "キャッシュ 5R", "race_num": 5, "venue": "東京", "horses": []},
        "cached_at": "12:34:56", "stage": 5, "race_date": "20260907",
    }

    def _fake(u, mode):
        return {"race_info": "実取得 5R", "race_num": 5, "venue": "東京", "horses": []}
    monkeypatch.setattr(index, "analyze_race_url", _fake)

    resp = suite_client.post("/race/api/scrape", json={"url": url, "force": True})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race_info"] == "実取得 5R"
    assert "cached_from_monitor" not in data


def test_race_scrape_uncached_url_passes_through(suite_client, monkeypatch):
    def _fake(u, mode):
        return {"race_info": "未キャッシュ 3R", "race_num": 3, "venue": "京都", "horses": []}
    monkeypatch.setattr(index, "analyze_race_url", _fake)

    resp = suite_client.post(
        "/race/api/scrape",
        json={"url": "https://www.jra.go.jp/JRADB/accessD.html?CNAME=t73b-not-cached"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race_info"] == "未キャッシュ 3R"
    assert "cached_from_monitor" not in data
