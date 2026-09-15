"""Framework-neutral programmatic API service layer."""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

from ..accounting.contract import FlowKind
from ..accounting.flows import normalize_flow_kind, validate_flow_amount
from ..config import get_settings
from ..db import engine, repo_documents, repo_ledger, repo_recon_coverage
from ..ingest.storage import capture
from ..reporting.exports import (
    render_period_statement_csv,
    render_period_statement_pdf,
)
from ..reporting.period_statements import build_period_statement


REPORTS = {
    "coverage",
    "budget_vs_actual",
    "recurring_deltas",
    "goals_progress",
    "monthly_spend_by_category",
}


def _row_to_dict(row) -> dict:
    return dict(row)


def _clamp_limit(limit: int) -> int:
    return max(1, min(500, int(limit)))


def _valid_date(value: str) -> str:
    try:
        return dt.date.fromisoformat((value or "").strip()).isoformat()
    except ValueError as exc:
        raise ValueError("posted_on must be a valid ISO date") from exc


def _resolve_category(conn, category: str | None) -> tuple[int, str, str, bool]:
    requested = (category or "").strip()
    row = repo_ledger.find_category_by_name(conn, requested) if requested else None
    fallback = row is None
    if row is None:
        category_id = repo_ledger.ensure_uncategorized(conn)
        row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    return int(row["id"]), str(row["name"]), str(row["kind"]), fallback


def _validate_deferred_amount_sign(
    amount_cents: int,
    category_name: str,
    category_kind: str,
    flow_kind: FlowKind,
) -> None:
    """Preserve the legacy sign guard when callers omit semantic meaning.

    An explicit refund/reimbursement/reversal tag may legitimately be positive
    in an expense-purpose category. ``unknown`` callers retain the safer prior
    contract and must opt into the offset flow instead of silently changing it.
    """
    if flow_kind != FlowKind.UNKNOWN:
        return
    if category_kind == "expense" and amount_cents > 0:
        raise ValueError(
            f"amount_cents must be negative for expense category '{category_name}' "
            f"when flow_kind is unknown; provide refund, reimbursement, or reversal "
            f"explicitly, or supply an income category explicitly (got {amount_cents})"
        )
    if category_kind == "income" and amount_cents < 0:
        raise ValueError(
            f"amount_cents must be positive for income category '{category_name}' "
            f"when flow_kind is unknown (got {amount_cents})"
        )


def _statement_line_counts(conn, doc_id: int) -> dict:
    rows = conn.execute(
        """
        SELECT match_status, COUNT(*) AS n
        FROM statement_lines
        WHERE source_document_id=? AND review_disposition='active'
        GROUP BY match_status
        """,
        (doc_id,),
    ).fetchall()
    by_status = {row["match_status"]: int(row["n"]) for row in rows}
    total = sum(by_status.values())
    matched = sum(by_status.get(status, 0) for status in ("matched", "promoted"))
    unmatched = sum(by_status.get(status, 0) for status in ("unmatched", "needs_review"))
    return {"total": total, "matched": matched, "unmatched": unmatched, "by_status": by_status}


def ingest_bytes(
    *,
    raw: bytes,
    filename: str,
    channel: str,
    declared_mime: str | None = None,
) -> dict:
    result = capture(raw=raw, original_name=filename, channel=channel, declared_mime=declared_mime)
    status = result["status"]
    return {
        "doc_id": result.get("source_document_id"),
        "job_id": result.get("job_id"),
        "status": status,
        "sha256": result["sha256"],
        "kind": result.get("kind"),
        "duplicate": status == "duplicate",
    }


