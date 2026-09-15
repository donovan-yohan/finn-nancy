"""Deterministic PDF statement adapter tests.

Fixtures are synthesised with PyMuPDF so the repository never carries real
financial data. Each fixture reproduces the layout hazard it is named for.
"""
from __future__ import annotations

import fitz
import pytest

from app.ingest.structured.pdf_adapter import parse_pdf_statement
from app.ingest.structured.types import StructuredImportError

FONT = "helv"
SIZE = 8.0


def _right(page, x_right: float, y: float, text: str) -> None:
    width = fitz.get_text_length(text, fontname=FONT, fontsize=SIZE)
    page.insert_text((x_right - width, y), text, fontname=FONT, fontsize=SIZE)


def _left(page, x: float, y: float, text: str) -> None:
    page.insert_text((x, y), text, fontname=FONT, fontsize=SIZE)


def _scotia_pdf(rows, *, opening, withdrawals, deposits, closing) -> bytes:
    """Two unsigned amount columns; direction encoded only by x-position."""
    doc = fitz.open()
    page = doc.new_page()
    _left(page, 73, 60, "www.syntheticcolumnbank.invalid")
    _left(page, 73, 80, "Your Chequing account")
    _left(page, 73, 92, "June 18 to July 17, 2026")
    _left(page, 73, 100, f"Opening Balance on June 18, 2026 ${opening}")
    _left(page, 73, 115, f"Minus total withdrawals ${withdrawals}")
    _left(page, 73, 130, f"Plus total deposits ${deposits}")
    _left(page, 73, 145, f"Closing Balance on July 17, 2026 ${closing}")
    _left(page, 73, 170, "Here's what happened in your account this statement period")
    _left(page, 73, 190, "Date")
    _left(page, 113, 190, "Transactions")
    _left(page, 252, 190, "withdrawn")
    _left(page, 296, 190, "($)")
    _left(page, 322, 190, "deposited")
    _left(page, 361, 190, "($)")
    _left(page, 395, 190, "Balance")
    _left(page, 426, 190, "($)")
    y = 210
    for date, desc, withdrawal, deposit, balance in rows:
        _left(page, 73, y, date)
        _left(page, 113, y, desc)
        if withdrawal:
            _right(page, 306, y, withdrawal)
        if deposit:
            _right(page, 371, y, deposit)
        _right(page, 436, y, balance)
        y += 18
    return doc.tobytes()


def _synthetic_card_pdf(sections, *, previous, credits, debits, closing) -> bytes:
    """Single signed amount column plus per-card sections that span pages."""
    doc = fitz.open()
    page = doc.new_page()
    _left(page, 26, 50, "www.examplecard.invalid")
    _left(page, 26, 65, "Account Number XXXX XXXX XXXX 9001")
    _left(page, 26, 80, "Statement Period Jun 26, 2026 - Jul 25, 2026")
    _left(page, 26, 100, f"Previous balance ${previous}")
    _left(page, 26, 115, f"Payments & credits ${credits}")
    _left(page, 26, 130, f"New purchases & debits ${debits}")
    _left(page, 26, 145, f"New Balance ${closing}")
    _left(page, 26, 170, "Transaction Details")
    _left(page, 26, 185, "Trans Date")
    _left(page, 62, 185, "Post Date")
    _left(page, 102, 185, "Description")
    _left(page, 318, 185, "Amount")
    _left(page, 352, 185, "($)")
    y = 205
    for card, rows in sections:
        _left(page, 26, y, f"Card Number XXXX XXXX XXXX {card}")
        y += 18
        for trans, post, merchant, city, amount in rows:
            if y > 720:
                # Continue on a fresh page WITHOUT repeating the card header:
                # the card section must survive the page break.
                page = doc.new_page()
                _left(page, 26, 50, "Account Number XXXX XXXX XXXX 9001")
                _left(page, 26, 70, "Transaction Details - continued")
                _left(page, 26, 85, "Trans Date")
                _left(page, 62, 85, "Post Date")
                _left(page, 102, 85, "Description")
                _left(page, 318, 85, "Amount")
                _left(page, 352, 85, "($)")
                y = 105
            _left(page, 26, y, trans)
            _left(page, 59, y, post)
            _left(page, 95, y, merchant)
            _left(page, 204, y, city)
            _right(page, 366, y, amount)
            y += 18
    return doc.tobytes()


