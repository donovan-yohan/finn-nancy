"""Generated-only informed approval and retained mutation-authority contracts."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from html import unescape
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, repo_actions
from app.web.filters import money


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key, value in {
        "STRICT_LOCAL_MODE": "true", "READ_ONLY": "false", "PLAID_ENABLED": "false",
        "API_TOKEN": "", "EMBEDDINGS_ENABLED": "false", "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def enqueue(db, payload=None):
    with engine.write_tx(db) as conn:
        return repo_actions.enqueue_proposal(
            conn, kind="recategorization",
            payload=payload or {"transaction_id": 3, "to_category_id": 5},
            evidence={"transaction_ids": [3], "category_ids": [5]},
            confidence=.91, rationale="Generated approval consequence", agent_run_id="test:consequence",
        )


def article(text, proposal_id):
    match = re.search(r'id="proposal-' + str(proposal_id) + r'">(.*?)</article>', text, re.S)
    assert match is not None, "Proposal missing from queue"
    return match[1]


def before_primary(text, proposal_id):
    return unescape(article(text, proposal_id).split("Approve as-is")[0])


def dump(db):
    with engine.read_conn(db, read_only=True) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        return "\n".join(conn.iterdump())


def test_genuine_proposal_names_exact_transaction_and_category_change_before_approve(app_env):
    proposal_id = enqueue(app_env)
    with engine.read_conn(app_env, read_only=True) as conn:
        txn = conn.execute("SELECT t.*, a.currency FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.id=3").fetchone()
        current = conn.execute("SELECT c.name FROM categories c JOIN transaction_splits s ON s.category_id=c.id WHERE s.transaction_id=3").fetchone()[0]
        target = conn.execute("SELECT name FROM categories WHERE id=5").fetchone()[0]
    from app.web.app import create_app
    client = TestClient(create_app())  # Never enter lifespan or start workers.
    before = dump(app_env)
    response = client.get("/actions")
    client.close()
    assert response.status_code == 200
    primary = before_primary(response.text, proposal_id)
    assert txn["description"] in primary
    assert txn["posted_on"] in primary
    assert money(txn["amount_cents"]) + " " + txn["currency"] in primary
    assert f"{current} → {target}" in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", primary))
    assert 'aria-label="Category change"' in primary
    assert dump(app_env) == before


@pytest.fixture
def web(app_env):
    from app.web.app import create_app

    client = TestClient(create_app())
    yield client
    client.close()


def summary(text, proposal_id):
    match = re.search(r'<section class="manage-consequence"[^>]*>(.*?)</section>',
                      before_primary(text, proposal_id), re.S)
    assert match is not None, "Consequence missing before primary approval"
    return unescape(match[1])


def raw_payload(db, proposal_id, payload):
    # A previously valid proposal can outlive references or contain legacy bad data.
    with engine.write_tx(db) as conn:
        conn.execute("UPDATE proposed_actions SET payload_json=? WHERE id=?",
                     (json.dumps(payload), proposal_id))


def split_transaction(db):
    with engine.write_tx(db) as conn:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id=3")
        first = conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (3,4,-10000)").lastrowid
        second = conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (3,6,-6243)").lastrowid
    return first, second


def test_selected_split_explains_only_its_exact_contribution_and_real_apply_revert(app_env, web):
    first, second = split_transaction(app_env)
    proposal_id = enqueue(app_env, {"transaction_id": 3, "split_id": first, "to_category_id": 5})
    text = summary(web.get("/actions").text, proposal_id)
    assert "Transaction total -$162.43 CAD" in text
    assert "Selected split amount -$100.00 CAD" in text
    assert "Other splits stay unchanged." in text
    with engine.read_conn(app_env) as conn:
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM categories")}
    assert f'{names[4]} → {names[5]}' in re.sub(r"<[^>]+>", "", text)
    assert names[6] not in text
    assert web.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT id,category_id,amount_cents FROM transaction_splits WHERE transaction_id=3 ORDER BY id")]
        assert rows == [(first, 5, -10000), (second, 6, -6243)]
    assert web.post(f"/actions/{proposal_id}/revert", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        assert [tuple(r) for r in conn.execute("SELECT id,category_id,amount_cents FROM transaction_splits WHERE transaction_id=3 ORDER BY id")] == [(first, 4, -10000), (second, 6, -6243)]


@pytest.mark.parametrize("split_id", [None, 999999, 1, "not-a-split", 3.5, True, {}, []])
def test_unresolved_split_is_honest_without_guessing_full_transaction_change(app_env, web, split_id):
    proposal_id = enqueue(app_env)
    split_transaction(app_env)
    payload = {"transaction_id": 3, "to_category_id": 5}
    if split_id is not None:
        payload["split_id"] = split_id
    raw_payload(app_env, proposal_id, payload)
    before = dump(app_env)
    response = web.get("/actions")
    assert response.status_code == 200
    text = summary(response.text, proposal_id)
    assert "Category change unavailable" in text
    assert "→" not in text and "Other splits stay unchanged" not in text
    assert "Transaction total -$162.43 CAD" in text
    assert dump(app_env) == before


@pytest.mark.parametrize("transaction_id", [None, 999999, "bad", 3.5, True, {}, [], "03", " 3 ", 10**50, "9" * 5000])
def test_missing_or_malformed_transaction_is_unavailable_not_coerced(app_env, web, transaction_id):
    proposal_id = enqueue(app_env)
    raw_payload(app_env, proposal_id, {"transaction_id": transaction_id, "to_category_id": 5})
    before = dump(app_env)
    response = web.get("/actions")
    assert response.status_code == 200
    text = summary(response.text, proposal_id)
    assert "Transaction unavailable" in text
    assert "weekly groceries" not in text and "→" not in text
    assert dump(app_env) == before


@pytest.mark.parametrize("target", [None, 999999, "bad", 5.5, True, {}, [], "05", " 5 ", 10**50])
def test_missing_or_malformed_target_is_unavailable_without_name_fallback(app_env, web, target):
    proposal_id = enqueue(app_env)
    raw_payload(app_env, proposal_id, {"transaction_id": 3, "to_category_id": target})
    before = dump(app_env)
    response = web.get("/actions")
    assert response.status_code == 200
    assert "Proposed category unavailable" in summary(response.text, proposal_id)
    assert dump(app_env) == before


def test_valid_name_fallback_uses_canonical_name_and_real_approval(app_env, web):
    with engine.read_conn(app_env) as conn:
        name = conn.execute("SELECT name FROM categories WHERE id=5").fetchone()[0]
    proposal_id = enqueue(app_env, {"transaction_id": 3, "to_category_name": f" {name.swapcase()} "})
    assert f"→ {name}" in re.sub(r"<[^>]+>", "", summary(web.get("/actions").text, proposal_id))
    assert web.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT category_id FROM transaction_splits WHERE transaction_id=3").fetchone()[0] == 5


def test_invalid_explicit_target_does_not_fall_back_to_valid_name(app_env, web):
    proposal_id = enqueue(app_env)
    with engine.read_conn(app_env) as conn:
        name = conn.execute("SELECT name FROM categories WHERE id=5").fetchone()[0]
    raw_payload(app_env, proposal_id, {"transaction_id": 3, "to_category_id": "05", "to_category_name": name})
    text = summary(web.get("/actions").text, proposal_id)
    assert "Proposed category unavailable" in text
    assert f"→ {name}" not in text


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_hostile_names_escape_and_extreme_cents_remain_exact(app_env, web):
    hostile = '\'"><img src=x onerror=globalThis.PWNED=1><script>PWNED</script>&'
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET description=?, amount_cents=-9007199254740993 WHERE id=3", (hostile,))
        conn.execute("UPDATE transaction_splits SET amount_cents=-9007199254740993 WHERE transaction_id=3")
        conn.execute("UPDATE categories SET name=? WHERE id=4", ("Current " + hostile,))
        conn.execute("UPDATE categories SET name=? WHERE id=5", ("Proposed " + hostile,))
    proposal_id = enqueue(app_env)
    before = dump(app_env)
    response = web.get("/actions")
    assert response.status_code == 200
    assert "-$90,071,992,547,409.93 CAD" in summary(response.text, proposal_id)
    assert "Current " + hostile in summary(response.text, proposal_id)
    assert "Proposed " + hostile in summary(response.text, proposal_id)
    assert not any(tag == "img" and attrs.get("src") == "x" or attrs.get("onerror") for tag, attrs in Tags(response.text).tags)
    assert "<script>PWNED</script>" not in response.text
    assert dump(app_env) == before


def test_get_projection_runs_through_query_only_connections_without_write_admission(app_env, web, monkeypatch):
    enqueue(app_env)
    before = dump(app_env)
    original = engine.read_conn
    reads = []

    @contextmanager
    def query_only(path, **kwargs):
        with original(path, read_only=True) as conn:
            reads.append(conn.execute("PRAGMA query_only").fetchone()[0])
            yield conn

    def forbidden(*args, **kwargs):
        raise AssertionError("GET entered writer")

    monkeypatch.setattr(engine, "read_conn", query_only)
    monkeypatch.setattr(engine, "write_tx", forbidden)
    assert web.get("/actions").status_code == 200
    assert reads and all(reads)
    assert dump(app_env) == before


def test_ambiguous_split_approval_is_still_refused_without_any_write(app_env, web):
    proposal_id = enqueue(app_env)
    split_transaction(app_env)
    before = dump(app_env)
    assert "cannot resolve a single split" in summary(web.get("/actions").text, proposal_id)
    refused = web.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert refused.status_code == 400
    assert refused.json()["detail"] == "transaction must have exactly one split"
    assert dump(app_env) == before


def test_no_split_proposal_reports_unavailable(app_env, web):
    proposal_id = enqueue(app_env)
    with engine.write_tx(app_env) as conn:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id=3")
    before = dump(app_env)
    text = summary(web.get("/actions").text, proposal_id)
    assert "cannot resolve a single split" in text and "→" not in text
    assert dump(app_env) == before


def test_closed_period_refusal_preserves_full_dump_and_explicit_override_still_applies(app_env, web):
    from app.db import repo_close

    proposal_id = enqueue(app_env)
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-01", reason="Generated closed-period consequence")
    before = dump(app_env)
    response = web.get("/actions")
    assert "2026-01 is closed" in before_primary(response.text, proposal_id)
    inputs = [attrs for tag, attrs in Tags(article(response.text, proposal_id)).tags
              if tag == "input" and attrs.get("name") == "override"]
    assert len(inputs) == 2 and all(attrs.get("value") == "1" and "checked" not in attrs for attrs in inputs)
    refused = web.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert refused.status_code == 400 and "closed" in refused.text
    assert dump(app_env) == before
    assert web.post(f"/actions/{proposal_id}/approve", data={"override": "1"}, follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal is not None and proposal["status"] == "approved"
        assert conn.execute("SELECT category_id FROM transaction_splits WHERE transaction_id=3").fetchone()[0] == 5
        assert repo_close.list_audit(conn, "2026-01")


@pytest.mark.parametrize("payload", [
    {"transaction_id": "3", "to_category_id": "5", "split_id": "3"},
    {"transaction_id": 3, "to_category_id": "", "to_category_name": "restaurants"},
])
def test_supported_reference_forms_resolve_without_modifying_payload(app_env, web, payload):
    proposal_id = enqueue(app_env, payload)
    before = dump(app_env)
    page = web.get("/actions")
    text = summary(page.text, proposal_id)
    assert "Groceries → Restaurants" in re.sub(r"<[^>]+>", "", text)
    assert dump(app_env) == before
