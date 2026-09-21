"""Course overview regressions using a fixture DOM and deferred, fake weather requests."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
SOURCE = (ROOT / "script.js").read_text(encoding="utf-8")
SCRIPT = SOURCE[SOURCE.index("// ---- course presentation helpers (begin) ----"):
                SOURCE.index("const WIND_LINE_STYLE =")]
EV_SUMMARY = SOURCE[SOURCE.index("async function renderEvSummary("):
                    SOURCE.index("function renderBookData(")]
NODE = shutil.which("node")
VENUES = {"札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata",
          "東京": "tokyo", "中山": "nakayama", "中京": "chukyo", "京都": "kyoto",
          "阪神": "hanshin", "小倉": "kokura"}


HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = {}, pending = [], loads = [], overlay = [];
function element(id) {
  const classes = new Set(), attributes = {}, listeners = {};
  return {id, children: [], parentNode: null, dataset: {}, hidden: false, disabled: false,
    naturalWidth: 1024, naturalHeight: 650, open: false,
    style: {setProperty(name, value) {this[name] = value;}},
    classList: {add(name) {classes.add(name);}, remove(name) {classes.delete(name);},
      contains(name) {return classes.has(name);}},
    appendChild(child) {
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(x => x !== child);
      if (child.id) nodes[child.id] = child;
      child.parentNode = this; this.children.push(child); return child;
    },
    closest() {return nodes.wrapper;},
    setAttribute(name, value) {attributes[name] = value;},
    getAttribute(name) {return Object.prototype.hasOwnProperty.call(attributes,name) ? attributes[name] : null;},
    removeAttribute(name) {delete attributes[name];},
    set src(value) {
      attributes.src = value;
      loads.push({src: value, onload: typeof this.onload, onerror: typeof this.onerror});
      if (input.autoLoad && this.onload) this.onload();
    },
    get src() {return attributes.src || '';},
    set textContent(value) {this.text = value; this.html = '';},
    get textContent() {return this.text || '';},
    set innerHTML(value) {this.html = value; this.text = '';},
    get innerHTML() {return this.html || '';},
    addEventListener(name, listener) {listeners[name] = listener;},
    showModal() {this.open = true;},
    close() {this.open = false; if(listeners.close) listeners.close();}
  };
}
['courseLayoutImage', 'courseMapDialog', 'courseMapViewport', 'courseMapDialogTitle',
 'courseMapHome', 'courseContext', 'windOverlay', 'windLegend', 'courseImageStatus',
 'expandCourseMap', 'courseOfficialLink', 'windStraight', 'windBackstretch',
 'windObservation', 'windDataDisplay', 'wrapper', 'evSummaryHost', 'evSummaryPanel'].forEach(id => nodes[id] = element(id));
nodes.courseMapHome.appendChild(nodes.wrapper);
nodes.wrapper.appendChild(nodes.courseLayoutImage);
nodes.wrapper.appendChild(nodes.windOverlay);
nodes.windOverlay.setAttribute('hidden', '');
nodes.windLegend.hidden = true;
const context = {input, nodes, pending, loads, overlay, console, window: {},
  evSummaryRequestId:0, raceViewEpoch:1, currentMarkUrl:'fixture-race', IS_EMBEDDED:false,
  document: {getElementById(id) {return nodes[id] || null;}, createElement() {return element('');}},
  fetch(url) {return new Promise((resolve, reject) => pending.push({url, resolve, reject}));},
  renderWindOverlay(...args) {overlay.push({render: args});},
  clearWindOverlay(message) {overlay.push({clear: message || ''}); nodes.windOverlay.innerHTML = '';}
};
vm.createContext(context);
vm.runInContext(input.script, context, {timeout: 2000});
vm.runInContext(input.evSummary, context, {timeout: 2000});
nodes.courseMapDialog.addEventListener('close', context.restoreCourseMap);
(async () => {
  const result = await vm.runInContext('(async () => {' + input.program + '})()', context, {timeout: 2000});
  process.stdout.write(JSON.stringify(result));
})().catch(error => {process.stderr.write(error.stack); process.exitCode = 1;});
"""


