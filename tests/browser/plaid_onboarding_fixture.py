"""Generated-only onboarding server. No live credentials, files or provider traffic."""
from pathlib import Path
import asyncio
import json

import httpx
from fastapi.responses import JSONResponse

from app.plaid_sandbox.web import create_app
from app.plaid_sandbox.client import SandboxError
from tests.test_plaid_sandbox_service import GeneratedPlaid, connected, page


def onboarding_app(root: Path, port: int, scenario: str):
    private_file = None
    if scenario != "manual":
        private = root / "private"
        private.mkdir(parents=True, mode=0o700)
        private_file = private / "plaid-sandbox.json"
        private_file.write_text(json.dumps({"environment": "sandbox", "client_id": "generated-client", "secret": "generated-secret"}))
        private_file.chmod(0o600)
    provider = GeneratedPlaid()
    provider.pages = [page()] * 40
    app = create_app(root / "data", port=port, credentials_file=private_file,
                     transport=httpx.MockTransport(provider))
    store, service = app.state.store, app.state.service
    populated = {"accounts", "drafts", "identity", "keyboard", "lifecycle", "revocation", "repair-exit", "repair-success", "sync-focus"}
    if scenario.startswith("empty-"):
        populated.add(scenario)
    if scenario in populated | {"manual"}:
        if private_file:
            store.enable_private_file("plaid-sandbox.v1", "generated-fixture")
        else:
            store.configure("generated-client", "generated-secret", "plaid-sandbox.v1")
    if scenario in populated:
        for name, suffix in [("Generated Alpha", "alpha"), ("Generated Beta", "beta")]:
            connected(service, store, name, suffix)
    if scenario == "manual":
        store.disable("generated-fixture")
    if scenario == "repair-success":
        store.connection_error(store.overview()["connections"][0]["id"], "ITEM_LOGIN_REQUIRED", "generated-fixture")
    if scenario == "identity":
        connection_id = store.overview()["connections"][0]["id"]
        store.save_accounts(connection_id, [
            {"account_id": f"generated-account-{index}", "name": "Example Checking", "mask": mask}
            for index, mask in enumerate(["1111", None, None, "0000"], start=2)
        ])
    if scenario.startswith("empty-"):
        connection_id = store.overview()["connections"][0]["id"]
        with store.tx() as db:
            db.execute("DELETE FROM accounts WHERE connection_id=?", (connection_id,))
        for connection in store.overview()["connections"]:
            if scenario in {"empty-disconnected", "empty-unconfirmed"}:
                store.disconnected(connection["id"], "generated-fixture", confirmed=scenario == "empty-disconnected")
            elif scenario == "empty-login":
                store.connection_error(connection["id"], "ITEM_LOGIN_REQUIRED", "generated-fixture")
        if scenario == "empty-disabled":
            store.disable("generated-fixture")
    if scenario == "revocation":
        connections = store.overview()["connections"]
        store.revocation_state(connections[0]["id"], "generated-fixture", uncertain=True)
        store.disconnected(connections[1]["id"], "generated-fixture", confirmed=False)
    if scenario == "lifecycle":
        connections = store.overview()["connections"]
        store.connection_error(connections[0]["id"], "ITEM_LOGIN_REQUIRED", "generated-fixture")
        store.connection_error(connections[1]["id"], "INVALID_UPDATE_WINDOW", "generated-fixture")
        attempt = service.begin("generated-fixture", connections[1]["owner_id"], consent="plaid-sandbox.v1")
        provider.fail_exchange = True
        try:
            service.exchange("generated-fixture", attempt["session_id"], provider.completions[attempt["link_token"]])
        except SandboxError:
            pass
        provider.fail_exchange = False
    control = {"counts": {}, "hold": "", "waiting": "", "fail": "", "fail_overview": False, "fail_discover": False}
    release = asyncio.Event()
    original_discover = service.discover
    original_sync = service.sync

    def generated_discover(connection_id):
        if control["fail_discover"]:
            raise SandboxError("generated_discover_failure")
        return original_discover(connection_id)

    service.discover = generated_discover

    def generated_sync(connection_id, actor):
        outcome = control.get("sync_state", "")
        if not outcome:
            return original_sync(connection_id, actor)
        if outcome == "disabled":
            store.disable("generated-other-browser")
        elif outcome == "disconnected":
            store.disconnected(connection_id, "generated-other-browser", confirmed=True)
        else:
            store.connection_error(connection_id, outcome, "generated-fixture")
        raise SandboxError(outcome)

    service.sync = generated_sync

    @app.middleware("http")
    async def generated_controls(request, call_next):
        path = request.url.path
        if path == "/__fixture":
            if request.method == "POST":
                fields = await request.json()
                external = fields.pop("external_mapping", None)
                if fields.pop("clear_accounts", False):
                    with store.tx() as db:
                        db.execute("DELETE FROM accounts")
                if external:
                    account = store.overview()["accounts"][external["index"]]
                    store.map_account(account["connection_id"], account["account_id"],
                                      selected=external.get("selected", bool(account["selected"])),
                                      ownership=external.get("ownership", account["ownership"]), actor="generated-other-browser")
                if fields.pop("release", False):
                    control["hold"] = ""
                    release.set()
                if fields.get("hold"):
                    release.clear()
                control.update(fields)
            return JSONResponse({**control, "provider_calls": [p for p, _ in provider.calls]})
        if path.startswith("/api/sandbox/"):
            action = path.rsplit("/", 1)[-1]
            if request.method == "POST":
                control["counts"][action] = control["counts"].get(action, 0) + 1
            if control.get("hold") == action:
                control["waiting"] = action
                await release.wait()
                control["waiting"] = ""
            if control["fail"] == action or (action == "overview" and control["fail_overview"]):
                return JSONResponse({"error": "generated_failure"}, status_code=503)
        return await call_next(request)

    return app
