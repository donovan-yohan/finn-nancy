"""Deterministic positive-flow proposals and explicit operator commands.

Listing and scoring are pure reads.  No score, model, or source-sign heuristic
can change accounting state.  Accept commands are the only write seam: callers
wrap them in one ``engine.write_tx`` so flow classifications, one typed
relationship, and the append-only decision event commit or roll back together.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from ..accounting import flows
from ..accounting.contract import FlowKind
from ..db import (
    repo_close,
    repo_documents,
    repo_ledger,
    repo_statement_expectations,
    repo_statements,
)
from ..ingest.normalize import norm_merchant


SCORER_VERSION = "positive-flow-v1"
PAIR_WINDOW_DAYS = 180
OFFSET_FLOW_KINDS = frozenset(
    {
        FlowKind.REFUND.value,
        FlowKind.REIMBURSEMENT.value,
        FlowKind.REVERSAL.value,
    }
)


def _text(value: str, field: str) -> str:
    result = (value or "").strip()
    if not result:
        raise ValueError(f"{field} is required for auditability")
    return result


def _month(value: str) -> str:
    return repo_statement_expectations.normalize_month(value)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _request_fingerprint(
    *,
    action_kind: str,
    subject_transaction_id: int,
    statement_line_id: int,
    selected_statement_month: str,
    proposal_key: str,
    candidate_transaction_id: int | None,
    proposed_flow_kind: str,
    relationship_kind: str,
    proposed_candidate_flow_kind: str,
    evidence_fingerprint: str,
    actor: str,
    reason: str,
    selected_category_id: int | None,
    allocation_explicit: bool,
    reverts_event_id: int | None = None,
) -> str:
    """Hash the complete normalized command, not merely its operation key.

    Candidate and flow fields remain explicit even though the proposal key also
    commits to them.  That redundancy makes an idempotent replay independently
    auditable and prevents a future proposal-key implementation change from
    weakening request identity.
    """
    return _digest(
        {
            "action_kind": action_kind,
            "subject_transaction_id": int(subject_transaction_id),
            "statement_line_id": int(statement_line_id),
            "selected_statement_month": _month(selected_statement_month),
            "proposal_key": str(proposal_key),
            "candidate_transaction_id": (
                int(candidate_transaction_id)
                if candidate_transaction_id is not None
                else None
            ),
            "proposed_flow_kind": str(proposed_flow_kind),
            "relationship_kind": str(relationship_kind),
            "proposed_candidate_flow_kind": str(
                proposed_candidate_flow_kind
            ),
            "evidence_fingerprint": str(evidence_fingerprint),
            "actor": _text(actor, "actor"),
            "reason": _text(reason, "reason"),
            "selected_category_id": (
                int(selected_category_id)
                if selected_category_id is not None
                else None
            ),
            "allocation_explicit": bool(allocation_explicit),
            "reverts_event_id": (
                int(reverts_event_id)
                if reverts_event_id is not None
                else None
            ),
        }
    )


def _proposal_key(
    subject_id: int,
    proposed_flow_kind: str,
    relationship_kind: str = "",
    candidate_id: int | None = None,
) -> str:
    return _digest(
        {
            "subject_transaction_id": int(subject_id),
            "proposed_flow_kind": proposed_flow_kind,
            "relationship_kind": relationship_kind,
            "candidate_transaction_id": candidate_id,
        }
    )


def _confidence_band(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _days_between(left: str, right: str) -> int:
    try:
        return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)
    except ValueError:
        return PAIR_WINDOW_DAYS + 1


def _subject_rows(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    selected = _month(month)
    return conn.execute(
        """
        SELECT
          transaction_row.id AS transaction_id,
          transaction_row.account_id,
          transaction_row.posted_on,
          transaction_row.description,
          transaction_row.counterparty,
          transaction_row.amount_cents,
          transaction_row.flow_kind,
          transaction_row.source,
          transaction_row.external_id,
          transaction_row.recon_status,
          account.name AS account_name,
          account.kind AS account_kind,
          UPPER(TRIM(account.currency)) AS currency,
          flow_status.semantic_status,
          flow_status.semantic_reason,
          line.id AS statement_line_id,
          line.review_revision AS statement_line_revision,
          line.source_document_id,
          line.raw_description,
          line.norm_merchant,
          line.row_confidence,
          line.source_anchor_id,
          line.match_status,
          line.statement_period,
          document.original_name AS document_name,
          anchor.locator_kind,
          anchor.locator_json,
          review.source_kind,
          COALESCE(
            expectation.period_month,
            review.period_month,
            line.statement_period,
            substr(transaction_row.posted_on, 1, 7)
          ) AS period_month,
          COALESCE((
            SELECT import.adapter_id
            FROM structured_statement_import_rows import_row
            JOIN structured_statement_imports import
              ON import.id = import_row.import_id
            WHERE import_row.statement_line_id = line.id
            ORDER BY import_row.id DESC
            LIMIT 1
          ), '') AS adapter_id,
          COALESCE((
            SELECT import.adapter_version
            FROM structured_statement_import_rows import_row
            JOIN structured_statement_imports import
              ON import.id = import_row.import_id
            WHERE import_row.statement_line_id = line.id
            ORDER BY import_row.id DESC
            LIMIT 1
          ), '') AS adapter_version
        FROM statement_lines line
        JOIN transactions transaction_row
          ON transaction_row.id = COALESCE(
            line.matched_transaction_id,
            (
              SELECT accepted.subject_transaction_id
              FROM positive_flow_decision_events accepted
              WHERE accepted.statement_line_id = line.id
                AND accepted.event_kind = 'accept'
                AND NOT EXISTS (
                  SELECT 1
                  FROM positive_flow_decision_events undone
                  WHERE undone.event_kind = 'undo'
                    AND undone.reverts_event_id = accepted.id
                )
              ORDER BY accepted.id DESC
              LIMIT 1
            )
          )
        JOIN accounts account ON account.id = transaction_row.account_id
        JOIN source_documents document
          ON document.id = line.source_document_id
        JOIN v_transaction_flow_status flow_status
          ON flow_status.transaction_id = transaction_row.id
        LEFT JOIN statement_source_anchors anchor
          ON anchor.id = line.source_anchor_id
        LEFT JOIN statement_reviews review
          ON review.source_document_id = line.source_document_id
        LEFT JOIN statement_expectation_documents expectation_link
          ON expectation_link.source_document_id = line.source_document_id
         AND expectation_link.status = 'active'
        LEFT JOIN account_statement_expectations expectation
          ON expectation.id = expectation_link.expectation_id
        WHERE line.review_disposition = 'active'
          AND (
            line.match_status IN ('matched', 'promoted')
            OR EXISTS (
              SELECT 1
              FROM positive_flow_decision_events accepted
              WHERE accepted.statement_line_id = line.id
                AND accepted.subject_transaction_id = transaction_row.id
                AND accepted.event_kind = 'accept'
                AND NOT EXISTS (
                  SELECT 1
                  FROM positive_flow_decision_events undone
                  WHERE undone.event_kind = 'undo'
                    AND undone.reverts_event_id = accepted.id
                )
            )
          )
          AND transaction_row.amount_cents > 0
          AND COALESCE(
            expectation.period_month,
            review.period_month,
            line.statement_period,
            substr(transaction_row.posted_on, 1, 7)
          ) = ?
        ORDER BY transaction_row.posted_on, transaction_row.id, line.id
        """,
        (selected,),
    ).fetchall()


def _intake_rows(
    conn: sqlite3.Connection, month: str
) -> list[sqlite3.Row]:
    selected = _month(month)
    return conn.execute(
        """
        SELECT
          line.id AS statement_line_id,
          line.review_revision AS statement_line_revision,
          line.source_document_id,
          line.account_id,
          line.posted_on,
          line.raw_description,
          line.norm_merchant,
          line.amount_cents,
          UPPER(TRIM(line.currency)) AS currency,
          line.is_pending,
          line.statement_period,
          line.row_hash,
          line.match_status,
          line.row_confidence,
          line.source_anchor_id,
          account.name AS account_name,
          account.kind AS account_kind,
          UPPER(TRIM(account.currency)) AS account_currency,
          document.original_name AS document_name,
          anchor.locator_kind,
          anchor.locator_json,
          review.source_kind,
          COALESCE(
            expectation.period_month,
            review.period_month,
            line.statement_period,
            substr(line.posted_on, 1, 7)
          ) AS period_month,
          COALESCE((
            SELECT import.adapter_id
            FROM structured_statement_import_rows import_row
            JOIN structured_statement_imports import
              ON import.id = import_row.import_id
            WHERE import_row.statement_line_id = line.id
            ORDER BY import_row.id DESC
            LIMIT 1
          ), '') AS adapter_id,
          COALESCE((
            SELECT import.adapter_version
            FROM structured_statement_import_rows import_row
            JOIN structured_statement_imports import
              ON import.id = import_row.import_id
            WHERE import_row.statement_line_id = line.id
            ORDER BY import_row.id DESC
            LIMIT 1
          ), '') AS adapter_version
        FROM statement_lines line
        JOIN source_documents document
          ON document.id = line.source_document_id
        LEFT JOIN accounts account ON account.id = line.account_id
        LEFT JOIN statement_source_anchors anchor
          ON anchor.id = line.source_anchor_id
        LEFT JOIN statement_reviews review
          ON review.source_document_id = line.source_document_id
        LEFT JOIN statement_expectation_documents expectation_link
          ON expectation_link.source_document_id = line.source_document_id
         AND expectation_link.status = 'active'
        LEFT JOIN account_statement_expectations expectation
          ON expectation.id = expectation_link.expectation_id
        WHERE line.review_disposition = 'active'
          AND line.amount_cents > 0
          AND line.match_status IN ('ignored', 'unmatched', 'needs_review')
          AND line.matched_transaction_id IS NULL
          AND COALESCE(
            expectation.period_month,
            review.period_month,
            line.statement_period,
            substr(line.posted_on, 1, 7)
          ) = ?
        ORDER BY line.posted_on, line.id
        """,
        (selected,),
    ).fetchall()


def _intake_evidence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scorer_version": SCORER_VERSION,
        "statement_line_id": int(row["statement_line_id"]),
        "statement_line_revision": int(row["statement_line_revision"]),
        "source_document_id": int(row["source_document_id"]),
        "account_id": (
            int(row["account_id"]) if row["account_id"] is not None else None
        ),
        "posted_on": str(row["posted_on"]),
        "amount_cents": int(row["amount_cents"]),
        "currency": str(row["currency"]),
        "account_currency": str(row["account_currency"] or ""),
        "row_hash": str(row["row_hash"]),
        "match_status": str(row["match_status"]),
        "is_pending": int(row["is_pending"]),
        "period_month": str(row["period_month"]),
        "source_anchor_id": row["source_anchor_id"],
        "row_confidence": float(row["row_confidence"] or 0.0),
        "adapter_id": str(row["adapter_id"]),
        "adapter_version": str(row["adapter_version"]),
    }


def list_positive_flow_intake(
    conn: sqlite3.Connection, month: str
) -> list[dict[str, Any]]:
    """List positive statement rows that still need a ledger subject."""
    items: list[dict[str, Any]] = []
    for row in _intake_rows(conn, month):
        evidence = _intake_evidence(row)
        blockers: list[str] = []
        if row["account_id"] is None:
            blockers.append("assign the statement account first")
        if int(row["is_pending"]):
            blockers.append("wait for the pending row to post")
        if (
            row["account_currency"] is not None
            and str(row["currency"]) != str(row["account_currency"])
        ):
            blockers.append("resolve the statement/account currency mismatch")
        items.append(
            {
                "line": dict(row),
                "evidence": evidence,
                "evidence_fingerprint": _digest(evidence),
                "operation_key": f"positive-flow:{uuid.uuid4().hex}",
                "recoverable": not blockers,
                "blockers": blockers,
            }
        )
    return items


def _require_intake_line(
    conn: sqlite3.Connection, line_id: int, month: str
) -> dict[str, Any]:
    matches = [
        item
        for item in list_positive_flow_intake(conn, month)
        if int(item["line"]["statement_line_id"]) == int(line_id)
    ]
    if not matches:
        raise ValueError(
            "positive statement row is not actionable intake in the selected month"
        )
    if len(matches) != 1:
        raise ValueError(
            "positive statement row has ambiguous account-period evidence"
        )
    item = matches[0]
    if not item["recoverable"]:
        raise ValueError("; ".join(item["blockers"]))
    return item


def _require_subject(
    conn: sqlite3.Connection, subject_transaction_id: int, month: str
) -> sqlite3.Row:
    matches = [
        row
        for row in _subject_rows(conn, month)
        if int(row["transaction_id"]) == int(subject_transaction_id)
    ]
    if not matches:
        raise ValueError(
            "positive-flow subject is not an active matched statement row "
            "in the selected month"
        )
    if len(matches) != 1:
        raise ValueError(
            "positive-flow subject has ambiguous statement evidence; "
            "resolve duplicate matches first"
        )
    return matches[0]


def _accept_scope_month(
    conn: sqlite3.Connection, event: sqlite3.Row
) -> str:
    row = conn.execute(
        """
        SELECT COALESCE(
                 expectation.period_month,
                 review.period_month,
                 line.statement_period,
                 substr(transaction_row.posted_on, 1, 7)
               ) AS period_month
        FROM positive_flow_decision_events event
        JOIN transactions transaction_row
          ON transaction_row.id = event.subject_transaction_id
        JOIN statement_lines line ON line.id = event.statement_line_id
        LEFT JOIN statement_reviews review
          ON review.source_document_id = line.source_document_id
        LEFT JOIN statement_expectation_documents expectation_link
          ON expectation_link.source_document_id = line.source_document_id
         AND expectation_link.status = 'active'
        LEFT JOIN account_statement_expectations expectation
          ON expectation.id = expectation_link.expectation_id
        WHERE event.id = ?
        """,
        (int(event["id"]),),
    ).fetchone()
    if row is None or not row["period_month"]:
        raise ValueError("accepted positive-flow event has no statement period")
    return str(row["period_month"])


def _guard_statement_and_endpoint_months(
    conn: sqlite3.Connection,
    selected_statement_month: str,
    *transaction_ids: int,
) -> None:
    """Read every relevant lock before the caller performs any mutation."""
    selected = _month(selected_statement_month)
    if repo_close.is_month_locked(conn, selected):
        raise repo_close.MonthLockedError(selected)
    for transaction_id in dict.fromkeys(int(value) for value in transaction_ids):
        repo_close.guard_transaction_write(conn, transaction_id)


def _live_acceptance(
    conn: sqlite3.Connection, subject_transaction_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT accepted.*
        FROM positive_flow_decision_events accepted
        WHERE accepted.subject_transaction_id = ?
          AND accepted.event_kind = 'accept'
          AND NOT EXISTS (
            SELECT 1
            FROM positive_flow_decision_events undone
            WHERE undone.event_kind = 'undo'
              AND undone.reverts_event_id = accepted.id
          )
        ORDER BY accepted.id DESC
        LIMIT 1
        """,
        (int(subject_transaction_id),),
    ).fetchone()


