"""Geometry-aware extraction engine for text-layer PDF statements.

The engine never rasterizes and never calls a model. It reads the PDF's own text
layer together with each word's bounding box, because for several issuers the
geometry *is* the data: Synthetic Column Bank and Synthetic Checking print withdrawals and deposits as
unsigned numbers distinguished only by which column they sit in. Flattening the
page to text destroys that distinction silently.

Every row carries its source rectangle so a reviewer can be shown exactly where
a value came from, and so a failed arithmetic check can point at the offending
line on the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import fitz

from .pdf_profiles import COLUMN_POSITION, EXPLICIT_SIGN, SECTION, StatementProfile

_AMOUNT = re.compile(r"^-?\$?-?[\d,]+\.\d{2}-?$")
_MONTH_NUMBER = {
    m: i for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1
    )
}


@dataclass
class SourceBox:
    """A region of a page, normalized to 0-1 so it survives any render scale."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "page": self.page,
            "x0": round(self.x0, 5),
            "y0": round(self.y0, 5),
            "x1": round(self.x1, 5),
            "y1": round(self.y1, 5),
        }


MAX_DETAIL_CHARS = 240


@dataclass
class EngineRow:
    line_number: int
    date_text: str
    description: str
    transacted_text: str = ""
    amounts: dict[str, int] = field(default_factory=dict)
    merchant: str = ""
    city: str = ""
    section: str = ""
    date_inherited: bool = False
    boxes: list[SourceBox] = field(default_factory=list)
    detail: str = ""
    detail_line_count: int = 0

    @property
    def signed_amount_cents(self) -> int:
        if "amount" in self.amounts:
            return self.amounts["amount"]
        return self.amounts.get("deposit", 0) - self.amounts.get("withdrawal", 0)

    @property
    def balance_cents(self) -> int | None:
        return self.amounts.get("balance")


def to_cents(text: str) -> int:
    """Parse a printed amount, honouring leading and trailing minus signs."""
    raw = text.strip()
    negative = raw.startswith("-") or raw.endswith("-")
    digits = raw.strip("-").lstrip("$").replace(",", "").replace(".", "")
    if not digits.isdigit():
        raise ValueError(f"unparseable amount: {text!r}")
    value = int(digits)
    return -value if negative else value


def _lines(page, profile: StatementProfile, page_number: int):
    """Cluster a page's words into visual lines, keeping geometry."""
    height = page.rect.height or 1.0
    top = height * profile.header_fraction
    bottom = height * profile.footer_fraction
    buckets: dict[int, list] = {}
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if x0 < profile.content_x_min:
            continue
        if not (top <= y0 <= bottom):
            continue
        buckets.setdefault(round((y0 + y1) / 2 / 2.5), []).append((x0, y0, x1, y1, word))
    for key in sorted(buckets):
        yield sorted(buckets[key], key=lambda item: item[0]), page_number


def _box(words, page_number: int, page) -> SourceBox:
    width = page.rect.width or 1.0
    height = page.rect.height or 1.0
    return SourceBox(
        page=page_number,
        x0=min(w[0] for w in words) / width,
        y0=min(w[1] for w in words) / height,
        x1=max(w[2] for w in words) / width,
        y1=max(w[3] for w in words) / height,
    )


def column_edges(doc, profile: StatementProfile) -> dict[str, float]:
    """Locate each amount column by its header token's right edge.

    Amounts are right-aligned under their header, so the right edge is the
    stable anchor; header text and amount text rarely share a left margin.
    """
    wanted = {column.header_token.lower(): column for column in profile.columns}
    for page_number, page in enumerate(doc):
        for words, _ in _lines(page, profile, page_number):
            found: dict[str, float] = {}
            for index, (x0, y0, x1, y1, word) in enumerate(words):
                column = wanted.get(word.lower())
                if column is None or column.name in found:
                    continue
                edge = x1
                # A trailing unit marker such as "($)" extends the header.
                if index + 1 < len(words) and words[index + 1][4] == column.unit_token:
                    edge = words[index + 1][2]
                found[column.name] = edge
            if len(found) == len(profile.columns):
                return found
    raise LookupError("statement column headers were not found")


def _year_lookup(period_start: str, period_end: str):
    """Rows print 'Jul 17' with no year; infer it, handling a December rollover."""
    start = date.fromisoformat(period_start) if period_start else None
    end = date.fromisoformat(period_end) if period_end else None

    def resolve(month_name: str) -> int | None:
        month = _MONTH_NUMBER.get(month_name[:3].title())
        if month is None:
            return None
        for candidate in (start, end):
            if candidate is not None and candidate.month == month:
                return candidate.year
        if start is not None and end is not None and start.year != end.year:
            return start.year if month >= start.month else end.year
        return (end or start).year if (end or start) else None

    return resolve


PROVINCES = frozenset(
    "ON QC BC AB MB SK NS NB NL PE NT YT NU".split()
)


def _split_city(words, profile: StatementProfile) -> tuple[str, str]:
    """Separate the merchant from the trailing city/province it shares a column with.

    A trailing province code is the reliable anchor. The inter-token gap is the
    fallback for foreign localities that print no province, and it is only a
    heuristic: issuers tighten the spacing when the merchant name runs long.
    """
    tokens = [w[4] for w in words]
    if len(tokens) >= 3 and tokens[-1].upper() in PROVINCES:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    if profile.province_anchored_city and len(tokens) >= 3:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    if profile.city_gap:
        for index in range(1, len(words)):
            if words[index][0] - words[index - 1][2] > profile.city_gap:
                return (
                    " ".join(tokens[:index]).strip(),
                    " ".join(tokens[index:]).strip(),
                )
    return " ".join(tokens).strip(), ""


