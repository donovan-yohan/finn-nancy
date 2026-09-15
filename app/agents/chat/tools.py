"""Read-only LangChain tools for the finance chat agent."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.db import (
    engine,
    repo_actions,
    repo_embeddings,
    repo_ledger,
    repo_merchant_knowledge,
    repo_rag,
)
from app.reconcile.merchant_resolution import resolve_descriptor

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_QUERY_LIMIT = 50

QUERY_NAMES = {
    "monthly_cashflow",
    "spend_by_category",
    "category_trend",
    "budget_vs_actual",
    "top_merchants",
    "recent_transactions",
    "leisure_vs_bigticket",
    "runway",
}

QUERY_PARAM_ALLOWLIST: dict[str, set[str]] = {
    "monthly_cashflow": {"start_month", "end_month", "limit"},
    "spend_by_category": {"month", "limit"},
    "category_trend": {"category_id", "category", "start_month", "end_month", "limit"},
    "budget_vs_actual": {"month", "limit"},
    "top_merchants": {"month", "limit"},
    "recent_transactions": {"start_date", "end_date", "merchant", "category_id", "category", "limit"},
    "leisure_vs_bigticket": {"start_month", "end_month", "limit"},
    "runway": {"limit"},
}


class QueryFinancesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Literal[
        "monthly_cashflow",
        "spend_by_category",
        "category_trend",
        "budget_vs_actual",
        "top_merchants",
        "recent_transactions",
        "leisure_vs_bigticket",
        "runway",
    ] = Field(description="Named query to run (pinned vocabulary; no arbitrary SQL).")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Parameters object. Months use YYYY-MM. Dates use YYYY-MM-DD. Supported keys depend on query."
        ),
    )


class SearchHistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Plain-language terms to search in approved transaction history.")
    limit: int = Field(default=8, description="Maximum rows to return; capped at 25.")


class ExplainCategorizationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: int = Field(description="Approved ledger transaction id to explain.")


class ProposeRecategorizationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: int = Field(description="Approved ledger transaction id to recategorize.")
    category_name: str = Field(description="Existing category name to move the transaction to.")
    rationale: str = Field(description="Brief reason for the proposed recategorization.")


@contextmanager
def read_only_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    with engine.read_conn(db_path, read_only=True) as conn:
        ensure_read_only_connection(conn)
        yield conn


def ensure_read_only_connection(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA query_only").fetchone()
    if int(row[0]) != 1:
        raise RuntimeError("chat tools require a query_only connection")


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    }


def _limit(value: Any, default: int = 12) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, MAX_QUERY_LIMIT))


def _valid_month(value: str) -> bool:
    return bool(MONTH_RE.match(value)) and 1 <= int(value[-2:]) <= 12


def _latest_month(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(month) AS month FROM v_month_spine").fetchone()
    return row["month"] or ""


def _validate_common(query: str, params: dict[str, Any]) -> str | None:
    if query not in QUERY_NAMES:
        return f"Unknown finance query '{query}'. Use one of: {', '.join(sorted(QUERY_NAMES))}."
    unknown = sorted(set(params) - QUERY_PARAM_ALLOWLIST[query])
    if unknown:
        return f"Unsupported parameter(s) for {query}: {', '.join(unknown)}."
    for key in ("month", "start_month", "end_month"):
        if key in params and not _valid_month(str(params[key])):
            return f"Parameter {key} must be YYYY-MM."
    for key in ("start_date", "end_date"):
        if key in params and not DATE_RE.match(str(params[key])):
            return f"Parameter {key} must be YYYY-MM-DD."
    return None


def _category_filter(params: dict[str, Any]) -> tuple[str, list[Any]]:
    if "category_id" in params:
        try:
            return " AND category_id = ?", [int(params["category_id"])]
        except (TypeError, ValueError):
            return " AND 0", []
    if "category" in params:
        return " AND category_name LIKE ?", [f"%{params['category']}%"]
    return "", []


def query_finances(db_path: str, query: str, **params: Any) -> dict[str, Any]:
    """Run a named read-only finance query over curated reporting views."""
    params = _clean_params(params)
    error = _validate_common(query, params)
    if error is not None:
        return {"ok": False, "error": error, "rows": []}

    with read_only_conn(db_path) as conn:
        limit = _limit(params.get("limit"))
        if query == "monthly_cashflow":
            where = []
            args: list[Any] = []
            if "start_month" in params:
                where.append("month >= ?")
                args.append(params["start_month"])
            if "end_month" in params:
                where.append("month <= ?")
                args.append(params["end_month"])
            sql = "SELECT * FROM v_cashflow_monthly_trend"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY month DESC LIMIT ?"
            rows = conn.execute(sql, (*args, limit)).fetchall()
            return {"ok": True, "query": query, "params": params, "rows": _rows(rows)}

        if query == "spend_by_category":
            month = str(params.get("month") or _latest_month(conn))
            rows = conn.execute(
                """
                SELECT month, category_id, category_name, category_kind, brand_owner, color,
                       amount_cents, magnitude_cents, prev_magnitude_cents,
                       magnitude_delta_cents, magnitude_pct_change
                FROM v_report_category_monthly_trend
                WHERE month = ?
                  AND category_kind = 'expense'
                ORDER BY magnitude_cents DESC, category_name
                LIMIT ?
                """,
                (month, limit),
            ).fetchall()
            return {"ok": True, "query": query, "params": {**params, "month": month}, "rows": _rows(rows)}

        if query == "category_trend":
            where = ["1=1"]
            args = []
            category_sql, category_args = _category_filter(params)
            if category_sql:
                where.append(category_sql.removeprefix(" AND "))
                args.extend(category_args)
            if "start_month" in params:
                where.append("month >= ?")
                args.append(params["start_month"])
            if "end_month" in params:
                where.append("month <= ?")
                args.append(params["end_month"])
            rows = conn.execute(
                f"""
                SELECT *
                FROM v_report_category_monthly_trend
                WHERE {' AND '.join(where)}
                ORDER BY month DESC, magnitude_cents DESC, category_name
                LIMIT ?
                """,
                (*args, limit),
            ).fetchall()
            return {"ok": True, "query": query, "params": params, "rows": _rows(rows)}

        if query == "budget_vs_actual":
            month = str(params.get("month") or _latest_month(conn))
            rows = conn.execute(
                """
                SELECT *
                FROM v_budget_vs_actual
                WHERE month = ?
                ORDER BY ABS(remaining_cents) DESC, category_name
                LIMIT ?
                """,
                (month, limit),
            ).fetchall()
            return {"ok": True, "query": query, "params": {**params, "month": month}, "rows": _rows(rows)}

        if query == "top_merchants":
            month = str(params.get("month") or _latest_month(conn))
            rows = conn.execute(
                """
                SELECT *
                FROM v_top_merchants
                WHERE month = ?
                ORDER BY merchant_rank
                LIMIT ?
                """,
                (month, limit),
            ).fetchall()
            return {"ok": True, "query": query, "params": {**params, "month": month}, "rows": _rows(rows)}

        if query == "recent_transactions":
            where = ["1=1"]
            args = []
            if "start_date" in params:
                where.append("t.posted_on >= ?")
                args.append(params["start_date"])
            if "end_date" in params:
                where.append("t.posted_on <= ?")
                args.append(params["end_date"])
            if "merchant" in params:
                where.append("(t.counterparty LIKE ? OR t.description LIKE ?)")
                args.extend([f"%{params['merchant']}%", f"%{params['merchant']}%"])
            if "category_id" in params:
                try:
                    where.append("c.id = ?")
                    args.append(int(params["category_id"]))
                except (TypeError, ValueError):
                    return {"ok": False, "error": "Parameter category_id must be an integer.", "rows": []}
            if "category" in params:
                where.append("c.name LIKE ?")
                args.append(f"%{params['category']}%")
            rows = conn.execute(
                f"""
                SELECT
                  t.id, t.posted_on, a.name AS account_name, t.description, t.counterparty,
                  t.amount_cents, GROUP_CONCAT(DISTINCT c.name) AS categories
                FROM transactions t
                JOIN accounts a ON a.id = t.account_id
                LEFT JOIN transaction_splits s ON s.transaction_id = t.id
                LEFT JOIN categories c ON c.id = s.category_id
                WHERE {' AND '.join(where)}
                GROUP BY t.id
                ORDER BY t.posted_on DESC, t.id DESC
                LIMIT ?
                """,
                (*args, limit),
            ).fetchall()
            return {"ok": True, "query": query, "params": params, "rows": _rows(rows)}

        if query == "leisure_vs_bigticket":
            where = []
            args = []
            if "start_month" in params:
                where.append("month >= ?")
                args.append(params["start_month"])
            if "end_month" in params:
                where.append("month <= ?")
                args.append(params["end_month"])
            sql = "SELECT * FROM v_leisure_vs_bigticket"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY month DESC LIMIT ?"
            rows = conn.execute(sql, (*args, limit)).fetchall()
            return {"ok": True, "query": query, "params": params, "rows": _rows(rows)}

        rows = conn.execute(
            """
            SELECT *
            FROM v_cashflow_runway
            ORDER BY month DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"ok": True, "query": query, "params": params, "rows": _rows(rows)}


