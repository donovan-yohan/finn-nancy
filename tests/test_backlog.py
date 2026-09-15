from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.backlog.batch import run_batch
from app.backlog.schemas import BacklogCategoryGuess
from app.backlog.suggest import (
    get_backlog_transaction,
    list_uncategorized_expense_backlog,
    suggest_category,
)
from app.actions.recategorization import RecategorizationHandler
from app.db import (
    engine,
    repo_actions,
    repo_labels,
    repo_ledger,
    repo_merchant_knowledge,
    repo_statements,
)
from app.db.repo_merchant_knowledge import Evidence
from app.evals.metrics import expense_uncategorized_metrics


def _client() -> TestClient:
    from app.web.app import create_app

    return TestClient(create_app())


def _insert_backlog_txn(
    db_path: str,
    *,
    merchant: str = "Mystery Market",
    description: str = "mystery purchase",
    amount_cents: int = -1234,
    external_id: str = "backlog:test",
) -> int:
    with engine.write_tx(db_path) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)
        cur = conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source, external_id, source_confidence, flow_kind
            )
            VALUES (3, '2026-04-03', ?, ?, ?, 'test', ?, 1.0, 'purchase')
            """,
            (description, merchant, amount_cents, external_id),
        )
        txn_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (?, ?, ?)
            """,
            (txn_id, uncategorized_id, amount_cents),
        )
        return txn_id


def _enqueue_recategorization(
    db_path: str,
    *,
    transaction_id: int,
    to_category_id: int,
    agent_run_id: str = "backlog:test-label",
) -> int:
    with engine.write_tx(db_path) as conn:
        return repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": transaction_id, "to_category_id": to_category_id},
            evidence={
                "transaction_ids": [transaction_id],
                "category_ids": [to_category_id],
                "similar_transaction_ids": [3, 11, 17],
            },
            confidence=0.8,
            rationale="test recategorization",
            agent_run_id=agent_run_id,
        )


def _confirm_transaction_category(
    conn,
    transaction_id: int,
    *,
    operation_key: str | None = None,
) -> int:
    row = conn.execute(
        """
        SELECT
          t.id,
          COALESCE(NULLIF(t.counterparty, ''), t.description) AS descriptor,
          s.id AS split_id,
          s.category_id
        FROM transactions t
        JOIN transaction_splits s ON s.transaction_id=t.id
        JOIN categories c ON c.id=s.category_id
        WHERE t.id=?
          AND c.kind='expense'
          AND c.name <> 'Uncategorized'
        """,
        (transaction_id,),
    ).fetchone()
    assert row is not None
    return repo_merchant_knowledge.confirm_category(
        conn,
        descriptor=str(row["descriptor"]),
        category_id=int(row["category_id"]),
        scope=repo_merchant_knowledge.scope_for_transaction(conn, transaction_id),
        operation_key=operation_key or f"test:backlog-category:{transaction_id}",
        actor="test:operator",
        reason="operator confirmed fixture category evidence",
        evidence=Evidence(
            transaction_id=transaction_id,
            transaction_split_id=int(row["split_id"]),
        ),
    )


def _confirm_seed_expenses(db_path: str) -> None:
    """Treat the pre-FN-149 sample ledger as reviewed for isolated batch tests."""
    with engine.write_tx(db_path) as conn:
        rows = conn.execute(
            """
            SELECT status.transaction_id
            FROM v_expense_resolution_status status
            JOIN transactions t ON t.id=status.transaction_id
            WHERE status.resolution_status='unresolved'
              AND status.category_name <> 'Uncategorized'
              AND t.source <> 'opening'
            ORDER BY status.transaction_id
            """
        ).fetchall()
        for row in rows:
            _confirm_transaction_category(
                conn,
                int(row["transaction_id"]),
                operation_key=(
                    f"test:backlog-seed-category:{int(row['transaction_id'])}"
                ),
            )


