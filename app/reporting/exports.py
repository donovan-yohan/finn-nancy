"""Deterministic CSV and real-PDF renderers for the shared FN-148 model."""
from __future__ import annotations

import csv
import io
import textwrap
from collections.abc import Mapping

import fitz

from .models import EvidenceAmount, EvidenceSet, PeriodStatement


class PeriodStatementExportError(ValueError):
    """Base error for a period-statement export that cannot be rendered."""


class UnsupportedPdfGlyphError(PeriodStatementExportError):
    """Raised when the bundled PDF font cannot represent report text."""

    def __init__(self, codepoints: tuple[int, ...]):
        self.codepoints = codepoints
        preview = ", ".join(f"U+{codepoint:04X}" for codepoint in codepoints)
        super().__init__(
            "PDF export cannot represent one or more characters with the "
            f"bundled font: {preview}"
        )


_CSV_FIELDS = (
    "record_type",
    "line_key",
    "schema_version",
    "month",
    "close_state",
    "account_id",
    "account_name",
    "account_kind",
    "key",
    "label",
    "cents",
    "currency",
    "posted_on",
    "flow_kind",
    "category_id",
    "category_name",
    "canonical_merchant",
    "source",
    "reconciliation_state",
    "period_role",
    "resolution_disposition",
    "row_ids",
    "transaction_ids",
    "transaction_split_ids",
    "statement_line_ids",
    "source_document_ids",
    "source_anchor_ids",
    "merchant_entity_ids",
    "merchant_pattern_ids",
    "merchant_claim_ids",
    "category_claim_ids",
    "resolution_event_ids",
    "category_ids",
    "relationship_ids",
    "assertion_ids",
    "exception_ids",
    "acknowledgement_ids",
    "close_snapshot_ids",
    "report_digest",
)


def _joined(values: tuple[object, ...]) -> str:
    return "|".join(str(value) for value in values)


def _evidence_columns(evidence: EvidenceSet) -> dict[str, str]:
    return {
        "row_ids": _joined(evidence.row_ids),
        "transaction_ids": _joined(evidence.transaction_ids),
        "transaction_split_ids": _joined(evidence.transaction_split_ids),
        "statement_line_ids": _joined(evidence.statement_line_ids),
        "source_document_ids": _joined(evidence.source_document_ids),
        "source_anchor_ids": _joined(evidence.source_anchor_ids),
        "merchant_entity_ids": _joined(evidence.merchant_entity_ids),
        "merchant_pattern_ids": _joined(evidence.merchant_pattern_ids),
        "merchant_claim_ids": _joined(
            evidence.canonical_merchant_claim_ids
        ),
        "category_claim_ids": _joined(evidence.category_claim_ids),
        "resolution_event_ids": _joined(evidence.resolution_event_ids),
        "category_ids": _joined(evidence.category_ids),
        "relationship_ids": _joined(evidence.relationship_ids),
        "assertion_ids": _joined(evidence.assertion_ids),
        "exception_ids": _joined(evidence.exception_ids),
        "acknowledgement_ids": _joined(evidence.acknowledgement_ids),
        "close_snapshot_ids": _joined(evidence.close_snapshot_ids),
    }


def _amount_row(
    *,
    record_type: str,
    key: str,
    label: str,
    amount: EvidenceAmount,
    report_digest: str,
    account_id: int | None = None,
    resolution_disposition: str = "",
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "record_type": record_type,
        "line_key": amount.line_key,
        "account_id": "" if account_id is None else account_id,
        "key": key,
        "label": label,
        "cents": amount.cents,
        "currency": amount.currency,
        "resolution_disposition": resolution_disposition,
        **dict(details or {}),
        **_evidence_columns(amount.evidence),
        "report_digest": report_digest,
    }


def _spreadsheet_safe(value: object) -> object:
    """Keep untrusted text inert when a CSV is opened as a spreadsheet."""
    if not isinstance(value, str) or not value:
        return value
    candidate = value.lstrip(" \t\r\n")
    if value[0] in "\t\r\n" or (
        candidate and candidate[0] in ("=", "+", "-", "@")
    ):
        return f"'{value}"
    return value


def _write_csv_row(
    writer: csv.DictWriter,
    row: Mapping[str, object],
) -> None:
    writer.writerow(
        {key: _spreadsheet_safe(value) for key, value in row.items()}
    )


