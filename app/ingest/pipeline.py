"""ingest_document handler: extract -> validate -> promote or stage for review.

The slow vision call happens OUTSIDE the write lock; only the quick DB writes take it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..accounting.contract import currency_review_reason
from ..config import get_settings
from ..db import (
    engine,
    repo_documents,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from .extract.receipt import extract_receipt
from .extract.statement import checksum_ok, extract_statement
from .promote import promote_receipt
from .schemas import ExtractedReceipt
from .storage import blob_abspath
from .triage import triage_document
from .verify import verify_receipt


logger = logging.getLogger(__name__)


def _validate(r: ExtractedReceipt, min_conf: float, home_currency: str = "CAD") -> tuple[bool, str]:
    currency_reason = currency_review_reason(r.currency, home_currency)
    if currency_reason is not None:
        return False, currency_reason
    if (r.confidence or 0.0) < min_conf:
        return False, "low_confidence"
    if int(r.total_cents or 0) <= 0:
        return False, "no_total"
    parts = int(r.subtotal_cents or 0) + int(r.tax_cents or 0) + int(r.tip_cents or 0)
    if parts > 0:
        tolerance = max(100, int(0.01 * int(r.total_cents)))
        if abs(parts - int(r.total_cents)) > tolerance:
            return False, "arithmetic_mismatch"
    return True, "ok"


def _process_statement(db_path, doc, llm) -> dict:
    settings = get_settings()
    source_document_id = int(doc["id"])
    raw = Path(blob_abspath(doc["storage_ref"])).read_bytes()
    parsed = extract_statement(llm, raw)  # slow LLM call, no lock held

    with engine.write_tx(db_path) as conn:
        account_id = repo_statements.match_account_by_last4(conn, parsed)
        currency_reason = currency_review_reason(parsed.currency, settings.home_currency)
        if currency_reason is None and account_id is not None:
            account = conn.execute(
                "SELECT currency FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            currency_reason = currency_review_reason(
                account["currency"] if account is not None else None,
                settings.home_currency,
            )
        extraction_id = repo_documents.insert_extraction(
            conn,
            source_document_id=source_document_id,
            doc_kind="statement",
            extracted_json=parsed.model_dump_json(),
            confidence=float(parsed.confidence or 0.0),
            external_id="",
            proposed_account_id=account_id,
            proposed_category_id=None,
            review_status="pending",
        )
        envelope = repo_statement_reviews.create_from_extraction(
            conn,
            source_document_id=source_document_id,
            extraction_id=extraction_id,
            account_id=account_id,
            parsed=parsed,
            raw=raw,
            actor="ingest:extractor",
        )
        staged = repo_statements.stage_lines(
            conn,
            source_document_id=source_document_id,
            account_id=account_id,
            parsed=parsed,
            row_anchor_ids=repo_statement_reviews.row_anchor_ids(
                parsed, envelope.page_anchor_ids
            ),
        )
        repo_statement_reviews.record_row_evidence(
            conn,
            review_id=int(envelope.review["id"]),
            extraction_id=extraction_id,
            parsed=parsed,
            account_id=account_id,
            anchors=envelope.page_anchor_ids,
        )

        if not parsed.rows and not parsed.zero_activity:
            repo_documents.set_status(conn, source_document_id, "needs_review")
            return {"status": "needs_review", "reason": "no_rows",
                     "extraction_id": extraction_id, **staged}
        if account_id is None:
            repo_documents.set_status(conn, source_document_id, "needs_review")
            return {"status": "needs_review", "reason": "account_unresolved",
                    "extraction_id": extraction_id, **staged}

        if (
            currency_reason is None
            and checksum_ok(parsed)
            and staged["staged"] == 0
            and staged["duplicates"] == len(parsed.rows)
            and repo_statements.is_exact_duplicate_reexport(
                conn,
                account_id=account_id,
                parsed=parsed,
            )
        ):
            # The immutable source is retained, but it contributes no new row
            # evidence and must not regress a reconciled expectation as
            # "new evidence".  The existing linked source remains authoritative.
            repo_documents.set_status(conn, source_document_id, "processed")
            return {
                "status": "staged",
                "lines": 0,
                "duplicates": staged["duplicates"],
                "reconcile_job": None,
            }

        expectation, _ = repo_statement_expectations.attach_exact_document(
            conn,
            source_document_id,
            actor="ingest:auto",
            reason="exact account and declared statement closing period",
        )
        if (
            expectation is None
            or repo_statement_expectations.active_link_for_document(
                conn, source_document_id
            )
            is None
        ):
            conn.execute(
                """UPDATE ingest_extractions
                   SET review_status='pending'
                   WHERE id=?""",
                (extraction_id,),
            )
            repo_documents.set_status(conn, source_document_id, "needs_review")
            return {
                "status": "needs_review",
                "reason": "statement_expectation_unresolved",
                "extraction_id": extraction_id,
                **staged,
            }
        completeness = repo_statement_reviews.completeness(
            conn, int(envelope.review["id"])
        )
        if completeness["hard_blockers"] or completeness["review_blockers"]:
            reason = (
                completeness["hard_blockers"] + completeness["review_blockers"]
            )[0]
            repo_documents.set_status(conn, source_document_id, "needs_review")
            return {
                "status": "needs_review",
                "reason": reason,
                "extraction_id": extraction_id,
                **staged,
            }

        _, _ = repo_statement_reviews.approve(
            conn,
            int(envelope.review["id"]),
            expected_revision=int(envelope.review["revision"]),
            actor="ingest:auto",
            reason="complete anchored statement extraction approved automatically",
        )
        job = conn.execute(
            """SELECT id FROM jobs
               WHERE type='reconcile_document' AND source_document_id=?
               ORDER BY id DESC LIMIT 1""",
            (source_document_id,),
        ).fetchone()
        return {"status": "staged", "lines": staged["staged"], "duplicates": staged["duplicates"],
                "reconcile_job": int(job["id"]) if job is not None else None}


def _needs_triage(mime: str | None) -> bool:
    """Only image uploads earn the extra vision triage pass.

    An image's sniffed kind is a blind 'receipt' default (images carry no format-level
    statement/receipt signal), so a vision re-check genuinely adds information. PDFs are
    already content-classified by pdf.looks_like_statement, and anything else can't be
    rendered for the vision model — both skip triage rather than pay a second sequential
    LLM call that (under llm_concurrency=1) would roughly double ingest latency.
    """
    return bool(mime) and mime.startswith("image/")


def process_document(db_path, source_document_id: int, llm) -> dict:
    settings = get_settings()

    with engine.read_conn(db_path) as conn:
        doc = repo_documents.get_document(conn, source_document_id)
    if doc is None:
        return {"status": "missing"}

    kind = doc["kind"]
    if settings.triage_enabled and _needs_triage(doc["mime_type"]):
        raw = Path(blob_abspath(doc["storage_ref"])).read_bytes()
        try:
            triage = triage_document(llm, raw, doc["mime_type"])
            if triage.confidence >= settings.triage_min_confidence and triage.kind != kind:
                logger.info(
                    "document triage override doc_id=%s old_kind=%s new_kind=%s confidence=%.3f",
                    source_document_id, kind, triage.kind, triage.confidence,
                )
                with engine.write_tx(db_path) as conn:
                    repo_documents.set_kind(conn, source_document_id, triage.kind)
                    repo_documents.record_triage(
                        conn, source_document_id,
                        from_kind=kind, to_kind=triage.kind, confidence=triage.confidence,
                    )
                kind = triage.kind
        except Exception:  # noqa: BLE001 - triage must never block ingestion
            logger.exception("document triage failed doc_id=%s", source_document_id)

    if kind == "statement":
        return _process_statement(db_path, doc, llm)

    if kind != "receipt":
        # Other kinds ('upload'/'invoice'/'other') park for review.
        with engine.write_tx(db_path) as conn:
            repo_documents.set_status(conn, source_document_id, "needs_review")
        return {"status": "unsupported_kind", "kind": kind}

    raw = Path(blob_abspath(doc["storage_ref"])).read_bytes()
    receipt = extract_receipt(llm, raw)  # slow LLM call, no lock held
    ok, reason = _validate(
        receipt, settings.classify_min_confidence, settings.home_currency
    )
    if ok and settings.verifier_enabled:
        # Second pass BEFORE the write lock (FN-110): reconcile the extracted amounts
        # and optionally judge them against the image. A finding routes to review.
        ok, reason = verify_receipt(llm, raw, receipt, source_document_id)

    with engine.write_tx(db_path) as conn:
        extraction_id = repo_documents.insert_extraction(
            conn,
            source_document_id=source_document_id,
            doc_kind="receipt",
            extracted_json=receipt.model_dump_json(),
            confidence=float(receipt.confidence or 0.0),
            external_id="rcpt:" + doc["sha256"][:16],
            proposed_account_id=None,
            proposed_category_id=None,
            review_status="pending" if not ok else "auto",
        )
        if not ok:
            repo_documents.set_status(conn, source_document_id, "needs_review")
            return {"status": "needs_review", "reason": reason, "extraction_id": extraction_id}

        result = promote_receipt(
            conn, source_document_id=source_document_id, sha256=doc["sha256"],
            receipt=receipt, extraction_id=extraction_id,
        )
        repo_documents.set_status(conn, source_document_id, "processed")
        return result
