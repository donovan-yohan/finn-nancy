from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.web.routes.pwa import STATIC_DIR, static_assets_version


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_srcs: list[str] = []
        self.link_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "script" and attr_map.get("src"):
            self.script_srcs.append(attr_map["src"] or "")
        if tag == "link" and attr_map.get("href"):
            self.link_hrefs.append(attr_map["href"] or "")


def _client(sample_db, monkeypatch):
    monkeypatch.setenv("DB_PATH", sample_db)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def test_base_page_uses_same_origin_assets(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "unpkg" not in body
    assert "cdn.jsdelivr" not in body

    parser = AssetParser()
    parser.feed(body)
    assert "/static/htmx.min.js" in parser.script_srcs
    assert "/static/capture-store.js" in parser.script_srcs
    assert "/static/capture-outbox.js" in parser.script_srcs
    for asset_url in parser.script_srcs + parser.link_hrefs:
        assert "https://" not in asset_url


def test_htmx_is_vendored(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert len(response.content) > 10_000
    assert b"htmx" in response.content


def test_manifest_route(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["name"] == "finn-nancy"
    assert manifest["start_url"] == "/"
    assert manifest["icons"]
    assert manifest["share_target"] == {
        "action": "/share-target",
        "method": "POST",
        "enctype": "multipart/form-data",
        "params": {
            "title": "title",
            "text": "text",
            "url": "url",
            "files": [
                {
                    "name": "files",
                    "accept": ["image/*", "application/pdf"],
                }
            ],
        },
    }
    assert {shortcut["url"] for shortcut in manifest["shortcuts"]} == {
        "/upload?mode=camera&intent=receipt",
        "/upload?mode=shortcut&intent=expense",
        "/upload?mode=shortcut&intent=income",
    }


def test_static_assets_version_is_stable_and_content_derived(tmp_path):
    (tmp_path / "app.css").write_bytes(b"body { color: black; }")
    (tmp_path / "htmx.min.js").write_bytes(b"window.htmx = {};")
    (tmp_path / "sw.js").write_bytes(b"ignored service worker template")

    first = static_assets_version(tmp_path)
    second = static_assets_version(tmp_path)

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{12}", first)

    (tmp_path / "app.css").write_bytes(b"body { color: blue; }")

    assert static_assets_version(tmp_path) != first


def test_service_worker_route(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {
        "application/javascript",
        "text/javascript",
    }
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["cache-control"] == "no-cache"
    assert "addEventListener" in response.text
    version = static_assets_version(STATIC_DIR)
    assert f'finn-nancy-{version}' in response.text
    assert "__CACHE_VERSION__" not in response.text
    assert 'importScripts("/static/capture-store.js")' in response.text
    assert 'url.pathname === "/share-target"' in response.text
    assert 'event.tag === "finn-capture-outbox"' in response.text
    assert "claimDueCapture" in response.text
    assert "requestCaptureStoragePersistence" in response.text


def test_pwa_icons_are_served(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    for path in ["/static/icon-192.png", "/static/icon-512.png"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
