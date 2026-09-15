from __future__ import annotations

import json
from pathlib import Path

from app.evals import (
    chat_groundedness,
    classification_metrics,
    expense_uncategorized_metrics,
    field_accuracy,
    receipt_arithmetic_ok,
    reconciliation_metrics,
)

DATA_DIR = Path(__file__).parent / "data"


def _load(name: str):
    return json.loads((DATA_DIR / name).read_text())


def test_extraction_field_accuracy_and_arithmetic_invariants():
    cases = _load("extraction_cases.json")
    fields = ["merchant", "purchased_on", "currency", "total_cents", "card_last4"]

    perfect = cases[0]
    metrics = field_accuracy(perfect["predicted"], perfect["expected"], fields)
    assert metrics["accuracy"] == 1.0
    assert receipt_arithmetic_ok(perfect["predicted"]) is True

    arithmetic_fail = cases[1]
    assert receipt_arithmetic_ok(arithmetic_fail["predicted"]) is False
    # The field-level eval still pinpoints that the wrong value is total_cents.
    by_field = field_accuracy(arithmetic_fail["predicted"], arithmetic_fail["expected"], fields)
    assert by_field["fields"]["total_cents"] is False


def test_classification_tracks_top1_and_uncategorized_rate():
    metrics = classification_metrics(_load("classification_cases.json"))
    assert metrics == {
        "total": 3,
        "correct": 2,
        "top1_accuracy": 2 / 3,
        "uncategorized_count": 1,
        "uncategorized_rate": 1 / 3,
    }


def test_reconciliation_tracks_precision_recall_and_double_count_invariant():
    metrics = reconciliation_metrics(_load("reconciliation_cases.json"))
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["no_double_count"] is True

    duplicate = [
        {"statement_line_id": 1, "expected_transaction_id": 10, "predicted_transaction_id": 10},
        {"statement_line_id": 2, "expected_transaction_id": 11, "predicted_transaction_id": 10},
    ]
    metrics = reconciliation_metrics(duplicate)
    assert metrics["duplicate_transaction_ids"] == [10]
    assert metrics["no_double_count"] is False
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 1


def test_chat_groundedness_requires_citations_for_verifiable_claims():
    metrics = chat_groundedness(_load("chat_cases.json"))
    assert metrics == {
        "total": 2,
        "grounded": 2,
        "grounded_rate": 1.0,
        "ungrounded_claims": [],
    }

    metrics = chat_groundedness([
        {"claim_id": "cashflow", "text": "Cashflow improved by $50."},
    ])
    assert metrics["grounded_rate"] == 0.0
    assert metrics["ungrounded_claims"] == ["cashflow"]


def test_uncategorized_health_metric_reads_reporting_view(app_env):
    from app.db import engine, repo_ledger

    with engine.read_conn(app_env) as conn:
        before = expense_uncategorized_metrics(conn)

    with engine.write_tx(app_env) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)
        txn_id = conn.execute(
            """
                INSERT INTO transactions(account_id, posted_on, description, counterparty,
                                         amount_cents, source, external_id, source_confidence,
                                         flow_kind)
                VALUES (1, '2026-04-01', 'unknown shop', 'Mystery', -1234,
                        'eval', 'eval:uncategorized', 1.0, 'purchase')
            RETURNING id
            """
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
            (txn_id, uncategorized_id, -1234),
        )

    with engine.read_conn(app_env) as conn:
        after = expense_uncategorized_metrics(conn)

    assert after["total"] == before["total"] + 1
    assert after["uncategorized_count"] == before["uncategorized_count"] + 1
    assert after["uncategorized_rate"] == after["uncategorized_count"] / after["total"]
