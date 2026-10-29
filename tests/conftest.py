from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from app.db import migrate
from app.ingest.schemas import ExtractedReceipt


def pytest_addoption(parser):
    parser.addoption(
        "--run-llm",
        action="store_true",
        default=False,
        help="run eval tests marked llm",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: live LLM-backed evals, skipped by default")


def pytest_collection_modifyitems(config, items):
    run_llm = config.getoption("--run-llm") or os.getenv("RUN_LLM_EVALS") == "1"
    if run_llm:
        return
    skip_llm = pytest.mark.skip(reason="LLM eval skipped; pass --run-llm or RUN_LLM_EVALS=1")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip_llm)


@pytest.fixture
def sample_db(tmp_path):
    """A fresh sample database (schema + fixtures) in a temp dir."""
    path = tmp_path / "sample.sqlite"
    migrate.seed_sample(str(path))
    return str(path)


@pytest.fixture
def empty_db(tmp_path):
    """A migrated but empty database."""
    path = tmp_path / "empty.sqlite"
    migrate.init_db(str(path))
    return str(path)


@pytest.fixture
def household_db(empty_db):
    """Explicit generated identities for bank/consent tests, not production init."""
    from app.db import engine

    with engine.write_tx(empty_db) as conn:
        # Deliberately opt into fixed synthetic IDs used by existing lifecycle proofs.
        conn.executemany(
            """INSERT INTO household_members(id,name,slug,source,is_active,name_required)
               VALUES (?,?,?,'manual',1,0) ON CONFLICT(id) DO UPDATE SET
               name=excluded.name,slug=excluded.slug,source='manual',is_active=1,name_required=0""",
            [(1, "Generated Alex", "generated-alex"), (2, "Generated Jo", "generated-jo")],
        )
    return empty_db


@pytest.fixture
def app_env(tmp_path, sample_db, monkeypatch):
    """Point config at a temp sample DB + temp data dir, isolated per test."""
    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield sample_db
    get_settings.cache_clear()


@pytest.fixture
def make_jpeg():
    def _make(color=(180, 40, 40), size=(80, 80)) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format="JPEG")
        return buf.getvalue()

    return _make


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, messages):
        return self._result


class _FakeLLM:
    """Stands in for ChatOpenAI: returns a fixed ExtractedReceipt, ignores the image."""

    def __init__(self, result: ExtractedReceipt):
        self._result = result

    def with_structured_output(self, schema, **kwargs):
        return _FakeStructured(self._result)

    def invoke(self, messages):
        return self._result


@pytest.fixture
def fake_llm():
    def _factory(receipt: ExtractedReceipt) -> _FakeLLM:
        return _FakeLLM(receipt)

    return _factory
