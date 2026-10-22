"""Narrow generated fixtures for the two independent UI review regressions."""
from pathlib import Path

from tests.browser.workspace_ui_fixture import generated_workspace


def generated_review(root: Path):
    app = generated_workspace(root)
    from app.db import engine, repo_merchant_knowledge as knowledge
    from app.db.repo_merchant_knowledge import Evidence

    with engine.write_tx(root / 'workspace.sqlite') as conn:
        conn.execute("INSERT INTO categories(id,name,kind) VALUES (900,'Generated accepted category','expense')")
        conn.execute("INSERT INTO categories(id,name,kind) VALUES (901,'Generated sibling category','expense')")
        ids = {}
        for i in range(55):
            key = f'accepted-{i}'
            amount = -2200 if i == 0 else -1200 if i == 1 else -100
            tid = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,
                counterparty,amount_cents,source,external_id,flow_kind)
                VALUES (1,'2026-06-14',?,?,?,'sample',?,'purchase')""",
                (f'Generated contributor {i:03}', key, amount, key)).lastrowid
            assert tid is not None
            sid = conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,900,?)',
                               (tid, -800 if i == 1 else amount)).lastrowid
            knowledge.confirm_category(conn, descriptor=key, category_id=900,
                scope=knowledge.scope_for_transaction(conn, tid), operation_key=key,
                actor='operator:test', reason='Generated approval',
                evidence=Evidence(transaction_id=tid, transaction_split_id=sid))
            if i == 1:
                sibling = conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,901,-400)', (tid,)).lastrowid
                knowledge.confirm_category(conn, descriptor=key, category_id=901,
                    scope=knowledge.scope_for_transaction(conn, tid), operation_key=key+'-sibling',
                    actor='operator:test', reason='Generated approval',
                    evidence=Evidence(transaction_id=tid, transaction_split_id=sibling))
            ids[key] = tid
        for key, flow, amount in [('unaccepted', 'purchase', -500), ('unknown', 'unknown', -300)]:
            tid = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,
                counterparty,amount_cents,source,external_id,flow_kind)
                VALUES (1,'2026-06-14',?,?,?,'sample',?,?)""",
                ('Generated contributor '+key, key, amount, key, flow)).lastrowid
            conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,900,?)', (tid, amount))
            ids[key] = tid

    @app.get('/__review_fixture')
    def manifest():
        return {'ids': ids, 'category': 900, 'contribution_cents': -8300}

    return app
