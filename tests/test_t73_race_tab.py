"""SPEC-T73 (オッズ監視 → レース詳細 (jra-web解析画面) への遷移) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T73-race-detail-tab.md §3
  - suite の Flask test client で /race/ が200でindex.htmlの内容を返す、
    /race/script.js が200、/race/.env /race/ability.db /race/backups/x が404。
  - /race/api/scrape に analyze_race_url を monkeypatch した POST で JSON が返る
    (index app のルートが /race 配下で動く)。
  - / (ポータル) に data-tab="race" のタブと frame-race の iframe が含まれる。
  - jra_ev._slim_state() の各 race に url キーがある (既存キーは不変)。
"""
import pytest

import jra_ev
import jra_suite
# jra_suite が sys.path に api/ を追加した上で `index` を import 済みなので、
# ここでの `import index` は同じモジュールオブジェクト (index.app) を再利用する。
import index


@pytest.fixture()
def suite_client():
    app = jra_suite.create_app()
    return app.test_client()


# ─── /race/ マウント ────────────────────────────────────────────────────────

def test_race_index_page_returns_200_with_urlinput(suite_client):
    resp = suite_client.get("/race/")
    assert resp.status_code == 200
    assert b'id="urlInput"' in resp.data


def test_race_script_js_returns_200(suite_client):
    resp = suite_client.get("/race/script.js")
    assert resp.status_code == 200


@pytest.mark.parametrize("denied_path", [
    "/race/.env",
    "/race/ability.db",
    "/race/backups/x",
])
def test_race_denied_static_paths_return_404(suite_client, denied_path):
    resp = suite_client.get(denied_path)
    assert resp.status_code == 404


def test_race_no_slash_redirects_to_slash(suite_client):
    resp = suite_client.get("/race")
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers.get("Location", "").endswith("/race/")


def test_create_app_does_not_duplicate_static_guard_hook():
    # index.app はモジュール単位のシングルトンなので、create_app() が複数回
    # 呼ばれても before_request フックは1回しか登録されない (補足事項)。
    jra_suite.create_app()
    jra_suite.create_app()
    jra_suite.create_app()
    hooks = index.app.before_request_funcs.get(None, [])
    assert hooks.count(jra_suite._reject_denied_race_static_paths) == 1


# ─── /race/api/scrape (index.py のルートが /race 配下でも動く) ─────────────

def test_race_api_scrape_returns_json(suite_client, monkeypatch):
    def fake_analyze_race_url(url, mode):
        return {"race_info": "テストレース 5R", "race_num": 5, "venue": "東京",
                "horses": []}

    monkeypatch.setattr(index, "analyze_race_url", fake_analyze_race_url)
    resp = suite_client.post(
        "/race/api/scrape",
        json={"url": "https://www.jra.go.jp/JRADB/accessD.html?CNAME=test", "mode": "簡易"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race_info"] == "テストレース 5R"
    assert data["race_num"] == 5


# ─── ポータル (タブシェル) に4つ目のタブが増える ───────────────────────────

def test_portal_has_race_tab_and_iframe(suite_client):
    resp = suite_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'data-tab="race"' in html
    assert 'id="frame-race"' in html
    assert "レース詳細" in html


# ─── jra_ev._slim_state() の races に url キーが追加される ─────────────────

def test_slim_state_races_have_url_key(monkeypatch):
    fake_rec = {
        "venue": "東京", "race_num": 5, "race_info": "5R テスト",
        "start_time": "10:30", "horses": [], "n_picked": 0, "wide_picks": [],
        "checked15": False, "checked5": False, "finished": False,
        "last_update": "10:00:00", "odds_ok": True, "day_label": "",
        "url": "https://www.jra.go.jp/JRADB/accessD.html?CNAME=test",
    }
    monkeypatch.setitem(jra_ev.STATE, "races", {"東京_5": fake_rec})
    slim = jra_ev._slim_state()
    assert len(slim["races"]) == 1
    race = slim["races"][0]
    assert race["url"] == fake_rec["url"]
    # 既存キーは不変
    for key in ("venue", "race_num", "race_info", "start_time", "horses",
                "n_picked", "wide_picks", "checked15", "checked5", "finished",
                "last_update", "odds_ok", "day_label", "rid"):
        assert key in race
