"""Deterministically generate the privacy-safe FN-149A hard-case corpus.

The fixtures deliberately use invented merchants, opaque row identifiers, and
synthetic dates/amounts. They are contract data for the evaluator, not evidence
that a production matcher has passed the gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

CASE_SCHEMA_VERSION = "fn149-hard-case.v1"
PREDICTION_SCHEMA_VERSION = "fn149-prediction.v1"
CORPUS_VERSION = "fn149-synthetic-corpus.v1"
SCORER_VERSION = "fn149-controlled-reference.v1"
TAXONOMY_VERSION = "fn149-synthetic-taxonomy.v1"
KNOWLEDGE_VERSION = "none"
MANIFEST_VERSION = "fn149-corpus-manifest.v1"
POLICY_VERSION = "fn149-eval-policy.v1"

POSITIVE_COHORTS = (
    "exact",
    "tolerance",
    "split",
    "partial",
    "one_to_many",
    "many_to_one",
    "paired_transfer",
    "refund",
    "pending_final",
    "repeated_amount",
    "tip_fee",
)
SPECIAL_COHORTS = ("duplicate", "no_match", "ambiguous")
EXPENSE_CATEGORIES = ("groceries", "dining", "transport", "supplies")
REQUIRED_COHORT_COUNTS_PER_SPLIT = {
    **{cohort: 60 for cohort in POSITIVE_COHORTS},
    **{cohort: 30 for cohort in SPECIAL_COHORTS},
}
MINIMUM_ELIGIBLE_DECISIONS_PER_SPLIT = {
    "same_event": 690,
    "canonical_merchant": 690,
    "expense_category": 630,
}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_json_bytes(row) for row in rows)


def _row(
    case_id: str,
    suffix: str,
    *,
    kind: str,
    amount_cents: int,
    descriptor: str,
    posted_on: str,
    pending: bool = False,
) -> dict[str, Any]:
    return {
        "row_id": f"synthetic-row:{case_id}:{suffix}",
        "kind": kind,
        "amount_cents": amount_cents,
        "currency": "CAD",
        "posted_on": posted_on,
        "descriptor": descriptor,
        "pending": pending,
        "privacy_tag": "deterministic_synthetic",
    }


def _candidate(case_id: str, suffix: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidate_id": f"synthetic-candidate:{case_id}:{suffix}",
        "source_row_ids": [row["row_id"] for row in rows],
    }


def _case(split: str, cohort: str, index: int) -> dict[str, Any]:
    family_index = index // 3
    variant = index % 3
    case_id = f"{split}-{cohort}-{index:03d}"
    family_id = f"synthetic-family:{split}:{cohort}:{family_index:02d}"
    merchant_id = f"synthetic-merchant:{split}:{cohort}:{family_index:02d}"
    category_id = f"synthetic-category:{EXPENSE_CATEGORIES[family_index % len(EXPENSE_CATEGORIES)]}"
    amount = 1_000 + POSITIVE_COHORTS.index(cohort) * 100 + index * 7 if cohort in POSITIVE_COHORTS else 4_000 + index * 11
    day = 1 + index % 27
    posted_on = f"2026-06-{day:02d}"
    descriptor = f"SYNTH-{split[:3].upper()}-{cohort.upper()}-F{family_index:02d}-T{variant:02d}"

    statement = _row(
        case_id,
        "statement-1",
        kind="statement_line",
        amount_cents=-amount,
        descriptor=descriptor,
        posted_on=posted_on,
    )
    rows = [statement]
    candidates: list[dict[str, Any]] = []
    same_event: dict[str, str] = {}
    merchant: dict[str, str] = {statement["row_id"]: merchant_id}
    category: dict[str, str] = {statement["row_id"]: category_id}
    support = {"same_event": True, "canonical_merchant": True, "expense_category": True}

    receipt = _row(
        case_id,
        "receipt-1",
        kind="receipt",
        amount_cents=-amount,
        descriptor=f"SYNTH-RECEIPT-{split[:3].upper()}-{cohort.upper()}-F{family_index:02d}",
        posted_on=posted_on,
    )

    if cohort == "split":
        receipt["amount_cents"] = -(amount // 2)
        receipt_2 = _row(
            case_id,
            "receipt-2",
            kind="receipt",
            amount_cents=-(amount - amount // 2),
            descriptor=receipt["descriptor"],
            posted_on=posted_on,
        )
        rows.extend([receipt, receipt_2])
        candidates.append(_candidate(case_id, "primary", [statement, receipt, receipt_2]))
    elif cohort == "partial":
        receipt["amount_cents"] = -(amount - 125)
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))
    elif cohort == "one_to_many":
        statement["amount_cents"] = -(amount // 2)
        statement_2 = _row(
            case_id,
            "statement-2",
            kind="statement_line",
            amount_cents=-(amount - amount // 2),
            descriptor=descriptor,
            posted_on=posted_on,
        )
        rows.extend([statement_2, receipt])
        candidates.append(_candidate(case_id, "primary", [statement, statement_2, receipt]))
        merchant[statement_2["row_id"]] = merchant_id
        category[statement_2["row_id"]] = category_id
    elif cohort == "many_to_one":
        receipt["amount_cents"] = -(amount // 2)
        receipt_2 = _row(
            case_id,
            "receipt-2",
            kind="receipt",
            amount_cents=-(amount - amount // 2),
            descriptor=receipt["descriptor"],
            posted_on=posted_on,
        )
        rows.extend([receipt, receipt_2])
        candidates.append(_candidate(case_id, "primary", [statement, receipt, receipt_2]))
    elif cohort == "paired_transfer":
        statement_2 = _row(
            case_id,
            "statement-2",
            kind="statement_line",
            amount_cents=amount,
            descriptor=f"{descriptor}-PAIR",
            posted_on=posted_on,
        )
        rows.append(statement_2)
        candidates.append(_candidate(case_id, "primary", [statement, statement_2]))
        merchant = {statement["row_id"]: "abstain", statement_2["row_id"]: "abstain"}
        category = {statement["row_id"]: "abstain", statement_2["row_id"]: "abstain"}
        support["canonical_merchant"] = False
        support["expense_category"] = False
    elif cohort == "refund":
        statement["amount_cents"] = amount
        receipt["amount_cents"] = amount
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))
        category = {statement["row_id"]: "abstain"}
        support["expense_category"] = False
    elif cohort == "pending_final":
        pending = _row(
            case_id,
            "statement-pending",
            kind="statement_line",
            amount_cents=-amount,
            descriptor=f"{descriptor}-PENDING",
            posted_on=posted_on,
            pending=True,
        )
        rows.extend([pending, receipt])
        candidates.append(_candidate(case_id, "primary", [statement, pending, receipt]))
    elif cohort in {"repeated_amount", "duplicate"}:
        other = _row(
            case_id,
            "receipt-2",
            kind="receipt",
            amount_cents=-amount,
            descriptor=f"{receipt['descriptor']}-OTHER",
            posted_on=posted_on,
        )
        rows.extend([receipt, other])
        primary = _candidate(case_id, "primary", [statement, receipt])
        alternate_rows = [statement, other] if cohort == "repeated_amount" else [statement, receipt]
        alternate = _candidate(case_id, "alternate", alternate_rows)
        candidates.extend([primary, alternate])
        same_event[alternate["candidate_id"]] = "abstain"
    elif cohort == "tip_fee":
        receipt["amount_cents"] = -(amount - 175)
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))
    elif cohort == "tolerance":
        receipt["amount_cents"] = -(amount - 1)
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))
    elif cohort in {"no_match", "ambiguous"}:
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))
        if cohort == "ambiguous":
            other = _row(
                case_id,
                "receipt-2",
                kind="receipt",
                amount_cents=-amount,
                descriptor=f"{receipt['descriptor']}-EQUAL",
                posted_on=posted_on,
            )
            rows.append(other)
            candidates.append(_candidate(case_id, "alternate", [statement, other]))
        same_event = {candidate["candidate_id"]: "abstain" for candidate in candidates}
        merchant = {statement["row_id"]: "abstain"}
        category = {statement["row_id"]: "abstain"}
        support = {"same_event": False, "canonical_merchant": False, "expense_category": False}
    else:
        rows.append(receipt)
        candidates.append(_candidate(case_id, "primary", [statement, receipt]))

    if cohort not in {"no_match", "ambiguous"}:
        same_event[candidates[0]["candidate_id"]] = "match"

    expense_totals: dict[str, int] = {}
    rows_by_id = {row["row_id"]: row for row in rows}
    for subject_id, expected in category.items():
        if expected == "abstain":
            continue
        amount_cents = int(rows_by_id[subject_id]["amount_cents"])
        if amount_cents < 0:
            expense_totals[expected] = expense_totals.get(expected, 0) + amount_cents

    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "case_id": case_id,
        "split": split,
        "family_id": family_id,
        "cohort": cohort,
        "privacy": {
            "source": "deterministic_generator",
            "contains_personal_data": False,
        },
        "automation_support": support,
        "rows": rows,
        "same_event_candidates": candidates,
        "gold": {
            "same_event": dict(sorted(same_event.items())),
            "canonical_merchant": dict(sorted(merchant.items())),
            "expense_category": dict(sorted(category.items())),
            "expense_totals_cents": dict(sorted(expense_totals.items())),
        },
    }


def generate_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for split in ("developer", "sealed"):
        for cohort in POSITIVE_COHORTS:
            cases.extend(
                _case(split, cohort, index)
                for index in range(REQUIRED_COHORT_COUNTS_PER_SPLIT[cohort])
            )
        for cohort in SPECIAL_COHORTS:
            cases.extend(
                _case(split, cohort, index)
                for index in range(REQUIRED_COHORT_COUNTS_PER_SPLIT[cohort])
            )
    return cases


def prediction_for(case: dict[str, Any]) -> dict[str, Any]:
    gold = case["gold"]
    return {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "scorer_version": SCORER_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "merchant_knowledge_version": KNOWLEDGE_VERSION,
        "case_id": case["case_id"],
        "split": case["split"],
        "mode": {
            "local_model": "disabled_controlled_placeholder",
            "web_search": "disabled_controlled_placeholder",
        },
        "same_event": [
            {
                "candidate_id": candidate_id,
                "decision": "automatic_match" if expected == "match" else "abstain",
            }
            for candidate_id, expected in gold["same_event"].items()
        ],
        "canonical_merchant": [
            {
                "subject_row_id": subject_id,
                "decision": "automatic_assign" if expected != "abstain" else "abstain",
                **({"value": expected} if expected != "abstain" else {}),
            }
            for subject_id, expected in gold["canonical_merchant"].items()
        ],
        "expense_category": [
            {
                "subject_row_id": subject_id,
                "decision": "automatic_assign" if expected != "abstain" else "abstain",
                **({"value": expected} if expected != "abstain" else {}),
            }
            for subject_id, expected in gold["expense_category"].items()
        ],
    }


def policy() -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "automation_authority": "disabled_foundation_only",
        "approved_versions": {
            "case_schema_version": CASE_SCHEMA_VERSION,
            "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
            "corpus_version": CORPUS_VERSION,
            "scorer_version": SCORER_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "merchant_knowledge_version": KNOWLEDGE_VERSION,
        },
        "required_splits": ["developer", "sealed"],
        "required_cohort_counts_per_split": {
            split: dict(sorted(REQUIRED_COHORT_COUNTS_PER_SPLIT.items()))
            for split in ("developer", "sealed")
        },
        "minimum_eligible_decisions_per_split": {
            split: dict(MINIMUM_ELIGIBLE_DECISIONS_PER_SPLIT)
            for split in ("developer", "sealed")
        },
        "required_modes": {
            "local_model": "disabled_controlled_placeholder",
            "web_search": "disabled_controlled_placeholder",
        },
        "thresholds": {
            "precision_min": 0.995,
            "precision_lower_bound_min": 0.995,
            "confidence_level": 0.95,
            "false_assignments_max": 0,
            "duplicate_row_uses_max": 0,
            "required_abstention_rate": 1.0,
            "expense_total_delta_cents_max": 0,
        },
    }


def render_bundle() -> dict[str, bytes]:
    cases = generate_cases()
    rendered: dict[str, bytes] = {}
    for split in ("developer", "sealed"):
        split_cases = [case for case in cases if case["split"] == split]
        rendered[f"{split}.v1.jsonl"] = _jsonl_bytes(split_cases)
        rendered[f"{split}.predictions.v1.jsonl"] = _jsonl_bytes(
            [prediction_for(case) for case in split_cases]
        )
    rendered["policy.v1.json"] = _json_bytes(policy())

    files = []
    for name, payload in sorted(rendered.items()):
        kind = "policy" if name.startswith("policy") else ("predictions" if ".predictions." in name else "corpus")
        split = None if kind == "policy" else name.split(".", 1)[0]
        files.append(
            {
                "path": name,
                "kind": kind,
                "split": split,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "line_count": payload.count(b"\n"),
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": CASE_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "generator": "app.evals.fn149_corpus:v1",
        "privacy": {
            "source": "deterministic_synthetic_only",
            "production_data_forbidden": True,
            "descriptor_prefix": "SYNTH-",
        },
        "files": files,
    }
    rendered["manifest.v1.json"] = _json_bytes(manifest)
    return rendered


def write_bundle(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in render_bundle().items():
        (output_dir / name).write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tests" / "evals" / "data" / "fn149",
    )
    args = parser.parse_args()
    write_bundle(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
