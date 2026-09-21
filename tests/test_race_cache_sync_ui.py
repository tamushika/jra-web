"""Execute the embedded cache-sync UI in Node with a small offline DOM.

No browser/server/network or racing database is used. These behavior tests cover
late responses, single-flight polling, retained view state and cache-only I/O.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


BOOTSTRAP = r"""
const vm = require('vm');
const assert = require('assert/strict');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const elements = new Map();
const timers = new Map();
const events = new Map();
const documentEvents = new Map();
let timerId = 0;
class Element {
    constructor(tag = 'div', id = '') {
        this.tagName = tag.toUpperCase(); this.id = id; this.children = [];
        this.parentElement = null; this.style = {}; this._text = ''; this._html = '';
        this.value = ''; this.scrollLeft = 0; this.scrollTop = 0;
        this.dataset = new Proxy({}, {set: (obj, key, value) => {obj[key] = String(value); return true;}});
        this.classes = new Set();
        this.classList = {contains: value => this.classes.has(value),
            add: value => this.classes.add(value), remove: value => this.classes.delete(value)};
        if (id) elements.set(id, this);
    }
    set className(value) {this.classes = new Set(value.split(' '));}
    get className() {return Array.from(this.classes).join(' ');}
    set value(value) {this._value = String(value);}
    get value() {return this._value;}
    clearChildren() {
        for (const child of this.children) {child.clearChildren(); if (child.id) elements.delete(child.id);}
        this.children = [];
    }
    set textContent(value) {this.clearChildren(); this._text = String(value);}
    get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
    set innerHTML(value) {
        this.clearChildren(); this._html = String(value);
        if (this.tagName === 'SELECT' && value.includes('<option')) {
            const option = new Element('option'); option.value = ''; this.appendChild(option);
        }
    }
    get innerHTML() {return this._html;}
    get options() {return this.children;}
    appendChild(child) {
        this.children.push(child); child.parentElement = this;
        if (child.id) elements.set(child.id, child);
        return child;
    }
    get nextElementSibling() {
        if (!this.parentElement) return null;
        const siblings = this.parentElement.children;
        return siblings[siblings.indexOf(this) + 1] || null;
    }
    after(child) {
        const siblings = this.parentElement.children;
        siblings.splice(siblings.indexOf(this) + 1, 0, child); child.parentElement = this.parentElement;
    }
    querySelector(selector) {
        const cls = selector.startsWith('.') ? selector.slice(1) : null;
        for (const child of this.children) {
            if (cls && child.classList.contains(cls)) return child;
            const found = child.querySelector(selector); if (found) return found;
        }
        return null;
    }
    addEventListener() {}
    click() {if (this.onclick) this.onclick({stopPropagation() {}});}
    closest() {return this;}
    insertAdjacentElement(_where, child) {if (child.id) elements.set(child.id, child);}
    remove() {if (this.id) elements.delete(this.id);}
}
for (const id of ['urlInput', 'raceInfo', 'babaInfo', 'criteriaList', 'ultraDetails',
    'harabValue', 'horsesTbody', 'historyTbody', 'searchBtn', 'getUrlBtn', 'runAiBtn',
    'runPastDataBtn', 'trackBiasBtn', 'matchClassCheckbox', 'matchConditionCheckbox',
    'pastDataResultsContainer', 'pastDataStatus']) new Element('div', id);
