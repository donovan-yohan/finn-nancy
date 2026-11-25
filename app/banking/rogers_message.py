"""Authenticate an intact issuer message before extracting purchase-alert evidence."""
from __future__ import annotations

import base64
import hashlib
import logging
import quopri
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.errors import HeaderParseError
from email.headerregistry import AddressHeader, BaseHeader, ContentTypeHeader
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import default
from typing import Literal, Protocol, cast

import dkim
import dns.exception
import dns.message
import dns.query
import dns.rcode
import dns.resolver
import regex
from dkim.canonicalization import CanonicalizationPolicy, InvalidCanonicalizationPolicyError
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

MAX_MESSAGE_BYTES = 256 * 1024
_MAX_HEADER_BYTES = 32 * 1024
_MAX_BODY_BYTES = 32 * 1024
_MAX_HEADERS = 128
_MAX_CRITICAL_HEADER_BYTES = 2048
_MAX_SIGNATURES = 5
_MAX_SIGNATURE_BYTES = 4096
_MAX_KEY_BYTES = 2048
_DNS_TIMEOUT = 2.0
_MATCH_TIMEOUT = 0.05

_REQUIRED_HEADERS = {b"from", b"to", b"subject", b"message-id"}
_MIME_HEADERS = {b"mime-version", b"content-type", b"content-transfer-encoding", b"content-disposition"}
_FIELDS = {"card_last4", "amount", "currency", "merchant", "authorized_on"}
_DOT_ATOM = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_DOMAIN = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_SELECTOR = re.compile(_LABEL.encode("ascii"))
_MESSAGE_ID = re.compile(rf"<{_DOT_ATOM}@{_DOT_ATOM}>")
_HEADER_START = re.compile(rb"[\x21-\x39\x3b-\x7e]+:")
_AMOUNT = re.compile(r"(0|[1-9][0-9]{0,11}|[1-9][0-9]{0,2}(?:,[0-9]{3}){1,3})\.([0-9]{2})")
# dkimpy diagnostics can contain signature fields and even rejected message data.
_QUIET_DKIM = logging.Logger(__name__, level=logging.CRITICAL + 1)

RejectCode = Literal["untrusted_sender", "untrusted_recipient", "unauthenticated", "unsupported_format", "message_too_large"]


class MessageRejected(ValueError):
    def __init__(self, code: RejectCode):
        self.code = code
        super().__init__(code)


class MessageRetry(RuntimeError):
    """A transient DNS failure prevented authentication; the message can be retried."""

    code = "dns_unavailable"

    def __init__(self):
        super().__init__(self.code)


class _LookupRetry(MessageRetry):
    """A selector lookup failed; unlike policy errors, other signatures may still verify."""


class DNSLookup(Protocol):
    def __call__(self, name: bytes, *, timeout: float) -> bytes | None: ...


class AlertFormat(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)

    sender: str = Field(min_length=3, max_length=254)
    recipient: str = Field(min_length=3, max_length=254)
    signing_domain: str = Field(min_length=3, max_length=253)
    subject: str = Field(min_length=1, max_length=512)
    body_pattern: str = Field(min_length=1, max_length=2048)
    _pattern: regex.Pattern = PrivateAttr()

    @field_validator("sender", "recipient")
    @classmethod
    def _valid_address(cls, address: str) -> str:
        local, separator, domain = address.rpartition("@")
        if not separator or len(local) > 64 or not re.fullmatch(_DOT_ATOM, local) or not _DOMAIN.fullmatch(domain):
            raise ValueError("expected a single ASCII addr-spec")
        return address

    @field_validator("signing_domain")
    @classmethod
    def _valid_domain(cls, domain: str) -> str:
        if not _DOMAIN.fullmatch(domain):
            raise ValueError("expected a fully qualified ASCII signing domain")
        return domain

    @field_validator("subject")
    @classmethod
    def _valid_subject(cls, subject: str) -> str:
        if subject != subject.strip() or not subject.isprintable():
            raise ValueError("expected an exact printable subject")
        return subject

    @model_validator(mode="after")
    def _compile(self):
        try:
            pattern = regex.compile(self.body_pattern, flags=regex.VERSION1)
        except (regex.error, OverflowError, RecursionError):
            raise ValueError("invalid alert body pattern") from None
        if pattern.groupindex.keys() != _FIELDS or pattern.groups != len(_FIELDS):
            raise ValueError("expected exactly the five named alert fields; use non-capturing helper groups")
        self._pattern = pattern
        return self


@dataclass(frozen=True, slots=True)
class Alert:
    message_key: str
    authorized_on: str
    card_last4: str
    amount_cents: int
    currency: str
    merchant: str


