"""Brave fallback provider and the provider composition.

No network: every request is served by an httpx MockTransport, so the tests
assert the exact contract each vendor is called with.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents.reconciliation_proposals.search import (
    BRAVE_ENDPOINT,
    BraveMerchantSearch,
    FallbackMerchantSearch,
    MerchantSearchConfig,
    MerchantSearchConfigurationError,
    MerchantSearchResult,
    sanitize_merchant_query,
)

BRAVE = MerchantSearchConfig(
    provider="brave", enabled=True, strict_local_mode=False,
    consent_granted=True, consent_version="v1", api_key="test-key", max_results=3,
)


def _brave(handler) -> BraveMerchantSearch:
    return BraveMerchantSearch(BRAVE, transport=httpx.MockTransport(handler))


def test_brave_request_carries_the_documented_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url).split("?")[0]
        seen["token"] = request.headers.get("X-Subscription-Token")
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"web": {"results": [
                {"title": "SyntheticInk", "url": "https://syntheticink.com/",
                 "description": "pocket eReaders"},
            ]}},
        )

    provider = _brave(handler)
    result = provider.search(sanitize_merchant_query("SYNTHETICINK"))
    provider.close()

    assert seen["method"] == "GET"
    assert seen["url"] == BRAVE_ENDPOINT
    assert seen["token"] == "test-key"
    assert seen["params"]["q"] == "SYNTHETICINK"
    assert seen["params"]["count"] == "3"
    assert result.status == "queried"
    assert result.provider == "brave"
    assert result.evidence[0].citation_url == "https://syntheticink.com/"
    assert result.evidence[0].snippet == "pocket eReaders"
    assert result.evidence[0].provider == "brave"


def test_brave_quota_exhaustion_is_retryable_not_an_absent_merchant():
    """429 must not be recorded as 'this merchant does not exist'."""
    provider = _brave(lambda request: httpx.Response(429, json={}))
    result = provider.search(sanitize_merchant_query("SYNTHETICINK"))
    provider.close()
    assert result.status == "failed"
    assert result.error_code == "engines_unavailable"


def test_brave_other_http_errors_stay_distinct():
    provider = _brave(lambda request: httpx.Response(401, json={}))
    result = provider.search(sanitize_merchant_query("SYNTHETICINK"))
    provider.close()
    assert result.error_code == "http_error"


def test_brave_empty_results_are_a_real_answer():
    provider = _brave(lambda request: httpx.Response(200, json={"web": {"results": []}}))
    result = provider.search(sanitize_merchant_query("NOSUCHBRAND"))
    provider.close()
    assert result.status == "queried"
    assert result.error_code == ""
    assert result.evidence == ()


def test_brave_requires_an_api_key():
    with pytest.raises(MerchantSearchConfigurationError):
        BraveMerchantSearch(
            MerchantSearchConfig(
                provider="brave", enabled=True, strict_local_mode=False,
                consent_granted=True, consent_version="v1", api_key="",
            )
        )


class _Stub:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def search(self, query, *, private_terms=()):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]

    def resolve_evidence(self, **kwargs): ...
    def list_evidence(self, **kwargs): ...
    def close(self): ...


def _queried(n=1):
    from app.agents.reconciliation_proposals.search import MerchantSearchEvidence
    evidence = tuple(
        MerchantSearchEvidence(
            provider="searxng", citation_url=f"https://x.test/{i}", title=f"t{i}",
            snippet="s", content_digest="d" * 64, retrieved_at="2026-09-12T00:00:00Z",
        )
        for i in range(n)
    )
    return MerchantSearchResult(status="queried", provider="searxng", evidence=evidence)


def _blocked():
    return MerchantSearchResult(
        status="failed", provider="searxng", error_code="engines_unavailable"
    )


def test_fallback_is_not_used_when_the_primary_answers():
    primary, fallback = _Stub(_queried()), _Stub(_queried())
    composed = FallbackMerchantSearch(primary, fallback)
    composed.search(sanitize_merchant_query("SYNTHETICINK"))
    assert primary.calls == 1
    assert fallback.calls == 0


def test_fallback_takes_over_when_every_backend_is_blocked():
    primary, fallback = _Stub(_blocked()), _Stub(_queried())
    composed = FallbackMerchantSearch(primary, fallback)
    result = composed.search(sanitize_merchant_query("SYNTHETICINK"))
    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.status == "queried"


def test_a_genuine_empty_answer_does_not_leak_the_query_to_the_fallback():
    """The primary searched and found nothing. That is an answer.

    Asking a second vendor the same question would expose the query again for
    no new information.
    """
    primary, fallback = _Stub(_queried(0)), _Stub(_queried())
    composed = FallbackMerchantSearch(primary, fallback)
    composed.search(sanitize_merchant_query("NOSUCHBRAND"))
    assert fallback.calls == 0
