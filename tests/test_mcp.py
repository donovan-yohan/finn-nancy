from __future__ import annotations

import asyncio
import base64
import inspect

import pytest

pytest.importorskip("mcp")

from app.db import engine
from app.ingest.schemas import ExtractedReceipt


def _tool_names(server) -> set[str]:
    result = server.list_tools()
    if inspect.isawaitable(result):
        tools = asyncio.run(result)
    else:
        tools = result
    return {tool.name for tool in tools}


async def _drive_worker_until_transaction(app_env, doc_id: int, fake_llm, receipt: ExtractedReceipt):
    from app.workers.runner import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop, llm=fake_llm(receipt), poll_seconds=0.05))
    txn = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        with engine.read_conn(app_env) as conn:
            txn = conn.execute(
                "SELECT * FROM transactions WHERE source_document_id=?",
                (doc_id,),
            ).fetchone()
        if txn is not None:
            break
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    return txn


def test_mcp_registers_expected_tools():
    from app.mcp.server import build_server

    names = _tool_names(build_server())
    assert {"ingest_document", "add_transaction", "query_finances", "reconcile_status"} <= names


def test_mcp_ingest_document_files_receipt_e2e(app_env, make_jpeg, fake_llm):
    from app.mcp.server import ingest_document

    raw = make_jpeg()
    result = ingest_document(
        filename="receipt.jpg",
        content_base64=base64.b64encode(raw).decode("ascii"),
        declared_mime="image/jpeg",
    )
    assert result["status"] == "staged"

    receipt = ExtractedReceipt(
        merchant="MCP Cafe",
        purchased_on="2026-06-13",
        currency="CAD",
        subtotal_cents=2200,
        total_cents=2200,
        category_guess="Restaurants",
        confidence=0.9,
    )
    txn = asyncio.run(_drive_worker_until_transaction(app_env, result["doc_id"], fake_llm, receipt))
    assert txn is not None

    with engine.read_conn(app_env) as conn:
        doc = conn.execute("SELECT * FROM source_documents WHERE id=?", (result["doc_id"],)).fetchone()
        existing = conn.execute(
            "SELECT * FROM transactions WHERE source_document_id=?",
            (result["doc_id"],),
        ).fetchone()
    assert doc["status"] == "processed"
    assert existing is not None and existing["amount_cents"] == -2200


def test_mcp_add_transaction_is_idempotent(app_env):
    from app.mcp.server import add_transaction

    first = add_transaction(
        amount_cents=-3400,
        posted_on="2026-06-14",
        description="MCP Structured Dinner",
        counterparty="MCP Bistro",
        category="Restaurants",
        external_id="mcp-test-dedupe-1",
    )
    second = add_transaction(
        amount_cents=-3400,
        posted_on="2026-06-14",
        description="MCP Structured Dinner",
        counterparty="MCP Bistro",
        category="Restaurants",
        external_id="mcp-test-dedupe-1",
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["transaction_id"] == first["transaction_id"]


def test_mcp_add_transaction_without_external_id_creates_distinct_rows(app_env):
    from app.mcp.server import add_transaction

    first = add_transaction(
        amount_cents=-3400,
        posted_on="2026-06-14",
        description="MCP Structured Dinner",
        counterparty="MCP Bistro",
        category="Restaurants",
    )
    second = add_transaction(
        amount_cents=-3400,
        posted_on="2026-06-14",
        description="MCP Structured Dinner",
        counterparty="MCP Bistro",
        category="Restaurants",
    )
    assert first["created"] is True
    assert second["created"] is True
    assert second["transaction_id"] != first["transaction_id"]
    assert first["external_id"].startswith("api:")
    assert second["external_id"].startswith("api:")
    assert second["external_id"] != first["external_id"]

    ids = {first["transaction_id"], second["transaction_id"]}
    with engine.read_conn(app_env) as conn:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE id IN (?, ?)",
            (first["transaction_id"], second["transaction_id"]),
        ).fetchall()
    assert {int(row["id"]) for row in rows} == ids


def test_mcp_add_transaction_unknown_account_raises(app_env):
    from app.mcp.server import add_transaction

    with pytest.raises(ValueError, match="unknown account_id: 5003"):
        add_transaction(
            amount_cents=-3400,
            posted_on="2026-06-14",
            description="MCP Unknown Account",
            counterparty="MCP Bistro",
            category="Restaurants",
            account_id=5003,
            external_id="mcp-test-unknown-account",
        )


def test_mcp_query_finances_catalog(app_env):
    from app.mcp.server import query_finances

    for report in (
        "coverage",
        "budget_vs_actual",
        "recurring_deltas",
        "goals_progress",
        "monthly_spend_by_category",
    ):
        result = query_finances(report=report)
        assert result["report"] == report
        assert isinstance(result["rows"], list)
        assert "semantic_completeness" in result


def test_mcp_monthly_report_discloses_unknown_rows_excluded_from_totals(app_env):
    from app.mcp.server import add_transaction, query_finances

    created = add_transaction(
        amount_cents=-1299,
        posted_on="2026-06-21",
        description="VAGUE BANK ENTRY",
        category="Restaurants",
        external_id="mcp-report-unknown",
    )
    assert created["flow_kind"] == "unknown"

    report = query_finances(report="monthly_spend_by_category", month="2026-06")
    completeness = report["semantic_completeness"]
    assert completeness["complete"] is False
    assert completeness["excluded_unknown_count"] == 1
    assert completeness["transaction_count"] >= 1
    assert "excluded from financial totals" in completeness["notice"]


def test_mcp_reconcile_status_overall_and_document(app_env, make_jpeg):
    from app.mcp.server import ingest_document, reconcile_status

    cap = ingest_document(
        filename="receipt.jpg",
        content_base64=base64.b64encode(make_jpeg()).decode("ascii"),
        declared_mime="image/jpeg",
    )
    overall = reconcile_status()
    document = reconcile_status(doc_id=cap["doc_id"])
    assert isinstance(overall, dict)
    assert isinstance(document, dict)
    assert document["doc_id"] == cap["doc_id"]