def run_js(program, **data):
    if NODE is None:
        pytest.skip("Node.js required for course overview renderer tests")
    completed = subprocess.run([NODE, "-e", HARNESS],
        input=json.dumps({"script": SCRIPT, "evSummary": EV_SUMMARY, "program": program, **data}),
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_course_and_wind_live_at_top_with_unique_ids_and_large_image():
    top = HTML.index('<section class="race-course-overview"')
    matrix = HTML.index('<section class="matrix-section"')
    tabs = HTML.index('<div class="tabs">')
    assert top < matrix < tabs
    assert top < HTML.index('id="evSummaryHost"') < matrix
    details_tag = re.search(r'<details\b[^>]*id="evSummaryPanel"[^>]*>', HTML).group()
    assert " open" not in details_tag and "hidden" in details_tag
    for element_id in ("courseLayoutImage", "windDataDisplay", "harabValue", "windOverlay",
                       "courseMapHome", "windStraight", "windBackstretch"):
        assert HTML.count(f'id="{element_id}"') == 1
        assert top < HTML.index(f'id="{element_id}"') < matrix
    image_tag = re.search(r'<img\b[^>]*id="courseLayoutImage"[^>]*>', HTML).group()
    assert "250" not in image_tag and "max-height" not in image_tag
    assert '<dialog id="courseMapDialog"' in HTML
    assert "courseDialog.addEventListener('close', restoreCourseMap)" in SOURCE


def test_ev_summary_refresh_preserves_user_disclosure_state_below_course():
    result = run_js("""
      async function refresh() {
        const promise = renderEvSummary({venue:'阪神',race_num:12});
        pending.at(-1).resolve({ok:true,json:async () => ({races:[{url:'fixture-race',horses:[]}]})});
        await promise;
      }
      await refresh();
      const first = nodes.evSummaryPanel.open;
      await refresh();
      const second = nodes.evSummaryPanel.open;
      nodes.evSummaryPanel.open = true;
      await refresh();
      return {first,second,userOpen:nodes.evSummaryPanel.open,hidden:nodes.evSummaryPanel.hidden,
        parent:nodes.evSummary.parentNode.id,count:nodes.evSummaryHost.children.length};
    """)
    assert result == {"first": False, "second": False, "userOpen": True, "hidden": False,
                      "parent": "evSummaryHost", "count": 1}


@pytest.mark.parametrize("venue,slug", VENUES.items())
def test_all_ten_venues_have_explicit_official_fallback_images(venue, slug):
    result = run_js("return getCoursePresentation(input.data);",
                    data={"venue": venue, "race_type": "芝", "dist_val": 1600})
    official = f"https://www.jra.go.jp/facilities/race/{slug}/course/"
    assert result["officialPage"] == official
    assert result["image"] == official + "img/pic_course_heimenzu.gif"
    assert result["title"] == f"{venue} 芝 1600m"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//evil.invalid/course.gif",
    "https://evil.invalid/course.gif", "https://www.jra.go.jp.evil.invalid/course.gif",
    "https://www.jra.go.jp@evil.invalid/course.gif", "http://www.jra.go.jp/course.gif"])
def test_untrusted_image_urls_are_replaced_with_official_fallback(url):
    result = run_js("return getCoursePresentation(input.data);", data={"venue": "阪神", "course_image": url})
    assert result["image"] == "https://www.jra.go.jp/facilities/race/hanshin/course/img/pic_course_heimenzu.gif"


def test_unknown_venue_has_no_invented_map_and_hanshin_distance_does_not_infer_loop():
    result = run_js("return [getCoursePresentation({}), getCoursePresentation({venue:'阪神', race_type:'芝', dist_val:1400})];")
    assert result[0]["image"] == result[0]["officialPage"] == ""
    assert result[0]["title"] == "競馬場未確認 馬場未確認 距離未確認"
    assert result[1]["title"] == "阪神 芝 1400m"
    assert "内" not in result[1]["title"] and "外" not in result[1]["title"]


