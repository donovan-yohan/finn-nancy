"""Capture identity, content-free telemetry, provenance, and transport policy."""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from typing import Any
from uuid import UUID, uuid5


TRANSPORT_DISCLOSURE_VERSION = "2026-07-26-v1"
ORIGIN_NAMESPACE = UUID("69e3f8c0-9b0f-4e4e-b0b8-d63a4b0e1f46")

TRANSPORTS: tuple[dict[str, str], ...] = (
    {
        "channel": "web",
        "label": "Web and camera",
        "classification": "local_only",
        "disclosure": "The original travels only between this browser and your tailnet service.",
    },
    {
        "channel": "share",
        "label": "Installed share target",
        "classification": "local_only",
        "disclosure": "The operating system hands the original to this installed local-first app.",
    },
    {
        "channel": "inbox",
        "label": "Local drop folder",
        "classification": "local_only",
        "disclosure": "The original stays on the host filesystem and local ingestion path.",
    },
    {
        "channel": "api",
        "label": "Programmatic API",
        "classification": "direct_network",
        "disclosure": (
            "The original crosses the authenticated caller-to-service network "
            "boundary without a third-party relay."
        ),
    },
    {
        "channel": "mcp",
        "label": "Local MCP process",
        "classification": "local_only",
        "disclosure": (
            "The original enters through the local MCP process bridge and local "
            "ingestion path."
        ),
    },
    {
        "channel": "telegram",
        "label": "Telegram",
        "classification": "third_party",
        "disclosure": (
            "Receipt files and status replies pass through Telegram infrastructure "
            "before reaching or returning from finn-nancy."
        ),
    },
)

_TRANSPORT_BY_CHANNEL = {item["channel"]: item for item in TRANSPORTS}
_SOURCES = {
    "camera",
    "file",
    "share",
    "shortcut",
    "web",
    "inbox",
    "telegram",
    "api",
    "mcp",
    "unspecified",
}
_CHANNELS = {"web", "share", "inbox", "telegram", "api", "mcp"}
_EVENT_KINDS = {
    "accepted",
    "durable",
    "durable_ack",
    "deduplicated",
    "processing",
    "processed",
    "retry",
    "terminal_failure",
    "offline_recovered",
}


def stable_origin_id(channel: str, occurrence_key: str) -> str:
    """Return an opaque, deterministic UUID for one channel-native occurrence."""
    normalized_channel = _normalize_channel(channel, {})
    if not occurrence_key:
        raise ValueError("capture occurrence key is required")
    return str(uuid5(ORIGIN_NAMESPACE, f"{normalized_channel}:{occurrence_key}"))


