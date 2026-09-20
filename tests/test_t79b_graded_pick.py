"""SPEC-T79b (重賞データページの出走馬ピックアップ) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T79b-graded-entry-pick.md §3
  1. api.graded_pick.derive_entry_attrs: 出走馬属性 -> 過去傾向の区分ラベル変換
  2. api.graded_pick.score_entry: 合致スコア (収縮つき対数比) の計算
  3. API /graded/api/pick: jra_suite.create_app() の test client で検証
  4. index_graded.html: 文言・セクションidの存在確認 + 既存T79テストの回帰確認
"""
import math
import os

import pytest

import jra_ev
import jra_graded
import jra_suite
from api import graded_pick
from api.graded_pick import derive_entry_attrs, score_entry
from test_t79_graded import _build_fixture_ability_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── §3.1: derive_entry_attrs ──────────────────────────────────────────────

def test_kyaku_uses_corner_mode_of_last_3_races():
    # 3-3-2-1 (c4=1->逃げ) が2走、5-5-6-7 (c4=7->差し) が1走 -> 最頻値の逃げ。
    horse = {"hist": [{"corners": "3-3-2-1"}, {"corners": "3-3-2-1"},
                      {"corners": "5-5-6-7"}], "kyakushitsu": "ー"}
    assert derive_entry_attrs(horse)["kyaku"] == "逃げ"


def test_kyaku_tie_break_prefers_most_recent():
    # 逃げ1票・差し1票の同数 -> hist[0] (直近) 側の逃げを採用。
    horse = {"hist": [{"corners": "3-3-2-1"}, {"corners": "5-5-6-7"}, {"corners": "-"}],
             "kyakushitsu": "ー"}
    assert derive_entry_attrs(horse)["kyaku"] == "逃げ"


@pytest.mark.parametrize("arrow,expected", [
    ("◀◁◁◁", "逃げ"),
    ("◀◀◁◁", "先行"),
    ("◁◀◁◁", "先行"),
    ("◁◀◀◁", "差し"),
    ("◁◁◀◁", "差し"),
    ("◁◁◀◀", "追込"),
    ("◁◁◁◀", "追込"),
    ("ー", None),
])
def test_kyaku_falls_back_to_kyakushitsu_arrow_when_no_corners(arrow, expected):
    horse = {"hist": [], "kyakushitsu": arrow}
    assert derive_entry_attrs(horse)["kyaku"] == expected


@pytest.mark.parametrize("sex_age,expected", [
    ("牡3", "牡3"),
    ("牡6", "牡6+"),
    ("牝5", "牝5"),  # T79実装 (jra_graded._sex_age_label) は牡牝とも6歳以上のみ「+」
    ("牝6", "牝6+"),
    ("セ4", "セ"),
    ("セ10", "セ"),
])
def test_sex_age_label_matches_t79_bucketing(sex_age, expected):
    # SPEC本文の例示 (牝5→牝5+) は T79 の実際の集計関数 jra_graded._sex_age_label
    # (牡牝とも「6歳以上のみ+」) と食い違っているため、スコアが実際に集計行と
    # 一致するよう、本実装は T79 の実装済み関数と同じ境界に合わせている
    # (完了報告に逸脱として明記)。
    horse = {"sex_age": sex_age}
    assert derive_entry_attrs(horse)["sex_age"] == expected


def test_sex_age_label_ignores_trailing_coat_color():
    # 実データの horse['sex_age'] は "牡5/芦" のように毛色が付く (SPEC調査時に発見)。
    assert derive_entry_attrs({"sex_age": "牡5/芦"})["sex_age"] == "牡5"
    assert derive_entry_attrs({"sex_age": "牝8/鹿"})["sex_age"] == "牝6+"


@pytest.mark.parametrize("weight,expected", [
    (439, "〜439"), (440, "440-479"), (479, "440-479"),
    (480, "480-519"), (519, "480-519"), (520, "520〜"),
])
def test_weight_bucket_boundaries(weight, expected):
    horse = {"current_weight": weight}
    assert derive_entry_attrs(horse)["weight"] == expected


@pytest.mark.parametrize("days,expected", [
    (14, "中1週以下"), (15, "中2-3週"), (28, "中2-3週"),
    (29, "中4-8週"), (63, "中4-8週"), (64, "中9週以上"),
])
def test_interval_bucket_boundaries(days, expected):
    horse = {"interval_days": days}
    assert derive_entry_attrs(horse)["interval"] == expected


