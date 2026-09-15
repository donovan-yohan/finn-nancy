from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import (
    engine,
    repo_actions,
    repo_budgets,
    repo_ledger,
    repo_merchant_knowledge,
)
from app.db.repo_merchant_knowledge import Evidence


def _set_embeddings_enabled(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "true" if enabled else "false")
    get_settings.cache_clear()


def _client() -> TestClient:
    from app.web.app import create_app

    return TestClient(create_app())


def _enqueue_recategorization(
    db_path: str,
    *,
    transaction_id: int = 3,
    to_category_id: int = 5,
) -> int:
    with engine.write_tx(db_path) as conn:
        return repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": transaction_id, "to_category_id": to_category_id},
            evidence={
                "transaction_ids": [transaction_id],
                "statement_line_ids": [],
                "category_ids": [to_category_id],
            },
            confidence=0.91,
            rationale="Synthetic Market belongs elsewhere",
            agent_run_id="run-recat",
        )


def _insert_raw_proposal(conn, *, kind: str, payload: dict) -> int:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    cur = conn.execute(
        """
        INSERT INTO proposed_actions(
          kind, payload_json, original_payload_json, evidence_json,
          confidence, rationale, agent_run_id, status
        )
        VALUES (?, ?, ?, '{}', 0.5, 'raw proposal', 'raw-run', 'proposed')
        """,
        (kind, payload_json, payload_json),
    )
    return int(cur.lastrowid)


def _split_state(db_path: str, transaction_id: int = 3) -> dict:
    with engine.read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT t.amount_cents AS transaction_amount_cents,
                   ts.category_id,
                   ts.amount_cents AS split_amount_cents
            FROM transactions t
            JOIN transaction_splits ts ON ts.transaction_id = t.id
            WHERE t.id=?
            """,
            (transaction_id,),
        ).fetchone()
    return dict(row)


def _ledger_state(db_path: str, transaction_id: int = 3) -> dict:
    with engine.read_conn(db_path) as conn:
        txn = conn.execute(
            "SELECT amount_cents FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
        splits = conn.execute(
            """
            SELECT id, category_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id=?
            ORDER BY id
            """,
            (transaction_id,),
        ).fetchall()
    return {
        "transaction_amount_cents": txn["amount_cents"],
        "splits": [dict(row) for row in splits],
    }


def _insert_test_transaction(
    db_path: str,
    *,
    category_id: int,
    amount_cents: int,
    source: str = "sample",
    description: str = "test transaction",
    external_id: str = "test-transaction",
    flow_kind: str = "unknown",
) -> int:
    with engine.write_tx(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source, external_id, flow_kind
            )
            VALUES (1, '2026-04-01', ?, 'Test Merchant', ?, ?, ?, ?)
            """,
            (description, amount_cents, source, external_id, flow_kind),
        )
        transaction_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (?, ?, ?)
            """,
            (transaction_id, category_id, amount_cents),
        )
    return transaction_id


def test_enqueue_recategorization_appears_in_queue_and_audit(app_env):
    proposal_id = _enqueue_recategorization(app_env)

    with engine.read_conn(app_env) as conn:
        proposals = repo_actions.list_proposals(conn)
        trail = repo_actions.audit_trail(conn, proposal_id)

    assert [proposal["id"] for proposal in proposals] == [proposal_id]
    assert proposals[0]["payload"] == {"transaction_id": 3, "to_category_id": 5}
    assert proposals[0]["status_label"] == "awaiting approval"
    assert [(row["from_status"], row["to_status"]) for row in trail] == [(None, "proposed")]

    page = _client().get("/actions")
    assert page.status_code == 200
    assert "Synthetic Market belongs elsewhere" in page.text
    assert 'href="/txn/3/edit">#3</a>' in page.text
    assert 'href="/categories#category-5">#5</a>' in page.text


def test_reject_and_revert_forms_require_confirm(app_env):
    # FN-116: reject (queue) and revert (audit) are destructive; each must guard its
    # POST with a confirm step whose copy names the specific action.
    proposal_id = _enqueue_recategorization(app_env)
    client = _client()

    queue = client.get("/actions")
    assert queue.status_code == 200
    assert f'action="/actions/{proposal_id}/reject"' in queue.text
    assert f'confirm("Reject action #{proposal_id} (recategorization)?")' in queue.text

    approved = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    audit = client.get(f"/actions/{proposal_id}/audit")
    assert audit.status_code == 200
    assert f'action="/actions/{proposal_id}/revert"' in audit.text
    assert f'confirm("Revert action #{proposal_id} (recategorization)?")' in audit.text


def test_approve_recategorization_applies_and_repost_is_noop(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    client = _client()

    response = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/actions"

    assert _split_state(app_env) == {
        "transaction_amount_cents": -16243,
        "category_id": 5,
        "split_amount_cents": -16243,
    }
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)
        claim = repo_merchant_knowledge.current_claim(
            conn,
            int(proposal["revert"]["knowledge_claim_id"]),
        )
        resolution = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=3 AND transaction_split_id=3
            """
        ).fetchone()

    assert proposal["status"] == "approved"
    assert proposal["applied_at"] is not None
    revert = dict(proposal["revert"])
    knowledge_claim_id = revert.pop("knowledge_claim_id")
    assert isinstance(knowledge_claim_id, int)
    assert claim["event_kind"] == "accepted"
    assert claim["trust_state"] == "human_confirmed"
    assert claim["category_id"] == 5
    assert claim["transaction_id"] == 3
    assert claim["transaction_split_id"] == 3
    assert claim["proposed_action_id"] == proposal_id
    assert resolution["resolution_status"] == "resolved"
    assert revert == {
        "transaction_id": 3,
        "split_id": 3,
        "category_id": 4,
        "transaction_amount_cents": -16243,
        "split_amount_cents": -16243,
        "expected": {
            "category_id": 5,
            "transaction_amount_cents": -16243,
            "split_amount_cents": -16243,
        },
    }
    assert [(row["from_status"], row["to_status"]) for row in trail] == [
        (None, "proposed"),
        ("proposed", "approved"),
    ]

    again = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert again.status_code == 303
    assert _split_state(app_env)["category_id"] == 5
    with engine.read_conn(app_env) as conn:
        assert len(repo_actions.audit_trail(conn, proposal_id)) == 2


def test_edit_then_approve_uses_edited_payload_and_keeps_original(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    response = _client().post(
        f"/actions/{proposal_id}/approve",
        data={"to_category_id": "6"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert _split_state(app_env)["category_id"] == 6
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)

    assert proposal["status"] == "edited_approved"
    assert proposal["payload"]["to_category_id"] == 6
    assert proposal["original_payload"]["to_category_id"] == 5
    assert [(row["from_status"], row["to_status"]) for row in trail] == [
        (None, "proposed"),
        ("proposed", "proposed"),
        ("proposed", "edited_approved"),
    ]
    assert trail[1]["detail"]["event"] == "payload_edited"


def test_reject_with_feedback_applies_nothing(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    response = _client().post(
        f"/actions/{proposal_id}/reject",
        data={"feedback": "merchant evidence is weak"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _split_state(app_env)["category_id"] == 4

    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)
    assert proposal["status"] == "rejected"
    assert proposal["feedback"] == "merchant evidence is weak"
    assert (trail[-1]["from_status"], trail[-1]["to_status"]) == ("proposed", "rejected")


def test_request_evidence_keeps_active_without_mutation(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    response = _client().post(
        f"/actions/{proposal_id}/request-evidence",
        data={"feedback": "show the receipt"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _split_state(app_env)["category_id"] == 4

    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        proposals = repo_actions.list_proposals(conn)
    assert proposal["status"] == "needs_evidence"
    assert proposal["feedback"] == "show the receipt"
    assert [proposal["id"] for proposal in proposals] == [proposal_id]


def test_snooze_hides_then_resurfaces_as_of_date(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    response = _client().post(
        f"/actions/{proposal_id}/snooze",
        data={"snoozed_until": "2999-01-01"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        hidden = repo_actions.list_proposals(conn)
        resurfaced = repo_actions.list_proposals(conn, as_of="2999-01-01")
    assert proposal["status"] == "snoozed"
    assert proposal["snoozed_until"] == "2999-01-01"
    assert hidden == []
    assert [proposal["id"] for proposal in resurfaced] == [proposal_id]


def test_audit_trail_returns_all_edit_and_approval_transitions(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    _client().post(
        f"/actions/{proposal_id}/approve",
        data={"to_category_id": "6"},
        follow_redirects=False,
    )

    with engine.read_conn(app_env) as conn:
        trail = repo_actions.audit_trail(conn, proposal_id)

    assert [row["to_status"] for row in trail] == ["proposed", "proposed", "edited_approved"]
    assert [row["detail"].get("event") for row in trail] == ["enqueued", "payload_edited", None]
    assert trail[-1]["detail"]["apply"]["to_category_id"] == 6

    page = _client().get(f"/actions/{proposal_id}/audit")
    assert page.status_code == 200
    assert "Audit trail" in page.text
    assert "edited + approved" in page.text


def test_revert_recategorization_restores_original_category_and_amount(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    client = _client()

    approved = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    assert _split_state(app_env) == {
        "transaction_amount_cents": -16243,
        "category_id": 5,
        "split_amount_cents": -16243,
    }

    reverted = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert reverted.status_code == 303
    assert _split_state(app_env) == {
        "transaction_amount_cents": -16243,
        "category_id": 4,
        "split_amount_cents": -16243,
    }
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)
        claim = repo_merchant_knowledge.current_claim(
            conn,
            int(proposal["revert"]["knowledge_claim_id"]),
        )
        resolution = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=3 AND transaction_split_id=3
            """
        ).fetchone()
    assert proposal["status"] == "reverted"
    assert trail[-1]["to_status"] == "reverted"
    assert claim["event_kind"] == "undo"
    assert resolution["resolution_status"] == "unresolved"


def test_recategorization_correction_and_revert_do_not_resurrect_prior_claim(
    app_env,
):
    client = _client()
    first_proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    assert client.post(
        f"/actions/{first_proposal_id}/approve",
        follow_redirects=False,
    ).status_code == 303
    with engine.read_conn(app_env) as conn:
        first = repo_actions.get_proposal(conn, first_proposal_id)
        first_claim_id = int(first["revert"]["knowledge_claim_id"])

    second_proposal_id = _enqueue_recategorization(app_env, to_category_id=6)
    assert client.post(
        f"/actions/{second_proposal_id}/approve",
        follow_redirects=False,
    ).status_code == 303
    with engine.read_conn(app_env) as conn:
        second = repo_actions.get_proposal(conn, second_proposal_id)
        second_claim_id = int(second["revert"]["knowledge_claim_id"])
        first_after_correction = repo_merchant_knowledge.current_claim(
            conn,
            first_claim_id,
        )
        second_after_correction = repo_merchant_knowledge.current_claim(
            conn,
            second_claim_id,
        )

    assert second_after_correction["event_kind"] == "corrected"
    assert second_after_correction["supersedes_claim_id"] == first_claim_id
    assert first_after_correction["event_kind"] == "retired"

    assert client.post(
        f"/actions/{second_proposal_id}/revert",
        follow_redirects=False,
    ).status_code == 303
    with engine.read_conn(app_env) as conn:
        first_after_revert = repo_merchant_knowledge.current_claim(
            conn,
            first_claim_id,
        )
        second_after_revert = repo_merchant_knowledge.current_claim(
            conn,
            second_claim_id,
        )
        resolution = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=3 AND transaction_split_id=3
            """
        ).fetchone()

    assert _split_state(app_env)["category_id"] == 5
    assert first_after_revert["event_kind"] == "retired"
    assert second_after_revert["event_kind"] == "undo"
    assert resolution["resolution_status"] == "unresolved"


def test_rejecting_model_category_proposal_rejects_claim_without_ledger_write(
    app_env,
):
    with engine.write_tx(app_env) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)
        transaction_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=3,
                posted_on="2026-04-05",
                description="MODEL ONLY MARKET",
                counterparty="Model Only Market",
                amount_cents=-1200,
                source="test",
                external_id="model-only-category-proposal",
                source_document_id=None,
                source_confidence=0.7,
                flow_kind="purchase",
            )
        )
        split_id = repo_ledger.insert_split(
            conn,
            transaction_id=transaction_id,
            category_id=uncategorized_id,
            amount_cents=-1200,
        )
        claim_id = repo_merchant_knowledge.propose_category(
            conn,
            descriptor="Model Only Market",
            category_id=4,
            scope=repo_merchant_knowledge.scope_for_transaction(
                conn,
                transaction_id,
            ),
            operation_key="test:model-only-category-proposal",
            actor_kind="model",
            actor="model:test",
            reason="model guessed groceries",
            evidence=Evidence(
                transaction_id=transaction_id,
                transaction_split_id=split_id,
            ),
            provenance_ref="test-run:model-only",
        )
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={
                "transaction_id": transaction_id,
                "to_category_id": 4,
            },
            evidence={
                "merchant_resolution_claim_id": claim_id,
                "transaction_ids": [transaction_id],
                "category_ids": [4],
            },
            confidence=0.7,
            rationale="model category proposal",
            agent_run_id="model:test",
        )

    response = _client().post(
        f"/actions/{proposal_id}/reject",
        data={"feedback": "wrong merchant category"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        claim = repo_merchant_knowledge.current_claim(conn, claim_id)
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE id=?",
            (split_id,),
        ).fetchone()
        resolution = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=? AND transaction_split_id=?
            """,
            (transaction_id, split_id),
        ).fetchone()

    assert proposal["status"] == "rejected"
    assert claim["event_kind"] == "rejected"
    assert claim["trust_state"] == "rejected"
    assert split["category_id"] == uncategorized_id
    assert resolution["resolution_status"] == "unresolved"


def test_recategorization_claim_failure_rolls_back_action_and_split(
    app_env,
    monkeypatch,
):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    def fail_confirmation(*args, **kwargs):
        raise ValueError("forced category-claim failure")

    monkeypatch.setattr(
        repo_merchant_knowledge,
        "confirm_category",
        fail_confirmation,
    )
    response = _client().post(
        f"/actions/{proposal_id}/approve",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "forced category-claim failure" in response.text
    assert _split_state(app_env)["category_id"] == 4
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        event_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM merchant_resolution_events
            WHERE proposed_action_id=?
            """,
            (proposal_id,),
        ).fetchone()[0]
    assert proposal["status"] == "proposed"
    assert event_count == 0


def test_approve_and_revert_recategorization_enqueue_embedding_refresh(app_env, monkeypatch):
    _set_embeddings_enabled(monkeypatch, enabled=True)
    from app.web.routes.actions import approve_action, revert_action

    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)

    approved = approve_action(proposal_id, to_category_id=None, decision=None)
    assert approved.status_code == 303
    with engine.read_conn(app_env) as conn:
        jobs = conn.execute(
            "SELECT id, status FROM jobs WHERE type='embed_transactions' ORDER BY id"
        ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "pending"

    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jobs[0]["id"],))

    reverted = revert_action(proposal_id)
    assert reverted.status_code == 303
    with engine.read_conn(app_env) as conn:
        jobs = conn.execute(
            "SELECT id, status FROM jobs WHERE type='embed_transactions' ORDER BY id"
        ).fetchall()
    assert [job["status"] for job in jobs] == ["done", "pending"]


def test_opening_balance_recategorization_rejected_at_enqueue_and_approve(app_env):
    transaction_id = _insert_test_transaction(
        app_env,
        category_id=8,
        amount_cents=50000,
        source="opening",
        description="opening balance",
        external_id="open:test-action",
    )

    with engine.write_tx(app_env) as conn:
        with pytest.raises(ValueError, match="opening balance"):
            repo_actions.enqueue_proposal(
                conn,
                kind="recategorization",
                payload={"transaction_id": transaction_id, "to_category_id": 5},
                evidence={},
                confidence=0.8,
                rationale="Opening balance should stay transfer",
                agent_run_id="run-opening",
            )
        proposal_id = _insert_raw_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": transaction_id, "to_category_id": 5},
        )

    failed = _client().post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert failed.status_code == 400
    assert "opening balance" in failed.text
    assert _split_state(app_env, transaction_id) == {
        "transaction_amount_cents": 50000,
        "category_id": 8,
        "split_amount_cents": 50000,
    }
    with engine.read_conn(app_env) as conn:
        assert repo_actions.get_proposal(conn, proposal_id)["status"] == "proposed"


def test_approve_recategorization_rejects_multi_split_and_rolls_back_edit(app_env):
    with engine.write_tx(app_env) as conn:
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (4, 9, -100)
            """
        )
        with pytest.raises(ValueError, match="exactly one split"):
            repo_actions.enqueue_proposal(
                conn,
                kind="recategorization",
                payload={"transaction_id": 4, "to_category_id": 6},
                evidence={},
                confidence=0.8,
                rationale="Multi split should not enqueue",
                agent_run_id="run-multi",
            )

    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    with engine.write_tx(app_env) as conn:
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (3, 9, -1000)
            """
        )
    before = _ledger_state(app_env)

    failed = _client().post(
        f"/actions/{proposal_id}/approve",
        data={"to_category_id": "6"},
        follow_redirects=False,
    )
    assert failed.status_code == 400
    assert _ledger_state(app_env) == before
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)
    assert proposal["status"] == "proposed"
    assert proposal["payload"]["to_category_id"] == 5
    assert len(trail) == 1


def test_recategorization_preserves_refunds_and_reverts_kind_changes(app_env):
    client = _client()
    refund_id = _insert_test_transaction(
        app_env,
        category_id=4,
        amount_cents=1500,
        description="refund",
        external_id="refund:test-action",
        flow_kind="refund",
    )
    refund_proposal = _enqueue_recategorization(
        app_env,
        transaction_id=refund_id,
        to_category_id=5,
    )

    assert client.post(f"/actions/{refund_proposal}/approve", follow_redirects=False).status_code == 303
    assert _split_state(app_env, refund_id) == {
        "transaction_amount_cents": 1500,
        "category_id": 5,
        "split_amount_cents": 1500,
    }
    assert client.post(f"/actions/{refund_proposal}/revert", follow_redirects=False).status_code == 303
    assert _split_state(app_env, refund_id) == {
        "transaction_amount_cents": 1500,
        "category_id": 4,
        "split_amount_cents": 1500,
    }

    income_id = _insert_test_transaction(
        app_env,
        category_id=1,
        amount_cents=1500,
        description="income conversion",
        external_id="income-conversion:test-action",
        flow_kind="income",
    )
    income_proposal = _enqueue_recategorization(
        app_env,
        transaction_id=income_id,
        to_category_id=5,
    )

    assert client.post(f"/actions/{income_proposal}/approve", follow_redirects=False).status_code == 303
    assert _split_state(app_env, income_id) == {
        "transaction_amount_cents": 1500,
        "category_id": 5,
        "split_amount_cents": 1500,
    }
    assert client.post(f"/actions/{income_proposal}/revert", follow_redirects=False).status_code == 303
    assert _split_state(app_env, income_id) == {
        "transaction_amount_cents": 1500,
        "category_id": 1,
        "split_amount_cents": 1500,
    }


def test_recategorization_never_flips_typed_purchase_from_category(app_env):
    purchase_id = _insert_test_transaction(
        app_env,
        category_id=4,
        amount_cents=-500,
        description="typed purchase",
        external_id="typed-purchase:recategory",
        flow_kind="purchase",
    )
    proposal_id = _enqueue_recategorization(
        app_env,
        transaction_id=purchase_id,
        to_category_id=5,
    )

    assert _client().post(
        f"/actions/{proposal_id}/approve",
        follow_redirects=False,
    ).status_code == 303
    assert _split_state(app_env, purchase_id) == {
        "transaction_amount_cents": -500,
        "category_id": 5,
        "split_amount_cents": -500,
    }
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT flow_kind FROM transactions WHERE id=?",
            (purchase_id,),
        ).fetchone()
    assert row["flow_kind"] == "purchase"


