from __future__ import annotations

import subprocess
from base64 import b64encode
from dataclasses import FrozenInstanceError
from email import policy
from email.message import EmailMessage

import dkim
import dns.exception
import dns.message
import dns.resolver
import dns.rrset
import pytest
from pydantic import ValidationError

from app.banking import rogers_message
from app.banking.rogers_message import (
    MAX_MESSAGE_BYTES,
    Alert,
    AlertFormat,
    MessageRejected,
    MessageRetry,
    parse_message,
)

SENDER = "alerts@example.invalid"
RECIPIENT = "original@example.invalid"
DOMAIN = "example.invalid"
SUBJECT = "Synthetic purchase alert"
MESSAGE_ID = "<generated-1@example.invalid>"
BODY = "Card: 4321\nPurchase: CAD 12.34\nMerchant: Fictional Books\nAuthorized: 2026-03-04\n"
PATTERN = (
    r"Card: (?P<card_last4>[^\n]+)\nPurchase: (?P<currency>[^ ]+) (?P<amount>[^\n]+)\n"
    r"Merchant: (?P<merchant>[^\n]+)\nAuthorized: (?P<authorized_on>[^\n]+)"
)
SIGNED_HEADERS = (
    b"from", b"to", b"subject", b"message-id", b"mime-version", b"content-type", b"content-transfer-encoding"
)


@pytest.fixture(scope="module")
def signing_keys():
    # Create a new, test-only key in memory; never read or write credential files.
    private_key = subprocess.run(
        ["openssl", "genrsa", "2048"], check=True, capture_output=True, timeout=10
    ).stdout
    public_key = subprocess.run(
        ["openssl", "pkey", "-pubout", "-outform", "DER"], input=private_key,
        check=True, capture_output=True, timeout=10,
    ).stdout
    return _SigningKeys((private_key, b"v=DKIM1; k=rsa; p=" + b64encode(public_key)))


class _SigningKeys(tuple):
    def __repr__(self):
        return "<generated signing material>"


@pytest.fixture
def fmt():
    return AlertFormat(sender=SENDER, recipient=RECIPIENT, signing_domain=DOMAIN, subject=SUBJECT, body_pattern=PATTERN)


def _raw(body=BODY, *, message_id=MESSAGE_ID, sender=SENDER, recipient=RECIPIENT, subject=SUBJECT, cte=None):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = f"Synthetic Alerts <{sender}>"
    message['To'] = recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content(body, charset="utf-8", cte=cte)
    return message.as_bytes()


def _signed(raw, private_key, *, headers=SIGNED_HEADERS, domain=DOMAIN, selector=b"fixture", **kwargs):
    return dkim.sign(
        raw, selector, domain.encode("ascii"), private_key, include_headers=headers, **kwargs
    ) + raw


def _dns(public_record, names):
    def lookup(name: bytes, *, timeout: float) -> bytes:
        assert timeout > 0
        names.append(name)
        return public_record

    return lookup


def test_verified_original_survives_intact_forwarding(signing_keys, fmt):
    private_key, key_record = signing_keys
    names = []
    stages = []
    raw = _signed(_raw(), private_key)
    forwarded = b"Received: from relay.invalid by mailbox.invalid\r\nResent-To: inbox@example.invalid\r\n" + raw

    def lookup(name, *, timeout):
        stages.append("resolve")
        return _dns(key_record, names)(name, timeout=timeout)

    alert = parse_message(forwarded, fmt, dns_lookup=lookup, check_allowed=lambda: stages.append("policy"))
    assert alert == Alert(
        message_key=alert.message_key, authorized_on="2026-03-04", card_last4="4321",
        amount_cents=-1234, currency="CAD", merchant="Fictional Books",
    )
    assert len(alert.message_key) == 64 and MESSAGE_ID not in alert.message_key
    assert names == [b"fixture._domainkey.example.invalid."]
    assert stages == ["policy", "resolve"]


