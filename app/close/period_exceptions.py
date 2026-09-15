"""Deterministic FN-147 close exceptions and immutable snapshot preview.

Clean close is derived from evidence, never requested by a caller.  Every
exception emitted here is typed and deep-linked to the workflow that can
resolve it.  Planning signals (budget variance and goal pace) deliberately do
not decide accounting close truth.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from ..db import repo_close_inbox, repo_statement_expectations
from ..reconcile import assertions
from . import anomaly, checklist, summary


def _item(
    exception_type: str,
    *,
    subject_kind: str,
    subject_id: object,
    reason: str,
    resolution_href: str,
    amount_cents: int = 0,
    affected_ids: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "exception_type": exception_type,
        "subject_kind": subject_kind,
        "subject_id": str(subject_id),
        "reason": reason,
        "resolution_href": resolution_href,
        "amount_cents": int(amount_cents),
        "affected_ids": affected_ids or {},
        "evidence": evidence or {},
    }


def _statement_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in repo_statement_expectations.period_matrix(conn, month):
        requirement = str(row["requirement_state"])
        lifecycle = row["lifecycle_state"]
        if requirement == "unconfigured":
            reason = f"{row['account_name']} has no statement policy for {month}"
        elif requirement == "required" and lifecycle != "reconciled":
            reason = (
                f"{row['account_name']} statement is {lifecycle or 'not received'}; "
                "required evidence must be reconciled"
            )
        else:
            continue
        items.append(
            _item(
                "missing_statement",
                subject_kind="account_period",
                subject_id=f"{row['account_id']}:{month}",
                reason=reason,
                resolution_href=f"/close?month={month}#statement-matrix",
                affected_ids={
                    "account_id": int(row["account_id"]),
                    "period_month": month,
                    "expectation_id": row["id"],
                },
                evidence={
                    "requirement_state": requirement,
                    "lifecycle_state": lifecycle,
                    "document_count": int(row["document_count"]),
                },
            )
        )
    return items


def _unresolved_line_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT
             line.id, line.posted_on, line.amount_cents, line.match_status,
             line.match_method, line.review_revision, line.source_anchor_id,
             COALESCE(
               expectation.period_month,
               review.period_month,
               NULLIF(line.statement_period, ''),
               substr(line.posted_on, 1, 7)
             ) AS effective_month
           FROM statement_lines line
           LEFT JOIN statement_reviews review
             ON review.source_document_id=line.source_document_id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=line.source_document_id
            AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE line.review_disposition='active'
             AND COALESCE(
               expectation.period_month,
               review.period_month,
               NULLIF(line.statement_period, ''),
               substr(line.posted_on, 1, 7)
             )=?
             AND (
               line.is_pending=1
               OR line.match_status NOT IN ('matched','promoted','ignored')
             )
           ORDER BY line.id""",
        (month,),
    ).fetchall()
    return [
        _item(
            "unresolved_line",
            subject_kind="statement_line",
            subject_id=row["id"],
            reason=(
                f"statement line {row['id']} is {row['match_status']} and cannot "
                "support a closed ledger"
            ),
            resolution_href=f"/recon?month={month}#line-{row['id']}",
            amount_cents=int(row["amount_cents"]),
            affected_ids={
                "statement_line_id": int(row["id"]),
                "period_month": month,
            },
            evidence={
                "match_status": str(row["match_status"]),
                "match_method": str(row["match_method"] or ""),
                "review_revision": int(row["review_revision"]),
                "source_anchor_id": row["source_anchor_id"],
            },
        )
        for row in rows
    ]


