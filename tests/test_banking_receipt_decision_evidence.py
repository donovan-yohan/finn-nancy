"""Separate-purchase review is canonical machine evidence, not hidden audit."""
import json

import pytest

from app.actions.recategorization import RecategorizationHandler
from app.api import finance
from app.config import Settings, get_settings
from app.db import engine, repo_documents
from app.ingest.promote import promote_receipt
from tests.test_banking_feed import banking, book  # noqa: F401
from tests.test_banking_accounting_regressions import receipt_fixture


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_reviewed_separate_receipt_is_visible_to_machine_apply_and_undo(banking, monkeypatch):
    service, _, account, _, _ = banking
    monkeypatch.setenv('DB_PATH', str(service.db_path))
    _, existing = book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
        result = promote_receipt(conn, source_document_id=doc, sha256='e'*64,
            receipt=receipt, extraction_id=extraction, account_id=account,
            human_review=True, acknowledge_separate_purchase=True,
            separate_purchase_reason='Generated independently verified second purchase')
        tid = result['transaction_id']
        repo_documents.link_extraction_txn(conn, extraction, tid, review_status='approved')
        repo_documents.set_status(conn, doc, 'processed')
        target = conn.execute("INSERT INTO categories(name,kind) VALUES ('Generated meals','expense')").lastrowid
    before = finance.transaction_detail(tid)
    events = before['evidence']['bank_receipt_decisions']
    assert len(events) == 1
    assert events[0]['transaction_id'] == tid
    assert events[0]['original_json'] == receipt.model_dump_json()
    assert json.loads(events[0]['candidate_transaction_ids_json']) == [existing]
    assert events[0]['actor'] == 'operator:receipt-review'
    assert events[0]['reason'] == 'Generated independently verified second purchase'
    assert {r['id'] for r in before['evidence']['documents']} == {doc}
    assert {r['id'] for r in before['evidence']['extractions']} == {extraction}
    assert finance.transaction_detail(existing)['evidence']['bank_receipt_decisions'] == []
    proposal = finance.propose_category(operation_key='decision-evidence:category',
        transaction_id=tid, split_id=before['splits'][0]['id'], to_category_id=target,
        expected_state_digest=before['state_digest'], rationale='Generated reviewed category')
    with engine.write_tx(service.db_path) as conn:
        applied = RecategorizationHandler().apply(conn, proposal['payload'])
        RecategorizationHandler().revert(conn, applied['revert'])
    after = finance.transaction_detail(tid)
    assert after['splits'] == before['splits']
    assert after['amount_cents'] == before['amount_cents'] == -1234
    assert after['evidence']['bank_receipt_decisions'] == events
    assert after['evidence']['extractions'] == before['evidence']['extractions']


def test_appended_separate_receipt_audit_invalidates_category_approval(banking, monkeypatch):
    service, _, account, _, _ = banking
    monkeypatch.setenv('DB_PATH', str(service.db_path))
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
        tid = promote_receipt(conn, source_document_id=doc, sha256='e'*64,
            receipt=receipt, extraction_id=extraction, account_id=account,
            human_review=True)['transaction_id']
        repo_documents.link_extraction_txn(conn, extraction, tid, review_status='approved')
        repo_documents.set_status(conn, doc, 'processed')
        target = conn.execute("INSERT INTO categories(name,kind) VALUES ('Generated meals','expense')").lastrowid
    before = finance.transaction_detail(tid)
    proposal = finance.propose_category(operation_key='decision-evidence:stale',
        transaction_id=tid, split_id=before['splits'][0]['id'], to_category_id=target,
        expected_state_digest=before['state_digest'], rationale='Generated category proposal')
    with engine.write_tx(service.db_path) as conn:
        # Isolate an audit-only delta; all preexisting rows return to their exact values.
        conn.execute("UPDATE ingest_extractions SET transaction_id=NULL,review_status='pending' WHERE id=?", (extraction,))
        conn.execute('''INSERT INTO bank_receipt_decisions(extraction_id,source_document_id,
            transaction_id,decision,operation_key,actor,reason,original_json,reviewed_json,
            candidate_transaction_ids_json) VALUES (?,?,?,'separate_purchase',
            'generated-audit:append','operator:receipt-review','Generated explicit review',?,?, '[]')''',
            (extraction, doc, tid, receipt.model_dump_json(), receipt.model_dump_json()))
        repo_documents.link_extraction_txn(conn, extraction, tid, review_status='approved')
    after = finance.transaction_detail(tid)
    assert after['evidence']['extractions'] == before['evidence']['extractions']
    assert after['splits'] == before['splits']
    assert after['state_digest'] != before['state_digest']
    with pytest.raises(ValueError, match='stale'):
        with engine.write_tx(service.db_path) as conn:
            RecategorizationHandler().apply(conn, proposal['payload'])
    assert finance.transaction_detail(tid) == after
