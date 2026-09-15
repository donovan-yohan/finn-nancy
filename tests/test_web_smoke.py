from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import migrate


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
    assert r.text.count('name="nav-sheet"') >= 2
    assert '<details class="fab" name="nav-sheet">' in r.text
    assert '<details class="more" name="nav-sheet">' in r.text


def test_dashboard_nav_pills_wrap_without_overlap(sample_db, monkeypatch):
    """Pill links must be atomic inline-flex boxes (zero-specificity default
    so explicit display rules like .block still win), and the hero nav row
    must wrap via a flex container with gaps — not middot-separated inline
    pills whose boxes overflow the line box and overlap on mobile."""
    client = _client(sample_db, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    css = (
        Path(__file__).parents[1] / "app" / "web" / "static" / "app.css"
    ).read_text()
    # Whitespace-tolerant: pins the rule and declaration, not its formatting.
    assert re.search(r":where\(\s*\.link\s*\)\s*\{[^}]*display:\s*inline-flex;", css)
    # The hero nav is a gap-separated chip row, not inline middot-separated pills.
    assert '<p class="link-row subtle">' in r.text
    hero = r.text.split('<header class="hero">')[1].split("</header>")[0]
    assert "link-row" in hero
    assert "·" not in re.sub(r"<[^>]+>", "", hero)


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
    assert "1 transaction(s) are excluded from the totals above" in response.text
    assert 'href="/activity?semantic_review=1"' in response.text


def test_partials_and_health(sample_db, monkeypatch):
    client = _client(sample_db, monkeypatch)
    assert client.get("/transactions").status_code == 200
    assert client.get("/categories").status_code == 200
    insights = client.get("/insights")
    assert insights.status_code == 200
    assert "budget vs actual" in insights.text
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
    assert expected_head == "039_month_spine_budgets.sql"
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
