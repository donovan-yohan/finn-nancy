"""Resolve one statement run and gather deterministic row-id evidence."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .schemas import StatementRun
from .tools import (
    IdAllowlist,
    PlanningAnalystResult,
    ReceiptMatcherResult,
    RecurringAnalystResult,
    StatementAuditResult,
    planning_analyst_tool,
    receipt_matcher_tool,
    recurring_analyst_tool,
    statement_auditor_tool,
)


@dataclass(frozen=True)
class EvidenceBundle:
    statement_audit: StatementAuditResult
    receipt_matches: ReceiptMatcherResult
    recurring: RecurringAnalystResult
    planning: PlanningAnalystResult

    @property
    def allowed_ids(self) -> IdAllowlist:
        return (
            self.statement_audit.allowed_ids.merge(self.receipt_matches.allowed_ids)
            .merge(self.recurring.allowed_ids)
            .merge(self.planning.allowed_ids)
        )


def resolve_statement_run(
    conn: sqlite3.Connection,
    *,
    source_document_id: int | None = None,
    month: str | None = None,
) -> StatementRun:
    if source_document_id is None and month is None:
        raise ValueError("source_document_id or month is required")

    doc_name: str | None = None
    doc_months: list[str] = []
    if source_document_id is not None:
        doc = conn.execute(
            """
            SELECT document_name
            FROM v_statement_coverage_by_doc
            WHERE source_document_id = ?
            """,
            (source_document_id,),
        ).fetchone()
        if doc is None:
            raise LookupError(f"source_document_id {source_document_id} has no coverage rows")
        doc_name = doc["document_name"]
        doc_months = [
            row["month"]
            for row in conn.execute(
                """
                SELECT month
                FROM v_statement_coverage_lines
                WHERE source_document_id = ?
                GROUP BY month
                ORDER BY month
                """,
                (source_document_id,),
            ).fetchall()
        ]
        if not doc_months:
            raise LookupError(f"source_document_id {source_document_id} has no statement lines")
        if month is not None and month not in doc_months:
            raise LookupError(f"source_document_id {source_document_id} has no statement lines in {month}")

    resolved_month = month
    run_months: list[str]
    if source_document_id is not None:
        run_months = doc_months
        resolved_month = month or doc_months[-1]
    elif resolved_month is None:
        row = conn.execute(
            """
            SELECT month
            FROM v_statement_coverage_lines
            GROUP BY month
            ORDER BY COUNT(*) DESC, month DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise LookupError("no statement lines")
        resolved_month = row["month"]
        run_months = [resolved_month]
    else:
        run_months = [resolved_month]

    if source_document_id is not None:
        where = "source_document_id = ?"
        args: list[object] = [source_document_id]
    else:
        where = "month = ?"
        args = [resolved_month]
    rows = conn.execute(
        f"""
        SELECT DISTINCT account_id
        FROM v_statement_coverage_lines
        WHERE {where}
          AND account_id IS NOT NULL
        ORDER BY account_id
        """,
        args,
    ).fetchall()
    account_ids = [int(row["account_id"]) for row in rows]

    return StatementRun(
        source_document_id=source_document_id,
        month=resolved_month,
        months=run_months,
        account_ids=account_ids,
        document_name=doc_name,
    )


def gather_all_evidence(conn: sqlite3.Connection, run: StatementRun) -> EvidenceBundle:
    """Fetch every deterministic evidence source once for tests or diagnostics."""
    return EvidenceBundle(
        statement_audit=statement_auditor_tool(conn, run),
        receipt_matches=receipt_matcher_tool(conn, run),
        recurring=recurring_analyst_tool(conn, run),
        planning=planning_analyst_tool(conn, run),
    )
