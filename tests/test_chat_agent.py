from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

from app.agents.chat.agent import load_thread_messages, stream_chat
from app.agents.chat.tools import (
    explain_categorization,
    get_recurring_insights,
    propose_recategorization,
    query_finances,
    read_only_conn,
)
from app.db import (
    engine,
    repo_actions,
    repo_ledger,
    repo_merchant_knowledge,
    repo_statements,
)
from app.db.repo_merchant_knowledge import Evidence
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.reconcile import apply as reconcile_apply
from app.reconcile.engine import reconcile_document


class ScriptedChat:
    def __init__(self, responses: list[AIMessage] | None = None, *, repeat_tool: bool = False):
        self.responses = list(responses or [])
        self.repeat_tool = repeat_tool
        self.calls = 0
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.repeat_tool:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_finances",
                        "args": {"query": "monthly_cashflow", "params": {"limit": 1}},
                        "id": f"call-{self.calls}",
                    }
                ],
            )
        if not self.responses:
            raise AssertionError("no scripted response left")
        return self.responses.pop(0)


class ProposeThenNarrateChat:
    def __init__(self, *, transaction_id: int, category_name: str = "Restaurants"):
        self.transaction_id = transaction_id
        self.category_name = category_name
        self.calls = 0
        self.bound_tools = []
        self.tool_result: dict[str, Any] | None = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "propose_recategorization",
                        "args": {
                            "transaction_id": self.transaction_id,
                            "category_name": self.category_name,
                            "rationale": "User asked to move this category.",
                        },
                        "id": f"call-propose-{self.transaction_id}",
                    }
                ],
            )

        tool_messages = [message for message in messages if getattr(message, "type", "") == "tool"]
        assert tool_messages
        self.tool_result = json.loads(tool_messages[-1].content)
        return AIMessage(content=f"Could not queue recategorization: {self.tool_result['error']}")


class DelayedEchoChat:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        await asyncio.sleep(0.05)
        user_text = [getattr(message, "content", "") for message in messages if getattr(message, "type", "") == "human"][-1]
        return AIMessage(content=f"answer: {user_text}")


class StreamingToolThenAnswerChat(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    streaming: bool = True
    calls: int = 0
    bound_tools: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "streaming-tool-then-answer"

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="unused"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Let me check "))
            yield ChatGenerationChunk(message=AIMessageChunk(content="March spending."))
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_calls=[
                        {
                            "name": "query_finances",
                            "args": {"query": "spend_by_category", "params": {"month": "2026-03"}},
                            "id": "call-preamble-tool",
                        }
                    ],
                )
            )
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content="March groceries "))
        yield ChatGenerationChunk(message=AIMessageChunk(content="were $158.70."))


async def _events_async(db_path: str, llm: Any, message: str = "question", thread_id: str = "thread-test"):
    out = []
    async for event in stream_chat(db_path, thread_id, message, llm=llm, warm_after_s=5.0):
        out.append(event)
    return out


def _events(db_path: str, llm: Any, message: str = "question", thread_id: str = "thread-test"):
    return asyncio.run(_events_async(db_path, llm, message, thread_id))


def _category_id(conn, name: str) -> int:
    row = repo_ledger.find_category_by_name(conn, name)
    assert row is not None
    return int(row["id"])


def _confirm_transaction_category(
    db_path: str,
    transaction_id: int,
    *,
    operation_key: str,
) -> int:
    with engine.write_tx(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(NULLIF(t.counterparty, ''), t.description) AS descriptor,
              split.id AS split_id,
              split.category_id
            FROM transactions t
            JOIN transaction_splits split ON split.transaction_id=t.id
            WHERE t.id=?
            """,
            (transaction_id,),
        ).fetchone()
        assert row is not None
        return repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=str(row["descriptor"]),
            category_id=int(row["category_id"]),
            scope=repo_merchant_knowledge.scope_for_transaction(
                conn,
                transaction_id,
            ),
            operation_key=operation_key,
            actor="test:operator",
            reason="operator confirmed chat fixture category",
            evidence=Evidence(
                transaction_id=transaction_id,
                transaction_split_id=int(row["split_id"]),
            ),
        )


def _statement_doc(conn, ref: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
            VALUES ('statement', ?, ?, ?, 'application/pdf', 'processed')
            """,
            (f"{ref}.pdf", f"{ref}.pdf", f"sha-{ref}"),
        ).lastrowid
    )


