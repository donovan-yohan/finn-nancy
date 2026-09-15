"""statement_lines access — staging, selection, and match mutations.

Staging computes each line's occurrence ordinal (occ) against BOTH already-staged
identical lines and earlier identical lines in the same batch, then dedups on
UNIQUE(account_id, row_hash) so re-uploading the same statement is a no-op.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid

from ..accounting.flows import validate_flow_amount
from ..ingest.normalize import norm_merchant, row_hash
from ..ingest.schemas import ExtractedStatement
from . import repo_documents, repo_jobs


class DocumentAlreadyResolvedError(Exception):
    """resolve_document_account refused: the doc's statement_lines are past the
    account-resolution stage (some line is no longer 'unmatched').

    Resolving moves EVERY line onto the chosen account and recomputes row hashes; doing
    that after reconcile has run would silently relocate lines that are already
    matched/promoted into real transactions (whose transactions.account_id is set once at
    match/promote time and never revisited), leaving statement_lines and transactions
    disagreeing on the account. Callers translate this into a state-conflict response."""

    def __init__(self, source_document_id: int) -> None:
        super().__init__(
            f"statement document {source_document_id} is already resolved; its lines are "
            f"past account resolution (some line is no longer 'unmatched')"
        )
        self.source_document_id = source_document_id


def match_account_by_last4(conn: sqlite3.Connection, parsed: ExtractedStatement) -> int | None:
    """Match a statement to an account by its sanitized last-four digits.

    Never guess — no match beats a wrong match. last4 is LLM output dropped straight into
    a LIKE pattern; strip everything but digits and require exactly 4 of them so stray
    '%'/'_' (SQL wildcards) or other junk can never turn into an unintended wildcard match.
    """
    last4 = re.sub(r"\D", "", parsed.account_last4 or "")[-4:]
    if len(last4) != 4:
        return None
    rows = conn.execute(
        """SELECT id FROM accounts
           WHERE external_ref != '' AND external_ref LIKE ?
           ORDER BY id
           LIMIT 2""",
        (f"%{last4}",),
    ).fetchall()
    return int(rows[0]["id"]) if len(rows) == 1 else None


def stage_lines(conn: sqlite3.Connection, *, source_document_id: int,
                account_id: int | None, parsed: ExtractedStatement,
                row_anchor_ids: list[int | None] | None = None) -> dict:
    """Insert parsed rows as statement_lines. Returns {'staged': n, 'duplicates': n}."""
    staged = dupes = 0
    batch_seen: dict[tuple, int] = {}
    for index, row in enumerate(parsed.rows):
        merchant = norm_merchant(row.description)
        key = (account_id, row.posted_on, row.amount_cents, merchant)
        # occ = ordinal WITHIN this parse only. Restaging the same document and
        # re-exports containing the same rows then reproduce identical hashes and
        # collapse via UNIQUE(account_id, row_hash); counting prior DB rows here
        # would shift occ on every restage and duplicate every line.
        # Known edge: an identical (date, amount, merchant) pair split across two
        # different exports collapses to one line — rare enough to accept.
        occ = batch_seen.get(key, 0)
        # Advance for every input occurrence, including a duplicate already in
        # the database. Otherwise a re-export can keep retrying occurrence 0
        # and silently miss a later identical charge whose tombstone freed
        # occurrence 1.
        batch_seen[key] = occ + 1
        h = row_hash(account_id, row.posted_on, row.amount_cents, row.description, occ)
        anchor_id = (
            row_anchor_ids[index]
            if row_anchor_ids is not None and index < len(row_anchor_ids)
            else None
        )
        field_scores: list[float] = []
        for value in row.field_confidence.values():
            if not isinstance(value, (float, int)):
                continue
            try:
                score = float(value)
            except (OverflowError, TypeError, ValueError):
                score = 0.0
            if not math.isfinite(score):
                score = 0.0
            field_scores.append(min(1.0, max(0.0, score)))
        row_confidence = (
            min(field_scores)
            if field_scores
            else float(parsed.confidence or 0.0)
        )
        if not math.isfinite(row_confidence):
            row_confidence = 0.0
        row_confidence = min(1.0, max(0.0, row_confidence))
        cur = conn.execute(
            """INSERT OR IGNORE INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, balance_cents, is_pending, statement_period, row_hash,
                 flow_kind, source_anchor_id, row_confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'unknown',?,?)""",
            (source_document_id, account_id, row.posted_on, row.description, merchant,
             row.amount_cents, parsed.currency, row.balance_cents, int(row.is_pending),
             parsed.statement_period, h, anchor_id, row_confidence),
        )
        if cur.rowcount:
            staged += 1
        else:
            dupes += 1
    return {"staged": staged, "duplicates": dupes}


def is_exact_duplicate_reexport(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    parsed: ExtractedStatement,
) -> bool:
    """Prove every parsed row already exists for the same account and period.

    This is the narrow no-op case where staging inserted zero rows because the
    account/row-hash uniqueness contract recognized a byte-different re-export.
    The declared closing period must also agree; row hashes intentionally do not
    encode it, so hash equality alone is insufficient.
    """
    if not parsed.rows or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", parsed.statement_period):
        return False
    batch_seen: dict[tuple, int] = {}
    for parsed_row in parsed.rows:
        merchant = norm_merchant(parsed_row.description)
        key = (
            int(account_id),
            parsed_row.posted_on,
            parsed_row.amount_cents,
            merchant,
        )
        occurrence = batch_seen.get(key, 0)
        batch_seen[key] = occurrence + 1
        expected_hash = row_hash(
            int(account_id),
            parsed_row.posted_on,
            parsed_row.amount_cents,
            parsed_row.description,
            occurrence,
        )
        existing = conn.execute(
            """SELECT statement_period
               FROM statement_lines
               WHERE account_id=? AND row_hash=? AND review_disposition='active'""",
            (int(account_id), expected_hash),
        ).fetchone()
        if (
            existing is None
            or str(existing["statement_period"] or "") != parsed.statement_period
        ):
            return False
    return True


def _recompute_row_hashes(conn: sqlite3.Connection, source_document_id: int,
                          account_id: int) -> int:
    """After a doc's lines are moved onto a real account, their row_hash was computed with
    the previous account (None or an auto-resolved guess); recompute against `account_id`,
    replicating stage_lines' per-key occurrence-ordinal logic so a LATER re-staging of the
    same statement under this account reproduces matching hashes instead of silently
    duplicating. If a recomputed hash collides with UNIQUE(account_id, row_hash) — i.e.
    another doc already staged this exact row under this account — this line IS that row
    re-exported; retain it as an audited excluded row instead of physically deleting
    captured evidence.
    """
    lines = conn.execute(
        """SELECT * FROM statement_lines
           WHERE source_document_id=? AND review_disposition='active'
           ORDER BY id""",
        (source_document_id,),
    ).fetchall()
    occ_seen: dict[tuple, int] = {}
    duplicates = 0
    for line in lines:
        key = (account_id, line["posted_on"], line["amount_cents"], line["norm_merchant"])
        occ = occ_seen.get(key, 0)
        occ_seen[key] = occ + 1
        new_hash = row_hash(account_id, line["posted_on"], line["amount_cents"],
                            line["raw_description"], occ)
        try:
            conn.execute("UPDATE statement_lines SET row_hash=? WHERE id=?", (new_hash, line["id"]))
        except sqlite3.IntegrityError:
            existing = conn.execute(
                """SELECT statement_period
                   FROM statement_lines
                   WHERE account_id=? AND row_hash=?
                     AND review_disposition='active'""",
                (account_id, new_hash),
            ).fetchone()
            if (
                existing is None
                or str(existing["statement_period"] or "")
                != str(line["statement_period"] or "")
            ):
                raise ValueError(
                    "duplicate statement row disagrees on declared closing period"
                ) from None
            review = conn.execute(
                "SELECT id FROM statement_reviews WHERE source_document_id=?",
                (int(source_document_id),),
            ).fetchone()
            if review is None:
                raise ValueError(
                    "duplicate statement row cannot be excluded without review evidence"
                ) from None
            operation_key = f"statement:dedupe:{line['id']}:{uuid.uuid4()}"
            conn.execute(
                """INSERT INTO statement_review_audit(
                     operation_key, statement_review_id, statement_line_id,
                     event_kind, old_values_json, new_values_json, actor, reason
                   )
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    operation_key,
                    int(review["id"]),
                    int(line["id"]),
                    "row_excluded",
                    json.dumps(
                        {
                            "review_disposition": "active",
                            "row_hash": line["row_hash"],
                        }
                    ),
                    json.dumps(
                        {
                            "review_disposition": "excluded",
                            "row_hash": (
                                f"excluded:{int(line['id'])}:{line['row_hash']}"
                            ),
                        }
                    ),
                    "statement:dedupe",
                    "exact duplicate row retained as excluded evidence",
                ),
            )
            conn.execute(
                """UPDATE statement_lines
                   SET review_disposition='excluded',
                       row_hash=?,
                       review_revision=review_revision+1,
                       review_operation_key=?
                   WHERE id=?""",
                (
                    f"excluded:{int(line['id'])}:{line['row_hash']}",
                    operation_key,
                    int(line["id"]),
                ),
            )
            duplicates += 1
    return duplicates


