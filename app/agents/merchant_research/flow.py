"""The merchant research flow.

An explicitly invoked pass over descriptors that deterministic rules could not
explain. It mirrors, as code, the sequence a person follows by hand: strip the
processor noise, see whether anything is left worth looking up, look it up,
read the evidence, and either name the merchant with citations or say you do
not know.

Three properties hold regardless of configuration:

* nothing here writes to the ledger -- the flow returns proposals;
* a descriptor the deterministic layer already explains never causes a search,
  so there is no needless egress;
* a query is sanitized to merchant terms before it can leave, and strict-local
  mode refuses egress outright.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence

from pydantic import BaseModel, Field

from ...reconcile.descriptor_affixes import classify
from ..reconciliation_proposals.search import (
    BraveMerchantSearch,
    FallbackMerchantSearch,
    MerchantSearchConfig,
    MerchantSearchProvider,
    MerchantSearchError,
    MerchantSearchSanitizationError,
    NoMerchantSearch,
    SearxngMerchantSearch,
    sanitize_merchant_query,
)
from . import prompts
from .models import MerchantFinding, ResearchReport


class _ModelAnswer(BaseModel):
    """What the model is allowed to say back."""

    abstain: bool = Field(description="True when the merchant cannot be identified")
    canonical_merchant: str | None = Field(default=None)
    category: str | None = Field(default=None)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    citation_urls: list[str] = Field(default_factory=list)


def _finding(descriptor: str, **kwargs) -> MerchantFinding:
    return MerchantFinding(descriptor=descriptor, **kwargs)


def _abstain(descriptor: str, reason: str, *, searched: bool = False, **kwargs):
    return _finding(
        descriptor, resolved=False, abstention_reason=reason, searched=searched, **kwargs
    )


def research_descriptors(
    descriptors: Sequence[tuple[str, str]],
    *,
    config: MerchantSearchConfig,
    provider: MerchantSearchProvider | None = None,
    llm=None,
    private_terms: Iterable[str] = (),
    min_interval_s: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ResearchReport:
    """Research ``(descriptor, locality)`` pairs and return proposals.

    ``min_interval_s`` paces outbound searches. Both back ends throttle under
    burst -- Brave answers 429 past one query per second, and a scraper gets
    CAPTCHA-blocked -- and a throttled backend is indistinguishable from an
    unknown merchant, so pacing protects answer quality, not just the quota.
    """
    findings: list[MerchantFinding] = []
    diagnostics: list[str] = []
    structured = None
    last_search_at: float | None = None

    for descriptor, locality in descriptors:
        affixes = classify(descriptor, locality=locality)

        if affixes.non_merchant_kind:
            findings.append(_abstain(
                descriptor, "", source="non_merchant",
                processor=affixes.processor,
                notes=f"recognised as {affixes.non_merchant_kind}, not a merchant",
            ))
            continue

        if affixes.platform:
            # The marketplace is the merchant of record, so this is settled
            # without a model and without leaving the machine.
            findings.append(_finding(
                descriptor, resolved=True, source="deterministic",
                canonical_merchant=affixes.platform, confidence=1.0,
                processor=affixes.processor, platform=affixes.platform,
            ))
            continue

        evidence = ()
        searched = False
        egress_block = _egress_block(config)
        if egress_block is None and provider is not None:
            try:
                query = sanitize_merchant_query(
                    affixes.merchant_text or descriptor, private_terms=private_terms
                )
            except MerchantSearchSanitizationError as exc:
                findings.append(_abstain(
                    descriptor, "query_rejected", processor=affixes.processor,
                    notes=str(exc),
                ))
                continue
            if min_interval_s > 0 and last_search_at is not None:
                waited = clock() - last_search_at
                if waited < min_interval_s:
                    sleep(min_interval_s - waited)
            last_search_at = clock()
            result = provider.search(query, private_terms=private_terms)
            searched = result.status == "queried"
            evidence = result.evidence
            if result.error_code:
                diagnostics.append(f"{descriptor}: search {result.error_code}")
            if result.error_code == "engines_unavailable":
                # Retryable, and explicitly not "this merchant does not exist".
                findings.append(_abstain(
                    descriptor, "search_unavailable", processor=affixes.processor,
                    notes="every search backend refused the query",
                ))
                continue

        if llm is None:
            findings.append(_abstain(
                descriptor, egress_block or "model_unavailable",
                searched=searched, processor=affixes.processor,
            ))
            continue

        if structured is None:
            structured = llm.with_structured_output(
                _ModelAnswer, method="json_schema"
            )
        message = prompts.USER_TEMPLATE.format(
            descriptor=descriptor,
            locality=locality or "(none printed)",
            processor=affixes.processor or "(none detected)",
            evidence_block=prompts.evidence_block(evidence),
        )
        try:
            answer = structured.invoke(
                [{"role": "system", "content": prompts.SYSTEM},
                 {"role": "user", "content": message}]
            )
        except Exception as exc:  # the endpoint is remote and may be down
            diagnostics.append(f"{descriptor}: model {type(exc).__name__}")
            findings.append(_abstain(
                descriptor, "model_unavailable", searched=searched,
                processor=affixes.processor,
            ))
            continue

        findings.append(_interpret(
            descriptor, answer, affixes=affixes, evidence=evidence, searched=searched,
            egress_block=egress_block,
        ))

    resolved = sum(1 for item in findings if item.resolved)
    return ResearchReport(
        findings=tuple(findings),
        searched_count=sum(1 for item in findings if item.searched),
        resolved_count=resolved,
        abstained_count=len(findings) - resolved,
        diagnostics=tuple(diagnostics),
    )


def _egress_block(config: MerchantSearchConfig) -> str | None:
    """Why this descriptor may not be searched, or None when it may."""
    if config.strict_local_mode:
        # The kill switch outranks the feature default.
        return "strict_local_mode"
    if not config.enabled or config.provider == "none":
        return "search_disabled"
    return None


def _interpret(descriptor, answer, *, affixes, evidence, searched, egress_block):
    if answer.abstain or not (answer.canonical_merchant or "").strip():
        return _abstain(
            descriptor,
            "model_abstained" if not egress_block or searched else egress_block,
            searched=searched, processor=affixes.processor,
        )
    category = (answer.category or "").strip().lower()
    if category not in prompts.CATEGORIES:
        # An answer outside the closed vocabulary is not an answer.
        return _abstain(
            descriptor, "schema_invalid", searched=searched,
            processor=affixes.processor,
        )

    offered = {item.citation_url for item in evidence}
    citations = tuple(url for url in answer.citation_urls if url in offered)
    source = "web_evidence" if (searched and citations) else "local_model"
    return _finding(
        descriptor, resolved=True, source=source,
        canonical_merchant=answer.canonical_merchant.strip(),
        category=category,
        confidence=float(answer.confidence),
        citations=citations,
        searched=searched,
        processor=affixes.processor,
        platform=affixes.platform,
    )


def config_from_settings(settings) -> MerchantSearchConfig:
    """Build a search config from application settings.

    Search is enabled by default, but ``strict_local_mode`` is applied here so
    every caller inherits the kill switch rather than having to remember it.
    """
    allowed = tuple(
        item.strip()
        for item in str(settings.merchant_search_allowed_endpoints or "").split(",")
        if item.strip()
    )
    endpoint = str(settings.merchant_search_endpoint or "").strip()
    if endpoint and endpoint not in allowed:
        allowed = (*allowed, endpoint)
    provider = str(settings.merchant_search_provider or "none").strip().lower()
    consent_version = str(
        getattr(settings, "merchant_search_consent_version", "") or ""
    ).strip()
    return MerchantSearchConfig(
        provider="searxng" if provider == "searxng" else "none",
        enabled=bool(settings.merchant_search_enabled),
        strict_local_mode=bool(settings.strict_local_mode),
        consent_granted=bool(consent_version),
        consent_version=consent_version,
        endpoint=endpoint,
        allowed_endpoints=allowed,
        timeout_seconds=float(settings.merchant_search_timeout_s),
        max_results=int(settings.merchant_search_max_results),
    )


def brave_config_from_settings(settings) -> MerchantSearchConfig:
    """Fallback provider config. Shares the kill switch with the primary."""
    base = config_from_settings(settings)
    return MerchantSearchConfig(
        provider="brave",
        enabled=base.enabled,
        strict_local_mode=base.strict_local_mode,
        consent_granted=base.consent_granted,
        consent_version=base.consent_version,
        timeout_seconds=base.timeout_seconds,
        max_results=base.max_results,
        api_key=str(getattr(settings, "brave_api_key", "") or "").strip(),
    )


def build_research_provider(settings) -> MerchantSearchProvider:
    """Compose the privacy-preferred provider with a reliable fallback.

    strict_local_mode disables both, so the kill switch cannot be bypassed by
    configuring the fallback alone.
    """
    primary_config = config_from_settings(settings)
    fallback_config = brave_config_from_settings(settings)

    if primary_config.strict_local_mode or not primary_config.enabled:
        return NoMerchantSearch("disabled")

    primary = None
    if primary_config.provider == "searxng":
        try:
            primary = SearxngMerchantSearch(primary_config)
        except MerchantSearchError:
            primary = None

    fallback = None
    if fallback_config.api_key:
        try:
            fallback = BraveMerchantSearch(fallback_config)
        except MerchantSearchError:
            fallback = None

    if primary is not None and fallback is not None:
        return FallbackMerchantSearch(primary, fallback)
    if primary is not None:
        return primary
    if fallback is not None:
        return fallback
    return NoMerchantSearch("none")
