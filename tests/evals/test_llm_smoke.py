from __future__ import annotations

import os

import pytest


@pytest.mark.llm
def test_local_llm_smoke():
    """Optional live endpoint eval; skipped unless RUN_LLM_EVALS=1 or --run-llm."""
    from langchain_openai import ChatOpenAI

    from app.config import get_settings

    settings = get_settings()
    llm = ChatOpenAI(
        base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("LOCAL_LLM_BASE") or str(settings.local_llm_base),
        api_key=os.getenv("OPENAI_API_KEY") or os.getenv("LOCAL_LLM_KEY") or settings.local_llm_key or "local-llm",
        model=os.getenv("ORCHESTRATOR_MODEL") or os.getenv("CHAT_MODEL") or settings.chat_model,
        timeout=30,
        max_retries=0,
    )
    result = llm.invoke("Reply exactly: EVAL-OK")
    assert result.content.strip() == "EVAL-OK"
