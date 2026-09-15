"""Transaction embedding cache and similarity retrieval.

Vectors are stored as float32 BLOBs and scanned in-process. At the intended
personal-ledger scale this avoids a loadable-extension dependency while keeping a
clean fallback path: if embeddings are disabled, missing, stale, or unavailable,
callers get FTS neighbors from ``rag_fts`` instead.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import sqlite3
import sys
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import get_settings
from ..llm.client import make_embeddings
from ..llm.gate import llm_gate
from . import engine, repo_jobs, repo_rag

logger = logging.getLogger(__name__)

REF_KIND_TRANSACTION = "transaction"
DEFAULT_BATCH_SIZE = 16
MAX_SIMILAR_LIMIT = 25


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def pack_vector(vector: Sequence[float]) -> bytes:
    values = array("f", (float(value) for value in vector))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def unpack_vector(blob: bytes, dims: int) -> array:
    if len(blob) != int(dims) * 4:
        raise ValueError("embedding blob size does not match dims")
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def vector_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    norm_a = vector_norm(a)
    norm_b = vector_norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    dot = sum(float(left) * float(right) for left, right in zip(a, b))
    return dot / (norm_a * norm_b)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(left) * float(right) for left, right in zip(a, b))


def _limit(k: int) -> int:
    try:
        parsed = int(k)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(parsed, MAX_SIMILAR_LIMIT))


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _quote_fts_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _fallback_fts_matches(query: str) -> list[str]:
    match = repo_rag.fts_query(query)
    matches = [match] if match else []
    tokens = [token for token in re.findall(r"\w+", query or "") if len(token) > 2]
    if len(tokens) > 1:
        matches.append(" OR ".join(_quote_fts_token(token) for token in tokens[:8]))
    return matches


def _transaction_query_text(conn: sqlite3.Connection, transaction_id: int) -> str:
    row = conn.execute(
        """
        SELECT
          t.posted_on,
          t.description,
          t.counterparty,
          GROUP_CONCAT(DISTINCT c.name) AS categories
        FROM transactions t
        LEFT JOIN transaction_splits s ON s.transaction_id = t.id
        LEFT JOIN categories c ON c.id = s.category_id
        WHERE t.id = ?
        GROUP BY t.id
        """,
        (transaction_id,),
    ).fetchone()
    if row is None:
        return ""
    return " ".join(
        part
        for part in (
            row["counterparty"],
            row["description"],
            row["categories"],
            row["posted_on"],
        )
        if part
    )


def _fts_neighbors(
    conn: sqlite3.Connection,
    query: str,
    *,
    exclude_id: int | None,
    k: int,
) -> list[dict[str, Any]]:
    if not (query or "").strip():
        return []
    for match in _fallback_fts_matches(query):
        try:
            rows = conn.execute(
                """
                WITH category_rollup AS (
                  SELECT
                    s.transaction_id,
                    GROUP_CONCAT(DISTINCT c.name) AS category
                  FROM transaction_splits s
                  JOIN categories c ON c.id = s.category_id
                  GROUP BY s.transaction_id
                )
                SELECT
                  rc.ref_id AS transaction_id,
                  t.posted_on,
                  COALESCE(NULLIF(t.counterparty, ''), t.description) AS merchant,
                  t.description,
                  t.amount_cents,
                  COALESCE(cr.category, 'Uncategorized') AS category,
                  bm25(rag_fts) AS rank
                FROM rag_fts
                JOIN rag_chunks rc ON rc.id = rag_fts.rowid
                JOIN transactions t ON t.id = rc.ref_id
                LEFT JOIN category_rollup cr ON cr.transaction_id = t.id
                WHERE rag_fts MATCH ?
                  AND rc.ref_kind = 'transaction'
                  AND rc.ref_id != ?
                ORDER BY bm25(rag_fts), t.posted_on DESC, t.id DESC
                LIMIT ?
                """,
                (match, int(exclude_id or -1), k),
            ).fetchall()
        except sqlite3.OperationalError:
            logger.info("transaction similarity FTS fallback failed", exc_info=True)
            return []
        if rows:
            out: list[dict[str, Any]] = []
            for row in rows:
                item = _row_dict(row)
                item["score"] = -float(item.pop("rank") or 0.0)
                item["source"] = "fts"
                out.append(item)
            return out
    return []


def _embedding_rows(conn: sqlite3.Connection, model: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH category_rollup AS (
          SELECT
            s.transaction_id,
            GROUP_CONCAT(DISTINCT c.name) AS category
          FROM transaction_splits s
          JOIN categories c ON c.id = s.category_id
          GROUP BY s.transaction_id
        )
        SELECT
          e.ref_id AS transaction_id,
          e.dims,
          e.norm,
          e.content_sha256,
          e.vector,
          rc.content,
          t.posted_on,
          COALESCE(NULLIF(t.counterparty, ''), t.description) AS merchant,
          t.description,
          t.amount_cents,
          COALESCE(cr.category, 'Uncategorized') AS category
        FROM embeddings e
        JOIN rag_chunks rc
          ON rc.ref_kind = e.ref_kind
         AND rc.ref_id = e.ref_id
        JOIN transactions t ON t.id = e.ref_id
        LEFT JOIN category_rollup cr ON cr.transaction_id = t.id
        WHERE e.ref_kind = 'transaction'
          AND e.model = ?
        """,
        (model,),
    ).fetchall()


