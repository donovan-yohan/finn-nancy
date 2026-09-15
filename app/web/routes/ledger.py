"""Manual transaction routes: add / edit / delete a single transaction (+ its one split)."""
from __future__ import annotations

import sqlite3
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...accounting import flows
from ...accounting.contract import FlowKind
from ...config import get_settings
from ...db import engine, repo_captures, repo_close, repo_embeddings, repo_ledger
from ..forms import dollars_to_cents
from ..templating import templates

router = APIRouter()


def _parse_date(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "invalid date") from None
    return raw


def _get_category(conn: sqlite3.Connection, category_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown category")
    return row


def _get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown account")
    return row


def _signed_cents(
    category_kind: str,
    raw_amount: str,
    flow_kind: FlowKind | str = FlowKind.UNKNOWN,
) -> int:
    """Turn entered dollars into signed cents without conflating purpose and flow.

    A known flow supplies the intrinsic direction. Unknown preserves the legacy
    category-based sign until a reviewer identifies the movement. Transfer-like
    and reversal flows honour the sign entered because direction is contextual.
    """
    cents = dollars_to_cents(raw_amount)
    try:
        flow = flows.normalize_flow_kind(flow_kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if flow in (FlowKind.PURCHASE, FlowKind.FEE):
        return -abs(cents)
    if flow in (
        FlowKind.INCOME,
        FlowKind.INTEREST,
        FlowKind.REFUND,
        FlowKind.REIMBURSEMENT,
    ):
        return abs(cents)
    if flow != FlowKind.UNKNOWN:
        return cents
    if category_kind == "expense":
        return -abs(cents)
    if category_kind == "income":
        return abs(cents)
    return cents


def _form_context(conn: sqlite3.Connection, *, txn=None, split=None, multi_split=False,
                  lock_error: str = "") -> dict:
    capture_provenance = (
        repo_captures.provenance_for_document(
            conn, int(txn["source_document_id"])
        )
        if txn is not None and txn["source_document_id"] is not None
        else []
    )
    return {
        "active": "activity",
        "brand": "finn",
        "accounts": conn.execute("SELECT * FROM accounts WHERE is_active=1 ORDER BY name").fetchall(),
        "categories": conn.execute("SELECT * FROM categories ORDER BY kind, name").fetchall(),
        "txn": txn,
        "split": split,
        "multi_split": multi_split,
        "lock_error": lock_error,
        "today": date.today().isoformat(),
        "flow_kinds": list(FlowKind),
        "capture_provenance": capture_provenance,
    }


def _reload_edit_form(request: Request, conn: sqlite3.Connection, txn_id: int, message: str):
    """Re-render the edit form carrying an inline lock message (HTTP 200, not a 400)."""
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
    splits = conn.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn_id,)
    ).fetchall()
    ctx = _form_context(
        conn,
        txn=txn,
        split=splits[0] if len(splits) == 1 else None,
        multi_split=len(splits) > 1,
        lock_error=message,
    )
    return templates.TemplateResponse(request, "txn_form.html", ctx)


@router.get("/txn/new", response_class=HTMLResponse)
def new_txn_form(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = _form_context(conn)
    return templates.TemplateResponse(request, "txn_form.html", ctx)


@router.post("/txn/new")
def create_txn(
    posted_on: str = Form(...),
    description: str = Form(...),
    counterparty: str = Form(""),
    amount: str = Form(...),
    account_id: int = Form(...),
    category_id: int = Form(...),
    flow_kind: str = Form(FlowKind.UNKNOWN.value),
    notes: str = Form(""),
):
    posted_on = _parse_date(posted_on)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _get_account(conn, account_id)
        category = _get_category(conn, category_id)
        cents = _signed_cents(category["kind"], amount, flow_kind)
        try:
            txn_id = repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on=posted_on,
                description=description,
                counterparty=counterparty,
                amount_cents=cents,
                source="manual",
                external_id=f"manual:{uuid4().hex}",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind=flow_kind,
                notes=notes,
            )
        except repo_close.MonthLockedError as exc:
            raise HTTPException(409, str(exc)) from exc
        if txn_id is not None:
            repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=category_id, amount_cents=cents)
            repo_embeddings.enqueue_embed_transactions(conn)
    return RedirectResponse("/activity", status_code=303)


@router.get("/txn/{txn_id}/edit", response_class=HTMLResponse)
def edit_txn_form(request: Request, txn_id: int):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if txn is None:
            raise HTTPException(404, "unknown transaction")
        splits = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchall()
        multi_split = len(splits) > 1
        ctx = _form_context(conn, txn=txn, split=splits[0] if len(splits) == 1 else None, multi_split=multi_split)
    return templates.TemplateResponse(request, "txn_form.html", ctx)


