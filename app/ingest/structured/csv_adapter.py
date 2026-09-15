"""Explicit mapped-CSV adapter; no header guessing and no model calls."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import Counter
from decimal import Decimal, DecimalException

from .types import (
    AdapterLimits,
    DiagnosticCollector,
    MAPPED_CSV_VERSION,
    MappedCsvV1,
    ParsedStatement,
    ParsedStatementRow,
    StructuredImportError,
    ImportDiagnostic,
    MAX_SIGNED_CENTS,
)


_MONEY = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?|\.\d{1,2})$"
)


def _decode(raw: bytes, limits: AdapterLimits) -> str:
    if len(raw) > limits.max_bytes:
        raise StructuredImportError(
            [ImportDiagnostic("file_too_large", "The CSV exceeds the import size limit.")]
        )
    if b"\x00" in raw:
        raise StructuredImportError(
            [ImportDiagnostic("csv_nul_byte", "The CSV contains an invalid NUL byte.")]
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StructuredImportError(
            [ImportDiagnostic("csv_encoding", "The CSV must be UTF-8 encoded.")]
        ) from exc


def _cents(value: str, *, allow_blank: bool = False) -> int | None:
    candidate = value.strip()
    if not candidate and allow_blank:
        return None
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1].strip()
    if not _MONEY.fullmatch(candidate):
        raise ValueError("amount is not a finite decimal with valid grouping")
    try:
        amount = Decimal(candidate.replace(",", ""))
        cents = amount * 100
        if not amount.is_finite() or not cents.is_finite():
            raise ValueError("amount is not finite")
        if cents != cents.to_integral_value():
            raise ValueError("amount has more than two decimal places")
        result = int(cents)
    except (DecimalException, OverflowError, ValueError) as exc:
        raise ValueError("amount is not a decimal number") from exc
    if abs(result) > MAX_SIGNED_CENTS:
        raise ValueError("amount exceeds the signed-cent storage limit")
    return -abs(result) if negative else result


def _date(value: str, date_format: str) -> str:
    parsed = dt.datetime.strptime(value.strip(), date_format).date()
    return parsed.isoformat()


def _pending(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "0", "false", "no", "posted", "cleared"}:
        return False
    if normalized in {"1", "true", "yes", "pending", "p"}:
        return True
    raise ValueError("pending value is not recognized")


def _row_amount(row: dict[str, str], mapping: MappedCsvV1) -> int:
    if mapping.amount_column:
        value = _cents(row[mapping.amount_column])
        assert value is not None
        return value
    debit = _cents(row[mapping.debit_column], allow_blank=True)
    credit = _cents(row[mapping.credit_column], allow_blank=True)
    if debit not in (None, 0) and credit not in (None, 0):
        raise ValueError("debit and credit are both populated")
    if debit in (None, 0) and credit in (None, 0):
        raise ValueError("debit and credit are both blank")
    return abs(int(credit)) if credit not in (None, 0) else -abs(int(debit))


def parse_mapped_csv(
    raw: bytes,
    mapping: MappedCsvV1,
    *,
    home_currency: str = "CAD",
    limits: AdapterLimits | None = None,
) -> ParsedStatement:
    limits = limits or AdapterLimits()
    text = _decode(raw, limits)
    diagnostics = DiagnosticCollector(limits.max_diagnostics)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=mapping.delimiter)
    headers = reader.fieldnames or []
    if not headers:
        raise StructuredImportError(
            [ImportDiagnostic("csv_header_missing", "The CSV header row is missing.")]
        )
    if len(headers) > limits.max_fields:
        raise StructuredImportError(
            [ImportDiagnostic("too_many_fields", "The CSV has too many columns.")]
        )
    if len(headers) != len(set(headers)):
        raise StructuredImportError(
            [ImportDiagnostic("duplicate_headers", "The CSV has duplicate column names.")]
        )
    missing = sorted(set(mapping.columns) - set(headers))
    if missing:
        raise StructuredImportError(
            [
                ImportDiagnostic(
                    "mapped_column_missing",
                    "One or more mapped CSV columns are missing.",
                    field=missing[0],
                )
            ]
        )

    rows: list[ParsedStatementRow] = []
    token_count = len(headers)
    for source_row_number, row in enumerate(reader, start=2):
        if len(rows) >= limits.max_rows:
            raise StructuredImportError(
                [ImportDiagnostic("too_many_rows", "The CSV has too many transaction rows.")]
            )
        if None in row:
            diagnostics.add(
                "csv_row_width",
                "A CSV row has more values than the header.",
                row_number=source_row_number,
            )
            continue
        values = {key: value or "" for key, value in row.items()}
        token_count += len(values)
        if token_count > limits.max_tokens:
            raise StructuredImportError(
                [ImportDiagnostic("too_many_tokens", "The CSV token limit was exceeded.")]
            )
        oversized = next(
            (key for key, value in values.items() if len(value) > limits.max_field_chars),
            None,
        )
        if oversized is not None:
            diagnostics.add(
                "field_too_long",
                "A CSV field exceeds the length limit.",
                row_number=source_row_number,
                field=oversized,
            )
            continue
        try:
            posted_on = _date(values[mapping.date_column], mapping.date_format)
        except (ValueError, TypeError):
            diagnostics.add(
                "date_invalid",
                "A transaction date does not match the selected format.",
                row_number=source_row_number,
                field=mapping.date_column,
            )
            continue
        description = values[mapping.description_column].strip()
        if not description:
            diagnostics.add(
                "description_missing",
                "A transaction description is missing.",
                row_number=source_row_number,
                field=mapping.description_column,
            )
            continue
        try:
            amount_cents = _row_amount(values, mapping)
        except ValueError:
            diagnostics.add(
                "amount_invalid",
                "A transaction amount is invalid or ambiguous.",
                row_number=source_row_number,
            )
            continue
        currency = (
            values[mapping.currency_column].strip().upper()
            if mapping.currency_column
            else mapping.default_currency.strip().upper()
        )
        if len(currency) != 3 or not currency.isalpha():
            diagnostics.add(
                "currency_invalid",
                "A row currency must be a three-letter code.",
                row_number=source_row_number,
                field=mapping.currency_column,
            )
            continue
        try:
            balance = (
                _cents(values[mapping.balance_column], allow_blank=True)
                if mapping.balance_column
                else None
            )
            pending = (
                _pending(values[mapping.pending_column])
                if mapping.pending_column
                else False
            )
        except ValueError:
            diagnostics.add(
                "optional_field_invalid",
                "A mapped balance or pending value is invalid.",
                row_number=source_row_number,
            )
            continue
        rows.append(
            ParsedStatementRow(
                source_row_number=source_row_number,
                posted_on=posted_on,
                description=description,
                amount_cents=amount_cents,
                currency=currency,
                balance_cents=balance,
                is_pending=pending,
                provider_fitid=(
                    values[mapping.fitid_column].strip()
                    if mapping.fitid_column
                    else ""
                ),
                anchor={
                    "kind": "raw_row",
                    "adapter": "mapped_csv",
                    "row_number": source_row_number,
                },
            )
        )
    diagnostics.raise_if_any()
    if not rows:
        raise StructuredImportError(
            [ImportDiagnostic("transactions_missing", "The CSV has no transaction rows.")]
        )
    currencies = Counter(row.currency for row in rows)
    reasons: list[str] = []
    if len(currencies) > 1:
        reasons.append("mixed_currency")
    elif next(iter(currencies)) != home_currency.strip().upper():
        reasons.append("foreign_currency")
    dates = sorted(row.posted_on for row in rows)
    return ParsedStatement(
        adapter_id="mapped_csv",
        adapter_version=MAPPED_CSV_VERSION,
        rows=tuple(rows),
        period_start_on=dates[0],
        period_end_on=dates[-1],
        currency=next(iter(currencies)) if len(currencies) == 1 else "",
        review_reasons=tuple(reasons),
    )
