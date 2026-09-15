from __future__ import annotations

import asyncio
import math
from typing import Sequence

import pytest

from app.config import get_settings
from app.db import engine, repo_embeddings, repo_ledger


class FakeEmbeddings:
    def __init__(self):
        self.document_batches: list[list[str]] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        lower = text.lower()
        if "alpha" in lower or "market basket" in lower:
            return [1.0, 0.0, 0.0]
        if "beta" in lower or "hydro" in lower:
            return [0.0, 1.0, 0.0]
        if "cafe" in lower:
            return [0.7, 0.3, 0.0]
        return [0.0, 0.0, 1.0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.document_batches.append(batch)
        return [self._vector(text) for text in batch]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


def _set_embeddings(monkeypatch, *, enabled: bool = True, model: str = "fake-embed") -> None:
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("EMBED_MODEL", model)
    get_settings.cache_clear()


def _seed_txns(db_path: str) -> tuple[int, int, int]:
    with engine.write_tx(db_path) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Card','Test','credit','CAD')"
        ).lastrowid
        groceries = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Groceries','expense','finn','#61D394')"
        ).lastrowid
        utilities = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Utilities','expense','finn','#5BC0EB')"
        ).lastrowid
        first = repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on="2026-06-01",
            description="Alpha Market bananas",
            counterparty="Alpha Market",
            amount_cents=-1200,
            source="test",
            external_id="alpha-1",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        second = repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on="2026-06-08",
            description="Alpha Market apples",
            counterparty="Alpha Market",
            amount_cents=-1300,
            source="test",
            external_id="alpha-2",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        third = repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on="2026-06-09",
            description="Beta Hydro bill",
            counterparty="Beta Hydro",
            amount_cents=-9900,
            source="test",
            external_id="beta-1",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert first is not None and second is not None and third is not None
        repo_ledger.insert_split(conn, transaction_id=first, category_id=groceries, amount_cents=-1200)
        repo_ledger.insert_split(conn, transaction_id=second, category_id=groceries, amount_cents=-1300)
        repo_ledger.insert_split(conn, transaction_id=third, category_id=utilities, amount_cents=-9900)
    return first, second, third


def test_make_embeddings_wires_qwen3(monkeypatch):
    import app.llm.client as client_module

    captured = {}

    class CapturingEmbeddings:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def embed_documents(self, texts):
            captured["documents"] = list(texts)
            return [[1.0, 0.0]]

        def embed_query(self, text):
            captured["query"] = text
            return [0.0, 1.0]

    monkeypatch.setattr(client_module, "OpenAIEmbeddings", CapturingEmbeddings)
    monkeypatch.setenv("EMBED_MODEL", "custom-embedding")
    get_settings.cache_clear()

    embeddings = client_module.make_embeddings()

    assert captured["kwargs"]["model"] == "custom-embedding"
    assert captured["kwargs"]["check_embedding_ctx_length"] is False
    assert captured["kwargs"]["base_url"]
    assert embeddings.embed_documents(["raw transaction text"]) == [[1.0, 0.0]]
    assert captured["documents"] == ["raw transaction text"]
    assert embeddings.embed_query("why groceries") == [0.0, 1.0]
    assert captured["query"].startswith("Instruct:")
    assert captured["query"].endswith("why groceries")


def test_vector_storage_roundtrip_and_top_k(empty_db, monkeypatch):
    _set_embeddings(monkeypatch, enabled=True)
    first, second, third = _seed_txns(empty_db)
    fake = FakeEmbeddings()

    result = repo_embeddings.embed_missing_transactions(empty_db, embeddings=fake, batch_size=10)
    rows = repo_embeddings.similar_transactions(empty_db, txn_id=first, k=3)

    assert result["embedded"] == 3
    assert [row["transaction_id"] for row in rows[:2]] == [second, third]
    assert rows[0]["source"] == "vector"
    assert rows[0]["score"] > rows[1]["score"]
    assert first not in [row["transaction_id"] for row in rows]

    packed = repo_embeddings.pack_vector([1.0, 2.5, -3.0])
    unpacked = repo_embeddings.unpack_vector(packed, 3)
    assert list(unpacked) == pytest.approx([1.0, 2.5, -3.0])
    assert repo_embeddings.cosine_similarity([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)


def test_embed_backfill_idempotent_and_stale_only(app_env, monkeypatch):
    _set_embeddings(monkeypatch, enabled=True)
    fake = FakeEmbeddings()

    first = repo_embeddings.embed_missing_transactions(app_env, embeddings=fake, batch_size=50)
    second = repo_embeddings.embed_missing_transactions(app_env, embeddings=fake, batch_size=50)

    with engine.read_conn(app_env) as conn:
        tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        embed_count = conn.execute("SELECT COUNT(*) FROM embeddings WHERE ref_kind='transaction'").fetchone()[0]

    assert first["embedded"] == tx_count
    assert second["embedded"] == 0
    assert embed_count == tx_count

    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET notes='stale vector note' WHERE id=3")

    third = repo_embeddings.embed_missing_transactions(app_env, embeddings=fake, batch_size=50)
    assert third["embedded"] == 1


def test_embed_backfill_flag_off_noop(app_env, monkeypatch):
    _set_embeddings(monkeypatch, enabled=False)
    fake = FakeEmbeddings()

    result = repo_embeddings.embed_missing_transactions(app_env, embeddings=fake)

    assert result["status"] == "disabled"
    assert result["embedded"] == 0
    assert fake.document_batches == []


def test_embed_transactions_job_dispatch_uses_missing_rows(app_env, monkeypatch):
    _set_embeddings(monkeypatch, enabled=True)
    fake = FakeEmbeddings()
    monkeypatch.setattr(repo_embeddings, "make_embeddings", lambda: fake)

    from app.workers.handlers import handle_job

    with engine.write_tx(app_env) as conn:
        conn.execute("INSERT INTO jobs(type, payload_json) VALUES ('embed_transactions', '{}')")
        job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()

    result = handle_job(app_env, job, llm=None)

    with engine.read_conn(app_env) as conn:
        tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        embed_count = conn.execute("SELECT COUNT(*) FROM embeddings WHERE ref_kind='transaction'").fetchone()[0]

    assert result["embedded"] == tx_count
    assert embed_count == tx_count


def test_similar_transactions_falls_back_to_fts_when_disabled(app_env, monkeypatch):
    _set_embeddings(monkeypatch, enabled=False)

    rows = repo_embeddings.similar_transactions(app_env, text="Synthetic Market groceries", k=3)

    assert rows
    assert {row["source"] for row in rows} == {"fts"}
    assert any(row["merchant"] == "Synthetic Market" for row in rows)


@pytest.mark.llm
def test_live_qwen3_embedding_related_pair_scores_higher(monkeypatch):
    from app.llm.client import make_embeddings
    from app.llm.gate import llm_gate

    get_settings.cache_clear()

    async def _embed():
        embeddings = make_embeddings()
        async with llm_gate():
            return await asyncio.to_thread(
                embeddings.embed_documents,
                [
                    "Costco groceries bananas coffee and paper towels",
                    "Costco grocery run with bananas and coffee beans",
                    "Hydro electricity utility bill for the apartment",
                ],
            )

    first, related, unrelated = asyncio.run(_embed())

    assert len(first) == len(related) == len(unrelated)
    assert not math.isclose(repo_embeddings.vector_norm(first), 0.0)
    assert repo_embeddings.cosine_similarity(first, related) > repo_embeddings.cosine_similarity(first, unrelated)