def _stored_vector_for_txn(
    conn: sqlite3.Connection,
    *,
    model: str,
    transaction_id: int,
) -> tuple[array, float] | None:
    row = conn.execute(
        """
        SELECT e.vector, e.dims, e.norm, e.content_sha256, rc.content
        FROM embeddings e
        JOIN rag_chunks rc
          ON rc.ref_kind = e.ref_kind
         AND rc.ref_id = e.ref_id
        WHERE e.ref_kind = 'transaction'
          AND e.ref_id = ?
          AND e.model = ?
        """,
        (transaction_id, model),
    ).fetchone()
    if row is None or row["content_sha256"] != content_hash(row["content"]):
        return None
    return unpack_vector(row["vector"], int(row["dims"])), float(row["norm"] or 0.0)


def _vector_neighbors(
    conn: sqlite3.Connection,
    *,
    query_vector: Sequence[float],
    query_norm: float,
    model: str,
    exclude_id: int | None,
    k: int,
) -> list[dict[str, Any]]:
    if query_norm == 0.0:
        return []
    scored: list[dict[str, Any]] = []
    for row in _embedding_rows(conn, model):
        transaction_id = int(row["transaction_id"])
        if exclude_id is not None and transaction_id == int(exclude_id):
            continue
        if row["content_sha256"] != content_hash(row["content"]):
            continue
        dims = int(row["dims"])
        if dims != len(query_vector):
            continue
        row_norm = float(row["norm"] or 0.0)
        if row_norm == 0.0:
            continue
        try:
            vector = unpack_vector(row["vector"], dims)
        except ValueError:
            continue
        score = _dot(query_vector, vector) / (query_norm * row_norm)
        scored.append(
            {
                "transaction_id": transaction_id,
                "posted_on": row["posted_on"],
                "merchant": row["merchant"],
                "description": row["description"],
                "amount_cents": row["amount_cents"],
                "category": row["category"],
                "score": score,
                "source": "vector",
            }
        )
    scored.sort(key=lambda item: (item["score"], item["posted_on"], item["transaction_id"]), reverse=True)
    return scored[:k]