def test_exact_replay_collapses_but_equal_signed_alerts_with_other_ids_do_not(signing_keys, fmt):
    private_key, record = signing_keys
    first = _signed(_raw(), private_key)
    equal = _signed(_raw(message_id="<generated-2@example.invalid>"), private_key)
    parse = lambda raw: parse_message(raw, fmt, dns_lookup=_dns(record, []))
    original = parse(first)
    assert original == parse(first) == parse(b"Received: by forwarding.invalid\r\n" + first)
    second = parse(equal)
    assert original.message_key != second.message_key
    assert (original.authorized_on, original.card_last4, original.amount_cents, original.currency, original.merchant) == (
        second.authorized_on, second.card_last4, second.amount_cents, second.currency, second.merchant
    )


def test_signed_plain_alternative_ignores_html_and_refuses_attachments(signing_keys, fmt):
    message = EmailMessage(policy=policy.SMTP)
    message['From'], message['Subject'], message['Message-ID'] = SENDER, SUBJECT, MESSAGE_ID
    message['To'] = RECIPIENT
    message.set_content(BODY, cte='base64')
    message.add_alternative('<img src="https://example.invalid/a">Different merchant', subtype='html')
    private, record = signing_keys
    raw = _signed(message.as_bytes(), private)
    actual = parse_message(raw, fmt, dns_lookup=_dns(record, []))
    assert (actual.merchant, actual.amount_cents) == ('Fictional Books', -1234)
    _assert_rejected(raw.replace(b'Different merchant', b'Different merchant!'), fmt, 'unauthenticated')
    next(message.iter_parts())['Content-Disposition'] = 'attachment; filename="original.txt"'
    _assert_rejected(_signed(message.as_bytes(), private), fmt, 'unsupported_format', _dns(record, []))


def test_message_key_is_scoped_by_verified_sender(signing_keys, fmt):
    private_key, record = signing_keys
    another_sender = "fixture-other@example.invalid"
    other_fmt = AlertFormat(**(fmt.model_dump() | {"sender": another_sender}))
    first = parse_message(_signed(_raw(), private_key), fmt, dns_lookup=_dns(record, []))
    second = parse_message(
        _signed(_raw(sender=another_sender), private_key), other_fmt, dns_lookup=_dns(record, [])
    )
    assert first.message_key != second.message_key


def _never_lookup(*args, **kwargs):
    pytest.fail("no DNS request is authorized for this input")


def _assert_rejected(raw, fmt, code, dns_lookup=_never_lookup):
    with pytest.raises(MessageRejected) as error:
        parse_message(raw, fmt, dns_lookup=dns_lookup)
    assert error.value.code == str(error.value) == code


def test_spoofed_authentication_results_is_not_a_signature(fmt):
    spoof = b"Authentication-Results: relay.invalid; dkim=pass header.d=example.invalid\r\n"
    _assert_rejected(spoof + _raw(), fmt, "unauthenticated")


def test_signed_original_recipient_not_forwarding_destination(signing_keys, fmt):
    private, record = signing_keys
    other = _signed(_raw(recipient='other@example.invalid'), private)
    _assert_rejected(b'Resent-To: original@example.invalid\r\n' + other, fmt, 'untrusted_recipient', _dns(record, []))


def test_inline_manual_forward_is_not_the_signed_original(signing_keys, fmt):
    quoted = "Forwarded original:\n" + _signed(_raw(), signing_keys[0]).decode("ascii")
    _assert_rejected(_raw(quoted), fmt, "unauthenticated")


def test_mailbox_and_domain_are_exact(signing_keys, fmt):
    private_key, _ = signing_keys
    wrong_sender = _signed(_raw(sender="Alerts@example.invalid"), private_key)
    _assert_rejected(wrong_sender, fmt, "untrusted_sender")
    wrong_domain = _signed(_raw(), private_key, domain="sub.example.invalid")
    _assert_rejected(wrong_domain, fmt, "unauthenticated")


