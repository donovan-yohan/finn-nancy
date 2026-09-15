"""Merchant research flow.

The properties under test are the ones that make the flow safe to leave on:
deterministic answers never cause egress, the kill switch outranks the feature
default, private terms never leave, and an unidentifiable descriptor abstains
instead of acquiring a plausible name.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agents.merchant_research import research_descriptors
from app.agents.merchant_research.flow import _ModelAnswer, config_from_settings
from app.agents.reconciliation_proposals.search import (
    MerchantSearchConfig,
    MerchantSearchEvidence,
    MerchantSearchResult,
)


def _evidence(url: str, title: str, snippet: str) -> MerchantSearchEvidence:
    return MerchantSearchEvidence(
        provider="searxng", citation_url=url, title=title, snippet=snippet,
        content_digest="d" * 64, retrieved_at="2026-09-12T00:00:00Z",
    )


class FakeProvider:
    """Records what was asked, so egress can be asserted on."""

    def __init__(self, evidence=()):
        self.evidence = tuple(evidence)
        self.queries: list[str] = []

    def search(self, query, *, private_terms=()):
        self.queries.append(query.value)
        return MerchantSearchResult(
            status="queried" if self.evidence else "queried",
            provider="searxng", sanitized_query=query.value, evidence=self.evidence,
        )

    def resolve_evidence(self, *a, **k): ...
    def list_evidence(self, *a, **k): ...
    def close(self): ...


class FakeLLM:
    def __init__(self, answer: _ModelAnswer):
        self.answer = answer
        self.prompts: list[str] = []

    def with_structured_output(self, schema, method=""):
        return self

    def invoke(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.answer


OPEN = MerchantSearchConfig(
    provider="searxng", enabled=True, strict_local_mode=False,
    consent_granted=True, endpoint="https://search.example.test",
    allowed_endpoints=("https://search.example.test",),
)


def test_deterministic_platform_never_searches_or_calls_a_model():
    provider, llm = FakeProvider(), FakeLLM(_ModelAnswer(abstain=True))
    report = research_descriptors(
        [("EXMP MARKET*SK1KG11G3", "EXAMPLEVILLE EX")],
        config=OPEN, provider=provider, llm=llm,
    )
    finding = report.findings[0]
    assert finding.resolved and finding.source == "deterministic"
    assert finding.canonical_merchant == "ExampleMarket"
    assert provider.queries == []
    assert llm.prompts == []


def test_non_merchant_descriptor_is_explained_without_egress():
    provider = FakeProvider()
    report = research_descriptors(
        [("PP*9003CODE", "0000000000 ON")],
        config=OPEN, provider=provider, llm=FakeLLM(_ModelAnswer(abstain=True)),
    )
    finding = report.findings[0]
    assert finding.source == "non_merchant"
    assert not finding.resolved
    assert "not a merchant" in finding.notes
    assert provider.queries == []


def test_strict_local_mode_outranks_the_enabled_default():
    config = MerchantSearchConfig(
        provider="searxng", enabled=True, strict_local_mode=True,
        consent_granted=True, endpoint="https://search.example.test",
        allowed_endpoints=("https://search.example.test",),
    )
    provider = FakeProvider([_evidence("https://x.test", "X", "x")])
    report = research_descriptors(
        [("SP SYNTHETICINK", "SYNTHETIC DISTRICT")],
        config=config, provider=provider,
        llm=FakeLLM(_ModelAnswer(abstain=True)),
    )
    assert provider.queries == []
    assert report.findings[0].searched is False


def test_evidence_backed_resolution_carries_only_offered_citations():
    real = "https://www.ltddir.com/companies/syntheticink-limited/"
    provider = FakeProvider([
        _evidence(real, "SyntheticInk Limited", "e-ink display company in Synthetic District"),
    ])
    llm = FakeLLM(_ModelAnswer(
        abstain=False, canonical_merchant="SyntheticInk", category="shopping",
        confidence=0.9, citation_urls=[real, "https://invented.test/made-up"],
    ))
    report = research_descriptors(
        [("SP SYNTHETICINK", "SYNTHETIC DISTRICT")], config=OPEN, provider=provider, llm=llm,
    )
    finding = report.findings[0]
    assert finding.resolved and finding.source == "web_evidence"
    assert finding.canonical_merchant == "SyntheticInk"
    # A URL the provider never returned cannot become a citation.
    assert finding.citations == (real,)


def test_no_evidence_leaves_the_model_free_to_abstain():
    provider = FakeProvider()
    llm = FakeLLM(_ModelAnswer(abstain=True))
    report = research_descriptors(
        [("SP SYNTHETIC NOISE", "SYNTHETIC REGION")], config=OPEN, provider=provider, llm=llm,
    )
    finding = report.findings[0]
    assert not finding.resolved
    assert finding.abstention_reason == "model_abstained"
    assert not finding.canonical_merchant
    assert "No web evidence" in llm.prompts[0]


def test_category_outside_the_closed_vocabulary_is_rejected():
    provider = FakeProvider([_evidence("https://x.test", "X", "x")])
    llm = FakeLLM(_ModelAnswer(
        abstain=False, canonical_merchant="Something", category="crypto",
        confidence=0.99, citation_urls=["https://x.test"],
    ))
    report = research_descriptors(
        [("SP MYSTERY", "")], config=OPEN, provider=provider, llm=llm,
    )
    assert report.findings[0].resolved is False
    assert report.findings[0].abstention_reason == "schema_invalid"


def test_amounts_and_dates_never_reach_the_query():
    provider = FakeProvider()
    research_descriptors(
        [("SP SOMESHOP 101.25 2026-07-17", "")],
        config=OPEN, provider=provider, llm=FakeLLM(_ModelAnswer(abstain=True)),
    )
    assert provider.queries == []


def test_private_terms_are_refused_before_egress():
    """A household name reaching the sanitizer must stop there."""
    provider = FakeProvider()
    report = research_descriptors(
        [("SQ *SAMPLE PERSON", "")],
        config=OPEN, provider=provider,
        llm=FakeLLM(_ModelAnswer(abstain=True)),
        private_terms=("sample person",),
    )
    assert provider.queries == []
    assert report.findings[0].abstention_reason == "query_rejected"


def test_a_person_to_person_transfer_is_stopped_before_the_sanitizer():
    """An e-transfer names a person, so it is settled deterministically and
    never becomes a query at all -- one guard earlier than the sanitizer."""
    provider = FakeProvider()
    llm = FakeLLM(_ModelAnswer(abstain=True))
    report = research_descriptors(
        [("Interac e-Transfer sent to Sample Member A", "")],
        config=OPEN, provider=provider, llm=llm,
    )
    assert provider.queries == []
    assert llm.prompts == []
    assert report.findings[0].source == "non_merchant"


def test_a_model_failure_abstains_rather_than_propagating():
    class Broken(FakeLLM):
        def invoke(self, messages):
            raise RuntimeError("endpoint down")

    report = research_descriptors(
        [("SP MYSTERY", "")], config=OPEN, provider=FakeProvider(),
        llm=Broken(_ModelAnswer(abstain=True)),
    )
    assert report.findings[0].abstention_reason == "model_unavailable"
    assert report.diagnostics


def test_report_counts_and_coverage():
    provider = FakeProvider()
    report = research_descriptors(
        [("EXMP MARKET*X1", "EXAMPLEVILLE EX"), ("SP SYNTHETIC NOISE", "")],
        config=OPEN, provider=provider, llm=FakeLLM(_ModelAnswer(abstain=True)),
    )
    assert report.resolved_count == 1
    assert report.abstained_count == 1
    assert report.coverage == pytest.approx(0.5)


def test_settings_enable_search_by_default_but_honour_strict_local(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    monkeypatch.setenv("MERCHANT_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("MERCHANT_SEARCH_ENDPOINT", "https://search.example.test")
    get_settings.cache_clear()
    config = config_from_settings(get_settings())
    assert config.enabled is True
    assert config.strict_local_mode is False
    assert config.provider == "searxng"

    monkeypatch.setenv("STRICT_LOCAL_MODE", "true")
    get_settings.cache_clear()
    assert config_from_settings(get_settings()).strict_local_mode is True
    get_settings.cache_clear()


class RefusingProvider(FakeProvider):
    """SearXNG answered, but every upstream engine declined the query."""

    def search(self, query, *, private_terms=()):
        self.queries.append(query.value)
        return MerchantSearchResult(
            status="failed", provider="searxng", sanitized_query=query.value,
            error_code="engines_unavailable",
        )


def test_blocked_search_engines_are_not_reported_as_an_absent_merchant():
    """Rate limiting must not masquerade as 'this merchant does not exist'.

    Both arrive as zero evidence. Collapsing them would record a retryable
    outage as a researched dead end.
    """
    provider = RefusingProvider()
    llm = FakeLLM(_ModelAnswer(abstain=True))
    report = research_descriptors(
        [("SP MYSTERY", "")], config=OPEN, provider=provider, llm=llm,
    )
    finding = report.findings[0]
    assert finding.abstention_reason == "search_unavailable"
    assert finding.abstention_reason != "model_abstained"
    # The model is never asked, because there was nothing to reason over.
    assert llm.prompts == []
    assert report.diagnostics


def test_outbound_searches_are_paced():
    """Both back ends throttle under burst, and a throttled back end is
    indistinguishable from an unknown merchant, so pacing protects answers."""
    now = [0.0]
    slept: list[float] = []
    provider = FakeProvider([_evidence("https://x.test", "X", "x")])
    llm = FakeLLM(_ModelAnswer(abstain=True))

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    research_descriptors(
        [("SP ONE", ""), ("SP TWO", ""), ("SP THREE", "")],
        config=OPEN, provider=provider, llm=llm,
        min_interval_s=1.2, clock=lambda: now[0], sleep=sleep,
    )
    assert provider.queries == ["ONE", "TWO", "THREE"]
    # The first query is immediate; each later one waits out the interval.
    assert slept == [1.2, 1.2]


def test_pacing_is_off_by_default():
    provider = FakeProvider([_evidence("https://x.test", "X", "x")])
    slept: list[float] = []
    research_descriptors(
        [("SP ONE", ""), ("SP TWO", "")],
        config=OPEN, provider=provider, llm=FakeLLM(_ModelAnswer(abstain=True)),
        sleep=lambda s: slept.append(s),
    )
    assert slept == []
