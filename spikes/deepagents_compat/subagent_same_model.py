from __future__ import annotations

from deepagents import SubAgent, create_deep_agent

from .helpers import final_text, make_pinned_llm, messages_from_state, run_with_gate, short_error, tool_call_names, tool_result_texts, trim_text
from .results import SmokeResult

PRIMITIVE = "4. Deep Agents subagent with same Mistral model"


def run() -> SmokeResult:
    try:
        llm = make_pinned_llm()
        critic: SubAgent = {
            "name": "finance_critic",
            "description": "Critiques fake finance reconciliation plans for missing evidence.",
            "system_prompt": (
                "You are a concise finance QA critic. Return one sentence with PASS or RISK "
                "and mention whether amount and merchant evidence are present."
            ),
            "tools": [],
            "model": llm,
        }
        agent = create_deep_agent(
            model=llm,
            tools=[],
            subagents=[critic],
            system_prompt=(
                "Compatibility test. You must call the finance_critic subagent using the task tool. "
                "Use the same resident model; do not mention or request another model."
            ),
        )
        state = run_with_gate(
            lambda: agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Ask finance_critic to review this fake plan: match Green Grocer "
                                "statement amount -4230 cents to txn-001 because merchant and amount match. "
                                "Then summarize the critic result."
                            ),
                        }
                    ]
                },
                config={"recursion_limit": 12},
            )
        )
        messages = messages_from_state(state)
        calls = tool_call_names(messages)
        results = tool_result_texts(messages)
        text = final_text(state)
        if "task" in calls and ("amount" in " ".join(results + [text]).lower()):
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "Deep Agents task tool delegated to one subagent using the same ChatOpenAI instance.",
                [
                    f"tool_calls={calls}",
                    f"task_result={trim_text(results, limit=900)}",
                    f"final={trim_text(text)}",
                ],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Subagent graph ran, but task delegation was not observed in the messages.",
            [
                f"tool_calls={calls}",
                f"tool_results={trim_text(results, limit=900)}",
                f"final={trim_text(text)}",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