def record_transaction(
    *,
    amount_cents: int,
    posted_on: str,
    description: str,
    counterparty: str = "",
    category: str | None = None,
    account_id: int | None = None,
    card_last4: str | None = None,
    external_id: str | None = None,
    source: str = "api",
    source_confidence: float = 1.0,
    notes: str = "",
    flow_kind: FlowKind | str = FlowKind.UNKNOWN,
) -> dict:
    """Record a transaction and one matching split.

    Retries: pass a stable external_id to make the call idempotent; without one
    every call creates a new transaction. Category names must already exist;
    omitted or unmatched names fall back to Uncategorized and the response
    reports that fallback. Category purpose and flow semantics are orthogonal.
    ``flow_kind='unknown'`` explicitly defers ambiguous meaning into the durable
    review queue.
    """
    amount_cents = int(amount_cents)
    if amount_cents == 0:
        raise ValueError("amount_cents must be non-zero")
    posted_on = _valid_date(posted_on)
    flow = validate_flow_amount(flow_kind, amount_cents)

    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        resolved_account_id: int | None = None
        if account_id is not None:
            row = conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown account_id: {account_id}")
            resolved_account_id = int(row["id"])
        else:
            resolved_account_id = repo_ledger.ensure_default_account(conn, card_last4)

        if external_id is None:
            external_id = f"api:{uuid4().hex}"

        category_id, resolved_category_name, category_kind, category_fallback = _resolve_category(
            conn, category
        )
        _validate_deferred_amount_sign(
            amount_cents,
            resolved_category_name,
            category_kind,
            flow,
        )

        response_meta = {
            "account_id": resolved_account_id,
            "category_id": category_id,
            "category_name": resolved_category_name,
            "category_fallback": category_fallback,
            "flow_kind": flow.value,
        }

        txn_id = repo_ledger.insert_transaction(
            conn,
            account_id=resolved_account_id,
            posted_on=posted_on,
            description=description,
            counterparty=counterparty,
            amount_cents=amount_cents,
            source=source,
            external_id=external_id,
            source_document_id=None,
            source_confidence=source_confidence,
            flow_kind=flow,
            notes=notes,
        )
        if txn_id is None:
            existing_rows = conn.execute(
                """SELECT txn.id, txn.account_id, txn.posted_on,
                          txn.description, txn.counterparty, txn.amount_cents,
                          txn.flow_kind, split.amount_cents AS split_amount_cents,
                          category.id AS category_id,
                          category.name AS category_name
                   FROM transactions txn
                   LEFT JOIN transaction_splits split
                     ON split.transaction_id=txn.id
                   LEFT JOIN categories category
                     ON category.id=split.category_id
                   WHERE txn.source=? AND txn.external_id=?
                   ORDER BY split.id""",
                (source, external_id),
            ).fetchall()
            if (
                len(existing_rows) != 1
                or existing_rows[0]["category_id"] is None
                or existing_rows[0]["split_amount_cents"] is None
            ):
                raise ValueError(
                    "external_id exists without exactly one persisted category split"
                )
            existing = existing_rows[0]
            expected = (
                resolved_account_id,
                posted_on,
                description,
                counterparty,
                amount_cents,
                flow.value,
                category_id,
                amount_cents,
            )
            observed = (
                int(existing["account_id"]),
                existing["posted_on"],
                existing["description"],
                existing["counterparty"],
                int(existing["amount_cents"]),
                normalize_flow_kind(existing["flow_kind"]).value,
                int(existing["category_id"]),
                int(existing["split_amount_cents"]),
            )
            if observed != expected:
                raise ValueError(
                    "external_id already exists with different transaction semantics"
                )
            persisted_meta = {
                "account_id": int(existing["account_id"]),
                "category_id": int(existing["category_id"]),
                "category_name": str(existing["category_name"]),
                "category_fallback": str(existing["category_name"]) == "Uncategorized",
                "flow_kind": normalize_flow_kind(existing["flow_kind"]).value,
            }
            return {
                "transaction_id": int(existing["id"]),
                "created": False,
                "status": "existing",
                "external_id": external_id,
                **persisted_meta,
            }

        repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=category_id,
            amount_cents=amount_cents,
            memo=description,
        )
        return {
            "transaction_id": txn_id,
            "created": True,
            "status": "created",
            "external_id": external_id,
            **response_meta,
        }


def document_status(*, doc_id: int) -> dict | None:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        doc = repo_documents.get_document(conn, doc_id)
        if doc is None:
            return None

        transaction_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM transactions WHERE source_document_id=? ORDER BY id",
                (doc_id,),
            ).fetchall()
        ]
        extractions = [
            {
                "review_status": row["review_status"],
                "confidence": row["confidence"],
                "external_id": row["external_id"],
                "transaction_id": row["transaction_id"],
                "doc_kind": row["doc_kind"],
            }
            for row in conn.execute(
                """SELECT review_status, confidence, external_id, transaction_id, doc_kind
                   FROM ingest_extractions
                   WHERE source_document_id=?
                   ORDER BY id""",
                (doc_id,),
            ).fetchall()
        ]
        job_row = conn.execute(
            """SELECT type, status, attempts, last_error
               FROM jobs
               WHERE source_document_id=?
               ORDER BY id DESC
               LIMIT 1""",
            (doc_id,),
        ).fetchone()

        out: dict[str, Any] = {
            "doc_id": int(doc["id"]),
            "kind": doc["kind"],
            "status": doc["status"],
            "sha256": doc["sha256"],
            "mime_type": doc["mime_type"],
            "original_name": doc["original_name"],
            "created_at": doc["created_at"],
            "transaction_ids": transaction_ids,
            "extractions": extractions,
            "job": _row_to_dict(job_row) if job_row is not None else None,
        }
        if doc["kind"] == "statement":
            counts = _statement_line_counts(conn, doc_id)
            out["statement_lines"] = {
                "total": counts["total"],
                "matched": counts["matched"],
                "unmatched": counts["unmatched"],
            }
        return out


