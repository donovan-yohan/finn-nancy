from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_budgets, repo_insight_prose, repo_jobs


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeInsightLLM:
    model_name = "fake-nancy"

    def __init__(self, content: str = "A calm fake summary."):
        self.content = content
        self.calls = 0
        self.messages = None

    def invoke(self, messages):
        self.calls += 1
        self.messages = messages
        return FakeMessage(self.content)


def _first_month(db_path: str) -> str:
    with engine.read_conn(db_path) as conn:
        return repo_budgets.available_months(conn)[0]


def test_insight_prose_upsert_overwrites_unique_row(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo_insight_prose.upsert(
            conn,
            period_month="2026-06",
            scope="household",
            kind="monthly_summary",
            body="first",
            model="fake-1",
        )
        repo_insight_prose.upsert(
            conn,
            period_month="2026-06",
            scope="household",
            kind="monthly_summary",
            body="second",
            model="fake-2",
        )

    with engine.read_conn(empty_db) as conn:
        row = repo_insight_prose.get(conn, "2026-06", "household", "monthly_summary")
        count = conn.execute("SELECT COUNT(*) FROM insight_prose").fetchone()[0]
    assert count == 1
    assert row["body"] == "second"
    assert row["model"] == "fake-2"


def test_monthly_insight_job_handler_caches_fake_llm_body(sample_db):
    from app.workers.handlers import handle_job

    month = _first_month(sample_db)
    with engine.write_tx(sample_db) as conn:
        job_id = repo_jobs.enqueue(
            conn,
            "monthly_insight",
            {"period_month": month, "scope": "household", "kind": "monthly_summary"},
        )
    with engine.read_conn(sample_db) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    llm = FakeInsightLLM("Nancy sees a clean path forward.")
    result = handle_job(sample_db, job, llm)
    assert result["status"] == "cached"
    assert llm.calls == 1
    assert "monthly household finance digest" in llm.messages[1].content

    with engine.read_conn(sample_db) as conn:
        row = repo_insight_prose.get(conn, month, "household", "monthly_summary")
    assert row["body"] == "Nancy sees a clean path forward."
    assert row["model"] == "fake-nancy"


def test_insights_route_renders_cached_prose_and_generate_enqueues(sample_db, monkeypatch):
    from app.config import get_settings
    from app.web.app import create_app

    month = _first_month(sample_db)
    body = "Cached Nancy paragraph.\n\nSecond paragraph."
    with engine.write_tx(sample_db) as conn:
        repo_insight_prose.upsert(
            conn,
            period_month=month,
            scope="household",
            kind="monthly_summary",
            body=body,
            model="fake-nancy",
        )

    monkeypatch.setenv("DB_PATH", sample_db)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("INBOX_DIR", raising=False)
    get_settings.cache_clear()
    client = TestClient(create_app())

    resp = client.get(f"/insights?month={month}")
    assert resp.status_code == 200
    assert "Cached Nancy paragraph." in resp.text
    assert "Regenerate" in resp.text

    post = client.post(
        "/insights/prose/generate",
        data={"month": month},
        follow_redirects=False,
    )
    assert post.status_code == 303
    assert post.headers["location"] == f"/insights?month={month}"
    with engine.read_conn(sample_db) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='monthly_insight' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert job is not None


def test_monthly_insight_empty_llm_body_raises_without_caching(sample_db):
    from app.workers.insight_prose import handle_monthly_insight

    month = _first_month(sample_db)

    with pytest.raises(ValueError, match="empty monthly summary"):
        handle_monthly_insight(
            sample_db,
            {"period_month": month, "scope": "household", "kind": "monthly_summary"},
            FakeInsightLLM("   "),
        )

    with engine.read_conn(sample_db) as conn:
        assert repo_insight_prose.get(conn, month, "household", "monthly_summary") is None


def test_monthly_insight_empty_llm_body_does_not_overwrite_existing_cache(sample_db):
    from app.workers.insight_prose import handle_monthly_insight

    month = _first_month(sample_db)
    with engine.write_tx(sample_db) as conn:
        repo_insight_prose.upsert(
            conn,
            period_month=month,
            scope="household",
            kind="monthly_summary",
            body="Existing real summary.",
            model="fake-old",
        )

    with pytest.raises(ValueError, match="empty monthly summary"):
        handle_monthly_insight(
            sample_db,
            {"period_month": month, "scope": "household", "kind": "monthly_summary"},
            FakeInsightLLM("\n\t"),
        )

    with engine.read_conn(sample_db) as conn:
        row = repo_insight_prose.get(conn, month, "household", "monthly_summary")
    assert row["body"] == "Existing real summary."
    assert row["model"] == "fake-old"
