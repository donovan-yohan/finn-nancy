from __future__ import annotations

from pydantic import BaseModel, Field

from langchain_core.tools import tool

from .sample_data import candidate_match, spend_total


class SumPostedTransactionsArgs(BaseModel):
    account: str = Field(description="Account name, for example checking")
    month: str = Field(description="Month in YYYY-MM form")


@tool(args_schema=SumPostedTransactionsArgs)
def sum_posted_transactions(account: str, month: str) -> dict[str, object]:
    """Return fake posted spending totals for one account and month."""
    total = spend_total(account=account, month=month)
    return {"account": account, "month": month, "posted_spend_cents": total}


class ReconciliationCandidatesArgs(BaseModel):
    statement_amount_cents: int = Field(description="Signed statement amount in cents")
    merchant_hint: str | None = Field(default=None, description="Optional merchant text hint")


@tool(args_schema=ReconciliationCandidatesArgs)
def reconciliation_candidates(
    statement_amount_cents: int, merchant_hint: str | None = None
) -> list[dict[str, object]]:
    """Return fake candidate ledger rows for a statement line."""
    return candidate_match(
        amount_cents=statement_amount_cents,
        merchant_hint=merchant_hint,
    )


FINANCE_TOOLS = [sum_posted_transactions, reconciliation_candidates]
