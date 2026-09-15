"""Goals UI and monthly close routes."""
from __future__ import annotations

import sqlite3
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import get_settings
from ...db import engine, repo_budgets, repo_goals
from ..forms import dollars_to_cents
from ..templating import templates

router = APIRouter()

GOAL_KINDS = {"savings_target", "payoff", "sinking_fund"}
BRAND_OWNERS = {"finn", "nancy", "shared"}
GOAL_STATUSES = {"active", "paused", "completed", "cancelled"}


def _today_month() -> str:
    return date.today().strftime("%Y-%m")


def _parse_month(raw: str, *, required: bool = True) -> str | None:
    raw = raw.strip()
    if not raw:
        if required:
            raise HTTPException(400, "month is required")
        return None
    try:
        return repo_goals.add_months(raw, 0)
    except ValueError:
        raise HTTPException(400, "invalid month") from None


def _positive_cents(raw: str, label: str) -> int:
    cents = dollars_to_cents(raw.strip())
    if cents <= 0:
        raise HTTPException(400, f"{label} must be positive")
    return cents


def _nonnegative_cents(raw: str, label: str) -> int:
    cents = dollars_to_cents(raw.strip() or "0")
    if cents < 0:
        raise HTTPException(400, f"{label} must be non-negative")
    return cents


def _optional_int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "invalid id") from None


def _priority(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, "invalid priority") from None


def _validate_kind(kind: str) -> str:
    if kind not in GOAL_KINDS:
        raise HTTPException(400, "invalid goal kind")
    return kind


def _validate_brand_owner(brand_owner: str) -> str:
    if brand_owner not in BRAND_OWNERS:
        raise HTTPException(400, "invalid brand_owner")
    return brand_owner


def _month_choices(conn: sqlite3.Connection, selected_month: str | None = None) -> list[str]:
    current_month = _today_month()
    choices = {month for month in repo_budgets.available_months(conn) if month <= current_month}
    for row in conn.execute(
        """
        SELECT start_month AS month FROM goals
        UNION
        SELECT target_month AS month FROM goals WHERE target_month IS NOT NULL
        UNION
        SELECT month FROM goal_ledger
        """
    ).fetchall():
        if row["month"] and row["month"] <= current_month:
            choices.add(row["month"])
    if selected_month and selected_month <= current_month:
        choices.add(selected_month)
    if not choices:
        choices.add(current_month)
    return sorted(choices, reverse=True)


def _selected_month(conn: sqlite3.Connection, raw: str | None) -> tuple[str, list[str]]:
    month = _parse_month(raw or "", required=False)
    if month and month > _today_month():
        month = None
    months = _month_choices(conn, month)
    return (month or months[0], months)


def _validate_linked_refs(
    conn: sqlite3.Connection,
    *,
    linked_transaction_id: int | None,
    linked_category_id: int | None,
) -> None:
    if linked_transaction_id is not None:
        row = conn.execute("SELECT 1 FROM transactions WHERE id=?", (linked_transaction_id,)).fetchone()
        if row is None:
            raise HTTPException(400, "linked transaction not found")
    if linked_category_id is not None:
        row = conn.execute("SELECT 1 FROM categories WHERE id=?", (linked_category_id,)).fetchone()
        if row is None:
            raise HTTPException(400, "linked category not found")


def _goal_context(
    conn: sqlite3.Connection,
    *,
    selected_month: str | None = None,
    edit_goal_id: int | None = None,
) -> dict:
    month, months = _selected_month(conn, selected_month)
    edit_goal = None
    if edit_goal_id is not None:
        edit_goal = conn.execute("SELECT * FROM goals WHERE id=?", (edit_goal_id,)).fetchone()
        if edit_goal is None:
            raise HTTPException(404, "goal not found")
    include_transaction_id = None
    if edit_goal is not None and edit_goal["linked_transaction_id"] is not None:
        include_transaction_id = int(edit_goal["linked_transaction_id"])
    return {
        "active": "goals",
        "brand": "nancy",
        "selected_month": month,
        "months": months,
        "goals": repo_goals.goal_progress_rows(conn, month),
        "edit_goal": edit_goal,
        "kinds": sorted(GOAL_KINDS),
        "brand_owners": sorted(BRAND_OWNERS),
        **repo_goals.goal_form_options(conn, include_transaction_id=include_transaction_id),
    }


def _redirect_back(request: Request, fallback: str) -> RedirectResponse:
    target = request.headers.get("referer") or fallback
    return RedirectResponse(target, status_code=303)


@router.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, month: str | None = None):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = _goal_context(conn, selected_month=month)
    return templates.TemplateResponse(request, "goals.html", ctx)


