from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .helpers import make_pinned_llm, run_with_gate, short_error, trim_text
from .results import SmokeResult

PRIMITIVE = "2. with_structured_output methods"
METHODS = ("json_schema", "function_calling", "json_mode")


class ReconDecision(BaseModel):
    merchant: str = Field(description="Merchant name from the statement line")
    amount_cents: int = Field(description="Signed amount in cents")
    category: Literal["groceries", "transit", "income", "unknown"]
    confidence: float = Field(ge=0, le=1)
    action: Literal["match", "needs_review"]


def _run_method(method: str) -> tuple[bool, str]:
    llm = make_pinned_llm()
    structured = llm.with_structured_output(ReconDecision, method=method)
    result = run_with_gate(
        lambda: structured.invoke(
            (
                "Extract a reconciliation decision for this fake statement row. "
                "Merchant: Green Grocer. Amount: -4230 cents. "
                "It is a grocery match and confidence is 0.91. "
                "Return only the structured object."
            )
        )
    )
    ok = isinstance(result, ReconDecision) and result.amount_cents == -4230
    return ok, trim_text(result.model_dump() if isinstance(result, ReconDecision) else result)


def run() -> SmokeResult:
    details: list[str] = []
    passed: list[str] = []
    blocked: list[str] = []
    for method in METHODS:
        try:
            ok, observed = _run_method(method)
            if ok:
                passed.append(method)
                details.append(f"{method}: PASS {observed}")
            else:
                blocked.append(method)
                details.append(f"{method}: NEEDS_ADAPTER unexpected_result={observed}")
        except Exception as exc:  # noqa: BLE001
            blocked.append(method)
            details.append(f"{method}: BLOCKED {short_error(exc)}")

    if len(passed) == len(METHODS):
        status = "PASS"
        note = "All structured-output methods returned the Pydantic schema."
    elif passed:
        status = "NEEDS_ADAPTER"
        note = f"Structured output is partial: works={passed}; blocked={blocked}."
    else:
        status = "BLOCKED"
        note = "No with_structured_output method returned the Pydantic schema."
    return SmokeResult(PRIMITIVE, status, note, details)
