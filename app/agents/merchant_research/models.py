"""Result types for merchant research.

Nothing here can name a transaction, account, category assignment, or ledger
mutation. A research result is a *proposal* carrying its own evidence, and the
only thing it can do on its own is be shown to a person.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RESEARCH_FLOW_VERSION = "merchant-research/v1"

ResolutionSource = Literal[
    "deterministic",   # the affix classifier explained it; no model, no egress
    "non_merchant",    # recognised as something that is not a merchant at all
    "local_model",     # the model identified it without web evidence
    "web_evidence",    # the model identified it from cited search results
]

AbstentionReason = Literal[
    "",
    "search_disabled",
    "strict_local_mode",
    "query_rejected",
    "no_evidence",
    "search_unavailable",
    "model_abstained",
    "model_unavailable",
    "schema_invalid",
]


@dataclass(frozen=True)
class MerchantFinding:
    descriptor: str
    resolved: bool
    source: ResolutionSource | None = None
    canonical_merchant: str = ""
    category: str = ""
    confidence: float = 0.0
    citations: tuple[str, ...] = ()
    abstention_reason: AbstentionReason = ""
    searched: bool = False
    processor: str = ""
    platform: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.resolved and self.source == "web_evidence" and not self.citations:
            # A web-derived claim without a citation is unverifiable, and an
            # unverifiable claim is what this whole flow exists to avoid.
            raise ValueError("a web-derived resolution must carry citations")
        if not self.resolved and (self.canonical_merchant or self.category):
            raise ValueError("an abstention must not carry a merchant or category")


@dataclass(frozen=True)
class ResearchReport:
    findings: tuple[MerchantFinding, ...] = ()
    flow_version: str = RESEARCH_FLOW_VERSION
    searched_count: int = 0
    resolved_count: int = 0
    abstained_count: int = 0
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    @property
    def coverage(self) -> float:
        total = len(self.findings)
        return (self.resolved_count / total) if total else 0.0
