"""monthly_insight job handler: build a deterministic digest, ask the LLM once, cache prose."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import get_settings
from ..db import engine, repo_budgets, repo_goals, repo_insight_prose

SCOPE = "household"
KIND = "monthly_summary"


def _pick(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys if key in row}


def _sorted(items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    def key_fn(item: dict[str, Any]) -> tuple:
        return tuple("" if item.get(key) is None else item.get(key) for key in keys)

    return sorted(items, key=key_fn)


def _goal_digest(goal: dict[str, Any]) -> dict[str, Any]:
    payoff = goal.get("payoff") or {}
    return {
        "goal_id": goal.get("goal_id"),
        "name": goal.get("name"),
        "status": goal.get("status"),
        "target_cents": goal.get("target_cents"),
        "contributed_cents": goal.get("contributed_cents"),
        "remaining_cents": goal.get("remaining_cents"),
        "monthly_contribution_cents": goal.get("monthly_contribution_cents"),
        "target_month": goal.get("target_month"),
        "auto_fund": goal.get("auto_fund"),
        "priority": goal.get("priority"),
        "payoff": _pick(
            payoff,
            [
                "state",
                "remaining_cents",
                "expected_by_now_cents",
                "required_monthly_cents",
                "projected_target_month",
                "months_remaining",
                "ahead_behind_cents",
            ],
        ),
    }


def build_digest(conn, period_month: str, *, scope: str = SCOPE, kind: str = KIND) -> dict[str, Any]:
    """Return stable, JSON-serializable context for the monthly prose prompt."""
    ctx = repo_budgets.insights_context(conn, period_month)
    selected_month = ctx["selected_month"]
    goals = [_goal_digest(dict(row)) for row in repo_goals.goal_progress_rows(conn, selected_month)]

    budget_rows = [
        _pick(
            row,
            [
                "category_id",
                "category_name",
                "budget_cents",
                "actual_cents",
                "remaining_cents",
                "owner_member_id",
                "budget_owner",
            ],
        )
        for row in ctx["budget_rows"]
    ]
    planning_cards = [
        _pick(
            card,
            [
                "card_key",
                "reason_code",
                "severity_class",
                "confidence_label",
                "title",
                "body",
                "suggested_action",
                "current_action_label",
                "transaction_ids",
                "statement_line_ids",
                "category_ids",
            ],
        )
        for card in ctx["planning_insight_cards"]
    ]
    recurring_deltas = [
        _pick(
            row,
            [
                "merchant",
                "account_name",
                "category_names",
                "previous_month",
                "month",
                "previous_amount_cents",
                "current_amount_cents",
                "amount_delta_cents",
                "direction_label",
                "pct_change",
                "previous_transaction_ids",
                "current_transaction_ids",
            ],
        )
        for row in ctx["recurring_deltas"]
    ]
    subscription_watchlist = [
        _pick(
            row,
            [
                "merchant",
                "account_name",
                "category_names",
                "estimated_amount_cents",
                "previous_amount_cents",
                "current_amount_cents",
                "expected_next_charge_on",
                "decision_label",
                "transaction_ids",
            ],
        )
        for row in ctx["subscription_watchlist"]
    ]

    return {
        "period_month": selected_month,
        "scope": scope,
        "kind": kind,
        "budget_summary": dict(ctx["budget_summary"]),
        "budget_rows": _sorted(budget_rows, ("category_name", "category_id")),
        "planning_insight_cards": _sorted(planning_cards, ("card_key", "title")),
        "recurring_deltas": _sorted(recurring_deltas, ("merchant", "account_name")),
        "runway": dict(ctx["runway"]) if ctx["runway"] else None,
        "subscription_watchlist": _sorted(subscription_watchlist, ("merchant", "account_name")),
        "goals": _sorted(goals, ("priority", "goal_id")),
    }


def _content_text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        content = " ".join(parts)
    return str(content)


def _model_name(llm: Any) -> str:
    return str(
        getattr(llm, "model_name", None)
        or getattr(llm, "model", None)
        or get_settings().chat_model
    )


def handle_monthly_insight(db_path, payload: dict[str, Any], llm) -> dict[str, Any]:
    period_month = str(payload.get("period_month") or "").strip()
    scope = str(payload.get("scope") or SCOPE).strip() or SCOPE
    kind = str(payload.get("kind") or KIND).strip() or KIND
    if not period_month:
        raise ValueError("monthly_insight requires period_month")

    with engine.read_conn(db_path) as conn:
        digest = build_digest(conn, period_month, scope=scope, kind=kind)

    prompt_json = json.dumps(digest, sort_keys=True, separators=(",", ":"))
    messages = [
        SystemMessage(
            content=(
                "You are Nancy, the forward-looking finance advisor in finn-nancy. "
                "Write 2 to 4 short paragraphs. Be calm, concrete, and plain-language. "
                "Use only the supplied digest. Do not invent balances, transactions, or goals."
            )
        ),
        HumanMessage(
            content=(
                "Turn this monthly household finance digest into a concise summary and next-step advice:\n"
                f"{prompt_json}"
            )
        ),
    ]
    resp = llm.invoke(messages)
    body = _content_text(resp).strip()
    if not body:
        raise ValueError("LLM returned an empty monthly summary")
    model = _model_name(llm)

    with engine.write_tx(db_path) as conn:
        prose_id = repo_insight_prose.upsert(
            conn,
            period_month=digest["period_month"],
            scope=scope,
            kind=kind,
            body=body,
            model=model,
        )

    return {
        "status": "cached",
        "insight_prose_id": prose_id,
        "period_month": digest["period_month"],
        "model": model,
    }