def search_history(db_path: str, query: str, limit: int = 8) -> dict[str, Any]:
    """Search approved transaction chunks with safe FTS5 BM25 matching."""
    with read_only_conn(db_path) as conn:
        return repo_rag.search_history(conn, query, limit)


def _category_rows(conn: sqlite3.Connection, transaction_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          s.id AS split_id,
          s.category_id,
          c.name,
          c.kind,
          s.amount_cents,
          s.memo
        FROM transaction_splits s
        JOIN categories c ON c.id = s.category_id
        WHERE s.transaction_id = ?
        ORDER BY s.id
        """,
        (transaction_id,),
    ).fetchall()
    return _rows(rows)


def _linked_extraction(conn: sqlite3.Connection, txn: sqlite3.Row) -> dict[str, Any] | None:
    if txn["source_document_id"] is None:
        row = conn.execute(
            """
            SELECT *
            FROM ingest_extractions
            WHERE transaction_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (txn["id"],),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ingest_extractions
            WHERE transaction_id = ?
               OR source_document_id = ?
            ORDER BY transaction_id = ? DESC, id DESC
            LIMIT 1
            """,
            (txn["id"], txn["source_document_id"], txn["id"]),
        ).fetchone()
    if row is None:
        return None
    category_guess = ""
    try:
        extracted = json.loads(row["extracted_json"] or "{}")
        if isinstance(extracted, dict):
            category_guess = str(extracted.get("category_guess") or "")
    except json.JSONDecodeError:
        pass
    return {
        "id": row["id"],
        "source_document_id": row["source_document_id"],
        "review_status": row["review_status"],
        "confidence": row["confidence"],
        "category_guess": category_guess,
    }


def _knowledge_signal(
    conn: sqlite3.Connection,
    *,
    transaction_id: int,
    merchant: str,
    current_category_ids: set[int],
) -> dict[str, Any]:
    scope = repo_merchant_knowledge.scope_for_transaction(
        conn,
        transaction_id,
    )
    resolution = resolve_descriptor(
        conn,
        descriptor=merchant,
        scope=scope,
    )
    merchant_resolution = asdict(resolution.merchant)
    category_resolution = asdict(resolution.category)
    merchant_resolution["claim_ids"] = list(merchant_resolution["claim_ids"])
    category_resolution["claim_ids"] = list(category_resolution["claim_ids"])
    return {
        "normalization_version": resolution.normalization_version,
        "descriptor_fingerprint": resolution.descriptor_fingerprint,
        "normalized_tokens": list(resolution.normalized_tokens),
        "knowledge_version": resolution.knowledge_version,
        "merchant": merchant_resolution,
        "category": category_resolution,
        "matches_current_category": (
            resolution.category.status == "resolved"
            and resolution.category.target_id is not None
            and int(resolution.category.target_id) in current_category_ids
        ),
    }


def _guess_signal(
    conn: sqlite3.Connection,
    *,
    extraction: dict[str, Any] | None,
    current_category_ids: set[int],
) -> dict[str, Any]:
    guess = str((extraction or {}).get("category_guess") or "").strip()
    if not guess:
        return {"category_guess": "", "category": None, "matches_current_category": False}
    category = repo_ledger.find_category_by_name(conn, guess)
    category_dict = _row_dict(category) if category is not None else None
    return {
        "category_guess": guess,
        "category": category_dict,
        "matches_current_category": (
            category is not None
            and category["kind"] == "expense"
            and int(category["id"]) in current_category_ids
        ),
    }


def _statement_signal(conn: sqlite3.Connection, transaction_id: int, source: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          id,
          source_document_id,
          match_status,
          match_method,
          match_score,
          match_rationale
        FROM statement_lines
        WHERE matched_transaction_id = ?
          AND review_disposition='active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (transaction_id,),
    ).fetchone()
    return {
        "is_statement_transaction": source == "statement",
        "line": _row_dict(row) if row is not None else None,
    }


def _deciding_signal(
    *,
    txn: sqlite3.Row,
    categories: list[dict[str, Any]],
    knowledge_signal: dict[str, Any],
    guess_signal: dict[str, Any],
    extraction_signal: dict[str, Any] | None,
    statement_signal: dict[str, Any],
) -> dict[str, Any]:
    del guess_signal, extraction_signal
    category_names = {str(category["name"]) for category in categories}
    category_knowledge = knowledge_signal.get("category") or {}
    if (
        knowledge_signal.get("matches_current_category")
        and category_knowledge.get("status") == "resolved"
        and category_knowledge.get("trust_state") == "human_confirmed"
    ):
        return {
            "method": "accepted_category_claim",
            "confidence": 1.0,
            "claim_ids": category_knowledge.get("claim_ids", []),
            "explanation": (
                "accepted category evidence at the applicable merchant scope "
                "matches the current split"
            ),
        }
    if txn["source"] == "statement" and (statement_signal.get("line") or {}).get("match_status") == "promoted":
        return {
            "method": "unresolved_statement_category",
            "confidence": 0.0,
            "explanation": (
                "statement promotion recorded the expense event, but no accepted "
                "category claim explains the current split"
            ),
        }
    if category_names & {"Uncategorized", "Uncategorized Income"}:
        return {
            "method": "unresolved",
            "confidence": 0.0,
            "explanation": (
                "no accepted category claim explains the expense; model guesses "
                "and proposals do not decide the ledger category"
            ),
        }
    return {
        "method": "assigned_without_accepted_evidence",
        "confidence": 0.0,
        "explanation": (
            "the split has a category assignment, but no accepted category claim "
            "currently supports it"
        ),
    }


def explain_categorization(db_path: str, transaction_id: int) -> dict[str, Any]:
    """Return the deterministic evidence bundle for why a transaction has its category."""
    with read_only_conn(db_path) as conn:
        txn = conn.execute(
            """
            SELECT
              t.id,
              t.posted_on,
              t.description,
              t.counterparty,
              t.amount_cents,
              t.source,
              t.source_document_id,
              t.source_confidence,
              t.recon_status,
              t.cleared_on,
              a.name AS account_name
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if txn is None:
            return {"ok": False, "error": "transaction not found", "transaction_id": transaction_id}
        categories = _category_rows(conn, transaction_id)
        current_category_ids = {int(category["category_id"]) for category in categories}
        merchant = txn["counterparty"] or txn["description"]
        extraction = _linked_extraction(conn, txn)
        knowledge = _knowledge_signal(
            conn,
            transaction_id=transaction_id,
            merchant=merchant,
            current_category_ids=current_category_ids,
        )
        guess = _guess_signal(conn, extraction=extraction, current_category_ids=current_category_ids)
        statement = _statement_signal(conn, transaction_id, txn["source"])
        try:
            similar = repo_embeddings.similar_transactions(conn, txn_id=transaction_id, k=5)
        except Exception:  # noqa: BLE001 - explanation should still return core evidence
            similar = []
        deciding = _deciding_signal(
            txn=txn,
            categories=categories,
            knowledge_signal=knowledge,
            guess_signal=guess,
            extraction_signal=extraction,
            statement_signal=statement,
        )
        return {
            "ok": True,
            "transaction": {
                "id": txn["id"],
                "posted_on": txn["posted_on"],
                "merchant": merchant,
                "description": txn["description"],
                "counterparty": txn["counterparty"],
                "amount_cents": txn["amount_cents"],
                "source": txn["source"],
                "source_confidence": txn["source_confidence"],
                "account_name": txn["account_name"],
                "recon_status": txn["recon_status"],
                "cleared_on": txn["cleared_on"],
                "categories": categories,
            },
            "classifier_order": [
                "accepted_category_claim",
                "model_proposal",
                "unresolved",
            ],
            "deciding_signal": deciding,
            "knowledge_signal": knowledge,
            "extraction_signal": extraction,
            "guess_signal": guess,
            "statement_signal": statement,
            "similar_transactions": similar,
        }


def propose_recategorization(
    db_path: str,
    *,
    thread_id: str,
    transaction_id: int,
    category_name: str,
    rationale: str,
) -> dict[str, Any]:
    """Queue a recategorization proposal for human approval; never edits the ledger."""
    category_name = (category_name or "").strip()
    if not category_name:
        return {"ok": False, "error": "category_name is required"}
    explanation = explain_categorization(db_path, transaction_id)
    if not explanation.get("ok"):
        return explanation
    try:
        with engine.write_tx(db_path) as conn:
            category = repo_ledger.find_category_by_name(conn, category_name)
            if category is None:
                return {"ok": False, "error": f"Unknown category '{category_name}'. Choose an existing category."}
            neighbor_ids = [
                int(row["transaction_id"])
                for row in explanation.get("similar_transactions", [])
                if row.get("transaction_id") is not None
            ]
            evidence = {
                "transaction_ids": [transaction_id],
                "category_ids": [int(category["id"])],
                "similar_transaction_ids": neighbor_ids,
                "transaction": explanation.get("transaction", {}),
                "deciding_signal": explanation.get("deciding_signal", {}),
                "knowledge_signal": explanation.get("knowledge_signal", {}),
                "extraction_signal": explanation.get("extraction_signal", {}),
                "statement_signal": explanation.get("statement_signal", {}),
            }
            proposal_id = repo_actions.enqueue_proposal(
                conn,
                kind="recategorization",
                payload={"transaction_id": transaction_id, "to_category_id": int(category["id"])},
                evidence=evidence,
                confidence=0.7,
                rationale=(rationale or "").strip(),
                agent_run_id=f"chat:{thread_id}",
            )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": "queued for approval",
        "approval_url": "/actions",
        "transaction_id": transaction_id,
        "to_category": {"id": int(category["id"]), "name": category["name"]},
    }