new Element('select', 'historyHorseSelect');
const tableContainer = new Element('div', 'tableContainer');
const activeTab = new Element('div', 'activeTab'); activeTab.classList.add('active');
const document = {
    getElementById: id => elements.get(id) || null,
    createElement: tag => new Element(tag),
    addEventListener: (type, callback) => documentEvents.set(type, callback),
    querySelector: () => null,
    querySelectorAll: selector => {
        if (selector === '.table-container') return [tableContainer];
        if (selector === '#horsesTbody tr[data-horse-num]')
            return elements.get('horsesTbody').children.filter(row => row.dataset.horseNum);
        return [];
    },
};
const window = {
    location: {pathname: input.path || '/race/', search: input.search || '', origin: 'http://localhost:5005'},
    innerWidth: 1200, scrollX: 18, scrollY: 315,
    scrollTo(x, y) {this.scrollX = x; this.scrollY = y;},
    addEventListener: (type, callback) => events.set(type, callback),
};
const sandbox = {
    assert, elements, timers, events, documentEvents, tableContainer, activeTab,
    document, window, location: window.location, console, URLSearchParams, AbortController,
    setTimeout: (callback, delay) => {const id = ++timerId; timers.set(id, {callback, delay}); return id;},
    clearTimeout: id => timers.delete(id),
    fetch: async () => {throw new Error('Unexpected network operation');},
    alert: () => {throw new Error('Unexpected alert');},
};
const context = vm.createContext(sandbox);
const helpers = `
function fixtureView(url = 'race-A', version = '10') {
    currentMarkUrl = url; monitorSync.url = url;
    monitorSync.version = version; monitorSync.capturedAt = '2026-09-19T09:00:00.000001+09:00';
    document.getElementById('urlInput').value = url;
}
function cacheResponse(version = '20', extra = {}) {
    return {available: true, cache_version: version,
        captured_at: '2026-09-19T09:00:00.000002+09:00', age_seconds: 30,
        result: {race_info: 'updated'}, ...extra};
}
function response(value) {return {ok: true, json: async () => value};}
function deferred() {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};}
`;
vm.runInContext(input.source + '\n' + helpers + '\n(async () => {\n' + input.body + '\n})()', context)
    .then(() => process.stdout.write('ok'))
    .catch(error => {console.error(error); process.exitCode = 1;});
"""


def run_ui(body, *, path="/race/", search=""):
    result = subprocess.run(
        [NODE, "-e", BOOTSTRAP],
        input=json.dumps({"source": (ROOT / "script.js").read_text(encoding="utf-8"),
                          "body": body, "path": path, "search": search}),
        text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_sync_is_embedded_only():
    run_ui("""
fixtureView();
let calls = 0;
fetch = async () => {calls++; throw new Error('must not fetch');};
await pollMonitorCache(); scheduleMonitorSync();
assert.equal(calls, 0); assert.equal(timers.size, 0);
""", path="/")


def test_poll_is_single_flight_and_schedules_only_after_completion():
    run_ui("""
fixtureView(); const pending = deferred(); const calls = [];
fetch = (url, options) => {calls.push({url, options}); return pending.promise;};
const first = pollMonitorCache(); await pollMonitorCache();
assert.equal(calls.length, 1); assert.equal(monitorSync.inFlight, true);
assert.equal(calls[0].options.cache, 'no-store');
assert.equal(calls[0].url, 'api/cache?url=race-A');
assert.deepEqual(Array.from(timers.values()).map(timer => timer.delay), [10000]);
pending.resolve(response({available: false, reason: 'not_cached'})); await first;
assert.equal(monitorSync.inFlight, false);
assert.deepEqual(Array.from(timers.values()).map(timer => timer.delay), [15000]);
""")


@pytest.mark.parametrize("reason", ["not_cached", "different_day", "invalid_timestamp"])
def test_cache_miss_never_calls_scrape_or_replaces_view(reason):
    run_ui(f"""
fixtureView(); const calls = [];
fetch = async url => {{calls.push(url); return response({{available: false, reason: {json.dumps(reason)}}});}};
applyMonitorRefresh = () => {{throw new Error('must not replace existing data');}};
await pollMonitorCache();
assert.deepEqual(calls, ['api/cache?url=race-A']);
assert.equal(monitorSync.status, 'unavailable');
assert.equal(monitorSync.reason, {json.dumps(reason)});
assert.match(document.getElementById('monitorFreshness').textContent, /前回取得分/);
""")


def test_same_version_updates_freshness_without_reapplying_race_data():
    run_ui("""
fixtureView();
fetch = async () => response(cacheResponse('10', {age_seconds: 240}));
applyMonitorRefresh = () => {throw new Error('unchanged version must not rerender');};
await pollMonitorCache();
assert.equal(monitorSync.status, 'ready'); assert.equal(monitorSync.ageSeconds, 240);
assert.match(document.getElementById('monitorFreshness').textContent, /予定取得待ち/);
assert.doesNotMatch(document.getElementById('monitorFreshness').textContent, /故障/);
""")


def test_same_version_refreshes_only_local_ev_summary():
    run_ui("""
