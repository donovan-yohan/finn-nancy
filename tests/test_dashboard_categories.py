"""Accepted categories and unresolved spending remain distinct on Home."""
from app.db import engine, repo_ledger, repo_merchant_knowledge as knowledge, views
from app.db.repo_merchant_knowledge import Evidence


def test_dashboard_category_breakdown_is_period_scoped_and_accepted_only(empty_db):
    with engine.write_tx(empty_db) as conn:
        conn.execute("INSERT INTO accounts(id,name,kind,currency) VALUES (1,'Generated account','chequing','CAD')")
        conn.execute("INSERT INTO categories(id,name,kind) VALUES (1,'Groceries','expense')")
        for month, amount in [('2026-05', -1100), ('2026-06', -2200)]:
            tid = repo_ledger.insert_transaction(conn, account_id=1, posted_on=month+'-01',
                description='Generated market', counterparty='Generated market', amount_cents=amount,
                source='manual', external_id=month, source_document_id=None,
                source_confidence=1.0, flow_kind='purchase')
            assert tid is not None
            sid = repo_ledger.insert_split(conn, transaction_id=tid, category_id=1, amount_cents=amount)
            knowledge.confirm_category(conn, descriptor='Generated market', category_id=1,
                scope=knowledge.scope_for_transaction(conn, tid), operation_key=month,
                actor='operator:test', reason='Generated approval',
                evidence=Evidence(transaction_id=tid, transaction_split_id=sid))
        tid = repo_ledger.insert_transaction(conn, account_id=1, posted_on='2026-06-02',
            description='Generated unresolved market', counterparty='Generated unresolved market',
            amount_cents=-500, source='manual', external_id='unresolved', source_document_id=None,
            source_confidence=1.0, flow_kind='purchase')
        assert tid is not None
        repo_ledger.insert_split(conn, transaction_id=tid, category_id=1, amount_cents=-500)
    with engine.read_conn(empty_db) as conn:
        context = views.dashboard_context(conn, 6, empty_db, month='2026-06')
    assert len(context['categories']) == 1
    assert context['categories'][0]['total_cents'] == -2200
    assert context['categories'][0]['category_id'] == 1
    assert context['stats']['money_out_cents'] == 2700
    assert context['category_resolution']['excluded_expense_cents'] == 500
    assert context['category_resolution']['unresolved_split_count'] == 1
