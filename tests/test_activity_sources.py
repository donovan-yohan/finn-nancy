"""FNDS-01: canonical originals in the shared, read-only transaction disclosure."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, repo_documents, repo_statements
from app.ingest.schemas import ExtractedStatement, StatementRow


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("STRICT_LOCAL_MODE", "true")
    monkeypatch.setenv("PLAID_ENABLED", "false")
    monkeypatch.setenv("READ_ONLY", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _document(conn, root, kind, name, content):
    raw = content.encode()
    storage = hashlib.sha256(raw).hexdigest() + ".txt"
    root.mkdir(parents=True, exist_ok=True)
    (root / storage).write_bytes(raw)
    return repo_documents.insert_source_document(
        conn, kind=kind, original_name=name, storage_ref=storage,
        sha256=hashlib.sha256(raw).hexdigest(), mime_type="text/plain", status="processed",
    )


def _linked_pair(conn, root, transaction_id=3):
    receipt = _document(conn, root, "receipt", "Generated linked receipt.txt", "Generated receipt original")
    statement = _document(conn, root, "statement", "Generated linked statement.csv", "Generated statement original")
    conn.execute("UPDATE transactions SET source_document_id=? WHERE id=?", (receipt, transaction_id))
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    parsed = ExtractedStatement(
        institution="Generated bank", currency="CAD", statement_period="2026-01", confidence=1.0,
        rows=[StatementRow(posted_on=txn["posted_on"], description="Generated matching line",
                           amount_cents=txn["amount_cents"], is_pending=False)],
    )
    repo_statements.stage_lines(conn, source_document_id=statement, account_id=txn["account_id"], parsed=parsed)
    line = conn.execute("SELECT id FROM statement_lines WHERE source_document_id=?", (statement,)).fetchone()[0]
    repo_statements.set_match(conn, line, status="needs_review", rationale="generated source proof")
    return receipt, statement, line


def _row(text, transaction_id):
    match = re.search(r'id="transaction-' + str(transaction_id) + r'">(.*?)</details>', text, re.S)
    assert match is not None
    return match[1]


def _links(text):
    return re.findall(r'href="(/review/doc/\d+/file)"', text)


def _dump(db):
    with engine.read_conn(db) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        return "\n".join(conn.iterdump())


def test_confirm_match_exposes_both_originals_without_second_expense(app_env):
    root = Path(get_settings().data_dir)
    with engine.write_tx(app_env) as conn:
        receipt, statement, line = _linked_pair(conn, root)
        before_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        before_splits = [tuple(r) for r in conn.execute("SELECT * FROM transaction_splits ORDER BY id")]
    from app.web.app import create_app
    client = TestClient(create_app())  # no lifespan/workers
    try:
        result = client.post(f"/recon/line/{line}/confirm", data={"month": "2026-01", "transaction_id": "3"}, follow_redirects=False)
        assert result.status_code == 303, result.text
        with engine.read_conn(app_env) as conn:
            assert conn.execute("SELECT matched_transaction_id FROM statement_lines WHERE id=?", (line,)).fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before_count
            assert [tuple(r) for r in conn.execute("SELECT * FROM transaction_splits ORDER BY id")] == before_splits
        before_get = _dump(app_env)
        for route in ("/activity", "/?month=2026-01", "/transactions"):
            response = client.get(route)
            assert response.status_code == 200
            row = _row(response.text, 3)
            assert _links(row) == [f"/review/doc/{receipt}/file", f"/review/doc/{statement}/file"]
            assert "Receipt: Generated linked receipt.txt" in row
            assert "Statement: Generated linked statement.csv" in row
            assert "-$162.43" in row and "purchase" in row
        for doc, expected in ((receipt, b"Generated receipt original"), (statement, b"Generated statement original")):
            original = client.get(f"/review/doc/{doc}/file")
            assert original.status_code == 200
            assert original.content == expected
        assert _dump(app_env) == before_get
    finally:
        client.close()


def _statement_line(conn, document, *, transaction_id: int | None = 3, status="matched", disposition="active", suffix=""):
    return conn.execute(
        """INSERT INTO statement_lines(source_document_id,account_id,posted_on,raw_description,
           amount_cents,row_hash,match_status,matched_transaction_id,review_disposition,statement_period)
           VALUES (?,3,'2026-01-05','Generated matching line',-16243,?,?,?,?,'2026-01')""",
        (document, f"source-proof-{document}-{suffix}", status, transaction_id, disposition),
    ).lastrowid


def test_zero_one_multiple_dedup_and_inactive_edges_never_guess_sources(app_env):
    from app.db import views
    from app.web.app import create_app
    root = Path(get_settings().data_dir)
    hostile = 'Generated <img src=x onerror=alert(1)> & "receipt".txt'
    with engine.write_tx(app_env) as conn:
        receipt = _document(conn, root, "receipt", hostile, "Generated hostile filename original")
        conn.execute("UPDATE transactions SET source_document_id=? WHERE id=3", (receipt,))
        # The schema allows only one active matched/promoted line per transaction.
        promoted = _document(conn, root, "statement", "Generated promoted.csv", "Generated promoted original")
        _statement_line(conn, promoted, status="promoted")

        excluded = []
        for status, disposition, txn in (("unmatched", "active", 3), ("needs_review", "active", 3),
                ("ignored", "active", 3), ("matched", "excluded", 3), ("promoted", "excluded", 3),
                ("matched", "active", 4), ("matched", "active", None)):
            doc = _document(conn, root, "statement", "Generated non-evidence.csv", f"Generated non-evidence {status} {disposition} {txn}")
            _statement_line(conn, doc, status=status, disposition=disposition, transaction_id=txn)
            excluded.append(doc)
        only = _document(conn, root, "receipt", "Generated only receipt.txt", "Generated one source")
        conn.execute("UPDATE transactions SET source_document_id=? WHERE id=6", (only,))
    client = TestClient(create_app())
    try:
        before = _dump(app_env)
        for route in ("/activity", "/?month=2026-01", "/transactions"):
            text = client.get(route).text
            multi = _row(text, 3)
            assert _links(multi) == [f"/review/doc/{receipt}/file", f"/review/doc/{promoted}/file"]
            assert all(f"/review/doc/{doc}/file" not in multi for doc in excluded)
            assert "&lt;img" in multi and "<img" not in multi and "&#34;receipt&#34;" in multi
            single = _row(text, 6)
            assert _links(single) == [f"/review/doc/{only}/file"]
            zero = _row(text, 5)
            assert not _links(zero)
            assert "No linked originals." in zero
        with engine.read_conn(app_env) as conn:
            rows = views.activity_results(conn, q="weekly groceries")["transactions"]
            assert [source["id"] for source in next(row for row in rows if row["id"] == 3)["originals"]] == [receipt, promoted]
        assert _dump(app_env) == before
    finally:
        client.close()


def test_revoked_match_disappears_while_receipt_original_survives(app_env):
    from app.db import views
    root = Path(get_settings().data_dir)
    with engine.write_tx(app_env) as conn:
        receipt, statement, line = _linked_pair(conn, root)
        repo_statements.set_match(conn, line, status="matched", transaction_id=3, method="manual")
    with engine.read_conn(app_env) as conn:
        row = next(row for row in views.activity_results(conn)["transactions"] if row["id"] == 3)
        assert [source["id"] for source in row["originals"]] == [receipt, statement]
    with engine.write_tx(app_env) as conn:
        repo_statements.set_match(conn, line, status="unmatched")
    with engine.read_conn(app_env) as conn:
        row = next(row for row in views.activity_results(conn)["transactions"] if row["id"] == 3)
        assert [source["id"] for source in row["originals"]] == [receipt]


def test_real_promotion_deduplicates_transaction_and_line_source(app_env):
    from app.web.app import create_app
    root = Path(get_settings().data_dir)
    with engine.write_tx(app_env) as conn:
        doc = _document(conn, root, "statement", "Generated booked statement.csv", "Generated booked original")
        line = _statement_line(conn, doc, status="needs_review", transaction_id=None)
    client = TestClient(create_app())
    try:
        result = client.post(f"/recon/line/{line}/promote", data={"month": "2026-01", "flow_kind": "purchase"}, follow_redirects=False)
        assert result.status_code == 303, result.text
        with engine.read_conn(app_env) as conn:
            txn = conn.execute("SELECT matched_transaction_id FROM statement_lines WHERE id=?", (line,)).fetchone()[0]
        assert _links(_row(client.get("/transactions").text, txn)) == [f"/review/doc/{doc}/file"]
    finally:
        client.close()


def test_page_projection_has_one_batch_read_not_per_row_queries(app_env):
    from app.db import views
    root = Path(get_settings().data_dir)
    with engine.write_tx(app_env) as conn:
        for index in range(60):
            doc = _document(conn, root, "receipt", f"Generated batch {index}.txt", f"Generated batch original {index}")
            txn = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,amount_cents,source,flow_kind,source_document_id,external_id)
                VALUES (1,'2026-05-01','Generated batch',-123,'manual','purchase',?,?)""", (doc, f"generated-batch-{index}")).lastrowid
            conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-123)", (txn,))
    with engine.read_conn(app_env) as conn:
        for limit in (1, 6, 50):
            queries = []
            conn.set_trace_callback(queries.append)
            result = views.activity_results(conn, q="Generated batch", limit=limit)
            assert len(queries) == 2, queries  # ledger page + exactly one evidence projection
            assert len(result["transactions"]) == limit
            assert all(len(row["originals"]) == 1 for row in result["transactions"])
            returned = [row["id"] for row in result["transactions"]]
            assert "VALUES " + ",".join(f"({txn})" for txn in returned) in queries[1]
            assert result["next_cursor"].endswith(":" + str(returned[-1]))
        queries = []
        conn.set_trace_callback(queries.append)
        assert views.activity_results(conn, q="missing-generated-batch")["transactions"] == []
        assert len(queries) == 1
        conn.set_trace_callback(None)


