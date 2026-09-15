"""Persistence helpers for versioned structured-statement preview/confirm."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


TERMINAL_STATUSES = {"confirmed", "duplicate", "rejected"}


def get_import(conn: sqlite3.Connection, import_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM structured_statement_imports WHERE id=?",
        (int(import_id),),
    ).fetchone()


def get_for_document(
    conn: sqlite3.Connection, source_document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM structured_statement_imports
           WHERE source_document_id=?
           ORDER BY id DESC LIMIT 1""",
        (int(source_document_id),),
    ).fetchone()


def rows_for_import(
    conn: sqlite3.Connection, import_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM structured_statement_import_rows
           WHERE import_id=? ORDER BY source_row_number""",
        (int(import_id),),
    ).fetchall()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def create_preview(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    account_id: int,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    mapping_version: str,
    mapping: dict[str, Any],
    provider_identity_hash: str,
    account_last4: str,
    period_start_on: str,
    period_end_on: str,
    statement_issued_on: str,
    currency: str,
    opening_balance_cents: int | None,
    closing_balance_cents: int | None,
    manual_fields: list[str] | tuple[str, ...],
    row_count: int,
    status: str,
    review_reasons: list[str] | tuple[str, ...],
    diagnostics: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    actor: str,
) -> sqlite3.Row:
    latest = get_for_document(conn, source_document_id)
    configuration = {
        "account_id": int(account_id),
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "mapping_version": mapping_version,
        "mapping": mapping,
        "provider_identity_hash": provider_identity_hash,
        "account_last4": account_last4,
        "period_start_on": period_start_on or None,
        "period_end_on": period_end_on or None,
        "statement_issued_on": statement_issued_on or None,
        "currency": currency,
        "opening_balance_cents": opening_balance_cents,
        "closing_balance_cents": closing_balance_cents,
        "manual_fields": sorted(set(manual_fields)),
        "row_count": int(row_count),
        "review_reasons": list(review_reasons),
        "diagnostics": list(diagnostics),
    }
    config_fingerprint = hashlib.sha256(
        _json(configuration).encode()
    ).hexdigest()
    existing = conn.execute(
        """SELECT * FROM structured_statement_imports
           WHERE source_document_id=? AND config_fingerprint=?""",
        (int(source_document_id), config_fingerprint),
    ).fetchone()
    if existing is not None:
        return existing
    attempt_number = int(latest["attempt_number"]) + 1 if latest is not None else 1
    cur = conn.execute(
        """INSERT INTO structured_statement_imports(
             source_document_id, supersedes_import_id, attempt_number,
             config_fingerprint, account_id, source_sha256,
             adapter_id, adapter_version, mapping_version, mapping_json,
             provider_identity_hash, account_last4,
             period_start_on, period_end_on, statement_issued_on, currency,
             opening_balance_cents, closing_balance_cents, manual_fields_json,
             row_count, status, review_reasons_json, diagnostics_json
           )
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(source_document_id),
            int(latest["id"]) if latest is not None else None,
            attempt_number,
            config_fingerprint,
            int(account_id),
            source_sha256,
            adapter_id,
            adapter_version,
            mapping_version,
            _json(mapping),
            provider_identity_hash,
            account_last4,
            period_start_on or None,
            period_end_on or None,
            statement_issued_on or None,
            currency,
            opening_balance_cents,
            closing_balance_cents,
            _json(sorted(set(manual_fields))),
            int(row_count),
            status,
            _json(list(review_reasons)),
            _json(list(diagnostics)),
        ),
    )
    import_id = int(cur.lastrowid)
    current = get_import(conn, import_id)
    assert current is not None
    conn.execute(
        """INSERT INTO structured_statement_import_audit(
             operation_key, import_id, event_kind,
             old_values_json, new_values_json, actor, reason
           )
           VALUES (?,?,?,?,?,?,?)""",
        (
            f"structured:{import_id}:preview:{int(current['revision'])}",
            import_id,
            "preview_created",
            _json(
                {
                    "supersedes_import_id": (
                        int(latest["id"]) if latest is not None else None
                    )
                }
            ),
            _json(
                {
                    "status": str(current["status"]),
                    "revision": int(current["revision"]),
                    "row_count": int(current["row_count"]),
                    "review_reasons": list(review_reasons),
                    "manual_fields": sorted(set(manual_fields)),
                }
            ),
            actor,
            "versioned structured statement preview",
        ),
    )
    return current


def finish_import(
    conn: sqlite3.Connection,
    *,
    import_id: int,
    expected_revision: int,
    statement_review_id: int | None,
    status: str,
    overlap_kind: str,
    staged_count: int,
    duplicate_count: int,
    review_reasons: list[str] | tuple[str, ...],
    actor: str,
    reason: str,
) -> sqlite3.Row:
    current = get_import(conn, import_id)
    if current is None:
        raise ValueError("structured import not found")
    if str(current["status"]) in TERMINAL_STATUSES:
        return current
    if int(current["revision"]) != int(expected_revision):
        raise ValueError("structured import revision conflict")
    terminal = status in {"confirmed", "duplicate"}
    if status == "confirmed" and statement_review_id is None:
        raise ValueError("confirmed structured import requires a statement review")
    cur = conn.execute(
        """UPDATE structured_statement_imports
           SET statement_review_id=?, status=?, overlap_kind=?,
               staged_count=?, duplicate_count=?,
               review_reasons_json=?, revision=revision+1,
               evaluated_at=CURRENT_TIMESTAMP,
               confirmed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
               confirmed_by=CASE WHEN ? THEN ? ELSE NULL END,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND revision=?""",
        (
            int(statement_review_id) if statement_review_id is not None else None,
            status,
            overlap_kind,
            int(staged_count),
            int(duplicate_count),
            _json(list(review_reasons)),
            int(terminal),
            int(terminal),
            actor,
            int(import_id),
            int(expected_revision),
        ),
    )
    if cur.rowcount != 1:
        raise ValueError("structured import revision conflict")
    updated = get_import(conn, import_id)
    assert updated is not None
    event_kind = {
        "confirmed": "confirmed",
        "duplicate": "duplicate_confirmed",
        "needs_review": "confirmation_blocked",
    }[status]
    conn.execute(
        """INSERT INTO structured_statement_import_audit(
             operation_key, import_id, event_kind,
             old_values_json, new_values_json, actor, reason
           )
           VALUES (?,?,?,?,?,?,?)""",
        (
            f"structured:{import_id}:confirm:{int(updated['revision'])}",
            int(import_id),
            event_kind,
            _json(
                {
                    "status": str(current["status"]),
                    "revision": int(current["revision"]),
                }
            ),
            _json(
                {
                    "status": str(updated["status"]),
                    "revision": int(updated["revision"]),
                    "overlap_kind": str(updated["overlap_kind"]),
                    "staged_count": int(updated["staged_count"]),
                    "duplicate_count": int(updated["duplicate_count"]),
                }
            ),
            actor,
            reason,
        ),
    )
    return updated
