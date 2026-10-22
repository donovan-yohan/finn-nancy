"""Shared presentation contracts; all runtime inputs are synthetic."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "app/web/static/theme.js"

# Exercise the shipped script without dependencies, a server, or financial storage.
HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const options = JSON.parse(process.argv[1]);
let cookie = options.cookie || '';
const writes = [];
const root = { dataset: {} };
const meta = { content: '', setAttribute(name, value) { this[name] = value; } };
const select = {
  value: 'system', listeners: {},
  addEventListener(name, callback) { this.listeners[name] = callback; }
};
const control = { parentNode: null };
const host = { appendChild(child) { child.parentNode = this; } };
const media = {
  matches: Boolean(options.dark),
  addEventListener(name, callback) { this.listener = callback; },
  addListener(callback) { this.listener = callback; }
};
const ready = {};
const document = {
  documentElement: root, readyState: options.ready ? 'complete' : 'loading',
  querySelector(selector) {
    if (selector === 'meta[name="theme-color"]') return meta;
    if (selector === '[data-theme-select]') return select;
    if (selector === '.theme-control') return control;
    if (selector === '[data-theme-host]' || selector === '.hero, .chat-head') return options.noHeader ? null : host;
    if (selector === '.app-main') return host;
    return null;
  },
  querySelectorAll(selector) { return selector === '[data-theme-select]' ? [select] : []; },
  addEventListener(name, callback) { ready[name] = callback; }
};
Object.defineProperty(document, 'cookie', {
  get() { if (options.blocked) throw new Error('cookies blocked'); return cookie; },
  set(value) {
    if (options.blocked) throw new Error('cookies blocked');
    writes.push(value);
    cookie = value.includes('Max-Age=0') ? '' : value.split(';')[0];
  }
});
const window = {
  matchMedia() { return media; },
  location: { protocol: options.protocol || 'https:' }
};
for (const storage of ['localStorage', 'sessionStorage']) {
  Object.defineProperty(window, storage, { get() { throw new Error('financial storage touched'); } });
}
vm.runInNewContext(fs.readFileSync('app/web/static/theme.js', 'utf8'), { document, window });
const initial = { theme: root.dataset.theme, color: meta.content, writes: [...writes] };
if (options.ready) assert.equal(ready.DOMContentLoaded, undefined);
else {
  assert.equal(typeof ready.DOMContentLoaded, 'function');
  ready.DOMContentLoaded();
}
const selected = select.value;
for (const mode of options.choices || []) {
  select.value = mode;
  select.listeners.change({ target: select });
}
if (options.changeSystem !== undefined) {
  media.matches = options.changeSystem;
  media.listener({ matches: media.matches });
}
console.log(JSON.stringify({
  initial, selected, theme: root.dataset.theme, color: meta.content,
  value: select.value, writes, mounted: control.parentNode === host
}));
"""