def _read_wire(raw: bytes) -> dkim.DKIM:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageRejected("message_too_large")
    wire = raw.replace(b"\r\n", b"\n")
    head, separator, body = wire.partition(b"\n\n")
    if not separator or not head or len(head) > _MAX_HEADER_BYTES or b"\r" in wire:
        raise MessageRejected("unsupported_format")
    lines = head.split(b"\n")
    if lines[0].startswith((b" ", b"\t")) or any(
        len(line) > 998
        or re.search(rb"[\x00-\x08\x0b-\x1f\x7f]", line)
        or not (line.startswith((b" ", b"\t")) or _HEADER_START.match(line))
        for line in lines
    ):
        raise MessageRejected("unsupported_format")
    if any(len(line) > 998 for line in body.split(b"\n")):
        raise MessageRejected("unsupported_format")
    message = dkim.DKIM(wire, timeout=_DNS_TIMEOUT, logger=_QUIET_DKIM)
    if len(message.headers) > _MAX_HEADERS:
        raise MessageRejected("unsupported_format")
    return message


def _critical(name: bytes) -> bool:
    return name in _REQUIRED_HEADERS or name == b"mime-version" or name.startswith(b"content-")


def _header_view(headers: list[tuple[bytes, bytes]]) -> Message:
    # Feed the email parser only the specific headers consumed here. Trace and
    # relay assertions, including Authentication-Results, are never authority.
    return BytesHeaderParser(policy=default).parsebytes(
        b"".join(name + b":" + value for name, value in headers if _critical(name.lower())) + b"\r\n"
    )


def _check_address(headers: Message, header: str, expected: str, code: RejectCode) -> None:
    try:
        field = cast(AddressHeader | None, headers.get(header))
    except (HeaderParseError, ValueError, IndexError, RecursionError):
        raise MessageRejected(code) from None
    if (field is None or cast(BaseHeader, field).defects or len(field.groups) != 1
            or field.groups[0].display_name is not None or len(field.addresses) != 1
            or field.addresses[0].addr_spec != expected):
        raise MessageRejected(code)


def _system_dns(name: bytes, *, timeout: float, check_allowed: Callable[[], None]) -> bytes | None:
    """Bounded recursive TXT lookup. An actual UDP/TCP send gets its own policy check."""
    try:
        addresses = dns.resolver.Resolver().nameservers
        if not addresses:
            raise _LookupRetry()
        request = dns.message.make_query(name.decode("ascii"), "TXT")
    except (dns.exception.DNSException, OSError, ValueError):
        raise _LookupRetry() from None

    address = addresses[0]  # The system-configured recursive nameserver, never a sender-supplied host.
    deadline = time.monotonic() + timeout
    check_allowed()
    try:
        response = dns.query.udp(request, address, timeout=timeout, raise_on_truncation=True)
    except dns.message.Truncated:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _LookupRetry() from None
        check_allowed()
        try:
            response = dns.query.tcp(request, address, timeout=remaining)
        except (dns.exception.DNSException, OSError):
            raise _LookupRetry() from None
    except (dns.exception.DNSException, OSError):
        raise _LookupRetry() from None

    if response.rcode() == dns.rcode.NXDOMAIN:
        return None
    if response.rcode() != dns.rcode.NOERROR:
        raise _LookupRetry()
    try:
        answer = response.resolve_chaining().answer
    except dns.exception.DNSException:
        raise _LookupRetry() from None
    if answer is None or len(answer) != 1:
        return None
    return b"".join(next(iter(answer)).strings)


