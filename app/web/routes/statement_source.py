"""Split-screen source verification for text-layer PDF statements.

Shows the rendered statement page beside the rows extracted from it, with each
row linked to the exact region of the page it came from. The point is to make
mis-extraction visible in one glance rather than requiring a reviewer to trust
a table of numbers.

Rows are re-derived from the stored original on each request. Extraction is
deterministic and cheap, so nothing needs to be persisted for this view, and a
reviewer is always looking at what the current parser actually produces.
"""
from __future__ import annotations

import hashlib

import fitz
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...config import get_settings
from ...db import engine, repo_card_holders
from ...ingest.storage import blob_abspath
from ...ingest.structured.pdf_adapter import parse_pdf_statement
from ...ingest.structured.types import StructuredImportError
from ..templating import templates

router = APIRouter()

RENDER_DPI = 132
MAX_RENDER_PAGES = 40


def _document(document_id: int):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        row = conn.execute(
            """SELECT id, original_name, storage_ref, mime_type, sha256
               FROM source_documents WHERE id=?""",
            (int(document_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return row


def _raw(document) -> bytes:
    path = blob_abspath(str(document["storage_ref"]))
    if not path.is_file():
        raise HTTPException(status_code=410, detail="original is unavailable")
    return path.read_bytes()


@router.get("/review/pdf/{document_id}", response_class=HTMLResponse)
def statement_source(request: Request, document_id: int):
    document = _document(document_id)
    raw = _raw(document)

    parsed = None
    blockers: list[dict] = []
    try:
        parsed = parse_pdf_statement(raw, home_currency=get_settings().home_currency)
    except StructuredImportError as exc:
        blockers = [item.as_dict() for item in exc.diagnostics]

    with fitz.open(stream=raw, filetype="pdf") as doc:
        pages = [
            {
                "number": index,
                "label": index + 1,
                # The wrapper locks this aspect so overlays land correctly and
                # the page reserves its height before the image arrives.
                "aspect": (page.rect.height or 1.0) / (page.rect.width or 1.0),
            }
            for index, page in enumerate(doc)
            if index < MAX_RENDER_PAGES
        ]

    rows = []
    for row in parsed.rows if parsed is not None else ():
        anchor = row.anchor or {}
        rows.append({
            "number": row.source_row_number,
            "posted_on": row.posted_on,
            "description": row.description,
            "amount_cents": row.amount_cents,
            "balance_cents": row.balance_cents,
            "card_last4": anchor.get("card_last4", ""),
            "merchant": anchor.get("merchant_text", ""),
            "locality": anchor.get("locality", ""),
            "detail": anchor.get("detail", ""),
            "boxes": anchor.get("boxes", []),
        })

    # Rows are grouped by the card section that governs them so a supplemental
    # card's spend stays visually separate from the primary cardholder's.
    sections: list[dict] = []
    for row in rows:
        if not sections or sections[-1]["card_last4"] != row["card_last4"]:
            sections.append({"card_last4": row["card_last4"], "rows": []})
        sections[-1]["rows"].append(row)
    # Record each card we see so it can be named in Manage. A card observed
    # here is never blocking; until someone names it, it reports under a stable
    # placeholder.
    cards = [s["card_last4"] for s in sections if s["card_last4"]]
    settings = get_settings()
    if cards:
        with engine.write_tx(settings.db_path) as conn:
            for card in cards:
                repo_card_holders.observe(conn, card)
    with engine.read_conn(settings.db_path) as conn:
        labels = repo_card_holders.labels_for(conn, cards)

    for section in sections:
        section["total_cents"] = sum(r["amount_cents"] for r in section["rows"])
        card = section["card_last4"]
        section["holder"] = labels.get(card, "")
        section["holder_named"] = bool(
            card and section["holder"] != repo_card_holders.unassigned_label(card)
        )

    return templates.TemplateResponse(
        request,
        "statement_source.html",
        {
            "document": dict(document),
            "parsed": parsed,
            "rows": rows,
            "sections": sections,
            "pages": pages,
            "blockers": blockers,
            "active": "more",
            "brand": "finn",
        },
    )


@router.get("/review/pdf/{document_id}/page/{page_number}.png")
def statement_page_image(document_id: int, page_number: int):
    document = _document(document_id)
    raw = _raw(document)
    if page_number < 0 or page_number >= MAX_RENDER_PAGES:
        raise HTTPException(status_code=404, detail="page not found")

    with fitz.open(stream=raw, filetype="pdf") as doc:
        if page_number >= doc.page_count:
            raise HTTPException(status_code=404, detail="page not found")
        pixmap = doc.load_page(page_number).get_pixmap(dpi=RENDER_DPI)
        payload = pixmap.tobytes("png")

    # The original is content-addressed and immutable, so the render of any one
    # of its pages is too.
    etag = hashlib.sha256(
        f"{document['sha256']}:{page_number}:{RENDER_DPI}".encode()
    ).hexdigest()
    return Response(
        content=payload,
        media_type="image/png",
        headers={
            "ETag": f'"{etag}"',
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )
