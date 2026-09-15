"""Declarative per-institution profiles for the geometry-aware PDF engine.

A profile describes *where* things sit and *how direction is encoded*. It holds
no parsing logic, so supporting a new institution is a profile, not a parser.

Direction encoding is the part that actually differs between banks:

``explicit_sign``
    One amount column; a leading ``-`` marks the credit side.
``column_position``
    Two unsigned amount columns; the x-position alone decides debit vs credit.
    Text extraction destroys this, which is why the engine works on geometry.
``section``
    Unsigned amounts whose direction comes from the section heading above them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EXPLICIT_SIGN = "explicit_sign"
COLUMN_POSITION = "column_position"
SECTION = "section"


@dataclass(frozen=True)
class AmountColumn:
    """One numeric column, located by its header token(s)."""

    name: str
    header_token: str
    # Amounts are right-aligned, so the header's right edge anchors the column.
    unit_token: str = "($)"


@dataclass(frozen=True)
class TotalSpec:
    """A control total from the statement's own summary box."""

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class StatementProfile:
    profile_id: str
    version: str
    institution: str

    # All patterns must match the first page for the profile to claim the file.
    detect: tuple[re.Pattern[str], ...]

    sign_mode: str
    columns: tuple[AmountColumn, ...]

    # Card/loan accounts run the balance equation the other way: charges raise
    # the balance owed while payments reduce it.
    liability: bool = False

    period: re.Pattern[str] | None = None
    account_last4: re.Pattern[str] | None = None

    totals: tuple[TotalSpec, ...] = ()

    # Rows begin only after this marker, and stop at the first stop marker.
    body_start: tuple[str, ...] = ()
    body_stop: tuple[str, ...] = ()

    # Lines that look like rows but are balance markers, not transactions.
    pseudo_rows: tuple[str, ...] = ("Opening Balance", "Closing Balance", "Balance forward")

    # Splits a statement into per-card sections; group 1 is the card's last 4.
    section_pattern: re.Pattern[str] | None = None
    # For SECTION sign mode: heading text -> direction multiplier.
    section_directions: dict[str, int] = field(default_factory=dict)

    date_pattern: re.Pattern[str] | None = None
    # Card issuers print both a transaction date and a posting date. Strip both
    # or the second one is mistaken for the start of the merchant name.
    date_columns: int = 1
    # Some issuers omit the date on subsequent same-day rows.
    date_carry_forward: bool = False
    # True where a description legitimately wraps onto following lines. Where
    # it is False, only an explicit `continuation` pattern may extend a row --
    # otherwise trailing page content such as legal terms is absorbed into
    # whichever row happened to come last.
    detail_lines: bool = False
    # A wrapped description is a line or two, never a page. Bounding this stops
    # trailing page furniture being absorbed by whichever row came last.
    max_detail_lines: int = 1

    # Left edge of real content; anything further left is printer marginalia.
    content_x_min: float = 0.0
    # Header/footer bands excluded from row extraction, as a fraction of page height.
    header_fraction: float = 0.0
    footer_fraction: float = 1.0

    # Merchant and city share a column, separated by a visible gap.
    city_gap: float = 0.0
    province_anchored_city: bool = False

    # Continuation lines that annotate the row above rather than start a new one.
    continuation: tuple[re.Pattern[str], ...] = ()


MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_DAY = re.compile(rf"^(?:{MONTHS})\s+\d{{1,2}}$")


EXAMPLE_MASTERCARD = StatementProfile(
    profile_id="example_card_pdf",
    version="example-card-pdf/v1",
    institution="Example Card Issuer",
    detect=(re.compile(r"examplecard\.invalid", re.I), re.compile(r"Transaction Details", re.I)),
    sign_mode=EXPLICIT_SIGN,
    columns=(AmountColumn("amount", "Amount", "($)"),),
    liability=True,
    period=re.compile(r"Statement Period\s+(.+?\d{4})\s*-\s*(.+?\d{4})"),
    account_last4=re.compile(r"Account Number\s+(?:[X\s]*)(\d{4})"),
    totals=(
        TotalSpec("previous_balance", re.compile(r"Previous balance\s+\$(-?[\d,]+\.\d{2})")),
        TotalSpec("credits", re.compile(r"Payments & credits\s+\$(-?[\d,]+\.\d{2})")),
        TotalSpec("debits", re.compile(r"New purchases & debits\s+\$(-?[\d,]+\.\d{2})")),
        TotalSpec("closing_balance", re.compile(r"New Balance\s+\$(-?[\d,]+\.\d{2})")),
    ),
    body_start=("Transaction Details",),
    body_stop=("Interest Rate Chart",),
    section_pattern=re.compile(r"Card Number\s+(?:[X\s]*)(\d{4})"),
    date_pattern=_DAY,
    date_columns=2,
    city_gap=8.0,
    # A statement-period FX note annotates the purchase above it; the row is
    # still billed in the home currency and must not fail the currency guard.
    continuation=(re.compile(r"^FOREIGN CURRENCY\s+[A-Z]{3}\s+[\d,]+\.\d{2}\s*@", re.I),),
)


EXAMPLE_SAVINGS_BANK = StatementProfile(
    profile_id="example_savings_bank_pdf",
    version="example-savings-bank-pdf/v1",
    institution="Example Savings Bank",
    detect=(re.compile(r"examplesavingsbank\.invalid", re.I), re.compile(r"Activity details", re.I)),
    sign_mode=EXPLICIT_SIGN,
    columns=(AmountColumn("amount", "Withdrawals"), AmountColumn("balance", "Balance")),
    period=re.compile(r"(\w+ \d{1,2}, \d{4}) to (\w+ \d{1,2}, \d{4})"),
    account_last4=re.compile(r"#\s*([\d\- ]{6,})"),
    totals=(
        TotalSpec("opening_balance", re.compile(r"Opening balance\s+\$([\d,]+\.\d{2})")),
        TotalSpec("credits", re.compile(r"Total deposits\s+\+?\s*\$([\d,]+\.\d{2})")),
        TotalSpec("debits", re.compile(r"Total withdrawals\s+-?\s*\$([\d,]+\.\d{2})")),
        TotalSpec("closing_balance", re.compile(r"Closing balance\s+=?\s*\$([\d,]+\.\d{2})")),
    ),
    body_start=("Activity details",),
    date_pattern=_DAY,
)


SYNTHETIC_COLUMN_BANK = StatementProfile(
    profile_id="syntheticcolumnbank_pdf",
    version="syntheticcolumnbank-pdf/v1",
    institution="Synthetic Column Bank",
    detect=(re.compile(r"syntheticcolumnbank\.invalid", re.I),),
    sign_mode=COLUMN_POSITION,
    columns=(
        AmountColumn("withdrawal", "withdrawn"),
        AmountColumn("deposit", "deposited"),
        AmountColumn("balance", "Balance"),
    ),
    period=re.compile(r"(\w+ \d{1,2}) to (\w+ \d{1,2}, \d{4})"),
    account_last4=re.compile(r"account number:?\s*([\d ]{8,})", re.I),
    totals=(
        TotalSpec("opening_balance", re.compile(r"Opening Balance on [^$]{0,40}\$([\d,]+\.\d{2})")),
        TotalSpec("debits", re.compile(r"Minus total withdrawals\s*\$([\d,]+\.\d{2})")),
        TotalSpec("credits", re.compile(r"Plus total deposits\s*\$([\d,]+\.\d{2})")),
        TotalSpec("closing_balance", re.compile(r"Closing Balance on [^$]{0,40}\$([\d,]+\.\d{2})")),
    ),
    body_start=("Here's what happened",),
    date_pattern=_DAY,
    detail_lines=True,
    # Rotated print artifacts render at x~21 on the same y-band as real rows.
    content_x_min=60.0,
    footer_fraction=0.94,
)


SYNTHETIC_CHECKING_CHEQUING = StatementProfile(
    profile_id="synthetic_checking_chequing_pdf",
    version="synthetic_checking-chequing-pdf/v1",
    institution="Synthetic Checking",
    detect=(re.compile(r"Synthetic Checking Account Statement", re.I),),
    sign_mode=COLUMN_POSITION,
    columns=(
        AmountColumn("withdrawal", "Withdrawals"),
        AmountColumn("deposit", "Deposits"),
        AmountColumn("balance", "Balance"),
    ),
    period=re.compile(r"For (\w+ \d{1,2}) to (\w+ \d{1,2}, \d{4})"),
    account_last4=re.compile(r"Account number:?\s*([\d\-]{5,})"),
    totals=(
        TotalSpec("opening_balance", re.compile(r"Opening balance on [^$]{0,30}\$([\d,]+\.\d{2})")),
        TotalSpec("debits", re.compile(r"Withdrawals\s+-\s*([\d,]+\.\d{2})")),
        TotalSpec("credits", re.compile(r"Deposits\s+\+\s*([\d,]+\.\d{2})")),
        TotalSpec("closing_balance", re.compile(r"Closing balance on [^$]{0,30}=?\s*\$([\d,]+\.\d{2})")),
    ),
    body_start=("Transaction details",),
    date_pattern=_DAY,
    detail_lines=True,
    max_detail_lines=2,
    # Synthetic Checking omits the date on subsequent same-day rows; an undated line may be a
    # whole transaction. The balance column, not the date, decides.
    date_carry_forward=True,
    footer_fraction=0.90,
)


PROFILES: tuple[StatementProfile, ...] = (
    EXAMPLE_MASTERCARD,
    EXAMPLE_SAVINGS_BANK,
    SYNTHETIC_COLUMN_BANK,
    SYNTHETIC_CHECKING_CHEQUING,
)

PROFILES_BY_ID = {profile.profile_id: profile for profile in PROFILES}


SYNTHETIC_CHECKING_CARD = StatementProfile(
    profile_id="synthetic_checking_card_pdf",
    version="synthetic_checking-card-pdf/v1",
    institution="Synthetic Checking",
    detect=(re.compile(r"Synthetic Checking\s+\w*\s*World\s+Mastercard|Synthetic Checking Cardholder Agreement", re.I),),
    sign_mode=SECTION,
    liability=True,
    columns=(AmountColumn("amount", "Amount", "Amount($)"),),
    period=re.compile(r"(\w+ \d{1,2}) to (\w+ \d{1,2}, \d{4})"),
    account_last4=re.compile(r"Account number\s+([\dX]{4}(?:[\sX\d]{4,}))"),
    totals=(
        TotalSpec("previous_balance", re.compile(r"Previous balance\s+\$([\d,]+\.\d{2})")),
        TotalSpec("credits", re.compile(r"Total credits\s+-\s*\$([\d,]+\.\d{2})")),
        TotalSpec("debits", re.compile(r"Total charges\s+\+\s*\$([\d,]+\.\d{2})")),
        TotalSpec("closing_balance", re.compile(r"Total balance\s+=\s*\$([\d,]+\.\d{2})")),
    ),
    body_start=("Transactions",),
    body_stop=("Information about your",),
    pseudo_rows=("Total payments", "Total purchases", "Total charges", "Total credits"),
    # Direction comes from the heading a run of rows sits under.
    section_directions={"your payments": -1, "your credits": -1, "your purchases": 1},
    date_pattern=_DAY,
    footer_fraction=0.92,
)

PROFILES = PROFILES + (SYNTHETIC_CHECKING_CARD,)
PROFILES_BY_ID = {profile.profile_id: profile for profile in PROFILES}
