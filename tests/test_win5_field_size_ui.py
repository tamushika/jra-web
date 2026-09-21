"""WIN5 race-card counts and compact quality UI; synthetic data, no live IO."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest
import requests

import jra_ev
import jra_win5


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Live network IO is forbidden in WIN5 UI tests")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    monkeypatch.setattr(jra_win5, "log_race_prediction", Mock())
    monkeypatch.setattr(jra_win5, "LoggingStore", Mock())


@pytest.mark.parametrize("horses,expected", [
    (None, None), ([], None), ([None], None),
    ([{"num": 1}, {"num": 2, "score": None}], 2),
    ([{"num": 1}, {"num": 2, "scratched": True}], 1),
    ([{"num": 1, "cancelled": True}, {"num": 2, "withdrawn": True},
      {"num": 3, "is_scratched": True}], 0),
])
def test_active_count_is_not_the_scored_count_and_unknown_is_not_zero(horses, expected):
    assert jra_win5._active_field_size(horses) == expected


def test_counts_only_reuse_same_date_same_race_monitor_cards(monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", {
        "ok": {"race_date": "20260919", "venue": "阪神", "race_num": 10,
               "horses": [{"num": 1}, {"num": 2}, {"num": 3, "scratched": True}]},
        "old": {"race_date": "20260918", "venue": "中山", "race_num": 11,
                "horses": [{"num": 1}]},
        "empty": {"race_date": "20260919", "venue": "中京", "race_num": 11,
                  "horses": []},
        "unknown_day": {"venue": "京都", "race_num": 11, "horses": [{"num": 1}]},
    })
    races = [{"venue": venue, "race_num": number} for venue, number in
             [("阪神", 10), ("中山", 11), ("中京", 11), ("京都", 11), ("阪神", 11)]]

    result = jra_win5._fill_field_sizes_from_ev_monitor(races, "20260919")

    assert [race["field_size"] for race in result] == [2, None, None, None, None]
    assert result[0]["field_size_source"] == "ev_monitor"
    assert all(race["field_size_source"] is None for race in result[1:])


def test_unknown_target_date_never_reuses_monitor_count(monkeypatch):
    monkeypatch.setitem(jra_ev.STATE, "races", [{
        "venue": "阪神", "race_num": 11, "horses": [{"num": 1}]}])
    race = {"venue": "阪神", "race_num": 11, "field_size": 18}

    assert jra_win5._fill_field_sizes_from_ev_monitor([race], "")[0]["field_size"] is None


def test_target_endpoint_adds_verified_count_without_extra_fetch(monkeypatch):
    target = {"date": "9月19日", "date_yyyymmdd": "20260919", "races": [
        {"idx": 0, "venue": "阪神", "race_num": 10, "time": "", "url": ""}]}
    monkeypatch.setattr(jra_win5, "_scrape_win5_target", lambda: (deepcopy(target), None))
    monkeypatch.setattr(jra_win5, "_find_win5_urls", lambda *_args: {0: "https://example.invalid/race"})
    monkeypatch.setattr(jra_win5, "_fill_missing_from_siblings", lambda _r, found, _d: found)
    monkeypatch.setitem(jra_ev.STATE, "races", [{
        "race_date": "20260919", "venue": "阪神", "race_num": "10", "horses": [
            {"num": 1}, {"num": 2}, {"num": 3, "cancelled": True}]}])

    data = jra_win5.app.test_client().get("/api/win5_races").get_json()

    assert data["success"] is True
    assert data["races"][0]["field_size"] == 2
    assert data["races"][0]["field_size_source"] == "ev_monitor"


def test_analysis_count_includes_unscored_but_not_cancelled_horses(monkeypatch):
    monkeypatch.setattr(jra_win5.scoring, "load_score_weights", lambda *_a: {"use_ml": True})
    monkeypatch.setattr(jra_win5.scoring, "load_factor_table", lambda *_a: {})
    monkeypatch.setattr(jra_win5, "get_upset", lambda *_a: ("B", 40))
    monkeypatch.setattr(jra_win5.scoring, "assess_ml_score", lambda *_a: {
        "score": None, "details": [], "source": "unavailable", "failure_reason": "初出走"})
    result = {"venue": "阪神", "race_type": "芝", "dist_val": 1600,
              "horses": [{"num": 1}, {"num": 2}, {"num": 3, "withdrawn": True}]}

    data = jra_win5._analyze_one_body(0, "https://example.invalid/race", result)

    assert data["field_size"] == 2
    assert data["field_size_source"] == "analysis"
    assert data["source_url"] == "https://example.invalid/race"
    assert data["probability_quality"]["ok"] is False
    assert data["probability_quality"]["scored"] == 0
    assert data["probability_quality"]["total"] == 2


def _run_js(action, payload):
    if NODE is None:
        pytest.skip("Node.js is required for WIN5 UI tests")
    html = (ROOT / "index_win5.html").read_text(encoding="utf-8")
    helpers = html[html.index("function esc(s)"):html.index("// ── STEP 1")]
    renderer = html[html.index("function renderRaceScores("):html.index("function toggleDetail(")]
    script = "let raceList = []; let analyzed = []; let analysisGeneration = 0;\n" + helpers + renderer
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const context = {document: {getElementById(id) {
  return elements[id] ||= {innerHTML: '', value: '', textContent: '', disabled: false, insertAdjacentHTML(_where, html) { this.innerHTML += html; }};
}}};
vm.createContext(context);
vm.runInContext(input.script, context, {timeout: 2000});
context.payload = input.payload;
if (input.action === 'field') {
  process.stdout.write(JSON.stringify({size: context.fieldSize(input.payload), html: context.fieldSizeHtml(input.payload)}));
} else if (input.action === 'cards') {
  vm.runInContext('raceList = payload; renderTargetCards();', context);
  process.stdout.write(JSON.stringify({html: elements.raceGrid.innerHTML}));
} else if (input.action === 'scores') {
  context.renderRaceScores(context.document.getElementById('scores'), 0, input.payload);
  process.stdout.write(JSON.stringify({html: elements.scores.innerHTML}));
} else if (input.action === 'update') {
  vm.runInContext('raceList = [{}]; updateRaceFieldSize(0, payload);', context);
  process.stdout.write(JSON.stringify({html: elements.fieldSize0.innerHTML, race: vm.runInContext('raceList[0]', context)}));
} else if (input.action === 'url_change') {
  vm.runInContext('raceList = [payload.race]; analyzed = [{horses: [{}]}];', context);
  for (const id of ['scoreArea', 'kaimeArea', 'watchArea']) context.document.getElementById(id).innerHTML = 'STALE';
  context.onRaceUrlChange(0, input.payload.url);
  process.stdout.write(JSON.stringify({elements, race: vm.runInContext('raceList[0]', context),
    analyzed: vm.runInContext('analyzed', context), generation: vm.runInContext('analysisGeneration', context)}));
} else if (input.action === 'matches') {
  process.stdout.write(JSON.stringify({matches: context.analysisMatchesTarget(input.payload.race, input.payload.data)}));
} else if (input.action === 'watch_counts') {
  vm.runInContext('raceList = [{}];', context);
  context.document.getElementById('url0').value = input.payload.current_url;
  context.document.getElementById('fieldSize0').innerHTML = 'CURRENT';
  context.updateWatchFieldSizes(input.payload.races);
  process.stdout.write(JSON.stringify({html: elements.fieldSize0.innerHTML}));
} else if (input.action === 'apply_analysis') {
  vm.runInContext('raceList = [payload.race]; applyAnalyzedRace(0, payload.data);', context);
  process.stdout.write(JSON.stringify({elements, race: vm.runInContext('raceList[0]', context)}));
}
"""
    completed = subprocess.run([NODE, "-e", harness], input=json.dumps({
        "script": script, "action": action, "payload": payload}),
        text=True, encoding="utf-8", capture_output=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("payload,expected", [
    ({}, None), ({"field_size": None}, None), ({"field_size": 0}, 0),
    ({"field_size": 16}, 16), ({"field_size": "16"}, None),
    ({"horses": []}, None), ({"horses": [{"num": 1}, {"num": 2, "cancelled": True}]}, 1),
])
def test_count_renderer_distinguishes_unknown_cancelled_and_obtained(payload, expected):
    result = _run_js("field", payload)
    assert result["size"] == expected
    if expected is None:
        assert "未取得" in result["html"]
        assert "<strong>0</strong>" not in result["html"]
    else:
        assert f"<strong>{expected}</strong>" in result["html"]
        assert "頭出走" in result["html"]


def test_all_five_target_cards_have_count_and_accessible_url_editor():
    result = _run_js("cards", [{
        "venue": "阪神", "race_num": 7 + i, "time": "14:30", "field_size": 8 + i,
        "field_size_source": "ev_monitor", "url": "https://example.invalid/<race>"}
        for i in range(5)])
    assert result["html"].count('class="win5-race-card"') == 5
    assert result["html"].count("頭出走") == 5
    for i in range(5):
        assert f'id="url{i}"' in result["html"]
        assert f'id="fieldSize{i}"' in result["html"]
    assert "&lt;race&gt;" in result["html"]
    assert "オッズ監視の取得済み出馬表" in result["html"]


def test_analysis_updates_card_headcount_even_when_scoring_unavailable():
    result = _run_js("update", {"field_size": 14, "field_size_source": "analysis",
                                "probability_quality": {"ok": False}})
    assert "<strong>14</strong>" in result["html"]
    assert result["race"]["field_size"] == 14


def test_long_quality_reasons_are_collapsed_and_escaped_not_one_overflowing_line():
    result = _run_js("scores", {
        "race_info": "阪神5R <script>", "field_size": 18, "horses": [],
        "probability_quality": {"ok": False, "scored": 0, "total": 18,
                                "missing": [{"num": n, "reason": "初出走 <script>"} for n in range(1, 19)]}})
    assert '<details class="w5-quality is-warning">' in result["html"]
    assert '<details class="w5-quality is-warning" open' not in result["html"]
    assert result["html"].count("<li>") == 18
    assert "確率判定不可" in result["html"]
    assert "<script>" not in result["html"]
    assert "18頭出走" in result["html"]


def test_page_keeps_existing_controls_and_wraps_wide_score_table():
    html = (ROOT / "index_win5.html").read_text(encoding="utf-8")
    for element_id in ["loadBtn", "analyzeBtn", "kaimeBtn", "singleAxis", "watchArea", "raceGrid"]:
        assert f'id="{element_id}"' in html
    assert '<body class="local-app win5-app">' in html
    assert '<div class="table-scroll"><table class="w5-score-table">' in html
    assert 'grid-template-columns: repeat(2,minmax(0,1fr))' in html


def test_manual_url_change_invalidates_counts_labels_time_and_prior_analysis():
    target = {"venue": "阪神", "race_num": 10, "race_date": "20260919"}
    result = _run_js("url_change", {"race": {
        "venue": "阪神", "race_num": 10, "url": "https://example.invalid/old", "time": "14:50",
        "field_size": 16, "field_size_source": "ev_monitor", "target": target},
        "url": "https://example.invalid/edited"})

    assert result["race"]["field_size"] is None
    assert result["race"]["field_size_source"] is None
    assert result["race"]["time"] == ""
    assert result["race"]["target"] == target
    assert result["race"]["urlEdited"] is True
    assert result["elements"]["raceVenue0"]["textContent"] == "レース未確認"
    assert "未取得" in result["elements"]["fieldSize0"]["innerHTML"]
    assert result["elements"]["kaimeBtn"]["disabled"] is True
    assert all(result["elements"][key]["innerHTML"] == "" for key in ["scoreArea", "kaimeArea", "watchArea"])
    assert result["analyzed"] == []
    assert result["generation"] == 1


@pytest.mark.parametrize("change,expected", [
    ({}, True), ({"venue": "中山"}, False), ({"race_num": 11}, False),
    ({"race_date": "20260920"}, False), ({"race_date": None}, False),
])
def test_analysis_must_match_known_win5_target_before_count_is_applied(change, expected):
    identity = {"venue": "阪神", "race_num": 10, "race_date": "20260919"}
    result = _run_js("matches", {"race": {"target": identity}, "data": {**identity, **change}})
    assert result["matches"] is expected


def test_successful_reanalysis_uses_confirmed_metadata_for_card():
    result = _run_js("apply_analysis", {"race": {"venue": "未確認", "race_num": "?", "urlEdited": True},
        "data": {"venue": "阪神", "race_num": 10, "race_info": "【阪神 10R】芝1600m 14:50発走", "field_size": 15}})
    assert result["race"]["urlEdited"] is False
    assert result["race"]["time"] == "14:50"
    assert result["elements"]["raceVenue0"]["textContent"] == "阪神 10R"
    assert "<strong>15</strong>" in result["elements"]["fieldSize0"]["innerHTML"]


@pytest.mark.parametrize("race,changes", [
    ({"field_size": 16, "source_url": "https://example.invalid/current"}, True),
    ({"field_size": 16, "source_url": "https://example.invalid/old"}, False),
    ({"field_size": 16}, False),
    ({"source_url": "https://example.invalid/current", "horses": [{"num": 1}]}, False),
])
def test_watch_count_only_updates_matching_current_url_with_explicit_count(race, changes):
    result = _run_js("watch_counts", {"current_url": "https://example.invalid/current", "races": [race]})
    if changes:
        assert "<strong>16</strong>" in result["html"]
    else:
        assert result["html"] == "CURRENT"