def _retrying_embed_documents(embeddings: Any, texts: Sequence[str]) -> list[list[float]]:
    settings = get_settings()

    @retry(
        reraise=True,
        stop=stop_after_attempt(max(1, int(settings.llm_max_retries))),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _call() -> list[list[float]]:
        return embeddings.embed_documents(list(texts))

    return _call()


def _retrying_embed_query(embeddings: Any, text: str) -> list[float]:
    settings = get_settings()

    @retry(
        reraise=True,
        stop=stop_after_attempt(max(1, int(settings.llm_max_retries))),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _call() -> list[float]:
        return embeddings.embed_query(text)

    return _call()


def _embed_query_with_gate(text: str) -> list[float]:
    embeddings = make_embeddings()

    async def _run() -> list[float]:
        async with llm_gate():
            return await asyncio.to_thread(_retrying_embed_query, embeddings, text)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    raise RuntimeError("text embedding retrieval must run outside an active event loop")


def _stale_chunks(conn: sqlite3.Connection, *, model: str, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          rc.ref_id,
          rc.content,
          e.content_sha256,
          e.dims
        FROM rag_chunks rc
        LEFT JOIN embeddings e
          ON e.ref_kind = rc.ref_kind
         AND e.ref_id = rc.ref_id
         AND e.model = ?
        JOIN transactions t ON t.id = rc.ref_id
        WHERE rc.ref_kind = 'transaction'
        ORDER BY rc.ref_id
        """,
        (model,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        digest = content_hash(row["content"])
        if row["content_sha256"] == digest and int(row["dims"] or 0) > 0:
            continue
        out.append({"ref_id": int(row["ref_id"]), "content": row["content"], "content_sha256": digest})
        if limit is not None and len(out) >= limit:
            break
    return out


def upsert_transaction_embeddings(
    conn: sqlite3.Connection,
    chunks: Sequence[dict[str, Any]],
    vectors: Sequence[Sequence[float]],
    *,
    model: str,
) -> int:
    count = 0
    for chunk, vector in zip(chunks, vectors):
        dims = len(vector)
        if dims <= 0:
            continue
        conn.execute(
            """
            INSERT INTO embeddings(
              ref_kind, ref_id, model, dims, norm, content_sha256, content, vector
            )
            VALUES ('transaction', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref_kind, ref_id, model) DO UPDATE SET
              dims = excluded.dims,
              norm = excluded.norm,
              content_sha256 = excluded.content_sha256,
              content = excluded.content,
              vector = excluded.vector,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(chunk["ref_id"]),
                model,
                dims,
                vector_norm(vector),
                chunk["content_sha256"],
                chunk["content"],
                pack_vector(vector),
            ),
        )
        count += 1
    return count


def embed_missing_transactions(
    db_path: str | Path,
    *,
    embeddings: Any | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.embeddings_enabled:
        return {"ok": True, "status": "disabled", "embedded": 0, "remaining": 0}

    model = settings.embed_model
    embeddings = embeddings or make_embeddings()
    batch_size = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
    embedded = 0

    while limit is None or embedded < limit:
        remaining_limit = None if limit is None else max(0, limit - embedded)
        if remaining_limit == 0:
            break
        with engine.read_conn(db_path) as conn:
            chunks = _stale_chunks(conn, model=model, limit=min(batch_size, remaining_limit or batch_size))
        if not chunks:
            return {"ok": True, "status": "up_to_date", "embedded": embedded, "remaining": 0}
        try:
            vectors = _retrying_embed_documents(embeddings, [chunk["content"] for chunk in chunks])
        except Exception as exc:  # noqa: BLE001 - embedding failures must not break ingestion/jobs
            logger.warning("transaction embedding backfill failed", exc_info=True)
            return {"ok": False, "status": "embedding_failed", "embedded": embedded, "error": repr(exc)}
        with engine.write_tx(db_path) as conn:
            embedded += upsert_transaction_embeddings(conn, chunks, vectors, model=model)

    with engine.read_conn(db_path) as conn:
        remaining = len(_stale_chunks(conn, model=model, limit=None))
    return {"ok": True, "status": "partial", "embedded": embedded, "remaining": remaining}


def enqueue_embed_transactions(conn: sqlite3.Connection) -> int | None:
    if not get_settings().embeddings_enabled:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE type = 'embed_transactions'
          AND status IN ('pending', 'running')
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return repo_jobs.enqueue(conn, "embed_transactions", {})


def _similar_with_conn(
    conn: sqlite3.Connection,
    *,
    txn_id: int | None,
    text: str | None,
    k: int,
) -> list[dict[str, Any]]:
    if txn_id is None and not text:
        raise ValueError("txn_id or text is required")
    if txn_id is not None and text:
        raise ValueError("pass txn_id or text, not both")

    limit = _limit(k)
    fallback_query = text or _transaction_query_text(conn, int(txn_id or 0))
    settings = get_settings()

    if not settings.embeddings_enabled:
        return _fts_neighbors(conn, fallback_query, exclude_id=txn_id, k=limit)

    model = settings.embed_model
    try:
        if txn_id is not None:
            stored = _stored_vector_for_txn(conn, model=model, transaction_id=int(txn_id))
            if stored is None:
                return _fts_neighbors(conn, fallback_query, exclude_id=txn_id, k=limit)
            query_vector, query_norm = stored
        else:
            query_vector = _embed_query_with_gate(text or "")
            query_norm = vector_norm(query_vector)
        rows = _vector_neighbors(
            conn,
            query_vector=query_vector,
            query_norm=query_norm,
            model=model,
            exclude_id=txn_id,
            k=limit,
        )
        if rows:
            return rows
    except Exception:  # noqa: BLE001 - retrieval must degrade to FTS
        logger.info("transaction vector similarity unavailable; falling back to FTS", exc_info=True)
    return _fts_neighbors(conn, fallback_query, exclude_id=txn_id, k=limit)


def similar_transactions(
    conn_or_db_path: sqlite3.Connection | str | Path,
    *,
    txn_id: int | None = None,
    text: str | None = None,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Return nearest approved transactions, preferring vectors and falling back to FTS."""
    if isinstance(conn_or_db_path, sqlite3.Connection):
        return _similar_with_conn(conn_or_db_path, txn_id=txn_id, text=text, k=k)
    with engine.read_conn(conn_or_db_path) as conn:
        return _similar_with_conn(conn, txn_id=txn_id, text=text, k=k)
