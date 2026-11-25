"""Generated-only evidence and accounting-boundary tests."""
import sqlite3
from types import SimpleNamespace

import pytest

from app.banking.client import BankingError
from app.db import engine, repo_rogers_alerts as alerts
from app.db import repo_period_policy


def sample(key='a', **overrides):
    return SimpleNamespace(**({'message_key': key * 64, 'authorized_on': '2026-04-05',
                               'card_last4': '0123', 'amount_cents': -765,
                               'currency': 'CAD', 'merchant': 'Example Store'} | overrides))


def test_replays_equal_charges_and_review_do_not_touch_ledger(empty_db):
    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Example card','credit','CAD')").lastrowid
        key = 'b' * 64
        assert alerts.advance(conn, key, None, (14, 4), alert=sample()) == 'staged'
        assert alerts.advance(conn, key, (14, 4), (14, 5), alert=sample()) == 'duplicate'
        assert alerts.advance(conn, key, (14, 5), (14, 6), alert=sample('c')) == 'staged'
        assert alerts.advance(conn, key, (14, 6), (14, 7), alert=sample(merchant='Different')) == 'identity_conflict'
        rows = alerts.list_alerts(conn, key, {'0123': account})
        assert len(rows) == 2 and all(row['suggested_account_id'] == account for row in rows)
        assert {row['merchant'] for row in rows} == {'Example Store'}
        first, second = [row['id'] for row in rows]
        assert alerts.decide(conn, first, 0, 'confirm', account, 'Checked card', key, {'0123': account})['status'] == 'confirm'
        assert alerts.decide(conn, first, 0, 'confirm', account, 'Checked card', key, {'0123': account})['status'] == 'confirm'
        with pytest.raises(BankingError, match='stale_alert_review'):
            alerts.decide(conn, first, 0, 'dismiss', None, 'No longer needed', key, {})
        assert alerts.decide(conn, second, 0, 'dismiss', None, 'Already checked', key, {})['status'] == 'dismiss'
        assert [row['decision'] for row in alerts.list_alerts(conn, key, {})] == ['confirm', 'dismiss']
        for table in ('transactions', 'transaction_splits', 'source_documents', 'bank_observations'):
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_unmapped_or_stale_scope_cannot_confirm(empty_db):
    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Example card','credit','CAD')").lastrowid
        key = 'b' * 64
        alerts.advance(conn, key, None, (14, 4), alert=sample())
        alert_id = alerts.list_alerts(conn, key, {})[0]['id']
        for scope, cards in [(key, {}), ('d' * 64, {'0123': account})]:
            assert alerts.list_alerts(conn, scope, cards)[0]['suggested_account_id'] is None
            with pytest.raises(BankingError, match='alert_account_changed'):
                alerts.decide(conn, alert_id, 0, 'confirm', account, 'Check', scope, cards)


def test_stale_cursor_cannot_leave_an_alert(empty_db):
    with engine.write_tx(empty_db) as conn:
        key = 'b' * 64
        alerts.advance(conn, key, None, (14, 4))
        with pytest.raises(BankingError, match='mailbox_cursor_changed'):
            alerts.advance(conn, key, None, (14, 5), alert=sample())
        assert alerts.cursor(conn, key) == (14, 4)
        assert alerts.list_alerts(conn, key, {}) == []


def test_verified_duplicate_retains_one_alert_across_mailbox_migration(empty_db):
    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Example card','credit','CAD')").lastrowid
        old, new = 'b' * 64, 'd' * 64
        alerts.advance(conn, old, None, (14, 4), alert=sample())
        assert alerts.advance(conn, new, None, (20, 1), alert=sample(merchant='Different')) == 'identity_conflict'
        assert alerts.list_alerts(conn, new, {'0123': account})[0]['suggested_account_id'] is None
        assert alerts.advance(conn, new, (20, 1), (20, 2), alert=sample()) == 'duplicate'
        rows = alerts.list_alerts(conn, new, {'0123': account})
        assert len(rows) == 1 and rows[0]['suggested_account_id'] == account


def test_replace_cannot_rewrite_alert_or_review(empty_db):
    with engine.write_tx(empty_db) as conn:
        key = 'b' * 64
        alerts.advance(conn, key, None, (14, 1), alert=sample())
        alert_id = alerts.list_alerts(conn, key, {})[0]['id']
        review_id = alerts.decide(conn, alert_id, 0, 'dismiss', None, 'First', key, {})['review_id']
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('''INSERT OR REPLACE INTO rogers_alerts
                (id,source_key,message_key,authorized_on,card_last4,amount_cents,currency,merchant)
                VALUES(?,?,?,'2026-04-05','0123',-765,'CAD','Changed')''', (alert_id, key, 'a'*64))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('''INSERT OR REPLACE INTO rogers_alert_reviews (id,alert_id,previous_id,decision,reason)
                VALUES(?,?,?,'dismiss','Changed')''', (review_id, alert_id, review_id))


