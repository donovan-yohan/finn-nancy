"""FN-147 immutable period-close policy and closed-period write capability.

The append-only close tables are the source of truth.  Callers may either
write an open/reopened month, explicitly reopen a closed month, or ask this
module to perform an audited override which reopens every locked month before
the caller's mutation executes in the same database transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_EXCEPTION_TYPES = frozenset(
    {
        "missing_statement",
        "unresolved_line",
        "unclassified_positive_flow",
        "unconfirmed_merchant_category",
        "unexplained_balance_delta",
        "evidence_gap",
        "manual_adjustment",
    }
)
_CLOSED_STATES = frozenset({"clean_closed", "closed_with_exceptions"})


class PeriodPolicyError(ValueError):
    """Base error for invalid close transitions and guarded period writes."""


class PeriodLockedError(PeriodPolicyError):
    """A write targeted a closed period without reopen or audited override."""

    def __init__(self, month: str):
        self.month = month
        super().__init__(
            f"{month} is closed; explicitly reopen it or use an audited override"
        )


class PeriodTransitionError(PeriodPolicyError):
    """The requested lifecycle transition is not valid from current state."""


class PeriodOperationConflict(PeriodPolicyError):
    """An idempotency key was reused for a semantically different operation."""


class PeriodAcknowledgementRequired(PeriodTransitionError):
    """An exception close was attempted without exact durable acknowledgements."""

    def __init__(self, month: str, exception_tokens: Iterable[str]):
        self.month = month
        self.exception_tokens = tuple(sorted(str(token) for token in exception_tokens))
        super().__init__(
            f"{month} cannot close with exceptions until every exception is "
            "durably acknowledged"
        )


def normalize_month(value: str) -> str:
    """Return a strict ``YYYY-MM`` month or raise a typed policy error."""
    month = str(value).strip()
    if not _MONTH_RE.fullmatch(month):
        raise PeriodPolicyError(f"invalid period month: {value!r}")
    return month


def month_for_date(value: str | date) -> str:
    """Return the posting month for an ISO date/date-like value."""
    text = value.isoformat() if isinstance(value, date) else str(value).strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise PeriodPolicyError(f"invalid posting date: {value!r}") from exc
    return parsed.strftime("%Y-%m")


def _required_text(value: object, *, field: str, maximum: int) -> str:
    text = str(value).strip()
    if not text:
        raise PeriodPolicyError(f"{field} is required")
    if len(text) > maximum:
        raise PeriodPolicyError(f"{field} exceeds {maximum} characters")
    return text


def _operation_key(value: object) -> str:
    return _required_text(value, field="operation_key", maximum=180)


def _json_object(value: Mapping[str, Any] | None, *, field: str) -> str:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise PeriodPolicyError(f"{field} must be an object")
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise PeriodPolicyError(f"{field} must be JSON serializable") from exc


def _json_array(value: list[Mapping[str, Any]], *, field: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise PeriodPolicyError(f"{field} must be JSON serializable") from exc


def _digest(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _stable_key(prefix: str, operation_key: str, suffix: str = "") -> str:
    seed = f"{operation_key}:{suffix}" if suffix else operation_key
    return f"{prefix}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"


def get_state(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    """Return the immutable-policy current-state projection for ``month``."""
    return conn.execute(
        "SELECT * FROM v_current_period_close_state WHERE month=?",
        (normalize_month(month),),
    ).fetchone()


def current_state(conn: sqlite3.Connection, month: str) -> str:
    """Return one of open, clean_closed, closed_with_exceptions, or reopened."""
    row = get_state(conn, month)
    return "open" if row is None else str(row["state"])


def list_snapshot_history(
    conn: sqlite3.Connection,
    month: str,
) -> list[sqlite3.Row]:
    """Return every immutable snapshot for a month, newest cycle first."""
    return conn.execute(
        """SELECT *
           FROM v_period_close_snapshot_history
           WHERE month=?
           ORDER BY cycle_number DESC, snapshot_id DESC""",
        (normalize_month(month),),
    ).fetchall()


def list_current_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    """Return current typed exceptions plus reversible acknowledgement state."""
    rows = conn.execute(
        """SELECT
             exception.*,
             acknowledgement.id AS acknowledgement_id,
             acknowledgement.actor AS acknowledgement_actor,
             acknowledgement.reason AS acknowledgement_reason,
             acknowledgement.created_at AS acknowledged_at
           FROM v_period_close_active_exceptions exception
           JOIN period_close_cycles cycle ON cycle.id=exception.cycle_id
           LEFT JOIN v_period_close_current_acknowledgements acknowledgement
             ON acknowledgement.id=(
               SELECT current_ack.id
               FROM v_period_close_current_acknowledgements current_ack
               WHERE current_ack.exception_id=exception.id
               ORDER BY current_ack.id DESC
               LIMIT 1
             )
           WHERE cycle.month=?
           ORDER BY exception.exception_type, exception.subject_kind,
                    exception.subject_id, exception.id""",
        (normalize_month(month),),
    ).fetchall()
    return [
        {
            **dict(row),
            "affected_ids": json.loads(str(row["affected_ids_json"])),
            "evidence": json.loads(str(row["evidence_json"])),
            "is_acknowledged": row["acknowledgement_id"] is not None,
        }
        for row in rows
    ]


def is_month_locked(conn: sqlite3.Connection, month: str) -> bool:
    return current_state(conn, month) in _CLOSED_STATES


def _operation_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    operation_key: str,
) -> sqlite3.Row | None:
    # ``table`` is always an internal literal selected by this module.
    return conn.execute(
        f"SELECT * FROM {table} WHERE operation_key=?",
        (operation_key,),
    ).fetchone()


def _assert_existing_semantics(
    row: sqlite3.Row,
    expected: Mapping[str, object],
    *,
    operation_key: str,
) -> None:
    mismatches = {
        key: (row[key], value)
        for key, value in expected.items()
        if row[key] != value
    }
    if mismatches:
        raise PeriodOperationConflict(
            f"operation_key {operation_key!r} was reused with different semantics"
        )


def close_period(
    conn: sqlite3.Connection,
    month: str,
    *,
    snapshot: Mapping[str, Any],
    exceptions: Iterable[Mapping[str, Any]],
    actor: str,
    reason: str,
    operation_key: str,
) -> sqlite3.Row:
    """Append a close cycle, typed exceptions, immutable snapshot, and event.

    The state is derived: zero typed exceptions is ``clean_closed``; any typed
    exception is ``closed_with_exceptions``.  Callers cannot request a cleaner
    label than the evidence supports.
    """
    month = normalize_month(month)
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    request_snapshot_json = _json_object(snapshot, field="snapshot")
    request_snapshot_digest = _digest(request_snapshot_json)
    request_exceptions = _normalize_exception_set(exceptions)
    request_exception_json = _json_array(
        [_exception_digest_payload(item) for item in request_exceptions],
        field="exceptions",
    )
    request_exception_digest = _digest(request_exception_json)

    existing = _operation_row(
        conn,
        table="period_close_snapshots",
        operation_key=f"{operation_key}:snapshot",
    )
    if existing is not None:
        cycle = conn.execute(
            "SELECT month FROM period_close_cycles WHERE id=?",
            (existing["cycle_id"],),
        ).fetchone()
        if cycle is None or cycle["month"] != month:
            raise PeriodOperationConflict(
                f"operation_key {operation_key!r} belongs to another period"
            )
        _assert_existing_semantics(
            existing,
            {
                "request_exception_digest": request_exception_digest,
                "request_snapshot_digest": request_snapshot_digest,
                "created_by": actor,
                "reason": reason,
            },
            operation_key=operation_key,
        )
        return existing

    prior = get_state(conn, month)
    from_state = "open" if prior is None else str(prior["state"])
    if from_state not in {"open", "reopened"}:
        raise PeriodTransitionError(
            f"{month} cannot close from {from_state}; reopen it first"
        )

    effective_exceptions = _merge_effective_exceptions(
        conn,
        month,
        request_exceptions,
    )
    preclose_acknowledgements = [
        _current_preclose_acknowledgement(
            conn,
            month=month,
            exception_token=_exception_token(item),
        )
        for item in effective_exceptions
    ]
    missing_acknowledgements = [
        _exception_token(item)
        for item, acknowledgement in zip(
            effective_exceptions,
            preclose_acknowledgements,
            strict=True,
        )
        if acknowledgement is None
    ]
    if missing_acknowledgements:
        raise PeriodAcknowledgementRequired(month, missing_acknowledgements)
    effective_exception_json = _json_array(
        [_exception_digest_payload(item) for item in effective_exceptions],
        field="effective exceptions",
    )
    effective_exception_digest = _digest(effective_exception_json)
    close_state = "clean_closed" if not effective_exceptions else "closed_with_exceptions"
    exception_type_counts: dict[str, int] = {}
    for item in effective_exceptions:
        exception_type = str(item["exception_type"])
        exception_type_counts[exception_type] = (
            exception_type_counts.get(exception_type, 0) + 1
        )
    authoritative_snapshot = json.loads(request_snapshot_json)
    authoritative_snapshot.update(
        {
            "month": month,
            "close_state": close_state,
            "exception_count": len(effective_exceptions),
            "exception_type_counts": dict(sorted(exception_type_counts.items())),
            "exception_lineage_keys": [
                str(item["lineage_key"]) for item in effective_exceptions
            ],
            "exception_acknowledgement_count": len(
                preclose_acknowledgements
            ),
            "exception_acknowledgements": [
                {
                    "lineage_key": str(item["lineage_key"]),
                    "exception_token": _exception_token(item),
                    "actor": str(acknowledgement["actor"]),
                    "reason": str(acknowledgement["reason"]),
                    "evidence_digest": str(
                        acknowledgement["evidence_digest"]
                    ),
                }
                for item, acknowledgement in zip(
                    effective_exceptions,
                    preclose_acknowledgements,
                    strict=True,
                )
                if acknowledgement is not None
            ],
        }
    )
    snapshot_json = _json_object(
        authoritative_snapshot,
        field="authoritative snapshot",
    )

    latest_cycle = conn.execute(
        """SELECT id, cycle_number
           FROM period_close_cycles
           WHERE month=?
           ORDER BY cycle_number DESC, id DESC
           LIMIT 1""",
        (month,),
    ).fetchone()
    cycle_number = 1 if latest_cycle is None else int(latest_cycle["cycle_number"]) + 1
    prior_cycle_id = None if latest_cycle is None else int(latest_cycle["id"])
    cycle_operation = f"{operation_key}:cycle"
    cycle_cursor = conn.execute(
        """INSERT INTO period_close_cycles(
             cycle_key, month, cycle_number, prior_cycle_id, created_by,
             reason, operation_key
           ) VALUES (?,?,?,?,?,?,?)""",
        (
            _stable_key("cycle", operation_key),
            month,
            cycle_number,
            prior_cycle_id,
            actor,
            reason,
            cycle_operation,
        ),
    )
    cycle_id = int(cycle_cursor.lastrowid)

    for index, (item, acknowledgement) in enumerate(
        zip(
            effective_exceptions,
            preclose_acknowledgements,
            strict=True,
        )
    ):
        exception_id = _insert_exception(
            conn,
            cycle_id=cycle_id,
            item=item,
            actor=actor,
            operation_key=f"{operation_key}:exception:{index}",
        )
        assert acknowledgement is not None
        _insert_frozen_acknowledgement(
            conn,
            exception_id=exception_id,
            exception_token=_exception_token(item),
            source=acknowledgement,
            operation_key=f"{operation_key}:exception:{index}:ack",
        )

    snapshot_operation = f"{operation_key}:snapshot"
    snapshot_cursor = conn.execute(
        """INSERT INTO period_close_snapshots(
             snapshot_key, cycle_id, snapshot_number, close_state,
             exception_count, request_exception_digest, exception_digest,
             request_snapshot_digest, snapshot_json, snapshot_digest,
             created_by, reason, operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("snapshot", operation_key),
            cycle_id,
            cycle_number,
            close_state,
            len(effective_exceptions),
            request_exception_digest,
            effective_exception_digest,
            request_snapshot_digest,
            snapshot_json,
            _digest(snapshot_json),
            actor,
            reason,
            snapshot_operation,
        ),
    )
    snapshot_id = int(snapshot_cursor.lastrowid)

    evidence = _json_object(
        {
            "snapshot_digest": _digest(snapshot_json),
            "request_exception_digest": request_exception_digest,
            "exception_digest": effective_exception_digest,
            "request_snapshot_digest": request_snapshot_digest,
            "exception_count": len(effective_exceptions),
            "exception_acknowledgement_count": len(
                preclose_acknowledgements
            ),
        },
        field="close evidence",
    )
    conn.execute(
        """INSERT INTO period_close_events(
             event_key, cycle_id, event_kind, from_state, to_state, snapshot_id,
             actor, reason, affected_ids_json, evidence_json, evidence_digest,
             operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("event", operation_key),
            cycle_id,
            close_state,
            from_state,
            close_state,
            snapshot_id,
            actor,
            reason,
            _json_object({"period_month": month}, field="affected_ids"),
            evidence,
            _digest(evidence),
            f"{operation_key}:event",
        ),
    )
    _update_legacy_projection(
        conn,
        month=month,
        status="closed",
        summary_json=snapshot_json,
    )
    row = conn.execute(
        "SELECT * FROM period_close_snapshots WHERE id=?",
        (snapshot_id,),
    ).fetchone()
    assert row is not None
    return row


def _insert_exception(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    item: Mapping[str, Any],
    actor: str,
    operation_key: str,
) -> int:
    exception_type = str(item["exception_type"])
    subject_kind = str(item["subject_kind"])
    subject_id = str(item["subject_id"])
    exception_reason = str(item["reason"])
    affected_json = _json_object(
        item.get("affected_ids"),
        field="exception affected_ids",
    )
    evidence_json = _json_object(
        item.get("evidence"),
        field="exception evidence",
    )
    resolution_href = str(item["resolution_href"])
    cursor = conn.execute(
        """INSERT INTO period_close_exceptions(
             exception_key, cycle_id, lineage_key, prior_exception_id,
             exception_type, subject_kind, subject_id, affected_ids_json,
             evidence_json, evidence_digest, amount_cents, reason,
             resolution_href, created_by, operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("exception", operation_key),
            cycle_id,
            str(item["lineage_key"]),
            item.get("prior_exception_id"),
            exception_type,
            subject_kind,
            subject_id,
            affected_json,
            evidence_json,
            _digest(evidence_json),
            int(item.get("amount_cents", 0)),
            exception_reason,
            resolution_href,
            actor,
            operation_key,
        ),
    )
    return int(cursor.lastrowid)


