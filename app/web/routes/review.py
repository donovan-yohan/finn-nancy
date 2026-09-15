"""Review queue: approve, fix, or reject staged extractions awaiting human review."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ...accounting.contract import currency_review_reason
from ...config import get_settings
from ...db import (
    engine,
    repo_admin,
    repo_captures,
    repo_close,
    repo_documents,
    repo_embeddings,
    repo_ledger,
    repo_merchant_knowledge,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
    repo_structured_imports,
)
from ...db.repo_merchant_knowledge import Evidence
from ...ingest.extract.statement import checksum_ok
from ...ingest.promote import promote_receipt
from ...ingest.schemas import ExtractedReceipt, ExtractedStatement
from ...ingest.storage import blob_abspath
from ...reconcile.descriptor_normalization import DescriptorNormalizationError
from ..forms import dollars_to_cents
from ..templating import templates
from .manage import ACCOUNT_KINDS

router = APIRouter()


def _statement_redirect(doc_id: int, notice: str = "") -> RedirectResponse:
    target = f"/review/statement/{int(doc_id)}"
    if notice:
        target += "?" + urlencode({"notice": notice})
    return RedirectResponse(target, status_code=303)


def _optional_dollars(raw: str) -> int | None:
    return None if not raw.strip() else dollars_to_cents(raw)


def _optional_int(raw: str) -> int | None:
    if not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "invalid integer") from None


def _statement_reason(conn, parsed: ExtractedStatement) -> str:
    """Return the first review gate that keeps a statement from auto-processing."""
    if not parsed.rows:
        return (
            "zero_activity_proof_review"
            if parsed.zero_activity
            else "no_rows"
        )
    reason = currency_review_reason(parsed.currency, get_settings().home_currency)
    if reason is not None:
        return reason
    if not checksum_ok(parsed):
        return "checksum_mismatch"
    if repo_statements.match_account_by_last4(conn, parsed) is None:
        return "account_unresolved"
    return "review_requested"


def _queue_items(conn) -> list[dict]:
    docs = conn.execute(
        "SELECT * FROM source_documents WHERE status='needs_review' ORDER BY created_at DESC, id DESC"
    ).fetchall()
    items = []
    for doc in docs:
        extraction = conn.execute(
            """SELECT * FROM ingest_extractions
               WHERE source_document_id=? AND review_status='pending'
               ORDER BY id DESC LIMIT 1""",
            (doc["id"],),
        ).fetchone()
        receipt = None
        statement = None
        statement_review = None
        structured_import = repo_structured_imports.get_for_document(
            conn, int(doc["id"])
        )
        reason = None
        if extraction is not None:
            try:
                if extraction["doc_kind"] == "statement":
                    statement = ExtractedStatement.model_validate_json(extraction["extracted_json"])
                    statement_review = repo_statement_reviews.get_for_document(
                        conn, int(doc["id"])
                    )
                    reason = _statement_reason(conn, statement)
                elif extraction["doc_kind"] == "receipt":
                    receipt = ExtractedReceipt.model_validate_json(extraction["extracted_json"])
            except ValueError:
                pass
        similar_transactions = []
        if receipt is not None:
            query = " ".join(
                part
                for part in (
                    receipt.merchant,
                    receipt.category_guess,
                    receipt.purchased_on,
                    str(receipt.total_cents or ""),
                )
                if part
            )
            try:
                similar_transactions = repo_embeddings.similar_transactions(conn, text=query, k=3)
            except Exception:  # noqa: BLE001 - review must render without retrieval
                similar_transactions = []
        items.append(
            {
                "doc": doc,
                "extraction": extraction,
                "receipt": receipt,
                "statement": statement,
                "statement_review": statement_review,
                "structured_import": structured_import,
                "reason": reason,
                "triage": repo_documents.triage_note(doc),
                "similar_transactions": similar_transactions,
                "capture_provenance": repo_captures.provenance_for_document(
                    conn, int(doc["id"])
                ),
            }
        )
    return items


@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        items = _queue_items(conn)
        categories = conn.execute(
            "SELECT * FROM categories WHERE kind='expense' ORDER BY name"
        ).fetchall()
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "items": items,
            "categories": categories,
            "accounts": accounts,
            "account_kinds": sorted(ACCOUNT_KINDS),
            "queue_count": len(items),
            "active": "more",
            "brand": "finn",
        },
    )


@router.get("/review/doc/{doc_id}/file")
def review_doc_file(doc_id: int):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        doc = repo_documents.get_document(conn, doc_id)
    if doc is None:
        raise HTTPException(status_code=404)
    # storage_ref is trusted as a data_dir-relative path, but legacy/malicious rows can
    # carry an absolute or '..'-laden ref; resolve and confirm it stays inside data_dir
    # before ever serving it as a file.
    resolved = blob_abspath(doc["storage_ref"]).resolve()
    data_dir = Path(get_settings().data_dir).resolve()
    if not resolved.is_relative_to(data_dir):
        raise HTTPException(status_code=404)
    if not resolved.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(resolved, media_type=doc["mime_type"] or "application/octet-stream")


@router.get("/review/statement/{doc_id}", response_class=HTMLResponse)
def statement_review(
    request: Request, doc_id: int, notice: str = ""
):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        doc = repo_documents.get_document(conn, doc_id)
        if doc is None or doc["kind"] != "statement":
            raise HTTPException(404, "statement review not found")
        try:
            detail = repo_statement_reviews.detail(conn, doc_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        accounts = conn.execute(
            "SELECT * FROM accounts WHERE is_active=1 ORDER BY name"
        ).fetchall()
        extraction = conn.execute(
            """SELECT * FROM ingest_extractions
               WHERE source_document_id=? AND doc_kind='statement'
               ORDER BY id DESC LIMIT 1""",
            (int(doc_id),),
        ).fetchone()
        parsed = None
        if extraction is not None:
            try:
                parsed = ExtractedStatement.model_validate_json(
                    extraction["extracted_json"]
                )
            except ValueError:
                parsed = None
        expectation_link = (
            repo_statement_expectations.active_link_for_document(conn, doc_id)
        )
    return templates.TemplateResponse(
        request,
        "statement_review.html",
        {
            "doc": doc,
            "detail": detail,
            "review": detail["review"],
            "pages": detail["pages"],
            "lines": detail["lines"],
            "completeness": detail["completeness"],
            "audit": detail["audit"],
            "structured_import": detail["structured_import"],
            "accounts": accounts,
            "parsed": parsed,
            "expectation_link": expectation_link,
            "notice": notice,
            "active": "more",
            "brand": "finn",
        },
    )


@router.post("/review/{extraction_id}/approve")
def approve(
    extraction_id: int,
    merchant: str = Form(...),
    purchased_on: str = Form(...),
    total: str = Form(...),
    category_name: str = Form(...),
    account_id: str = Form(""),
):
    settings = get_settings()

    with engine.write_tx(settings.db_path) as conn:
        extraction = conn.execute(
            "SELECT * FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        if extraction is None:
            return RedirectResponse("/review", status_code=303)
        if extraction["review_status"] != "pending":
            return RedirectResponse("/review", status_code=303)
        if extraction["doc_kind"] != "receipt":
            raise HTTPException(400, "extraction is not a receipt")
        doc = repo_documents.get_document(conn, extraction["source_document_id"])
        if doc is None:
            return RedirectResponse("/review", status_code=303)

        total_cents = dollars_to_cents(total)
        acct_id = int(account_id) if account_id.strip() else None
        base = ExtractedReceipt.model_validate_json(extraction["extracted_json"])
        currency_reason = currency_review_reason(base.currency, settings.home_currency)
        if currency_reason is not None:
            raise HTTPException(400, f"{currency_reason}: promotion is disabled")
        if acct_id is not None:
            account = conn.execute(
                "SELECT currency FROM accounts WHERE id=?", (acct_id,)
            ).fetchone()
            if account is None:
                raise HTTPException(400, "invalid account")
            account_currency_reason = currency_review_reason(
                account["currency"], settings.home_currency
            )
            if account_currency_reason is not None:
                raise HTTPException(
                    400, f"{account_currency_reason}: account promotion is disabled"
                )
        receipt = base.model_copy(
            update={
                "merchant": merchant,
                "purchased_on": purchased_on,
                "total_cents": total_cents,
                "category_guess": category_name,
            }
        )
        chosen_category = repo_ledger.find_category_by_name(
            conn, category_name.strip()
        )
        if (
            chosen_category is None
            or chosen_category["kind"] != "expense"
        ):
            raise HTTPException(400, "invalid expense category")

        try:
            result = promote_receipt(
                conn,
                source_document_id=doc["id"],
                sha256=doc["sha256"],
                receipt=receipt,
                extraction_id=extraction_id,
                account_id=acct_id,
                human_review=True,
            )
        except repo_close.MonthLockedError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result["status"] == "inserted":
            transaction_id = int(result["transaction_id"])
            split_id = int(result["split_id"])
            txn = conn.execute(
                "SELECT account_id FROM transactions WHERE id=?",
                (transaction_id,),
            ).fetchone()
            scope = repo_merchant_knowledge.scope_for(
                conn,
                account_id=int(txn["account_id"]),
            )
            try:
                repo_merchant_knowledge.confirm_merchant(
                    conn,
                    descriptor=merchant,
                    canonical_name=merchant,
                    scope=scope,
                    operation_key=f"review:{extraction_id}:merchant",
                    actor="operator:receipt-review",
                    reason="operator confirmed receipt merchant",
                    evidence=Evidence(transaction_id=transaction_id),
                    provenance_kind="operator",
                    provenance_ref=f"extraction:{extraction_id}",
                )
            except DescriptorNormalizationError as exc:
                raise HTTPException(400, str(exc)) from exc

            if chosen_category["name"] != "Uncategorized":
                conn.execute(
                    "UPDATE transaction_splits SET category_id=? WHERE id=?",
                    (int(chosen_category["id"]), split_id),
                )
                repo_merchant_knowledge.confirm_category(
                    conn,
                    descriptor=merchant,
                    category_id=int(chosen_category["id"]),
                    scope=scope,
                    operation_key=f"review:{extraction_id}:category",
                    actor="operator:receipt-review",
                    reason="operator confirmed receipt expense category",
                    evidence=Evidence(
                        transaction_id=transaction_id,
                        transaction_split_id=split_id,
                    ),
                    provenance_kind="manual_recategorization",
                    provenance_ref=f"extraction:{extraction_id}",
                )
        # Whether inserted or duplicate, review is complete: mark approved + processed
        # (link_extraction_txn already set review_status='auto' on insert; override to
        # reflect the human decision).
        conn.execute(
            "UPDATE ingest_extractions SET review_status='approved' WHERE id=?",
            (extraction_id,),
        )
        repo_documents.set_status(conn, doc["id"], "processed")

    return RedirectResponse("/review", status_code=303)


@router.post("/review/{extraction_id}/approve-statement")
def approve_statement(
    extraction_id: int,
    account_id: str = Form(""),
    name: str = Form(""),
    institution: str = Form(""),
    kind: str = Form(""),
    external_ref: str = Form(""),
    statement_cadence: str = Form(""),
    statement_anchor_month: str = Form(""),
):
    """Resolve statement identity, but leave approval to the evidence review."""
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        extraction = conn.execute(
            "SELECT * FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        if extraction is None:
            return RedirectResponse("/review", status_code=303)
        if extraction["review_status"] != "pending":
            return RedirectResponse("/review", status_code=303)
        if extraction["doc_kind"] != "statement":
            raise HTTPException(400, "extraction is not a statement")
        doc = repo_documents.get_document(conn, extraction["source_document_id"])
        if doc is None:
            return RedirectResponse("/review", status_code=303)
        parsed = ExtractedStatement.model_validate_json(extraction["extracted_json"])
        review = repo_statement_reviews.get_for_document(conn, int(doc["id"]))
        if review is None:
            raise HTTPException(
                409, "statement has no canonical review envelope; re-ingest it"
            )

        if account_id.strip():
            try:
                chosen_account_id = int(account_id)
            except ValueError:
                raise HTTPException(400, "invalid account") from None
            account = conn.execute(
                "SELECT id FROM accounts WHERE id=?", (chosen_account_id,)
            ).fetchone()
            if account is None:
                raise HTTPException(400, "invalid account")
        else:
            if not name.strip():
                raise HTTPException(400, "account name is required")
            if kind not in ACCOUNT_KINDS:
                raise HTTPException(400, "invalid account kind")
            cur = conn.execute(
                "INSERT INTO accounts(name, institution, kind, external_ref) VALUES (?,?,?,?)",
                (name.strip(), institution.strip(), kind, external_ref.strip()),
            )
            chosen_account_id = int(cur.lastrowid)
            if statement_cadence not in {"monthly", "quarterly", "annual"}:
                raise HTTPException(
                    400,
                    "new statement accounts require an explicit monthly, "
                    "quarterly, or annual cadence",
                )
            anchor: int | None = None
            if statement_cadence in {"quarterly", "annual"}:
                try:
                    anchor = int(statement_anchor_month)
                except ValueError:
                    raise HTTPException(
                        400,
                        "quarterly and annual statement policies require "
                        "anchor month 1-12",
                    ) from None
            try:
                effective_month = repo_statement_expectations.normalize_month(
                    str(review["period_month"] or "")
                )
                repo_statement_expectations.record_policy(
                    conn,
                    account_id=chosen_account_id,
                    effective_from_month=effective_month,
                    configuration_state="configured",
                    requirement_mode="required",
                    cadence=statement_cadence,
                    anchor_month=anchor,
                    actor="review:operator",
                    reason="statement cadence selected during account creation",
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        page = int(
            parsed.field_pages.get(
                "account_fingerprint",
                parsed.field_pages.get("account_last4", 1),
            )
            or 1
        )
        try:
            repo_statement_reviews.update_metadata(
                conn,
                int(review["id"]),
                expected_revision=int(review["revision"]),
                actor="review:operator",
                reason="operator confirmed statement account identity",
                source_page=page,
                values={"account_id": chosen_account_id},
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return _statement_redirect(
        int(doc["id"]), "Account saved. Review metadata and rows before approval."
    )


@router.post("/review/statement/{doc_id}/metadata")
def update_statement_metadata(
    doc_id: int,
    expected_revision: int = Form(...),
    source_page: int = Form(...),
    account_id: str = Form(...),
    period_start_on: str = Form(""),
    period_end_on: str = Form(""),
    statement_issued_on: str = Form(""),
    opening_balance: str = Form(""),
    closing_balance: str = Form(""),
    currency: str = Form(""),
    activity_kind: str = Form("unknown"),
    declared_page_count: str = Form(""),
    declared_row_count: str = Form(""),
    reason: str = Form(...),
):
    settings = get_settings()
    try:
        chosen_account_id = int(account_id)
    except ValueError:
        raise HTTPException(400, "invalid account") from None
    values = {
        "account_id": chosen_account_id,
        "period_start_on": period_start_on,
        "period_end_on": period_end_on,
        "statement_issued_on": statement_issued_on,
        "opening_balance_cents": _optional_dollars(opening_balance),
        "closing_balance_cents": _optional_dollars(closing_balance),
        "currency": currency,
        "activity_kind": activity_kind,
        "declared_page_count": _optional_int(declared_page_count),
        "declared_row_count": _optional_int(declared_row_count),
    }
    with engine.write_tx(settings.db_path) as conn:
        review = repo_statement_reviews.get_for_document(conn, doc_id)
        if review is None:
            raise HTTPException(404, "statement review not found")
        try:
            repo_statement_reviews.update_metadata(
                conn,
                int(review["id"]),
                expected_revision=expected_revision,
                actor="review:operator",
                reason=reason,
                source_page=source_page,
                values=values,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return _statement_redirect(doc_id, "Statement metadata saved and audited.")


@router.post("/review/statement/{doc_id}/row/{line_id}")
def correct_statement_row(
    doc_id: int,
    line_id: int,
    expected_review_revision: int = Form(...),
    expected_line_revision: int = Form(...),
    source_page: int = Form(...),
    posted_on: str = Form(...),
    description: str = Form(...),
    amount: str = Form(...),
    currency: str = Form(...),
    balance: str = Form(""),
    is_pending: str | None = Form(None),
    reason: str = Form(...),
):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        line = conn.execute(
            """SELECT source_document_id FROM statement_lines
               WHERE id=? AND source_document_id=?""",
            (int(line_id), int(doc_id)),
        ).fetchone()
        if line is None:
            raise HTTPException(404, "statement row not found")
        try:
            repo_statement_reviews.correct_row(
                conn,
                line_id,
                expected_review_revision=expected_review_revision,
                expected_line_revision=expected_line_revision,
                actor="review:operator",
                reason=reason,
                source_page=source_page,
                posted_on=posted_on,
                description=description,
                amount_cents=dollars_to_cents(amount),
                currency=currency,
                balance_cents=_optional_dollars(balance),
                is_pending=is_pending is not None,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return _statement_redirect(doc_id, "Statement row corrected and audited.")


@router.post("/review/statement/{doc_id}/row")
def add_statement_row(
    doc_id: int,
    expected_review_revision: int = Form(...),
    source_page: int = Form(...),
    posted_on: str = Form(...),
    description: str = Form(...),
    amount: str = Form(...),
    currency: str = Form(...),
    balance: str = Form(""),
    is_pending: str | None = Form(None),
    reason: str = Form(...),
):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        review = repo_statement_reviews.get_for_document(conn, doc_id)
        if review is None:
            raise HTTPException(404, "statement review not found")
        try:
            repo_statement_reviews.add_row(
                conn,
                int(review["id"]),
                expected_review_revision=expected_review_revision,
                actor="review:operator",
                reason=reason,
                source_page=source_page,
                posted_on=posted_on,
                description=description,
                amount_cents=dollars_to_cents(amount),
                currency=currency,
                balance_cents=_optional_dollars(balance),
                is_pending=is_pending is not None,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return _statement_redirect(doc_id, "Statement row added and audited.")


def _set_statement_row_disposition(
    *,
    doc_id: int,
    line_id: int,
    expected_review_revision: int,
    expected_line_revision: int,
    reason: str,
    restore: bool,
) -> RedirectResponse:
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        line = conn.execute(
            """SELECT source_document_id FROM statement_lines
               WHERE id=? AND source_document_id=?""",
            (int(line_id), int(doc_id)),
        ).fetchone()
        if line is None:
            raise HTTPException(404, "statement row not found")
        mutator = (
            repo_statement_reviews.restore_row
            if restore
            else repo_statement_reviews.exclude_row
        )
        try:
            mutator(
                conn,
                line_id,
                expected_review_revision=expected_review_revision,
                expected_line_revision=expected_line_revision,
                actor="review:operator",
                reason=reason,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    verb = "restored" if restore else "excluded"
    return _statement_redirect(doc_id, f"Statement row {verb} with audit history.")


@router.post("/review/statement/{doc_id}/row/{line_id}/exclude")
def exclude_statement_row(
    doc_id: int,
    line_id: int,
    expected_review_revision: int = Form(...),
    expected_line_revision: int = Form(...),
    reason: str = Form(...),
):
    return _set_statement_row_disposition(
        doc_id=doc_id,
        line_id=line_id,
        expected_review_revision=expected_review_revision,
        expected_line_revision=expected_line_revision,
        reason=reason,
        restore=False,
    )


@router.post("/review/statement/{doc_id}/row/{line_id}/restore")
def restore_statement_row(
    doc_id: int,
    line_id: int,
    expected_review_revision: int = Form(...),
    expected_line_revision: int = Form(...),
    reason: str = Form(...),
):
    return _set_statement_row_disposition(
        doc_id=doc_id,
        line_id=line_id,
        expected_review_revision=expected_review_revision,
        expected_line_revision=expected_line_revision,
        reason=reason,
        restore=True,
    )


@router.post("/review/statement/{doc_id}/approve")
def approve_statement_review(
    doc_id: int,
    expected_revision: int = Form(...),
    reason: str = Form(...),
    override_reason: str = Form(""),
):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        review = repo_statement_reviews.get_for_document(conn, doc_id)
        if review is None:
            raise HTTPException(404, "statement review not found")
        try:
            approved, _ = repo_statement_reviews.approve(
                conn,
                int(review["id"]),
                expected_revision=expected_revision,
                actor="review:operator",
                reason=reason,
                override_reason=override_reason,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    state = str(approved["review_state"]).replace("_", " ")
    return _statement_redirect(
        doc_id, f"Statement {state}; reconciliation is ready."
    )


@router.post("/review/statement/{doc_id}/archive")
def archive_statement_review(
    doc_id: int,
    reason: str = Form(...),
):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        try:
            repo_statement_reviews.archive_source(
                conn,
                doc_id,
                actor="review:operator",
                reason=reason,
            )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{extraction_id}/reject")
def reject(extraction_id: int):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        extraction = conn.execute(
            "SELECT * FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        if extraction is None or extraction["review_status"] != "pending":
            return RedirectResponse("/review", status_code=303)
        if extraction["doc_kind"] == "statement":
            try:
                repo_statement_reviews.archive_source(
                    conn,
                    int(extraction["source_document_id"]),
                    actor="review:operator",
                    reason="operator rejected statement extraction",
                )
            except (ValueError, repo_close.MonthLockedError) as exc:
                raise HTTPException(409, str(exc)) from exc
        else:
            conn.execute(
                "UPDATE ingest_extractions SET review_status='rejected' WHERE id=?",
                (extraction_id,),
            )
            repo_documents.set_status(
                conn, extraction["source_document_id"], "archived"
            )
    return RedirectResponse("/review", status_code=303)


@router.post("/review/doc/{doc_id}/delete")
def delete_doc(doc_id: int):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        try:
            review = repo_statement_reviews.get_for_document(conn, doc_id)
            if review is not None:
                repo_statement_reviews.archive_source(
                    conn,
                    doc_id,
                    actor="review:operator",
                    reason="operator archived statement document from review",
                )
            else:
                doc = repo_documents.get_document(conn, doc_id)
                if doc is not None and doc["kind"] == "statement":
                    raise ValueError(
                        "statement has no canonical review envelope; re-ingest it"
                    )
                repo_admin.delete_document(
                    conn,
                    doc_id,
                    actor="review:operator",
                    reason="operator deleted document from review",
                )
        except (ValueError, repo_close.MonthLockedError) as exc:
            raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/review", status_code=303)
