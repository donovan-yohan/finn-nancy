"""Gold-blind controlled scorer for the synthetic FN-149B packet.

This module has no packet loader, filesystem access, label type, or evaluator
import.  Its public function accepts only already-sanitized inputs, the
synthetic knowledge snapshot, and opaque packet identity digests.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .fn149b_descriptor_corpus import (
    CONTROLLED_SCORER_VERSION,
    CORPUS_VERSION,
    KNOWLEDGE_VERSION,
    POLICY_VERSION,
    PREDICTION_SCHEMA_VERSION,
    canonical_json_bytes,
    scope_fingerprint,
    sha256_hex,
)


AUTOMATIC_EVIDENCE_SOURCES = frozenset({"confirmed_local"})
CONTROLLED_SCORER_ARTIFACT_DIGEST = sha256_hex(
    canonical_json_bytes(
        {
            "implementation": "app.evals.fn149b_controlled_scorer:"
            "controlled_reference_predictions",
            "version": CONTROLLED_SCORER_VERSION,
        }
    )
)


def applicable_claims(
    scorer_input: dict[str, Any],
    knowledge_claims: Iterable[dict[str, Any]],
    claim_name: str,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return every descriptor-and-scope-applicable claim for one claim type."""

    descriptor = str(scorer_input["descriptor"]).casefold()
    scope = scorer_input["scope"]
    candidates: list[tuple[str, dict[str, Any]]] = []
    for knowledge_claim in knowledge_claims:
        match_text = str(knowledge_claim.get("match_text") or "").casefold()
        if not match_text or match_text not in descriptor:
            continue
        if knowledge_claim.get("scope") != scope:
            continue
        resolution = knowledge_claim.get(claim_name)
        if isinstance(resolution, dict):
            candidates.append((str(knowledge_claim["claim_id"]), resolution))
    return tuple(sorted(candidates, key=lambda item: item[0]))


def _controlled_claim(
    scorer_input: dict[str, Any],
    knowledge_claims: Iterable[dict[str, Any]],
    claim_name: str,
) -> dict[str, Any]:
    candidates = applicable_claims(scorer_input, knowledge_claims, claim_name)
    fingerprint = scope_fingerprint(scorer_input["scope"])
    if not candidates:
        return {
            "status": "no_evidence",
            "target_id": None,
            "target_name": None,
            "claim_ids": [],
            "scope_fingerprint": fingerprint,
            "trust_state": "no_evidence",
            "automatic_assignment_allowed": False,
            "evidence_sources": [],
        }

    sources = sorted({str(value["source"]) for _, value in candidates})
    claim_ids = [claim_id for claim_id, _ in candidates]
    targets = {
        (str(value["target_id"]), str(value["target_name"]))
        for _, value in candidates
    }
    if set(sources) <= AUTOMATIC_EVIDENCE_SOURCES and len(targets) == 1:
        target_id, target_name = next(iter(targets))
        return {
            "status": "resolved",
            "target_id": target_id,
            "target_name": target_name,
            "claim_ids": claim_ids,
            "scope_fingerprint": fingerprint,
            "trust_state": "confirmed_local",
            "automatic_assignment_allowed": True,
            "evidence_sources": sources,
        }

    return {
        "status": "abstained",
        "target_id": None,
        "target_name": None,
        "claim_ids": claim_ids,
        "scope_fingerprint": fingerprint,
        "trust_state": (
            "conflicting_confirmed"
            if set(sources) <= AUTOMATIC_EVIDENCE_SOURCES
            else "untrusted_candidate"
        ),
        "automatic_assignment_allowed": False,
        "evidence_sources": sources,
    }


def controlled_reference_predictions(
    sanitized_inputs: Iterable[dict[str, Any]],
    knowledge: dict[str, Any],
    *,
    corpus_digest: str,
    knowledge_digest: str,
    manifest_digest: str,
    policy_digest: str,
) -> tuple[dict[str, Any], ...]:
    """Score explicit allowlisted input without a label or filesystem boundary."""

    claims = knowledge["claims"]
    predictions: list[dict[str, Any]] = []
    run_id = (
        "controlled-reference:"
        f"{corpus_digest[:16]}:{knowledge_digest[:16]}"
    )
    for raw_input in sanitized_inputs:
        scorer_input = {
            key: deepcopy(raw_input[key])
            for key in (
                "schema_version",
                "corpus_version",
                "case_id",
                "split",
                "descriptor",
                "scope",
            )
        }
        public_input_digest = sha256_hex(canonical_json_bytes(scorer_input))
        predictions.append(
            {
                "schema_version": PREDICTION_SCHEMA_VERSION,
                "corpus_version": CORPUS_VERSION,
                "case_id": scorer_input["case_id"],
                "split": scorer_input["split"],
                "scorer": {
                    "kind": "controlled_reference",
                    "version": CONTROLLED_SCORER_VERSION,
                    "artifact_digest": CONTROLLED_SCORER_ARTIFACT_DIGEST,
                    "run_id": run_id,
                    "hidden_gold_access": False,
                },
                "policy_version": POLICY_VERSION,
                "policy_digest": policy_digest,
                "manifest_digest": manifest_digest,
                "knowledge_version": KNOWLEDGE_VERSION,
                "corpus_digest": corpus_digest,
                "knowledge_digest": knowledge_digest,
                "public_input_digest": public_input_digest,
                **{
                    claim_name: _controlled_claim(
                        scorer_input,
                        claims,
                        claim_name,
                    )
                    for claim_name in ("merchant", "category")
                },
            }
        )
    return tuple(predictions)
