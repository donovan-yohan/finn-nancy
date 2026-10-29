"""Generated interface contracts for the foundation close/report migration."""
from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, repo_period_policy
from app.reporting.period_statements import (
    FrozenPeriodStatementIntegrityError,
    FrozenPeriodStatementUnavailable,
    UnsupportedReportCurrency,
    build_period_statement,
)
from app.web.filters import money
from app.web.routes import close, statements
from tests.test_close_page import (
    FIXTURE_MONTH,
    _add_categorized_expense,
    _add_uncategorized_expense,
    _add_unknown_flow,
    _reopen_data,
    _signoff_data,
    _waive_required_matrix,
)
from tests.test_period_statements import MONTH, _seed_golden_month


class Element:
    def __init__(self, tag="root", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    @property
    def text(self):
        return "".join(
            "" if isinstance(child, Element) and child.tag == "wbr" else child.text if isinstance(child, Element) else child
            for child in self.children
        ).strip()

    def find_all(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if (tag is None or child.tag == tag) and all(
                child.attrs.get(key) == value for key, value in attrs.items()
            ):
                found.append(child)
            found.extend(child.find_all(tag, **attrs))
        return found


class Document(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.root = Element()
        self.stack = [self.root]
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"input", "meta", "link", "br", "hr", "img", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def document(response):
    assert response.status_code == 200
    return Document(response.text).root


def one(root, tag=None, **attrs):
    matches = root.find_all(tag, **attrs)
    assert len(matches) == 1, (tag, attrs, len(matches))
    return matches[0]


def _client(db_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("STRICT_LOCAL_MODE", "true")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(close.router)
    app.include_router(statements.router)
    return TestClient(app)


def _save(name, response):
    if output := os.environ.get("DESIGN_CLOSE_ARTIFACT_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(response.text)


def _foundation(root):
    sheets = [node.attrs["href"] for node in root.find_all("link", rel="stylesheet")]
    assert sheets == [
        "/static/foundation/vendor/bootstrap.min.css",
        "/static/foundation/tokens.css",
        "/static/foundation/components.css",
        "/static/foundation/close-report.css",
    ]
    assert len(root.find_all("h1")) == 1
    ids = [node.attrs["id"] for node in root.find_all() if "id" in node.attrs]
    assert len(ids) == len(set(ids))
    for node in root.find_all("a"):
        if node.attrs.get("href", "").startswith("#"):
            assert node.attrs["href"][1:] in ids
    workspace = one(root, "main", id="workspace-content")
    for node in workspace.find_all("input"):
        if node.attrs.get("type") == "hidden":
            continue
        expected = "form-check-input" if node.attrs.get("type") == "checkbox" else "form-control"
        assert expected in node.attrs.get("class", "").split()
    for node in workspace.find_all("select"):
        assert "form-select" in node.attrs.get("class", "").split()
    for node in workspace.find_all("button"):
        assert "btn" in node.attrs.get("class", "").split()


def _guarded_form(root, action, required):
    form = one(root, "form", action=action)
    assert "data-required-submit" in form.attrs
    inputs = {node.attrs["name"]: node for node in form.find_all("input")}
    for name in required:
        assert "required" in inputs[name].attrs
    assert inputs["month"].attrs["value"] == FIXTURE_MONTH
    if "operation_key" in inputs:
        assert inputs["operation_key"].attrs["value"].startswith("close:web:")
    submit = one(form, "button")
    assert "disabled" in submit.attrs
    assert submit.attrs["aria-disabled"] == "true"
    assert "data-submit-control" in submit.attrs
    assert len([node for node in form.find_all() if "data-form-requirements" in node.attrs]) == 1
    return form


def test_close_foundation_preserves_preparation_policy_gate_and_matrix(app_env, monkeypatch):
    client = _client(app_env, monkeypatch)
    _add_uncategorized_expense(app_env)
    response = client.get(f"/close?month={FIXTURE_MONTH}")
    root = document(response)
    _foundation(root)
    _guarded_form(root, "/close/prepare", {"actor", "reason", "confirm_prepare"})
    form = _guarded_form(root, "/close/signoff", {"actor", "reason", "confirm_close"})
    assert form.attrs["data-policy-blocked"] == "true"
    assert "audited statement matrix first" in form.attrs["data-policy-blocked-reason"]
    matrix = one(root, "table")
    assert "table" in matrix.attrs["class"].split()
    assert len(matrix.find_all("th", scope="col")) == 5
    assert matrix.find_all("caption")
    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, FIXTURE_MONTH) == "open"
    section_ids = [node.attrs.get("id") for node in root.find_all("section")]
    assert section_ids.index("close-preparation") < section_ids.index("close-exceptions")
    assert section_ids.index("statement-matrix") < section_ids.index("close-action")
    _save("close-setup.html", response)


def test_exception_acknowledgement_close_reopen_retains_authority_and_anchors(app_env, monkeypatch):
    client = _client(app_env, monkeypatch)
    _waive_required_matrix(app_env)
    _add_uncategorized_expense(app_env)
    response = client.get(f"/close?month={FIXTURE_MONTH}")
    root = document(response)
    ack_form = next(node for node in root.find_all("form") if "/preclose-exception/" in node.attrs.get("action", ""))
    _guarded_form(root, ack_form.attrs["action"], {"actor", "reason", "evidence_note", "confirm_acknowledgement"})
    assert client.post("/close/signoff", data=_signoff_data()).status_code == 400
    ack_data = {
        "month": FIXTURE_MONTH, "actor": "fixture:design-reviewer",
        "reason": "Generated unresolved category reviewed",
        "evidence_note": "Generated statement checked",
        "confirm_acknowledgement": "1", "operation_key": "fixture:design:ack",
    }
    assert client.post(ack_form.attrs["action"], data=ack_data, follow_redirects=False).status_code == 303
    reviewed = client.get(f"/close?month={FIXTURE_MONTH}")
    reviewed_root = document(reviewed)
    assert "acknowledged · unresolved" in reviewed_root.text
    assert _guarded_form(reviewed_root, "/close/signoff", {"actor", "reason", "confirm_close"}).attrs["data-policy-blocked"] == "false"
    withdrawal = next(node for node in reviewed_root.find_all("form") if node.attrs.get("action", "").endswith("/withdraw"))
    _guarded_form(reviewed_root, withdrawal.attrs["action"], {"actor", "reason", "evidence_note", "confirm_withdrawal"})
    assert reviewed_root.find_all("details")
    assert client.post("/close/signoff", data=_signoff_data(operation_key="fixture:design:close"), follow_redirects=False).status_code == 303
    frozen = client.get(f"/close?month={FIXTURE_MONTH}")
    frozen_root = document(frozen)
    _foundation(frozen_root)
    _guarded_form(frozen_root, "/close/reopen", {"actor", "reason", "confirm_reopen"})
    assert not frozen_root.find_all("form", action="/close/signoff")
    with engine.read_conn(app_env) as conn:
        state = repo_period_policy.get_state(conn, FIXTURE_MONTH)
        snapshot = repo_period_policy.list_snapshot_history(conn, FIXTURE_MONTH)[0]
        exception = repo_period_policy.list_current_exceptions(conn, FIXTURE_MONTH)[0]
    assert state is not None
    assert state["state"] == "closed_with_exceptions"
    assert snapshot["snapshot_digest"] in frozen_root.text
    one(frozen_root, id=f"close-snapshot-{state['snapshot_id']}")
    one(frozen_root, id=f"close-exception-{exception['id']}")
    one(frozen_root, id=f"close-acknowledgement-{exception['acknowledgement_id']}")
    assert "live checklist · snapshot unchanged" in frozen.text
    _save("close-frozen.html", frozen)
    assert client.post("/close/reopen", data=_reopen_data(operation_key="fixture:design:reopen"), follow_redirects=False).status_code == 303
    reopened = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'data-snapshot-current="false"' in reopened.text
    assert "historical snapshot" in reopened.text
    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, FIXTURE_MONTH) == "reopened"
    _save("close-reopened.html", reopened)


def test_prepared_matrix_retains_labeled_waiver_editor(app_env, monkeypatch):
    client = _client(app_env, monkeypatch)
    assert client.post("/close/prepare", data={
        "month": FIXTURE_MONTH, "actor": "fixture:design",
        "reason": "Generated matrix preparation", "confirm_prepare": "1",
    }, follow_redirects=False).status_code == 303
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    root = document(page)
    _foundation(root)
    editors = [node for node in root.find_all("form") if node.attrs.get("action", "").endswith("/waive")]
    assert editors
    for form in editors:
        assert [node.attrs["name"] for node in form.find_all("input") if "required" in node.attrs] == ["actor", "reason"]
        assert len(form.find_all("label")) == 2
    _save("close-waivers.html", page)


def test_clean_close_keeps_reopen_and_snapshot_report_reachable(app_env, monkeypatch):
    client = _client(app_env, monkeypatch)
    _add_categorized_expense(app_env)
    _waive_required_matrix(app_env)
    response = client.post(
        "/close/signoff", data=_signoff_data(operation_key="fixture:design:clean"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    root = document(page)
    _foundation(root)
    assert 'data-close-state="clean_closed"' in page.text
    _guarded_form(root, "/close/reopen", {"actor", "reason", "confirm_reopen"})
    assert root.find_all("a", href="#close-action")
    assert "unresolved" in root.text
    _save("close-clean.html", page)


def test_unknown_flow_remains_a_review_exception_not_sign_inferred(app_env, monkeypatch):
    client = _client(app_env, monkeypatch)
    _waive_required_matrix(app_env)
    _add_unknown_flow(app_env)
    response = client.get(f"/close?month={FIXTURE_MONTH}")
    root = document(response)
    _foundation(root)
    assert "AMBIGUOUS MOVEMENT" in response.text or "flow" in root.text
    assert _guarded_form(root, "/close/signoff", {"actor", "reason", "confirm_close"}).attrs["data-policy-blocked"] == "true"
    assert client.post("/close/signoff", data=_signoff_data()).status_code == 400
    _save("close-unknown.html", response)


@pytest.mark.parametrize("account_scope", [False, True])
def test_statement_all_totals_and_rows_match_canonical_producer(empty_db, monkeypatch, account_scope):
    _seed_golden_month(empty_db)
    client = _client(empty_db, monkeypatch)
    report = build_period_statement(empty_db, month=MONTH, home_currency="CAD")
    selected = report.accounts[0] if account_scope else None
    url = f"/statements?month={MONTH}" + (f"&account_id={selected.account_id}" if selected else "")
    response = client.get(url)
    root = document(response)
    _foundation(root)
    context = getattr(response, "context")
    for metric in context["evidence_metrics"]:
        links = root.find_all("a", href=f"#evidence-{metric['key']}")
        assert len(links) == 1
        assert money(metric["amount"].cents) in links[0].text
        assert metric["amount"].currency in links[0].text
        target = one(root, "article", id=f"evidence-{metric['key']}")
        assert target.attrs["data-line-key"] == metric["line_key"]
        assert money(metric["amount"].cents) in target.text
        expected_refs = {f"#report-row-{row.row_id}" for row in metric["rows"]}
        assert {node.attrs["href"] for node in target.find_all("a") if node.attrs.get("href", "").startswith("#report-row-")} == expected_refs
        assert target.find_all("details")
    expected_rows = [row for row in report.rows if not selected or row.account_id == selected.account_id]
    actual_rows = [node for node in root.find_all("article") if node.attrs.get("id", "").startswith("report-row-")]
    assert len(actual_rows) == len(expected_rows)
    for row in expected_rows:
        node = one(root, "article", id=f"report-row-{row.row_id}")
        assert row.description in node.text
        assert money(row.amount_cents) in node.text
        assert row.flow_kind.replace("_", " ") in node.text
        assert node.find_all("a", href=f"/txn/{row.transaction_id}/edit")
    assert bool(root.find_all("a", href=f"/statements/export?month={MONTH}&format=csv")) == (not account_scope)
    _save("statement-account.html" if account_scope else "statement-household.html", response)


@pytest.mark.parametrize("error, kind", [
    (UnsupportedReportCurrency("Generated currency mismatch"), "unsupported_currency"),
    (FrozenPeriodStatementUnavailable("Generated legacy snapshot"), "legacy_snapshot_unavailable"),
    (FrozenPeriodStatementIntegrityError("Generated digest mismatch"), "snapshot_integrity_error"),
])
def test_blocked_report_replaces_totals_and_exports(empty_db, monkeypatch, error, kind):
    client = _client(empty_db, monkeypatch)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(statements, "build_period_statement", fail)
    response = client.get(f"/statements?month={MONTH}")
    assert response.status_code == 409
    root = Document(response.text).root
    _foundation(root)
    assert f'data-report-error-kind="{kind}"' in response.text
    assert not root.find_all("a", href=f"/statements/export?month={MONTH}&format=csv")
    assert "period-total-link" not in response.text
    assert root.find_all("section", role="alert")
    _save(f"statement-blocked-{kind}.html", response)


def test_large_signed_values_and_long_evidence_names_are_not_truncated(empty_db, monkeypatch):
    _seed_golden_month(empty_db)
    client = _client(empty_db, monkeypatch)
    descriptor = "Generated very long evidence description " * 12
    with engine.write_tx(empty_db) as conn:
        row = conn.execute("SELECT id FROM transactions WHERE flow_kind='purchase' LIMIT 1").fetchone()
        conn.execute("UPDATE transactions SET description=?, amount_cents=-9007199254740991 WHERE id=?", (descriptor, row["id"]))
        splits = conn.execute("SELECT id FROM transaction_splits WHERE transaction_id=?", (row["id"],)).fetchall()
        assert len(splits) == 1
        conn.execute("UPDATE transaction_splits SET amount_cents=-9007199254740991 WHERE id=?", (splits[0]["id"],))
    response = client.get(f"/statements?month={MONTH}")
    root = document(response)
    _foundation(root)
    assert "-$90,071,992,547,409.91" in root.text
    assert descriptor.strip() in root.text
    _save("statement-stress.html", response)
