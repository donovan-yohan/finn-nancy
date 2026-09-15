"""Small, deterministic metrics for verifiability-first evals.

These helpers intentionally accept dict-like rows as well as pydantic models so
eval fixtures can stay simple JSON.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import sqlite3
from typing import Any


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def receipt_arithmetic_ok(receipt: Any, *, tolerance_cents: int = 0) -> bool:
    """Return whether subtotal + tax + tip equals total within tolerance."""
    subtotal = int(_get(receipt, "subtotal_cents", 0) or 0)
    tax = int(_get(receipt, "tax_cents", 0) or 0)
    tip = int(_get(receipt, "tip_cents", 0) or 0)
    total = int(_get(receipt, "total_cents", 0) or 0)
    return abs((subtotal + tax + tip) - total) <= tolerance_cents


def field_accuracy(predicted: Any, expected: Any, fields: Iterable[str]) -> dict[str, Any]:
    """Exact-match field accuracy for extraction evals."""
    checks = {field: _get(predicted, field) == _get(expected, field) for field in fields}
    correct = sum(1 for ok in checks.values() if ok)
    total = len(checks)
    return {
        "correct": correct,
        "total": total,
        "accuracy": _rate(correct, total),
        "fields": checks,
    }


def classification_metrics(
    cases: Iterable[Mapping[str, Any]],
    *,
    uncategorized_label: str = "Uncategorized",
) -> dict[str, Any]:
    """Top-1 accuracy and Uncategorized rate for category predictions."""
    rows = list(cases)
    total = len(rows)
    correct = sum(
        1 for row in rows
        if row.get("predicted_category") == row.get("expected_category")
    )
    uncategorized = sum(
        1 for row in rows
        if row.get("predicted_category") == uncategorized_label
    )
    return {
        "total": total,
        "correct": correct,
        "top1_accuracy": _rate(correct, total),
        "uncategorized_count": uncategorized,
        "uncategorized_rate": _rate(uncategorized, total),
    }


def expense_uncategorized_metrics(
    conn: sqlite3.Connection,
    *,
    uncategorized_label: str = "Uncategorized",
) -> dict[str, Any]:
    """Measure Uncategorized expense split rate from the reporting view.

    This is the first real-data product-health hook for issue #1/#8: run it
    against a copied finance DB to quantify the categorization backlog.
    """
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN category_name = ? THEN 1 ELSE 0 END), 0) AS uncategorized
        FROM v_split_detail
        WHERE category_kind = 'expense'
        """,
        (uncategorized_label,),
    ).fetchone()
    total = int(row["total"] or 0)
    uncategorized = int(row["uncategorized"] or 0)
    return {
        "total": total,
        "uncategorized_count": uncategorized,
        "uncategorized_rate": _rate(uncategorized, total),
    }


def reconciliation_metrics(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Precision/recall for 1:1 reconciliation plus no-double-count invariant."""
    rows = list(cases)
    true_positive = false_positive = false_negative = 0
    predicted_ids: list[int] = []

    for row in rows:
        predicted = row.get("predicted_transaction_id")
        expected = row.get("expected_transaction_id")
        if predicted is not None:
            predicted_ids.append(int(predicted))
        if predicted == expected and expected is not None:
            true_positive += 1
        elif predicted is not None and expected is None:
            false_positive += 1
        elif predicted is None and expected is not None:
            false_negative += 1
        elif predicted != expected:
            false_positive += 1
            false_negative += 1

    counts = Counter(predicted_ids)
    duplicate_ids = sorted(txn_id for txn_id, count in counts.items() if count > 1)
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return {
        "total": len(rows),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": _rate(true_positive, precision_denominator),
        "recall": _rate(true_positive, recall_denominator),
        "duplicate_transaction_ids": duplicate_ids,
        "no_double_count": not duplicate_ids,
    }


def chat_groundedness(claims: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Check that each answer claim has at least one citation/row reference."""
    rows = list(claims)
    ungrounded: list[str] = []
    grounded = 0
    for idx, row in enumerate(rows):
        if row.get("requires_citation", True) is False:
            grounded += 1
            continue
        citations = row.get("citations") or row.get("citation_ids") or []
        if citations:
            grounded += 1
        else:
            ungrounded.append(str(row.get("claim_id", idx)))
    total = len(rows)
    return {
        "total": total,
        "grounded": grounded,
        "grounded_rate": _rate(grounded, total),
        "ungrounded_claims": ungrounded,
    }
