"""Descriptor affix classification.

Cases are drawn from the shapes real Canadian card and chequing statements
produce. Values are synthetic.
"""
from __future__ import annotations

import pytest

from app.reconcile.descriptor_affixes import classify


@pytest.mark.parametrize(
    "raw,expected,processor",
    [
        ("SQ *SYNTHETIC BAKERY", "SYNTHETIC BAKERY", "square"),
        ("TST-Synthetic Tea Shop", "SYNTHETIC TEA SHOP", "toast"),
        ("SP+AFF* SYNTHETIC TEA INC", "SYNTHETIC TEA INC", "stripe"),
        ("SP SYNTHETIC DEVICE", "SYNTHETIC DEVICE", "stripe"),
        ("LSP*Synthetic Components Inc", "SYNTHETIC COMPONENTS INC", "lightspeed"),
        ("PAYPAL *Synthetic Art", "SYNTHETIC ART", "paypal"),
    ],
)
def test_processor_prefix_is_stripped_and_named(raw, expected, processor):
    result = classify(raw)
    assert result.merchant_text == expected
    assert result.processor == processor
    assert result.platform == ""


def test_marketplace_is_kept_as_the_merchant_not_stripped_as_a_processor():
    """The order code is noise; ExampleMarket is the merchant of record."""
    result = classify("EXMP MARKET*SK1KG11G3")
    assert result.merchant_text == "EXAMPLEMARKET"
    assert result.platform == "ExampleMarket"
    assert "SK1KG11G3" not in result.merchant_text


def test_marketplace_variants_collapse_to_one_key():
    assert classify("EXMP MARKET*F02GI5E03").merchant_text == "EXAMPLEMARKET"
    assert classify("ExampleMarket*R791X4SY3").merchant_text == "EXAMPLEMARKET"


def test_category_bearing_sub_brands_are_preserved():
    """Rideshare and food delivery share a prefix and must not merge."""
    trip = classify("EXAMPLE RIDE/EXAMPLERIDE")
    eats = classify("EXAMPLE RIDE/EXAMPLEEATS")
    assert trip.platform == eats.platform == "ExampleRide"
    assert trip.merchant_text != eats.merchant_text
    assert trip.merchant_text == "EXAMPLERIDE"
    assert eats.merchant_text == "EXAMPLERIDE EXAMPLEEATS"


def test_store_numbers_and_masked_accounts_are_noise():
    assert classify("EXAMPLE DISCOUNT # 261").merchant_text == "EXAMPLE DISCOUNT"
    assert classify("EXAMPLE DISCOUNT # 855").merchant_text == "EXAMPLE DISCOUNT"
    assert classify("EXAMPLE MOBILE ******9004").merchant_text == "EXAMPLE MOBILE"


def test_transit_fare_codes_collapse_to_the_operator():
    assert classify("EXAMPLE TRANSIT/S2WKX977G7").merchant_text == "EXAMPLETRANSIT"
    assert classify("EXAMPLE TRANSIT/S8ZQZ79ZSF").merchant_text == "EXAMPLETRANSIT"


def test_case_variants_collapse():
    assert classify("SyntheticVerse").merchant_text == classify("SYNTHETICVERSE").merchant_text


def test_unidentifiable_descriptors_are_flagged_rather_than_guessed():
    assert classify("SP SYNTHETIC NOISE").needs_resolution is False  # a name, just unknown
    assert classify("").needs_resolution
    # A clean string is not necessarily an identified merchant, but it is not
    # syntactically unresolvable either.
    assert not classify("SYNTHETIC CLIMBING").needs_resolution


def test_classification_is_bounded_and_total():
    result = classify("X" * 5000)
    assert len(result.raw) == 5000
    assert len(result.merchant_text) <= 512


def test_platform_name_is_not_repeated_when_the_remainder_restates_it():
    result = classify("IC* SYNTHETICCART")
    assert result.platform == "SyntheticCart"
    assert result.merchant_text == "SYNTHETICCART"


def test_paypal_card_verification_is_not_a_merchant():
    """PP*NNNNCODE is the code you enter in PayPal to confirm a linked card.

    It names no merchant and is reversed on the same statement, so sending it
    to resolution only invites a model to read a brand out of a code.
    """
    result = classify("PP*9003CODE")
    assert result.is_merchant is False
    assert result.non_merchant_kind == "paypal_card_verification"
    assert result.merchant_text == ""
    assert result.needs_resolution is False
    assert result.processor == "paypal"


def test_processor_is_recovered_from_the_locality_service_number():
    """Issuers print the processor's phone number in the locality column."""
    result = classify("synthetic-art.example.invalid", locality="0000000000 HKG")
    assert result.processor == "paypal"
    assert "processor_phone:paypal" in result.applied


def test_locality_without_a_known_service_number_changes_nothing():
    assert classify("SYNTHETIC CLIMBING", locality="EXAMPLEVILLE EX").processor == ""


def test_transaction_type_prefixes_are_stripped_to_the_counterparty():
    """The query sanitizer rejects banking words such as 'debit' outright, so a
    descriptor that keeps its prefix cannot be looked up at all."""
    assert classify("PREAUTHORIZED DEBIT EXAMPLEVILLE HYDRO").merchant_text == "EXAMPLEVILLE HYDRO"
    assert classify("Auto-withdrawal by EXAMPLE BANK").merchant_text == "EXAMPLE BANK"
    assert classify("Payroll dep. Synthetic Payroll").merchant_text == "SYNTHETIC PAYROLL"
    assert classify("DEPOSIT SYNTHETIC DEPOSIT").merchant_text == "SYNTHETIC DEPOSIT"


def test_a_bare_transaction_type_names_no_merchant():
    for descriptor in ("WITHDRAWAL", "SERVICE CHARGE", "SERVICE CHARGE DISCOUNT",
                       "Interest", "Mortgage payment #000000-0"):
        result = classify(descriptor)
        assert result.is_merchant is False, descriptor
        assert result.merchant_text == ""


def test_transfers_never_become_a_searchable_merchant():
    """A transfer names one of the household's own accounts, or a person.

    Stripping the prefix would leave a person's name or an account number, and the
    whole point of the sanitizer is that neither ever reaches a search engine.
    """
    for descriptor in ("Interac e-Transfer sent to Sample Member A",
                       "Transfer from Synthetic Checking Chequin",
                       "Transfer to 00000 00000 00 Savings Finder"):
        result = classify(descriptor)
        assert result.is_merchant is False, descriptor
        assert result.merchant_text == ""
        assert result.non_merchant_kind.startswith("transaction_")