def _insert_frozen_acknowledgement(
    conn: sqlite3.Connection,
    *,
    exception_id: int,
    exception_token: str,
    source: sqlite3.Row,
    operation_key: str,
) -> sqlite3.Row:
    """Consume one exact pre-close acknowledgement onto a frozen exception."""
    cursor = conn.execute(
        """INSERT INTO period_close_acknowledgements(
             acknowledgement_key, exception_id, exception_token, event_kind,
             actor, reason, evidence_json, evidence_digest, operation_key,
             source_preclose_acknowledgement_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("ack", operation_key),
            int(exception_id),
            exception_token,
            "acknowledged",
            str(source["actor"]),
            str(source["reason"]),
            str(source["evidence_json"]),
            str(source["evidence_digest"]),
            operation_key,
            int(source["id"]),
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_acknowledgements WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def _normalize_exception(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise PeriodPolicyError("each exception must be an object")
    exception_type = str(item.get("exception_type", "")).strip()
    if exception_type not in _EXCEPTION_TYPES:
        raise PeriodPolicyError(f"unsupported exception_type: {exception_type!r}")
    subject_kind = _required_text(
        item.get("subject_kind", exception_type),
        field="subject_kind",
        maximum=80,
    )
    subject_id = str(item.get("subject_id", "")).strip()
    if len(subject_id) > 160:
        raise PeriodPolicyError("subject_id exceeds 160 characters")
    reason = _required_text(
        item.get("reason", ""),
        field="exception reason",
        maximum=500,
    )
    resolution_href = str(item.get("resolution_href", "")).strip()
    if len(resolution_href) > 500:
        raise PeriodPolicyError("resolution_href exceeds 500 characters")
    identity_json = _json_object(
        {
            "exception_type": exception_type,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
        },
        field="exception identity",
    )
    provided_lineage = str(item.get("lineage_key", "")).strip()
    lineage_key = provided_lineage or f"lineage:{_digest(identity_json)}"
    if len(lineage_key) > 200:
        raise PeriodPolicyError("lineage_key exceeds 200 characters")
    affected = json.loads(
        _json_object(item.get("affected_ids"), field="exception affected_ids")
    )
    evidence = json.loads(
        _json_object(item.get("evidence"), field="exception evidence")
    )
    return {
        "lineage_key": lineage_key,
        "exception_type": exception_type,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "affected_ids": affected,
        "evidence": evidence,
        "amount_cents": int(item.get("amount_cents", 0)),
        "reason": reason,
        "resolution_href": resolution_href,
    }


def _normalize_exception_set(
    exceptions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [_normalize_exception(item) for item in exceptions]
    normalized.sort(key=lambda item: str(item["lineage_key"]))
    if len({item["lineage_key"] for item in normalized}) != len(normalized):
        raise PeriodPolicyError("duplicate close exception identity")
    return normalized


def _exception_digest_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "lineage_key",
            "exception_type",
            "subject_kind",
            "subject_id",
            "affected_ids",
            "evidence",
            "amount_cents",
            "reason",
            "resolution_href",
        )
    }


def _exception_token(item: Mapping[str, Any]) -> str:
    payload_json = _json_object(
        _exception_digest_payload(item),
        field="exception token payload",
    )
    return f"exception:{_digest(payload_json)}"


def _exception_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "lineage_key": str(row["lineage_key"]),
        "prior_exception_id": int(row["id"]),
        "exception_type": str(row["exception_type"]),
        "subject_kind": str(row["subject_kind"]),
        "subject_id": str(row["subject_id"]),
        "affected_ids": json.loads(str(row["affected_ids_json"])),
        "evidence": json.loads(str(row["evidence_json"])),
        "amount_cents": int(row["amount_cents"]),
        "reason": str(row["reason"]),
        "resolution_href": str(row["resolution_href"]),
    }


def _merge_effective_exceptions(
    conn: sqlite3.Connection,
    month: str,
    request_exceptions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    effective_by_lineage = {
        str(item["lineage_key"]): item
        for item in _unresolved_month_exceptions(conn, month)
    }
    for request_item in request_exceptions:
        item = dict(request_item)
        lineage_key = str(item["lineage_key"])
        prior = effective_by_lineage.get(lineage_key)
        if prior is not None:
            item["prior_exception_id"] = int(prior["prior_exception_id"])
        effective_by_lineage[lineage_key] = item
    return [
        effective_by_lineage[lineage_key]
        for lineage_key in sorted(effective_by_lineage)
    ]


def _current_preclose_acknowledgement(
    conn: sqlite3.Connection,
    *,
    month: str,
    exception_token: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT *
           FROM v_period_close_current_preacknowledgements
           WHERE month=? AND exception_token=?
           ORDER BY id DESC
           LIMIT 1""",
        (normalize_month(month), exception_token),
    ).fetchone()


