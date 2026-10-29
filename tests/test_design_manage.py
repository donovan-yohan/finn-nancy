"""Generated-only request/render contracts for administration and category review."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings


class Page(HTMLParser):
    """Collect native markup without a browser or additional parser dependency."""

    def __init__(self, text: str):
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def tags(self, tag):
        return [attrs for name, attrs in self.elements if name == tag]


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name, value in {
        "STRICT_LOCAL_MODE": "true", "PLAID_ENABLED": "false", "READ_ONLY": "false",
        "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "API_TOKEN": "",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client():
    from app.web.app import create_app

    # No lifespan startup: these tests never launch workers or transports.
    return TestClient(create_app())


def assert_foundation(text):
    page = Page(text)
    styles = [node.get("href") for node in page.tags("link")]
    assert "/static/foundation/vendor/bootstrap.min.css" in styles
    assert "/static/foundation/manage.css" in styles
    assert "/static/app.css" not in styles
    assert text.count("<h1") == 1
    return page


def test_manage_native_groups_and_labeled_editors(app_env):
    response = client().get("/manage")
    assert response.status_code == 200
    page = assert_foundation(response.text)
    assert {"accounts", "statement-policies", "cardholders", "categories"} <= {
        node.get("id") for node in page.tags("details")
    }
    assert any(node.get("id") == "capture-privacy" for node in page.tags("section"))
    assert not any(node.get("id") == "capture-privacy" for node in page.tags("details"))
    labels = {node.get("for") for node in page.tags("label")}
    fields = [node for tag in ("input", "select", "textarea") for node in page.tags(tag)
              if node.get("type") != "hidden"]
    assert all(node.get("id") in labels for node in fields)
    forms = page.tags("form")
    assert any(node.get("id") == "acct-1" and node.get("action") == "/manage/accounts/1"
               for node in forms)
    assert any(node.get("id") == "statement-policy-3" for node in forms)
    assert 'class="fn-editor' in response.text


def test_backlog_foundation_and_htmx_job_feedback(app_env):
    from app.db import engine

    web = client()
    response = web.get("/backlog")
    page = assert_foundation(response.text)
    forms = [node for node in page.tags("form") if node.get("hx-post")]
    assert len(forms) == 2
    assert all(node.get("hx-post") == "/backlog/suggest" and
               node.get("hx-target") == "#backlog-job" and
               node.get("hx-swap") == "innerHTML" for node in forms)
    assert '<label class="form-label" for="suggestion-limit">' in response.text
    queued = web.post("/backlog/suggest", data={"limit": "25", "include_closed": "1"},
                      headers={"HX-Request": "true"})
    assert queued.status_code == 200
    assert 'role="status"' in queued.text
    assert "Queued backlog suggestions" in queued.text
    with engine.write_tx(app_env) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE type='backlog_suggest'").fetchone()
        assert '"include_closed":true' in job["payload_json"].replace(" ", "")
        conn.execute("UPDATE jobs SET status='error', last_error=? WHERE id=?",
                     ("Generated failure <unsafe>", job["id"]))
    status = web.get("/backlog/status")
    assert "error" in status.text
    assert "Generated failure &lt;unsafe&gt;" in status.text
    assert "Generated failure <unsafe>" not in status.text
    assert 'class="manage-job-errors"' in status.text


def enqueue(db_path):
    from app.db import engine, repo_actions

    with engine.write_tx(db_path) as conn:
        return repo_actions.enqueue_proposal(
            conn, kind="recategorization",
            payload={"transaction_id": 3, "to_category_id": 5},
            evidence={"transaction_ids": [3], "category_ids": [5]},
            confidence=0.91, rationale="Generated category review", agent_run_id="design:manage",
        )


def test_approval_foundation_labels_and_confirmed_recovery(app_env):
    from app.db import engine, repo_actions

    proposal_id = enqueue(app_env)
    web = client()
    queue = web.get("/actions")
    page = assert_foundation(queue.text)
    labels = {node.get("for") for node in page.tags("label")}
    assert all(node.get("id") in labels for tag in ("select", "textarea")
               for node in page.tags(tag))
    assert f'confirm("Reject action #{proposal_id} (recategorization)?")' in queue.text
    assert f'action="/actions/{proposal_id}/request-evidence"' in queue.text
    approved = web.post(f"/actions/{proposal_id}/approve", data={"to_category_id": "6"},
                        follow_redirects=False)
    assert approved.status_code == 303
    audit = web.get(f"/actions/{proposal_id}/audit")
    assert_foundation(audit.text)
    assert f'confirm("Revert action #{proposal_id} (recategorization)?")' in audit.text
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal is not None and proposal["status"] == "edited_approved"
        assert conn.execute("SELECT category_id FROM transaction_splits WHERE transaction_id=3").fetchone()[0] == 6
    assert web.post(f"/actions/{proposal_id}/revert", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal is not None and proposal["status"] == "reverted"
        assert conn.execute("SELECT category_id FROM transaction_splits WHERE transaction_id=3").fetchone()[0] == 4
    assert 'action="/actions/' not in web.get(f"/actions/{proposal_id}/audit").text


def test_closed_period_override_is_explicit_for_apply_and_revert(app_env):
    from app.db import engine, repo_actions, repo_close

    proposal_id = enqueue(app_env)
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-01", reason="Generated closure")
    web = client()
    queue = web.get("/actions")
    overrides = [node for node in Page(queue.text).tags("input")
                 if node.get("name") == "override"]
    assert len(overrides) == 2
    assert all(node.get("type") == "checkbox" and node.get("value") == "1" and
               "checked" not in node for node in overrides)
    assert "2026-01 is closed" in queue.text
    refused = web.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert refused.status_code == 400 and "closed" in refused.text
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal is not None and proposal["status"] == "proposed"
    assert web.post(f"/actions/{proposal_id}/approve", data={"override": "1"},
                    follow_redirects=False).status_code == 303
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-01", reason="Generated reclosure")
    audit = web.get(f"/actions/{proposal_id}/audit")
    assert 'name="override" value="1"' in audit.text
    refused = web.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert refused.status_code == 400 and "closed" in refused.text
    assert web.post(f"/actions/{proposal_id}/revert", data={"override": "1"},
                    follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        assert len(repo_close.list_audit(conn, "2026-01")) > 0
        assert conn.execute("SELECT category_id FROM transaction_splits WHERE transaction_id=3").fetchone()[0] == 4


def test_read_only_backlog_refusal_and_invalid_limit_keep_real_errors(app_env, monkeypatch):
    from app.db import engine

    web = client()
    invalid = web.post("/backlog/suggest", data={"limit": "invalid"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "limit must be a positive integer or all"
    monkeypatch.setenv("READ_ONLY", "true")
    get_settings.cache_clear()
    assert web.get("/backlog").status_code == 200
    refused = web.post("/backlog/suggest", data={"limit": "25"})
    assert refused.status_code == 400
    assert refused.json()["detail"] == "backlog suggestions are disabled in read-only mode"
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='backlog_suggest'").fetchone()[0] == 0


def test_manage_external_form_owners_retain_complete_fields(app_env):
    page = Page(client().get("/manage").text)
    forms = {node.get("id"): node for node in page.tags("form") if node.get("id")}
    expected = {
        "acct-1": {"name", "institution", "kind", "external_ref", "is_active"},
        "statement-policy-3": {"effective_from_month", "configuration_state", "requirement_mode",
                               "cadence", "anchor_month", "active_from", "active_to", "reason"},
        "cat-4": {"name", "brand_owner", "color"},
    }
    for owner, names in expected.items():
        assert forms[owner]["method"] == "post"
        assert names == {attrs["name"] for tag, attrs in page.elements
                         if tag in {"input", "select"} and attrs.get("form") == owner}


def test_transport_consent_and_kill_switch_remain_distinct(app_env, monkeypatch):
    web = client()
    assert web.post("/manage/capture-transports/telegram", data={"decision": "consented"},
                    follow_redirects=False).status_code == 400
    assert web.post("/manage/capture-transports/telegram",
                    data={"decision": "consented", "acknowledge": "1"},
                    follow_redirects=False).status_code == 303
    strict = web.get("/manage")
    assert "Blocked by strict-local mode." in strict.text
    assert "Revoke Telegram consent" in strict.text
    assert 'name="decision" value="revoked"' in strict.text
    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    get_settings.cache_clear()
    allowed = web.get("/manage")
    assert "Allowed by consent, but Telegram credentials are incomplete." in allowed.text
    assert "Revoke Telegram consent" in allowed.text
    assert web.post("/manage/capture-transports/telegram", data={"decision": "revoked"},
                    follow_redirects=False).status_code == 303
    assert 'name="acknowledge" value="1" required' in web.get("/manage").text


def test_empty_reads_remain_truthful(empty_db, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    web = client()
    assert "No proposals awaiting review." in web.get("/actions").text
    assert "No purchase or fee transactions need category evidence." in web.get("/backlog").text
    assert "No cards seen yet." in web.get("/manage").text


def test_multi_split_backlog_preserves_split_scope_and_refuses_batch(app_env):
    from app.db import engine, repo_ledger
    from app.web.filters import money

    with engine.write_tx(app_env) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id=3")
        conn.executemany(
            "INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (3,?,?)",
            [(uncategorized_id, -10000), (4, -6243)],
        )
    text = client().get("/backlog").text
    # The same transaction has two distinct source rows, never a duplicate full amount.
    rows = [row for row in Page(text).tags("tr") if row.get("data-transaction-id") == "3"]
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert all(row["data-mutation-supported"] == "false" for row in rows)
    assert money(-10000) in text and money(-6243) in text
    assert 'href="/txn/3/edit">Review splits</a>' in text
    assert 'aria-label="Batch approval unavailable for multi-split transaction 3"' in text
    assert 'data-label="Split amount"' in text


def test_current_and_future_policy_versions_remain_separate(app_env):
    from datetime import date

    from app.db import engine, repo_statement_expectations

    current_month = date.today().strftime("%Y-%m")
    future_month = f"{date.today().year + 1:04d}-01"
    with engine.write_tx(app_env) as conn:
        for month, mode, cadence in [(current_month, "required", "monthly"),
                                     (future_month, "no_statement", "none")]:
            repo_statement_expectations.record_policy(
                conn, account_id=3, effective_from_month=month,
                configuration_state="configured", requirement_mode=mode, cadence=cadence,
                actor="test:design", reason="Generated prospective policy",
            )
    text = client().get("/manage").text
    assert f"Current for {current_month}: configured · required · monthly" in text
    assert f"Future from {future_month}: configured · no_statement · none" in text
    assert '<label class="form-label" for="policy-reason-3">Change reason</label>' in text
    assert 'name="reason" required' in text


def test_stale_proposal_is_refused_without_losing_audit(app_env):
    from app.actions.category_state import category_state, state_digest
    from app.db import engine, repo_actions

    with engine.write_tx(app_env) as conn:
        proposal_id = repo_actions.enqueue_proposal(
            conn, kind="recategorization",
            payload={"transaction_id": 3, "to_category_id": 5,
                     "expected_state_digest": state_digest(category_state(conn, 3))},
            evidence={}, confidence=0.5, rationale="Generated stale proposal",
            agent_run_id="test:stale",
        )
        conn.execute("UPDATE transactions SET description='Generated later edit' WHERE id=3")
    web = client()
    response = web.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"] == "transaction state is stale; inspect again"
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal is not None and proposal["status"] == "proposed"
    assert web.get(f"/actions/{proposal_id}/audit").status_code == 200


def test_generated_route_artifacts(app_env, tmp_path):
    """Retain optional parent-review HTML without reading any real financial data."""
    import os
    from pathlib import Path

    from app.db import engine, repo_card_holders

    with engine.write_tx(app_env) as conn:
        repo_card_holders.observe(conn, "9001")
    proposal_id = enqueue(app_env)
    web = client()
    output = Path(os.environ.get("MANAGE_RENDER_ARTIFACTS", str(tmp_path / "renders")))
    output.mkdir(parents=True, exist_ok=True)
    for name, route in [("manage", "/manage"), ("backlog", "/backlog"),
                        ("approvals", "/actions"), ("audit", f"/actions/{proposal_id}/audit")]:
        response = web.get(route)
        assert response.status_code == 200
        assert_foundation(response.text)
        (output / f"{name}.html").write_text(response.text)
    # Actual current fragment after a generated request, not a fabricated renderer model.
    response = web.post("/backlog/suggest", data={"limit": "25"})
    assert response.status_code == 200
    (output / "backlog-job.html").write_text(response.text)
