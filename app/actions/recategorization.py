"""Apply/revert recategorization proposals."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from ..db import (
    repo_close,
    repo_ledger,
    repo_merchant_knowledge,
)
from ..db.repo_merchant_knowledge import Evidence
from ..accounting.contract import currency_review_reason
from ..config import get_settings
from .category_state import category_state, state_digest


def _truthy(value: object) -> bool:
    return value in (True, 1, "1", "true", "True")


def _int_field(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None


def _target_category(conn: sqlite3.Connection, payload: Mapping[str, object]) -> sqlite3.Row:
    category_id = payload.get("to_category_id")
    if category_id not in (None, ""):
        row = conn.execute(
            "SELECT * FROM categories WHERE id=?",
            (_int_field(payload, "to_category_id"),),
        ).fetchone()
    else:
        name = str(payload.get("to_category_name") or "").strip()
        if not name:
            raise ValueError("to_category_id is required")
        row = repo_ledger.find_category_by_name(conn, name)
    if row is None:
        raise ValueError("target category not found")
    return row


def _single_split(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row:
    splits = conn.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
        (transaction_id,),
    ).fetchall()
    if len(splits) != 1:
        raise ValueError("transaction must have exactly one split")
    return splits[0]


def _selected_split(conn: sqlite3.Connection, payload: Mapping[str, object]) -> sqlite3.Row:
    transaction_id = _int_field(payload, "transaction_id")
    if payload.get("split_id") in (None, ""):
        return _single_split(conn, transaction_id)
    split = conn.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id=? AND id=?",
        (transaction_id, _int_field(payload, "split_id")),
    ).fetchone()
    if split is None:
        raise ValueError("split does not belong to transaction")
    return split


def _optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None


def _accept_category_decision(
    conn: sqlite3.Connection,
    *,
    payload: Mapping[str, object],
    transaction_id: int,
    split_id: int,
    category_id: int,
) -> int | None:
    proposed_action_id = _optional_int(payload, "_proposed_action_id")
    if proposed_action_id is None:
        return None
    actor = str(payload.get("_actor") or "operator:actions").strip()
    txn = conn.execute(
        """
        SELECT
          account_id,
          flow_kind,
          COALESCE(NULLIF(counterparty, ''), description) AS descriptor
        FROM transactions
        WHERE id=?
        """,
        (transaction_id,),
    ).fetchone()
    if txn is None:
        raise ValueError("recategorization transaction not found")
    category = conn.execute(
        "SELECT kind, name FROM categories WHERE id=?",
        (category_id,),
    ).fetchone()
    if (
        category is None
        or category["kind"] != "expense"
        or category["name"] == "Uncategorized"
        or txn["flow_kind"]
        not in {"purchase", "fee", "refund", "reimbursement", "reversal"}
    ):
        # The action may still be a legitimate non-expense category edit, but it
        # is not evidence for the reusable expense-category resolver.
        return None
    descriptor = str(txn["descriptor"] or "").strip()
    if not descriptor:
        raise ValueError("recategorization requires a merchant descriptor")
    scope = repo_merchant_knowledge.scope_for_transaction(
        conn,
        transaction_id,
    )
    evidence = Evidence(
        transaction_id=transaction_id,
        transaction_split_id=split_id,
        proposed_action_id=proposed_action_id,
    )
    proposed_claim_id = _optional_int(
        payload,
        "_merchant_resolution_claim_id",
    )
    prior_rows = conn.execute(
        """
        SELECT claim_id, category_id
        FROM v_active_merchant_resolution_claims
        WHERE claim_kind='expense_category'
          AND transaction_split_id=?
        ORDER BY claim_id
        """,
        (split_id,),
    ).fetchall()
    conflicting_prior_ids = [
        int(row["claim_id"])
        for row in prior_rows
        if int(row["category_id"]) != category_id
    ]
    accepted_claim_id: int
    accepted_linked_proposal = False
    if proposed_claim_id is not None:
        current = repo_merchant_knowledge.current_claim(conn, proposed_claim_id)
        if (
            current is not None
            and current["event_kind"] in {"proposed", "legacy_imported"}
            and int(current["category_id"] or 0) == category_id
        ):
            repo_merchant_knowledge.accept_proposal(
                conn,
                claim_id=proposed_claim_id,
                operation_key=f"action:{proposed_action_id}:accept-category",
                actor=actor,
                reason="operator approved category proposal",
                evidence=evidence,
            )
            accepted_claim_id = proposed_claim_id
            accepted_linked_proposal = True
        else:
            if current is not None and current["event_kind"] in {
                "proposed",
                "legacy_imported",
            }:
                repo_merchant_knowledge.reject_claim(
                    conn,
                    claim_id=proposed_claim_id,
                    operation_key=(
                        f"action:{proposed_action_id}:reject-edited-category"
                    ),
                    actor=actor,
                    reason="operator selected a different category",
                )
            proposed_claim_id = None

    if not accepted_linked_proposal and conflicting_prior_ids:
        accepted_claim_id = repo_merchant_knowledge.correct_category(
            conn,
            prior_claim_id=conflicting_prior_ids[0],
            descriptor=descriptor,
            category_id=category_id,
            scope=scope,
            operation_key=f"action:{proposed_action_id}:correct-category",
            actor=actor,
            reason="operator corrected the accepted expense category",
            evidence=evidence,
        )
    elif not accepted_linked_proposal:
        accepted_claim_id = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=category_id,
            scope=scope,
            operation_key=f"action:{proposed_action_id}:confirm-category",
            actor=actor,
            reason="operator confirmed expense category",
            evidence=evidence,
            provenance_ref=f"proposed-action:{proposed_action_id}",
        )

    for prior in prior_rows:
        prior_claim_id = int(prior["claim_id"])
        if prior_claim_id == accepted_claim_id:
            continue
        if int(prior["category_id"]) == category_id:
            continue
        current_prior = repo_merchant_knowledge.current_claim(
            conn,
            prior_claim_id,
        )
        if current_prior is None or current_prior["event_kind"] not in {
            "accepted",
            "corrected",
        }:
            continue
        repo_merchant_knowledge.retire_claim(
            conn,
            claim_id=prior_claim_id,
            operation_key=(
                f"action:{proposed_action_id}:retire-category:"
                f"{prior_claim_id}"
            ),
            actor=actor,
            reason="prior category claim retired by recategorization",
        )
    return accepted_claim_id


class RecategorizationHandler:
    """Move a single-split transaction to a different category.

    Category is purpose, never movement direction. Recategorization therefore
    preserves transaction/split amounts for every flow kind.
    """

    def normalize(self, payload: Mapping[str, object]) -> dict:
        normalized = dict(payload)
        for key in ("transaction_id", "to_category_id", "split_id"):
            if key not in normalized or normalized[key] in (None, ""):
                continue
            try:
                normalized[key] = int(normalized[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer") from None
        if "to_category_name" in normalized:
            normalized["to_category_name"] = str(normalized.get("to_category_name") or "").strip()
        return normalized

    def validate(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> None:
        transaction_id = _int_field(payload, "transaction_id")
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        if txn is None:
            raise ValueError("transaction not found")
        if txn["source"] == "opening":
            raise ValueError("opening balance amount/category cannot be edited")
        target_category = _target_category(conn, payload)
        if (
            txn["flow_kind"] in {"purchase", "fee"}
            and target_category["kind"] != "expense"
        ):
            raise ValueError(
                "purchase and fee transactions require an expense category"
            )
        _selected_split(conn, payload)
        expected = payload.get("expected_state_digest")
        if expected is not None:
            state = category_state(conn, transaction_id)
            if state_digest(state) != expected:
                raise ValueError("transaction state is stale; inspect again")
        if payload.get("machine_category_proposal"):
            if expected is None:
                raise ValueError("expected_state_digest is required")
            state = category_state(conn, transaction_id)
            status = conn.execute(
                "SELECT semantic_status FROM v_transaction_flow_status WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if any(
                row["accepted_observation_id"] != row["current_observation_id"]
                or row["accepted_binding_id"] != row["current_binding_id"]
                or row["pending"] or row["kind"] == "removed"
                for row in state["bank_source_state"]
            ):
                raise ValueError("bank source evidence requires review")
            if txn["flow_kind"] not in {"purchase", "fee", "refund", "reimbursement", "reversal"} or status[0] != "complete":
                raise ValueError("expense flow must be resolved before proposing a category")
            if target_category["kind"] != "expense":
                raise ValueError("target must be an existing expense category")
            home_currency = get_settings().home_currency
            if currency_review_reason(state["account"]["currency"], home_currency):
                raise ValueError("expense currency requires review")
            for extraction in state["extractions"]:
                # Statement extractions have their own reviewed field evidence;
                # only receipt JSON is the receipt-currency authority here.
                if extraction["doc_kind"] != "receipt":
                    continue
                try:
                    receipt = json.loads(extraction["extracted_json"])
                except (ValueError, TypeError):
                    raise ValueError("receipt currency requires review") from None
                currency = receipt.get("currency") if isinstance(receipt, dict) else None
                if not isinstance(currency, str) or currency_review_reason(currency, home_currency):
                    raise ValueError("receipt currency requires review")
            if sum(s["amount_cents"] for s in state["splits"]) != txn["amount_cents"]:
                raise ValueError("split amounts must sum to transaction amount")
            if any(r["is_pending"] or r["review_disposition"] != "active" for r in state["statement_lines"]):
                raise ValueError("pending or inactive source evidence requires review")
            if any(currency_review_reason(r["currency"], home_currency) for r in state["statement_lines"]):
                raise ValueError("source currency requires review")
            if any(r["review_state"] not in {"approved", "approved_with_override"} for r in state["statement_reviews"]):
                raise ValueError("source statement requires review")
            if any(r["review_status"] in {"pending", "rejected"} for r in state["extractions"]):
                raise ValueError("pending source extraction requires review")
            if any(r["status"] not in {"processed", "matched"} for r in state["documents"]):
                raise ValueError("source document requires review")

    def apply(self, conn: sqlite3.Connection, payload: Mapping[str, object]) -> dict:
        self.validate(conn, payload)
        transaction_id = _int_field(payload, "transaction_id")
        # Soft lock: a recategorization landing on a closed-month txn is blocked unless
        # the proposal carries override=True. MonthLockedError subclasses ValueError, so
        # the approval-queue route's existing ValueError handling surfaces it as a queue
        # failure rather than crashing the worker.
        override = _truthy(payload.get("override"))
        locked_months = repo_close.guard_transaction_write(
            conn, transaction_id, override=override
        )
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        split = _selected_split(conn, payload)
        target_category = _target_category(conn, payload)
        target_transaction_amount_cents = int(txn["amount_cents"])
        target_split_amount_cents = int(split["amount_cents"])
        target_category_id = int(target_category["id"])

        detail = {
            "transaction_id": transaction_id,
            "split_id": int(split["id"]),
            "from_category_id": int(split["category_id"]),
            "to_category_id": target_category_id,
            "from_transaction_amount_cents": int(txn["amount_cents"]),
            "to_transaction_amount_cents": target_transaction_amount_cents,
            "from_split_amount_cents": int(split["amount_cents"]),
            "to_split_amount_cents": target_split_amount_cents,
        }
        noop = (
            int(split["category_id"]) == target_category_id
            and int(split["amount_cents"]) == target_split_amount_cents
            and int(txn["amount_cents"]) == target_transaction_amount_cents
        )
        revert: dict[str, object] = {
            "transaction_id": transaction_id,
            "split_id": int(split["id"]),
            "category_id": int(split["category_id"]),
            "transaction_amount_cents": int(txn["amount_cents"]),
            "split_amount_cents": int(split["amount_cents"]),
            "expected": {
                "category_id": target_category_id,
                "transaction_amount_cents": target_transaction_amount_cents,
                "split_amount_cents": target_split_amount_cents,
            },
        }
        if not noop:
            conn.execute(
                "UPDATE transaction_splits SET category_id=? WHERE id=?",
                (target_category_id, split["id"]),
            )
        knowledge_claim_id = _accept_category_decision(
            conn,
            payload=payload,
            transaction_id=transaction_id,
            split_id=int(split["id"]),
            category_id=target_category_id,
        )
        if knowledge_claim_id is not None:
            revert["knowledge_claim_id"] = knowledge_claim_id
            detail["knowledge_claim_id"] = knowledge_claim_id
        if payload.get("split_id") not in (None, ""):
            revert["expected_state_digest"] = state_digest(category_state(conn, transaction_id))
        for month in locked_months:  # non-empty only on an override apply
            repo_close.record_audit(
                conn, month=month, entity="transaction", entity_id=transaction_id,
                field="category_id", old_value=int(split["category_id"]),
                new_value=target_category_id, reason="override recategorization",
            )
        return {
            "revert": revert if (not noop or knowledge_claim_id is not None) else None,
            "detail": detail,
            "noop": noop,
        }

    def revert(self, conn: sqlite3.Connection, revert_payload: Mapping[str, object]) -> dict:
        transaction_id = _int_field(revert_payload, "transaction_id")
        # Soft lock: reverting a recategorization back into a closed month is blocked
        # unless the revert carries override=True (mirrors apply()). MonthLockedError
        # subclasses ValueError, so the revert route's existing ValueError handling
        # surfaces it rather than crashing.
        override = _truthy(revert_payload.get("override"))
        locked_months = repo_close.guard_transaction_write(
            conn, transaction_id, override=override
        )
        split_id = _int_field(revert_payload, "split_id")
        category_id = _int_field(revert_payload, "category_id")
        transaction_amount_cents = _int_field(revert_payload, "transaction_amount_cents")
        split_amount_cents = _int_field(revert_payload, "split_amount_cents")
        expected = revert_payload.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError("invalid recategorization revert payload")
        expected_category_id = _int_field(expected, "category_id")
        expected_transaction_amount_cents = _int_field(expected, "transaction_amount_cents")
        expected_split_amount_cents = _int_field(expected, "split_amount_cents")

        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        category = conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone()
        if txn is None or category is None:
            raise ValueError("recategorization revert target not found")
        splits = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
            (transaction_id,),
        ).fetchall()
        exact_digest = revert_payload.get("expected_state_digest")
        if exact_digest is not None:
            if state_digest(category_state(conn, transaction_id)) != exact_digest:
                raise ValueError("transaction state has changed; revert refused")
            matching = [s for s in splits if int(s["id"]) == split_id]
        else:
            matching = splits if len(splits) == 1 and int(splits[0]["id"]) == split_id else []
        if len(matching) != 1:
            raise ValueError(
                "transaction split shape has changed since this action was applied; revert refused"
            )
        split = matching[0]
        if (
            int(txn["amount_cents"]) != expected_transaction_amount_cents
            or int(split["category_id"]) != expected_category_id
            or int(split["amount_cents"]) != expected_split_amount_cents
        ):
            raise ValueError("transaction has changed since this action was applied; revert refused")

        if exact_digest is not None:
            conn.execute("UPDATE transaction_splits SET category_id=? WHERE id=?", (category_id, split_id))
        else:
            conn.execute(
                "UPDATE transactions SET amount_cents=? WHERE id=?",
                (transaction_amount_cents, transaction_id),
            )
            conn.execute(
                "UPDATE transaction_splits SET category_id=?, amount_cents=? WHERE id=?",
                (category_id, split_amount_cents, split_id),
            )
        knowledge_claim_id = _optional_int(
            revert_payload,
            "knowledge_claim_id",
        )
        if knowledge_claim_id is not None:
            repo_merchant_knowledge.undo_claim(
                conn,
                claim_id=knowledge_claim_id,
                operation_key=f"action:revert:{knowledge_claim_id}",
                actor="operator:actions",
                reason="operator reverted category approval",
            )
        for month in locked_months:  # non-empty only on an override revert
            repo_close.record_audit(
                conn, month=month, entity="transaction", entity_id=transaction_id,
                field="category_id", old_value=expected_category_id,
                new_value=category_id, reason="override revert recategorization",
            )
        return {
            "transaction_id": transaction_id,
            "split_id": split_id,
            "restored_category_id": category_id,
            "restored_transaction_amount_cents": transaction_amount_cents,
            "restored_split_amount_cents": split_amount_cents,
            "knowledge_claim_id": knowledge_claim_id,
        }
