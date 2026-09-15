"""Deterministic, no-model adapter for text-layer PDF statements.

Import is gated on arithmetic the statement asserts about itself: the recomputed
running balance must reproduce every printed balance, and the summed rows must
reproduce the summary box. A profile that drifts, or an unrecognised layout that
slips past detection, fails the gate and routes to review rather than importing
plausible-looking but wrong rows.
"""
from __future__ import annotations

import fitz

from .pdf_engine import EngineRow, extract_rows, read_totals
from .pdf_profiles import PROFILES, PROFILES_BY_ID, StatementProfile
from .types import (
    AdapterLimits,
    DiagnosticCollector,
    ImportDiagnostic,
    ParsedStatement,
    ParsedStatementRow,
    StructuredImportError,
)

PDF_VERSION = "pdf-statement/v1"
MAX_PAGES = 40


def detect_profile(doc) -> StatementProfile | None:
    text = "\n".join(page.get_text() for page in doc[: min(3, doc.page_count)])
    for profile in PROFILES:
        if all(pattern.search(text) for pattern in profile.detect):
            return profile
    return None


def _period(doc, profile: StatementProfile) -> tuple[str, str]:
    if profile.period is None:
        return "", ""
    text = "\n".join(page.get_text() for page in doc)
    flat = text.replace("\n", " ")
    match = profile.period.search(flat)
    if not match:
        return "", ""
    start_raw, end_raw = match.group(1), match.group(2)
    end = _parse_long_date(end_raw)
    # Some issuers omit the year on the period's start date.
    start = _parse_long_date(start_raw, default_year=end[:4] if end else "")
    return start, end


