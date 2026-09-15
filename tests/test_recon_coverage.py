from __future__ import annotations

import sqlite3

from app.db import engine, repo_ledger, repo_recon_coverage, repo_statements


def _fixture(conn: sqlite3.Connection) -> dict:
    account_id = conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Coverage Card','Test','credit','CAD')"
    ).lastrowid
    groceries_id = conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Coverage Groceries','expense','finn','#4EA1FF')"
    ).lastrowid
    bills_id = conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Coverage Bills','expense','nancy','#ff9f43')"
    ).lastrowid
    doc_id = conn.execute(
        """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
           VALUES ('statement','coverage-june.pdf','blobs/coverage-june','sha-coverage','application/pdf','matched')"""
    ).lastrowid

    matched_txn = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on="2026-06-03",
        description="Receipt Grocery",
        counterparty="",
        amount_cents=-1000,
        source="receipt",
        external_id="coverage-matched",
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert matched_txn is not None
    repo_ledger.insert_split(conn, transaction_id=matched_txn, category_id=groceries_id, amount_cents=-1000)
    repo_statements.mark_cleared(conn, matched_txn, "2026-06-03")

    promoted_txn = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on="2026-06-04",
        description="Statement Hydro",
        counterparty="",
        amount_cents=-2000,
        source="statement",
        external_id="coverage-promoted",
        source_document_id=doc_id,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert promoted_txn is not None
    repo_ledger.insert_split(conn, transaction_id=promoted_txn, category_id=bills_id, amount_cents=-2000)
    repo_statements.mark_cleared(conn, promoted_txn, "2026-06-04")

    candidate_txn = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on="2026-06-05",
        description="Uncleared candidate",
        counterparty="",
        amount_cents=-3000,
        source="receipt",
        external_id="coverage-candidate",
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert candidate_txn is not None

    lines = [
        ("2026-06-03", "GROCERY STORE", -1000, "matched", matched_txn, "manual", "h-cov-match"),
        ("2026-06-04", "HYDRO", -2000, "promoted", promoted_txn, "manual", "h-cov-promote"),
        ("2026-06-05", "AMBIGUOUS SHOP", -3000, "needs_review", None, "", "h-cov-ambiguous"),
        ("2026-06-06", "BRAND NEW SERVICE", -4000, "unmatched", None, "", "h-cov-new"),
        ("2026-06-07", "CARD PAYMENT", -500, "ignored", None, "manual", "h-cov-ignore"),
        ("2026-06-08", "PAYROLL", 6000, "ignored", None, "manual", "h-cov-income"),
    ]
    for posted_on, desc, amount, status, txn_id, method, row_hash in lines:
        line_id = conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, is_pending, row_hash, match_status)
               VALUES (?,?,?,?,?,?, 'CAD',0,?,?)""",
            (doc_id, account_id, posted_on, desc, desc, amount, row_hash, status),
        ).lastrowid
        if txn_id is not None:
            repo_statements.set_match(conn, line_id, status=status, method=method, transaction_id=txn_id)

    return {"doc_id": doc_id, "account_id": account_id}


def test_statement_coverage_dashboard_summarizes_matched_unmatched_and_reasons(app_env):
    with engine.write_tx(app_env) as conn:
        _fixture(conn)

    with engine.read_conn(app_env) as conn:
        coverage = repo_recon_coverage.coverage_dashboard(conn)

    summary = coverage["summary"]
    assert summary["line_count"] == 6
    assert summary["statement_spend_cents"] == 10500
    assert summary["covered_spend_cents"] == 3000
    assert summary["unmatched_spend_cents"] == 7000
    assert summary["ignored_spend_cents"] == 500
    assert summary["income_cents"] == 0
    assert summary["positive_review_cents"] == 6000
    assert summary["coverage_pct"] == 30.0

    reasons = {row["reason"]: row["spend_cents"] for row in coverage["reasons"]}
    assert reasons == {
        "positive flow review": 0,
        "new merchant": 4000,
        "ambiguous match": 3000,
    }

    assert coverage["documents"][0]["document_name"] == "coverage-june.pdf"
    assert coverage["documents"][0]["coverage_pct"] == 30.0

    categories = {row["label"]: row for row in coverage["categories"]}
    assert categories["Coverage Bills"]["covered_spend_cents"] == 2000
    assert categories["Coverage Groceries"]["covered_spend_cents"] == 1000
    assert categories["Unmatched / needs review"]["unmatched_spend_cents"] == 7000
    assert categories["Flow meaning / needs review"]["positive_review_cents"] == 6000

    attention = coverage["attention_lines"]
    assert [row["attention_reason"] for row in attention] == [
        "positive flow review",
        "new merchant",
        "ambiguous match",
    ]
