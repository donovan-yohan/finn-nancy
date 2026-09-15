"""Hand-rolled LangGraph chat loop: agent node <-> tools node."""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, TypedDict

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.llm.client import make_llm
from app.llm.gate import llm_gate

from .prompts import SYSTEM_PROMPT
from .tools import make_tools

CHAT_RECURSION_LIMIT = 13


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


async def _ainvoke(llm: Any, messages: list[Any]) -> Any:
    if hasattr(llm, "ainvoke"):
        return await llm.ainvoke(messages)
    return await asyncio.to_thread(llm.invoke, messages)


def build_chat_graph(
    db_path: str,
    *,
    thread_id: str = "",
    llm: Any | None = None,
    llm_factory: Any | None = None,
    checkpointer: Any | None = None,
):
    tools = make_tools(db_path, thread_id=thread_id)
    llm_factory = llm_factory or make_llm
    bound_llm: Any | None = None

    def _llm() -> Any:
        nonlocal bound_llm
        if bound_llm is None:
            base = llm if llm is not None else llm_factory()
            bound_llm = base.bind_tools(tools)
        return bound_llm

    async def agent_node(state: ChatState) -> dict[str, Any]:
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state.get("messages", [])]
        async with llm_gate():
            response = await _ainvoke(_llm(), messages)
        return {"messages": [response]}

    def should_continue(state: ChatState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