def _stage_statement(conn, *, doc_id: int, description: str, posted_on: str, amount_cents: int) -> None:
    repo_statements.stage_lines(
        conn,
        source_document_id=doc_id,
        account_id=1,
        parsed=ExtractedStatement(
            institution="Test Bank",
            currency="CAD",
            statement_period=posted_on[:7],
            rows=[
                StatementRow(
                    posted_on=posted_on,
                    description=description,
                    amount_cents=amount_cents,
                )
            ],
        ),
    )


def _promote_statement_line(
    db_path: str,
    *,
    ref: str,
    description: str,
    posted_on: str,
    amount_cents: int,
) -> int:
    with engine.write_tx(db_path) as conn:
        doc_id = _statement_doc(conn, ref)
        _stage_statement(
            conn,
            doc_id=doc_id,
            description=description,
            posted_on=posted_on,
            amount_cents=amount_cents,
        )

    result = reconcile_document(db_path, doc_id, llm=None)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.write_tx(db_path) as conn:
        lines = repo_statements.lines_for_document(conn, doc_id)
        assert len(lines) == 1
        transaction_id = reconcile_apply.promote_line(conn, int(lines[0]["id"]))
        assert transaction_id is not None

    with engine.read_conn(db_path) as conn:
        line = repo_statements.lines_for_document(conn, doc_id)[0]
        assert line["match_status"] == "promoted"
        assert int(line["matched_transaction_id"]) == transaction_id
        return int(line["matched_transaction_id"])


def _insert_opening_balance_transaction(db_path: str) -> int:
    with engine.write_tx(db_path) as conn:
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=1,
            posted_on="2026-06-01",
            description="opening balance",
            counterparty="Opening Balance",
            amount_cents=50000,
            source="opening",
            external_id="chat-opening-balance",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="opening",
        )
        assert txn_id is not None
        repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=_category_id(conn, "Savings transfer"),
            amount_cents=50000,
        )
        return txn_id


def _insert_multi_split_transaction(db_path: str) -> int:
    with engine.write_tx(db_path) as conn:
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=3,
            posted_on="2026-06-02",
            description="split purchase",
            counterparty="Split Merchant",
            amount_cents=-4200,
            source="sample",
            external_id="chat-multi-split",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert txn_id is not None
        repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=_category_id(conn, "Groceries"),
            amount_cents=-3000,
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=_category_id(conn, "Utilities"),
            amount_cents=-1200,
        )
        return txn_id


def test_chat_graph_executes_tools_and_final_answer(app_env):
    _confirm_transaction_category(
        app_env,
        17,
        operation_key="test:chat-graph-category:17",
    )
    llm = ScriptedChat(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_finances",
                        "args": {"query": "spend_by_category", "params": {"month": "2026-03"}},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="Groceries were $158.70 in March from transaction #17."),
        ]
    )

    events = _events(app_env, llm, "How much did groceries cost in March?")

    assert [event["type"] for event in events] == ["step_start", "step_end", "token", "done"]
    assert events[0]["tool"] == "query_finances"
    assert "Groceries" in events[1]["output"]
    assert events[-1]["message"] == "Groceries were $158.70 in March from transaction #17."
    assert llm.calls == 2
    assert {tool.name for tool in llm.bound_tools} >= {"query_finances", "search_history"}


def test_concurrent_turns_same_thread_are_serialized(app_env):
    async def run() -> list[dict[str, Any]]:
        llm = DelayedEchoChat()

        async def collect(message: str) -> list[dict[str, Any]]:
            return await _events_async(app_env, llm, message, thread_id="shared-concurrent-thread")

        first, second = await asyncio.gather(collect("first question"), collect("second question"))
        messages = await load_thread_messages(app_env, "shared-concurrent-thread")
        assert first[-1] == {"type": "done", "message": "answer: first question"}
        assert second[-1] == {"type": "done", "message": "answer: second question"}
        return messages

    rendered = asyncio.run(run())
    transcript = [(message["role"], message["content"]) for message in rendered]

    assert ("user", "first question") in transcript
    assert ("assistant", "answer: first question") in transcript
    assert ("user", "second question") in transcript
    assert ("assistant", "answer: second question") in transcript


