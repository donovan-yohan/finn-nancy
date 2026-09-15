"""The executable Phase-0 accounting contract and normalized flow vocabulary."""
from __future__ import annotations

import json
import re
import sqlite3
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class AccountingContractViolation(AssertionError):
    """One or more normalized-accounting invariants were violated."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class FlowKind(StrEnum):
    UNKNOWN = "unknown"
    PURCHASE = "purchase"
    INCOME = "income"
    REFUND = "refund"
    REIMBURSEMENT = "reimbursement"
    INTERNAL_TRANSFER = "internal_transfer"
    CARD_PAYMENT = "card_payment"
    FEE = "fee"
    INTEREST = "interest"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"
    OPENING = "opening"


class ReportBucket(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"
    OPENING = "opening"
    REVIEW = "review"


class ReconciliationDisposition(StrEnum):
    MATCH_EXISTING = "match_existing"
    PROMOTE = "promote"
    IGNORE = "ignore"
    REVIEW = "review"
    NOT_APPLICABLE = "not_applicable"


class CloseImpact(StrEnum):
    ELIGIBLE = "eligible"
    BLOCKING = "blocking"
    AUDITED_EXCEPTION = "audited_exception"


class Scenario(StrEnum):
    TRANSACTION = "transaction"
    EDITED_EXTRACTION = "edited_extraction"
    DUPLICATE_IMPORT = "duplicate_import"
    MISSING_STATEMENT = "missing_statement"
    CLOSED_PERIOD = "closed_period"
    CURRENCY_REVIEW = "currency_review"


class CapturePath(StrEnum):
    MANUAL_ENTRY = "manual_entry"
    RECEIPT_INGEST = "receipt_ingest"
    STATEMENT_IMPORT = "statement_import"


class Provenance(StrEnum):
    MANUAL = "manual"
    RECEIPT = "receipt"
    STATEMENT = "statement"
    SYSTEM = "system"
    HUMAN_EDIT = "human_edit"


class LedgerLeg(BaseModel):
    account_ref: str
    amount_cents: int


class ExpectedEffect(BaseModel):
    ledger_transaction_count: int
    report_bucket: ReportBucket
    income_cents: int = 0
    spending_cents: int = 0
    external_cash_cents: int = 0
    reconciliation: ReconciliationDisposition
    close_impact: CloseImpact


class ContractCase(BaseModel):
    id: str
    scenario: Scenario
    posted_on: str = ""
    flow_kind: FlowKind | None = None
    currency: str = ""
    provenance: list[Provenance] = Field(default_factory=list)
    paths: list[CapturePath] = Field(default_factory=list)
    ledger_legs: list[LedgerLeg] = Field(default_factory=list)
    relationship: str = ""
    original_amount_cents: int | None = None
    expected: ExpectedEffect


class ContractPack(BaseModel):
    version: str
    month: str
    home_currency: str
    cases: list[ContractCase]


_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


def load_contract_pack(path: str | Path) -> ContractPack:
    """Load a versioned, synthetic contract pack from JSON."""
    return ContractPack.model_validate(json.loads(Path(path).read_text()))


def currency_review_reason(currency: str | None, home_currency: str) -> str | None:
    """Return the fail-closed reason for a non-home/ambiguous currency.

    V1 has one home currency. A blank, malformed, or non-home ISO code may be retained
    in staging evidence, but cannot be promoted into reports.
    """
    value = (currency or "").strip().upper()
    home = (home_currency or "").strip().upper()
    if not _ISO_CURRENCY.fullmatch(value):
        return "ambiguous_currency"
    if value != home:
        return "unsupported_currency"
    return None


def _derived_effect(case: ContractCase) -> tuple[ReportBucket, int, int, int]:
    """Derive (bucket, income, spending, external cash) from normalized semantics."""
    amounts = [leg.amount_cents for leg in case.ledger_legs]
    flow = case.flow_kind
    if flow in (FlowKind.INCOME, FlowKind.INTEREST):
        return ReportBucket.INCOME, sum(amounts), 0, sum(amounts)
    if flow in (FlowKind.PURCHASE, FlowKind.FEE):
        return ReportBucket.EXPENSE, 0, -sum(amounts), sum(amounts)
    if flow in (FlowKind.REFUND, FlowKind.REIMBURSEMENT, FlowKind.REVERSAL):
        return ReportBucket.EXPENSE, 0, -sum(amounts), sum(amounts)
    if flow in (FlowKind.INTERNAL_TRANSFER, FlowKind.CARD_PAYMENT):
        return ReportBucket.TRANSFER, 0, 0, 0
    if flow == FlowKind.ADJUSTMENT:
        return ReportBucket.ADJUSTMENT, 0, 0, 0
    if flow == FlowKind.OPENING:
        return ReportBucket.OPENING, 0, 0, 0
    return ReportBucket.REVIEW, 0, 0, 0


def _case_violations(case: ContractCase, *, month: str, home_currency: str) -> list[str]:
    errors: list[str] = []
    prefix = f"case {case.id}"
    reason = currency_review_reason(case.currency, home_currency)

    if case.posted_on and (
        not _ISO_DATE.fullmatch(case.posted_on) or not case.posted_on.startswith(month + "-")
    ):
        errors.append(f"{prefix}: posted_on must be an ISO date in golden month {month}")

    if case.scenario == Scenario.CURRENCY_REVIEW:
        if reason is None:
            errors.append(f"{prefix}: currency-review case must be foreign or ambiguous")
        if case.ledger_legs or case.expected.ledger_transaction_count != 0:
            errors.append(f"{prefix}: unsupported currency must not reach the ledger")
        if case.expected.reconciliation != ReconciliationDisposition.REVIEW:
            errors.append(f"{prefix}: unsupported currency must reconcile to review")
        if case.expected.close_impact != CloseImpact.BLOCKING:
            errors.append(f"{prefix}: unsupported currency must block clean close")
        if case.expected.report_bucket != ReportBucket.REVIEW:
            errors.append(f"{prefix}: unsupported currency must not enter a report bucket")
        if (
            case.expected.income_cents
            or case.expected.spending_cents
            or case.expected.external_cash_cents
        ):
            errors.append(f"{prefix}: unsupported currency must have zero aggregate effect")
        return errors

    if reason is not None:
        errors.append(f"{prefix}: home-currency case has {reason}")

    if case.scenario in (Scenario.MISSING_STATEMENT, Scenario.CLOSED_PERIOD):
        if case.ledger_legs or case.expected.ledger_transaction_count != 0:
            errors.append(f"{prefix}: control case must not create a ledger transaction")
        if case.expected.close_impact != CloseImpact.BLOCKING:
            errors.append(f"{prefix}: control case must block clean close")
        if case.expected.report_bucket != ReportBucket.REVIEW:
            errors.append(f"{prefix}: control case must stay outside report aggregates")
        if (
            case.expected.income_cents
            or case.expected.spending_cents
            or case.expected.external_cash_cents
        ):
            errors.append(f"{prefix}: control case must have zero aggregate effect")
        if (
            case.scenario == Scenario.MISSING_STATEMENT
            and case.expected.reconciliation != ReconciliationDisposition.REVIEW
        ):
            errors.append(f"{prefix}: missing statement must remain a review blocker")
        return errors

    if case.flow_kind is None:
        errors.append(f"{prefix}: transaction case requires flow_kind")
        return errors

    amounts = [leg.amount_cents for leg in case.ledger_legs]
    if any(amount == 0 for amount in amounts):
        errors.append(f"{prefix}: zero-value ledger legs are not normalized events")
    if len(amounts) != case.expected.ledger_transaction_count:
        errors.append(
            f"{prefix}: expected {case.expected.ledger_transaction_count} ledger transaction(s), "
            f"fixture has {len(amounts)}"
        )

    if case.flow_kind in (FlowKind.PURCHASE, FlowKind.FEE) and (
        len(amounts) != 1 or amounts[0] >= 0
    ):
        errors.append(f"{prefix}: purchase/fee must have one negative ledger leg")
    if case.flow_kind in (FlowKind.INCOME, FlowKind.INTEREST) and (
        len(amounts) != 1 or amounts[0] <= 0
    ):
        errors.append(f"{prefix}: income/interest must have one positive ledger leg")
    if case.flow_kind in (FlowKind.REFUND, FlowKind.REIMBURSEMENT, FlowKind.REVERSAL) and (
        len(amounts) != 1 or amounts[0] <= 0
    ):
        errors.append(f"{prefix}: refund/reimbursement/reversal must have one positive ledger leg")
    if case.flow_kind in (FlowKind.INTERNAL_TRANSFER, FlowKind.CARD_PAYMENT):
        if len(amounts) != 2 or sum(amounts) != 0 or len({leg.account_ref for leg in case.ledger_legs}) != 2:
            errors.append(f"{prefix}: transfer/payment requires equal-and-opposite legs on two accounts")
        if not case.relationship.startswith("pair:"):
            errors.append(f"{prefix}: transfer/payment requires a pair relationship")
    if case.flow_kind in (FlowKind.REFUND, FlowKind.REIMBURSEMENT, FlowKind.REVERSAL):
        if not case.relationship:
            errors.append(f"{prefix}: {case.flow_kind.value} requires provenance relationship")

    bucket, income, spending, external = _derived_effect(case)
    expected_tuple = (
        case.expected.report_bucket,
        case.expected.income_cents,
        case.expected.spending_cents,
        case.expected.external_cash_cents,
    )
    if (bucket, income, spending, external) != expected_tuple:
        errors.append(
            f"{prefix}: derived report effect {(bucket.value, income, spending, external)} "
            f"!= expected {(case.expected.report_bucket.value, *expected_tuple[1:])}"
        )

    if case.scenario == Scenario.EDITED_EXTRACTION:
        if case.original_amount_cents is None or len(amounts) != 1:
            errors.append(f"{prefix}: edited extraction requires original and one corrected amount")
        elif case.original_amount_cents == amounts[0]:
            errors.append(f"{prefix}: edited extraction must retain a distinct original value")
        if Provenance.HUMAN_EDIT not in case.provenance:
            errors.append(f"{prefix}: edited extraction must retain human-edit provenance")

    if case.scenario == Scenario.DUPLICATE_IMPORT:
        if CapturePath.STATEMENT_IMPORT not in case.paths:
            errors.append(f"{prefix}: duplicate-import case must exercise statement import")
        if case.expected.ledger_transaction_count != 1:
            errors.append(f"{prefix}: duplicate import must have exactly one ledger effect")

    for path, provenance in (
        (CapturePath.MANUAL_ENTRY, Provenance.MANUAL),
        (CapturePath.RECEIPT_INGEST, Provenance.RECEIPT),
        (CapturePath.STATEMENT_IMPORT, Provenance.STATEMENT),
    ):
        if path in case.paths and provenance not in case.provenance:
            errors.append(f"{prefix}: {path.value} requires {provenance.value} provenance")

    return errors


def assert_contract_pack(pack: ContractPack) -> None:
    """Validate the normalized fixture contract independently of the current schema."""
    errors: list[str] = []
    if not pack.version.strip():
        errors.append("pack version is required")
    if not _ISO_MONTH.fullmatch(pack.month):
        errors.append("pack month must be YYYY-MM")
    if not _ISO_CURRENCY.fullmatch(pack.home_currency.strip().upper()):
        errors.append("pack home_currency must be a three-letter ISO code")

    ids = [case.id for case in pack.cases]
    if len(ids) != len(set(ids)):
        errors.append("case ids must be unique")
    required_paths = set(CapturePath)
    observed_paths = {path for case in pack.cases for path in case.paths}
    if not required_paths <= observed_paths:
        missing = ", ".join(sorted(path.value for path in required_paths - observed_paths))
        errors.append(f"golden pack does not exercise path(s): {missing}")

    id_set = set(ids)
    for case in pack.cases:
        errors.extend(_case_violations(case, month=pack.month, home_currency=pack.home_currency))
        if (
            case.flow_kind in (FlowKind.REFUND, FlowKind.REIMBURSEMENT, FlowKind.REVERSAL)
            and case.relationship not in id_set
        ):
            errors.append(f"case {case.id}: relationship target {case.relationship!r} is absent")

    if errors:
        raise AccountingContractViolation(errors)


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def ledger_invariant_violations(
    conn: sqlite3.Connection,
    *,
    transaction_ids: list[int],
    home_currency: str,
) -> list[str]:
    """Return central row-level ledger/provenance violations for the selected rows."""
    if not transaction_ids:
        return []
    errors: list[str] = []
    ids = sorted(set(int(value) for value in transaction_ids))
    rows = conn.execute(
        f"""SELECT t.*, a.name AS account_ref, a.currency AS account_currency
            FROM transactions t
            JOIN accounts a ON a.id=t.account_id
            WHERE t.id IN ({_placeholders(ids)})
            ORDER BY t.id""",
        ids,
    ).fetchall()
    found = {int(row["id"]) for row in rows}
    for missing in sorted(set(ids) - found):
        errors.append(f"transaction {missing}: missing")

    for row in rows:
        txn_id = int(row["id"])
        splits = conn.execute(
            "SELECT amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY id",
            (txn_id,),
        ).fetchall()
        if not splits:
            errors.append(f"transaction {txn_id}: has no splits")
        elif sum(int(split["amount_cents"]) for split in splits) != int(row["amount_cents"]):
            errors.append(f"transaction {txn_id}: splits do not equal transaction amount")

        reason = currency_review_reason(row["account_currency"], home_currency)
        if reason is not None:
            errors.append(f"transaction {txn_id}: account has {reason}")

        if row["source"] == "receipt":
            doc = conn.execute(
                "SELECT kind FROM source_documents WHERE id=?", (row["source_document_id"],)
            ).fetchone()
            if doc is None or doc["kind"] != "receipt":
                errors.append(f"transaction {txn_id}: receipt provenance is missing")
        elif row["source"] == "statement":
            linked = conn.execute(
                """SELECT currency FROM statement_lines
                   WHERE matched_transaction_id=? AND source_document_id=?
                     AND row_hash=? AND match_status='promoted'
                     AND review_disposition='active'""",
                (txn_id, row["source_document_id"], row["external_id"]),
            ).fetchone()
            if linked is None:
                errors.append(f"transaction {txn_id}: statement provenance is missing")
            else:
                reason = currency_review_reason(linked["currency"], home_currency)
                if reason is not None:
                    errors.append(f"transaction {txn_id}: statement has {reason}")

        linked_lines = conn.execute(
            """SELECT currency FROM statement_lines
               WHERE matched_transaction_id=? AND review_disposition='active'""",
            (txn_id,),
        ).fetchall()
        for linked_line in linked_lines:
            reason = currency_review_reason(linked_line["currency"], home_currency)
            if reason is not None:
                errors.append(f"transaction {txn_id}: corroborating statement has {reason}")

    reused = conn.execute(
        f"""SELECT matched_transaction_id, COUNT(*) AS n
            FROM statement_lines
            WHERE matched_transaction_id IN ({_placeholders(ids)})
              AND review_disposition='active'
            GROUP BY matched_transaction_id
            HAVING COUNT(*) > 1""",
        ids,
    ).fetchall()
    for row in reused:
        errors.append(
            f"transaction {int(row['matched_transaction_id'])}: claimed by {int(row['n'])} statement lines"
        )
    return errors


def assert_ledger_invariants(
    conn: sqlite3.Connection,
    *,
    transaction_ids: list[int],
    home_currency: str,
) -> None:
    errors = ledger_invariant_violations(
        conn, transaction_ids=transaction_ids, home_currency=home_currency
    )
    if errors:
        raise AccountingContractViolation(errors)


def assert_golden_month(
    conn: sqlite3.Connection,
    pack: ContractPack,
    transaction_ids_by_case: dict[str, list[int]],
) -> dict[str, int]:
    """Assert fixture expectations against one isolated, synthetic ledger month.

    The isolated DB must contain no unassigned golden-month transaction. This exact-set
    check is what makes a receipt plus its matched statement fail if it is booked twice.
    """
    assert_contract_pack(pack)
    errors: list[str] = []
    expected_case_ids = {case.id for case in pack.cases}
    unknown = set(transaction_ids_by_case) - expected_case_ids
    if unknown:
        errors.append(f"unknown transaction case mapping(s): {', '.join(sorted(unknown))}")

    assigned: list[int] = []
    observed_external_cash_cents = 0
    for case in pack.cases:
        ids = [int(value) for value in transaction_ids_by_case.get(case.id, [])]
        assigned.extend(ids)
        if len(ids) != case.expected.ledger_transaction_count:
            errors.append(
                f"case {case.id}: observed {len(ids)} transaction(s), "
                f"expected {case.expected.ledger_transaction_count}"
            )
            continue
        if not ids:
            continue
        rows = conn.execute(
            f"""SELECT t.id, t.amount_cents, t.source, t.flow_kind,
                       a.name AS account_ref
                FROM transactions t JOIN accounts a ON a.id=t.account_id
                WHERE t.id IN ({_placeholders(ids)})""",
            ids,
        ).fetchall()
        observed = sorted((row["account_ref"], int(row["amount_cents"])) for row in rows)
        expected = sorted((leg.account_ref, leg.amount_cents) for leg in case.ledger_legs)
        if observed != expected:
            errors.append(f"case {case.id}: observed ledger legs {observed} != expected {expected}")
        observed_flows = {row["flow_kind"] for row in rows}
        expected_flow = case.flow_kind.value if case.flow_kind is not None else None
        if observed_flows != {expected_flow}:
            errors.append(
                f"case {case.id}: persisted flow_kind(s) {sorted(observed_flows)} "
                f"!= expected {[expected_flow]}"
            )

        if case.flow_kind in (FlowKind.INTERNAL_TRANSFER, FlowKind.CARD_PAYMENT):
            pair = conn.execute(
                f"""SELECT source_transaction_id, target_transaction_id
                    FROM transaction_relationships
                    WHERE status='active' AND relationship_kind='transfer_pair'
                      AND source_transaction_id IN ({_placeholders(ids)})
                      AND target_transaction_id IN ({_placeholders(ids)})""",
                [*ids, *ids],
            ).fetchall()
            endpoints = {
                frozenset(
                    (int(edge["source_transaction_id"]), int(edge["target_transaction_id"]))
                )
                for edge in pair
            }
            if endpoints != {frozenset(ids)}:
                errors.append(f"case {case.id}: persisted transfer pair is missing or invalid")
        elif case.flow_kind in (
            FlowKind.REFUND,
            FlowKind.REIMBURSEMENT,
            FlowKind.REVERSAL,
        ):
            relationship_kind = {
                FlowKind.REFUND: "refund_of",
                FlowKind.REIMBURSEMENT: "reimbursement_for",
                FlowKind.REVERSAL: "reversal_of",
            }[case.flow_kind]
            target_ids = [
                int(value) for value in transaction_ids_by_case.get(case.relationship, [])
            ]
            if not target_ids:
                errors.append(
                    f"case {case.id}: relationship target {case.relationship!r} is missing"
                )
            else:
                edge = conn.execute(
                    f"""SELECT 1
                        FROM transaction_relationships
                        WHERE status='active' AND relationship_kind=?
                          AND source_transaction_id IN ({_placeholders(ids)})
                          AND target_transaction_id IN ({_placeholders(target_ids)})
                        LIMIT 1""",
                    [relationship_kind, *ids, *target_ids],
                ).fetchone()
                if edge is None:
                    errors.append(
                        f"case {case.id}: persisted {relationship_kind} provenance is missing"
                    )

        expected_category_kind = {
            ReportBucket.INCOME: "income",
            ReportBucket.EXPENSE: "expense",
            ReportBucket.TRANSFER: "transfer",
            ReportBucket.OPENING: "transfer",
            ReportBucket.ADJUSTMENT: "transfer",
        }.get(case.expected.report_bucket)
        if expected_category_kind is not None:
            category_kinds = {
                row["kind"]
                for row in conn.execute(
                    f"""SELECT DISTINCT c.kind
                        FROM transaction_splits s
                        JOIN categories c ON c.id=s.category_id
                        WHERE s.transaction_id IN ({_placeholders(ids)})""",
                    ids,
                ).fetchall()
            }
            if category_kinds != {expected_category_kind}:
                errors.append(
                    f"case {case.id}: report category kind(s) {sorted(category_kinds)} "
                    f"!= expected {[expected_category_kind]}"
                )

        if case.flow_kind in (
            FlowKind.PURCHASE,
            FlowKind.INCOME,
            FlowKind.INTEREST,
            FlowKind.REFUND,
            FlowKind.REIMBURSEMENT,
            FlowKind.FEE,
            FlowKind.REVERSAL,
        ):
            observed_external_cash_cents += sum(int(row["amount_cents"]) for row in rows)

        if case.expected.reconciliation == ReconciliationDisposition.MATCH_EXISTING:
            if len(rows) != 1:
                errors.append(f"case {case.id}: match-existing must retain one ledger row")
            else:
                if rows[0]["source"] != "receipt":
                    errors.append(f"case {case.id}: match-existing must preserve receipt provenance")
                linked = conn.execute(
                    """SELECT 1 FROM statement_lines
                       WHERE matched_transaction_id=? AND match_status='matched'
                         AND review_disposition='active'""",
                    (int(rows[0]["id"]),),
                ).fetchone()
                if linked is None:
                    errors.append(f"case {case.id}: statement did not corroborate existing row")
        elif case.expected.reconciliation == ReconciliationDisposition.PROMOTE:
            if any(row["source"] != "statement" for row in rows):
                errors.append(f"case {case.id}: promoted rows must preserve statement provenance")
        elif case.scenario == Scenario.EDITED_EXTRACTION:
            if any(row["source"] != "receipt" for row in rows):
                errors.append(f"case {case.id}: edited extraction must preserve receipt provenance")
        elif (
            case.paths == [CapturePath.MANUAL_ENTRY]
            and case.flow_kind not in (FlowKind.OPENING, FlowKind.ADJUSTMENT)
            and any(row["source"] != "manual" for row in rows)
        ):
            errors.append(f"case {case.id}: manual entry must preserve manual provenance")
        elif case.flow_kind == FlowKind.OPENING and any(row["source"] != "opening" for row in rows):
            errors.append(f"case {case.id}: opening balance must preserve opening provenance")
        elif case.flow_kind == FlowKind.ADJUSTMENT and any(
            row["source"] != "adjustment" for row in rows
        ):
            errors.append(f"case {case.id}: adjustment must preserve adjustment provenance")

    if len(assigned) != len(set(assigned)):
        errors.append("a transaction is assigned to more than one contract case")

    actual_month_ids = {
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM transactions WHERE substr(posted_on,1,7)=?", (pack.month,)
        ).fetchall()
    }
    if actual_month_ids != set(assigned):
        extra = sorted(actual_month_ids - set(assigned))
        missing = sorted(set(assigned) - actual_month_ids)
        if extra:
            errors.append(f"golden month has unassigned transaction(s): {extra}")
        if missing:
            errors.append(f"assigned transaction(s) are outside golden month: {missing}")

    errors.extend(
        ledger_invariant_violations(
            conn, transaction_ids=assigned, home_currency=pack.home_currency
        )
    )

    expected_income_cents = sum(case.expected.income_cents for case in pack.cases)
    expected_spending_cents = sum(case.expected.spending_cents for case in pack.cases)
    expected_external_cash_cents = sum(
        case.expected.external_cash_cents for case in pack.cases
    )
    report = conn.execute(
        """SELECT income_cents, expense_cents, net_cents
           FROM v_cashflow_monthly WHERE month=?""",
        (pack.month,),
    ).fetchone()
    actual_income_cents = int(report["income_cents"]) if report is not None else 0
    actual_spending_cents = int(report["expense_cents"]) if report is not None else 0
    actual_net_cents = int(report["net_cents"]) if report is not None else 0
    if actual_income_cents != expected_income_cents:
        errors.append(
            f"cashflow report income {actual_income_cents} != expected {expected_income_cents}"
        )
    if actual_spending_cents != expected_spending_cents:
        errors.append(
            f"cashflow report spending {actual_spending_cents} != expected {expected_spending_cents}"
        )
    if actual_net_cents != expected_external_cash_cents:
        errors.append(
            f"cashflow report net {actual_net_cents} != expected {expected_external_cash_cents}"
        )
    if observed_external_cash_cents != expected_external_cash_cents:
        errors.append(
            f"observed external cash {observed_external_cash_cents} "
            f"!= expected {expected_external_cash_cents}"
        )

    # Exercise the production close read-model: any contract-level blocker must keep
    # the month out of the all-complete state. FN-141 will replace the conservative
    # no-statement rule with an explicit expected account-period matrix.
    from ..close import checklist

    contract_blocker_count = sum(
        case.expected.close_impact == CloseImpact.BLOCKING for case in pack.cases
    )
    close_check = checklist.build_checklist(conn, pack.month)
    if contract_blocker_count and close_check["all_complete"]:
        errors.append("production close checklist reports complete despite contract blockers")

    if errors:
        raise AccountingContractViolation(errors)

    return {
        "income_cents": actual_income_cents,
        "spending_cents": actual_spending_cents,
        "external_cash_cents": observed_external_cash_cents,
        "transaction_count": len(assigned),
        "close_blocker_count": contract_blocker_count,
    }
