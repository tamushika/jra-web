"""Race-selector regression checks; fixtures never access the network or a DB."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "style.css").read_text(encoding="utf-8")
SOURCE = (ROOT / "script.js").read_text(encoding="utf-8")
RENDER = SOURCE[SOURCE.index("function renderMatrix("):SOURCE.index("function updateHistoryTable(")]
NODE = shutil.which("node")


def declarations(selector):
    """Find ordinary rules, including comma-separated selector lists."""
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        if selector in [item.strip() for item in selectors.split(",")]:
            return dict(re.findall(r"([\w-]+)\s*:\s*([^;{}]+);", body))
    raise AssertionError(f"CSS selector missing: {selector}")


def test_two_digit_race_buttons_override_shared_padding_without_shrinking():
    rule = declarations(".local-app .r-btn")
    assert rule["padding"] == "4px 0"
    assert rule["flex"] == "0 0 40px"
    assert rule["width"] == rule["min-width"] == rule["max-width"] == "40px"
    assert rule["min-height"] == "40px"
    assert rule["white-space"] == "nowrap"
    assert rule["display"] == "inline-flex"
    assert rule["justify-content"] == "center"
    # Two class selectors outrank .local-app button (one class and one tag).
    assert ".local-app .r-btn".count(".") > ".local-app button".count(".")


def test_matrix_scrolls_inside_flexible_parent_without_compressing_venue_labels():
    container = declarations(".matrix-container")
    assert container["min-width"] == "0"
    assert container["max-width"] == "100%"
    assert container["overflow-x"] == "auto"
    assert container["overscroll-behavior-x"] == "contain"
    row = declarations(".matrix-row")
    assert row["flex-wrap"] == "nowrap"
    assert row["width"] == "max-content"
    assert declarations(".matrix-label")["flex"] == "0 0 150px"
    assert "overflow-x: visible; /* Disable horizontal scrolling */" not in CSS
    assert "flex: 1 1 calc(25% - 8px)" not in CSS


def test_criteria_move_below_selector_at_tablet_width():
    responsive = re.search(r"@media \(max-width: 1100px\)\s*\{(.*?)\n\}", CSS, re.S).group(1)
    assert ".matrix-section { flex-wrap: wrap;" in responsive
    assert ".matrix-container { flex: 1 1 100%; }" in responsive
    assert ".criteria-container { flex: 1 1 100%; width: 100%; min-width: 0; }" in responsive


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function element() {
  return {children: [], className: '', style: {}, textContent: '', value: '',
    appendChild(child) { this.children.push(child); },
    set innerHTML(value) { this.children = []; this.html = value; },
    get innerHTML() { return this.html || ''; },
    get classList() { return {add: name => {this.className += ' ' + name;}}; }
  };
}
const container = element(), urlInput = element(), calls = [];
const context = {
  document: {createElement: element, getElementById(id) {
    if (id === 'matrixContainer') return container;
    if (id === 'urlInput') return urlInput;
    throw new Error('Unexpected DOM access ' + id);
  }},
  raceCache: {'fixture-2': true, 'fixture-12': false}, IS_EMBEDDED: input.embedded,
  monitorSync: {manualSelection: false}, startScraping() {calls.push(urlInput.value);}
};
vm.createContext(context);
vm.runInContext(input.render, context, {timeout: 1000});
context.renderMatrix(input.rows, '阪神');
const rows = container.children.map(row => row.children.map(child => ({
  text: child.textContent, className: child.className
})));
if (container.children.length) container.children[0].children.at(-1).onclick();
process.stdout.write(JSON.stringify({rows, html: container.innerHTML,
  calls, manualSelection: context.monitorSync.manualSelection}));
"""


def render(rows, embedded=True):
    if NODE is None:
        pytest.skip("Node.js required for matrix renderer tests")
    completed = subprocess.run([NODE, "-e", HARNESS],
        input=json.dumps({"render": RENDER, "rows": rows, "embedded": embedded}),
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("embedded", [True, False])
def test_all_twelve_race_buttons_keep_labels_order_status_and_click_target(embedded):
    result = render([{"text": "9/19(土) 4回阪神5日", "races": [
        {"r": n, "url": f"fixture-{n}"} for n in range(12, 0, -1)]}], embedded)
    label, *buttons = result["rows"][0]
    assert label["text"] == "9/19(土) 4回阪神5日"
    assert [button["text"] for button in buttons] == [f"{n}R" for n in range(1, 13)]
    assert "has-star" in buttons[1]["className"]
    assert "visited-no-star" in buttons[-1]["className"]
    assert result["calls"] == ["fixture-12"]
    assert result["manualSelection"] is embedded


@pytest.mark.parametrize("rows", [None, []])
def test_empty_matrix_still_has_explicit_empty_state(rows):
    result = render(rows)
    assert result["rows"] == []
    assert "マトリックスデータなし" in result["html"]
    assert result["calls"] == []
