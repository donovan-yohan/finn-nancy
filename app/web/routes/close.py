"""Truthful month-close workspace backed by the immutable FN-147 policy.

The page deliberately keeps three concepts separate:

* live readiness signals and typed exceptions;
* the current lifecycle state (open, clean closed, exception closed, reopened);
* immutable snapshot history, where a reopened snapshot remains historical.

Acknowledging an exception only records that a person reviewed it.  It never
resolves the item or upgrades an exception close into a clean close.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...close import checklist, period_exceptions
from ...config import get_settings
from ...db import (
    engine,
    repo_budgets,
    repo_close,
    repo_close_inbox,
    repo_period_policy,
    repo_statement_expectations,
)
from ...reconcile import adjust
from ...reporting.period_statements import (
    build_period_statement,
    period_statement_snapshot_payload,
)
from ..templating import templates

router = APIRouter()

_CLOSED_STATES = frozenset({"clean_closed", "closed_with_exceptions"})


def _today_month() -> str:
    return date.today().strftime("%Y-%m")


def _valid_month(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if len(raw) == 7 and raw[4] == "-" and raw[:4].isdigit() and raw[5:].isdigit():
        if 1 <= int(raw[5:]) <= 12:  # reject 00 / 13+ before repo_goals raises ValueError
            return raw
    return None


def _month_choices(conn: sqlite3.Connection, selected: str | None) -> list[str]:
    current = _today_month()
    choices = {m for m in repo_budgets.reconciliation_months(conn) if m <= current}
    choices.update(
        month
        for month in repo_statement_expectations.reconciliation_months(conn)
        if month <= current
    )
    if selected and selected <= current:
        choices.add(selected)
    if not choices:
        choices.add(current)
    return sorted(choices, reverse=True)


def _selected_month(conn: sqlite3.Connection, raw: str | None) -> tuple[str, list[str]]:
    month = _valid_month(raw)
    if month and month > _today_month():
        month = None
    months = _month_choices(conn, month)
    return (month or months[0], months)


def _json_mapping(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_state(period: sqlite3.Row | None) -> str:
    """Conservatively expose legacy rows created after migration in old tests/tools."""
    if period is None:
        return "open"
    status = str(period["status"])
    if status == "closed":
        return "closed_with_exceptions"
    if status == "reopened":
        return "reopened"
    return "open"


def _state_copy(
    *,
    month: str,
    state: str,
    snapshot_id: int | None,
    exception_count: int,
    acknowledgement_count: int,
) -> dict[str, str]:
    if state == "clean_closed":
        return {
            "title": f"{month} closed cleanly",
            "eyebrow": "clean close · 0 unresolved",
            "detail": (
                f"Snapshot #{snapshot_id} is immutable and current. "
                "The month is locked against edits."
                if snapshot_id is not None
                else "This legacy close has no verified FN-147 snapshot."
            ),
        }
    if state == "closed_with_exceptions":
        return {
            "title": f"{month} closed with exceptions",
            "eyebrow": (
                f"exception close · {acknowledgement_count} of "
                f"{exception_count} acknowledged"
            ),
            "detail": (
                f"Snapshot #{snapshot_id} is immutable and current. "
                "Acknowledged items remain unresolved."
                if snapshot_id is not None
                else "This legacy close remains unverified and cannot be called clean."
            ),
        }
    if state == "reopened":
        return {
            "title": f"Finish reopened {month}",
            "eyebrow": f"reopened · {exception_count} unresolved",
            "detail": (
                "The prior snapshot is historical. Resolve live issues or "
                "create a new, independently numbered close snapshot."
            ),
        }
    close_kind = "clean close" if exception_count == 0 else "exception close"
    return {
        "title": f"Close {month}",
        "eyebrow": f"{close_kind} · {exception_count} unresolved",
        "detail": (
            "Review the live evidence below. Closing freezes exactly what is "
            "known now; it never hides an unresolved item."
        ),
    }


def _context(conn: sqlite3.Connection, raw_month: str | None) -> dict:
    month, months = _selected_month(conn, raw_month)
    check = checklist.build_checklist(conn, month)
    # GET remains read-only.  A missing state row means open; it must not be
    # materialized merely because somebody viewed the close workspace.
    state_row = repo_period_policy.get_state(conn, month)
    period = repo_close.get_period(conn, month)  # compatibility projection only
    state = (
        str(state_row["state"])
        if state_row is not None
        else _legacy_state(period)
    )
    locked = state in _CLOSED_STATES
    statement_matrix = repo_statement_expectations.period_matrix(conn, month)
    statement_setup_required = any(
        not bool(row["materialized"]) for row in statement_matrix
    )

    history = [
        {**dict(row), "snapshot": _json_mapping(row["snapshot_json"])}
        for row in repo_period_policy.list_snapshot_history(conn, month)
    ]
    current_snapshot = next(
        (row for row in history if bool(row["is_current"])),
        None,
    )
    latest_snapshot = history[0] if history else None
    inbox = repo_close_inbox.build_inbox(conn, month)

    if locked and state_row is not None:
        exceptions = repo_period_policy.list_current_exceptions(conn, month)
    elif locked:
        # A post-migration legacy writer cannot prove clean criteria.  Surface
        # the evidence gap rather than silently presenting an empty list.
        exceptions = [
            {
                "id": None,
                "exception_type": "evidence_gap",
                "subject_kind": "legacy_close",
                "subject_id": month,
                "reason": "legacy close lacks a verified FN-147 evidence snapshot",
                "resolution_href": f"/close?month={month}",
                "amount_cents": 0,
                "affected_ids": {"period_month": month},
                "evidence": {},
                "is_acknowledged": False,
            }
        ]
    else:
        exceptions = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            month,
            period_exceptions.collect_period_exceptions(conn, month),
        )
    inbox_by_ident = {item.ident: item for item in inbox}
    for item in exceptions:
        if (
            str(item.get("exception_type")) == "unclassified_positive_flow"
            and str(item.get("subject_kind")) == "statement_line"
        ):
            inbox_item = inbox_by_ident.get(
                f"positive-flow-{item.get('subject_id')}"
            )
            if inbox_item is not None:
                item["display_title"] = inbox_item.title

    exception_count = len(exceptions)
    acknowledgement_count = sum(
        bool(item.get("is_acknowledged")) for item in exceptions
    )
    unacknowledged_exception_count = (
        exception_count - acknowledgement_count
    )
    snapshot_preview = (
        current_snapshot["snapshot"]
        if current_snapshot is not None
        else period_exceptions.build_close_snapshot(conn, month, exceptions)
    )
    snapshot_id = (
        int(current_snapshot["snapshot_id"])
        if current_snapshot is not None
        else None
    )
    state_copy = _state_copy(
        month=month,
        state=state,
        snapshot_id=snapshot_id,
        exception_count=exception_count,
        acknowledgement_count=acknowledgement_count,
    )

    statement_blockers = [row for row in statement_matrix if row["blocking"]]
    return {
        "active": "more",
        "brand": "nancy",
        "selected_month": month,
        "months": months,
        "checklist": check,
        "period": period,
        "close_state": state,
        "state_copy": state_copy,
        "locked": locked,
        "summary": snapshot_preview,
        "current_snapshot": current_snapshot,
        "current_snapshot_id": snapshot_id,
        "snapshot_reference_id": (
            int(latest_snapshot["snapshot_id"])
            if latest_snapshot is not None
            else None
        ),
        "snapshot_history": history,
        "snapshot_current": current_snapshot is not None,
        "exceptions": exceptions,
        "exception_count": exception_count,
        "acknowledgement_count": acknowledgement_count,
        "unacknowledged_exception_count": unacknowledged_exception_count,
        "exception_close_ready": unacknowledged_exception_count == 0,
        "statement_setup_required": statement_setup_required,
        "preclose_review_ready": not statement_setup_required,
        "close_action_label": (
            "Clean close · 0 unresolved"
            if exception_count == 0
            else f"Exception close · {acknowledgement_count} of "
            f"{exception_count} acknowledged"
        ),
        "operation_nonce": uuid4().hex,
        "audit": repo_close.list_audit(conn, month),
        "inbox": inbox,
        "inbox_count": len(inbox),
        "locked_assertions": (
            [item for item in inbox if item.source == "assertion"]
            if locked
            else []
        ),
        "statement_matrix": statement_matrix,
        "statement_blockers": statement_blockers,
        "statement_blocker_count": len(statement_blockers),
    }


def _required_form_text(
    value: str,
    *,
    field: str,
    maximum: int,
) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(400, f"{field} is required")
    if len(text) > maximum:
        raise HTTPException(400, f"{field} exceeds {maximum} characters")
    return text


def _web_operation_key(raw: str, *, action: str, month: str) -> str:
    value = raw.strip() or f"close:web:{action}:{month}:{uuid4().hex}"
    if len(value) > 180:
        raise HTTPException(400, "operation key exceeds 180 characters")
    return value


@router.get("/close", response_class=HTMLResponse)
def close_page(request: Request, month: str | None = None, notice: str | None = None):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        ctx = _context(conn, month)
    # Inline notice from a POST-redirect-GET (e.g. a refused/failed reconciliation
    # adjustment); rendered as a warn banner, autoescaped by Jinja.
    ctx["notice"] = notice
    return templates.TemplateResponse(request, "close.html", ctx)


@router.post("/close/signoff")
def sign_off(
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    confirm_close: str | None = Form(None),
    operation_key: str = Form(""),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    if selected > _today_month():
        raise HTTPException(400, "month cannot be in the future")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    if confirm_close != "1":
        raise HTTPException(400, "close confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action="signoff",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if repo_period_policy.current_state(conn, selected) in _CLOSED_STATES:
                raise HTTPException(400, "month is already closed")
            if any(
                not bool(row["materialized"])
                for row in repo_statement_expectations.period_matrix(
                    conn,
                    selected,
                )
            ):
                raise HTTPException(
                    400,
                    "prepare the audited statement matrix before close review",
                )
            legacy_period = repo_close.get_period(conn, selected)
            legacy_from_state = (
                "open" if legacy_period is None else str(legacy_period["status"])
            )
            repo_statement_expectations.prepare_period(
                conn,
                month=selected,
                actor=actor,
                reason=reason,
            )
            exceptions = period_exceptions.collect_period_exceptions(conn, selected)
            exceptions = repo_period_policy.apply_preclose_acknowledgements(
                conn,
                selected,
                exceptions,
            )
            unacknowledged_count = sum(
                not bool(item.get("is_acknowledged")) for item in exceptions
            )
            if unacknowledged_count:
                raise HTTPException(
                    400,
                    "acknowledge every active exception before exception close",
                )
            snapshot = period_exceptions.build_close_snapshot(
                conn,
                selected,
                exceptions,
            )
            report = build_period_statement(
                conn,
                month=selected,
                home_currency=settings.home_currency,
            )
            close_state = (
                "clean_closed"
                if not exceptions
                else "closed_with_exceptions"
            )
            snapshot.update(
                period_statement_snapshot_payload(
                    report,
                    close_state=close_state,
                    exception_types=(
                        str(item["exception_type"]) for item in exceptions
                    ),
                )
            )
            close_snapshot = repo_period_policy.close_period(
                conn,
                selected,
                snapshot=snapshot,
                exceptions=exceptions,
                actor=actor,
                reason=reason,
                operation_key=operation_key,
            )

            # Keep the old append-only audit readable while the immutable
            # FN-147 event/snapshot tables remain the lifecycle authority.
            compatibility_period = repo_close.get_period(conn, selected)
            repo_close.record_audit(
                conn,
                month=selected,
                entity="period",
                entity_id=(
                    None
                    if compatibility_period is None
                    else int(compatibility_period["id"])
                ),
                field="status",
                old_value=legacy_from_state,
                new_value="closed",
                reason=reason,
            )
    except HTTPException:
        raise
    except repo_period_policy.PeriodTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return RedirectResponse(f"/close?{urlencode({'month': selected})}", status_code=303)


@router.post("/close/prepare")
def prepare_statement_matrix(
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    confirm_prepare: str | None = Form(None),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    if confirm_prepare != "1":
        raise HTTPException(400, "statement matrix confirmation is required")
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_statement_expectations.prepare_period(
                conn,
                month=selected,
                actor=actor,
                reason=reason,
            )
    except (ValueError, repo_close.MonthLockedError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(
        f"/close?{urlencode({'month': selected})}", status_code=303
    )


@router.post("/close/assertion/adjust")
def adjust_locked_balance_assertion(
    month: str = Form(...),
    account_id: int = Form(...),
    asof_date: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    confirm_adjustment: str | None = Form(None),
    operation_key: str = Form(""),
):
    """Reopen through an audited override before booking one balance adjustment."""
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    try:
        assertion_date = date.fromisoformat(asof_date.strip())
    except ValueError as exc:
        raise HTTPException(400, "invalid assertion date") from exc
    if assertion_date.strftime("%Y-%m") != selected:
        raise HTTPException(
            400,
            "assertion date is outside the selected month",
        )
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    if confirm_adjustment != "1":
        raise HTTPException(400, "adjustment confirmation is required")
    operation_key = _required_form_text(
        operation_key,
        field="operation key",
        maximum=180,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if not repo_close.is_month_locked(conn, selected):
                raise HTTPException(
                    409,
                    "month is no longer closed; review live evidence before adjusting",
                )
            result = adjust.create_adjustment(
                conn,
                account_id=int(account_id),
                asof_date=assertion_date.isoformat(),
                override=True,
                actor=actor,
                override_reason=reason,
                operation_key=operation_key,
            )
    except HTTPException:
        raise
    except (
        repo_period_policy.PeriodOperationConflict,
        repo_period_policy.PeriodTransitionError,
    ) as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except repo_close.MonthLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if result.status == "no_assertion":
        raise HTTPException(404, "no balance assertion for that account and date")
    if result.status == "tie":
        notice = "Balance already matches; no adjustment or override was needed."
    elif result.status == "exists":
        notice = (
            "The period was reopened with an audited override, but the "
            "reconciliation adjustment could not be booked. Review the live evidence."
        )
    else:
        notice = (
            "Period reopened with an audited override and reconciliation "
            "adjustment booked. The prior snapshot remains immutable and historical."
        )
    return RedirectResponse(
        f"/close?{urlencode({'month': selected, 'notice': notice})}",
        status_code=303,
    )


@router.post("/close/statement/{expectation_id}/waive")
def waive_statement(
    expectation_id: int,
    month: str = Form(...),
    actor: str = Form(...),
    reason: str = Form(...),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            row = repo_statement_expectations.expectation(conn, expectation_id)
            if row["period_month"] != selected:
                raise ValueError("statement expectation is outside the selected month")
            repo_statement_expectations.waive(
                conn,
                expectation_id,
                actor=actor,
                reason=reason,
            )
    except repo_close.MonthLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(
        f"/close?{urlencode({'month': selected})}", status_code=303
    )


@router.post("/close/statement/{expectation_id}/restore")
def restore_statement_waiver(
    expectation_id: int,
    month: str = Form(...),
    actor: str = Form(...),
    reason: str = Form(...),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            row = repo_statement_expectations.expectation(conn, expectation_id)
            if row["period_month"] != selected:
                raise ValueError("statement expectation is outside the selected month")
            repo_statement_expectations.restore_waiver(
                conn,
                expectation_id,
                actor=actor,
                reason=reason,
            )
    except repo_close.MonthLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(
        f"/close?{urlencode({'month': selected})}", status_code=303
    )


@router.post("/close/reopen")
def reopen(
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    evidence_note: str = Form(""),
    confirm_reopen: str | None = Form(None),
    operation_key: str = Form(""),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    if confirm_reopen != "1":
        raise HTTPException(400, "reopen confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action="reopen",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            state_row = repo_period_policy.get_state(conn, selected)
            period = repo_close.get_period(conn, selected)
            if state_row is None:
                # Compatibility for a legacy close created by an old caller
                # after migration.  It has no immutable snapshot to invalidate.
                if period is None or str(period["status"]) != "closed":
                    raise HTTPException(400, "month is not closed")
                repo_close.reopen(conn, selected, reason=reason)
            else:
                if str(state_row["state"]) not in _CLOSED_STATES:
                    raise HTTPException(400, "month is not closed")
                repo_period_policy.reopen_period(
                    conn,
                    selected,
                    actor=actor,
                    reason=reason,
                    operation_key=operation_key,
                    affected_ids={"period_month": selected},
                    evidence=(
                        {"operator_note": evidence_note.strip()}
                        if evidence_note.strip()
                        else None
                    ),
                )
                compatibility_period = repo_close.get_period(conn, selected)
                repo_close.record_audit(
                    conn,
                    month=selected,
                    entity="period",
                    entity_id=(
                        None
                        if compatibility_period is None
                        else int(compatibility_period["id"])
                    ),
                    field="status",
                    old_value=str(state_row["state"]),
                    new_value="reopened",
                    reason=reason,
                )
    except HTTPException:
        raise
    except repo_period_policy.PeriodTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    return RedirectResponse(f"/close?{urlencode({'month': selected})}", status_code=303)


@router.post("/close/exception/{exception_id}/acknowledge")
def acknowledge_close_exception(
    exception_id: int,
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    evidence_note: str = Form(""),
    confirm_acknowledgement: str | None = Form(None),
    operation_key: str = Form(""),
):
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    evidence_note = _required_form_text(
        evidence_note,
        field="evidence note",
        maximum=500,
    )
    if confirm_acknowledgement != "1":
        raise HTTPException(400, "acknowledgement confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action=f"ack:{int(exception_id)}",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if repo_period_policy.current_state(conn, selected) != (
                "closed_with_exceptions"
            ):
                raise HTTPException(
                    409,
                    "exceptions can only be acknowledged on a locked exception close",
                )
            current = repo_period_policy.list_current_exceptions(conn, selected)
            item = next(
                (row for row in current if int(row["id"]) == int(exception_id)),
                None,
            )
            if item is None:
                raise HTTPException(
                    404,
                    "exception is not current for the selected month",
                )
            if bool(item.get("is_acknowledged")):
                raise HTTPException(409, "exception is already acknowledged")
            repo_period_policy.acknowledge_exception(
                conn,
                int(exception_id),
                actor=actor,
                reason=reason,
                operation_key=operation_key,
                evidence={"operator_note": evidence_note},
            )
    except HTTPException:
        raise
    except repo_period_policy.PeriodOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    query = urlencode(
        {
            "month": selected,
            "notice": (
                f"Exception #{int(exception_id)} acknowledged; "
                "it remains unresolved."
            ),
        }
    )
    return RedirectResponse(f"/close?{query}", status_code=303)


@router.post("/close/preclose-exception/{exception_token}/acknowledge")
def acknowledge_preclose_exception(
    exception_token: str,
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    evidence_note: str = Form(""),
    confirm_acknowledgement: str | None = Form(None),
    operation_key: str = Form(""),
):
    """Record durable review before an exception close is allowed."""
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    token = _required_form_text(
        exception_token,
        field="exception token",
        maximum=240,
    )
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    evidence_note = _required_form_text(
        evidence_note,
        field="evidence note",
        maximum=500,
    )
    if confirm_acknowledgement != "1":
        raise HTTPException(400, "acknowledgement confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action=f"preclose-ack:{token}",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if repo_period_policy.current_state(conn, selected) not in {
                "open",
                "reopened",
            }:
                raise HTTPException(
                    409,
                    "pre-close review is only available while the month is open",
                )
            current = repo_period_policy.apply_preclose_acknowledgements(
                conn,
                selected,
                period_exceptions.collect_period_exceptions(conn, selected),
            )
            item = next(
                (
                    row
                    for row in current
                    if str(row.get("exception_token")) == token
                ),
                None,
            )
            if item is None:
                raise HTTPException(
                    404,
                    "exception is no longer active for the selected month",
                )
            if bool(item.get("is_acknowledged")):
                raise HTTPException(409, "exception is already acknowledged")
            repo_period_policy.acknowledge_preclose_exception(
                conn,
                selected,
                item,
                actor=actor,
                reason=reason,
                operation_key=operation_key,
                evidence={"operator_note": evidence_note},
            )
    except HTTPException:
        raise
    except repo_period_policy.PeriodOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    query = urlencode(
        {
            "month": selected,
            "notice": (
                "Exception acknowledged for close review; "
                "it remains unresolved."
            ),
        }
    )
    return RedirectResponse(f"/close?{query}", status_code=303)


@router.post(
    "/close/preclose-acknowledgement/{acknowledgement_id}/withdraw"
)
def withdraw_preclose_exception_acknowledgement(
    acknowledgement_id: int,
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    evidence_note: str = Form(""),
    confirm_withdrawal: str | None = Form(None),
    operation_key: str = Form(""),
):
    """Append a reversal while keeping the prospective exception unresolved."""
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    evidence_note = _required_form_text(
        evidence_note,
        field="evidence note",
        maximum=500,
    )
    if confirm_withdrawal != "1":
        raise HTTPException(400, "withdrawal confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action=f"preclose-ack-withdrawal:{int(acknowledgement_id)}",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if repo_period_policy.current_state(conn, selected) not in {
                "open",
                "reopened",
            }:
                raise HTTPException(
                    409,
                    "pre-close review is only available while the month is open",
                )
            current = repo_period_policy.apply_preclose_acknowledgements(
                conn,
                selected,
                period_exceptions.collect_period_exceptions(conn, selected),
            )
            item = next(
                (
                    row
                    for row in current
                    if row.get("acknowledgement_id") is not None
                    and int(row["acknowledgement_id"])
                    == int(acknowledgement_id)
                ),
                None,
            )
            if item is None:
                raise HTTPException(
                    409,
                    "acknowledgement is no longer current for an active exception",
                )
            repo_period_policy.withdraw_preclose_acknowledgement(
                conn,
                int(acknowledgement_id),
                actor=actor,
                reason=reason,
                operation_key=operation_key,
                evidence={"operator_note": evidence_note},
            )
    except HTTPException:
        raise
    except (
        repo_period_policy.PeriodOperationConflict,
        repo_period_policy.PeriodTransitionError,
    ) as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    query = urlencode(
        {
            "month": selected,
            "notice": (
                "Pre-close acknowledgement withdrawn; "
                "the exception remains unresolved."
            ),
        }
    )
    return RedirectResponse(f"/close?{query}", status_code=303)


@router.post(
    "/close/exception/{exception_id}/acknowledgement/"
    "{acknowledgement_id}/withdraw"
)
def withdraw_close_exception_acknowledgement(
    exception_id: int,
    acknowledgement_id: int,
    month: str = Form(...),
    actor: str = Form(""),
    reason: str = Form(""),
    evidence_note: str = Form(""),
    confirm_withdrawal: str | None = Form(None),
    operation_key: str = Form(""),
):
    """Append a withdrawal for the current per-exception acknowledgement."""
    selected = _valid_month(month)
    if selected is None:
        raise HTTPException(400, "invalid month")
    actor = _required_form_text(actor, field="actor", maximum=160)
    reason = _required_form_text(reason, field="reason", maximum=500)
    evidence_note = _required_form_text(
        evidence_note,
        field="evidence note",
        maximum=500,
    )
    if confirm_withdrawal != "1":
        raise HTTPException(400, "withdrawal confirmation is required")
    operation_key = _web_operation_key(
        operation_key,
        action=f"ack-withdrawal:{int(acknowledgement_id)}",
        month=selected,
    )

    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            if repo_period_policy.current_state(conn, selected) != (
                "closed_with_exceptions"
            ):
                raise HTTPException(
                    409,
                    "acknowledgements can only be withdrawn on a locked "
                    "exception close",
                )
            current = repo_period_policy.list_current_exceptions(conn, selected)
            item = next(
                (row for row in current if int(row["id"]) == int(exception_id)),
                None,
            )
            if item is None:
                raise HTTPException(
                    404,
                    "exception is not current for the selected month",
                )
            current_acknowledgement_id = item.get("acknowledgement_id")
            if current_acknowledgement_id is None:
                raise HTTPException(409, "exception is not acknowledged")
            if int(current_acknowledgement_id) != int(acknowledgement_id):
                raise HTTPException(
                    409,
                    "acknowledgement is no longer current for this exception",
                )
            repo_period_policy.withdraw_acknowledgement(
                conn,
                int(acknowledgement_id),
                actor=actor,
                reason=reason,
                operation_key=operation_key,
                evidence={"operator_note": evidence_note},
            )
    except HTTPException:
        raise
    except (
        repo_period_policy.PeriodOperationConflict,
        repo_period_policy.PeriodTransitionError,
    ) as exc:
        raise HTTPException(409, str(exc)) from exc
    except repo_period_policy.PeriodPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc

    query = urlencode(
        {
            "month": selected,
            "notice": (
                f"Acknowledgement for exception #{int(exception_id)} withdrawn; "
                "the exception remains unresolved."
            ),
        }
    )
    return RedirectResponse(f"/close?{query}", status_code=303)
