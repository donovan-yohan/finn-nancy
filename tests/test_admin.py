from __future__ import annotations

import sqlite3

from app.db import engine, repo_admin, repo_documents, repo_ledger
from app.services.backup import backup_now


def test_backup_now_snapshots_db(app_env):
    from app.config import get_settings

    settings = get_settings()
    with engine.read_conn(app_env) as conn:
        (want,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()

    target = backup_now(app_env, settings.data_dir)

    assert target.exists()
    conn = sqlite3.connect(str(target))
    try:
        (got,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    finally:
        conn.close()
    assert got == want
    assert (target.parent / "README.txt").exists()


def test_delete_document_undoes_import(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = repo_documents.insert_source_document(
            conn, kind="receipt", original_name="r.jpg", storage_ref="originals/blobs/aa/bb/r.jpg",
            sha256="a" * 64, mime_type="image/jpeg", status="processed",
        )
        other_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        cat = repo_ledger.find_category_by_name(conn, "Groceries")
        txn1 = repo_ledger.insert_transaction(
            conn, account_id=1, posted_on="2026-06-01", description="Loblaws",
            counterparty="Loblaws", amount_cents=-1000, source="receipt",
            external_id="rcpt:test-a", source_document_id=doc_id, source_confidence=0.9,
            flow_kind="purchase",
        )
        repo_ledger.insert_split(conn, transaction_id=txn1, category_id=cat["id"], amount_cents=-1000)
        txn2 = repo_ledger.insert_transaction(
            conn, account_id=1, posted_on="2026-06-02", description="Loblaws again",
            counterparty="Loblaws", amount_cents=-500, source="receipt",
            external_id="rcpt:test-b", source_document_id=doc_id, source_confidence=0.9,
            flow_kind="purchase",
        )
        repo_ledger.insert_split(conn, transaction_id=txn2, category_id=cat["id"], amount_cents=-500)

    with engine.write_tx(app_env) as conn:
        result = repo_admin.delete_document(conn, doc_id)

    assert result == {"document_id": doc_id, "transactions_deleted": 2, "existed": True}

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT * FROM transactions WHERE id IN (?,?)", (txn1, txn2)).fetchall() == []
        assert conn.execute("SELECT * FROM transaction_splits WHERE transaction_id IN (?,?)", (txn1, txn2)).fetchall() == []
        assert repo_documents.get_document(conn, doc_id) is None
        remaining = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert remaining == other_before

    with engine.write_tx(app_env) as conn:
        second = repo_admin.delete_document(conn, doc_id)
    assert second == {"document_id": doc_id, "transactions_deleted": 0, "existed": False}


def test_list_documents_newest_first_and_filtered(app_env):
    with engine.write_tx(app_env) as conn:
        d1 = repo_documents.insert_source_document(
            conn, kind="receipt", original_name="one.jpg", storage_ref="originals/blobs/1/1/one.jpg",
            sha256="1" * 64, mime_type="image/jpeg", status="processed",
        )
        d2 = repo_documents.insert_source_document(
            conn, kind="receipt", original_name="two.jpg", storage_ref="originals/blobs/2/2/two.jpg",
            sha256="2" * 64, mime_type="image/jpeg", status="needs_review",
        )

    with engine.read_conn(app_env) as conn:
        docs = repo_admin.list_documents(conn)
        needs_review = repo_admin.list_documents(conn, status="needs_review")

    ids = [d["id"] for d in docs]
    assert ids.index(d2) < ids.index(d1)
    assert [d["id"] for d in needs_review] == [d2]
