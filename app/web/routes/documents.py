"""Document capture routes: mobile upload page, upload handler, status polling."""
from __future__ import annotations

from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...config import get_settings
from ...db import engine, repo_captures, repo_documents, repo_import_runs, repo_jobs
from ...ingest.storage import (
    BlobDurabilityError,
    ClientCaptureConflict,
    InvalidClientCaptureId,
    capture,
)
from ..templating import templates

router = APIRouter()

CAPTURE_SOURCES = {"camera", "file", "share", "shortcut", "web"}
CAPTURE_INTENTS = {"receipt", "statement", "expense", "income", "unspecified"}


@router.get("/processing", response_class=HTMLResponse)
def processing_page(request: Request, page: int = Query(default=1, ge=1)):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        documents = repo_documents.processing_documents(conn, limit=51, offset=(page - 1) * 50)
    return templates.TemplateResponse(
        request,
        "processing.html",
        {
            "active": "more", "brand": "finn", "page": page,
            "documents": documents[:50], "has_more": len(documents) > 50,
            "extraction_failure": repo_documents.extraction_failure,
        },
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(
    request: Request,
    mode: str = "file",
    intent: str = "receipt",
    capture_id: list[str] = Query(default=[]),
    proof_run_id: str = Query(default=""),
    device_cohort_id: str = Query(default=""),
):
    resolved_mode = mode if mode in CAPTURE_SOURCES else "file"
    resolved_intent = intent if intent in CAPTURE_INTENTS else "receipt"
    try:
        proof_run_id = repo_captures.normalize_proof_scope(
            proof_run_id, "proof_run_id"
        )
        device_cohort_id = repo_captures.normalize_proof_scope(
            device_cohort_id, "device_cohort_id"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if bool(proof_run_id) != bool(device_cohort_id):
        raise HTTPException(
            status_code=422,
            detail="proof_run_id and device_cohort_id must be supplied together",
        )
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "active": "",
            "brand": "finn",
            "capture_mode": resolved_mode,
            "capture_intent": resolved_intent,
            "server_capture_ids": capture_id,
            "proof_run_id": proof_run_id,
            "device_cohort_id": device_cohort_id,
        },
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: list[UploadFile] = File(...)):
    settings = get_settings()
    # Every upload becomes a run so the batch has a durable URL to come back to,
    # even when it is a single file.
    with engine.write_tx(settings.db_path) as conn:
        run_id = repo_import_runs.create(conn, channel="web")
    results = []
    for f in files:
        raw = await f.read()
        results.append(
            capture(raw=raw, original_name=f.filename or "upload",
                    channel="web", declared_mime=f.content_type,
                    import_run_id=run_id)
        )
    return templates.TemplateResponse(
        request, "_upload_results.html", {"results": results, "run_id": run_id}
    )


def _bounded(value: str, *, maximum: int) -> str:
    return (value or "").strip()[:maximum]


def _source_metadata(
    *,
    source: str,
    intent: str,
    shared_title: str = "",
    shared_text: str = "",
    shared_url: str = "",
    accepted_at: str = "",
    client_attempts: int | None = None,
    proof_run_id: str = "",
    device_cohort_id: str = "",
) -> dict:
    source = source.strip().lower()
    if source not in CAPTURE_SOURCES:
        raise ValueError("source must be a registered capture source")
    metadata = {
        "source": source,
        "intent": intent if intent in CAPTURE_INTENTS else "unspecified",
        "shared_title": _bounded(shared_title, maximum=200),
        "shared_text": _bounded(shared_text, maximum=2_000),
        "shared_url": _bounded(shared_url, maximum=2_000),
    }
    if client_attempts is not None:
        metadata["client_attempts"] = int(client_attempts)
    if accepted_at.strip():
        metadata["accepted_at"] = _bounded(accepted_at, maximum=64)
    if proof_run_id or device_cohort_id:
        metadata["proof_run_id"] = repo_captures.normalize_proof_scope(
            proof_run_id, "proof_run_id"
        )
        metadata["device_cohort_id"] = repo_captures.normalize_proof_scope(
            device_cohort_id, "device_cohort_id"
        )
        if not metadata["proof_run_id"] or not metadata["device_cohort_id"]:
            raise ValueError(
                "proof_run_id and device_cohort_id must be supplied together"
            )
    return metadata


