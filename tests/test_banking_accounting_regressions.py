"""Generated-only accounting regressions at receipt, statement, SQL and HTTP seams."""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_banking_feed import banking, book, row  # noqa: F401 - fixture reuse
from app.config import Settings, get_settings
from app.db import engine, repo_documents, repo_ledger, repo_period_policy, repo_statements
from app.ingest.promote import promote_receipt
from app.ingest.schemas import ExtractedReceipt, ExtractedStatement, StatementRow
from app.reconcile.apply import confirm_match, promote_line, unreconcile_document
from app.reconcile.engine import _candidates


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def receipt_fixture(conn, *, day='2026-06-12', cents=1234, tag='e'):
    receipt = ExtractedReceipt(merchant='Generated Cafe', purchased_on=day,
                               total_cents=cents, currency='CAD', confidence=.99)
    doc = repo_documents.insert_source_document(conn, kind='receipt',
        original_name='generated.jpg', storage_ref=f'generated-{tag}.jpg', sha256=tag*64,
        mime_type='image/jpeg', status='needs_review')
    extraction = repo_documents.insert_extraction(conn, source_document_id=doc,
        doc_kind='receipt', extracted_json=receipt.model_dump_json(), confidence=.99,
        external_id=tag, proposed_account_id=None, proposed_category_id=None,
        review_status='pending')
    return receipt, doc, extraction


def statement_fixture(conn, account):
    doc = repo_documents.insert_source_document(conn, kind='statement',
        original_name='generated.pdf', storage_ref='generated.pdf', sha256='f'*64,
        mime_type='application/pdf')
    parsed = ExtractedStatement(institution='Generated Bank', currency='CAD',
        statement_period='2026-06', rows=[StatementRow(posted_on='2026-06-12',
        description='GENERATED CAFE', amount_cents=-1234)])
    repo_statements.stage_lines(conn, source_document_id=doc, account_id=account, parsed=parsed)
    return doc, repo_statements.lines_for_document(conn, doc)[0]


@pytest.mark.parametrize('order', ['RFS', 'RSF', 'FRS', 'FSR', 'SRF', 'SFR'])
def test_all_six_arrival_orders_preserve_one_booking_and_all_evidence(banking, order):
    service, connection, account, pages, calls = banking
    txn = None
    for step in order:
        if step == 'R':
            with engine.write_tx(service.db_path) as conn:
                receipt, receipt_doc, extraction = receipt_fixture(conn)
                raw = receipt.model_dump_json()
                outcome = promote_receipt(conn, source_document_id=receipt_doc,
                    sha256='e'*64, receipt=receipt, extraction_id=extraction, account_id=account)
                if txn is not None:
                    assert outcome['status'] == 'needs_review'
                    assert outcome['candidate_transaction_ids'] == [txn]
                else:
                    assert outcome['status'] == 'inserted'
                    txn = outcome['transaction_id']
            if outcome['status'] == 'needs_review':
                service.attach_receipt(extraction_id=extraction, transaction_id=txn,
                    operation_key='receipt-attach', reason='Generated same-purchase review')
        elif step == 'F':
            service.sync(connection)
            subject = service.feed()[0]['id']
            plan = service.review_plan(subject)
            if txn is not None:
                assert [r['id'] for r in plan['candidates']] == [txn]
                service.decide(subject, fingerprint=plan['fingerprint'], operation_key='feed-match',
                    action='match', transaction_id=txn, reason='Generated same-purchase review')
            else:
                txn = service.decide(subject, fingerprint=plan['fingerprint'], operation_key='feed-new',
                    action='new', flow_kind='purchase', reason='Generated reviewed meaning')['transaction_id']
        else:
            with engine.write_tx(service.db_path) as conn:
                statement_doc, line = statement_fixture(conn, account)
                candidates = _candidates(conn, line, set())
                if txn is not None:
                    assert [r['id'] for r in candidates] == [txn]
                    confirm_match(conn, line['id'], txn)
                else:
                    txn = promote_line(conn, line['id'])
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits').fetchone()[0] == -1234
        assert conn.execute('SELECT transaction_id FROM bank_associations').fetchone()[0] == txn
        line = repo_statements.lines_for_document(conn, statement_doc)[0]
        assert line['matched_transaction_id'] == txn
        assert line['match_status'] in {'matched', 'promoted'}
        extracted = conn.execute('SELECT * FROM ingest_extractions WHERE id=?', (extraction,)).fetchone()
        assert extracted['transaction_id'] == txn
        assert extracted['extracted_json'] == raw
        assert conn.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 2
        if order.startswith('S'):
            # Corroborating evidence is not an authorization to infer flow from sign.
            assert conn.execute('SELECT flow_kind FROM transactions').fetchone()[0] == 'unknown'


