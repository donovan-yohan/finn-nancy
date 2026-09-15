from __future__ import annotations

import pytest

from app.ingest.structured import (
    AdapterLimits,
    MappedCsvV1,
    StructuredImportError,
    parse_mapped_csv,
    parse_ofx,
)


CSV_MAPPING = MappedCsvV1(
    date_column="Date",
    description_column="Description",
    amount_column="Amount",
    balance_column="Balance",
    currency_column="Currency",
    pending_column="Status",
    fitid_column="Transaction ID",
)


OFX1 = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252

<OFX>
<SIGNONMSGSRSV1><SONRS><DTSERVER>20260701120000
<FI><ORG>Synthetic Bank<FID>9001</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>CAD
<BANKACCTFROM><BANKID>0001<ACCTID>123456789012</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260601000000<DTEND>20260630000000
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260603120000<TRNAMT>-12.34
<FITID>posted-1<NAME>Corner Cafe</STMTTRN>
</BANKTRANLIST>
<BANKTRANLISTP>
<STMTTRNP><TRNTYPE>DEBIT<DTPOSTED>20260629120000<TRNAMT>-8.50
<FITID>pending-1<NAME>Pending Market</STMTTRNP>
</BANKTRANLISTP>
<LEDGERBAL><BALAMT>979.16<DTASOF>20260630120000</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


OFX2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<OFX xmlns="urn:ofx:spec">
  <SIGNONMSGSRSV1><SONRS><DTSERVER>20260701120000</DTSERVER>
    <FI><ORG>Synthetic Card</ORG><FID>4242</FID></FI>
  </SONRS></SIGNONMSGSRSV1>
  <CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
    <CURDEF>CAD</CURDEF>
    <CCACCTFROM><ACCTID>9999888877774242</ACCTID></CCACCTFROM>
    <BANKTRANLIST>
      <DTSTART>20260601000000</DTSTART><DTEND>20260630000000</DTEND>
      <STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260612120000</DTPOSTED>
        <TRNAMT>-42.10</TRNAMT><FITID>card-1</FITID>
        <NAME>Synthetic Grocer</NAME><MEMO>weekly shop</MEMO>
      </STMTTRN>
    </BANKTRANLIST>
    <LEDGERBAL><BALAMT>-42.10</BALAMT><DTASOF>20260630120000</DTASOF></LEDGERBAL>
  </CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""


def test_mapped_csv_is_explicit_signed_and_preserves_repeated_rows():
    raw = (
        "Date,Description,Amount,Balance,Currency,Status,Transaction ID\n"
        "2026-06-03,Corner Cafe,-12.34,987.66,CAD,posted,csv-1\n"
        "2026-06-03,Corner Cafe,-12.34,975.32,CAD,posted,csv-2\n"
        "2026-06-05,Payroll,100.00,1075.32,CAD,pending,csv-3\n"
    ).encode()

    parsed = parse_mapped_csv(raw, CSV_MAPPING)

    assert parsed.adapter_id == "mapped_csv"
    assert parsed.adapter_version == "mapped-csv/v1"
    assert [row.amount_cents for row in parsed.rows] == [-1234, -1234, 10000]
    assert [row.source_row_number for row in parsed.rows] == [2, 3, 4]
    assert [row.anchor["row_number"] for row in parsed.rows] == [2, 3, 4]
    assert parsed.rows[-1].is_pending is True
    assert parsed.review_reasons == ()


def test_mapped_csv_requires_declared_columns_and_actionable_row_errors():
    with pytest.raises(StructuredImportError) as missing:
        parse_mapped_csv(b"Date,Description\n2026-06-01,Cafe\n", CSV_MAPPING)
    assert missing.value.diagnostics[0].code == "mapped_column_missing"
    assert missing.value.diagnostics[0].field == "Amount"

    malformed = (
        "Date,Description,Amount,Balance,Currency,Status,Transaction ID\n"
        "06-01-2026,Cafe,not-money,,CAD,maybe,\n"
    ).encode()
    with pytest.raises(StructuredImportError) as invalid:
        parse_mapped_csv(malformed, CSV_MAPPING)
    assert {item.code for item in invalid.value.diagnostics} == {"date_invalid"}
    assert invalid.value.diagnostics[0].row_number == 2


def test_mapped_csv_routes_mixed_and_foreign_currency_to_review():
    mixed = (
        "Date,Description,Amount,Balance,Currency,Status,Transaction ID\n"
        "2026-06-01,Cafe,-1.00,,CAD,posted,\n"
        "2026-06-02,Shop,-2.00,,USD,posted,\n"
    ).encode()
    assert parse_mapped_csv(mixed, CSV_MAPPING).review_reasons == ("mixed_currency",)

    foreign = mixed.replace(b",CAD,", b",USD,")
    assert parse_mapped_csv(foreign, CSV_MAPPING).review_reasons == (
        "foreign_currency",
    )


def test_csv_limits_bound_bytes_rows_fields_and_diagnostics():
    with pytest.raises(StructuredImportError, match="size limit"):
        parse_mapped_csv(b"x" * 20, CSV_MAPPING, limits=AdapterLimits(max_bytes=10))

    raw = (
        "Date,Description,Amount,Balance,Currency,Status,Transaction ID\n"
        "2026-06-01,A,-1.00,,CAD,posted,\n"
        "2026-06-02,B,-2.00,,CAD,posted,\n"
    ).encode()
    with pytest.raises(StructuredImportError) as rows:
        parse_mapped_csv(raw, CSV_MAPPING, limits=AdapterLimits(max_rows=1))
    assert rows.value.diagnostics[0].code == "too_many_rows"


