"""Approval queue repository for agent-proposed financial actions."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from ..actions import get_handler

ACTIVE_STATUSES = ("proposed", "needs_evidence")
FINAL_STATUSES = ("approved", "edited_approved", "rejected", "reverted")
APPLIED_STATUSES = ("approved", "edited_approved")

STATUS_LABELS = {
    "proposed": "awaiting approval",
    "approved": "approved",
    "edited_approved": "edited + approved",
    "rejected": "rejected",
    "needs_evidence": "needs evidence",
    "snoozed": "snoozed",
    "reverted": "reverted",
}


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _json_load(raw: str | None) -> Any:
    if raw in (None, ""):
        return None
    return json.loads(raw)


def _require_dict(value: object, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def normalize_payload(kind: str, payload: dict) -> dict:
    handler = get_handler(kind)
    raw = _require_dict(payload, "payload")
    normalizer = getattr(handler, "normalize", None)
    if normalizer is None:
        return dict(raw)
    normalized = normalizer(raw)
    return _require_dict(normalized, "payload")


def _decode_proposal(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["payload"] = _json_load(row["payload_json"]) or {}
    out["original_payload"] = _json_load(row["original_payload_json"]) or {}
    out["evidence"] = _json_load(row["evidence_json"]) or {}
    out["revert"] = _json_load(row["revert_json"])
    out["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    return out


def _decode_audit(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["payload_snapshot"] = _json_load(row["payload_snapshot_json"]) or {}
    out["detail"] = _json_load(row["detail_json"]) or {}
    out["to_status_label"] = STATUS_LABELS.get(row["to_status"], row["to_status"])
    out["from_status_label"] = (
        STATUS_LABELS.get(row["from_status"], row["from_status"])
        if row["from_status"] is not None
        else None
    )
    return out


def _insert_audit(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    from_status: str | None,
    to_status: str,
    actor: str = "",
    feedback: str = "",
    payload: dict | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO proposed_action_audit(
          proposed_action_id, from_status, to_status, actor, feedback,
          payload_snapshot_json, detail_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            from_status,
            to_status,
            actor,
            feedback,
            _json_dump(payload or {}),
            _json_dump(detail or {}),
        ),
    )


def _load_or_raise(conn: sqlite3.Connection, proposal_id: int) -> dict:
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise LookupError("proposal not found")
    return proposal


def _ensure_active(status: str) -> None:
    if status not in {"proposed", "needs_evidence", "snoozed"}:
        raise ValueError("proposal is not awaiting a decision")


def enqueue_proposal(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict,
    evidence: dict | None = None,
    confidence: float = 0.0,
    rationale: str = "",
    agent_run_id: str = "",
) -> int:
    handler = get_handler(kind)
    payload = normalize_payload(kind, payload)
    evidence = _require_dict(evidence, "evidence")
    confidence = float(confidence)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    handler.validate(conn, payload)
    payload_json = _json_dump(payload)
    cur = conn.execute(
        """
        INSERT INTO proposed_actions(
          kind, payload_json, original_payload_json, evidence_json,
          confidence, rationale, agent_run_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')
        """,
        (
            kind,
            payload_json,
            payload_json,
            _json_dump(evidence),
            confidence,
            rationale,
            agent_run_id,
        ),
    )
    proposal_id = int(cur.lastrowid)
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=None,
        to_status="proposed",
        payload=payload,
        detail={"event": "enqueued"},
    )
    return proposal_id


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM proposed_actions WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        return None
    return _decode_proposal(row)


def list_proposals(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | tuple[str, ...] | None = None,
    as_of: str | None = None,
) -> list[dict]:
    if statuses is None:
        today = as_of or date.today().isoformat()
        rows = conn.execute(
            """
            SELECT *
            FROM proposed_actions
            WHERE status IN ('proposed', 'needs_evidence')
               OR (status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?)
            ORDER BY created_at DESC, id DESC
            """,
            (today,),
        ).fetchall()
    else:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT *
            FROM proposed_actions
            WHERE status IN ({placeholders})
            ORDER BY created_at DESC, id DESC
            """,
            tuple(statuses),
        ).fetchall()
    return [_decode_proposal(row) for row in rows]


