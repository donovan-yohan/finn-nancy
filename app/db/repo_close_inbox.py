"""The Close Inbox read-model (FN-102): one worst-first queue of only the items an
agent is *unconfident* about for a month, so closing never means re-checking data
that is already settled.

It composes the exception sources that already exist as their own surfaces, each
tagged with where it resolves:

  1. Low-confidence extractions  — ``ingest_extractions.review_status='pending'``   → /review
  2. Unmatched statement lines   — ``match_status IN ('unmatched','needs_review')`` → /recon
  3. Positive-flow decisions     — unresolved positive statement evidence            → /recon
  4. Unresolved expense evidence — ``v_expense_resolution_status``                  → /backlog
  5. Anomaly flags               — ``app.close.anomaly.scan_anomalies``             → /insights
  6. Balance-assertion breaks    — ``app.reconcile.assertions.scan_assertion_exceptions`` → /recon

Ordering is a single deterministic worst-first sort: largest dollar magnitude first
(``severity_cents``), then — so equal-magnitude items are stable — ascending model
confidence (the extraction signal; other sources carry none and sort after), then the
oldest item first, then a stable source/id key. Everything here is a pure read: no
INSERT/UPDATE runs, so ``/close`` can build the inbox on a read-only GET.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..close import anomaly
from ..config import Settings, get_settings
from . import repo_statement_expectations
from ..reconcile import assertions


@dataclass(frozen=True)
class InboxItem:
    """One close exception, rendered worst-first. ``severity_cents`` is the unsigned
    magnitude used for ordering; ``amount_cents`` is the signed figure to display (0 when
    the source has no single amount); ``confidence`` is the extraction confidence, or
    None for the magnitude-only sources."""

    source: str          # includes statement_expectation plus the legacy inbox sources
    source_label: str    # human tag rendered on the row
    title: str
    detail: str
    href: str            # deep link into the surface that resolves it
    severity_cents: int  # unsigned magnitude — primary worst-first sort key
    amount_cents: int = 0
    confidence: float | None = None
    posted_on: str = ""  # best available date; oldest-first tie-break
    ident: str = ""      # stable final tie-break within a source
    account_id: int | None = None  # set on 'assertion' items so close.html can offer FN-107 adjust


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _sort_key(item: InboxItem) -> tuple:
    # Worst first: biggest magnitude, then least-confident, then oldest, then stable.
    confidence = item.confidence if item.confidence is not None else 1.0
    return (
        -item.severity_cents,
        confidence,
        item.posted_on or "9999-99-99",
        item.source,
        item.ident,
    )


def _extraction_month(kind: str, payload: dict, created_at: str) -> str:
    """Return only a proved account-period for statements.

    Receipt review retains its purchase/capture-month behavior.  A statement
    without one valid declared closing period stays in the general review queue
    instead of being guessed into a close period from transaction or capture
    dates.
    """
    if kind == "receipt":
        purchased_on = str(payload.get("purchased_on") or "")
        if len(purchased_on) >= 7:
            return purchased_on[:7]
    elif kind == "statement":
        period = str(payload.get("statement_period") or "")
        try:
            return repo_statement_expectations.normalize_month(period)
        except ValueError:
            return ""
    return (created_at or "")[:7]


def _extraction_severity(kind: str, payload: dict) -> int:
    if kind == "receipt":
        return abs(int(payload.get("total_cents") or 0))
    if kind == "statement":
        closing = payload.get("closing_balance_cents")
        if closing is not None:
            return abs(int(closing))
        return sum(abs(int(r.get("amount_cents") or 0)) for r in (payload.get("rows") or []))
    return 0


def _extraction_items(conn: sqlite3.Connection, month: str) -> list[InboxItem]:
    rows = conn.execute(
        """SELECT ie.id, ie.doc_kind, ie.extracted_json, ie.confidence, ie.created_at,
                  sd.original_name
           FROM ingest_extractions ie
           JOIN source_documents sd ON sd.id = ie.source_document_id
           WHERE ie.review_status='pending'
           ORDER BY ie.id""",
    ).fetchall()

    items: list[InboxItem] = []
    for row in rows:
        kind = row["doc_kind"]
        try:
            payload = json.loads(row["extracted_json"])
        except (ValueError, TypeError):
            payload = {}
        if _extraction_month(kind, payload, row["created_at"]) != month:
            continue

        confidence = float(row["confidence"] or 0.0)
        severity = _extraction_severity(kind, payload)
        pct = f"{round(confidence * 100)}%"
        if kind == "receipt":
            who = str(payload.get("merchant") or "").strip() or row["original_name"]
            title = f"{who} receipt needs review"
        elif kind == "statement":
            who = str(payload.get("institution") or "").strip() or row["original_name"]
            title = f"{who} statement needs review"
        else:
            title = f"{row['original_name']} needs review"
        amount_note = f" · {_money(severity)}" if severity else ""
        items.append(
            InboxItem(
                source="extraction",
                source_label="Low-confidence extraction",
                title=title,
                detail=f"Extraction confidence {pct}{amount_note}; approve, fix, or reject on the review queue.",
                href="/review",
                severity_cents=severity,
                amount_cents=severity,
                confidence=confidence,
                posted_on=(row["created_at"] or "")[:10],
                ident=f"extraction-{row['id']}",
            )
        )
    return items


def _statement_line_items(conn: sqlite3.Connection, month: str) -> list[InboxItem]:
    rows = conn.execute(
        """SELECT sl.id, sl.posted_on, sl.raw_description, sl.norm_merchant,
                  sl.amount_cents, sl.match_status, a.name AS account_name
           FROM statement_lines sl
           LEFT JOIN accounts a ON a.id = sl.account_id
           JOIN statement_expectation_documents link
             ON link.source_document_id=sl.source_document_id
            AND link.status='active'
           JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE sl.match_status IN ('unmatched','needs_review')
             AND sl.review_disposition='active'
             AND sl.amount_cents <= 0
             AND expectation.period_month = ?
           ORDER BY sl.id""",
        (month,),
    ).fetchall()

    items: list[InboxItem] = []
    for row in rows:
        merchant = (row["norm_merchant"] or row["raw_description"] or "statement line").strip()
        account = row["account_name"] or "unresolved account"
        amount = int(row["amount_cents"])
        status = "needs review" if row["match_status"] == "needs_review" else "unmatched"
        items.append(
            InboxItem(
                source="statement_line",
                source_label="Unmatched statement line",
                title=f"{merchant} — {_money(amount)}",
                detail=f"{status.capitalize()} on {account}, posted {row['posted_on']}; match it in reconciliation.",
                href=f"/recon?month={month}",
                severity_cents=abs(amount),
                amount_cents=amount,
                posted_on=row["posted_on"],
                ident=f"statement_line-{row['id']}",
            )
        )
    return items


def _positive_flow_items(
    conn: sqlite3.Connection, month: str
) -> list[InboxItem]:
    rows = conn.execute(
        """
        SELECT
          line.id,
          line.posted_on,
          line.raw_description,
          line.amount_cents,
          line.match_status,
          line.matched_transaction_id,
          line.row_confidence,
          line.source_anchor_id,
          account.name AS account_name,
          document.original_name AS document_name,
          COALESCE(flow_status.semantic_status, 'unknown') AS semantic_status
        FROM statement_lines line
        LEFT JOIN transactions txn ON txn.id=line.matched_transaction_id
        LEFT JOIN v_transaction_flow_status flow_status
          ON flow_status.transaction_id=txn.id
        LEFT JOIN accounts account ON account.id=line.account_id
        JOIN source_documents document ON document.id=line.source_document_id
        LEFT JOIN statement_reviews statement_review
          ON statement_review.source_document_id=line.source_document_id
        LEFT JOIN statement_expectation_documents expectation_link
          ON expectation_link.source_document_id=line.source_document_id
         AND expectation_link.status='active'
        LEFT JOIN account_statement_expectations expectation
          ON expectation.id=expectation_link.expectation_id
        WHERE line.review_disposition='active'
          AND line.amount_cents > 0
          AND COALESCE(
            expectation.period_month,
            statement_review.period_month,
            line.statement_period,
            substr(line.posted_on, 1, 7)
          )=?
          AND NOT (
            line.match_status IN ('matched', 'promoted')
            AND flow_status.semantic_status='complete'
          )
        ORDER BY line.id
        """,
        (month,),
    ).fetchall()
    items: list[InboxItem] = []
    for row in rows:
        account = row["account_name"] or "unresolved account"
        amount = int(row["amount_cents"])
        if row["matched_transaction_id"] is None:
            next_step = "create its transaction, then explain the credit"
        elif row["semantic_status"] == "missing_relationship":
            next_step = "pair it to the matching purchase or owned-account leg"
        else:
            next_step = "classify or pair it from the statement evidence"
        anchor = (
            f" · source anchor #{row['source_anchor_id']}"
            if row["source_anchor_id"] is not None
            else ""
        )
        if (
            row["matched_transaction_id"] is None
            or str(row["match_status"]) not in ("matched", "promoted")
        ):
            href = (
                f"/recon?month={month}"
                f"#positive-intake-{int(row['id'])}"
            )
        else:
            href = (
                f"/recon?month={month}"
                f"#positive-flow-{int(row['matched_transaction_id'])}"
            )
        items.append(
            InboxItem(
                source="positive_flow",
                source_label="Money in to explain",
                title=f"{row['raw_description']} — {_money(amount)}",
                detail=(
                    f"{row['document_name']} on {account}, posted "
                    f"{row['posted_on']}{anchor}; {next_step}. "
                    "It is excluded from income until resolved."
                ),
                href=href,
                severity_cents=abs(amount),
                amount_cents=amount,
                confidence=float(row["row_confidence"] or 0.0),
                posted_on=str(row["posted_on"]),
                ident=f"positive-flow-{row['id']}",
            )
        )
    return items


def _statement_expectation_items(
    conn: sqlite3.Connection, month: str
) -> list[InboxItem]:
    items: list[InboxItem] = []
    for row in repo_statement_expectations.period_matrix(conn, month):
        if row["requirement_state"] == "unconfigured":
            items.append(
                InboxItem(
                    source="statement_expectation",
                    source_label="Statement policy",
                    title=f"{row['account_name']} statement policy is unconfigured",
                    detail=(
                        "Choose whether this account issues statements and its "
                        "cadence before the month can close."
                    ),
                    href="/manage",
                    severity_cents=0,
                    posted_on=f"{month}-01",
                    ident=f"statement-expectation-{row['account_id']}",
                    account_id=int(row["account_id"]),
                )
            )
        elif (
            row["requirement_state"] == "required"
            and row["lifecycle_state"] != "reconciled"
        ):
            lifecycle = str(row["lifecycle_state"] or "expected")
            items.append(
                InboxItem(
                    source="statement_expectation",
                    source_label="Required statement",
                    title=f"{row['account_name']} statement is {lifecycle}",
                    detail=(
                        "Upload and review the required statement, then resolve every "
                        "linked line before close."
                    ),
                    href=f"/recon?month={month}",
                    severity_cents=0,
                    posted_on=f"{month}-01",
                    ident=f"statement-expectation-{row['account_id']}",
                    account_id=int(row["account_id"]),
                )
            )
    return items


def _expense_resolution_items(
    conn: sqlite3.Connection,
    month: str,
) -> list[InboxItem]:
    rows = conn.execute(
        """
        SELECT
          status.transaction_id,
          status.transaction_split_id,
          status.posted_on,
          status.split_amount_cents,
          status.category_name,
          txn.description,
          txn.counterparty,
          account.name AS account_name
        FROM v_expense_resolution_status status
        JOIN transactions txn ON txn.id=status.transaction_id
        JOIN accounts account ON account.id=txn.account_id
        WHERE status.resolution_status='unresolved'
          AND status.posted_on >= ?
          AND status.posted_on < date(?, '+1 month')
        ORDER BY status.transaction_id, status.transaction_split_id
        """,
        (f"{month}-01", f"{month}-01"),
    ).fetchall()

    items: list[InboxItem] = []
    for row in rows:
        who = (row["counterparty"] or row["description"] or "transaction").strip()
        amount = int(row["split_amount_cents"])
        category_name = str(row["category_name"])
        resolution = (
            "has no category yet"
            if category_name == "Uncategorized"
            else f"has {category_name} assigned, but it has not been confirmed"
        )
        items.append(
            InboxItem(
                source="expense_resolution",
                source_label="Category confirmation needed",
                title=f"{who} — {_money(amount)}",
                detail=(
                    f"{who} on {row['account_name']}, posted {row['posted_on']}, "
                    f"{resolution}; confirm it in the backlog."
                ),
                href=f"/backlog#txn-{row['transaction_id']}",
                severity_cents=abs(amount),
                amount_cents=amount,
                posted_on=row["posted_on"],
                ident=f"expense-resolution-{row['transaction_split_id']}",
            )
        )
    return items


def _anomaly_items(conn: sqlite3.Connection, month: str, settings: Settings) -> list[InboxItem]:
    flags = anomaly.scan_anomalies(
        conn,
        month,
        trailing_months=settings.anomaly_trailing_months,
        deviation_pct=settings.anomaly_deviation_pct,
        min_deviation_cents=settings.anomaly_min_deviation_cents,
        min_recurring_months=settings.anomaly_min_recurring_months,
    )
    items: list[InboxItem] = []
    for index, flag in enumerate(flags):
        items.append(
            InboxItem(
                source="anomaly",
                source_label="Spending anomaly",
                title=flag.title,
                detail=flag.detail,
                href="/insights",
                severity_cents=int(flag.severity_cents),
                amount_cents=int(flag.deviation_cents),
                posted_on=f"{month}-01",
                ident=f"anomaly-{flag.kind}-{index}",
            )
        )
    return items


def _assertion_items(conn: sqlite3.Connection, month: str) -> list[InboxItem]:
    checks = assertions.scan_assertion_exceptions(conn, month=month)
    items: list[InboxItem] = []
    for check in checks:
        items.append(
            InboxItem(
                source="assertion",
                source_label="Balance assertion",
                title=check.title,
                detail=check.detail,
                href=f"/recon?month={month}",
                severity_cents=int(check.severity_cents),
                amount_cents=int(check.delta_cents),
                posted_on=check.asof_date,
                ident=f"assertion-{check.account_id}-{check.asof_date}",
                account_id=check.account_id,
            )
        )
    return items


def build_inbox(
    conn: sqlite3.Connection,
    month: str,
    *,
    settings: Settings | None = None,
) -> list[InboxItem]:
    """Return the worst-first Close Inbox for ``month``: every open exception across the
    five sources, sorted by :func:`_sort_key`. Pure reads — safe on a read-only GET."""
    settings = settings or get_settings()
    items: list[InboxItem] = []
    items.extend(_statement_expectation_items(conn, month))
    items.extend(_extraction_items(conn, month))
    items.extend(_statement_line_items(conn, month))
    items.extend(_positive_flow_items(conn, month))
    items.extend(_expense_resolution_items(conn, month))
    items.extend(_anomaly_items(conn, month, settings))
    items.extend(_assertion_items(conn, month))
    items.sort(key=_sort_key)
    return items