SCOTIA_ROWS = [
    ("Jun 18", "Transfer to", "17.00", "", "3,791.33"),
    ("Jun 19", "Mortgage payment", "1,209.24", "", "2,582.09"),
    ("Jun 29", "Payroll dep.", "", "2,928.72", "5,510.81"),
    ("Jul 2", "Utility bill", "36.77", "", "5,474.04"),
]


def test_column_position_direction_is_read_from_geometry():
    raw = _scotia_pdf(
        SCOTIA_ROWS, opening="3,808.33", withdrawals="1,263.01",
        deposits="2,928.72", closing="5,474.04",
    )
    parsed = parse_pdf_statement(raw)
    assert parsed.adapter_id == "syntheticcolumnbank_pdf"
    assert [row.amount_cents for row in parsed.rows] == [-1700, -120924, 292872, -3677]
    assert parsed.opening_balance_cents == 380833
    assert parsed.closing_balance_cents == 547404


def test_unsigned_amounts_are_not_all_treated_as_deposits():
    """The withdrawal column carries no minus sign; only x-position says so."""
    parsed = parse_pdf_statement(
        _scotia_pdf(SCOTIA_ROWS, opening="3,808.33", withdrawals="1,263.01",
                    deposits="2,928.72", closing="5,474.04")
    )
    assert sum(r.amount_cents for r in parsed.rows if r.amount_cents < 0) == -126301
    assert sum(r.amount_cents for r in parsed.rows if r.amount_cents > 0) == 292872


def test_arithmetic_gate_blocks_a_broken_running_balance():
    tampered = list(SCOTIA_ROWS)
    tampered[1] = ("Jun 19", "Mortgage payment", "1,209.24", "", "2,999.99")
    raw = _scotia_pdf(tampered, opening="3,808.33", withdrawals="1,263.01",
                      deposits="2,928.72", closing="5,474.04")
    with pytest.raises(StructuredImportError) as excinfo:
        parse_pdf_statement(raw)
    codes = {item.code for item in excinfo.value.diagnostics}
    assert "pdf_running_balance_mismatch" in codes


def test_arithmetic_gate_blocks_a_control_total_mismatch():
    raw = _scotia_pdf(SCOTIA_ROWS, opening="3,808.33", withdrawals="9,999.99",
                      deposits="2,928.72", closing="5,474.04")
    with pytest.raises(StructuredImportError) as excinfo:
        parse_pdf_statement(raw)
    assert "pdf_debit_total_mismatch" in {i.code for i in excinfo.value.diagnostics}


def test_gate_failure_points_at_the_offending_row():
    tampered = list(SCOTIA_ROWS)
    tampered[2] = ("Jun 29", "Payroll dep.", "", "2,928.72", "9,999.99")
    raw = _scotia_pdf(tampered, opening="3,808.33", withdrawals="1,263.01",
                      deposits="2,928.72", closing="5,474.04")
    with pytest.raises(StructuredImportError) as excinfo:
        parse_pdf_statement(raw)
    balance = [i for i in excinfo.value.diagnostics if i.code == "pdf_running_balance_mismatch"]
    assert balance and balance[0].row_number == 3


def test_card_sections_survive_a_page_break():
    primary = [("Jun 25", "Jun 26", f"MERCHANT {n:02d}", "EXAMPLEVILLE EX", "10.00") for n in range(40)]
    supplemental = [("Jul 1", "Jul 2", "OTHER MERCHANT", "EXAMPLE REGION", "25.00")]
    raw = _synthetic_card_pdf(
        [("9001", primary), ("9002", supplemental)],
        previous="0.00", credits="0.00", debits="425.00", closing="425.00",
    )
    parsed = parse_pdf_statement(raw)
    assert len(parsed.rows) == 41
    cards = [row.anchor.get("card_last4") for row in parsed.rows]
    assert cards.count("9001") == 40
    assert cards.count("9002") == 1
    # The supplemental card's row is the last one, on a later page.
    assert parsed.rows[-1].anchor["card_last4"] == "9002"
    assert parsed.rows[-1].anchor["boxes"][0]["page"] > 0