def normalize_proof_scope(value: object, field: str) -> str:
    """Accept only canonical UUIDs for privacy-safe proof cohort tags."""
    candidate = str(value or "").strip().lower()
    if not candidate:
        return ""
    try:
        parsed = UUID(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    canonical = str(parsed)
    if candidate != canonical:
        raise ValueError(f"{field} must be a canonical UUID")
    return canonical


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _normalized_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return _utc_now()
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return _utc_now()
    if parsed.tzinfo is None:
        return _utc_now()
    # A future client clock must not make a reliability duration negative.
    now = dt.datetime.now(dt.UTC)
    parsed = parsed.astimezone(dt.UTC)
    if parsed > now + dt.timedelta(minutes=5):
        return _utc_now()
    return parsed.replace(microsecond=0).isoformat()


def _normalized_source(source_metadata: dict | None, channel: str) -> str:
    value = str((source_metadata or {}).get("source") or "").strip().lower()
    if value and value not in _SOURCES:
        raise ValueError("capture source is not registered")
    if value in _SOURCES:
        return value
    if channel in _SOURCES:
        return channel
    raise ValueError("capture source is not registered")


def _normalize_channel(channel: str, source_metadata: dict | None) -> str:
    value = str(channel or "").strip().lower()
    source = str((source_metadata or {}).get("source") or "").strip().lower()
    if value == "web" and source == "share":
        return "share"
    if value in _CHANNELS:
        return value
    raise ValueError("capture channel is not registered")


def _transport_class(channel: str) -> str:
    item = _TRANSPORT_BY_CHANNEL.get(channel)
    if item is None:
        raise ValueError("capture channel is not registered")
    return item["classification"]


def get_submission(
    conn: sqlite3.Connection, client_capture_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM capture_submissions WHERE client_capture_id=?",
        (client_capture_id,),
    ).fetchone()


def append_event(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    source_document_id: int,
    event_kind: str,
    stage_key: str,
    occurred_at: str | None = None,
    duration_ms: int | None = None,
    attempt_no: int = 0,
    reason_code: str = "",
    event_key: str | None = None,
) -> bool:
    """Append one content-free event, idempotently by its stable event key."""
    if event_kind not in _EVENT_KINDS:
        raise ValueError(f"unsupported capture event: {event_kind}")
    normalized_stage = str(stage_key or "").strip().lower()
    if (
        not normalized_stage
        or len(normalized_stage) > 64
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for char in normalized_stage
        )
    ):
        raise ValueError("capture event stage_key is invalid")
    attempt_no = int(attempt_no)
    if not 0 <= attempt_no <= 1000:
        raise ValueError("capture event attempt_no must be between 0 and 1000")
    if duration_ms is not None:
        duration_ms = int(duration_ms)
        if not 0 <= duration_ms <= 604_800_000:
            raise ValueError("capture event duration_ms must be between 0 and 604800000")
    normalized_reason = str(reason_code or "").strip().lower()
    if (
        len(normalized_reason) > 64
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in normalized_reason)
    ):
        raise ValueError("capture event reason_code is invalid")
    key = event_key or (
        f"{capture_id}:{event_kind}:{normalized_stage}:"
        f"{attempt_no}:{normalized_reason or 'none'}"
    )
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO capture_events(
          event_key, capture_id, source_document_id, event_kind, stage_key,
          occurred_at, duration_ms, attempt_no, reason_code
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            capture_id,
            int(source_document_id),
            event_kind,
            normalized_stage,
            _normalized_timestamp(occurred_at),
            duration_ms,
            attempt_no,
            normalized_reason,
        ),
    )
    return cur.rowcount == 1


def insert_submission(
    conn: sqlite3.Connection,
    *,
    client_capture_id: str,
    source_document_id: int,
    sha256: str,
    channel: str,
    source_metadata: dict | None = None,
    dedup_kind: str = "new",
) -> None:
    """Commit one immutable origin and its accepted/durable evidence."""
    if dedup_kind not in {"new", "content"}:
        raise ValueError("new capture provenance must be new or content dedup")
    metadata = source_metadata or {}
    normalized_channel = _normalize_channel(channel, metadata)
    normalized_source = _normalized_source(metadata, normalized_channel)
    proof_run_id = normalize_proof_scope(
        metadata.get("proof_run_id"), "proof_run_id"
    )
    device_cohort_id = normalize_proof_scope(
        metadata.get("device_cohort_id"), "device_cohort_id"
    )
    if bool(proof_run_id) != bool(device_cohort_id):
        raise ValueError("proof_run_id and device_cohort_id must be supplied together")
    accepted_at = _normalized_timestamp(metadata.get("accepted_at"))
    durable_at = _utc_now()
    conn.execute(
        """
        INSERT INTO capture_submissions(
          client_capture_id, source_document_id, sha256, channel,
          source_metadata_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            client_capture_id,
            source_document_id,
            sha256,
            normalized_channel,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        ),
    )
    conn.execute(
        """
        INSERT INTO capture_provenance(
          capture_id, source_document_id, channel, source, transport_class,
          dedup_kind, proof_run_id, device_cohort_id, accepted_at, durable_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_capture_id,
            source_document_id,
            normalized_channel,
            normalized_source,
            _transport_class(normalized_channel),
            dedup_kind,
            proof_run_id,
            device_cohort_id,
            accepted_at,
            durable_at,
        ),
    )
    append_event(
        conn,
        capture_id=client_capture_id,
        source_document_id=source_document_id,
        event_kind="accepted",
        stage_key="capture",
        occurred_at=accepted_at,
    )
    if dedup_kind == "content":
        append_event(
            conn,
            capture_id=client_capture_id,
            source_document_id=source_document_id,
            event_kind="deduplicated",
            stage_key="capture",
            reason_code="content_hash",
        )
    append_event(
        conn,
        capture_id=client_capture_id,
        source_document_id=source_document_id,
        event_kind="durable",
        stage_key="capture",
        occurred_at=durable_at,
    )
    if metadata.get("client_attempts") is not None:
        record_client_attempts(
            conn,
            capture_id=client_capture_id,
            source_document_id=source_document_id,
            lifetime_attempts=int(metadata["client_attempts"]),
        )
    _record_existing_processed_state(conn, client_capture_id, source_document_id)


