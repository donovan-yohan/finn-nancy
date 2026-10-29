"""Generated UI contracts for capture and deterministic statement imports."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient


class Elements(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.form_controls: list[tuple[str | None, str, dict[str, str | None]]] = []
        self.form_action: str | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.nodes.append((tag, attributes))
        if tag == "form":
            self.form_action = attributes.get("action")
        elif tag in {"input", "select", "textarea", "button"} and self.form_action:
            self.form_controls.append((self.form_action, tag, attributes))

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_action = None


def client():
    from app.web.app import create_app

    return TestClient(create_app())


@pytest.mark.parametrize("path", ["/upload", "/processing", "/review/import"])
def test_capture_pages_use_shared_foundation_without_legacy_css(app_env, path):
    response = client().get(path)
    assert response.status_code == 200
    nodes = Elements(response.text).nodes
    styles = [attrs["href"] for tag, attrs in nodes if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert styles == [
        "/static/foundation/vendor/bootstrap.min.css",
        "/static/foundation/tokens.css",
        "/static/foundation/components.css",
        "/static/foundation/capture.css",
    ]
    assert sum(tag == "h1" for tag, _ in nodes) == 1
    assert 'id="workspace-content"' in response.text
    assert 'src="/static/capture-store.js"' in response.text
    assert 'src="/static/capture-outbox.js"' in response.text


def test_upload_preserves_picker_and_provenance_hooks(app_env):
    response = client().get(
        "/upload?mode=camera&intent=income&capture_id=synthetic-shared-id"
    )
    nodes = Elements(response.text).nodes
    form = next(attrs for tag, attrs in nodes if tag == "form" and "data-capture-form" in attrs)
    assert form == {
        "class": "capture-page-form", "action": "/upload", "method": "post",
        "enctype": "multipart/form-data", "data-capture-form": None,
        "data-source": "camera", "data-intent": "income",
        "data-proof-run-id": "", "data-device-cohort-id": "",
    }
    file = next(attrs for tag, attrs in nodes if tag == "input" and attrs.get("id") == "capture-page-files")
    assert file["accept"] == "image/*,application/pdf"
    assert file["id"] == "capture-page-files"
    assert file["capture"] == "environment"
    assert "multiple" not in file
    assert "required" in file
    assert file["aria-describedby"] == "capture-page-help"
    assert 'data-server-capture-id="synthetic-shared-id"' in response.text
    assert 'id="capture-center"' not in response.text
    normal = client().get("/upload").text
    normal_file = next(attrs for tag, attrs in Elements(normal).nodes if tag == "input" and attrs.get("id") == "capture-page-files")
    assert "multiple" in normal_file
    assert "capture" not in normal_file


def test_statement_form_preserves_all_controls_and_mapping_hooks(app_env):
    response = client().get("/review/import")
    page = Elements(response.text)
    nodes = page.nodes
    fields = {attrs["name"]: (tag, attrs) for action, tag, attrs in page.form_controls
              if action == "/review/import/preview" and tag in {"input", "select"} and "name" in attrs}
    assert set(fields) == {
        "file", "adapter_kind", "account_id", "date_column", "description_column",
        "amount_column", "debit_column", "credit_column", "balance_column",
        "currency_column", "pending_column", "fitid_column", "date_format",
        "delimiter", "default_currency", "period_start_on", "period_end_on",
        "statement_issued_on", "opening_balance", "closing_balance",
    }
    for tag, attrs in fields.values():
        assert ("form-select" if tag == "select" else "form-control") in str(attrs["class"])
    assert fields["file"][1]["accept"] == ".csv,.ofx,.qfx,text/csv,application/x-ofx"
    assert "data-structured-import-format" in fields["adapter_kind"][1]
    assert any(tag == "fieldset" and "data-csv-mapping-fields" in attrs for tag, attrs in nodes)
    assert any(tag == "form" and attrs.get("action") == "/review/import/preview" and attrs.get("method") == "post" for tag, attrs in nodes)


def test_import_run_keeps_partial_counts_recovery_and_single_poller(app_env):
    from app.db import engine, repo_documents, repo_import_runs

    with engine.write_tx(app_env) as conn:
        run = repo_import_runs.create(conn)
        ids = []
        for index, status in enumerate(["processed", "needs_review", "staged"]):
            doc = repo_documents.insert_source_document(
                conn, kind="statement", original_name=f"Generated statement {index}.pdf",
                storage_ref=f"generated-{index}", sha256=f"{index + 100:064x}",
                mime_type="application/pdf", status="staged" if index == 1 else status,
            )
            conn.execute("UPDATE source_documents SET import_run_id=? WHERE id=?", (run, doc))
            ids.append(doc)
        repo_documents.record_extraction_failure(conn, ids[1], reason="generated", attempts=3)
    response = client().get(f"/imports/runs/{run}")
    assert response.status_code == 200
    assert "/static/foundation/capture.css" in response.text
    assert "/static/app.css" not in response.text
    assert response.text.count("hx-trigger=") == 1
    assert f'hx-get="/imports/runs/{run}/status"' in response.text
    for label in ["imported", "need you", "still working"]:
        assert f"<strong>1</strong> {label}" in response.text
    assert f'hx-post="/documents/{ids[1]}/retry"' in response.text
    assert 'hx-target="#run-status"' in response.text
    assert "Nothing from it has been recorded." in response.text
    assert "run-item attention" in response.text
    assert "run-item settled" in response.text
    assert "run-item working" in response.text
    partial = client().get(f"/imports/runs/{run}/status").text
    assert 'id="run-status" class="fn-capture-run"' in partial
    assert "<html" not in partial
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE source_documents SET status='processed' WHERE id=?", (ids[2],))
    terminal = client().get(f"/imports/runs/{run}/status").text
    assert "hx-trigger" not in terminal
    assert "<strong>2</strong> imported" in terminal


def test_server_status_fragments_keep_recovery_and_exact_money(app_env):
    from app.web.templating import templates

    fragment = templates.env.get_template("_doc_status.html")
    pending = fragment.render(doc={"id": 12, "status": "staged"}, pending=True)
    assert 'hx-get="/documents/12/status"' in pending
    assert 'hx-trigger="every 2s"' in pending
    failed = fragment.render(doc={"id": 12}, pending=False, failure={"attempts": 3})
    assert "hx-trigger" not in failed
    assert 'hx-target="closest .doc-status"' in failed
    assert 'class="btn btn-outline-secondary"' in failed
    assert "The original is safe." in failed
    done = fragment.render(doc={"id": 12}, pending=False, failure=None,
                           txn={"description": "Generated receipt", "amount_cents": -12345},
                           category_name="Generated category")
    assert "Generated receipt" in done
    assert "-$123.45" in done
    assert "hx-trigger" not in done


def test_upload_result_fragment_keeps_per_file_outcomes_and_batch_link(app_env):
    from app.web.templating import templates

    html = templates.env.get_template("_upload_results.html").render(
        results=[
            {"status": "duplicate", "source_document_id": 41},
            {"status": "staged", "source_document_id": 42},
            {"status": "needs_review", "source_document_id": 43},
        ],
        run_id=8,
    )
    assert "Already imported (doc #41)" in html
    assert 'hx-get="/documents/42/status"' in html
    assert 'hx-trigger="load delay:1s, every 2s"' in html
    assert html.count("hx-trigger") == 1
    assert "needs_review" in html
    assert 'href="/imports/runs/8"' in html
    assert "Saved" not in html


def test_import_empty_states_are_not_failed_reads(app_env, empty_db, monkeypatch):
    from app.config import get_settings
    from app.db import engine, repo_import_runs

    with engine.write_tx(app_env) as conn:
        run = repo_import_runs.create(conn)
    html = client().get(f"/imports/runs/{run}").text
    assert "This run has no documents." in html
    assert "hx-trigger" not in html
    monkeypatch.setenv("DB_PATH", empty_db)
    get_settings.cache_clear()
    form = client().get("/review/import")
    assert form.status_code == 200
    assert "No active ledger accounts." in form.text
    assert 'href="/manage"' in form.text
    get_settings.cache_clear()


def test_capture_runtime_design_contracts_execute():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    assert node is not None, "Node.js is required for capture runtime contracts"
    result = subprocess.run(
        [node, "--test", "tests/js/design_capture.test.cjs"],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"\bpass 3\b", result.stdout), result.stdout
    assert re.search(r"\bfail 0\b", result.stdout), result.stdout
