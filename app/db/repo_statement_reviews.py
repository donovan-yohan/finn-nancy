"""Canonical statement metadata, source evidence, and editable row review.

The extracted claim is immutable evidence.  ``statement_reviews`` and
``statement_lines`` are current projections whose changes are coupled to
append-only audit rows by migration 031.  Row removal is a tombstone
(``review_disposition='excluded'``), never a physical delete.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

import fitz

from ..accounting.contract import currency_review_reason
from ..config import get_settings
from ..ingest.normalize import norm_merchant, row_hash
from ..ingest.schemas import ExtractedStatement
from . import (
    repo_assertions,
    repo_close,
    repo_documents,
    repo_jobs,
    repo_statement_expectations,
    repo_statements,
)

AUTO_FIELD_CONFIDENCE = 0.85
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_ISO_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUIRED_METADATA_FIELDS = (
    "period_start_on",
    "period_end_on",
    "statement_issued_on",
    "opening_balance_cents",
    "closing_balance_cents",
    "currency",
    "account_fingerprint",
)


@dataclass(frozen=True)
class ReviewEnvelope:
    review: sqlite3.Row
    page_anchor_ids: dict[int, int]


def _text(value: str | None, field: str) -> str:
    result = (value or "").strip()
    if not result:
        raise ValueError(f"{field} is required for auditability")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _operation(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def _valid_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if not _ISO_DATE.fullmatch(raw):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return raw if parsed.isoformat() == raw else None


def _period_month(parsed: ExtractedStatement) -> str | None:
    declared = (parsed.statement_period or "").strip()
    end = _valid_date(parsed.period_end_on)
    if end is not None:
        end_month = end[:7]
        if declared and declared != end_month:
            return None
        return end_month
    return declared if _ISO_MONTH.fullmatch(declared) else None


def _positive_count(value: int | None) -> int | None:
    """Normalize untrusted extracted counts into reviewable nullable claims."""
    if value is None:
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= _MAX_SQLITE_INTEGER else None


def _nonnegative_count(value: int | None) -> int | None:
    """Normalize untrusted extracted counts into reviewable nullable claims."""
    if value is None:
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= _MAX_SQLITE_INTEGER else None


def _confidence(value: float | int | None) -> float:
    """Clamp an untrusted score; non-finite values always force review."""
    try:
        parsed = float(value or 0.0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return min(1.0, max(0.0, parsed))


def account_fingerprint(parsed: ExtractedStatement) -> str:
    """Return a privacy-safe v1 digest; never persist a full account number."""
    digits = re.sub(r"\D", "", parsed.account_last4 or "")
    if len(digits) != 4:
        return ""
    material = "\0".join(
        (
            "v1",
            " ".join((parsed.institution or "").casefold().split()),
            " ".join((parsed.account_hint or "").casefold().split()),
            digits,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _page_digests(raw: bytes, observed_pages: int) -> list[str]:
    if observed_pages <= 0:
        return []
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            return [
                hashlib.sha256(
                    document.load_page(index).get_pixmap(dpi=72).tobytes("png")
                ).hexdigest()
                for index in range(document.page_count)
            ]
    except Exception:
        # The source digest still binds the evidence.  This fallback is stable and
        # deliberately cannot make completeness pass without matching page count.
        return [
            hashlib.sha256(raw + f"\0page:{number}".encode()).hexdigest()
            for number in range(1, observed_pages + 1)
        ]


def get_for_document(
    conn: sqlite3.Connection, source_document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM statement_reviews WHERE source_document_id=?",
        (int(source_document_id),),
    ).fetchone()


def get_review(conn: sqlite3.Connection, review_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM statement_reviews WHERE id=?", (int(review_id),)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown statement_review_id: {review_id}")
    return row


def _page_anchor_ids(
    conn: sqlite3.Connection, review_id: int
) -> dict[int, int]:
    return {
        int(row["page_number"]): int(row["anchor_id"])
        for row in conn.execute(
            """SELECT page.page_number, anchor.id AS anchor_id
               FROM statement_review_pages page
               JOIN statement_source_anchors anchor
                 ON anchor.page_id=page.id
                AND anchor.locator_kind='page'
               WHERE page.statement_review_id=?
               ORDER BY page.page_number, anchor.id""",
            (int(review_id),),
        )
    }


def _field_values(parsed: ExtractedStatement) -> dict[str, Any]:
    return {
        "institution": parsed.institution,
        "account_hint": parsed.account_hint,
        "account_last4": parsed.account_last4,
        "account_fingerprint": account_fingerprint(parsed),
        "period_month": parsed.statement_period,
        "period_start_on": parsed.period_start_on,
        "period_end_on": parsed.period_end_on,
        "statement_issued_on": parsed.statement_issued_on,
        "opening_balance_cents": parsed.opening_balance_cents,
        "closing_balance_cents": parsed.closing_balance_cents,
        "currency": parsed.currency,
        "declared_page_count": parsed.declared_page_count,
        "declared_row_count": parsed.declared_row_count,
        "zero_activity": bool(parsed.zero_activity),
    }


def create_from_extraction(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    extraction_id: int,
    account_id: int | None,
    parsed: ExtractedStatement,
    raw: bytes,
    actor: str,
) -> ReviewEnvelope:
    """Create the immutable evidence envelope for one statement extraction."""
    existing = get_for_document(conn, source_document_id)
    if existing is not None:
        return ReviewEnvelope(existing, _page_anchor_ids(conn, int(existing["id"])))

    document = repo_documents.get_document(conn, int(source_document_id))
    if document is None or document["kind"] != "statement":
        raise ValueError("statement review requires a statement source document")
    actor = _text(actor, "actor")
    source_sha = str(document["sha256"] or "")
    if len(source_sha) != 64:
        source_sha = hashlib.sha256(raw).hexdigest()
    observed_pages = max(0, int(parsed.observed_page_count or 0))
    extracted_pages = max(
        0, min(observed_pages, int(parsed.extracted_page_count or 0))
    )
    period_month = _period_month(parsed)
    fingerprint = account_fingerprint(parsed)
    activity_kind = (
        "zero_activity"
        if parsed.zero_activity
        else ("transactions" if parsed.rows else "unknown")
    )
    cur = conn.execute(
        """INSERT INTO statement_reviews(
             source_document_id, extraction_id, account_id,
             period_start_on, period_end_on, statement_issued_on, period_month,
             opening_balance_cents, closing_balance_cents, currency,
             account_fingerprint, activity_kind,
             declared_page_count, declared_row_count,
             observed_page_count, extracted_page_count, extraction_truncated
           )
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(source_document_id),
            int(extraction_id),
            account_id,
            _valid_date(parsed.period_start_on),
            _valid_date(parsed.period_end_on),
            _valid_date(parsed.statement_issued_on),
            period_month,
            parsed.opening_balance_cents,
            parsed.closing_balance_cents,
            (parsed.currency or "").strip().upper(),
            fingerprint,
            activity_kind,
            _positive_count(parsed.declared_page_count),
            _nonnegative_count(parsed.declared_row_count),
            observed_pages,
            extracted_pages,
            int(bool(parsed.extraction_truncated)),
        ),
    )
    review_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO statement_review_audit(
             operation_key, statement_review_id, event_kind, actor, reason
           )
           VALUES (?,?,?,?,?)""",
        (
            f"review:created:{review_id}",
            review_id,
            "review_created",
            actor,
            "statement extraction evidence created",
        ),
    )

    page_ids: dict[int, int] = {}
    anchors: dict[int, int] = {}
    digests = _page_digests(raw, observed_pages)
    for page_number, page_sha in enumerate(digests, start=1):
        page = conn.execute(
            """INSERT INTO statement_review_pages(
                 statement_review_id, page_number, source_sha256, page_sha256,
                 included_in_extraction
               )
               VALUES (?,?,?,?,?)""",
            (
                review_id,
                page_number,
                source_sha,
                page_sha,
                int(page_number <= extracted_pages),
            ),
        )
        page_id = int(page.lastrowid)
        page_ids[page_number] = page_id
        anchor = conn.execute(
            """INSERT INTO statement_source_anchors(
                 statement_review_id, page_id, locator_kind, locator_json,
                 source_sha256, created_by
               )
               VALUES (?,?,'page',?,?,?)""",
            (
                review_id,
                page_id,
                _json({"page": page_number}),
                source_sha,
                actor,
            ),
        )
        anchors[page_number] = int(anchor.lastrowid)

    for field_name, value in _field_values(parsed).items():
        page_number = int(parsed.field_pages.get(field_name, 0) or 0)
        if page_number == 0 and observed_pages == 1:
            page_number = 1
        anchor_id = anchors.get(page_number)
        confidence = _confidence(
            parsed.field_confidence.get(field_name, parsed.confidence or 0.0)
        )
        conn.execute(
            """INSERT INTO statement_field_evidence(
                 evidence_key, statement_review_id, extraction_id, field_name,
                 original_value_json, confidence, source_anchor_id, origin
               )
               VALUES (?,?,?,?,?,?,?,'extractor')""",
            (
                f"extraction:{extraction_id}:metadata:{field_name}",
                review_id,
                int(extraction_id),
                field_name,
                _json(value),
                confidence,
                anchor_id,
            ),
        )

    review = get_review(conn, review_id)
    return ReviewEnvelope(review, anchors)


def row_anchor_ids(
    parsed: ExtractedStatement, anchors: dict[int, int]
) -> list[int | None]:
    one_page = next(iter(anchors.values())) if len(anchors) == 1 else None
    return [
        anchors.get(int(row.page_number or 0), one_page)
        for row in parsed.rows
    ]


def record_row_evidence(
    conn: sqlite3.Connection,
    *,
    review_id: int,
    extraction_id: int,
    parsed: ExtractedStatement,
    account_id: int | None,
    anchors: dict[int, int],
) -> None:
    """Bind immutable extracted row claims to staged rows when they were inserted."""
    occurrence: dict[tuple[Any, ...], int] = {}
    one_page = next(iter(anchors.values())) if len(anchors) == 1 else None
    for index, parsed_row in enumerate(parsed.rows):
        merchant = norm_merchant(parsed_row.description)
        key = (
            account_id,
            parsed_row.posted_on,
            parsed_row.amount_cents,
            merchant,
        )
        ordinal = occurrence.get(key, 0)
        occurrence[key] = ordinal + 1
        expected_hash = row_hash(
            account_id,
            parsed_row.posted_on,
            parsed_row.amount_cents,
            parsed_row.description,
            ordinal,
        )
        line = conn.execute(
            """SELECT id FROM statement_lines
               WHERE source_document_id=? AND row_hash=?
               ORDER BY id LIMIT 1""",
            (
                int(get_review(conn, review_id)["source_document_id"]),
                expected_hash,
            ),
        ).fetchone()
        if line is None:
            continue
        scores = [
            _confidence(value)
            for value in parsed_row.field_confidence.values()
            if isinstance(value, (int, float))
        ]
        confidence = (
            min(scores) if scores else _confidence(parsed.confidence)
        )
        anchor_id = anchors.get(int(parsed_row.page_number or 0), one_page)
        conn.execute(
            """INSERT OR IGNORE INTO statement_field_evidence(
                 evidence_key, statement_review_id, statement_line_id,
                 extraction_id, field_name, original_value_json, confidence,
                 source_anchor_id, origin
               )
               VALUES (?,?,?,?,?,?,?,?, 'extractor')""",
            (
                f"extraction:{extraction_id}:row:{index}",
                int(review_id),
                int(line["id"]),
                int(extraction_id),
                "row_snapshot",
                parsed_row.model_dump_json(),
                confidence,
                anchor_id,
            ),
        )


def _audit(
    conn: sqlite3.Connection,
    *,
    review_id: int,
    event_kind: str,
    actor: str,
    reason: str,
    old: Any,
    new: Any,
    line_id: int | None = None,
    operation_key: str | None = None,
) -> str:
    key = operation_key or _operation(f"review:{event_kind}")
    conn.execute(
        """INSERT INTO statement_review_audit(
             operation_key, statement_review_id, statement_line_id,
             event_kind, old_values_json, new_values_json, actor, reason
           )
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            key,
            int(review_id),
            line_id,
            event_kind,
            _json(old),
            _json(new),
            _text(actor, "actor"),
            _text(reason, "reason"),
        ),
    )
    return key


