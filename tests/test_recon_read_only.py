"""Generated-only proof through the real factory, middleware, apply and database."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import socket
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import (
    engine,
    migrate,
    repo_assertions,
    repo_documents,
    repo_ledger,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.reconcile import apply, positive_flows


MONTH = "2026-06"


def _snapshot(path):
    # Every table, column and row (including updates/audits/FTS), not just counts.
    with engine.read_conn(path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        return "\n".join(conn.iterdump())


@pytest.fixture
def real_recon(tmp_path, monkeypatch):
    attempts = {"network": 0, "dotenv": 0, "outside_generated_db": 0, "lifespan": 0}
    active = True
    path = tmp_path / "generated-recon.sqlite"

    def audit(event, args):
        if not active:
            return
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(args[0])).name
            if name == ".env" or name.startswith(".env."):
                attempts["dotenv"] += 1
                raise AssertionError("dotenv access forbidden")
        if event == "sqlite3.connect":
            if Path(os.fsdecode(args[0])).resolve() != path.resolve():
                attempts["outside_generated_db"] += 1
                raise AssertionError("only this generated database is allowed")

    sys.addaudithook(audit)

    def no_network(*args, **kwargs):
        attempts["network"] += 1
        raise AssertionError("provider/network access forbidden")

    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (socket, "create_connection"), (socket, "getaddrinfo")):
        monkeypatch.setattr(target, name, no_network)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for key, value in {
        "DB_PATH": str(path), "DATA_DIR": str(tmp_path / "data"),
        "STRICT_LOCAL_MODE": "true", "PLAID_ENABLED": "false", "READ_ONLY": "true",
        "EMBEDDINGS_ENABLED": "false", "MERCHANT_SEARCH_ENABLED": "false",
        "LOCAL_LLM_BASE": "http://blocked.invalid/v1", "API_TOKEN": "generated-recon-key",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        migrate.init_db(path)
        lines = {}
        docs = {}
        with engine.write_tx(path) as conn:
            account = int(conn.execute(
                "INSERT INTO accounts(name,kind,currency) VALUES "
                "('Generated recon account','chequing','CAD')"
            ).lastrowid)
            for sign, amount in (("negative", -2500), ("positive", 10000)):
                doc = int(conn.execute(
                    "INSERT INTO source_documents(kind,original_name,storage_ref,sha256,"
                    "mime_type,status) VALUES ('statement',?,?,?,'text/csv','needs_review')",
                    (f"generated-{sign}.csv", f"generated/{sign}.csv",
                     hashlib.sha256(f"generated-{sign}".encode()).hexdigest()),
                ).lastrowid)
                parsed = ExtractedStatement(
                    institution="Generated bank", currency="CAD", statement_period=MONTH,
                    confidence=1.0, rows=[StatementRow(posted_on="2026-06-20",
                        description=f"GENERATED {sign.upper()}", amount_cents=amount,
                        is_pending=False)],
                )
                assert repo_statements.stage_lines(
                    conn, source_document_id=doc, account_id=account, parsed=parsed,
                ) == {"staged": 1, "duplicates": 0}
                line = int(conn.execute(
                    "SELECT id FROM statement_lines WHERE source_document_id=?", (doc,)
                ).fetchone()[0])
                repo_statements.set_match(conn, line, status="needs_review",
                                          rationale="generated review fixture")
                lines[sign], docs[sign] = line, doc

        # Import only after dotenv, generated DB and egress fences are installed.
        from app.web.app import create_app

        @asynccontextmanager
        async def no_lifespan(app):
            attempts["lifespan"] += 1
            raise AssertionError("worker/lifespan must not start")
            yield

        app = create_app()
        app.router.lifespan_context = no_lifespan
        client = TestClient(app)  # No context manager: deliberately no lifespan.
        admissions = []
        actual_write_tx = engine.write_tx

        @contextmanager
        def observed_write_tx(db_path):
            admissions.append(str(db_path))
            with actual_write_tx(db_path) as conn:
                yield conn

        monkeypatch.setattr(engine, "write_tx", observed_write_tx)
        state = SimpleNamespace(path=path, lines=lines, docs=docs, account=account,
                                app=app, client=client, admissions=admissions)
        try:
            yield state
        finally:
            client.close()
            assert not hasattr(app.state, "worker_stop")
    finally:
        active = False
        get_settings.cache_clear()
        assert attempts == dict.fromkeys(attempts, 0), attempts
        print(json.dumps({"isolation_attempts": attempts}))


def _receipt(response, before, after, admissions):
    print(json.dumps({"path": response.request.url.path,
                      "status": response.status_code,
                      "location": response.headers.get("location"),
                      "caller": response.headers.get("x-fn-caller"),
                      "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                      "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
                      "write_admissions": len(admissions)}))


def test_read_only_refuses_legitimate_promotion(real_recon):
    state = real_recon
    before = _snapshot(state.path)
    response = state.client.post(
        f"/recon/line/{state.lines['negative']}/promote",
        data={"month": MONTH, "flow_kind": "purchase"}, follow_redirects=False,
    )
    after = _snapshot(state.path)
    _receipt(response, before, after, state.admissions)
    assert response.status_code == 403
    assert response.json() == {"detail": "read_only"}
    assert state.admissions == []
    assert after == before


@pytest.mark.parametrize("keyed", [False, True], ids=["browser", "bearer"])
@pytest.mark.parametrize("sign", ["negative", "positive"])
def test_runtime_read_only_toggle_preserves_real_promotion(real_recon, monkeypatch, keyed, sign):
    state = real_recon
    headers = {"Authorization": "Bearer generated-recon-key"} if keyed else {}
    form = {"month": MONTH, "flow_kind": "purchase" if sign == "negative" else "unknown"}
    url = f"/recon/line/{state.lines[sign]}/promote"
    before = _snapshot(state.path)
    # Same app/client throughout; policy must not be captured at router/factory creation.
    for enabled in (True, False, True):
        monkeypatch.setenv("READ_ONLY", str(enabled).lower())
        get_settings.cache_clear()
        state.admissions.clear()
        prior = _snapshot(state.path)
        response = state.client.post(url, data=form, headers=headers, follow_redirects=False)
        after = _snapshot(state.path)
        _receipt(response, prior, after, state.admissions)
        assert response.headers["x-fn-caller"] == ("api" if keyed else "web_ui")
        if enabled:
            assert response.status_code == 403
            assert response.json() == {"detail": "read_only"}
            assert state.admissions == []
            assert after == prior
        else:
            assert response.status_code == 303
            assert response.headers["location"] == f"/recon?month={MONTH}"
            assert len(state.admissions) == 1
            assert after != before
            with engine.read_conn(state.path) as conn:
                txn = conn.execute("SELECT * FROM transactions").fetchone()
                assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
                assert txn["amount_cents"] == (-2500 if sign == "negative" else 10000)
                assert txn["flow_kind"] == form["flow_kind"]
                assert txn["account_id"] == state.account
                assert txn["source"] == "statement"
                assert txn["source_document_id"] == state.docs[sign]
                assert txn["recon_status"] == "cleared"
                assert txn["cleared_on"] == "2026-06-20"
                line = conn.execute("SELECT * FROM statement_lines WHERE id=?",
                                    (state.lines[sign],)).fetchone()
                assert line["match_status"] == "promoted"
                assert line["matched_transaction_id"] == txn["id"]
                assert txn["external_id"] == line["row_hash"]
                splits = conn.execute("SELECT amount_cents FROM transaction_splits").fetchall()
                assert [row[0] for row in splits] == [txn["amount_cents"]]
                assert conn.execute("SELECT status FROM source_documents WHERE id=?",
                                    (state.docs[sign],)).fetchone()[0] == "matched"
                if sign == "positive":
                    assert conn.execute("SELECT status FROM transaction_flow_reviews "
                                        "WHERE transaction_id=?", (txn["id"],)).fetchone()[0] == "pending"
                    assert conn.execute("SELECT COALESCE(SUM(income_cents),0) "
                                        "FROM v_cashflow_monthly").fetchone()[0] == 0
        read_before = _snapshot(state.path)
        state.admissions.clear()
        page = state.client.get(f"/recon?month={MONTH}", headers=headers)
        assert page.status_code == 200
        assert "2026-06" in page.text
        assert state.admissions == []
        assert _snapshot(state.path) == read_before


# Explicit action inventory is compared to both production decorators and composed
# runtime routes below. Adding a POST cannot silently become untested coverage.
POST_PATHS = {
    "confirm": "/recon/line/{line_id}/confirm",
    "promote": "/recon/line/{line_id}/promote",
    "ignore": "/recon/line/{line_id}/ignore",
    "recover": "/recon/positive/line/{line_id}/recover",
    "reject": "/recon/positive/{subject_transaction_id}/reject",
    "restore": "/recon/positive/{subject_transaction_id}/restore",
    "classification": "/recon/positive/{subject_transaction_id}/accept-classification",
    "pair": "/recon/positive/{subject_transaction_id}/accept-pair",
    "undo": "/recon/positive/acceptance/{accept_event_id}/undo",
    "assign": "/recon/doc/{doc_id}/assign-account",
    "unreconcile": "/recon/doc/{doc_id}/unreconcile",
    "adjust": "/recon/assertion/adjust",
    "rerun": "/recon/doc/{doc_id}/rerun",
}


def _manual_purchase(conn, account, *, amount=-20000):
    category = repo_ledger.ensure_uncategorized(conn)
    transaction = repo_ledger.insert_transaction(
        conn, account_id=account, posted_on="2026-06-19", description="GENERATED PURCHASE",
        counterparty="GENERATED", amount_cents=amount, source="manual",
        external_id="generated-purchase", source_document_id=None, source_confidence=1.0,
        flow_kind="purchase",
    )
    assert transaction is not None
    repo_ledger.insert_split(conn, transaction_id=transaction, category_id=category,
                             amount_cents=amount)
    return transaction, category


def _review(conn, subject):
    return next(item for item in positive_flows.list_positive_flow_reviews(
        conn, MONTH, include_suppressed=True,
    ) if int(item["subject"]["transaction_id"]) == subject)


def _prepare_action(state, action):
    """Build genuine reachable operands; exact same payload is exercised writable."""
    form = {"month": MONTH}
    ids = {"line_id": state.lines["negative"], "doc_id": state.docs["negative"]}
    with engine.write_tx(state.path) as conn:
        if action == "confirm":
            txn, _ = _manual_purchase(conn, state.account, amount=-2500)
            form["transaction_id"] = str(txn)
        elif action == "promote":
            form["flow_kind"] = "purchase"
        elif action == "recover":
            ids["line_id"] = state.lines["positive"]
            repo_statements.set_match(conn, ids["line_id"], status="ignored",
                                      rationale="generated legacy credit")
            intake = next(item for item in positive_flows.list_positive_flow_intake(conn, MONTH)
                          if int(item["line"]["statement_line_id"]) == ids["line_id"])
            form.update(evidence_fingerprint=intake["evidence_fingerprint"],
                        operation_key="generated:recover", reason="generated recovery")
        elif action in {"reject", "restore", "classification", "pair", "undo"}:
            subject = apply.promote_line(conn, state.lines["positive"])
            assert subject is not None
            ids["subject_transaction_id"] = subject
            _, category = _manual_purchase(conn, state.account)
            review = _review(conn, subject)
            proposal = next(p for p in review["proposals"] if p["proposed_flow_kind"] ==
                            ("income" if action in {"classification", "undo"} else "refund"))
            if action == "restore":
                positive_flows.reject_proposal(
                    conn, subject_transaction_id=subject, month=MONTH,
                    proposal_key=proposal["proposal_key"],
                    evidence_fingerprint=proposal["evidence_fingerprint"],
                    operation_key="generated:setup-reject", actor="generated:test",
                    reason="generated rejection",
                )
                proposal = next(p for p in _review(conn, subject)["proposals"]
                                if p["proposal_key"] == proposal["proposal_key"])
                assert proposal["state"] == "suppressed"
            if action == "undo":
                event = positive_flows.accept_classification(
                    conn, subject_transaction_id=subject, month=MONTH, flow_kind="income",
                    evidence_fingerprint=proposal["evidence_fingerprint"],
                    operation_key="generated:setup-accept", actor="generated:test",
                    reason="generated classification",
                )
                acceptance = _review(conn, subject)["acceptance"]
                ids["accept_event_id"] = event
                fingerprint = acceptance["undo_fingerprint"]
            else:
                fingerprint = proposal["evidence_fingerprint"]
            form.update(evidence_fingerprint=fingerprint, operation_key=f"generated:{action}",
                        reason="generated decision")
            if action in {"reject", "restore", "pair"}:
                form["proposal_key"] = proposal["proposal_key"]
                if action == "pair":
                    form["selected_category_id"] = str(category)
            elif action == "classification":
                form["flow_kind"] = "income"
        elif action == "assign":
            conn.execute("UPDATE statement_lines SET account_id=NULL, match_status='unmatched' WHERE id=?",
                         (state.lines["negative"],))
            parsed = ExtractedStatement(
                institution="Generated bank", currency="CAD", statement_period=MONTH,
                period_start_on="2026-06-01", period_end_on="2026-06-30",
                statement_issued_on="2026-06-30", observed_page_count=1,
                extracted_page_count=1, confidence=1.0,
            )
            extraction = repo_documents.insert_extraction(
                conn, source_document_id=ids["doc_id"], doc_kind="statement",
                extracted_json=parsed.model_dump_json(), confidence=1.0, external_id="",
                proposed_account_id=None, proposed_category_id=None, review_status="pending",
            )
            repo_statement_reviews.create_from_extraction(
                conn, source_document_id=ids["doc_id"], extraction_id=extraction,
                account_id=None, parsed=parsed, raw=b"generated statement", actor="generated:test",
            )
            form["account_id"] = str(state.account)
        elif action == "unreconcile":
            assert apply.promote_line(conn, state.lines["negative"]) is not None
            repo_documents.set_status(conn, ids["doc_id"], "matched")
        elif action == "adjust":
            repo_assertions.record_assertion(conn, account_id=state.account,
                                             asof_date="2026-06-30", asserted_cents=100)
            form = {"account_id": str(state.account), "asof_date": "2026-06-30"}
    return POST_PATHS[action].format(**ids), form


@pytest.mark.parametrize("keyed", [False, True], ids=["browser", "bearer"])
@pytest.mark.parametrize("action", POST_PATHS)
def test_every_recon_post_refuses_before_admission_with_real_writable_control(
    real_recon, monkeypatch, action, keyed,
):
    state = real_recon
    url, form = _prepare_action(state, action)
    headers = {"Authorization": "Bearer generated-recon-key"} if keyed else {}
    state.admissions.clear()
    before = _snapshot(state.path)
    # Fail loudly if policy is moved behind any transaction/mutator/queue boundary.
    from app.web.routes import recon
    with monkeypatch.context() as boundary:
        invoked = []

        def forbidden(*args, **kwargs):
            invoked.append(True)
            raise AssertionError("read-only reached a mutation boundary")

        boundary.setattr(engine, "write_tx", forbidden)
        for module, names in (
            (apply, ("confirm_match", "promote_line", "ignore_line", "unreconcile_document")),
            (positive_flows, ("recover_positive_line", "reject_proposal", "restore_proposal",
                              "accept_classification", "accept_pair", "undo_acceptance")),
            (repo_statements, ("set_flow_kind",)),
            (repo_statement_reviews, ("update_metadata",)),
            (recon.adjust, ("create_adjustment",)), (recon.repo_jobs, ("enqueue",)),
        ):
            for name in names:
                boundary.setattr(module, name, forbidden)
        response = state.client.post(url, data=form, headers=headers, follow_redirects=False)
        assert invoked == []
    after = _snapshot(state.path)
    _receipt(response, before, after, state.admissions)
    assert response.status_code == 403
    assert response.json() == {"detail": "read_only"}
    assert response.headers["x-fn-caller"] == ("api" if keyed else "web_ui")
    assert state.admissions == []
    assert after == before

    # Real handler/domain proof that the refused form was legitimate, not an
    # unrelated invalid-ID/fingerprint/form rejection dressed up as safety.
    monkeypatch.setenv("READ_ONLY", "false")
    get_settings.cache_clear()
    writable = state.client.post(url, data=form, headers=headers, follow_redirects=False)
    committed = _snapshot(state.path)
    _receipt(writable, before, committed, state.admissions)
    assert writable.status_code == 303
    expected = (f"/review/statement/{state.docs['negative']}" if action == "assign" else
                f"/close?month={MONTH}" if action == "adjust" else f"/recon?month={MONTH}")
    assert writable.headers["location"] == expected  # No disguised failure notice.
    assert len(state.admissions) == 1
    assert committed != before
    _assert_action_effect(state, action)


def _assert_action_effect(state, action):
    with engine.read_conn(state.path) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?",
                            (state.lines["negative"],)).fetchone()
        if action in {"confirm", "promote", "ignore", "unreconcile"}:
            assert line["match_status"] == {
                "confirm": "matched", "promote": "promoted", "ignore": "ignored",
                "unreconcile": "unmatched",
            }[action]
        elif action == "assign":
            assert line["account_id"] == state.account
            review = repo_statement_reviews.get_for_document(conn, state.docs["negative"])
            assert review is not None
            assert review["account_id"] == state.account
        elif action == "rerun":
            assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='reconcile_document' "
                                "AND source_document_id=?", (state.docs["negative"],)).fetchone()[0] == 1
        elif action == "adjust":
            assert conn.execute("SELECT amount_cents FROM transactions WHERE source='adjustment'").fetchone()[0] == 100
        else:
            event = conn.execute("SELECT * FROM positive_flow_decision_events "
                                 "WHERE operation_key=?", (f"generated:{action}",)).fetchone()
            assert event is not None
            if action == "recover":
                assert conn.execute("SELECT match_status FROM statement_lines WHERE id=?",
                                    (state.lines["positive"],)).fetchone()[0] == "promoted"
            if action in {"classification", "pair", "undo", "recover"}:
                flow = conn.execute("SELECT flow_kind FROM transactions WHERE id=?",
                                    (event["subject_transaction_id"],)).fetchone()[0]
                assert flow == {"classification": "income", "pair": "refund",
                                "undo": "unknown", "recover": "unknown"}[action]
            if action == "pair":
                assert conn.execute("SELECT COUNT(*) FROM transaction_relationships "
                                    "WHERE revoked_at IS NULL").fetchone()[0] == 1


def test_inventory_covers_source_and_composed_mutation_routes(real_recon):
    from app.web.routes import recon
    source = ast.parse(inspect.getsource(recon))
    source_posts = {
        ast.literal_eval(decorator.args[0])
        for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "post"
    }
    assert source_posts == set(POST_PATHS.values())

    def composed_routes(routes):
        # FastAPI supports both eagerly copied routes and lazy included routers.
        for route in routes:
            if hasattr(route, "effective_candidates"):
                yield from composed_routes(route.effective_candidates())
            else:
                yield route

    routes = [route for route in composed_routes(real_recon.app.routes)
              if getattr(route, "path", "").startswith("/recon")]
    mutations = [route for route in routes
                 if route.methods - {"GET", "HEAD", "OPTIONS"}]
    assert {route.path for route in mutations} == set(POST_PATHS.values())
    assert all(route.methods == {"POST"} for route in mutations)
    for route in routes:
        assert recon._require_writable_reconciliation in {
            dependency.call for dependency in route.dependant.dependencies
        }, route.path