def test_typed_purchase_cannot_move_to_income_category(app_env):
    purchase_id = _insert_test_transaction(
        app_env,
        category_id=4,
        amount_cents=-500,
        description="typed purchase invalid category",
        external_id="typed-purchase:income-category",
        flow_kind="purchase",
    )
    with engine.write_tx(app_env) as conn:
        with pytest.raises(ValueError, match="require an expense category"):
            repo_actions.enqueue_proposal(
                conn,
                kind="recategorization",
                payload={
                    "transaction_id": purchase_id,
                    "to_category_id": 1,
                },
                evidence={},
                confidence=0.8,
                rationale="invalid typed purchase category",
                agent_run_id="run-invalid-purpose",
            )
        proposal_id = _insert_raw_proposal(
            conn,
            kind="recategorization",
            payload={
                "transaction_id": purchase_id,
                "to_category_id": 1,
            },
        )

    response = _client().post(
        f"/actions/{proposal_id}/approve",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "require an expense category" in response.text
    assert _split_state(app_env, purchase_id) == {
        "transaction_amount_cents": -500,
        "category_id": 4,
        "split_amount_cents": -500,
    }


def test_revert_recategorization_refuses_diverged_current_state(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    client = _client()

    assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET amount_cents=-11111 WHERE id=3")
        conn.execute(
            """
            UPDATE transaction_splits
            SET category_id=6, amount_cents=-11111
            WHERE transaction_id=3
            """
        )

    failed = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert failed.status_code == 400
    assert "transaction has changed since this action was applied" in failed.text
    assert _split_state(app_env) == {
        "transaction_amount_cents": -11111,
        "category_id": 6,
        "split_amount_cents": -11111,
    }
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        statuses = [row["to_status"] for row in repo_actions.audit_trail(conn, proposal_id)]
    assert proposal["status"] == "approved"
    assert "reverted" not in statuses


def test_revert_recategorization_refuses_split_shape_change(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    client = _client()

    assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.write_tx(app_env) as conn:
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (3, 9, -1000)
            """
        )
    before = _ledger_state(app_env)

    failed = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert failed.status_code == 400
    assert _ledger_state(app_env) == before
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        statuses = [row["to_status"] for row in repo_actions.audit_trail(conn, proposal_id)]
    assert proposal["status"] == "approved"
    assert "reverted" not in statuses


def test_subscription_label_approve_idempotent_and_revert_remove_or_restore(app_env):
    client = _client()
    with engine.write_tx(app_env) as conn:
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="subscription_label",
            payload={"merchant": "Approval Stream", "account_id": 1, "decision": "subscription"},
            evidence={"transaction_ids": [], "statement_line_ids": [], "category_ids": []},
            confidence=0.8,
            rationale="Monthly stream pattern",
            agent_run_id="run-sub",
        )

    approved = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    again = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert again.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            """
            SELECT decision
            FROM subscription_watchlist_decisions
            WHERE merchant='Approval Stream' AND account_id=1
            """
        ).fetchone()
        assert row["decision"] == "subscription"
        assert len(repo_actions.audit_trail(conn, proposal_id)) == 2

    removed = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert removed.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT 1 FROM subscription_watchlist_decisions WHERE merchant='Approval Stream' AND account_id=1"
        ).fetchone()
    assert row is None

    with engine.write_tx(app_env) as conn:
        conn.execute(
            """
            INSERT INTO subscription_watchlist_decisions(merchant, account_id, decision)
            VALUES ('Prior Stream', 1, 'watch_next_month')
            """
        )
        restore_id = repo_actions.enqueue_proposal(
            conn,
            kind="subscription_label",
            payload={"merchant": "Prior Stream", "account_id": 1, "decision": "not_subscription"},
            evidence={},
            confidence=0.8,
            rationale="Different label",
            agent_run_id="run-sub-2",
        )

    assert client.post(f"/actions/{restore_id}/approve", follow_redirects=False).status_code == 303
    assert client.post(f"/actions/{restore_id}/revert", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            """
            SELECT decision
            FROM subscription_watchlist_decisions
            WHERE merchant='Prior Stream' AND account_id=1
            """
        ).fetchone()
    assert row["decision"] == "watch_next_month"


def test_subscription_label_revert_refuses_later_decision_change(app_env):
    client = _client()
    with engine.write_tx(app_env) as conn:
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="subscription_label",
            payload={"merchant": "Approval Stream", "account_id": 1, "decision": "subscription"},
            evidence={},
            confidence=0.8,
            rationale="Monthly stream pattern",
            agent_run_id="run-sub",
        )

    assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.write_tx(app_env) as conn:
        repo_budgets.set_subscription_watchlist_decision(
            conn,
            merchant="Approval Stream",
            account_id=1,
            decision="already_known",
        )

    failed = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert failed.status_code == 400
    assert "watchlist decision has changed since this action was applied" in failed.text
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            """
            SELECT decision
            FROM subscription_watchlist_decisions
            WHERE merchant='Approval Stream' AND account_id=1
            """
        ).fetchone()
        proposal = repo_actions.get_proposal(conn, proposal_id)
        statuses = [audit["to_status"] for audit in repo_actions.audit_trail(conn, proposal_id)]
    assert row["decision"] == "already_known"
    assert proposal["status"] == "approved"
    assert "reverted" not in statuses


def test_malformed_recategorization_payload_rejected_at_enqueue_and_approve(app_env):
    with engine.write_tx(app_env) as conn:
        with pytest.raises(ValueError, match="to_category_id"):
            repo_actions.enqueue_proposal(
                conn,
                kind="recategorization",
                payload={"transaction_id": 3, "to_category_id": [5]},
                evidence={},
                confidence=0.8,
                rationale="Malformed category",
                agent_run_id="run-bad",
            )
        proposal_id = _insert_raw_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": 3, "to_category_id": [5]},
        )

    failed = _client().post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert failed.status_code == 400
    with engine.read_conn(app_env) as conn:
        assert repo_actions.get_proposal(conn, proposal_id)["status"] == "proposed"


def test_type_normalization_prevents_spurious_edit_and_preselects_category(app_env):
    with engine.write_tx(app_env) as conn:
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": "3", "to_category_id": "5"},
            evidence={"transaction_ids": ["3"], "statement_line_ids": [], "category_ids": ["5"]},
            confidence=0.9,
            rationale="String ids still normalize",
            agent_run_id="run-strings",
        )

    page = _client().get("/actions")
    assert page.status_code == 200
    assert 'value="5" selected' in page.text

    response = _client().post(
        f"/actions/{proposal_id}/approve",
        data={"to_category_id": "5"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        trail = repo_actions.audit_trail(conn, proposal_id)
    assert proposal["status"] == "approved"
    assert proposal["payload"] == {"transaction_id": 3, "to_category_id": 5}
    assert [row["detail"].get("event") for row in trail] == ["enqueued", None]


def test_snooze_normalizes_compact_iso_dates(app_env):
    proposal_id = _enqueue_recategorization(app_env, to_category_id=5)
    with engine.write_tx(app_env) as conn:
        repo_actions.snooze(conn, proposal_id, snoozed_until="20260801", actor="user")

    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        hidden = repo_actions.list_proposals(conn, as_of="2026-07-31")
        resurfaced = repo_actions.list_proposals(conn, as_of="2026-08-01")
    assert proposal["snoozed_until"] == "2026-08-01"
    assert hidden == []
    assert [proposal["id"] for proposal in resurfaced] == [proposal_id]


def test_stub_kind_can_queue_reject_and_approve_is_400_without_corruption(app_env):
    with engine.write_tx(app_env) as conn:
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="budget_update",
            payload={"category_id": 4, "amount_cents": 50000},
            evidence={"transaction_ids": [], "statement_line_ids": [], "category_ids": [4]},
            confidence=0.7,
            rationale="Raise grocery budget",
            agent_run_id="run-budget",
        )

    page = _client().get("/actions")
    assert page.status_code == 200
    assert "Raise grocery budget" in page.text

    failed = _client().post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert failed.status_code == 400
    assert "not implemented" in failed.text
    with engine.read_conn(app_env) as conn:
        proposal = repo_actions.get_proposal(conn, proposal_id)
        assert proposal["status"] == "proposed"
        assert len(repo_actions.audit_trail(conn, proposal_id)) == 1

    rejected = _client().post(
        f"/actions/{proposal_id}/reject",
        data={"feedback": "budget changes need manual review"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert repo_actions.get_proposal(conn, proposal_id)["status"] == "rejected"
