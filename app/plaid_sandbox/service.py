"""Person-consented Sandbox Link and staged-only sync orchestration."""
from __future__ import annotations

import hashlib
import time
import sqlite3
from decimal import InvalidOperation

from .client import SandboxClient, SandboxError, sandbox_token
from .store import CONSENT


class Service:
    def __init__(self, store, *, transport=None):
        self.store = store
        self._transport = transport

    def client(self):
        return SandboxClient(enabled=self.store.status()["enabled"], credentials=self.store.credentials(), transport=self._transport)

    def begin(self, browser, owner_id, *, consent, connection_id=None):
        if consent != CONSENT or owner_id not in {m["id"] for m in self.store.snapshot()["members"]}:
            raise SandboxError("member_consent_required")
        with self.store.operation():
            options = {"products": ["transactions"]}
            if connection_id:
                connection = self.store.connection(connection_id)
                if connection["owner_id"] != owner_id or not connection["access_token"] or connection["status"] not in {"connected", "needs_repair"}:
                    raise SandboxError("owner_or_connection_mismatch")
                options = {"access_token": connection["access_token"]}
            elif any(a["status"] == "uncertain" and not a.get("acknowledged_at")
                     for a in self.store.snapshot(owner_id)["link_attempts"]):
                raise SandboxError("exchange_uncertain_do_not_replay")
            result = self.client().call("/link/token/create", client_name="Finn Nancy Sandbox", language="en",
                country_codes=["CA", "US"], user={"client_user_id": owner_id}, **options)
            token = sandbox_token(result.get("link_token"), "link")
            session_id = self.store.new_link(browser, owner_id, token, connection_id)
            return {"session_id": session_id, "link_token": token}

    def exchange(self, browser, session_id, public_token):
        sandbox_token(public_token, "public")
        digest = hashlib.sha256(public_token.encode()).hexdigest()
        with self.store.operation():
            link = self.store.link(session_id)
            if not link or link["browser"] != browser:
                raise SandboxError("session_mismatch")
            if link["status"] == "complete" and link["public_digest"] == digest:
                return link["connection_id"]
            if link["connection_id"] or link["status"] != "ready":
                raise SandboxError("exchange_uncertain_do_not_replay")
            if time.time() - link["created"] > 1800:
                raise SandboxError("session_expired")
            # Validate opt-in before durably claiming the one-shot exchange.
            client = self.client()
            if not client.enabled or not self.store.credentials():
                raise SandboxError("sandbox_disabled_or_not_configured")
            # Authenticate completion under this exact server-issued Link token.
            # Standard Web Link event access is not universally enabled by Plaid.
            try:
                completion = client.call("/link/token/get", link_token=link["link_token"])
                sessions = completion.get("link_sessions")
                verified = False
                if completion.get("link_token") == link["link_token"] and isinstance(sessions, list):
                    for session in sessions:
                        if not isinstance(session, dict) or not isinstance(session.get("finished_at"), str) or not session["finished_at"]:
                            continue
                        if "results" in session:
                            results = session["results"]
                            items = results.get("item_add_results") if isinstance(results, dict) else None
                            if isinstance(items, list) and any(isinstance(item, dict) and item.get("public_token") == public_token for item in items):
                                verified = True
                        elif isinstance(session.get("on_success"), dict):
                            # Documented legacy fallback, never a bypass for malformed current results.
                            verified = verified or session["on_success"].get("public_token") == public_token
            except SandboxError:
                raise SandboxError("link_completion_unverified") from None
            if not verified:
                raise SandboxError("link_completion_unverified")
            self.store.claim_exchange(session_id, digest)
            try:
                result = client.call("/item/public_token/exchange", public_token=public_token)
            except SandboxError:
                raise SandboxError("exchange_uncertain_do_not_replay") from None
            access = sandbox_token(result.get("access_token"), "access")
            item_id = result.get("item_id")
            if not isinstance(item_id, str) or not 1 <= len(item_id) <= 200:
                raise SandboxError("invalid_response")
            connection = self.store.finish_exchange(session_id, link["owner_id"], item_id, access, browser)
            self.discover(connection)
            return connection

    def finish_repair(self, browser, session_id):
        with self.store.operation():
            link = self.store.link(session_id)
            if not link or link["browser"] != browser or not link["connection_id"] or time.time() - link["created"] > 1800:
                raise SandboxError("session_mismatch")
            connection = self.store.connection(link["connection_id"])
            if not connection["access_token"] or connection["status"] not in {"connected", "needs_repair"}:
                raise SandboxError("connection_disconnected")
            info = self.client().call("/item/get", access_token=connection["access_token"])
            if info.get("item", {}).get("error"):
                raise SandboxError("repair_still_required")
            self.discover(connection["id"])
            self.store.repaired(session_id, connection["id"], browser)

    def disconnect(self, connection_id, actor):
        with self.store.operation():
            connection = self.store.connection(connection_id)
            if connection["status"] == "disconnected":
                return "confirmed"
            if connection["status"] == "revocation_unconfirmed":
                return "unconfirmed"
            if not connection["access_token"]:
                raise SandboxError("connection_disconnected")
            client = self.client()
            if not client.enabled or not self.store.credentials():
                raise SandboxError("sandbox_disabled_or_not_configured")
            if connection["status"] in {"revoking", "revocation_uncertain"}:
                try:
                    info = client.call("/item/get", access_token=connection["access_token"])
                    if info.get("item", {}).get("item_id") != connection["item_id"]:
                        raise SandboxError("item_mismatch")
                except SandboxError as exc:
                    if str(exc) == "INVALID_ACCESS_TOKEN":
                        # Unusable access is not proof that the remote Item was deleted.
                        self.store.disconnected(connection_id, actor, confirmed=False)
                        return "unconfirmed"
                    raise SandboxError("revocation_uncertain") from None
            self.store.revocation_state(connection_id, actor)
            try:
                client.call("/item/remove", access_token=connection["access_token"])
                self.store.disconnected(connection_id, actor)
            except (SandboxError, sqlite3.Error):
                self.store.revocation_state(connection_id, actor, uncertain=True)
                raise SandboxError("revocation_uncertain") from None
            return "confirmed"

    def sync(self, connection_id, actor):
        with self.store.operation():
            connection = self.store.connection(connection_id)
            if not connection["access_token"] or connection["status"] in {"revoking", "revocation_uncertain", "revocation_unconfirmed"}:
                raise SandboxError("connection_disconnected")
            try:
                self.discover(connection_id)
                changes, cursor = self._window(connection)
                info = self.client().call("/item/get", access_token=connection["access_token"])
                status = info.get("status") or {}
                if not isinstance(status, dict):
                    raise SandboxError("invalid_response")
                transactions = status.get("transactions") or {}
                if not isinstance(transactions, dict):
                    raise SandboxError("invalid_response")
                bank_update = transactions.get("last_successful_update")
                if bank_update is not None:
                    from datetime import datetime
                    if not isinstance(bank_update, str) or len(bank_update) > 64:
                        raise SandboxError("invalid_response")
                    parsed_update = datetime.fromisoformat(bank_update.replace("Z", "+00:00"))
                    if parsed_update.tzinfo is None:
                        raise SandboxError("invalid_response")
                self.store.commit_window(connection_id, connection["cursor"], cursor, changes, bank_update, actor)
            except SandboxError as exc:
                self.store.connection_error(connection_id, str(exc), actor)
                raise
            except (ValueError, KeyError, TypeError, sqlite3.Error, InvalidOperation):
                self.store.connection_error(connection_id, "invalid_update_window", actor)
                raise SandboxError("invalid_update_window") from None

    def _window(self, connection):
        for attempt in range(3):
            cursor = connection["cursor"]
            changes = []
            try:
                for _ in range(20):
                    result = self.client().call("/transactions/sync", access_token=connection["access_token"],
                                                cursor=cursor, count=500, options={"include_original_description": True})
                    if type(result.get("has_more")) is not bool or not isinstance(result.get("next_cursor"), str) or len(result["next_cursor"]) > 256:
                        raise SandboxError("invalid_update_window")
                    for kind in ("added", "modified", "removed"):
                        if not isinstance(result.get(kind), list):
                            raise SandboxError("invalid_update_window")
                        changes.extend((kind, row) for row in result[kind])
                    if len(changes) > 10000:
                        raise SandboxError("update_window_limit")
                    cursor = result["next_cursor"]
                    if not result["has_more"]:
                        return changes, cursor
                raise SandboxError("update_window_limit")
            except SandboxError as exc:
                if str(exc) in {"TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "network_uncertain", "RATE_LIMIT_EXCEEDED"} and attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        raise SandboxError("update_window_limit")

    def discover(self, connection_id):
        connection = self.store.connection(connection_id)
        result = self.client().call("/accounts/get", access_token=connection["access_token"])
        if result.get("item", {}).get("item_id") != connection["item_id"]:
            raise SandboxError("item_mismatch")
        accounts = result.get("accounts")
        if not isinstance(accounts, list) or len(accounts) > 200:
            raise SandboxError("invalid_response")
        for account in accounts:
            if not isinstance(account, dict) or not all(isinstance(account.get(k), str) and 0 < len(account[k]) <= 200 for k in ("account_id", "name")):
                raise SandboxError("invalid_response")
        self.store.save_accounts(connection_id, accounts)
