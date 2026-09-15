from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ..reporting.exports import UnsupportedPdfGlyphError
from ..reporting.period_statements import (
    FrozenPeriodStatementIntegrityError,
    FrozenPeriodStatementUnavailable,
    PeriodStatementError,
    UnsupportedReportCurrency,
)
from . import service
from .auth import require_api_token
from .schemas import TransactionIn

router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_api_token)])


@router.post("/ingest")
async def ingest(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    result = service.ingest_bytes(
        raw=raw,
        filename=file.filename or "upload",
        channel="api",
        declared_mime=file.content_type,
    )
    status_url = str(request.url_for("api_document_status", doc_id=result["doc_id"]))
    return {**result, "status_url": status_url}


@router.post("/transactions")
def record_transaction(body: TransactionIn):
    """Record a structured transaction.

    Category is purpose; ``flow_kind`` is movement meaning. Use ``unknown`` to
    defer ambiguity to review. Omitted or unmatched category names fall back to
    Uncategorized and the response reports that fallback.

    Retries: pass a stable external_id to make the call idempotent; without one
    every call creates a new transaction.
    """
    try:
        return service.record_transaction(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/documents/{doc_id}", name="api_document_status")
def api_document_status(doc_id: int):
    result = service.document_status(doc_id=doc_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no document {doc_id}")
    return result


@router.get("/reports/{report}")
def run_report(report: str, month: str | None = None, limit: int = 50):
    try:
        return service.run_report(report=report, month=month, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/period-statements/{month}/export")
def export_period_statement(month: str, format: str):
    try:
        content, media_type, filename = service.export_period_statement(
            month=month,
            export_format=format,
        )
    except UnsupportedPdfGlyphError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        FrozenPeriodStatementIntegrityError,
        FrozenPeriodStatementUnavailable,
        UnsupportedReportCurrency,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (PeriodStatementError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/period-statements/{month}")
def get_period_statement(month: str):
    try:
        return service.get_period_statement(month=month)
    except (
        FrozenPeriodStatementIntegrityError,
        FrozenPeriodStatementUnavailable,
        UnsupportedReportCurrency,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PeriodStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reconcile")
def reconcile_status(doc_id: int | None = None):
    return service.reconcile_status(doc_id=doc_id)