def _assert_revision(row: sqlite3.Row, expected_revision: int) -> None:
    if int(row["revision"]) != int(expected_revision):
        raise ValueError("statement review changed; reload before saving")


def _assert_line_revision(row: sqlite3.Row, expected_revision: int) -> None:
    if int(row["review_revision"]) != int(expected_revision):
        raise ValueError("statement row changed; reload before saving")


def _assert_document_unresolved(conn: sqlite3.Connection, source_document_id: int) -> None:
    resolved = conn.execute(
        """SELECT 1 FROM statement_lines
           WHERE source_document_id=?
             AND review_disposition='active'
             AND (
               match_status NOT IN ('unmatched', 'needs_review')
               OR matched_transaction_id IS NOT NULL
             )
           LIMIT 1""",
        (int(source_document_id),),
    ).fetchone()
    if resolved is not None:
        raise ValueError(
            "unreconcile the statement before changing reviewed row evidence"
        )


def _guard_review_month(conn: sqlite3.Connection, review: sqlite3.Row) -> None:
    month = str(review["period_month"] or "")
    if month and repo_close.is_month_locked(conn, month):
        raise repo_close.MonthLockedError(month)


def _reopen_projection_if_needed(
    conn: sqlite3.Connection,
    review: sqlite3.Row,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    if review["review_state"] not in {"approved", "approved_with_override"}:
        return review
    repo_statement_expectations.reopen_document_review(
        conn,
        int(review["source_document_id"]),
        actor=actor,
        reason=reason,
    )
    key = _audit(
        conn,
        review_id=int(review["id"]),
        event_kind="review_reopened",
        actor=actor,
        reason=reason,
        old={"review_state": review["review_state"]},
        new={"review_state": "pending"},
    )
    conn.execute(
        """UPDATE statement_reviews
           SET review_state='pending', reviewed_at=NULL, reviewed_by=NULL,
               override_reason=NULL, revision=revision+1,
               last_operation_key=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (key, int(review["id"])),
    )
    repo_documents.set_status(
        conn, int(review["source_document_id"]), "needs_review"
    )
    return get_review(conn, int(review["id"]))


def _sync_expectation_identity(
    conn: sqlite3.Connection,
    review: sqlite3.Row,
    *,
    actor: str,
    reason: str,
) -> None:
    """Keep the account-period evidence link aligned with active reviewed rows."""
    source_document_id = int(review["source_document_id"])
    if (
        repo_statement_expectations.exact_document_identity(
            conn, source_document_id
        )
        is None
    ):
        repo_statement_expectations.detach_document_source(
            conn,
            source_document_id,
            actor=actor,
            reason=reason,
        )
        return
    repo_statement_expectations.attach_exact_document(
        conn,
        source_document_id,
        actor=actor,
        reason=reason,
    )


def _anchor_for_page(
    conn: sqlite3.Connection, review_id: int, page_number: int
) -> int:
    review = get_review(conn, review_id)
    if str(review["source_kind"]) == "structured_rows":
        anchor = conn.execute(
            """SELECT id FROM statement_source_anchors
               WHERE statement_review_id=? AND locator_kind='raw_row'
               ORDER BY id
               LIMIT 1 OFFSET ?""",
            (int(review_id), max(0, int(page_number) - 1)),
        ).fetchone()
        if anchor is None:
            raise ValueError(
                "source anchor must identify an imported statement row"
            )
        return int(anchor["id"])
    anchors = _page_anchor_ids(conn, review_id)
    anchor_id = anchors.get(int(page_number))
    if anchor_id is None:
        raise ValueError("source page must identify an observed statement page")
    return anchor_id


def update_metadata(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    expected_revision: int,
    actor: str,
    reason: str,
    source_page: int,
    values: dict[str, Any],
) -> sqlite3.Row:
    review = get_review(conn, review_id)
    _assert_revision(review, expected_revision)
    _guard_review_month(conn, review)
    _assert_document_unresolved(conn, int(review["source_document_id"]))
    review = _reopen_projection_if_needed(
        conn, review, actor=actor, reason=reason
    )
    anchor_id = _anchor_for_page(conn, int(review["id"]), source_page)
    allowed = {
        "account_id",
        "period_start_on",
        "period_end_on",
        "statement_issued_on",
        "opening_balance_cents",
        "closing_balance_cents",
        "currency",
        "account_fingerprint",
        "activity_kind",
        "declared_page_count",
        "declared_row_count",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported statement metadata: {sorted(unknown)}")

    normalized: dict[str, Any] = {}
    for field, value in values.items():
        if field in {"period_start_on", "period_end_on", "statement_issued_on"}:
            normalized[field] = _valid_date(str(value or ""))
            if str(value or "").strip() and normalized[field] is None:
                raise ValueError(f"{field} must be YYYY-MM-DD")
        elif field in {
            "opening_balance_cents",
            "closing_balance_cents",
            "declared_page_count",
            "declared_row_count",
            "account_id",
        }:
            normalized[field] = None if value in (None, "") else int(value)
        elif field == "currency":
            normalized[field] = str(value or "").strip().upper()
        elif field == "activity_kind":
            activity = str(value or "")
            if activity not in {"unknown", "transactions", "zero_activity"}:
                raise ValueError("invalid statement activity kind")
            normalized[field] = activity
        else:
            normalized[field] = str(value or "").strip()
    if (
        normalized.get("declared_page_count") is not None
        and int(normalized["declared_page_count"]) <= 0
    ):
        raise ValueError("declared page count must be positive")
    if (
        normalized.get("declared_row_count") is not None
        and int(normalized["declared_row_count"]) < 0
    ):
        raise ValueError("declared row count must not be negative")

    if "account_id" in normalized and normalized["account_id"] is None:
        raise ValueError("account is required once statement identity is resolved")
    if normalized.get("account_id") is not None:
        account = conn.execute(
            "SELECT id FROM accounts WHERE id=?",
            (int(normalized["account_id"]),),
        ).fetchone()
        if account is None:
            raise ValueError("invalid account")
    start = normalized.get("period_start_on", review["period_start_on"])
    end = normalized.get("period_end_on", review["period_end_on"])
    if start is not None and end is not None and str(start) > str(end):
        raise ValueError("statement period start must not follow period end")
    normalized["period_month"] = str(end)[:7] if end else review["period_month"]
    identity_changed = (
        (
            "account_id" in normalized
            and normalized["account_id"] != review["account_id"]
        )
        or normalized["period_month"] != review["period_month"]
    )
    if identity_changed:
        repo_statement_expectations.detach_document_source(
            conn,
            int(review["source_document_id"]),
            actor=actor,
            reason="statement account or closing period was corrected",
        )

    old_values = {field: review[field] for field in normalized}
    key = _audit(
        conn,
        review_id=int(review["id"]),
        event_kind=(
            "account_resolved" if "account_id" in normalized else "metadata_corrected"
        ),
        actor=actor,
        reason=reason,
        old=old_values,
        new=normalized,
    )
    assignments = ", ".join(f"{field}=?" for field in normalized)
    conn.execute(
        f"""UPDATE statement_reviews
            SET {assignments}, revision=revision+1, last_operation_key=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (*normalized.values(), key, int(review["id"])),
    )
    updated = get_review(conn, int(review["id"]))

    if "account_id" in normalized:
        repo_statements.resolve_document_account(
            conn,
            source_document_id=int(updated["source_document_id"]),
            account_id=int(normalized["account_id"]),
            enqueue_reconciliation=False,
        )
    if "period_month" in normalized:
        conn.execute(
            """UPDATE statement_lines SET statement_period=?
               WHERE source_document_id=?""",
            (
                updated["period_month"] or "",
                int(updated["source_document_id"]),
            ),
        )

    extraction_id = updated["extraction_id"]
    for field, value in normalized.items():
        conn.execute(
            """INSERT INTO statement_field_evidence(
                 evidence_key, statement_review_id, extraction_id, field_name,
                 original_value_json, confidence, source_anchor_id, origin
               )
               VALUES (?,?,?,?,?,1.0,?,'manual')""",
            (
                _operation(f"manual:{review_id}:{field}"),
                int(review_id),
                extraction_id,
                field,
                _json(value),
                anchor_id,
            ),
        )
    repo_statement_expectations.attach_exact_document(
        conn,
        int(updated["source_document_id"]),
        actor=actor,
        reason="statement metadata identified exact account and closing period",
    )
    return updated


def _line(conn: sqlite3.Connection, line_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM statement_lines WHERE id=?", (int(line_id),)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown statement_line_id: {line_id}")
    return row


def _line_review(
    conn: sqlite3.Connection, line: sqlite3.Row
) -> sqlite3.Row:
    review = get_for_document(conn, int(line["source_document_id"]))
    if review is None:
        raise ValueError("statement row has no review envelope")
    return review


def _line_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "posted_on": row["posted_on"],
        "description": row["raw_description"],
        "amount_cents": int(row["amount_cents"]),
        "currency": row["currency"],
        "balance_cents": row["balance_cents"],
        "is_pending": int(row["is_pending"]),
        "review_disposition": row["review_disposition"],
        "source_anchor_id": row["source_anchor_id"],
        "row_hash": row["row_hash"],
    }


def _occurrence_for_values(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    line_id: int | None,
    account_id: int | None,
    posted_on: str,
    amount_cents: int,
    description: str,
) -> int:
    merchant = norm_merchant(description)
    preceding = conn.execute(
        """SELECT COUNT(*) FROM statement_lines
           WHERE source_document_id=?
             AND review_disposition='active'
             AND (? IS NULL OR id < ?)
             AND account_id IS ?
             AND posted_on=?
             AND amount_cents=?
             AND norm_merchant=?""",
        (
            int(source_document_id),
            line_id,
            line_id,
            account_id,
            posted_on,
            int(amount_cents),
            merchant,
        ),
    ).fetchone()[0]
    return int(preceding)


def correct_row(
    conn: sqlite3.Connection,
    line_id: int,
    *,
    expected_review_revision: int,
    expected_line_revision: int,
    actor: str,
    reason: str,
    source_page: int,
    posted_on: str,
    description: str,
    amount_cents: int,
    currency: str,
    balance_cents: int | None,
    is_pending: bool,
) -> sqlite3.Row:
    line = _line(conn, line_id)
    review = _line_review(conn, line)
    _assert_revision(review, expected_review_revision)
    _assert_line_revision(line, expected_line_revision)
    _guard_review_month(conn, review)
    _assert_document_unresolved(conn, int(review["source_document_id"]))
    if line["review_disposition"] != "active":
        raise ValueError("restore the row before correcting it")
    review = _reopen_projection_if_needed(
        conn, review, actor=actor, reason=reason
    )
    anchor_id = _anchor_for_page(conn, int(review["id"]), source_page)
    posted = _valid_date(posted_on)
    if posted is None:
        raise ValueError("row date must be YYYY-MM-DD")
    desc = _text(description, "description")
    normalized_currency = _text(currency, "currency").upper()
    occurrence = _occurrence_for_values(
        conn,
        source_document_id=int(review["source_document_id"]),
        line_id=int(line_id),
        account_id=line["account_id"],
        posted_on=posted,
        amount_cents=int(amount_cents),
        description=desc,
    )
    new_hash = row_hash(
        line["account_id"], posted, int(amount_cents), desc, occurrence
    )
    collision = conn.execute(
        """SELECT id FROM statement_lines
           WHERE account_id IS ? AND row_hash=? AND id<>?
             AND review_disposition='active'""",
        (line["account_id"], new_hash, int(line_id)),
    ).fetchone()
    if collision is not None:
        raise ValueError("corrected row duplicates another active statement row")
    new = {
        "posted_on": posted,
        "description": desc,
        "amount_cents": int(amount_cents),
        "currency": normalized_currency,
        "balance_cents": balance_cents,
        "is_pending": int(bool(is_pending)),
        "source_anchor_id": anchor_id,
    }
    key = _audit(
        conn,
        review_id=int(review["id"]),
        line_id=int(line_id),
        event_kind="row_corrected",
        actor=actor,
        reason=reason,
        old=_line_snapshot(line),
        new=new,
    )
    conn.execute(
        """UPDATE statement_lines
           SET posted_on=?, raw_description=?, norm_merchant=?,
               amount_cents=?, currency=?, balance_cents=?, is_pending=?,
               row_hash=?, flow_kind='unknown',
               match_status='unmatched', matched_transaction_id=NULL,
               match_method='', match_score=0, match_rationale='',
               source_anchor_id=?, row_confidence=1.0,
               review_revision=review_revision+1, review_operation_key=?
           WHERE id=?""",
        (
            posted,
            desc,
            norm_merchant(desc),
            int(amount_cents),
            normalized_currency,
            balance_cents,
            int(bool(is_pending)),
            new_hash,
            anchor_id,
            key,
            int(line_id),
        ),
    )
    return _line(conn, line_id)


