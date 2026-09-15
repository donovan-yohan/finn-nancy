"""Suggest categories for the unresolved expense-evidence backlog.

Vote rule:
1. Retrieve neighbors with ``similar_transactions(conn, txn_id=txn.id, k=k)``.
   This uses the stored-vector path when embeddings are enabled and lets the
   repository fall back to FTS without making a new embedding call.
2. A neighbor votes only when its current expense splits collapse to exactly
   one category backed by an active, accepted split-level category claim.
3. Weighted vote: vector neighbors contribute ``max(score, 0.0)``; FTS
   neighbors contribute ``1.0`` because BM25 magnitudes are not comparable with
   vector scores. Sum weights per candidate. Pick the largest total, breaking
   ties by highest single-neighbor score and then category name.
4. Confidence is ``winner_weight / sum(all_voting_weights)`` multiplied by
   ``min(1.0, n_supporting_neighbors / 3.0)`` and clamped to ``[0, 1]``.
5. If no neighbor winner reaches the confidence threshold, fall back to the
   classifier order: accepted scoped category knowledge, then one structured
   LLM guess against existing expense category names, then no suggestion.
   Every result remains a proposal: FN-149B grants neither accepted knowledge
   nor a model response automatic ledger-write authority.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..db import repo_embeddings, repo_ledger, repo_merchant_knowledge
from ..reconcile.merchant_resolution import resolve_descriptor
from .schemas import BacklogCategoryGuess

NEIGHBOR_CONFIDENCE_THRESHOLD = 0.34
TRUSTED_KNOWLEDGE_CONFIDENCE = 1.0
LLM_DEFAULT_CONFIDENCE = 0.4


@dataclass(frozen=True)
class Suggestion:
    transaction_id: int
    suggested_category_id: int | None
    suggested_category_name: str
    confidence: float
    method: str
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    vote_breakdown: dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    knowledge_claim_ids: tuple[int, ...] = ()
    knowledge_version: str = ""


@dataclass
class _Vote:
    category_id: int
    category_name: str
    weight: float
    best_score: float
    supporters: list[dict[str, Any]] = field(default_factory=list)


def _row_value(row: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            value = getattr(row, key, None)
        if value is not None:
            return value
    return default


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _merchant(transaction: Any) -> str:
    return str(
        _row_value(transaction, "counterparty", "merchant", default="")
        or _row_value(transaction, "description", default="")
        or ""
    )


def _neighbor_label(neighbor: dict[str, Any]) -> str:
    merchant = str(neighbor.get("merchant") or neighbor.get("description") or "transaction").strip()
    txn_id = neighbor.get("transaction_id")
    return f"{merchant} #{txn_id}" if txn_id is not None else merchant


def _example_neighbors(neighbors: list[dict[str, Any]], *, limit: int = 3) -> str:
    labels = [_neighbor_label(neighbor) for neighbor in neighbors[:limit]]
    return ", ".join(label for label in labels if label)


def _expense_category_by_id(conn: sqlite3.Connection, category_id: int | None) -> sqlite3.Row | None:
    if category_id is None:
        return None
    row = conn.execute("SELECT * FROM categories WHERE id=?", (int(category_id),)).fetchone()
    if row is None or row["kind"] != "expense" or row["name"] == "Uncategorized":
        return None
    return row


def _resolved_neighbor_category(
    conn: sqlite3.Connection,
    neighbor: dict[str, Any],
) -> sqlite3.Row | None:
    try:
        transaction_id = int(neighbor["transaction_id"])
    except (KeyError, TypeError, ValueError):
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT
          resolved.category_id AS id,
          resolved.category_name AS name,
          category.kind
        FROM v_transaction_split_category_resolution resolved
        JOIN transaction_splits split
          ON split.id = resolved.transaction_split_id
         AND split.transaction_id = resolved.transaction_id
         AND split.category_id = resolved.category_id
        JOIN categories category ON category.id = resolved.category_id
        WHERE resolved.transaction_id = ?
          AND category.kind = 'expense'
          AND category.name <> 'Uncategorized'
        ORDER BY resolved.category_id
        """,
        (transaction_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return rows[0]


def _neighbor_weight(neighbor: dict[str, Any]) -> float:
    if neighbor.get("source") == "fts":
        return 1.0
    try:
        return max(float(neighbor.get("score") or 0.0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _neighbor_score(neighbor: dict[str, Any]) -> float:
    try:
        return float(neighbor.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _neighbor_vote(
    conn: sqlite3.Connection,
    *,
    transaction_id: int,
    neighbors: list[dict[str, Any]],
) -> Suggestion | None:
    votes: dict[int, _Vote] = {}
    for neighbor in neighbors:
        category = _resolved_neighbor_category(conn, neighbor)
        if category is None:
            continue
        category_id = int(category["id"])
        weight = _neighbor_weight(neighbor)
        score = _neighbor_score(neighbor)
        vote = votes.setdefault(
            category_id,
            _Vote(
                category_id=category_id,
                category_name=str(category["name"]),
                weight=0.0,
                best_score=score,
            ),
        )
        vote.weight += weight
        vote.best_score = max(vote.best_score, score)
        if weight > 0.0:
            vote.supporters.append(neighbor)

    total_weight = sum(vote.weight for vote in votes.values())
    if total_weight <= 0.0:
        return None

    winner = sorted(
        votes.values(),
        key=lambda vote: (-vote.weight, -vote.best_score, vote.category_name.lower()),
    )[0]
    supporting = len(winner.supporters)
    confidence = _clamp_confidence(
        (winner.weight / total_weight) * min(1.0, supporting / 3.0)
    )
    if confidence < NEIGHBOR_CONFIDENCE_THRESHOLD:
        return None

    examples = _example_neighbors(winner.supporters)
    voting_count = sum(len(vote.supporters) for vote in votes.values())
    rationale = (
        f"{supporting} of {voting_count} similar approved txns"
        f"{f' (e.g. {examples})' if examples else ''} are {winner.category_name}."
    )
    return Suggestion(
        transaction_id=transaction_id,
        suggested_category_id=winner.category_id,
        suggested_category_name=winner.category_name,
        confidence=confidence,
        method="neighbor_vote",
        neighbors=neighbors,
        vote_breakdown={vote.category_name: vote.weight for vote in votes.values()},
        rationale=rationale,
    )


def _fallback_context(neighbors: list[dict[str, Any]]) -> str:
    examples = _example_neighbors(neighbors)
    if not examples:
        return "Neighbor evidence was inconclusive."
    return f"Neighbor evidence was inconclusive (e.g. {examples})."


def _knowledge_suggestion(
    conn: sqlite3.Connection,
    *,
    transaction_id: int,
    transaction: Any,
    neighbors: list[dict[str, Any]],
    vote_breakdown: dict[str, float],
) -> Suggestion | None:
    descriptor = _merchant(transaction).strip()
    if not descriptor:
        return None
    scope = repo_merchant_knowledge.scope_for_transaction(conn, transaction_id)
    resolution = resolve_descriptor(
        conn,
        descriptor=descriptor,
        scope=scope,
    )
    category_resolution = resolution.category
    if category_resolution.status != "resolved":
        return None
    category = _expense_category_by_id(conn, category_resolution.target_id)
    if category is None:
        return None
    return Suggestion(
        transaction_id=transaction_id,
        suggested_category_id=int(category["id"]),
        suggested_category_name=str(category["name"]),
        confidence=TRUSTED_KNOWLEDGE_CONFIDENCE,
        method="trusted_knowledge",
        neighbors=neighbors,
        vote_breakdown=vote_breakdown,
        rationale=(
            f"{_fallback_context(neighbors)} Accepted scoped category knowledge "
            f"supports {category['name']}; ledger assignment still requires approval."
        ),
        knowledge_claim_ids=category_resolution.claim_ids,
        knowledge_version=resolution.knowledge_version,
    )


def _existing_expense_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM categories
        WHERE kind='expense' AND name <> 'Uncategorized'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _read_guess_field(result: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(field_name, default)
    return getattr(result, field_name, default)


def _llm_suggestion(
    conn: sqlite3.Connection,
    *,
    transaction_id: int,
    transaction: Any,
    neighbors: list[dict[str, Any]],
    vote_breakdown: dict[str, float],
    llm: Any,
) -> Suggestion | None:
    category_names = _existing_expense_names(conn)
    if not category_names:
        return None
    settings = get_settings()
    structured = llm.with_structured_output(
        BacklogCategoryGuess,
        method=settings.structured_method,
    )
    messages = [
        (
            "system",
            "Choose one existing expense category for this personal-finance transaction. "
            "Return an empty category_name if none fits. Never invent categories.",
        ),
        (
            "human",
            "\n".join(
                [
                    f"Merchant: {_merchant(transaction)}",
                    f"Description: {_row_value(transaction, 'description', default='')}",
                    f"Amount cents: {_row_value(transaction, 'amount_cents', default=0)}",
                    "Existing expense categories:",
                    *[f"- {name}" for name in category_names],
                ]
            ),
        ),
    ]
    result = structured.invoke(messages)
    category_name = str(_read_guess_field(result, "category_name", "") or "").strip()
    if not category_name:
        return None
    category = repo_ledger.find_category_by_name(conn, category_name)
    if category is None or category["kind"] != "expense" or category["name"] == "Uncategorized":
        return None
    raw_confidence = _read_guess_field(result, "confidence", LLM_DEFAULT_CONFIDENCE)
    try:
        confidence = _clamp_confidence(float(raw_confidence))
    except (TypeError, ValueError):
        confidence = LLM_DEFAULT_CONFIDENCE
    return Suggestion(
        transaction_id=transaction_id,
        suggested_category_id=int(category["id"]),
        suggested_category_name=str(category["name"]),
        confidence=confidence,
        method="llm_guess",
        neighbors=neighbors,
        vote_breakdown=vote_breakdown,
        rationale=(
            f"{_fallback_context(neighbors)} Classifier guessed {category['name']} "
            "from existing expense categories."
        ),
    )


def suggest_category(
    conn: sqlite3.Connection,
    transaction: Any,
    *,
    k: int = 8,
    llm: Any | None = None,
) -> Suggestion:
    """Suggest one existing expense category for an Uncategorized transaction."""
    transaction_id = int(_row_value(transaction, "id", "transaction_id"))
    neighbors = repo_embeddings.similar_transactions(conn, txn_id=transaction_id, k=k)

    voted = _neighbor_vote(conn, transaction_id=transaction_id, neighbors=neighbors)
    if voted is not None:
        return voted

    vote_breakdown: dict[str, float] = {}
    for neighbor in neighbors:
        category = _resolved_neighbor_category(conn, neighbor)
        if category is None:
            continue
        vote_breakdown[str(category["name"])] = (
            vote_breakdown.get(str(category["name"]), 0.0) + _neighbor_weight(neighbor)
        )

    knowledge = _knowledge_suggestion(
        conn,
        transaction_id=transaction_id,
        transaction=transaction,
        neighbors=neighbors,
        vote_breakdown=vote_breakdown,
    )
    if knowledge is not None:
        return knowledge

    if llm is not None:
        llm_guess = _llm_suggestion(
            conn,
            transaction_id=transaction_id,
            transaction=transaction,
            neighbors=neighbors,
            vote_breakdown=vote_breakdown,
            llm=llm,
        )
        if llm_guess is not None:
            return llm_guess

    return Suggestion(
        transaction_id=transaction_id,
        suggested_category_id=None,
        suggested_category_name="",
        confidence=0.0,
        method="none",
        neighbors=neighbors,
        vote_breakdown=vote_breakdown,
        rationale=_fallback_context(neighbors),
    )


def list_uncategorized_expense_backlog(
    conn: sqlite3.Connection,
    *,
    limit: int | None = 100,
    include_closed: bool = False,
    mutation_safe_only: bool = False,
) -> list[sqlite3.Row]:
    """Return expense splits lacking accepted category evidence.

    Reconciliation settles the event, not its category. Cleared expenses remain
    actionable until the current split category has an active accepted claim.
    Transactions in a closed month remain excluded unless ``include_closed`` is
    set (the explicit backlog "include closed months" toggle). Read surfaces
    include multi-split blockers; callers that enqueue the existing whole-
    transaction recategorization action must set ``mutation_safe_only``.
    """
    params: list[Any] = []
    guard_sql = ""
    if not include_closed:
        guard_sql += (
            "\n          AND strftime('%Y-%m', status.posted_on) NOT IN"
            "\n              (SELECT month FROM closed_periods WHERE status='closed')"
        )
    if mutation_safe_only:
        guard_sql += "\n          AND sc.split_count = 1"
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(max(1, int(limit)))
    return conn.execute(
        f"""
        WITH split_counts AS (
          SELECT transaction_id, COUNT(*) AS split_count
          FROM transaction_splits
          GROUP BY transaction_id
        )
        SELECT
          t.id,
          t.id AS transaction_id,
          status.transaction_split_id,
          status.posted_on,
          COALESCE(NULLIF(t.counterparty, ''), t.description) AS merchant,
          t.counterparty,
          t.description,
          t.amount_cents,
          status.split_amount_cents,
          t.source,
          t.recon_status,
          status.category_id,
          status.category_name,
          c.kind AS category_kind,
          status.resolution_status,
          sc.split_count,
          CASE WHEN sc.split_count = 1 THEN 1 ELSE 0 END AS mutation_supported,
          ROW_NUMBER() OVER (
            PARTITION BY t.id
            ORDER BY status.transaction_split_id
          ) AS transaction_resolution_row_number
        FROM v_expense_resolution_status status
        JOIN transactions t ON t.id = status.transaction_id
        JOIN split_counts sc ON sc.transaction_id = t.id
        JOIN categories c ON c.id = status.category_id
        WHERE t.source <> 'opening'
          AND status.resolution_status = 'unresolved'
          {guard_sql}
        ORDER BY ABS(status.split_amount_cents) DESC, status.posted_on DESC,
                 t.id DESC, status.transaction_split_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()


def get_backlog_transaction(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        WITH split_counts AS (
          SELECT transaction_id, COUNT(*) AS split_count
          FROM transaction_splits
          GROUP BY transaction_id
        )
        SELECT
          t.id,
          t.id AS transaction_id,
          status.transaction_split_id,
          status.posted_on,
          COALESCE(NULLIF(t.counterparty, ''), t.description) AS merchant,
          t.counterparty,
          t.description,
          t.amount_cents,
          status.split_amount_cents,
          t.source,
          t.recon_status,
          status.category_id,
          status.category_name,
          c.kind AS category_kind,
          status.resolution_status,
          sc.split_count,
          CASE WHEN sc.split_count = 1 THEN 1 ELSE 0 END AS mutation_supported
        FROM v_expense_resolution_status status
        JOIN transactions t ON t.id = status.transaction_id
        JOIN split_counts sc ON sc.transaction_id = t.id AND sc.split_count = 1
        JOIN categories c ON c.id = status.category_id
        WHERE t.id = ?
          AND t.source <> 'opening'
          AND status.resolution_status = 'unresolved'
        """,
        (int(transaction_id),),
    ).fetchone()
