"""Build the one evidence-backed model consumed by UI, API, CSV, and PDF."""
from __future__ import annotations

import calendar
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..db import engine, repo_period_statements
from ..reconcile.automation_policy import authority_manifest
from .models import (
    AccountPeriodStatement,
    CloseEvidence,
    EvidenceAmount,
    EvidenceSet,
    ExpenseBucket,
    ExpenseResolutionSummary,
    FlowBucket,
    HouseholdTotals,
    LiquidPositionExclusion,
    PeriodStatement,
    StatementEvidenceRow,
)

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_LIQUID_ACCOUNT_KINDS = frozenset({"cash", "chequing", "savings", "credit"})
_OUTFLOW_KINDS = frozenset({"purchase", "fee"})
_OFFSET_KINDS = frozenset({"refund", "reimbursement", "reversal"})
_INCOME_KINDS = frozenset({"income", "interest"})
_TRANSFER_KINDS = frozenset({"internal_transfer", "card_payment"})
_SNAPSHOT_FIELD = "fn148_period_statement"


class PeriodStatementError(ValueError):
    """Base error for a report that cannot be represented truthfully."""


class UnsupportedReportCurrency(PeriodStatementError):
    """A contributing row cannot be summed into the home-currency report."""


class FrozenPeriodStatementUnavailable(PeriodStatementError):
    """A closed snapshot predates FN-148 and cannot be rebuilt truthfully."""


class FrozenPeriodStatementIntegrityError(PeriodStatementError):
    """A frozen report no longer matches its canonical digest."""


@contextmanager
def _read_connection(
    db_or_conn: str | Path | sqlite3.Connection,
) -> Iterator[sqlite3.Connection]:
    if isinstance(db_or_conn, sqlite3.Connection):
        yield db_or_conn
        return
    with engine.read_conn(str(db_or_conn), read_only=True) as conn:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()


def _period_bounds(month: str) -> tuple[str, str]:
    value = str(month).strip()
    if not _MONTH.fullmatch(value):
        raise PeriodStatementError("month must be YYYY-MM")
    year, month_number = (int(part) for part in value.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return f"{value}-01", f"{value}-{last_day:02d}"


def _stable(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted({int(value) for value in values if value is not None}))


def _stable_text(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value)}))


def _merge_evidence(*items: EvidenceSet) -> EvidenceSet:
    return EvidenceSet(
        row_ids=_stable_text(
            value for item in items for value in item.row_ids
        ),
        transaction_ids=_stable(
            value for item in items for value in item.transaction_ids
        ),
        transaction_split_ids=_stable(
            value for item in items for value in item.transaction_split_ids
        ),
        statement_line_ids=_stable(
            value for item in items for value in item.statement_line_ids
        ),
        source_document_ids=_stable(
            value for item in items for value in item.source_document_ids
        ),
        source_anchor_ids=_stable(
            value for item in items for value in item.source_anchor_ids
        ),
        merchant_entity_ids=_stable(
            value for item in items for value in item.merchant_entity_ids
        ),
        merchant_pattern_ids=_stable(
            value for item in items for value in item.merchant_pattern_ids
        ),
        canonical_merchant_claim_ids=_stable(
            value
            for item in items
            for value in item.canonical_merchant_claim_ids
        ),
        category_claim_ids=_stable(
            value for item in items for value in item.category_claim_ids
        ),
        resolution_event_ids=_stable(
            value for item in items for value in item.resolution_event_ids
        ),
        category_ids=_stable(
            value for item in items for value in item.category_ids
        ),
        relationship_ids=_stable(
            value for item in items for value in item.relationship_ids
        ),
        assertion_ids=_stable(
            value for item in items for value in item.assertion_ids
        ),
        exception_ids=_stable(
            value for item in items for value in item.exception_ids
        ),
        acknowledgement_ids=_stable(
            value for item in items for value in item.acknowledgement_ids
        ),
        close_snapshot_ids=_stable(
            value for item in items for value in item.close_snapshot_ids
        ),
    )


def _amount(
    line_key: str,
    cents: int,
    currency: str,
    evidence: EvidenceSet,
) -> EvidenceAmount:
    return EvidenceAmount(
        line_key=line_key,
        cents=int(cents),
        currency=currency,
        evidence=evidence,
    )