def add_row(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    expected_review_revision: int,
    actor: str,
    reason: str,
    source_page: int,
    posted_on: str,
    description: str,
    amount_cents: int,
    currency: str,
    balance_cents: int | None = None,
    is_pending: bool = False,
) -> sqlite3.Row:
    review = get_review(conn, review_id)
    _assert_revision(review, expected_review_revision)
    _guard_review_month(conn, review)
    _assert_document_unresolved(conn, int(review["source_document_id"]))
    review = _reopen_projection_if_needed(
        conn, review, actor=actor, reason=reason
    )
    anchor_id = _anchor_for_page(conn, review_id, source_page)
    posted = _valid_date(posted_on)
    if posted is None:
        raise ValueError("row date must be YYYY-MM-DD")
    desc = _text(description, "description")
    normalized_currency = _text(currency, "currency").upper()
    occurrence = _occurrence_for_values(
        conn,
        source_document_id=int(review["source_document_id"]),
        line_id=None,
        account_id=review["account_id"],
        posted_on=posted,
        amount_cents=int(amount_cents),
        description=desc,
    )
    new_hash = row_hash(
        review["account_id"], posted, int(amount_cents), desc, occurrence
    )
    try:
        cur = conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description,
                 norm_merchant, amount_cents, currency, balance_cents,
                 is_pending, statement_period, row_hash, flow_kind,
                 source_anchor_id, row_confidence
               )
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'unknown',?,1.0)""",
            (
                int(review["source_document_id"]),
                review["account_id"],
                posted,
                desc,
                norm_merchant(desc),
                int(amount_cents),
                normalized_currency,
                balance_cents,
                int(bool(is_pending)),
                review["period_month"] or "",
                new_hash,
                anchor_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("added row duplicates another active statement row") from exc
    line_id = int(cur.lastrowid)
    line = _line(conn, line_id)
    _audit(
        conn,
        review_id=review_id,
        line_id=line_id,
        event_kind="row_added",
        actor=actor,
        reason=reason,
        old={},
        new=_line_snapshot(line),
        operation_key=f"row:added:{line_id}",
    )
    conn.execute(
        """INSERT INTO statement_field_evidence(
             evidence_key, statement_review_id, statement_line_id,
             extraction_id, field_name, original_value_json, confidence,
             source_anchor_id, origin
           )
           VALUES (?,?,?,?,?, ?,1.0,?,'manual')""",
        (
            f"manual:row:{line_id}",
            review_id,
            line_id,
            review["extraction_id"],
            "row_snapshot",
            _json(_line_snapshot(line)),
            anchor_id,
        ),
    )
    _sync_expectation_identity(
        conn,
        review,
        actor=actor,
        reason="manual statement row established exact account-period identity",
    )
    return line


def _set_row_disposition(
    conn: sqlite3.Connection,
    line_id: int,
    *,
    expected_review_revision: int,
    expected_line_revision: int,
    actor: str,
    reason: str,
    disposition: str,
) -> sqlite3.Row:
    line = _line(conn, line_id)
    review = _line_review(conn, line)
    _assert_revision(review, expected_review_revision)
    _assert_line_revision(line, expected_line_revision)
    _guard_review_month(conn, review)
    _assert_document_unresolved(conn, int(review["source_document_id"]))
    if line["review_disposition"] == disposition:
        return line
    review = _reopen_projection_if_needed(
        conn, review, actor=actor, reason=reason
    )
    event = "row_excluded" if disposition == "excluded" else "row_restored"
    if disposition == "excluded":
        new_hash = f"excluded:{int(line_id)}:{line['row_hash']}"
    else:
        occurrence = _occurrence_for_values(
            conn,
            source_document_id=int(review["source_document_id"]),
            line_id=int(line_id),
            account_id=line["account_id"],
            posted_on=str(line["posted_on"]),
            amount_cents=int(line["amount_cents"]),
            description=str(line["raw_description"]),
        )
        new_hash = row_hash(
            line["account_id"],
            str(line["posted_on"]),
            int(line["amount_cents"]),
            str(line["raw_description"]),
            occurrence,
        )
        collision = conn.execute(
            """SELECT id FROM statement_lines
               WHERE account_id IS ? AND row_hash=? AND id<>?
                 AND review_disposition='active'""",
            (line["account_id"], new_hash, int(line_id)),
        ).fetchone()
        if collision is not None:
            raise ValueError(
                "restored row duplicates another active statement row"
            )
    key = _audit(
        conn,
        review_id=int(review["id"]),
        line_id=int(line_id),
        event_kind=event,
        actor=actor,
        reason=reason,
        old={
            "review_disposition": line["review_disposition"],
            "row_hash": line["row_hash"],
        },
        new={"review_disposition": disposition, "row_hash": new_hash},
    )
    conn.execute(
        """UPDATE statement_lines
           SET review_disposition=?, row_hash=?, match_status='unmatched',
               matched_transaction_id=NULL, match_method='', match_score=0,
               match_rationale='', review_revision=review_revision+1,
               review_operation_key=?
           WHERE id=?""",
        (disposition, new_hash, key, int(line_id)),
    )
    _sync_expectation_identity(
        conn,
        review,
        actor=actor,
        reason=(
            "active statement rows changed account-period evidence identity"
        ),
    )
    return _line(conn, line_id)


def exclude_row(conn: sqlite3.Connection, line_id: int, **kwargs) -> sqlite3.Row:
    return _set_row_disposition(
        conn, line_id, disposition="excluded", **kwargs
    )


def restore_row(conn: sqlite3.Connection, line_id: int, **kwargs) -> sqlite3.Row:
    return _set_row_disposition(conn, line_id, disposition="active", **kwargs)


def archive_source(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    """Archive a reviewed statement without deleting source or row evidence."""
    review = get_for_document(conn, source_document_id)
    if review is None:
        raise ValueError("statement review not found")
    _guard_review_month(conn, review)
    _assert_document_unresolved(conn, int(source_document_id))
    review = _reopen_projection_if_needed(
        conn, review, actor=actor, reason=reason
    )
    document = repo_documents.get_document(conn, int(source_document_id))
    old_status = document["status"] if document is not None else ""
    for line in conn.execute(
        """SELECT * FROM statement_lines
           WHERE source_document_id=? AND review_disposition='active'
           ORDER BY id""",
        (int(source_document_id),),
    ).fetchall():
        current = get_review(conn, int(review["id"]))
        _set_row_disposition(
            conn,
            int(line["id"]),
            expected_review_revision=int(current["revision"]),
            expected_line_revision=int(line["review_revision"]),
            actor=actor,
            reason=reason,
            disposition="excluded",
        )
    repo_statement_expectations.detach_document_source(
        conn,
        int(source_document_id),
        actor=actor,
        reason=reason,
    )
    _audit(
        conn,
        review_id=int(review["id"]),
        event_kind="review_archived",
        actor=actor,
        reason=reason,
        old={"document_status": old_status},
        new={"document_status": "archived"},
    )
    conn.execute(
        """UPDATE ingest_extractions
           SET review_status='rejected'
           WHERE source_document_id=? AND doc_kind='statement'
             AND review_status='pending'""",
        (int(source_document_id),),
    )
    repo_documents.set_status(conn, int(source_document_id), "archived")
    return get_review(conn, int(review["id"]))


def completeness(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    home_currency: str | None = None,
    confidence_threshold: float = AUTO_FIELD_CONFIDENCE,
) -> dict[str, Any]:
    review = get_review(conn, review_id)
    home = home_currency or get_settings().home_currency
    hard: list[str] = []
    blockers: list[str] = []
    source_anchor_count = 0

    if review["account_id"] is None:
        hard.append("account_unresolved")
    else:
        account = conn.execute(
            "SELECT currency FROM accounts WHERE id=?",
            (int(review["account_id"]),),
        ).fetchone()
        account_currency = str(account["currency"] or "").strip().upper()
        account_reason = currency_review_reason(account_currency, home)
        if account_reason is not None:
            hard.append(f"account_{account_reason}")
        if (
            account_currency
            and str(review["currency"] or "").strip().upper()
            and account_currency != str(review["currency"]).strip().upper()
        ):
            hard.append("account_currency_mismatch")
    if not review["period_month"] or not review["period_end_on"]:
        hard.append("closing_period_missing")
    elif str(review["period_end_on"])[:7] != str(review["period_month"]):
        hard.append("closing_period_mismatch")
    currency_reason = currency_review_reason(review["currency"], home)
    if currency_reason is not None:
        hard.append(currency_reason)

    for field in (
        "period_start_on",
        "statement_issued_on",
        "opening_balance_cents",
        "closing_balance_cents",
        "account_fingerprint",
    ):
        value = review[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            blockers.append(f"missing_{field}")

    observed = int(review["observed_page_count"])
    if str(review["source_kind"]) == "structured_rows":
        raw_anchor_count = int(
            conn.execute(
                """SELECT COUNT(*) FROM statement_source_anchors
                   WHERE statement_review_id=? AND locator_kind='raw_row'""",
                (int(review_id),),
            ).fetchone()[0]
        )
        source_anchor_count = raw_anchor_count
        if raw_anchor_count == 0:
            hard.append("structured_source_anchor_missing")
        imported = conn.execute(
            """SELECT status, overlap_kind, review_reasons_json
               FROM structured_statement_imports
               WHERE statement_review_id=?""",
            (int(review_id),),
        ).fetchone()
        if imported is None:
            hard.append("structured_import_missing")
        elif str(imported["status"]) == "needs_review":
            try:
                import_reasons = json.loads(
                    str(imported["review_reasons_json"])
                )
            except (TypeError, ValueError):
                import_reasons = ["diagnostics_invalid"]
            for reason in import_reasons:
                if isinstance(reason, str) and reason.strip():
                    hard.append(f"structured_{reason.strip()}")
            if str(imported["overlap_kind"]) in {"partial", "ambiguous"}:
                hard.append(f"structured_{str(imported['overlap_kind'])}_overlap")
    else:
        pages = conn.execute(
            """SELECT page.*, anchor.id AS anchor_id
               FROM statement_review_pages page
               LEFT JOIN statement_source_anchors anchor
                 ON anchor.page_id=page.id AND anchor.locator_kind='page'
               WHERE page.statement_review_id=?
               ORDER BY page.page_number""",
            (int(review_id),),
        ).fetchall()
        source_anchor_count = len(pages)
        page_numbers = [int(page["page_number"]) for page in pages]
        if observed <= 0 or page_numbers != list(range(1, observed + 1)):
            blockers.append("page_sequence_incomplete")
        if int(review["extracted_page_count"]) != observed or int(
            review["extraction_truncated"]
        ):
            blockers.append("source_truncated")
        if review["declared_page_count"] is None:
            blockers.append("declared_page_count_missing")
        elif int(review["declared_page_count"]) != observed:
            blockers.append("page_count_mismatch")
        if any(page["anchor_id"] is None for page in pages):
            blockers.append("page_anchor_missing")

    evidence = {
        str(row["field_name"]): row
        for row in conn.execute(
            """SELECT evidence.*
               FROM statement_field_evidence evidence
               WHERE evidence.statement_review_id=?
                 AND evidence.statement_line_id IS NULL
               ORDER BY evidence.id""",
            (int(review_id),),
        )
    }
    for field in _REQUIRED_METADATA_FIELDS:
        claim = evidence.get(field)
        has_structured_header = (
            claim is not None
            and str(review["source_kind"]) == "structured_rows"
            and claim["structured_header_id"] is not None
        )
        if claim is None or (
            claim["source_anchor_id"] is None and not has_structured_header
        ):
            blockers.append(f"{field}_anchor_missing")
        elif float(claim["confidence"]) < float(confidence_threshold):
            blockers.append(f"{field}_low_confidence")

    active_lines = conn.execute(
        """SELECT * FROM statement_lines
           WHERE source_document_id=? AND review_disposition='active'
           ORDER BY posted_on, id""",
        (int(review["source_document_id"]),),
    ).fetchall()
    active_count = len(active_lines)
    if review["activity_kind"] == "transactions":
        if active_count == 0:
            blockers.append("transaction_rows_missing")
        if review["declared_row_count"] is None:
            blockers.append("declared_row_count_missing")
        elif int(review["declared_row_count"]) != active_count:
            blockers.append("row_count_mismatch")
        if any(line["source_anchor_id"] is None for line in active_lines):
            blockers.append("row_anchor_missing")
        if any(
            float(line["row_confidence"]) < float(confidence_threshold)
            for line in active_lines
        ):
            blockers.append("row_low_confidence")
    elif review["activity_kind"] == "zero_activity":
        if active_count:
            hard.append("zero_activity_has_rows")
        if review["declared_row_count"] != 0:
            blockers.append("zero_activity_row_count_not_zero")
        zero_claim = evidence.get("zero_activity")
        if zero_claim is None or zero_claim["source_anchor_id"] is None:
            hard.append("zero_activity_source_missing")
        elif float(zero_claim["confidence"]) < float(confidence_threshold):
            blockers.append("zero_activity_low_confidence")
        if (
            review["opening_balance_cents"] is None
            or review["closing_balance_cents"] is None
            or int(review["opening_balance_cents"])
            != int(review["closing_balance_cents"])
        ):
            blockers.append("zero_activity_balance_mismatch")
    else:
        blockers.append("activity_unknown")

    if (
        review["opening_balance_cents"] is not None
        and review["closing_balance_cents"] is not None
    ):
        total = sum(int(line["amount_cents"]) for line in active_lines)
        opening = int(review["opening_balance_cents"])
        closing = int(review["closing_balance_cents"])
        asset = opening + total == closing
        debt = opening - total == closing
        if not (asset or debt):
            # A contradicted balance is a hard blocker with no override path:
            # rows that cannot reproduce the statement's own closing balance
            # must never promote. Absent evidence is a different case below --
            # conflating "cannot check" with "check failed" is how a
            # fail-closed gate gets overridden out of existence.
            hard.append("balance_checksum_mismatch")
    else:
        blockers.append("balance_checksum_unavailable")

    return {
        "hard_blockers": sorted(set(hard)),
        "review_blockers": sorted(set(blockers)),
        "active_row_count": active_count,
        "excluded_row_count": int(
            conn.execute(
                """SELECT COUNT(*) FROM statement_lines
                   WHERE source_document_id=? AND review_disposition='excluded'""",
                (int(review["source_document_id"]),),
            ).fetchone()[0]
        ),
        "observed_page_count": observed,
        "source_anchor_count": source_anchor_count,
        "complete": not hard and not blockers,
    }


def _canonical_asserted_cents(review: sqlite3.Row, total: int) -> int:
    closing = int(review["closing_balance_cents"])
    opening = review["opening_balance_cents"]
    if opening is None:
        return closing
    asset = abs(int(opening) + total - closing) <= 1
    debt = abs(int(opening) - total - closing) <= 1
    return -closing if debt and not asset else closing


def approve(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    expected_revision: int,
    actor: str,
    reason: str,
    override_reason: str = "",
) -> tuple[sqlite3.Row, dict[str, Any]]:
    review = get_review(conn, review_id)
    if review["review_state"] in {"approved", "approved_with_override"}:
        return review, completeness(conn, review_id)
    _assert_revision(review, expected_revision)
    _guard_review_month(conn, review)
    result = completeness(conn, review_id)
    if result["hard_blockers"]:
        raise ValueError(
            "statement has hard blockers: " + ", ".join(result["hard_blockers"])
        )
    override = (override_reason or "").strip()
    if result["review_blockers"] and not override:
        raise ValueError(
            "statement completeness requires review: "
            + ", ".join(result["review_blockers"])
        )
    state = "approved_with_override" if result["review_blockers"] else "approved"
    audit_reason = override if state == "approved_with_override" else reason
    event = (
        "review_approved_with_override"
        if state == "approved_with_override"
        else "review_approved"
    )
    key = _audit(
        conn,
        review_id=int(review_id),
        event_kind=event,
        actor=actor,
        reason=audit_reason,
        old={"review_state": review["review_state"]},
        new={
            "review_state": state,
            "review_blockers": result["review_blockers"],
        },
    )
    conn.execute(
        """UPDATE statement_reviews
           SET review_state=?, reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?,
               override_reason=?, revision=revision+1, last_operation_key=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            state,
            _text(actor, "actor"),
            override if state == "approved_with_override" else None,
            key,
            int(review_id),
        ),
    )
    updated = get_review(conn, review_id)
    expectation, _ = repo_statement_expectations.attach_exact_document(
        conn,
        int(updated["source_document_id"]),
        actor=actor,
        reason="statement metadata identified exact account and closing period",
    )
    if (
        expectation is None
        or repo_statement_expectations.active_link_for_document(
            conn, int(updated["source_document_id"])
        )
        is None
    ):
        raise ValueError(
            "statement needs a required account-period expectation before approval"
        )
    repo_statement_expectations.mark_document_reviewed(
        conn,
        int(updated["source_document_id"]),
        actor=actor,
        reason=audit_reason,
    )
    repo_documents.set_status(conn, int(updated["source_document_id"]), "processed")
    if updated["extraction_id"] is not None:
        conn.execute(
            """UPDATE ingest_extractions
               SET review_status='approved', proposed_account_id=?
               WHERE id=?""",
            (updated["account_id"], int(updated["extraction_id"])),
        )

    active = conn.execute(
        """SELECT * FROM statement_lines
           WHERE source_document_id=? AND review_disposition='active'""",
        (int(updated["source_document_id"]),),
    ).fetchall()
    total = sum(int(line["amount_cents"]) for line in active)
    if updated["closing_balance_cents"] is not None:
        repo_assertions.record_assertion(
            conn,
            account_id=int(updated["account_id"]),
            asof_date=str(updated["period_end_on"]),
            asserted_cents=_canonical_asserted_cents(updated, total),
            source_document_id=int(updated["source_document_id"]),
            statement_period=str(updated["period_month"]),
        )
    if updated["activity_kind"] == "zero_activity":
        repo_statement_expectations.sync_document_reconciliation(
            conn,
            int(updated["source_document_id"]),
            actor=actor,
            reason="approved zero-activity statement reconciles by balance proof",
        )
    else:
        repo_jobs.enqueue(
            conn,
            "reconcile_document",
            {"source_document_id": int(updated["source_document_id"])},
            source_document_id=int(updated["source_document_id"]),
        )
    return updated, result


