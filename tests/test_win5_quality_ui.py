"""Execute WIN5 watch polling and its real renderer without browser/network IO."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for WIN5 UI tests")


@pytest.fixture
def watch_script():
    html = (ROOT / "index_win5.html").read_text(encoding="utf-8")
    helpers = html[html.index("function esc(s)"):html.index("// ── STEP 1")]
    renderer = html[html.index("function isUsableWin5Plan("):html.index("// ── STEP 4")]
    poll_start = html.index("async function pollWatch()")
    poller = html[poll_start:html.index("// SPEC-T74:", poll_start)]
    return helpers + "\n" + renderer + "\n" + poller


def _valid_result():
    return {
        "success": True, "est_hit_rate": 0.125, "alloc_method": "prob",
        "formula": "1×1×1×1×1=1点", "budget": 100,
        "picks": [{
            "idx": i, "venue": "東京", "upset_rank": "B", "k": 1,
            "coverage": 0.6, "is_axis": i == 0,
            "horses": [{"num": 1, "name": f"馬{i}", "score": 10.0, "win_prob": 0.6}],
        } for i in range(5)],
    }


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (input.nan) input.result.est_hit_rate = NaN;
const status = {textContent: '予約済み'};
const area = {innerHTML: '前回の買い目'};
let removed = 0;
const cleared = [];
const notifications = [];
let audioContexts = 0;
let beeps = 0;
const urls = [];
class FakeNotification {
  static permission = 'granted';
  constructor(title, options) { notifications.push({title, options}); }
}
class FakeAudioContext {
  constructor() { audioContexts++; this.currentTime = 0; this.destination = {}; }
  createOscillator() {
    return {frequency: {value: 0}, connect() {}, start() {beeps++;}, stop() {}};
  }
  createGain() { return {gain: {value: 0}, connect() {}}; }
}
const context = {
  watchTimer: 42,
  window: {Notification: FakeNotification, AudioContext: FakeAudioContext},
  Notification: FakeNotification,
  document: {
    getElementById(id) {
      if (id === 'watchStatus') return status;
      if (id === 'watchArea') return area;
      return {classList: {add() {}}};
    },
    querySelectorAll() { return [{classList: {remove() {removed++;}}}]; },
  },
  clearInterval(id) { cleared.push(id); },
  async fetch(url) {
    urls.push(url);
    return {async json() {return {status: 'done', updated_at: '14:45:00', result: input.result};}};
  },
};
vm.createContext(context);
vm.runInContext(input.script, context, {timeout: 2000});
(async () => {
  await context.pollWatch();
  process.stdout.write(JSON.stringify({
    status: status.textContent, html: area.innerHTML, removed, cleared,
    notifications, audioContexts, beeps, urls,
  }));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def _run(watch_script, result, *, nan=False):
    completed = subprocess.run(
        [NODE, "-e", NODE_HARNESS],
        input=json.dumps({"script": watch_script, "result": result, "nan": nan}),
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _assert_unavailable(state):
    assert "判定不可" in state["status"]
    assert "再計算完了" not in state["status"]
    assert "前回の買い目" not in state["html"]
    assert "推定的中率:" not in state["html"]
    assert state["notifications"] == []
    assert state["audioContexts"] == state["beeps"] == 0
    assert state["cleared"] == [42]
    assert state["removed"] >= 1


def test_unavailable_watch_result_renders_failure_without_notification_or_sound(watch_script):
    result = {
        "success": False, "error": "正常ML採点が不足しています",
        "picks": [], "est_hit_rate": None, "alloc_method": "unavailable",
    }

    state = _run(watch_script, result)

    _assert_unavailable(state)
    assert result["error"] in state["html"]
    assert state["urls"] == ["api/win5_watch"]


def test_valid_five_race_result_still_notifies_and_sounds(watch_script):
    state = _run(watch_script, _valid_result())

    assert state["status"] == "再計算完了 (14:45:00 時点のオッズ)"
    assert "推定的中率: 12.50%" in state["html"]
    assert len(state["notifications"]) == 1
    assert "12.50%" in state["notifications"][0]["options"]["body"]
    assert state["notifications"][0]["options"]["tag"] == "win5-watch"
    assert state["audioContexts"] == state["beeps"] == 1
    assert state["cleared"] == [42]


@pytest.mark.parametrize("invalid", [
    "null", "nan", "missing_estimate", "missing_picks", "four_races",
    "negative_estimate", "over_one", "numeric_string",
])
def test_malformed_success_cannot_render_probability_or_raise_alert(watch_script, invalid):
    result = _valid_result()
    if invalid in ("null", "nan"):
        result["est_hit_rate"] = None
    elif invalid == "missing_estimate":
        result.pop("est_hit_rate")
    elif invalid == "missing_picks":
        result.pop("picks")
    elif invalid == "four_races":
        result["picks"].pop()
    elif invalid == "negative_estimate":
        result["est_hit_rate"] = -0.1
    elif invalid == "over_one":
        result["est_hit_rate"] = 1.01
    elif invalid == "numeric_string":
        result["est_hit_rate"] = "0.125"

    state = _run(watch_script, result, nan=invalid == "nan")

    _assert_unavailable(state)


def test_explicit_error_overrides_numerically_complete_result(watch_script):
    result = deepcopy(_valid_result())
    result["error"] = "採点状態を確認してください"

    state = _run(watch_script, result)

    _assert_unavailable(state)
    assert result["error"] in state["html"]
