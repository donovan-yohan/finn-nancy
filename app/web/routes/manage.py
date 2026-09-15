"""Manage UI: accounts + categories CRUD, opening balances."""
from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...accounting.contract import FlowKind
from ...config import get_settings
from ...db import engine, repo_card_holders
from ...db import (
    repo_captures,
    repo_close,
    repo_ledger,
    repo_statement_expectations,
)
from ..forms import dollars_to_cents
from ..templating import templates

router = APIRouter()

ACCOUNT_KINDS = {"chequing", "savings", "credit", "cash", "investment"}
CATEGORY_KINDS = {"income", "expense", "transfer"}
BRAND_OWNERS = {"finn", "nancy", "shared"}


def _ensure_opening_balance_category(conn: sqlite3.Connection) -> int:
    """Get-or-create the system 'Opening Balance' category (kind=transfer, so cashflow ignores it)."""
    row = conn.execute(
        "SELECT id FROM categories WHERE name='Opening Balance' AND kind='transfer'"
    ).fetchone()
    if row:
        return int(row["id"])
    taken = conn.execute("SELECT kind FROM categories WHERE name='Opening Balance'").fetchone()
    if taken:
        raise HTTPException(
            400,
            f"'Opening Balance' category already exists with kind={taken['kind']!r}, expected 'transfer'",
        )
    cur = conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) "
        "VALUES ('Opening Balance','transfer','shared','#9AA5B1')"
    )
    return int(cur.lastrowid)


def _context(request: Request) -> dict:
    settings = get_settings()
    runtime = getattr(request.app.state, "telegram_runtime", None)
    runtime_snapshot = (
        runtime.snapshot()
        if runtime is not None
        else {
            "configured": bool(
                settings.telegram_bot_token and settings.telegram_chat_id
            ),
            "allowed": False,
            "running": False,
        }
    )
    statement_effective_month = date.today().strftime("%Y-%m")
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        card_holders = repo_card_holders.listing(conn)
        unnamed_cards = repo_card_holders.unnamed_count(conn)
        statement_policies = {
            int(account["id"]): policy
            for account in accounts
            if (
                policy := repo_statement_expectations.policy_for_month(
                    conn, int(account["id"]), statement_effective_month
                )
            )
            is not None
        }
        latest_statement_policies = repo_statement_expectations.latest_policies(conn)
        categories = conn.execute("SELECT * FROM categories ORDER BY kind, name").fetchall()
        openings = {
            r["account_id"]: r
            for r in conn.execute("SELECT * FROM transactions WHERE source='opening'").fetchall()
        }
        capture_transports = repo_captures.transport_settings(
            conn,
            strict_local_mode=settings.strict_local_mode,
            configured_channels=(
                {"telegram"} if runtime_snapshot["configured"] else set()
            ),
            running_channels=(
                {"telegram"} if runtime_snapshot["running"] else set()
            ),
        )
        capture_metrics = repo_captures.capture_metrics(conn)
    return {
        "accounts": accounts,
        "statement_policies": statement_policies,
        "latest_statement_policies": latest_statement_policies,
        "statement_effective_month": statement_effective_month,
        "statement_configurations": [
            item.value for item in repo_statement_expectations.PolicyConfiguration
        ],
        "statement_requirement_modes": [
            item.value for item in repo_statement_expectations.RequirementMode
        ],
        "statement_cadences": [
            item.value for item in repo_statement_expectations.StatementCadence
        ],
        "categories": categories,
        "openings": openings,
        "brand": "finn",
        "account_kinds": sorted(ACCOUNT_KINDS),
        "category_kinds": sorted(CATEGORY_KINDS),
        "brand_owners": sorted(BRAND_OWNERS),
        "strict_local_mode": settings.strict_local_mode,
        "capture_transports": capture_transports,
        "capture_metrics": capture_metrics,
        "card_holders": card_holders,
        "unnamed_card_count": unnamed_cards,
    }


def _canonical_statement_policy_fields(
    configuration_state: str,
    requirement_mode: str,
    cadence: str,
    anchor_month: str,
) -> tuple[str | None, str | None, str | None]:
    mode = requirement_mode or None
    normalized_cadence = cadence or None
    normalized_anchor = anchor_month or None
    if configuration_state == "unconfigured":
        return None, None, None
    if requirement_mode == "no_statement":
        return "no_statement", "none", None
    if requirement_mode == "required" and cadence == "monthly":
        return "required", "monthly", None
    return mode, normalized_cadence, normalized_anchor


@router.get("/manage", response_class=HTMLResponse)
def manage_page(request: Request):
    return templates.TemplateResponse(
        request, "manage.html", {**_context(request), "active": "more"}
    )