def _insert_confirmed_evidence_transaction(
    db_path: str,
    *,
    descriptor: str,
    category_id: int,
    external_id: str,
) -> tuple[int, int]:
    with engine.write_tx(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source, external_id, source_confidence, flow_kind
            )
            VALUES (3, '2026-03-03', ?, ?, -900, 'test', ?, 1.0, 'purchase')
            """,
            (descriptor, descriptor, external_id),
        )
        transaction_id = int(cur.lastrowid)
        split_id = int(
            conn.execute(
                """
                INSERT INTO transaction_splits(
                  transaction_id, category_id, amount_cents
                )
                VALUES (?, ?, -900)
                """,
                (transaction_id, category_id),
            ).lastrowid
        )
        claim_id = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=category_id,
            scope=repo_merchant_knowledge.scope_for_transaction(
                conn,
                transaction_id,
            ),
            operation_key=f"test:backlog-knowledge:{external_id}",
            actor="test:operator",
            reason="operator confirmed reusable category evidence",
            evidence=Evidence(
                transaction_id=transaction_id,
                transaction_split_id=split_id,
            ),
        )
        return transaction_id, claim_id


def _neighbor(
    transaction_id: int,
    *,
    merchant: str,
    category: str,
    score: float,
    source: str = "vector",
) -> dict:
    return {
        "transaction_id": transaction_id,
        "posted_on": "2026-03-01",
        "merchant": merchant,
        "description": merchant,
        "amount_cents": -1000,
        "category": category,
        "score": score,
        "source": source,
    }


def _groceries_neighbors() -> list[dict]:
    return [
        _neighbor(3, merchant="Synthetic Market", category="Groceries", score=0.8),
        _neighbor(11, merchant="Synthetic Market", category="Groceries", score=0.7),
        _neighbor(17, merchant="Synthetic Market", category="Groceries", score=0.5),
        _neighbor(4, merchant="Night Owl Cafe", category="Restaurants", score=0.9),
    ]


def _row(db_path: str, txn_id: int):
    with engine.read_conn(db_path) as conn:
        return get_backlog_transaction(conn, txn_id)


def test_suggester_weighted_neighbor_vote_uses_txn_id_and_confidence_math(app_env, monkeypatch):
    expected_txn_id = _insert_backlog_txn(app_env, external_id="backlog:vote")
    neighbors = _groceries_neighbors()
    with engine.write_tx(app_env) as conn:
        for neighbor in neighbors:
            _confirm_transaction_category(
                conn,
                int(neighbor["transaction_id"]),
            )

    def fake_similar(conn, *, txn_id=None, text=None, k=5):
        assert txn_id == expected_txn_id
        assert text is None
        assert k == 8
        return neighbors

    monkeypatch.setattr("app.backlog.suggest.repo_embeddings.similar_transactions", fake_similar)

    with engine.read_conn(app_env) as conn:
        suggestion = suggest_category(conn, _row(app_env, expected_txn_id), k=8)

    assert suggestion.method == "neighbor_vote"
    assert suggestion.suggested_category_name == "Groceries"
    assert suggestion.suggested_category_id == 4
    assert suggestion.confidence == pytest.approx(2.0 / 2.9)
    assert suggestion.vote_breakdown == {"Groceries": pytest.approx(2.0), "Restaurants": pytest.approx(0.9)}
    assert [row["transaction_id"] for row in suggestion.neighbors] == [3, 11, 17, 4]
    assert "Synthetic Market #3" in suggestion.rationale


def test_suggester_trusted_knowledge_and_llm_fallbacks_are_deterministic(
    app_env,
    monkeypatch,
):
    knowledge_txn = _insert_backlog_txn(
        app_env,
        merchant="Exact Mart 123",
        external_id="backlog:knowledge",
    )
    llm_txn = _insert_backlog_txn(
        app_env,
        merchant="Guess Bistro",
        external_id="backlog:llm",
    )
    monkeypatch.setattr(
        "app.backlog.suggest.repo_embeddings.similar_transactions",
        lambda conn, *, txn_id=None, text=None, k=5: [],
    )
    _, knowledge_claim_id = _insert_confirmed_evidence_transaction(
        app_env,
        descriptor="Exact Mart 123",
        category_id=4,
        external_id="backlog:knowledge-evidence",
    )

    with engine.read_conn(app_env) as conn:
        knowledge_suggestion = suggest_category(
            conn,
            get_backlog_transaction(conn, knowledge_txn),
            llm=None,
        )

    assert knowledge_suggestion.method == "trusted_knowledge"
    assert knowledge_suggestion.suggested_category_name == "Groceries"
    assert knowledge_suggestion.confidence == pytest.approx(1.0)
    assert knowledge_suggestion.knowledge_claim_ids == (knowledge_claim_id,)
    assert knowledge_suggestion.knowledge_version

    class FakeStructured:
        def __init__(self, parent):
            self.parent = parent

        def invoke(self, messages):
            self.parent.messages = messages
            return BacklogCategoryGuess(category_name="Restaurants", confidence=0.42)

    class FakeLLM:
        def __init__(self):
            self.schema = None
            self.method = None
            self.messages = None

        def with_structured_output(self, schema, **kwargs):
            self.schema = schema
            self.method = kwargs.get("method")
            return FakeStructured(self)

    fake_llm = FakeLLM()
    with engine.read_conn(app_env) as conn:
        llm_suggestion = suggest_category(conn, get_backlog_transaction(conn, llm_txn), llm=fake_llm)

    assert fake_llm.schema is BacklogCategoryGuess
    assert fake_llm.method == "json_schema"
    assert llm_suggestion.method == "llm_guess"
    assert llm_suggestion.suggested_category_name == "Restaurants"
    assert llm_suggestion.confidence == pytest.approx(0.42)


def test_backlog_suggest_job_enqueues_valid_proposals_and_dedupes(app_env, monkeypatch):
    _confirm_seed_expenses(app_env)
    fresh_txn = _insert_backlog_txn(app_env, merchant="Fresh Market", external_id="backlog:fresh")
    existing_txn = _insert_backlog_txn(app_env, merchant="Already Queued", external_id="backlog:existing")
    neighbors = _groceries_neighbors()
    with engine.write_tx(app_env) as conn:
        for neighbor in neighbors:
            _confirm_transaction_category(
                conn,
                int(neighbor["transaction_id"]),
            )

    monkeypatch.setattr(
        "app.backlog.suggest.repo_embeddings.similar_transactions",
        lambda conn, *, txn_id=None, text=None, k=5: neighbors,
    )
    with engine.write_tx(app_env) as conn:
        repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": existing_txn, "to_category_id": 5},
            evidence={"transaction_ids": [existing_txn], "category_ids": [5]},
            confidence=0.7,
            rationale="already queued",
            agent_run_id="manual:test",
        )

    summary = run_batch(app_env, batch="unit", limit=10, llm=None)

    assert summary["scanned"] == 2
    assert summary["suggested"] == 1
    assert summary["skipped_existing"] == 1
    assert summary["no_suggestion"] == 0
    assert summary["errors"] == 0

    with engine.read_conn(app_env) as conn:
        proposals = [
            proposal
            for proposal in repo_actions.list_proposals(
                conn,
                statuses=("proposed", "needs_evidence", "snoozed"),
            )
            if proposal["agent_run_id"] == "backlog:unit"
        ]
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal["payload"] == {"transaction_id": fresh_txn, "to_category_id": 4}
        assert proposal["evidence"]["similar_transaction_ids"] == [3, 11, 17, 4]
        assert [row["transaction_id"] for row in proposal["evidence"]["neighbors"]] == [3, 11, 17, 4]
        RecategorizationHandler().validate(conn, proposal["payload"])


def test_backlog_suggest_job_does_not_block_unrelated_writes_during_llm_fallback(
    app_env,
    monkeypatch,
):
    _confirm_seed_expenses(app_env)
    txn_id = _insert_backlog_txn(
        app_env,
        merchant="No Match Cafe",
        external_id="backlog:llm-lock",
    )
    monkeypatch.setattr(
        "app.backlog.suggest.repo_embeddings.similar_transactions",
        lambda conn, *, txn_id=None, text=None, k=5: [],
    )
    entered = threading.Event()
    release = threading.Event()
    batch_done = threading.Event()
    write_done = threading.Event()
    batch_errors: list[BaseException] = []
    write_errors: list[BaseException] = []
    result: dict[str, dict[str, int]] = {}

    class BlockingStructured:
        def invoke(self, messages):
            entered.set()
            assert release.wait(timeout=5.0)
            return BacklogCategoryGuess(category_name="Restaurants", confidence=0.5)

    class BlockingLLM:
        def with_structured_output(self, schema, **kwargs):
            assert schema is BacklogCategoryGuess
            return BlockingStructured()

    def run_job() -> None:
        try:
            result["summary"] = run_batch(app_env, batch="llm-lock", limit=1, llm=BlockingLLM())
        except BaseException as exc:  # noqa: BLE001 - surfaced after cleanup
            batch_errors.append(exc)
        finally:
            batch_done.set()

    def unrelated_write() -> None:
        try:
            with engine.write_tx(app_env) as conn:
                conn.execute("UPDATE accounts SET name=name WHERE id=1")
        except BaseException as exc:  # noqa: BLE001 - surfaced after cleanup
            write_errors.append(exc)
        finally:
            write_done.set()

    batch_thread = threading.Thread(target=run_job, daemon=True)
    batch_thread.start()
    entered_before_timeout = entered.wait(timeout=2.0)
    if not entered_before_timeout:
        release.set()
        batch_thread.join(timeout=3.0)
    assert entered_before_timeout, batch_errors

    write_thread = threading.Thread(target=unrelated_write, daemon=True)
    write_thread.start()
    completed_during_llm = write_done.wait(timeout=1.0)
    batch_still_blocked = not batch_done.is_set()
    release.set()
    batch_thread.join(timeout=3.0)
    write_thread.join(timeout=3.0)

    assert completed_during_llm, "unrelated write_tx was blocked by the batch LLM fallback"
    assert batch_still_blocked
    assert batch_errors == []
    assert write_errors == []
    assert result["summary"] == {
        "suggested": 1,
        "skipped_existing": 0,
        "no_suggestion": 0,
        "scanned": 1,
        "errors": 0,
    }

    with engine.read_conn(app_env) as conn:
        proposals = repo_actions.list_proposals(conn, statuses=("proposed",))
    assert [
        proposal["payload"]
        for proposal in proposals
        if proposal["agent_run_id"] == "backlog:llm-lock"
    ] == [{"transaction_id": txn_id, "to_category_id": 5}]


def test_backlog_page_renders_seeded_backlog_and_suggestion(app_env):
    txn_id = _insert_backlog_txn(app_env, merchant="Render Market", external_id="backlog:render")
    with engine.write_tx(app_env) as conn:
        repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": txn_id, "to_category_id": 4},
            evidence={
                "transaction_ids": [txn_id],
                "category_ids": [4],
                "similar_transaction_ids": [3],
                "neighbors": [_neighbor(3, merchant="Synthetic Market", category="Groceries", score=0.8)],
                "method": "neighbor_vote",
            },
            confidence=0.8,
            rationale="Market neighbors are groceries",
            agent_run_id="backlog:render",
        )

    response = _client().get("/backlog")

    assert response.status_code == 200
    assert "Uncategorized" in response.text
    assert "Render Market" in response.text
    assert "Groceries" in response.text
    assert f'action="/actions/' in response.text
    assert "Synthetic Market" in response.text


def test_backlog_keeps_cleared_and_assigned_but_unconfirmed_rows_actionable(
    app_env,
):
    cleared_id = _insert_backlog_txn(
        app_env,
        merchant="Cleared Mystery",
        external_id="backlog:cleared-unresolved",
    )
    assigned_id = _insert_backlog_txn(
        app_env,
        merchant="Assigned Without Evidence",
        external_id="backlog:assigned-unconfirmed",
    )
    with engine.write_tx(app_env) as conn:
        repo_statements.mark_cleared(conn, cleared_id, "2026-04-03")
        groceries = repo_ledger.find_category_by_name(conn, "Groceries")
        conn.execute(
            """
            UPDATE transaction_splits
            SET category_id=?
            WHERE transaction_id=?
            """,
            (int(groceries["id"]), assigned_id),
        )

    response = _client().get("/backlog")

    assert response.status_code == 200
    assert "Confirm expense categories" in response.text
    assert f'id="txn-{cleared_id}"' in response.text
    assert "Cleared Mystery" in response.text
    assert "reconciled" in response.text
    assert f'id="txn-{assigned_id}"' in response.text
    assert "Assigned Without Evidence" in response.text
    assert "current category: Groceries" in response.text


def test_backlog_shows_multi_split_blocker_but_disables_batch_approval(app_env):
    with engine.write_tx(app_env) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)
        groceries = repo_ledger.find_category_by_name(conn, "Groceries")
        transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, counterparty, amount_cents,
                  source, external_id, source_confidence, flow_kind
                )
                VALUES (
                  3, '2026-04-04', 'mixed market purchase', 'Split Market',
                  -3000, 'test', 'backlog:multi-split', 1.0, 'purchase'
                )
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO transaction_splits(
              transaction_id, category_id, amount_cents, memo
            )
            VALUES (?, ?, -1000, 'unknown line'), (?, ?, -2000, 'grocery line')
            """,
            (
                transaction_id,
                uncategorized_id,
                transaction_id,
                int(groceries["id"]),
            ),
        )

    with engine.read_conn(app_env) as conn:
        rows = [
            dict(row)
            for row in list_uncategorized_expense_backlog(conn, limit=None)
            if int(row["transaction_id"]) == transaction_id
        ]
        mutation_row = get_backlog_transaction(conn, transaction_id)

    assert len(rows) == 2
    assert {row["resolution_status"] for row in rows} == {"unresolved"}
    assert {row["mutation_supported"] for row in rows} == {0}
    assert mutation_row is None

    response = _client().get("/backlog")

    assert response.status_code == 200
    assert f'id="txn-{transaction_id}"' in response.text
    assert 'data-mutation-supported="false"' in response.text
    assert (
        f'aria-label="Batch approval unavailable for multi-split transaction '
        f'{transaction_id}"'
    ) in response.text
    assert f'href="/txn/{transaction_id}/edit">review splits</a>' in response.text