def audit_trail(conn: sqlite3.Connection, proposal_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM proposed_action_audit
        WHERE proposed_action_id=?
        ORDER BY created_at ASC, id ASC
        """,
        (proposal_id,),
    ).fetchall()
    return [_decode_audit(row) for row in rows]


def edit_payload(conn: sqlite3.Connection, proposal_id: int, new_payload: dict) -> None:
    proposal = _load_or_raise(conn, proposal_id)
    _ensure_active(proposal["status"])
    new_payload = normalize_payload(proposal["kind"], new_payload)
    conn.execute(
        """
        UPDATE proposed_actions
        SET payload_json=?, decided_by='user', decided_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (_json_dump(new_payload), proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status=proposal["status"],
        actor="user",
        payload=new_payload,
        detail={"event": "payload_edited"},
    )


def reject(conn: sqlite3.Connection, proposal_id: int, *, feedback: str, actor: str = "user") -> None:
    proposal = _load_or_raise(conn, proposal_id)
    _ensure_active(proposal["status"])
    feedback = feedback.strip()
    if not feedback:
        raise ValueError("feedback is required")
    conn.execute(
        """
        UPDATE proposed_actions
        SET status='rejected', feedback=?, decided_by=?, decided_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (feedback, actor, proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status="rejected",
        actor=actor,
        feedback=feedback,
        payload=proposal["payload"],
        detail={"event": "rejected"},
    )


def request_evidence(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    feedback: str,
    actor: str = "user",
) -> None:
    proposal = _load_or_raise(conn, proposal_id)
    _ensure_active(proposal["status"])
    feedback = feedback.strip()
    if not feedback:
        raise ValueError("feedback is required")
    conn.execute(
        """
        UPDATE proposed_actions
        SET status='needs_evidence', feedback=?, decided_by=?, decided_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (feedback, actor, proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status="needs_evidence",
        actor=actor,
        feedback=feedback,
        payload=proposal["payload"],
        detail={"event": "requested_evidence"},
    )


def snooze(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    snoozed_until: str,
    actor: str = "user",
) -> None:
    proposal = _load_or_raise(conn, proposal_id)
    _ensure_active(proposal["status"])
    try:
        normalized_snoozed_until = date.fromisoformat(snoozed_until).isoformat()
    except (TypeError, ValueError):
        raise ValueError("snoozed_until must be an ISO date") from None
    conn.execute(
        """
        UPDATE proposed_actions
        SET status='snoozed', snoozed_until=?, decided_by=?, decided_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (normalized_snoozed_until, actor, proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status="snoozed",
        actor=actor,
        payload=proposal["payload"],
        detail={"event": "snoozed", "snoozed_until": normalized_snoozed_until},
    )


def mark_applied(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    status: str,
    revert: dict | None,
    detail: dict,
    actor: str,
) -> None:
    if status not in APPLIED_STATUSES:
        raise ValueError("invalid applied status")
    proposal = _load_or_raise(conn, proposal_id)
    _ensure_active(proposal["status"])
    conn.execute(
        """
        UPDATE proposed_actions
        SET status=?, revert_json=?, decided_by=?, decided_at=CURRENT_TIMESTAMP,
            applied_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, _json_dump(revert) if revert is not None else None, actor, proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status=status,
        actor=actor,
        payload=proposal["payload"],
        detail=detail,
    )


def mark_reverted(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    detail: dict,
    actor: str,
) -> None:
    proposal = _load_or_raise(conn, proposal_id)
    if proposal["status"] not in APPLIED_STATUSES:
        raise ValueError("proposal is not applied")
    conn.execute(
        """
        UPDATE proposed_actions
        SET status='reverted', decided_by=?, decided_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (actor, proposal_id),
    )
    _insert_audit(
        conn,
        proposal_id=proposal_id,
        from_status=proposal["status"],
        to_status="reverted",
        actor=actor,
        payload=proposal["payload"],
        detail=detail,
    )