def _lookup_key(name: bytes, dns_lookup: DNSLookup | None, check_allowed: Callable[[], None]) -> bytes | None:
    if dns_lookup is None:
        return _system_dns(name, timeout=_DNS_TIMEOUT, check_allowed=check_allowed)
    check_allowed()  # Outside exception handling, including for TimeoutError-shaped policy withdrawal.
    try:
        return dns_lookup(name, timeout=_DNS_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except (MessageRetry, dns.exception.DNSException, OSError):
        raise _LookupRetry() from None


def _eligible_key(record: bytes | None) -> bool:
    if not record or len(record) > _MAX_KEY_BYTES:
        return False
    try:
        tags = dkim.parse_tag_value(record)
    except dkim.InvalidTagValueList:
        return False
    return (
        bool(tags.get(b"p"))
        and tags.get(b"k", b"rsa") == b"rsa"
        and b"sha256" in re.split(rb"\s*:\s*", tags.get(b"h", b"sha256"))
        and tags.get(b"s", b"*") in {b"*", b"email"}
    )


def _authenticated_view(
    message: dkim.DKIM,
    critical_names: set[bytes],
    domain: bytes,
    dns_lookup: DNSLookup | None,
    check_allowed: Callable[[], None],
) -> tuple[list[tuple[bytes, bytes]], bytes]:
    count = sum(name.lower() == b"dkim-signature" for name, _ in message.headers)
    if not 1 <= count <= _MAX_SIGNATURES:
        raise MessageRejected("unauthenticated")

    keys: dict[bytes, bytes | None] = {}
    deferred = set()
    for index, value in enumerate(value for name, value in message.headers if name.lower() == b"dkim-signature"):
        if len(value) > _MAX_SIGNATURE_BYTES:
            continue
        try:
            tags, signed, _ = message.verify_headerprep(index)
            if (tags[b"d"] != domain or tags[b"a"] != b"rsa-sha256" or b"l" in tags
                    or not _SELECTOR.fullmatch(tags[b"s"]) or not critical_names.issubset(signed)):
                continue
            # Restrict the optional signing identity to this same domain, not a subdomain.
            if b"i" in tags and tags[b"i"].rsplit(b"@", 1)[-1] != domain:
                continue
            canon = CanonicalizationPolicy.from_c_value(tags.get(b"c"))
            body = canon.canonicalize_body(message.body)
            claimed_hash = base64.b64decode(re.sub(rb"\s+", b"", tags[b"bh"]), validate=True)
            if claimed_hash != hashlib.sha256(body).digest():
                continue
        except (dkim.DKIMException, InvalidCanonicalizationPolicyError, ValueError, IndexError):
            continue

        name = tags[b"s"] + b"._domainkey." + domain + b"."
        if len(name) > 254 or name in deferred:
            continue
        if name not in keys:
            try:
                keys[name] = _lookup_key(name, dns_lookup, check_allowed)
            except _LookupRetry:
                deferred.add(name)
                continue
        record = keys[name]
        if not _eligible_key(record):
            continue
        # Only this candidate's DNS name can use the prefetched TXT record. No
        # transport or policy exception crosses the verifier's exception handlers.
        def public_key(query, *, timeout):
            return record if query == name else None

        try:
            if message.verify(index, dnsfunc=public_key):
                return canon.canonicalize_headers(message.headers), body
        except (dkim.DKIMException, ValueError, IndexError, TypeError):
            continue
    if deferred:
        raise MessageRetry()
    raise MessageRejected("unauthenticated")


def _plain_alternative(body: bytes, boundary: str) -> tuple[Message, bytes]:
    """Accept exactly two signed leaf parts; never decode or render the HTML sibling."""
    if not re.fullmatch(r"[A-Za-z0-9'()+_,./:=? -]{1,70}", boundary) or boundary.endswith(' '):
        raise MessageRejected('unsupported_format')
    marker = re.escape(b'--' + boundary.encode('ascii'))
    delimiters = list(re.finditer(rb'^' + marker + rb'(--)?[ \t]*\r\n', body, flags=re.M))
    if [item[1] for item in delimiters] != [None, None, b'--']:
        raise MessageRejected('unsupported_format')
    parts = []
    for start, end, mime_type in zip(delimiters, delimiters[1:], ('text/plain', 'text/html')):
        part = body[start.end():end.start()].removesuffix(b'\r\n')
        head, sep, payload = part.partition(b'\r\n\r\n')
        if not sep or not head or len(head) > _MAX_CRITICAL_HEADER_BYTES:
            raise MessageRejected('unsupported_format')
        headers = BytesHeaderParser(policy=default).parsebytes(head + b'\r\n\r\n')
        names = [name.lower().encode('ascii') for name in headers.keys()]
        if (len(names) != len(set(names)) or set(names) - (_MIME_HEADERS - {b'content-disposition'})
                or headers.defects or any(cast(BaseHeader, item).defects for item in headers.values())
                or 'Content-Type' not in headers or headers.get_content_type() != mime_type):
            raise MessageRejected('unsupported_format')
        parts.append((headers, payload))
    return parts[0]


def _text(headers: Message, body: bytes) -> str:
    if headers.get("MIME-Version", "1.0") != "1.0" or "Content-Disposition" in headers:
        raise MessageRejected("unsupported_format")
    mime = cast(ContentTypeHeader | None, headers.get("Content-Type"))
    if mime is not None and mime.content_type == 'multipart/alternative':
        if (mime.params.keys() != {'boundary'}
                or headers.get('Content-Transfer-Encoding', '7bit').lower() not in {'7bit', '8bit'}):
            raise MessageRejected('unsupported_format')
        try:
            part, payload = _plain_alternative(body, mime.params['boundary'])
        except (HeaderParseError, ValueError, IndexError, RecursionError):
            raise MessageRejected('unsupported_format') from None
        return _text(part, payload)
    if mime is not None:
        if mime.content_type != "text/plain" or mime.params.keys() - {"charset"}:
            raise MessageRejected("unsupported_format")
        charset = mime.params.get("charset", "us-ascii").lower()
    else:
        charset = "us-ascii"
    if charset not in {"us-ascii", "utf-8"}:
        raise MessageRejected("unsupported_format")
    encoding = str(headers.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    try:
        if encoding == "base64":
            compact = body.replace(b"\r\n", b"")
            body = base64.b64decode(compact, validate=True)
            if base64.b64encode(body) != compact:
                raise ValueError
        elif encoding == "quoted-printable":
            if (re.search(rb"[^\x09\x0d\x0a\x20-\x7e]|=(?![0-9A-F]{2}|\r\n)|[ \t]\r\n", body)
                    or body.endswith(b"=\r\n")):
                raise ValueError
            body = quopri.decodestring(body)
        elif (encoding not in {"7bit", "8bit"}) or (encoding == "7bit" and not body.isascii()):
            raise ValueError
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError
        text = body.decode(charset).replace("\r\n", "\n").rstrip("\n")
        if not text or any(
            char not in "\n\t" and unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in text
        ):
            raise ValueError
    except ValueError:
        raise MessageRejected("unsupported_format") from None
    return text


def _purchase(fmt: AlertFormat, headers: Message, body: bytes) -> Alert:
    try:
        bad_headers = headers.defects or any(cast(BaseHeader, header).defects for header in headers.values())
    except (HeaderParseError, ValueError, IndexError, RecursionError):
        raise MessageRejected("unsupported_format") from None
    if bad_headers:
        raise MessageRejected("unsupported_format")
    if headers.get("Subject") != fmt.subject:
        raise MessageRejected("unsupported_format")
    message_id = str(headers["Message-ID"]).strip(" \t")
    if len(message_id) > 254 or not _MESSAGE_ID.fullmatch(message_id):
        raise MessageRejected("unsupported_format")
    try:
        match = fmt._pattern.fullmatch(_text(headers, body), timeout=_MATCH_TIMEOUT)
    except TimeoutError:
        raise MessageRejected("unsupported_format") from None
    if not match:
        raise MessageRejected("unsupported_format")
    captures = match.capturesdict()
    if any(len(values) != 1 for values in captures.values()):
        raise MessageRejected("unsupported_format")
    fields = {name: values[0] for name, values in captures.items()}
    authorized_on = fields["authorized_on"]
    card_last4 = fields["card_last4"]
    currency = fields["currency"]
    merchant = fields["merchant"]
    amount = _AMOUNT.fullmatch(fields["amount"])
    if (not amount or not re.fullmatch(r"[0-9]{4}", card_last4)
            or not re.fullmatch(r"[A-Z]{3}", currency) or not 1 <= len(merchant) <= 160
            or merchant != merchant.strip() or not merchant.isprintable()
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", authorized_on)):
        raise MessageRejected("unsupported_format")
    try:
        date.fromisoformat(authorized_on)
    except ValueError:
        raise MessageRejected("unsupported_format") from None
    whole, fraction = amount.groups()
    cents = int(whole.replace(",", "")) * 100 + int(fraction)
    if cents <= 0:
        raise MessageRejected("unsupported_format")
    key = hashlib.sha256(f"rogers-alert\0{fmt.sender}\0{fmt.recipient}\0{message_id}".encode("ascii")).hexdigest()
    return Alert(key, authorized_on, card_last4, -cents, currency, merchant)


def parse_message(
    raw: bytes,
    fmt: AlertFormat,
    *,
    dns_lookup: DNSLookup | None = None,
    check_allowed: Callable[[], None] = lambda: None,
) -> Alert:
    """Accept intact plaintext (including a plain MIME alternative), never forwarded attachments."""
    message = _read_wire(raw)
    critical = []
    for name, value in message.headers:
        name = name.lower()
        if _critical(name):
            if len(value) > _MAX_CRITICAL_HEADER_BYTES:
                raise MessageRejected("unsupported_format")
            critical.append(name)
    names = set(critical)
    if critical.count(b"from") != 1:
        raise MessageRejected("untrusted_sender")
    if critical.count(b"to") != 1:
        raise MessageRejected("untrusted_recipient")
    if (len(critical) != len(names) or not _REQUIRED_HEADERS.issubset(names)
            or names - _REQUIRED_HEADERS - _MIME_HEADERS):
        raise MessageRejected("unsupported_format")
    headers = _header_view(message.headers)
    _check_address(headers, 'From', fmt.sender, 'untrusted_sender')
    _check_address(headers, 'To', fmt.recipient, 'untrusted_recipient')
    signed_headers, signed_body = _authenticated_view(
        message, names, fmt.signing_domain.encode("ascii"), dns_lookup, check_allowed
    )
    # Parse the signed canonical representation so DKIM-equivalent whitespace
    # changes cannot alter the subject, captured fields or deduplication key.
    headers = _header_view(signed_headers)
    _check_address(headers, 'From', fmt.sender, 'untrusted_sender')
    _check_address(headers, 'To', fmt.recipient, 'untrusted_recipient')
    return _purchase(fmt, headers, signed_body)
