"""Image HTTP acceptance follows the deployed foundation, not retired assets."""
from __future__ import annotations

import io
import json
import runpy
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fault", [None, "missing", "html", "empty", "legacy"])
def test_image_http_checks_real_foundation_and_api_authority(app_env, monkeypatch, capsys, fault):
    from app.config import get_settings
    from app.web.app import create_app

    monkeypatch.setenv("API_TOKEN", "generated-image-http-key")
    get_settings.cache_clear()
    requests = []
    target = "/static/foundation/tokens.css"
    try:
        with_client = TestClient(create_app())  # No lifespan, workers or provider startup.
        try:
            def fetch(request, timeout):
                assert timeout == 5
                assert request.full_url.startswith("http://127.0.0.1:8080/")
                path = request.full_url.removeprefix("http://127.0.0.1:8080")
                requests.append(path)
                response = with_client.get(path, headers=dict(request.header_items()))
                body, headers, status = response.content, dict(response.headers), response.status_code
                if path == target and fault == "missing":
                    status = 404
                elif path == target and fault == "html":
                    headers["content-type"] = "text/html"
                    body = b"<html>not the required stylesheet</html>" * 10
                elif path == target and fault == "empty":
                    body = b""
                elif path == "/static/app.css" and fault == "legacy":
                    status = 200
                response_headers = Message()
                for name, value in headers.items():
                    response_headers[name] = value
                if status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "generated response", response_headers, io.BytesIO(body))
                return SimpleNamespace(code=status, headers=response_headers, read=lambda: body)

            monkeypatch.setattr(urllib.request, "urlopen", fetch)
            check = runpy.run_path(str(ROOT / "scripts/smoke-image"))["HTTP"]
            if fault:
                with pytest.raises(AssertionError):
                    exec(compile(check, "image-http-probe", "exec"), {})
            else:
                exec(compile(check, "image-http-probe", "exec"), {})
                result = json.loads(capsys.readouterr().out)
                for asset in ("vendor/bootstrap.min.css", "tokens.css", "components.css", "activity.css"):
                    assert result[f"/static/foundation/{asset}:none"] == 200
                assert result["/static/app.css:none"] == 404
                assert result["/api/finance/categories:missing"] == 401
                assert result["/api/finance/categories:wrong"] == 401
                assert result["/api/finance/categories:valid"] == 200
                assert set(requests) >= {target, "/static/app.css"}
        finally:
            with_client.close()
    finally:
        get_settings.cache_clear()