def _automatic_authority_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    """Automatic match methods stay review evidence while authority is disabled."""
    rows = conn.execute(
        """SELECT
             line.id, line.amount_cents, line.match_status, line.match_method,
             line.match_score, line.match_rationale, line.matched_transaction_id,
             line.review_revision
           FROM statement_lines line
           LEFT JOIN statement_reviews review
             ON review.source_document_id=line.source_document_id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=line.source_document_id
            AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE line.review_disposition='active'
             AND COALESCE(
               expectation.period_month,
               review.period_month,
               NULLIF(line.statement_period, ''),
               substr(line.posted_on, 1, 7)
             )=?
             AND line.match_status IN ('matched','promoted')
             AND COALESCE(line.match_method, '') NOT IN ('manual')
           ORDER BY line.id""",
        (month,),
    ).fetchall()
    return [
        _item(
            "evidence_gap",
            subject_kind="automatic_match",
            subject_id=row["id"],
            reason=(
                f"statement line {row['id']} was finalized by "
                f"{row['match_method'] or 'legacy/unknown'} without an approved "
                "production automation authority tuple"
            ),
            resolution_href=f"/recon?month={month}#line-{row['id']}",
            amount_cents=int(row["amount_cents"]),
            affected_ids={
                "statement_line_id": int(row["id"]),
                "transaction_id": row["matched_transaction_id"],
            },
            evidence={
                "match_status": str(row["match_status"]),
                "match_method": str(row["match_method"] or ""),
                "match_score": float(row["match_score"] or 0),
                "match_rationale": str(row["match_rationale"] or ""),
                "review_revision": int(row["review_revision"]),
                "automation_authority": "disabled_pending_production_evidence",
            },
        )
        for row in rows
    ]


def _positive_flow_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT
             line.id AS statement_line_id,
             line.amount_cents,
             line.match_status,
             line.matched_transaction_id,
             COALESCE(flow.semantic_status, 'unknown') AS semantic_status
           FROM statement_lines line
           LEFT JOIN transactions transaction_row
             ON transaction_row.id=line.matched_transaction_id
           LEFT JOIN v_transaction_flow_status flow
             ON flow.transaction_id=transaction_row.id
           LEFT JOIN statement_reviews review
             ON review.source_document_id=line.source_document_id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=line.source_document_id
            AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE line.review_disposition='active'
             AND line.amount_cents > 0
             AND COALESCE(
               expectation.period_month,
               review.period_month,
               NULLIF(line.statement_period, ''),
               substr(line.posted_on, 1, 7)
             )=?
             AND NOT (
               line.match_status IN ('matched','promoted')
               AND flow.semantic_status='complete'
             )
           ORDER BY line.id""",
        (month,),
    ).fetchall()
    return [
        _item(
            "unclassified_positive_flow",
            subject_kind="statement_line",
            subject_id=row["statement_line_id"],
            reason=(
                f"positive statement line {row['statement_line_id']} needs an "
                f"accounting explanation ({row['semantic_status']})"
            ),
            resolution_href=(
                f"/recon?month={month}#positive-intake-{row['statement_line_id']}"
            ),
            amount_cents=int(row["amount_cents"]),
            affected_ids={
                "statement_line_id": int(row["statement_line_id"]),
                "transaction_id": row["matched_transaction_id"],
            },
            evidence={
                "match_status": str(row["match_status"]),
                "semantic_status": str(row["semantic_status"]),
            },
        )
        for row in rows
    ]


def _transaction_flow_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    """Map every non-complete ledger flow, including negative pending reviews."""
    rows = conn.execute(
        """SELECT
             transaction_row.id,
             transaction_row.posted_on,
             transaction_row.amount_cents,
             transaction_row.flow_kind,
             flow.semantic_status,
             review.status AS review_status,
             review.reason AS review_reason
           FROM transactions transaction_row
           JOIN v_transaction_flow_status flow
             ON flow.transaction_id=transaction_row.id
           LEFT JOIN transaction_flow_reviews review
             ON review.transaction_id=transaction_row.id
           WHERE transaction_row.posted_on >= ?
             AND transaction_row.posted_on < date(?, '+1 month')
             AND flow.semantic_status <> 'complete'
             AND NOT (
               transaction_row.amount_cents > 0
               AND EXISTS (
                 SELECT 1
                 FROM statement_lines line
                 WHERE line.matched_transaction_id=transaction_row.id
                   AND line.review_disposition='active'
                   AND line.amount_cents > 0
               )
             )
           ORDER BY transaction_row.id""",
        (f"{month}-01", f"{month}-01"),
    ).fetchall()
    return [
        _item(
            (
                "unclassified_positive_flow"
                if int(row["amount_cents"]) > 0
                else "evidence_gap"
            ),
            subject_kind="transaction_flow",
            subject_id=row["id"],
            reason=(
                f"transaction {row['id']} accounting flow is "
                f"{row['semantic_status']}; resolve its pending flow review"
            ),
            resolution_href=f"/review#flow-transaction-{row['id']}",
            amount_cents=int(row["amount_cents"]),
            affected_ids={"transaction_id": int(row["id"])},
            evidence={
                "posted_on": str(row["posted_on"]),
                "flow_kind": str(row["flow_kind"]),
                "semantic_status": str(row["semantic_status"]),
                "review_status": row["review_status"],
                "review_reason": str(row["review_reason"] or ""),
            },
        )
        for row in rows
    ]


def _category_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT *
           FROM v_expense_resolution_status
           WHERE resolution_status='unresolved'
             AND posted_on >= ?
             AND posted_on < date(?, '+1 month')
           ORDER BY transaction_id, transaction_split_id""",
        (f"{month}-01", f"{month}-01"),
    ).fetchall()
    return [
        _item(
            "unconfirmed_merchant_category",
            subject_kind="transaction_split",
            subject_id=row["transaction_split_id"],
            reason=(
                f"transaction {row['transaction_id']} split "
                f"{row['transaction_split_id']} has no human-confirmed expense category"
            ),
            resolution_href=f"/backlog#txn-{row['transaction_id']}",
            amount_cents=int(row["split_amount_cents"]),
            affected_ids={
                "transaction_id": int(row["transaction_id"]),
                "transaction_split_id": int(row["transaction_split_id"]),
                "category_id": int(row["category_id"]),
            },
            evidence={
                "category_name": str(row["category_name"]),
                "resolution_status": str(row["resolution_status"]),
                "accepted_claim_count": int(row["accepted_claim_count"] or 0),
            },
        )
        for row in rows
    ]