def test_gets_are_query_only_full_database_equal_including_cursor_sources(app_env, monkeypatch):
    from app.web.app import create_app
    root = Path(get_settings().data_dir)
    with engine.write_tx(app_env) as conn:
        receipt, statement, line = _linked_pair(conn, root)
        repo_statements.set_match(conn, line, status="matched", transaction_id=3, method="manual")
        for index in range(55):
            txn = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,amount_cents,source,flow_kind,external_id)
                VALUES (1,'2026-05-01','Generated cursor row',-123,'manual','purchase',?)""", (f"generated-cursor-{index}",)).lastrowid
            conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-123)", (txn,))
    before = _dump(app_env)
    actual_connect = engine.connect
    connections = []
    def query_only(path, **kwargs):
        conn = actual_connect(path, read_only=True)
        connections.append(conn.execute("PRAGMA query_only").fetchone()[0])
        return conn
    def forbidden(*args, **kwargs):
        raise AssertionError("GET entered write transaction")
    monkeypatch.setattr(engine, "connect", query_only)
    monkeypatch.setattr(engine, "write_tx", forbidden)
    client = TestClient(create_app())
    try:
        for route in ("/activity", "/?month=2026-01", "/transactions?category_id=4&report_bucket=expense&category_resolution=accepted"):
            assert client.get(route).status_code == 200
        first = client.get("/transactions")
        cursor_match = re.search(r'hx-get="([^"]+)"', first.text)
        assert cursor_match is not None
        cursor = cursor_match[1].replace("&amp;", "&")
        second = client.get(cursor, headers={"HX-Request": "true"})
        assert second.status_code == 200
        row = _row(second.text, 3)
        assert _links(row) == [f"/review/doc/{receipt}/file", f"/review/doc/{statement}/file"]
        assert 'id="tx-list"' not in second.text and "<script>" not in second.text
        for doc in (receipt, statement):
            assert client.get(f"/review/doc/{doc}/file").status_code == 200
        assert connections and all(connections)
        assert _dump(app_env) == before
    finally:
        client.close()
