"""Split approvals must not become transaction-wide gold classification labels."""
from fastapi.testclient import TestClient
import pytest

from app.api import finance, service
from app.db import engine


def test_partial_split_approval_does_not_mislabel_whole_transaction(app_env):
    created = service.record_transaction(amount_cents=-1200, posted_on='2026-06-15',
        description='Generated mixed receipt', category='Uncategorized',
        flow_kind='purchase', external_id='generated-mixed-label')
    tid = created['transaction_id']
    with engine.write_tx(app_env) as conn:
        split = conn.execute('SELECT id,category_id FROM transaction_splits WHERE transaction_id=?', (tid,)).fetchone()
        conn.execute('UPDATE transaction_splits SET amount_cents=-800 WHERE id=?', (split['id'],))
        conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,?,?)',
            (tid, split['category_id'], -400))
        category = int(conn.execute("SELECT id FROM categories WHERE name='Restaurants'").fetchone()[0])
    before = finance.transaction_detail(tid)
    proposal = finance.propose_category(operation_key='generated:partial-label', transaction_id=tid,
        split_id=split['id'], to_category_id=category, expected_state_digest=before['state_digest'],
        rationale='Only the selected portion is a meal.')
    from app.web.app import create_app
    client = TestClient(create_app())
    response = client.post(f"/actions/{proposal['id']}/approve", follow_redirects=False)
    assert response.status_code == 303, response.text
    with engine.read_conn(app_env) as conn:
        assert conn.execute('SELECT COUNT(*) FROM classification_labels WHERE transaction_id=?', (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM v_active_merchant_resolution_claims WHERE transaction_split_id=? AND claim_kind='expense_category'", (split['id'],)).fetchone()[0] == 1
    changed = finance.transaction_detail(tid)
    assert changed['splits'][0]['category_id'] == category
    assert changed['splits'][1] == before['splits'][1]


@pytest.mark.parametrize('multisplit', [True, False])
def test_noop_confirmation_clears_only_multisplit_label(app_env, multisplit):
    from app.db import repo_labels
    from app.web.app import create_app

    tid = service.record_transaction(amount_cents=-1200, posted_on='2026-06-15',
        description='Generated noop label', category='Restaurants', flow_kind='purchase',
        external_id='generated-noop-label')['transaction_id']
    with engine.write_tx(app_env) as conn:
        split = conn.execute('SELECT * FROM transaction_splits WHERE transaction_id=?', (tid,)).fetchone()
        if multisplit:
            conn.execute('UPDATE transaction_splits SET amount_cents=-800 WHERE id=?', (split['id'],))
            conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,?,?)',
                (tid, split['category_id'], -400))
        repo_labels.record_label(conn, transaction_id=tid, category_id=split['category_id'],
            category_name='Restaurants', merchant='Generated noop label', description='Generated',
            amount_cents=-1200, neighbor_ids=[], confidence=1, source='generated-human')
        original_label = dict(conn.execute('SELECT * FROM classification_labels WHERE transaction_id=?', (tid,)).fetchone())
    before = finance.transaction_detail(tid)
    p = finance.propose_category(operation_key='generated:noop-label', transaction_id=tid,
        split_id=split['id'], to_category_id=split['category_id'],
        expected_state_digest=before['state_digest'], rationale='Confirm selected category')
    client = TestClient(create_app())
    assert client.post(f"/actions/{p['id']}/approve", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        labels = [dict(r) for r in conn.execute('SELECT * FROM classification_labels WHERE transaction_id=?', (tid,))]
        assert labels == ([] if multisplit else [original_label])
        assert conn.execute("SELECT COUNT(*) FROM v_active_merchant_resolution_claims WHERE transaction_split_id=? AND claim_kind='expense_category'", (split['id'],)).fetchone()[0] == 1
    assert finance.transaction_detail(tid)['splits'] == before['splits']
    assert client.post(f"/actions/{p['id']}/revert", follow_redirects=False).status_code == 303
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM v_active_merchant_resolution_claims WHERE transaction_split_id=? AND claim_kind='expense_category'", (split['id'],)).fetchone()[0] == 0
