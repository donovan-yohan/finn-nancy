"""FN-102: the Close Inbox — one worst-first queue of only agent-unconfident items,
composed from five sources with per-item source tags and resolve deep-links."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.close import period_exceptions
from app.db import (
    engine,
    repo_assertions,
    repo_close,
    repo_close_inbox,
    repo_ledger,
    repo_merchant_knowledge,
    repo_period_policy,
    repo_statement_expectations,
    repo_statements,
)
from app.db.repo_merchant_knowledge import Evidence
from app.web.routes import close

MONTH = "2026-06"
TRAILING = ("2026-03", "2026-04", "2026-05")


# --- seeding helpers ----------------------------------------------------------

def _account(conn, name: str, kind: str = "credit") -> int:
    return int(
        conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES (?, 'Test', ?, 'CAD')",
            (name, kind),
        ).lastrowid
    )


def _category(conn, name: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES (?, 'expense', 'nancy', '#ff9f43')",
            (name,),
        ).lastrowid
    )


def _expense(conn, *, account_id, category_id, posted_on, merchant, amount_cents, external_id) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn, account_id=account_id, posted_on=posted_on, description=f"{merchant} charge",
        counterparty=merchant, amount_cents=-abs(amount_cents), source="test",
        external_id=external_id, source_document_id=None, source_confidence=1.0,
        flow_kind="purchase",
    )
    split_id = repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=-abs(amount_cents),
    )
    repo_merchant_knowledge.confirm_category(
        conn,
        descriptor=merchant,
        category_id=category_id,
        scope=repo_merchant_knowledge.scope_for_transaction(conn, int(txn_id)),
        operation_key=f"test:close-inbox-category:{txn_id}",
        actor="test:operator",
        reason="operator confirmed close-inbox fixture category",
        evidence=Evidence(
            transaction_id=int(txn_id),
            transaction_split_id=split_id,
        ),
    )
    repo_statements.mark_cleared(conn, txn_id, posted_on)
    return int(txn_id)


def _pending_receipt(conn, *, merchant, total_cents, confidence, purchased_on=f"{MONTH}-05") -> int:
    doc_id = int(
        conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('receipt', ?, ?, ?, 'image/jpeg', 'needs_review')",
            (f"{merchant}.jpg", f"blobs/{merchant}-{total_cents}", f"sha-{merchant}-{total_cents}"),
        ).lastrowid
    )
    payload = json.dumps({"merchant": merchant, "purchased_on": purchased_on,
                          "total_cents": total_cents, "confidence": confidence})
    return int(
        conn.execute(
            "INSERT INTO ingest_extractions(source_document_id, doc_kind, extracted_json, "
            "confidence, review_status) VALUES (?, 'receipt', ?, ?, 'pending')",
            (doc_id, payload, confidence),
        ).lastrowid
    )


def _pending_statement(
    conn,
    *,
    name: str,
    statement_period: str,
    posted_on: str,
) -> int:
    doc_id = int(
        conn.execute(
            """INSERT INTO source_documents(
                 kind, original_name, storage_ref, sha256, mime_type, status
               )
               VALUES ('statement', ?, ?, ?, 'application/pdf', 'needs_review')""",
            (name, f"blobs/{name}", f"sha-{name}"),
        ).lastrowid
    )
    payload = json.dumps(
        {
            "institution": name,
            "statement_period": statement_period,
            "rows": [
                {
                    "posted_on": posted_on,
                    "description": "Synthetic merchant",
                    "amount_cents": -1200,
                }
            ],
        }
    )
    return int(
        conn.execute(
            """INSERT INTO ingest_extractions(
                 source_document_id, doc_kind, extracted_json,
                 confidence, review_status
               )
               VALUES (?, 'statement', ?, 0.4, 'pending')""",
            (doc_id, payload),
        ).lastrowid
    )


def _unmatched_line(conn, *, account_id, amount_cents, posted_on=f"{MONTH}-11",
                    merchant="UNKNOWN VENDOR", status="unmatched") -> int:
    period = posted_on[:7]
    policy = repo_statement_expectations.policy_for_month(
        conn, account_id, period
    )
    if policy is None or policy["configuration_state"] != "configured":
        repo_statement_expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month=period,
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:policy",
            reason="synthetic close-inbox statement",
        )
    doc_id = int(
        conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('statement', 'jun.pdf', ?, ?, 'application/pdf', 'processed')",
            (
                f"blobs/jun-{amount_cents}-{posted_on}",
                f"feed-{account_id}-{amount_cents}-{posted_on}",
            ),
        ).lastrowid
    )
    line_id = int(
        conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, is_pending, statement_period, row_hash, match_status)
               VALUES (?,?,?,?,?,?, 'CAD', 0, ?, ?, ?)""",
            (doc_id, account_id, posted_on, merchant, merchant, amount_cents,
             period, f"h-{amount_cents}-{posted_on}", status),
        ).lastrowid
    )
    row, attached = repo_statement_expectations.attach_exact_document(
        conn,
        doc_id,
        actor="test:ingest",
        reason="exact synthetic statement identity",
    )
    assert attached and row is not None
    repo_statement_expectations.mark_document_reviewed(
        conn,
        doc_id,
        actor="test:review",
        reason="synthetic statement reviewed",
    )
    repo_statement_expectations.sync_document_reconciliation(
        conn,
        doc_id,
        actor="test:reconcile",
        reason="synchronize synthetic line disposition",
    )
    return line_id


