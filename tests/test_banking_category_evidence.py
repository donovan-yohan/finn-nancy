"""Category approval sees bank evidence changes, not only ledger fields."""
import pytest

from app.api import finance
from app.db import engine
from tests.test_banking_feed import banking, row  # noqa: F401


def test_bank_observation_change_invalidates_pending_category_approval(banking, monkeypatch):
    service, connection, account, pages, calls = banking
    monkeypatch.setenv('DB_PATH', str(service.db_path))
    from app.config import get_settings
    get_settings.cache_clear()
    service.sync(connection)
    subject = service.feed()[0]
    plan = service.review_plan(subject['id'])
    result = service.decide(subject['id'], fingerprint=plan['fingerprint'], operation_key='evidence:book',
        action='new', flow_kind='purchase', reason='Generated reviewed expense')
    tid = result['transaction_id']
    with engine.write_tx(service.db_path) as conn:
        category = conn.execute("INSERT INTO categories(name,kind) VALUES ('Generated meals','expense')").lastrowid
    before = finance.transaction_detail(tid)
    assert before['evidence']['bank_associations'][0]['transaction_id'] == tid
    proposal = finance.propose_category(operation_key='evidence:category', transaction_id=tid,
        split_id=before['splits'][0]['id'], to_category_id=category,
        expected_state_digest=before['state_digest'], rationale='Generated category proposal')
    pages.append({'added': [], 'modified': [row(amount=15.00)], 'removed': [],
                  'next_cursor': 'changed-evidence', 'has_more': False})
    service.sync(connection)
    after = finance.transaction_detail(tid)
    assert after['amount_cents'] == -1234
    assert after['state_digest'] != before['state_digest']
    from app.actions.recategorization import RecategorizationHandler
    with pytest.raises(ValueError, match='stale'):
        with engine.write_tx(service.db_path) as conn:
            RecategorizationHandler().apply(conn, proposal['payload'])
    with pytest.raises(ValueError, match='bank.*review'):
        finance.propose_category(operation_key='evidence:changed-category', transaction_id=tid,
            split_id=after['splits'][0]['id'], to_category_id=category,
            expected_state_digest=after['state_digest'], rationale='Changed evidence not resolved')
    get_settings.cache_clear()
