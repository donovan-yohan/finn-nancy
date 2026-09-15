from __future__ import annotations

from typing import TypedDict
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .helpers import content_to_text, make_pinned_llm, run_with_gate, short_error, trim_text
from .results import SmokeResult

PRIMITIVE = "7. HITL interrupt and resume"


class HitlState(TypedDict, total=False):
    messages: list[HumanMessage]
    proposal: str
    approval: dict[str, object]
    applied: bool


def run() -> SmokeResult:
    try:
        llm = make_pinned_llm()

        def draft(state: HitlState) -> dict[str, object]:
            response = llm.invoke(
                [
                    HumanMessage(
                        content=(
                            "Draft one short fake approval proposal for matching statement line stl-001 "
                            "to txn-001 for Green Grocer amount -4230 cents. Include the word APPLY."
                        )
                    )
                ]
            )
            return {"proposal": content_to_text(response.content)}

        def approval_gate(state: HitlState) -> dict[str, object]:
            approved = interrupt(
                {
                    "kind": "approval_required",
                    "proposal": state["proposal"],
                    "sensitive_tool": "apply_reconciliation_match",
                }
            )
            return {"approval": approved}

        def apply_match(state: HitlState) -> dict[str, object]:
            approval = state.get("approval", {})
            return {"applied": bool(approval.get("approved"))}

        builder = StateGraph(HitlState)
        builder.add_node("draft", draft)
        builder.add_node("approval_gate", approval_gate)
        builder.add_node("apply_match", apply_match)
        builder.add_edge(START, "draft")
        builder.add_edge("draft", "approval_gate")
        builder.add_edge("approval_gate", "apply_match")
        builder.add_edge("apply_match", END)
        graph = builder.compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": f"hitl-spike-{uuid4()}"}}

        first = run_with_gate(lambda: graph.invoke({"messages": []}, config=config))
        interrupt_payload = first.get("__interrupt__")
        resumed = run_with_gate(
            lambda: graph.invoke(
                Command(resume={"approved": True, "approved_by": "compat-spike"}),
                config=config,
            )
        )
        if interrupt_payload and resumed.get("applied") is True:
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "Graph interrupted before the sensitive step and resumed with Command(resume=...).",
                [
                    f"interrupt={trim_text(interrupt_payload, limit=900)}",
                    f"resumed={trim_text(resumed, limit=900)}",
                ],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Graph ran, but interrupt/resume state was not observed as expected.",
            [
                f"first={trim_text(first, limit=900)}",
                f"resumed={trim_text(resumed, limit=900)}",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
