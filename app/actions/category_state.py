"""Canonical read snapshot used by inspection, approval and exact undo."""
from __future__ import annotations

import hashlib
import json
import sqlite3


def category_state(conn: sqlite3.Connection, transaction_id: int) -> dict:
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    if txn is None:
        raise LookupError("transaction not found")
    splits = [dict(r) for r in conn.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id", (transaction_id,)
    )]
    lines = [dict(r) for r in conn.execute(
        "SELECT * FROM statement_lines WHERE matched_transaction_id=? ORDER BY id", (transaction_id,)
    )]
    receipt_evidence = [dict(r) for r in conn.execute(
        "SELECT * FROM bank_receipt_evidence WHERE transaction_id=? ORDER BY id", (transaction_id,)
    )]
    receipt_decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM bank_receipt_decisions WHERE transaction_id=? ORDER BY id", (transaction_id,)
    )]
    document_ids = sorted({r["source_document_id"] for r in lines} | {
        r["source_document_id"] for r in receipt_evidence
    } | {
        r["source_document_id"] for r in receipt_decisions
    } | (
        {txn["source_document_id"]} if txn["source_document_id"] is not None else set()
    ))
    documents, extractions = [], []
    for doc_id in document_ids:
        row = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        if row is not None:
            documents.append(dict(row))
        extractions.extend(dict(r) for r in conn.execute(
            "SELECT * FROM ingest_extractions WHERE source_document_id=? ORDER BY id", (doc_id,)
        ))
    relationships = [dict(r) for r in conn.execute(
        """SELECT * FROM transaction_relationships
        WHERE source_transaction_id=? OR target_transaction_id=? ORDER BY id""",
        (transaction_id, transaction_id),
    )]
    account = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (txn["account_id"],)).fetchone())
    evidence = {"statement_reviews": [], "statement_field_evidence": [], "statement_source_anchors": [],
                "statement_review_pages": [], "statement_review_audit": [], "structured_statement_imports": [],
                "structured_statement_import_rows": [], "structured_statement_import_headers": []}
    evidence["bank_receipt_evidence"] = receipt_evidence
    evidence["bank_receipt_decisions"] = receipt_decisions
    evidence["bank_associations"] = [dict(r) for r in conn.execute(
        "SELECT * FROM bank_associations WHERE transaction_id=? ORDER BY id", (transaction_id,)
    )]
    evidence["bank_revocations"] = [dict(r) for r in conn.execute(
        "SELECT r.* FROM bank_revocations r JOIN bank_associations a ON a.id=r.association_id "
        "WHERE a.transaction_id=? ORDER BY r.id", (transaction_id,)
    )]
    evidence["bank_source_state"] = [dict(r) for r in conn.execute(
        """SELECT a.id AS association_id, a.observation_id AS accepted_observation_id,
                  s.observation_id AS current_observation_id, a.binding_id AS accepted_binding_id,
                  current_binding.id AS current_binding_id, current_binding.revision AS binding_revision,
                  o.kind, o.pending, o.amount_cents, o.currency, o.posted_on,
                  o.description, o.merchant, o.provider_account_id, o.pending_predecessor,
                  o.suggestion_json, accepted.posted_on AS accepted_posted_on
           FROM v_bank_associations a
           JOIN bank_subjects s ON s.id=a.subject_id
           JOIN bank_observations o ON o.id=s.observation_id
           JOIN bank_observations accepted ON accepted.id=a.observation_id
           JOIN bank_bindings binding ON binding.id=a.binding_id
           JOIN v_bank_bindings current_binding ON current_binding.bank_account_id=binding.bank_account_id
           WHERE a.transaction_id=? ORDER BY a.id""", (transaction_id,)
    )]
    for doc_id in document_ids:
        reviews = [dict(r) for r in conn.execute("SELECT * FROM statement_reviews WHERE source_document_id=? ORDER BY id", (doc_id,))]
        evidence["statement_reviews"].extend(reviews)
        for review in reviews:
            for table in ("statement_field_evidence", "statement_source_anchors", "statement_review_pages",
                          "statement_review_audit", "structured_statement_imports", "structured_statement_import_headers"):
                evidence[table].extend(dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE statement_review_id=? ORDER BY id", (review["id"],)
                ))
    for line in lines:
        evidence["structured_statement_import_rows"].extend(dict(r) for r in conn.execute(
            "SELECT * FROM structured_statement_import_rows WHERE statement_line_id=? ORDER BY id", (line["id"],)
        ))
    evidence["merchant_resolution_events"] = [dict(r) for r in conn.execute(
        "SELECT * FROM merchant_resolution_events WHERE transaction_id=? ORDER BY id", (transaction_id,)
    )]
    claim_ids = sorted({r["claim_id"] for r in evidence["merchant_resolution_events"]})
    evidence["merchant_resolution_claims"] = []
    for claim_id in claim_ids:
        evidence["merchant_resolution_claims"].append(dict(conn.execute(
            "SELECT * FROM merchant_resolution_claims WHERE id=?", (claim_id,)
        ).fetchone()))
    return {"transaction": dict(txn), "account": account, "splits": splits,
            "statement_lines": lines, "documents": documents,
            "extractions": extractions, "relationships": relationships, **evidence}


def state_digest(state: dict) -> str:
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()
