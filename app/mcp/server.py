from __future__ import annotations

import base64

from mcp.server.fastmcp import FastMCP

from ..accounting.contract import FlowKind
from ..api import finance, service


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


def search_transactions(q: str | None = None, account_id: int | None = None,
                        category_id: int | None = None, date_from: str | None = None,
                        date_to: str | None = None, flow_kind: str | None = None,
                        review: str | None = None, cursor: str | None = None, limit: int = 50) -> dict:
    """Read bounded canonical transactions. Unknown meaning remains explicit."""
    return finance.search_transactions(q=q, account_id=account_id, category_id=category_id,
        date_from=date_from, date_to=date_to, flow_kind=flow_kind, review=review, cursor=cursor, limit=limit)


def inspect_transaction(transaction_id: int) -> dict:
    """Read exact splits, canonical source evidence, flow resolution and state digest."""
    return finance.transaction_detail(transaction_id)


def list_categories() -> dict:
    """List existing household categories; no category creation."""
    return finance.categories()


def inspect_proposal(proposal_id: int) -> dict:
    """Read proposal and immutable human-decision audit."""
    return finance.proposal_detail(proposal_id)


def period_statement(month: str) -> dict:
    """Read the shared evidence-backed period statement."""
    return service.get_period_statement(month=month)


def propose_category(operation_key: str, transaction_id: int, split_id: int, to_category_id: int,
                     expected_state_digest: str, rationale: str, confidence: float | None = None,
                     evidence_refs: list[str] | None = None) -> dict:
    """Queue one split category proposal for human approval; never apply or undo."""
    return finance.propose_category(operation_key=operation_key, transaction_id=transaction_id,
        split_id=split_id, to_category_id=to_category_id, expected_state_digest=expected_state_digest,
        rationale=rationale, confidence=confidence, evidence_refs=evidence_refs)


def build_server(*, read_only: bool = False, proposals: bool = False) -> FastMCP:
    if read_only and proposals:
        raise ValueError("read_only and proposals are incompatible")
    server = FastMCP("finn-nancy")
    if not read_only and not proposals:
        server.tool()(ingest_document)
        server.tool()(add_transaction)
    server.tool()(query_finances)
    server.tool()(reconcile_status)
    for tool in (search_transactions, inspect_transaction, list_categories, inspect_proposal, period_statement):
        server.tool()(tool)
    if proposals:
        server.tool()(propose_category)
    return server


def main(*, read_only: bool = False, proposals: bool = False) -> None:
    build_server(read_only=read_only, proposals=proposals).run()