def _record_existing_processed_state(
    conn: sqlite3.Connection, capture_id: str, source_document_id: int
) -> None:
    row = conn.execute(
        """
        SELECT id, status, attempts, finished_at
        FROM jobs
        WHERE source_document_id=? AND type='ingest_document'
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_document_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] == "done":
        append_event(
            conn,
            capture_id=capture_id,
            source_document_id=source_document_id,
            event_kind="processed",
            stage_key=f"job_{int(row['id'])}",
            occurred_at=row["finished_at"],
            attempt_no=int(row["attempts"] or 0),
            reason_code="existing_document",
        )
    elif row["status"] == "dead":
        append_event(
            conn,
            capture_id=capture_id,
            source_document_id=source_document_id,
            event_kind="terminal_failure",
            stage_key=f"job_{int(row['id'])}",
            occurred_at=row["finished_at"],
            attempt_no=int(row["attempts"] or 0),
            reason_code="existing_document",
        )


def record_replay(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    source_document_id: int,
) -> None:
    count = conn.execute(
        """
        SELECT COUNT(*)
        FROM capture_events
        WHERE capture_id=? AND event_kind='deduplicated'
          AND reason_code='id_replay'
        """,
        (capture_id,),
    ).fetchone()[0]
    attempt_no = min(int(count) + 1, 1000)
    append_event(
        conn,
        capture_id=capture_id,
        source_document_id=source_document_id,
        event_kind="deduplicated",
        stage_key="capture_replay",
        attempt_no=attempt_no,
        reason_code="id_replay",
    )


def record_client_attempts(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    source_document_id: int,
    lifetime_attempts: int,
) -> int:
    """Append the missing lifetime client retry ordinals exactly once."""
    lifetime_attempts = int(lifetime_attempts)
    if not 1 <= lifetime_attempts <= 1000:
        raise ValueError("client_attempts must be between 1 and 1000")
    row = conn.execute(
        """
        SELECT MAX(attempt_no)
        FROM capture_events
        WHERE capture_id=?
          AND event_kind='retry'
          AND stage_key='client_delivery'
          AND reason_code='client_retry'
        """,
        (capture_id,),
    ).fetchone()
    recorded = int(row[0] or 0)
    inserted = 0
    for retry_ordinal in range(recorded + 1, lifetime_attempts):
        inserted += int(
            append_event(
                conn,
                capture_id=capture_id,
                source_document_id=source_document_id,
                event_kind="retry",
                stage_key="client_delivery",
                attempt_no=retry_ordinal,
                reason_code="client_retry",
            )
        )
    return inserted


def record_client_event(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    client_event: str,
    duration_ms: int,
    sequence_no: int,
) -> bool:
    row = get_submission(conn, capture_id)
    if row is None or row["source_document_id"] is None:
        raise LookupError("capture not found")
    mapping = {
        "online_durable_ack": (
            "durable_ack",
            "client_delivery",
            "online_request",
        ),
        "offline_recovered": (
            "offline_recovered",
            "client_recovery",
            "device_reconnected",
        ),
    }
    resolved = mapping.get(client_event)
    if resolved is None:
        raise ValueError("unsupported client capture event")
    event_kind, stage_key, reason_code = resolved
    return append_event(
        conn,
        capture_id=capture_id,
        source_document_id=int(row["source_document_id"]),
        event_kind=event_kind,
        stage_key=stage_key,
        duration_ms=duration_ms,
        attempt_no=sequence_no,
        reason_code=reason_code,
    )


def record_document_event(
    conn: sqlite3.Connection,
    *,
    source_document_id: int | None,
    event_kind: str,
    stage_key: str,
    attempt_no: int = 0,
    reason_code: str = "",
) -> int:
    if source_document_id is None:
        return 0
    rows = conn.execute(
        """
        SELECT capture_id
        FROM capture_provenance
        WHERE source_document_id=?
        ORDER BY created_at, capture_id
        """,
        (source_document_id,),
    ).fetchall()
    inserted = 0
    for row in rows:
        inserted += int(
            append_event(
                conn,
                capture_id=row["capture_id"],
                source_document_id=source_document_id,
                event_kind=event_kind,
                stage_key=stage_key,
                attempt_no=attempt_no,
                reason_code=reason_code,
            )
        )
    return inserted


def record_transport_decision(
    conn: sqlite3.Connection,
    *,
    transport: str,
    decision: str,
    disclosure_version: str = TRANSPORT_DISCLOSURE_VERSION,
) -> int:
    if transport != "telegram":
        raise ValueError("only third-party transports require consent")
    if decision not in {"consented", "revoked"}:
        raise ValueError("transport decision must be consented or revoked")
    cur = conn.execute(
        """
        INSERT INTO capture_transport_consents(
          transport, disclosure_version, decision
        )
        VALUES (?, ?, ?)
        """,
        (transport, disclosure_version, decision),
    )
    return int(cur.lastrowid)


def current_transport_consent(
    conn: sqlite3.Connection,
    transport: str,
    *,
    disclosure_version: str = TRANSPORT_DISCLOSURE_VERSION,
) -> bool:
    row = conn.execute(
        """
        SELECT decision, disclosure_version
        FROM capture_transport_consents
        WHERE transport=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (transport,),
    ).fetchone()
    return bool(
        row
        and row["decision"] == "consented"
        and row["disclosure_version"] == disclosure_version
    )


