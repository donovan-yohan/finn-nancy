from __future__ import annotations

from langchain.agents import create_agent

from .helpers import final_text, make_pinned_llm, messages_from_state, short_error, tool_call_names, tool_result_texts, trim_text, run_with_gate
from .results import SmokeResult
from .tools import FINANCE_TOOLS

PRIMITIVE = "1. create_agent typed finance tools"


def run() -> SmokeResult:
    try:
        llm = make_pinned_llm()
        agent = create_agent(
            model=llm,
            tools=FINANCE_TOOLS,
            system_prompt=(
                "You are testing tool compatibility. Use the provided finance tools. "
                "Do not answer from memory when a tool can answer."
            ),
        )
        prompts = [
            (
                "Use sum_posted_transactions to compute checking spending in 2026-07. "
                "Reply with only the tool result total."
            ),
            (
                "Use reconciliation_candidates for statement_amount_cents=-4230 and merchant_hint=Green. "
                "Reply with only the best transaction id."
            ),
        ]
        states = [
            run_with_gate(
                lambda prompt=prompt: agent.invoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"recursion_limit": 8},
                )
            )
            for prompt in prompts
        ]
        messages = [message for state in states for message in messages_from_state(state)]
        calls = tool_call_names(messages)
        tool_texts = tool_result_texts(messages)
        text = " | ".join(final_text(state) for state in states)
        expected_tools = {"sum_posted_transactions", "reconciliation_candidates"}
        if expected_tools.issubset(set(calls)) and "txn-001" in " ".join(tool_texts + [text]):
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "create_agent invoked both typed tools in simple native tool-call turns.",
                [
                    f"tool_calls={calls}",
                    f"tool_results={trim_text(tool_texts)}",
                    f"final={trim_text(text)}",
                ],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Agent ran but did not complete the exact typed-tool path.",
            [
                f"tool_calls={calls}",
                f"tool_results={trim_text(tool_texts)}",
                f"final={trim_text(text)}",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
