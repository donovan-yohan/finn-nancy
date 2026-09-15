"""Stable JSON domain model shared by period-statement surfaces and exports."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ResolutionDisposition = Literal["confirmed", "human_approved", "unresolved"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class EvidenceSet(FrozenModel):
    row_ids: tuple[str, ...] = ()
    transaction_ids: tuple[int, ...] = ()
    transaction_split_ids: tuple[int, ...] = ()
    statement_line_ids: tuple[int, ...] = ()
    source_document_ids: tuple[int, ...] = ()
    source_anchor_ids: tuple[int, ...] = ()
    merchant_entity_ids: tuple[int, ...] = ()
    merchant_pattern_ids: tuple[int, ...] = ()
    canonical_merchant_claim_ids: tuple[int, ...] = ()
    category_claim_ids: tuple[int, ...] = ()
    resolution_event_ids: tuple[int, ...] = ()
    category_ids: tuple[int, ...] = ()
    relationship_ids: tuple[int, ...] = ()
    assertion_ids: tuple[int, ...] = ()
    exception_ids: tuple[int, ...] = ()
    acknowledgement_ids: tuple[int, ...] = ()
    close_snapshot_ids: tuple[int, ...] = ()


class EvidenceAmount(FrozenModel):
    line_key: str
    cents: int
    currency: str
    evidence: EvidenceSet = Field(default_factory=EvidenceSet)


class FlowBucket(FrozenModel):
    flow_kind: str
    direction: Literal["inflow", "outflow"]
    amount: EvidenceAmount


class StatementEvidenceRow(FrozenModel):
    row_id: str
    transaction_id: int
    transaction_split_id: int
    account_id: int
    posted_on: str
    description: str
    amount_cents: int
    flow_kind: str
    category_id: int
    category_name: str
    canonical_merchant: str | None = None
    resolution_disposition: ResolutionDisposition
    source: str
    reconciliation_state: str
    period_role: Literal["opening_evidence", "period_activity"]
    statement_line_ids: tuple[int, ...] = ()
    source_document_ids: tuple[int, ...] = ()
    evidence: EvidenceSet


class AccountPeriodStatement(FrozenModel):
    account_id: int
    name: str
    institution: str
    account_kind: str
    currency: str
    period_start: str
    period_end: str
    opening_balance: EvidenceAmount
    typed_inflows: tuple[FlowBucket, ...]
    typed_outflows: tuple[FlowBucket, ...]
    transfers_in: EvidenceAmount
    transfers_out: EvidenceAmount
    refunds: EvidenceAmount
    debt_movement: EvidenceAmount
    ledger_closing_balance: EvidenceAmount
    asserted_statement_closing_balance: EvidenceAmount | None
    assertion_delta: EvidenceAmount | None
    assertion_as_of: str | None
    reconciliation_state: str
    liquid_position_included: bool
    liquid_position_exclusion_reason: str | None


class HouseholdTotals(FrozenModel):
    income: EvidenceAmount
    gross_money_out: EvidenceAmount
    refunds: EvidenceAmount
    net_money_out: EvidenceAmount
    external_cash_movement: EvidenceAmount
    transfer_neutrality_control: EvidenceAmount
    adjustment_movement: EvidenceAmount
    unclassified_movement: EvidenceAmount
    opening_liquid_position: EvidenceAmount
    closing_liquid_position: EvidenceAmount


class LiquidPositionExclusion(FrozenModel):
    account_id: int
    account_name: str
    reason: str
    evidence: EvidenceSet


class ExpenseBucket(FrozenModel):
    bucket_id: str
    category_id: int | None
    category_name: str
    canonical_merchant: str | None
    resolution_disposition: ResolutionDisposition
    amount: EvidenceAmount


class ExpenseResolutionSummary(FrozenModel):
    confirmed: EvidenceAmount
    human_approved: EvidenceAmount
    unresolved: EvidenceAmount
    resolved: EvidenceAmount
    buckets: tuple[ExpenseBucket, ...]


class CloseEvidence(FrozenModel):
    state: str
    cycle_id: int | None = None
    snapshot_id: int | None = None
    snapshot_digest: str | None = None
    snapshot_is_current: bool = False
    exception_ids: tuple[int, ...] = ()
    exception_types: tuple[str, ...] = ()
    acknowledgement_ids: tuple[int, ...] = ()


class PeriodStatement(FrozenModel):
    schema_version: str = "fn148-period-statement.v1"
    report_digest: str = ""
    month: str
    period_start: str
    period_end: str
    as_of_date: str
    home_currency: str
    household: HouseholdTotals
    accounts: tuple[AccountPeriodStatement, ...]
    rows: tuple[StatementEvidenceRow, ...]
    liquid_position_exclusions: tuple[LiquidPositionExclusion, ...]
    expense_resolution: ExpenseResolutionSummary
    close: CloseEvidence
    automation_policy_version: str
    automation_authority_mode: str
    automation_authority: dict[str, bool]