def resolve_document_account(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    account_id: int,
    enqueue_reconciliation: bool = True,
) -> dict:
    """Move every staged line of a statement doc onto `account_id` in place, recompute its
    row hashes, mark the doc processed, and enqueue reconciliation.

    Shared by the two surfaces that resolve a statement's account, so both converge on the
    identical statement_lines state:
      * /review approve-statement — the triage-queue entry point, where a human first
        assigns or creates the account for a doc that landed in the review inbox.
      * /recon assign-account — the reconciliation workspace, where an already-staged
        account-less doc gets its account resolved.
    Both MUST update in place rather than DELETE + re-INSERT: a checksum_mismatch doc whose
    lines were already staged under an auto-resolved account would otherwise be double-booked
    (the prior rows survive because they aren't account-less, and a second full set is
    inserted under the chosen account). Updating every line — regardless of its current
    account_id — then recomputing hashes keeps exactly one set of lines under the doc.

    Idempotency/scope guard: refuse if ANY of the doc's lines has already moved past
    'unmatched' (i.e. reconcile has run and matched/promoted/ignored/queued them). A repeat
    resolve (double submit, browser resubmit, direct POST) would otherwise relocate lines
    already tied to real transactions — see DocumentAlreadyResolvedError. Docs entering via
    /review approve-statement are always still all-unmatched (needs_review statement docs
    never enqueue reconcile), so this never fires on the review path.
    """
    resolved = conn.execute(
        """SELECT 1 FROM statement_lines
           WHERE source_document_id=? AND review_disposition='active'
             AND match_status!='unmatched'
           LIMIT 1""",
        (source_document_id,),
    ).fetchone()
    if resolved is not None:
        raise DocumentAlreadyResolvedError(source_document_id)
    line_count = int(
        conn.execute(
            """SELECT COUNT(*) FROM statement_lines
               WHERE source_document_id=? AND review_disposition='active'""",
            (source_document_id,),
        ).fetchone()[0]
    )
    conn.execute(
        "UPDATE statement_lines SET account_id=? WHERE source_document_id=?",
        (account_id, source_document_id),
    )
    duplicates = _recompute_row_hashes(conn, source_document_id, account_id)
    if enqueue_reconciliation:
        repo_documents.set_status(conn, source_document_id, "processed")
        repo_jobs.enqueue(
            conn,
            "reconcile_document",
            {"source_document_id": source_document_id},
            source_document_id=source_document_id,
        )
    return {
        "lines": line_count - duplicates,
        "duplicates": duplicates,
    }


