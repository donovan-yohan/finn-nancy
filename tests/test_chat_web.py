from __future__ import annotations

import html
import json
import re

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


def _client(app_env):
    from app.web.app import create_app

    return TestClient(create_app())


def _events(response_text: str) -> list[dict]:
    events = []
    for frame in response_text.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        payload = None
        for line in frame.splitlines():
            if line.startswith("data:"):
                payload = line.removeprefix("data:").strip()
        if payload:
            events.append(json.loads(payload))
    return events


def test_chat_page_renders_and_tab_present(app_env):
    client = _client(app_env)
    response = client.get("/chat")

    assert response.status_code == 200
    assert 'href="/chat"' in response.text
    assert "Chat" in response.text
    assert "fn_chat_thread" in response.headers.get("set-cookie", "")


def test_chat_page_wires_enter_to_send_and_autofocus(app_env):
    client = _client(app_env)
    response = client.get("/chat")

    assert response.status_code == 200
    # Textarea is focused on load: HTML autofocus attribute + JS focus fallback.
    assert re.search(r'<textarea[^>]*\bautofocus\b', response.text)
    assert "input.focus();" in response.text
    # Keydown handler: Enter sends, Shift+Enter inserts a newline, IME
    # composition Enter is ignored, and a send in flight is not re-fired.
    assert "input.addEventListener('keydown'" in response.text
    assert (
        "event.key === 'Enter' && !event.shiftKey && !event.isComposing"
        in response.text
    )
    assert "if (!send.disabled)" in response.text
    assert "submitText(input.value)" in response.text
    # SSE submit path is unchanged.
    assert 'action="/chat/stream"' in response.text


def test_chat_sse_stream_frames_with_fake_agent(app_env, monkeypatch):
    async def fake_stream(db_path, thread_id, message):
        yield {"type": "warming"}
        yield {"type": "step_start", "tool": "query_finances", "input": {"query": "monthly_cashflow"}}
        yield {"type": "step_end", "tool": "query_finances", "output": '{"rows":[]}'}
        yield {"type": "token", "text": "hello"}
        yield {"type": "done", "message": "hello"}

    import app.web.routes.chat as chat_routes

    monkeypatch.setattr(chat_routes, "stream_chat", fake_stream)
    client = _client(app_env)
    response = client.post("/chat/stream", data={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [event["type"] for event in events] == ["warming", "step_start", "step_end", "token", "done"]
    assert events[1]["tool"] == "query_finances"


def test_chat_sse_error_frame(app_env, monkeypatch):
    async def fake_stream(db_path, thread_id, message):
        yield {"type": "error", "message": "boom"}

    import app.web.routes.chat as chat_routes

    monkeypatch.setattr(chat_routes, "stream_chat", fake_stream)
    response = _client(app_env).post("/chat/stream", data={"message": "hi"})

    events = _events(response.text)
    assert events == [{"type": "error", "message": "boom"}]


class PersistFakeChat:
    def __init__(self):
        self.responses = [AIMessage(content="first answer"), AIMessage(content="second answer")]
        self.seen_messages = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.seen_messages.append(messages)
        return self.responses.pop(0)


def test_chat_thread_persistence_across_two_messages(app_env, monkeypatch):
    import app.agents.chat.graph as chat_graph

    fake = PersistFakeChat()
    monkeypatch.setattr(chat_graph, "make_llm", lambda: fake)

    client = _client(app_env)
    client.get("/chat")
    first = client.post("/chat/stream", data={"message": "first question"})
    second = client.post("/chat/stream", data={"message": "second question"})
    page = client.get("/chat")

    assert first.status_code == 200
    assert second.status_code == 200
    assert "first answer" in page.text
    assert "second answer" in page.text
    second_seen = [getattr(message, "content", "") for message in fake.seen_messages[-1]]
    assert "first question" in second_seen
    assert "first answer" in second_seen
    assert "second question" in second_seen


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


def test_chat_why_starts_fresh_seeded_thread(app_env, monkeypatch):
    seen = []

    async def fake_stream(db_path, thread_id, message):
        seen.append({"thread_id": thread_id, "message": message})
        yield {"type": "token", "text": "because"}
        yield {"type": "done", "message": "because"}

    import app.web.routes.chat as chat_routes

    monkeypatch.setattr(chat_routes, "stream_chat", fake_stream)
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

    response = client.post("/chat/stream", data={"message": seed})
    events = _events(response.text)

    assert events[-1] == {"type": "done", "message": "because"}
    assert seen == [{"thread_id": second_cookie, "message": seed}]


def test_chat_why_missing_transaction_404(app_env):
    response = _client(app_env).get("/chat?why=999999")

    assert response.status_code == 404


def test_transactions_partial_contains_why_affordance(app_env):
    response = _client(app_env).get("/transactions")

    assert response.status_code == 200
    assert 'data-why-url="/chat?why=3"' in response.text
    assert 'href="/chat?why=3">why?</a>' in response.text
    assert "hold for why" not in response.text