def _parse_long_date(text: str, default_year: str = "") -> str:
    from datetime import datetime

    cleaned = " ".join(text.replace(",", " ").split())
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    if default_year:
        for fmt in ("%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(cleaned, fmt).date()
                return parsed.replace(year=int(default_year)).isoformat()
            except ValueError:
                continue
    return ""


def verify_arithmetic(
    rows: list[EngineRow],
    totals: dict[str, int],
    profile: StatementProfile,
) -> list[ImportDiagnostic]:
    """Recompute what the statement asserts about itself. Any drift is fatal."""
    problems: list[ImportDiagnostic] = []
    inflow = sum(r.signed_amount_cents for r in rows if r.signed_amount_cents > 0)
    outflow = sum(-r.signed_amount_cents for r in rows if r.signed_amount_cents < 0)
    # On a card, a positive row is a charge you owe more for; on a chequing
    # account the same sign is money arriving. The equation flips with it.
    debits, credits = (inflow, outflow) if profile.liability else (outflow, inflow)

    if "debits" in totals and debits != totals["debits"]:
        problems.append(ImportDiagnostic(
            "pdf_debit_total_mismatch",
            f"Summed charges ({_money(debits)}) do not match the statement total "
            f"({_money(totals['debits'])}).",
        ))
    if "credits" in totals and credits != totals["credits"]:
        problems.append(ImportDiagnostic(
            "pdf_credit_total_mismatch",
            f"Summed credits ({_money(credits)}) do not match the statement total "
            f"({_money(totals['credits'])}).",
        ))

    opening = totals.get("opening_balance", totals.get("previous_balance"))
    closing = totals.get("closing_balance")
    if opening is not None and closing is not None:
        computed = opening + debits - credits if profile.liability else opening - debits + credits
        if computed != closing:
            problems.append(ImportDiagnostic(
                "pdf_closing_balance_mismatch",
                f"Opening balance plus activity ({_money(computed)}) does not reach the "
                f"stated closing balance ({_money(closing)}).",
            ))

    # Per-row running balance, where the issuer prints one.
    printed = [r for r in rows if r.balance_cents is not None]
    if printed and opening is not None:
        running = opening
        for row in rows:
            if row.balance_cents is None:
                continue
            running += row.signed_amount_cents
            if running != row.balance_cents:
                problems.append(ImportDiagnostic(
                    "pdf_running_balance_mismatch",
                    f"Row {row.line_number + 1} breaks the running balance: expected "
                    f"{_money(running)}, the statement prints {_money(row.balance_cents)}.",
                    row_number=row.line_number + 1,
                ))
                break
            running = row.balance_cents
    return problems


def _money(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def parse_pdf_statement(
    raw: bytes,
    *,
    home_currency: str = "CAD",
    limits: AdapterLimits | None = None,
    profile_id: str = "",
) -> ParsedStatement:
    limits = limits or AdapterLimits()
    if len(raw) > limits.max_bytes:
        raise StructuredImportError(
            [ImportDiagnostic("pdf_too_large", "The PDF is too large to import.")]
        )
    diagnostics = DiagnosticCollector(limits.max_diagnostics)

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        raise StructuredImportError(
            [ImportDiagnostic("pdf_unreadable", "The file could not be read as a PDF.")]
        ) from None

    with doc:
        if doc.page_count > MAX_PAGES:
            raise StructuredImportError(
                [ImportDiagnostic("pdf_too_many_pages", "The PDF has too many pages.")]
            )
        if doc.is_encrypted:
            raise StructuredImportError(
                [ImportDiagnostic("pdf_encrypted", "The PDF is password protected.")]
            )

        profile = PROFILES_BY_ID.get(profile_id) if profile_id else detect_profile(doc)
        if profile is None:
            raise StructuredImportError([ImportDiagnostic(
                "pdf_profile_unknown",
                "This statement layout is not recognised. Import it as CSV or OFX, "
                "or add a profile for this institution.",
            )])

        characters = sum(len(page.get_text()) for page in doc)
        if characters < 200:
            raise StructuredImportError([ImportDiagnostic(
                "pdf_no_text_layer",
                "This PDF has no text layer, so it cannot be imported without OCR.",
            )])

        period_start, period_end = _period(doc, profile)
        try:
            rows = extract_rows(
                doc,
                profile,
                period_start=period_start,
                period_end=period_end,
                max_rows=limits.max_rows,
            )
        except LookupError:
            raise StructuredImportError([ImportDiagnostic(
                "pdf_columns_not_found",
                "The statement's transaction columns could not be located.",
            )]) from None

        if not rows:
            raise StructuredImportError(
                [ImportDiagnostic("pdf_no_rows", "No transactions were found.")]
            )

        totals = read_totals(doc, profile)
        problems = verify_arithmetic(rows, totals, profile)
        if problems:
            # Hard gate: nothing promotes from a statement that cannot prove itself.
            raise StructuredImportError(problems[: limits.max_diagnostics])

        account_last4 = ""
        if profile.account_last4 is not None:
            text = "\n".join(page.get_text() for page in doc)
            match = profile.account_last4.search(text.replace("\n", " "))
            if match:
                digits = "".join(c for c in match.group(1) if c.isdigit())
                account_last4 = digits[-4:]

        unresolved = [i + 1 for i, row in enumerate(rows) if not row.date_text]
        if unresolved:
            # Dropping undated rows would import a silently incomplete statement.
            raise StructuredImportError([ImportDiagnostic(
                "pdf_row_date_unresolved",
                f"{len(unresolved)} row date(s) could not be resolved against the "
                "statement period.",
                row_number=unresolved[0],
            )])

        parsed_rows = []
        for index, row in enumerate(rows):
            anchor = {
                "boxes": [box.as_dict() for box in row.boxes],
                "profile": profile.profile_id,
            }
            if row.section:
                anchor["card_last4"] = row.section
            if row.transacted_text and row.transacted_text != row.date_text:
                anchor["transacted_on"] = row.transacted_text
            if row.merchant:
                anchor["merchant_text"] = row.merchant[: limits.max_field_chars]
            if row.city:
                anchor["locality"] = row.city[: limits.max_field_chars]
            if row.detail:
                anchor["detail"] = row.detail[: limits.max_field_chars]
            parsed_rows.append(ParsedStatementRow(
                source_row_number=index + 1,
                posted_on=row.date_text,
                description=(row.description or row.merchant)[: limits.max_field_chars],
                amount_cents=row.signed_amount_cents,
                # Amounts are billed in the home currency; a foreign-currency
                # note annotates the original purchase and must not trip the
                # non-home-currency guard.
                currency=home_currency,
                balance_cents=row.balance_cents,
                anchor=anchor,
            ))

        opening = totals.get("opening_balance", totals.get("previous_balance"))
        return ParsedStatement(
            adapter_id=profile.profile_id,
            adapter_version=profile.version,
            rows=tuple(parsed_rows),
            institution=profile.institution,
            account_last4=account_last4,
            period_start_on=period_start,
            period_end_on=period_end,
            currency=home_currency,
            opening_balance_cents=opening,
            closing_balance_cents=totals.get("closing_balance"),
            diagnostics=diagnostics.as_tuple() if hasattr(diagnostics, "as_tuple") else (),
        )
