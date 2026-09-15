from __future__ import annotations

import base64

from mcp.server.fastmcp import FastMCP

from ..accounting.contract import FlowKind
from ..api import service


def ingest_document(filename: str, content_base64: str, declared_mime: str | None = None) -> dict:
    """Stage a receipt/statement (base64 bytes) into the ingest pipeline."""
    raw = base64.b64decode(content_base64)
    return service.ingest_bytes(raw=raw, filename=filename, channel="mcp", declared_mime=declared_mime)


def add_transaction(
    amount_cents: int,
    posted_on: str,
    description: str,
    counterparty: str = "",
    category: str | None = None,
    account_id: int | None = None,
    card_last4: str | None = None,
    external_id: str | None = None,
    source: str = "mcp",
    source_confidence: float = 1.0,
    notes: str = "",
    flow_kind: FlowKind = FlowKind.UNKNOWN,
) -> dict:
    """Record a structured transaction (+signed split).

    ``flow_kind`` is independent of category. Pass ``unknown`` to explicitly
    defer ambiguous semantics to review.

    Retries: pass a stable external_id to make the call idempotent; without one
    every call creates a new transaction.
    """
    return service.record_transaction(
        amount_cents=amount_cents,
        posted_on=posted_on,
        description=description,
        counterparty=counterparty,
        category=category,
        account_id=account_id,
        card_last4=card_last4,
        external_id=external_id,
        source=source,
        source_confidence=source_confidence,
        notes=notes,
        flow_kind=flow_kind,
    )


def query_finances(report: str, month: str | None = None, limit: int = 50) -> dict:
    """Read-only parameterized reports over v_* views.

    reports: coverage, budget_vs_actual, recurring_deltas, goals_progress,
    monthly_spend_by_category.
    """
    return service.run_report(report=report, month=month, limit=limit)


def reconcile_status(doc_id: int | None = None) -> dict:
    """Statement/document reconciliation coverage state."""
    return service.reconcile_status(doc_id=doc_id)


def build_server() -> FastMCP:
    server = FastMCP("finn-nancy")
    server.tool()(ingest_document)
    server.tool()(add_transaction)
    server.tool()(query_finances)
    server.tool()(reconcile_status)
    return server


def main() -> None:
    build_server().run()