def test_backlog_approval_seeds_classification_label_and_drops_uncategorized_rate(
    app_env,
    monkeypatch,
):
    _confirm_seed_expenses(app_env)
    txn_id = _insert_backlog_txn(app_env, merchant="Seed Market", external_id="backlog:label")
    with engine.write_tx(app_env) as conn:
        for neighbor in _groceries_neighbors():
            _confirm_transaction_category(
                conn,
                int(neighbor["transaction_id"]),
            )
    monkeypatch.setattr(
        "app.backlog.suggest.repo_embeddings.similar_transactions",
        lambda conn, *, txn_id=None, text=None, k=5: _groceries_neighbors(),
    )
    with engine.read_conn(app_env) as conn:
        before = expense_uncategorized_metrics(conn)

    summary = run_batch(app_env, batch="labels", limit=10, llm=None)
    assert summary["suggested"] == 1

    with engine.read_conn(app_env) as conn:
        proposal = [
            proposal
            for proposal in repo_actions.list_proposals(conn, statuses=("proposed",))
            if proposal["payload"].get("transaction_id") == txn_id
        ][0]

    response = _client().post(f"/actions/{proposal['id']}/approve", follow_redirects=False)

    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            """
            SELECT c.name
            FROM transaction_splits s
            JOIN categories c ON c.id = s.category_id
            WHERE s.transaction_id=?
            """,
            (txn_id,),
        ).fetchone()
        labels = repo_labels.list_labels(conn)
        after = expense_uncategorized_metrics(conn)

    assert split["name"] == "Groceries"
    assert len(labels) == 1
    assert labels[0]["transaction_id"] == txn_id
    assert labels[0]["category_name"] == "Groceries"
    assert labels[0]["neighbor_ids"] == [3, 11, 17, 4]
    assert labels[0]["source"] == "backlog:labels"
    assert after["uncategorized_count"] == before["uncategorized_count"] - 1
    assert after["uncategorized_rate"] < before["uncategorized_rate"]