@pytest.mark.parametrize('evidence', ['active', 'revoked', 'receipt'])
def test_unreconcile_retains_historical_bank_and_receipt_evidence(banking, evidence):
    service, connection, account, pages, calls = banking
    with engine.write_tx(service.db_path) as conn:
        doc, line = statement_fixture(conn, account)
        txn = promote_line(conn, line['id'])
    if evidence in {'active', 'revoked'}:
        service.sync(connection)
        subject = service.feed()[0]['id']
        plan = service.review_plan(subject)
        service.decide(subject, fingerprint=plan['fingerprint'], operation_key='match-statement',
            action='match', transaction_id=txn, reason='Generated reviewed evidence')
        if evidence == 'revoked':
            plan = service.review_plan(subject)
            service.decide(subject, fingerprint=plan['fingerprint'], operation_key='revoke-statement',
                action='revoke', reason='Generated reviewed correction')
    else:
        with engine.write_tx(service.db_path) as conn:
            receipt, receipt_doc, extraction = receipt_fixture(conn)
        # Direct writer is intentional: this reproduces durable evidence independently of B1.
        with engine.write_tx(service.db_path) as conn:
            conn.execute("""INSERT INTO bank_receipt_evidence(extraction_id,source_document_id,
                transaction_id,actor,reason,operation_key) VALUES (?,?,?,'generated','Generated review','receipt')""",
                (extraction, receipt_doc, txn))
            repo_documents.link_extraction_txn(conn, extraction, txn, review_status='approved')
    with engine.read_conn(service.db_path) as conn:
        before = {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')]
                  for table in ['bank_associations', 'bank_revocations', 'bank_receipt_evidence']}
    with engine.write_tx(service.db_path) as conn:
        result = unreconcile_document(conn, doc)
        assert result == {'lines': 1, 'reset': 1, 'removed': 0}
    with engine.read_conn(service.db_path) as conn:
        assert dict(conn.execute('SELECT recon_status,cleared_on FROM transactions WHERE id=?',
                                (txn,)).fetchone()) == {'recon_status': 'uncleared', 'cleared_on': ''}
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits').fetchone()[0] == -1234
        assert repo_statements.lines_for_document(conn, doc)[0]['match_status'] == 'unmatched'
        for table, rows in before.items():
            assert [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] == rows


def test_unreconcile_still_removes_disposable_statement_promotion(banking):
    service, connection, account, pages, calls = banking
    with engine.write_tx(service.db_path) as conn:
        doc, line = statement_fixture(conn, account)
        promote_line(conn, line['id'])
        assert unreconcile_document(conn, doc) == {'lines': 1, 'reset': 0, 'removed': 1}
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM transaction_splits').fetchone()[0] == 0


def insert_receipt_evidence(conn, extraction, doc, txn):
    conn.execute("""INSERT INTO bank_receipt_evidence(extraction_id,source_document_id,
        transaction_id,actor,reason,operation_key) VALUES (?,?,?,'generated','Generated SQL review','sql-attach')""",
        (extraction, doc, txn))


@pytest.mark.parametrize('locked_month', ['2026-06', '2026-07'])
def test_sql_and_application_guard_both_receipt_and_target_months(banking, locked_month):
    service, connection, account, pages, calls = banking
    pages[0]['added'] = [row(date='2026-07-01')]
    subject, txn = book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn, day='2026-06-30')
        repo_period_policy.close_period(conn, locked_month, snapshot={'generated': True},
            exceptions=[], actor='generated', reason='Generated close', operation_key='close')
    with pytest.raises(Exception, match='closed|locked'):
        service.attach_receipt(extraction_id=extraction, transaction_id=txn,
            operation_key='app-attach', reason='Generated receipt review')
    with pytest.raises(sqlite3.IntegrityError, match='closed period bank receipt evidence'):
        with engine.write_tx(service.db_path) as conn:
            insert_receipt_evidence(conn, extraction, doc, txn)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_evidence').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1


