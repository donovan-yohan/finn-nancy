"""Bounded stdlib parser for OFX1 SGML and OFX2 XML/QFX."""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from typing import Iterator

from .types import (
    AdapterLimits,
    ImportDiagnostic,
    MAX_SIGNED_CENTS,
    OFX_SGML_VERSION,
    OFX_XML_VERSION,
    ParsedStatement,
    ParsedStatementRow,
    StructuredImportError,
)


_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z0-9_.:-]+)(?:\s[^>]*)?>")
_FORBIDDEN = (
    b"<!DOCTYPE",
    b"<!ENTITY",
    b"<XI:INCLUDE",
    b"HTTP://WWW.W3.ORG/2001/XINCLUDE",
)
_ACCOUNT_TAGS = {"STMTRS", "CCSTMTRS"}
_MONEY = re.compile(r"^[+-]?(?:\d+(?:\.\d{1,2})?|\.\d{1,2})$")


@dataclass
class _Node:
    tag: str
    value: str = ""
    children: list["_Node"] = field(default_factory=list)


def _fail(code: str, message: str) -> StructuredImportError:
    return StructuredImportError([ImportDiagnostic(code, message)])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].upper()


def _guard(raw: bytes, limits: AdapterLimits) -> None:
    if len(raw) > limits.max_bytes:
        raise _fail("file_too_large", "The OFX/QFX file exceeds the import size limit.")
    if b"\x00" in raw:
        raise _fail("ofx_nul_byte", "The OFX/QFX file contains an invalid NUL byte.")
    upper = raw.upper()
    if any(value in upper for value in _FORBIDDEN):
        raise _fail(
            "ofx_external_markup",
            "DTD, ENTITY, and XInclude markup is not allowed.",
        )


def _convert_xml(
    element: ET.Element,
    *,
    limits: AdapterLimits,
    depth: int,
    counter: list[int],
) -> _Node:
    if depth > limits.max_depth:
        raise _fail("ofx_depth_limit", "The OFX XML nesting limit was exceeded.")
    counter[0] += 1
    if counter[0] > limits.max_tokens:
        raise _fail("ofx_token_limit", "The OFX XML token limit was exceeded.")
    value = (element.text or "").strip()
    if len(value) > limits.max_field_chars:
        raise _fail("ofx_field_limit", "An OFX field exceeds the length limit.")
    node = _Node(_local(element.tag), value)
    node.children = [
        _convert_xml(child, limits=limits, depth=depth + 1, counter=counter)
        for child in list(element)
    ]
    return node


def _parse_xml(body: bytes, limits: AdapterLimits) -> _Node:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise _fail("ofx_xml_invalid", "The OFX XML document is malformed.") from exc
    return _convert_xml(root, limits=limits, depth=1, counter=[0])


def _parse_sgml(text: str, limits: AdapterLimits) -> _Node:
    start = text.upper().find("<OFX")
    if start < 0:
        raise _fail("ofx_root_missing", "The OFX root aggregate is missing.")
    text = text[start:]
    dummy = _Node("_ROOT")
    stack = [dummy]
    position = 0
    tokens = 0
    for match in _TAG.finditer(text):
        between = text[position : match.start()].strip()
        if between:
            tokens += 1
            if len(between) > limits.max_field_chars:
                raise _fail("ofx_field_limit", "An OFX field exceeds the length limit.")
            if len(stack) == 1:
                raise _fail("ofx_text_outside_root", "OFX text appears outside an aggregate.")
            stack[-1].value = between
        closing = bool(match.group(1))
        tag = _local(match.group(2))
        tokens += 1
        if tokens > limits.max_tokens:
            raise _fail("ofx_token_limit", "The OFX SGML token limit was exceeded.")
        if closing:
            while len(stack) > 1 and stack[-1].tag != tag:
                if stack[-1].value and not stack[-1].children:
                    stack.pop()
                else:
                    raise _fail(
                        "ofx_sgml_mismatch",
                        "The OFX SGML aggregate nesting is malformed.",
                    )
            if len(stack) == 1:
                raise _fail("ofx_sgml_mismatch", "An OFX closing tag is unmatched.")
            stack.pop()
        else:
            while len(stack) > 1 and stack[-1].value and not stack[-1].children:
                stack.pop()
            node = _Node(tag)
            stack[-1].children.append(node)
            stack.append(node)
            if len(stack) - 1 > limits.max_depth:
                raise _fail("ofx_depth_limit", "The OFX SGML nesting limit was exceeded.")
        position = match.end()
    trailing = text[position:].strip()
    if trailing:
        if len(trailing) > limits.max_field_chars:
            raise _fail("ofx_field_limit", "An OFX field exceeds the length limit.")
        if len(stack) == 1:
            raise _fail(
                "ofx_text_outside_root", "OFX text appears outside an aggregate."
            )
        stack[-1].value = trailing
    while len(stack) > 1 and stack[-1].value and not stack[-1].children:
        stack.pop()
    if len(stack) != 1:
        raise _fail(
            "ofx_sgml_truncated",
            "The OFX SGML document ended before its aggregates were closed.",
        )
    roots = [node for node in dummy.children if node.tag == "OFX"]
    if len(roots) != 1:
        raise _fail("ofx_root_count", "The file must contain exactly one OFX root.")
    return roots[0]