def extract_rows(
    doc,
    profile: StatementProfile,
    *,
    period_start: str = "",
    period_end: str = "",
    max_rows: int = 10_000,
) -> list[EngineRow]:
    edges = column_edges(doc, profile)
    resolve_year = _year_lookup(period_start, period_end)
    rows: list[EngineRow] = []
    in_body = not profile.body_start
    section = ""
    direction = 1
    last_date = ""
    current: EngineRow | None = None
    line_number = 0

    for page_number, page in enumerate(doc):
        # A wrapped description never spans a page break, so a new page starts
        # with no row open to append to.
        current = None
        for words, _ in _lines(page, profile, page_number):
            text = " ".join(w[4] for w in words).strip()
            if not text:
                continue
            if any(marker.lower() in text.lower() for marker in profile.body_stop):
                in_body = False
                current = None
                continue
            if not in_body:
                if any(marker.lower() in text.lower() for marker in profile.body_start):
                    in_body = True
                continue

            if profile.sign_mode == SECTION and profile.section_directions:
                heading = text.strip().lower()
                for marker, multiplier in profile.section_directions.items():
                    if heading.startswith(marker):
                        # Amounts here are unsigned; the heading is the only
                        # thing saying whether they add to or reduce the balance.
                        direction = multiplier
                        current = None
                        break

            if profile.section_pattern is not None:
                match = profile.section_pattern.search(text)
                if match:
                    # A card section governs every row until the next header,
                    # including across page breaks -- the page header names the
                    # ACCOUNT, not the card, so it must not reset this.
                    section = match.group(1)
                    current = None
                    continue

            if any(marker.lower() in text.lower() for marker in profile.pseudo_rows):
                current = None
                continue

            if any(pattern.search(text) for pattern in profile.continuation):
                if current is not None:
                    current.detail = f"{current.detail} {text}".strip()[:MAX_DETAIL_CHARS]
                    current.boxes.append(_box(words, page_number, page))
                continue

            numeric = [(w[2], w[4]) for w in words if _AMOUNT.match(w[4])]
            body = [w for w in words if not _AMOUNT.match(w[4])]

            dates: list[str] = []
            if profile.date_pattern is not None:
                while len(dates) < profile.date_columns and len(body) >= 2:
                    candidate = f"{body[0][4]} {body[1][4]}"
                    if not profile.date_pattern.match(candidate):
                        break
                    dates.append(candidate)
                    body = body[2:]
            # The posting date is what the statement's own ordering and period
            # are built on, so it is the one rows are filed under.
            date_text = dates[-1] if dates else ""
            transacted_text = dates[0] if dates else ""

            assigned: dict[str, int] = {}
            for x_right, token in numeric:
                name = min(edges, key=lambda key: abs(edges[key] - x_right))
                value = to_cents(token)
                if profile.sign_mode == SECTION and name != "balance":
                    value = abs(value) * direction
                assigned[name] = value

            has_balance = "balance" in assigned
            has_value = any(key != "balance" for key in assigned)

            if not date_text:
                # An undated line is a new transaction only when it carries a
                # balance; otherwise it annotates the row above. Get this wrong
                # and whole transactions vanish into a description.
                is_row = profile.date_carry_forward and has_balance
                if not is_row:
                    if (
                        profile.detail_lines
                        and current is not None
                        and body
                        and current.detail_line_count < profile.max_detail_lines
                    ):
                        current.detail = (
                            f"{current.detail} {' '.join(w[4] for w in body)}"
                        ).strip()[:MAX_DETAIL_CHARS]
                        current.detail_line_count += 1
                        current.boxes.append(_box(words, page_number, page))
                    continue
                date_text = last_date
                if not date_text:
                    continue

            if not (has_value or has_balance):
                continue

            last_date = date_text
            merchant, city = _split_city(body, profile)
            if profile.sign_mode == EXPLICIT_SIGN and "balance" in assigned and not has_value:
                continue

            row = EngineRow(
                line_number=line_number,
                date_text=_iso_date(date_text, resolve_year),
                transacted_text=_iso_date(transacted_text, resolve_year),
                description=" ".join(w[4] for w in body).strip(),
                amounts=assigned,
                merchant=merchant,
                city=city,
                section=section,
                date_inherited=date_text == last_date and not date_text,
                boxes=[_box(words, page_number, page)],
            )
            rows.append(row)
            current = row
            line_number += 1
            if len(rows) > max_rows:
                raise ValueError("statement exceeds the maximum row count")

    if profile.sign_mode == COLUMN_POSITION:
        for row in rows:
            row.amounts.setdefault("withdrawal", 0)
            row.amounts.setdefault("deposit", 0)
    return rows


def _iso_date(text: str, resolve_year) -> str:
    parts = text.split()
    if len(parts) != 2:
        return ""
    month = _MONTH_NUMBER.get(parts[0][:3].title())
    year = resolve_year(parts[0])
    if month is None or year is None:
        return ""
    try:
        return date(year, month, int(parts[1])).isoformat()
    except ValueError:
        return ""


def read_totals(doc, profile: StatementProfile) -> dict[str, int]:
    """Read the statement's own summary box; these anchor the arithmetic gate.

    Matching runs per visual line, not over the raw text stream. Summary boxes
    sit in multi-column layouts where the extraction order interleaves labels
    and values from different columns, which silently pairs a label with a
    neighbouring column's number.
    """
    totals: dict[str, int] = {}
    lines: list[str] = []
    for page_number, page in enumerate(doc):
        for words, _ in _lines(page, profile, page_number):
            lines.append(" ".join(w[4] for w in words))
    for spec in profile.totals:
        for line in lines:
            match = spec.pattern.search(line)
            if match:
                totals[spec.name] = to_cents(match.group(1))
                break
    return totals
