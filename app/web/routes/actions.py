"""Human approval queue for agent-proposed financial actions."""
from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...actions import get_handler
from ...config import get_settings
from ...db import (
    engine,
    repo_actions,
    repo_budgets,
    repo_close,
    repo_embeddings,
    repo_labels,
    repo_merchant_knowledge,
)
from ..templating import templates

router = APIRouter()


def _first_day_next_month(today: date | None = None) -> str:
    today = today or date.today()
    if today.month == 12:
        return f"{today.year + 1}-01-01"
    return f"{today.year}-{today.month + 1:02d}-01"


def _proposal_or_404(conn: sqlite3.Connection, proposal_id: int) -> dict:
    proposal = repo_actions.get_proposal(conn, proposal_id)
    if proposal is None:
        raise HTTPException(404, "proposal not found")
    return proposal


def _statement_line_months(conn: sqlite3.Connection, proposals: list[dict]) -> dict[int, str]:
    line_ids: set[int] = set()
    for proposal in proposals:
        for raw_id in proposal.get("evidence", {}).get("statement_line_ids") or []:
            try:
                line_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
    if not line_ids:
        return {}
    placeholders = ",".join("?" for _ in line_ids)
    rows = conn.execute(
        f"""
        SELECT id, strftime('%Y-%m', posted_on) AS month
        FROM statement_lines
        WHERE id IN ({placeholders})
          AND review_disposition='active'
        """,
        tuple(line_ids),
    ).fetchall()
    return {int(row["id"]): row["month"] for row in rows}


def _payload_items(proposals: list[dict]) -> None:
    for proposal in proposals:
        proposal["payload_items"] = list(proposal.get("payload", {}).items())


def _edited_payload(
    proposal: dict,
    *,
    to_category_id: int | None,
    decision: str | None,
) -> dict:
    payload = dict(proposal["payload"])
    if proposal["kind"] == "recategorization" and to_category_id is not None:
        payload["to_category_id"] = to_category_id
    elif proposal["kind"] == "subscription_label" and decision is not None:
        payload["decision"] = decision
    return payload


def _evidence_neighbor_ids(evidence: dict) -> list[int]:
    raw_ids = evidence.get("similar_transaction_ids") or []
    if not raw_ids and evidence.get("neighbors"):
        raw_ids = [row.get("transaction_id") for row in evidence.get("neighbors") or []]
    out: list[int] = []
    for raw_id in raw_ids:
        try:
            out.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    return out


def _merchant_resolution_claim_id(proposal: dict) -> int | None:
    raw = (proposal.get("evidence") or {}).get(
        "merchant_resolution_claim_id"
    )
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            "merchant resolution claim evidence is invalid"
        ) from None


def _record_recategorization_label(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    proposal: dict,
    result: dict,
) -> None:
    if proposal["kind"] != "recategorization" or result.get("noop"):
        return
    detail = result.get("detail") or {}
    try:
        transaction_id = int(detail["transaction_id"])
        category_id = int(detail["to_category_id"])
    except (KeyError, TypeError, ValueError):
        return
    category = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if category is None or category["name"] == "Uncategorized":
        return
    txn = conn.execute(
        """
        SELECT
          COALESCE(NULLIF(counterparty, ''), description) AS merchant,
          description,
          amount_cents
        FROM transactions
        WHERE id=?
        """,
        (transaction_id,),
    ).fetchone()
    if txn is None:
        return
    repo_labels.record_label(
        conn,
        transaction_id=transaction_id,
        category_id=category_id,
        category_name=str(category["name"]),
        merchant=str(txn["merchant"] or ""),
        description=str(txn["description"] or ""),
        amount_cents=int(txn["amount_cents"]),
        neighbor_ids=_evidence_neighbor_ids(proposal.get("evidence") or {}),
        confidence=float(proposal.get("confidence") or 0.0),
        source=str(proposal.get("agent_run_id") or ""),
        proposed_action_id=proposal_id,
    )