def run_theme(**options):
    assert THEME.is_file(), "The shared early theme script has not been implemented"
    node = shutil.which("node")
    assert node is not None, "Node is required for theme runtime checks"
    result = subprocess.run(
        [node, "-e", HARNESS, json.dumps(options)], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_default_theme_follows_system_before_dom_ready():
    for dark, expected, color in ((False, "light", "#ffffff"), (True, "dark", "#000000")):
        result = run_theme(dark=dark)
        assert result["initial"] == {"theme": expected, "color": color, "writes": []}
        assert result["selected"] == "system"
        assert result["mounted"] is True


def test_script_also_connects_when_dom_is_already_ready():
    result = run_theme(ready=True, cookie="finn_nancy_theme=dark")
    assert result["selected"] == "dark"
    assert result["mounted"] is True


def test_headerless_pages_keep_the_control_available_after_content():
    result = run_theme(noHeader=True)
    assert result["mounted"] is True


@pytest.mark.parametrize("mode,system_dark", [("light", True), ("dark", False)])
def test_saved_preference_overrides_os_without_writing_on_load(mode, system_dark):
    result = run_theme(cookie=f"other=ignored; finn_nancy_theme={mode}", dark=system_dark)
    assert result["initial"]["theme"] == mode
    assert result["selected"] == mode
    assert result["writes"] == []


@pytest.mark.parametrize("cookie", [
    "finn_nancy_theme=unknown", "finn_nancy_theme=%E0%A4%A",
    "finn_nancy_theme=dark=light", "finn_nancy_theme=", "finn_nancy_theme=Dark",
    "finn_nancy_theme=dark; finn_nancy_theme=light", "not_finn_nancy_theme=dark",
])
def test_malformed_preference_falls_back_to_system(cookie):
    result = run_theme(cookie=cookie)
    assert result["initial"]["theme"] == "light"
    assert result["selected"] == "system"


def test_preference_changes_use_only_a_scoped_nonfinancial_cookie():
    result = run_theme(choices=["dark", "light"])
    assert result["theme"] == "light"
    assert result["value"] == "light"
    assert len(result["writes"]) == 2
    for mode, cookie in zip(("dark", "light"), result["writes"]):
        assert cookie == f"finn_nancy_theme={mode}; Path=/; Max-Age=31536000; SameSite=Lax; Secure"


def test_system_removes_preference_and_tracks_os_changes():
    result = run_theme(cookie="finn_nancy_theme=light", choices=["system"], changeSystem=True)
    assert result["theme"] == "dark"
    assert result["color"] == "#000000"
    assert result["value"] == "system"
    assert result["writes"] == ["finn_nancy_theme=; Path=/; Max-Age=0; SameSite=Lax; Secure"]


def test_os_changes_do_not_override_explicit_preference():
    result = run_theme(cookie="finn_nancy_theme=dark", changeSystem=False)
    assert result["theme"] == "dark"
    assert result["value"] == "dark"


def test_blocked_cookies_do_not_break_the_control():
    result = run_theme(blocked=True, choices=["dark"])
    assert result["selected"] == "system"
    assert result["theme"] == "dark"
    assert result["value"] == "dark"
    assert result["writes"] == []


def test_unknown_control_value_resets_safely_on_http():
    result = run_theme(choices=["not-a-theme"], protocol="http:")
    assert result["theme"] == "light"
    assert result["value"] == "system"
    assert result["writes"] == ["finn_nancy_theme=; Path=/; Max-Age=0; SameSite=Lax"]


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.nav_links = []
        self.primary_nav = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "nav":
            self.primary_nav = attributes.get("aria-label") == "primary"
        if tag == "a" and self.primary_nav:
            self.nav_links.append(attributes)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.primary_nav = False


@pytest.mark.parametrize("candidate", [False, True])
def test_all_shells_load_early_csp_compatible_theme_and_one_labelled_control(candidate):
    templates = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"))
    templates.globals["pending_action_count"] = lambda: 0
    request = Request({"type": "http", "scheme": "http", "path": "/",
                       "server": ("testserver", 80), "headers": [], "query_string": b""})
    html = templates.get_template("base.html").render(
        sandbox_candidate=candidate, request=request, active="home",
    )
    parsed = Elements(html)
    elements = parsed.elements
    scripts = [attributes for tag, attributes in elements if tag == "script"]
    theme = next((script for script in scripts if script.get("src") == "/static/theme.js"), None)
    assert theme is not None, "All pages need the shared theme script"
    assert not {"defer", "async", "type"}.intersection(theme), "Set theme before styles/paint"
    assert html.index('/static/theme.js') < html.index('/static/app.css')
    select = [attributes for tag, attributes in elements if tag == "select" and "data-theme-select" in attributes]
    assert len(select) == 1
    identifier = select[0]["id"]
    assert any(tag == "label" and attributes.get("for") == identifier for tag, attributes in elements)
    assert "Appearance" in html
    assert [attributes["value"] for tag, attributes in elements if tag == "option"] == ["system", "light", "dark"]
    assert 'onchange=' not in html
    assert 'name="theme-color" content="#ffffff"' in html
    assert any(tag == "a" and attributes.get("href") == "#workspace-content"
               and "skip-link" in attributes.get("class", "").split()
               for tag, attributes in elements)
    assert 'Skip to content</a>' in html
    if candidate:
        assert [link["href"] for link in parsed.nav_links] == [
            "/", "/activity", "/accounts", "/connections",
        ]
        assert parsed.nav_links[0].get("aria-current") == "page"
        for label in ("Home", "Activity", "Accounts", "Connections"):
            assert f"<span>{label}</span>" in html
        assert '/static/capture-store.js' not in html


def theme_tokens(mode):
    css = (ROOT / "app/web/static/app.css").read_text()
    rule = re.search(r':root\[data-theme="' + mode + r'"\]\s*\{([^}]+)\}', css)
    assert rule is not None, f"Missing explicit {mode} palette"
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", rule[1]))


@pytest.mark.parametrize("mode,expected", [
    ("light", {"--bg": "#ffffff", "--panel": "#fafafa", "--panel-2": "#f5f5f5", "--finn": "#0064d9", "--nancy": "#bc4700"}),
    ("dark", {"--bg": "#000000", "--panel": "#0a0a0a", "--panel-2": "#141414", "--finn": "#52a8ff", "--nancy": "#ff8a3d"}),
])
def test_palettes_keep_neutral_surfaces_and_readable_semantics(mode, expected):
    tokens = theme_tokens(mode)
    assert {key: tokens[key] for key in expected} == expected

    def luminance(color):
        values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

    for foreground in ("--text", "--muted", "--finn", "--nancy", "--green", "--red"):
        for surface in ("--bg", "--panel", "--panel-2"):
            levels = sorted((luminance(tokens[foreground]), luminance(tokens[surface])))
            assert (levels[1] + .05) / (levels[0] + .05) >= 4.5, (mode, foreground, surface)
    for accent in ("finn", "nancy"):
        levels = sorted((luminance(tokens["--" + accent]), luminance(tokens["--on-" + accent])))
        assert (levels[1] + .05) / (levels[0] + .05) >= 4.5


def test_shared_foundation_has_spacing_flat_surfaces_and_accessible_controls():
    css = (ROOT / "app/web/static/app.css").read_text()
    for token in ("--space-1", "--space-2", "--space-3", "--space-4", "--space-6", "--gutter", "--radius-sm", "--radius", "--on-accent"):
        assert token + ":" in css
    for navy in ("#090d14", "#111827", "#0f1724", "#05121f", "rgba(9, 13, 20", "rgba(17, 24, 39"):
        assert navy not in css
    assert "gradient(" not in css
    assert "blur(" not in css
    assert re.search(r"input, select, textarea\s*\{[^}]*min-height:\s*(?:44px|var\(--control-size\))", css)
    assert ':focus-visible' in css
    assert '.theme-control' in css and '.theme-control[hidden]' in css
    # Evidence target rings remain meaningful highlights, not floating shadows.
    assert re.search(r"\.period-report-row:target\s*\{[^}]*border-color:\s*var\(--finn\)", css)
    assert re.search(r"\.region\.is-active\s*\{[^}]*background:\s*var\(--finn-soft\)", css)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_role_text_keeps_contrast_on_shared_tinted_surfaces(mode):
    tokens = theme_tokens(mode)

    def luminance(channels):
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

    def channels(color):
        return [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]

    for accent in ("finn", "nancy"):
        foreground = channels(tokens["--" + accent])
        rgba = re.fullmatch(r"rgba\((\d+), (\d+), (\d+), ([.\d]+)\)", tokens["--" + accent + "-soft"])
        assert rgba is not None
        alpha = float(rgba[4])
        tint = [int(value) / 255 for value in rgba.groups()[:3]]
        for surface in ("--bg", "--panel", "--panel-2"):
            background = [a * alpha + b * (1 - alpha) for a, b in zip(tint, channels(tokens[surface]))]
            levels = sorted((luminance(foreground), luminance(background)))
            assert (levels[1] + .05) / (levels[0] + .05) >= 4.5, (mode, accent, surface)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_control_boundaries_are_distinguishable_from_panels(mode):
    tokens = theme_tokens(mode)
    assert "--control-line" in tokens, "Controls need stronger boundaries than row separators"

    def luminance(color):
        values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

    for surface in ("--bg", "--panel", "--panel-2"):
        levels = sorted((luminance(tokens["--control-line"]), luminance(tokens[surface])))
        assert (levels[1] + .05) / (levels[0] + .05) >= 3
    css = (ROOT / "app/web/static/app.css").read_text()
    assert re.search(r"input, select, textarea\s*\{[^}]*border:\s*1px solid var\(--control-line\)", css)
