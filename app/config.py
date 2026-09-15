"""Application settings, loaded from environment / .env via pydantic-settings.

Field names map to upper-case env vars (e.g. ``db_path`` <- ``DB_PATH``).
Access through ``get_settings()`` so the parsed config is cached; call
``get_settings.cache_clear()`` after mutating the environment (CLI / tests).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # storage
    db_path: Path = Path("data/local.sqlite")
    data_dir: Path = Path("data")
    chat_checkpoint_db: str = "chat_checkpoints.sqlite"

    # server
    addr: str = "127.0.0.1:8080"
    read_only: bool = False

    # programmatic API (M8)
    api_token: str = ""
    # Cap request bodies before FastAPI parses/uploads them (default 25 MiB).
    api_max_body_bytes: int = 25 * 1024 * 1024

    # Optional OpenAI-compatible LLM endpoint. It can be local or remote.
    local_llm_base: str = "http://llm.example.invalid/v1"
    local_llm_key: str = ""
    chat_model: str = "qwen3.8-27b-unleashed"
    embed_model: str = "qwen3-embedding"
    embeddings_enabled: bool = False
    llm_timeout_s: int = 180
    llm_max_retries: int = 3
    llm_concurrency: int = 1
    structured_method: str = "json_schema"

    # locale / extraction defaults
    home_currency: str = "CAD"
    home_locale: str = "Exampleville, Example Region"

    # thresholds (used from M1+/M4+)
    big_ticket_threshold_cents: int = 30000
    classify_min_confidence: float = 0.6

    # month-end anomaly scan (FN-105) — deterministic close-inbox source
    anomaly_trailing_months: int = 3
    anomaly_deviation_pct: float = 30.0
    anomaly_min_deviation_cents: int = 2500
    anomaly_min_recurring_months: int = 3
    # Vision triage re-checks a document's sniffed kind before extraction. It runs one
    # extra sequential vision call — but only on image uploads (whose kind is a blind
    # 'receipt' default); PDFs are already content-classified and skip it. Under
    # llm_concurrency=1 that call roughly doubles an image's ingest latency, so set
    # triage_enabled=False to trade the mis-sniff safety net for speed.
    triage_enabled: bool = True
    triage_min_confidence: float = 0.7
    # Second-pass verifier (FN-110): a deterministic arithmetic check on the extracted
    # receipt, plus an optional LLM-as-judge vision call. Findings route the doc to
    # needs_review instead of auto-filing. The arithmetic check is free and on by default;
    # the judge is the one extra sequential vision call, off by default so it is opt-in
    # (like triage, under llm_concurrency=1 it roughly doubles an image's ingest latency).
    verifier_enabled: bool = True
    verifier_llm_judge: bool = False
    verifier_min_confidence: float = 0.7

    # optional capture channels (safe off by default)
    # Fail closed even when third-party credentials exist. Enabling Telegram
    # additionally requires a current, versioned consent record in SQLite.
    strict_local_mode: bool = True

    # Merchant research (FN-150 search adapter). Enabled by default so the
    # ingestion flow can look up descriptors it cannot otherwise explain, but
    # strict_local_mode still overrides it: the egress kill switch has to
    # outrank a feature default or it is not a kill switch. Queries are
    # sanitized to merchant terms first, so amounts, dates, and account
    # identifiers never leave regardless of these values.
    merchant_search_enabled: bool = True
    merchant_search_provider: str = "none"
    merchant_search_endpoint: str = ""
    merchant_search_allowed_endpoints: str = ""
    merchant_search_timeout_s: float = 5.0
    merchant_search_max_results: int = 5
    # Which transport disclosure the operator accepted. The search adapter
    # fails closed on an empty value, so bumping this string invalidates stale
    # consent and forces the disclosure to be accepted again.
    merchant_search_consent_version: str = "merchant-search-disclosure.v1"
    # Brave Web Search API key. Used only as a fallback when the self-hosted
    # provider is unavailable, because the query reaches Brave alongside an
    # account-identifying key.
    brave_api_key: str = ""
    # Minimum gap between outbound searches. Brave's free tier allows one
    # query per second and answers 429 beyond it; a self-hosted scraper gets
    # CAPTCHA-blocked under burst. Research is a background drain, so pacing
    # costs nothing that matters.
    merchant_search_min_interval_s: float = 1.2
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    inbox_dir: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