def test_recursive_malformed_from_fails_closed(fmt):
    raw = _raw().replace(b"Synthetic Alerts <", b"(" * 650 + b"\r\n " + b"(" * 650 + b"<")
    _assert_rejected(raw, fmt, "untrusted_sender")


@pytest.mark.parametrize("header", SIGNED_HEADERS[1:])
def test_all_consumed_present_headers_must_be_in_the_valid_signature(signing_keys, fmt, header):
    covered = tuple(name for name in SIGNED_HEADERS if name != header)
    _assert_rejected(_signed(_raw(), signing_keys[0], headers=covered), fmt, "unauthenticated")


def test_signatures_cannot_combine_domain_and_header_authority(signing_keys, fmt):
    raw = _raw()
    private_key, _ = signing_keys
    covers_headers = _signed(raw, private_key, domain="relay.invalid")[:-len(raw)]
    correct_domain = _signed(raw, private_key, headers=(b"from", b"subject"))
    _assert_rejected(covers_headers + correct_domain, fmt, "unauthenticated")


@pytest.mark.parametrize("header,code", [
    (b"fRoM: alerts@example.invalid", "untrusted_sender"),
    (b"To: other@example.invalid", "untrusted_recipient"),
    (b"subject: Synthetic purchase alert", "unsupported_format"),
    (b"message-id: <alternate@example.invalid>", "unsupported_format"),
    (b"content-type: text/plain", "unsupported_format"),
    (b"content-transfer-encoding: base64", "unsupported_format"),
    (b"mime-version: 1.0", "unsupported_format"),
])
def test_duplicate_critical_headers_are_rejected_even_when_original_is_signed(signing_keys, fmt, header, code):
    _assert_rejected(header + b"\r\n" + _signed(_raw(), signing_keys[0]), fmt, code)


def test_tampered_body_and_signed_message_id_are_rejected(signing_keys, fmt):
    private_key, record = signing_keys
    raw = _signed(_raw(), private_key)
    _assert_rejected(raw.replace(b"12.34", b"82.34"), fmt, "unauthenticated")
    _assert_rejected(
        raw.replace(MESSAGE_ID.encode(), b"<alternate@example.invalid>"),
        fmt, "unauthenticated", _dns(record, []),
    )


def test_partial_body_signature_is_rejected_even_without_append(signing_keys, fmt):
    _assert_rejected(_signed(_raw(), signing_keys[0], length=True), fmt, "unauthenticated")


def test_signature_selector_and_candidate_budget_are_bounded(signing_keys, fmt):
    private_key, _ = signing_keys
    raw = _raw()
    _assert_rejected(_signed(raw, private_key, selector=b"bad.name"), fmt, "unauthenticated")
    signed = _signed(raw, private_key)
    signature = signed[:-len(raw)]
    _assert_rejected(signature * 20 + raw, fmt, "unauthenticated")


def test_eligible_second_signature_has_an_independent_guarded_resolution(signing_keys, fmt):
    private_key, record = signing_keys
    raw = _raw()
    first = _signed(raw, private_key, selector=b"missing")[:-len(raw)]
    calls = []

    def lookup(name, *, timeout):
        calls.append(name)
        if name.startswith(b"missing."):
            raise dns.exception.Timeout
        return record

    alert = parse_message(
        first + _signed(raw, private_key), fmt,
        dns_lookup=lookup, check_allowed=lambda: calls.append("guard"),
    )
    assert alert.amount_cents == -1234
    assert calls == ["guard", b"missing._domainkey.example.invalid.",
                     "guard", b"fixture._domainkey.example.invalid."]


def test_resolver_timeout_is_retryable_and_content_free(signing_keys, fmt):
    def unavailable(name, *, timeout):
        raise dns.exception.Timeout("synthetic resolver text not for display")

    with pytest.raises(MessageRetry, match="^dns_unavailable$"):
        parse_message(_signed(_raw(), signing_keys[0]), fmt, dns_lookup=unavailable)


