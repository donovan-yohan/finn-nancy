from __future__ import annotations

from uuid import uuid4

from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from .helpers import final_text, make_pinned_llm, run_with_gate, short_error, trim_text
from .results import SmokeResult

PRIMITIVE = "5. LangGraph checkpoint persistence"


def run() -> SmokeResult:
    try:
        thread_id = f"deepagents-spike-{uuid4()}"
        agent = create_agent(
            model=make_pinned_llm(),
            tools=[],
            checkpointer=MemorySaver(),
            system_prompt=(
                "You are testing memory persistence. Follow exact reply constraints."
            ),
        )
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 4}
        first = run_with_gate(
            lambda: agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Remember this fake reconciliation code for the next turn: RCN-42. "
                                "Reply exactly STORED."
                            ),
                        }
                    ]
                },
                config=config,
            )
        )
        second = run_with_gate(
            lambda: agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "What reconciliation code did I ask you to remember? Reply with only the code.",
                        }
                    ]
                },
                config=config,
            )
        )
        first_text = final_text(first)
        second_text = final_text(second)
        if "RCN-42" in second_text:
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "MemorySaver persisted prior thread messages and resume retrieved the remembered code.",
                [f"first={trim_text(first_text)}", f"second={trim_text(second_text)}"],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Checkpointed graph ran, but the resumed turn did not recover the remembered code.",
            [f"first={trim_text(first_text)}", f"second={trim_text(second_text)}"],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
