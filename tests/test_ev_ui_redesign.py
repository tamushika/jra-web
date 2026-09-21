"""Execute EV rendering with fixture DOM only: no database, browser, or network."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HTML = (ROOT / "index_ev.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = new Map();
function element(id) {
  let html = '';
  const classes = new Set();
  const attrs = new Map();
  return {id, textContent: '', style: {}, dataset: {}, details: [],
    classList: {toggle(name, value) {if(value) classes.add(name); else classes.delete(name);},
      contains(name) {return classes.has(name);}},
    setAttribute(name, value) {attrs.set(name, value);},
    getAttribute(name) {return attrs.get(name);},
    get innerHTML() {return html;},
    set innerHTML(value) {
      html = value;
      this.details = [...value.matchAll(/<details\s+data-quality-rid="([^"]*)"([^>]*)>/g)]
        .map(match => ({dataset: {qualityRid: match[1]}, open: /\bopen\b/.test(match[2])}));
    },
    querySelectorAll(selector) {
      if (selector !== 'details[data-quality-rid]') throw new Error('Unexpected selector ' + selector);
      return this.details;
    },
  };
}
['raceArea', 'raceStats', 'summary', 'startBtn', 'monitorSettings', 'notifState'].forEach(id => nodes.set(id, element(id)));
const buttons = ['all', 'picked', 'unavailable'].map(value => {
  const button = element('filter-' + value); button.dataset.raceFilter = value; return button;
});
const context = {input, nodes, buttons, console,
  document: {getElementById(id) {return nodes.get(id) || null;},
    querySelectorAll(selector) {
      if (selector !== '[data-race-filter]') throw new Error('Unexpected selector ' + selector);
      return buttons;
    }},
  setTimeout() {return 1;}, clearTimeout() {},
  fetch() {throw new Error('Network is forbidden in EV UI tests');},
  window: {location: {origin: 'https://offline.invalid'}, parent: {postMessage() {}}},
};
vm.createContext(context);
vm.runInContext(input.script, context, {timeout: 2000});
const output = vm.runInContext(input.program, context, {timeout: 2000});
process.stdout.write(JSON.stringify(output));
"""


def run_js(program, **data):
    if NODE is None:
        pytest.skip("Node.js required for EV renderer tests")
    result = subprocess.run([NODE, "-e", HARNESS],
                            input=json.dumps({"script": SCRIPT, "program": program, **data}),
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def race(rid="fixture-1", *, picked=False, ok=True, total=16):
    return {"rid": rid, "url": "/fixture/1", "venue": "阪神", "race_num": 1,
            "start_time": "09:45", "day_label": "SAMPLE", "horses": [],
            "wide_picks": [], "n_picked": int(picked),
            "probability_quality": {"ok": ok, "total": total, "scored": total if ok else 0,
                "missing": [] if ok else [{"num": i, "reason": "初出走"} for i in range(1, total + 1)]}}


def excluded_race(rid="阪神_4"):
    return {"rid": rid, "url": "/fixture/hanshin-4", "venue": "阪神", "race_num": 4,
            "race_info": "【阪神 4R】障害2970m", "start_time": "11:20",
            "day_label": "9/19(土)", "race_type": "障害", "analysis_excluded": True,
            "excluded_reason": "jump_race",
            "excluded_reason_label": "障害レースのため予測対象外",
            "horses": [], "wide_picks": [], "n_picked": 0}


def test_sixteen_identical_missing_reasons_are_grouped_and_collapsed():
    html = run_js("qualityHtml(input.race)", race=race(ok=False))
    assert html.count("<li>") == 1
    assert html.count("初出走") == 1
    assert "初出走：16頭" in html
    assert "1・2・3・4・5・6・7・8・9・10・11・12・13・14・15・16番" in html
    assert "正常ML採点 0/16頭" in html
    assert "勝率/EV判定不可" in html
    assert "未採点の理由を確認" in html
    assert not re.search(r"<details[^>]*\bopen\b", html)


def test_missing_reasons_are_grouped_without_losing_distinct_causes():
    fixture = race(ok=False, total=3)
    fixture["probability_quality"]["missing"] = [
        {"num": 1, "reason": "初出走"}, {"num": 2, "reason": "モデル未読込"},
        {"num": 3, "reason": None}]
    html = run_js("qualityHtml(input.race)", race=fixture)
    assert html.count("<li>") == 3
    assert all(text in html for text in ("初出走：1頭", "モデル未読込：1頭", "理由未確認：1頭"))


def test_quality_text_and_rid_are_html_escaped():
    fixture = race(rid='\"><img src=x onerror=alert(1)>', ok=False, total=1)
    fixture["probability_quality"]["missing"] = [{"num": "<b>1</b>", "reason": '<script>"&</script>'}]
    html = run_js("qualityHtml(input.race)", race=fixture)
    assert "<script>" not in html and "<img" not in html and "<b>1</b>" not in html
    assert "&lt;script&gt;&quot;&amp;&lt;/script&gt;" in html
    assert "&lt;b&gt;1&lt;/b&gt;" in html
    assert 'data-quality-rid="&quot;&gt;&lt;img' in html


@pytest.mark.parametrize("quality,expected", [(None, "採点状況：未確認"),
    ({"ok": True, "scored": 12, "total": 12}, "正常ML採点 12/12頭")])
def test_good_and_unknown_quality_have_no_unavailable_disclosure(quality, expected):
    fixture = race()
    fixture["probability_quality"] = quality
    html = run_js("qualityHtml(input.race)", race=fixture)
    assert expected in html
    assert "<details" not in html and "勝率/EV判定不可" not in html


@pytest.mark.parametrize("filter_name,visible", [("all", 3), ("picked", 1), ("unavailable", 1)])
def test_filter_changes_visible_races_but_not_total_summary_counts(filter_name, visible):
    fixtures = [race("picked-race", picked=True), race("normal-race"), race("missing-race", ok=False)]
    for i, fixture in enumerate(fixtures, 1):
        fixture["race_num"] = i
    result = run_js("""
      stateCache = {races: input.races}; setRaceFilter(input.filter);
      ({stats: nodes.get('raceStats').innerHTML, summary: nodes.get('summary').innerHTML,
        area: nodes.get('raceArea').innerHTML,
        buttons: buttons.map(b => ({value: b.dataset.raceFilter, active: b.classList.contains('active'),
          pressed: b.getAttribute('aria-pressed')}))})
    """, races=fixtures, filter=filter_name)
    assert re.findall(r"<strong[^>]*>(\d+)", result["stats"]) == ["3", "1", "1"]
    assert f"{visible} / 3レースを表示" in result["summary"]
    assert result["area"].count('class="ev-cell ') == visible
    for button in result["buttons"]:
        assert button["active"] is (button["value"] == filter_name)
        assert button["pressed"] == str(button["value"] == filter_name).lower()


def test_open_details_survives_refresh_and_hide_show_but_respects_user_close():
    result = run_js("""
      stateCache = {races: [input.race]}; render();
      nodes.get('raceArea').details[0].open = true;
      render();
      const afterRefresh = nodes.get('raceArea').details[0].open;
      setRaceFilter('picked');
      const empty = nodes.get('raceArea').innerHTML;
      setRaceFilter('all');
      const afterFilter = nodes.get('raceArea').details[0].open;
      nodes.get('raceArea').details[0].open = false; render();
      ({afterRefresh, afterFilter, afterClose: nodes.get('raceArea').details[0].open, empty})
    """, race=race(ok=False))
    assert result["afterRefresh"] is True
    assert result["afterFilter"] is True
    assert result["afterClose"] is False
    assert "この条件に該当するレースはありません" in result["empty"]


def test_filtered_grid_omits_all_empty_rows_and_keeps_venue_order():
    fixtures = [
        {**race("early-hanshin"), "venue": "阪神", "race_num": 1, "start_time": "09:45"},
        {**race("early-nakayama"), "venue": "中山", "race_num": 1, "start_time": "10:00"},
        {**race("late-hanshin", ok=False), "venue": "阪神", "race_num": 6, "start_time": "15:00"},
        {**race("earlier-nakayama", ok=False), "venue": "中山", "race_num": 4, "start_time": "14:00"},
    ]
    html = run_js("""
      stateCache = {races: input.races}; setRaceFilter('unavailable');
      nodes.get('raceArea').innerHTML
    """, races=fixtures)
    assert re.findall(r"<tr><th>(\d+)R</th>", html) == ["4", "6"]
    assert re.findall(r'<th scope="col">(.*?)</th>', html) == ["阪神", "中山"]
    assert html.count('<td></td>') == 2


def test_jump_placeholder_fills_hanshin_fourth_race_cell_with_explicit_reason():
    fixtures = [
        {**race("hanshin-3"), "venue": "阪神", "race_num": 3, "start_time": "10:45"},
        {**race("nakayama-4"), "venue": "中山", "race_num": 4, "start_time": "11:35"},
    ]
    html = run_js("""
      stateCache = {races: input.races, excluded_races: [input.excluded]}; render();
      nodes.get('raceArea').innerHTML
    """, races=fixtures, excluded=excluded_race())

    assert re.findall(r'<th scope="col">(.*?)</th>', html) == ["阪神", "中山"]
    row = re.search(r"<tr><th>4R</th>(.*?)</tr>", html, re.S).group(1)
    hanshin_cell = re.match(r"<td>(.*?)</td>", row, re.S).group(1)
    assert "阪神4R" in hanshin_cell
    assert "障害レースのため予測対象外" in hanshin_cell
    assert "正常ML採点" not in hanshin_cell


def test_excluded_reason_is_escaped_and_unavailable_filter_remains_ml_only():
    excluded = excluded_race()
    excluded["excluded_reason_label"] = '<img src=x onerror="alert(1)">&対象外'
    escaped = run_js("cellHtml(input.race)", race=excluded)
    assert "<img" not in escaped
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;対象外" in escaped

    unavailable = {**race("missing-race", ok=False), "venue": "阪神", "race_num": 3}
    html = run_js("""
      stateCache = {races: [input.unavailable], excluded_races: [input.excluded]};
      setRaceFilter('unavailable'); nodes.get('raceArea').innerHTML
    """, unavailable=unavailable, excluded=excluded_race())
    assert "正常ML採点 0/16頭" in html
    assert "障害レースのため予測対象外" not in html


def legacy_full_matrix(*, missing=(("阪神", 4),)):
    """Old backends returned monitored races only, without excluded_races."""
    missing = set(missing)
    rows = []
    for venue, offset in (("阪神", 0), ("中山", 15)):
        for race_num in range(1, 13):
            if (venue, race_num) in missing:
                continue
            minutes = 9 * 60 + race_num * 25 + offset
            rows.append({
                **race(f"{venue}-{race_num}"),
                "venue": venue,
                "race_num": race_num,
                "start_time": f"{minutes // 60:02d}:{minutes % 60:02d}",
            })
    return rows


def test_legacy_backend_infers_unique_hanshin_fourth_race_exclusion():
    result = run_js("""
      stateCache = {races: input.races, health: input.health}; render();
      ({area: nodes.get('raceArea').innerHTML,
        stats: nodes.get('raceStats').innerHTML,
        summary: nodes.get('summary').innerHTML})
    """, races=legacy_full_matrix(), health={
        "analysis": {"total": 24, "excluded": 1},
    })

    assert re.findall(r'<th scope="col">(.*?)</th>', result["area"]) == ["阪神", "中山"]
    assert re.findall(r"<tr><th>(\d+)R</th>", result["area"]) == [str(n) for n in range(1, 13)]
    row = re.search(r"<tr><th>4R</th>(.*?)</tr>", result["area"], re.S).group(1)
    hanshin_cell = re.match(r"<td>(.*?)</td>", row, re.S).group(1)
    assert "阪神4R" in hanshin_cell
    assert "障害レースのため予測対象外" in hanshin_cell
    assert re.search(r"<button[^>]*\bdisabled\b", hanshin_cell)
    assert result["area"].count('class="ev-cell excluded"') == 1
    assert "<td></td>" not in result["area"]
    assert re.findall(r"<strong[^>]*>(\d+)", result["stats"])[0] == "23"
    assert "24 / 24レースを表示" in result["summary"]
    assert "対象外 1R" in result["summary"]


@pytest.mark.parametrize("races,analysis", [
    (legacy_full_matrix(missing=(("阪神", 4), ("中山", 7))),
     {"total": 24, "succeeded": 22, "failed": 1, "excluded": 1}),
    (legacy_full_matrix(),
     {"total": 24, "succeeded": 23, "failed": 1, "excluded": 0}),
])
def test_legacy_backend_does_not_guess_when_grid_hole_is_not_uniquely_excluded(
        races, analysis):
    html = run_js("""
      stateCache = {races: input.races, health: {analysis: input.analysis}}; render();
      nodes.get('raceArea').innerHTML
    """, races=races, analysis=analysis)

    assert "障害レースのため予測対象外" not in html
    assert 'class="ev-cell excluded"' not in html


def test_new_backend_empty_excluded_list_is_authoritative_not_inferred():
    html = run_js("""
      stateCache = {races: input.races, excluded_races: [], health: {analysis: input.analysis}};
      render(); nodes.get('raceArea').innerHTML
    """, races=legacy_full_matrix(), analysis={
        "total": 24, "excluded": 1,
    })

    assert "障害レースのため予測対象外" not in html
    assert 'class="ev-cell excluded"' not in html


def test_empty_state_is_distinct_from_empty_filter_and_invalid_filter_resets():
    result = run_js("""
      stateCache = {races: []}; setRaceFilter('not-a-filter');
      ({area: nodes.get('raceArea').innerHTML, summary: nodes.get('summary').innerHTML, filter: raceFilter})
    """)
    assert "監視レースはまだありません" in result["area"]
    assert result["summary"] == ""
    assert result["filter"] == "all"


def test_race_button_uses_escaped_data_attribute_not_interpolated_javascript():
    fixture = race(rid="');alert(1);//")
    html = run_js("cellHtml(input.race)", race=fixture)
    assert 'onclick="openRace(this.dataset.rid)"' in html
    assert 'data-rid="&#39;);alert(1);//"' in html
    assert "openRace('" not in html
    fixture["url"] = None
    assert re.search(r"<button[^>]*\bdisabled\b", run_js("cellHtml(input.race)", race=fixture))


def test_grid_overrides_global_nowrap_with_bounded_cell_layout():
    styles = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
    for selector in (r"\.ev-grid", r"\.ev-grid th, \.ev-grid td", r"\.ev-cell", r"\.ev-quality li"):
        block = re.search(selector + r"\s*\{([^}]+)\}", styles).group(1)
        assert "white-space: normal" in block
    grid = re.search(r"\.ev-grid\s*\{([^}]+)\}", styles).group(1)
    cell = re.search(r"\.ev-cell\s*\{([^}]+)\}", styles).group(1)
    assert "table-layout: fixed" in grid
    assert "min-width: 0" in cell and "overflow-wrap: anywhere" in cell
    assert '<div class="table-scroll"><table class="ev-grid">' in SCRIPT


def test_empty_cells_are_unobtrusive_and_options_collapse_on_small_screens():
    styles = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
    empty = re.search(r"\.ev-grid td:empty\s*\{([^}]+)\}", styles).group(1)
    assert "background: transparent" in empty and "border-color: transparent" in empty
    options = re.search(r"\.ev-options\s*\{([^}]+)\}", styles).group(1)
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in options
    assert re.search(r"@media\s*\(max-width:480px\)\s*\{\s*\.ev-options\s*\{[^}]*grid-template-columns:\s*1fr", styles)