@pytest.mark.parametrize('invalid', ['different_document', 'wrong_document_kind', 'wrong_extraction_kind',
    'booked', 'approved', 'malformed_json', 'non_object', 'bad_date', 'impossible_date',
    'missing_date', 'foreign_currency', 'wrong_amount', 'string_amount'])
def test_sql_receipt_identity_and_extraction_boundary_rejects_invalid_evidence(banking, invalid):
    service, connection, account, pages, calls = banking
    subject, txn = book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
        if invalid == 'different_document':
            _, doc, _ = receipt_fixture(conn, tag='d')
        elif invalid == 'wrong_document_kind':
            conn.execute("UPDATE source_documents SET kind='statement' WHERE id=?", (doc,))
        elif invalid == 'wrong_extraction_kind':
            conn.execute("UPDATE ingest_extractions SET doc_kind='statement' WHERE id=?", (extraction,))
        elif invalid == 'booked':
            repo_documents.link_extraction_txn(conn, extraction, txn, review_status='pending')
        elif invalid == 'approved':
            conn.execute("UPDATE ingest_extractions SET review_status='approved' WHERE id=?", (extraction,))
        else:
            raw = json.loads(receipt.model_dump_json())
            updates = {'bad_date': {'purchased_on': 'not-a-date'},
                'impossible_date': {'purchased_on': '2026-02-30'},
                'missing_date': {'purchased_on': None}, 'foreign_currency': {'currency': 'USD'},
                'wrong_amount': {'total_cents': 999}, 'string_amount': {'total_cents': '1234'}}
            raw.update(updates.get(invalid, {}))
            serialized = '{' if invalid == 'malformed_json' else ('[]' if invalid == 'non_object' else json.dumps(raw))
            conn.execute('UPDATE ingest_extractions SET extracted_json=? WHERE id=?', (serialized, extraction))
    with pytest.raises(sqlite3.IntegrityError, match='invalid bank receipt evidence'):
        with engine.write_tx(service.db_path) as conn:
            insert_receipt_evidence(conn, extraction, doc, txn)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_evidence').fetchone()[0] == 0


def test_valid_receipt_attachment_remains_append_only_and_preserves_extraction(banking):
    service, connection, account, pages, calls = banking
    subject, txn = book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
    result = service.attach_receipt(extraction_id=extraction, transaction_id=txn,
        operation_key='valid-attach', reason='Generated explicit review')
    assert result == {'status': 'attached', 'transaction_id': txn}
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT extracted_json FROM ingest_extractions WHERE id=?',
                            (extraction,)).fetchone()[0] == receipt.model_dump_json()
    for sql in ['DELETE FROM bank_receipt_evidence', "UPDATE bank_receipt_evidence SET reason='changed'"]:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            with engine.write_tx(service.db_path) as conn:
                conn.execute(sql)


def test_review_can_record_an_explicit_separate_equal_purchase_with_audit(banking, monkeypatch):
    from app.web.routes.review import router
    service, connection, account, pages, calls = banking
    subject, existing = book(service)
    monkeypatch.setenv('DB_PATH', service.db_path)
    get_settings.cache_clear()
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
    app = FastAPI()
    app.include_router(router)
    form = {'merchant': 'Generated Cafe reviewed', 'purchased_on': '2026-06-12',
        'total': '12.34', 'category_name': 'Uncategorized', 'account_id': str(account)}
    with TestClient(app, raise_server_exceptions=False) as client:
        ordinary = client.post(f'/review/{extraction}/approve', data=form, follow_redirects=False)
        assert ordinary.status_code == 409
        response = client.post(f'/review/{extraction}/approve', data={**form,
            'acknowledge_separate_purchase': 'true',
            'separate_purchase_reason': 'Generated review: two independently verified purchases'},
            follow_redirects=False)
        assert response.status_code == 303, response.text
        assert client.post(f'/review/{extraction}/approve', data={**form,
            'acknowledge_separate_purchase': 'true',
            'separate_purchase_reason': 'Generated review: two independently verified purchases'},
            follow_redirects=False).status_code == 303
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits').fetchone()[0] == -2468
        event = conn.execute('SELECT * FROM bank_receipt_decisions').fetchone()
        assert event['extraction_id'] == extraction
        assert event['source_document_id'] == doc
        assert event['transaction_id'] != existing
        assert event['actor'] == 'operator:receipt-review'
        assert event['decision'] == 'separate_purchase'
        assert event['reason'] == 'Generated review: two independently verified purchases'
        assert event['operation_key'] == f'receipt:{extraction}:separate-purchase'
        assert event['original_json'] == receipt.model_dump_json()
        assert json.loads(event['reviewed_json'])['merchant'] == 'Generated Cafe reviewed'
        assert json.loads(event['candidate_transaction_ids_json']) == [existing]
        assert conn.execute('SELECT extracted_json FROM ingest_extractions WHERE id=?',
                            (extraction,)).fetchone()[0] == receipt.model_dump_json()
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_decisions').fetchone()[0] == 1
    for sql in ['DELETE FROM bank_receipt_decisions', "UPDATE bank_receipt_decisions SET reason='changed'"]:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            with engine.write_tx(service.db_path) as conn:
                conn.execute(sql)


