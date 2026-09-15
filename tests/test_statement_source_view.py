"""Split-screen statement verification surface.

Covers the contract the review UI depends on: every extracted row is linked to
a source region on a rendered page, and a statement that fails the arithmetic
gate still renders its pages so the reviewer can find the break.
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine
from app.ingest import storage
from tests.test_pdf_statement_adapter import SCOTIA_ROWS, _scotia_pdf


def _client(db_path: str, data_dir: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("HOME_CURRENCY", "CAD")
    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def _store(db_path: str, raw: bytes, name: str = "statement.pdf") -> int:
    captured = storage.capture(
        raw=raw,
        original_name=name,
        channel="web",
        declared_mime="application/pdf",
        source_metadata={"source": "file", "intent": "statement"},
        enqueue_ingest=False,
        forced_kind="statement",
    )
    return int(captured["source_document_id"])


def _good_pdf() -> bytes:
    return _scotia_pdf(
        SCOTIA_ROWS, opening="3,808.33", withdrawals="1,263.01",
        deposits="2,928.72", closing="5,474.04",
    )


def test_every_row_links_to_a_source_region(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    document_id = _store(empty_db, _good_pdf())

    response = client.get(f"/review/pdf/{document_id}")
    assert response.status_code == 200
    body = response.text

    row_ids = set(re.findall(r'id="row-(\d+)"', body))
    assert row_ids == {"1", "2", "3", "4"}
    for number in row_ids:
        # The row points at a region, and that region points back at the row.
        assert f'href="#region-{number}-0"' in body
        assert f'id="region-{number}-0"' in body
        assert f'href="#row-{number}"' in body


def test_regions_carry_normalized_overlay_coordinates(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    document_id = _store(empty_db, _good_pdf())

    body = client.get(f"/review/pdf/{document_id}").text
    coords = re.findall(r"--x0:([\d.]+); --y0:([\d.]+); --x1:([\d.]+); --y1:([\d.]+)", body)
    assert coords
    for x0, y0, x1, y1 in coords:
        assert 0.0 <= float(x0) < float(x1) <= 1.0
        assert 0.0 <= float(y0) < float(y1) <= 1.0


def test_page_renders_as_a_cacheable_image(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    document_id = _store(empty_db, _good_pdf())

    response = client.get(f"/review/pdf/{document_id}/page/0.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["etag"]

    assert client.get(f"/review/pdf/{document_id}/page/99.png").status_code == 404


def test_blocked_statement_still_shows_its_pages_and_names_the_break(
    empty_db, tmp_path, monkeypatch
):
    """A reviewer cannot find the break if the gate hides the evidence."""
    tampered = list(SCOTIA_ROWS)
    tampered[1] = ("Jun 19", "Mortgage payment", "1,209.24", "", "2,999.99")
    raw = _scotia_pdf(tampered, opening="3,808.33", withdrawals="1,263.01",
                      deposits="2,928.72", closing="5,474.04")
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    document_id = _store(empty_db, raw)

    body = client.get(f"/review/pdf/{document_id}").text
    assert "Nothing from this statement has been recorded" in body
    assert "running balance" in body
    assert f"/review/pdf/{document_id}/page/0.png" in body


def test_missing_document_is_not_found(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    assert client.get("/review/pdf/98765").status_code == 404
