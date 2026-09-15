from __future__ import annotations

import json
import sqlite3

from app.db import engine, repo_ledger, repo_rag


def _insert_costco_receipt(conn, *, posted_on: str = "2026-03-14", merchant: str = "COSTCO WHOLESALE") -> int:
    doc_id = conn.execute(
        """
        INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
        VALUES ('receipt', 'costco.jpg', ?, ?, 'image/jpeg', 'processed')
        """,
        (f"originals/blobs/costco-{posted_on}.jpg", f"sha-costco-{posted_on}"),
    ).lastrowid
    extracted = {
        "merchant": merchant,
        "purchased_on": posted_on,
        "currency": "CAD",
        "subtotal_cents": 17150,
        "tax_cents": 1202,
        "tip_cents": 0,
        "total_cents": 18352,
        "line_items": [
            {"description": "organic bananas", "amount_cents": 599},
            {"description": "coffee beans", "amount_cents": 1899},
            {"description": "paper towels", "amount_cents": 2499},
        ],
        "card_last4": "4242",
        "category_guess": "Groceries",
        "confidence": 0.98,
        "unreadable_fields": [],
    }
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=3,
        posted_on=posted_on,
        description=merchant,
        counterparty=merchant,
        amount_cents=-18352,
        source="receipt",
        external_id=f"rcpt:costco-{posted_on}",
        source_document_id=doc_id,
        source_confidence=0.98,
        flow_kind="purchase",
        notes="warehouse receipt",
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=4,
        amount_cents=-18352,
        memo="Costco groceries",
    )
    conn.execute(
        """
        INSERT INTO ingest_extractions(
          source_document_id, doc_kind, extracted_json, confidence, external_id,
          proposed_account_id, proposed_category_id, review_status, transaction_id
        ) VALUES (?, 'receipt', ?, 0.98, ?, 3, 4, 'auto', ?)
        """,
        (doc_id, json.dumps(extracted), f"rcpt:costco-{posted_on}", txn_id),
    )
    return int(txn_id)


def test_chunk_builder_is_deterministic(sample_db):
    with engine.write_tx(sample_db) as conn:
        before = repo_rag.transaction_chunk(conn, 3)
        assert before is not None
        rebuilt = repo_rag.rebuild_rag_chunks(conn, 3)
        after = repo_rag.transaction_chunk(conn, 3)

    assert rebuilt == 1
    assert after is not None
    assert before["content"] == after["content"]
    assert "content_sha256" not in before
    assert "content_sha256" not in after


def test_bare_sqlite_core_dml_syncs_rag_without_sha256_udf(empty_db):
    conn = sqlite3.connect(empty_db)

    def chunk_content(txn_id: int) -> str | None:
        row = conn.execute("SELECT content FROM rag_chunks WHERE ref_kind='transaction' AND ref_id=?", (txn_id,)).fetchone()
        return row[0] if row else None

    try:
        conn.execute("PRAGMA foreign_keys=ON")
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Bare Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Bare Groceries','expense','shared','#abcdef')"
        ).lastrowid
        doc_id = conn.execute(
            """
            INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
            VALUES ('receipt', 'bare.json', 'bare/raw.json', 'raw-sha', 'application/json', 'processed')
            """
        ).lastrowid
        txn_id = conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source_document_id, source, external_id, notes
            ) VALUES (?, '2026-03-15', 'Bare receipt', 'Bare Merchant', -1234, ?, 'test', 'bare-raw', '')
            """,
            (account_id, doc_id),
        ).lastrowid
        assert chunk_content(txn_id) is not None
        assert "Bare Merchant" in chunk_content(txn_id)

        split_id = conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents, memo) VALUES (?, ?, -1234, 'initial memo')",
            (txn_id, category_id),
        ).lastrowid
        assert "Bare Groceries" in chunk_content(txn_id)
        assert "initial memo" in chunk_content(txn_id)

        conn.execute("UPDATE transactions SET notes='updated transaction note' WHERE id=?", (txn_id,))
        assert "updated transaction note" in chunk_content(txn_id)

        conn.execute("UPDATE transaction_splits SET memo='changed split memo' WHERE id=?", (split_id,))
        assert "changed split memo" in chunk_content(txn_id)

        conn.execute("UPDATE categories SET name='Bare Market' WHERE id=?", (category_id,))
        assert "Bare Market" in chunk_content(txn_id)

        extraction_id = conn.execute(
            """
            INSERT INTO ingest_extractions(
              source_document_id, doc_kind, extracted_json, confidence, external_id,
              proposed_account_id, proposed_category_id, review_status, transaction_id
            ) VALUES (?, 'receipt', ?, 1.0, 'bare-extraction', ?, ?, 'auto', ?)
            """,
            (
                doc_id,
                json.dumps({"line_items": [{"description": "almond milk", "amount_cents": 499}]}),
                account_id,
                category_id,
                txn_id,
            ),
        ).lastrowid
        assert "almond milk" in chunk_content(txn_id)

        conn.execute(
            "UPDATE ingest_extractions SET extracted_json=? WHERE id=?",
            (json.dumps({"line_items": [{"description": "coffee pods", "amount_cents": 1299}]}), extraction_id),
        )
        assert "coffee pods" in chunk_content(txn_id)
        assert "almond milk" not in chunk_content(txn_id)

        conn.execute("DELETE FROM ingest_extractions WHERE id=?", (extraction_id,))
        assert "coffee pods" not in chunk_content(txn_id)

        conn.execute("DELETE FROM transaction_splits WHERE id=?", (split_id,))
        assert "Uncategorized" in chunk_content(txn_id)

        conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
        conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        assert chunk_content(txn_id) is None
        conn.commit()
    finally:
        conn.close()


def test_backfill_covers_all_sample_transactions(sample_db):
    with engine.read_conn(sample_db) as conn:
        tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE ref_kind='transaction'"
        ).fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM rag_fts").fetchone()[0]

    assert chunk_count == tx_count
    assert fts_count == tx_count


def test_incremental_sync_insert_edit_category_and_delete(app_env):
    with engine.write_tx(app_env) as conn:
        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=3,
            posted_on="2026-04-02",
            description="Corner store snacks",
            counterparty="Corner Store",
            amount_cents=-1234,
            source="test",
            external_id="rag-corner-store",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
            notes="first pass",
        )
        assert txn_id is not None
        split_id = repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=4,
            amount_cents=-1234,
            memo="snacks",
        )

    with engine.read_conn(app_env) as conn:
        inserted = repo_rag.search_history(conn, "corner snacks")
    assert [row["transaction_id"] for row in inserted["rows"]] == [txn_id]

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE transactions SET counterparty=?, description=?, notes=? WHERE id=?",
            ("Edited Merchant", "edited pantry run", "changed note", txn_id),
        )
        conn.execute("UPDATE transaction_splits SET category_id=?, memo=? WHERE id=?", (5, "dinner", split_id))

    with engine.read_conn(app_env) as conn:
        stale = repo_rag.search_history(conn, "corner snacks")
        updated = repo_rag.search_history(conn, "edited merchant dinner")
        chunk = conn.execute("SELECT * FROM rag_chunks WHERE ref_id=?", (txn_id,)).fetchone()

    assert stale["rows"] == []
    assert [row["transaction_id"] for row in updated["rows"]] == [txn_id]
    assert "Restaurants" in chunk["content"]

    with engine.write_tx(app_env) as conn:
        conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))

    with engine.read_conn(app_env) as conn:
        deleted = conn.execute("SELECT * FROM rag_chunks WHERE ref_id=?", (txn_id,)).fetchone()
        search = repo_rag.search_history(conn, "edited merchant")

    assert deleted is None
    assert search["rows"] == []


def test_fts_search_costco_receipt_and_march(app_env):
    with engine.write_tx(app_env) as conn:
        txn_id = _insert_costco_receipt(conn)

    with engine.read_conn(app_env) as conn:
        costco = repo_rag.search_history(conn, "costco")
        march = repo_rag.search_history(conn, "costco march")
        line_items = repo_rag.search_history(conn, "coffee beans")

    assert costco["rows"][0]["transaction_id"] == txn_id
    assert march["rows"][0]["transaction_id"] == txn_id
    assert line_items["rows"][0]["transaction_id"] == txn_id
    assert march["rows"][0]["posted_on"] == "2026-03-14"
    assert march["rows"][0]["amount_cents"] == -18352
    assert "COSTCO" in march["rows"][0]["snippet"].upper()


def test_fts_ranking_and_hostile_queries(app_env):
    with engine.write_tx(app_env) as conn:
        first = _insert_costco_receipt(conn, posted_on="2026-03-14", merchant="COSTCO WHOLESALE")
        second = _insert_costco_receipt(conn, posted_on="2026-04-14", merchant="COSTCO GAS")

    with engine.read_conn(app_env) as conn:
        rows = repo_rag.search_history(conn, "costco", limit=5)["rows"]
        ranks = [row["rank"] for row in rows]
        hostile_results = [
            repo_rag.search_history(conn, raw)
            for raw in ['costco "march"', "costco AND *", "******", "кофе costco"]
        ]

    assert {first, second} <= {row["transaction_id"] for row in rows}
    assert ranks == sorted(ranks)
    for result in hostile_results:
        assert "rows" in result