fixtureView(); let summaryCalls = 0;
window.lastRaceData = {venue: '東京', race_num: 1};
fetch = async url => {
    assert.equal(url, 'api/cache?url=race-A'); return response(cacheResponse('10'));
};
applyMonitorRefresh = () => {throw new Error('same version must not rerender');};
renderEvSummary = data => {assert.equal(data, window.lastRaceData); summaryCalls++;};
await pollMonitorCache();
assert.equal(summaryCalls, 1);
""")


def test_older_timestamp_or_nanosecond_version_cannot_replace_newer_view():
    run_ui("""
fixtureView();
assert.equal(monitorCacheIsNewer(cacheResponse('9')), false);
assert.equal(monitorCacheIsNewer(cacheResponse('11')), true);
assert.equal(monitorCacheIsNewer(cacheResponse('11', {captured_at: '2026-09-19T08:59:59+09:00'})), false);
monitorSync.version = '1789776000000000002';
assert.equal(monitorCacheIsNewer(cacheResponse('1789776000000000001')), false);
assert.equal(monitorCacheIsNewer(cacheResponse('1789776000000000003')), true);
fetch = async () => response(cacheResponse('9'));
applyMonitorRefresh = () => {throw new Error('older version must not apply');};
await pollMonitorCache();
assert.equal(monitorSync.version, '1789776000000000002');
""")


def test_late_response_after_race_switch_is_ignored():
    run_ui("""
fixtureView(); const pending = deferred();
fetch = () => pending.promise;
applyMonitorRefresh = () => {throw new Error('response belongs to previous race');};
const first = pollMonitorCache();
raceViewEpoch++; fixtureView('race-B', '30');
pending.resolve(response(cacheResponse('20'))); await first;
assert.equal(currentMarkUrl, 'race-B'); assert.equal(monitorSync.version, '30');
assert.equal(monitorSync.inFlight, false);
assert.deepEqual(Array.from(timers.values()).map(timer => timer.delay), [15000]);
""")


def test_late_response_after_input_change_is_ignored():
    run_ui("""
fixtureView(); const pending = deferred();
fetch = () => pending.promise;
applyMonitorRefresh = () => {throw new Error('input no longer matches');};
const first = pollMonitorCache(); document.getElementById('urlInput').value = 'race-B';
pending.resolve(response(cacheResponse())); await first;
assert.equal(monitorSync.version, '10'); assert.equal(timers.size, 0);
""")


def test_late_response_after_switching_away_and_back_is_ignored():
    run_ui("""
fixtureView(); const pending = deferred(); fetch = () => pending.promise;
applyMonitorRefresh = () => {throw new Error('old view generation must not apply');};
const first = pollMonitorCache();
raceViewEpoch++; fixtureView('race-B');
raceViewEpoch++; fixtureView('race-A', '15');
pending.resolve(response(cacheResponse('20'))); await first;
assert.equal(monitorSync.version, '15');
""")


def test_late_autopick_response_cannot_override_manual_race_selection():
    run_ui("""
fixtureView(); const pending = deferred(); fetch = () => pending.promise;
const autopicking = autoPickRaceFromEvState();
raceViewEpoch++; fixtureView('race-B'); monitorSync.manualSelection = true;
pending.resolve(response({races: [{url: 'race-C', start_time: '23:59'}]}));
await autopicking;
assert.equal(currentMarkUrl, 'race-B'); assert.equal(window.location.href, undefined);
""")


def test_timeout_aborts_fetch_and_reschedules_without_scraping():
    run_ui("""
fixtureView(); let calls = 0;
fetch = (_url, {signal}) => new Promise((_resolve, reject) => {
    calls++; signal.addEventListener('abort', () => reject(new Error('aborted')));
});
const polling = pollMonitorCache();
Array.from(timers.values()).find(timer => timer.delay === 10000).callback();
await polling;
assert.equal(calls, 1); assert.equal(monitorSync.status, 'error');
assert.equal(monitorSync.inFlight, false);
assert.match(document.getElementById('monitorFreshness').textContent, /同期確認に失敗/);
assert.deepEqual(Array.from(timers.values()).map(timer => timer.delay), [15000]);
""")


def test_passive_refresh_preserves_details_sort_marks_tabs_and_scroll():
    run_ui("""