def _unresolved_month_exceptions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT exception.*
           FROM period_close_exceptions exception
           JOIN period_close_cycles cycle ON cycle.id=exception.cycle_id
           WHERE cycle.month=?
             AND NOT EXISTS (
               SELECT 1
               FROM period_close_exceptions later
               JOIN period_close_cycles later_cycle
                 ON later_cycle.id=later.cycle_id
               WHERE later.lineage_key=exception.lineage_key
                 AND later.id>exception.id
                 AND later_cycle.month=cycle.month
             )
             AND NOT EXISTS (
               SELECT 1
               FROM period_close_resolutions resolution
               WHERE resolution.exception_id=exception.id
                 AND resolution.event_kind='resolved'
                 AND NOT EXISTS (
                   SELECT 1
                   FROM period_close_resolutions reversal
                   WHERE reversal.reverses_resolution_id=resolution.id
                     AND reversal.event_kind='reversed'
                 )
             )
           ORDER BY exception.lineage_key""",
        (month,),
    ).fetchall()
    return [_exception_from_row(row) for row in rows]


def apply_preclose_acknowledgements(
    conn: sqlite3.Connection,
    month: str,
    exceptions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the exact effective exception set with current pre-close acks.

    The opaque token covers the full normalized exception payload, including
    its affected ids and evidence. Any material evidence change therefore
    requires a fresh acknowledgement before close.
    """
    month = normalize_month(month)
    request_exceptions = _normalize_exception_set(exceptions)
    effective_exceptions = _merge_effective_exceptions(
        conn,
        month,
        request_exceptions,
    )
    enriched: list[dict[str, Any]] = []
    for item in effective_exceptions:
        exception_token = _exception_token(item)
        acknowledgement = _current_preclose_acknowledgement(
            conn,
            month=month,
            exception_token=exception_token,
        )
        enriched.append(
            {
                **item,
                "exception_token": exception_token,
                "is_acknowledged": acknowledgement is not None,
                "acknowledgement_id": (
                    None
                    if acknowledgement is None
                    else int(acknowledgement["id"])
                ),
                "acknowledgement_actor": (
                    None
                    if acknowledgement is None
                    else str(acknowledgement["actor"])
                ),
                "acknowledgement_reason": (
                    None
                    if acknowledgement is None
                    else str(acknowledgement["reason"])
                ),
                "acknowledged_at": (
                    None
                    if acknowledgement is None
                    else str(acknowledgement["created_at"])
                ),
            }
        )
    return enriched