def _uncategorized(conn, *, account_id, amount_cents, posted_on=f"{MONTH}-09",
                   merchant="MYSTERY CHARGE") -> int:
    cat_id = repo_ledger.ensure_uncategorized(conn)
    txn_id = repo_ledger.insert_transaction(
        conn, account_id=account_id, posted_on=posted_on, description=merchant,
        counterparty=merchant, amount_cents=-abs(amount_cents), source="manual",
        external_id=f"uncat-{amount_cents}-{posted_on}", source_document_id=None, source_confidence=1.0,
        flow_kind="purchase",
    )
    repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=cat_id, amount_cents=-abs(amount_cents))
    return int(txn_id)


# --- source composition -------------------------------------------------------

def test_inbox_composes_all_six_sources(empty_db):
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        # 1) low-confidence extraction
        _pending_receipt(conn, merchant="Cafe", total_cents=1800, confidence=0.35)
        # 2) unmatched statement line
        _unmatched_line(conn, account_id=card, amount_cents=-9900)
        # 3) uncategorized expense
        _uncategorized(conn, account_id=card, amount_cents=4200)
        # 4) anomaly — travel spikes vs a steady trailing average
        travel = _category(conn, "Travel")
        for m in TRAILING:
            _expense(conn, account_id=card, category_id=travel, posted_on=f"{m}-08",
                     merchant="Airline", amount_cents=50000, external_id=f"travel-{m}")
        _expense(conn, account_id=card, category_id=travel, posted_on=f"{MONTH}-08",
                 merchant="Airline", amount_cents=200000, external_id=f"travel-{MONTH}")
        # 5) balance assertion that does not tie
        assert_acct = _account(conn, "Chequing", kind="chequing")
        _expense(conn, account_id=assert_acct, category_id=travel, posted_on=f"{MONTH}-02",
                 merchant="Rent", amount_cents=100000, external_id="rent-jun")
        repo_assertions.record_assertion(conn, account_id=assert_acct, asof_date=f"{MONTH}-30",
                                         asserted_cents=-90000)

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    sources = {i.source for i in items}
    assert sources == {
        "extraction",
        "statement_expectation",
        "statement_line",
        "expense_resolution",
        "anomaly",
        "assertion",
    }
    # Every item carries a source tag and a deep link to a real resolve surface.
    hrefs = {i.source: i.href for i in items}
    assert hrefs["extraction"] == "/review"
    assert hrefs["statement_line"] == f"/recon?month={MONTH}"
    expectation_hrefs = {
        item.href for item in items if item.source == "statement_expectation"
    }
    assert expectation_hrefs == {"/manage", f"/recon?month={MONTH}"}
    assert hrefs["expense_resolution"].startswith("/backlog#txn-")
    assert hrefs["anomaly"] == "/insights"
    assert hrefs["assertion"] == f"/recon?month={MONTH}"
    assert all(i.source_label for i in items)


def test_inbox_sorted_worst_first_by_magnitude(empty_db):
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        _pending_receipt(conn, merchant="Cafe", total_cents=1800, confidence=0.35)
        _unmatched_line(conn, account_id=card, amount_cents=-9900)
        _uncategorized(conn, account_id=card, amount_cents=4200)

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    severities = [i.severity_cents for i in items]
    assert severities == sorted(severities, reverse=True)
    # Largest magnitude leads; smallest trails.
    assert items[0].source == "statement_line"  # 9900
    assert items[-1].source == "statement_expectation"  # zero-magnitude blocker
    assert items[-2].source == "extraction"              # 1800


def test_equal_magnitude_extractions_order_by_ascending_confidence(empty_db):
    with engine.write_tx(empty_db) as conn:
        _pending_receipt(conn, merchant="Alpha", total_cents=5000, confidence=0.60)
        _pending_receipt(conn, merchant="Bravo", total_cents=5000, confidence=0.20)

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    assert [i.confidence for i in items] == [0.20, 0.60]
    assert items[0].title.startswith("Bravo")


