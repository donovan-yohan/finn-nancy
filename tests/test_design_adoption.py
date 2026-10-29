"""Structural adoption inventory plus generated HTTP proof, not all-state acceptance.

Every template has a page, fragment host or shared owner. Feature/browser suites
still own financial transitions, responsive geometry and adverse-state coverage.
"""
from __future__ import annotations

import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, nodes
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/web/templates"
STATIC = ROOT / "app/web/static"

# Explicit ownership makes a newly added or stranded template a review decision.
PAGE_STYLES = {
    "activity.css": {"dashboard.html", "activity.html", "txn_form.html"},
    "capture.css": {"upload.html", "processing.html", "structured_import.html", "import_run.html"},
    "evidence.css": {"review.html", "recon_review.html", "statement_review.html", "statement_source.html"},
    "close-report.css": {"close.html", "period_statement.html"},
    "nancy.css": {"insights.html", "categories.html", "goals.html", "goals_close.html", "chat.html"},
    "manage.css": {"manage.html", "backlog.html", "approvals.html"},
    "banking.css": {"banking_feed.html", "banking_review.html", "connections_notice.html",
                    "sandbox_shell.html", "connections.html", "sandbox_activity.html",
                    "sandbox_accounts.html", "sandbox_overview.html", "sandbox_unavailable.html"},
    "../household-onboarding.css": {"banking_connections.html"},
}
FRAGMENT_HOSTS = {
    "transactions_partial.html": {"activity.html", "dashboard.html"},
    "_upload_results.html": {"upload.html"},  # returned by the native upload/HTMX action
    "_doc_status.html": {"processing.html"},
    "_import_run_status.html": {"import_run.html"},
    "_coverage_breakdown.html": {"recon_review.html"},
    "_backlog_job.html": {"backlog.html"},
    "_tabbar.html": {"foundation_base.html"},
}
SHARED = {"base.html", "foundation_base.html", "foundation_macros.html"}
PAGES = set().union(*PAGE_STYLES.values())
INVENTORY = PAGES | set(FRAGMENT_HOSTS) | SHARED
SHEETS = ["/static/foundation/vendor/bootstrap.min.css", "/static/foundation/tokens.css",
          "/static/foundation/components.css"]


def template_tree(name):
    return Environment().parse((TEMPLATES / name).read_text())


def ancestry(name):
    chain = []
    while True:
        assert name not in chain, f"Template inheritance cycle: {chain}"
        chain.append(name)
        parents = list(template_tree(name).find_all(nodes.Extends))
        if not parents:
            return chain
        assert len(parents) == 1 and isinstance(parents[0].template, nodes.Const)
        name = parents[0].template.value


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_all_41_templates_have_explicit_foundation_or_fragment_ownership():
    actual = {path.name for path in TEMPLATES.glob("*.html")}
    assert len(INVENTORY) == 41
    assert actual == INVENTORY, "Assign new templates to their foundation page or fragment host"
    assert len(PAGES) + len(FRAGMENT_HOSTS) + len(SHARED) == len(INVENTORY)
    for name in PAGES | {"base.html"}:
        assert ancestry(name)[-1] == "foundation_base.html", name
        assert "<html" not in (TEMPLATES / name).read_text().lower(), name
    for fragment, hosts in FRAGMENT_HOSTS.items():
        assert not list(template_tree(fragment).find_all(nodes.Extends)), fragment
        assert "<html" not in (TEMPLATES / fragment).read_text().lower(), fragment
        for host in hosts:
            assert ancestry(host)[-1] == "foundation_base.html"
            if fragment != "_upload_results.html":
                includes = {node.template.value for node in template_tree(host).find_all(nodes.Include)
                            if isinstance(node.template, nodes.Const)}
                assert fragment in includes, (fragment, host)
    macros = {
        node.template.value for node in template_tree("banking_connections.html").find_all(nodes.Import)
        if isinstance(node.template, nodes.Const)
    }
    assert "foundation_macros.html" in macros


