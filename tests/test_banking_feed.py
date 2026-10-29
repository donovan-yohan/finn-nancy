"""Synthetic canonical feed: sync never books; review writes one signed booking."""
import json
import secrets
from decimal import Decimal

import httpx
import pytest

from app.db import engine, repo_ledger


@pytest.fixture
def banking(household_db, tmp_path):
    from app.banking.service import BankingService, CONSENT_VERSION
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credentials.chmod(0o600)
    pages = [{'added': [row()], 'modified': [], 'removed': [], 'next_cursor': 'cursor-one', 'has_more': False}]
    calls = []
    def provider(request):
        calls.append(request.url.path)
        assert request.url.host == 'production.plaid.com'
        if request.url.path == '/transactions/sync':
            return httpx.Response(200, json=pages.pop(0))
        raise AssertionError(request.url.path)
    service = BankingService(db_path=household_db, private_dir=tmp_path / 'private',
        credentials_file=credentials, origin='http://banking.example.invalid', completion_uri='http://banking.example.invalid/connections',
        enabled=lambda: True, strict_local=lambda: False, read_only=lambda: False,
        home_currency='CAD', transport=httpx.MockTransport(provider))
    service.consent(1, granted=True, version=CONSENT_VERSION)
    token_ref = service.store.put('generated-access-' + secrets.token_hex(12))
    with engine.write_tx(household_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Generated chequing','chequing','CAD')").lastrowid
        connection = conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (1,'generated-item',?,?,'active')", (token_ref, CONSENT_VERSION)).lastrowid
        conn.execute("INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (?,'generated-account','Chequing','CAD','chequing')", (connection,))
    service.bind(connection, 'generated-account', account_id=account, included=True,
                 ownership='shared', role='primary', expected_revision=0)
    return service, connection, account, pages, calls


def row(**updates):
    return {'transaction_id': 'generated-txn', 'account_id': 'generated-account',
        'amount': 12.34, 'iso_currency_code': 'CAD', 'date': '2026-06-12',
        'pending': False, 'pending_transaction_id': None, 'merchant_name': 'Generated Cafe',
        'name': 'GENERATED CAFE ORIGINAL', **updates}


def test_sync_then_explicit_review_books_one_uncleared_balanced_transaction(banking):
    service, connection, account, pages, calls = banking
    assert service.sync(connection)['observations'] == 1
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
    subject = service.feed()[0]
    assert subject['amount_cents'] == -1234
    plan = service.review_plan(subject['id'])
    assert plan['candidates'] == []
    result = service.decide(subject['id'], fingerprint=plan['fingerprint'], operation_key='generated-book',
                            action='new', flow_kind='purchase', reason='Reviewed cafe expense')
    assert service.decide(subject['id'], fingerprint=plan['fingerprint'], operation_key='generated-book',
                            action='new', flow_kind='purchase', reason='Reviewed cafe expense') == result
    with engine.read_conn(service.db_path) as conn:
        txn = conn.execute('SELECT * FROM transactions').fetchone()
        assert txn['account_id'] == account
        assert txn['source'] == 'bank_feed'
        assert txn['flow_kind'] == 'purchase'
        assert txn['recon_status'] == 'uncleared'
        assert txn['cleared_on'] == ''
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits').fetchone()[0] == -1234
        assert conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0] == 1


def book(service):
    service.sync(1)
    subject=service.feed()[0]['id']
    p=service.review_plan(subject)
    result=service.decide(subject,fingerprint=p['fingerprint'],operation_key='book',action='new',flow_kind='purchase',reason='Generated review')
    return subject,result['transaction_id']