def _close_models(raw: Mapping[str, Any]) -> tuple[CloseEvidence, EvidenceSet]:
    exceptions = list(raw.get("exceptions") or [])
    snapshot_id = raw.get("snapshot_id")
    exception_ids = _stable(
        int(item["id"]) for item in exceptions if item.get("id") is not None
    )
    acknowledgement_ids = _stable(
        int(item["acknowledgement_id"])
        for item in exceptions
        if item.get("acknowledgement_id") is not None
    )
    snapshot = raw.get("snapshot") or {}
    close = CloseEvidence(
        state=str(raw.get("state") or "open"),
        cycle_id=(
            None if raw.get("cycle_id") is None else int(raw["cycle_id"])
        ),
        snapshot_id=None if snapshot_id is None else int(snapshot_id),
        snapshot_digest=(
            None
            if not snapshot
            else str(snapshot.get("snapshot_digest") or "") or None
        ),
        snapshot_is_current=bool(raw.get("snapshot_is_current")),
        exception_ids=exception_ids,
        exception_types=_stable_text(
            str(item["exception_type"]) for item in exceptions
        ),
        acknowledgement_ids=acknowledgement_ids,
    )
    evidence = EvidenceSet(
        exception_ids=exception_ids,
        acknowledgement_ids=acknowledgement_ids,
        close_snapshot_ids=(
            () if snapshot_id is None else (int(snapshot_id),)
        ),
    )
    return close, evidence


def _row_evidence(
    raw: Mapping[str, Any],
    *,
    statements: Mapping[int, Sequence[Mapping[str, Any]]],
    claims: Mapping[int, Sequence[Mapping[str, Any]]],
    relationship_ids: Mapping[int, Sequence[int]],
    assertion_ids: Mapping[int, Sequence[int]],
    close_evidence: EvidenceSet,
) -> EvidenceSet:
    transaction_id = int(raw["transaction_id"])
    split_id = int(raw["transaction_split_id"])
    linked_lines = statements.get(transaction_id, ())
    linked_claims = claims.get(transaction_id, ())
    merchant_claims = [
        item
        for item in linked_claims
        if item["claim_kind"] == "canonical_merchant"
    ]
    category_claims = [
        item
        for item in linked_claims
        if item["claim_kind"] == "expense_category"
        and item.get("transaction_split_id") is not None
        and int(item["transaction_split_id"]) == split_id
    ]
    supporting_claims = [*merchant_claims, *category_claims]
    source_documents = {
        int(item["source_document_id"]) for item in linked_lines
    }
    if raw.get("transaction_source_document_id") is not None:
        source_documents.add(int(raw["transaction_source_document_id"]))
    return _merge_evidence(
        EvidenceSet(
            row_ids=(f"txn:{transaction_id}:split:{split_id}",),
            transaction_ids=(transaction_id,),
            transaction_split_ids=(split_id,),
            statement_line_ids=_stable(
                int(item["statement_line_id"]) for item in linked_lines
            ),
            source_document_ids=_stable(source_documents),
            source_anchor_ids=_stable(
                [
                    int(item["source_anchor_id"])
                    for item in (*linked_lines, *supporting_claims)
                    if item.get("source_anchor_id") is not None
                ]
            ),
            merchant_entity_ids=_stable(
                int(item["merchant_entity_id"])
                for item in merchant_claims
                if item.get("merchant_entity_id") is not None
            ),
            merchant_pattern_ids=_stable(
                int(item["pattern_id"])
                for item in supporting_claims
                if item.get("pattern_id") is not None
            ),
            canonical_merchant_claim_ids=_stable(
                int(item["claim_id"]) for item in merchant_claims
            ),
            category_claim_ids=_stable(
                int(item["claim_id"]) for item in category_claims
            ),
            resolution_event_ids=_stable(
                int(item["acceptance_event_id"]) for item in supporting_claims
            ),
            category_ids=(int(raw["category_id"]),),
            relationship_ids=_stable(
                relationship_ids.get(transaction_id, ())
            ),
            assertion_ids=_stable(
                assertion_ids.get(int(raw["account_id"]), ())
            ),
        ),
        close_evidence,
    )


def _canonical_merchant(
    transaction_id: int,
    claims: Mapping[int, Sequence[Mapping[str, Any]]],
) -> str | None:
    values = {
        str(item["canonical_name"])
        for item in claims.get(transaction_id, ())
        if item["claim_kind"] == "canonical_merchant"
        and item.get("canonical_name")
    }
    return next(iter(values)) if len(values) == 1 else None


def _resolution_disposition(
    raw: Mapping[str, Any],
    claims: Mapping[int, Sequence[Mapping[str, Any]]],
) -> str:
    flow_kind = str(raw["flow_kind"])
    if flow_kind not in _OUTFLOW_KINDS:
        return "confirmed"
    split_id = int(raw["transaction_split_id"])
    category_id = int(raw["category_id"])
    accepted = [
        item
        for item in claims.get(int(raw["transaction_id"]), ())
        if item["claim_kind"] == "expense_category"
        and item.get("transaction_split_id") is not None
        and int(item["transaction_split_id"]) == split_id
        and item.get("category_id") is not None
        and int(item["category_id"]) == category_id
    ]
    # The active-claim projection contains only split-specific human-confirmed
    # accept/correct events. FN-149 currently grants no automatic authority.
    return "human_approved" if accepted else "unresolved"


