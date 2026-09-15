"""Public entry point for the read-only reconciliation analyst."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.db.engine import read_conn
from app.llm.client import make_llm

from .evidence import resolve_statement_run
from .graph import build_reconciliation_graph
from .schemas import ReconciliationReview
from .tools import IdAllowlist


async def areview_reconciliation(
    db_path: str,
    *,
    source_document_id: int | None = None,
    month: str | None = None,
    llm_factory: Callable[[], Any] = make_llm,
    llm: Any | None = None,
) -> ReconciliationReview:
    if llm is not None and llm_factory is not make_llm:
        raise ValueError("pass either llm or llm_factory, not both")

    agent_run_id = uuid4().hex
    graph = build_reconciliation_graph()
    with read_conn(db_path) as conn:
        statement_run = resolve_statement_run(
            conn,
            source_document_id=source_document_id,
            month=month,
        )
        result = await graph.ainvoke(
            {
                "conn": conn,
                "llm": llm if llm is not None else llm_factory(),
                "agent_run_id": agent_run_id,
                "statement_run": statement_run,
                "evidence_bundle": {},
                "allowed_ids": IdAllowlist(),
                "candidate_findings": [],
                "candidate_actions": [],
            },
            config={"recursion_limit": 8},
        )
    return result["review"]


def review_reconciliation(
    db_path: str,
    *,
    source_document_id: int | None = None,
    month: str | None = None,
    llm_factory: Callable[[], Any] = make_llm,
    llm: Any | None = None,
) -> ReconciliationReview:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            areview_reconciliation(
                db_path,
                source_document_id=source_document_id,
                month=month,
                llm_factory=llm_factory,
                llm=llm,
            )
        )
    raise RuntimeError("review_reconciliation cannot run inside an active event loop; use areview_reconciliation")
