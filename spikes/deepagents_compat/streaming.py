from __future__ import annotations

from langchain.agents import create_agent

from .helpers import make_pinned_llm, run_with_gate, short_error, trim_text
from .results import SmokeResult
from .tools import FINANCE_TOOLS

PRIMITIVE = "6. Streaming timeline events"


def run() -> SmokeResult:
    try:
        agent = create_agent(
            model=make_pinned_llm(),
            tools=FINANCE_TOOLS,
            system_prompt="Use tools for finance arithmetic. Keep the final answer short.",
        )

        def _collect() -> list[object]:
            return list(
                agent.stream(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "Use sum_posted_transactions for checking in 2026-07, "
                                    "then answer with the total cents."
                                ),
                            }
                        ]
                    },
                    config={"recursion_limit": 8},
                    stream_mode="updates",
                )
            )

        updates = run_with_gate(_collect)
        rendered = [trim_text(update, limit=500) for update in updates]
        joined = " ".join(rendered)
        if len(updates) >= 2 and ("tools" in joined or "sum_posted_transactions" in joined):
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "stream_mode='updates' yielded model/tool timeline chunks.",
                [f"update_count={len(updates)}", f"updates={trim_text(rendered, limit=1200)}"],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Streaming produced updates, but no clear tool step was visible.",
            [f"update_count={len(updates)}", f"updates={trim_text(rendered, limit=1200)}"],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