def _assertion_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    items = [
        _item(
            "unexplained_balance_delta",
            subject_kind="balance_assertion",
            subject_id=check.assertion_id,
            reason=check.detail,
            resolution_href=f"/recon?month={month}#balance-assertions",
            amount_cents=int(check.delta_cents),
            affected_ids={
                "assertion_id": check.assertion_id,
                "account_id": check.account_id,
                "asof_date": check.asof_date,
            },
            evidence={
                "asserted_cents": check.asserted_cents,
                "ledger_cents": check.ledger_cents,
                "delta_cents": check.delta_cents,
                "statement_period": check.statement_period,
            },
        )
        for check in assertions.scan_assertion_exceptions(conn, month=month)
    ]

    missing = conn.execute(
        """SELECT
             expectation.id AS expectation_id,
             expectation.account_id,
             link.source_document_id,
             review.id AS review_id,
             review.period_end_on,
             review.closing_balance_cents,
             assertion.id AS assertion_id
           FROM account_statement_expectations expectation
           JOIN statement_expectation_documents link
             ON link.expectation_id=expectation.id AND link.status='active'
           LEFT JOIN statement_reviews review
             ON review.source_document_id=link.source_document_id
           LEFT JOIN account_balance_assertions assertion
             ON assertion.account_id=expectation.account_id
            AND assertion.source_document_id=link.source_document_id
            AND assertion.statement_period=expectation.period_month
            AND assertion.asof_date=review.period_end_on
           WHERE expectation.period_month=?
             AND expectation.requirement_state='required'
             AND expectation.lifecycle_state='reconciled'
             AND (
               review.id IS NULL
               OR review.review_state NOT IN ('approved','approved_with_override')
               OR review.period_end_on IS NULL
               OR review.closing_balance_cents IS NULL
               OR assertion.id IS NULL
             )
           ORDER BY expectation.account_id, link.source_document_id""",
        (month,),
    ).fetchall()
    for row in missing:
        items.append(
            _item(
                "evidence_gap",
                subject_kind="statement_balance_evidence",
                subject_id=f"{row['account_id']}:{row['source_document_id']}",
                reason=(
                    "required reconciled statement lacks a current, source-linked "
                    "closing balance assertion"
                ),
                resolution_href=f"/review?document_id={row['source_document_id']}",
                affected_ids={
                    "expectation_id": int(row["expectation_id"]),
                    "account_id": int(row["account_id"]),
                    "source_document_id": int(row["source_document_id"]),
                    "statement_review_id": row["review_id"],
                    "assertion_id": row["assertion_id"],
                },
                evidence={
                    "period_end_on": row["period_end_on"],
                    "closing_balance_cents": row["closing_balance_cents"],
                },
            )
        )
    return items