def test_prev_race_resolves_to_graded_key_when_known():
    horse = {"hist": [{"race_name": "神戸新聞杯", "rank": "1", "place": "阪神",
                       "track_type": "芝", "distance": 2200}]}
    known_keys = {"神戸新聞": {"place": "阪神", "track_type": "芝", "distance": 2200}}
    attrs = derive_entry_attrs(horse, known_keys=known_keys, sponsors=[], aliases={})
    assert attrs["prev_race"] == "神戸新聞"
    assert attrs["prev_rank"] == "1着"


def test_prev_race_falls_back_to_normalized_name_when_not_graded():
    horse = {"hist": [{"race_name": "未勝利", "rank": "5"}]}
    known_keys = {"神戸新聞": {"place": "阪神", "track_type": "芝", "distance": 2200}}
    attrs = derive_entry_attrs(horse, known_keys=known_keys, sponsors=[], aliases={})
    assert attrs["prev_race"] == "未勝利"
    assert attrs["prev_rank"] == "4-9着"


def test_prev_race_none_when_no_history():
    assert derive_entry_attrs({"hist": []})["prev_race"] is None
    assert derive_entry_attrs({})["prev_rank"] is None


def test_popularity_prefers_ev_pop_over_odds_rank_fallback():
    horse = {"pop": "9"}  # analyze_race_urlのオッズ順位フォールバック
    ev_horse = {"pop": 1}  # EV監視の実際の人気
    assert derive_entry_attrs(horse, ev_horse=ev_horse)["popularity"] == "1番人気"
    assert derive_entry_attrs(horse)["popularity"] == "7-9番人気"


@pytest.mark.parametrize("pop,expected", [
    (1, "1番人気"), (2, "2番人気"), (3, "3番人気"),
    (4, "4-6番人気"), (6, "4-6番人気"), (7, "7-9番人気"),
    (9, "7-9番人気"), (10, "10番人気以下"),
])
def test_popularity_bucket_boundaries(pop, expected):
    assert derive_entry_attrs({"pop": str(pop)})["popularity"] == expected


@pytest.mark.parametrize("w_num,expected", [(1, "1"), (8, "8"), (0, None), (None, None)])
def test_waku_label(w_num, expected):
    assert derive_entry_attrs({"w_num": w_num})["waku"] == expected


def test_affi_passthrough_and_unknown_value_is_none():
    assert derive_entry_attrs({"affi": "美浦"})["affi"] == "美浦"
    assert derive_entry_attrs({"affi": "栗東"})["affi"] == "栗東"
    assert derive_entry_attrs({"affi": "謎"})["affi"] is None


def test_jockey_and_sire_passthrough_trimmed():
    horse = {"jock": " 武豊 ", "sire": "ディープインパクト"}
    attrs = derive_entry_attrs(horse)
    assert attrs["jockey"] == "武豊"
    assert attrs["sire"] == "ディープインパクト"


# ─── §3.2: score_entry ─────────────────────────────────────────────────────

def _lift(fuku_rate, n, overall, k=10):
    shrunk = (fuku_rate * n + overall * k) / (n + k)
    return math.log(shrunk / overall)


def test_score_entry_computes_shrunk_log_lift_and_excludes_popularity_from_ex_pop():
    overall = 0.5
    aggregates = {
        "waku": [{"label": "1", "n": 20, "c1": 8, "c2": 5, "c3": 3, "out": 4, "fuku_rate": 0.8}],
        "popularity": [{"label": "1番人気", "n": 30, "c1": 15, "c2": 9, "c3": 6, "out": 0, "fuku_rate": 0.75}],
        "jockey": [],  # 該当行なし -> 「データ少」でスキップ (0扱い)
    }
    attrs = {"popularity": "1番人気", "waku": "1", "kyaku": None, "sex_age": None,
             "affi": None, "prev_race": None, "prev_rank": None, "sire": None,
             "jockey": "ルメール", "weight": None, "interval": None}

    result = score_entry(attrs, aggregates, overall)

    expected_waku_lift = _lift(0.8, 20, overall)
    expected_pop_lift = _lift(0.75, 30, overall)
    assert result["score"] == pytest.approx(expected_waku_lift + expected_pop_lift, abs=1e-4)
    assert result["score_ex_pop"] == pytest.approx(expected_waku_lift, abs=1e-4)

    factors_in_detail = {d["factor"] for d in result["detail"]}
    assert factors_in_detail == {"waku", "popularity"}  # jockeyは該当行なしでdetailに出ない