def _primary_assertions(
    assertions: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    period_start: str,
    period_end: str,
) -> dict[int, Mapping[str, Any]]:
    selected: dict[int, Mapping[str, Any]] = {}
    for account_id, rows in assertions.items():
        candidates = [
            item
            for item in rows
            if period_start <= str(item["asof_date"]) <= period_end
        ]
        if candidates:
            selected[int(account_id)] = max(
                candidates,
                key=lambda item: (str(item["asof_date"]), int(item["id"])),
            )
    return selected


def _report_digest(statement: PeriodStatement) -> str:
    payload = statement.model_dump(mode="json", exclude={"report_digest"})
    # Database-assigned close identities/currentness are addresses, not frozen
    # financial truth. They are attached from the immutable FN-147 rows when a
    # frozen report is read, so they cannot participate in its content digest.
    payload["close"]["cycle_id"] = None
    payload["close"]["snapshot_id"] = None
    payload["close"]["snapshot_digest"] = None
    payload["close"]["snapshot_is_current"] = False
    payload["close"]["exception_ids"] = []
    payload["close"]["acknowledgement_ids"] = []

    evidence_fields = set(EvidenceSet.model_fields)

    def strip_close_addresses(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == evidence_fields:
                value["exception_ids"] = []
                value["acknowledgement_ids"] = []
                value["close_snapshot_ids"] = []
                return
            for child in value.values():
                strip_close_addresses(child)
        elif isinstance(value, list):
            for child in value:
                strip_close_addresses(child)

    strip_close_addresses(payload)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_digest(statement: PeriodStatement) -> PeriodStatement:
    return statement.model_copy(
        update={"report_digest": _report_digest(statement)}
    )


def period_statement_snapshot_payload(
    statement: PeriodStatement,
    *,
    close_state: str,
    exception_types: Iterable[str] = (),
) -> dict[str, Any]:
    """Return the canonical object callers embed in the close snapshot.

    Close state is frozen, while database-assigned snapshot/cycle IDs remain
    address metadata and are attached on read.
    """
    if close_state not in {"clean_closed", "closed_with_exceptions"}:
        raise PeriodStatementError("invalid close state for report snapshot")
    frozen_exception_types = _stable_text(exception_types)
    if close_state == "clean_closed" and frozen_exception_types:
        raise PeriodStatementError(
            "clean closed report cannot contain exception types"
        )
    if close_state == "closed_with_exceptions" and not frozen_exception_types:
        raise PeriodStatementError(
            "exception-closed report must contain exception types"
        )
    frozen_close = statement.close.model_copy(
        update={
            "state": close_state,
            "cycle_id": None,
            "snapshot_id": None,
            "snapshot_digest": None,
            "snapshot_is_current": False,
            "exception_ids": (),
            "exception_types": frozen_exception_types,
            "acknowledgement_ids": (),
        }
    )
    frozen = _with_digest(
        statement.model_copy(update={"close": frozen_close, "report_digest": ""})
    )
    return {_SNAPSHOT_FIELD: frozen.model_dump(mode="json")}


def _frozen_statement(
    raw_close: Mapping[str, Any],
) -> PeriodStatement:
    snapshot = raw_close.get("snapshot") or {}
    try:
        snapshot_payload = json.loads(str(snapshot["snapshot_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FrozenPeriodStatementUnavailable(
            "closed period snapshot has no readable FN-148 report"
        ) from exc
    frozen_payload = snapshot_payload.get(_SNAPSHOT_FIELD)
    if not isinstance(frozen_payload, dict):
        raise FrozenPeriodStatementUnavailable(
            "closed period snapshot predates FN-148 report freezing"
        )
    statement = PeriodStatement.model_validate(frozen_payload)
    if _report_digest(statement) != statement.report_digest:
        raise FrozenPeriodStatementIntegrityError(
            "frozen period statement digest mismatch"
        )
    close, close_evidence = _close_models(raw_close)
    if tuple(statement.close.exception_types) != tuple(close.exception_types):
        raise FrozenPeriodStatementIntegrityError(
            "frozen period statement exception lineage mismatch"
        )

    payload = statement.model_dump(mode="json")
    evidence_fields = set(EvidenceSet.model_fields)

    def attach_close_addresses(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == evidence_fields:
                evidence = EvidenceSet.model_validate(value)
                value.clear()
                value.update(
                    _merge_evidence(evidence, close_evidence).model_dump(
                        mode="json"
                    )
                )
                return
            for child in value.values():
                attach_close_addresses(child)
        elif isinstance(value, list):
            for child in value:
                attach_close_addresses(child)

    attach_close_addresses(payload)
    payload["close"] = close.model_dump(mode="json")
    return PeriodStatement.model_validate(payload)


def _allocate_offset(
    cents: int,
    target_rows: Sequence[StatementEvidenceRow],
) -> list[tuple[StatementEvidenceRow, int]]:
    """Allocate a positive offset across target splits with stable remainders."""
    weights = [abs(int(row.amount_cents)) for row in target_rows]
    total = sum(weights)
    if cents <= 0 or not target_rows or total <= 0:
        return []
    allocated = [(cents * weight) // total for weight in weights]
    remainder = cents - sum(allocated)
    ranks = sorted(
        range(len(target_rows)),
        key=lambda index: (
            -((cents * weights[index]) % total),
            target_rows[index].transaction_split_id,
        ),
    )
    for index in ranks[:remainder]:
        allocated[index] += 1
    return list(zip(target_rows, allocated, strict=True))


def _build_live_statement(
    raw: Mapping[str, Any],
    *,
    month: str,
    period_start: str,
    period_end: str,
    currency: str,
) -> PeriodStatement:
    split_rows = list(raw["splits"])
    statements = raw["statement_lines"]
    claims = raw["claims"]
    relationships = list(raw["relationships"])
    transfer_pair_splits = list(raw.get("transfer_pair_splits") or ())
    assertions = raw["assertions"]
    close, close_evidence = _close_models(raw["close"])
    policy = authority_manifest()

    account_by_id = {
        int(item["id"]): item for item in raw["accounts"]
    }
    rows_by_account: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    raw_by_transaction: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for item in split_rows:
        account_id = int(item["account_id"])
        rows_by_account[account_id].append(item)
        raw_by_transaction[int(item["transaction_id"])].append(item)

    relationship_ids: dict[int, list[int]] = defaultdict(list)
    offset_relationship: dict[int, Mapping[str, Any]] = {}
    for relationship in relationships:
        relationship_id = int(relationship["id"])
        source_id = int(relationship["source_transaction_id"])
        target_id = int(relationship["target_transaction_id"])
        relationship_ids[source_id].append(relationship_id)
        relationship_ids[target_id].append(relationship_id)
        if str(relationship["relationship_kind"]) in {
            "refund_of",
            "reimbursement_for",
            "reversal_of",
        }:
            offset_relationship[source_id] = relationship

    primary_assertion = _primary_assertions(
        assertions,
        period_start=period_start,
        period_end=period_end,
    )
    assertion_ids = {
        account_id: [int(item["id"])]
        for account_id, item in primary_assertion.items()
    }
    period_transfer_transaction_ids = {
        int(item["transaction_id"])
        for item in split_rows
        if period_start <= str(item["posted_on"]) <= period_end
        and str(item["flow_kind"]) in _TRANSFER_KINDS
    }
    paired_transfer_transaction_ids: set[int] = set()
    for relationship in relationships:
        if str(relationship["relationship_kind"]) != "transfer_pair":
            continue
        source_id = int(relationship["source_transaction_id"])
        target_id = int(relationship["target_transaction_id"])
        if (
            source_id not in period_transfer_transaction_ids
            and target_id not in period_transfer_transaction_ids
        ):
            continue
        paired_transfer_transaction_ids.update((source_id, target_id))
    transfer_control_split_rows = [
        item
        for item in transfer_pair_splits
        if int(item["transaction_id"])
        in paired_transfer_transaction_ids
        and str(item["flow_kind"]) in _TRANSFER_KINDS
    ]

    contributing_account_ids = {
        int(item["account_id"]) for item in split_rows
    } | {
        int(account_id) for account_id in primary_assertion
    }
    currency_account_ids = contributing_account_ids | {
        int(item["account_id"]) for item in transfer_control_split_rows
    }
    for account_id in sorted(currency_account_ids):
        account = account_by_id.get(account_id)
        if account is None:
            raise PeriodStatementError(
                f"report evidence references unknown account {account_id}"
            )
        account_currency = str(account.get("currency") or "").strip().upper()
        if not _CURRENCY.fullmatch(account_currency) or account_currency != currency:
            raise UnsupportedReportCurrency(
                f"account {account_id} contributes {account_currency or 'blank'} "
                f"to {currency} report"
            )
    currency_transaction_ids = set(raw_by_transaction) | {
        int(item["transaction_id"]) for item in transfer_control_split_rows
    }
    for transaction_id, linked_lines in statements.items():
        if transaction_id not in currency_transaction_ids:
            continue
        for line in linked_lines:
            statement_currency = str(line.get("currency") or "").strip().upper()
            if (
                not _CURRENCY.fullmatch(statement_currency)
                or statement_currency != currency
            ):
                raise UnsupportedReportCurrency(
                    f"statement line {line['statement_line_id']} contributes "
                    f"{statement_currency or 'blank'} to {currency} report"
                )

    evidence_rows: list[StatementEvidenceRow] = []
    row_by_split: dict[int, StatementEvidenceRow] = {}
    for item in split_rows:
        evidence = _row_evidence(
            item,
            statements=statements,
            claims=claims,
            relationship_ids=relationship_ids,
            assertion_ids=assertion_ids,
            close_evidence=close_evidence,
        )
        transaction_id = int(item["transaction_id"])
        linked_lines = statements.get(transaction_id, ())
        row = StatementEvidenceRow(
            row_id=evidence.row_ids[0],
            transaction_id=transaction_id,
            transaction_split_id=int(item["transaction_split_id"]),
            account_id=int(item["account_id"]),
            posted_on=str(item["posted_on"]),
            description=str(item["description"]),
            amount_cents=int(item["split_amount_cents"]),
            flow_kind=str(item["flow_kind"]),
            category_id=int(item["category_id"]),
            category_name=str(item["category_name"]),
            canonical_merchant=_canonical_merchant(transaction_id, claims),
            resolution_disposition=_resolution_disposition(item, claims),
            source=str(item["source"]),
            reconciliation_state=str(item["recon_status"]),
            period_role=(
                "period_activity"
                if str(item["posted_on"]) >= period_start
                else "opening_evidence"
            ),
            statement_line_ids=_stable(
                int(line["statement_line_id"]) for line in linked_lines
            ),
            source_document_ids=evidence.source_document_ids,
            evidence=evidence,
        )
        evidence_rows.append(row)
        row_by_split[row.transaction_split_id] = row

    evidence_rows.sort(
        key=lambda item: (
            item.account_id,
            item.posted_on,
            item.transaction_id,
            item.transaction_split_id,
        )
    )
    report_rows_by_account: dict[int, list[StatementEvidenceRow]] = defaultdict(list)
    report_rows_by_transaction: dict[int, list[StatementEvidenceRow]] = defaultdict(list)
    for row in evidence_rows:
        report_rows_by_account[row.account_id].append(row)
        report_rows_by_transaction[row.transaction_id].append(row)

    accounts: list[AccountPeriodStatement] = []
    exclusions: list[LiquidPositionExclusion] = []
    included_opening: list[EvidenceAmount] = []
    included_closing: list[EvidenceAmount] = []
    for account_id in sorted(contributing_account_ids):
        account = account_by_id[account_id]
        account_rows = report_rows_by_account.get(account_id, [])
        opening_rows = [
            row for row in account_rows if row.posted_on < period_start
        ]
        period_rows = [
            row
            for row in account_rows
            if period_start <= row.posted_on <= period_end
        ]
        opening_evidence = _merge_evidence(
            *(row.evidence for row in opening_rows),
            close_evidence,
        )
        closing_evidence = _merge_evidence(
            *(row.evidence for row in account_rows),
            close_evidence,
        )
        opening_balance = _amount(
            f"account:{account_id}:opening_balance",
            sum(row.amount_cents for row in opening_rows),
            currency,
            opening_evidence,
        )
        ledger_closing = _amount(
            f"account:{account_id}:ledger_closing_balance",
            sum(row.amount_cents for row in account_rows),
            currency,
            closing_evidence,
        )

        inflows: list[FlowBucket] = []
        outflows: list[FlowBucket] = []
        by_flow_direction: dict[
            tuple[str, str], list[StatementEvidenceRow]
        ] = defaultdict(list)
        for row in period_rows:
            direction = "inflow" if row.amount_cents >= 0 else "outflow"
            by_flow_direction[(row.flow_kind, direction)].append(row)
        for (flow_kind, direction), flow_rows in sorted(
            by_flow_direction.items()
        ):
            bucket = FlowBucket(
                flow_kind=flow_kind,
                direction=direction,
                amount=_amount(
                    f"account:{account_id}:{direction}:{flow_kind}",
                    sum(row.amount_cents for row in flow_rows),
                    currency,
                    _merge_evidence(
                        *(row.evidence for row in flow_rows),
                        close_evidence,
                    ),
                ),
            )
            (inflows if direction == "inflow" else outflows).append(bucket)

        transfer_in_rows = [
            row
            for row in period_rows
            if row.flow_kind in _TRANSFER_KINDS and row.amount_cents > 0
        ]
        transfer_out_rows = [
            row
            for row in period_rows
            if row.flow_kind in _TRANSFER_KINDS and row.amount_cents < 0
        ]
        refund_rows = [
            row
            for row in period_rows
            if row.flow_kind in _OFFSET_KINDS and row.amount_cents > 0
        ]
        assertion = primary_assertion.get(account_id)
        asserted_amount: EvidenceAmount | None = None
        delta_amount: EvidenceAmount | None = None
        assertion_as_of: str | None = None
        assertion_delta: int | None = None
        if assertion is not None:
            assertion_as_of = str(assertion["asof_date"])
            ledger_at_assertion_rows = [
                row for row in account_rows if row.posted_on <= assertion_as_of
            ]
            assertion_evidence = _merge_evidence(
                *(
                    row.evidence
                    for row in ledger_at_assertion_rows
                ),
                EvidenceSet(
                    assertion_ids=(int(assertion["id"]),),
                    source_document_ids=(
                        ()
                        if assertion.get("source_document_id") is None
                        else (int(assertion["source_document_id"]),)
                    ),
                ),
                close_evidence,
            )
            asserted = int(assertion["asserted_cents"])
            ledger_at_assertion = sum(
                row.amount_cents for row in ledger_at_assertion_rows
            )
            assertion_delta = ledger_at_assertion - asserted
            asserted_amount = _amount(
                f"account:{account_id}:asserted_statement_closing_balance",
                asserted,
                currency,
                assertion_evidence,
            )
            delta_amount = _amount(
                f"account:{account_id}:assertion_delta",
                assertion_delta,
                currency,
                assertion_evidence,
            )

        if assertion is None:
            reconciliation_state = "missing_assertion"
        elif assertion_delta != 0:
            reconciliation_state = "assertion_exception"
        elif any(row.reconciliation_state != "cleared" for row in period_rows):
            reconciliation_state = "unreconciled"
        elif any(
            int(item["report_eligible"]) != 1
            for item in rows_by_account.get(account_id, [])
            if period_start <= str(item["posted_on"]) <= period_end
        ):
            reconciliation_state = "semantic_review"
        else:
            reconciliation_state = "reconciled"

        account_kind = str(account["kind"])
        if account_kind not in _LIQUID_ACCOUNT_KINDS:
            exclusion_reason = "unsupported_account_kind_v1"
        elif assertion is None:
            exclusion_reason = "missing_stated_as_of_balance"
        elif assertion_as_of != period_end:
            exclusion_reason = "assertion_not_at_period_end"
        elif reconciliation_state != "reconciled":
            exclusion_reason = reconciliation_state
        else:
            exclusion_reason = None
        liquid_included = exclusion_reason is None
        if liquid_included:
            included_opening.append(opening_balance)
            included_closing.append(ledger_closing)
        else:
            exclusions.append(
                LiquidPositionExclusion(
                    account_id=account_id,
                    account_name=str(account["name"]),
                    reason=str(exclusion_reason),
                    evidence=_merge_evidence(
                        opening_balance.evidence,
                        ledger_closing.evidence,
                        close_evidence,
                    ),
                )
            )

        accounts.append(
            AccountPeriodStatement(
                account_id=account_id,
                name=str(account["name"]),
                institution=str(account["institution"]),
                account_kind=account_kind,
                currency=currency,
                period_start=period_start,
                period_end=period_end,
                opening_balance=opening_balance,
                typed_inflows=tuple(inflows),
                typed_outflows=tuple(outflows),
                transfers_in=_amount(
                    f"account:{account_id}:transfers_in",
                    sum(row.amount_cents for row in transfer_in_rows),
                    currency,
                    _merge_evidence(
                        *(row.evidence for row in transfer_in_rows),
                        close_evidence,
                    ),
                ),
                transfers_out=_amount(
                    f"account:{account_id}:transfers_out",
                    sum(abs(row.amount_cents) for row in transfer_out_rows),
                    currency,
                    _merge_evidence(
                        *(row.evidence for row in transfer_out_rows),
                        close_evidence,
                    ),
                ),
                refunds=_amount(
                    f"account:{account_id}:refunds",
                    sum(row.amount_cents for row in refund_rows),
                    currency,
                    _merge_evidence(
                        *(row.evidence for row in refund_rows),
                        close_evidence,
                    ),
                ),
                debt_movement=_amount(
                    f"account:{account_id}:debt_movement",
                    (
                        ledger_closing.cents - opening_balance.cents
                        if account_kind == "credit"
                        else 0
                    ),
                    currency,
                    _merge_evidence(
                        *(row.evidence for row in period_rows),
                        close_evidence,
                    ),
                ),
                ledger_closing_balance=ledger_closing,
                asserted_statement_closing_balance=asserted_amount,
                assertion_delta=delta_amount,
                assertion_as_of=assertion_as_of,
                reconciliation_state=reconciliation_state,
                liquid_position_included=liquid_included,
                liquid_position_exclusion_reason=exclusion_reason,
            )
        )

    period_rows = [
        row
        for row in evidence_rows
        if period_start <= row.posted_on <= period_end
    ]
    income_rows = [
        row for row in period_rows if row.flow_kind in _INCOME_KINDS
    ]
    gross_out_rows = [
        row
        for row in period_rows
        if row.flow_kind in _OUTFLOW_KINDS and row.amount_cents < 0
    ]
    offset_rows = [
        row
        for row in period_rows
        if row.flow_kind in _OFFSET_KINDS
        and row.amount_cents > 0
        and (
            (relationship := offset_relationship.get(row.transaction_id))
            is not None
        )
        and any(
            target.flow_kind in _OUTFLOW_KINDS
            and target.amount_cents < 0
            for target in report_rows_by_transaction.get(
                int(relationship["target_transaction_id"]),
                (),
            )
        )
    ]
    period_transfer_rows = [
        row for row in period_rows if row.flow_kind in _TRANSFER_KINDS
    ]
    transfer_control_contributions = [
        (
            int(item["split_amount_cents"]),
            (
                row_by_split[int(item["transaction_split_id"])].evidence
                if int(item["transaction_split_id"]) in row_by_split
                else _row_evidence(
                    item,
                    statements=statements,
                    claims=claims,
                    relationship_ids=relationship_ids,
                    assertion_ids=assertion_ids,
                    close_evidence=close_evidence,
                )
            ),
        )
        for item in transfer_control_split_rows
    ]
    transfer_control_contributions.extend(
        (row.amount_cents, row.evidence)
        for row in period_transfer_rows
        if row.transaction_id not in paired_transfer_transaction_ids
    )
    adjustment_rows = [
        row for row in period_rows if row.flow_kind == "adjustment"
    ]
    unclassified_rows = [
        row
        for row in period_rows
        if row.flow_kind == "unknown"
        or (
            row.flow_kind not in _TRANSFER_KINDS
            and any(
                int(item["report_eligible"]) != 1
                for item in raw_by_transaction[row.transaction_id]
            )
        )
    ]
    external_rows = [
        row
        for row in period_rows
        if row.flow_kind not in _TRANSFER_KINDS
        and row.flow_kind != "opening"
    ]

    expense_contributions: list[
        tuple[
            str,
            int | None,
            str,
            str | None,
            int,
            EvidenceSet,
        ]
    ] = []
    for row in gross_out_rows:
        expense_contributions.append(
            (
                row.resolution_disposition,
                row.category_id,
                row.category_name,
                row.canonical_merchant,
                abs(row.amount_cents),
                row.evidence,
            )
        )
    for transaction_id, source_rows in sorted(report_rows_by_transaction.items()):
        current_offsets = [
            row
            for row in source_rows
            if period_start <= row.posted_on <= period_end
            and row.flow_kind in _OFFSET_KINDS
            and row.amount_cents > 0
        ]
        if not current_offsets:
            continue
        offset_cents = sum(row.amount_cents for row in current_offsets)
        relationship = offset_relationship.get(transaction_id)
        target_rows: list[StatementEvidenceRow] = []
        if relationship is not None:
            target_rows = [
                row
                for row in report_rows_by_transaction.get(
                    int(relationship["target_transaction_id"]),
                    (),
                )
                if row.flow_kind in _OUTFLOW_KINDS and row.amount_cents < 0
            ]
        allocations = _allocate_offset(offset_cents, target_rows)
        if not allocations:
            # The positive cash movement remains visible as unclassified
            # evidence, but without a valid relationship it cannot reduce an
            # expense bucket or net money-out total.
            continue
        for target, allocated in allocations:
            expense_contributions.append(
                (
                    target.resolution_disposition,
                    target.category_id,
                    target.category_name,
                    target.canonical_merchant,
                    -allocated,
                    _merge_evidence(
                        *(row.evidence for row in current_offsets),
                        target.evidence,
                        EvidenceSet(
                            relationship_ids=(int(relationship["id"]),)
                        ),
                        close_evidence,
                    ),
                )
            )

    grouped_expenses: dict[
        tuple[str, int | None, str, str | None],
        list[tuple[int, EvidenceSet]],
    ] = defaultdict(list)
    for disposition, category_id, category_name, merchant, cents, evidence in (
        expense_contributions
    ):
        grouped_expenses[
            (disposition, category_id, category_name, merchant)
        ].append((cents, evidence))
    expense_buckets: list[ExpenseBucket] = []
    for (
        disposition,
        category_id,
        category_name,
        merchant,
    ), contributions in sorted(
        grouped_expenses.items(),
        key=lambda item: (
            {"confirmed": 0, "human_approved": 1, "unresolved": 2}[
                item[0][0]
            ],
            item[0][1] if item[0][1] is not None else 2**63,
            item[0][2],
            item[0][3] or "",
        ),
    ):
        bucket_key = (
            f"expense:{disposition}:"
            f"{category_id if category_id is not None else 'none'}:"
            f"{merchant or 'all'}"
        )
        expense_buckets.append(
            ExpenseBucket(
                bucket_id=bucket_key,
                category_id=category_id,
                category_name=category_name,
                canonical_merchant=merchant,
                resolution_disposition=disposition,
                amount=_amount(
                    bucket_key,
                    sum(cents for cents, _ in contributions),
                    currency,
                    _merge_evidence(
                        *(evidence for _, evidence in contributions),
                        close_evidence,
                    ),
                ),
            )
        )

    def resolution_amount(disposition: str) -> EvidenceAmount:
        selected = [
            bucket
            for bucket in expense_buckets
            if bucket.resolution_disposition == disposition
        ]
        return _amount(
            f"expense_resolution:{disposition}",
            sum(bucket.amount.cents for bucket in selected),
            currency,
            _merge_evidence(
                *(bucket.amount.evidence for bucket in selected),
                close_evidence,
            ),
        )

    confirmed = resolution_amount("confirmed")
    human_approved = resolution_amount("human_approved")
    unresolved = resolution_amount("unresolved")
    resolved = _amount(
        "expense_resolution:resolved",
        confirmed.cents + human_approved.cents,
        currency,
        _merge_evidence(
            confirmed.evidence,
            human_approved.evidence,
            close_evidence,
        ),
    )

    income = _amount(
        "household:income",
        sum(row.amount_cents for row in income_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in income_rows),
            close_evidence,
        ),
    )
    gross_out = _amount(
        "household:gross_money_out",
        sum(abs(row.amount_cents) for row in gross_out_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in gross_out_rows),
            close_evidence,
        ),
    )
    refunds = _amount(
        "household:refunds",
        sum(row.amount_cents for row in offset_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in offset_rows),
            close_evidence,
        ),
    )
    net_out = _amount(
        "household:net_money_out",
        gross_out.cents - refunds.cents,
        currency,
        _merge_evidence(
            gross_out.evidence,
            refunds.evidence,
            close_evidence,
        ),
    )
    external = _amount(
        "household:external_cash_movement",
        sum(row.amount_cents for row in external_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in external_rows),
            close_evidence,
        ),
    )
    transfer_control = _amount(
        "household:transfer_neutrality_control",
        sum(cents for cents, _ in transfer_control_contributions),
        currency,
        _merge_evidence(
            *(
                evidence
                for _, evidence in transfer_control_contributions
            ),
            close_evidence,
        ),
    )
    adjustments = _amount(
        "household:adjustment_movement",
        sum(row.amount_cents for row in adjustment_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in adjustment_rows),
            close_evidence,
        ),
    )
    unclassified = _amount(
        "household:unclassified_movement",
        sum(row.amount_cents for row in unclassified_rows),
        currency,
        _merge_evidence(
            *(row.evidence for row in unclassified_rows),
            close_evidence,
        ),
    )
    opening_liquid = _amount(
        "household:opening_liquid_position",
        sum(item.cents for item in included_opening),
        currency,
        _merge_evidence(
            *(item.evidence for item in included_opening),
            close_evidence,
        ),
    )
    closing_liquid = _amount(
        "household:closing_liquid_position",
        sum(item.cents for item in included_closing),
        currency,
        _merge_evidence(
            *(item.evidence for item in included_closing),
            close_evidence,
        ),
    )
    if resolved.cents + unresolved.cents != net_out.cents:
        raise PeriodStatementError(
            "expense resolution buckets do not equal net money out"
        )

    return _with_digest(
        PeriodStatement(
            month=month,
            period_start=period_start,
            period_end=period_end,
            as_of_date=period_end,
            home_currency=currency,
            household=HouseholdTotals(
                income=income,
                gross_money_out=gross_out,
                refunds=refunds,
                net_money_out=net_out,
                external_cash_movement=external,
                transfer_neutrality_control=transfer_control,
                adjustment_movement=adjustments,
                unclassified_movement=unclassified,
                opening_liquid_position=opening_liquid,
                closing_liquid_position=closing_liquid,
            ),
            accounts=tuple(accounts),
            rows=tuple(evidence_rows),
            liquid_position_exclusions=tuple(exclusions),
            expense_resolution=ExpenseResolutionSummary(
                confirmed=confirmed,
                human_approved=human_approved,
                unresolved=unresolved,
                resolved=resolved,
                buckets=tuple(expense_buckets),
            ),
            close=close,
            automation_policy_version=str(policy["policy_version"]),
            automation_authority_mode=str(policy["authority_mode"]),
            automation_authority=dict(policy["assignments"]),
        )
    )


def build_period_statement(
    db_or_conn: str | Path | sqlite3.Connection,
    *,
    month: str,
    home_currency: str,
) -> PeriodStatement:
    """Return the canonical FN-148 model or the immutable frozen close copy."""
    period_start, period_end = _period_bounds(month)
    currency = str(home_currency).strip().upper()
    if not _CURRENCY.fullmatch(currency):
        raise PeriodStatementError("home_currency must be a three-letter ISO code")
    with _read_connection(db_or_conn) as conn:
        raw = repo_period_statements.load_period_evidence(
            conn,
            month=month,
            period_end=period_end,
        )
        if raw["close"]["state"] in {
            "clean_closed",
            "closed_with_exceptions",
        }:
            return _frozen_statement(raw["close"])
        return _build_live_statement(
            raw,
            month=month,
            period_start=period_start,
            period_end=period_end,
            currency=currency,
        )
