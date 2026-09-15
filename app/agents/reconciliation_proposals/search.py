"""Privacy-bounded, evidence-only merchant search for FN-150.

Search is deliberately a separate optional adapter.  It has no database handle
and its return types cannot name a transaction, category, flow, action, or
ledger mutation.  Callers may display the untrusted evidence and, through the
existing human approval path, persist a separately validated structured fact.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import html
import json
import re
import secrets
from threading import RLock
import time
from typing import Literal, Protocol
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import httpx

SEARCH_SANITIZER_VERSION = "fn150-merchant-search-sanitizer.v1"
SEARCH_ADAPTER_VERSION = "fn150-searxng-search.v1"
BRAVE_ADAPTER_VERSION = "brave-web-search.v1"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MAX_CACHE_TTL_SECONDS = 600
MAX_QUERY_CHARS = 96
MAX_QUERY_TOKENS = 10
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CITATION_URL_CHARS = 2048
MAX_TITLE_CHARS = 180
MAX_SNIPPET_CHARS = 500

SearchStatus = Literal[
    "none",
    "disabled",
    "consent_blocked",
    "not_needed",
    "queried",
    "failed",
]
CacheState = Literal["not_applicable", "miss", "hit"]
SearchResolutionStatus = Literal[
    "resolved",
    "missing",
    "expired",
    "digest_mismatch",
    "citation_mismatch",
    "consent_mismatch",
    "invalid_reference",
]
SearchEvidenceListStatus = Literal[
    "available",
    "missing",
    "expired",
    "consent_mismatch",
    "invalid_reference",
]
SearchErrorCode = Literal[
    "",
    "network_error",
    "redirect_rejected",
    "http_error",
    "invalid_content_type",
    "response_too_large",
    "invalid_payload",
    "engines_unavailable",
]

_SPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]{0,500}>")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_AMOUNT_RE = re.compile(
    r"(?:[$€£¥₹₩₽₺₫]|"
    r"\b(?:usd|cad|eur|gbp|aud|nzd|jpy|cny|inr|chf)\b|"
    r"\b\d{1,9}(?:[.,]\d{2})\b)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?:\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|"
    r"\b\d{1,2}[-/.]\d{1,2}(?:[-/.]\d{2,4})?\b|"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b)",
    re.IGNORECASE,
)
_LONG_DIGITS_RE = re.compile(r"\d{4,}")
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_PHONE_RE = re.compile(r"(?:\+?\d[\s().-]*){7,}")
_URL_RE = re.compile(r"\b(?:https?://|www\.)", re.IGNORECASE)
_PRIVATE_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"account|acct|card|credit|debit|visa|mastercard|amex|iban|routing|"
    r"transit|ending|last\s*four|statement|balance|posted|amount|"
    r"receipt|invoice|subtotal|tax|tip|total|cashier|terminal|"
    r"transaction|authorization|approval|reference|"
    r"household|family|spouse|wife|husband|partner|child|"
    r"customer|member|address|phone|email|my|our"
    r")\b",
    re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior)|"
    r"system\s+prompt|developer\s+message|tool\s+call|"
    r"execute\s+(?:this|command)|follow\s+these\s+instructions|"
    r"<\s*(?:script|system|assistant|tool)\b)",
    re.IGNORECASE,
)
_OPAQUE_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,96}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONSENT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


class MerchantSearchError(ValueError):
    """Base error whose message never echoes the rejected private value."""


class MerchantSearchSanitizationError(MerchantSearchError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"merchant search query rejected: {code}")


class MerchantSearchConfigurationError(MerchantSearchError):
    """The optional provider is configured outside the fail-closed contract."""


@dataclass(frozen=True)
class SanitizedMerchantQuery:
    value: str
    sanitizer_version: str = SEARCH_SANITIZER_VERSION

    def __post_init__(self) -> None:
        canonical = _canonicalize_candidate(self.value)
        if canonical != self.value:
            raise MerchantSearchSanitizationError("query_not_canonical")
        _reject_private_query(canonical, private_terms=())
        if self.sanitizer_version != SEARCH_SANITIZER_VERSION:
            raise MerchantSearchSanitizationError("sanitizer_version_unapproved")


@dataclass(frozen=True)
class MerchantSearchEvidence:
    """Untrusted cited text with intentionally zero accounting authority."""

    provider: Literal["searxng", "brave"]
    citation_url: str
    title: str
    snippet: str
    content_digest: str
    retrieved_at: str
    untrusted: Literal[True] = True


@dataclass(frozen=True)
class MerchantSearchResult:
    status: SearchStatus
    provider: Literal["none", "searxng", "brave"]
    sanitized_query: str = ""
    cache_state: CacheState = "not_applicable"
    evidence: tuple[MerchantSearchEvidence, ...] = ()
    error_code: SearchErrorCode = ""
    query_digest: str = ""
    cache_key: str = ""
    retrieved_at: str = ""
    expires_at: str = ""
    consent_version: str = ""

    @property
    def citations(self) -> tuple[str, ...]:
        return tuple(item.citation_url for item in self.evidence)


@dataclass(frozen=True)
class MerchantSearchResolution:
    """Exact outcome from resolving one process-local evidence reference."""

    status: SearchResolutionStatus
    evidence: MerchantSearchEvidence | None = None
    expires_at: str = ""


@dataclass(frozen=True)
class MerchantSearchEvidenceList:
    """Live, read-only citations for one opaque process-local cache entry."""

    status: SearchEvidenceListStatus
    sanitized_query: str = ""
    evidence: tuple[MerchantSearchEvidence, ...] = ()
    expires_at: str = ""


class MerchantSearchProvider(Protocol):
    def search(
        self,
        query: SanitizedMerchantQuery,
        *,
        private_terms: Iterable[str] = (),
    ) -> MerchantSearchResult: ...

    def resolve_evidence(
        self,
        *,
        cache_key: str,
        evidence_digest: str,
        citation_url: str,
        consent_version: str,
    ) -> MerchantSearchResolution: ...

    def list_evidence(
        self,
        *,
        cache_key: str,
        consent_version: str,
    ) -> MerchantSearchEvidenceList: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class MerchantSearchConfig:
    provider: Literal["none", "searxng", "brave"] = "none"
    enabled: bool = False
    strict_local_mode: bool = True
    consent_granted: bool = False
    consent_version: str = ""
    endpoint: str = ""
    allowed_endpoints: tuple[str, ...] = ()
    timeout_seconds: float = 5.0
    cache_ttl_seconds: int = MAX_CACHE_TTL_SECONDS
    cache_capacity: int = 128
    max_results: int = 5
    private_terms: tuple[str, ...] = ()
    api_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_endpoints",
            tuple(str(item) for item in self.allowed_endpoints),
        )
        object.__setattr__(
            self,
            "private_terms",
            tuple(str(item) for item in self.private_terms),
        )
        if not (0.05 <= float(self.timeout_seconds) <= 15.0):
            raise MerchantSearchConfigurationError(
                "merchant search timeout must be between 0.05 and 15 seconds"
            )
        if not (1 <= int(self.cache_ttl_seconds) <= MAX_CACHE_TTL_SECONDS):
            raise MerchantSearchConfigurationError(
                "merchant search cache TTL must be between 1 and 600 seconds"
            )
        if not (1 <= int(self.cache_capacity) <= 512):
            raise MerchantSearchConfigurationError(
                "merchant search cache capacity must be between 1 and 512"
            )
        if not (1 <= int(self.max_results) <= 10):
            raise MerchantSearchConfigurationError(
                "merchant search result limit must be between 1 and 10"
            )
        if self.consent_version and not _CONSENT_VERSION_RE.fullmatch(
            self.consent_version
        ):
            raise MerchantSearchConfigurationError(
                "merchant search consent version is invalid"
            )


@dataclass(frozen=True)
class _CacheEntry:
    stored_at: float
    query: str
    query_digest: str
    cache_key: str
    retrieved_at: str
    expires_at: str
    consent_version: str
    evidence: tuple[MerchantSearchEvidence, ...]


def _canonicalize_candidate(raw: str) -> str:
    candidate = unicodedata.normalize("NFKC", str(raw or ""))
    return _SPACE_RE.sub(" ", candidate).strip()


def _contains_private_term(candidate: str, private_terms: Iterable[str]) -> bool:
    folded = candidate.casefold()
    for raw_term in private_terms:
        term = _canonicalize_candidate(raw_term).casefold()
        if len(term) >= 2 and term in folded:
            return True
    return False


def _reject_private_query(
    candidate: str,
    *,
    private_terms: Iterable[str],
) -> None:
    if not candidate:
        raise MerchantSearchSanitizationError("empty_query")
    if any(unicodedata.category(char).startswith("C") for char in candidate):
        raise MerchantSearchSanitizationError("control_character")
    if len(candidate) > MAX_QUERY_CHARS:
        raise MerchantSearchSanitizationError("query_too_long")
    if len(candidate.split()) > MAX_QUERY_TOKENS:
        raise MerchantSearchSanitizationError("query_has_too_many_tokens")
    if _CONTROL_RE.search(candidate):
        raise MerchantSearchSanitizationError("control_character")
    if _AMOUNT_RE.search(candidate):
        raise MerchantSearchSanitizationError("amount")
    if _DATE_RE.search(candidate):
        raise MerchantSearchSanitizationError("date")
    if _LONG_DIGITS_RE.search(candidate):
        raise MerchantSearchSanitizationError("identifier")
    if _EMAIL_RE.search(candidate) or _PHONE_RE.search(candidate):
        raise MerchantSearchSanitizationError("person_contact")
    if _URL_RE.search(candidate):
        raise MerchantSearchSanitizationError("url")
    if _PRIVATE_CONTEXT_RE.search(candidate):
        raise MerchantSearchSanitizationError("private_context")
    # Python's stdlib ``re`` has no Unicode property escapes.  Keep this
    # explicit title check separate and conservative.
    if re.search(
        r"\b(?:mr|mrs|ms|miss|dr|prof)\.?\s+[^\W\d_]",
        candidate,
        re.IGNORECASE,
    ):
        raise MerchantSearchSanitizationError("person_name")
    if _contains_private_term(candidate, private_terms):
        raise MerchantSearchSanitizationError("private_term")
    if _INJECTION_RE.search(candidate):
        raise MerchantSearchSanitizationError("instruction_content")


def sanitize_merchant_query(
    raw: str,
    *,
    private_terms: Iterable[str] = (),
) -> SanitizedMerchantQuery:
    """Return a generic merchant-only query or reject the whole input.

    ``private_terms`` lets the local caller supply known person/household
    values without teaching this module, logging them, or attempting to infer
    whether an ordinary proper name is a person or a business.
    """

    source = str(raw or "")
    if any(unicodedata.category(char).startswith("C") for char in source):
        raise MerchantSearchSanitizationError("control_character")
    canonical = _canonicalize_candidate(source)
    _reject_private_query(canonical, private_terms=private_terms)
    return SanitizedMerchantQuery(canonical)


def _validate_endpoint(endpoint: str) -> str:
    if not endpoint or endpoint != endpoint.strip():
        raise MerchantSearchConfigurationError("search endpoint is missing")
    if len(endpoint) > MAX_CITATION_URL_CHARS or any(
        unicodedata.category(char).startswith("C") for char in endpoint
    ):
        raise MerchantSearchConfigurationError("search endpoint is invalid")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise MerchantSearchConfigurationError(
            "search endpoint must use http or https"
        )
    if not parsed.hostname or parsed.username or parsed.password:
        raise MerchantSearchConfigurationError(
            "search endpoint host is invalid"
        )
    if parsed.query or parsed.fragment:
        raise MerchantSearchConfigurationError(
            "search endpoint cannot contain query or fragment data"
        )
    if parsed.path.rstrip("/") != "/search":
        raise MerchantSearchConfigurationError(
            "SearXNG endpoint must be the exact /search path"
        )
    return endpoint


def _clean_text(value: object, *, maximum: int) -> str:
    source = html.unescape(str(value or ""))
    source = _TAG_RE.sub(" ", source)
    source = _CONTROL_RE.sub(" ", source)
    source = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in source
    )
    return _SPACE_RE.sub(" ", source).strip()[:maximum]


def _citation_url(value: object) -> str:
    source = str(value or "").strip()
    if len(source) > MAX_CITATION_URL_CHARS or any(
        unicodedata.category(char).startswith("C") for char in source
    ):
        return ""
    parsed = urlsplit(source)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.hostname or parsed.username or parsed.password:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def _evidence_digest(
    *,
    provider: str,
    citation_url: str,
    title: str,
    snippet: str,
) -> str:
    canonical = json.dumps(
        {
            "provider": provider,
            "citation_url": citation_url,
            "title": title,
            "snippet": snippet,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _all_engines_refused(payload: object) -> bool:
    """True when SearXNG answered but every backend it tried declined."""
    if not isinstance(payload, dict):
        return False
    unresponsive = payload.get("unresponsive_engines")
    return isinstance(unresponsive, list) and bool(unresponsive)


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _valid_reference(
    *,
    cache_key: str,
    evidence_digest: str,
    citation_url: str,
    consent_version: str,
) -> bool:
    if not _OPAQUE_CACHE_KEY_RE.fullmatch(cache_key):
        return False
    if not _DIGEST_RE.fullmatch(evidence_digest):
        return False
    if not _CONSENT_VERSION_RE.fullmatch(consent_version):
        return False
    return bool(citation_url and _citation_url(citation_url) == citation_url)


class NoMerchantSearch:
    def __init__(self, status: Literal["none", "disabled", "consent_blocked"]):
        self._status = status

    def search(
        self,
        query: SanitizedMerchantQuery,
        *,
        private_terms: Iterable[str] = (),
    ) -> MerchantSearchResult:
        del query, private_terms
        return MerchantSearchResult(status=self._status, provider="none")

    def resolve_evidence(
        self,
        *,
        cache_key: str,
        evidence_digest: str,
        citation_url: str,
        consent_version: str,
    ) -> MerchantSearchResolution:
        del cache_key, evidence_digest, citation_url, consent_version
        return MerchantSearchResolution(status="missing")

    def list_evidence(
        self,
        *,
        cache_key: str,
        consent_version: str,
    ) -> MerchantSearchEvidenceList:
        del cache_key, consent_version
        return MerchantSearchEvidenceList(status="missing")

    def close(self) -> None:
        return None


class SearxngMerchantSearch:
    """SearXNG metasearch. Shares cache and evidence handling with Brave."""

    _provider_name = "searxng"

    """Exact-allowlisted SearXNG client with a bounded process-local TTL-LRU."""

    def __init__(
        self,
        config: MerchantSearchConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        cache_key_factory: Callable[[], str] | None = None,
    ):
        if config.provider != self._provider_name:
            raise MerchantSearchConfigurationError(
                f"adapter requires provider={self._provider_name}"
            )
        if (
            config.strict_local_mode
            or not config.enabled
            or not config.consent_granted
            or not config.consent_version
        ):
            raise MerchantSearchConfigurationError(
                "SearXNG adapter requires enabled non-local consent"
            )
        endpoint = self._resolve_endpoint(config)
        self._config = config
        self._endpoint = endpoint
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._cache_key_factory = cache_key_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_keys: dict[str, str] = {}
        self._lock = RLock()
        self._client = httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": f"finn-nancy/{SEARCH_ADAPTER_VERSION}",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _resolve_endpoint(self, config: MerchantSearchConfig) -> str:
        """SearXNG is operator-hosted, so its endpoint must be allowlisted."""
        endpoint = _validate_endpoint(config.endpoint)
        allowed = tuple(
            _validate_endpoint(item) for item in config.allowed_endpoints
        )
        if endpoint not in allowed:
            raise MerchantSearchConfigurationError(
                "search endpoint is not on the exact allowlist"
            )
        return endpoint

    def _request(self, query: SanitizedMerchantQuery):
        return self._client.stream(
            "POST",
            self._endpoint,
            data={"q": query.value, "format": "json", "categories": "general"},
        )

    def _rows(self, payload: object) -> object:
        return payload["results"]

    def _backends_refused(self, payload: object) -> bool:
        return _all_engines_refused(payload)

    def _status_error(self, status_code: int) -> SearchErrorCode:
        return "http_error"

    def _cache_get(
        self,
        key: str,
    ) -> _CacheEntry | None:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if now - entry.stored_at >= self._config.cache_ttl_seconds:
                self._remove_entry(key, entry)
                return None
            self._cache.move_to_end(key)
            return entry

    def _cache_put(
        self,
        query: str,
        *,
        retrieved_at: str,
        expires_at: str,
        evidence: tuple[MerchantSearchEvidence, ...],
    ) -> _CacheEntry:
        with self._lock:
            existing = self._cache.pop(query, None)
            if existing is not None:
                self._cache_keys.pop(existing.cache_key, None)
            cache_key = ""
            for _ in range(8):
                candidate = self._cache_key_factory()
                if (
                    _OPAQUE_CACHE_KEY_RE.fullmatch(candidate)
                    and candidate not in self._cache_keys
                ):
                    cache_key = candidate
                    break
            if not cache_key:
                raise MerchantSearchConfigurationError(
                    "opaque search cache key generation failed"
                )
            entry = _CacheEntry(
                stored_at=self._clock(),
                query=query,
                query_digest=_query_digest(query),
                cache_key=cache_key,
                retrieved_at=retrieved_at,
                expires_at=expires_at,
                consent_version=self._config.consent_version,
                evidence=evidence,
            )
            self._cache[query] = entry
            self._cache_keys[cache_key] = query
            while len(self._cache) > self._config.cache_capacity:
                evicted_query, evicted = self._cache.popitem(last=False)
                del evicted_query
                self._cache_keys.pop(evicted.cache_key, None)
            return entry

    def _remove_entry(self, query: str, entry: _CacheEntry) -> None:
        self._cache.pop(query, None)
        self._cache_keys.pop(entry.cache_key, None)

    def _result_from_entry(
        self,
        entry: _CacheEntry,
        *,
        cache_state: Literal["miss", "hit"],
    ) -> MerchantSearchResult:
        return MerchantSearchResult(
            status="queried",
            provider=self._provider_name,
            sanitized_query=entry.query,
            cache_state=cache_state,
            evidence=entry.evidence,
            query_digest=entry.query_digest,
            cache_key=entry.cache_key,
            retrieved_at=entry.retrieved_at,
            expires_at=entry.expires_at,
            consent_version=entry.consent_version,
        )

    def resolve_evidence(
        self,
        *,
        cache_key: str,
        evidence_digest: str,
        citation_url: str,
        consent_version: str,
    ) -> MerchantSearchResolution:
        """Resolve one exact, unexpired in-memory search item.

        The opaque cache key is deliberately not derived from the query.  A
        process restart loses this cache and therefore blocks approval until a
        fresh search is run.
        """

        if not _valid_reference(
            cache_key=cache_key,
            evidence_digest=evidence_digest,
            citation_url=citation_url,
            consent_version=consent_version,
        ):
            return MerchantSearchResolution(status="invalid_reference")
        now = self._clock()
        with self._lock:
            query = self._cache_keys.get(cache_key)
            if query is None:
                return MerchantSearchResolution(status="missing")
            entry = self._cache.get(query)
            if entry is None or entry.cache_key != cache_key:
                self._cache_keys.pop(cache_key, None)
                return MerchantSearchResolution(status="missing")
            if now - entry.stored_at >= self._config.cache_ttl_seconds:
                expires_at = entry.expires_at
                self._remove_entry(query, entry)
                return MerchantSearchResolution(
                    status="expired",
                    expires_at=expires_at,
                )
            if not secrets.compare_digest(
                consent_version,
                entry.consent_version,
            ):
                return MerchantSearchResolution(
                    status="consent_mismatch",
                    expires_at=entry.expires_at,
                )
            matched = next(
                (
                    item
                    for item in entry.evidence
                    if secrets.compare_digest(
                        evidence_digest,
                        item.content_digest,
                    )
                ),
                None,
            )
            if matched is None:
                return MerchantSearchResolution(
                    status="digest_mismatch",
                    expires_at=entry.expires_at,
                )
            if not secrets.compare_digest(citation_url, matched.citation_url):
                return MerchantSearchResolution(
                    status="citation_mismatch",
                    expires_at=entry.expires_at,
                )
            self._cache.move_to_end(query)
            return MerchantSearchResolution(
                status="resolved",
                evidence=matched,
                expires_at=entry.expires_at,
            )

    def list_evidence(
        self,
        *,
        cache_key: str,
        consent_version: str,
    ) -> MerchantSearchEvidenceList:
        """List current sanitized citations and their disclosed query."""

        if (
            not _OPAQUE_CACHE_KEY_RE.fullmatch(cache_key)
            or not _CONSENT_VERSION_RE.fullmatch(consent_version)
        ):
            return MerchantSearchEvidenceList(status="invalid_reference")
        now = self._clock()
        with self._lock:
            query = self._cache_keys.get(cache_key)
            if query is None:
                return MerchantSearchEvidenceList(status="missing")
            entry = self._cache.get(query)
            if entry is None or entry.cache_key != cache_key:
                self._cache_keys.pop(cache_key, None)
                return MerchantSearchEvidenceList(status="missing")
            if now - entry.stored_at >= self._config.cache_ttl_seconds:
                expires_at = entry.expires_at
                self._remove_entry(query, entry)
                return MerchantSearchEvidenceList(
                    status="expired",
                    expires_at=expires_at,
                )
            if not secrets.compare_digest(
                consent_version,
                entry.consent_version,
            ):
                return MerchantSearchEvidenceList(
                    status="consent_mismatch",
                    expires_at=entry.expires_at,
                )
            self._cache.move_to_end(query)
            return MerchantSearchEvidenceList(
                status="available",
                sanitized_query=entry.query,
                evidence=entry.evidence,
                expires_at=entry.expires_at,
            )

    def _failed(
        self,
        query: SanitizedMerchantQuery,
        code: SearchErrorCode,
    ) -> MerchantSearchResult:
        return MerchantSearchResult(
            status="failed",
            provider=self._provider_name,
            sanitized_query=query.value,
            cache_state="miss",
            error_code=code,
        )

    def search(
        self,
        query: SanitizedMerchantQuery,
        *,
        private_terms: Iterable[str] = (),
    ) -> MerchantSearchResult:
        _reject_private_query(
            query.value,
            private_terms=(
                *self._config.private_terms,
                *(str(item) for item in private_terms),
            ),
        )
        cached = self._cache_get(query.value)
        if cached is not None:
            return self._result_from_entry(cached, cache_state="hit")

        try:
            with self._request(query) as response:
                if 300 <= response.status_code < 400:
                    return self._failed(query, "redirect_rejected")
                if response.status_code != 200:
                    return self._failed(
                        query, self._status_error(response.status_code)
                    )
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type.lower():
                    return self._failed(query, "invalid_content_type")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        return self._failed(query, "response_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError:
            return self._failed(query, "network_error")

        try:
            payload = json.loads(b"".join(chunks))
            rows = self._rows(payload)
            if not isinstance(rows, list):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._failed(query, "invalid_payload")

        # An empty result set from a metasearch engine is ambiguous: the
        # merchant may not exist, or every upstream may have refused the query.
        # SearXNG reports the latter in `unresponsive_engines`, and collapsing
        # the two would let rate limiting masquerade as "no such merchant" and
        # be recorded as a researched dead end.
        if not rows and self._backends_refused(payload):
            return self._failed(query, "engines_unavailable")

        observed_now = self._now().astimezone(UTC)
        retrieved_at = observed_now.isoformat()
        expires_at = (
            observed_now + timedelta(seconds=self._config.cache_ttl_seconds)
        ).isoformat()
        evidence: list[MerchantSearchEvidence] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            citation = _citation_url(row.get("url"))
            if not citation:
                continue
            title = _clean_text(row.get("title"), maximum=MAX_TITLE_CHARS)
            snippet = _clean_text(
                row.get("content") or row.get("snippet") or row.get("description"),
                maximum=MAX_SNIPPET_CHARS,
            )
            if not title and not snippet:
                continue
            evidence.append(
                MerchantSearchEvidence(
                    provider=self._provider_name,
                    citation_url=citation,
                    title=title,
                    snippet=snippet,
                    content_digest=_evidence_digest(
                        provider=self._provider_name,
                        citation_url=citation,
                        title=title,
                        snippet=snippet,
                    ),
                    retrieved_at=retrieved_at,
                )
            )
            if len(evidence) >= self._config.max_results:
                break

        result = tuple(evidence)
        entry = self._cache_put(
            query.value,
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            evidence=result,
        )
        return self._result_from_entry(entry, cache_state="miss")


class BraveMerchantSearch(SearxngMerchantSearch):
    """Brave Web Search API.

    Kept as a fallback rather than the default. It is an official API with a
    quota, so it does not get CAPTCHA-blocked the way a scraper does, but the
    query reaches Brave alongside an account-identifying key -- where a
    self-hosted SearXNG only exposes the operator's address. Privacy first,
    reliability when privacy has already failed.
    """

    _provider_name = "brave"

    def _resolve_endpoint(self, config: MerchantSearchConfig) -> str:
        if not config.api_key.strip():
            raise MerchantSearchConfigurationError(
                "Brave search requires an API key"
            )
        # The endpoint is the vendor's, not an operator choice, so there is no
        # allowlist to satisfy.
        return BRAVE_ENDPOINT

    def _request(self, query: SanitizedMerchantQuery):
        return self._client.stream(
            "GET",
            self._endpoint,
            params={
                "q": query.value,
                "count": max(1, min(int(self._config.max_results), 20)),
                "safesearch": "off",
                "result_filter": "web",
            },
            headers={"X-Subscription-Token": self._config.api_key},
        )

    def _rows(self, payload: object) -> object:
        if not isinstance(payload, dict):
            raise TypeError
        web = payload.get("web") or {}
        if not isinstance(web, dict):
            raise TypeError
        # An absent results key means no hits, which is a valid empty answer.
        return web.get("results", [])

    def _backends_refused(self, payload: object) -> bool:
        # Brave reports exhaustion through HTTP status, not the payload.
        return False

    def _status_error(self, status_code: int) -> SearchErrorCode:
        # 429 is quota exhaustion: retryable, and explicitly not "no such
        # merchant". Same distinction the SearXNG path draws from
        # unresponsive_engines.
        if status_code in (429, 503):
            return "engines_unavailable"
        return "http_error"


class FallbackMerchantSearch:
    """Try the privacy-preferred provider, fall back when it is unavailable.

    Only an explicitly retryable failure falls through. A merchant the primary
    searched successfully and found nothing for is a real answer, and asking a
    second vendor the same question would leak the query for no new
    information.
    """

    _FALL_THROUGH = frozenset({"engines_unavailable", "network_error", "http_error"})

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    def search(
        self,
        query: SanitizedMerchantQuery,
        *,
        private_terms: Iterable[str] = (),
    ) -> MerchantSearchResult:
        result = self._primary.search(query, private_terms=private_terms)
        if result.status == "queried" and result.evidence:
            return result
        if result.status in {"none", "disabled", "consent_blocked"} or (
            result.error_code in self._FALL_THROUGH
        ):
            return self._fallback.search(query, private_terms=private_terms)
        return result

    def resolve_evidence(self, **kwargs) -> MerchantSearchResolution:
        resolved = self._primary.resolve_evidence(**kwargs)
        if resolved.status == "resolved":
            return resolved
        return self._fallback.resolve_evidence(**kwargs)

    def list_evidence(self, **kwargs) -> MerchantSearchEvidenceList:
        listed = self._primary.list_evidence(**kwargs)
        if listed.status == "available":
            return listed
        return self._fallback.list_evidence(**kwargs)

    def close(self) -> None:
        self._primary.close()
        self._fallback.close()


def build_merchant_search_provider(
    config: MerchantSearchConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    cache_key_factory: Callable[[], str] | None = None,
) -> MerchantSearchProvider:
    """Build the optional provider; all non-explicit states fail closed."""

    if config.provider == "none":
        return NoMerchantSearch("none")
    if config.strict_local_mode or not config.enabled:
        return NoMerchantSearch("disabled")
    if not config.consent_granted or not config.consent_version.strip():
        return NoMerchantSearch("consent_blocked")
    return SearxngMerchantSearch(
        config,
        transport=transport,
        clock=clock,
        now=now,
        cache_key_factory=cache_key_factory,
    )


def merchant_search_config_from_settings(
    settings: object,
    *,
    private_terms: Iterable[str] = (),
) -> MerchantSearchConfig:
    """Translate app settings into the fail-closed search adapter contract."""

    raw_allowed = getattr(settings, "merchant_search_allowed_endpoints", ())
    if isinstance(raw_allowed, str):
        allowed_endpoints = (raw_allowed,) if raw_allowed else ()
    else:
        allowed_endpoints = tuple(str(item) for item in raw_allowed)
    return MerchantSearchConfig(
        provider=str(getattr(settings, "merchant_search_provider", "none")),
        enabled=bool(getattr(settings, "merchant_search_enabled", False)),
        strict_local_mode=bool(
            getattr(settings, "strict_local_mode", True)
        ),
        consent_granted=bool(
            getattr(settings, "merchant_search_consent_granted", False)
        ),
        consent_version=str(
            getattr(settings, "merchant_search_consent_version", "")
        ),
        endpoint=str(getattr(settings, "merchant_search_endpoint", "")),
        allowed_endpoints=allowed_endpoints,
        timeout_seconds=float(
            getattr(settings, "merchant_search_timeout_s", 5.0)
        ),
        cache_ttl_seconds=int(
            getattr(settings, "merchant_search_cache_ttl_s", 600)
        ),
        cache_capacity=int(
            getattr(settings, "merchant_search_cache_capacity", 128)
        ),
        max_results=int(
            getattr(settings, "merchant_search_max_results", 5)
        ),
        private_terms=tuple(str(item) for item in private_terms),
    )


def build_merchant_search_provider_from_settings(
    settings: object,
    *,
    private_terms: Iterable[str] = (),
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    cache_key_factory: Callable[[], str] | None = None,
) -> MerchantSearchProvider:
    """Build the one process-wide provider shared by worker and web routes."""

    return build_merchant_search_provider(
        merchant_search_config_from_settings(
            settings,
            private_terms=private_terms,
        ),
        transport=transport,
        clock=clock,
        now=now,
        cache_key_factory=cache_key_factory,
    )


def merchant_search_not_needed() -> MerchantSearchResult:
    return MerchantSearchResult(status="not_needed", provider="none")
