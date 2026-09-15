"""Portal routes: home summary, activity, budgets, insights, health."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ...config import get_settings
from ...accounting.contract import FlowKind
from ...db import engine, repo_budgets, repo_insight_prose, repo_jobs, views
from ...diagnostics import version_payload
from ..forms import dollars_to_cents
from ..templating import templates

router = APIRouter()


def _context(tx_limit: int) -> dict:
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = views.dashboard_context(conn, tx_limit, str(settings.db_path))
        ctx["review_count"] = conn.execute(
            "SELECT COUNT(*) FROM source_documents WHERE status='needs_review'"
        ).fetchone()[0]
        return ctx


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", {**_context(6), "active": "home", "brand": "shared"}
    )


def _activity_context(
    *,
    q: str,
    category_id: str,
    account_id: str,
    date_from: str,
    date_to: str,
    flow_kind: str,
    semantic_review: bool,
    cursor: str,
    append: bool,
) -> dict:
    """Assemble the filtered/paginated activity context shared by /activity and /transactions.

    ``append`` is a property of the endpoint, not of the params: only the
    ``/transactions`` fragment renders rows-only appends. ``/activity`` always
    renders the full wrapper, so a stray ``cursor`` never strips its header or
    the why-hold script.
    """
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        result = views.activity_results(
            conn,
            q=q,
            account_id=account_id,
            category_id=category_id,
            date_from=date_from,
            date_to=date_to,
            flow_kind=flow_kind,
            semantic_review=semantic_review,
            cursor=cursor,
        )
        options = views.filter_options(conn)
        pending_flow_review_count = int(
            conn.execute(
                """SELECT COUNT(*) FROM v_transaction_flow_status
                   WHERE semantic_status <> 'complete'"""
            ).fetchone()[0]
        )

    filters = {
        "q": q.strip(),
        "category_id": category_id.strip(),
        "account_id": account_id.strip(),
        "date_from": date_from.strip(),
        "date_to": date_to.strip(),
        "flow_kind": flow_kind.strip(),
        "semantic_review": "1" if semantic_review else "",
    }
    # Active filters, minus the cursor, so the load-more control carries them forward.
    filter_qs = urlencode({k: v for k, v in filters.items() if v})
    return {
        **result,
        **options,
        "filters": filters,
        "filter_qs": filter_qs,
        "append": append,
        "flow_kinds": [item.value for item in FlowKind],
        "pending_flow_review_count": pending_flow_review_count,
    }


@router.get("/activity", response_class=HTMLResponse)
def activity(
    request: Request,
    q: str = "",
    category_id: str = "",
    account_id: str = "",
    date_from: str = "",
    date_to: str = "",
    flow_kind: str = "",
    semantic_review: str = "",
    cursor: str = "",
):
    ctx = _activity_context(
        q=q,
        category_id=category_id,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        flow_kind=flow_kind,
        semantic_review=semantic_review == "1",
        cursor=cursor,
        append=False,
    )
    return templates.TemplateResponse(
        request, "activity.html", {**ctx, "active": "activity", "brand": "finn"}
    )


@router.get("/categories", response_class=HTMLResponse)
def categories(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = repo_budgets.budget_management_context(conn)
    ctx["active"] = "more"
    return templates.TemplateResponse(request, "categories.html", ctx)


def _nonnegative_cents(raw: str) -> int:
    cents = dollars_to_cents(raw.strip() or "0")
    if cents < 0:
        raise HTTPException(400, "amount must be non-negative")
    return cents


def _parse_owner_member_id(raw: str) -> int | None:
    if raw == "shared":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "invalid budget owner") from None


@router.post("/categories/{category_id}/budget")
def update_budget(
    category_id: int,
    amount: str = Form("0"),
    owner_member_id: str = Form("shared"),
    is_leisure: str | None = Form(None),
):
    cents = _nonnegative_cents(amount)
    member_id = _parse_owner_member_id(owner_member_id)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_budgets.set_category_budget(
                conn, category_id=category_id, amount_cents=cents, owner_member_id=member_id
            )
            repo_budgets.set_category_leisure(conn, category_id=category_id, is_leisure=is_leisure == "1")
    except LookupError:
        raise HTTPException(404, "expense category not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/categories", status_code=303)


@router.post("/categories/members")
def create_budget_member(name: str = Form(...)):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_budgets.create_household_member(conn, name=name)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "member already exists") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/categories", status_code=303)


@router.post("/categories/settings/big-ticket")
def update_big_ticket_threshold(amount: str = Form("0")):
    cents = _nonnegative_cents(amount)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        repo_budgets.set_big_ticket_threshold_cents(conn, cents)
    return RedirectResponse("/categories", status_code=303)


@router.get("/insights", response_class=HTMLResponse)
def insights(request: Request, month: str | None = None):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = repo_budgets.insights_context(conn, month)
        ctx["insight_prose"] = (
            repo_insight_prose.get(conn, ctx["selected_month"], "household", "monthly_summary")
            if ctx["selected_month"]
            else None
        )
    return templates.TemplateResponse(request, "insights.html", ctx)


@router.post("/insights/prose/generate")
def generate_insight_prose(month: str = Form("")):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = repo_budgets.insights_context(conn, month or None)
    selected_month = ctx["selected_month"]
    if not selected_month:
        raise HTTPException(400, "no month available")
    payload = {
        "period_month": selected_month,
        "scope": "household",
        "kind": "monthly_summary",
    }
    with engine.write_tx(settings.db_path) as conn:
        repo_jobs.enqueue(conn, "monthly_insight", payload)
    return RedirectResponse(f"/insights?{urlencode({'month': selected_month})}", status_code=303)


@router.post("/insights/subscriptions/action")
def update_subscription_watchlist(
    merchant: str = Form(...),
    account_id: int = Form(...),
    decision: str = Form(...),
    month: str = Form(""),
):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_budgets.set_subscription_watchlist_decision(
                conn,
                merchant=merchant,
                account_id=account_id,
                decision=decision,
            )
    except LookupError:
        raise HTTPException(404, "account not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    suffix = f"?{urlencode({'month': month})}" if month else ""
    return RedirectResponse(f"/insights{suffix}", status_code=303)


@router.post("/insights/cards/action")
def update_planning_insight_card_action(
    card_key: str = Form(...),
    action: str = Form(...),
    month: str = Form(""),
):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_budgets.set_planning_insight_card_action(conn, card_key, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    suffix = f"?{urlencode({'month': month})}" if month else ""
    return RedirectResponse(f"/insights{suffix}", status_code=303)


@router.get("/transactions", response_class=HTMLResponse)
def transactions(
    request: Request,
    q: str = "",
    category_id: str = "",
    account_id: str = "",
    date_from: str = "",
    date_to: str = "",
    flow_kind: str = "",
    semantic_review: str = "",
    cursor: str = "",
):
    ctx = _activity_context(
        q=q,
        category_id=category_id,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        flow_kind=flow_kind,
        semantic_review=semantic_review == "1",
        cursor=cursor,
        # Only a cursored /transactions request is a rows-only load-more append.
        append=bool(views._parse_cursor(cursor)),
    )
    return templates.TemplateResponse(request, "transactions_partial.html", ctx)


@router.get("/healthz", response_class=PlainTextResponse)
def healthz():
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=True) as conn:
        conn.execute("SELECT 1")
    return "ok\n"


@router.get("/version")
def version_info():
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=True) as conn:
        return version_payload(conn)
