"""Registered action kinds whose financial mutation is not built yet."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping


class StubHandler:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def normalize(self, payload: Mapping[str, object]) -> dict:
        return dict(payload)

    def validate(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> None:
        return None

    def apply(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
        raise NotImplementedError(f"{self.kind} approval is not implemented yet")

    def revert(self, conn: sqlite3.Connection, revert_payload: Mapping[str, object]) -> dict:
        raise ValueError(f"{self.kind} actions are not revertible yet")