def test_rows_carry_normalized_source_boxes():
    parsed = parse_pdf_statement(
        _scotia_pdf(SCOTIA_ROWS, opening="3,808.33", withdrawals="1,263.01",
                    deposits="2,928.72", closing="5,474.04")
    )
    for row in parsed.rows:
        boxes = row.anchor["boxes"]
        assert boxes
        for box in boxes:
            assert 0.0 <= box["x0"] < box["x1"] <= 1.0
            assert 0.0 <= box["y0"] < box["y1"] <= 1.0


def test_unknown_layout_is_rejected_rather_than_guessed():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Some Other Bank " * 40, fontname=FONT, fontsize=SIZE)
    with pytest.raises(StructuredImportError) as excinfo:
        parse_pdf_statement(doc.tobytes())
    assert "pdf_profile_unknown" in {i.code for i in excinfo.value.diagnostics}


def test_scanned_pdf_without_text_layer_is_rejected():
    doc = fitz.open()
    doc.new_page()
    with pytest.raises(StructuredImportError) as excinfo:
        parse_pdf_statement(doc.tobytes())
    assert {i.code for i in excinfo.value.diagnostics} & {
        "pdf_no_text_layer", "pdf_profile_unknown"
    }


def test_foreign_currency_rows_stay_in_home_currency():
    """An FX note annotates the purchase; the row is still billed in CAD."""
    raw = _synthetic_card_pdf(
        [("9001", [("Jun 28", "Jun 29", "OVERSEAS SHOP", "EXAMPLE REGION", "1.95")])],
        previous="0.00", credits="0.00", debits="1.95", closing="1.95",
    )
    parsed = parse_pdf_statement(raw, home_currency="CAD")
    assert [row.currency for row in parsed.rows] == ["CAD"]


def test_two_date_columns_do_not_shift_the_description():
    """Card statements print a transaction date and a posting date.

    Stripping only one leaves the second parsed as the start of the merchant
    name, which silently corrupts every description on the statement.
    """
    raw = _synthetic_card_pdf(
        [("9001", [("Jun 25", "Jun 26", "SYNTHETIC TEA INC", "EXAMPLE REGION", "25.55")])],
        previous="0.00", credits="0.00", debits="25.55", closing="25.55",
    )
    parsed = parse_pdf_statement(raw)
    row = parsed.rows[0]
    assert row.anchor["merchant_text"] == "SYNTHETIC TEA INC"
    assert row.anchor["locality"] == "EXAMPLE REGION"
    # Rows file under the posting date; the transaction date is kept alongside.
    assert row.posted_on.endswith("-06-26")
    assert row.anchor["transacted_on"].endswith("-06-25")


def test_trailing_page_text_is_not_absorbed_into_the_last_row():
    """Statements carry pages of legal terms after the transactions."""
    doc = fitz.open(stream=_synthetic_card_pdf(
        [("9001", [("Jun 25", "Jun 26", "SYNTHETIC TEA INC", "EXAMPLE REGION", "25.55")])],
        previous="0.00", credits="0.00", debits="25.55", closing="25.55",
    ), filetype="pdf")
    terms = doc.new_page()
    for index in range(30):
        _left(terms, 40, 60 + index * 18,
              "Disputed Transactions You must review your Statement and check that "
              "the information about your Purchases is accurate.")
    raw = doc.tobytes()
    doc.close()

    parsed = parse_pdf_statement(raw)
    assert len(parsed.rows) == 1
    assert "Disputed" not in parsed.rows[0].anchor.get("detail", "")
    assert len(parsed.rows[0].anchor.get("detail", "")) < 80
