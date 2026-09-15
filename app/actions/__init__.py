"""App-owned approval action handlers.

Handlers are pure connection-level mutators. Route handlers own transactions.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Protocol

from .recategorization import RecategorizationHandler
from .stubs import StubHandler
from .subscription_label import SubscriptionLabelHandler


class ActionHandler(Protocol):
    def normalize(self, payload: Mapping[str, object]) -> dict:
        ...

    def validate(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> None:
        ...

    def apply(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
        ...

    def revert(self, conn: sqlite3.Connection, revert_payload: Mapping[str, object]) -> dict:
        ...


_REGISTRY: dict[str, ActionHandler] = {
    "recategorization": RecategorizationHandler(),
    "subscription_label": SubscriptionLabelHandler(),
    "receipt_match": StubHandler("receipt_match"),
    "budget_update": StubHandler("budget_update"),
    "recurring_series": StubHandler("recurring_series"),
}


def get_handler(kind: str) -> ActionHandler:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise ValueError(f"unknown proposed action kind: {kind}") from None


def registered_kinds() -> tuple[str, ...]:
    return tuple(_REGISTRY)