def test_late_receipt_goes_to_review_and_explicit_attachment_preserves_bytes(banking,monkeypatch):
    from app.ingest.promote import promote_receipt
    from app.ingest.schemas import ExtractedReceipt
    from app.db import repo_documents
    from app.config import get_settings, Settings
    Settings.model_config['env_file']=None
    get_settings.cache_clear()
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    receipt=ExtractedReceipt(merchant='Generated Cafe',purchased_on='2026-06-12',total_cents=1234,currency='CAD',confidence=.99)
    raw=receipt.model_dump_json()
    with engine.write_tx(service.db_path) as conn:
        doc=repo_documents.insert_source_document(conn,kind='receipt',original_name='generated.jpg',storage_ref='generated.jpg',sha256='a'*64,mime_type='image/jpeg')
        extraction=repo_documents.insert_extraction(conn,source_document_id=doc,doc_kind='receipt',extracted_json=raw,confidence=.99,external_id='generated',proposed_account_id=None,proposed_category_id=None,review_status='auto')
        result=promote_receipt(conn,source_document_id=doc,sha256='a'*64,receipt=receipt,extraction_id=extraction)
        assert result['status']=='needs_review'
        assert result['reason']=='bank_feed_receipt_candidate'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==1
        assert conn.execute('SELECT status FROM source_documents WHERE id=?',(doc,)).fetchone()[0]=='needs_review'
    attached=service.attach_receipt(extraction_id=extraction,transaction_id=txn,operation_key='attach',reason='Reviewed receipt evidence')
    assert attached['status']=='attached'
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT extracted_json FROM ingest_extractions WHERE id=?',(extraction,)).fetchone()[0]==raw
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM bank_receipt_evidence').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM merchant_resolution_claims').fetchone()[0]==0


def test_changed_associated_feed_blocks_close_without_mutating_ledger(banking):
    from app.close.checklist import build_checklist
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    pages.append({'added':[],'modified':[row(amount=13.34)],'removed':[],'next_cursor':'changed','has_more':False})
    service.sync(connection)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT amount_cents FROM transactions WHERE id=?',(txn,)).fetchone()[0]==-1234
        check=build_checklist(conn,'2026-06')
        assert check['has_blocking_bank_feed']
        row_check=next(r for r in check['rows'] if r['key']=='bank_feed')
        assert row_check['href']=='/bank-feed'
        assert row_check['count']==1
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0]==2


def test_review_candidates_include_cleared_and_equal_charges_remain_ambiguous(banking):
    service,connection,account,pages,calls=banking
    with engine.write_tx(service.db_path) as conn:
        for index in range(2):
            txn=repo_ledger.insert_transaction(conn,account_id=account,posted_on='2026-06-12',description='Generated Cafe',counterparty='Generated Cafe',amount_cents=-1234,source='statement',external_id=f'generated-cleared-{index}',source_document_id=None,source_confidence=1,flow_kind='purchase')
            repo_ledger.insert_split(conn,transaction_id=txn,category_id=repo_ledger.ensure_uncategorized(conn),amount_cents=-1234)
            conn.execute("UPDATE transactions SET recon_status='cleared',cleared_on='2026-06-30' WHERE id=?",(txn,))
    service.sync(connection)
    subject=service.feed()[0]['id']
    plan=service.review_plan(subject)
    assert len(plan['candidates'])==2
    with pytest.raises(ValueError,match='candidate_acknowledgement_required'):
        service.decide(subject,fingerprint=plan['fingerprint'],operation_key='new-without-ack',action='new',flow_kind='purchase',reason='Generated')
    target=plan['candidates'][0]['id']
    service.decide(subject,fingerprint=plan['fingerprint'],operation_key='match-cleared',action='match',transaction_id=target,reason='Reviewed statement evidence')
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==2
        assert conn.execute('SELECT recon_status FROM transactions WHERE id=?',(target,)).fetchone()[0]=='cleared'


@pytest.mark.parametrize('update,blocker', [({'pending':True},'pending'),({'iso_currency_code':'USD'},'currency_review')])
def test_pending_and_foreign_subjects_cannot_book(banking,update,blocker):
    service,connection,account,pages,calls=banking
    pages[0]['added']=[row(**update)]
    service.sync(connection)
    subject=service.feed()[0]['id']; p=service.review_plan(subject)
    with pytest.raises(ValueError,match=blocker):
        service.decide(subject,fingerprint=p['fingerprint'],operation_key='blocked',action='new',flow_kind='purchase',reason='Generated')