@router.post("/goals")
def create_goal(
    name: str = Form(...),
    kind: str = Form(...),
    target: str = Form(...),
    start_month: str = Form(...),
    target_month: str = Form(""),
    monthly_contribution: str = Form("0"),
    linked_transaction_id: str = Form(""),
    linked_category_id: str = Form(""),
    priority: str = Form("100"),
    brand_owner: str = Form("nancy"),
    color: str = Form("#FF9F43"),
    notes: str = Form(""),
    auto_fund: str | None = Form(None),
):
    kind = _validate_kind(kind)
    brand_owner = _validate_brand_owner(brand_owner)
    target_cents = _positive_cents(target, "target")
    monthly_cents = _nonnegative_cents(monthly_contribution, "monthly contribution")
    start = _parse_month(start_month)
    target_m = _parse_month(target_month, required=False)
    if target_m and target_m < start:
        raise HTTPException(400, "target month must be on or after start month")
    auto = 1 if auto_fund == "1" or (auto_fund is None and kind in {"payoff", "sinking_fund"}) else 0
    linked_txn = _optional_int(linked_transaction_id)
    linked_category = _optional_int(linked_category_id)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _validate_linked_refs(
            conn,
            linked_transaction_id=linked_txn,
            linked_category_id=linked_category,
        )
        conn.execute(
            """
            INSERT INTO goals(
              name, kind, target_cents, start_month, target_month,
              monthly_contribution_cents, linked_transaction_id, linked_category_id,
              auto_fund, priority, brand_owner, color, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                kind,
                target_cents,
                start,
                target_m,
                monthly_cents,
                linked_txn,
                linked_category,
                auto,
                _priority(priority),
                brand_owner,
                color.strip() or "#FF9F43",
                notes,
            ),
        )
    return RedirectResponse("/goals", status_code=303)


@router.get("/goals/close", response_class=HTMLResponse)
def close_review(request: Request, month: str | None = None):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        selected, months = _selected_month(conn, month)
        ctx = {
            "active": "goals",
            "brand": "nancy",
            "selected_month": selected,
            "months": months,
            "review_rows": repo_goals.close_review_rows(conn, selected),
        }
    return templates.TemplateResponse(request, "goals_close.html", ctx)


@router.post("/goals/close")
def run_close(month: str = Form(...)):
    selected = _parse_month(month)
    if selected > _today_month():
        raise HTTPException(400, "month cannot be in the future")
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        repo_goals.close_month(conn, selected)
    return RedirectResponse(f"/goals/close?{urlencode({'month': selected})}", status_code=303)


@router.get("/goals/{goal_id}/edit", response_class=HTMLResponse)
def edit_goal_page(request: Request, goal_id: int, month: str | None = None):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = _goal_context(conn, selected_month=month, edit_goal_id=goal_id)
    return templates.TemplateResponse(request, "goals.html", ctx)


@router.post("/goals/{goal_id}")
def update_goal(
    goal_id: int,
    name: str = Form(...),
    kind: str = Form(...),
    target: str = Form(...),
    start_month: str = Form(...),
    target_month: str = Form(""),
    monthly_contribution: str = Form("0"),
    linked_transaction_id: str = Form(""),
    linked_category_id: str = Form(""),
    priority: str = Form("100"),
    brand_owner: str = Form("nancy"),
    color: str = Form("#FF9F43"),
    notes: str = Form(""),
    auto_fund: str | None = Form(None),
):
    kind = _validate_kind(kind)
    brand_owner = _validate_brand_owner(brand_owner)
    target_cents = _positive_cents(target, "target")
    monthly_cents = _nonnegative_cents(monthly_contribution, "monthly contribution")
    start = _parse_month(start_month)
    target_m = _parse_month(target_month, required=False)
    if target_m and target_m < start:
        raise HTTPException(400, "target month must be on or after start month")
    linked_txn = _optional_int(linked_transaction_id)
    linked_category = _optional_int(linked_category_id)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _validate_linked_refs(
            conn,
            linked_transaction_id=linked_txn,
            linked_category_id=linked_category,
        )
        cur = conn.execute(
            """
            UPDATE goals
            SET name=?,
                kind=?,
                target_cents=?,
                start_month=?,
                target_month=?,
                monthly_contribution_cents=?,
                linked_transaction_id=?,
                linked_category_id=?,
                auto_fund=?,
                priority=?,
                brand_owner=?,
                color=?,
                notes=?
            WHERE id=?
            """,
            (
                name.strip(),
                kind,
                target_cents,
                start,
                target_m,
                monthly_cents,
                linked_txn,
                linked_category,
                1 if auto_fund == "1" else 0,
                _priority(priority),
                brand_owner,
                color.strip() or "#FF9F43",
                notes,
                goal_id,
            ),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "goal not found")
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/{goal_id}/status")
def set_goal_status(goal_id: int, status: str = Form(...)):
    if status not in GOAL_STATUSES:
        raise HTTPException(400, "invalid status")
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        cur = conn.execute("UPDATE goals SET status=? WHERE id=?", (status, goal_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "goal not found")
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/{goal_id}/catch-up")
def catch_up_goal(request: Request, goal_id: int, month: str = Form("")):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        selected, _ = _selected_month(conn, month)
        try:
            repo_goals.catch_up(conn, goal_id, selected)
        except LookupError:
            raise HTTPException(404, "goal not found") from None
    return _redirect_back(request, f"/goals?{urlencode({'month': selected})}")


@router.post("/goals/{goal_id}/extend")
def extend_goal(request: Request, goal_id: int, month: str = Form("")):
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        selected, _ = _selected_month(conn, month)
        try:
            repo_goals.extend(conn, goal_id, selected)
        except LookupError:
            raise HTTPException(404, "goal not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return _redirect_back(request, f"/goals?{urlencode({'month': selected})}")