@router.post("/manage/capture-transports/telegram")
def record_telegram_transport_decision(
    decision: str = Form(...),
    acknowledge: str = Form(""),
):
    if decision == "consented" and acknowledge != "1":
        raise HTTPException(
            400,
            "consent requires acknowledging that Telegram receives files and replies",
        )
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_captures.record_transport_decision(
                conn,
                transport="telegram",
                decision=decision,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/manage#capture-privacy", status_code=303)


@router.post("/manage/accounts")
def create_account(name: str = Form(...), institution: str = Form(""), kind: str = Form(...),
                    external_ref: str = Form("")):
    if kind not in ACCOUNT_KINDS:
        raise HTTPException(400, "invalid account kind")
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO accounts(name, institution, kind, external_ref) VALUES (?,?,?,?)",
            (name, institution, kind, external_ref),
        )
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/accounts/{account_id}")
def update_account(account_id: int, name: str = Form(...), institution: str = Form(""),
                    kind: str = Form(...), external_ref: str = Form(""), is_active: int = Form(...)):
    if kind not in ACCOUNT_KINDS:
        raise HTTPException(400, "invalid account kind")
    if is_active not in (0, 1):
        raise HTTPException(400, "invalid is_active")
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        conn.execute(
            "UPDATE accounts SET name=?, institution=?, kind=?, external_ref=?, is_active=? WHERE id=?",
            (name, institution, kind, external_ref, is_active, account_id),
        )
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/accounts/{account_id}/statement-policy")
def record_statement_policy(
    account_id: int,
    effective_from_month: str = Form(...),
    configuration_state: str = Form(...),
    requirement_mode: str = Form(""),
    cadence: str = Form(""),
    anchor_month: str = Form(""),
    active_from: str = Form(""),
    active_to: str = Form(""),
    reason: str = Form(...),
):
    settings = get_settings()
    requirement_mode, cadence, anchor_month = _canonical_statement_policy_fields(
        configuration_state,
        requirement_mode,
        cadence,
        anchor_month,
    )
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_statement_expectations.record_policy(
                conn,
                account_id=account_id,
                effective_from_month=effective_from_month,
                configuration_state=configuration_state,
                requirement_mode=requirement_mode,
                cadence=cadence,
                anchor_month=anchor_month,
                active_from=active_from or None,
                active_to=active_to or None,
                actor="web:manage",
                reason=reason,
            )
    except repo_close.MonthLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/categories")
def create_category(name: str = Form(...), kind: str = Form(...), brand_owner: str = Form("finn"),
                     color: str = Form("#4EA1FF")):
    if kind not in CATEGORY_KINDS:
        raise HTTPException(400, "invalid category kind")
    if brand_owner not in BRAND_OWNERS:
        raise HTTPException(400, "invalid brand_owner")
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES (?,?,?,?)",
                (name, kind, brand_owner, color),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "category name already exists") from None
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/categories/{category_id}")
def update_category(category_id: int, name: str = Form(...), brand_owner: str = Form(...),
                     color: str = Form(...)):
    if brand_owner not in BRAND_OWNERS:
        raise HTTPException(400, "invalid brand_owner")
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            row = conn.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "category not found")
            if row["name"] == "Uncategorized" and name != "Uncategorized":
                raise HTTPException(400, "cannot rename Uncategorized")
            conn.execute(
                "UPDATE categories SET name=?, brand_owner=?, color=? WHERE id=?",
                (name, brand_owner, color, category_id),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "category name already exists") from None
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/accounts/{account_id}/opening")
def set_opening_balance(account_id: int, amount: str = Form(...), posted_on: str = Form("")):
    cents = dollars_to_cents(amount)
    if posted_on:
        try:
            posted_on = date.fromisoformat(posted_on).isoformat()
        except ValueError:
            raise HTTPException(400, "invalid date")
    else:
        posted_on = date.today().isoformat()

    external_id = f"open:{account_id}"
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        category_id = _ensure_opening_balance_category(conn)
        existing = conn.execute(
            "SELECT id FROM transactions WHERE source='opening' AND external_id=?", (external_id,)
        ).fetchone()
        if existing:
            txn_id = int(existing["id"])
            try:
                repo_close.guard_transaction_write(
                    conn,
                    txn_id,
                    extra_month=posted_on[:7],
                )
            except repo_close.MonthLockedError as exc:
                raise HTTPException(409, str(exc)) from exc
            conn.execute(
                "UPDATE transactions SET amount_cents=?, posted_on=? WHERE id=?",
                (cents, posted_on, txn_id),
            )
            conn.execute(
                "UPDATE transaction_splits SET amount_cents=? WHERE transaction_id=?",
                (cents, txn_id),
            )
        else:
            try:
                txn_id = repo_ledger.insert_transaction(
                    conn, account_id=account_id, posted_on=posted_on,
                    description="Opening balance", counterparty="",
                    amount_cents=cents, source="opening", external_id=external_id,
                    source_document_id=None, source_confidence=1.0,
                    flow_kind=FlowKind.OPENING,
                )
            except repo_close.MonthLockedError as exc:
                raise HTTPException(409, str(exc)) from exc
            if txn_id is not None:
                repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=category_id,
                                         amount_cents=cents)
    return RedirectResponse("/manage", status_code=303)


@router.post("/manage/cardholders/{card_last4}")
def name_cardholder(card_last4: str, display_name: str = Form("")):
    """Name the person who spends on a card.

    Naming is never required: an unnamed card keeps reporting under its
    placeholder rather than blocking a statement or a close.
    """
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        repo_card_holders.set_name(conn, card_last4, display_name)
    return RedirectResponse("/manage", status_code=303)