fixtureView(); activeSortCol = '的中スコア';
allHorseMarks['race-A'] = {'2': '◎'};
const originalHorses = [
    {num: 1, name: 'horse1', score_ml: 1, hist: []},
    {num: 2, name: 'horse2', score_ml: 2, hist: []},
];
globalHorsesData = originalHorses; renderHorsesTable(originalHorses);
document.querySelectorAll('#horsesTbody tr[data-horse-num]')[1].querySelector('.expand-history-btn').click();
const select = document.getElementById('historyHorseSelect');
select.value = '2';
tableContainer.scrollLeft = 43; tableContainer.scrollTop = 87;
document.getElementById('pastDataResultsContainer').style.display = 'block';
fetchTrackBias = () => {throw new Error('passive update must not fetch track bias');};
fetchWindData = () => {throw new Error('passive update must not fetch wind');};
_tryRestorePastData = () => {throw new Error('must preserve existing past-data panel');};
renderMatrix = () => {}; renderNotableSiresTable = () => {}; renderBookData = () => {};
renderEvSummary = () => {};
const data = {race_info: 'Tokyo1R', venue: '東京', race_type: '芝', dist_val: 1600,
    criteria_lines: [], horses: [{...originalHorses[0], score_ml: 4}, {...originalHorses[1], score_ml: 9}],
    cached_from_monitor: '09:00:00', monitor_captured_at: '2026-09-19T09:00:00.000002+09:00',
    monitor_cache_version: '20'};
applyMonitorRefresh(data, 'race-A');
const rows = document.querySelectorAll('#horsesTbody tr[data-horse-num]');
assert.equal(rows.map(row => row.dataset.horseNum).join(','), '2,1');
assert.equal(rows[0].nextElementSibling.classList.contains('history-row'), true);
assert.notEqual(rows[0].nextElementSibling.style.display, 'none');
assert.equal(document.getElementById('historyHorseSelect').value, '2');
assert.equal(getMarks()['2'], '◎'); assert.equal(activeTab.classList.contains('active'), true);
assert.equal(activeSortCol, '的中スコア');
assert.equal(tableContainer.scrollLeft, 43); assert.equal(tableContainer.scrollTop, 87);
assert.equal(window.scrollX, 18); assert.equal(window.scrollY, 315);
assert.equal(document.getElementById('pastDataResultsContainer').style.display, 'block');
assert.equal(monitorSync.version, '20'); assert.equal(apiCache['race-A'].data, data);
""")


def test_ev_summary_late_response_cannot_overwrite_new_race():
    run_ui("""
fixtureView(); const pending = deferred(); fetch = () => pending.promise;
const first = renderEvSummary({venue: '東京', race_num: 1});
raceViewEpoch++; fixtureView('race-B', '30');
pending.resolve(response({races: [{url: 'race-A', venue: '東京', race_num: 1, horses: []}]}));
await first;
assert.equal(document.getElementById('evSummary'), null);
""")


def test_ev_summary_late_response_cannot_overwrite_newer_same_race_summary():
    run_ui("""
fixtureView(); const pending = deferred(); let count = 0;
fetch = () => ++count === 1 ? pending.promise : Promise.resolve(response({races: [
    {url: 'race-A', venue: '東京', race_num: 1, ml_coverage: {ok: false, scored: 1, total: 3}},
]}));
const first = renderEvSummary({venue: '東京', race_num: 1});
await renderEvSummary({venue: '東京', race_num: 1});
const expected = document.getElementById('evSummary').innerHTML;
pending.resolve(response({races: []})); await first;
assert.equal(document.getElementById('evSummary').innerHTML, expected);
""")


@pytest.mark.parametrize("autopick,manual,expected_pick", [(False, False, 0), (True, False, 1), (True, True, 0)])
def test_analysis_done_preserves_manual_selection_and_existing_autopick(autopick, manual, expected_pick):
    run_ui(f"""
fixtureView(); let picked = 0, polled = 0;
startScraping = () => {{}};
autoPickRaceFromEvState = () => {{picked++;}};
pollMonitorCache = () => {{polled++;}};
documentEvents.get('DOMContentLoaded')();
monitorSync.manualSelection = {json.dumps(manual)};
events.get('message')({{origin: window.location.origin, data: {{type: 'jra-analysis-done'}}}});
assert.equal(picked, {expected_pick}); assert.equal(polled, {1 - expected_pick});
""", search="?url=race-A&auto=1" + ("&autopick=1" if autopick else ""))