def test_streaming_tool_preamble_is_reset_and_done_is_authoritative(app_env):
    llm = StreamingToolThenAnswerChat()

    events = _events(app_env, llm, "How did March groceries look?", thread_id="stream-reset-thread")

    types = [event["type"] for event in events]
    reset_index = types.index("token_reset")
    assert reset_index > 0
    assert any(event["type"] == "token" and "Let me check" in event["text"] for event in events[:reset_index])
    assert events[-1] == {"type": "done", "message": "March groceries were $158.70."}

    visible_text = ""
    for event in events:
        if event["type"] == "token":
            visible_text += event["text"]
        elif event["type"] == "token_reset":
            visible_text = ""
        elif event["type"] == "done":
            visible_text = event["message"]

    assert visible_text == "March groceries were $158.70."


def test_parallel_same_tool_calls_keep_outputs_matched_by_run_id(app_env):
    for iteration in range(6):
        llm = ScriptedChat(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "query_finances",
                            "args": {"query": "spend_by_category", "params": {"month": "2099-01"}},
                            "id": f"call-spend-{iteration}",
                        },
                        {
                            "name": "query_finances",
                            "args": {"query": "top_merchants", "params": {"month": "2099-01"}},
                            "id": f"call-merchants-{iteration}",
                        },
                    ],
                ),
                AIMessage(content="done"),
            ]
        )

        events = _events(app_env, llm, "parallel calls", thread_id=f"parallel-tool-thread-{iteration}")
        starts = [event for event in events if event["type"] == "step_start"]
        ends = [event for event in events if event["type"] == "step_end"]
        started_by_id = {event["id"]: event["input"]["query"] for event in starts}
        ended_by_id = {event["id"]: json.loads(event["output"])["query"] for event in ends}

        assert len(starts) == 2
        assert len(set(started_by_id)) == 2
        assert all(event["id"] for event in starts + ends)
        assert ended_by_id == started_by_id


def test_chat_graph_stops_at_recursion_limit(app_env):
    events = _events(app_env, ScriptedChat(repeat_tool=True), "loop")

    assert events[-1]["type"] == "error"
    assert "tool-step limit" in events[-1]["message"]


def test_chat_tool_connections_are_read_only(sample_db):
    with read_only_conn(sample_db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_fail(id INTEGER)")


def test_query_finances_rejects_unknown_query(sample_db):
    result = query_finances(sample_db, "drop_tables", month="2026-03")

    assert result["ok"] is False
    assert "Unknown finance query" in result["error"]


def test_query_finances_catalog_returns_rows(sample_db):
    _confirm_transaction_category(
        sample_db,
        17,
        operation_key="test:chat-catalog-category:17",
    )
    cases = [
        ("monthly_cashflow", {}),
        ("spend_by_category", {"month": "2026-03"}),
        ("category_trend", {"category": "Groceries"}),
        ("budget_vs_actual", {"month": "2026-03"}),
        ("top_merchants", {"month": "2026-03"}),
        ("recent_transactions", {"merchant": "Market"}),
        ("leisure_vs_bigticket", {}),
        ("runway", {}),
    ]

    for name, params in cases:
        result = query_finances(sample_db, name, **params)
        assert result["ok"] is True, name
        assert result["rows"], name


def test_explain_categorization_separates_accepted_merchant_and_category_claims(
    app_env,
):
    with engine.write_tx(app_env) as conn:
        split = conn.execute(
            """
            SELECT id, category_id
            FROM transaction_splits
            WHERE transaction_id=3
            """
        ).fetchone()
        scope = repo_merchant_knowledge.scope_for_transaction(conn, 3)
        merchant_claim_id = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor="Synthetic Market",
            canonical_name="Synthetic Market",
            scope=scope,
            operation_key="test:chat-market-basket-merchant",
            actor="test:operator",
            reason="operator confirmed canonical merchant",
            evidence=Evidence(transaction_id=3),
        )
        category_claim_id = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="Synthetic Market",
            category_id=int(split["category_id"]),
            scope=scope,
            operation_key="test:chat-market-basket-category",
            actor="test:operator",
            reason="operator confirmed expense category",
            evidence=Evidence(
                transaction_id=3,
                transaction_split_id=int(split["id"]),
            ),
        )

    result = explain_categorization(app_env, 3)

    assert result["ok"] is True
    assert result["deciding_signal"]["method"] == "accepted_category_claim"
    assert result["deciding_signal"]["claim_ids"] == [category_claim_id]
    assert result["knowledge_signal"]["matches_current_category"] is True
    assert result["knowledge_signal"]["merchant"]["status"] == "resolved"
    assert result["knowledge_signal"]["merchant"]["claim_ids"] == [
        merchant_claim_id
    ]
    assert result["knowledge_signal"]["category"]["status"] == "resolved"
    assert result["knowledge_signal"]["category"]["claim_ids"] == [
        category_claim_id
    ]
    assert (
        result["knowledge_signal"]["merchant"]["automatic_assignment_allowed"]
        is False
    )
    assert (
        result["knowledge_signal"]["category"]["automatic_assignment_allowed"]
        is False
    )
    assert result["transaction"]["categories"][0]["name"] == "Groceries"
    assert result["similar_transactions"]
    assert all(row["transaction_id"] != 3 for row in result["similar_transactions"])