def _proposal_state(
    conn: sqlite3.Connection, subject_transaction_id: int, proposal_key: str
) -> str:
    row = conn.execute(
        """
        SELECT event_kind
        FROM positive_flow_decision_events
        WHERE subject_transaction_id = ? AND proposal_key = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(subject_transaction_id), proposal_key),
    ).fetchone()
    if row is None or row["event_kind"] in ("restore", "undo"):
        return "available"
    if row["event_kind"] == "reject":
        return "suppressed"
    return "resolved"


def _candidate_rows(
    conn: sqlite3.Connection, subject: sqlite3.Row
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          candidate.id AS transaction_id,
          candidate.account_id,
          candidate.posted_on,
          candidate.description,
          candidate.counterparty,
          candidate.amount_cents,
          candidate.flow_kind,
          candidate.recon_status,
          candidate.source,
          candidate.source_document_id,
          account.name AS account_name,
          account.kind AS account_kind,
          UPPER(TRIM(account.currency)) AS currency,
          document.original_name AS source_document_name,
          flow_status.semantic_status,
          COALESCE((
            SELECT SUM(offset_source.amount_cents)
            FROM transaction_relationships offset_relationship
            JOIN transactions offset_source
              ON offset_source.id = offset_relationship.source_transaction_id
            WHERE offset_relationship.status = 'active'
              AND offset_relationship.relationship_kind IN (
                'refund_of', 'reimbursement_for'
              )
              AND offset_relationship.target_transaction_id = candidate.id
          ), 0) AS applied_offset_cents,
          EXISTS (
            SELECT 1
            FROM transaction_relationships relationship
            WHERE relationship.status = 'active'
              AND relationship.relationship_kind = 'reversal_of'
              AND relationship.target_transaction_id = candidate.id
          ) AS has_reversal,
          EXISTS (
            SELECT 1
            FROM transaction_relationships relationship
            WHERE relationship.status = 'active'
              AND relationship.relationship_kind = 'transfer_pair'
              AND (
                relationship.source_transaction_id = candidate.id
                OR relationship.target_transaction_id = candidate.id
              )
          ) AS has_transfer_pair
        FROM transactions candidate
        JOIN accounts account ON account.id = candidate.account_id
        LEFT JOIN source_documents document
          ON document.id = candidate.source_document_id
        JOIN v_transaction_flow_status flow_status
          ON flow_status.transaction_id = candidate.id
        WHERE candidate.id <> ?
          AND candidate.amount_cents < 0
          AND UPPER(TRIM(account.currency)) = ?
          AND candidate.posted_on
                BETWEEN date(?, ?) AND date(?, '+14 days')
        ORDER BY candidate.posted_on DESC, candidate.id DESC
        """,
        (
            int(subject["transaction_id"]),
            str(subject["currency"]),
            str(subject["posted_on"]),
            f"-{PAIR_WINDOW_DAYS} days",
            str(subject["posted_on"]),
        ),
    ).fetchall()