def acknowledge_preclose_exception(
    conn: sqlite3.Connection,
    month: str,
    exception: Mapping[str, Any],
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Append an exact acknowledgement while the month remains writable."""
    month = normalize_month(month)
    if is_month_locked(conn, month):
        raise PeriodTransitionError(
            f"{month} is already closed; use the frozen exception workflow"
        )
    normalized = _normalize_exception(exception)
    exception_json = _json_object(
        _exception_digest_payload(normalized),
        field="pre-close exception",
    )
    exception_digest = _digest(exception_json)
    exception_token = f"exception:{exception_digest}"
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")

    existing = _operation_row(
        conn,
        table="period_close_preacknowledgements",
        operation_key=operation_key,
    )
    if existing is not None:
        _assert_existing_semantics(
            existing,
            {
                "month": month,
                "exception_token": exception_token,
                "event_kind": "acknowledged",
                "actor": actor,
                "reason": reason,
                "exception_json": exception_json,
                "exception_digest": exception_digest,
                "evidence_json": evidence_json,
                "evidence_digest": _digest(evidence_json),
            },
            operation_key=operation_key,
        )
        return existing

    cursor = conn.execute(
        """INSERT INTO period_close_preacknowledgements(
             acknowledgement_key, month, exception_token, event_kind,
             exception_json, exception_digest, actor, reason, evidence_json,
             evidence_digest, operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("preclose-ack", operation_key),
            month,
            exception_token,
            "acknowledged",
            exception_json,
            exception_digest,
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_preacknowledgements WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def withdraw_preclose_acknowledgement(
    conn: sqlite3.Connection,
    acknowledgement_id: int,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Reverse one unused pre-close acknowledgement with an append-only row."""
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")

    existing = _operation_row(
        conn,
        table="period_close_preacknowledgements",
        operation_key=operation_key,
    )
    if existing is not None:
        _assert_existing_semantics(
            existing,
            {
                "event_kind": "withdrawn",
                "actor": actor,
                "reason": reason,
                "evidence_json": evidence_json,
                "evidence_digest": _digest(evidence_json),
                "reverses_acknowledgement_id": int(acknowledgement_id),
            },
            operation_key=operation_key,
        )
        return existing

    acknowledgement = conn.execute(
        """SELECT *
           FROM v_period_close_current_preacknowledgements
           WHERE id=?""",
        (int(acknowledgement_id),),
    ).fetchone()
    if acknowledgement is None:
        raise PeriodTransitionError(
            "pre-close acknowledgement is unknown, withdrawn, or already used"
        )
    cursor = conn.execute(
        """INSERT INTO period_close_preacknowledgements(
             acknowledgement_key, month, exception_token, event_kind,
             exception_json, exception_digest, actor, reason, evidence_json,
             evidence_digest, operation_key, reverses_acknowledgement_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("preclose-ack-withdrawal", operation_key),
            str(acknowledgement["month"]),
            str(acknowledgement["exception_token"]),
            "withdrawn",
            str(acknowledgement["exception_json"]),
            str(acknowledgement["exception_digest"]),
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
            int(acknowledgement_id),
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_preacknowledgements WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def reopen_period(
    conn: sqlite3.Connection,
    month: str,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    affected_ids: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Explicitly reopen a closed period and invalidate its current snapshot."""
    return _reopen_period(
        conn,
        month,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        affected_ids=affected_ids,
        evidence=evidence,
        reopen_kind="explicit",
    )


def _reopen_period(
    conn: sqlite3.Connection,
    month: str,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    affected_ids: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
    reopen_kind: str,
) -> sqlite3.Row:
    month = normalize_month(month)
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    affected_json = _json_object(affected_ids, field="affected_ids")
    evidence_json = _json_object(evidence, field="evidence")
    reopen_operation = f"{operation_key}:reopen"

    existing = _operation_row(
        conn,
        table="period_close_reopens",
        operation_key=reopen_operation,
    )
    if existing is not None:
        cycle = conn.execute(
            "SELECT month FROM period_close_cycles WHERE id=?",
            (existing["cycle_id"],),
        ).fetchone()
        if cycle is None or cycle["month"] != month:
            raise PeriodOperationConflict(
                f"operation_key {operation_key!r} belongs to another period"
            )
        _assert_existing_semantics(
            existing,
            {
                "reopen_kind": reopen_kind,
                "actor": actor,
                "reason": reason,
                "evidence_json": evidence_json,
                "evidence_digest": _digest(evidence_json),
            },
            operation_key=operation_key,
        )
        return existing

    state = get_state(conn, month)
    from_state = "open" if state is None else str(state["state"])
    if state is None or from_state not in _CLOSED_STATES:
        raise PeriodTransitionError(
            f"{month} cannot reopen from {from_state}; it is not closed"
        )
    cycle_id = int(state["cycle_id"])
    snapshot_id = int(state["snapshot_id"])
    event_operation = f"{operation_key}:event"
    event_cursor = conn.execute(
        """INSERT INTO period_close_events(
             event_key, cycle_id, event_kind, from_state, to_state, snapshot_id,
             actor, reason, affected_ids_json, evidence_json, evidence_digest,
             operation_key, reverses_event_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("event", operation_key),
            cycle_id,
            "override_reopened" if reopen_kind == "override" else "reopened",
            from_state,
            "reopened",
            None,
            actor,
            reason,
            affected_json,
            evidence_json,
            _digest(evidence_json),
            event_operation,
            int(state["event_id"]),
        ),
    )
    event_id = int(event_cursor.lastrowid)
    cursor = conn.execute(
        """INSERT INTO period_close_reopens(
             reopen_key, cycle_id, event_id, invalidated_snapshot_id,
             reopen_kind, actor, reason, evidence_json, evidence_digest,
             operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("reopen", operation_key),
            cycle_id,
            event_id,
            snapshot_id,
            reopen_kind,
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            reopen_operation,
        ),
    )
    _update_legacy_projection(conn, month=month, status="reopened")
    row = conn.execute(
        "SELECT * FROM period_close_reopens WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def override_periods(
    conn: sqlite3.Connection,
    months: Iterable[str],
    *,
    actor: str,
    reason: str,
    operation_key: str,
    affected_ids: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
) -> list[sqlite3.Row]:
    """Reopen every locked affected month and append one override receipt each."""
    normalized = sorted({normalize_month(month) for month in months})
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    affected_json = _json_object(affected_ids, field="affected_ids")
    if affected_json == "{}":
        raise PeriodPolicyError("affected_ids is required for an audited override")
    evidence_json = _json_object(evidence, field="evidence")
    rows: list[sqlite3.Row] = []

    for month in normalized:
        child_operation = f"{operation_key}:{month}"
        override_operation = f"{child_operation}:override"
        existing = _operation_row(
            conn,
            table="period_write_overrides",
            operation_key=override_operation,
        )
        if existing is not None:
            _assert_existing_semantics(
                existing,
                {
                    "month": month,
                    "actor": actor,
                    "reason": reason,
                    "affected_ids_json": affected_json,
                    "evidence_json": evidence_json,
                    "evidence_digest": _digest(evidence_json),
                },
                operation_key=operation_key,
            )
            rows.append(existing)
            continue
        if not is_month_locked(conn, month):
            continue
        reopen = _reopen_period(
            conn,
            month,
            actor=actor,
            reason=reason,
            operation_key=child_operation,
            affected_ids=json.loads(affected_json),
            evidence=json.loads(evidence_json),
            reopen_kind="override",
        )
        cursor = conn.execute(
            """INSERT INTO period_write_overrides(
                 override_key, request_operation_key, month, cycle_id,
                 reopen_id, actor, reason, affected_ids_json, evidence_json,
                 evidence_digest, operation_key
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _stable_key("override", operation_key, month),
                operation_key,
                month,
                int(reopen["cycle_id"]),
                int(reopen["id"]),
                actor,
                reason,
                affected_json,
                evidence_json,
                _digest(evidence_json),
                override_operation,
            ),
        )
        row = conn.execute(
            "SELECT * FROM period_write_overrides WHERE id=?",
            (int(cursor.lastrowid),),
        ).fetchone()
        assert row is not None
        rows.append(row)
    return rows


def guard_months(
    conn: sqlite3.Connection,
    months: Iterable[str],
    *,
    override: bool = False,
    actor: str = "",
    reason: str = "",
    operation_key: str = "",
    affected_ids: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> list[str]:
    """Enforce the atomic write boundary for one or more affected periods."""
    normalized = sorted({normalize_month(month) for month in months})
    locked = [month for month in normalized if is_month_locked(conn, month)]
    if not locked:
        return []
    if not override:
        raise PeriodLockedError(locked[0])
    override_periods(
        conn,
        locked,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        affected_ids=affected_ids or {},
        evidence=evidence,
    )
    return locked


def transaction_month(conn: sqlite3.Connection, transaction_id: int) -> str | None:
    row = conn.execute(
        "SELECT strftime('%Y-%m', posted_on) AS month FROM transactions WHERE id=?",
        (int(transaction_id),),
    ).fetchone()
    return None if row is None else str(row["month"])


def guard_transaction_write(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    override: bool = False,
    extra_month: str | None = None,
    actor: str = "",
    reason: str = "",
    operation_key: str = "",
    affected_ids: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> list[str]:
    months: list[str] = []
    own = transaction_month(conn, transaction_id)
    if own is not None:
        months.append(own)
    if extra_month is not None:
        months.append(extra_month)
    return guard_months(
        conn,
        months,
        override=override,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        affected_ids=affected_ids or {"transaction_id": int(transaction_id)},
        evidence=evidence,
    )


def guard_transaction_insert(
    conn: sqlite3.Connection,
    posted_on: str | date,
    *,
    override: bool = False,
    actor: str = "",
    reason: str = "",
    operation_key: str = "",
    affected_ids: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> list[str]:
    month = month_for_date(posted_on)
    return guard_months(
        conn,
        [month],
        override=override,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        affected_ids=affected_ids or {"posted_on": str(posted_on)},
        evidence=evidence,
    )


def acknowledge_exception(
    conn: sqlite3.Connection,
    exception_id: int,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Append an acknowledgement; this never resolves or changes close state."""
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")
    exception = conn.execute(
        "SELECT * FROM period_close_exceptions WHERE id=?",
        (int(exception_id),),
    ).fetchone()
    if exception is None:
        raise PeriodPolicyError(f"unknown close exception: {exception_id}")
    exception_token = _exception_token(_exception_from_row(exception))
    existing = _operation_row(
        conn,
        table="period_close_acknowledgements",
        operation_key=operation_key,
    )
    if existing is not None:
        _assert_existing_semantics(
            existing,
            {
                "exception_id": int(exception_id),
                "exception_token": exception_token,
                "event_kind": "acknowledged",
                "actor": actor,
                "reason": reason,
                "evidence_json": evidence_json,
                "evidence_digest": _digest(evidence_json),
            },
            operation_key=operation_key,
        )
        return existing
    cursor = conn.execute(
        """INSERT INTO period_close_acknowledgements(
             acknowledgement_key, exception_id, exception_token, event_kind,
             actor, reason, evidence_json, evidence_digest, operation_key
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("ack", operation_key),
            int(exception_id),
            exception_token,
            "acknowledged",
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_acknowledgements WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def withdraw_acknowledgement(
    conn: sqlite3.Connection,
    acknowledgement_id: int,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Reverse one acknowledgement with a compensating append-only row."""
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")
    acknowledgement = conn.execute(
        """SELECT *
           FROM period_close_acknowledgements
           WHERE id=? AND event_kind='acknowledged'""",
        (int(acknowledgement_id),),
    ).fetchone()
    if acknowledgement is None:
        raise PeriodPolicyError(
            f"unknown acknowledgement: {acknowledgement_id}"
        )
    if conn.execute(
        """SELECT 1
           FROM period_close_acknowledgements
           WHERE reverses_acknowledgement_id=?""",
        (int(acknowledgement_id),),
    ).fetchone() is not None:
        raise PeriodTransitionError("acknowledgement is already withdrawn")
    cursor = conn.execute(
        """INSERT INTO period_close_acknowledgements(
             acknowledgement_key, exception_id, exception_token, event_kind,
             actor, reason, evidence_json, evidence_digest, operation_key,
             reverses_acknowledgement_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("ack-withdrawal", operation_key),
            int(acknowledgement["exception_id"]),
            str(acknowledgement["exception_token"]),
            "withdrawn",
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
            int(acknowledgement_id),
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_acknowledgements WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def resolve_exception(
    conn: sqlite3.Connection,
    exception_id: int,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Append a resolution without relabeling the historical close snapshot."""
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")
    existing = _operation_row(
        conn,
        table="period_close_resolutions",
        operation_key=operation_key,
    )
    if existing is not None:
        _assert_existing_semantics(
            existing,
            {
                "exception_id": int(exception_id),
                "event_kind": "resolved",
                "actor": actor,
                "reason": reason,
                "evidence_json": evidence_json,
                "evidence_digest": _digest(evidence_json),
            },
            operation_key=operation_key,
        )
        return existing
    exception = conn.execute(
        "SELECT id FROM period_close_exceptions WHERE id=?",
        (int(exception_id),),
    ).fetchone()
    if exception is None:
        raise PeriodPolicyError(f"unknown close exception: {exception_id}")
    cursor = conn.execute(
        """INSERT INTO period_close_resolutions(
             resolution_key, exception_id, event_kind, actor, reason,
             evidence_json, evidence_digest, operation_key
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            _stable_key("resolution", operation_key),
            int(exception_id),
            "resolved",
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_resolutions WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def reverse_resolution(
    conn: sqlite3.Connection,
    resolution_id: int,
    *,
    actor: str,
    reason: str,
    operation_key: str,
    evidence: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Reverse a resolution without deleting or editing its evidence."""
    actor = _required_text(actor, field="actor", maximum=160)
    reason = _required_text(reason, field="reason", maximum=500)
    operation_key = _operation_key(operation_key)
    evidence_json = _json_object(evidence, field="evidence")
    resolution = conn.execute(
        """SELECT *
           FROM period_close_resolutions
           WHERE id=? AND event_kind='resolved'""",
        (int(resolution_id),),
    ).fetchone()
    if resolution is None:
        raise PeriodPolicyError(f"unknown resolution: {resolution_id}")
    if conn.execute(
        """SELECT 1
           FROM period_close_resolutions
           WHERE reverses_resolution_id=?""",
        (int(resolution_id),),
    ).fetchone() is not None:
        raise PeriodTransitionError("resolution is already reversed")
    cursor = conn.execute(
        """INSERT INTO period_close_resolutions(
             resolution_key, exception_id, event_kind, actor, reason,
             evidence_json, evidence_digest, operation_key,
             reverses_resolution_id
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            _stable_key("resolution-reversal", operation_key),
            int(resolution["exception_id"]),
            "reversed",
            actor,
            reason,
            evidence_json,
            _digest(evidence_json),
            operation_key,
            int(resolution_id),
        ),
    )
    row = conn.execute(
        "SELECT * FROM period_close_resolutions WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def _update_legacy_projection(
    conn: sqlite3.Connection,
    *,
    month: str,
    status: str,
    summary_json: str | None = None,
) -> None:
    conn.execute("INSERT OR IGNORE INTO closed_periods(month) VALUES (?)", (month,))
    if summary_json is None:
        conn.execute(
            """UPDATE closed_periods
               SET status=?, closed_at=NULL
               WHERE month=?""",
            (status, month),
        )
    else:
        conn.execute(
            """UPDATE closed_periods
               SET status=?, closed_at=CURRENT_TIMESTAMP, summary_json=?
               WHERE month=?""",
            (status, summary_json, month),
        )
