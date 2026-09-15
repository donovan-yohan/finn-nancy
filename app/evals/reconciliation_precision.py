"""Deterministic FN-149A precision evaluator.

This module evaluates externally supplied decisions against a versioned
synthetic corpus. It never imports a matcher, model client, or web provider and
has no ledger write path.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from math import sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any

from .fn149_corpus import (
    CASE_SCHEMA_VERSION,
    CORPUS_VERSION,
    KNOWLEDGE_VERSION,
    MANIFEST_VERSION,
    POLICY_VERSION,
    PREDICTION_SCHEMA_VERSION,
    MINIMUM_ELIGIBLE_DECISIONS_PER_SPLIT,
    REQUIRED_COHORT_COUNTS_PER_SPLIT,
    SCORER_VERSION,
    TAXONOMY_VERSION,
)

SAFE_PRECISION_MIN = 0.995
SAFE_CONFIDENCE_LEVEL = 0.95
CONTROLLED_DISABLED_MODE = {
    "local_model": "disabled_controlled_placeholder",
    "web_search": "disabled_controlled_placeholder",
}
REQUIRED_SPLITS = ("developer", "sealed")


class BundleError(ValueError):
    """Raised when corpus integrity or shape is invalid."""


def wilson_lower_bound(successes: int, trials: int, confidence: float = 0.95) -> float:
    """One-sided Wilson score lower bound for a binomial proportion.

    The z score comes from the standard normal inverse CDF. A zero-trial result
    is deliberately zero so a small/empty pack cannot claim precision.
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("successes and trials must satisfy 0 <= successes <= trials")
    if trials == 0:
        return 0.0
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between 0.5 and 1")
    z = NormalDist().inv_cdf(confidence)
    proportion = successes / trials
    z2 = z * z
    center = proportion + z2 / (2 * trials)
    margin = z * sqrt(
        proportion * (1 - proportion) / trials + z2 / (4 * trials * trials)
    )
    return max(0.0, (center - margin) / (1 + z2 / trials))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"{path.name}: root must be an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BundleError(f"{path.name}: cannot read: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise BundleError(f"{path.name}:{line_number}: blank lines are forbidden")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"{path.name}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise BundleError(f"{path.name}:{line_number}: row must be an object")
        rows.append(value)
    return rows


def _safe_child(root: Path, relative: str) -> Path:
    child = Path(relative)
    if child.is_absolute() or ".." in child.parts or len(child.parts) != 1:
        raise BundleError(f"manifest path is not a safe bundle filename: {relative!r}")
    return root / child


