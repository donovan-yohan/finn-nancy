"""Versioned adapter contracts and hard parser limits."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any


MAPPED_CSV_VERSION = "mapped-csv/v1"
OFX_SGML_VERSION = "ofx-sgml/v1"
OFX_XML_VERSION = "ofx-xml/v1"
MAX_SIGNED_CENTS = 9_223_372_036_854_775_807
SUPPORTED_DATE_FORMATS = {
    "%Y-%m-%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
}


@dataclass(frozen=True)
class AdapterLimits:
    max_bytes: int = 5 * 1024 * 1024
    max_tokens: int = 200_000
    max_depth: int = 32
    max_rows: int = 10_000
    max_fields: int = 64
    max_field_chars: int = 4_096
    max_diagnostics: int = 50


@dataclass(frozen=True)
class ImportDiagnostic:
    code: str
    message: str
    row_number: int | None = None
    field: str = ""

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.row_number is not None:
            result["row_number"] = self.row_number
        if self.field:
            result["field"] = self.field
        return result


class StructuredImportError(ValueError):
    """A bounded, content-free adapter failure safe to show in the UI."""

    def __init__(self, diagnostics: list[ImportDiagnostic] | tuple[ImportDiagnostic, ...]):
        bounded = tuple(diagnostics)
        if not bounded:
            bounded = (
                ImportDiagnostic("structured_import_invalid", "The import is invalid."),
            )
        self.diagnostics = bounded
        super().__init__(bounded[0].message)


class DiagnosticCollector:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.items: list[ImportDiagnostic] = []
        self.truncated = False

    def add(
        self,
        code: str,
        message: str,
        *,
        row_number: int | None = None,
        field: str = "",
    ) -> None:
        if len(self.items) >= self.limit:
            self.truncated = True
            return
        self.items.append(
            ImportDiagnostic(
                code=code,
                message=message,
                row_number=row_number,
                field=field,
            )
        )

    def raise_if_any(self) -> None:
        if self.truncated and len(self.items) < self.limit:
            self.add(
                "diagnostics_truncated",
                "Additional import errors were omitted.",
            )
        if self.items:
            raise StructuredImportError(self.items)


@dataclass(frozen=True)
class MappedCsvV1:
    date_column: str
    description_column: str
    amount_column: str = ""
    debit_column: str = ""
    credit_column: str = ""
    balance_column: str = ""
    currency_column: str = ""
    pending_column: str = ""
    fitid_column: str = ""
    date_format: str = "%Y-%m-%d"
    delimiter: str = ","
    default_currency: str = "CAD"
    version: str = MAPPED_CSV_VERSION

    def __post_init__(self) -> None:
        required = (self.date_column.strip(), self.description_column.strip())
        if not all(required):
            raise ValueError("CSV date and description columns are required")
        has_amount = bool(self.amount_column.strip())
        has_split = bool(self.debit_column.strip() or self.credit_column.strip())
        if has_amount == has_split:
            raise ValueError(
                "CSV mapping requires either one signed amount column or debit/credit columns"
            )
        if has_split and not (
            self.debit_column.strip() and self.credit_column.strip()
        ):
            raise ValueError("CSV debit and credit columns must be mapped together")
        if self.version != MAPPED_CSV_VERSION:
            raise ValueError("unsupported CSV mapping version")
        if self.date_format not in SUPPORTED_DATE_FORMATS:
            raise ValueError("unsupported CSV date format")
        if len(self.delimiter) != 1 or self.delimiter in "\r\n":
            raise ValueError("CSV delimiter must be one non-newline character")
        currency = self.default_currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("CSV default currency must be a three-letter code")
        for value in self.columns:
            if len(value) > 128:
                raise ValueError("CSV column names must be at most 128 characters")

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(
            value.strip()
            for value in (
                self.date_column,
                self.description_column,
                self.amount_column,
                self.debit_column,
                self.credit_column,
                self.balance_column,
                self.currency_column,
                self.pending_column,
                self.fitid_column,
            )
            if value.strip()
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "date_column": self.date_column,
            "description_column": self.description_column,
            "amount_column": self.amount_column,
            "debit_column": self.debit_column,
            "credit_column": self.credit_column,
            "balance_column": self.balance_column,
            "currency_column": self.currency_column,
            "pending_column": self.pending_column,
            "fitid_column": self.fitid_column,
            "date_format": self.date_format,
            "delimiter": self.delimiter,
            "default_currency": self.default_currency.upper(),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "MappedCsvV1":
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("CSV mapping must be an object")
        allowed = set(cls.__dataclass_fields__)
        if set(value) - allowed:
            raise ValueError("CSV mapping contains unknown fields")
        return cls(**value)


@dataclass(frozen=True)
class ParsedStatementRow:
    source_row_number: int
    posted_on: str
    description: str
    amount_cents: int
    currency: str
    balance_cents: int | None = None
    is_pending: bool = False
    provider_fitid: str = field(default="", repr=False)
    anchor: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedStatement:
    adapter_id: str
    adapter_version: str
    rows: tuple[ParsedStatementRow, ...]
    institution: str = ""
    account_last4: str = ""
    provider_name: str = ""
    provider_id: str = ""
    provider_account_token: str = field(default="", repr=False)
    period_start_on: str = ""
    period_end_on: str = ""
    statement_issued_on: str = ""
    currency: str = ""
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None
    manual_fields: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    diagnostics: tuple[ImportDiagnostic, ...] = ()

    @property
    def period_month(self) -> str:
        return self.period_end_on[:7] if len(self.period_end_on) == 10 else ""

    @property
    def requires_review(self) -> bool:
        return bool(self.review_reasons)


@dataclass(frozen=True)
class ImportMetadata:
    period_start_on: str = ""
    period_end_on: str = ""
    statement_issued_on: str = ""
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "period_start_on",
            "period_end_on",
            "statement_issued_on",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                continue
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
            if parsed.isoformat() != value:
                raise ValueError(f"{field_name} must be YYYY-MM-DD")
        if (
            self.period_start_on
            and self.period_end_on
            and self.period_start_on > self.period_end_on
        ):
            raise ValueError("period_start_on must be on or before period_end_on")
        for field_name in ("opening_balance_cents", "closing_balance_cents"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field_name} must be integer cents")
            if abs(value) > MAX_SIGNED_CENTS:
                raise ValueError(
                    f"{field_name} exceeds the signed-cent storage limit"
                )