def test_review_chain_only_accepts_a_forward_extension(empty_db):
    with engine.write_tx(empty_db) as conn:
        key = 'b' * 64
        alerts.advance(conn, key, None, (14, 1), alert=sample())
        alert_id = alerts.list_alerts(conn, key, {})[0]['id']
        conn.execute("INSERT INTO rogers_alert_reviews(id,alert_id,decision,reason) VALUES(10,?,'dismiss','First')",
                     (alert_id,))
        with pytest.raises(sqlite3.IntegrityError, match='invalid alert review order'):
            conn.execute('''INSERT INTO rogers_alert_reviews(id,alert_id,previous_id,decision,reason)
                VALUES(5,?,10,'dismiss','Out of order')''', (alert_id,))
        assert alerts.decide(conn, alert_id, 10, 'dismiss', None, 'Second', key, {})['review_id'] > 10
        assert [tuple(row) for row in conn.execute(
            'SELECT previous_id, reason FROM rogers_alert_reviews ORDER BY id')] == [(None, 'First'), (10, 'Second')]


def test_alert_cannot_block_close_or_bypass_closed_review_guard(empty_db):
    with engine.write_tx(empty_db) as conn:
        key = 'b' * 64
        alerts.advance(conn, key, None, (14, 4), alert=sample())
        repo_period_policy.close_period(conn, '2026-04', snapshot={'generated': True}, exceptions=[],
            actor='generated-test', reason='Generated close', operation_key='generated-close')
        alerts.advance(conn, key, (14, 4), (14, 5), alert=sample('c'))
        alert_id = alerts.list_alerts(conn, key, {})[0]['id']
        with pytest.raises(repo_period_policy.PeriodPolicyError):
            alerts.decide(conn, alert_id, 0, 'dismiss', None, 'Reviewed', key, {})
        with pytest.raises(sqlite3.IntegrityError, match='closed period alert review'):
            conn.execute("INSERT INTO rogers_alert_reviews(alert_id,decision,reason) VALUES (?,'dismiss','Reviewed')",
                         (alert_id,))
        assert conn.execute('SELECT COUNT(*) FROM rogers_alerts').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM rogers_alert_reviews').fetchone()[0] == 0


@pytest.mark.parametrize('arrival', ['alert_first', 'receipt_first', 'statement_first'])
def test_authorization_does_not_change_statement_reconciliation(empty_db, arrival):
    from app.db import repo_documents, repo_ledger, repo_statements
    from app.ingest.schemas import ExtractedStatement, StatementRow
    from app.reconcile import apply, feed
    from app.reconcile.engine import reconcile_document

    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Example card','credit','CAD')").lastrowid
        key = 'b' * 64

        def alert():
            alerts.advance(conn, key, None, (14, 1), alert=sample())
            alert_id = alerts.list_alerts(conn, key, {'0123': account})[0]['id']
            before = conn.execute('SELECT * FROM transactions ORDER BY id').fetchall()
            assert alerts.decide(conn, alert_id, 0, 'confirm', account, 'Verified account', key, {'0123': account})['status'] == 'confirm'
            assert conn.execute('SELECT * FROM transactions ORDER BY id').fetchall() == before

        if arrival == 'alert_first':
            alert()
        candidate = None
        if arrival == 'receipt_first':
            candidate = repo_ledger.insert_transaction(conn, account_id=account, posted_on='2026-04-05',
                description='Example Store', counterparty='Example Store', amount_cents=-765, source='receipt',
                external_id='generated-receipt', source_document_id=None, source_confidence=1, flow_kind='purchase')
            repo_ledger.insert_split(conn, transaction_id=candidate,
                category_id=repo_ledger.ensure_uncategorized(conn), amount_cents=-765)
            alert()
        doc = repo_documents.insert_source_document(conn, kind='statement', original_name='generated.pdf',
            storage_ref='generated.pdf', sha256='d'*64, mime_type='application/pdf', status='processed')
        repo_statements.stage_lines(conn, source_document_id=doc, account_id=account, parsed=ExtractedStatement(
            institution='Example Bank', currency='CAD', statement_period='2026-04', rows=[
                StatementRow(posted_on='2026-04-05', description='Example Store', amount_cents=-765)]))

    assert reconcile_document(empty_db, doc, llm=None)['needs_review'] == 1
    with engine.write_tx(empty_db) as conn:
        line = repo_statements.lines_for_document(conn, doc)[0]['id']
        if candidate is not None:
            apply.confirm_match(conn, line, candidate)
        else:
            candidate = apply.promote_line(conn, line)
        if arrival == 'statement_first':
            alert()
            assert [t['id'] for t in feed.receipt_candidates(conn, amount_cents=-765, posted_on='2026-04-05')] == [candidate]
        assert [tuple(row) for row in conn.execute('SELECT id,recon_status FROM transactions')] == [(candidate, 'cleared')]
        assert [tuple(row) for row in conn.execute('SELECT transaction_id,amount_cents FROM transaction_splits')] == [(candidate, -765)]
        assert conn.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
