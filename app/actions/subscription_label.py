"""Apply/revert subscription watchlist label proposals."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from ..db import repo_budgets


def _text_field(payload: Mapping[str, object], key: str) -> str:
    try:
        return str(payload.get(key) or "").strip()
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be text") from None


def _merchant(payload: Mapping[str, object]) -> str:
    merchant = _text_field(payload, "merchant")
    if not merchant:
        raise ValueError("merchant is required")
    return merchant


def _account_id(payload: Mapping[str, object]) -> int:
    try:
        return int(payload.get("account_id"))
    except (TypeError, ValueError):
        raise ValueError("account_id must be an integer") from None


def _decision(payload: Mapping[str, object], key: str = "decision") -> str:
    decision = _text_field(payload, key)
    if decision not in repo_budgets.SUBSCRIPTION_WATCHLIST_DECISIONS:
        raise ValueError("invalid subscription decision")
    return decision


class SubscriptionLabelHandler:
    def normalize(self, payload: Mapping[str, object]) -> dict:
        normalized = dict(payload)
        if "merchant" in normalized:
            normalized["merchant"] = _merchant(normalized)
        if "account_id" in normalized and normalized["account_id"] not in (None, ""):
            normalized["account_id"] = _account_id(normalized)
        if "decision" in normalized:
            normalized["decision"] = _decision(normalized)
        return normalized

    def validate(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> None:
        merchant = _merchant(payload)
        account_id = _account_id(payload)
        decision = _decision(payload)
        if not decision:
            raise ValueError("decision is required")
        account = conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone()
        if account is None:
            raise ValueError("account not found")
        if not merchant:
            raise ValueError("merchant is required")

    def apply(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
        self.validate(conn, payload)
        merchant = _merchant(payload)
        account_id = _account_id(payload)
        decision = _decision(payload)
        prior = conn.execute(
            """
            SELECT decision, decided_at
            FROM subscription_watchlist_decisions
            WHERE merchant=? AND account_id=?
            """,
            (merchant, account_id),
        ).fetchone()
        detail = {
            "merchant": merchant,
            "account_id": account_id,
            "to_decision": decision,
            "from_decision": prior["decision"] if prior is not None else None,
        }
        if prior is not None and prior["decision"] == decision:
            return {"revert": None, "detail": detail, "noop": True}

        revert = {
            "merchant": merchant,
            "account_id": account_id,
            "to_decision": decision,
            "prior": dict(prior) if prior is not None else None,
        }
        repo_budgets.set_subscription_watchlist_decision(
            conn,
            merchant=merchant,
            account_id=account_id,
            decision=decision,
        )
        return {"revert": revert, "detail": detail, "noop": False}

    def revert(self, conn: sqlite3.Connection, revert_payload: Mapping[str, object]) -> dict:
        merchant = _merchant(revert_payload)
        account_id = _account_id(revert_payload)
        applied_decision = _decision(revert_payload, "to_decision")
        current = conn.execute(
            """
            SELECT decision
            FROM subscription_watchlist_decisions
            WHERE merchant=? AND account_id=?
            """,
            (merchant, account_id),
        ).fetchone()
        if current is None or current["decision"] != applied_decision:
            raise ValueError(
                "watchlist decision has changed since this action was applied; revert refused"
            )
        prior = revert_payload.get("prior")
        if prior is None:
            conn.execute(
                "DELETE FROM subscription_watchlist_decisions WHERE merchant=? AND account_id=?",
                (merchant, account_id),
            )
            return {"merchant": merchant, "account_id": account_id, "restored_decision": None}

        if not isinstance(prior, Mapping):
            raise ValueError("invalid subscription revert payload")
        decision = _decision(prior)
        repo_budgets.set_subscription_watchlist_decision(
            conn,
            merchant=merchant,
            account_id=account_id,
            decision=decision,
        )
        decided_at = str(prior.get("decided_at") or "").strip()
        if decided_at:
            conn.execute(
                """
                UPDATE subscription_watchlist_decisions
                SET decided_at=?
                WHERE merchant=? AND account_id=?
                """,
                (decided_at, merchant, account_id),
            )
        return {"merchant": merchant, "account_id": account_id, "restored_decision": decision}