def _load_bundle(root: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _read_json(root / "manifest.v1.json")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise BundleError("manifest version is not approved")
    cases: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    policy: dict[str, Any] | None = None
    seen_paths: set[str] = set()
    seen_split_files: dict[str, set[str]] = {
        "corpus": set(),
        "predictions": set(),
    }
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BundleError("manifest files must be a non-empty list")
    for entry in files:
        if not isinstance(entry, dict):
            raise BundleError("manifest file entry must be an object")
        relative = str(entry.get("path") or "")
        if relative in seen_paths:
            raise BundleError(f"manifest path is duplicated: {relative}")
        seen_paths.add(relative)
        path = _safe_child(root, relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BundleError(f"{relative}: cannot read: {exc}") from exc
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != entry.get("sha256"):
            raise BundleError(
                f"{relative}: checksum drift expected {entry.get('sha256')} got {actual_sha}"
            )
        if payload.count(b"\n") != entry.get("line_count"):
            raise BundleError(f"{relative}: line_count drift")
        kind = entry.get("kind")
        if kind == "corpus":
            split = entry.get("split")
            if split not in REQUIRED_SPLITS or split in seen_split_files["corpus"]:
                raise BundleError(f"{relative}: duplicate or invalid corpus split {split!r}")
            seen_split_files["corpus"].add(str(split))
            rows = _read_jsonl(path)
            if any(row.get("split") != split for row in rows):
                raise BundleError(f"{relative}: case split does not match manifest file split")
            cases.extend(rows)
        elif kind == "predictions":
            split = entry.get("split")
            if split not in REQUIRED_SPLITS or split in seen_split_files["predictions"]:
                raise BundleError(f"{relative}: duplicate or invalid prediction split {split!r}")
            seen_split_files["predictions"].add(str(split))
            rows = _read_jsonl(path)
            if any(row.get("split") != split for row in rows):
                raise BundleError(
                    f"{relative}: prediction split does not match manifest file split"
                )
            predictions.extend(rows)
        elif kind == "policy":
            if entry.get("split") is not None:
                raise BundleError(f"{relative}: policy file must not declare a split")
            if policy is not None:
                raise BundleError("manifest contains multiple policy files")
            policy = _read_json(path)
        else:
            raise BundleError(f"{relative}: unsupported manifest kind {kind!r}")
    if policy is None:
        raise BundleError("manifest has no policy file")
    required = set(REQUIRED_SPLITS)
    for kind, splits in seen_split_files.items():
        if splits != required:
            raise BundleError(
                f"manifest {kind} split files expected {sorted(required)!r} got {sorted(splits)!r}"
            )
    return manifest, policy, cases, predictions


def _validate_policy(policy: dict[str, Any], violations: list[str]) -> None:
    if policy.get("policy_version") != POLICY_VERSION:
        violations.append("policy version is not approved")
    if policy.get("automation_authority") != "disabled_foundation_only":
        violations.append("foundation policy must keep production automation disabled")
    approved = policy.get("approved_versions")
    expected = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "scorer_version": SCORER_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "merchant_knowledge_version": KNOWLEDGE_VERSION,
    }
    if approved != expected:
        violations.append(f"approved version tuple mismatch: expected {expected!r}")
    if policy.get("required_splits") != list(REQUIRED_SPLITS):
        violations.append(f"policy required_splits must equal {list(REQUIRED_SPLITS)!r}")
    if policy.get("required_modes") != CONTROLLED_DISABLED_MODE:
        violations.append("policy controlled modes must keep local model and web disabled")
    required_counts = {
        split: dict(sorted(REQUIRED_COHORT_COUNTS_PER_SPLIT.items()))
        for split in REQUIRED_SPLITS
    }
    if policy.get("required_cohort_counts_per_split") != required_counts:
        violations.append("policy required hard-cohort counts do not match the approved contract")
    minimum_eligible = {
        split: dict(MINIMUM_ELIGIBLE_DECISIONS_PER_SPLIT)
        for split in REQUIRED_SPLITS
    }
    if policy.get("minimum_eligible_decisions_per_split") != minimum_eligible:
        violations.append("policy minimum eligible decision counts do not match the approved contract")
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict):
        violations.append("policy thresholds missing")
        return
    if float(thresholds.get("precision_min", 0.0)) < SAFE_PRECISION_MIN:
        violations.append("policy precision_min weakens the 99.5% floor")
    if float(thresholds.get("precision_lower_bound_min", 0.0)) < SAFE_PRECISION_MIN:
        violations.append("policy precision lower-bound floor is below 99.5%")
    if float(thresholds.get("confidence_level", 0.0)) != SAFE_CONFIDENCE_LEVEL:
        violations.append("policy confidence_level must be one-sided 95%")
    exact = {
        "false_assignments_max": 0,
        "duplicate_row_uses_max": 0,
        "required_abstention_rate": 1.0,
        "expense_total_delta_cents_max": 0,
    }
    for key, expected_value in exact.items():
        if thresholds.get(key) != expected_value:
            violations.append(f"policy {key} must equal {expected_value!r}")


def _metric_result(metric: dict[str, int], confidence: float) -> dict[str, Any]:
    tp = metric["true_positive"]
    fp = metric["false_positive"]
    fn = metric["false_negative"]
    decisions = tp + fp
    eligible = metric["eligible"]
    return {
        **metric,
        "precision": tp / decisions if decisions else 0.0,
        "precision_lower_bound": wilson_lower_bound(tp, decisions, confidence),
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "coverage": metric["automatic_on_eligible"] / eligible if eligible else 0.0,
    }


def _blank_metric_counts() -> dict[str, dict[str, int]]:
    return {
        name: {
            "true_positive": 0,
            "false_positive": 0,
            "false_negative": 0,
            "eligible": 0,
            "automatic_on_eligible": 0,
        }
        for name in ("same_event", "canonical_merchant", "expense_category")
    }