def test_dkim_library_errors_do_not_log_message_or_key_material(signing_keys, fmt, caplog):
    caplog.set_level("DEBUG")
    raw = _signed(_raw(), signing_keys[0])
    _assert_rejected(raw, fmt, "unauthenticated", _dns(b"v=DKIM1; k=rsa; p=QUJDRA==", []))
    assert not caplog.records


@pytest.mark.parametrize("withdrawal", [TimeoutError, MessageRetry])
def test_policy_withdrawal_is_never_reclassified(signing_keys, fmt, withdrawal):
    stopped = withdrawal()

    def check():
        raise stopped

    with pytest.raises(withdrawal) as error:
        parse_message(_signed(_raw(), signing_keys[0]), fmt, dns_lookup=_never_lookup, check_allowed=check)
    assert error.value is stopped


@pytest.mark.parametrize("withdrawn", [False, True])
def test_system_dns_checks_policy_again_before_tcp_fallback(signing_keys, fmt, monkeypatch, withdrawn):
    private_key, record = signing_keys
    calls = []
    stopped = TimeoutError()

    def check():
        calls.append("guard")
        if withdrawn and calls == ["guard", "udp", "guard"]:
            raise stopped

    def udp(request, address, *, timeout, raise_on_truncation):
        calls.append("udp")
        request = dns.message.from_wire(request.to_wire())
        assert request.question[0].name.to_text() == "fixture._domainkey.example.invalid."
        assert address == "192.0.2.1" and raise_on_truncation and timeout > 0
        raise dns.message.Truncated

    def tcp(request, address, *, timeout):
        calls.append("tcp")
        assert address == "192.0.2.1" and timeout > 0
        response = dns.message.make_response(request)
        chunks = [(b'"' + record[i:i + 200] + b'"').decode() for i in range(0, len(record), 200)]
        response.answer.append(dns.rrset.from_text(request.question[0].name, 60, "IN", "TXT", " ".join(chunks)))
        return dns.message.from_wire(response.to_wire())

    class Resolver:
        nameservers = ["192.0.2.1"]

    monkeypatch.setattr(rogers_message.dns.resolver, "Resolver", Resolver)
    monkeypatch.setattr(rogers_message.dns.query, "udp", udp)
    monkeypatch.setattr(rogers_message.dns.query, "tcp", tcp)
    raw = _signed(_raw(), private_key)
    if withdrawn:
        with pytest.raises(TimeoutError) as error:
            parse_message(raw, fmt, check_allowed=check)
        assert error.value is stopped
        assert calls == ["guard", "udp", "guard"]
    else:
        assert parse_message(raw, fmt, check_allowed=check).amount_cents == -1234
        assert calls == ["guard", "udp", "guard", "tcp"]


@pytest.mark.parametrize("cte", ["7bit", "base64", "quoted-printable", "8bit"])
def test_supported_signed_plaintext_encodings_parse_identically(signing_keys, fmt, cte):
    private_key, record = signing_keys
    body = BODY if cte == "7bit" else BODY.replace("Fictional Books", "Librairie fictive Montréal")
    alert = parse_message(_signed(_raw(body, cte=cte), private_key), fmt, dns_lookup=_dns(record, []))
    assert alert.merchant == ("Fictional Books" if cte == "7bit" else "Librairie fictive Montréal")


def test_signed_canonical_whitespace_not_wire_whitespace_drives_the_pattern(signing_keys, fmt):
    private_key, record = signing_keys
    raw = _signed(_raw(), private_key, canonicalize=(b"relaxed", b"relaxed"))
    equivalent = raw.replace(b"purchase alert", b"purchase \t alert").replace(b"Fictional Books", b"Fictional\t Books")
    assert parse_message(raw, fmt, dns_lookup=_dns(record, [])) == parse_message(
        equivalent, fmt, dns_lookup=_dns(record, [])
    )


