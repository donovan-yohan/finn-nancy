"""Evaluate FN-149B descriptor resolution without exposing hidden gold.

Controlled-reference output proves the evaluator contract and corpus health; it
never grants production automation.  A future production scorer must provide
an exact approved scorer/corpus/policy/knowledge tuple and pass every precision,
coverage, abstention, and evidence-authority gate.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable

from .fn149b_controlled_scorer import (
    AUTOMATIC_EVIDENCE_SOURCES,
    CONTROLLED_SCORER_ARTIFACT_DIGEST,
    controlled_reference_predictions,
)
from .fn149b_descriptor_corpus import (
    CONTROLLED_SCORER_VERSION,
    CORPUS_VERSION,
    GOLD_SCHEMA_VERSION,
    INPUT_SCHEMA_VERSION,
    KNOWLEDGE_VERSION,
    POLICY_VERSION,
    PREDICTION_SCHEMA_VERSION,
    REQUIRED_COHORT_COUNTS_PER_SPLIT,
    DescriptorPacket,
    PacketError,
    canonical_json_bytes,
    load_packet,
    policy_contract_digest,
    sanitize_scorer_input,
    scope_fingerprint,
    sha256_hex,
)
from .reconciliation_precision import wilson_lower_bound


CLAIM_NAMES = ("merchant", "category")
ALLOWED_STATUSES = frozenset({"resolved", "abstained", "no_evidence"})
SAFE_THRESHOLDS = {
    "precision_min": 0.995,
    "precision_lower_bound_min": 0.995,
    "confidence_level": 0.95,
    "false_assignments_max": 0,
    "required_abstention_rate": 1.0,
    "supported_cohort_coverage_min": 1.0,
}
SAFE_MINIMUMS = {
    split: {"category": 630, "merchant": 630}
    for split in ("developer", "sealed")
}
SAFE_MINIMUM_INDEPENDENT_CLUSTERS = {
    split: {"category": 600, "merchant": 600}
    for split in ("developer", "sealed")
}


def _blank_counts() -> dict[str, int]:
    return {
        "true_positive": 0,
        "false_positive": 0,
        "false_negative": 0,
        "eligible": 0,
        "automatic_on_eligible": 0,
    }


def _metric(counts: dict[str, int], confidence: float) -> dict[str, Any]:
    automatic = counts["true_positive"] + counts["false_positive"]
    eligible = counts["eligible"]
    return {
        **counts,
        "trial_unit": "descriptor_variant_diagnostic",
        "precision": (
            counts["true_positive"] / automatic if automatic else 0.0
        ),
        "precision_lower_bound": wilson_lower_bound(
            counts["true_positive"],
            automatic,
            confidence,
        ),
        "coverage": (
            counts["automatic_on_eligible"] / eligible if eligible else 0.0
        ),
        "correct_coverage": (
            counts["true_positive"] / eligible if eligible else 0.0
        ),
    }


def _validate_policy(policy: dict[str, Any], violations: list[str]) -> None:
    if policy.get("policy_version") != POLICY_VERSION:
        violations.append("policy version is not approved")
    if (
        policy.get("automation_authority")
        != "disabled_pending_approved_production_gate"
    ):
        violations.append("packet policy must keep production automation disabled")
    versions = policy.get("versions")
    expected_versions = {
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "gold_schema_version": GOLD_SCHEMA_VERSION,
        "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "knowledge_version": KNOWLEDGE_VERSION,
        "controlled_scorer_version": CONTROLLED_SCORER_VERSION,
    }
    if versions != expected_versions:
        violations.append("policy version tuple does not match evaluator contract")
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict):
        violations.append("policy thresholds are missing")
    else:
        for key, expected in SAFE_THRESHOLDS.items():
            if thresholds.get(key) != expected:
                violations.append(
                    f"policy {key} must equal the sealed value {expected!r}"
                )
    minimums = policy.get("minimum_eligible_per_split")
    if minimums != SAFE_MINIMUMS:
        violations.append("policy eligible-decision floors were weakened")
    cluster_minimums = policy.get("minimum_independent_clusters_per_split")
    if cluster_minimums != SAFE_MINIMUM_INDEPENDENT_CLUSTERS:
        violations.append("policy independent-cluster floors were weakened")
    required_counts = {
        split: dict(REQUIRED_COHORT_COUNTS_PER_SPLIT)
        for split in ("developer", "sealed")
    }
    if policy.get("required_cohort_counts_per_split") != required_counts:
        violations.append("policy descriptor cohort counts were weakened")
    if policy.get("automatic_evidence_sources") != ["confirmed_local"]:
        violations.append("policy automatic evidence authority was widened")


def _prediction_map(
    predictions: Iterable[dict[str, Any]], violations: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, prediction in enumerate(predictions):
        if not isinstance(prediction, dict):
            violations.append(f"prediction {index} must be an object")
            continue
        case_id = prediction.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in result:
            violations.append(f"duplicate or invalid prediction case id {case_id!r}")
            continue
        result[case_id] = prediction
    return result


def _evaluator_applicable_claim_ids(
    *,
    case: dict[str, Any],
    sealed_knowledge: dict[str, Any],
    claim_name: str,
) -> tuple[str, ...]:
    """Independently reconstruct the complete applicable claim set.

    This oracle intentionally does not share the controlled scorer's
    applicability helper: scorer omissions must remain observable to evaluation.
    """

    descriptor = case.get("descriptor")
    scope = case.get("scope")
    claims = sealed_knowledge.get("claims")
    if (
        not isinstance(descriptor, str)
        or not isinstance(scope, dict)
        or not isinstance(claims, list)
    ):
        return ()

    descriptor_folded = descriptor.casefold()
    applicable_ids: list[str] = []
    for knowledge_claim in claims:
        if not isinstance(knowledge_claim, dict):
            continue
        claim_id = knowledge_claim.get("claim_id")
        match_text = knowledge_claim.get("match_text")
        if (
            not isinstance(claim_id, str)
            or not claim_id
            or not isinstance(match_text, str)
            or not match_text
        ):
            continue
        if knowledge_claim.get("scope") != scope:
            continue
        if match_text.casefold() not in descriptor_folded:
            continue
        if not isinstance(knowledge_claim.get(claim_name), dict):
            continue
        applicable_ids.append(claim_id)
    return tuple(sorted(applicable_ids))


def _claim_shape(
    *,
    case: dict[str, Any],
    prediction: dict[str, Any],
    claim_name: str,
    knowledge_by_id: dict[str, dict[str, Any]],
    applicable_claim_ids: tuple[str, ...],
    violations: list[str],
) -> tuple[dict[str, Any], bool, bool]:
    case_id = str(case["case_id"])
    claim = prediction.get(claim_name)
    if not isinstance(claim, dict):
        violations.append(f"{case_id}: {claim_name} prediction must be an object")
        return {}, False, False
    required_fields = {
        "status",
        "target_id",
        "target_name",
        "claim_ids",
        "scope_fingerprint",
        "trust_state",
        "automatic_assignment_allowed",
        "evidence_sources",
    }
    if set(claim) != required_fields:
        violations.append(f"{case_id}: {claim_name} prediction fields drifted")
    status = claim.get("status")
    if status not in ALLOWED_STATUSES:
        violations.append(f"{case_id}: {claim_name} has invalid status {status!r}")
    claim_ids = claim.get("claim_ids")
    sources = claim.get("evidence_sources")
    if (
        not isinstance(claim_ids, list)
        or not all(isinstance(item, str) and item for item in claim_ids)
        or len(claim_ids) != len(set(claim_ids))
    ):
        violations.append(f"{case_id}: {claim_name} claim_ids are invalid")
        claim_ids = []
    if sorted(claim_ids) != sorted(applicable_claim_ids):
        violations.append(
            f"{case_id}: {claim_name} claim_ids do not equal the complete "
            "descriptor-applicable set"
        )
    if (
        not isinstance(sources, list)
        or not all(isinstance(item, str) and item for item in sources)
        or len(sources) != len(set(sources))
    ):
        violations.append(f"{case_id}: {claim_name} evidence_sources are invalid")
        sources = []
    bound_resolutions: list[dict[str, Any]] = []
    for claim_id in claim_ids:
        knowledge_claim = knowledge_by_id.get(claim_id)
        if knowledge_claim is None:
            violations.append(
                f"{case_id}: {claim_name} references unknown claim {claim_id}"
            )
            continue
        if knowledge_claim.get("scope") != case["scope"]:
            violations.append(
                f"{case_id}: {claim_name} claim {claim_id} is outside scope"
            )
            continue
        resolution = knowledge_claim.get(claim_name)
        if not isinstance(resolution, dict):
            violations.append(
                f"{case_id}: {claim_name} claim {claim_id} has no resolution"
            )
            continue
        bound_resolutions.append(resolution)
    bound_sources = sorted(
        {str(resolution.get("source")) for resolution in bound_resolutions}
    )
    if sources != bound_sources:
        violations.append(
            f"{case_id}: {claim_name} evidence sources do not match claim records"
        )
    expected_fingerprint = scope_fingerprint(case["scope"])
    if claim.get("scope_fingerprint") != expected_fingerprint:
        violations.append(f"{case_id}: {claim_name} scope fingerprint mismatch")
    automatic = claim.get("automatic_assignment_allowed") is True
    evidence_authoritative = bool(bound_resolutions) and (
        set(bound_sources) <= AUTOMATIC_EVIDENCE_SOURCES
    )
    if automatic:
        if status != "resolved":
            violations.append(
                f"{case_id}: {claim_name} automatic assignment is not resolved"
            )
        if not claim.get("target_id") or not claim.get("target_name"):
            violations.append(
                f"{case_id}: {claim_name} automatic assignment has no target"
            )
        if not claim_ids:
            violations.append(
                f"{case_id}: {claim_name} automatic assignment has no claims"
            )
        if not evidence_authoritative:
            violations.append(
                f"{case_id}: {claim_name} automatic assignment uses "
                "non-authoritative evidence"
            )
        bound_targets = {
            (
                resolution.get("target_id"),
                resolution.get("target_name"),
            )
            for resolution in bound_resolutions
        }
        predicted_target = (claim.get("target_id"), claim.get("target_name"))
        if bound_targets != {predicted_target}:
            violations.append(
                f"{case_id}: {claim_name} automatic target does not match claims"
            )
        if claim.get("trust_state") != "confirmed_local":
            violations.append(
                f"{case_id}: {claim_name} automatic assignment is not confirmed"
            )
    elif status != "resolved":
        if claim.get("target_id") is not None or claim.get("target_name") is not None:
            violations.append(
                f"{case_id}: {claim_name} abstention leaks a target decision"
            )
    return claim, automatic, evidence_authoritative


def _automation_receipt(
    *,
    packet: DescriptorPacket,
    policy: dict[str, Any],
    predictions: dict[str, dict[str, Any]],
    evaluation_ok: bool,
    authority_violations: list[str],
    production_evidence_blockers: list[str],
) -> dict[str, Any]:
    observed_kinds = {
        str(prediction.get("scorer", {}).get("kind"))
        for prediction in predictions.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    observed_versions = {
        str(prediction.get("scorer", {}).get("version"))
        for prediction in predictions.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    observed_artifacts = {
        str(prediction.get("scorer", {}).get("artifact_digest"))
        for prediction in predictions.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    observed_run_ids = {
        str(prediction.get("scorer", {}).get("run_id"))
        for prediction in predictions.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    blockers: list[str] = []
    blockers.append("independently_verifiable_production_run_receipt_missing")
    if observed_kinds != {"production"}:
        blockers.append("scorer_kind_is_not_production")
    if CONTROLLED_SCORER_VERSION in observed_versions:
        blockers.append("controlled_reference_version_has_no_authority")
    if CONTROLLED_SCORER_ARTIFACT_DIGEST in observed_artifacts:
        blockers.append("controlled_reference_artifact_has_no_authority")
    approved = policy.get("approved_production_tuple")
    if not isinstance(approved, dict):
        blockers.append("approved_production_tuple_missing")
    else:
        expected = {
            "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
            "corpus_version": CORPUS_VERSION,
            "corpus_digest": packet.corpus_digest,
            "policy_version": POLICY_VERSION,
            "policy_digest": packet.policy_digest,
            "knowledge_version": KNOWLEDGE_VERSION,
            "knowledge_digest": packet.knowledge_digest,
            "scorer_version": (
                next(iter(observed_versions)) if len(observed_versions) == 1 else None
            ),
            "scorer_artifact_digest": (
                next(iter(observed_artifacts))
                if len(observed_artifacts) == 1
                else None
            ),
        }
        if approved != expected:
            blockers.append("approved_production_tuple_mismatch")
    if any(
        prediction.get("scorer", {}).get("hidden_gold_access") is not False
        for prediction in predictions.values()
        if isinstance(prediction.get("scorer"), dict)
    ):
        blockers.append("hidden_gold_non_access_attestation_missing")
    if authority_violations:
        blockers.append("non_authoritative_evidence_present")
    blockers.extend(production_evidence_blockers)
    if not evaluation_ok:
        blockers.append("evaluation_gate_failed")
    blockers = list(dict.fromkeys(blockers))
    return {
        "enabled": not blockers,
        "authority": (
            "approved_production_gate" if not blockers else "disabled_fail_closed"
        ),
        "scorer_kinds": sorted(observed_kinds),
        "scorer_versions": sorted(observed_versions),
        "scorer_artifact_digests": sorted(observed_artifacts),
        "scorer_run_ids": sorted(observed_run_ids),
        "approved_production_tuple": deepcopy(approved),
        "observed_tuple": {
            "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
            "corpus_version": CORPUS_VERSION,
            "corpus_digest": packet.corpus_digest,
            "policy_version": POLICY_VERSION,
            "policy_digest": packet.policy_digest,
            "manifest_digest": packet.manifest_digest,
            "knowledge_version": KNOWLEDGE_VERSION,
            "knowledge_digest": packet.knowledge_digest,
        },
        "blockers": blockers,
    }


def evaluate_predictions(
    packet: DescriptorPacket,
    predictions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate prediction rows and issue a separate automation receipt."""

    violations: list[str] = []
    authority_violations: list[str] = []
    _validate_policy(packet.policy, violations)
    actual_corpus_digest = sha256_hex(
        b"".join(
            canonical_json_bytes(sanitize_scorer_input(case))
            for case in packet.public_cases
        )
    )
    actual_knowledge_digest = sha256_hex(canonical_json_bytes(packet.knowledge))
    actual_policy_digest = policy_contract_digest(packet.policy)
    if packet.corpus_digest != actual_corpus_digest:
        violations.append("expanded public corpus digest drifted")
    if packet.knowledge_digest != actual_knowledge_digest:
        violations.append("knowledge snapshot digest drifted")
    if packet.policy_digest != actual_policy_digest:
        violations.append("policy snapshot digest drifted")
    cases = {str(case["case_id"]): case for case in packet.public_cases}
    gold = {str(row["case_id"]): row for row in packet.gold_rows}
    knowledge_by_id = {
        str(claim["claim_id"]): claim
        for claim in packet.knowledge.get("claims", [])
        if isinstance(claim, dict) and "claim_id" in claim
    }
    prediction_by_id = _prediction_map(predictions, violations)
    if set(gold) != set(cases):
        violations.append("gold rows do not cover public cases exactly")
    missing = sorted(set(cases) - set(prediction_by_id))
    extra = sorted(set(prediction_by_id) - set(cases))
    if missing:
        violations.append(f"missing predictions for {missing[:3]!r}")
    if extra:
        violations.append(f"predictions reference unknown cases {extra[:3]!r}")

    metric_counts = {claim: _blank_counts() for claim in CLAIM_NAMES}
    split_counts = {
        split: {claim: _blank_counts() for claim in CLAIM_NAMES}
        for split in ("developer", "sealed")
    }
    cohort_coverage_counts: dict[
        str, dict[str, dict[str, int]]
    ] = defaultdict(lambda: defaultdict(lambda: {"eligible": 0, "automatic": 0}))
    cohort_abstention_counts: dict[
        str, dict[str, dict[str, int]]
    ] = defaultdict(lambda: defaultdict(lambda: {"required": 0, "correct": 0}))
    independent_clusters: dict[
        str, dict[str, dict[str, dict[str, int]]]
    ] = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: {"eligible": 0, "automatic": 0, "correct": 0}
            )
        )
    )
    scorer_kinds: set[str] = set()
    scorer_versions: set[str] = set()

    for case_id, case in cases.items():
        expected = gold.get(case_id, {})
        prediction = prediction_by_id.get(case_id)
        if prediction is None:
            continue
        split = str(case["split"])
        cohort = str(case["cohort"])
        version_checks = {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "corpus_version": CORPUS_VERSION,
            "policy_version": POLICY_VERSION,
            "policy_digest": packet.policy_digest,
            "manifest_digest": packet.manifest_digest,
            "knowledge_version": KNOWLEDGE_VERSION,
            "corpus_digest": packet.corpus_digest,
            "knowledge_digest": packet.knowledge_digest,
            "split": split,
        }
        for field, approved in version_checks.items():
            if prediction.get(field) != approved:
                violations.append(f"{case_id}: prediction {field} mismatch")
        scorer = prediction.get("scorer")
        if not isinstance(scorer, dict):
            violations.append(f"{case_id}: scorer identity is missing")
        else:
            if set(scorer) != {
                "kind",
                "version",
                "artifact_digest",
                "run_id",
                "hidden_gold_access",
            }:
                violations.append(f"{case_id}: scorer identity fields drifted")
            scorer_kind = str(scorer.get("kind"))
            scorer_version = str(scorer.get("version"))
            scorer_kinds.add(scorer_kind)
            scorer_versions.add(scorer_version)
            if scorer_kind not in {"controlled_reference", "production"}:
                violations.append(f"{case_id}: scorer kind is not recognized")
            if scorer_kind == "controlled_reference" and (
                scorer_version != CONTROLLED_SCORER_VERSION
                or scorer.get("artifact_digest")
                != CONTROLLED_SCORER_ARTIFACT_DIGEST
            ):
                violations.append(
                    f"{case_id}: controlled scorer identity is not approved"
                )
            if (
                scorer_kind == "production"
                and scorer.get("artifact_digest")
                == CONTROLLED_SCORER_ARTIFACT_DIGEST
            ):
                violations.append(
                    f"{case_id}: production scorer uses controlled artifact"
                )
            artifact_digest = scorer.get("artifact_digest")
            if (
                not isinstance(artifact_digest, str)
                or len(artifact_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in artifact_digest
                )
            ):
                violations.append(f"{case_id}: scorer artifact digest is invalid")
            if not isinstance(scorer.get("run_id"), str) or not scorer.get("run_id"):
                violations.append(f"{case_id}: scorer run id is missing")
            if scorer.get("hidden_gold_access") is not False:
                violations.append(
                    f"{case_id}: scorer did not attest hidden-gold non-access"
                )
        expected_input_digest = sha256_hex(
            canonical_json_bytes(sanitize_scorer_input(case))
        )
        if prediction.get("public_input_digest") != expected_input_digest:
            violations.append(f"{case_id}: public scorer input digest mismatch")

        for claim_name in CLAIM_NAMES:
            applicable_ids = _evaluator_applicable_claim_ids(
                case=case,
                sealed_knowledge=packet.knowledge,
                claim_name=claim_name,
            )
            claim, automatic, evidence_authoritative = _claim_shape(
                case=case,
                prediction=prediction,
                claim_name=claim_name,
                knowledge_by_id=knowledge_by_id,
                applicable_claim_ids=applicable_ids,
                violations=violations,
            )
            expectation = expected.get(claim_name)
            if not isinstance(expectation, dict):
                violations.append(f"{case_id}: {claim_name} gold is missing")
                continue
            counts = metric_counts[claim_name]
            by_split = split_counts[split][claim_name]

            def bump(field: str) -> None:
                counts[field] += 1
                by_split[field] += 1

            if expectation.get("decision") == "assign":
                bump("eligible")
                cohort_coverage_counts[claim_name][cohort]["eligible"] += 1
                if automatic:
                    bump("automatic_on_eligible")
                    cohort_coverage_counts[claim_name][cohort]["automatic"] += 1
                correct = automatic and (
                    claim.get("target_id") == expectation.get("target_id")
                )
                cluster = independent_clusters[split][claim_name][
                    str(case.get("template_id") or case["family_id"])
                ]
                cluster["eligible"] += 1
                if automatic:
                    cluster["automatic"] += 1
                if correct:
                    cluster["correct"] += 1
                if correct:
                    bump("true_positive")
                else:
                    bump("false_negative")
                    if automatic:
                        bump("false_positive")
            else:
                cohort_abstention_counts[claim_name][cohort]["required"] += 1
                if automatic:
                    bump("false_positive")
                else:
                    cohort_abstention_counts[claim_name][cohort]["correct"] += 1

            if automatic and not evidence_authoritative:
                authority_violations.append(
                    f"{case_id}:{claim_name}:non_authoritative_evidence"
                )

    if len(scorer_kinds) != 1:
        violations.append("prediction packet must use exactly one scorer kind")
    if len(scorer_versions) != 1:
        violations.append("prediction packet must use exactly one scorer version")
    run_ids = {
        str(prediction.get("scorer", {}).get("run_id"))
        for prediction in prediction_by_id.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    artifact_digests = {
        str(prediction.get("scorer", {}).get("artifact_digest"))
        for prediction in prediction_by_id.values()
        if isinstance(prediction.get("scorer"), dict)
    }
    if len(run_ids) != 1:
        violations.append("prediction packet must use exactly one scorer run id")
    if len(artifact_digests) != 1:
        violations.append(
            "prediction packet must use exactly one scorer artifact digest"
        )

    policy_thresholds = packet.policy.get("thresholds")
    thresholds = (
        policy_thresholds
        if isinstance(policy_thresholds, dict)
        and set(SAFE_THRESHOLDS) <= set(policy_thresholds)
        else SAFE_THRESHOLDS
    )
    confidence = float(thresholds["confidence_level"])
    metrics = {
        claim: _metric(metric_counts[claim], confidence)
        for claim in CLAIM_NAMES
    }
    metrics_by_split = {
        split: {
            claim: _metric(split_counts[split][claim], confidence)
            for claim in CLAIM_NAMES
        }
        for split in ("developer", "sealed")
    }
    for label, claim_metrics in [
        ("", metrics),
        *[
            (f"{split}:", metrics_by_split[split])
            for split in ("developer", "sealed")
        ],
    ]:
        for claim_name, metric in claim_metrics.items():
            prefix = f"{label}{claim_name}"
            if metric["false_positive"] > thresholds["false_assignments_max"]:
                violations.append(f"{prefix}: false assignments exceed policy")
            if metric["precision"] < thresholds["precision_min"]:
                violations.append(f"{prefix}: precision below policy")
            if (
                metric["precision_lower_bound"]
                < thresholds["precision_lower_bound_min"]
            ):
                violations.append(
                    f"{prefix}: one-sided precision lower bound below policy"
                )
    policy_minimums = packet.policy.get("minimum_eligible_per_split")
    minimums = (
        policy_minimums
        if isinstance(policy_minimums, dict)
        and all(
            isinstance(policy_minimums.get(split), dict)
            and set(CLAIM_NAMES) <= set(policy_minimums[split])
            for split in ("developer", "sealed")
        )
        else SAFE_MINIMUMS
    )
    for split in ("developer", "sealed"):
        for claim_name in CLAIM_NAMES:
            actual = metrics_by_split[split][claim_name]["eligible"]
            minimum = minimums[split][claim_name]
            if actual < minimum:
                violations.append(
                    f"{split}:{claim_name}: eligible decisions {actual} "
                    f"below approved minimum {minimum}"
                )

    independent_cluster_metrics: dict[str, dict[str, dict[str, Any]]] = {
        split: {} for split in ("developer", "sealed")
    }
    production_evidence_blockers: list[str] = []
    policy_cluster_minimums = packet.policy.get(
        "minimum_independent_clusters_per_split"
    )
    cluster_minimums = (
        policy_cluster_minimums
        if isinstance(policy_cluster_minimums, dict)
        and all(
            isinstance(policy_cluster_minimums.get(split), dict)
            and set(CLAIM_NAMES) <= set(policy_cluster_minimums[split])
            for split in ("developer", "sealed")
        )
        else SAFE_MINIMUM_INDEPENDENT_CLUSTERS
    )
    for split in ("developer", "sealed"):
        for claim_name in CLAIM_NAMES:
            clusters = independent_clusters[split][claim_name]
            trials = len(clusters)
            successes = sum(
                state["correct"] == state["eligible"]
                and state["automatic"] == state["eligible"]
                for state in clusters.values()
            )
            lower_bound = wilson_lower_bound(successes, trials, confidence)
            minimum = cluster_minimums[split][claim_name]
            independent_cluster_metrics[split][claim_name] = {
                "trial_unit": "independent_descriptor_template",
                "successful": successes,
                "eligible": trials,
                "precision": successes / trials if trials else 0.0,
                "precision_lower_bound": lower_bound,
                "minimum_required": minimum,
                "sufficient_for_production": (
                    trials >= minimum
                    and lower_bound
                    >= thresholds["precision_lower_bound_min"]
                ),
            }
            if trials < minimum:
                production_evidence_blockers.append(
                    f"{split}:{claim_name}:independent_cluster_count_below_policy"
                )
            if lower_bound < thresholds["precision_lower_bound_min"]:
                production_evidence_blockers.append(
                    f"{split}:{claim_name}:independent_cluster_lower_bound_below_policy"
                )

    supported_cohort_coverage: dict[str, dict[str, float]] = {}
    for claim_name, cohorts in cohort_coverage_counts.items():
        supported_cohort_coverage[claim_name] = {}
        for cohort, counts in sorted(cohorts.items()):
            rate = counts["automatic"] / counts["eligible"]
            supported_cohort_coverage[claim_name][cohort] = rate
            if rate < thresholds["supported_cohort_coverage_min"]:
                violations.append(
                    f"{claim_name}:{cohort}: supported cohort coverage below policy"
                )

    abstention_by_cohort: dict[str, dict[str, dict[str, Any]]] = {}
    for claim_name, cohorts in cohort_abstention_counts.items():
        abstention_by_cohort[claim_name] = {}
        for cohort, counts in sorted(cohorts.items()):
            rate = counts["correct"] / counts["required"]
            abstention_by_cohort[claim_name][cohort] = {
                **counts,
                "rate": rate,
            }
            if rate < thresholds["required_abstention_rate"]:
                violations.append(
                    f"{claim_name}:{cohort}: abstention rate below policy"
                )

    authority_violations = list(dict.fromkeys(authority_violations))
    evaluation_ok = not violations and not authority_violations
    receipt = _automation_receipt(
        packet=packet,
        policy=packet.policy,
        predictions=prediction_by_id,
        evaluation_ok=evaluation_ok,
        authority_violations=authority_violations,
        production_evidence_blockers=production_evidence_blockers,
    )
    return {
        "ok": evaluation_ok,
        "evidence_class": (
            "controlled_reference"
            if scorer_kinds == {"controlled_reference"}
            else "production_scoring"
            if scorer_kinds == {"production"}
            else "mixed_or_invalid"
        ),
        "packet_identity": packet.identity,
        "case_count": len(cases),
        "prediction_count": len(prediction_by_id),
        "metrics": metrics,
        "metrics_by_split": metrics_by_split,
        "independent_cluster_metrics": independent_cluster_metrics,
        "supported_cohort_coverage": supported_cohort_coverage,
        "abstention_by_cohort": abstention_by_cohort,
        "authority_violations": authority_violations,
        "violations": violations,
        "automation_receipt": receipt,
    }


def evaluate_packet(packet_dir: str | Path) -> dict[str, Any]:
    packet = load_packet(packet_dir)
    sanitized_inputs = tuple(
        sanitize_scorer_input(case) for case in packet.public_cases
    )
    predictions = controlled_reference_predictions(
        sanitized_inputs,
        packet.knowledge,
        corpus_digest=packet.corpus_digest,
        knowledge_digest=packet.knowledge_digest,
        manifest_digest=packet.manifest_digest,
        policy_digest=packet.policy_digest,
    )
    return evaluate_predictions(packet, predictions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packet-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "fn149b",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = evaluate_packet(args.packet_dir)
    except PacketError as exc:
        report = {
            "ok": False,
            "evidence_class": "invalid_packet",
            "automation_receipt": {
                "enabled": False,
                "authority": "disabled_fail_closed",
                "blockers": ["packet_integrity_failed"],
            },
            "violations": [str(exc)],
        }
    rendered = json.dumps(
        report,
        sort_keys=True,
        indent=2 if args.pretty else None,
        separators=None if args.pretty else (",", ":"),
    )
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