def test_recategorization_label_recorrection_replaces_previous_category(app_env):
    client = _client()
    txn_id = _insert_backlog_txn(app_env, external_id="backlog:recorrect-label")

    first_id = _enqueue_recategorization(
        app_env,
        transaction_id=txn_id,
        to_category_id=4,
        agent_run_id="backlog:label-a",
    )
    assert client.post(f"/actions/{first_id}/approve", follow_redirects=False).status_code == 303
    second_id = _enqueue_recategorization(
        app_env,
        transaction_id=txn_id,
        to_category_id=5,
        agent_run_id="backlog:label-b",
    )
    assert client.post(f"/actions/{second_id}/approve", follow_redirects=False).status_code == 303

    with engine.read_conn(app_env) as conn:
        labels = [
            label
            for label in repo_labels.list_labels(conn)
            if label["transaction_id"] == txn_id
        ]
        export_rows = repo_labels.export_eval_rows(conn)

    assert len(labels) == 1
    assert labels[0]["category_id"] == 5
    assert labels[0]["category_name"] == "Restaurants"
    assert labels[0]["source"] == "backlog:label-b"
    assert [row["transaction_id"] for row in export_rows].count(txn_id) == 1


def test_revert_recategorization_clears_classification_label(app_env):
    client = _client()
    txn_id = _insert_backlog_txn(app_env, external_id="backlog:revert-label")
    proposal_id = _enqueue_recategorization(
        app_env,
        transaction_id=txn_id,
        to_category_id=4,
        agent_run_id="backlog:label-revert",
    )

    assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        assert [
            label
            for label in repo_labels.list_labels(conn)
            if label["transaction_id"] == txn_id
        ]

    assert client.post(f"/actions/{proposal_id}/revert", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        labels = [
            label
            for label in repo_labels.list_labels(conn)
            if label["transaction_id"] == txn_id
        ]

    assert labels == []


def test_export_eval_rows_has_unique_transaction_ids_after_multiple_corrections(app_env):
    client = _client()
    first_txn = _insert_backlog_txn(app_env, external_id="backlog:export-label-a")
    second_txn = _insert_backlog_txn(app_env, external_id="backlog:export-label-b")

    for category_id in (4, 5, 9):
        proposal_id = _enqueue_recategorization(
            app_env,
            transaction_id=first_txn,
            to_category_id=category_id,
            agent_run_id=f"backlog:export-a-{category_id}",
        )
        assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    for category_id in (5, 6):
        proposal_id = _enqueue_recategorization(
            app_env,
            transaction_id=second_txn,
            to_category_id=category_id,
            agent_run_id=f"backlog:export-b-{category_id}",
        )
        assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303

    with engine.read_conn(app_env) as conn:
        export_rows = repo_labels.export_eval_rows(conn)

    transaction_ids = [row["transaction_id"] for row in export_rows]
    assert len(transaction_ids) == len(set(transaction_ids))
    assert {
        row["transaction_id"]: row["expected_category"]
        for row in export_rows
        if row["transaction_id"] in {first_txn, second_txn}
    } == {
        first_txn: "Utilities",
        second_txn: "Subscriptions",
    }