def _latest_month(conn, view: str) -> str | None:
    if view == "v_report_budget_vs_actual":
        sql = "SELECT MAX(month) AS month FROM v_report_budget_vs_actual"
        params: tuple = ()
    elif view == "v_report_category_monthly":
        sql = (
            "SELECT MAX(month) AS month FROM v_report_category_monthly "
            "WHERE category_kind='expense'"
        )
        params = ()
    else:  # pragma: no cover - guarded by fixed callers
        raise ValueError("unsupported month view")
    row = conn.execute(sql, params).fetchone()
    view_month = row["month"] if row is not None else None
    txn_row = conn.execute(
        "SELECT MAX(substr(posted_on, 1, 7)) AS month FROM transactions"
    ).fetchone()
    txn_month = txn_row["month"] if txn_row is not None else None
    return max(item for item in (view_month, txn_month) if item is not None) \
        if view_month is not None or txn_month is not None else None


def _semantic_completeness(conn, month: str | None) -> dict:
    if month is None:
        return {
            "complete": None,
            "excluded_unknown_count": 0,
            "excluded_missing_relationship_count": 0,
            "excluded_transaction_count": 0,
            "transaction_count": 0,
            "category_resolution": {
                "money_out_cents": 0,
                "resolved_expense_cents": 0,
                "excluded_expense_cents": 0,
                "resolved_split_count": 0,
                "unresolved_split_count": 0,
            },
            "scope": "not_applicable",
        }
    row = conn.execute(
        """SELECT COUNT(*) AS total_count,
                  COALESCE(SUM(
                    CASE WHEN flow_status.semantic_status='unknown' THEN 1 ELSE 0 END
                  ), 0) AS unknown_count,
                  COALESCE(SUM(
                    CASE WHEN flow_status.semantic_status='missing_relationship'
                      THEN 1 ELSE 0 END
                  ), 0) AS missing_relationship_count
           FROM transactions txn
           JOIN v_transaction_flow_status flow_status
             ON flow_status.transaction_id=txn.id
           WHERE txn.posted_on >= ? AND txn.posted_on < date(?, '+1 month')""",
        (f"{month}-01", f"{month}-01"),
    ).fetchone()
    unknown_count = int(row["unknown_count"])
    missing_relationship_count = int(row["missing_relationship_count"])
    blocked_count = unknown_count + missing_relationship_count
    category_row = conn.execute(
        """
        SELECT
          money_out_cents,
          resolved_expense_cents,
          excluded_expense_cents,
          resolved_split_count,
          unresolved_split_count
        FROM v_expense_resolution_monthly_control
        WHERE month=?
        """,
        (month,),
    ).fetchone()
    category_resolution = {
        "money_out_cents": int(category_row["money_out_cents"])
        if category_row is not None
        else 0,
        "resolved_expense_cents": int(category_row["resolved_expense_cents"])
        if category_row is not None
        else 0,
        "excluded_expense_cents": int(category_row["excluded_expense_cents"])
        if category_row is not None
        else 0,
        "resolved_split_count": int(category_row["resolved_split_count"])
        if category_row is not None
        else 0,
        "unresolved_split_count": int(category_row["unresolved_split_count"])
        if category_row is not None
        else 0,
    }
    unresolved_category_count = category_resolution["unresolved_split_count"]
    notice_parts: list[str] = []
    if blocked_count:
        notice_parts.append(
            f"{blocked_count} transaction(s) with unknown flow or missing "
            "provenance are excluded from financial totals until reviewed"
        )
    if unresolved_category_count:
        notice_parts.append(
            f"{unresolved_category_count} purchase/fee split(s) without accepted "
            "category evidence are excluded from the resolved expense breakdown"
        )
    return {
        "complete": blocked_count == 0 and unresolved_category_count == 0,
        "excluded_unknown_count": unknown_count,
        "excluded_missing_relationship_count": missing_relationship_count,
        "excluded_transaction_count": blocked_count,
        "transaction_count": int(row["total_count"]),
        "category_resolution": category_resolution,
        "scope": month,
        "notice": (
            "; ".join(notice_parts) + "."
            if notice_parts
            else (
                "All transactions have typed accounting flow and every "
                "purchase/fee split has accepted category evidence."
            )
        ),
    }


