"""Real Chromium regressions using generated pages; no settings, DB, or providers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/web/static"


@pytest.fixture
def generated_theme_site(tmp_path):
    site = tmp_path / "site"
    (site / "static").mkdir(parents=True)
    # Copy only public UI assets, never configuration, databases, or financial data.
    for name in (
        "app.css", "theme.js", "capture-store.js", "capture-outbox.js", "htmx.min.js",
        "manifest.webmanifest", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
    ):
        shutil.copyfile(STATIC / name, site / "static" / name)
    (site / "sw.js").write_text(
        (STATIC / "sw.js").read_text().replace("__CACHE_VERSION__", "generated-theme-regression")
    )
    shutil.copyfile(STATIC / "manifest.webmanifest", site / "manifest.webmanifest")
    templates = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"))
    templates.globals["pending_action_count"] = lambda: 0

    def request(path):
        return Request({"type": "http", "scheme": "http", "path": path,
                        "server": ("testserver", 80), "headers": [], "query_string": b""})

    html = templates.get_template("upload.html").render(
        sandbox_candidate=False, capture_intent="receipt", capture_mode="file",
        proof_run_id="", device_cohort_id="", server_capture_ids=[], active="upload",
        request=request("/upload"),
    )
    # These are generated shell fixtures, not copies of any user's saved pages.
    for name in ("index.html", "upload", "processing"):
        (site / name).write_text(html)
    # Exercise every selector in the two shared workspace focus rules with real
    # keyboard navigation. No accounting/report state is needed for their CSS.
    focus_html = templates.from_string('''{% extends "base.html" %}
      {% block content %}
      <header class="hero"><h1>Generated keyboard focus fixture</h1></header>
      <div class="grid single period-statement-workspace">
        <section class="card">
          <a id="period-total" class="period-total-link stat-card" href="#generated">Generated total</a>
          <a id="period-link" class="link" href="#generated">Generated evidence</a>
          <button id="period-primary" class="primary">Generated action</button>
          <label>Generated period<select id="period-select"><option>Generated month</option></select></label>
        </section>
      </div>
      <div class="grid single close-workspace">
        <section class="card">
          <button id="close-button" class="primary">Generated close</button>
          <a id="close-link" class="link" href="#generated">Generated review</a>
          <label>Generated reason<input id="close-input" class="text-input"></label>
          <label>Generated decision<select id="close-select"><option>Generated option</option></select></label>
          <details><summary id="close-summary">Generated history</summary></details>
        </section>
      </div>
      {% endblock %}''').render(sandbox_candidate=False, request=request("/focus"))
    (site / "focus").write_text(focus_html)
    return site


def run_browser(site, tmp_path, scenario):
    node = shutil.which("node")
    assert node is not None, "Node is required for shared-theme browser regressions"
    chrome = os.environ.get("CHROMIUM_BIN") or shutil.which("chromium")
    if not chrome:
        mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        chrome = str(mac_chrome) if mac_chrome.is_file() else None
    assert chrome, "Chromium is required; set CHROMIUM_BIN to its executable"
    # --user-data-dir does not cover Linux's launcher or fontconfig caches.
    # Supply generated XDG homes, never the caller's profile. On macOS an
    # artificial HOME breaks Chrome's native services; leave it absent there.
    environment = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path),
                   "CHROMIUM_BIN": chrome}
    homes = [("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
             ("XDG_DATA_HOME", "data")]
    if sys.platform != "darwin":
        homes.append(("HOME", "home"))
    for variable, name in homes:
        directory = tmp_path / "browser-environment" / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment[variable] = str(directory)
    result = subprocess.run(
        [node, str(ROOT / "tests/js/test_ui_theme_browser.cjs"),
         str(site), str(tmp_path / "chrome"), scenario],
        cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_first_install_keeps_explicit_theme_and_control_offline_without_http_cache(
    generated_theme_site, tmp_path,
):
    output = run_browser(generated_theme_site, tmp_path, "first-install-offline")
    assert '"themePrecached":true' in output
    assert '"offlineTheme":"dark"' in output
    assert '"serverStopped":true' in output


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_workspace_keyboard_focus_uses_accessible_theme_color(
    generated_theme_site, tmp_path, mode,
):
    output = run_browser(generated_theme_site, tmp_path, "focus-" + mode)
    assert '"keyboardTargets":9' in output