@pytest.mark.parametrize('missing', ['acknowledgement', 'human_review', 'account', 'reason'])
def test_separate_receipt_requires_explicit_human_account_and_reason(banking, missing):
    service, connection, account, pages, calls = banking
    book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
        kwargs = dict(human_review=True, account_id=account,
            acknowledge_separate_purchase=True, separate_purchase_reason='Generated separate purchase review')
        if missing == 'acknowledgement':
            kwargs['acknowledge_separate_purchase'] = False
        elif missing == 'human_review':
            kwargs['human_review'] = False
        elif missing == 'account':
            kwargs['account_id'] = None
        else:
            kwargs['separate_purchase_reason'] = ' '
        if missing == 'acknowledgement':
            outcome = promote_receipt(conn, source_document_id=doc, sha256='e'*64,
                receipt=receipt, extraction_id=extraction, **kwargs)
            assert outcome['status'] == 'needs_review'
        else:
            with pytest.raises(ValueError, match='separate_purchase_review_required'):
                promote_receipt(conn, source_document_id=doc, sha256='e'*64,
                    receipt=receipt, extraction_id=extraction, **kwargs)
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1


def test_receipt_review_renders_all_candidates_and_separate_choice_unchecked(banking, monkeypatch):
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = {}
            self.current = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'form':
                self.current = attrs.get('action')
                self.forms[self.current] = []
            elif self.current is not None:
                self.forms[self.current].append((tag, attrs))
        def handle_endtag(self, tag):
            if tag == 'form':
                self.current = None
    from app.web.routes.review import router
    service, connection, account, pages, calls = banking
    subject, existing = book(service)
    with engine.write_tx(service.db_path) as conn:
        other = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Generated second','chequing','CAD')").lastrowid
        doc, line = statement_fixture(conn, other)
        second = promote_line(conn, line['id'])
        receipt, receipt_doc, extraction = receipt_fixture(conn)
    monkeypatch.setenv('DB_PATH', service.db_path)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        page = client.get('/review')
        assert page.status_code == 200
        parsed = Forms()
        parsed.feed(page.text)
        form = parsed.forms.get(f'/review/{extraction}/attach')
        assert form is not None
        assert {int(a['value']) for tag, a in form if tag == 'option' and a.get('value')} == {existing, second}
        approve_form = parsed.forms[f'/review/{extraction}/approve']
        checkbox = next((a for tag, a in approve_form if a.get('name') == 'acknowledge_separate_purchase'), None)
        assert checkbox is not None and 'checked' not in checkbox
        assert any(a.get('name') == 'separate_purchase_reason' for tag, a in approve_form)
        attached = client.post(f'/review/{extraction}/attach', data={
            'transaction_id': str(second), 'reason': 'Generated review confirmed the statement purchase'},
            follow_redirects=False)
        assert attached.status_code == 303, attached.text
    with engine.read_conn(service.db_path) as conn:
        evidence = conn.execute('SELECT * FROM bank_receipt_evidence').fetchone()
        assert evidence['transaction_id'] == second
        assert evidence['source_document_id'] == receipt_doc
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_decisions').fetchone()[0] == 0


@pytest.mark.parametrize('amount,flow', [(12.34, 'income'), (12.34, 'interest'),
                                         (-12.34, 'purchase'), (-12.34, 'fee')])