@pytest.mark.parametrize(
    "amount",
    [
        "Infinity",
        "NaN",
        "1e1000000",
        "92233720368547758.08",
        '"1,2,3"',
    ],
)
def test_csv_rejects_nonfinite_overflow_and_malformed_grouping(amount):
    raw = (
        "Date,Description,Amount,Balance,Currency,Status,Transaction ID\n"
        f"2026-06-01,Cafe,{amount},,CAD,posted,row-1\n"
    ).encode()
    with pytest.raises(StructuredImportError) as blocked:
        parse_mapped_csv(raw, CSV_MAPPING)
    assert blocked.value.diagnostics[0].code == "amount_invalid"


def test_ofx1_sgml_preserves_signed_amount_fitid_and_pending_identity():
    parsed = parse_ofx(OFX1)

    assert parsed.adapter_id == "ofx_sgml"
    assert parsed.adapter_version == "ofx-sgml/v1"
    assert parsed.provider_name == "Synthetic Bank:9001"
    assert parsed.account_last4 == "9012"
    assert "123456789012" not in repr(parsed)
    assert [row.amount_cents for row in parsed.rows] == [-1234, -850]
    assert [row.provider_fitid for row in parsed.rows] == ["posted-1", "pending-1"]
    assert [row.is_pending for row in parsed.rows] == [False, True]
    assert parsed.closing_balance_cents == 97916
    assert parsed.period_start_on == "2026-06-01"
    assert parsed.period_end_on == "2026-06-30"


def test_ofx2_xml_qfx_uses_same_bounded_contract():
    parsed = parse_ofx(OFX2)

    assert parsed.adapter_id == "ofx_xml"
    assert parsed.adapter_version == "ofx-xml/v1"
    assert parsed.account_last4 == "4242"
    assert parsed.rows[0].description == "Synthetic Grocer \u2014 weekly shop"
    assert parsed.rows[0].amount_cents == -4210
    assert parsed.rows[0].provider_fitid == "card-1"
    assert parsed.closing_balance_cents == -4210


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE OFX><OFX/>',
        b'<?xml version="1.0"?><!ENTITY x "boom"><OFX/>',
        b'<OFX xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include/></OFX>',
    ],
)
def test_ofx_rejects_dtd_entity_and_xinclude(payload):
    with pytest.raises(StructuredImportError) as blocked:
        parse_ofx(payload)
    assert blocked.value.diagnostics[0].code == "ofx_external_markup"


def test_ofx_rejects_multiple_account_aggregates():
    duplicate = OFX2.replace(
        b"</CREDITCARDMSGSRSV1>",
        b"<CCSTMTTRNRS><CCSTMTRS><CURDEF>CAD</CURDEF>"
        b"<CCACCTFROM><ACCTID>other</ACCTID></CCACCTFROM>"
        b"</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>",
    )
    with pytest.raises(StructuredImportError) as blocked:
        parse_ofx(duplicate)
    assert blocked.value.diagnostics[0].code == "ofx_account_aggregate_count"


def test_ofx_row_currency_conflict_fails_to_review():
    conflicted = OFX2.replace(
        b"<NAME>Synthetic Grocer</NAME>",
        b"<NAME>Synthetic Grocer</NAME>"
        b"<CURRENCY><CURSYM>USD</CURSYM><CURRATE>1</CURRATE></CURRENCY>",
    )
    parsed = parse_ofx(conflicted)
    assert "row_currency_conflict" in parsed.review_reasons
    assert parsed.currency == ""


def test_ofx_limits_depth_and_field_lengths():
    deep = b'<?xml version="1.0"?><OFX><A><B><C><D/></C></B></A></OFX>'
    with pytest.raises(StructuredImportError) as depth:
        parse_ofx(deep, limits=AdapterLimits(max_depth=3))
    assert depth.value.diagnostics[0].code == "ofx_depth_limit"

    oversized = OFX2.replace(b"Synthetic Grocer", b"x" * 100)
    with pytest.raises(StructuredImportError) as field:
        parse_ofx(oversized, limits=AdapterLimits(max_field_chars=32))
    assert field.value.diagnostics[0].code == "ofx_field_limit"


def test_ofx1_rejects_truncated_aggregates_but_allows_omitted_leaf_end_tags():
    assert parse_ofx(OFX1).rows
    truncated = OFX1.rsplit(b"</OFX>", 1)[0]
    with pytest.raises(StructuredImportError) as blocked:
        parse_ofx(truncated)
    assert blocked.value.diagnostics[0].code == "ofx_sgml_truncated"


@pytest.mark.parametrize(
    "amount",
    ["Infinity", "NaN", "1e1000000", "92233720368547758.08"],
)
def test_ofx_rejects_nonfinite_and_out_of_range_cents(amount):
    payload = OFX2.replace(b"-42.10", amount.encode(), 1)
    with pytest.raises(StructuredImportError) as blocked:
        parse_ofx(payload)
    assert blocked.value.diagnostics[0].code == "ofx_transaction_invalid"

    balance = OFX2.replace(
        b"<BALAMT>-42.10</BALAMT>",
        f"<BALAMT>{amount}</BALAMT>".encode(),
    )
    with pytest.raises(StructuredImportError) as blocked_balance:
        parse_ofx(balance)
    assert blocked_balance.value.diagnostics[0].code == "ofx_balance_invalid"