def list_budgets(db_path: str) -> dict[str, Any]:
    """Return configured budgets with actuals for the latest ledger month."""
    with read_only_conn(db_path) as conn:
        month = _latest_month(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM v_budget_vs_actual
            WHERE month = ?
            ORDER BY brand_owner, category_name
            """,
            (month,),
        ).fetchall()
        return {"ok": True, "month": month, "rows": _rows(rows)}


def list_goals(db_path: str) -> dict[str, Any]:
    """Return goals and deterministic progress rows."""
    with read_only_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM v_goal_progress
            ORDER BY status = 'active' DESC, goal_id
            """
        ).fetchall()
        return {"ok": True, "rows": _rows(rows)}


def get_recurring_insights(db_path: str) -> dict[str, Any]:
    """Return recurring payment deltas and subscription watchlist candidates."""
    with read_only_conn(db_path) as conn:
        deltas = conn.execute(
            """
            SELECT *
            FROM v_recurring_payment_deltas
            WHERE is_meaningful_delta = 1
            ORDER BY month DESC, ABS(amount_delta_cents) DESC, merchant
            LIMIT 12
            """
        ).fetchall()
        subscriptions = conn.execute(
            """
            SELECT *
            FROM v_subscription_watchlist_candidates
            ORDER BY last_month DESC, estimated_amount_cents DESC, merchant
            LIMIT 12
            """
        ).fetchall()
        return {
            "ok": True,
            "recurring_payment_deltas": _rows(deltas),
            "subscription_watchlist_candidates": _rows(subscriptions),
        }