@pytest.mark.parametrize("transform", [
    lambda raw: raw.replace(b"text/plain", b"text/html"),
    lambda raw: raw.replace(b"text/plain", b"message/rfc822"),
    lambda raw: raw.replace(b"text/plain", b"multipart/mixed"),
    lambda raw: raw.replace(b"charset=\"utf-8\"", b"charset=\"utf-8\"; charset=\"us-ascii\""),
    lambda raw: raw.replace(b"7bit", b"base64"),
    lambda raw: raw.replace(b"7bit", b"quoted-printable").replace(b"Fictional", b"Fictional=QZ"),
    lambda raw: b"Content-Disposition: attachment; filename=fixture.txt\r\n" + raw,
])
def test_signed_but_unsupported_or_malformed_mime_is_not_extracted(signing_keys, fmt, transform):
    private_key, record = signing_keys
    raw = _raw()
    modified = transform(raw)
    assert modified != raw
    signed = _signed(modified, private_key, headers=SIGNED_HEADERS + (b"content-disposition",))
    _assert_rejected(signed, fmt, "unsupported_format", _dns(record, []))


@pytest.mark.parametrize("before,after", [
    ("2026-03-04", "2026-02-30"), ("2026-03-04", "20260304"),
    ("12.34", "0.00"), ("12.34", "-12.34"), ("12.34", "12.345"), ("12.34", "1,2.34"),
    ("4321", "４３２１"), ("CAD", "$"), ("Fictional Books", " "), ("Fictional Books", "A\u202eB"),
    ("Authorized: 2026-03-04", "Authorized: 2026-03-04\nDeclined"),
])
def test_independent_field_validation_and_fullmatch_reject_ambiguous_alerts(signing_keys, fmt, before, after):
    private_key, record = signing_keys
    raw = _signed(_raw(BODY.replace(before, after)), private_key)
    _assert_rejected(raw, fmt, "unsupported_format", _dns(record, []))


def test_signed_subject_must_be_exact(signing_keys, fmt):
    private_key, record = signing_keys
    raw = _signed(_raw(subject="Synthetic declined alert"), private_key)
    _assert_rejected(raw, fmt, "unsupported_format", _dns(record, []))


def test_named_fields_cannot_capture_multiple_values(signing_keys, fmt):
    private_key, record = signing_keys
    repeat = PATTERN.replace(r"(?P<card_last4>[^\n]+)", r"(?P<card_last4>[0-9]{4}) (?P<card_last4>[0-9]{4})")
    fmt = AlertFormat(**(fmt.model_dump() | {"body_pattern": repeat}))
    raw = _signed(_raw(BODY.replace("4321", "4321 9876")), private_key)
    _assert_rejected(raw, fmt, "unsupported_format", _dns(record, []))


def test_pattern_backtracking_is_bounded(signing_keys, fmt):
    private_key, record = signing_keys
    pathological = PATTERN.replace(r"(?P<merchant>[^\n]+)", r"(?P<merchant>(?:a|aa)+b)")
    fmt = AlertFormat(**(fmt.model_dump() | {"body_pattern": pathological}))
    raw = _signed(_raw(BODY.replace("Fictional Books", "a" * 120)), private_key)
    _assert_rejected(raw, fmt, "unsupported_format", _dns(record, []))


def test_message_byte_limit_precedes_parsing_and_egress(fmt):
    raw = b"x" * (MAX_MESSAGE_BYTES + 1)
    _assert_rejected(raw, fmt, "message_too_large")


def test_strict_frozen_format_and_frozen_alert(signing_keys, fmt):
    private_key, record = signing_keys
    with pytest.raises(ValidationError, match="string_type"):
        AlertFormat(**(fmt.model_dump() | {"subject": b"not a string"}))
    with pytest.raises(ValidationError, match="five named alert fields"):
        AlertFormat(**(fmt.model_dump() | {"body_pattern": r"(?P<other>.+)"}))
    with pytest.raises(ValidationError, match="frozen_instance"):
        fmt.subject = "replacement"
    result = parse_message(_signed(_raw(), private_key), fmt, dns_lookup=_dns(record, []))
    with pytest.raises(FrozenInstanceError):
        setattr(result, "currency", "USD")