def transport_allowed(
    conn: sqlite3.Connection,
    transport: str,
    *,
    strict_local_mode: bool,
) -> bool:
    item = _TRANSPORT_BY_CHANNEL.get(transport)
    if item is None:
        return False
    if item["classification"] != "third_party":
        return True
    if strict_local_mode:
        return False
    return current_transport_consent(conn, transport)


def transport_settings(
    conn: sqlite3.Connection,
    *,
    strict_local_mode: bool,
    configured_channels: set[str] | None = None,
    running_channels: set[str] | None = None,
) -> list[dict[str, Any]]:
    configured_channels = configured_channels or set()
    running_channels = running_channels or set()
    settings = []
    for item in TRANSPORTS:
        channel = item["channel"]
        consented = (
            current_transport_consent(conn, channel)
            if item["classification"] == "third_party"
            else True
        )
        allowed = transport_allowed(
            conn, channel, strict_local_mode=strict_local_mode
        )
        configured = (
            channel in configured_channels
            if item["classification"] == "third_party"
            else True
        )
        running = (
            channel in running_channels
            if item["classification"] == "third_party"
            else True
        )
        settings.append(
            {
                **item,
                "consented": consented,
                "allowed": allowed,
                "configured": configured,
                "running": running,
                "enabled": allowed and configured and running,
                "disclosure_version": (
                    TRANSPORT_DISCLOSURE_VERSION
                    if item["classification"] == "third_party"
                    else ""
                ),
            }
        )
    return settings


def provenance_for_document(
    conn: sqlite3.Connection, source_document_id: int
) -> list[dict[str, str]]:
    """Return display-safe provenance without capture IDs or source metadata."""
    rows = conn.execute(
        """
        SELECT channel, source, transport_class, accepted_at
        FROM capture_provenance
        WHERE source_document_id=?
        ORDER BY accepted_at, capture_id
        """,
        (source_document_id,),
    ).fetchall()
    return [
        {
            "channel": row["channel"],
            "source": row["source"],
            "transport_class": row["transport_class"],
            "transport_label": (
                _TRANSPORT_BY_CHANNEL.get(row["channel"], {}).get("label")
                or row["channel"].replace("_", " ").title()
            ),
            "classification_label": (
                "Local-only"
                if row["transport_class"] == "local_only"
                else (
                    "Direct network"
                    if row["transport_class"] == "direct_network"
                    else "Third-party"
                )
            ),
            "accepted_at": row["accepted_at"],
        }
        for row in rows
    ]


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _duration_summary(values: list[int]) -> dict[str, int | None]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p90_ms": _percentile(values, 0.90),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values) if values else None,
    }