def test_explain_categorization_guess_only(app_env):
    extracted = {
        "merchant": "Guess Cafe",
        "purchased_on": "2026-06-12",
        "currency": "CAD",
        "total_cents": 2200,
        "category_guess": "Restaurants",
        "confidence": 0.82,
        "line_items": [],
        "unreadable_fields": [],
    }
    with engine.write_tx(app_env) as conn:
        doc_id = conn.execute(
            """
            INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
            VALUES ('receipt', 'guess.jpg', 'guess.jpg', 'guess-sha', 'image/jpeg', 'processed')
            """
        ).lastrowid
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=3,
            posted_on="2026-06-12",
            description="Guess Cafe receipt",
            counterparty="Guess Cafe",
            amount_cents=-2200,
            source="receipt",
            external_id="guess-cafe",
            source_document_id=doc_id,
            source_confidence=0.82,
            flow_kind="purchase",
        )
        assert txn_id is not None
        repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=5, amount_cents=-2200)
        conn.execute(
            """
            INSERT INTO ingest_extractions(
              source_document_id, doc_kind, extracted_json, confidence, external_id,
              proposed_account_id, proposed_category_id, review_status, transaction_id
            ) VALUES (?, 'receipt', ?, 0.82, 'guess-cafe', 3, 5, 'auto', ?)
            """,
            (doc_id, json.dumps(extracted), txn_id),
        )

    result = explain_categorization(app_env, txn_id)

    assert result["ok"] is True
    assert result["deciding_signal"]["method"] == "assigned_without_accepted_evidence"
    assert result["deciding_signal"]["confidence"] == 0.0
    assert result["guess_signal"]["matches_current_category"] is True
    assert result["extraction_signal"]["category_guess"] == "Restaurants"
    assert result["knowledge_signal"]["category"]["status"] == "no_evidence"


def test_explain_categorization_statement_promoted_keeps_trusted_knowledge_advisory(
    app_env,
):
    with engine.write_tx(app_env) as conn:
        groceries_id = _category_id(conn, "Groceries")
        evidence_txn_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=3,
                posted_on="2026-06-18",
                description="FRESHMART #441",
                counterparty="FRESHMART #441",
                amount_cents=-1800,
                source="manual",
                external_id="freshmart-category-evidence",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        evidence_split_id = repo_ledger.insert_split(
            conn,
            transaction_id=evidence_txn_id,
            category_id=groceries_id,
            amount_cents=-1800,
        )
        category_claim_id = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="FRESHMART #441",
            category_id=groceries_id,
            scope=repo_merchant_knowledge.scope_for(conn),
            operation_key="test:chat-freshmart-category",
            actor="test:operator",
            reason="operator confirmed reusable Freshmart category",
            evidence=Evidence(
                transaction_id=evidence_txn_id,
                transaction_split_id=evidence_split_id,
            ),
        )

    txn_id = _promote_statement_line(
        app_env,
        ref="statement-freshmart-alias",
        description="FRESHMART #441",
        posted_on="2026-06-19",
        amount_cents=-2400,
    )

    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?",
            (txn_id,),
        ).fetchone()
        assert split["category_id"] == _category_id(conn, "Uncategorized")

    result = explain_categorization(app_env, txn_id)

    assert result["ok"] is True
    assert result["transaction"]["counterparty"] == ""
    assert result["transaction"]["merchant"] == "FRESHMART #441"
    assert result["deciding_signal"]["method"] == "unresolved_statement_category"
    assert result["knowledge_signal"]["category"]["status"] == "resolved"
    assert result["knowledge_signal"]["category"]["target_name"] == "Groceries"
    assert result["knowledge_signal"]["category"]["claim_ids"] == [
        category_claim_id
    ]
    assert result["knowledge_signal"]["matches_current_category"] is False
    assert (
        result["knowledge_signal"]["category"]["automatic_assignment_allowed"]
        is False
    )
    assert result["statement_signal"]["line"]["match_status"] == "promoted"


