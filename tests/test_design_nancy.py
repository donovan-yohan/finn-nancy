"""Generated-only route and native form contracts for Nancy's foundation port."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_budgets, repo_goals, repo_ledger


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None]
    children: list[Element | str] = field(default_factory=list)

    @property
    def text(self):
        return "".join(c.text if isinstance(c, Element) else c for c in self.children)

    def all(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if isinstance(child, Element):
                if (tag is None or child.tag == tag) and all(child.attrs.get(k) == v for k, v in attrs.items()):
                    found.append(child)
                found.extend(child.all(tag, **attrs))
        return found


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = Element("document", {})
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@pytest.fixture
def client(app_env, monkeypatch):
    monkeypatch.setenv("HERMES_CHAT_URL", "")
    from app.config import get_settings
    from app.web.app import create_app

    get_settings.cache_clear()
    return TestClient(create_app())


def _capture(name, html):
    destination = os.getenv("NANCY_RENDER_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.html").write_text(html)


def _post(client, action, data):
    response = client.post(action, data=data, follow_redirects=False)
    assert response.status_code == 303
    return response


@pytest.mark.parametrize("route,title,name", [
    ("/insights?month=2026-03", "Insights", "insights"),
    ("/categories", "Budget", "budget"),
    ("/goals?month=2026-03", "Goals", "goals"),
    ("/goals/close?month=2026-03", "Goal close", "goals-close"),
    ("/chat", "Namako", "chat-disabled"),
])
def test_foundation_routes_preserve_native_controls(client, route, title, name):
    response = client.get(route)
    assert response.status_code == 200
    _capture(name, response.text)
    doc = Document(response.text).root
    assert [n.text for n in doc.all("h1")] == [title]
    styles = [n.attrs["href"] for n in doc.all("link", rel="stylesheet")]
    assert styles == ["/static/foundation/vendor/bootstrap.min.css", "/static/foundation/tokens.css", "/static/foundation/components.css", "/static/foundation/nancy.css"]
    assert doc.all("main", id="workspace-content")
    for table in doc.all("table"):
        assert "table" in table.attrs["class"].split()
    for button in doc.all("button"):
        assert "btn" in button.attrs.get("class", "").split()
    controls = doc.all("input") + doc.all("select") + doc.all("textarea")
    ids = [n.attrs["id"] for n in doc.all() if n.attrs.get("id")]
    assert len(ids) == len(set(ids))
    labels = doc.all("label")
    for control in controls:
        if control.attrs.get("type") == "hidden":
            continue
        assert any(label.attrs.get("for") == control.attrs.get("id") and control.attrs.get("id") or control in label.all() for label in labels)


def test_budget_forms_keep_external_form_ownership_and_round_trip(client, app_env):
    _post(client, "/categories/members", {"name": "Generated Nancy Owner"})
    with engine.read_conn(app_env) as conn:
        owner = conn.execute("SELECT id FROM household_members WHERE name='Generated Nancy Owner'").fetchone()[0]
    page = client.get("/categories")
    doc = Document(page.text).root
    row = doc.all("tr", id="category-5")[0]
    form = row.all("form")[0]
    assert form.attrs == {"id": "budget-5", "action": "/categories/5/budget", "method": "post"}
    assert {n.attrs["name"] for n in row.all("input") + row.all("select")} == {"owner_member_id", "amount", "is_leisure"}
    assert all(n.attrs["form"] == "budget-5" for n in row.all("input") + row.all("select") + row.all("button"))
    _post(client, form.attrs["action"], {"owner_member_id": owner, "amount": "150.01", "is_leisure": "1"})
    _post(client, "/categories/settings/big-ticket", {"amount": "300.01"})
    refreshed = client.get("/categories")
    assert 'value="150.01"' in refreshed.text
    assert 'value="300.01"' in refreshed.text
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT amount_cents,owner_member_id FROM budgets WHERE category_id=5 AND period_month='' ").fetchone()
        assert tuple(row) == (15001, owner)
        assert conn.execute("SELECT is_leisure FROM categories WHERE id=5").fetchone()[0] == 1
    _capture("budget-saved", refreshed.text)


def test_split_contributions_are_not_replaced_by_transaction_amount(client, app_env):
    with engine.write_tx(app_env) as conn:
        first, second = [conn.execute(
            "INSERT INTO categories(name,kind,brand_owner,color) VALUES (?,'expense','nancy','#bc4700')",
            (name,),
        ).lastrowid for name in ["Generated split one", "Generated split two"]]
        txn = repo_ledger.insert_transaction(conn, account_id=1, posted_on="2026-03-20", description="Generated split purchase", counterparty="Generated split merchant", amount_cents=-10001, source="test", external_id="design-nancy-split", source_document_id=None, source_confidence=1.0, flow_kind="purchase")
        assert txn is not None
        assert first is not None and second is not None
        repo_ledger.insert_split(conn, transaction_id=txn, category_id=first, amount_cents=-4001)
        repo_ledger.insert_split(conn, transaction_id=txn, category_id=second, amount_cents=-6000)
        repo_budgets.set_category_budget(conn, category_id=first, amount_cents=10000, owner_member_id=None)
    page = client.get("/insights?month=2026-03")
    doc = Document(page.text).root
    rows = [n for n in doc.all("article") if n.attrs.get("class") == "budget-row"]
    category = next(n for n in rows if "Generated split one" in n.text)
    assert "$40.01 of $100.00" in category.text
    assert "$59.99" in category.text
    assert "$100.01" not in category.text
    assert "$60.00" in next(n for n in rows if "Generated split two" in n.text).text
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT SUM(amount_cents) FROM transaction_splits WHERE transaction_id=?", (txn,)).fetchone()[0] == -10001
    _capture("insights-split", page.text)


def test_goal_create_edit_pause_and_allocation_sources(client, app_env):
    _post(client, "/categories/5/budget", {"amount": "150.00", "owner_member_id": "shared"})
    form = {"name": "Generated exact goal", "kind": "payoff", "target": "200.01", "monthly_contribution": "50.01", "start_month": "2026-03", "target_month": "2026-06", "linked_category_id": "5", "linked_transaction_id": "", "auto_fund": "1", "notes": "Generated notes", "priority": "1", "brand_owner": "nancy", "color": "#bc4700"}
    _post(client, "/goals", form)
    with engine.read_conn(app_env) as conn:
        goal_id = conn.execute("SELECT id FROM goals WHERE name=?", (form["name"],)).fetchone()[0]
    edit = client.get(f"/goals/{goal_id}/edit?month=2026-03")
    doc = Document(edit.text).root
    editor = doc.all("form", action=f"/goals/{goal_id}")[0]
    assert {n.attrs["name"] for n in editor.all("input") + editor.all("select") + editor.all("textarea")} == set(form)
    _post(client, f"/goals/{goal_id}", {**form, "notes": "Updated generated notes"})
    _post(client, "/goals/close", {"month": "2026-03"})
    page = client.get("/goals/close?month=2026-03")
    doc = Document(page.text).root
    row = next(n for n in doc.all("tr") if form["name"] in n.text)
    assert "$50.01" in row.text and "Restaurants" in row.text
    with engine.read_conn(app_env) as conn:
        review = next(n for n in repo_goals.close_review_rows(conn, "2026-03") if n["id"] == goal_id)
        assert sum(source["cents"] for source in review["note_json"]["sources"]) == review["actual_cents"] == 5001
        assert conn.execute("SELECT notes FROM goals WHERE id=?", (goal_id,)).fetchone()[0] == "Updated generated notes"
    _capture("goals-close-applied", page.text)
    _capture("goal-edit", edit.text)
    _post(client, f"/goals/{goal_id}/status", {"status": "paused"})
    assert "Activate" in client.get("/goals?month=2026-03").text
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0] == "paused"


def test_suggestion_acceptance_is_explicit_and_does_not_change_splits(client, app_env):
    _post(client, "/categories/5/budget", {"amount": "1.00"})
    page = client.get("/insights?month=2026-03")
    doc = Document(page.text).root
    proposal = doc.all("form", action="/insights/cards/action")[0]
    assert [n.attrs["value"] for n in proposal.all("button")] == ["accepted", "dismissed", "snoozed"]
    assert "Accept suggestion" in proposal.text
    key = proposal.all("input", name="card_key")[0].attrs["value"]
    with engine.read_conn(app_env) as conn:
        before = [tuple(n) for n in conn.execute("SELECT * FROM transaction_splits ORDER BY id")]
    _post(client, "/insights/cards/action", {"card_key": key, "month": "2026-03", "action": "accepted"})
    with engine.read_conn(app_env) as conn:
        assert repo_budgets.planning_insight_cards(conn, "2026-03")[0]["current_action"] == "accepted"
        assert [tuple(n) for n in conn.execute("SELECT * FROM transaction_splits ORDER BY id")] == before


def test_chat_preserves_configuration_and_recovery_hooks(client, monkeypatch):
    disabled = client.get("/chat")
    assert 'data-chat-enabled="false"' in disabled.text
    assert disabled.headers["cache-control"] == "no-store"
    monkeypatch.setenv("HERMES_CHAT_URL", "ws://127.0.0.1:19119/api/ws")
    from app.config import get_settings
    get_settings.cache_clear()
    page = client.get("/chat")
    assert 'data-chat-enabled="true"' in page.text
    assert "19119" not in page.text
    doc = Document(page.text).root
    for name in ["form", "input", "list", "send", "stop", "status", "error", "uncertain", "reconnect", "requests", "empty"]:
        assert len(doc.all(id="chat-" + name)) == 1
    assert doc.all("p", id="chat-error")[0].attrs["role"] == "alert"
    assert doc.all("p", id="chat-uncertain")[0].attrs["role"] == "status"
    assert doc.all("form", action="/chat/new")[0].attrs["method"] == "post"
    assert 'src="/static/hermes-chat.js"' in page.text
    _capture("chat-enabled", page.text)


def test_empty_insights_has_real_start_action_and_no_fake_totals(empty_db, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "generated-data"))
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    page = TestClient(create_app()).get("/insights")
    assert page.status_code == 200
    doc = Document(page.text).root
    assert doc.all("h1")[0].text == "Insights"
    assert "No activity yet" in doc.text
    assert doc.all("a", href="/upload")
    assert not doc.all("section", **{"class": "nancy-totals"})
    assert doc.all("form", action="/insights/prose/generate")
    _capture("insights-empty", page.text)
    get_settings.cache_clear()


def test_insights_keeps_subscriptions_evidence_decisions_and_prose_order(client, app_env):
    from app.db import repo_insight_prose
    from tests.test_subscription_watchlist import _seed_subscription_fixture

    account, _ = _seed_subscription_fixture(app_env)
    with engine.write_tx(app_env) as conn:
        repo_insight_prose.upsert(conn, period_month="2026-06", scope="household", kind="monthly_summary", body="Generated cached interpretation", model="generated-model")
    page = client.get("/insights?month=2026-06")
    doc = Document(page.text).root
    forms = doc.all("form", action="/insights/subscriptions/action")
    assert forms
    assert {f.all("input", name="decision")[0].attrs["value"] for f in forms} == {"subscription", "not_subscription", "already_known", "watch_next_month"}
    target = next(f for f in forms if f.all("input", name="merchant")[0].attrs["value"] == "Fresh Stream")
    assert target.all("input", name="account_id")[0].attrs["value"] == str(account)
    assert target.all("input", name="month")[0].attrs["value"] == "2026-06"
    assert "$12.99" in doc.text and "2026-07-15" in doc.text
    assert page.text.index("Budget vs actual") < page.text.index("Planning suggestions") < page.text.index("Monthly summary")
    assert "Generated cached interpretation" in doc.text
    _post(client, "/insights/subscriptions/action", {"merchant": "Fresh Stream", "account_id": account, "decision": "subscription", "month": "2026-06"})
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT decision FROM subscription_watchlist_decisions WHERE merchant='Fresh Stream' AND account_id=?", (account,)).fetchone()[0] == "subscription"
    assert "marked subscription" in client.get("/insights?month=2026-06").text
    _capture("insights-recurring-and-prose", page.text)


def test_large_cent_editor_values_remain_exact(client, app_env):
    cents = 9007199254740991
    with engine.write_tx(app_env) as conn:
        repo_budgets.set_category_budget(conn, category_id=5, amount_cents=cents, owner_member_id=None)
        conn.execute("INSERT INTO goals(name,kind,target_cents,start_month,monthly_contribution_cents) VALUES ('Generated large target','payoff',?,'2026-03',?)", (cents, cents))
        goal = conn.execute("SELECT id FROM goals WHERE name='Generated large target'").fetchone()[0]
    assert 'value="90071992547409.91"' in client.get("/categories").text
    edit = client.get(f"/goals/{goal}/edit?month=2026-03")
    assert edit.status_code == 200
    assert edit.text.count('value="90071992547409.91"') == 2
    _capture("goal-large-target", edit.text)


def test_planning_evidence_links_keep_partial_scope_and_absent_lines(client, app_env):
    from tests.test_planning_insight_cards import _seed_planning_cards_fixture

    _seed_planning_cards_fixture(app_env)
    page = client.get("/insights?month=2026-06")
    doc = Document(page.text).root
    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")
    assert cards
    for card in cards:
        article = doc.all("article", **{"data-card-key": card["card_key"]})[0]
        assert card["title"] in article.text
        assert card["current_action_label"] in article.text
        for note in card["evidence_notes"]:
            assert note in article.text
        for transaction in card["transaction_ids"]:
            assert article.all("a", href=f"/txn/{transaction}/edit")
        for category in card["category_ids"]:
            assert article.all("a", href=f"/categories#category-{category}")
        if not card["statement_line_ids"]:
            assert "Statement lines" not in article.text
    _capture("insights-planning-evidence", page.text)
