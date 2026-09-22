"""Evidence-backed household and account period-statement web surfaces.

All amounts come from ``build_period_statement``.  This route only selects a
precomputed household or account view and delegates exports to the shared
renderers; it never re-derives accounting totals.
"""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...config import get_settings
from ...db import engine, repo_budgets, repo_statement_expectations
from ...reporting.exports import (
    UnsupportedPdfGlyphError,
    render_period_statement_csv,
    render_period_statement_pdf,
)
from ...reporting.models import (
    AccountPeriodStatement,
    EvidenceAmount,
    StatementEvidenceRow,
)
from ...reporting.period_statements import (
    FrozenPeriodStatementIntegrityError,
    FrozenPeriodStatementUnavailable,
    PeriodStatementError,
    UnsupportedReportCurrency,
    build_period_statement,
)
from ..templating import templates

router = APIRouter()

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_LOCKED_CLOSE_STATES = {"clean_closed", "closed_with_exceptions"}


def _month_choices(db_path: str, selected: str | None) -> list[str]:
    with engine.read_conn(db_path, read_only=True) as conn:
        months = set(repo_budgets.reconciliation_months(conn))
        months.update(repo_statement_expectations.reconciliation_months(conn))
    if selected:
        months.add(selected)
    if not months:
        months.add(date.today().strftime("%Y-%m"))
    return sorted(months, reverse=True)


def _selected_month(db_path: str, raw: str | None) -> tuple[str, list[str]]:
    value = (raw or "").strip()
    if value and not _MONTH.fullmatch(value):
        raise HTTPException(400, "month must be YYYY-MM")
    months = _month_choices(db_path, value or None)
    return value or months[0], months


def _metric(key: str, label: str, amount: EvidenceAmount) -> dict:
    safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-") or "line"
    return {
        "key": safe_key,
        "line_key": amount.line_key,
        "label": label,
        "amount": amount,
    }


def _household_metrics(statement) -> list[dict]:
    household = statement.household
    return [
        _metric("income", "Income", household.income),
        _metric("gross-money-out", "Gross money out", household.gross_money_out),
        _metric("refunds", "Refunds", household.refunds),
        _metric("net-money-out", "Net money out", household.net_money_out),
        _metric(
            "external-cash-movement",
            "External cash movement",
            household.external_cash_movement,
        ),
        _metric(
            "transfer-neutrality-control",
            "Transfer neutrality control",
            household.transfer_neutrality_control,
        ),
        _metric(
            "adjustment-movement",
            "Adjustment movement",
            household.adjustment_movement,
        ),
        _metric(
            "unclassified-movement",
            "Unclassified movement",
            household.unclassified_movement,
        ),
        _metric(
            "opening-liquid-position",
            "Opening liquid position",
            household.opening_liquid_position,
        ),
        _metric(
            "closing-liquid-position",
            "Closing liquid position",
            household.closing_liquid_position,
        ),
    ]


def _account_metrics(account: AccountPeriodStatement) -> list[dict]:
    metrics = [_metric("opening-balance", "Opening balance", account.opening_balance)]
    metrics.extend(
        _metric(
            f"typed-inflow-{index}",
            f"{bucket.flow_kind.replace('_', ' ')} in",
            bucket.amount,
        )
        for index, bucket in enumerate(account.typed_inflows, start=1)
    )
    metrics.extend(
        _metric(
            f"typed-outflow-{index}",
            f"{bucket.flow_kind.replace('_', ' ')} out",
            bucket.amount,
        )
        for index, bucket in enumerate(account.typed_outflows, start=1)
    )
    metrics.extend(
        [
            _metric("transfers-in", "Transfers in", account.transfers_in),
            _metric("transfers-out", "Transfers out", account.transfers_out),
            _metric("refunds", "Refunds", account.refunds),
            _metric("debt-movement", "Debt movement", account.debt_movement),
            _metric(
                "ledger-closing-balance",
                "Ledger closing balance",
                account.ledger_closing_balance,
            ),
        ]
    )
    if account.asserted_statement_closing_balance is not None:
        metrics.append(
            _metric(
                "asserted-closing-balance",
                "Asserted statement closing balance",
                account.asserted_statement_closing_balance,
            )
        )
    if account.assertion_delta is not None:
        metrics.append(
            _metric("assertion-delta", "Assertion delta", account.assertion_delta)
        )
    return metrics


def _with_evidence_rows(
    metrics: list[dict],
    rows: tuple[StatementEvidenceRow, ...],
) -> list[dict]:
    """Attach the exact canonical contribution rows without deriving totals."""
    rows_by_id = {row.row_id: row for row in rows}
    enriched = []
    for metric in metrics:
        row_ids = metric["amount"].evidence.row_ids
        enriched.append(
            {
                **metric,
                "rows": tuple(
                    rows_by_id[row_id] for row_id in row_ids if row_id in rows_by_id
                ),
            }
        )
    return enriched


def _blocked_response(
    request: Request,
    *,
    selected_month: str,
    months: list[str],
    home_currency: str,
    report_error: str,
    error_kind: str,
    status_code: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "period_statement.html",
        {
            "statement": None,
            "report_error": report_error,
            "error_kind": error_kind,
            "selected_month": selected_month,
            "months": months,
            "home_currency": home_currency,
            "active": "statements",
            "brand": "finn",
        },
        status_code=status_code,
    )