def make_tools(db_path: str, *, thread_id: str = "") -> list[StructuredTool]:
    def _query_finances(query: str, params: dict[str, Any] | None = None) -> str:
        """Run one named finance query. Put filters in params, never ask for arbitrary SQL."""
        return _json(query_finances(db_path, query, **_clean_params(params)))

    def _search_history(query: str, limit: int = 8) -> str:
        """Search approved transaction history by merchant, month name, notes, category, or receipt item text."""
        return _json(search_history(db_path, query, limit))

    def _list_budgets() -> str:
        """List budget rows and actual spend for the latest ledger month."""
        return _json(list_budgets(db_path))

    def _list_goals() -> str:
        """List financial goals with progress, remaining amount, and status."""
        return _json(list_goals(db_path))

    def _get_recurring_insights() -> str:
        """List recurring payment increases/decreases and subscription watchlist candidates."""
        return _json(get_recurring_insights(db_path))

    def _explain_categorization(transaction_id: int) -> str:
        """Explain why a transaction has its current category, with concrete evidence."""
        return _json(explain_categorization(db_path, transaction_id))

    def _propose_recategorization(transaction_id: int, category_name: str, rationale: str) -> str:
        """Queue a recategorization proposal for human approval; never edit the ledger directly."""
        return _json(
            propose_recategorization(
                db_path,
                thread_id=thread_id,
                transaction_id=transaction_id,
                category_name=category_name,
                rationale=rationale,
            )
        )

    return [
        StructuredTool.from_function(
            func=_query_finances,
            name="query_finances",
            description=(
                "Typed read-only catalog over finance views. query must be one of: "
                f"{', '.join(sorted(QUERY_NAMES))}. Use params for month/date/category/merchant/limit filters."
            ),
            args_schema=QueryFinancesArgs,
        ),
        StructuredTool.from_function(
            func=_search_history,
            name="search_history",
            description="FTS5 search over approved ledger transaction chunks. Good for fuzzy recall.",
            args_schema=SearchHistoryArgs,
        ),
        StructuredTool.from_function(func=_list_budgets, name="list_budgets"),
        StructuredTool.from_function(func=_list_goals, name="list_goals"),
        StructuredTool.from_function(func=_get_recurring_insights, name="get_recurring_insights"),
        StructuredTool.from_function(
            func=_explain_categorization,
            name="explain_categorization",
            description=(
                "Read-only evidence bundle for why a ledger transaction has its current category. "
                "Use for any 'why was this categorized' question."
            ),
            args_schema=ExplainCategorizationArgs,
        ),
        StructuredTool.from_function(
            func=_propose_recategorization,
            name="propose_recategorization",
            description=(
                "Queue a recategorization proposal for human approval. The category_name must already exist; "
                "this never edits the ledger directly."
            ),
            args_schema=ProposeRecategorizationArgs,
        ),
    ]
