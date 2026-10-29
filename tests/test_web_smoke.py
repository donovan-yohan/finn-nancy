from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import engine, migrate, repo_documents


def _client(sample_db, monkeypatch):
    monkeypatch.setenv("DB_PATH", sample_db)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def test_dashboard_renders(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text.lower()
    assert "finn" in body and "nancy" in body
    assert "$" in r.text  # money formatting rendered
    assert '<title>Home · Finn Nancy</title>' in r.text
    assert '<h1>Home</h1>' in r.text
    assert r.text.count('<h1>') == 1
    assert '<details class="fn-more" name="nav-sheet">' in r.text
    assert 'aria-label="More navigation"' in r.text
    assert 'class="fn-disclosure fn-quick-capture fab"' in r.text
    assert 'data-capture-picker' in r.text
    for href in ("/activity", "/upload", "/insights", "/processing", "/review", "/recon", "/close", "/statements"):
        assert f'href="{href}"' in r.text
    # Literal fixture expectations, not values recomputed by the same view.
    totals = re.search(r'<section class="home-stats"[^>]*>(.*?)</section>', r.text, re.S)
    assert totals is not None
    assert re.findall(r'<a class="stat-number"[^>]*>(.*?)</a>', totals[1]) == [
        '$6,100.00', '$2,405.29', '$3,694.71',
    ]
    assert 'Recorded totals for 2026-03' in r.text


def test_dashboard_attention_navigation_uses_wrapping_native_controls(sample_db, monkeypatch):
    """Exercise real attention links and the deployed wrapping action recipe.

    Pixel overlap is covered by the companion Home/Activity browser regression.
    """
    with engine.write_tx(sample_db) as conn:
        repo_documents.insert_source_document(
            conn, kind="receipt", original_name="synthetic-review.jpg",
            storage_ref="synthetic-only", sha256="7" * 64,
            mime_type="image/jpeg", status="needs_review",
        )
    client = _client(sample_db, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    css = (Path(__file__).parents[1] / "app/web/static/foundation/components.css").read_text()
    assert re.search(r"\.fn-actions\s*\{[^}]*display:flex;[^}]*flex-wrap:wrap;[^}]*gap:var\(--space-2\)", css)
    assert re.search(r"\.btn\s*\{[^}]*display:inline-flex;", css)
    assert '/static/foundation/components.css' in r.text
    attention = re.search(r'<nav class="fn-actions home-attention"[^>]*>(.*?)</nav>', r.text, re.S)
    assert attention is not None
    assert 'aria-label="Needs attention"' in r.text
    assert 'href="/review"' in attention[1]
    assert 'Review (1)' in attention[1]
    hero = r.text.split('<header class="fn-page-head">')[1].split("</header>")[0]
    assert '<h1>Home</h1>' in hero
    assert 'href="/manage"' not in hero


def test_dashboard_discloses_transactions_excluded_from_totals(sample_db, monkeypatch):
    with sqlite3.connect(sample_db) as conn:
        transaction_id = int(
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, amount_cents,
                     source, external_id, flow_kind)
                   VALUES (1, '2026-06-22', 'ambiguous dashboard row', -500,
                           'manual', 'dashboard-semantic-review', 'unknown')"""
            ).lastrowid
        )
        conn.execute(
            """INSERT INTO transaction_splits(
                 transaction_id, category_id, amount_cents
               ) VALUES (?, 4, -500)""",
            (transaction_id,),
        )

    response = _client(sample_db, monkeypatch).get("/")
    assert response.status_code == 200
    assert "1 transaction is excluded from these totals because flow meaning or required provenance is incomplete." in response.text
    totals = re.search(r'<section class="home-stats"[^>]*>(.*?)</section>', response.text, re.S)
    assert totals is not None
    assert re.findall(r'<a class="stat-number"[^>]*>(.*?)</a>', totals[1]) == ['$0.00', '$0.00', '$0.00']
    assert "Review excluded movements" in response.text
    assert 'href="/activity?semantic_review=1&amp;date_from=2026-06-01&amp;date_to=2026-06-30"' in response.text


def test_partials_and_health(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    assert client.get("/transactions").status_code == 200
    assert client.get("/categories").status_code == 200
    insights = client.get("/insights")
    assert insights.status_code == 200
    assert "Budget vs actual" in insights.text
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text.strip() == "ok"


def test_version_reports_loaded_revision_and_schema_head(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.1.0"
    assert body["build_sha"] == "unknown" or re.fullmatch(r"[0-9a-f]{40}", body["build_sha"])
    assert body["build_tree_sha"] == "unknown" or re.fullmatch(
        r"[0-9a-f]{40}", body["build_tree_sha"]
    )
    assert str(uuid.UUID(body["database_identity"])) == body["database_identity"]
    assert re.fullmatch(r"[0-9a-f]{64}", body["schema_digest"])
    expected_head = max(path.name for path in migrate.MIGRATIONS_DIR.glob("*.sql"))
    assert expected_head == "042_household_onboarding.sql"
    assert body["expected_migration_head"] == expected_head
    assert body["applied_migration_head"] == body["expected_migration_head"]
    assert body["expected_migration_digest"] == body["applied_migration_digest"]
    assert body["migration_status"] == "ok"
    assert body["migration_sequence_matches"] is True


def test_version_does_not_create_missing_database_uuid(sample_db, monkeypatch):
    from app.db.migrate import DATABASE_IDENTITY_SETTING_KEY

    with sqlite3.connect(sample_db) as conn:
        conn.execute(
            "DELETE FROM app_settings WHERE key = ?",
            (DATABASE_IDENTITY_SETTING_KEY,),
        )

    body = _client(sample_db, monkeypatch).get("/version").json()
    assert body["database_identity"] is None
    with sqlite3.connect(sample_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM app_settings WHERE key = ?",
            (DATABASE_IDENTITY_SETTING_KEY,),
        ).fetchone()[0]
    assert count == 0


def test_version_detects_missing_intermediate_migration(sample_db, monkeypatch):
    with sqlite3.connect(sample_db) as conn:
        filenames = [
            row[0]
            for row in conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY rowid"
            )
        ]
        missing = filenames[len(filenames) // 2]
        assert missing != filenames[-1]
        conn.execute("DELETE FROM schema_migrations WHERE filename = ?", (missing,))

    body = _client(sample_db, monkeypatch).get("/version").json()
    assert body["applied_migration_head"] == body["expected_migration_head"]
    assert body["applied_migration_digest"] != body["expected_migration_digest"]
    assert body["migration_status"] == "missing"
    assert body["migration_sequence_matches"] is False


def test_version_detects_unknown_applied_migration(sample_db, monkeypatch):
    with sqlite3.connect(sample_db) as conn:
        conn.execute(
            "INSERT INTO schema_migrations(filename) VALUES (?)",
            ("999_unknown.sql",),
        )

    body = _client(sample_db, monkeypatch).get("/version").json()
    assert body["applied_migration_head"] == "999_unknown.sql"
    assert body["applied_migration_digest"] != body["expected_migration_digest"]
    assert body["migration_status"] == "unknown"
    assert body["migration_sequence_matches"] is False


def test_version_detects_out_of_order_applied_migration(sample_db, monkeypatch):
    with sqlite3.connect(sample_db) as conn:
        filenames = [
            row[0]
            for row in conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY rowid"
            )
        ]
        moved = filenames[len(filenames) // 2]
        conn.execute("DELETE FROM schema_migrations WHERE filename = ?", (moved,))
        conn.execute(
            "INSERT INTO schema_migrations(filename) VALUES (?)",
            (moved,),
        )

    body = _client(sample_db, monkeypatch).get("/version").json()
    assert body["migration_status"] == "out_of_order"
    assert body["migration_sequence_matches"] is False