def _candidate_split_options(
    conn: sqlite3.Connection, transaction_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          split.id AS split_id,
          split.category_id,
          split.amount_cents,
          split.memo,
          category.name AS category_name,
          category.kind AS category_kind
        FROM transaction_splits split
        JOIN categories category ON category.id = split.category_id
        WHERE split.transaction_id = ?
        ORDER BY split.id
        """,
        (int(transaction_id),),
    ).fetchall()
    return [
        {
            "split_id": int(row["split_id"]),
            "category_id": int(row["category_id"]),
            "amount_cents": int(row["amount_cents"]),
            "memo": str(row["memo"] or ""),
            "category_name": str(row["category_name"]),
            "category_kind": str(row["category_kind"]),
        }
        for row in rows
    ]


def _audit_evidence(
    subject: sqlite3.Row,
    *,
    candidate: sqlite3.Row | None,
    candidate_splits: list[dict[str, Any]],
    confidence: float,
    reasons: list[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "statement_line_id": int(subject["statement_line_id"]),
        "source_document_id": int(subject["source_document_id"]),
        "source_anchor_id": subject["source_anchor_id"],
        "row_confidence": float(subject["row_confidence"] or 0.0),
        "subject_amount_cents": int(subject["amount_cents"]),
        "subject_posted_on": str(subject["posted_on"]),
        "confidence": round(float(confidence), 4),
        "reasons": reasons,
    }
    if candidate is not None:
        payload.update(
            {
                "candidate_transaction_id": int(candidate["transaction_id"]),
                "candidate_amount_cents": int(candidate["amount_cents"]),
                "candidate_posted_on": str(candidate["posted_on"]),
                "candidate_currency": str(candidate["currency"]),
                "candidate_source": str(candidate["source"]),
                "candidate_source_document_id": candidate[
                    "source_document_id"
                ],
                "candidate_splits": candidate_splits,
            }
        )
    return payload


def _fingerprint_payload(
    conn: sqlite3.Connection,
    subject: sqlite3.Row,
    *,
    proposed_flow_kind: str,
    relationship_kind: str,
    candidate: sqlite3.Row | None,
    proposed_candidate_flow_kind: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "subject": {
            "transaction_id": int(subject["transaction_id"]),
            "account_id": int(subject["account_id"]),
            "posted_on": str(subject["posted_on"]),
            "amount_cents": int(subject["amount_cents"]),
            "flow_kind": str(subject["flow_kind"]),
            "description": str(subject["description"] or ""),
            "counterparty": str(subject["counterparty"] or ""),
            "recon_status": str(subject["recon_status"]),
            "splits": _split_snapshot(conn, int(subject["transaction_id"])),
        },
        "statement": {
            "line_id": int(subject["statement_line_id"]),
            "line_revision": int(subject["statement_line_revision"]),
            "match_status": str(subject["match_status"]),
            "source_document_id": int(subject["source_document_id"]),
            "source_anchor_id": subject["source_anchor_id"],
            "row_confidence": float(subject["row_confidence"] or 0.0),
        },
        "proposal": {
            "proposed_flow_kind": proposed_flow_kind,
            "relationship_kind": relationship_kind,
            "proposed_candidate_flow_kind": proposed_candidate_flow_kind,
        },
    }
    if candidate is not None:
        payload["candidate"] = {
            "transaction_id": int(candidate["transaction_id"]),
            "account_id": int(candidate["account_id"]),
            "posted_on": str(candidate["posted_on"]),
            "amount_cents": int(candidate["amount_cents"]),
            "flow_kind": str(candidate["flow_kind"]),
            "currency": str(candidate["currency"]),
            "source": str(candidate["source"]),
            "source_document_id": candidate["source_document_id"],
            "description": str(candidate["description"] or ""),
            "counterparty": str(candidate["counterparty"] or ""),
            "recon_status": str(candidate["recon_status"]),
            "applied_offset_cents": int(candidate["applied_offset_cents"]),
            "has_reversal": int(candidate["has_reversal"]),
            "has_transfer_pair": int(candidate["has_transfer_pair"]),
            "splits": _split_snapshot(conn, int(candidate["transaction_id"])),
        }
    return payload


def _proposal(
    conn: sqlite3.Connection,
    subject: sqlite3.Row,
    *,
    proposed_flow_kind: str,
    relationship_kind: str = "",
    candidate: sqlite3.Row | None = None,
    proposed_candidate_flow_kind: str = "",
    confidence: float,
    reasons: list[str],
    action_label: str,
) -> dict[str, Any]:
    candidate_id = (
        int(candidate["transaction_id"]) if candidate is not None else None
    )
    candidate_splits = (
        _candidate_split_options(conn, candidate_id)
        if candidate_id is not None
        else []
    )
    expense_categories: dict[int, dict[str, Any]] = {}
    for split in candidate_splits:
        if split["category_kind"] != "expense":
            continue
        category_id = int(split["category_id"])
        option = expense_categories.setdefault(
            category_id,
            {
                "category_id": category_id,
                "category_name": str(split["category_name"]),
                "candidate_split_cents": 0,
            },
        )
        option["candidate_split_cents"] += int(split["amount_cents"])
    allocation_options = list(expense_categories.values())
    allocation_options.sort(
        key=lambda item: (str(item["category_name"]), int(item["category_id"]))
    )
    is_offset = proposed_flow_kind in OFFSET_FLOW_KINDS
    # A category already present on the candidate ledger row is not accepted
    # category-resolution evidence.  Until an exact trusted projection is wired
    # here, every offset requires an explicit operator allocation.
    automatic_category_id = None
    key = _proposal_key(
        int(subject["transaction_id"]),
        proposed_flow_kind,
        relationship_kind,
        candidate_id,
    )
    fingerprint = _digest(
        _fingerprint_payload(
            conn,
            subject,
            proposed_flow_kind=proposed_flow_kind,
            relationship_kind=relationship_kind,
            candidate=candidate,
            proposed_candidate_flow_kind=proposed_candidate_flow_kind,
        )
    )
    state = _proposal_state(conn, int(subject["transaction_id"]), key)
    evidence = _audit_evidence(
        subject,
        candidate=candidate,
        candidate_splits=candidate_splits,
        confidence=confidence,
        reasons=reasons,
    )
    relationship_name = {
        flows.RelationshipKind.REFUND_OF.value: "refund",
        flows.RelationshipKind.REIMBURSEMENT_FOR.value: "reimbursement",
        flows.RelationshipKind.REVERSAL_OF.value: "charge reversal",
        flows.RelationshipKind.TRANSFER_PAIR.value: (
            "card payment"
            if proposed_flow_kind == FlowKind.CARD_PAYMENT.value
            else "owned-account transfer"
        ),
    }.get(relationship_kind, "match")
    return {
        "proposal_key": key,
        "evidence_fingerprint": fingerprint,
        "proposal_kind": "pair" if candidate is not None else "classification",
        "proposed_flow_kind": proposed_flow_kind,
        "relationship_kind": relationship_kind,
        "candidate_transaction_id": candidate_id,
        "proposed_candidate_flow_kind": proposed_candidate_flow_kind,
        "candidate": (
            {**dict(candidate), "splits": candidate_splits}
            if candidate is not None
            else None
        ),
        "allocation_options": allocation_options,
        "requires_allocation": is_offset,
        "automatic_category_id": automatic_category_id,
        "projected_effect_cents": (
            int(subject["amount_cents"]) if is_offset else 0
        ),
        "confidence": round(float(confidence), 2),
        "confidence_pct": round(float(confidence) * 100),
        "confidence_band": _confidence_band(confidence),
        "reasons": reasons,
        "action_label": action_label,
        "reject_label": f"Not a {relationship_name}",
        "suppression_scope_copy": (
            f"Only this {relationship_name} interpretation will be hidden; "
            "other relationship interpretations for this transaction remain."
        ),
        "state": state,
        "operation_key": f"positive-flow:{uuid.uuid4().hex}",
        "audit_evidence": evidence,
    }


def _classification_proposals(
    conn: sqlite3.Connection, subject: sqlite3.Row
) -> list[dict[str, Any]]:
    return [
        _proposal(
            conn,
            subject,
            proposed_flow_kind=flow_kind,
            confidence=0.0,
            reasons=[
                "classification requires explicit operator confirmation",
                "amount sign is not used as income evidence",
            ],
            action_label=label,
        )
        for flow_kind, label in (
            (FlowKind.INCOME.value, "Mark as earned income"),
            (FlowKind.INTEREST.value, "Mark as interest income"),
        )
    ]


def _pair_proposals(
    conn: sqlite3.Connection, subject: sqlite3.Row
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    subject_amount = int(subject["amount_cents"])
    subject_text = norm_merchant(
        str(subject["raw_description"] or subject["description"] or "")
    )

    for candidate in _candidate_rows(conn, subject):
        candidate_amount = abs(int(candidate["amount_cents"]))
        candidate_text = norm_merchant(
            str(candidate["counterparty"] or candidate["description"] or "")
        )
        similarity = (
            fuzz.token_set_ratio(subject_text, candidate_text) / 100.0
            if subject_text and candidate_text
            else 0.0
        )
        days = _days_between(
            str(subject["posted_on"]), str(candidate["posted_on"])
        )
        date_score = max(0.0, 1.0 - min(days, 60) / 60.0)
        same_account = (
            int(subject["account_id"]) == int(candidate["account_id"])
        )
        exact_amount = subject_amount == candidate_amount

        if (
            exact_amount
            and not same_account
            and not int(candidate["has_transfer_pair"])
            and str(candidate["flow_kind"])
            in (
                FlowKind.UNKNOWN.value,
                FlowKind.INTERNAL_TRANSFER.value,
                FlowKind.CARD_PAYMENT.value,
            )
        ):
            exactly_one_credit = (
                str(subject["account_kind"]) == "credit"
            ) != (str(candidate["account_kind"]) == "credit")
            proposed_flow = (
                FlowKind.CARD_PAYMENT.value
                if exactly_one_credit
                else FlowKind.INTERNAL_TRANSFER.value
            )
            score = min(0.99, 0.7 + 0.2 * date_score + 0.1 * similarity)
            reasons = [
                "equal-and-opposite amount on another owned account",
                f"{days} day posting gap",
            ]
            if exactly_one_credit:
                reasons.append("exactly one account is a credit account")
            proposals.append(
                _proposal(
                    conn,
                    subject,
                    proposed_flow_kind=proposed_flow,
                    relationship_kind=flows.RelationshipKind.TRANSFER_PAIR.value,
                    candidate=candidate,
                    proposed_candidate_flow_kind=proposed_flow,
                    confidence=score,
                    reasons=reasons,
                    action_label=(
                        "Pair as card payment"
                        if exactly_one_credit
                        else "Pair as owned-account transfer"
                    ),
                )
            )

        remaining = candidate_amount - int(candidate["applied_offset_cents"])
        offset_candidate = (
            not int(candidate["has_reversal"])
            and subject_amount <= remaining
            and str(candidate["flow_kind"])
            in (
                FlowKind.UNKNOWN.value,
                FlowKind.PURCHASE.value,
                FlowKind.FEE.value,
            )
        )
        if offset_candidate:
            candidate_flow = (
                str(candidate["flow_kind"])
                if str(candidate["flow_kind"])
                in (FlowKind.PURCHASE.value, FlowKind.FEE.value)
                else FlowKind.PURCHASE.value
            )
            ratio_score = subject_amount / candidate_amount
            score = min(
                0.96,
                0.25
                + 0.2 * date_score
                + 0.3 * similarity
                + (0.15 if same_account else 0.0)
                + (0.1 if exact_amount else 0.05 * ratio_score),
            )
            reasons = [
                (
                    "exact amount match"
                    if exact_amount
                    else "amount fits the purchase's remaining offset"
                ),
                f"{days} day posting gap",
                f"descriptor similarity {round(similarity * 100)}%",
            ]
            if same_account:
                reasons.append("same account")
            for flow_kind, relationship_kind, label in (
                (
                    FlowKind.REFUND.value,
                    flows.RelationshipKind.REFUND_OF.value,
                    "Pair as refund",
                ),
                (
                    FlowKind.REIMBURSEMENT.value,
                    flows.RelationshipKind.REIMBURSEMENT_FOR.value,
                    "Pair as reimbursement",
                ),
            ):
                proposals.append(
                    _proposal(
                        conn,
                        subject,
                        proposed_flow_kind=flow_kind,
                        relationship_kind=relationship_kind,
                        candidate=candidate,
                        proposed_candidate_flow_kind=candidate_flow,
                        confidence=score,
                        reasons=reasons,
                        action_label=label,
                    )
                )

        if (
            exact_amount
            and same_account
            and not int(candidate["has_reversal"])
            and int(candidate["applied_offset_cents"]) == 0
            and str(candidate["flow_kind"])
            in (
                FlowKind.UNKNOWN.value,
                FlowKind.PURCHASE.value,
                FlowKind.FEE.value,
            )
        ):
            candidate_flow = (
                str(candidate["flow_kind"])
                if str(candidate["flow_kind"])
                in (FlowKind.PURCHASE.value, FlowKind.FEE.value)
                else FlowKind.PURCHASE.value
            )
            score = min(0.99, 0.55 + 0.25 * date_score + 0.2 * similarity)
            proposals.append(
                _proposal(
                    conn,
                    subject,
                    proposed_flow_kind=FlowKind.REVERSAL.value,
                    relationship_kind=flows.RelationshipKind.REVERSAL_OF.value,
                    candidate=candidate,
                    proposed_candidate_flow_kind=candidate_flow,
                    confidence=score,
                    reasons=[
                        "exact amount on the same account",
                        f"{days} day posting gap",
                        f"descriptor similarity {round(similarity * 100)}%",
                    ],
                    action_label="Pair as charge reversal",
                )
            )

    proposals.sort(
        key=lambda item: (
            -float(item["confidence"]),
            str(item["candidate"]["posted_on"]) if item["candidate"] else "",
            str(item["relationship_kind"]),
            int(item["candidate_transaction_id"] or 0),
        )
    )
    return proposals[:12]


def _all_proposals(
    conn: sqlite3.Connection, subject: sqlite3.Row
) -> list[dict[str, Any]]:
    return _classification_proposals(conn, subject) + _pair_proposals(
        conn, subject
    )


def _undo_fingerprint(
    conn: sqlite3.Connection, accepted: sqlite3.Row
) -> str:
    subject = conn.execute(
        """SELECT id, account_id, posted_on, amount_cents, flow_kind, recon_status
           FROM transactions WHERE id=?""",
        (int(accepted["subject_transaction_id"]),),
    ).fetchone()
    if subject is None:
        raise ValueError("accepted positive-flow subject is unavailable")
    payload: dict[str, Any] = {
        "accept_event_id": int(accepted["id"]),
        "subject": dict(subject),
        "subject_splits": _split_snapshot(
            conn, int(accepted["subject_transaction_id"])
        ),
        "statement_line_id": int(accepted["statement_line_id"]),
        "statement_line_revision": int(accepted["statement_line_revision"]),
    }
    if accepted["candidate_transaction_id"] is not None:
        candidate = conn.execute(
            """SELECT id, account_id, posted_on, amount_cents, flow_kind,
                      recon_status
               FROM transactions WHERE id=?""",
            (int(accepted["candidate_transaction_id"]),),
        ).fetchone()
        if candidate is None:
            raise ValueError("accepted positive-flow candidate is unavailable")
        payload["candidate"] = dict(candidate)
    if accepted["accepted_relationship_id"] is not None:
        relationship = conn.execute(
            """SELECT id, relationship_kind, source_transaction_id,
                      target_transaction_id, status
               FROM transaction_relationships WHERE id=?""",
            (int(accepted["accepted_relationship_id"]),),
        ).fetchone()
        if relationship is None:
            raise ValueError("accepted positive-flow relationship is unavailable")
        payload["relationship"] = dict(relationship)
    return _digest(payload)


def _accepted_view(
    conn: sqlite3.Connection, accepted: sqlite3.Row
) -> dict[str, Any]:
    candidate = None
    if accepted["candidate_transaction_id"] is not None:
        row = conn.execute(
            """
            SELECT candidate.*, account.name AS account_name,
                   account.kind AS account_kind,
                   UPPER(TRIM(account.currency)) AS currency,
                   document.original_name AS source_document_name
            FROM transactions candidate
            JOIN accounts account ON account.id = candidate.account_id
            LEFT JOIN source_documents document
              ON document.id = candidate.source_document_id
            WHERE candidate.id = ?
            """,
            (int(accepted["candidate_transaction_id"]),),
        ).fetchone()
        candidate = (
            {
                **dict(row),
                "splits": _candidate_split_options(
                    conn, int(accepted["candidate_transaction_id"])
                ),
            }
            if row is not None
            else None
        )
    selected_category = None
    if accepted["selected_category_id"] is not None:
        row = conn.execute(
            "SELECT id, name, kind FROM categories WHERE id=?",
            (int(accepted["selected_category_id"]),),
        ).fetchone()
        selected_category = dict(row) if row is not None else None
    return {
        "event_id": int(accepted["id"]),
        "proposed_flow_kind": str(accepted["proposed_flow_kind"]),
        "relationship_kind": str(accepted["relationship_kind"]),
        "candidate": candidate,
        "accepted_relationship_id": accepted["accepted_relationship_id"],
        "selected_category": selected_category,
        "allocation_explicit": bool(accepted["allocation_explicit"]),
        "actor": str(accepted["actor"]),
        "reason": str(accepted["reason"]),
        "created_at": str(accepted["created_at"]),
        "undo_fingerprint": _undo_fingerprint(conn, accepted),
        "operation_key": f"positive-flow:{uuid.uuid4().hex}",
    }


def list_positive_flow_reviews(
    conn: sqlite3.Connection,
    month: str,
    *,
    include_suppressed: bool = False,
) -> list[dict[str, Any]]:
    """Return positive statement transactions requiring review or offering undo."""
    selected = _month(month)
    reviews: list[dict[str, Any]] = []
    for subject in _subject_rows(conn, selected):
        accepted = _live_acceptance(conn, int(subject["transaction_id"]))
        if accepted is not None:
            reviews.append(
                {
                    "subject": dict(subject),
                    "status": "resolved",
                    "proposals": [],
                    "suppressed_proposals": [],
                    "acceptance": _accepted_view(conn, accepted),
                }
            )
            continue
        if str(subject["semantic_status"]) == "complete":
            continue
        proposals = _all_proposals(conn, subject)
        available = [
            item for item in proposals if item["state"] == "available"
        ]
        suppressed = [
            item for item in proposals if item["state"] == "suppressed"
        ]
        reviews.append(
            {
                "subject": dict(subject),
                "status": "needs_review",
                "proposals": (
                    available + suppressed
                    if include_suppressed
                    else available
                ),
                "suppressed_proposals": suppressed,
                "acceptance": None,
            }
        )
    return reviews


def _find_proposal(
    conn: sqlite3.Connection,
    subject: sqlite3.Row,
    proposal_key: str,
) -> dict[str, Any]:
    for proposal in _all_proposals(conn, subject):
        if proposal["proposal_key"] == proposal_key:
            return proposal
    raise ValueError("positive-flow proposal is no longer available")


def _operation_replay(
    conn: sqlite3.Connection,
    operation_key: str,
    *,
    action_kind: str,
    subject_transaction_id: int | None,
    statement_line_id: int | None,
    selected_statement_month: str,
    proposal_key: str,
    evidence_fingerprint: str,
    actor: str,
    reason: str,
    selected_category_id: int | None = None,
    allocation_explicit: bool = False,
    proposed_flow_kind: str | None = None,
    reverts_event_id: int | None = None,
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM positive_flow_decision_events WHERE operation_key=?",
        (_text(operation_key, "operation_key"),),
    ).fetchone()
    if row is None:
        return None
    stored_request = {
        "action_kind": str(row["action_kind"]),
        "subject_transaction_id": int(row["subject_transaction_id"]),
        "statement_line_id": int(row["statement_line_id"]),
        "selected_statement_month": str(row["selected_statement_month"]),
        "proposal_key": str(row["proposal_key"]),
        "candidate_transaction_id": (
            int(row["candidate_transaction_id"])
            if row["candidate_transaction_id"] is not None
            else None
        ),
        "proposed_flow_kind": str(row["proposed_flow_kind"]),
        "relationship_kind": str(row["relationship_kind"]),
        "proposed_candidate_flow_kind": str(
            row["proposed_candidate_flow_kind"]
        ),
        "evidence_fingerprint": str(row["evidence_fingerprint"]),
        "actor": str(row["actor"]),
        "reason": str(row["reason"]),
        "selected_category_id": (
            int(row["selected_category_id"])
            if row["selected_category_id"] is not None
            else None
        ),
        "allocation_explicit": bool(row["allocation_explicit"]),
        "reverts_event_id": (
            int(row["reverts_event_id"])
            if row["reverts_event_id"] is not None
            else None
        ),
    }
    expected_event_kind = {
        "recover_positive_line": "intake",
        "reject_proposal": "reject",
        "restore_proposal": "restore",
        "accept_classification": "accept",
        "accept_pair": "accept",
        "undo_acceptance": "undo",
    }.get(stored_request["action_kind"])
    expected_proposal_key = (
        f"intake:{stored_request['statement_line_id']}"
        if stored_request["action_kind"] == "recover_positive_line"
        else _proposal_key(
            stored_request["subject_transaction_id"],
            stored_request["proposed_flow_kind"],
            stored_request["relationship_kind"],
            stored_request["candidate_transaction_id"],
        )
    )
    stored_fingerprint = _request_fingerprint(**stored_request)
    if (
        expected_event_kind != str(row["event_kind"])
        or expected_proposal_key != stored_request["proposal_key"]
        or stored_fingerprint != str(row["request_fingerprint"])
    ):
        raise ValueError(
            "operation_key was already used for a different positive-flow request"
        )
    effective_category_id = (
        int(selected_category_id)
        if allocation_explicit and selected_category_id is not None
        else (
            int(row["selected_category_id"])
            if row["selected_category_id"] is not None
            else None
        )
    )
    expected_request = {
        "action_kind": action_kind,
        "subject_transaction_id": (
            int(subject_transaction_id)
            if subject_transaction_id is not None
            else int(row["subject_transaction_id"])
        ),
        "statement_line_id": (
            int(statement_line_id)
            if statement_line_id is not None
            else int(row["statement_line_id"])
        ),
        "selected_statement_month": _month(selected_statement_month),
        "proposal_key": str(proposal_key),
        "candidate_transaction_id": (
            int(row["candidate_transaction_id"])
            if row["candidate_transaction_id"] is not None
            else None
        ),
        "proposed_flow_kind": (
            proposed_flow_kind
            if proposed_flow_kind is not None
            else str(row["proposed_flow_kind"])
        ),
        "relationship_kind": str(row["relationship_kind"]),
        "proposed_candidate_flow_kind": str(
            row["proposed_candidate_flow_kind"]
        ),
        "evidence_fingerprint": str(evidence_fingerprint),
        "actor": _text(actor, "actor"),
        "reason": _text(reason, "reason"),
        "selected_category_id": effective_category_id,
        "allocation_explicit": bool(allocation_explicit),
        "reverts_event_id": (
            int(reverts_event_id) if reverts_event_id is not None else None
        ),
    }
    if stored_request != expected_request:
        raise ValueError(
            "operation_key was already used for a different positive-flow request"
        )
    return row


def _verify_fingerprint(proposal: dict[str, Any], expected: str) -> None:
    if proposal["evidence_fingerprint"] != expected:
        raise ValueError(
            "positive-flow evidence changed; refresh the month-end queue"
        )


def _event_fields(proposal: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(proposal["proposed_flow_kind"]),
        str(proposal["relationship_kind"]),
        proposal["candidate_transaction_id"],
        str(proposal["proposed_candidate_flow_kind"]),
    )


def _split_snapshot(
    conn: sqlite3.Connection, transaction_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, category_id, amount_cents, memo
        FROM transaction_splits
        WHERE transaction_id=?
        ORDER BY id
        """,
        (int(transaction_id),),
    ).fetchall()
    if not rows:
        raise ValueError("positive-flow subject has no accounting splits")
    return [
        {
            "id": int(row["id"]),
            "category_id": int(row["category_id"]),
            "amount_cents": int(row["amount_cents"]),
            "memo": str(row["memo"] or ""),
        }
        for row in rows
    ]


