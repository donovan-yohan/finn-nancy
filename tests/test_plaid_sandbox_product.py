"""Product views use deterministic synthetic imports, never operator records."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.plaid_sandbox.web import create_app


@pytest.fixture
def product(tmp_path):
    app = create_app(tmp_path / "generated-candidate", port=8772)
    store = app.state.store
    owner_a = store.add_member("Generated Alpha", "generated-actor")
    owner_b = store.add_member("Generated Beta", "generated-actor")
    browser = store.new_browser()
    connections = []
    for index, owner in enumerate((owner_a, owner_b)):
        session = store.new_link(browser["id"], owner, f"link-sandbox-generated-{index}")
        connection = store.finish_exchange(session, owner, f"generated-item-{index}",
                                           f"access-sandbox-generated-{index}", browser["id"])
        # Provider account IDs deliberately collide across different Items.
        store.save_accounts(connection, [{"account_id": "equal-account", "name": "Generated Checking",
                                          "mask": "0000", "type": "depository", "subtype": "checking"}])
        connections.append(connection)

    def row(transaction_id, amount, date, name, *, currency: str | None = "CAD", pending=False, pending_id=None):
        return {"transaction_id": transaction_id, "account_id": "equal-account", "amount": amount,
                "date": date, "name": name, "iso_currency_code": currency,
                "pending": pending, "pending_transaction_id": pending_id}

    rows_a = [row("equal-id", "12.34", "2026-01-03", "Generated Market"),
              row("pending", "5", "2026-01-05", "Generated Coffee", currency="USD", pending=True),
              row("obsolete", "7", "2026-01-01", "Generated Hold", pending=True),
              row("replacement", "14", "2026-01-02", "Generated Posted", pending_id="obsolete"),
              row("removed", "9", "2026-01-04", "Generated Removed"),
              row("wildcards", "0.01", "2026-01-05", "Generated 100% _ literal", currency=None)]
    store.commit_window(connections[0], "", "generated-cursor-a",
                        [("added", value) for value in rows_a] + [("removed", {"transaction_id": "removed"})],
                        None, browser["id"])
    store.commit_window(connections[1], "", "generated-cursor-b",
                        [("added", row("equal-id", "-20", "2026-01-03", "Generated Transfer"))],
                        None, browser["id"])
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        yield client, store, (owner_a, owner_b), connections


def test_activity_returns_current_imports_even_before_account_selection(product):
    client, _, _, connections = product
    response = client.get("/api/sandbox/activity")
    assert response.status_code == 200
    data = response.json()
    expected = {(connections[0], key) for key in ("equal-id", "pending", "replacement", "wildcards")}
    expected.add((connections[1], "equal-id"))
    assert {(row["connection_id"], row["transaction_id"]) for row in data["rows"]} == expected
    assert data["total"] == len(expected)
    assert data["offset"] == 0 and data["limit"] == 50 and not data["has_more"]
    assert all(not row["selected"] and not row["booked"] and row["flow_kind"] == "unknown" for row in data["rows"])
    assert {row["currency"] for row in data["rows"]} == {"CAD", "USD", None}
    assert all(value not in json.dumps(data) for value in ("access-sandbox", "link-sandbox", "generated-cursor"))


@pytest.mark.parametrize(("filters", "expected"), [
    ({"state": "pending"}, {(0, "pending")}),
    ({"state": "removed"}, {(0, "removed")}),
    ({"state": "superseded"}, {(0, "obsolete")}),
    ({"state": "posted", "q": "generated market"}, {(0, "equal-id")}),
    ({"q": "% _"}, {(0, "wildcards")}),
    ({"q": "Generated Checking", "state": "pending"}, {(0, "pending")}),
    ({"date_from": "2026-01-03", "date_to": "2026-01-03"}, {(0, "equal-id"), (1, "equal-id")}),
    ({"q": "' OR 1=1 --"}, set()),
    ({"inclusion": "selected"}, set()),
    ({"owner_id": "missing-generated-owner"}, set()),
])
def test_activity_filters_match_exact_import_evidence(product, filters, expected):
    client, _, _, connections = product
    response = client.get("/api/sandbox/activity", params=filters)
    assert response.status_code == 200
    data = response.json()
    assert {(row["connection_id"], row["transaction_id"]) for row in data["rows"]} == {
        (connections[index], transaction) for index, transaction in expected}
    assert data["total"] == len(expected)


def test_activity_uses_connection_scoped_accounts_and_persisted_inclusion(product):
    client, store, owners, connections = product
    store.map_account(connections[1], "equal-account", selected=True, ownership="shared", actor="generated-actor")
    for filters in ({"connection_id": connections[1], "account_id": "equal-account"},
                    {"owner_id": owners[1]}, {"inclusion": "selected"}):
        data = client.get("/api/sandbox/activity", params=filters).json()
        assert data["total"] == 1
        assert data["rows"][0]["connection_id"] == connections[1]
        assert data["rows"][0]["ownership"] == "shared"
        assert data["rows"][0]["amount_cents"] == 2000
    excluded = client.get("/api/sandbox/activity?inclusion=excluded").json()
    assert all(row["connection_id"] == connections[0] for row in excluded["rows"])


def test_activity_pages_have_stable_order_exact_totals_and_an_end(product):
    client, _, _, _ = product
    full = client.get("/api/sandbox/activity?state=all").json()
    collected = []
    for offset in range(0, full["total"] + 2, 2):
        data = client.get("/api/sandbox/activity", params={"state": "all", "offset": offset, "limit": 2}).json()
        assert data["offset"] == offset and data["limit"] == 2
        assert data["total"] == full["total"]
        assert data["has_more"] == (offset + len(data["rows"]) < data["total"])
        collected.extend(data["rows"])
    assert collected == full["rows"]
    assert len({(row["connection_id"], row["transaction_id"]) for row in collected}) == full["total"]


@pytest.mark.parametrize("filters", [
    {"state": "inferred_expense"}, {"inclusion": "unknown"}, {"offset": -1},
    {"limit": 0}, {"limit": 101}, {"q": "x" * 121},
    {"date_from": "not-a-date"}, {"date_to": "2026-02-30"},
    {"date_from": "2026-01-05", "date_to": "2026-01-01"},
    {"account_id": "equal-account"},
])
def test_activity_rejects_invalid_or_ambiguous_queries(product, filters):
    client, _, _, _ = product
    assert client.get("/api/sandbox/activity", params=filters).status_code in {400, 422}


def test_overview_exposes_real_import_counts_and_bounded_recent_rows(product):
    client, store, _, connections = product
    response = client.get("/api/sandbox/overview")
    assert response.status_code == 200
    data = response.json()
    current = client.get("/api/sandbox/activity").json()
    assert data["summary"]["transaction_count"] == current["total"]
    assert data["summary"]["connection_count"] == len(connections)
    assert data["summary"]["account_count"] == len(data["accounts"])
    assert data["summary"]["pending_count"] == sum(row["state"] == "pending" for row in current["rows"])
    assert data["summary"]["last_local_sync"] == max(row["last_local_sync"] for row in data["connections"])
    assert data["recent"] == current["rows"][:8]
    assert "staged" not in data
    assert data["csrf"]
    assert "link_attempts" in data
    for account in data["accounts"]:
        rows = [row for row in current["rows"] if row["connection_id"] == account["connection_id"]]
        assert account["transaction_count"] == len(rows)
        assert account["pending_count"] == sum(row["state"] == "pending" for row in rows)
        assert account["last_date"] == max(row["date"] for row in rows)
    assert all(value not in json.dumps(data) for value in ("access-sandbox", "link-sandbox", "generated-cursor"))
    # Old state consumers retain their explicitly historical staged view.
    assert len(store.snapshot()["staged"]) > current["total"]


def test_product_read_endpoints_never_fetch_or_change_evidence(product, monkeypatch):
    client, store, _, _ = product
    def forbidden(*args, **kwargs):
        raise AssertionError("reading imported data must not load credentials or contact Plaid")
    monkeypatch.setattr(store, "credentials", forbidden)
    monkeypatch.setattr(client.app.state.service, "sync", forbidden)
    def evidence():
        with store.tx() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                    for table in ("staged", "observations", "audit", "connections", "accounts")}
    before = evidence()
    for route in ("/api/sandbox/activity", "/api/sandbox/overview"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert client.get(route, headers={"Host": "generated-evil.invalid"}).status_code == 403
        assert client.get(route, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert evidence() == before
    with TestClient(client.app, base_url="http://127.0.0.1:8772") as stranger:
        assert stranger.get("/api/sandbox/activity").status_code == 403
        assert stranger.get("/api/sandbox/overview").status_code == 403


@pytest.mark.parametrize("route", ["/", "/activity", "/accounts", "/connections"])
def test_product_destinations_render_without_redirect_and_initialize_private_session(tmp_path, route):
    app = create_app(tmp_path / "generated-pages", port=8772)
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        response = client.get(route, follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "plaid-sandbox.js" in response.text
        for destination in ("/", "/activity", "/accounts", "/connections"):
            assert f'href="{destination}"' in response.text
        for unavailable in ("/upload", "/insights", "/goals", "/chat", "/manage"):
            assert f'href="{unavailable}"' not in response.text
        assert 'aria-current="page"' in response.text
        assert "serviceWorker.register" not in response.text
        assert "data-capture-picker" not in response.text
        assert "link-initialize.js" not in response.text
        assert client.get("/api/sandbox/overview").status_code == 200


def test_empty_overview_and_recent_limit_are_truthful(tmp_path):
    app = create_app(tmp_path / "generated-empty", port=8772)
    store = app.state.store
    empty = store.overview()
    assert empty["recent"] == empty["accounts"] == empty["connections"] == []
    assert empty["summary"] == {"connection_count": 0, "account_count": 0, "transaction_count": 0,
                                 "pending_count": 0, "last_local_sync": None}
    owner = store.add_member("Generated Member", "generated-actor")
    browser = store.new_browser()
    session = store.new_link(browser["id"], owner, "link-sandbox-generated-many")
    connection = store.finish_exchange(session, owner, "generated-many", "access-sandbox-generated-many", browser["id"])
    store.save_accounts(connection, [{"account_id": "generated-account", "name": "Generated Checking"}])
    rows = [{"transaction_id": f"generated-{index:02}", "account_id": "generated-account", "amount": "1.01",
             "date": "2026-01-01", "name": "Generated Purchase", "iso_currency_code": "CAD"} for index in range(20)]
    store.commit_window(connection, "", "generated-many-cursor", [("added", row) for row in rows], None, browser["id"])
    data = store.overview()
    assert len(data["recent"]) == 8
    assert data["summary"]["transaction_count"] == len(rows)