def test_explain_categorization_statement_promoted(app_env):
    txn_id = _promote_statement_line(
        app_env,
        ref="statement-uncategorized",
        description="Statement Only Merchant",
        posted_on="2026-06-20",
        amount_cents=-3100,
    )

    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            """
            SELECT c.name
            FROM transaction_splits ts
            JOIN categories c ON c.id = ts.category_id
            WHERE ts.transaction_id=?
            """,
            (txn_id,),
        ).fetchone()
        assert split["name"] == "Uncategorized"

    result = explain_categorization(app_env, txn_id)

    assert result["ok"] is True
    assert result["deciding_signal"]["method"] == "unresolved_statement_category"
    assert result["statement_signal"]["line"]["match_status"] == "promoted"
    assert result["similar_transactions"] == [] or isinstance(result["similar_transactions"], list)


def test_stream_chat_propose_recategorization_returns_clean_guard_errors(app_env):
    cases = [
        (
            "opening",
            _insert_opening_balance_transaction(app_env),
            "opening balance amount/category cannot be edited",
        ),
        (
            "multi_split",
            _insert_multi_split_transaction(app_env),
            "transaction must have exactly one split",
        ),
    ]

    for label, transaction_id, expected_error in cases:
        llm = ProposeThenNarrateChat(transaction_id=transaction_id)
        events = _events(
            app_env,
            llm,
            "Move this transaction to restaurants",
            thread_id=f"guarded-recat-{label}",
        )

        assert [event["type"] for event in events] == ["step_start", "step_end", "token", "done"]
        assert events[0]["tool"] == "propose_recategorization"
        tool_result = json.loads(events[1]["output"])
        assert tool_result["ok"] is False
        assert tool_result["error"] == expected_error
        assert llm.tool_result == tool_result
        assert expected_error in events[-1]["message"]

    with engine.read_conn(app_env) as conn:
        assert repo_actions.list_proposals(conn) == []


def test_propose_recategorization_enqueues_approval(app_env):
    result = propose_recategorization(
        app_env,
        thread_id="thread-abc",
        transaction_id=3,
        category_name="Restaurants",
        rationale="Market meal, not groceries",
    )

    assert result["ok"] is True
    assert result["status"] == "queued for approval"
    with engine.read_conn(app_env) as conn:
        proposals = repo_actions.list_proposals(conn)
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "recategorization"
    assert proposals[0]["payload"] == {"transaction_id": 3, "to_category_id": 5}
    assert proposals[0]["agent_run_id"] == "chat:thread-abc"
    assert proposals[0]["evidence"]["transaction_ids"] == [3]


def test_propose_recategorization_rejects_unknown_category_without_row(app_env):
    result = propose_recategorization(
        app_env,
        thread_id="thread-abc",
        transaction_id=3,
        category_name="Not A Category",
        rationale="try unknown",
    )

    assert result["ok"] is False
    assert "Unknown category" in result["error"]
    with engine.read_conn(app_env) as conn:
        assert repo_actions.list_proposals(conn) == []


def _insert_expense(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} payment",
        counterparty=merchant,
        amount_cents=-abs(amount_cents),
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=-abs(amount_cents),
    )
    repo_statements.mark_cleared(conn, txn_id, posted_on)
    return txn_id


def test_get_recurring_insights_returns_seeded_rows(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Recurring Chat Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Recurring Chat','expense','nancy','#ff9f43')"
        ).lastrowid
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="chat-koodo-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-04",
            merchant="Koodo",
            amount_cents=5210,
            external_id="chat-koodo-jun",
        )

    result = get_recurring_insights(app_env)

    assert result["recurring_payment_deltas"]
    assert result["recurring_payment_deltas"][0]["merchant"] == "Koodo"
    assert result["recurring_payment_deltas"][0]["amount_delta_cents"] == 577


@pytest.mark.llm
def test_live_chat_answers_groceries_with_tool_step(app_env):
    with engine.write_tx(app_env) as conn:
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=3,
            posted_on="2026-06-05",
            description="June groceries",
            counterparty="June Market",
            amount_cents=-3456,
            source="test",
            external_id="live-june-groceries",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert txn_id is not None
        repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=4, amount_cents=-3456)

    events = []
    async def collect():
        async for event in stream_chat(app_env, "live-thread", "How much did I spend on groceries in 2026-06?"):
            events.append(event)

    asyncio.run(collect())

    final = next(event for event in reversed(events) if event["type"] == "done")
    assert any(event["type"] == "step_start" for event in events)
    assert any(char.isdigit() for char in final["message"])