def test_score_entry_hits_and_misses_require_n_at_least_8():
    overall = 0.5
    aggregates = {
        # n=20, lift = log(0.7/0.5) = log(1.4) > log(1.25) -> hit
        "waku": [{"label": "1", "n": 20, "c1": 8, "c2": 5, "c3": 3, "out": 4, "fuku_rate": 0.8}],
        # n=10, lift = log(0.3/0.5) = log(0.6) < log(0.7) -> miss
        "kyaku": [{"label": "差し", "n": 10, "c1": 1, "c2": 0, "c3": 0, "out": 9, "fuku_rate": 0.1}],
        # n=5 (<8) -> 閾値を超えていてもhits/missesには出ない (detailには出る)
        "sex_age": [{"label": "牡4", "n": 5, "c1": 1, "c2": 1, "c3": 0, "out": 3, "fuku_rate": 0.4}],
    }
    attrs = {"popularity": None, "waku": "1", "kyaku": "差し", "sex_age": "牡4",
             "affi": None, "prev_race": None, "prev_rank": None, "sire": None,
             "jockey": None, "weight": None, "interval": None}

    result = score_entry(attrs, aggregates, overall)

    hit_factors = {h["factor"] for h in result["hits"]}
    miss_factors = {m["factor"] for m in result["misses"]}
    detail_factors = {d["factor"] for d in result["detail"]}
    assert hit_factors == {"waku"}
    assert miss_factors == {"kyaku"}
    assert "sex_age" not in hit_factors and "sex_age" not in miss_factors
    assert detail_factors == {"waku", "kyaku", "sex_age"}  # n<8でもdetailには載る

    waku_hit = next(h for h in result["hits"] if h["factor"] == "waku")
    assert waku_hit["c123"] == "8-5-3-4"
    assert waku_hit["label"] == "1"


def test_score_entry_prev_rank_maps_to_prev_rank_band_aggregate_key():
    overall = 0.5
    aggregates = {"prev_rank_band": [{"label": "1着", "n": 12, "c1": 6, "c2": 3, "c3": 1,
                                      "out": 2, "fuku_rate": 0.83}]}
    attrs = {"popularity": None, "waku": None, "kyaku": None, "sex_age": None,
             "affi": None, "prev_race": None, "prev_rank": "1着", "sire": None,
             "jockey": None, "weight": None, "interval": None}
    result = score_entry(attrs, aggregates, overall)
    assert {d["factor"] for d in result["detail"]} == {"prev_rank"}
    assert result["score"] > 0


def test_score_entry_jockey_matches_ignoring_width_and_spaces():
    overall = 0.5
    aggregates = {"jockey": [{"label": "ルメール", "n": 15, "c1": 5, "c2": 4, "c3": 3,
                              "out": 3, "fuku_rate": 0.8}]}
    attrs = {"popularity": None, "waku": None, "kyaku": None, "sex_age": None,
             "affi": None, "prev_race": None, "prev_rank": None, "sire": None,
             "jockey": " ﾙﾒｰﾙ ", "weight": None, "interval": None}
    result = score_entry(attrs, aggregates, overall)
    assert {d["factor"] for d in result["detail"]} == {"jockey"}


def test_score_entry_no_matching_row_contributes_zero():
    result = score_entry({"waku": "1", "popularity": None, "kyaku": None, "sex_age": None,
                          "affi": None, "prev_race": None, "prev_rank": None, "sire": None,
                          "jockey": None, "weight": None, "interval": None},
                         {"waku": []}, 0.5)
    assert result["score"] == 0
    assert result["score_ex_pop"] == 0
    assert result["hits"] == [] and result["misses"] == [] and result["detail"] == []


# ─── §3.3: API /graded/api/pick ────────────────────────────────────────────

@pytest.fixture()
def fixture_cache(tmp_path, monkeypatch):
    import build_graded_cache
    ability_db = tmp_path / "ability_fixture.db"
    _build_fixture_ability_db(str(ability_db))
    pedigree_path = tmp_path / "pedigree_cache.json"
    pedigree_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build_graded_cache, "PEDIGREE_PATH", str(pedigree_path))
    out_path = tmp_path / "graded_cache.sqlite"
    build_graded_cache.build_cache(years_from=2023, ability_db_path=str(ability_db),
                                   out_path=str(out_path))
    return str(out_path)


