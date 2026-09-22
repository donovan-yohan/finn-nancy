from __future__ import annotations

from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_documents
from app.web.app import create_app


def test_queue_is_a_page_not_a_global_overlay(app_env):
    client = TestClient(create_app())
    for path in ("/", "/activity", "/upload", "/goals"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'id="capture-center"' not in response.text
        assert 'href="/processing"' in response.text
        assert 'id="toast"' in response.text
    queue = client.get("/processing")
    assert queue.status_code == 200
    assert '<h1>Processing</h1>' in queue.text
    assert 'id="capture-status-list"' in queue.text
    assert 'id="capture-toggle"' not in queue.text
    assert "No uploaded files." in queue.text
    assert '"/processing"' in client.get("/sw.js").text


def test_queue_lists_other_device_uploads_and_retry(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = repo_documents.insert_source_document(
            conn, kind="receipt", original_name="synthetic-test.jpg",
            storage_ref="synthetic-only", sha256="1" * 64, mime_type="image/jpeg",
        )
        repo_documents.record_extraction_failure(conn, doc_id, reason="synthetic failure", attempts=3)
    client = TestClient(create_app())
    queue = client.get("/processing")
    assert "synthetic-test.jpg" in queue.text
    assert f'data-server-document-id="{doc_id}"' in queue.text
    assert f'hx-post="/documents/{doc_id}/retry"' in queue.text
    assert "The original is safe." in queue.text
    result = client.post(f"/documents/{doc_id}/retry")
    assert result.status_code == 200
    assert "processing" in result.text
    with engine.read_conn(app_env) as conn:
        document = repo_documents.get_document(conn, doc_id)
        assert document is not None and document["status"] == "staged"
        assert conn.execute("SELECT count(*) FROM jobs WHERE source_document_id=?", (doc_id,)).fetchone()[0] == 1


def test_queue_paginates_without_hiding_older_unfinished_files(app_env):
    with engine.write_tx(app_env) as conn:
        ids = []
        for index in range(53):
            ids.append(repo_documents.insert_source_document(
                conn, kind="receipt", original_name=f"synthetic-{index}.jpg",
                storage_ref=f"synthetic-{index}", sha256=f"{index:064x}", mime_type="image/jpeg",
                status="staged" if index == 0 else "processed",
            ))
    client = TestClient(create_app())
    first = client.get("/processing")
    second = client.get("/processing?page=2")
    assert first.text.count("data-server-document-id=") == 50
    assert second.text.count("data-server-document-id=") == 3
    assert f'data-server-document-id="{ids[0]}"' in first.text
    assert 'href="/processing?page=2"' in first.text
    assert 'href="/processing?page=1"' in second.text
    assert client.get("/processing?page=0").status_code == 422


@pytest.mark.parametrize("path,brand", [
    ("/activity", "finn"), ("/processing", "finn"), ("/close", "finn"),
    ("/backlog", "finn"), ("/actions", "finn"), ("/goals", "nancy"),
])
def test_page_role_colors(app_env, path, brand):
    response = TestClient(create_app()).get(path)
    assert response.status_code == 200
    assert f'brand-{brand}' in response.text


def test_theme_has_no_gradients_or_blur():
    css = (Path(__file__).resolve().parents[1] / "app/web/static/app.css").read_text()
    assert "gradient(" not in css
    assert "blur(" not in css


def test_text_tokens_meet_normal_text_contrast():
    css = (Path(__file__).resolve().parents[1] / "app/web/static/app.css").read_text()
    colors = dict(re.findall(r"--([\w-]+):\s*#([0-9a-f]{6});", css))

    def luminance(hex_color):
        components = [int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in components]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    for surface in ("bg", "panel", "panel-2"):
        for text in ("text", "muted", "finn", "nancy", "green", "red"):
            ratio = (luminance(colors[text]) + 0.05) / (luminance(colors[surface]) + 0.05)
            assert ratio >= 4.5, (text, surface, ratio)


def test_home_keeps_detail_available_without_empty_decoration(app_env):
    response = TestClient(create_app()).get("/")
    assert response.status_code == 200
    assert '<details class="card span-2 home-history">' in response.text
    assert 'class="month-card period-entry-link"' in response.text
    hero = response.text.split('<header class="hero">')[1].split('</header>')[0]
    assert 'href="/manage"' not in hero
    assert 'local finance' not in hero
    assert 'href="/manage"' in response.text  # retained in More


def test_empty_home_has_no_empty_chart_or_category_panel(empty_db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DB_PATH", empty_db)
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).get("/")
        assert response.status_code == 200
        assert 'home-history' not in response.text
        assert 'id="transactions"' in response.text
    finally:
        get_settings.cache_clear()
