"""Dashboard presentation tests: in-memory DOM/HTTP stubs, no DB or network."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index_perf.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1).split(
    "const initialDate =", 1
)[0]


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def test_dashboard_keeps_legacy_targets_and_has_six_accessible_views():
    parsed = Markup()
    parsed.feed(HTML)
    ids = [attrs["id"] for _, attrs in parsed.nodes if "id" in attrs]
    assert len(ids) == len(set(ids))
    for element_id in (
        "raceBody", "sumBody", "evBody", "evSum", "w5Body", "w5ShadowSummary",
        "vbSection", "vbBody-v1", "vbBody-p5-v1", "vbBody-p3-v1",
        "vbCalibration-p3-v1", "dailyChart", "seriesSelect", "daysBody", "dayBody",
        "status", "dateFilter", "syncBtn", "availableDates",
    ):
        assert element_id in ids
    tabs = [attrs for _, attrs in parsed.nodes if attrs.get("role") == "tab"]
    assert [tab["data-view"] for tab in tabs] == [
        "overview", "models", "ev", "win5", "paper", "details",
    ]
    for tab in tabs:
        assert all(target in ids for target in tab["aria-controls"].split())
    panels = [attrs for _, attrs in parsed.nodes if attrs.get("role") == "tabpanel"]
    assert all("hidden" in panel for panel in panels if panel["data-pane"] != "overview")
    assert HTML.index('id="overviewSection"') < HTML.index('id="raceSection"')


def test_hypothetical_metrics_and_paper_disclaimers_are_explicit():
    assert "異なるモデル間の重複レースは合算しません" in HTML
    assert "実購入の収支ではありません" in HTML
    assert "仮想運用であり実購入・購入推奨ではありません" in HTML
    assert "実運用移行資格はありません" in HTML
    assert "158.6%/100.1%" not in HTML
    assert "pf-table-scroll" in HTML
    assert "overflow-x: auto" in HTML
    assert ".perf-app [hidden] { display: none !important; }" in HTML


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = new Map();
function node(id) {
  if (nodes.has(id)) return nodes.get(id);
  const attrs = {}, listeners = {}, classes = new Set();
  const element = {id, value:'', textContent:'', innerHTML:'', disabled:false, hidden:false,
    dataset:{}, style:{}, tabIndex:0, clientWidth:800,
    classList:{contains(value){return classes.has(value);},toggle(value,on){
      if(on) classes.add(value); else classes.delete(value);
    }},
    setAttribute(key,value){attrs[key]=String(value);},
    getAttribute(key){return attrs[key];},
    addEventListener(key,fn){listeners[key]=fn;},
    querySelectorAll(){return [];},
    focus(){element.focused=true;}, attrs, listeners,
  };
  nodes.set(id,element);
  return element;
}
const views=['overview','models','ev','win5','paper','details'];
const tabs=views.map(view=>{const el=node('tab-'+view);el.dataset.view=view;return el;});
const panels=['overview','details','models','ev','win5','paper','overview','details','details']
  .map((pane,i)=>{const el=node('panel-'+i);el.dataset.pane=pane;return el;});
const metricButtons=['win_rate','tan_roi','fuku_roi'].map(metric=>{
  const el=node('metric-'+metric);el.dataset.metric=metric;return el;
});
const responses=[...(input.responses||[])];
const frames=[];
let resizeCallback=null;
const context={console,Date,URL,history:{replaceState(){}},location:{href:'http://fixture.invalid/perf/'},
  requestAnimationFrame(callback){frames.push(callback);return frames.length;},
  ResizeObserver:class {constructor(callback){resizeCallback=callback;}observe(){}},
  document:{
    getElementById:node,
    querySelector(){return node('container');},
    querySelectorAll(selector){
      if(selector==='.pf-tabs [data-view]')return tabs;
      if(selector==='[data-pane]')return panels;
      if(selector==='.metric-btn')return metricButtons;
      return [];
    },
  },
  fetch:async()=>{
    if(!responses.length)throw new Error('Unexpected request');
    const spec=responses.shift();
    if(spec.error)throw new Error(spec.error);
    return {ok:spec.status===undefined||spec.status<400,status:spec.status||200,json:async()=>spec.data};
  },
};
vm.createContext(context);
vm.runInContext(input.script,context,{timeout:2000});
function snapshot(){
  const fields={};
  for(const [id,el] of nodes)fields[id]={text:el.textContent,html:el.innerHTML,
    value:el.value,hidden:el.hidden,disabled:el.disabled,attrs:el.attrs,
    dataset:el.dataset,tabIndex:el.tabIndex,focused:!!el.focused,style:el.style};
  return JSON.parse(JSON.stringify(fields));
}
(async()=>{
  const results=[];
  for(const action of input.actions){
    if(action.type==='overview')context.setupOverview(action.data);
    else if(action.type==='select'){
      node('overviewModel').value=JSON.stringify(action.key);
      context.renderOverview(false);
    }else if(action.type==='view')context.showDashboardView(action.view);
    else if(action.type==='key'){
      context.setupDashboardNavigation();
      tabs[action.index].listeners.keydown({key:action.key,preventDefault(){}});
    }else if(action.type==='load'){
      node('dateFilter').value=action.date||'';
      await context.load();
    }else if(action.type==='graph'){
      context.setupGraph(action.series);
    }else if(action.type==='resize'){
      context.setupResponsiveGraph();
      for(const width of action.widths){node('dailyChart').clientWidth=width;resizeCallback();}
      if(action.flush!==false)while(frames.length)frames.shift()();
    }else throw new Error('Unexpected action');
    results.push(snapshot());
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


def run_ui(actions, responses=()):
    if NODE is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    result = subprocess.run(
        [NODE, "-e", HARNESS],
        input=json.dumps({"script": SCRIPT, "actions": actions, "responses": responses}),
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


MODELS = [
    {"app": "ev", "model": "conditional_logit", "version": "v1", "config": "base",
     "period": "20260901〜20260919", "races": 24, "settled": 20,
     "win_rate": 25.0, "top3_rate": 55.0, "tan_roi": 82.5, "fuku_roi": 91.0},
    {"app": "win5", "model": "other", "version": "v2", "config": "separate",
     "period": "20260901〜20260919", "races": 10, "settled": 8,
     "win_rate": 12.5, "top3_rate": 37.5, "tan_roi": 150.0, "fuku_roi": 75.0},
]


def payload(**overrides):
    data = {"summary": deepcopy(MODELS), "pending_races": 4, "daily": [],
            "ev": {"sum": {}, "rows": []}, "win5": [], "series": {"models": []}}
    data.update(overrides)
    return data


def model_key(model):
    return [model[key] for key in ("app", "model", "version", "config")]


def test_overview_uses_one_model_not_cross_model_aggregation():
    first, second = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "select", "key": model_key(MODELS[1])},
    ])
    assert first["kpiSample"]["text"] == "20 / 24"
    assert first["kpiWin"]["html"] == "25.0%"
    assert "82.5%" in first["kpiTan"]["html"]
    assert first["kpiPending"]["text"] == "結果待ち 4レース"
    assert second["kpiSample"]["text"] == "8 / 10"
    assert second["kpiWin"]["html"] == "12.5%"
    assert "150.0%" in second["kpiTan"]["html"]
    assert "win5 / other v2 (separate)" in second["overviewScope"]["text"]


def test_model_selection_survives_refetch_and_order_changes():
    last = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "select", "key": model_key(MODELS[1])},
        {"type": "overview", "data": payload(summary=list(reversed(MODELS)))},
    ])[-1]
    assert last["kpiSample"]["text"] == "8 / 10"


def test_pending_results_show_dashes_not_false_zero_percent():
    model = dict(MODELS[0], settled=0, win_rate=0, tan_roi=0, fuku_roi=0)
    state = run_ui([{"type": "overview", "data": payload(summary=[model])}])[0]
    assert state["kpiSample"]["text"] == "0 / 24"
    for key in ("kpiWin", "kpiTan", "kpiFuku"):
        assert state[key]["html"] == "—"
    assert "結果確定待ち" in state["kpiTop3"]["text"]


def test_empty_date_offers_all_period_without_stale_metric_cards():
    loaded, empty = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "overview", "data": payload(summary=[], selected_date="2026-09-20")},
    ])
    assert loaded["overviewMetrics"]["hidden"] is False
    assert empty["overviewMetrics"]["hidden"] is True
    assert empty["overviewEmpty"]["hidden"] is False
    assert empty["overviewModel"]["disabled"] is True
    assert empty["graphDateNote"]["hidden"] is False
    assert "2026-09-20" in empty["overviewScope"]["text"]


def test_tabs_show_only_requested_sections_and_maintain_aria():
    state = run_ui([{"type": "view", "view": "paper"}])[0]
    assert state["panel-5"]["hidden"] is False
    assert all(state[f"panel-{index}"]["hidden"] for index in range(9) if index != 5)
    assert state["tab-paper"]["attrs"]["aria-selected"] == "true"
    assert state["tab-paper"]["tabIndex"] == 0
    assert state["tab-overview"]["tabIndex"] == -1


def test_tab_keyboard_navigation_wraps_and_focuses_target():
    state = run_ui([{"type": "key", "index": 0, "key": "ArrowLeft"}])[0]
    assert state["tab-details"]["focused"] is True
    assert state["tab-details"]["attrs"]["aria-selected"] == "true"
    assert state["panel-1"]["hidden"] is False
    assert state["panel-7"]["hidden"] is False


@pytest.mark.parametrize("failure", [{"error": "offline"}, {"status": 503, "data": {}}])
def test_fetch_failures_warn_stale_values_and_restore_refresh(failure):
    state = run_ui([{"type": "load"}], [failure])[0]
    assert state["status"]["dataset"]["state"] == "error"
    assert "最新とは限りません" in state["status"]["text"]
    assert state["refreshBtn"]["disabled"] is False
    assert state["container"]["attrs"]["aria-busy"] == "false"


def test_successful_date_load_renders_overview_and_keeps_daily_graph_scoped():
    state = run_ui([{"type": "load", "date": "2026-09-19"}], [
        {"data": payload(selected_date="2026-09-19")},
    ])[0]
    assert state["status"]["dataset"]["state"] == "ready"
    assert state["kpiSample"]["text"] == "20 / 24"
    assert state["graphSection"]["style"]["display"] == "none"
    assert state["graphDateNote"]["hidden"] is False
    assert state["refreshBtn"]["disabled"] is False


def test_selected_model_initializes_matching_graph_series():
    state = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "select", "key": model_key(MODELS[1])},
        {"type": "graph", "series": {"models": [dict(item, points=[]) for item in MODELS]}},
    ])[-1]
    assert state["seriesSelect"]["value"] == "model:1"
    assert "表示できる日次データがありません" in state["dailyChart"]["html"]


def test_graph_selection_tracks_model_identity_not_changed_array_position():
    state = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "select", "key": model_key(MODELS[1])},
        {"type": "graph", "series": {"models": [dict(item, points=[]) for item in MODELS]}},
        {"type": "graph", "series": {"models": [dict(item, points=[]) for item in reversed(MODELS)]}},
    ])[-1]
    assert state["seriesSelect"]["value"] == "model:0"


def test_chart_resize_redraws_only_current_series_at_latest_width_without_fetch():
    points = [{"date": "2026-09-19", "settled": 20, "win_rate": 25,
               "tan_roi": 82.5, "fuku_roi": 91.0}]
    overview, desktop, mobile = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "graph", "series": {"models": [dict(MODELS[0], points=points)]}},
        {"type": "resize", "widths": [600, 390, 320]},
    ])
    assert desktop["dailyChart"]["attrs"]["viewBox"] == "0 0 800 300"
    assert mobile["dailyChart"]["attrs"]["viewBox"] == "0 0 320 300"
    assert mobile["seriesSelect"]["value"] == desktop["seriesSelect"]["value"]
    assert mobile["kpiSample"] == overview["kpiSample"]


def test_hidden_chart_does_not_redraw_to_zero_width():
    points = [{"date": "2026-09-19", "settled": 20, "win_rate": 25}]
    state = run_ui([
        {"type": "overview", "data": payload()},
        {"type": "graph", "series": {"models": [dict(MODELS[0], points=points)]}},
        {"type": "resize", "widths": [0]},
    ])[-1]
    assert state["dailyChart"]["attrs"]["viewBox"] == "0 0 800 300"
