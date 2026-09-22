from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient


def _client():
    # No `with` block -> lifespan (worker + warm) does not run; we exercise routes only.
    from app.web.app import create_app

    return TestClient(create_app())


class CaptureControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture_buttons: list[dict[str, str | None]] = []
        self.capture_forms: list[dict[str, str | None]] = []
        self.file_inputs: list[dict[str, str | None]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attr_map = dict(attrs)
        if tag == "button" and "data-capture-picker" in attr_map:
            self.capture_buttons.append(attr_map)
        if tag == "form" and "data-capture-form" in attr_map:
            self.capture_forms.append(attr_map)
        if tag == "input" and attr_map.get("type") == "file":
            self.file_inputs.append(attr_map)


def test_upload_page_renders_durable_accessible_capture(app_env):
    r = _client().get("/upload")
    assert r.status_code == 200
    assert "upload files" in r.text.lower()
    assert "keep your originals until saved confirms the server has a copy" in r.text.lower()
    assert 'href="/processing"' in r.text
    assert 'id="capture-center"' not in r.text

    parser = CaptureControlParser()
    parser.feed(r.text)
    assert len(parser.capture_buttons) == 2
    assert all(button.get("type") == "button" for button in parser.capture_buttons)
    assert parser.capture_forms == [
        {
            "class": "capture-page-form",
            "action": "/upload",
            "method": "post",
            "enctype": "multipart/form-data",
            "data-capture-form": None,
                "data-source": "file",
                "data-intent": "receipt",
                "data-proof-run-id": "",
                "data-device-cohort-id": "",
            }
        ]
    assert any(file_input.get("id") == "capture-page-files" for file_input in parser.file_inputs)


def test_capture_shortcut_context_is_bounded_and_camera_ready(app_env):
    response = _client().get("/upload?mode=camera&intent=income")
    assert response.status_code == 200
    assert "upload income" in response.text.lower()
    assert 'data-source="camera"' in response.text
    assert 'data-intent="income"' in response.text
    assert 'capture="environment"' in response.text

    invalid = _client().get("/upload?mode=unsafe&intent=unsafe")
    assert 'data-source="file"' in invalid.text
    assert 'data-intent="receipt"' in invalid.text


def test_upload_post_stages_document(app_env, make_jpeg):
    from app.db import engine

    client = _client()
    files = {"files": ("receipt.jpg", make_jpeg(), "image/jpeg")}
    r = client.post("/upload", files=files)
    assert r.status_code == 200
    assert "queued" in r.text.lower() or "processing" in r.text.lower()

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE kind='receipt' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='ingest_document' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert doc is not None and doc["status"] == "staged"
    assert job is not None and job["status"] == "pending"

    # status endpoint renders a polling fragment while staged
    s = client.get(f"/documents/{doc['id']}/status")
    assert s.status_code == 200
    assert "processing" in s.text.lower()


def test_share_target_fallback_commits_metadata_before_redirect(app_env, make_jpeg):
    client = _client()
    response = client.post(
        "/share-target",
        data={
            "title": "Receipt from share sheet",
            "text": "Lunch with client",
            "url": "https://merchant.example/receipt/123",
        },
        files={"files": ("shared.jpg", make_jpeg(), "image/jpeg")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    capture_ids = parse_qs(urlparse(location).query)["capture_id"]
    assert len(capture_ids) == 1

    status = client.get(f"/captures/{capture_ids[0]}")
    assert status.status_code == 200
    body = status.json()
    assert body["durable"] is True
    assert body["device_state"] == "saved"
    assert body["source_metadata"] == {
        "source": "share",
        "intent": "receipt",
        "shared_title": "Receipt from share sheet",
        "shared_text": "Lunch with client",
        "shared_url": "https://merchant.example/receipt/123",
    }


def test_share_target_fallback_keeps_all_21_files_and_statuses(app_env, make_jpeg):
    from app.db import engine

    client = _client()
    files = [
        (
            "files",
            (
                f"shared-{index}.jpg",
                make_jpeg(
                    color=(
                        index,
                        (index * 7) % 256,
                        (index * 13) % 256,
                    )
                ),
                "image/jpeg",
            ),
        )
        for index in range(21)
    ]

    response = client.post(
        "/share-target",
        files=files,
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    capture_ids = parse_qs(urlparse(location).query)["capture_id"]
    assert len(capture_ids) == 21
    assert len(set(capture_ids)) == 21

    import_page = client.get(location)
    assert import_page.status_code == 200
    assert import_page.text.count("data-server-capture-id=") == 21
    for capture_id in capture_ids:
        assert f'data-server-capture-id="{capture_id}"' in import_page.text
        status = client.get(f"/captures/{capture_id}")
        assert status.status_code == 200
        assert status.json()["durable"] is True

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 21
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 21
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 21


def test_upload_page_and_capture_route_preserve_proof_cohort(app_env, make_jpeg):
    client = _client()
    proof_run_id = str(uuid4())
    device_cohort_id = str(uuid4())
    page = client.get(
        "/upload",
        params={
            "proof_run_id": proof_run_id,
            "device_cohort_id": device_cohort_id,
        },
    )
    assert page.status_code == 200
    assert f'data-proof-run-id="{proof_run_id}"' in page.text
    assert f'data-device-cohort-id="{device_cohort_id}"' in page.text

    captured = client.post(
        "/captures",
        data={
            "client_capture_id": str(uuid4()),
            "source": "camera",
            "intent": "receipt",
            "client_attempts": "1",
            "proof_run_id": proof_run_id,
            "device_cohort_id": device_cohort_id,
        },
        files={"file": ("synthetic.jpg", make_jpeg(), "image/jpeg")},
    )
    assert captured.status_code == 200
    metrics = client.get(
        "/capture/metrics",
        params={
            "proof_run_id": proof_run_id,
            "device_cohort_id": device_cohort_id,
        },
    )
    assert metrics.status_code == 200
    assert metrics.json()["accepted"] == 1
    assert metrics.json()["by_source"] == {"camera": 1}

    missing_pair = client.get(
        "/upload", params={"proof_run_id": proof_run_id}
    )
    assert missing_pair.status_code == 422


def test_capture_route_rejects_unregistered_source(app_env, make_jpeg):
    from app.db import engine

    client = _client()
    response = client.post(
        "/captures",
        data={
            "client_capture_id": str(uuid4()),
            "source": "carrier_pigeon",
            "intent": "receipt",
            "client_attempts": "1",
        },
        files={"file": ("synthetic.jpg", make_jpeg(), "image/jpeg")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "source must be a registered capture source"
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0