def test_joint_secondary_matches_primary_booking_and_cannot_double_book(banking):
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    from app.banking.service import CONSENT_VERSION
    service.consent(2,granted=True,version=CONSENT_VERSION)
    with engine.write_tx(service.db_path) as conn:
        other=conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) SELECT 2,'generated-joint',token_ref,consent_version,'active' FROM bank_connections WHERE id=?",(connection,)).lastrowid
        conn.execute("INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (?,'generated-account','Joint','CAD','chequing')",(other,))
    with pytest.raises(Exception,match='primary feed'):
        service.bind(other,'generated-account',account_id=account,included=True,ownership='shared',role='primary',expected_revision=0)
    service.bind(other,'generated-account',account_id=account,included=True,ownership='shared',role='secondary',expected_revision=0)
    pages.append({'added':[row(transaction_id='joint-txn')],'modified':[],'removed':[],'next_cursor':'joint','has_more':False})
    service.sync(other)
    subject=next(r['id'] for r in service.feed() if r['id']!=subject)
    p=service.review_plan(subject)
    with pytest.raises(ValueError,match='secondary_feed_cannot_book'):
        service.decide(subject,fingerprint=p['fingerprint'],operation_key='joint-new',action='new',flow_kind='purchase',acknowledge_candidates=True,reason='Generated')
    service.decide(subject,fingerprint=p['fingerprint'],operation_key='joint-match',action='match',transaction_id=txn,reason='Reviewed same joint charge')
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0]==2


def test_stale_plan_and_changed_operation_replay_fail_closed(banking):
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    p=service.review_plan(subject)
    with pytest.raises(ValueError,match='operation_key_conflict'):
        service.decide(subject,fingerprint=p['fingerprint'],operation_key='book',action='new',flow_kind='purchase',reason='Different reason')
    with engine.write_tx(service.db_path) as conn:
        conn.execute("UPDATE transactions SET notes='intervening note' WHERE id=?",(txn,))
    with pytest.raises(ValueError,match='stale_review_plan'):
        service.decide(subject,fingerprint=p['fingerprint'],operation_key='stale-revoke',action='revoke',reason='Generated')


def test_sql_backstops_preserve_accepted_account_amount_and_immutable_evidence(banking):
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    for sql in ("UPDATE bank_observations SET merchant='changed'",'DELETE FROM bank_associations',
                'UPDATE transactions SET amount_cents=-999 WHERE id=1'):
        with pytest.raises(Exception):
            with engine.write_tx(service.db_path) as conn:
                conn.execute(sql)


def test_complete_window_is_atomic_and_generation_race_rejects_activation(banking):
    service,connection,account,pages,calls=banking
    pages[0]['has_more']=True
    pages.append({'added':[row(amount=Decimal('1.001'))],'modified':[],'removed':[],'next_cursor':'bad','has_more':False})
    # JSON transport cannot serialize Decimal; provider malformed exact decimal is a string.
    pages[1]['added'][0]['amount']='1.001'
    with pytest.raises(ValueError,match='invalid_provider_transaction'):
        service.sync(connection)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT generation FROM bank_connections').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0]==0
    def racing(req):
        with engine.write_tx(service.db_path) as conn:
            conn.execute('UPDATE bank_connections SET generation=generation+1 WHERE id=?',(connection,))
        return httpx.Response(200,json={'added':[row()],'modified':[],'removed':[],'next_cursor':'race','has_more':False})
    service.transport=httpx.MockTransport(racing)
    with pytest.raises(ValueError,match='stale_sync_window'):
        service.sync(connection)


def test_strict_local_read_only_and_withdrawal_stop_each_sync_page(banking):
    from app.banking.service import CONSENT_VERSION
    service,connection,account,pages,calls=banking
    service.strict_local=lambda:True
    with pytest.raises(ValueError,match='egress_blocked'):
        service.sync(connection)
    assert calls==[]
    service.strict_local=lambda:False
    service.read_only=lambda:True
    with pytest.raises(ValueError,match='read_only'):
        service.sync(connection)
    service.read_only=lambda:False
    service.consent(1,granted=False,version=CONSENT_VERSION)
    with pytest.raises(ValueError,match='current_member_consent_required'):
        service.sync(connection)
    assert calls==[]


def test_association_then_statement_clearing_preserves_single_booking(banking):
    from app.db import repo_statements, repo_documents
    from app.ingest.schemas import ExtractedStatement, StatementRow
    from app.reconcile.apply import confirm_match
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    with engine.write_tx(service.db_path) as conn:
        doc=repo_documents.insert_source_document(conn,kind='statement',original_name='generated-statement.pdf',storage_ref='generated-statement.pdf',sha256='c'*64,mime_type='application/pdf')
        parsed=ExtractedStatement(institution='Generated Bank',currency='CAD',statement_period='2026-06',
            rows=[StatementRow(posted_on='2026-06-12',description='GENERATED CAFE',amount_cents=-1234)])
        repo_statements.stage_lines(conn,source_document_id=doc,account_id=account,parsed=parsed)
        line=repo_statements.lines_for_document(conn,doc)[0]
        confirm_match(conn,line['id'],txn)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==1
        assert conn.execute('SELECT recon_status FROM transactions').fetchone()[0]=='cleared'


def test_receipt_candidates_are_visible_on_feed_for_explicit_attachment(banking):
    from app.db import repo_documents
    from app.ingest.schemas import ExtractedReceipt
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    with engine.write_tx(service.db_path) as conn:
        doc=repo_documents.insert_source_document(conn,kind='receipt',original_name='generated.jpg',storage_ref='late.jpg',sha256='b'*64,mime_type='image/jpeg',status='needs_review')
        receipt=ExtractedReceipt(merchant='Generated Cafe',purchased_on='2026-06-12',total_cents=1234,currency='CAD')
        extraction=repo_documents.insert_extraction(conn,source_document_id=doc,doc_kind='receipt',extracted_json=receipt.model_dump_json(),confidence=.9,external_id='late',proposed_account_id=None,proposed_category_id=None,review_status='pending')
    candidates=service.pending_receipts()
    assert candidates[0]['extraction_id']==extraction
    assert candidates[0]['candidates'][0]['id']==txn


def test_unreviewed_feed_is_rejected_by_actual_period_close_sql_guard(banking):
    from app.db import repo_period_policy
    service,connection,account,pages,calls=banking
    service.sync(connection)
    with pytest.raises(Exception,match='bank feed review required'):
        with engine.write_tx(service.db_path) as conn:
            repo_period_policy.close_period(conn,'2026-06',snapshot={'generated':True},exceptions=[],
                actor='generated-test',reason='Generated close attempt',operation_key='generated-close')


def test_sql_exclusion_cannot_mutate_closed_period_evidence(banking):
    from app.db import repo_period_policy
    service,connection,account,pages,calls=banking
    with engine.write_tx(service.db_path) as conn:
        repo_period_policy.close_period(conn,'2026-06',snapshot={'generated':True},exceptions=[],
            actor='generated-test',reason='Generated empty close',operation_key='generated-close')
    service.sync(connection)
    subject=service.feed()[0]
    with pytest.raises(Exception,match='closed period'):
        with engine.write_tx(service.db_path) as conn:
            conn.execute("INSERT INTO bank_exclusions(subject_id,observation_id,actor,reason,operation_key) VALUES (?,?,'generated-test','Generated','direct-exclude')",(subject['id'],subject['observation_id']))


def test_removal_retains_accepted_evidence_and_revoke_requires_explicit_review(banking):
    service,connection,account,pages,calls=banking
    subject,txn=book(service)
    pages.append({'added':[],'modified':[],'removed':[{'transaction_id':'generated-txn','account_id':'generated-account'}],'next_cursor':'removed','has_more':False})
    service.sync(connection)
    p=service.review_plan(subject)
    assert 'changed_associated_evidence' in p['blockers']
    assert p['subject']['amount_cents']==-1234
    service.decide(subject,fingerprint=p['fingerprint'],operation_key='revoke',action='revoke',reason='Provider removed this evidence; ledger retained for statement review')
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM bank_revocations').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0]==2


def test_guarded_new_booking_rejects_system_and_relationship_flows(banking):
    service,connection,account,pages,calls=banking
    service.sync(connection)
    subject=service.feed()[0]['id'];p=service.review_plan(subject)
    for flow in ['unknown','refund','reimbursement','internal_transfer','card_payment','adjustment','opening']:
        with pytest.raises(ValueError,match='flow_requires_reviewed_relationship'):
            service.decide(subject,fingerprint=p['fingerprint'],operation_key='flow-'+flow,action='new',flow_kind=flow,reason='Generated')


def test_consent_withdrawn_between_pages_prevents_second_dispatch(banking):
    from app.banking.service import CONSENT_VERSION
    service,connection,account,pages,calls=banking
    dispatches=[]
    def withdraw(req):
        dispatches.append(req.url.path)
        service.consent(1,granted=False,version=CONSENT_VERSION)
        return httpx.Response(200,json={'added':[row()],'modified':[],'removed':[],'next_cursor':'first','has_more':True})
    service.transport=httpx.MockTransport(withdraw)
    with pytest.raises(ValueError,match='current_member_consent_required'):
        service.sync(connection)
    assert dispatches==['/transactions/sync']
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0]==0
