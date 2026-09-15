from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage

from .helpers import make_pinned_llm, run_with_gate, short_error, trim_text
from .results import SmokeResult

PRIMITIVE = "3. Deep Agents planning and virtual filesystem"


def _actual_tool_names(updates: list[object]) -> list[str]:
    names: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        for payload in update.values():
            if not isinstance(payload, dict):
                continue
            for message in payload.get("messages", []):
                if isinstance(message, ToolMessage) and message.name:
                    names.append(message.name)
                if isinstance(message, AIMessage):
                    names.extend(str(call.get("name")) for call in message.tool_calls)
    return names


def _collect_updates(agent: Any) -> tuple[list[object], str | None]:
    updates: list[object] = []
    error: str | None = None
    try:
        for update in agent.stream(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Create a tiny reconciliation note in the virtual filesystem. "
                            "Call at most one native tool per turn. Required steps: write todos, "
                            "write /analysis/recon.md containing 'merchant: Green Grocer' and "
                            "'amount_cents: -4230', read it, edit it to add 'status: checked', "
                            "list /analysis, then answer with the checked status."
                        ),
                    }
                ]
            },
            config={"recursion_limit": 24},
            stream_mode="updates",
        ):
            updates.append(update)
    except Exception as exc:  # noqa: BLE001
        error = short_error(exc)
    return updates, error


def run() -> SmokeResult:
    try:
        agent = create_deep_agent(
            model=make_pinned_llm(),
            tools=[],
            system_prompt=(
                "Compatibility test. Use write_todos for the plan. "
                "Use native tool calls only. Never write [TOOL_CALLS] text. "
                "Use only write_todos, write_file, read_file, edit_file, and ls. "
                "Do not use execute, grep, or glob."
            ),
        )
        updates, error = run_with_gate(lambda: _collect_updates(agent))
        calls = _actual_tool_names(updates)
        rendered = [trim_text(update, limit=600) for update in updates[-6:]]
        expected = {"write_todos", "write_file", "read_file", "edit_file", "ls"}
        observed_blob = " ".join(rendered)
        if expected.issubset(set(calls)) and "status: checked" in observed_blob and not error:
            return SmokeResult(
                PRIMITIVE,
                "PASS",
                "Deep agent used todos plus state-backed file tools and preserved edited content.",
                [
                    f"tool_calls={calls}",
                    f"updates={trim_text(rendered, limit=1200)}",
                ],
            )
        return SmokeResult(
            PRIMITIVE,
            "NEEDS_ADAPTER",
            "Deep agent ran, but Mistral did not complete the built-in todo/file-tool sequence reliably.",
            [
                f"tool_calls={calls}",
                f"error={error}",
                f"updates={trim_text(rendered, limit=1200)}",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(PRIMITIVE, "BLOCKED", short_error(exc))
