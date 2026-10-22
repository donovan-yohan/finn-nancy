"""Loopback-only candidate, with no production router, worker, model or settings loader."""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from ..web.templating import templates
from .client import SandboxError
from .service import Service
from .store import Store

COOKIE = "fn_sandbox_session"


def create_app(root: Path, *, port=8772, transport=None, credentials_file=None):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid loopback port")
    origin = f"http://127.0.0.1:{port}"
    store = Store(root, credentials_file=credentials_file)
    service = Service(store, transport=transport)
    app = FastAPI(title="Finn Nancy Sandbox", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.service = service
    app.mount("/static", StaticFiles(directory=Path(__file__).parents[1] / "web" / "static"), name="static")

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        def denied(code=403):
            return JSONResponse({"error": "local_sandbox_request_rejected"}, status_code=code, headers={"Cache-Control": "no-store"})
        if request.headers.get("host") != f"127.0.0.1:{port}" or request.headers.get("sec-fetch-site") == "cross-site":
            return denied()
        if request.method not in {"GET", "HEAD"}:
            session = store.browser(request.cookies.get(COOKIE))
            if request.headers.get("origin") != origin or not session or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                return denied()
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 16384:
                    return denied(413)
                body.extend(chunk)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.plaid.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://sandbox.plaid.com; "
            "frame-src https://cdn.plaid.com; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'; worker-src 'none'")
        return response

    def render_page(request: Request, template: str, active: str):
        session = store.browser(request.cookies.get(COOKIE)) or store.new_browser()
        response = templates.TemplateResponse(request, template, {
            "active": active, "brand": "finn", "sandbox_candidate": True,
            "private_handoff": store.credentials_file is not None,
        })
        response.set_cookie(COOKIE, session["id"], httponly=True, samesite="strict", max_age=86400)
        return response

    @app.get("/")
    def home(request: Request):
        return render_page(request, "sandbox_overview.html", "home")

    @app.get("/activity")
    def activity(request: Request):
        return render_page(request, "sandbox_activity.html", "activity")

    @app.get("/accounts")
    def accounts(request: Request):
        return render_page(request, "sandbox_accounts.html", "accounts")

    def unavailable(request: Request):
        return templates.TemplateResponse(request, "sandbox_unavailable.html", {
            "active": "connections", "brand": "finn", "sandbox_candidate": True,
        })
    for path in ("upload", "insights", "goals", "chat", "processing", "statements", "actions", "close", "recon", "review", "backlog", "categories", "manage"):
        app.add_api_route("/" + path, unavailable, methods=["GET"])

    @app.get("/healthz")
    def health():
        return {"status": "ok", "environment": "sandbox", "ledger_authority": False}

    @app.get("/connections")
    def connections(request: Request):
        return render_page(request, "connections.html", "connections")

    @app.get("/api/sandbox/state")
    def state(request: Request, owner_id: str | None = None):
        session = store.browser(request.cookies.get(COOKIE))
        if not session:
            return JSONResponse({"error": "session_required"}, status_code=403)
        return {**store.status(), **store.snapshot(owner_id), "csrf": session["csrf"]}

    @app.get("/api/sandbox/overview")
    def overview_data(request: Request):
        session = store.browser(request.cookies.get(COOKIE))
        if not session:
            return JSONResponse({"error": "session_required"}, status_code=403)
        return {**store.status(), **store.overview(), "csrf": session["csrf"]}

    @app.get("/api/sandbox/activity")
    def activity_data(request: Request, owner_id: str | None = None, connection_id: str | None = None,
                      account_id: str | None = None, q: str = "", date_from: str | None = None,
                      date_to: str | None = None, state: str = "current", inclusion: str = "all",
                      offset: int = 0, limit: int = 50):
        if not store.browser(request.cookies.get(COOKIE)):
            return JSONResponse({"error": "session_required"}, status_code=403)
        try:
            return store.activity(owner_id=owner_id, connection_id=connection_id, account_id=account_id,
                                  q=q, date_from=date_from, date_to=date_to, state=state,
                                  inclusion=inclusion, offset=offset, limit=limit)
        except ValueError:
            return JSONResponse({"error": "invalid_activity_filter"}, status_code=400)

    @app.post("/api/sandbox/{action}")
    async def mutate(request: Request, action: str):
        session = store.browser(request.cookies.get(COOKIE))
        if not session:
            return JSONResponse({"error": "session_required"}, status_code=403)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("object required")
            for key, value in data.items():
                if key == "selected":
                    if type(value) is not bool:
                        raise ValueError("boolean required")
                elif value is None and key == "connection_id":
                    continue
                elif not isinstance(value, str) or len(value) > 256:
                    raise ValueError("bounded string required")
            actor = session["id"]
            if action == "setup":
                await run_in_threadpool(store.configure, data["client_id"], data["secret"], data["consent"], actor)
            elif action == "enable-private-file":
                await run_in_threadpool(store.enable_private_file, data["consent"], actor)
            elif action == "disable":
                await run_in_threadpool(store.disable, actor)
            elif action == "member":
                await run_in_threadpool(store.add_member, data["name"], actor)
            elif action == "link":
                return await run_in_threadpool(service.begin, actor, data["owner_id"], consent=data["consent"], connection_id=data.get("connection_id"))
            elif action == "acknowledge-exchange":
                await run_in_threadpool(store.acknowledge_exchange, data["session_id"], data["consent"], actor)
            elif action == "exchange":
                connection = await run_in_threadpool(service.exchange, actor, data["session_id"], data["public_token"])
                return {"connection_id": connection}
            elif action == "mapping":
                await run_in_threadpool(store.map_account, data["connection_id"], data["account_id"], selected=data["selected"], ownership=data["ownership"], actor=actor)
            elif action == "sync":
                await run_in_threadpool(service.sync, data["connection_id"], actor)
            elif action == "repair-complete":
                await run_in_threadpool(service.finish_repair, actor, data["session_id"])
            elif action == "disconnect":
                revocation = await run_in_threadpool(service.disconnect, data["connection_id"], actor)
                return {"ok": True, "revocation": revocation}
            else:
                return JSONResponse({"error": "unknown_action"}, status_code=404)
            return {"ok": True}
        except SandboxError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_sandbox_request"}, status_code=400)

    return app
