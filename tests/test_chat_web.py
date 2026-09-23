from __future__ import annotations

import html
import re
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient


def _client(app_env):
    from app.web.app import create_app

    return TestClient(create_app())


def test_chat_page_renders_and_tab_present(app_env):
    client = _client(app_env)
    response = client.get("/chat")

    assert response.status_code == 200
    assert 'href="/chat"' in response.text
    assert "Chat" in response.text
    assert "fn_chat_thread" in response.headers.get("set-cookie", "")


def test_chat_page_loads_native_frontend_disabled_until_configured(app_env):
    client = _client(app_env)
    response = client.get("/chat")

    assert response.status_code == 200
    assert 'src="/static/hermes-chat.js"' in response.text
    assert 'data-chat-enabled="false"' in response.text
    assert re.search(r'<textarea[^>]*\bdisabled\b', response.text)
    assert "Powered by Hermes" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_chat_has_no_silent_legacy_fallback(app_env):
    client = _client(app_env)
    assert client.post("/chat/stream", data={"message": "hi"}).status_code == 404
    assert client.post("/chat/ping").status_code == 404


def test_chat_new_rejects_cross_origin(app_env):
    assert _client(app_env).post("/chat/new", headers={"origin": "https://other.example"}).status_code == 403


def test_chat_thread_cookie_is_stable_and_does_not_expose_gateway(app_env, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("HERMES_CHAT_URL", "ws://127.0.0.1:19119/api/ws")
    monkeypatch.setenv("HERMES_CHAT_TOKEN", "synthetic-private-token")
    get_settings.cache_clear()
    client = _client(app_env)
    first = client.get("/chat")
    cookie = client.cookies.get("fn_chat_thread")
    page = client.get("/chat")
    assert first.status_code == 200
    assert cookie == client.cookies.get("fn_chat_thread")
    assert "HttpOnly" in first.headers["set-cookie"]
    assert "SameSite=strict" in first.headers["set-cookie"]
    assert 'data-chat-enabled="true"' in page.text
    assert "synthetic-private-token" not in page.text
    assert "19119" not in page.text


def test_hermes_frontend_contract():
    node = shutil.which("node")
    assert node is not None, "Node is required for chat frontend regression checks"
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", "tests/js/test_hermes_chat.cjs"], cwd=root, check=True)


def test_chat_new_thread_sets_new_cookie(app_env):
    client = _client(app_env)
    first = client.get("/chat")
    first_cookie = client.cookies.get("fn_chat_thread")
    response = client.post("/chat/new", follow_redirects=False)
    second_cookie = response.cookies.get("fn_chat_thread") or client.cookies.get("fn_chat_thread")

    assert first.status_code == 200
    assert response.status_code == 303
    assert response.headers["location"] == "/chat"
    assert first_cookie
    assert second_cookie
    assert second_cookie != first_cookie


def test_chat_why_starts_fresh_seeded_thread(app_env):
    client = _client(app_env)
    client.get("/chat")
    first_cookie = client.cookies.get("fn_chat_thread")

    page = client.get("/chat?why=3")
    second_cookie = client.cookies.get("fn_chat_thread")
    match = re.search(r'data-seed-message="([^"]+)"', page.text)
    assert page.status_code == 200
    assert first_cookie
    assert second_cookie
    assert second_cookie != first_cookie
    assert match is not None
    seed = html.unescape(match.group(1))
    assert seed.startswith("Why was transaction 3")
    assert "Synthetic Market" in seed


def test_chat_why_missing_transaction_404(app_env):
    response = _client(app_env).get("/chat?why=999999")

    assert response.status_code == 404


def test_transactions_partial_contains_why_affordance(app_env):
    response = _client(app_env).get("/transactions")

    assert response.status_code == 200
    assert 'data-why-url="/chat?why=3"' in response.text
    assert 'href="/chat?why=3">why?</a>' in response.text
    assert "hold for why" not in response.text
