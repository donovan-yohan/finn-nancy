"""Mapping a card's last four digits to a person.

Naming is a durable setting, never a precondition: an unnamed card must keep
reporting under a stable placeholder rather than blocking anything.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine, repo_card_holders as repo


def _client(db_path: str, data_dir: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def test_unknown_card_resolves_to_a_stable_placeholder(empty_db):
    with engine.read_conn(empty_db) as conn:
        assert repo.label_for(conn, "9002") == "Unassigned card ••9002"


def test_observing_a_card_is_idempotent(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo.observe(conn, "9002")
        repo.observe(conn, "9002")
        repo.observe(conn, "9002")
    with engine.read_conn(empty_db) as conn:
        assert len(repo.listing(conn)) == 1
        assert repo.unnamed_count(conn) == 1


def test_naming_a_card_changes_its_label(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo.observe(conn, "9001")
        repo.set_name(conn, "9001", "Sample Member A")
    with engine.read_conn(empty_db) as conn:
        assert repo.label_for(conn, "9001") == "Sample Member A"
        assert repo.unnamed_count(conn) == 0


def test_renaming_replaces_rather_than_duplicates(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo.set_name(conn, "9001", "Sample Member A")
        repo.set_name(conn, "9001", "Sample Member A Updated")
    with engine.read_conn(empty_db) as conn:
        listing = repo.listing(conn)
    assert len(listing) == 1
    assert listing[0]["display_name"] == "Sample Member A Updated"


def test_a_malformed_card_is_ignored_not_raised_on_observe(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo.observe(conn, "no")
        repo.observe(conn, "")
    with engine.read_conn(empty_db) as conn:
        assert repo.listing(conn) == []


def test_manage_lists_cards_and_accepts_a_name(empty_db, tmp_path, monkeypatch):
    with engine.write_tx(empty_db) as conn:
        repo.observe(conn, "9002")
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)

    body = client.get("/manage").text
    assert "Cardholders" in body
    assert "••9002" in body
    assert "without a name" in body

    response = client.post(
        "/manage/cardholders/9002", data={"display_name": "Sample Member B"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(empty_db) as conn:
        assert repo.label_for(conn, "9002") == "Sample Member B"
