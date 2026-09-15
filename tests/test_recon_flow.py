"""End-to-end M3 integration: capture -> stage -> reconcile, across receipts and statements.

Drives the REAL pipeline (app.ingest.storage.capture / app.ingest.pipeline.process_document)
and the REAL reconcile engine (app.reconcile.engine.reconcile_document / app.reconcile.apply),
never a route mini-app, so this exercises the exact code path production traffic would take.
One long, sequential scenario (single app_env) since later steps assert state built by
earlier ones, per the hand-off's numbered scenario list.
"""
from __future__ import annotations

import fitz  # PyMuPDF

from app.db import engine, repo_ledger, repo_statements
from app.ingest.pipeline import process_document
from app.ingest.schemas import ExtractedReceipt, ExtractedStatement, StatementRow
from app.ingest.storage import capture
from app.reconcile import apply as recon_apply
from app.reconcile import positive_flows
from app.reconcile.engine import reconcile_document
from app.reconcile.schemas import ReconBatchDecision, ReconDecision


# ---------------------------------------------------------------------------
# schema-keyed fake LLM: with_structured_output(schema).invoke(...) returns the canned
# object registered for that schema class. No network ever.
# ---------------------------------------------------------------------------

class _Structured:
    def __init__(self, result):
        self._result = result

    def invoke(self, messages):
        return self._result


class _SchemaLLM:
    def __init__(self, canned: dict):
        self._canned = canned

    def with_structured_output(self, schema, **kwargs):
        return _Structured(self._canned[schema])


_STATEMENT_MARKER_LINES = [
    "MONTHLY STATEMENT", "Account ending 9003", "Statement Period June 2026",
    "Opening balance   1,000.00", "Closing balance   3,331.40", "Minimum payment      25.00",
]