@pytest.fixture()
def suite_client(fixture_cache, monkeypatch):
    monkeypatch.setattr(jra_graded, "CACHE_PATH", fixture_cache)
    # /api/pick のスクレイプ結果TTLキャッシュ (レビュー対応) はプロセス内グローバル
    # なので、テスト間の汚染を防ぐため毎回空の辞書に差し替える。
    monkeypatch.setattr(jra_graded, "_SCRAPE_CACHE", {})
    app = jra_suite.create_app()
    return app.test_client()


_FAKE_URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde12092011110000"


def _fake_scrape_horses():
    return [
        {"num": 1, "name": "ピックA", "odds": 2.0, "pop": "1",
         "sex_age": "牡4", "w_num": 1, "current_weight": 480, "interval_days": 21,
         "affi": "美浦", "sire": "サイアーA", "jock": "騎手A", "kyakushitsu": "ー",
         "hist": [{"race_name": "サンプルG2", "rank": "1", "place": "中山",
                  "track_type": "芝", "distance": 2000, "corners": "3-3-2-1"}],
         "scratched": False},
        {"num": 2, "name": "ピックB", "odds": 99.0, "pop": "9",
         "sex_age": "牝3", "w_num": 2, "current_weight": 440, "interval_days": 90,
         "affi": "栗東", "sire": None, "jock": "騎手B", "kyakushitsu": "ー",
         "hist": [], "scratched": False},
        {"num": 3, "name": "ピック取消", "odds": 10.0, "pop": "3",
         "sex_age": "牡5", "w_num": 3, "current_weight": 460, "interval_days": 30,
         "affi": "美浦", "sire": None, "jock": "騎手C", "kyakushitsu": "ー",
         "hist": [], "scratched": True},
    ]


def test_api_pick_returns_400_for_invalid_url(suite_client):
    resp = suite_client.get("/graded/api/pick?key=サンプル&url=https://example.com/not-jra")
    assert resp.status_code == 400


def test_api_pick_returns_400_for_missing_url(suite_client):
    resp = suite_client.get("/graded/api/pick?key=サンプル")
    assert resp.status_code == 400


def test_api_pick_returns_404_for_unknown_key(suite_client, monkeypatch):
    monkeypatch.setattr(jra_ev, "get_cached_analysis",
                        lambda u: {"result": {"horses": _fake_scrape_horses(),
                                              "venue": "中山", "race_num": 11,
                                              "race_info": "dummy"}})
    resp = suite_client.get(f"/graded/api/pick?key=存在しない&url={_FAKE_URL}")
    assert resp.status_code == 404


def test_api_pick_uses_cached_analysis_and_excludes_scratched_and_orders_by_score(
        suite_client, monkeypatch):
    monkeypatch.setattr(jra_ev, "get_cached_analysis", lambda u: {
        "result": {"horses": _fake_scrape_horses(), "venue": "中山",
                  "race_num": 11, "race_info": "【中山 11R】芝2000m　サンプルG2"},
    } if u == _FAKE_URL else None)
    # EV監視側の実際の人気 (ev_horse.pop) がhorse['pop']より優先されることも確認する。
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "中山", "race_num": 11, "url": _FAKE_URL,
              "horses": [{"num": 2, "pop": 2}]},
    })

    resp = suite_client.get(
        f"/graded/api/pick?key=サンプル&url={_FAKE_URL}&years=10")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["race"]["venue"] == "中山"
    assert data["race"]["race_num"] == 11
    assert data["race"]["url"] == _FAKE_URL
    assert data["race"]["source"] == "cache"
    assert data["key"] == "サンプル"

    nums = [e["num"] for e in data["entries"]]
    assert nums.count(3) == 0  # 取消馬は除外
    assert set(nums) == {1, 2}

    # EV監視のpop=2が、horseのpop="9"より優先される。
    entry2 = next(e for e in data["entries"] if e["num"] == 2)
    assert entry2["pop"] == 2

    # score_ex_pop 降順に並んでいる。
    scores = [e["score_ex_pop"] for e in data["entries"]]
    assert scores == sorted(scores, reverse=True)

    # picks は score_ex_pop 上位 (最大3頭、今回は候補2頭なので2頭とも)。
    assert set(data["picks"]) <= {1, 2}
    assert len(data["picks"]) == len(data["entries"])
    assert data["picks"][0] == data["entries"][0]["num"]

    for entry in data["entries"]:
        assert isinstance(entry["attrs"], dict)
        assert "waku" in entry["attrs"]


