"""Classify the affixes banks bolt onto a merchant descriptor.

A statement descriptor is rarely just a merchant. It carries a payment
facilitator prefix, a store or terminal number, an order reference, and often a
trailing locality. Stripping those blindly destroys merchant identity, so this
module separates two things that look identical and are not:

``PROCESSOR``
    A payment facilitator. ``SQ *``, ``SP ``, ``TST-``, ``PP*`` name *who moved
    the money*, not who you bought from. Strip the prefix and keep it as scope.

``PLATFORM``
    A marketplace that *is* the merchant of record for categorisation.
    ``EXMP MARKET*SK1KG11G3`` is ExampleMarket, not ``SK1KG11G3``. Keep the name and
    strip only the order reference.

This layer deliberately does not decide what a merchant *is*. It removes known
noise and labels known prefixes so that resolution -- deterministic or
otherwise -- starts from the most informative string available, and so that an
unidentifiable descriptor is recognisable as such rather than silently reduced
to an order code.

It is separate from :mod:`app.reconcile.descriptor_normalization`, which
produces the identity fingerprint and must not change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_DESCRIPTOR_CHARS = 512

# (name, prefix pattern). Order matters: the first match wins.
PROCESSORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("square", re.compile(r"^SQ\s*\*\s*", re.I)),
    ("toast", re.compile(r"^TST-\s*", re.I)),
    ("stripe", re.compile(r"^SP\+AFF\*\s*", re.I)),
    ("stripe", re.compile(r"^SP\s+(?=[A-Za-z])")),
    ("paypal", re.compile(r"^(?:PAYPAL\s*\*|PP\*)\s*", re.I)),
    ("lightspeed", re.compile(r"^LSP\*\s*", re.I)),
    ("global-e", re.compile(r"^Global-e\s*/\s*", re.I)),
)

PLATFORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ExampleMarket", re.compile(r"^(?:EXMP\s+MARKET|ExampleMarket)\b\s*\*?\s*", re.I)),
    ("SyntheticCart", re.compile(r"^IC\*\s*", re.I)),
    ("SyntheticChat", re.compile(r"^DISCORD\s*\*\s*", re.I)),
    ("ExampleRide", re.compile(r"^EXAMPLE\s+RIDE\s*/\s*", re.I)),
    ("ExampleTransit", re.compile(r"^EXAMPLE\s+TRANSIT\s*/\s*", re.I)),
)

# Bank statements label the *kind* of movement before naming the counterparty.
# These are not merchants, and leaving them attached ruins the search query --
# and the query sanitizer rejects banking words such as "debit" outright, so a
# descriptor keeping its prefix cannot be looked up at all.
TRANSACTION_PREFIXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preauthorized_debit", re.compile(r"^PRE-?AUTHORI[SZ]ED\s+DEBIT\b\s*", re.I)),
    ("auto_withdrawal", re.compile(r"^AUTO-?\s*WITHDRAWAL(?:\s+BY)?\b\s*", re.I)),
    ("direct_deposit", re.compile(r"^DIRECT\s+DEPOSIT(?:\s+FROM)?\b\s*", re.I)),
    ("payroll", re.compile(r"^PAYROLL\s+DEP\.?\b\s*", re.I)),
    ("service_charge", re.compile(r"^SERVICE\s+CHARGE(?:\s+DISCOUNT)?\b\s*", re.I)),
    ("interac_transfer", re.compile(r"^(?:INTERAC\s+)?E-?TRANSFER(?:\s+(?:SENT|RECEIVED)?\s*(?:TO|FROM)?)?\b\s*", re.I)),
    ("internal_transfer", re.compile(r"^TRANSFER\s+(?:TO|FROM)\b\s*", re.I)),
    ("internal_transfer", re.compile(r"^INTERNET\s+TRANSFER\b\s*", re.I)),
    ("bill_payment", re.compile(r"^INTERNET\s+BILL\s+PAY\b\s*", re.I)),
    ("opening_balance", re.compile(r"^OPENING\s+BALANCE\b\s*", re.I)),
    ("closing_balance", re.compile(r"^CLOSING\s+BALANCE\b\s*", re.I)),
    ("payroll", re.compile(r"^PAY\s+\d{4}-SALARY-\S+\s*", re.I)),
    ("mortgage_payment", re.compile(r"^MORTGAGE\s+PAYMENT\b\s*", re.I)),
    ("utility_bill", re.compile(r"^(?:UTILITY|HYDRO)\s+BILL\b\s*", re.I)),
    ("loan_payment", re.compile(r"^LOANS?\b\s*", re.I)),
    ("deposit", re.compile(r"^DEPOSIT\b\s*", re.I)),
    ("withdrawal", re.compile(r"^WITHDRAWAL\b\s*", re.I)),
    ("interest", re.compile(r"^INTEREST(?:\s+RECEIVED)?\b\s*", re.I)),
)

# Movements between the household's own accounts, or to a named person. The
# counterparty is an account or a human being, never a merchant, so these must
# never reach a search engine no matter what follows the prefix.
NEVER_A_MERCHANT = frozenset({
    "internal_transfer",
    "interac_transfer",
    "service_charge",
    "withdrawal",
    "interest",
    "opening_balance",
    "closing_balance",
})

NOISE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("store_number", re.compile(r"\s*#\s*\d+\b")),
    ("masked_account", re.compile(r"\s*\*{4,}\d+\b")),
    # Every code rule requires a digit. A purely alphabetic suffix can be a
    # category-bearing sub-brand -- EXAMPLE RIDE/EXAMPLEEATS and /EXAMPLERIDE differ
    # only there, and collapsing them merges food delivery into rideshare.
    ("order_ref", re.compile(r"\s*\*(?=[A-Z0-9]*\d)[A-Z0-9]{6,}\b")),
    ("code_token", re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{7,}\b")),
    # A standalone run of six or more digits is a reference or confirmation
    # number. Shorter runs are left alone, because store numbers are meaningful.
    ("reference_number", re.compile(r"\b\d{6,}\b")),
)

CODE_LIKE = re.compile(r"^(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,}$")
MAX_PASSES = 3

# Descriptors that are not a merchant at all. Recognising these deterministically
# keeps them away from resolution entirely, rather than offering a model a string
# it will try to read a brand out of.
NON_MERCHANT = (
    # PayPal card verification: the digits are the code you enter in PayPal to
    # confirm a linked card, and the charge is reversed. It names no merchant.
    ("paypal_card_verification", re.compile(r"^(?:PP\*|PAYPAL\s*\*)\s*\d{4}CODE\b", re.I)),
)

# Processor service numbers that issuers print in the locality column. The number
# identifies the processor even when the merchant text does not.
PROCESSOR_PHONE = {
    "0000000000": "paypal",
}


@dataclass(frozen=True)
class DescriptorAffixes:
    raw: str
    merchant_text: str
    processor: str = ""
    platform: str = ""
    applied: tuple[str, ...] = ()
    non_merchant_kind: str = ""
    transaction_kind: str = ""

    @property
    def is_merchant(self) -> bool:
        """False when the descriptor names something other than a merchant."""
        return not self.non_merchant_kind

    @property
    def needs_resolution(self) -> bool:
        """True when nothing identifiable survived the cleanup.

        This is a syntactic signal only. A clean string is not the same as an
        identified merchant, and callers must not read it as one.
        """
        if self.non_merchant_kind:
            # Recognised and explained, so there is nothing left to resolve.
            return False
        if not self.merchant_text or len(self.merchant_text) <= 3:
            return True
        return any(CODE_LIKE.match(token) for token in self.merchant_text.split())


def classify(raw: str, *, locality: str = "") -> DescriptorAffixes:
    text = str(raw or "").strip()[:MAX_DESCRIPTOR_CHARS]
    processor = ""
    platform = ""
    applied: list[str] = []

    for kind, pattern in NON_MERCHANT:
        if pattern.search(text):
            return DescriptorAffixes(
                raw=str(raw or ""),
                merchant_text="",
                processor="paypal" if "paypal" in kind else "",
                applied=(f"non_merchant:{kind}",),
                non_merchant_kind=kind,
            )

    transaction_kind = ""
    for kind, pattern in TRANSACTION_PREFIXES:
        if pattern.search(text):
            text = pattern.sub("", text, count=1).strip()
            transaction_kind = kind
            applied.append(f"transaction:{kind}")
            break

    digits = "".join(c for c in str(locality or "") if c.isdigit())
    if digits in PROCESSOR_PHONE:
        processor = PROCESSOR_PHONE[digits]
        applied.append(f"processor_phone:{processor}")

    for name, pattern in PROCESSORS:
        if pattern.search(text):
            text = pattern.sub("", text, count=1)
            processor = name
            applied.append(f"processor:{name}")
            break

    for name, pattern in PLATFORMS:
        if not pattern.search(text):
            continue
        remainder = pattern.sub("", text, count=1).strip()
        platform = name
        applied.append(f"platform:{name}")
        compact = re.sub(r"[^A-Z0-9]", "", remainder.upper())
        redundant = compact == re.sub(r"[^A-Z0-9]", "", name.upper())
        # Keep a meaningful sub-brand; drop a bare order code or a remainder
        # that merely repeats the platform name.
        text = (
            name
            if not remainder or redundant or CODE_LIKE.match(compact)
            else f"{name} {remainder}"
        )
        break

    for _ in range(MAX_PASSES):
        before = text
        for label, pattern in NOISE:
            if pattern.search(text):
                text = pattern.sub(" ", text)
                applied.append(f"noise:{label}")
        text = " ".join(text.split())
        if text == before:
            break

    key = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    if transaction_kind and (
        transaction_kind in NEVER_A_MERCHANT
        or not key
        or key.replace(" ", "").isdigit()
    ):
        # The descriptor was only ever a movement type, with no counterparty.
        return DescriptorAffixes(
            raw=str(raw or ""),
            merchant_text="",
            processor=processor,
            applied=tuple(applied),
            non_merchant_kind=f"transaction_{transaction_kind}",
            transaction_kind=transaction_kind,
        )
    return DescriptorAffixes(
        raw=str(raw or ""),
        merchant_text=key,
        processor=processor,
        platform=platform,
        applied=tuple(applied),
        transaction_kind=transaction_kind,
    )
