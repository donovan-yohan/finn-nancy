"""Pinned local LLM clients. Everything routes through here.

We pin a single model (``qwen3.8-27b-unleashed``) so the llama-swap backend never pays a
cold model-swap. Retries are owned by callers (tenacity) so ``max_retries=0`` here.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from ..config import get_settings

QWEN3_QUERY_INSTRUCTION = (
    "Instruct: Given a personal-finance ledger question or transaction description, "
    "retrieve semantically similar approved ledger transactions.\n"
    "Query: "
)


def make_llm(*, temperature: float = 0.0, **overrides) -> ChatOpenAI:
    s = get_settings()
    timeout = overrides.pop("timeout", s.llm_timeout_s)
    return ChatOpenAI(
        base_url=s.local_llm_base,
        api_key=s.local_llm_key or "not-needed",  # local endpoint may not check it
        model=s.chat_model,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,
        **overrides,
    )


def qwen3_query_text(text: str) -> str:
    """Apply Qwen3-Embedding's query-side instruction convention."""
    return QWEN3_QUERY_INSTRUCTION + (text or "").strip()


class Qwen3Embeddings:
    """Small adapter: documents are embedded raw; queries get the Qwen3 prefix."""

    def __init__(self, client: Any):
        self.client = client

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.client.embed_documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self.client.embed_query(qwen3_query_text(text))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def make_embeddings(**overrides) -> Qwen3Embeddings:
    s = get_settings()
    timeout = overrides.pop("timeout", s.llm_timeout_s)
    client = OpenAIEmbeddings(
        base_url=s.local_llm_base,
        api_key=s.local_llm_key or "not-needed",
        model=s.embed_model,
        timeout=timeout,
        max_retries=0,
        check_embedding_ctx_length=False,
        **overrides,
    )
    return Qwen3Embeddings(client)