def _walk(node: _Node, ancestors: tuple[str, ...] = ()) -> Iterator[tuple[_Node, tuple[str, ...]]]:
    yield node, ancestors
    for child in node.children:
        yield from _walk(child, ancestors + (node.tag,))


def _all(node: _Node, tag: str) -> list[_Node]:
    wanted = tag.upper()
    return [item for item, _ in _walk(node) if item.tag == wanted]


def _text(node: _Node, *tags: str) -> str:
    wanted = {tag.upper() for tag in tags}
    for item, _ in _walk(node):
        if item.tag in wanted and item.value.strip():
            return item.value.strip()
    return ""


def _ofx_date(value: str) -> str:
    digits = re.sub(r"\D", "", value)[:8]
    if len(digits) != 8:
        raise ValueError("OFX date is missing")
    return dt.datetime.strptime(digits, "%Y%m%d").date().isoformat()


def _cents(value: str) -> int:
    candidate = value.strip()
    if not _MONEY.fullmatch(candidate):
        raise ValueError("OFX amount is not a finite decimal")
    try:
        amount = Decimal(candidate)
        cents = amount * 100
        if not amount.is_finite() or not cents.is_finite():
            raise ValueError("OFX amount is not finite")
        if cents != cents.to_integral_value():
            raise ValueError("OFX amount has more than two decimal places")
        result = int(cents)
    except (DecimalException, OverflowError, ValueError) as exc:
        raise ValueError("OFX amount is invalid") from exc
    if abs(result) > MAX_SIGNED_CENTS:
        raise ValueError("OFX amount exceeds the signed-cent storage limit")
    return result


def _row_currency(node: _Node, default: str) -> str:
    for child in node.children:
        if child.tag in {"CURRENCY", "ORIGCURRENCY"}:
            return _text(child, "CURSYM").upper() or child.value.strip().upper() or default
    return default