def _manual_adjustment_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, posted_on, amount_cents, description, notes
           FROM transactions
           WHERE flow_kind='adjustment'
             AND posted_on >= ?
             AND posted_on < date(?, '+1 month')
           ORDER BY id""",
        (f"{month}-01", f"{month}-01"),
    ).fetchall()
    return [
        _item(
            "manual_adjustment",
            subject_kind="transaction",
            subject_id=row["id"],
            reason=(
                f"manual adjustment transaction {row['id']} remains an explicit "
                "close exception"
            ),
            resolution_href=f"/txn/{row['id']}/edit",
            amount_cents=int(row["amount_cents"]),
            affected_ids={"transaction_id": int(row["id"])},
            evidence={
                "posted_on": str(row["posted_on"]),
                "description": str(row["description"]),
                "notes": str(row["notes"] or ""),
            },
        )
        for row in rows
    ]


def collect_period_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    """Collect all live typed close exceptions in deterministic order."""
    month = repo_statement_expectations.normalize_month(month)
    items: list[dict[str, Any]] = []
    items.extend(_statement_exceptions(conn, month))
    items.extend(_unresolved_line_exceptions(conn, month))
    items.extend(_automatic_authority_exceptions(conn, month))
    items.extend(_positive_flow_exceptions(conn, month))
    items.extend(_transaction_flow_exceptions(conn, month))
    items.extend(_category_exceptions(conn, month))
    items.extend(_assertion_exceptions(conn, month))
    items.extend(_manual_adjustment_exceptions(conn, month))
    items.sort(
        key=lambda item: (
            str(item["exception_type"]),
            str(item["subject_kind"]),
            str(item["subject_id"]),
        )
    )
    return items


def build_close_snapshot(
    conn: sqlite3.Connection,
    month: str,
    exceptions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the JSON-safe preview persisted by ``close_period``."""
    month = repo_statement_expectations.normalize_month(month)
    check = checklist.build_checklist(conn, month)
    inbox_count = len(repo_close_inbox.build_inbox(conn, month))
    snapshot = summary.compose_summary(
        conn,
        month,
        check=check,
        inbox_count=inbox_count,
        inbox_ack=False,
        variance_ack=False,
        anomaly_count=len(anomaly.scan_anomalies(conn, month)),
    )
    type_counts = Counter(str(item["exception_type"]) for item in exceptions)
    snapshot.update(
        {
            "month": month,
            "close_state": (
                "clean_closed" if not exceptions else "closed_with_exceptions"
            ),
            "exception_count": len(exceptions),
            "exception_type_counts": dict(sorted(type_counts.items())),
            "checklist": check,
            "planning_signals_are_non_authoritative": True,
            "automation_authority": "disabled_pending_production_evidence",
        }
    )
    return snapshot