@pytest.mark.parametrize("venue,race_type,distance,expected", [
    ("新潟", "芝", 1000, True), ("新潟", "芝", "1000", True),
    ("新潟", "ダート", 1000, False), ("新潟", "芝", 1200, False),
    ("阪神", "芝", 1000, False), ("新潟", "障害", 1000, False)])
def test_only_niigata_turf_1000_is_a_straight_course(venue, race_type, distance, expected):
    result = run_js("return getCoursePresentation(input.data).straightOnly;",
                    data={"venue": venue, "race_type": race_type, "dist_val": distance})
    assert result is expected


def test_new_map_resets_old_state_and_handlers_precede_even_cached_load():
    result = run_js("""
      window.lastWindData = {venue:'東京', dir:290, speed:3};
      nodes.windOverlay.innerHTML = 'old arrows';
      nodes.courseLayoutImage.style.display = 'block';
      updateCourseOverview({venue:'阪神', race_type:'芝', dist_val:1400});
      return {loads, wind:window.lastWindData, arrows:nodes.windOverlay.innerHTML,
        state:nodes.courseImageStatus.dataset.state, display:nodes.courseLayoutImage.style.display,
        failed:nodes.courseLayoutImage.dataset.loadFailed,
        fallback:nodes.wrapper.classList.contains('fallback'), disabled:nodes.expandCourseMap.disabled,
        viewBox:nodes.windOverlay.getAttribute('viewBox')};
    """, autoLoad=True)
    assert result["loads"][0]["onload"] == result["loads"][0]["onerror"] == "function"
    assert result["wind"] is None and result["arrows"] == ""
    assert result["state"] == "ready" and result["display"] == "block"
    assert result["failed"] == "0" and result["fallback"] is False
    assert result["disabled"] is False and result["viewBox"] == "0 0 1024 650"


def test_failed_image_disables_zoom_and_old_image_callbacks_are_ignored():
    result = run_js("""
      updateCourseOverview({venue:'東京'});
      const oldLoad = nodes.courseLayoutImage.onload, oldError = nodes.courseLayoutImage.onerror;
      updateCourseOverview({venue:'阪神'});
      oldLoad(); oldError();
      const before = nodes.courseImageStatus.dataset.state;
      nodes.courseLayoutImage.onerror(); openCourseMap();
      return {before, state:nodes.courseImageStatus.dataset.state, failed:nodes.courseLayoutImage.dataset.loadFailed,
        disabled:nodes.expandCourseMap.disabled, opened:nodes.courseMapDialog.open,
        fallback:nodes.wrapper.classList.contains('fallback'), parent:nodes.wrapper.parentNode.id,
        display:nodes.courseLayoutImage.style.display};
    """)
    assert result["before"] == "loading" and result["state"] == "error"
    assert result["failed"] == "1" and result["disabled"] is True
    assert result["opened"] is False and result["fallback"] is True
    assert result["parent"] == "courseMapHome" and result["display"] == "none"


def test_dialog_moves_same_map_node_and_close_returns_it_without_duplication():
    result = run_js("""
      updateCourseOverview({venue:'阪神'}); nodes.courseLayoutImage.onload();
      const original = nodes.wrapper;
      openCourseMap(); openCourseMap(); setCourseMapZoom(2); setCourseMapZoom(3);
      const shown = {open:nodes.courseMapDialog.open, same:nodes.courseMapViewport.children[0] === original,
        count:nodes.courseMapViewport.children.length, home:nodes.courseMapHome.children.length,
        zoom:nodes.courseMapViewport.style['--course-zoom']};
      closeCourseMap();
      toggleCourseWind(true); const visible = nodes.windOverlay.getAttribute('hidden') === null && !nodes.windLegend.hidden;
      toggleCourseWind(false);
      return {shown, open:nodes.courseMapDialog.open, home:nodes.courseMapHome.children.length,
        same:nodes.courseMapHome.children[0] === original, viewport:nodes.courseMapViewport.children.length,
        visible, hidden:nodes.windOverlay.getAttribute('hidden') !== null && nodes.windLegend.hidden};
    """)
    assert result["shown"] == {"open": True, "same": True, "count": 1, "home": 0, "zoom": "2"}
    assert result["open"] is False and result["home"] == 1 and result["same"] is True
    assert result["viewport"] == 0 and result["visible"] is True and result["hidden"] is True