def _claim_predictions(
    rows: Any,
    id_key: str,
    *,
    case_id: str,
    claim_name: str,
    violations: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        violations.append(f"{case_id}: {claim_name} predictions must be a list")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            violations.append(
                f"{case_id}: {claim_name} prediction {index} must be an object"
            )
            continue
        identifier = row.get(id_key)
        if not isinstance(identifier, str) or not identifier:
            violations.append(
                f"{case_id}: {claim_name} prediction {index} has invalid {id_key}"
            )
            continue
        if identifier in out:
            violations.append(
                f"{case_id}: duplicate {claim_name} prediction id {identifier}"
            )
            continue
        out[identifier] = row
    return out


def evaluate_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    violations: list[str] = []
    try:
        manifest, policy, cases, predictions = _load_bundle(root)
    except BundleError as exc:
        return {
            "ok": False,
            "automation_authority": "disabled_foundation_only",
            "violations": [str(exc)],
        }

    _validate_policy(policy, violations)
    approved = policy.get("approved_versions") if isinstance(policy.get("approved_versions"), dict) else {}
    required_modes = CONTROLLED_DISABLED_MODE
    thresholds = policy.get("thresholds") if isinstance(policy.get("thresholds"), dict) else {}
    confidence = float(thresholds.get("confidence_level", SAFE_CONFIDENCE_LEVEL))

    case_by_id: dict[str, dict[str, Any]] = {}
    prediction_by_id: dict[str, dict[str, Any]] = {}
    families: dict[str, set[str]] = defaultdict(set)
    descriptors: dict[str, set[str]] = defaultdict(set)
    global_row_ids: set[str] = set()
    case_counts_by_split: dict[str, Counter[str]] = defaultdict(Counter)

    for case in cases:
        case_id = str(case.get("case_id") or "")
        split = str(case.get("split") or "")
        if not case_id or case_id in case_by_id:
            violations.append(f"duplicate or empty case_id {case_id!r}")
            continue
        case_by_id[case_id] = case
        if case.get("schema_version") != approved.get("case_schema_version"):
            violations.append(f"{case_id}: case schema version mismatch")
        if case.get("corpus_version") != approved.get("corpus_version"):
            violations.append(f"{case_id}: corpus version mismatch")
        if split not in {"developer", "sealed"}:
            violations.append(f"{case_id}: invalid split {split!r}")
        cohort = str(case.get("cohort") or "")
        case_counts_by_split[split][cohort] += 1
        family_id = str(case.get("family_id") or "")
        if not family_id.startswith("synthetic-family:"):
            violations.append(f"{case_id}: family_id is not synthetic")
        families[split].add(family_id)
        privacy = case.get("privacy")
        if not isinstance(privacy, dict) or privacy.get("source") != "deterministic_generator" or privacy.get("contains_personal_data") is not False:
            violations.append(f"{case_id}: privacy declaration invalid")
        rows = case.get("rows")
        if not isinstance(rows, list) or not rows:
            violations.append(f"{case_id}: rows missing")
            continue
        local_row_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                violations.append(f"{case_id}: row is not an object")
                continue
            row_id = str(row.get("row_id") or "")
            descriptor = str(row.get("descriptor") or "")
            if not row_id.startswith("synthetic-row:") or row_id in global_row_ids:
                violations.append(f"{case_id}: duplicate or non-synthetic row_id {row_id!r}")
            global_row_ids.add(row_id)
            local_row_ids.add(row_id)
            if not descriptor.startswith("SYNTH-"):
                violations.append(f"{case_id}: descriptor is not generator-marked synthetic")
            descriptors[split].add(descriptor.casefold())
            if row.get("privacy_tag") != "deterministic_synthetic":
                violations.append(f"{case_id}: row privacy tag invalid")
        candidates = case.get("same_event_candidates")
        candidate_ids: set[str] = set()
        if not isinstance(candidates, list):
            violations.append(f"{case_id}: candidates missing")
            candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                violations.append(f"{case_id}: candidate is not an object")
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            if candidate_id in candidate_ids or not candidate_id.startswith("synthetic-candidate:"):
                violations.append(f"{case_id}: duplicate or invalid candidate {candidate_id!r}")
            candidate_ids.add(candidate_id)
            source_ids = candidate.get("source_row_ids")
            if not isinstance(source_ids, list) or len(source_ids) < 2 or not set(source_ids) <= local_row_ids:
                violations.append(f"{case_id}: candidate {candidate_id} has invalid source rows")
        gold = case.get("gold")
        if not isinstance(gold, dict):
            violations.append(f"{case_id}: gold labels missing")
            continue
        same_gold = gold.get("same_event")
        if not isinstance(same_gold, dict) or set(same_gold) != candidate_ids:
            violations.append(f"{case_id}: same-event gold does not cover candidates exactly")
        for claim_name in ("canonical_merchant", "expense_category"):
            claim_gold = gold.get(claim_name)
            if not isinstance(claim_gold, dict) or not set(claim_gold) <= local_row_ids:
                violations.append(f"{case_id}: {claim_name} gold references invalid rows")
            if isinstance(claim_gold, dict):
                prefix = "synthetic-merchant:" if claim_name == "canonical_merchant" else "synthetic-category:"
                for value in claim_gold.values():
                    if value != "abstain" and not str(value).startswith(prefix):
                        violations.append(f"{case_id}: {claim_name} gold value is not synthetic")

    overlap = sorted(families["developer"] & families["sealed"])
    if overlap:
        violations.append(f"family split leakage: {overlap[:5]!r}")
    descriptor_overlap = sorted(descriptors["developer"] & descriptors["sealed"])
    if descriptor_overlap:
        violations.append(f"descriptor split leakage: {descriptor_overlap[:5]!r}")
    required_splits = set(policy.get("required_splits") or [])
    actual_splits = {str(case.get("split")) for case in cases}
    if actual_splits != required_splits:
        violations.append(
            f"required split mismatch expected {sorted(required_splits)!r} got {sorted(actual_splits)!r}"
        )
    for split in REQUIRED_SPLITS:
        actual_counts = dict(sorted(case_counts_by_split[split].items()))
        if actual_counts != dict(sorted(REQUIRED_COHORT_COUNTS_PER_SPLIT.items())):
            violations.append(
                f"{split}: hard-cohort counts mismatch expected "
                f"{dict(sorted(REQUIRED_COHORT_COUNTS_PER_SPLIT.items()))!r} got "
                f"{actual_counts!r}"
            )

    for prediction in predictions:
        case_id = str(prediction.get("case_id") or "")
        if not case_id or case_id in prediction_by_id:
            violations.append(f"duplicate or empty prediction case_id {case_id!r}")
            continue
        prediction_by_id[case_id] = prediction
        version_checks = {
            "schema_version": "prediction_schema_version",
            "corpus_version": "corpus_version",
            "scorer_version": "scorer_version",
            "taxonomy_version": "taxonomy_version",
            "merchant_knowledge_version": "merchant_knowledge_version",
        }
        for field, approved_field in version_checks.items():
            if prediction.get(field) != approved.get(approved_field):
                violations.append(f"{case_id}: prediction {field} is not the approved version")
        if prediction.get("mode") != CONTROLLED_DISABLED_MODE:
            violations.append(f"{case_id}: controlled mode mismatch")

    missing_predictions = sorted(set(case_by_id) - set(prediction_by_id))
    extra_predictions = sorted(set(prediction_by_id) - set(case_by_id))
    if missing_predictions:
        violations.append(f"missing predictions for cases {missing_predictions[:5]!r}")
    if extra_predictions:
        violations.append(f"predictions reference unknown cases {extra_predictions[:5]!r}")
    for case_id in sorted(set(case_by_id) & set(prediction_by_id)):
        if prediction_by_id[case_id].get("split") != case_by_id[case_id].get("split"):
            violations.append(f"{case_id}: prediction split does not match case split")

    metric_counts = _blank_metric_counts()
    split_metric_counts = {
        split: _blank_metric_counts()
        for split in REQUIRED_SPLITS
    }
    cohort_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"eligible": 0, "automatic": 0})
    )
    abstention_required = 0
    abstention_correct = 0
    consumed_by: dict[str, list[str]] = defaultdict(list)
    expected_totals: dict[str, int] = defaultdict(int)
    actual_totals: dict[str, int] = defaultdict(int)

    for case_id, case in case_by_id.items():
        prediction = prediction_by_id.get(case_id, {})
        gold = case.get("gold") if isinstance(case.get("gold"), dict) else {}
        support = case.get("automation_support") if isinstance(case.get("automation_support"), dict) else {}
        cohort = str(case.get("cohort") or "")
        split = str(case.get("split") or "")

        def bump(claim_name: str, field: str) -> None:
            metric_counts[claim_name][field] += 1
            if split in split_metric_counts:
                split_metric_counts[split][claim_name][field] += 1

        rows_by_id = {
            str(row["row_id"]): row
            for row in case.get("rows", [])
            if isinstance(row, dict) and "row_id" in row
        }
        candidates_by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in case.get("same_event_candidates", [])
            if isinstance(candidate, dict) and "candidate_id" in candidate
        }

        same_predictions = _claim_predictions(
            prediction.get("same_event"),
            "candidate_id",
            case_id=case_id,
            claim_name="same_event",
            violations=violations,
        )
        same_gold = gold.get("same_event") if isinstance(gold.get("same_event"), dict) else {}
        for candidate_id, expected in same_gold.items():
            predicted = same_predictions.get(candidate_id, {})
            decision = predicted.get("decision")
            supported = support.get("same_event") is True
            requires_abstention = expected != "match" or not supported
            if requires_abstention:
                abstention_required += 1
                if decision == "abstain":
                    abstention_correct += 1
            if expected == "match" and supported:
                bump("same_event", "eligible")
                cohort_counts["same_event"][cohort]["eligible"] += 1
            if decision == "automatic_match":
                if expected == "match" and supported:
                    bump("same_event", "true_positive")
                    bump("same_event", "automatic_on_eligible")
                    cohort_counts["same_event"][cohort]["automatic"] += 1
                else:
                    bump("same_event", "false_positive")
                candidate = candidates_by_id.get(candidate_id, {})
                for row_id in candidate.get("source_row_ids", []):
                    consumed_by[str(row_id)].append(f"{case_id}:{candidate_id}")
            else:
                if expected == "match" and supported:
                    bump("same_event", "false_negative")
            if decision not in {"automatic_match", "abstain"}:
                violations.append(f"{case_id}: invalid or missing same_event decision for {candidate_id}")
        for candidate_id in set(same_predictions) - set(same_gold):
            violations.append(f"{case_id}: same_event prediction references unknown candidate {candidate_id}")

        for claim_name in ("canonical_merchant", "expense_category"):
            predictions_for_claim = _claim_predictions(
                prediction.get(claim_name),
                "subject_row_id",
                case_id=case_id,
                claim_name=claim_name,
                violations=violations,
            )
            claim_gold = gold.get(claim_name) if isinstance(gold.get(claim_name), dict) else {}
            for subject_id, expected in claim_gold.items():
                predicted = predictions_for_claim.get(subject_id, {})
                decision = predicted.get("decision")
                supported = support.get(claim_name) is True
                requires_abstention = expected == "abstain" or not supported
                if requires_abstention:
                    abstention_required += 1
                    if decision == "abstain":
                        abstention_correct += 1
                if expected != "abstain" and supported:
                    bump(claim_name, "eligible")
                    cohort_counts[claim_name][cohort]["eligible"] += 1
                if decision == "automatic_assign":
                    if expected != "abstain" and supported:
                        bump(claim_name, "automatic_on_eligible")
                        cohort_counts[claim_name][cohort]["automatic"] += 1
                    if expected != "abstain" and supported and predicted.get("value") == expected:
                        bump(claim_name, "true_positive")
                    else:
                        bump(claim_name, "false_positive")
                        if expected != "abstain" and supported:
                            bump(claim_name, "false_negative")
                    if claim_name == "expense_category":
                        row = rows_by_id.get(subject_id, {})
                        amount_cents = int(row.get("amount_cents") or 0)
                        value = predicted.get("value")
                        if amount_cents < 0 and isinstance(value, str):
                            actual_totals[value] += amount_cents
                else:
                    if expected != "abstain" and supported:
                        bump(claim_name, "false_negative")
                if decision not in {"automatic_assign", "abstain"}:
                    violations.append(
                        f"{case_id}: invalid or missing {claim_name} decision for {subject_id}"
                    )
            for subject_id in set(predictions_for_claim) - set(claim_gold):
                violations.append(f"{case_id}: {claim_name} prediction references unknown row {subject_id}")

        declared_totals = gold.get("expense_totals_cents")
        computed_case_totals: dict[str, int] = defaultdict(int)
        category_gold = gold.get("expense_category") if isinstance(gold.get("expense_category"), dict) else {}
        for subject_id, expected in category_gold.items():
            row = rows_by_id.get(subject_id, {})
            amount_cents = int(row.get("amount_cents") or 0)
            if expected != "abstain" and amount_cents < 0:
                computed_case_totals[str(expected)] += amount_cents
        if dict(sorted(computed_case_totals.items())) != declared_totals:
            violations.append(f"{case_id}: declared gold expense totals do not match source rows")
        if isinstance(declared_totals, dict):
            for category_id, amount_cents in declared_totals.items():
                expected_totals[str(category_id)] += int(amount_cents)

    metrics = {
        name: _metric_result(counts, confidence)
        for name, counts in metric_counts.items()
    }
    metrics_by_split = {
        split: {
            name: _metric_result(counts, confidence)
            for name, counts in split_metric_counts[split].items()
        }
        for split in REQUIRED_SPLITS
    }
    precision_min = float(thresholds.get("precision_min", SAFE_PRECISION_MIN))
    lower_min = float(thresholds.get("precision_lower_bound_min", SAFE_PRECISION_MIN))

    def gate_metric_set(prefix: str, claim_metrics: dict[str, dict[str, Any]]) -> None:
        for name, metric in claim_metrics.items():
            label = f"{prefix}{name}"
            if metric["false_positive"] > int(
                thresholds.get("false_assignments_max", 0)
            ):
                violations.append(
                    f"{label}: false automatic assignments exceed policy"
                )
            if metric["precision"] < precision_min:
                violations.append(f"{label}: precision below policy")
            if metric["precision_lower_bound"] < lower_min:
                violations.append(
                    f"{label}: one-sided precision lower bound below policy"
                )

    gate_metric_set("", metrics)
    for split in REQUIRED_SPLITS:
        gate_metric_set(f"{split}:", metrics_by_split[split])
        for claim_name, minimum in MINIMUM_ELIGIBLE_DECISIONS_PER_SPLIT.items():
            actual = metrics_by_split[split][claim_name]["eligible"]
            if actual < minimum:
                violations.append(
                    f"{split}:{claim_name}: eligible decisions {actual} below "
                    f"approved minimum {minimum}"
                )

    cohort_coverage: dict[str, dict[str, float]] = {}
    for claim_name, cohorts in cohort_counts.items():
        cohort_coverage[claim_name] = {}
        for cohort, counts in sorted(cohorts.items()):
            if counts["eligible"]:
                coverage = counts["automatic"] / counts["eligible"]
                cohort_coverage[claim_name][cohort] = coverage
                if coverage < 1.0:
                    violations.append(f"{claim_name}:{cohort}: supported cohort coverage regressed")

    duplicate_rows = {
        row_id: decisions
        for row_id, decisions in sorted(consumed_by.items())
        if len(decisions) > 1
    }
    if len(duplicate_rows) > int(thresholds.get("duplicate_row_uses_max", 0)):
        violations.append("automatic event matches consume one or more source rows twice")

    abstention_rate = (
        abstention_correct / abstention_required if abstention_required else 0.0
    )
    if abstention_rate < float(thresholds.get("required_abstention_rate", 1.0)):
        violations.append("required ambiguous/unsupported abstention rate regressed")

    expected_totals_dict = dict(sorted(expected_totals.items()))
    actual_totals_dict = dict(sorted(actual_totals.items()))
    category_ids = sorted(set(expected_totals_dict) | set(actual_totals_dict))
    total_delta = {
        category_id: actual_totals_dict.get(category_id, 0)
        - expected_totals_dict.get(category_id, 0)
        for category_id in category_ids
        if actual_totals_dict.get(category_id, 0) != expected_totals_dict.get(category_id, 0)
    }
    if total_delta:
        violations.append("predicted expense category totals do not equal golden totals")

    return {
        "ok": not violations,
        "automation_authority": policy.get("automation_authority"),
        "versions": approved,
        "controlled_modes": required_modes,
        "manifest": {
            "version": manifest.get("manifest_version"),
            "corpus_version": manifest.get("corpus_version"),
            "case_count": len(cases),
            "prediction_count": len(predictions),
        },
        "metrics": metrics,
        "metrics_by_split": metrics_by_split,
        "supported_cohort_coverage": cohort_coverage,
        "abstention": {
            "required": abstention_required,
            "correct": abstention_correct,
            "rate": abstention_rate,
        },
        "row_consumption": {
            "duplicate_row_count": len(duplicate_rows),
            "duplicates": duplicate_rows,
        },
        "split_leakage": {
            "family_overlap": overlap,
            "descriptor_overlap": descriptor_overlap,
        },
        "expense_totals_cents": {
            "expected": expected_totals_dict,
            "actual": actual_totals_dict,
            "delta": total_delta,
        },
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tests" / "evals" / "data" / "fn149",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate_bundle(args.bundle_dir)
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