def _render_statement(
    request: Request,
    *,
    month: str | None,
    account_id: str | None,
) -> HTMLResponse:
    settings = get_settings()
    selected_month, months = _selected_month(str(settings.db_path), month)
    try:
        statement = build_period_statement(
            settings.db_path,
            month=selected_month,
            home_currency=settings.home_currency,
        )
    except FrozenPeriodStatementUnavailable as exc:
        return _blocked_response(
            request,
            selected_month=selected_month,
            months=months,
            home_currency=settings.home_currency,
            report_error=str(exc),
            error_kind="legacy_snapshot_unavailable",
            status_code=409,
        )
    except FrozenPeriodStatementIntegrityError as exc:
        return _blocked_response(
            request,
            selected_month=selected_month,
            months=months,
            home_currency=settings.home_currency,
            report_error=str(exc),
            error_kind="snapshot_integrity_error",
            status_code=409,
        )
    except UnsupportedReportCurrency as exc:
        return _blocked_response(
            request,
            selected_month=selected_month,
            months=months,
            home_currency=settings.home_currency,
            report_error=str(exc),
            error_kind="unsupported_currency",
            status_code=409,
        )
    except PeriodStatementError as exc:
        return _blocked_response(
            request,
            selected_month=selected_month,
            months=months,
            home_currency=settings.home_currency,
            report_error=str(exc),
            error_kind="invalid_report",
            status_code=400,
        )

    if (
        statement.close.state in _LOCKED_CLOSE_STATES
        and (
            statement.close.snapshot_id is None
            or not statement.report_digest.strip()
        )
    ):
        return _blocked_response(
            request,
            selected_month=selected_month,
            months=months,
            home_currency=statement.home_currency,
            report_error=(
                "This legacy close has no frozen canonical period statement and "
                "digest. Historical totals are unavailable; reopen, review, and "
                "close the period again to create one."
            ),
            error_kind="legacy_snapshot_unavailable",
            status_code=409,
        )

    selected_account = None
    if account_id not in (None, ""):
        try:
            requested_account_id = int(account_id)
        except ValueError as exc:
            raise HTTPException(400, "account_id must be an integer") from exc
        selected_account = next(
            (
                account
                for account in statement.accounts
                if account.account_id == requested_account_id
            ),
            None,
        )
        if selected_account is None:
            raise HTTPException(404, "account is not present in this period statement")

    scope_rows = (
        tuple(
            row
            for row in statement.rows
            if row.account_id == selected_account.account_id
        )
        if selected_account is not None
        else statement.rows
    )
    base_metrics = (
        _account_metrics(selected_account)
        if selected_account is not None
        else _household_metrics(statement)
    )
    base_expense_metrics = (
        []
        if selected_account is not None
        else [
            _metric(
                "expense-confirmed",
                "Deterministically confirmed expenses",
                statement.expense_resolution.confirmed,
            ),
            _metric(
                "expense-human-approved",
                "Human-approved expenses",
                statement.expense_resolution.human_approved,
            ),
            _metric(
                "expense-unresolved",
                "Unresolved expenses",
                statement.expense_resolution.unresolved,
            ),
            _metric(
                "expense-resolved",
                "Resolved expenses",
                statement.expense_resolution.resolved,
            ),
        ]
    )
    base_bucket_metrics = (
        []
        if selected_account is not None
        else [
            _metric(
                f"expense-bucket-{bucket.bucket_id}",
                " · ".join(
                    part
                    for part in (
                        bucket.category_name,
                        bucket.canonical_merchant,
                        bucket.resolution_disposition.replace("_", " "),
                    )
                    if part
                ),
                bucket.amount,
            )
            for bucket in statement.expense_resolution.buckets
        ]
    )
    metrics = _with_evidence_rows(base_metrics, scope_rows)
    expense_metrics = _with_evidence_rows(base_expense_metrics, scope_rows)
    bucket_metrics = _with_evidence_rows(base_bucket_metrics, scope_rows)
    evidence_metrics = [*metrics, *expense_metrics, *bucket_metrics]
    report_origin = (
        "frozen_snapshot" if statement.close.snapshot_id is not None else "live"
    )
    return templates.TemplateResponse(
        request,
        "period_statement.html",
        {
            "statement": statement,
            "report_error": "",
            "error_kind": "",
            "selected_month": selected_month,
            "months": months,
            "selected_account": selected_account,
            "selected_account_id": (
                selected_account.account_id if selected_account is not None else None
            ),
            "scope_rows": scope_rows,
            "metrics": metrics,
            "expense_metrics": expense_metrics,
            "bucket_metrics": bucket_metrics,
            "evidence_metrics": evidence_metrics,
            "report_origin": report_origin,
            "home_currency": statement.home_currency,
            "active": "statements",
            "brand": "finn",
        },
    )


@router.get("/statements", response_class=HTMLResponse)
def period_statement_page(
    request: Request,
    month: str | None = None,
    account_id: str | None = None,
):
    return _render_statement(request, month=month, account_id=account_id)


@router.get("/statements/export")
def export_period_statement(
    month: str,
    format: str,
):
    selected_format = format.strip().lower()
    if selected_format not in {"csv", "pdf"}:
        raise HTTPException(400, "format must be csv or pdf")
    if not _MONTH.fullmatch(month.strip()):
        raise HTTPException(400, "month must be YYYY-MM")

    settings = get_settings()
    try:
        statement = build_period_statement(
            settings.db_path,
            month=month.strip(),
            home_currency=settings.home_currency,
        )
    except FrozenPeriodStatementUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except FrozenPeriodStatementIntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except UnsupportedReportCurrency as exc:
        raise HTTPException(409, str(exc)) from exc
    except PeriodStatementError as exc:
        raise HTTPException(400, str(exc)) from exc

    if selected_format == "csv":
        content = render_period_statement_csv(statement)
        media_type = "text/csv; charset=utf-8"
    else:
        try:
            content = render_period_statement_pdf(statement)
        except UnsupportedPdfGlyphError as exc:
            raise HTTPException(422, str(exc)) from exc
        media_type = "application/pdf"
    filename = f"finn-nancy-period-statement-{statement.month}.{selected_format}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