def parse_ofx(
    raw: bytes,
    *,
    home_currency: str = "CAD",
    limits: AdapterLimits | None = None,
) -> ParsedStatement:
    limits = limits or AdapterLimits()
    _guard(raw, limits)
    upper = raw.upper()
    ofx_start = upper.find(b"<OFX")
    if ofx_start < 0:
        raise _fail("ofx_root_missing", "The OFX root aggregate is missing.")
    header = upper[:ofx_start]
    xml_mode = b"OFXHEADER:200" in header or raw.lstrip().startswith(b"<?xml")
    if xml_mode:
        root = _parse_xml(raw[ofx_start:], limits)
        adapter_id = "ofx_xml"
        adapter_version = OFX_XML_VERSION
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        root = _parse_sgml(text, limits)
        adapter_id = "ofx_sgml"
        adapter_version = OFX_SGML_VERSION

    aggregates = [node for node, _ in _walk(root) if node.tag in _ACCOUNT_TAGS]
    if len(aggregates) != 1:
        raise _fail(
            "ofx_account_aggregate_count",
            "The file must contain exactly one bank or card account aggregate.",
        )
    aggregate = aggregates[0]
    account_id = _text(aggregate, "ACCTID")
    if not account_id:
        raise _fail("ofx_account_missing", "The OFX account identifier is missing.")
    currency = _text(aggregate, "CURDEF").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _fail("ofx_currency_invalid", "The OFX statement currency is invalid.")

    parsed_rows: list[ParsedStatementRow] = []
    review_reasons: list[str] = []
    transaction_index = 0
    for node, ancestors in _walk(aggregate):
        if node.tag not in {"STMTTRN", "STMTTRNP"}:
            continue
        transaction_index += 1
        if transaction_index > limits.max_rows:
            raise _fail("too_many_rows", "The OFX/QFX file has too many transaction rows.")
        try:
            posted_on = _ofx_date(_text(node, "DTPOSTED"))
            amount_cents = _cents(_text(node, "TRNAMT"))
        except ValueError as exc:
            raise _fail(
                "ofx_transaction_invalid",
                "An OFX transaction has an invalid date or signed amount.",
            ) from exc
        description = _text(node, "NAME")
        memo = _text(node, "MEMO")
        if not description:
            description = memo or _text(node, "TRNTYPE")
        elif memo and memo.casefold() != description.casefold():
            description = f"{description} — {memo}"
        if not description:
            raise _fail(
                "ofx_description_missing",
                "An OFX transaction description is missing.",
            )
        if len(description) > limits.max_field_chars:
            raise _fail("ofx_field_limit", "An OFX field exceeds the length limit.")
        row_currency = _row_currency(node, currency)
        if len(row_currency) != 3 or not row_currency.isalpha():
            raise _fail(
                "ofx_row_currency_invalid",
                "An OFX transaction currency is invalid.",
            )
        pending = node.tag == "STMTTRNP" or "BANKTRANLISTP" in ancestors
        fitid = _text(node, "FITID")
        if not fitid:
            review_reasons.append("missing_fitid")
        if row_currency != currency:
            review_reasons.append("row_currency_conflict")
        parsed_rows.append(
            ParsedStatementRow(
                source_row_number=transaction_index,
                posted_on=posted_on,
                description=description,
                amount_cents=amount_cents,
                currency=row_currency,
                is_pending=pending,
                provider_fitid=fitid,
                anchor={
                    "kind": "raw_row",
                    "adapter": adapter_id,
                    "transaction_index": transaction_index,
                    "tag": node.tag,
                },
            )
        )
    if not parsed_rows:
        raise _fail("transactions_missing", "The OFX/QFX file has no transaction rows.")
    row_currencies = {row.currency for row in parsed_rows}
    if len(row_currencies) > 1:
        review_reasons.append("mixed_currency")
    if currency != home_currency.strip().upper():
        review_reasons.append("foreign_currency")

    start = _text(aggregate, "DTSTART")
    end = _text(aggregate, "DTEND")
    issued = _text(root, "DTSERVER") or _text(aggregate, "DTASOF")
    try:
        period_start = _ofx_date(start) if start else min(row.posted_on for row in parsed_rows)
        period_end = _ofx_date(end) if end else max(row.posted_on for row in parsed_rows)
        issued_on = _ofx_date(issued) if issued else ""
    except ValueError as exc:
        raise _fail("ofx_period_invalid", "The OFX statement period is invalid.") from exc
    closing_text = ""
    balances = _all(aggregate, "LEDGERBAL")
    if balances:
        closing_text = _text(balances[0], "BALAMT")
    try:
        closing = _cents(closing_text) if closing_text else None
    except ValueError as exc:
        raise _fail("ofx_balance_invalid", "The OFX closing balance is invalid.") from exc

    org = _text(root, "ORG")
    fid = _text(root, "FID")
    provider = ":".join(part for part in (org, fid) if part)
    last4 = re.sub(r"[^A-Za-z0-9]", "", account_id)[-4:]
    return ParsedStatement(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        rows=tuple(parsed_rows),
        institution=org,
        account_last4=last4,
        provider_name=provider,
        provider_id=fid,
        provider_account_token=account_id,
        period_start_on=period_start,
        period_end_on=period_end,
        statement_issued_on=issued_on,
        currency=currency if row_currencies == {currency} else "",
        closing_balance_cents=closing,
        review_reasons=tuple(dict.fromkeys(review_reasons)),
    )