def _make_statement_pdf(extra: str = "") -> bytes:
    """A synthetic statement PDF: enough marker text for pdf_doc_kind() to classify it
    'statement'. `extra` lets a caller force a different sha256 (re-export scenario)."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    lines = list(_STATEMENT_MARKER_LINES)
    if extra:
        lines.append(extra)
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=10)
        y += 14
    return doc.tobytes()


def test_recon_flow_end_to_end(app_env, make_jpeg):
    db_path = app_env

    with engine.write_tx(db_path) as conn:
        conn.execute("UPDATE accounts SET external_ref=? WHERE id=?", ("Synthetic Bank:chequing:9003", 1))

    # =================================================================
    # Scenario 1: THE INVARIANT
    # =================================================================
        receipt_a = ExtractedReceipt(merchant="Loblaws", purchased_on="2026-06-03",
                                     currency="CAD", total_cents=4210,
                                     category_guess="Groceries", confidence=0.9)
        receipt_b = ExtractedReceipt(merchant="Spicy Noodle Place", purchased_on="2026-06-05",
                                     currency="CAD", total_cents=1800,
                                     category_guess="Restaurants", confidence=0.9)
        receipt_c = ExtractedReceipt(merchant="Corner Cafe", purchased_on="2026-06-20", total_cents=950,
                                     currency="CAD", category_guess="Restaurants", confidence=0.9)

    def _capture_receipt(receipt: ExtractedReceipt, color: tuple[int, int, int]) -> int:
        cap = capture(raw=make_jpeg(color=color), original_name="receipt.jpg", channel="web")
        assert cap["kind"] == "receipt"
        res = process_document(db_path, cap["source_document_id"],
                               _SchemaLLM({ExtractedReceipt: receipt}))
        assert res["status"] == "inserted"
        return res["transaction_id"]

    txn_a = _capture_receipt(receipt_a, (180, 40, 40))
    txn_b = _capture_receipt(receipt_b, (40, 180, 40))
    txn_c = _capture_receipt(receipt_c, (40, 40, 180))

    # Near-duplicate decoy for C: same cents, plausible nearby date, never actually chosen —
    # this is what forces the ambiguous-residue (LLM) path for the statement's fifth row.
    with engine.write_tx(db_path) as conn:
        decoy_id = repo_ledger.insert_transaction(
            conn, account_id=1, posted_on="2026-06-19", description="misc purchase",
            counterparty="Random Diner", amount_cents=-950, source="receipt",
            external_id="rcpt:decoy-c-pair", source_document_id=None, source_confidence=0.5,
            flow_kind="purchase",
        )
    assert decoy_id is not None

    # Rows sum to -4210-1800-9900+250000-950 = 233140, matching opening->closing below.
    statement1 = ExtractedStatement(
        institution="Synthetic Bank", account_hint="Chequing", account_last4="9003", currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=100000,
        closing_balance_cents=333140,
        declared_page_count=1,
        declared_row_count=5,
        field_confidence={
            field: 0.92
            for field in (
                "period_start_on",
                "period_end_on",
                "statement_issued_on",
                "opening_balance_cents",
                "closing_balance_cents",
                "currency",
                "account_fingerprint",
            )
        },
        field_pages={
            field: 1
            for field in (
                "period_start_on",
                "period_end_on",
                "statement_issued_on",
                "opening_balance_cents",
                "closing_balance_cents",
                "currency",
                "account_fingerprint",
            )
        },
        rows=[
            StatementRow(
                posted_on="2026-06-05",
                description="LOBLAWS #123",
                amount_cents=-4210,
                page_number=1,
            ),
            StatementRow(
                posted_on="2026-06-07",
                description="SPICY NOODLE PLACE #45",
                amount_cents=-1800,
                page_number=1,
            ),
            StatementRow(
                posted_on="2026-06-16",
                description="HYDRO SOMEWHERE",
                amount_cents=-9900,
                page_number=1,
            ),
            StatementRow(
                posted_on="2026-06-01",
                description="PAYROLL",
                amount_cents=250000,
                page_number=1,
            ),
            StatementRow(
                posted_on="2026-06-21",
                description="POS PURCHASE 9021",
                amount_cents=-950,
                page_number=1,
            ),
        ],
        confidence=0.92,
    )
    raw1 = _make_statement_pdf()
    cap1 = capture(raw=raw1, original_name="td-june.pdf", channel="web")
    assert cap1["kind"] == "statement"
    doc1_id = cap1["source_document_id"]

    res1 = process_document(db_path, doc1_id, _SchemaLLM({ExtractedStatement: statement1}))
    assert res1["status"] == "staged" and res1["lines"] == 5 and res1["duplicates"] == 0
    assert res1["reconcile_job"]

    # Negative purchase semantics are deterministic in this fixture. Positive
    # direction remains unknown until the explicit FN-144 decision below.
    with engine.write_tx(db_path) as conn:
        for line in repo_statements.lines_for_document(conn, doc1_id):
            if int(line["amount_cents"]) < 0:
                repo_statements.set_flow_kind(
                    conn,
                    int(line["id"]),
                    "purchase",
                )

    with engine.read_conn(db_path) as conn:
        count_before_reconcile = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        ambiguous_line = conn.execute(
            "SELECT id FROM statement_lines WHERE source_document_id=? AND amount_cents=-950",
            (doc1_id,),
        ).fetchone()
    ambiguous_line_id = ambiguous_line["id"]

    recon_llm1 = _SchemaLLM({ReconBatchDecision: ReconBatchDecision(decisions=[
        ReconDecision(statement_line_id=ambiguous_line_id, transaction_id=txn_c, confidence=0.9,
                     reason="matches the cafe receipt, not the decoy"),
    ])})
    result1 = reconcile_document(db_path, doc1_id, recon_llm1)
    assert result1 == {
        "matched": 0,
        "promoted": 0,
        "needs_review": 5,
        "ignored_pending": 0,
    }

    # FN-149B starts with same-event authority disabled even for exact and
    # high-confidence model matches. The end-of-month operator confirms each
    # receipt/statement pair explicitly; the ambiguous row deliberately chooses
    # the real receipt instead of the same-amount decoy.
    with engine.write_tx(db_path) as conn:
        lines = repo_statements.lines_for_document(conn, doc1_id)
        by_amount = {int(line["amount_cents"]): line for line in lines}
        for amount_cents in (-9900, 250000):
            assert (
                recon_apply.promote_line(
                    conn,
                    int(by_amount[amount_cents]["id"]),
                )
                is not None
            )
        for amount_cents, transaction_id in (
            (-4210, txn_a),
            (-1800, txn_b),
            (-950, txn_c),
        ):
            recon_apply.confirm_match(
                conn,
                int(by_amount[amount_cents]["id"]),
                int(transaction_id),
            )

    # A human-reviewed classification is the only point where PAYROLL becomes
    # income. The statement promoter deliberately persisted it as unknown.
    with engine.write_tx(db_path) as conn:
        review = positive_flows.list_positive_flow_reviews(conn, "2026-06")[0]
        proposal = next(
            item
            for item in review["proposals"]
            if item["proposed_flow_kind"] == "income"
        )
        positive_flows.accept_classification(
            conn,
            subject_transaction_id=int(review["subject"]["transaction_id"]),
            month="2026-06",
            flow_kind="income",
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:recon-flow-payroll-income",
            actor="test:operator",
            reason="synthetic pay statement proves earned income",
        )

    hydro_row_hash = payroll_row_hash = None
    with engine.read_conn(db_path) as conn:
        count_after_reconcile = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        for txn_id in (txn_a, txn_b, txn_c):
            txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
            assert txn["recon_status"] == "cleared"
            assert txn["account_id"] == 1  # resolved onto the statement's account

        # NO new txn for A/B/C — they stay receipt-sourced, never duplicated as 'statement'.
        n_statement_for_abc = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='statement' AND id IN (?,?,?)",
            (txn_a, txn_b, txn_c),
        ).fetchone()[0]
        assert n_statement_for_abc == 0

        lines = repo_statements.lines_for_document(conn, doc1_id)
        by_amount = {l["amount_cents"]: l for l in lines}
        assert by_amount[-4210]["match_status"] == "matched"
        assert by_amount[-1800]["match_status"] == "matched"
        assert by_amount[-950]["match_status"] == "matched"
        assert by_amount[-950]["matched_transaction_id"] == txn_c
        hydro_line, payroll_line = by_amount[-9900], by_amount[250000]
        assert hydro_line["match_status"] == "promoted"
        assert payroll_line["match_status"] == "promoted"
        hydro_row_hash, payroll_row_hash = hydro_line["row_hash"], payroll_line["row_hash"]

        hydro_txn = conn.execute("SELECT * FROM transactions WHERE id=?",
                                 (hydro_line["matched_transaction_id"],)).fetchone()
        payroll_txn = conn.execute("SELECT * FROM transactions WHERE id=?",
                                   (payroll_line["matched_transaction_id"],)).fetchone()
        assert hydro_txn["source"] == "statement" and hydro_txn["external_id"] == hydro_row_hash
        assert payroll_txn["source"] == "statement" and payroll_txn["external_id"] == payroll_row_hash

        hydro_split = conn.execute("SELECT * FROM transaction_splits WHERE transaction_id=?",
                                   (hydro_txn["id"],)).fetchone()
        payroll_split = conn.execute("SELECT * FROM transaction_splits WHERE transaction_id=?",
                                     (payroll_txn["id"],)).fetchone()
        assert hydro_split["amount_cents"] == -9900  # signed both directions
        assert payroll_split["amount_cents"] == 250000
        hydro_cat = conn.execute("SELECT kind FROM categories WHERE id=?",
                                 (hydro_split["category_id"],)).fetchone()
        payroll_cat = conn.execute("SELECT kind FROM categories WHERE id=?",
                                   (payroll_split["category_id"],)).fetchone()
        assert hydro_cat["kind"] == "expense"
        assert payroll_cat["kind"] == "income"  # deposit must not land in the expense bucket

    assert count_after_reconcile - count_before_reconcile == 2  # exactly HYDRO + PAYROLL

    with engine.read_conn(db_path) as conn:
        total_after_scenario1 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    # =================================================================
    # Scenario 5 (checked here, "after scenario 1" per the hand-off): v_cashflow_monthly
    # reflects the promoted rows exactly once — no double count vs. the receipts.
    # =================================================================
    with engine.read_conn(db_path) as conn:
        cashflow = conn.execute("SELECT * FROM v_cashflow_monthly WHERE month='2026-06'").fetchone()
    assert cashflow["income_cents"] == 250000                         # PAYROLL only
    assert cashflow["expense_cents"] == 4210 + 1800 + 9900 + 950      # A + B + HYDRO + C, once each
    assert cashflow["net_cents"] == 250000 - (4210 + 1800 + 9900 + 950)

    # =================================================================
    # Scenario 2: NO-DOUBLE-COUNT RE-UPLOAD
    # =================================================================
    cap_dupe = capture(raw=raw1, original_name="td-june-again.pdf", channel="web")
    assert cap_dupe["status"] == "duplicate"
    assert cap_dupe["source_document_id"] == doc1_id

    raw2 = _make_statement_pdf(extra="Reissued copy")  # byte-different re-export, same rows
    assert raw2 != raw1
    cap2 = capture(raw=raw2, original_name="td-june-reexport.pdf", channel="web")
    assert cap2["status"] == "staged"
    doc2_id = cap2["source_document_id"]
    assert doc2_id != doc1_id

    res2 = process_document(db_path, doc2_id, _SchemaLLM({ExtractedStatement: statement1}))
    # Restaging/re-export reproduces identical row_hashes (occ = in-batch ordinal),
    # so UNIQUE(account_id, row_hash) collapses every line. No-double-count invariant.
    assert res2["status"] == "staged" and res2["lines"] == 0 and res2["duplicates"] == 5
    assert res2["reconcile_job"] is None

    with engine.read_conn(db_path) as conn:
        count_before_bad_reconcile = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    result2 = reconcile_document(db_path, doc2_id, llm=None)
    # Every re-exported row collapsed at staging, so there is nothing to reconcile and the
    # ledger is untouched. This IS the no-double-count invariant.
    assert result2 == {"matched": 0, "promoted": 0, "needs_review": 0, "ignored_pending": 0}

    with engine.read_conn(db_path) as conn:
        count_after_reexport = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        decoy_txn = conn.execute("SELECT * FROM transactions WHERE id=?", (decoy_id,)).fetchone()
    assert count_after_reexport == count_before_bad_reconcile == total_after_scenario1
    assert decoy_txn["recon_status"] == "uncleared"  # decoy untouched by the re-export

    # =================================================================
    # Scenario 3: re-run reconcile_document on the settled doc -> idempotent, counts all zero
    # =================================================================
    result3 = reconcile_document(db_path, doc1_id, llm=None)
    assert result3 == {"matched": 0, "promoted": 0, "needs_review": 0, "ignored_pending": 0}
    with engine.read_conn(db_path) as conn:
        count_after_rerun = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_after_rerun == total_after_scenario1

    # =================================================================
    # Scenario 4: unreconcile_document -> re-run reconcile -> same end state as scenario 1
    # =================================================================
    with engine.write_tx(db_path) as conn:
        undo1 = recon_apply.unreconcile_document(conn, doc1_id)
    assert undo1 == {"lines": 5, "reset": 4, "removed": 1}

    with engine.read_conn(db_path) as conn:
        for txn_id in (txn_a, txn_b, txn_c):
            txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
            assert txn["recon_status"] == "uncleared"
            assert txn["cleared_on"] == ""
        # Lines are PRESERVED (reset to 'unmatched'), not deleted — the recon UI has no
        # in-app recovery for a doc whose lines vanished.
        lines_after_undo = repo_statements.lines_for_document(conn, doc1_id)
        assert len(lines_after_undo) == 5
        assert all(l["match_status"] == "unmatched" for l in lines_after_undo)
        assert all(l["matched_transaction_id"] is None for l in lines_after_undo)
        n_statement_txns = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='statement'"
        ).fetchone()[0]
        assert n_statement_txns == 1
        count_after_unreconcile = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_after_unreconcile == total_after_scenario1 - 1  # HYDRO gone; audited PAYROLL preserved

    # Re-run reconcile_document directly: the doc's lines are already staged (preserved by
    # unreconcile, just reset to 'unmatched'), so there is nothing left to re-stage.
    with engine.read_conn(db_path) as conn:
        ambiguous_line_2 = conn.execute(
            "SELECT id FROM statement_lines WHERE source_document_id=? AND amount_cents=-950",
            (doc1_id,),
        ).fetchone()
    recon_llm2 = _SchemaLLM({ReconBatchDecision: ReconBatchDecision(decisions=[
        ReconDecision(statement_line_id=ambiguous_line_2["id"], transaction_id=txn_c, confidence=0.9,
                     reason="matches the cafe receipt, not the decoy"),
    ])})
    result4 = reconcile_document(db_path, doc1_id, recon_llm2)
    assert result4 == {
        "matched": 0,
        "promoted": 0,
        "needs_review": 5,
        "ignored_pending": 0,
    }

    with engine.write_tx(db_path) as conn:
        lines = repo_statements.lines_for_document(conn, doc1_id)
        by_amount = {int(line["amount_cents"]): line for line in lines}
        assert (
            recon_apply.promote_line(
                conn,
                int(by_amount[-9900]["id"]),
            )
            is not None
        )
        payroll_txn = conn.execute(
            """
            SELECT id
            FROM transactions
            WHERE source='statement' AND external_id=?
            """,
            (payroll_row_hash,),
        ).fetchone()
        assert payroll_txn is not None
        for amount_cents, transaction_id in (
            (-4210, txn_a),
            (-1800, txn_b),
            (-950, txn_c),
            (250000, int(payroll_txn["id"])),
        ):
            recon_apply.confirm_match(
                conn,
                int(by_amount[amount_cents]["id"]),
                int(transaction_id),
            )

    with engine.read_conn(db_path) as conn:
        count_final = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        lines_final = repo_statements.lines_for_document(conn, doc1_id)
        by_amount_final = {l["amount_cents"]: l for l in lines_final}
        assert by_amount_final[-9900]["row_hash"] == hydro_row_hash        # same external_ids
        assert by_amount_final[250000]["row_hash"] == payroll_row_hash
        hydro_txn2 = conn.execute(
            "SELECT * FROM transactions WHERE source='statement' AND external_id=?",
            (hydro_row_hash,),
        ).fetchone()
        payroll_txn2 = conn.execute(
            "SELECT * FROM transactions WHERE source='statement' AND external_id=?",
            (payroll_row_hash,),
        ).fetchone()
    assert count_final == total_after_scenario1  # same txn count as scenario 1's end state
    assert hydro_txn2 is not None
    assert payroll_txn2 is not None