def render_period_statement_csv(statement: PeriodStatement) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=_CSV_FIELDS,
        lineterminator="\r\n",
    )
    writer.writeheader()
    _write_csv_row(
        writer,
        {
            "record_type": "metadata",
            "line_key": "report:metadata",
            "schema_version": statement.schema_version,
            "month": statement.month,
            "close_state": statement.close.state,
            "key": statement.month,
            "label": statement.schema_version,
            "currency": statement.home_currency,
            "report_digest": statement.report_digest,
            "close_snapshot_ids": (
                ""
                if statement.close.snapshot_id is None
                else str(statement.close.snapshot_id)
            ),
        }
    )
    household_lines = (
        ("income", "Income"),
        ("gross_money_out", "Gross money out"),
        ("refunds", "Refunds"),
        ("net_money_out", "Net money out"),
        ("external_cash_movement", "External cash movement"),
        ("transfer_neutrality_control", "Transfer neutrality control"),
        ("adjustment_movement", "Adjustment movement"),
        ("unclassified_movement", "Unclassified movement"),
        ("opening_liquid_position", "Opening liquid position"),
        ("closing_liquid_position", "Closing liquid position"),
    )
    for key, label in household_lines:
        amount = getattr(statement.household, key)
        _write_csv_row(
            writer,
            _amount_row(
                record_type="household_total",
                key=key,
                label=label,
                amount=amount,
                report_digest=statement.report_digest,
            )
        )
    for key, label in (
        ("confirmed", "Confirmed expense"),
        ("human_approved", "Human-approved expense"),
        ("unresolved", "Unresolved expense"),
        ("resolved", "Resolved expense"),
    ):
        _write_csv_row(
            writer,
            _amount_row(
                record_type="expense_resolution_total",
                key=key,
                label=label,
                amount=getattr(statement.expense_resolution, key),
                report_digest=statement.report_digest,
                resolution_disposition=key,
            )
        )
    for bucket in statement.expense_resolution.buckets:
        label = bucket.category_name
        if bucket.canonical_merchant:
            label += f" · {bucket.canonical_merchant}"
        _write_csv_row(
            writer,
            _amount_row(
                record_type="expense_bucket",
                key=bucket.bucket_id,
                label=label,
                amount=bucket.amount,
                report_digest=statement.report_digest,
                resolution_disposition=bucket.resolution_disposition,
                details={
                    "category_id": (
                        ""
                        if bucket.category_id is None
                        else bucket.category_id
                    ),
                    "category_name": bucket.category_name,
                    "canonical_merchant": bucket.canonical_merchant or "",
                },
            )
        )
    for account in statement.accounts:
        account_lines = [
            ("opening_balance", "Opening balance", account.opening_balance),
            ("transfers_in", "Transfers in", account.transfers_in),
            ("transfers_out", "Transfers out", account.transfers_out),
            ("refunds", "Refunds", account.refunds),
            ("debt_movement", "Debt movement", account.debt_movement),
            (
                "ledger_closing_balance",
                "Ledger closing balance",
                account.ledger_closing_balance,
            ),
        ]
        if account.asserted_statement_closing_balance is not None:
            account_lines.append(
                (
                    "asserted_statement_closing_balance",
                    "Asserted statement closing balance",
                    account.asserted_statement_closing_balance,
                )
            )
        if account.assertion_delta is not None:
            account_lines.append(
                ("assertion_delta", "Assertion delta", account.assertion_delta)
            )
        for key, label, amount in account_lines:
            _write_csv_row(
                writer,
                _amount_row(
                    record_type="account_total",
                    key=key,
                    label=f"{account.name} · {label}",
                    amount=amount,
                    report_digest=statement.report_digest,
                    account_id=account.account_id,
                    details={
                        "account_name": account.name,
                        "account_kind": account.account_kind,
                        "reconciliation_state": (
                            account.reconciliation_state
                        ),
                    },
                )
            )
        for flow in (*account.typed_inflows, *account.typed_outflows):
            _write_csv_row(
                writer,
                _amount_row(
                    record_type="account_flow",
                    key=f"{flow.direction}:{flow.flow_kind}",
                    label=f"{account.name} · {flow.flow_kind}",
                    amount=flow.amount,
                    report_digest=statement.report_digest,
                    account_id=account.account_id,
                    details={
                        "account_name": account.name,
                        "account_kind": account.account_kind,
                        "flow_kind": flow.flow_kind,
                        "reconciliation_state": (
                            account.reconciliation_state
                        ),
                    },
                )
            )
    accounts_by_id = {
        account.account_id: account for account in statement.accounts
    }
    for row in statement.rows:
        account = accounts_by_id[row.account_id]
        _write_csv_row(
            writer,
            {
                "record_type": "evidence_row",
                "line_key": row.row_id,
                "account_id": row.account_id,
                "account_name": account.name,
                "account_kind": account.account_kind,
                "key": row.flow_kind,
                "label": row.description,
                "cents": row.amount_cents,
                "currency": statement.home_currency,
                "posted_on": row.posted_on,
                "flow_kind": row.flow_kind,
                "category_id": row.category_id,
                "category_name": row.category_name,
                "canonical_merchant": row.canonical_merchant or "",
                "source": row.source,
                "reconciliation_state": row.reconciliation_state,
                "period_role": row.period_role,
                "resolution_disposition": row.resolution_disposition,
                **_evidence_columns(row.evidence),
                "report_digest": statement.report_digest,
            }
        )
    return output.getvalue().encode("utf-8")


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}${value // 100:,}.{value % 100:02d}"


def _safe(value: object) -> str:
    return " ".join(str(value).replace("\x00", "").split())


def _evidence_pdf(evidence: EvidenceSet) -> str:
    fields = (
        ("rows", evidence.row_ids),
        ("txns", evidence.transaction_ids),
        ("splits", evidence.transaction_split_ids),
        ("statement-lines", evidence.statement_line_ids),
        ("documents", evidence.source_document_ids),
        ("source-anchors", evidence.source_anchor_ids),
        ("merchant-entities", evidence.merchant_entity_ids),
        ("merchant-patterns", evidence.merchant_pattern_ids),
        ("merchant-claims", evidence.canonical_merchant_claim_ids),
        ("category-claims", evidence.category_claim_ids),
        ("resolution-events", evidence.resolution_event_ids),
        ("categories", evidence.category_ids),
        ("relationships", evidence.relationship_ids),
        ("assertions", evidence.assertion_ids),
        ("exceptions", evidence.exception_ids),
        ("acknowledgements", evidence.acknowledgement_ids),
        ("snapshots", evidence.close_snapshot_ids),
    )
    return "; ".join(
        f"{label}={_joined(values) or 'none'}" for label, values in fields
    )


def _amount_pdf(label: str, amount: EvidenceAmount) -> str:
    return (
        f"{label}: {_money(amount.cents)} [{amount.line_key}]; "
        f"{_evidence_pdf(amount.evidence)}"
    )


def _pdf_lines(statement: PeriodStatement) -> list[str]:
    lines = [
        f"Schema: {statement.schema_version}",
        f"Currency: {statement.home_currency}",
        (
            "Automation: "
            f"{statement.automation_policy_version}; "
            f"mode={statement.automation_authority_mode}; "
            + "; ".join(
                f"{key}={str(value).lower()}"
                for key, value in sorted(
                    statement.automation_authority.items()
                )
            )
        ),
        "",
        "Household totals",
        _amount_pdf("Income", statement.household.income),
        _amount_pdf("Gross money out", statement.household.gross_money_out),
        _amount_pdf("Refunds", statement.household.refunds),
        _amount_pdf("Net money out", statement.household.net_money_out),
        _amount_pdf(
            "External cash movement",
            statement.household.external_cash_movement,
        ),
        _amount_pdf(
            "Transfer neutrality control",
            statement.household.transfer_neutrality_control,
        ),
        _amount_pdf(
            "Adjustment movement",
            statement.household.adjustment_movement,
        ),
        _amount_pdf(
            "Unclassified movement",
            statement.household.unclassified_movement,
        ),
        _amount_pdf(
            "Opening liquid position",
            statement.household.opening_liquid_position,
        ),
        _amount_pdf(
            "Closing liquid position",
            statement.household.closing_liquid_position,
        ),
        "",
        "Expense resolution",
        _amount_pdf("Confirmed", statement.expense_resolution.confirmed),
        _amount_pdf(
            "Human-approved",
            statement.expense_resolution.human_approved,
        ),
        _amount_pdf("Unresolved", statement.expense_resolution.unresolved),
        _amount_pdf("Resolved", statement.expense_resolution.resolved),
    ]
    for bucket in statement.expense_resolution.buckets:
        merchant = (
            f" / {_safe(bucket.canonical_merchant)}"
            if bucket.canonical_merchant
            else ""
        )
        lines.append(
            f"- category={bucket.category_id or 'none'} "
            f"{_safe(bucket.category_name)}{merchant} "
            f"[{bucket.resolution_disposition}]: "
            f"{_money(bucket.amount.cents)}; "
            f"{_evidence_pdf(bucket.amount.evidence)}"
        )
    for account in statement.accounts:
        lines.extend(
            [
                "",
                (
                    f"Account {account.account_id}: {_safe(account.name)}; "
                    f"kind={account.account_kind}; "
                    f"reconciliation={account.reconciliation_state}; "
                    f"liquid-included="
                    f"{str(account.liquid_position_included).lower()}"
                ),
                _amount_pdf("Opening", account.opening_balance),
                _amount_pdf("Transfers in", account.transfers_in),
                _amount_pdf("Transfers out", account.transfers_out),
                _amount_pdf("Refunds", account.refunds),
                _amount_pdf("Debt movement", account.debt_movement),
                _amount_pdf("Ledger close", account.ledger_closing_balance),
                (
                    "Statement close: unavailable"
                    if account.asserted_statement_closing_balance is None
                    else _amount_pdf(
                        "Statement close",
                        account.asserted_statement_closing_balance,
                    )
                ),
                (
                    "Delta: unavailable"
                    if account.assertion_delta is None
                    else _amount_pdf("Delta", account.assertion_delta)
                ),
            ]
        )
        for flow in (*account.typed_inflows, *account.typed_outflows):
            lines.append(
                _amount_pdf(
                    f"- {flow.direction} {_safe(flow.flow_kind)}",
                    flow.amount,
                )
            )
    if statement.liquid_position_exclusions:
        lines.extend(["", "Liquid-position exclusions"])
        for exclusion in statement.liquid_position_exclusions:
            lines.append(
                f"Account {exclusion.account_id} "
                f"{_safe(exclusion.account_name)}: {exclusion.reason}; "
                f"{_evidence_pdf(exclusion.evidence)}"
            )
    lines.extend(["", "Evidence rows"])
    for row in statement.rows:
        lines.append(
            f"{row.row_id} {row.posted_on} {_safe(row.description)} "
            f"{_money(row.amount_cents)} flow={row.flow_kind}; "
            f"category={row.category_id}:{_safe(row.category_name)}; "
            f"merchant={_safe(row.canonical_merchant or 'none')}; "
            f"resolution={row.resolution_disposition}; "
            f"source={_safe(row.source)}; "
            f"reconciliation={row.reconciliation_state}; "
            f"period-role={row.period_role}; {_evidence_pdf(row.evidence)}"
        )
    return lines


def _pdf_page_identity(statement: PeriodStatement) -> tuple[str, str, str]:
    return (
        f"Digest: {statement.report_digest}",
        (
            f"Close: {statement.close.state}; "
            f"snapshot={statement.close.snapshot_id or 'none'}; "
            f"current={str(statement.close.snapshot_is_current).lower()}"
        ),
        (
            "Close evidence: "
            f"snapshot-digest={statement.close.snapshot_digest or 'none'}; "
            f"exceptions={_joined(statement.close.exception_ids) or 'none'}; "
            "acknowledgements="
            f"{_joined(statement.close.acknowledgement_ids) or 'none'}"
        ),
    )


def _validate_pdf_glyphs(font: fitz.Font, lines: list[str]) -> None:
    unsupported = tuple(
        sorted(
            {
                ord(character)
                for line in lines
                for character in line
                if font.has_glyph(ord(character)) == 0
            }
        )
    )
    if unsupported:
        raise UnsupportedPdfGlyphError(unsupported)


def render_period_statement_pdf(statement: PeriodStatement) -> bytes:
    unicode_font = fitz.Font("japan")
    font_name = "FNUnicode"
    title = f"Finn Nancy household statement - {statement.month}"
    identity_lines = _pdf_page_identity(statement)
    logical_lines = _pdf_lines(statement)
    _validate_pdf_glyphs(
        unicode_font,
        [title, *identity_lines, *logical_lines],
    )
    document = fitz.open()
    lines = [
        physical
        for logical in logical_lines
        for physical in (
            [""]
            if not logical
            else textwrap.wrap(
                logical,
                width=102,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    ]
    lines_per_page = 42
    for offset in range(0, max(len(lines), 1), lines_per_page):
        page_number = offset // lines_per_page + 1
        page = document.new_page(width=612, height=792)
        page.insert_font(
            fontname=font_name,
            fontbuffer=unicode_font.buffer,
        )
        page.insert_text(
            (54, 32),
            title,
            fontsize=12,
            fontname=font_name,
        )
        page.insert_text(
            (468, 32),
            f"Page {page_number}",
            fontsize=8,
            fontname=font_name,
        )
        page.insert_text(
            (54, 48),
            identity_lines[0],
            fontsize=7.5,
            fontname=font_name,
        )
        page.insert_text(
            (54, 61),
            identity_lines[1],
            fontsize=7.5,
            fontname=font_name,
        )
        page.insert_text(
            (54, 74),
            identity_lines[2],
            fontsize=7.5,
            fontname=font_name,
        )
        remaining = page.insert_textbox(
            fitz.Rect(54, 88, 558, 738),
            "\n".join(lines[offset : offset + lines_per_page]),
            fontsize=8.5,
            fontname=font_name,
            lineheight=1.25,
        )
        if remaining < 0:
            document.close()
            raise ValueError("period statement PDF page overflow")
    document.set_metadata(
        {
            "title": f"Finn Nancy period statement {statement.month}",
            "author": "Finn Nancy",
            "subject": statement.schema_version,
            "creator": "Finn Nancy",
            "producer": "PyMuPDF",
            "creationDate": "D:20000101000000Z",
            "modDate": "D:20000101000000Z",
        }
    )
    document.subset_fonts()
    raw = document.tobytes(garbage=4, deflate=True, no_new_id=True)
    document.close()
    return raw
