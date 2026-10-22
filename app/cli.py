"""finn-nancy command line (Typer).

Commands mirror the original Go binary (init-db, seed-sample, serve) plus a
``migrate`` command. Worker/warm commands arrive with M1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from .config import get_settings
from .db import migrate

app = typer.Typer(add_completion=False, help="finn-nancy — local AI finance app")
finance_app = typer.Typer(help="Bounded finance JSON reads and approval-only category proposals")
app.add_typer(finance_app, name="finance")


@app.command("api")
def remote_api(
    method: str = typer.Argument(..., help="HTTP method"),
    endpoint: str = typer.Argument(..., help="Origin-relative endpoint, including query if needed"),
    url: str | None = typer.Option(None, help="Application origin (or FN_API_URL)"),
    data: str | None = typer.Option(None, help="JSON object file, or - for stdin"),
    form: bool = typer.Option(False, help="Send scalar fields as a frontend form"),
):
    """Call the remote app with FN_API_KEY; no local ledger or settings access."""
    from .api.remote import request
    try:
        payload = None
        if data is not None:
            import sys
            if data == '-':
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(data).open('rb') as handle:
                    raw = handle.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('request data exceeds 1000000 bytes')
            try:
                payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                raise ValueError('request data must be valid JSON') from None
            if not isinstance(payload, dict):
                raise ValueError('request data must be a JSON object')
        result = request(method, endpoint, base_url=url, data=payload, form=form)
        typer.echo(json.dumps(result, sort_keys=True, allow_nan=False))
    except (ValueError, OSError) as exc:
        # Do not stringify filesystem/transport errors carrying paths or secrets.
        message = str(exc) if isinstance(exc, ValueError) else 'unable to read request data'
        typer.echo(json.dumps({'error': message}))
        raise typer.Exit(1) from None
    if result['status_code'] >= 400:
        raise typer.Exit(1)


def _finance_json(fn, db: str | None = None, **kwargs):
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    try:
        typer.echo(json.dumps(fn(**kwargs), sort_keys=True, allow_nan=False))
    except (ValueError, LookupError) as exc:
        typer.echo(json.dumps({"error": str(exc)}))
        raise typer.Exit(1) from exc


@finance_app.command("transactions")
def finance_transactions(
    db: str = typer.Option(None), q: str = typer.Option(None),
    account_id: int = typer.Option(None), category_id: int = typer.Option(None),
    date_from: str = typer.Option(None), date_to: str = typer.Option(None),
    flow_kind: str = typer.Option(None), review: str = typer.Option(None),
    cursor: str = typer.Option(None), limit: int = typer.Option(50),
):
    """Search canonical ledger rows; pagination is bound to the same filters."""
    from .api import finance
    _finance_json(finance.search_transactions, db, q=q, account_id=account_id,
                  category_id=category_id, date_from=date_from, date_to=date_to,
                  flow_kind=flow_kind, review=review, cursor=cursor, limit=limit)


@finance_app.command("transaction")
def finance_transaction(transaction_id: int, db: str = typer.Option(None)):
    """Inspect splits, canonical evidence, flow resolution and state digest."""
    from .api import finance
    _finance_json(finance.transaction_detail, db, transaction_id=transaction_id)


@finance_app.command("categories")
def finance_categories(db: str = typer.Option(None)):
    """List existing categories; never create categories."""
    from .api import finance
    _finance_json(finance.categories, db)


@finance_app.command("proposal")
def finance_proposal(proposal_id: int, db: str = typer.Option(None)):
    """Read proposal and append-only human decision history."""
    from .api import finance
    _finance_json(finance.proposal_detail, db, proposal_id=proposal_id)


@finance_app.command("period-statement")
def finance_period_statement(month: str, db: str = typer.Option(None)):
    """Read the shared evidence-backed period statement."""
    from .api import service
    _finance_json(service.get_period_statement, db, month=month)


@finance_app.command("propose-category")
def finance_propose_category(
    operation_key: str = typer.Option(...), transaction_id: int = typer.Option(...),
    split_id: int = typer.Option(...), to_category_id: int = typer.Option(...),
    expected_state_digest: str = typer.Option(...), rationale: str = typer.Option(...),
    confidence: float = typer.Option(None), evidence_ref: list[str] = typer.Option(None),
    db: str = typer.Option(None),
):
    """Queue one split category change for a human at /actions. No ledger write."""
    from .api import finance
    _finance_json(finance.propose_category, db, operation_key=operation_key,
                  transaction_id=transaction_id, split_id=split_id, to_category_id=to_category_id,
                  expected_state_digest=expected_state_digest, rationale=rationale,
                  confidence=confidence, evidence_refs=evidence_ref)


def _resolve_db(db: str | None) -> str:
    return db or str(get_settings().db_path)


@app.command("init-db")
def init_db(db: str = typer.Option(None, help="SQLite path (default from config)")):
    """Create/upgrade an empty database (schema only)."""
    path = _resolve_db(db)
    applied = migrate.init_db(path)
    typer.echo(f"init-db {path} — applied: {', '.join(applied) or 'none (up to date)'}")


@app.command()
def migrate_db(db: str = typer.Option(None, "--db", help="SQLite path")):
    """Apply any pending migrations to an existing database."""
    path = _resolve_db(db)
    applied = migrate.init_db(path)
    typer.echo(f"migrate {path} — applied: {', '.join(applied) or 'none (up to date)'}")


@app.command("seed-sample")
def seed_sample(db: str = typer.Option("data/sample.sqlite", help="SQLite path to (re)create")):
    """Rebuild a fake sample database (schema + fixtures)."""
    migrate.seed_sample(db)
    typer.echo(f"seed-sample {db} — created fake sample database")


@app.command()
def serve(
    db: str = typer.Option(None, help="SQLite path to serve"),
    addr: str = typer.Option(None, help="host:port bind address"),
    read_only: bool = typer.Option(False, "--read-only", help="open the DB query-only"),
):
    """Run the web portal."""
    import uvicorn

    settings = get_settings()
    if db:
        os.environ["DB_PATH"] = db
    if read_only:
        os.environ["READ_ONLY"] = "true"
    get_settings.cache_clear()  # re-read env with any CLI overrides

    bind = addr or settings.addr
    host, _, port = bind.rpartition(":")
    from .web.app import create_app

    uvicorn.run(create_app(), host=host or "127.0.0.1", port=int(port))


@app.command()
def worker(db: str = typer.Option(None, help="SQLite path")):
    """Run the background ingestion worker standalone (until Ctrl-C)."""
    import asyncio
    import signal

    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .workers.runner import run_worker

    async def _main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - platform dependent
                pass
        typer.echo("worker running — Ctrl-C to stop")
        await run_worker(stop)

    asyncio.run(_main())


@app.command("mcp-serve")
def mcp_serve(
    db: str = typer.Option(None, help="SQLite path"),
    read_only: bool = typer.Option(False, "--read-only", help="Expose report tools only"),
    proposals: bool = typer.Option(False, "--proposals", help="Only safe reads and approval-first category proposals"),
):
    """Run the MCP server over stdio (use --read-only for chat profiles)."""
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    try:
        from .mcp.server import main as _m
    except ImportError as exc:
        if exc.name == "mcp" or "mcp" in str(exc):
            typer.echo("install the mcp group: uv sync --group mcp")
            raise typer.Exit(1) from exc
        raise
    if read_only and proposals:
        raise typer.BadParameter("--read-only and --proposals are incompatible")
    _m(read_only=read_only, proposals=proposals)


@app.command()
def warm(db: str = typer.Option(None, help="SQLite path")):
    """Send one tiny completion to wake the model (mitigates cold start)."""
    import asyncio

    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .llm.warm import warm as _warm

    ok = asyncio.run(_warm())
    typer.echo("model warm" if ok else "warm failed (endpoint unreachable?)")


@app.command()
def backup(db: str = typer.Option(None, help="SQLite path")):
    """Snapshot the database (sqlite backup API, WAL-safe) into DATA_DIR/backups/."""
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .services.backup import backup_now

    settings = get_settings()
    path = backup_now(settings.db_path, settings.data_dir)
    typer.echo(f"backup written: {path}")


@app.command("embed-backfill")
def embed_backfill(db: str = typer.Option(None, help="SQLite path")):
    """Enqueue a resumable transaction-embedding backfill job."""
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .db import engine, repo_embeddings

    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        job_id = repo_embeddings.enqueue_embed_transactions(conn)
    if job_id is None:
        typer.echo("embed-backfill skipped — EMBEDDINGS_ENABLED=false")
    else:
        typer.echo(f"embed-backfill queued job {job_id}")


@app.command("export-eval-labels")
def export_eval_labels(
    out: Path | None = typer.Option(
        None,
        "--out",
        help="JSON output path (default: DATA_DIR/classification_labels.json)",
    ),
    db: str = typer.Option(None, help="SQLite path"),
):
    """Export approved recategorization labels for classification evals."""
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .db import engine, repo_labels

    settings = get_settings()
    out_path = out or (settings.data_dir / "classification_labels.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with engine.read_conn(settings.db_path) as conn:
        rows = repo_labels.export_eval_rows(conn)
    out_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    typer.echo(f"export-eval-labels wrote {len(rows)} rows to {out_path}")


@app.command("delete-doc")
def delete_doc(
    doc_id: int = typer.Argument(..., help="source_documents.id to delete (undo import)"),
    db: str = typer.Option(None, help="SQLite path"),
):
    """Undo an import: delete the document and every transaction derived from it."""
    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .db import engine, repo_admin

    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        result = repo_admin.delete_document(conn, doc_id)
    typer.echo(f"delete-doc: {result}")


@app.command()
def reprocess(
    doc_id: int = typer.Argument(..., help="source_documents.id to re-ingest"),
    db: str = typer.Option(None, help="SQLite path"),
):
    """Re-triage a document's kind (from its blob) and re-enqueue ingestion.

    Useful for documents parked in needs_review before a pipeline improvement.
    """
    from pathlib import Path

    if db:
        os.environ["DB_PATH"] = db
        get_settings.cache_clear()
    from .db import engine, repo_documents, repo_jobs
    from .ingest.extract.pdf import pdf_doc_kind
    from .ingest.storage import blob_abspath

    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        doc = repo_documents.get_document(conn, doc_id)
        if doc is None:
            typer.echo(f"no document {doc_id}")
            raise typer.Exit(1)
        kind = doc["kind"]
        if doc["mime_type"] == "application/pdf":
            raw = Path(blob_abspath(doc["storage_ref"])).read_bytes()
            kind = pdf_doc_kind(raw)
        conn.execute("UPDATE source_documents SET kind=?, status='staged' WHERE id=?", (kind, doc_id))
        job_id = repo_jobs.enqueue(conn, "ingest_document", {"source_document_id": doc_id},
                                   source_document_id=doc_id)
    typer.echo(f"reprocess doc {doc_id}: kind={kind}, job={job_id} (worker will pick it up)")


# Typer registers `migrate_db` as the command name "migrate-db"; expose "migrate" too.
app.command("migrate")(migrate_db)


def main() -> None:
    app()


if __name__ == "__main__":
    main()


@app.command("research-merchants")
def research_merchants(
    db: str = typer.Option(None, help="SQLite path"),
    limit: int = typer.Option(25, help="Maximum descriptors to research in one run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Research without recording"),
    account: int = typer.Option(None, help="Restrict to one account id"),
):
    """Research statement descriptors that local knowledge cannot explain.

    A paced background drain, not an ingest-time step. Descriptors already
    answered by the knowledge tables are skipped without a search, so repeat
    runs get cheaper as the dictionary fills.
    """
    from .db import engine
    from .agents.merchant_research import (
        build_research_provider,
        config_from_settings,
        research_descriptors,
    )
    from .agents.merchant_research.persistence import (
        already_known,
        has_pending_proposal,
        record_finding,
    )
    from .db.repo_merchant_knowledge import Evidence
    from .db import repo_merchant_knowledge
    from .llm.client import make_llm
    from .reconcile.descriptor_affixes import classify

    settings = get_settings()
    path = _resolve_db(db)

    with engine.read_conn(path) as conn:
        scope = repo_merchant_knowledge.scope_for(
            conn, account_id=int(account) if account else None
        )
        # Household and cardholder names are supplied as private terms so the
        # sanitizer refuses them even if a descriptor smuggles one through.
        private_terms = tuple(
            str(item["name"]).strip().casefold()
            for item in conn.execute(
                "SELECT name FROM household_members WHERE is_active=1"
            ).fetchall()
            if str(item["name"]).strip()
        ) + tuple(
            str(item["display_name"]).strip().casefold()
            for item in conn.execute(
                "SELECT display_name FROM card_holders WHERE trim(display_name) <> ''"
            ).fetchall()
        )

        rows = conn.execute(
            """SELECT raw_description, COUNT(*) AS n, MIN(id) AS line_id
               FROM statement_lines
               WHERE review_disposition='active' AND trim(raw_description) <> ''
                 AND (? IS NULL OR account_id = ?)
               GROUP BY raw_description
               ORDER BY n DESC""",
            (account, account),
        ).fetchall()

        pending: list[tuple[str, str]] = []
        # A representative row per descriptor, so a proposal is anchored to
        # something a reviewer can actually look at. The knowledge tables
        # refuse to accept a claim that names no durable subject.
        subject: dict[str, int] = {}
        skipped_known = skipped_proposed = skipped_deterministic = 0
        for row in rows:
            descriptor = str(row["raw_description"])
            affixes = classify(descriptor)
            if affixes.platform or affixes.non_merchant_kind:
                skipped_deterministic += 1
                continue
            if already_known(conn, descriptor, scope=scope):
                skipped_known += 1
                continue
            if has_pending_proposal(conn, descriptor, scope=scope):
                skipped_proposed += 1
                continue
            subject[descriptor] = int(row["line_id"])
            pending.append((descriptor, ""))
            if len(pending) >= max(1, int(limit)):
                break

    typer.echo(
        f"{len(rows)} distinct descriptors · {skipped_deterministic} settled locally · "
        f"{skipped_known} already known · {skipped_proposed} awaiting review · "
        f"{len(pending)} to research"
    )
    if not pending:
        return

    provider = build_research_provider(settings)
    try:
        report = research_descriptors(
            pending,
            config=config_from_settings(settings),
            provider=provider,
            llm=make_llm(),
            private_terms=private_terms,
            min_interval_s=settings.merchant_search_min_interval_s,
        )
    finally:
        provider.close()

    recorded = 0
    if not dry_run:
        with engine.write_tx(path) as conn:
            for finding in report.findings:
                line_id = subject.get(finding.descriptor)
                claim = record_finding(
                    conn, finding, scope=scope,
                    evidence=Evidence(statement_line_id=line_id),
                )
                if claim is not None:
                    recorded += 1

    for finding in report.findings:
        if finding.resolved:
            cite = finding.citations[0] if finding.citations else ""
            typer.echo(
                f"  resolved  {finding.descriptor[:34]:34} {finding.canonical_merchant[:20]:20} "
                f"{finding.category:13} {cite[:40]}"
            )
        else:
            typer.echo(
                f"  abstain   {finding.descriptor[:34]:34} "
                f"{finding.abstention_reason or finding.notes}"
            )
    typer.echo(
        f"\nsearched {report.searched_count} · resolved {report.resolved_count} · "
        f"abstained {report.abstained_count}"
        + ("" if dry_run else f" · recorded {recorded} proposals")
    )
    if report.diagnostics:
        typer.echo("diagnostics: " + "; ".join(report.diagnostics[:5]))