def test_api_pick_falls_back_to_scrape_when_not_cached(suite_client, monkeypatch):
    monkeypatch.setattr(jra_ev, "get_cached_analysis", lambda u: None)
    monkeypatch.setitem(jra_ev.STATE, "races", {})

    import api.index as index_api
    monkeypatch.setattr(index_api, "analyze_race_url", lambda url, mode: {
        "horses": _fake_scrape_horses(), "venue": "阪神", "race_num": 9,
        "race_info": "【阪神 9R】芝2000m　サンプルG2",
    })

    resp = suite_client.get(f"/graded/api/pick?key=サンプル&url={_FAKE_URL}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["race"]["source"] == "scrape"
    assert data["race"]["venue"] == "阪神"


def test_api_pick_scrape_result_is_ttl_cached_and_expires(suite_client, monkeypatch):
    # レビュー対応: analyze_race_url の直接スクレイプは毎回数秒かかるため、同じURLは
    # プロセス内TTLキャッシュ (10分・最大32件) から返し、再スクレイプしない。
    monkeypatch.setattr(jra_ev, "get_cached_analysis", lambda u: None)
    monkeypatch.setitem(jra_ev.STATE, "races", {})

    import api.index as index_api
    call_count = {"n": 0}

    def _fake_scrape(url, mode):
        call_count["n"] += 1
        return {"horses": _fake_scrape_horses(), "venue": "阪神", "race_num": 9,
               "race_info": "【阪神 9R】芝2000m　サンプルG2"}
    monkeypatch.setattr(index_api, "analyze_race_url", _fake_scrape)

    fake_now = [1_000_000.0]
    monkeypatch.setattr(jra_graded.time, "time", lambda: fake_now[0])

    resp1 = suite_client.get(f"/graded/api/pick?key=サンプル&url={_FAKE_URL}")
    assert resp1.status_code == 200
    assert resp1.get_json()["race"]["source"] == "scrape"
    assert call_count["n"] == 1

    # 年数を変えて再取得しても、TTL内であればスクレイプし直さず scrape_cache を返す。
    resp2 = suite_client.get(f"/graded/api/pick?key=サンプル&url={_FAKE_URL}&years=5")
    assert resp2.status_code == 200
    assert resp2.get_json()["race"]["source"] == "scrape_cache"
    assert call_count["n"] == 1

    # TTL (10分) を過ぎたら再スクレイプする。
    fake_now[0] += 601
    resp3 = suite_client.get(f"/graded/api/pick?key=サンプル&url={_FAKE_URL}")
    assert resp3.status_code == 200
    assert resp3.get_json()["race"]["source"] == "scrape"
    assert call_count["n"] == 2


def test_api_pick_scrape_failure_returns_502(suite_client, monkeypatch):
    monkeypatch.setattr(jra_ev, "get_cached_analysis", lambda u: None)
    monkeypatch.setitem(jra_ev.STATE, "races", {})

    import api.index as index_api

    def _boom(url, mode):
        raise RuntimeError("network down")
    monkeypatch.setattr(index_api, "analyze_race_url", _boom)

    resp = suite_client.get(f"/graded/api/pick?key=サンプル&url={_FAKE_URL}")
    assert resp.status_code == 502


def test_api_this_week_includes_url(suite_client, monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "r1": {"venue": "中山", "race_num": 11, "start_time": "15:40", "url": _FAKE_URL,
              "race_info": "【中山 11R】芝2000m 15:40発走　サンプルG2",
              "day_label": "テスト当日"},
    })
    resp = suite_client.get("/graded/api/this_week")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["races"]) == 1
    assert data["races"][0]["url"] == _FAKE_URL


# ─── §3.4: index_graded.html ───────────────────────────────────────────────

def test_index_graded_html_has_pick_section_and_disclaimer():
    path = os.path.join(BASE_DIR, "index_graded.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read().replace("\r\n", "\n")
    assert 'id="pickSection"' in html
    assert "購入判断には使っていません" in html