def run_report(*, report: str, month: str | None = None, limit: int = 50) -> dict:
    limit = _clamp_limit(limit)
    if report not in REPORTS:
        valid = ", ".join(sorted(REPORTS))
        raise ValueError(f"unknown report: {report} (valid: {valid})")

    settings = get_settings()
    resolved_month = month
    with engine.read_conn(settings.db_path) as conn:
        if report == "coverage":
            rows = conn.execute(
                "SELECT * FROM v_statement_coverage_by_doc ORDER BY last_posted_on DESC LIMIT ?",
                (limit,),
            ).fetchall()
            resolved_month = None
        elif report == "budget_vs_actual":
            if resolved_month is None:
                resolved_month = _latest_month(
                    conn,
                    "v_report_budget_vs_actual",
                )
            rows = (
                conn.execute(
                    """SELECT * FROM v_report_budget_vs_actual
                       WHERE month=?
                       ORDER BY category_name
                       LIMIT ?""",
                    (resolved_month, limit),
                ).fetchall()
                if resolved_month is not None
                else []
            )
        elif report == "recurring_deltas":
            if resolved_month is None:
                rows = conn.execute(
                    "SELECT * FROM v_recurring_payment_deltas WHERE is_meaningful_delta=1 LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM v_recurring_payment_deltas
                       WHERE is_meaningful_delta=1 AND month=?
                       LIMIT ?""",
                    (resolved_month, limit),
                ).fetchall()
        elif report == "goals_progress":
            rows = conn.execute(
                "SELECT * FROM v_goal_progress ORDER BY pct_complete DESC LIMIT ?",
                (limit,),
            ).fetchall()
            resolved_month = None
        else:
            if resolved_month is None:
                resolved_month = _latest_month(
                    conn,
                    "v_report_category_monthly",
                )
            rows = (
                conn.execute(
                    """SELECT * FROM v_report_category_monthly
                       WHERE category_kind='expense' AND month=?
                       ORDER BY magnitude_cents DESC
                       LIMIT ?""",
                    (resolved_month, limit),
                ).fetchall()
                if resolved_month is not None
                else []
            )
        semantic_completeness = _semantic_completeness(conn, resolved_month)

    row_dicts = [_row_to_dict(row) for row in rows]
    return {
        "report": report,
        "month": resolved_month,
        "rows": row_dicts,
        "count": len(row_dicts),
        "semantic_completeness": semantic_completeness,
    }


def get_period_statement(*, month: str) -> dict:
    """Return the canonical untruncated FN-148 JSON model."""
    settings = get_settings()
    statement = build_period_statement(
        settings.db_path,
        month=month,
        home_currency=settings.home_currency,
    )
    return statement.model_dump(mode="json")


def export_period_statement(
    *,
    month: str,
    export_format: str,
) -> tuple[bytes, str, str]:
    """Render CSV/PDF from the same canonical model returned by the JSON API."""
    settings = get_settings()
    statement = build_period_statement(
        settings.db_path,
        month=month,
        home_currency=settings.home_currency,
    )
    normalized = str(export_format).strip().lower()
    if normalized == "csv":
        return (
            render_period_statement_csv(statement),
            "text/csv; charset=utf-8",
            f"finn-nancy-{month}.csv",
        )
    if normalized == "pdf":
        return (
            render_period_statement_pdf(statement),
            "application/pdf",
            f"finn-nancy-{month}.pdf",
        )
    raise ValueError("format must be csv or pdf")


def reconcile_status(*, doc_id: int | None = None) -> dict:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        if doc_id is not None:
            row = conn.execute(
                "SELECT * FROM v_statement_coverage_by_doc WHERE source_document_id=?",
                (doc_id,),
            ).fetchone()
            return {
                "doc_id": doc_id,
                "document": _row_to_dict(row) if row is not None else None,
                "statement_lines": _statement_line_counts(conn, doc_id),
            }

        dashboard = repo_recon_coverage.coverage_dashboard(conn)
        documents = conn.execute(
            """SELECT *
               FROM v_statement_coverage_by_doc
               ORDER BY last_posted_on DESC, source_document_id DESC
               LIMIT ?""",
            (50,),
        ).fetchall()
        return {
            "summary": dashboard["summary"],
            "documents": [_row_to_dict(row) for row in documents],
        }
