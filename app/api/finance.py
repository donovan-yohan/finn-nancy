"""Shared approval-only finance services; never accepts actor or arbitrary SQL."""
from __future__ import annotations

import json
import re
import base64
from datetime import date

from ..actions.category_state import category_state, state_digest
from ..config import get_settings
from ..db import engine, repo_actions, repo_close
from ..accounting.contract import FlowKind
from .finance_schemas import SQLITE_MAX_ID

ACTOR = "agent:namako"


class OperationConflict(ValueError):
    """A stable key was reused with different content or state became stale."""


def _validate_id(value: object) -> None:
    if type(value) is not int or not 1 <= value <= SQLITE_MAX_ID:
        raise ValueError(f"IDs must be integers between 1 and {SQLITE_MAX_ID}")


def categories() -> dict:
    with engine.read_conn(get_settings().db_path) as conn:
        return {"rows": [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY kind,name,id")]}


def search_transactions(*, q: str | None = None, account_id: int | None = None,
                        category_id: int | None = None, date_from: str | None = None,
                        date_to: str | None = None, flow_kind: str | None = None,
                        review: str | None = None, cursor: str | None = None, limit: int = 50) -> dict:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if q is not None and (not isinstance(q, str) or len(q) > 200):
        raise ValueError("q must be at most 200 characters")
    for value in (account_id, category_id):
        if value is not None:
            _validate_id(value)
    for value in (date_from, date_to):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("dates must be valid ISO dates") from None
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must not follow date_to")
    if flow_kind is not None:
        FlowKind(flow_kind)
    if review not in (None, "needs_review", "resolved"):
        raise ValueError("review must be needs_review or resolved")
    filters = dict(q=q, account_id=account_id, category_id=category_id, date_from=date_from,
                   date_to=date_to, flow_kind=flow_kind, review=review)
    fingerprint = state_digest(filters)
    after_id = None
    if cursor is not None:
        try:
            if len(cursor) > 500:
                raise ValueError()
            decoded = json.loads(base64.urlsafe_b64decode(cursor).decode())
            if decoded["filters"] != fingerprint:
                raise ValueError()
            _validate_id(decoded["after_id"])
            after_id = decoded["after_id"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("cursor is invalid for these filters") from None
    where, params = [], []
    if q:
        where.append("(instr(lower(t.description),lower(?))>0 OR instr(lower(t.counterparty),lower(?))>0)")
        params.extend([q, q])
    for column, value, op in (("t.account_id", account_id, "="), ("t.posted_on", date_from, ">="),
                              ("t.posted_on", date_to, "<="), ("t.flow_kind", flow_kind, "="),
                              ("t.id", after_id, "<")):
        if value is not None:
            where.append(f"{column}{op}?")
            params.append(value)
    if category_id is not None:
        where.append("EXISTS(SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id AND s.category_id=?)")
        params.append(category_id)
    needs_review = """(f.semantic_status<>'complete' OR EXISTS(
        SELECT 1 FROM v_expense_resolution_status r WHERE r.transaction_id=t.id AND r.resolution_status='unresolved'))"""
    if review is not None:
        where.append(needs_review if review == "needs_review" else f"NOT {needs_review}")
    sql = """SELECT t.*, a.currency, f.semantic_status, f.semantic_reason, f.report_eligible
             FROM transactions t JOIN accounts a ON a.id=t.account_id
             JOIN v_transaction_flow_status f ON f.transaction_id=t.id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.id DESC LIMIT ?"
    with engine.read_conn(get_settings().db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, [*params, limit + 1])]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"filters": fingerprint, "after_id": rows[-1]["id"]}).encode()).decode()
    return {"rows": rows, "next_cursor": next_cursor}


def transaction_detail(transaction_id: int) -> dict:
    _validate_id(transaction_id)
    with engine.read_conn(get_settings().db_path) as conn:
        conn.execute("BEGIN")
        state = category_state(conn, transaction_id)
        flow = dict(conn.execute(
            "SELECT * FROM v_transaction_flow_status WHERE transaction_id=?", (transaction_id,)
        ).fetchone())
        resolution = [dict(r) for r in conn.execute(
            "SELECT * FROM v_expense_resolution_status WHERE transaction_id=? ORDER BY transaction_split_id",
            (transaction_id,),
        )]
        return {**state["transaction"], "currency": state["account"]["currency"],
                "splits": state["splits"], "evidence": {
                    k: v for k, v in state.items() if k not in {"transaction", "account", "splits"}
                }, "flow_resolution": flow, "category_resolution": resolution,
                "state_digest": state_digest(state)}


def proposal_detail(proposal_id: int) -> dict:
    _validate_id(proposal_id)
    with engine.read_conn(get_settings().db_path) as conn:
        conn.execute("BEGIN")
        proposal = repo_actions.get_proposal(conn, proposal_id)
        if proposal is None:
            raise LookupError("proposal not found")
        receipt = conn.execute(
            "SELECT operation_key,actor,created_at FROM category_operations WHERE proposed_action_id=?", (proposal_id,)
        ).fetchone()
        return {**proposal, **(dict(receipt) if receipt else {}), "audit": repo_actions.audit_trail(conn, proposal_id)}


def propose_category(*, operation_key: str, transaction_id: int, split_id: int,
                     to_category_id: int, expected_state_digest: str, rationale: str,
                     confidence: float | None = None, evidence_refs: list[str] | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,119}", operation_key):
        raise ValueError("operation_key must be a bounded identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_state_digest):
        raise ValueError("expected_state_digest must be a SHA-256 digest")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
        raise ValueError("rationale must contain 1 to 2000 characters")
    for value in (transaction_id, split_id, to_category_id):
        _validate_id(value)
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    refs = evidence_refs or []
    if len(refs) > 20 or any(not isinstance(r, str) or not re.fullmatch(r"(?:document|statement_line|transaction):[1-9][0-9]{0,17}", r) for r in refs):
        raise ValueError("evidence_refs must be validated canonical identifiers")
    request = dict(operation_key=operation_key, transaction_id=transaction_id, split_id=split_id,
                   to_category_id=to_category_id, expected_state_digest=expected_state_digest,
                   rationale=rationale, confidence=confidence, evidence_refs=refs)
    request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
    settings = get_settings()
    if settings.read_only:
        raise ValueError("finance proposals are disabled in read-only mode")
    with engine.write_tx(settings.db_path) as conn:
        prior = conn.execute("SELECT * FROM category_operations WHERE operation_key=?", (operation_key,)).fetchone()
        if prior:
            if prior["request_json"] != request_json:
                raise OperationConflict("operation_key already used for different content")
            proposal_id = int(prior["proposed_action_id"])
        else:
            state = category_state(conn, transaction_id)
            if state_digest(state) != expected_state_digest:
                raise OperationConflict("transaction state is stale; inspect again")
            repo_close.guard_transaction_write(conn, transaction_id)
            for ref in refs:
                kind, raw_id = ref.split(":")
                valid_ids = {
                    "document": {r["id"] for r in state["documents"]},
                    "statement_line": {r["id"] for r in state["statement_lines"]},
                    "transaction": {transaction_id},
                }
                if int(raw_id) not in valid_ids[kind]:
                    raise ValueError("evidence reference is not linked to this transaction")
            payload = {"transaction_id": transaction_id, "split_id": split_id,
                       "to_category_id": to_category_id, "expected_state_digest": expected_state_digest,
                       "machine_category_proposal": True}
            proposal_id = repo_actions.enqueue_proposal(
                conn, kind="recategorization", payload=payload, evidence={"refs": refs, "actor": ACTOR},
                rationale=rationale, confidence=confidence or 0, agent_run_id=ACTOR,
            )
            conn.execute(
                "INSERT INTO category_operations(operation_key,actor,request_json,proposed_action_id) VALUES (?,?,?,?)",
                (operation_key, ACTOR, request_json, proposal_id),
            )
    return proposal_detail(proposal_id)
