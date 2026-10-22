"""Generated canonical UI fixture. No lifespan, worker, credentials or providers."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from pathlib import Path

import fitz
from fastapi import FastAPI


def generated_workspace(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(DB_PATH=str(root / "workspace.sqlite"), DATA_DIR=str(root),
                      STRICT_LOCAL_MODE="true")
    from app.config import Settings, get_settings

    # Do not even read a repository .env; only these generated settings exist.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    from app.db import (engine, migrate, repo_actions, repo_documents,
                        repo_import_runs, repo_statement_reviews, repo_statements)
    from app.ingest.schemas import ExtractedStatement, StatementRow

    migrate.seed_sample(str(root / "workspace.sqlite"))
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Generated statement - June 2026")
    raw = pdf.tobytes()
    pdf.close()
    (root / "generated.pdf").write_bytes(raw)
    parsed = ExtractedStatement(
        institution="Generated Credit Union", account_hint="Checking",
        account_last4="4242", currency="CAD", statement_period="2026-06",
        period_start_on="2026-06-01", period_end_on="2026-06-30",
        statement_issued_on="2026-07-01", opening_balance_cents=10000,
        closing_balance_cents=8766, declared_page_count=1, declared_row_count=1,
        observed_page_count=1, extracted_page_count=1, confidence=.96,
        field_confidence={key: .96 for key in (
            "period_start_on", "period_end_on", "statement_issued_on",
            "opening_balance_cents", "closing_balance_cents", "currency",
            "account_fingerprint", "zero_activity")},
        rows=[StatementRow(posted_on="2026-06-12", description="Generated market",
                           amount_cents=-1234, page_number=1,
                           field_confidence={"posted_on": .96, "description": .96,
                                             "amount_cents": .96})],
    )
    with engine.write_tx(root / "workspace.sqlite") as conn:
        for i in range(64):
            amount = -1234 if i else -9007199254740991
            description = ("Generated unusually long evidence merchant " * 4 if i == 1
                           else f"Generated market {i:03}")
            txn = conn.execute(
                """INSERT INTO transactions(account_id, posted_on, description,
                   counterparty, amount_cents, source, external_id, flow_kind)
                   VALUES (1, '2026-06-12', ?, 'Generated counterparty', ?,
                           'sample', ?, 'purchase')""",
                (description, amount, f"workspace-ui-{i}"),
            ).lastrowid
            conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,?)", (txn, amount))
        unknown = conn.execute(
            """INSERT INTO transactions(account_id,posted_on,description,amount_cents,
               source,external_id,flow_kind) VALUES (1,'2026-06-13',
               'Generated movement requiring review',-456,'sample','workspace-unknown','unknown')"""
        ).lastrowid
        conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-456)", (unknown,))
        conn.execute("""INSERT INTO goals(name,kind,target_cents,start_month,target_month,
                     monthly_contribution_cents) VALUES ('Generated rainy day','savings_target',
                     100000,'2026-01','2026-12',10000)""")
        run = repo_import_runs.create(conn, channel="web", label="Generated statement batch")
        doc = repo_documents.insert_source_document(
            conn, kind="statement", original_name="generated-statement.pdf",
            storage_ref="generated.pdf", sha256=hashlib.sha256(raw).hexdigest(),
            mime_type="application/pdf", status="needs_review",
        )
        conn.execute("UPDATE source_documents SET import_run_id=? WHERE id=?", (run, doc))
        extraction = repo_documents.insert_extraction(
            conn, source_document_id=doc, doc_kind="statement",
            extracted_json=parsed.model_dump_json(), confidence=.96, external_id="",
            proposed_account_id=1, proposed_category_id=None, review_status="pending",
        )
        envelope = repo_statement_reviews.create_from_extraction(
            conn, source_document_id=doc, extraction_id=extraction, account_id=1,
            parsed=parsed, raw=raw, actor="fixture:generated-ui",
        )
        repo_statements.stage_lines(
            conn, source_document_id=doc, account_id=1, parsed=parsed,
            row_anchor_ids=repo_statement_reviews.row_anchor_ids(parsed, envelope.page_anchor_ids),
        )
        repo_statement_reviews.record_row_evidence(
            conn, review_id=int(envelope.review["id"]), extraction_id=extraction,
            parsed=parsed, account_id=1, anchors=envelope.page_anchor_ids,
        )
        proposal = repo_actions.enqueue_proposal(
            conn, kind="recategorization", payload={"transaction_id": 3, "to_category_id": 5},
            evidence={"transaction_ids": [3], "category_ids": [4, 5]}, confidence=.8,
            rationale="Generated category proposal",
        )
    from app.web.app import create_app

    app = create_app()
    # Uvicorn runs with lifespan=off: no background work or model warm-up.
    @app.get('/__fixture')
    def manifest():
        return {"statement": doc, "run": run, "proposal": proposal,
                "routes": ['/', '/activity', '/upload', '/insights?month=2026-06',
                           '/goals?month=2026-06', '/chat', '/processing', '/statements?month=2026-06',
                           '/actions', '/close?month=2026-06', '/recon?month=2026-06',
                           '/review', '/backlog', '/categories', '/manage', '/txn/new',
                           '/txn/3/edit', '/goals/1/edit?month=2026-06',
                           '/goals/close?month=2026-06', '/review/import',
                           f'/review/statement/{doc}', f'/review/pdf/{doc}',
                           f'/imports/runs/{run}', f'/actions/{proposal}/audit']}

    # Refuse any accidental provider/model connection even if a route changes.
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = address[0] == 'localhost'
            if not allowed:
                raise RuntimeError('Generated UI acceptance forbids external connections')
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    return app
