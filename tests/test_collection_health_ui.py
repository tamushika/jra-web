"""Execute the rendered portal JavaScript with deterministic DOM/fetch/timers.

Only the local Flask root page is rendered. No database, network requests, real
browser, browser timers, or temporary JavaScript files are needed.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jra_suite  # noqa: E402


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for portal UI tests")

HEALTH = {
    "date": "2026-09-19",
    "analysis": {"succeeded": 20, "total": 24, "failed": 0, "excluded": 4},
    "collection": {
        "last_fetch_at": "2026-09-19T03:00:00+09:00",
        "last_odds_at": "2026-09-19T03:00:00+09:00",
        "last_save_at": "2026-09-19T03:00:00+09:00",
        "fetch_errors": 0, "save_errors": 0,
    },
    "stages": {
        "30": {"saved": 8, "waiting": 12, "quality_failed": 0, "missing": 0, "unknown": 0},
        "10": {"saved": 6, "waiting": 14, "quality_failed": 0, "missing": 0, "unknown": 0},
        "2": {"saved": 4, "waiting": 16, "quality_failed": 0, "missing": 0, "unknown": 0},
    },
    "monitor": {"last_iteration_at": "2026-09-19T03:00:00+09:00"},
}


def _state(**changes):
    state = {
        "status": "ready", "races": [{"url": "fixture://race"}],
        "health": deepcopy(HEALTH), "started_at": "09:00:00",
    }
    state.update(changes)
    return state


@pytest.fixture
def portal_script(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Portal UI tests cannot access database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    response = jra_suite.create_app().test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    return scripts[0]


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = new Map();
function element(id) {
  const attrs = new Map();
  const classes = new Set();
  const listeners = new Map();
  return {
    id, textContent: '', disabled: false,
    classList: {toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
                contains(name) { return classes.has(name); }},
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    setAttribute(name, value) { attrs.set(name, value); },
    addEventListener(name, callback) { listeners.set(name, callback); },
    contentWindow: {postMessage() {}},
  };
}
['globalStart', 'globalStatus', 'collectionHealth', 'healthSummary', 'healthPanel'].forEach(id => nodes.set(id, element(id)));
const tabs = ['ev', 'win5', 'perf', 'race'].map(name => {
  const node = element('tab-' + name);
  node.setAttribute('data-tab', name);
  nodes.set('tab-' + name, node);
  const frame = element('frame-' + name);
  frame.setAttribute('data-src', '/' + name + '/');
  nodes.set('frame-' + name, frame);
  return node;
});
const events = new Map();
const timers = new Map();
let timerId = 0;
const calls = [];
const queue = [...input.responses];
const pending = [];
function response(spec) {
  return {ok: spec.status === undefined || spec.status < 400, status: spec.status || 200,
    json() {
      if (spec.badJson) return Promise.reject(new Error('invalid JSON'));
      return Promise.resolve(spec.state);
    }};
}
const context = {
  console, Date, AbortController,
  window: {
    location: {hash: '', origin: 'https://portal.invalid'},
    localStorage: {getItem() {return null;}, setItem() {}},
    addEventListener(name, fn) { events.set(name, fn); },
  },
  document: {
    getElementById(id) { return nodes.get(id) || null; },
    querySelectorAll() { return tabs; },
    querySelector(selector) {
      const match = /data-tab="([^"]+)"/.exec(selector);
      return match ? nodes.get('tab-' + match[1]) : null;
    },
  },
  setTimeout(fn, ms) { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
  clearTimeout(id) { timers.delete(id); },
  fetch(url, options) {
    calls.push({url, cache: options.cache});
    if (!queue.length) return Promise.reject(new Error('Unexpected fetch'));
    const spec = queue.shift();
    if (spec.reject) return Promise.reject(new Error('offline'));
    if (spec.hold) return new Promise((resolve, reject) => {
      pending.push({resolve, reject});
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
    return Promise.resolve(response(spec));
  },
};
vm.createContext(context);
function snapshot() {
  return {
    status: nodes.get('globalStatus').textContent,
    error: nodes.get('globalStatus').classList.contains('error'),
    health: nodes.get('collectionHealth').textContent,
    warning: nodes.get('collectionHealth').classList.contains('warning'),
    compactHealth: nodes.get('healthSummary').textContent,
    panelWarning: nodes.get('healthPanel').classList.contains('warning'),
    disabled: nodes.get('globalStart').disabled,
    delays: [...timers.values()].map(timer => timer.ms).sort((a,b) => a-b),
    calls: calls.length, requests: [...calls],
  };
}
async function flush() { await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  vm.runInContext(input.script, context, {timeout: 2000});
  await flush();
  const results = [snapshot()];
  for (const action of input.actions || []) {
    if (action.type === 'message') {
      events.get('message')({origin: 'https://portal.invalid',
        data: {type: 'jra-win5-fetched', ok: true, message: '取得完了'}});
    } else if (action.type === 'timer') {
      const selected = [...timers.entries()].find(([, timer]) => timer.ms === action.ms);
      if (!selected) throw new Error('Expected timer not found: ' + action.ms);
      timers.delete(selected[0]);
      selected[1].fn();
    } else if (action.type === 'resolve') {
      if (!pending.length) throw new Error('No request pending');
      pending.shift().resolve(response(action.response));
    } else { throw new Error('Unknown action: ' + action.type); }
    await flush();
    results.push(snapshot());
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def _run(portal_script, responses, actions=()):
    payload = {"script": portal_script, "responses": responses, "actions": actions}
    result = subprocess.run(
        [NODE, "-e", NODE_HARNESS], input=json.dumps(payload),
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_initial_ready_state_polls_again_after_fifteen_seconds(portal_script):
    state = _run(portal_script, [{"state": _state()}])[0]

    assert state["calls"] == 1
    assert state["requests"] == [{"url": "/ev/api/state", "cache": "no-store"}]
    assert state["delays"] == [15000]
    assert state["disabled"] is False
    assert state["error"] is False
    assert state["warning"] is False
    assert "解析完了: 1レース" in state["status"]


def test_analyzing_disables_start_and_uses_three_second_poll(portal_script):
    state = _run(portal_script, [{"state": _state(
        status="analyzing", progress={"done": 7, "total": 24})}])[0]

    assert state["status"] == "解析中 7/24"
    assert state["disabled"] is True
    assert state["delays"] == [3000]


@pytest.mark.parametrize("failure", [{"status": 503}, {"reject": True}, {"badJson": True}])
def test_http_network_and_invalid_json_failures_warn_and_retry(portal_script, failure):
    state = _run(portal_script, [failure])[0]

    assert state["error"] is True
    assert state["warning"] is True
    assert "通信失敗" in state["status"]
    assert "15秒後に再確認" in state["health"]
    assert state["disabled"] is False
    assert state["delays"] == [15000]


def test_application_error_is_visible_and_does_not_stop_polling(portal_script):
    state = _run(portal_script, [{"state": _state(error="出馬表の取得失敗")}])[0]

    assert state["status"] == "出馬表の取得失敗"
    assert state["error"] is True
    assert state["delays"] == [15000]


def test_successful_retry_clears_communication_failure_and_warning(portal_script):
    failed, recovered = _run(
        portal_script, [{"reject": True}, {"state": _state()}],
        [{"type": "timer", "ms": 15000}],
    )

    assert failed["error"] is True
    assert recovered["error"] is False
    assert recovered["warning"] is False
    assert "通信失敗" not in recovered["status"]
    assert "通信失敗" not in recovered["health"]
    assert recovered["calls"] == 2
    assert recovered["delays"] == [15000]


def test_inflight_guard_prevents_duplicate_request_and_preserves_retry(portal_script):
    pending, duplicate, resolved = _run(
        portal_script, [{"hold": True}],
        [{"type": "message"}, {"type": "resolve", "response": {"state": _state()}}],
    )

    assert pending["calls"] == duplicate["calls"] == resolved["calls"] == 1
    assert pending["delays"] == duplicate["delays"] == [10000]
    assert resolved["delays"] == [15000]
    assert resolved["error"] is False


def test_request_timeout_warns_then_successful_retry_is_allowed(portal_script):
    pending, timed_out, recovered = _run(
        portal_script, [{"hold": True}, {"state": _state()}],
        [{"type": "timer", "ms": 10000}, {"type": "timer", "ms": 15000}],
    )

    assert pending["delays"] == [10000]
    assert timed_out["error"] is True
    assert timed_out["delays"] == [15000]
    assert recovered["error"] is False
    assert recovered["calls"] == 2
    assert recovered["delays"] == [15000]


def test_health_shows_counts_and_waiting_is_not_missing(portal_script):
    state = _run(portal_script, [{"state": _state()}])[0]

    assert "本日 2026-09-19" in state["health"]
    assert "解析成功 20/24R（失敗 0・対象外 4）" in state["health"]
    assert "30分前: 保存 8・待機 12・品質不足 0・未取得 0・未確認 0" in state["health"]
    assert "10分前: 保存 6・待機 14" in state["health"]
    assert "2分前: 保存 4・待機 16" in state["health"]
    assert "監視巡回" in state["health"]
    assert "未確認 /" not in state["health"]
    assert state["warning"] is False


@pytest.mark.parametrize("field", ["missing", "quality_failed", "unknown"])
def test_stage_missing_quality_failure_and_unknown_raise_warning(portal_script, field):
    health = deepcopy(HEALTH)
    health["stages"]["10"][field] = 2
    state = _run(portal_script, [{"state": _state(health=health)}])[0]

    assert state["warning"] is True
    label = {"missing": "未取得", "quality_failed": "品質不足", "unknown": "未確認"}[field]
    assert label + " 2" in state["health"]


@pytest.mark.parametrize("section,field,value", [
    ("analysis", "failed", 1),
    ("collection", "fetch_errors", 1),
    ("collection", "save_errors", 1),
    ("collection", "last_save_at", None),
])
def test_collection_and_analysis_failures_raise_warning(portal_script, section, field, value):
    health = deepcopy(HEALTH)
    health[section][field] = value
    state = _run(portal_script, [{"state": _state(health=health)}])[0]

    assert state["warning"] is True


def test_missing_health_payload_is_explicitly_unknown_not_healthy(portal_script):
    state = _run(portal_script, [{"state": _state(health=None)}])[0]

    assert state["warning"] is True
    assert "収集状態は未確認" in state["health"]
    assert state["delays"] == [15000]


def test_compact_health_summary_and_multiline_details_are_consistent(portal_script):
    state = _run(portal_script, [{"state": _state()}])[0]

    assert "解析 20/24R" in state["compactHealth"]
    assert "最終保存" in state["compactHealth"]
    assert "未取得 0" in state["compactHealth"]
    assert "取得/保存エラー 0/0" in state["compactHealth"]
    assert state["panelWarning"] is False
    assert "\n30分前:" in state["health"]
    assert "\\n" not in state["health"]


def test_compact_summary_counts_missing_stages_and_flags_panel(portal_script):
    health = deepcopy(HEALTH)
    health["stages"]["30"]["missing"] = 2
    health["stages"]["10"]["missing"] = 1
    state = _run(portal_script, [{"state": _state(health=health)}])[0]

    assert "未取得 3" in state["compactHealth"]
    assert state["panelWarning"] is True


def test_compact_summary_warns_offline_then_recovers(portal_script):
    failed, recovered = _run(portal_script, [{"reject": True}, {"state": _state()}],
                             [{"type": "timer", "ms": 15000}])

    assert "接続を確認できません" in failed["compactHealth"]
    assert failed["panelWarning"] is True
    assert "解析 20/24R" in recovered["compactHealth"]
    assert recovered["panelWarning"] is False