def test_statement_extraction_requires_valid_declared_period_for_close_inbox(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        _pending_statement(
            conn,
            name="missing-period.pdf",
            statement_period="",
            posted_on=f"{MONTH}-02",
        )
        _pending_statement(
            conn,
            name="invalid-period.pdf",
            statement_period="2026-13",
            posted_on=f"{MONTH}-03",
        )
        _pending_statement(
            conn,
            name="declared-may.pdf",
            statement_period="2026-05",
            posted_on=f"{MONTH}-04",
        )
        _pending_statement(
            conn,
            name="declared-june.pdf",
            statement_period=MONTH,
            posted_on="2026-05-31",
        )

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    extraction_titles = [
        item.title for item in items if item.source == "extraction"
    ]
    assert extraction_titles == ["declared-june.pdf statement needs review"]


# --- settled items are excluded -----------------------------------------------

def test_settled_items_do_not_appear(empty_db):
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        food = _category(conn, "Food")
        # A matched statement line, an approved extraction, and a categorized txn.
        _unmatched_line(conn, account_id=card, amount_cents=-9900, status="matched")
        doc_id = int(
            conn.execute(
                "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
                "VALUES ('receipt', 'ok.jpg', 'blobs/ok', 'sha-ok', 'image/jpeg', 'processed')"
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO ingest_extractions(source_document_id, doc_kind, extracted_json, "
            "confidence, review_status) VALUES (?, 'receipt', ?, 0.9, 'approved')",
            (doc_id, json.dumps({"merchant": "OK", "purchased_on": f"{MONTH}-03", "total_cents": 100})),
        )
        _expense(conn, account_id=card, category_id=food, posted_on=f"{MONTH}-04",
                 merchant="Grocer", amount_cents=3000, external_id="food-jun")

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    assert items == []


def test_other_month_items_are_excluded(empty_db):
    # The three month-bound deterministic sources scope strictly to the selected month
    # (anomalies are intentionally cross-month and are exercised elsewhere).
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        _unmatched_line(conn, account_id=card, amount_cents=-5000, posted_on="2026-05-11")
        _uncategorized(conn, account_id=card, amount_cents=4200, posted_on="2026-05-09")
        _pending_receipt(conn, merchant="LastMonth", total_cents=2000, confidence=0.3,
                         purchased_on="2026-05-05")

    with engine.read_conn(empty_db) as conn:
        items = repo_close_inbox.build_inbox(conn, MONTH)

    month_bound = {"extraction", "statement_line", "expense_resolution"}
    assert not [i for i in items if i.source in month_bound]


def test_inbox_reaches_zero_after_resolution(empty_db):
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        line_id = _unmatched_line(conn, account_id=card, amount_cents=-9900)
        txn_id = _uncategorized(conn, account_id=card, amount_cents=4200)
        food = _category(conn, "Food")

    with engine.read_conn(empty_db) as conn:
        # Unmatched line + its required expectation + uncategorized transaction.
        assert len(repo_close_inbox.build_inbox(conn, MONTH)) == 3

    # Resolve both: match the statement line, categorize the transaction.
    with engine.write_tx(empty_db) as conn:
        repo_statements.set_match(conn, line_id, status="matched", method="manual")
        document_id = int(
            conn.execute(
                "SELECT source_document_id FROM statement_lines WHERE id=?",
                (line_id,),
            ).fetchone()[0]
        )
        repo_statement_expectations.sync_document_reconciliation(
            conn,
            document_id,
            actor="test:reconcile",
            reason="resolved inbox statement line",
        )
        conn.execute(
            "UPDATE transaction_splits SET category_id=? WHERE transaction_id=?",
            (food, txn_id),
        )
        split_id = int(
            conn.execute(
                "SELECT id FROM transaction_splits WHERE transaction_id=?",
                (txn_id,),
            ).fetchone()[0]
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="MYSTERY CHARGE",
            category_id=food,
            scope=repo_merchant_knowledge.scope_for_transaction(conn, txn_id),
            operation_key=f"test:close-inbox-resolve:{txn_id}",
            actor="test:operator",
            reason="operator confirmed the corrected expense category",
            evidence=Evidence(
                transaction_id=txn_id,
                transaction_split_id=split_id,
            ),
        )

    with engine.read_conn(empty_db) as conn:
        assert repo_close_inbox.build_inbox(conn, MONTH) == []


def test_assigned_category_without_accepted_evidence_stays_actionable(empty_db):
    with engine.write_tx(empty_db) as conn:
        card = _account(conn, "Everyday Card")
        food = _category(conn, "Food")
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=card,
            posted_on=f"{MONTH}-14",
            description="VAGUE PROCESSOR 1287",
            counterparty="",
            amount_cents=-2500,
            source="manual",
            external_id="unconfirmed-category",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=int(txn_id),
            category_id=food,
            amount_cents=-2500,
        )

    with engine.read_conn(empty_db) as conn:
        items = [
            item
            for item in repo_close_inbox.build_inbox(conn, MONTH)
            if item.source == "expense_resolution"
        ]

    assert len(items) == 1
    assert items[0].href == f"/backlog#txn-{txn_id}"
    assert "has not been confirmed" in items[0].detail


# --- route + sign-off ---------------------------------------------------------

def _client() -> TestClient:
    app = FastAPI()
    app.include_router(close.router)
    return TestClient(app)


def _seed_app_inbox(db: str) -> None:
    with engine.write_tx(db) as conn:
        _unmatched_line(
            conn,
            account_id=1,
            amount_cents=-9900,
            posted_on=f"{MONTH}-11",
            merchant="UNKNOWN VENDOR",
        )


def _seed_nonblocking_app_inbox(db: str) -> None:
    with engine.write_tx(db) as conn:
        _pending_receipt(
            conn,
            merchant="NeedsReview",
            total_cents=1700,
            confidence=0.25,
        )
        rows = repo_statement_expectations.prepare_period(
            conn,
            month=MONTH,
            actor="test:close",
            reason="materialize non-statement inbox fixture",
        )
        for row in rows:
            if (
                row["requirement_state"] == "required"
                and row["lifecycle_state"] == "expected"
            ):
                repo_statement_expectations.waive(
                    conn,
                    int(row["id"]),
                    actor="test:waiver",
                    reason="synthetic fixture explicitly has no statement",
                )


def _acknowledge_all_preclose(db: str) -> None:
    with engine.write_tx(db) as conn:
        repo_statement_expectations.prepare_period(
            conn,
            month=MONTH,
            actor="test:close-operator",
            reason="stabilize statement matrix before close review",
        )
        items = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            MONTH,
            period_exceptions.collect_period_exceptions(conn, MONTH),
        )
        for index, item in enumerate(items):
            repo_period_policy.acknowledge_preclose_exception(
                conn,
                MONTH,
                item,
                actor="test:close-operator",
                reason="reviewed generated close-inbox exception",
                operation_key=f"test:close-inbox:preack:{index}",
                evidence={"fixture": "synthetic inbox evidence"},
            )


def test_close_page_renders_inbox_section(app_env):
    _seed_app_inbox(app_env)
    r = _client().get(f"/close?month={MONTH}")
    assert r.status_code == 200
    assert "close inbox" in r.text.lower()
    assert 'data-exception-class="unresolved_line"' in r.text
    assert f'href="/recon?month={MONTH}"' in r.text
    # Review is per typed exception after close; there is no aggregate inbox gate.
    assert 'name="inbox_ack"' not in r.text


def test_signoff_freezes_inbox_count_and_typed_exceptions(app_env):
    _seed_app_inbox(app_env)
    with engine.read_conn(app_env) as conn:
        expected = len(repo_close_inbox.build_inbox(conn, MONTH))
    assert expected >= 1
    refused = _client().post(
        "/close/signoff",
        data={
            "month": MONTH,
            "actor": "test:close-operator",
            "reason": "attempted close before per-item review",
            "confirm_close": "1",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 400

    _acknowledge_all_preclose(app_env)
    r = _client().post(
        "/close/signoff",
        data={
            "month": MONTH,
            "actor": "test:close-operator",
            "reason": "reviewed the typed close exceptions",
            "confirm_close": "1",
            "operation_key": "test:close-inbox:signoff",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with engine.read_conn(app_env) as conn:
        state = repo_period_policy.get_state(conn, MONTH)
        items = repo_period_policy.list_current_exceptions(conn, MONTH)
        history = repo_period_policy.list_snapshot_history(conn, MONTH)
    assert state["state"] == "closed_with_exceptions"
    assert any(item["exception_type"] == "unresolved_line" for item in items)
    summary = json.loads(history[0]["snapshot_json"])
    assert summary["inbox_count"] == expected
    assert summary["inbox_ack"] is False


def test_signoff_with_inbox_requires_audited_operator_decision(app_env):
    _seed_app_inbox(app_env)
    r = _client().post(
        "/close/signoff",
        data={"month": MONTH, "inbox_ack": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, MONTH) == "open"


def test_close_get_is_read_only_safe(app_env):
    # Building the inbox on GET must never write (no period row created, safe under READ_ONLY).
    _seed_app_inbox(app_env)
    with engine.read_conn(app_env) as conn:
        assert repo_close.get_period(conn, MONTH) is None
    _client().get(f"/close?month={MONTH}")
    with engine.read_conn(app_env) as conn:
        assert repo_close.get_period(conn, MONTH) is None
