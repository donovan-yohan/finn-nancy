"""Typed flow semantics and auditable transaction relationships.

Purpose categories answer "what was this for?". ``flow_kind`` answers "what
movement happened?". This module is the single production validation path for
creating/changing those semantics; SQLite constraints and triggers remain the
last line of defence for direct or future writers.
"""
from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Mapping

from .contract import FlowKind


class RelationshipKind(StrEnum):
    TRANSFER_PAIR = "transfer_pair"
    REFUND_OF = "refund_of"
    REIMBURSEMENT_FOR = "reimbursement_for"
    PAYMENT_FOR = "payment_for"
    REVERSAL_OF = "reversal_of"


def normalize_flow_kind(value: FlowKind | str) -> FlowKind:
    try:
        return value if isinstance(value, FlowKind) else FlowKind(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in FlowKind)
        raise ValueError(f"invalid flow_kind {value!r}; expected one of: {allowed}") from exc


def normalize_relationship_kind(value: RelationshipKind | str) -> RelationshipKind:
    try:
        return value if isinstance(value, RelationshipKind) else RelationshipKind(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in RelationshipKind)
        raise ValueError(
            f"invalid relationship_kind {value!r}; expected one of: {allowed}"
        ) from exc


def validate_flow_amount(flow_kind: FlowKind | str, amount_cents: int) -> FlowKind:
    """Validate only semantics that are intrinsic to one transaction leg."""
    flow = normalize_flow_kind(flow_kind)
    amount = int(amount_cents)
    if amount == 0:
        raise ValueError("amount_cents must be non-zero")
    if flow in (FlowKind.PURCHASE, FlowKind.FEE) and amount >= 0:
        raise ValueError(f"{flow.value} requires a negative amount_cents")
    if flow in (
        FlowKind.INCOME,
        FlowKind.INTEREST,
        FlowKind.REFUND,
        FlowKind.REIMBURSEMENT,
        FlowKind.REVERSAL,
    ) and amount <= 0:
        raise ValueError(f"{flow.value} requires a positive amount_cents")
    return flow