@pytest.mark.parametrize("old_fails", [False, True])
def test_latest_weather_response_wins_even_when_previous_request_finishes_later(old_fails):
    result = run_js("""
      const first = fetchWindData('東京'), second = fetchWindData('阪神');
      pending[1].resolve({ok:true, json:async () => ({current_weather:{winddirection:70,windspeed:3,time:'2026-09-19T10:00'},timezone:'UTC'})});
      await second;
      const before = nodes.windDataDisplay.innerHTML;
      if (input.oldFails) pending[0].reject(new Error('old failed'));
      else pending[0].resolve({ok:true, json:async () => ({current_weather:{winddirection:0,windspeed:9}})});
      await first;
      return {same:nodes.windDataDisplay.innerHTML === before, wind:window.lastWindData,
        straight:nodes.windStraight.textContent, back:nodes.windBackstretch.textContent,
        observation:nodes.windObservation.textContent, rendered:overlay.filter(x => x.render)};
    """, oldFails=old_fails)
    assert result["same"] is True and result["wind"]["venue"] == "阪神"
    assert result["straight"] == "向かい風" and result["back"] == "追い風"
    assert "2026-09-19T10:00 (UTC)" in result["observation"]
    assert result["rendered"] == [{"render": ["阪神", 70, 3]}]


@pytest.mark.parametrize("weather,failure", [
    ({"winddirection": None, "windspeed": 3}, ""), ({"winddirection": "70", "windspeed": 3}, ""),
    ({"winddirection": 70, "windspeed": -1}, ""), ({"winddirection": 361, "windspeed": 3}, ""),
    ({"winddirection": -1, "windspeed": 3}, ""), ({}, ""), ({}, "network"), ({}, "http")])
def test_invalid_or_failed_weather_clears_previous_summary(weather, failure):
    result = run_js("""
      window.lastWindData = {venue:'阪神',dir:70,speed:3};
      updateWindSummary('向かい風','追い風','old time');
      const request = fetchWindData('阪神');
      if (input.failure === 'network') pending[0].reject(new Error('offline fixture'));
      else pending[0].resolve({ok:input.failure !== 'http',json:async () => ({current_weather:input.weather})});
      await request;
      return {straight:nodes.windStraight.textContent,back:nodes.windBackstretch.textContent,
        observation:nodes.windObservation.textContent,wind:window.lastWindData,
        rendered:overlay.filter(x => x.render),message:nodes.windDataDisplay.textContent};
    """, weather=weather, failure=failure)
    assert result["straight"] == result["back"] == "未取得"
    assert result["observation"] == "" and result["wind"] is None and result["rendered"] == []
    expected_message = "失敗" if failure else "有効な風データがありません"
    assert expected_message in result["message"]


def test_straight_only_course_omits_loop_bias_and_unknown_venue_never_fetches():
    result = run_js("""
      window.lastRaceData = {venue:'新潟',race_type:'芝',dist_val:1000};
      const request = fetchWindData('新潟');
      pending[0].resolve({ok:true,json:async () => ({current_weather:{winddirection:250,windspeed:3}})});
      await request;
      const straight = {front:nodes.windStraight.textContent,back:nodes.windBackstretch.textContent,
        html:nodes.windDataDisplay.innerHTML};
      await fetchWindData('未確認場');
      return {straight,requests:pending.length,wind:window.lastWindData,
        unknown:nodes.windStraight.textContent};
    """)
    assert result["straight"]["front"] == "個別確認" and result["straight"]["back"] == "なし（直線）"
    assert "通常の周回コース向けバイアス判定は表示しません" in result["straight"]["html"]
    assert "先行馬が有利" not in result["straight"]["html"]
    assert result["requests"] == 1 and result["wind"] is None and result["unknown"] == "未対応"