def _recategorization_transaction_id(proposal: dict, detail: dict | None = None) -> int | None:
    for payload in (detail or {}, proposal.get("revert") or {}, proposal.get("payload") or {}):
        try:
            return int(payload["transaction_id"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _annotate_locked_month(conn: sqlite3.Connection, proposal: dict) -> None:
    """Tag a recategorization proposal with the closed month it would edit, if any.

    Drives the override affordance in the UI: a proposal whose transaction sits in a
    closed month needs an explicit, audited override to apply or revert.
    """
    proposal["locked_month"] = None
    if proposal.get("kind") != "recategorization":
        return
    transaction_id = _recategorization_transaction_id(proposal)
    if transaction_id is None:
        return
    month = repo_close.transaction_month(conn, transaction_id)
    if month is not None and repo_close.is_month_locked(conn, month):
        proposal["locked_month"] = month


@router.get("/actions", response_class=HTMLResponse)
def actions_queue(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        proposals = repo_actions.list_proposals(conn)
        _payload_items(proposals)
        for proposal in proposals:
            _annotate_locked_month(conn, proposal)
        categories = conn.execute("SELECT * FROM categories ORDER BY kind, name").fetchall()
        line_months = _statement_line_months(conn, proposals)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "mode": "queue",
            "proposals": proposals,
            "categories": categories,
            "line_months": line_months,
            "subscription_decisions": repo_budgets.SUBSCRIPTION_WATCHLIST_DECISIONS,
            "next_snooze_date": _first_day_next_month(),
            "active": "more",
            "brand": "nancy",
        },
    )


@router.post("/actions/{proposal_id}/approve")
def approve_action(
    proposal_id: int,
    to_category_id: int | None = Form(None),
    decision: str | None = Form(None),
    override: str | None = Form(None),
):
    override_on = override == "1"
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            proposal = _proposal_or_404(conn, proposal_id)
            if proposal["status"] in repo_actions.APPLIED_STATUSES:
                return RedirectResponse("/actions", status_code=303)
            if proposal["status"] == "reverted":
                raise ValueError("proposal has already been reverted")

            new_payload = _edited_payload(
                proposal,
                to_category_id=to_category_id,
                decision=decision,
            )
            current_payload = repo_actions.normalize_payload(proposal["kind"], proposal["payload"])
            new_payload = repo_actions.normalize_payload(proposal["kind"], new_payload)
            edited = new_payload != current_payload
            if edited:
                repo_actions.edit_payload(conn, proposal_id, new_payload)
                proposal = _proposal_or_404(conn, proposal_id)
                payload = proposal["payload"]
            else:
                payload = current_payload

            handler = get_handler(proposal["kind"])
            handler.validate(conn, payload)
            # override is a per-request soft-lock opt-in, not part of the stored payload.
            result = handler.apply(
                conn,
                {
                    **payload,
                    "override": override_on,
                    "_proposed_action_id": proposal_id,
                    "_merchant_resolution_claim_id": (
                        _merchant_resolution_claim_id(proposal)
                    ),
                    "_actor": "operator:actions",
                },
            )
            repo_embeddings.enqueue_embed_transactions(conn)
            _record_recategorization_label(
                conn,
                proposal_id=proposal_id,
                proposal=proposal,
                result=result,
            )
            repo_actions.mark_applied(
                conn,
                proposal_id,
                status="edited_approved" if edited else "approved",
                revert=result.get("revert"),
                detail={"apply": result.get("detail", {}), "noop": bool(result.get("noop"))},
                actor="user",
            )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except NotImplementedError as exc:
        raise HTTPException(400, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/actions", status_code=303)


@router.post("/actions/{proposal_id}/reject")
def reject_action(proposal_id: int, feedback: str = Form("")):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            proposal = _proposal_or_404(conn, proposal_id)
            claim_id = _merchant_resolution_claim_id(proposal)
            repo_actions.reject(conn, proposal_id, feedback=feedback, actor="user")
            if claim_id is not None:
                repo_merchant_knowledge.reject_claim(
                    conn,
                    claim_id=claim_id,
                    operation_key=f"action:{proposal_id}:reject-knowledge",
                    actor="operator:actions",
                    reason="operator rejected category proposal",
                )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/actions", status_code=303)


@router.post("/actions/{proposal_id}/request-evidence")
def request_evidence_action(proposal_id: int, feedback: str = Form("")):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_actions.request_evidence(conn, proposal_id, feedback=feedback, actor="user")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/actions", status_code=303)


@router.post("/actions/{proposal_id}/snooze")
def snooze_action(proposal_id: int, snoozed_until: str = Form("")):
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            repo_actions.snooze(
                conn,
                proposal_id,
                snoozed_until=snoozed_until or _first_day_next_month(),
                actor="user",
            )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/actions", status_code=303)


@router.post("/actions/{proposal_id}/revert")
def revert_action(proposal_id: int, override: str | None = Form(None)):
    override_on = override == "1"
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            proposal = _proposal_or_404(conn, proposal_id)
            if proposal["status"] == "reverted":
                return RedirectResponse("/actions", status_code=303)
            if proposal["status"] not in repo_actions.APPLIED_STATUSES:
                raise ValueError("proposal is not applied")
            if proposal["revert"] is None:
                raise ValueError("proposal has no revert payload")
            handler = get_handler(proposal["kind"])
            # override is a per-request soft-lock opt-in, not part of the stored payload.
            detail = handler.revert(conn, {**proposal["revert"], "override": override_on})
            repo_embeddings.enqueue_embed_transactions(conn)
            repo_actions.mark_reverted(conn, proposal_id, detail=detail, actor="user")
            if proposal["kind"] == "recategorization":
                transaction_id = _recategorization_transaction_id(proposal, detail)
                if transaction_id is not None:
                    repo_labels.clear_label(conn, transaction_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except NotImplementedError as exc:
        raise HTTPException(400, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return RedirectResponse("/actions", status_code=303)


@router.get("/actions/{proposal_id}/audit", response_class=HTMLResponse)
def action_audit(request: Request, proposal_id: int):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        proposal = _proposal_or_404(conn, proposal_id)
        proposal["payload_items"] = list(proposal["payload"].items())
        _annotate_locked_month(conn, proposal)
        trail = repo_actions.audit_trail(conn, proposal_id)
        line_months = _statement_line_months(conn, [proposal])
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "mode": "audit",
            "proposal": proposal,
            "audit_rows": trail,
            "line_months": line_months,
            "active": "more",
            "brand": "nancy",
        },
    )