def test_wrong_sign_review_is_structured_4xx_and_does_not_book(banking, amount, flow):
    from app.banking.web import build_router
    service, connection, account, pages, calls = banking
    pages[0]['added'] = [row(amount=amount)]
    service.sync(connection)
    subject = service.feed()[0]['id']
    plan = service.review_plan(subject)
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, raise_server_exceptions=False) as client:
        overview = client.get('/connections/data/overview').json()
        response = client.post(f'/connections/data/decision/{subject}',
            headers={'Origin': service.origin, 'X-Banking-CSRF': overview['csrf']},
            json={'fingerprint': plan['fingerprint'], 'operation_key': 'wrong-sign',
                'action': 'new', 'flow_kind': flow, 'reason': 'Generated explicit choice'})
        assert 400 <= response.status_code < 500, response.text
        assert response.json() == {'detail': 'flow_amount_mismatch'}
        html = client.get(f'/bank-feed/{subject}').text
        from html.parser import HTMLParser
        class Options(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = []
                self.in_flow = False
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'select':
                    self.in_flow = attrs.get('name') == 'flow_kind'
                if tag == 'option' and self.in_flow:
                    self.values.append(attrs.get('value'))
            def handle_endtag(self, tag):
                if tag == 'select':
                    self.in_flow = False
        options = Options()
        options.feed(html)
        assert set(options.values) == ({'unknown', 'purchase', 'fee'} if amount > 0
                                        else {'unknown', 'income', 'interest'})
    with engine.read_conn(service.db_path) as conn:
        for table in ['transactions', 'transaction_splits', 'bank_operations', 'bank_associations']:
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('historical_only', [False, True])
def test_sql_closed_binding_activation_backstop_keeps_raw_observations(banking, historical_only):
    service, connection, account, pages, calls = banking
    service.bind(connection, 'generated-account', account_id=account, included=False,
        ownership='shared', role='primary', expected_revision=1)
    service.sync(connection)
    if historical_only:
        pages.append({'added': [], 'modified': [row(date='2026-07-01')], 'removed': [],
                      'next_cursor': 'july', 'has_more': False})
        service.sync(connection)
    with engine.write_tx(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM v_bank_feed_blockers').fetchone()[0] == 0
        repo_period_policy.close_period(conn, '2026-06', snapshot={'generated': True},
            exceptions=[], actor='generated', reason='Generated close', operation_key='close')
    with pytest.raises(sqlite3.IntegrityError, match='closed period bank binding activation'):
        with engine.write_tx(service.db_path) as conn:
            conn.execute("""INSERT INTO bank_bindings(bank_account_id,revision,account_id,included,
                ownership,role,actor) VALUES (1,3,?,1,'shared','primary','generated')""", (account,))
    with pytest.raises(Exception, match='closed|locked'):
        service.bind(connection, 'generated-account', account_id=account, included=True,
            ownership='shared', role='primary', expected_revision=2)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0] == (2 if historical_only else 1)
        assert conn.execute('SELECT included FROM v_bank_bindings').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM v_bank_feed_blockers').fetchone()[0] == 0


@pytest.mark.parametrize('invalid', ['malformed_json', 'bad_date', 'impossible_date'])
def test_invalid_receipt_extraction_is_structured_review_not_http_500(banking, invalid):
    from app.banking.web import build_router
    service, connection, account, pages, calls = banking
    subject, txn = book(service)
    with engine.write_tx(service.db_path) as conn:
        receipt, doc, extraction = receipt_fixture(conn)
        raw = '{' if invalid == 'malformed_json' else receipt.model_copy(update={
            'purchased_on': 'not-a-date' if invalid == 'bad_date' else '2026-02-30'}).model_dump_json()
        conn.execute('UPDATE ingest_extractions SET extracted_json=? WHERE id=?', (raw, extraction))
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, raise_server_exceptions=False) as client:
        csrf = client.get('/connections/data/overview').json()['csrf']
        response = client.post('/connections/data/receipt',
            headers={'Origin': service.origin, 'X-Banking-CSRF': csrf},
            json={'extraction_id': extraction, 'transaction_id': txn,
                  'operation_key': 'invalid-attach', 'reason': 'Generated review'})
        assert response.status_code == 409, response.text
        assert response.json() == {'detail': 'invalid_receipt_extraction'}
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_evidence').fetchone()[0] == 0