@router.post("/txn/{txn_id}/edit")
def update_txn(
    request: Request,
    txn_id: int,
    posted_on: str = Form(...),
    description: str = Form(...),
    counterparty: str = Form(""),
    notes: str = Form(""),
    amount: str | None = Form(None),
    category_id: int | None = Form(None),
    flow_kind: str | None = Form(None),
    override: str | None = Form(None),
):
    posted_on = _parse_date(posted_on)
    override_on = override == "1"
    target_month = posted_on[:7]
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if txn is None:
            raise HTTPException(404, "unknown transaction")
        if txn["source"] == "opening" and (amount is not None or category_id is not None):
            # Opening-balance txns are system-generated transfers; reclassifying the
            # amount/category here would corrupt the account's starting balance.
            raise HTTPException(400, "opening balance amount/category cannot be edited")
        # Soft lock: block edits to a closed month (or moving a txn into one) unless the
        # user opts into an audited override.
        try:
            locked_months = repo_close.guard_transaction_write(
                conn, txn_id, override=override_on, extra_month=target_month
            )
        except repo_close.MonthLockedError as exc:
            return _reload_edit_form(
                request, conn, txn_id,
                f"{exc.month} is closed. Tick “override the lock” to edit it — the change is audited.",
            )
        splits = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchall()
        old_amount = int(txn["amount_cents"])
        old_category_id = int(splits[0]["category_id"]) if len(splits) == 1 else None
        old_posted_on = txn["posted_on"]
        old_description = txn["description"]
        old_counterparty = txn["counterparty"]
        old_notes = txn["notes"]
        old_flow_kind = txn["flow_kind"]
        conn.execute(
            "UPDATE transactions SET posted_on=?, description=?, counterparty=?, notes=? WHERE id=?",
            (posted_on, description, counterparty, notes, txn_id),
        )
        new_amount = old_amount
        new_category_id = old_category_id
        proposed_flow_kind = flow_kind if flow_kind is not None else old_flow_kind
        if len(splits) == 1 and amount is not None and category_id is not None:
            category = _get_category(conn, category_id)
            cents = _signed_cents(category["kind"], amount, proposed_flow_kind)
            try:
                flows.validate_flow_amount(proposed_flow_kind, cents)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            conn.execute("UPDATE transactions SET amount_cents=? WHERE id=?", (cents, txn_id))
            conn.execute(
                "UPDATE transaction_splits SET category_id=?, amount_cents=? WHERE id=?",
                (category_id, cents, splits[0]["id"]),
            )
            new_amount = cents
            new_category_id = int(category_id)
        new_flow_kind = old_flow_kind
        if flow_kind is not None and txn["source"] != "opening":
            try:
                flows.set_flow_kind(
                    conn,
                    txn_id,
                    flow_kind,
                    actor="web:ledger",
                    reason="manual transaction edit",
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            new_flow_kind = flow_kind
        for month in locked_months:  # non-empty only when override_on
            field, old_value, new_value = _edit_audit_change(
                old_category_id=old_category_id, new_category_id=new_category_id,
                old_amount=old_amount, new_amount=new_amount,
                old_flow_kind=old_flow_kind, new_flow_kind=new_flow_kind,
                old_posted_on=old_posted_on, new_posted_on=posted_on,
                old_description=old_description, new_description=description,
                old_counterparty=old_counterparty, new_counterparty=counterparty,
                old_notes=old_notes, new_notes=notes,
            )
            repo_close.record_audit(
                conn, month=month, entity="transaction", entity_id=txn_id,
                field=field, old_value=old_value, new_value=new_value,
                reason="override edit via ledger",
            )
        repo_embeddings.enqueue_embed_transactions(conn)
    return RedirectResponse("/activity", status_code=303)


def _edit_audit_change(*, old_category_id, new_category_id, old_amount, new_amount,
                       old_flow_kind, new_flow_kind,
                       old_posted_on, new_posted_on, old_description, new_description,
                       old_counterparty, new_counterparty, old_notes, new_notes):
    """Pick the single most meaningful field changed by an override edit for the audit row.

    Priority reflects financial impact: category/amount first, then a date move (which
    can cross a closed-month boundary), then the free-text fields. Reporting the field
    that actually changed keeps the close audit trail truthful.
    """
    if old_category_id != new_category_id:
        return ("category_id", old_category_id, new_category_id)
    if old_amount != new_amount:
        return ("amount_cents", old_amount, new_amount)
    if old_flow_kind != new_flow_kind:
        return ("flow_kind", old_flow_kind, new_flow_kind)
    if old_posted_on != new_posted_on:
        return ("posted_on", old_posted_on, new_posted_on)
    for field, old_value, new_value in (
        ("description", old_description, new_description),
        ("counterparty", old_counterparty, new_counterparty),
        ("notes", old_notes, new_notes),
    ):
        if old_value != new_value:
            return (field, old_value, new_value)
    return ("fields", None, None)


@router.post("/txn/{txn_id}/delete")
def delete_txn(request: Request, txn_id: int, override: str | None = Form(None)):
    override_on = override == "1"
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        row = conn.execute(
            "SELECT id, amount_cents FROM transactions WHERE id=?", (txn_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "unknown transaction")
        try:
            locked_months = repo_close.guard_transaction_write(
                conn, txn_id, override=override_on
            )
        except repo_close.MonthLockedError as exc:
            return _reload_edit_form(
                request, conn, txn_id,
                f"{exc.month} is closed. Tick “override the lock” to delete this "
                "transaction — the deletion is audited.",
            )
        for month in locked_months:  # non-empty only when override_on
            repo_close.record_audit(
                conn, month=month, entity="transaction", entity_id=txn_id,
                field="deleted", old_value=int(row["amount_cents"]), new_value=None,
                reason="override delete via ledger",
            )
        conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))  # splits cascade
    return RedirectResponse("/activity", status_code=303)