def lines_for_document(conn: sqlite3.Connection, source_document_id: int,
                       match_status: str | None = None,
                       include_excluded: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM statement_lines WHERE source_document_id=?"
    args: list = [source_document_id]
    if not include_excluded:
        sql += " AND review_disposition='active'"
    if match_status:
        sql += " AND match_status=?"
        args.append(match_status)
    return conn.execute(sql + " ORDER BY posted_on, id", args).fetchall()


def set_flow_kind(conn: sqlite3.Connection, line_id: int, flow_kind: str) -> None:
    row = conn.execute(
        "SELECT amount_cents FROM statement_lines WHERE id=?",
        (int(line_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown statement_line_id: {line_id}")
    flow = validate_flow_amount(flow_kind, int(row["amount_cents"]))
    conn.execute(
        "UPDATE statement_lines SET flow_kind=? WHERE id=?",
        (flow.value, int(line_id)),
    )


def set_match(conn: sqlite3.Connection, line_id: int, *, status: str, method: str = "",
              transaction_id: int | None = None, score: float = 0.0,
              rationale: str = "") -> None:
    row = conn.execute(
        "SELECT review_disposition FROM statement_lines WHERE id=?",
        (int(line_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown statement_line_id: {line_id}")
    if row["review_disposition"] != "active":
        raise ValueError("excluded statement rows cannot be reconciled")
    conn.execute(
        """UPDATE statement_lines
           SET match_status=?, match_method=?, matched_transaction_id=?, match_score=?,
               match_rationale=?
           WHERE id=?""",
        (status, method, transaction_id, score, rationale[:500], line_id),
    )


def mark_cleared(conn: sqlite3.Connection, transaction_id: int, cleared_on: str,
                 account_id: int | None = None) -> None:
    """Overlay reconciliation state on the ledger row; optionally resolve its account."""
    conn.execute(
        "UPDATE transactions SET recon_status='cleared', cleared_on=? WHERE id=?",
        (cleared_on, transaction_id),
    )
    if account_id is not None:
        conn.execute("UPDATE transactions SET account_id=? WHERE id=?",
                     (account_id, transaction_id))


def review_queue(
    conn: sqlite3.Connection, *, month: str | None = None
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT sl.*, sd.original_name FROM statement_lines sl
           JOIN source_documents sd ON sd.id = sl.source_document_id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=sl.source_document_id
            AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE sl.match_status='needs_review'
             AND sl.review_disposition='active'
             AND (? IS NULL OR COALESCE(
               expectation.period_month, sl.statement_period
             ) = ?)
           ORDER BY sl.posted_on DESC, sl.id""",
        (month, month),
    ).fetchall()