def _split_json(snapshot: list[dict[str, Any]]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _ensure_semantic_category(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
) -> int:
    row = conn.execute(
        "SELECT id, kind FROM categories WHERE name=? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if row is not None:
        if str(row["kind"]) != kind:
            raise ValueError(
                f"system category {name!r} has kind {row['kind']!r}; "
                f"expected {kind!r}"
            )
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO categories(name, kind, brand_owner, color)
        VALUES (?, ?, 'shared', '#9AA5B1')
        """,
        (name, kind),
    )
    return int(cursor.lastrowid)


def _resolve_offset_category(
    conn: sqlite3.Connection,
    *,
    candidate_transaction_id: int,
    selected_category_id: int | None,
) -> tuple[int, bool]:
    options = {
        int(split["category_id"])
        for split in _candidate_split_options(
            conn, int(candidate_transaction_id)
        )
        if split["category_kind"] == "expense"
    }
    if not options:
        raise ValueError(
            "offset candidate has no expense category; categorize it first"
        )
    if selected_category_id is None:
        raise ValueError(
            "choose the accepted expense category this money-in offsets"
        )
    chosen = int(selected_category_id)
    if chosen not in options:
        raise ValueError(
            "selected offset category is not an expense split on the candidate"
        )
    return chosen, True


def _retag_subject_splits(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    proposed_flow_kind: str,
    selected_category_id: int | None = None,
) -> tuple[str, str]:
    prior = _split_snapshot(conn, subject_transaction_id)
    if proposed_flow_kind in (FlowKind.INCOME.value, FlowKind.INTEREST.value):
        category_id = _ensure_semantic_category(
            conn, name="Uncategorized Income", kind="income"
        )
    elif proposed_flow_kind in (
        FlowKind.REFUND.value,
        FlowKind.REIMBURSEMENT.value,
        FlowKind.REVERSAL.value,
    ):
        if selected_category_id is None:
            raise ValueError("an offset classification requires a category allocation")
        category_id = int(selected_category_id)
    elif proposed_flow_kind in (
        FlowKind.INTERNAL_TRANSFER.value,
        FlowKind.CARD_PAYMENT.value,
    ):
        category_id = _ensure_semantic_category(
            conn, name="Internal Transfers", kind="transfer"
        )
    else:  # pragma: no cover - callers constrain this vocabulary
        raise ValueError("unsupported positive-flow split classification")
    conn.execute(
        """
        UPDATE transaction_splits
        SET category_id=?
        WHERE transaction_id=?
        """,
        (category_id, int(subject_transaction_id)),
    )
    accepted = _split_snapshot(conn, subject_transaction_id)
    return _split_json(prior), _split_json(accepted)


def _restore_subject_splits(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    accepted_json: str,
    prior_json: str,
) -> None:
    current = _split_json(_split_snapshot(conn, subject_transaction_id))
    if current != accepted_json:
        raise ValueError(
            "accepted positive-flow splits changed; resolve that edit before undoing"
        )
    try:
        prior = json.loads(prior_json)
    except (TypeError, ValueError) as exc:  # pragma: no cover - schema guards
        raise ValueError("positive-flow prior split snapshot is invalid") from exc
    if not isinstance(prior, list) or not prior:
        raise ValueError("positive-flow prior split snapshot is empty")
    for split in prior:
        cursor = conn.execute(
            """
            UPDATE transaction_splits
            SET category_id=?, amount_cents=?, memo=?
            WHERE id=? AND transaction_id=?
            """,
            (
                int(split["category_id"]),
                int(split["amount_cents"]),
                str(split["memo"]),
                int(split["id"]),
                int(subject_transaction_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("positive-flow split identity changed")
    if _split_json(_split_snapshot(conn, subject_transaction_id)) != prior_json:
        raise ValueError("positive-flow prior split snapshot could not be restored")


def recover_positive_line(
    conn: sqlite3.Connection,
    *,
    statement_line_id: int,
    month: str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    """Promote exactly one unresolved positive row into unknown-flow review."""
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    proposal_key = f"intake:{int(statement_line_id)}"
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="recover_positive_line",
        subject_transaction_id=None,
        statement_line_id=int(statement_line_id),
        selected_statement_month=selected,
        proposal_key=proposal_key,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
    )
    if existing is not None:
        return int(existing["id"])

    intake = _require_intake_line(conn, statement_line_id, selected)
    if intake["evidence_fingerprint"] != evidence_fingerprint:
        raise ValueError(
            "positive statement intake changed; refresh the month-end queue"
        )
    line = intake["line"]
    prior_match_status = str(line["match_status"])

    canonical = conn.execute(
        """
        SELECT subject.*, UPPER(TRIM(account.currency)) AS account_currency,
               document.kind AS source_document_kind
        FROM transactions subject
        JOIN accounts account ON account.id = subject.account_id
        LEFT JOIN source_documents document
          ON document.id = subject.source_document_id
        WHERE subject.source = 'statement'
          AND subject.external_id = ?
        """,
        (str(line["row_hash"]),),
    ).fetchone()
    if canonical is not None:
        if (
            canonical["source_document_id"] is None
            or int(canonical["source_document_id"])
            != int(line["source_document_id"])
            or int(canonical["account_id"]) != int(line["account_id"])
            or str(canonical["posted_on"]) != str(line["posted_on"])
            or int(canonical["amount_cents"]) != int(line["amount_cents"])
            or str(canonical["account_currency"]) != str(line["currency"])
            or str(canonical["source_document_kind"]) != "statement"
        ):
            raise ValueError(
                "canonical statement transaction does not match this row's "
                "document, account, date, amount, currency, and source"
            )
        claimed_elsewhere = conn.execute(
            """
            SELECT 1
            FROM statement_lines
            WHERE id <> ?
              AND matched_transaction_id = ?
              AND review_disposition = 'active'
            LIMIT 1
            """,
            (int(statement_line_id), int(canonical["id"])),
        ).fetchone()
        if claimed_elsewhere is not None:
            raise ValueError(
                "canonical statement transaction is already bound to another row"
            )
        subject_transaction_id = int(canonical["id"])
        subject_flow_kind = str(canonical["flow_kind"])
        live_acceptance = _live_acceptance(conn, subject_transaction_id)
        if subject_flow_kind != FlowKind.UNKNOWN.value and (
            live_acceptance is None
            or int(live_acceptance["statement_line_id"]) != int(statement_line_id)
            or str(live_acceptance["selected_statement_month"]) != selected
            or str(live_acceptance["proposed_flow_kind"]) != subject_flow_kind
        ):
            raise ValueError(
                "canonical statement transaction must be unknown or retain its "
                "live positive-flow acceptance"
            )
        if subject_flow_kind == FlowKind.UNKNOWN.value and live_acceptance is not None:
            raise ValueError(
                "canonical statement transaction has inconsistent live acceptance"
            )
        _guard_statement_and_endpoint_months(
            conn, selected, subject_transaction_id
        )
        repo_statements.set_flow_kind(
            conn, int(statement_line_id), subject_flow_kind
        )
        repo_statements.set_match(
            conn,
            int(statement_line_id),
            status="promoted",
            transaction_id=subject_transaction_id,
            rationale="targeted positive-flow recovery",
        )
        repo_statements.mark_cleared(
            conn,
            subject_transaction_id,
            str(line["posted_on"]),
            account_id=int(line["account_id"]),
        )
    else:
        if repo_close.is_month_locked(conn, selected):
            raise repo_close.MonthLockedError(selected)
        repo_close.guard_transaction_insert(conn, str(line["posted_on"]))
        repo_statements.set_flow_kind(
            conn, int(statement_line_id), FlowKind.UNKNOWN.value
        )
        from .engine import promote_from_line

        staged_line = conn.execute(
            "SELECT * FROM statement_lines WHERE id=?",
            (int(statement_line_id),),
        ).fetchone()
        assert staged_line is not None
        promoted = promote_from_line(conn, staged_line)
        if promoted is None:
            raise ValueError(
                "positive statement row could not create its review transaction"
            )
        subject_transaction_id = int(promoted)
        subject_flow_kind = FlowKind.UNKNOWN.value

    accepted_splits_json = _split_json(
        _split_snapshot(conn, subject_transaction_id)
    )
    request_fingerprint = _request_fingerprint(
        action_kind="recover_positive_line",
        subject_transaction_id=subject_transaction_id,
        statement_line_id=int(statement_line_id),
        selected_statement_month=selected,
        proposal_key=proposal_key,
        candidate_transaction_id=None,
        proposed_flow_kind=subject_flow_kind,
        relationship_kind="",
        proposed_candidate_flow_kind="",
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=None,
        allocation_explicit=False,
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind,
              prior_statement_match_status,
              accepted_subject_splits_json, evidence_json, actor, reason
            ) VALUES (?,?,?,?,?,?,?,?,?,'intake',?,?,?,?,?,?)
            """,
            (
                _text(operation_key, "operation_key"),
                subject_transaction_id,
                int(statement_line_id),
                int(line["statement_line_revision"]),
                proposal_key,
                evidence_fingerprint,
                "recover_positive_line",
                selected,
                request_fingerprint,
                subject_flow_kind,
                prior_match_status,
                accepted_splits_json,
                json.dumps(
                    intake["evidence"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow intake failed: {exc}") from exc

    repo_statement_expectations.sync_document_reconciliation(
        conn,
        int(line["source_document_id"]),
        actor=actor_text,
        reason=reason_text,
    )
    remaining = conn.execute(
        """
        SELECT COUNT(*)
        FROM statement_lines
        WHERE source_document_id = ?
          AND review_disposition = 'active'
          AND (
            match_status IN ('unmatched', 'needs_review')
            OR (match_status = 'ignored' AND amount_cents > 0)
          )
        """,
        (int(line["source_document_id"]),),
    ).fetchone()[0]
    repo_documents.set_status(
        conn,
        int(line["source_document_id"]),
        "needs_review" if remaining else "matched",
    )
    return int(cursor.lastrowid)


def reject_proposal(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    month: str,
    proposal_key: str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="reject_proposal",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=None,
        selected_statement_month=selected,
        proposal_key=proposal_key,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
    )
    if existing is not None:
        return int(existing["id"])
    subject = _require_subject(conn, subject_transaction_id, selected)
    proposal = _find_proposal(conn, subject, proposal_key)
    _verify_fingerprint(proposal, evidence_fingerprint)
    if proposal["state"] != "available":
        raise ValueError("only an available positive-flow proposal can be rejected")
    proposed_flow, relationship, candidate_id, candidate_flow = _event_fields(
        proposal
    )
    request_fingerprint = _request_fingerprint(
        action_kind="reject_proposal",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=int(subject["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=proposal_key,
        candidate_transaction_id=candidate_id,
        proposed_flow_kind=proposed_flow,
        relationship_kind=relationship,
        proposed_candidate_flow_kind=candidate_flow,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=None,
        allocation_explicit=False,
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, relationship_kind,
              candidate_transaction_id, proposed_candidate_flow_kind,
              evidence_json, actor, reason
            ) VALUES (?,?,?,?,?,?,?,?,?,'reject',?,?,?,?,?,?,?)
            """,
            (
                _text(operation_key, "operation_key"),
                int(subject_transaction_id),
                int(subject["statement_line_id"]),
                int(subject["statement_line_revision"]),
                proposal_key,
                evidence_fingerprint,
                "reject_proposal",
                selected,
                request_fingerprint,
                proposed_flow,
                relationship,
                candidate_id,
                candidate_flow,
                json.dumps(
                    proposal["audit_evidence"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow reject failed: {exc}") from exc
    return int(cursor.lastrowid)


def restore_proposal(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    month: str,
    proposal_key: str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="restore_proposal",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=None,
        selected_statement_month=selected,
        proposal_key=proposal_key,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
    )
    if existing is not None:
        return int(existing["id"])
    subject = _require_subject(conn, subject_transaction_id, selected)
    proposal = _find_proposal(conn, subject, proposal_key)
    _verify_fingerprint(proposal, evidence_fingerprint)
    if proposal["state"] != "suppressed":
        raise ValueError("only a rejected positive-flow proposal can be restored")
    proposed_flow, relationship, candidate_id, candidate_flow = _event_fields(
        proposal
    )
    request_fingerprint = _request_fingerprint(
        action_kind="restore_proposal",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=int(subject["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=proposal_key,
        candidate_transaction_id=candidate_id,
        proposed_flow_kind=proposed_flow,
        relationship_kind=relationship,
        proposed_candidate_flow_kind=candidate_flow,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=None,
        allocation_explicit=False,
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, relationship_kind,
              candidate_transaction_id, proposed_candidate_flow_kind,
              evidence_json, actor, reason
            ) VALUES (?,?,?,?,?,?,?,?,?,'restore',?,?,?,?,?,?,?)
            """,
            (
                _text(operation_key, "operation_key"),
                int(subject_transaction_id),
                int(subject["statement_line_id"]),
                int(subject["statement_line_revision"]),
                proposal_key,
                evidence_fingerprint,
                "restore_proposal",
                selected,
                request_fingerprint,
                proposed_flow,
                relationship,
                candidate_id,
                candidate_flow,
                json.dumps(
                    proposal["audit_evidence"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow restore failed: {exc}") from exc
    return int(cursor.lastrowid)


def accept_classification(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    month: str,
    flow_kind: FlowKind | str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    normalized = flows.normalize_flow_kind(flow_kind)
    if normalized not in (FlowKind.INCOME, FlowKind.INTEREST):
        raise ValueError("positive-flow classification must be income or interest")
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    key = _proposal_key(int(subject_transaction_id), normalized.value)
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="accept_classification",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=None,
        selected_statement_month=selected,
        proposal_key=key,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        proposed_flow_kind=normalized.value,
    )
    if existing is not None:
        return int(existing["id"])
    subject = _require_subject(conn, subject_transaction_id, selected)
    proposal = _find_proposal(conn, subject, key)
    _verify_fingerprint(proposal, evidence_fingerprint)
    if proposal["state"] != "available" or _live_acceptance(
        conn, subject_transaction_id
    ):
        raise ValueError("positive-flow subject already has a live decision")
    prior_flow = str(subject["flow_kind"])
    _guard_statement_and_endpoint_months(
        conn, selected, int(subject_transaction_id)
    )
    flows.set_flow_kind(
        conn,
        int(subject_transaction_id),
        normalized,
        actor=actor_text,
        reason=reason_text,
    )
    prior_splits_json, accepted_splits_json = _retag_subject_splits(
        conn,
        subject_transaction_id=int(subject_transaction_id),
        proposed_flow_kind=normalized.value,
    )
    request_fingerprint = _request_fingerprint(
        action_kind="accept_classification",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=int(subject["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=key,
        candidate_transaction_id=None,
        proposed_flow_kind=normalized.value,
        relationship_kind="",
        proposed_candidate_flow_kind="",
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=None,
        allocation_explicit=False,
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, prior_subject_flow_kind,
              prior_subject_splits_json, accepted_subject_splits_json,
              evidence_json, actor, reason
            ) VALUES (?,?,?,?,?,?,?,?,?,'accept',?,?,?,?,?,?,?)
            """,
            (
                _text(operation_key, "operation_key"),
                int(subject_transaction_id),
                int(subject["statement_line_id"]),
                int(subject["statement_line_revision"]),
                key,
                evidence_fingerprint,
                "accept_classification",
                selected,
                request_fingerprint,
                normalized.value,
                prior_flow,
                prior_splits_json,
                accepted_splits_json,
                json.dumps(
                    proposal["audit_evidence"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow acceptance failed: {exc}") from exc
    return int(cursor.lastrowid)


def accept_pair(
    conn: sqlite3.Connection,
    *,
    subject_transaction_id: int,
    month: str,
    proposal_key: str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
    selected_category_id: int | None = None,
) -> int:
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    allocation_explicit = selected_category_id is not None
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="accept_pair",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=None,
        selected_statement_month=selected,
        proposal_key=proposal_key,
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=selected_category_id,
        allocation_explicit=allocation_explicit,
    )
    if existing is not None:
        return int(existing["id"])
    subject = _require_subject(conn, subject_transaction_id, selected)
    proposal = _find_proposal(conn, subject, proposal_key)
    _verify_fingerprint(proposal, evidence_fingerprint)
    if (
        proposal["proposal_kind"] != "pair"
        or proposal["state"] != "available"
        or _live_acceptance(conn, subject_transaction_id)
    ):
        raise ValueError("positive-flow pair is not available to accept")

    candidate_id = int(proposal["candidate_transaction_id"])
    candidate = conn.execute(
        "SELECT * FROM transactions WHERE id=?", (candidate_id,)
    ).fetchone()
    if candidate is None:
        raise ValueError("positive-flow pair candidate is unavailable")
    prior_subject_flow = str(subject["flow_kind"])
    prior_candidate_flow = str(candidate["flow_kind"])
    proposed_flow_kind = str(proposal["proposed_flow_kind"])
    effective_category_id: int | None = None
    if proposed_flow_kind in OFFSET_FLOW_KINDS:
        effective_category_id, allocation_explicit = _resolve_offset_category(
            conn,
            candidate_transaction_id=candidate_id,
            selected_category_id=selected_category_id,
        )
    elif selected_category_id is not None:
        raise ValueError(
            "category allocation applies only to refunds, reimbursements, "
            "or reversals"
        )
    request_fingerprint = _request_fingerprint(
        action_kind="accept_pair",
        subject_transaction_id=int(subject_transaction_id),
        statement_line_id=int(subject["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=proposal_key,
        candidate_transaction_id=candidate_id,
        proposed_flow_kind=proposed_flow_kind,
        relationship_kind=str(proposal["relationship_kind"]),
        proposed_candidate_flow_kind=str(
            proposal["proposed_candidate_flow_kind"]
        ),
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=effective_category_id,
        allocation_explicit=allocation_explicit,
    )
    _guard_statement_and_endpoint_months(
        conn, selected, int(subject_transaction_id), candidate_id
    )

    flows.set_flow_kind(
        conn,
        candidate_id,
        str(proposal["proposed_candidate_flow_kind"]),
        actor=actor_text,
        reason=reason_text,
    )
    flows.set_flow_kind(
        conn,
        int(subject_transaction_id),
        str(proposal["proposed_flow_kind"]),
        actor=actor_text,
        reason=reason_text,
    )
    prior_splits_json, accepted_splits_json = _retag_subject_splits(
        conn,
        subject_transaction_id=int(subject_transaction_id),
        proposed_flow_kind=proposed_flow_kind,
        selected_category_id=effective_category_id,
    )
    relationship_kind = flows.normalize_relationship_kind(
        str(proposal["relationship_kind"])
    )
    if relationship_kind == flows.RelationshipKind.TRANSFER_PAIR:
        source_id, target_id = candidate_id, int(subject_transaction_id)
    else:
        source_id, target_id = int(subject_transaction_id), candidate_id
    relationship_id = flows.create_relationship(
        conn,
        relationship_kind=relationship_kind,
        source_transaction_id=source_id,
        target_transaction_id=target_id,
        actor=actor_text,
        reason=reason_text,
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, relationship_kind,
              candidate_transaction_id, proposed_candidate_flow_kind,
              accepted_relationship_id, prior_subject_flow_kind,
              prior_candidate_flow_kind, selected_category_id,
              allocation_explicit, prior_subject_splits_json,
              accepted_subject_splits_json, evidence_json, actor, reason
            ) VALUES (
              ?,?,?,?,?,?,?,?,?,'accept',?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                _text(operation_key, "operation_key"),
                int(subject_transaction_id),
                int(subject["statement_line_id"]),
                int(subject["statement_line_revision"]),
                proposal_key,
                evidence_fingerprint,
                "accept_pair",
                selected,
                request_fingerprint,
                proposed_flow_kind,
                relationship_kind.value,
                candidate_id,
                str(proposal["proposed_candidate_flow_kind"]),
                relationship_id,
                prior_subject_flow,
                prior_candidate_flow,
                effective_category_id,
                int(allocation_explicit),
                prior_splits_json,
                accepted_splits_json,
                json.dumps(
                    {
                        **proposal["audit_evidence"],
                        "selected_category_id": effective_category_id,
                        "allocation_explicit": allocation_explicit,
                        "projected_effect_cents": int(
                            proposal["projected_effect_cents"]
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow pair acceptance failed: {exc}") from exc
    return int(cursor.lastrowid)


def undo_acceptance(
    conn: sqlite3.Connection,
    *,
    accept_event_id: int,
    month: str,
    evidence_fingerprint: str,
    operation_key: str,
    actor: str,
    reason: str,
) -> int:
    selected = _month(month)
    actor_text = _text(actor, "actor")
    reason_text = _text(reason, "reason")
    accepted = conn.execute(
        """
        SELECT * FROM positive_flow_decision_events
        WHERE id=? AND event_kind='accept'
        """,
        (int(accept_event_id),),
    ).fetchone()
    if accepted is None:
        raise ValueError("positive-flow acceptance was not found")
    if (
        str(accepted["selected_statement_month"]) != selected
        or _accept_scope_month(conn, accepted) != selected
    ):
        raise ValueError("positive-flow acceptance is outside the selected month")
    existing = _operation_replay(
        conn,
        operation_key,
        action_kind="undo_acceptance",
        subject_transaction_id=int(accepted["subject_transaction_id"]),
        statement_line_id=int(accepted["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=str(accepted["proposal_key"]),
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=(
            int(accepted["selected_category_id"])
            if accepted["selected_category_id"] is not None
            else None
        ),
        allocation_explicit=bool(accepted["allocation_explicit"]),
        reverts_event_id=int(accept_event_id),
    )
    if existing is not None:
        return int(existing["id"])
    if _live_acceptance(
        conn, int(accepted["subject_transaction_id"])
    ) is None:
        raise ValueError("positive-flow acceptance is already undone")
    current_fingerprint = _undo_fingerprint(conn, accepted)
    if current_fingerprint != evidence_fingerprint:
        raise ValueError(
            "accepted positive-flow state changed; refresh before undoing"
        )

    subject_id = int(accepted["subject_transaction_id"])
    candidate_id = (
        int(accepted["candidate_transaction_id"])
        if accepted["candidate_transaction_id"] is not None
        else None
    )
    request_fingerprint = _request_fingerprint(
        action_kind="undo_acceptance",
        subject_transaction_id=subject_id,
        statement_line_id=int(accepted["statement_line_id"]),
        selected_statement_month=selected,
        proposal_key=str(accepted["proposal_key"]),
        candidate_transaction_id=candidate_id,
        proposed_flow_kind=str(accepted["proposed_flow_kind"]),
        relationship_kind=str(accepted["relationship_kind"]),
        proposed_candidate_flow_kind=str(
            accepted["proposed_candidate_flow_kind"]
        ),
        evidence_fingerprint=evidence_fingerprint,
        actor=actor_text,
        reason=reason_text,
        selected_category_id=(
            int(accepted["selected_category_id"])
            if accepted["selected_category_id"] is not None
            else None
        ),
        allocation_explicit=bool(accepted["allocation_explicit"]),
        reverts_event_id=int(accept_event_id),
    )
    _guard_statement_and_endpoint_months(
        conn,
        selected,
        subject_id,
        *([candidate_id] if candidate_id is not None else []),
    )
    if accepted["accepted_relationship_id"] is not None:
        flows.revoke_relationship(
            conn,
            int(accepted["accepted_relationship_id"]),
            actor=actor_text,
            reason=reason_text,
        )
    _restore_subject_splits(
        conn,
        subject_transaction_id=subject_id,
        accepted_json=str(accepted["accepted_subject_splits_json"]),
        prior_json=str(accepted["prior_subject_splits_json"]),
    )
    flows.set_flow_kind(
        conn,
        subject_id,
        str(accepted["prior_subject_flow_kind"]),
        actor=actor_text,
        reason=reason_text,
    )
    if candidate_id is not None:
        flows.set_flow_kind(
            conn,
            candidate_id,
            str(accepted["prior_candidate_flow_kind"]),
            actor=actor_text,
            reason=reason_text,
        )
    try:
        cursor = conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, relationship_kind,
              candidate_transaction_id, proposed_candidate_flow_kind,
              accepted_relationship_id, prior_subject_flow_kind,
              prior_candidate_flow_kind, selected_category_id,
              allocation_explicit, prior_subject_splits_json,
              accepted_subject_splits_json, evidence_json, actor, reason,
              reverts_event_id
            ) VALUES (
              ?,?,?,?,?,?,?,?,?,'undo',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                _text(operation_key, "operation_key"),
                subject_id,
                int(accepted["statement_line_id"]),
                int(accepted["statement_line_revision"]),
                str(accepted["proposal_key"]),
                evidence_fingerprint,
                "undo_acceptance",
                selected,
                request_fingerprint,
                str(accepted["proposed_flow_kind"]),
                str(accepted["relationship_kind"]),
                candidate_id,
                str(accepted["proposed_candidate_flow_kind"]),
                accepted["accepted_relationship_id"],
                str(accepted["prior_subject_flow_kind"]),
                str(accepted["prior_candidate_flow_kind"]),
                accepted["selected_category_id"],
                int(accepted["allocation_explicit"]),
                str(accepted["prior_subject_splits_json"]),
                str(accepted["accepted_subject_splits_json"]),
                json.dumps(
                    {"accept_event_id": int(accept_event_id)},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_text,
                reason_text,
                int(accept_event_id),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"positive-flow undo failed: {exc}") from exc
    return int(cursor.lastrowid)
