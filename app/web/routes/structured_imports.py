"""Desktop-only preview and confirmation for deterministic statement imports."""
from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import get_settings
from ...db import engine
from ...ingest.structured.service import (
    confirm_import,
    load_preview,
    preview_import,
)
from ...ingest.structured.types import (
    AdapterLimits,
    ImportMetadata,
    MappedCsvV1,
    StructuredImportError,
)
from ..forms import dollars_to_cents
from ..templating import templates


router = APIRouter()

_DEFAULT_FORM_VALUES = {
    "account_id": "",
    "adapter_kind": "mapped_csv",
    "date_column": "date",
    "description_column": "description",
    "amount_column": "amount",
    "debit_column": "",
    "credit_column": "",
    "balance_column": "",
    "currency_column": "",
    "pending_column": "",
    "fitid_column": "",
    "date_format": "%Y-%m-%d",
    "delimiter": ",",
    "default_currency": "CAD",
    "period_start_on": "",
    "period_end_on": "",
    "statement_issued_on": "",
    "opening_balance": "",
    "closing_balance": "",
}


def _redirect(import_id: int, notice: str = "") -> RedirectResponse:
    target = f"/review/import/{int(import_id)}"
    if notice:
        target += "?" + urlencode({"notice": notice})
    return RedirectResponse(target, status_code=303)


def _optional_cents(raw: str) -> int | None:
    return None if not raw.strip() else dollars_to_cents(raw)


def _accounts() -> list:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE is_active=1 ORDER BY name"
        ).fetchall()


def _import_context(import_id: int) -> dict:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        row = conn.execute(
            """SELECT document.original_name AS source_name,
                      account.name AS account_name
               FROM structured_statement_imports imported
               JOIN source_documents document
                 ON document.id=imported.source_document_id
               JOIN accounts account ON account.id=imported.account_id
               WHERE imported.id=?""",
            (int(import_id),),
        ).fetchone()
    if row is None:
        raise ValueError("structured import display context not found")
    return dict(row)


def _form_values(**values: object) -> dict[str, str]:
    result = dict(_DEFAULT_FORM_VALUES)
    for key in result:
        if key in values:
            result[key] = str(values[key] if values[key] is not None else "")
    return result


def _render_import(
    request: Request,
    *,
    imported=None,
    parsed=None,
    diagnostics: list | None = None,
    review_reasons: list | None = None,
    notice: str = "",
    import_context: dict | None = None,
    form_error: str = "",
    form_values: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "structured_import.html",
        {
            "accounts": _accounts(),
            "imported": imported,
            "parsed": parsed,
            "diagnostics": diagnostics or [],
            "review_reasons": review_reasons or [],
            "notice": notice,
            "import_context": import_context,
            "form_error": form_error,
            "form_values": form_values or dict(_DEFAULT_FORM_VALUES),
            "active": "more",
            "brand": "finn",
        },
        status_code=status_code,
    )


@router.get("/review/import", response_class=HTMLResponse)
def import_form(request: Request):
    return _render_import(request)


@router.post("/review/import/preview")
async def import_preview(
    request: Request,
    file: UploadFile = File(...),
    account_id: int = Form(...),
    adapter_kind: str = Form(...),
    date_column: str = Form("date"),
    description_column: str = Form("description"),
    amount_column: str = Form("amount"),
    debit_column: str = Form(""),
    credit_column: str = Form(""),
    balance_column: str = Form(""),
    currency_column: str = Form(""),
    pending_column: str = Form(""),
    fitid_column: str = Form(""),
    date_format: str = Form("%Y-%m-%d"),
    delimiter: str = Form(","),
    default_currency: str = Form("CAD"),
    period_start_on: str = Form(""),
    period_end_on: str = Form(""),
    statement_issued_on: str = Form(""),
    opening_balance: str = Form(""),
    closing_balance: str = Form(""),
):
    submitted_values = _form_values(
        account_id=account_id,
        adapter_kind=adapter_kind,
        date_column=date_column,
        description_column=description_column,
        amount_column=amount_column,
        debit_column=debit_column,
        credit_column=credit_column,
        balance_column=balance_column,
        currency_column=currency_column,
        pending_column=pending_column,
        fitid_column=fitid_column,
        date_format=date_format,
        delimiter=delimiter,
        default_currency=default_currency,
        period_start_on=period_start_on,
        period_end_on=period_end_on,
        statement_issued_on=statement_issued_on,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
    )
    limits = AdapterLimits()
    raw = await file.read(limits.max_bytes + 1)
    try:
        mapping = None
        if adapter_kind == "mapped_csv":
            mapping_delimiter = "\t" if delimiter == "tab" else delimiter
            mapping = MappedCsvV1(
                date_column=date_column,
                description_column=description_column,
                amount_column=amount_column,
                debit_column=debit_column,
                credit_column=credit_column,
                balance_column=balance_column,
                currency_column=currency_column,
                pending_column=pending_column,
                fitid_column=fitid_column,
                date_format=date_format,
                delimiter=mapping_delimiter,
                default_currency=default_currency,
            )
        elif adapter_kind not in {"ofx", "pdf"}:
            raise ValueError("unsupported structured import adapter")
        metadata = ImportMetadata(
            period_start_on=period_start_on,
            period_end_on=period_end_on,
            statement_issued_on=statement_issued_on,
            opening_balance_cents=_optional_cents(opening_balance),
            closing_balance_cents=_optional_cents(closing_balance),
        )
        result = preview_import(
            raw=raw,
            original_name=file.filename or "statement-import",
            declared_mime=file.content_type,
            account_id=account_id,
            adapter_kind=adapter_kind,
            mapping=mapping,
            metadata=metadata,
            limits=limits,
        )
    except (StructuredImportError, ValueError, HTTPException):
        return _render_import(
            request,
            form_error=(
                "We couldn't create a preview. Check the selected format, CSV "
                "mapping, dates, and balances. Reselect the original statement "
                "file, then try again."
            ),
            form_values=submitted_values,
            status_code=400,
        )
    return _redirect(int(result.imported["id"]))


@router.get("/review/import/{import_id}", response_class=HTMLResponse)
def import_detail(request: Request, import_id: int, notice: str = ""):
    try:
        result = load_preview(import_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    imported = result.imported
    try:
        diagnostics = json.loads(str(imported["diagnostics_json"]))
        review_reasons = json.loads(str(imported["review_reasons_json"]))
    except (TypeError, ValueError):
        diagnostics = [
            {
                "code": "diagnostics_invalid",
                "message": "Stored import diagnostics could not be read.",
            }
        ]
        review_reasons = ["diagnostics_invalid"]
    return _render_import(
        request,
        imported=imported,
        parsed=result.parsed,
        diagnostics=diagnostics,
        review_reasons=review_reasons,
        notice=notice,
        import_context=_import_context(import_id),
    )


@router.post("/review/import/{import_id}/confirm")
def import_confirm(
    import_id: int,
    expected_revision: int = Form(...),
):
    try:
        imported = confirm_import(
            import_id,
            expected_revision=expected_revision,
        )
    except StructuredImportError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    notice = {
        "confirmed": "Rows staged. Continue with statement review and reconciliation.",
        "duplicate": "Exact prior rows found. The new original remains as source evidence.",
        "needs_review": "Nothing was staged. Resolve the highlighted review blockers.",
    }.get(str(imported["status"]), "Import updated.")
    return _redirect(import_id, notice)
