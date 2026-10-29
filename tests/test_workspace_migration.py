"""The category/bank additions upgrade a consistent generated legacy copy."""
from pathlib import Path
import shutil
import sqlite3

from app.db import engine, migrate


def test_category_and_banking_upgrade_preserve_copied_ledger(tmp_path, monkeypatch):
    canonical = migrate.MIGRATIONS_DIR
    legacy_migrations = tmp_path / 'legacy-migrations'
    legacy_migrations.mkdir()
    for path in canonical.glob('*.sql'):
        if path.name < '040_':
            shutil.copy2(path, legacy_migrations / path.name)
    monkeypatch.setattr(migrate, 'MIGRATIONS_DIR', legacy_migrations)
    source = tmp_path / 'generated-legacy.sqlite'
    migrate.seed_sample(source)
    with engine.read_conn(source) as conn:
        expected = {
            table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
            for table in ('accounts', 'transactions', 'transaction_splits', 'merchant_resolution_events')
        }
        identity = migrate.read_database_identity(conn)
    proof = tmp_path / 'generated-copy.sqlite'
    with sqlite3.connect(source) as source_conn, sqlite3.connect(proof) as proof_conn:
        source_conn.backup(proof_conn)
    monkeypatch.setattr(migrate, 'MIGRATIONS_DIR', canonical)
    assert migrate.init_db(proof) == ['040_category_operations.sql', '041_bank_feed.sql', '042_household_onboarding.sql']
    assert migrate.init_db(proof) == []
    with engine.read_conn(proof) as conn:
        observed = {
            table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
            for table in expected
        }
        assert observed == expected
        assert migrate.read_database_identity(conn) == identity
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM category_operations').fetchone()[0] == 0
    with engine.read_conn(source) as conn:
        assert conn.execute('SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1').fetchone()[0] == '039_month_spine_budgets.sql'
