"""Persist research findings into the scoped merchant knowledge tables.

Two jobs, and the first matters more than the second:

*Look up before researching.* A descriptor already carrying a claim is
answered from local knowledge, with no search and no model call. This is what
makes research volume decay instead of repeating every run, and it is the only
real defence against a rate-limited back end.

*Record findings as proposals, never as facts.* Everything written here lands
as ``untrusted_proposal``. A web-derived claim carries its citation, because
the repository refuses a web proposal without one. Promotion to trusted
knowledge stays a human decision through the existing accept path.
"""
from __future__ import annotations

import hashlib
import sqlite3

from ...db import repo_merchant_knowledge
from ...db.repo_merchant_knowledge import Evidence, MerchantScope
from ...reconcile.descriptor_normalization import (
    DescriptorNormalizationError,
    normalize_descriptor_v2,
)
from ...reconcile.merchant_resolution import resolve_descriptor
from .models import MerchantFinding, RESEARCH_FLOW_VERSION

# Maps how a finding was reached onto who the knowledge tables record as actor.
_ACTOR_KIND = {
    "web_evidence": "web",
    "local_model": "model",
    "deterministic": "system",
}


def already_known(
    conn: sqlite3.Connection, descriptor: str, *, scope: MerchantScope
) -> bool:
    """True when local knowledge already answers this descriptor."""
    try:
        resolution = resolve_descriptor(conn, descriptor=descriptor, scope=scope)
    except Exception:
        return False
    return resolution.merchant.status == "resolved"


def has_pending_proposal(
    conn: sqlite3.Connection, descriptor: str, *, scope: MerchantScope
) -> bool:
    """True when this descriptor already carries a proposal awaiting review.

    An unaccepted proposal is deliberately not treated as knowledge, so it does
    not short-circuit resolution. It does short-circuit *research*: asking the
    same question again spends quota to produce an answer already queued for a
    person.
    """
    try:
        normalized = normalize_descriptor_v2(descriptor)
    except DescriptorNormalizationError:
        return False
    row = conn.execute(
        """SELECT 1
           FROM merchant_resolution_claims claim
           JOIN merchant_descriptor_patterns pattern
             ON pattern.id = claim.pattern_id
           WHERE pattern.pattern_fingerprint = ?
             AND pattern.scope_fingerprint = ?
             AND claim.claim_kind = 'canonical_merchant'
           LIMIT 1""",
        (normalized.fingerprint, scope.fingerprint),
    ).fetchone()
    return row is not None


def operation_key(finding: MerchantFinding, *, scope: MerchantScope) -> str:
    """Stable per descriptor, scope, and answer, so re-running is idempotent."""
    material = "\x1f".join((
        RESEARCH_FLOW_VERSION,
        finding.descriptor,
        finding.canonical_merchant,
        finding.source or "",
        scope.household_scope,
        str(scope.account_id or ""),
        scope.processor_family,
    ))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"merchant-research:{digest}"


def record_finding(
    conn: sqlite3.Connection,
    finding: MerchantFinding,
    *,
    scope: MerchantScope,
    evidence: Evidence = Evidence(),
) -> int | None:
    """Write one resolved finding as an untrusted proposal.

    Returns the claim id, or None when the finding carries nothing to record.
    """
    if not finding.resolved or not finding.canonical_merchant.strip():
        return None
    actor_kind = _ACTOR_KIND.get(finding.source or "", "model")
    citation = finding.citations[0] if finding.citations else ""
    if actor_kind == "web" and not citation:
        # The repository would refuse this, and rightly: a web claim without a
        # citation cannot be checked by the person asked to accept it.
        return None
    return repo_merchant_knowledge.propose_merchant(
        conn,
        descriptor=finding.descriptor,
        canonical_name=finding.canonical_merchant,
        scope=scope,
        operation_key=operation_key(finding, scope=scope),
        actor_kind=actor_kind,
        actor=f"merchant-research/{finding.source}",
        reason=_reason(finding),
        evidence=evidence,
        citation_url=citation,
    )


def _reason(finding: MerchantFinding) -> str:
    parts = [f"researched via {finding.source}"]
    if finding.processor:
        parts.append(f"processor {finding.processor}")
    if finding.confidence:
        parts.append(f"confidence {finding.confidence:.2f}")
    return "; ".join(parts)[:200]