@router.post("/captures", response_class=JSONResponse)
async def create_capture(
    request: Request,
    file: UploadFile = File(...),
    client_capture_id: str = Form(...),
    source: str = Form("web"),
    intent: str = Form("receipt"),
    shared_title: str = Form(""),
    shared_text: str = Form(""),
    shared_url: str = Form(""),
    accepted_at: str = Form(""),
    client_attempts: int = Form(1),
    proof_run_id: str = Form(""),
    device_cohort_id: str = Form(""),
):
    """Durably accept one device-owned outbox item.

    The stable client id is committed in the same SQLite transaction as the
    source document and ingest job. A retry with the same id and bytes returns
    the original acknowledgement; reusing the id for different bytes is a
    conflict rather than a second capture.
    """
    raw = await file.read()
    if not 1 <= client_attempts <= 1000:
        raise HTTPException(status_code=422, detail="client_attempts must be between 1 and 1000")
    try:
        metadata = _source_metadata(
            source=source,
            intent=intent,
            shared_title=shared_title,
            shared_text=shared_text,
            shared_url=shared_url,
            accepted_at=accepted_at,
            client_attempts=client_attempts,
            proof_run_id=proof_run_id,
            device_cohort_id=device_cohort_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = capture(
            raw=raw,
            original_name=file.filename or "capture",
            channel="web",
            declared_mime=file.content_type,
            client_capture_id=client_capture_id,
            source_metadata=metadata,
        )
    except InvalidClientCaptureId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ClientCaptureConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BlobDurabilityError as exc:
        raise HTTPException(
            status_code=503,
            detail="capture original is not durably stored",
        ) from exc

    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        status = repo_captures.submission_status(
            conn, result["client_capture_id"]
        )
    if status is None:  # commit acknowledgement must never be guessed
        raise HTTPException(status_code=503, detail="durable capture acknowledgement unavailable")
    return JSONResponse(
        {
            **status,
            "replayed": bool(result.get("replayed")),
            "status_url": str(
                request.url_for(
                    "capture_status",
                    client_capture_id=result["client_capture_id"],
                )
            ),
        }
    )


@router.post(
    "/captures/{client_capture_id}/client-event",
    response_class=JSONResponse,
)
def capture_client_event(
    client_capture_id: str,
    event: str = Form("online_durable_ack"),
    duration_ms: int = Form(...),
    sequence_no: int = Form(1),
):
    """Record the phone-observed durable acknowledgement without document data."""
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            inserted = repo_captures.record_client_event(
                conn,
                capture_id=client_capture_id,
                client_event=event,
                duration_ms=duration_ms,
                sequence_no=sequence_no,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="capture not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"recorded": inserted})


@router.get("/capture/metrics", response_class=JSONResponse)
def capture_reliability_metrics(
    proof_run_id: str = Query(default=""),
    device_cohort_id: str = Query(default=""),
):
    """Return aggregate, content-free reliability telemetry."""
    settings = get_settings()
    try:
        with engine.read_conn(settings.db_path) as conn:
            metrics = repo_captures.capture_metrics(
                conn,
                proof_run_id=proof_run_id,
                device_cohort_id=device_cohort_id,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(metrics)


@router.get(
    "/captures/{client_capture_id}",
    name="capture_status",
    response_class=JSONResponse,
)
def capture_status(client_capture_id: str):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        status = repo_captures.submission_status(conn, client_capture_id)
    if status is None:
        raise HTTPException(status_code=404, detail="capture not found")
    return JSONResponse(status)


@router.post("/share-target")
async def share_target_fallback(
    files: list[UploadFile] = File(...),
    title: str = Form(""),
    text: str = Form(""),
    url: str = Form(""),
):
    """Online fallback when an installed browser does not dispatch through the SW.

    The service worker is the primary share-target path and persists files to
    IndexedDB before upload. This fallback still commits each original before
    redirecting and passes opaque capture ids to the status page.
    """
    capture_ids: list[str] = []
    metadata = _source_metadata(
        source="share",
        intent="receipt",
        shared_title=title,
        shared_text=text,
        shared_url=url,
    )
    for file in files:
        capture_id = str(uuid4())
        raw = await file.read()
        capture(
            raw=raw,
            original_name=file.filename or "shared-capture",
            channel="web",
            declared_mime=file.content_type,
            client_capture_id=capture_id,
            source_metadata=metadata,
        )
        capture_ids.append(capture_id)
    query = urlencode({"capture_id": capture_ids}, doseq=True)
    return RedirectResponse(f"/upload?{query}", status_code=303)


@router.get("/documents/{doc_id}/status", response_class=HTMLResponse)
def document_status(request: Request, doc_id: int):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        doc = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source_document_id=? ORDER BY id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        category_name = ""
        if txn is not None:
            row = conn.execute(
                """SELECT c.name FROM transaction_splits s
                   JOIN categories c ON c.id = s.category_id
                   WHERE s.transaction_id = ? LIMIT 1""",
                (txn["id"],),
            ).fetchone()
            category_name = row["name"] if row else ""
    pending = doc is not None and doc["status"] == "staged"
    failure = repo_documents.extraction_failure(doc) if doc is not None else None
    return templates.TemplateResponse(
        request,
        "_doc_status.html",
        {
            "doc": doc,
            "txn": txn,
            "category_name": category_name,
            "pending": pending,
            "failure": failure,
        },
    )


@router.post("/documents/{doc_id}/retry", response_class=HTMLResponse)
def document_retry(request: Request, doc_id: int):
    """Requeue a document whose extraction ran out of attempts."""
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE id=?", (doc_id,)
        ).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        if repo_documents.extraction_failure(doc) is None:
            raise HTTPException(status_code=409, detail="document has not failed")
        repo_documents.clear_extraction_failure(conn, doc_id)
        repo_jobs.enqueue(
            conn,
            "ingest_document",
            {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
    return document_status(request, doc_id)