def detail(conn: sqlite3.Connection, source_document_id: int) -> dict[str, Any]:
    review = get_for_document(conn, source_document_id)
    if review is None:
        raise ValueError("statement review not found")
    pages = conn.execute(
        """SELECT page.*, anchor.id AS anchor_id
           FROM statement_review_pages page
           LEFT JOIN statement_source_anchors anchor
             ON anchor.page_id=page.id AND anchor.locator_kind='page'
           WHERE page.statement_review_id=?
           ORDER BY page.page_number""",
        (int(review["id"]),),
    ).fetchall()
    lines = conn.execute(
        """SELECT line.*, page.page_number AS source_page_number,
                  anchor.locator_kind AS source_locator_kind,
                  anchor.locator_json AS source_locator_json,
                  (
                    SELECT COUNT(*)
                    FROM statement_source_anchors prior
                    WHERE prior.statement_review_id=anchor.statement_review_id
                      AND prior.locator_kind='raw_row'
                      AND prior.id <= anchor.id
                  ) AS source_anchor_ordinal
           FROM statement_lines line
           LEFT JOIN statement_source_anchors anchor
             ON anchor.id=line.source_anchor_id
           LEFT JOIN statement_review_pages page ON page.id=anchor.page_id
           WHERE line.source_document_id=?
           ORDER BY line.review_disposition, line.posted_on, line.id""",
        (int(source_document_id),),
    ).fetchall()
    return {
        "review": review,
        "pages": pages,
        "lines": lines,
        "structured_import": conn.execute(
            """SELECT * FROM structured_statement_imports
               WHERE statement_review_id=?""",
            (int(review["id"]),),
        ).fetchone(),
        "completeness": completeness(conn, int(review["id"])),
        "audit": conn.execute(
            """SELECT * FROM statement_review_audit
               WHERE statement_review_id=? ORDER BY id DESC""",
            (int(review["id"]),),
        ).fetchall(),
    }
