"""Shared shell contracts with synthetic Jinja inputs, never application settings."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/web/static"


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def render(base="foundation_base.html", path="/connections", **context):
    env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"),
                      autoescape=select_autoescape(["html"]))
    env.globals["pending_action_count"] = lambda: 3
    request = Request({"type": "http", "scheme": "http", "path": path,
                       "server": ("fixture.test", 80), "headers": [], "query_string": b""})
    template = env.from_string('{% extends "' + base + '" %}'
                              '{% block content %}<h1>Generated workspace</h1>'
                              '<div class="fn-nancy"><p id="nancy-neutral">Generated planning facts</p></div>{% endblock %}')
    return template.render(request=request, **context)


def test_compatibility_base_uses_exactly_the_same_shared_foundation():
    legacy = render("base.html")
    foundation = render()
    assert legacy == foundation
    assert "/static/app.css" not in legacy
    sheets = [a["href"] for tag, a in Elements(legacy).nodes
              if tag == "link" and a.get("rel") == "stylesheet"]
    assert sheets == ["/static/foundation/vendor/bootstrap.min.css",
                      "/static/foundation/tokens.css", "/static/foundation/components.css"]
    assert legacy.index("/static/theme.js") < legacy.index(sheets[0])


@pytest.mark.parametrize("base", ["base.html", "foundation_base.html"])
def test_shared_shell_retains_capture_navigation_and_accessibility_hooks(base):
    html = render(base)
    nodes = Elements(html).nodes
    ids = [a["id"] for _, a in nodes if "id" in a]
    assert len(ids) == len(set(ids)), "Stable hooks must have one owner"
    for identifier in ("workspace-content", "toast", "capture-announcer", "processing-nav-count",
                       "capture-camera-input", "capture-files-input", "appearance"):
        assert identifier in ids
    camera = next(a for _, a in nodes if a.get("id") == "capture-camera-input")
    files = next(a for _, a in nodes if a.get("id") == "capture-files-input")
    assert camera["capture"] == "environment"
    assert camera["data-source"] == "camera"
    assert files["data-source"] == "file" and "multiple" in files
    assert camera["data-intent"] == files["data-intent"] == "receipt"
    assert camera["name"] == files["name"] == "files"
    for endpoint in ("/connections", "/processing", "/statements", "/actions", "/close",
                     "/recon", "/review", "/backlog", "/categories", "/manage", "/goals", "/chat"):
        assert any(tag == "a" and a.get("href") == endpoint for tag, a in nodes)
    assert 'aria-current="page"' in html
    assert 'data-theme-host' in html
    assert 'class="theme-control"' in html
    assert html.count('data-theme-select') == 1
    assert 'navigator.serviceWorker.register' in html
    assert 'htmx:afterOnLoad' in html


def test_sandbox_keeps_separate_navigation_and_never_mounts_capture_transport():
    html = render(sandbox_candidate=True, path="/accounts", active="accounts")
    assert 'href="/accounts" aria-current="page"' in html
    for forbidden in ("capture-store.js", "capture-outbox.js", "navigator.serviceWorker.register",
                      'rel="manifest"', "data-capture-input", 'href="/upload"'):
        assert forbidden not in html
    assert 'sandbox-candidate' in html


def test_offline_shell_precaches_shared_foundation_and_page_styles():
    worker = (STATIC / "sw.js").read_text()
    for asset in ("foundation/vendor/bootstrap.min.css", "foundation/tokens.css",
                  "foundation/components.css"):
        assert '"/static/' + asset + '"' in worker
    assert "cacheShell" in worker


def test_shared_macros_escape_content_and_allow_headers_without_actions():
    env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"),
                      autoescape=select_autoescape(["html"]))
    html = env.from_string('''{% import 'foundation_macros.html' as ui %}
        {{ ui.page_header(title) }}
        {% call ui.state_message('No matching activity', message) %}
          <a class="btn btn-outline-secondary" href="/activity">Clear filters</a>
        {% endcall %}''').render(title='<script>bad()</script>', message='<img src=x>')
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert '&lt;img src=x&gt;' in html
    assert 'href="/activity"' in html
    assert 'class="fn-state"' in html


def test_base_children_keep_title_styles_content_and_script_blocks():
    env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"))
    request = Request({"type": "http", "path": "/", "headers": []})
    html = env.from_string('''{% extends 'base.html' %}
    {% block title %}Generated title{% endblock %}
    {% block styles %}{{ super() }}<link rel="stylesheet" href="/static/generated.css">{% endblock %}
    {% block content %}<h1>Generated content</h1>{% endblock %}
    {% block scripts %}<script src="/static/generated.js"></script>{% endblock %}''').render(request=request)
    for text in ("Generated title", "Generated content", "/static/generated.css", "/static/generated.js"):
        assert text in html
    assert html.count("/static/foundation/components.css") == 1


def test_generated_jinja_shell_in_real_chromium(tmp_path):
    """One owned browser covers both bases/themes and first-install offline."""
    from tests.test_household_onboarding_ui import render as render_connections

    site = tmp_path / "site"
    site.mkdir()
    shutil.copytree(STATIC, site / "static")
    (site / "sw.js").write_text((STATIC / "sw.js").read_text().replace(
        "__CACHE_VERSION__", "generated-design-shell"))
    shutil.copyfile(STATIC / "manifest.webmanifest", site / "manifest.webmanifest")
    for route, base in (("index.html", "foundation_base.html"), ("upload", "base.html"),
                        ("processing", "foundation_base.html")):
        html = render(base, path="/" if route == "index.html" else "/" + route)
        (site / route).write_text(html)
    (site / "connections").write_text(render_connections())
    (site / "generated.css").write_text(".generated-offline{min-height:48px}")
    # A page-owned stylesheet was not navigated to before install. It must be
    # discovered from the cached document, not rescued by the HTTP cache.
    processing = (site / "processing").read_text().replace("</head>",
        '<link rel="stylesheet" href="/static/generated.css"></head>')
    (site / "processing").write_text(processing)
    shutil.copyfile(site / "generated.css", site / "static/generated.css")
    proof = Path(os.environ.get("DESIGN_SHELL_PROOF", str(tmp_path / "proof")))
    proof.mkdir(parents=True, exist_ok=True)
    for route in ("index.html", "upload", "processing", "connections"):
        shutil.copyfile(site / route, proof / (route + ".html"))
    chrome = os.environ.get("CHROMIUM_BIN") or shutil.which("chromium")
    if not chrome:
        candidate = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        chrome = str(candidate) if candidate.is_file() else None
    assert chrome, "Set CHROMIUM_BIN for actual shell geometry and offline checks"
    node = shutil.which("node")
    assert node
    environment = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(tmp_path),
                   "CHROMIUM_BIN": chrome}
    process = subprocess.Popen([node, str(ROOT / "tests/js/design_shell.test.cjs"),
                                str(site), str(tmp_path / "chrome"), str(proof)],
                               env=environment, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=120)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    assert process.returncode == 0, stdout + stderr
    receipt = json.loads((proof / "browser-report.json").read_text())
    assert receipt["offline"] is True
    assert receipt["cleanup"] is True
    assert len(receipt["renders"]) == 12