def test_each_page_inherits_shared_assets_and_its_composition_sheet():
    base = (TEMPLATES / "foundation_base.html").read_text()
    positions = [base.index(sheet) for sheet in SHEETS]
    assert positions == sorted(positions)
    for sheet, pages in PAGE_STYLES.items():
        url = "/static/household-onboarding.css" if sheet.startswith("../") else "/static/foundation/" + sheet
        for page in pages:
            source = "\n".join((TEMPLATES / parent).read_text() for parent in ancestry(page))
            assert url in source, (page, url)
        assert (STATIC / url.removeprefix("/static/")).is_file()
    # Every route-rendered literal template is inventoried, including HTMX
    # fragments and the isolated Sandbox app. This does not execute every state.
    route_files = [*(ROOT / "app/web/routes").glob("*.py"),
                   ROOT / "app/banking/web.py", ROOT / "app/plaid_sandbox/web.py"]
    used = {node.value for path in route_files for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".html")}
    assert used == (PAGES - {"sandbox_shell.html", "connections_notice.html"}) | (set(FRAGMENT_HOSTS) - {"_tabbar.html", "_coverage_breakdown.html"})


@pytest.mark.parametrize("page", sorted(PAGES))
def test_each_page_head_renders_effective_foundation_and_composition_assets(page):
    from app.web.templating import templates

    env = Environment(loader=FileSystemLoader(TEMPLATES))
    env.filters.update(templates.env.filters)
    request = Request({"type": "http", "scheme": "http", "path": "/",
                       "server": ("testserver", 80), "headers": [], "query_string": b""})
    # Render actual title/style inheritance with only body/scripts replaced.
    # Feature route suites own real page data and financial state transitions.
    wrapper = env.from_string('{% extends "' + page + '" %}'
                              '{% block content %}<h1>Generated adoption fixture</h1>{% endblock %}'
                              '{% block scripts %}{% endblock %}')
    parsed = Markup(wrapper.render(request=request, pending_action_count=lambda: 0)).elements
    sheets = [attrs["href"] for tag, attrs in parsed if tag == "link" and attrs.get("rel") == "stylesheet"]
    composition = next(sheet for sheet, pages in PAGE_STYLES.items() if page in pages)
    url = "/static/household-onboarding.css" if composition.startswith("../") else "/static/foundation/" + composition
    assert sheets == [*SHEETS, url], page
    assert all(not (tag == "script" and attrs.get("src", "").startswith("http")) for tag, attrs in parsed)


def test_runtime_has_no_legacy_stylesheet_caller_or_competing_foundation_palette():
    assert not (STATIC / "app.css").exists()
    for path in (ROOT / "app").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}:
            assert not re.search(r"(?:/static/|[\"'/])app\.css(?:[\"'?\s]|$)", path.read_text()), path
    # Primitive values come from tokens.css. Page styles must consume them,
    # rather than silently overriding a canonical variable with another value.
    canonical = set(re.findall(r"(--[\w-]+):", (STATIC / "foundation/tokens.css").read_text()))
    sheets = [*(STATIC / "foundation").glob("*.css"), STATIC / "household-onboarding.css",
              STATIC / "banking.css", STATIC / "plaid-sandbox.css"]
    for path in sheets:
        if path.name != "tokens.css":
            assert not canonical.intersection(re.findall(r"(--[\w-]+)\s*:", path.read_text())), path


@pytest.mark.parametrize("route", ["/", "/activity", "/upload", "/processing", "/review", "/recon",
    "/close", "/statements?month=2026-03", "/insights", "/categories", "/goals", "/goals/close",
    "/chat", "/manage", "/backlog", "/actions", "/txn/new", "/txn/3/edit", "/review/import",
    "/connections", "/bank-feed"])
def test_real_route_families_ship_shared_foundation_and_accessible_shell(app_env, route):
    from app.web.app import create_app

    client = TestClient(create_app())  # No lifespan, provider, worker or model.
    response = client.get(route)
    assert response.status_code == 200, route
    parsed = Markup(response.text).elements
    styles = [attrs["href"] for tag, attrs in parsed if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert styles[:3] == SHEETS
    assert len(styles) == len(set(styles))
    assert len([tag for tag, _ in parsed if tag == "h1"]) == 1
    assert any(tag == "main" and attrs.get("id") == "workspace-content" and attrs.get("tabindex") == "-1"
               for tag, attrs in parsed)
    assert any(tag == "a" and attrs.get("href") == "#workspace-content" for tag, attrs in parsed)
    assert len([1 for tag, attrs in parsed if tag == "select" and "data-theme-select" in attrs]) == 1
    for url in styles:
        assert url.startswith("/static/")
        asset = client.get(url)
        assert asset.status_code == 200, url
        assert "text/css" in asset.headers["content-type"]
    assert '/static/app.css' not in response.text
    assert client.get('/static/app.css').status_code == 404