def capture_metrics(
    conn: sqlite3.Connection,
    *,
    proof_run_id: str = "",
    device_cohort_id: str = "",
) -> dict[str, Any]:
    proof_run_id = normalize_proof_scope(proof_run_id, "proof_run_id")
    device_cohort_id = normalize_proof_scope(
        device_cohort_id, "device_cohort_id"
    )
    if bool(proof_run_id) != bool(device_cohort_id):
        raise ValueError("proof_run_id and device_cohort_id must be supplied together")
    scope = (
        "cp.proof_run_id=:proof_run_id "
        "AND cp.device_cohort_id=:device_cohort_id"
        if proof_run_id
        else "1=1"
    )
    params = {
        "proof_run_id": proof_run_id,
        "device_cohort_id": device_cohort_id,
    }
    counts = {
        row["event_kind"]: int(row["n"])
        for row in conn.execute(
            f"""
            SELECT ce.event_kind, COUNT(*) AS n
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE {scope}
            GROUP BY ce.event_kind
            """,
            params,
        ).fetchall()
    }
    accepted = int(
        conn.execute(
            f"SELECT COUNT(*) FROM capture_provenance cp WHERE {scope}",
            params,
        ).fetchone()[0]
    )
    durable = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT ce.capture_id)
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE ce.event_kind='durable' AND {scope}
            """,
            params,
        ).fetchone()[0]
    )
    content_dedup = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM capture_provenance cp
            WHERE cp.dedup_kind='content' AND {scope}
            """,
            params,
        ).fetchone()[0]
    )
    replay_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE ce.event_kind='deduplicated'
              AND ce.reason_code='id_replay'
              AND {scope}
            """,
            params,
        ).fetchone()[0]
    )
    client_retry_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE ce.event_kind='retry'
              AND ce.stage_key='client_delivery'
              AND ce.reason_code='client_retry'
              AND {scope}
            """,
            params,
        ).fetchone()[0]
    )
    accepted_to_processed = [
        max(0, int(round(float(row["duration_ms"]))))
        for row in conn.execute(
            f"""
            WITH accepted AS (
              SELECT ce.capture_id, MIN(ce.occurred_at) AS at
              FROM capture_events ce
              JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
              WHERE ce.event_kind='accepted' AND {scope}
              GROUP BY ce.capture_id
            ),
            processed AS (
              SELECT capture_id, MIN(occurred_at) AS at
              FROM capture_events
              WHERE event_kind='processed'
              GROUP BY capture_id
            )
            SELECT (julianday(processed.at) - julianday(accepted.at))
                     * 86400000.0 AS duration_ms
            FROM accepted
            JOIN processed USING (capture_id)
            WHERE accepted.at IS NOT NULL AND processed.at IS NOT NULL
            """,
            params,
        ).fetchall()
        if row["duration_ms"] is not None
    ]
    online_durable_ack = [
        int(row["duration_ms"])
        for row in conn.execute(
            f"""
            SELECT ce.duration_ms
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE ce.event_kind='durable_ack'
              AND ce.reason_code='online_request'
              AND ce.duration_ms IS NOT NULL
              AND {scope}
            """,
            params,
        ).fetchall()
    ]
    offline_recovery = [
        int(row["duration_ms"])
        for row in conn.execute(
            f"""
            SELECT ce.duration_ms
            FROM capture_events ce
            JOIN capture_provenance cp ON cp.capture_id=ce.capture_id
            WHERE ce.event_kind='offline_recovered'
              AND ce.duration_ms IS NOT NULL
              AND {scope}
            """,
            params,
        ).fetchall()
    ]
    current_terminal_failures = int(
        conn.execute(
            f"""
            WITH latest_ingest AS (
              SELECT source_document_id, MAX(id) AS job_id
              FROM jobs
              WHERE type='ingest_document' AND source_document_id IS NOT NULL
              GROUP BY source_document_id
            )
            SELECT COUNT(*)
            FROM capture_provenance cp
            JOIN latest_ingest latest
              ON latest.source_document_id=cp.source_document_id
            JOIN jobs j ON j.id=latest.job_id
            WHERE j.status='dead' AND {scope}
            """,
            params,
        ).fetchone()[0]
    )
    review_or_filed = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM capture_provenance cp
            JOIN source_documents sd ON sd.id=cp.source_document_id
            WHERE {scope}
              AND (
                sd.status='needs_review'
                OR EXISTS(
                  SELECT 1
                  FROM transactions t
                  WHERE t.source_document_id=cp.source_document_id
                )
              )
            """,
            params,
        ).fetchone()[0]
    )
    by_channel = {
        str(row["channel"]): int(row["n"])
        for row in conn.execute(
            f"""
            SELECT cp.channel, COUNT(*) AS n
            FROM capture_provenance cp
            WHERE {scope}
            GROUP BY cp.channel
            """,
            params,
        ).fetchall()
    }
    by_source = {
        str(row["source"]): int(row["n"])
        for row in conn.execute(
            f"""
            SELECT cp.source, COUNT(*) AS n
            FROM capture_provenance cp
            WHERE {scope}
            GROUP BY cp.source
            """,
            params,
        ).fetchall()
    }
    return {
        "scope": {
            "proof_run_id": proof_run_id,
            "device_cohort_id": device_cohort_id,
        },
        "accepted": accepted,
        "durable": durable,
        "durability_pct": (100.0 * durable / accepted) if accepted else None,
        "processed": counts.get("processed", 0),
        "retry_count": counts.get("retry", 0),
        "client_retry_count": client_retry_count,
        "content_dedup_count": content_dedup,
        "replay_count": replay_count,
        "dedup_count": content_dedup + replay_count,
        "terminal_failures": current_terminal_failures,
        "terminal_failure_events": counts.get("terminal_failure", 0),
        "review_or_filed": review_or_filed,
        "by_channel": by_channel,
        "by_source": by_source,
        "accepted_to_processed": _duration_summary(accepted_to_processed),
        "online_durable_ack": _duration_summary(online_durable_ack),
        "offline_recovery": _duration_summary(offline_recovery),
    }


def submission_status(
    conn: sqlite3.Connection, client_capture_id: str
) -> dict | None:
    row = conn.execute(
        """
        SELECT
          cs.client_capture_id,
          cs.source_document_id,
          cs.sha256,
          cs.channel,
          cs.source_metadata_json,
          cs.stored_at,
          cp.transport_class,
          sd.kind AS document_kind,
          sd.status AS document_status,
          EXISTS(
            SELECT 1
            FROM transactions t
            WHERE t.source_document_id=cs.source_document_id
          ) AS has_ledger_transaction,
          (
            SELECT COUNT(*)
            FROM statement_lines sl
            WHERE sl.source_document_id=cs.source_document_id
              AND sl.match_status IN ('unmatched', 'needs_review')
              AND sl.review_disposition='active'
          ) AS unresolved_statement_lines,
          (
            SELECT j.status
            FROM jobs j
            WHERE j.source_document_id=cs.source_document_id
              AND j.type IN ('ingest_document', 'reconcile_document')
            ORDER BY j.id DESC
            LIMIT 1
          ) AS job_status
        FROM capture_submissions cs
        LEFT JOIN capture_provenance cp
          ON cp.capture_id=cs.client_capture_id
        LEFT JOIN source_documents sd ON sd.id=cs.source_document_id
        WHERE cs.client_capture_id=?
        """,
        (client_capture_id,),
    ).fetchone()
    if row is None:
        return None

    document_status = row["document_status"]
    job_status = row["job_status"]
    if row["source_document_id"] is None:
        server_state = "failed"
    elif job_status in {"error", "dead"}:
        server_state = "failed"
    elif document_status == "needs_review":
        server_state = "needs_review"
    elif (
        document_status == "matched"
        and int(row["unresolved_statement_lines"] or 0) == 0
    ):
        server_state = "logged"
    elif (
        document_status == "processed"
        and row["document_kind"] == "receipt"
        and bool(row["has_ledger_transaction"])
    ):
        server_state = "logged"
    elif job_status == "running":
        server_state = "processing"
    elif (
        row["document_kind"] == "statement"
        and document_status == "processed"
        and job_status == "pending"
    ):
        server_state = "processing"
    elif document_status in {"processed", "matched"}:
        server_state = (
            "needs_review"
            if row["document_kind"] == "statement"
            else "failed"
        )
    else:
        server_state = "queued"

    try:
        source_metadata = json.loads(row["source_metadata_json"] or "{}")
    except (TypeError, ValueError):
        source_metadata = {}
    if not isinstance(source_metadata, dict):
        source_metadata = {}

    return {
        "client_capture_id": row["client_capture_id"],
        "source_document_id": row["source_document_id"],
        "sha256": row["sha256"],
        "channel": row["channel"],
        "transport_class": row["transport_class"] or "local_only",
        "source_metadata": source_metadata,
        "stored_at": row["stored_at"],
        "durable": True,
        "device_state": "saved",
        "server_state": server_state,
    }