def _require_text(value: str, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required for auditability")
    return text


def _transaction(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT id, account_id, amount_cents, flow_kind
           FROM transactions WHERE id=?""",
        (int(transaction_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown transaction_id: {transaction_id}")
    return row


def _relationship_error(
    kind: RelationshipKind,
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> str | None:
    source_id = int(source["id"])
    target_id = int(target["id"])
    if source_id == target_id:
        return "a transaction cannot relate to itself"

    source_flow = normalize_flow_kind(str(source["flow_kind"]))
    target_flow = normalize_flow_kind(str(target["flow_kind"]))
    source_amount = int(source["amount_cents"])
    target_amount = int(target["amount_cents"])

    if kind == RelationshipKind.TRANSFER_PAIR:
        if (
            source_flow != target_flow
            or source_flow not in (FlowKind.INTERNAL_TRANSFER, FlowKind.CARD_PAYMENT)
            or int(source["account_id"]) == int(target["account_id"])
            or source_amount >= 0
            or target_amount <= 0
            or source_amount + target_amount != 0
        ):
            return (
                "transfer_pair requires equal-and-opposite internal_transfer or "
                "card_payment legs on two owned accounts"
            )
    elif kind == RelationshipKind.REFUND_OF:
        if not (
            source_flow == FlowKind.REFUND
            and target_flow in (FlowKind.PURCHASE, FlowKind.FEE)
            and source_amount > 0
            and target_amount < 0
            and source_amount <= -target_amount
        ):
            return "refund_of requires a positive refund no larger than its purchase/fee"
    elif kind == RelationshipKind.REIMBURSEMENT_FOR:
        if not (
            source_flow == FlowKind.REIMBURSEMENT
            and target_flow in (FlowKind.PURCHASE, FlowKind.FEE)
            and source_amount > 0
            and target_amount < 0
            and source_amount <= -target_amount
        ):
            return (
                "reimbursement_for requires a positive reimbursement no larger "
                "than its purchase/fee"
            )
    elif kind == RelationshipKind.PAYMENT_FOR:
        if not (
            source_flow == FlowKind.CARD_PAYMENT
            and target_flow in (FlowKind.PURCHASE, FlowKind.FEE)
        ):
            return "payment_for requires a card_payment source and purchase/fee target"
    elif kind == RelationshipKind.REVERSAL_OF:
        if not (
            source_flow == FlowKind.REVERSAL
            and source_amount > 0
            and target_flow in (FlowKind.PURCHASE, FlowKind.FEE)
            and target_amount < 0
            and source_amount + target_amount == 0
        ):
            return "reversal_of requires a positive reversal of one purchase/fee"
    return None


def _relationship_set_error(
    conn: sqlite3.Connection,
    kind: RelationshipKind,
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> str | None:
    source_id = int(source["id"])
    target_id = int(target["id"])
    if kind == RelationshipKind.TRANSFER_PAIR:
        conflict = conn.execute(
            """SELECT 1
               FROM transaction_relationships
               WHERE status='active'
                 AND relationship_kind='transfer_pair'
                 AND (
                   source_transaction_id IN (?, ?)
                   OR target_transaction_id IN (?, ?)
                 )
               LIMIT 1""",
            (source_id, target_id, source_id, target_id),
        ).fetchone()
        if conflict is not None:
            return "each transfer/card-payment leg can belong to only one active pair"

    if kind in (
        RelationshipKind.REFUND_OF,
        RelationshipKind.REIMBURSEMENT_FOR,
    ):
        reversal = conn.execute(
            """SELECT 1
               FROM transaction_relationships
               WHERE status='active' AND relationship_kind='reversal_of'
                 AND target_transaction_id=?
               LIMIT 1""",
            (target_id,),
        ).fetchone()
        if reversal is not None:
            return "a reversed purchase/fee cannot also receive partial offsets"
        existing_total = int(
            conn.execute(
                """SELECT COALESCE(SUM(source.amount_cents), 0)
                   FROM transaction_relationships relationship
                   JOIN transactions source
                     ON source.id=relationship.source_transaction_id
                   WHERE relationship.status='active'
                     AND relationship.relationship_kind IN (
                       'refund_of', 'reimbursement_for'
                     )
                     AND relationship.target_transaction_id=?""",
                (target_id,),
            ).fetchone()[0]
        )
        if existing_total + int(source["amount_cents"]) > -int(target["amount_cents"]):
            return "aggregate refunds/reimbursements exceed the purchase/fee amount"

    if kind == RelationshipKind.REVERSAL_OF:
        prior_offset = conn.execute(
            """SELECT 1
               FROM transaction_relationships
               WHERE status='active'
                 AND relationship_kind IN (
                   'refund_of', 'reimbursement_for', 'reversal_of'
                 )
                 AND target_transaction_id=?
               LIMIT 1""",
            (target_id,),
        ).fetchone()
        if prior_offset is not None:
            return "reversal target must be exclusive and can be reversed only once"
    return None


def create_relationship(
    conn: sqlite3.Connection,
    *,
    relationship_kind: RelationshipKind | str,
    source_transaction_id: int,
    target_transaction_id: int,
    actor: str,
    reason: str,
) -> int:
    """Create one validated active edge or fail before changing the database."""
    kind = normalize_relationship_kind(relationship_kind)
    source = _transaction(conn, source_transaction_id)
    target = _transaction(conn, target_transaction_id)
    error = _relationship_error(kind, source, target)
    if error is None:
        error = _relationship_set_error(conn, kind, source, target)
    if error is not None:
        raise ValueError(error)
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    try:
        cur = conn.execute(
            """INSERT INTO transaction_relationships(
                 relationship_kind, source_transaction_id, target_transaction_id,
                 created_by, reason)
               VALUES (?,?,?,?,?)""",
            (kind.value, int(source["id"]), int(target["id"]), actor, reason),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"relationship rejected: {exc}") from exc
    return int(cur.lastrowid)


def revoke_relationship(
    conn: sqlite3.Connection,
    relationship_id: int,
    *,
    actor: str,
    reason: str,
) -> None:
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    row = conn.execute(
        "SELECT status FROM transaction_relationships WHERE id=?",
        (int(relationship_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown relationship_id: {relationship_id}")
    if row["status"] != "active":
        raise ValueError("only an active relationship can be revoked")
    conn.execute(
        """UPDATE transaction_relationships
           SET status='revoked', revoked_at=CURRENT_TIMESTAMP,
               revoked_by=?, revocation_reason=?
           WHERE id=?""",
        (actor, reason, int(relationship_id)),
    )


def set_flow_kind(
    conn: sqlite3.Connection,
    transaction_id: int,
    flow_kind: FlowKind | str,
    *,
    actor: str,
    reason: str,
) -> bool:
    """Change a flow classification, preserving a durable audit record.

    Active relationship edges are revalidated against the proposed tag before
    mutation so a reclassification cannot make an existing edge impossible.
    """
    row = _transaction(conn, transaction_id)
    flow = validate_flow_amount(flow_kind, int(row["amount_cents"]))
    old_flow = normalize_flow_kind(str(row["flow_kind"]))
    if flow == old_flow:
        return False
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")

    proposed = dict(row)
    proposed["flow_kind"] = flow.value
    relationships = conn.execute(
        """SELECT *
           FROM transaction_relationships
           WHERE status='active'
             AND (source_transaction_id=? OR target_transaction_id=?)""",
        (int(transaction_id), int(transaction_id)),
    ).fetchall()
    for relationship in relationships:
        source = (
            proposed
            if int(relationship["source_transaction_id"]) == int(transaction_id)
            else dict(_transaction(conn, int(relationship["source_transaction_id"])))
        )
        target = (
            proposed
            if int(relationship["target_transaction_id"]) == int(transaction_id)
            else dict(_transaction(conn, int(relationship["target_transaction_id"])))
        )
        error = _relationship_error(
            normalize_relationship_kind(relationship["relationship_kind"]),
            source,
            target,
        )
        if error is not None:
            raise ValueError(f"flow_kind would invalidate relationship {relationship['id']}: {error}")

    conn.execute(
        "UPDATE transactions SET flow_kind=? WHERE id=?",
        (flow.value, int(transaction_id)),
    )
    conn.execute(
        """INSERT INTO transaction_flow_audit(
             transaction_id, old_flow_kind, new_flow_kind, actor, reason)
           VALUES (?,?,?,?,?)""",
        (int(transaction_id), old_flow.value, flow.value, actor, reason),
    )
    if flow == FlowKind.UNKNOWN:
        conn.execute(
            """UPDATE transaction_flow_reviews
               SET reason=?
               WHERE transaction_id=? AND status='pending'""",
            (reason, int(transaction_id)),
        )
    else:
        conn.execute(
            """UPDATE transaction_flow_reviews
               SET resolved_by=?
               WHERE transaction_id=? AND status='resolved'""",
            (actor, int(transaction_id)),
        )
    return True


def backfill_deterministic_flow_kinds(conn: sqlite3.Connection) -> int:
    """Repeatably classify only rows supported by an existing writer contract."""
    rows = conn.execute(
        """SELECT transaction_row.id, transaction_row.source,
                  transaction_row.amount_cents
           FROM transactions transaction_row
           JOIN accounts account ON account.id=transaction_row.account_id
           WHERE transaction_row.flow_kind='unknown'
             AND (
               (
                 transaction_row.source='receipt'
                 AND transaction_row.amount_cents < 0
                 AND UPPER(TRIM(account.currency))='CAD'
               )
               OR transaction_row.source='opening'
               OR transaction_row.source='adjustment'
             )
           ORDER BY transaction_row.id"""
    ).fetchall()
    for row in rows:
        flow = {
            "receipt": FlowKind.PURCHASE,
            "opening": FlowKind.OPENING,
            "adjustment": FlowKind.ADJUSTMENT,
        }[row["source"]]
        set_flow_kind(
            conn,
            int(row["id"]),
            flow,
            actor="migration:028",
            reason="deterministic source-contract backfill",
        )
    return len(rows)


def pending_flow_reviews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT review.*, txn.posted_on, txn.description, txn.amount_cents,
                  txn.account_id
           FROM transaction_flow_reviews review
           JOIN transactions txn ON txn.id=review.transaction_id
           WHERE review.status='pending'
           ORDER BY txn.posted_on, txn.id"""
    ).fetchall()
