"""Generated merchant projection contracts; never operator or provider records."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.plaid_sandbox.store import Store
from app.plaid_sandbox.web import create_app


def connect(store, suffix="a"):
    owner = store.add_member(f"Generated {suffix}", "generated-actor")
    link = store.new_link("generated-browser", owner, f"link-sandbox-generated-{suffix}")
    connection = store.finish_exchange(link, owner, f"generated-item-{suffix}",
                                       f"access-sandbox-generated-{suffix}", "generated-actor")
    store.save_accounts(connection, [{"account_id": "equal-account", "name": "Generated Checking"}])
    return connection


def source(transaction_id="equal-id", **extra):
    return {"transaction_id": transaction_id, "account_id": "equal-account", "amount": "12.34",
            "iso_currency_code": "CAD", "date": "2026-01-02", "name": "Generated provider description",
            "pending": False, "pending_transaction_id": None, **extra}


def metadata(**extra):
    return {"merchant_name": "Generated Merchant", "merchant_entity_id": "generated-entity",
            "original_description": "  GENERATED ORIGINAL\nBANK TEXT  ",
            "counterparties": [{"name": "Generated Marketplace", "entity_id": "generated-marketplace",
                                "type": "marketplace", "confidence_level": "HIGH",
                                "website": "https://generated.invalid", "access_token": "forbidden-generated-token"}],
            "personal_finance_category": {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_OTHER",
                                          "confidence_level": "VERY_HIGH", "version": "v2",
                                          "secret": "forbidden-generated-token"},
            "logo_url": "https://generated.invalid/logo", **extra}


def expected(**extra):
    return {"merchant_name": "Generated Merchant", "merchant_entity_id": "generated-entity",
            "original_description": "  GENERATED ORIGINAL\nBANK TEXT  ",
            "counterparties": [{"name": "Generated Marketplace", "entity_id": "generated-marketplace",
                                "type": "marketplace", "confidence_level": "HIGH"}],
            "category_suggestion": {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_OTHER",
                                    "confidence_level": "VERY_HIGH", "version": "v2"}, **extra}


def test_category_version_comes_from_documented_nested_response_field(tmp_path):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    value = metadata()
    value["personal_finance_category_version"] = "not-the-response-field"
    value["personal_finance_category"]["version"] = "v2"
    apply(store, connection, source(**value))
    assert store.activity()["rows"][0]["enrichment"]["category_suggestion"]["version"] == "v2"


def apply(store, connection, row, *, kind="modified", cursor=None):
    old = store.connection(connection)["cursor"]
    store.commit_window(connection, old, cursor or old + "x", [(kind, row)], None, "generated-actor")


class BaselineWriter(Store):
    """Frozen pre-enrichment window SQL; no dependency on a saved source file.

    Reuse only the connection/transaction helpers on an existing generated DB,
    never the current bootstrap that might repair a missing guard. The window
    deliberately names no enrichment column, just like the rollback version.
    """

    def __init__(self, root):
        self.root = root.resolve()
        self.path = self.root / "sandbox.sqlite"
        assert self.path.is_file()

    def commit_window(self, connection_id, original_cursor, cursor, changes, bank_update, actor):
        with self.tx() as db:
            current = db.execute("SELECT cursor FROM connections WHERE id=?", (connection_id,)).fetchone()
            if current[0] != original_cursor:
                raise ValueError("stale cursor")
            for kind, row in changes:
                payload = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO observations(connection_id,transaction_id,kind,digest,payload) VALUES(?,?,?,?,?)",
                           (connection_id, row["transaction_id"], kind, digest, payload))
                if kind == "removed":
                    db.execute("UPDATE staged SET state=CASE WHEN state='superseded' THEN state ELSE 'removed' END WHERE connection_id=? AND transaction_id=?",
                               (connection_id, row["transaction_id"]))
                    continue
                pending_id = row.get("pending_transaction_id")
                if pending_id:
                    db.execute("UPDATE staged SET state='superseded' WHERE connection_id=? AND transaction_id=?",
                               (connection_id, pending_id))
                if not db.execute("SELECT 1 FROM accounts WHERE connection_id=? AND account_id=?", (connection_id, row["account_id"])).fetchone():
                    raise ValueError("unknown provider account")
                amount = -Decimal(str(row["amount"])) * 100
                if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > 9_000_000_000_000_000:
                    raise ValueError("invalid amount precision")
                db.execute("""INSERT INTO staged(connection_id,transaction_id,account_id,amount_cents,currency,date,name,state,pending_transaction_id)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,transaction_id) DO UPDATE SET
                    account_id=excluded.account_id,amount_cents=excluded.amount_cents,currency=excluded.currency,
                    date=excluded.date,name=excluded.name,state=excluded.state,pending_transaction_id=excluded.pending_transaction_id""",
                    (connection_id, row["transaction_id"], row["account_id"], int(amount), row.get("iso_currency_code"),
                     row.get("date"), row.get("name"), "pending" if row.get("pending") else "posted", row.get("pending_transaction_id")))
            db.execute("UPDATE connections SET cursor=?,last_local_sync=?,last_bank_update=?,status='connected',error=NULL WHERE id=?",
                       (cursor, datetime.now(timezone.utc).isoformat(), bank_update, connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('sync_committed',?,?)", (actor, connection_id))


@pytest.mark.parametrize("different", [{}, {"name": "Generated NEW source", "amount": "22.34"}],
                         ids=["identical-core", "source-and-amount"])
def test_rollback_writer_invalidates_stale_projection_without_reopen_inference(tmp_path, different):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    other = connect(store, "b")
    original = source(**metadata(merchant_name="Generated OLD merchant"))
    apply(store, connection, original)
    apply(store, other, original)
    newer = {**original, **different, "merchant_name": "Generated NEW merchant",
             "original_description": "Generated NEW original"}
    apply(BaselineWriter(store.root), connection, newer)
    before_reopen = records(store)
    upgraded = Store(store.root)
    assert records(upgraded) == before_reopen
    for item in (upgraded.snapshot()["staged"][0], upgraded.activity(connection_id=connection)["rows"][0],
                 next(row for row in upgraded.overview()["recent"] if row["connection_id"] == connection)):
        assert item["enrichment"] is None
        assert item["name"] == newer["name"]
        assert item["amount_cents"] == (-2234 if different else -1234)
    assert upgraded.activity(q="Generated OLD", connection_id=connection)["total"] == 0
    assert upgraded.activity(q="Generated NEW merchant", connection_id=connection)["total"] == 0
    assert upgraded.activity(q="Generated NEW original", connection_id=connection)["total"] == 0
    assert upgraded.activity(q="Generated OLD", connection_id=other)["total"] == 1
    assert upgraded.connection(connection)["cursor"] == "xx"
    assert len(before_reopen["observations"]) == 3
    # Replaying the same deduplicated payload with the new writer establishes
    # its normalized metadata; it must not be cleared by the source-write guard.
    apply(upgraded, connection, newer)
    assert upgraded.activity(connection_id=connection)["rows"][0]["enrichment"] == expected(
        merchant_name="Generated NEW merchant", original_description="Generated NEW original")
    assert upgraded.activity(q="Generated NEW merchant", connection_id=connection)["total"] == 1
    assert records(upgraded)["observations"] == before_reopen["observations"]
    assert records(Store(store.root)) == records(upgraded)


def test_guard_install_on_existing_projection_schema_is_idempotent_without_recovery(tmp_path, monkeypatch):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    apply(store, connection, source("intentional-null"))
    with store.tx() as db:
        db.execute("DROP TRIGGER staged_source_invalidates_enrichment")
        columns = [dict(row) for row in db.execute("PRAGMA table_info(staged)")]
    before = records(store)

    def forbidden(*args):
        raise AssertionError("existing projection schema must not run recovery")

    monkeypatch.setattr(Store, "_recover_enrichment", forbidden)
    for _ in range(2):
        reopened = Store(store.root)
        assert records(reopened) == before
        with reopened.tx() as db:
            assert [dict(row) for row in db.execute("PRAGMA table_info(staged)")] == columns
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='staged_source_invalidates_enrichment'").fetchone()[0] == 1
    apply(BaselineWriter(store.root), connection, source(**metadata(merchant_name="Generated NEW")))
    assert Store(store.root).activity()["rows"][0]["enrichment"] is None


@pytest.mark.parametrize("column", ["connection_id", "transaction_id", "account_id", "amount_cents",
                                  "currency", "date", "name", "pending_transaction_id"])
def test_each_source_column_statement_invalidates_even_when_value_is_identical(tmp_path, column):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    with store.tx() as db:
        db.execute(f"UPDATE staged SET {column}={column}")
    assert store.activity()["rows"][0]["enrichment"] is None
    # An explicit enrichment-only write is not invalidated by its own guard.
    apply(store, connection, source(**metadata()))
    assert store.activity()["rows"][0]["enrichment"] == expected()


@pytest.mark.parametrize("kind", ["added", "modified"])
def test_projection_is_atomic_safe_shared_and_replays_exact_current_payload(tmp_path, kind):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    a = source(**metadata())
    b = source(**metadata(merchant_name="Generated Other", original_description=None,
                          counterparties=[], personal_finance_category=None, personal_finance_category_version=None))
    for index, row in enumerate((a, b, a)):
        apply(store, connection, row, kind=kind)
        projected = expected() if index != 1 else expected(merchant_name="Generated Other", original_description=None,
                                                           counterparties=[], category_suggestion=None)
        for item in (store.snapshot()["staged"][0], store.activity()["rows"][0], store.overview()["recent"][0]):
            assert item["enrichment"] == projected
            assert item["name"] == a["name"] and item["amount_cents"] == -1234
            assert item["flow_kind"] == "unknown" and item["booked"] is False
            assert "enrichment_json" not in item
            assert "forbidden-generated-token" not in json.dumps(item)
            assert "logo_url" not in json.dumps(item) and "website" not in json.dumps(item)
    with store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        assert json.loads(db.execute("SELECT payload FROM observations ORDER BY id DESC LIMIT 1").fetchone()[0]) == b
        assert json.loads(db.execute("SELECT enrichment_json FROM staged").fetchone()[0]) == expected()
        assert json.loads(db.execute("SELECT payload FROM observations ORDER BY id LIMIT 1").fetchone()[0]) == a
    # Reopen must not infer B from its larger immutable observation ID.
    assert Store(store.root).snapshot()["staged"][0]["enrichment"] == expected()
    apply(store, connection, source())
    assert store.activity()["rows"][0]["enrichment"] is None
    assert Store(store.root).snapshot()["staged"][0]["enrichment"] is None


@pytest.mark.parametrize("bad", [None, True, 7, {}, [], "   ", "x" * 4097],
                         ids=["null", "boolean", "number", "object", "list", "whitespace", "oversized"])
def test_malformed_optional_metadata_never_rejects_financial_update(tmp_path, bad):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    row = source(merchant_name=bad, merchant_entity_id=bad, original_description=bad,
                 counterparties=bad, personal_finance_category=bad, personal_finance_category_version=bad)
    apply(store, connection, row)
    item = store.snapshot()["staged"][0]
    assert item["enrichment"] is None
    assert item["amount_cents"] == -1234 and store.connection(connection)["cursor"] == "x"
    with store.tx() as db:
        assert json.loads(db.execute("SELECT payload FROM observations").fetchone()[0]) == row


@pytest.mark.parametrize("over", [False, True])
def test_application_display_bounds_are_exact_and_not_silent_truncation(tmp_path, over):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    delta = int(over)
    party = {"name": "n" * (512 + delta), "entity_id": "e" * (256 + delta),
             "type": "t" * (64 + delta), "confidence_level": "c" * (64 + delta)}
    row = source(merchant_name="m" * (512 + delta), merchant_entity_id="i" * (256 + delta),
                 original_description=" " + "o" * (4094 + delta) + " ",
                 counterparties=[party] * 11,
                 personal_finance_category={"primary": "p" * (128 + delta), "detailed": "d" * (128 + delta),
                                            "confidence_level": "c" * (64 + delta),
                                            "version": "v" * (64 + delta)})
    apply(store, connection, row)
    item = store.activity()["rows"][0]["enrichment"]
    if over:
        assert item is None
    else:
        assert item == {"merchant_name": row["merchant_name"], "merchant_entity_id": row["merchant_entity_id"],
                        "original_description": row["original_description"], "counterparties": [party] * 10,
                        "category_suggestion": row["personal_finance_category"]}


def test_partial_malformed_fields_normalize_independently_and_drop_empty_counterparties(tmp_path):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(merchant_name="Generated Valid", merchant_entity_id={"bad": 1},
          original_description=False, counterparties=[None, [], {}, {"website": "https://generated.invalid"},
          {"name": "Generated Party", "entity_id": 9, "type": [], "confidence_level": "HIGH", "secret": "hidden"}],
          personal_finance_category={"primary": "TRANSFER", "detailed": [], "confidence_level": False},
          personal_finance_category_version=32))
    assert store.activity()["rows"][0]["enrichment"] == {
        "merchant_name": "Generated Valid", "merchant_entity_id": None, "original_description": None,
        "counterparties": [{"name": "Generated Party", "entity_id": None, "type": None, "confidence_level": "HIGH"}],
        "category_suggestion": {"primary": "TRANSFER", "detailed": None, "confidence_level": None, "version": None}}


def records(store):
    with store.tx() as db:
        return {table: [dict(row) for row in db.execute("SELECT * FROM " + table)]
                for table in ("staged", "observations", "audit", "connections", "accounts")}


def legacy(store):
    """Reproduce precisely the prior candidate's ten-column staged schema."""
    with store.tx() as db:
        db.execute("DROP TRIGGER IF EXISTS staged_source_invalidates_enrichment")
        db.execute("ALTER TABLE staged DROP COLUMN enrichment_json")


def observation(store, connection, row, kind="modified", *, transaction_id="equal-id"):
    # Unique generated digests keep every candidate distinct without changing evidence.
    with store.tx() as db:
        ordinal = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        db.execute("INSERT INTO observations(connection_id,transaction_id,kind,digest,payload) VALUES(?,?,?,?,?)",
                   (connection, transaction_id, kind, str(ordinal), json.dumps(row)))


def test_legacy_upgrade_recovers_only_projection_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()), kind="added")
    # Different source financial fields and unknown provider fields must not win by ID.
    observation(store, connection, source(**metadata(merchant_name="Generated Wrong", amount="22.34")))
    observation(store, connection, source(**metadata(website="https://generated-other.invalid")))
    legacy(store)
    before = records(store)
    upgraded = Store(store.root)
    assert upgraded.snapshot()["staged"][0]["enrichment"] == expected()
    after = records(upgraded)
    for row in after["staged"]:
        assert row.pop("enrichment_json") is not None
    assert after == before
    assert records(Store(store.root)) == records(upgraded)
    with upgraded.tx() as db:
        columns = [dict(row) for row in db.execute("PRAGMA table_info(staged)")]
        column = next(row for row in columns if row["name"] == "enrichment_json")
        assert column["type"] == "TEXT" and column["notnull"] == 0
        assert column["dflt_value"] is None
        assert sum(row["name"] == "enrichment_json" for row in columns) == 1


@pytest.mark.parametrize("different", [
    {"account_id": "wrong-account"}, {"amount": "12.34001"}, {"amount": "NaN"}, {"amount": []},
    {"iso_currency_code": "USD"}, {"date": "2026-01-03"}, {"name": "Other exact source"},
    {"pending_transaction_id": "different-pending"}, {"pending": True}, {"transaction_id": "wrong-id"},
], ids=["account", "fractional-cents", "nonfinite", "malformed-amount", "currency", "date", "name", "pending-id", "pending-status", "payload-id"])
def test_legacy_recovery_requires_exact_financial_and_source_match(tmp_path, different):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source())
    with store.tx() as db:
        db.execute("DELETE FROM observations")
    observation(store, connection, source(**metadata(**different)))
    legacy(store)
    upgraded = Store(store.root)
    assert upgraded.snapshot()["staged"][0]["enrichment"] is None


@pytest.mark.parametrize("candidate", ["distinct-enrichment", "absent-enrichment", "malformed", "removed-only", "none"])
def test_legacy_ambiguous_or_missing_candidates_remain_null(tmp_path, candidate):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    if candidate == "distinct-enrichment":
        observation(store, connection, source(**metadata(merchant_name="Generated Different")))
    elif candidate == "absent-enrichment":
        observation(store, connection, source())
    else:
        with store.tx() as db:
            db.execute("DELETE FROM observations")
        if candidate == "malformed":
            observation(store, connection, ["not a payload"])
            with store.tx() as db:
                db.execute("UPDATE observations SET payload='not json'")
        elif candidate == "removed-only":
            observation(store, connection, source(**metadata()), kind="removed")
    legacy(store)
    before = records(store)
    upgraded = Store(store.root)
    assert upgraded.snapshot()["staged"][0]["enrichment"] is None
    after = records(upgraded)
    for row in after["staged"]:
        assert row.pop("enrichment_json") is None
    assert after == before


def test_schema_upgrade_and_backfill_roll_back_together_on_failure(tmp_path):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    legacy(store)
    before = records(store)
    with store.tx() as db:
        db.execute("CREATE TRIGGER generated_backfill_crash BEFORE UPDATE ON staged BEGIN SELECT RAISE(ABORT,'generated crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="generated crash"):
        Store(store.root)
    with store.tx() as db:
        assert "enrichment_json" not in {row["name"] for row in db.execute("PRAGMA table_info(staged)")}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='staged_source_invalidates_enrichment'").fetchone()
        db.execute("DROP TRIGGER generated_backfill_crash")
    assert records(store) == before
    assert Store(store.root).snapshot()["staged"][0]["enrichment"] == expected()


def test_normalized_merchant_and_original_search_precede_counts_and_pagination(tmp_path):
    store = Store(tmp_path / "generated")
    first = connect(store)
    second = connect(store, "b")
    rows = [source(f"generated-{index:02}", **metadata(merchant_name="Generated Search Merchant",
            original_description=f"Generated original needle {index}")) for index in range(13)]
    rows.extend([source("literal", **metadata(merchant_name="Generated 100% _ ! Merchant")),
                 source("plain", **metadata(merchant_name="Generated 100p X ! Merchant")),
                 source("original-literal", **metadata(merchant_name=None, original_description="Generated %_! needle")),
                 source("absent", **metadata(merchant_name=" " * 513, original_description=" " * 4097))])
    store.commit_window(first, "", "first", [("added", row) for row in rows], None, "generated-actor")
    apply(store, second, source(**metadata(merchant_name="Other connection needle")))
    for query in ("search merchant", "original needle"):
        collected = []
        for offset in range(0, 18, 4):
            data = store.activity(q=query, connection_id=first, offset=offset, limit=4)
            assert data["total"] == 13
            assert data["has_more"] == (offset + len(data["rows"]) < 13)
            collected.extend(row["transaction_id"] for row in data["rows"])
        assert collected == [f"generated-{index:02}" for index in range(13)]
    assert store.activity(q="100% _ !")["rows"][0]["transaction_id"] == "literal"
    assert store.activity(q="100% _ !")["total"] == 1
    assert store.activity(q="%_!")["rows"][0]["transaction_id"] == "original-literal"
    assert store.activity(q="' OR 1=1 --")["total"] == 0
    assert store.activity(q="Generated provider description", connection_id=first)["total"] == len(rows)
    assert store.activity(q="Generated Checking", connection_id=second)["total"] == 1
    assert store.activity(q="Other connection needle", connection_id=first)["total"] == 0


def test_every_sync_page_and_retry_requests_original_description(tmp_path):
    import httpx

    from app.plaid_sandbox.service import Service

    store = Store(tmp_path / "generated")
    store.configure("generated-client", "generated-secret", "plaid-sandbox.v1")
    connection = connect(store)
    responses = [
        {"added": [], "modified": [], "removed": [], "has_more": True, "next_cursor": "page-one"},
        {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"},
        {"added": [source(**metadata())], "modified": [], "removed": [], "has_more": True, "next_cursor": "page-two"},
        {"added": [], "modified": [], "removed": [], "has_more": False, "next_cursor": "end"},
    ]
    calls = []

    def transport(request):
        assert request.url.path == "/transactions/sync"
        calls.append(json.loads(request.content))
        result = responses.pop(0)
        return httpx.Response(400 if "error_code" in result else 200, json=result)

    svc = Service(store, transport=httpx.MockTransport(transport))
    changes, cursor = svc._window(store.connection(connection))
    assert cursor == "end" and changes == [("added", source(**metadata()))]
    assert [call["cursor"] for call in calls] == ["", "page-one", "", "page-two"]
    assert all(call.get("options") == {"include_original_description": True} for call in calls)


@pytest.mark.parametrize("writer", [Store, BaselineWriter], ids=["current", "rollback"])
@pytest.mark.parametrize("new_row", [False, True], ids=["update", "insert"])
@pytest.mark.parametrize("failure", ["cursor", "audit"], ids=["cursor-crash", "audit-crash"])
def test_enrichment_window_rolls_back_with_observations_cursor_and_audit(tmp_path, writer, new_row, failure):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    before = records(store)
    candidate = writer(store.root)
    row = source("new-id" if new_row else "equal-id", **metadata(merchant_name="Must roll back",
                 name="Generated changed source", amount="22.34"))
    with store.tx() as db:
        if failure == "cursor":
            db.execute("CREATE TRIGGER generated_window_crash BEFORE UPDATE OF cursor ON connections BEGIN SELECT RAISE(ABORT,'generated crash'); END")
        else:
            db.execute("CREATE TRIGGER generated_window_crash BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT,'generated crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="generated crash"):
        apply(candidate, connection, row)
    assert records(store) == before
    with store.tx() as db:
        db.execute("DROP TRIGGER generated_window_crash")
    old = store.connection(connection)["cursor"]
    with pytest.raises(ValueError, match="invalid amount precision"):
        candidate.commit_window(connection, old, "failed", [
            ("modified", row),
            ("added", source("bad-financial", amount="0.001", **metadata()))], None, "generated-actor")
    assert records(store) == before


@pytest.mark.parametrize("new_row", [False, True], ids=["update", "insert"])
def test_separate_projection_statement_failure_rolls_back_source_and_lifecycle(tmp_path, new_row):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    apply(store, connection, source("pending", pending=True, **metadata()))
    before = records(store)
    row = source("new-id" if new_row else "equal-id", **metadata(merchant_name="Generated Replacement",
                 name="Generated changed source", amount="22.34", pending_transaction_id="pending"))
    with store.tx() as db:
        # Allow invalidation to finish, then fail the current writer's restore.
        db.execute("""CREATE TRIGGER generated_projection_crash BEFORE UPDATE OF enrichment_json ON staged
            WHEN NEW.enrichment_json IS NOT NULL BEGIN SELECT RAISE(ABORT,'generated crash'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="generated crash"):
        apply(store, connection, row)
    assert records(store) == before
    with store.tx() as db:
        db.execute("DROP TRIGGER generated_projection_crash")
    apply(store, connection, row)
    result = {item["transaction_id"]: item for item in store.snapshot()["staged"]}
    assert result[row["transaction_id"]]["enrichment"] == expected(merchant_name="Generated Replacement")
    assert result[row["transaction_id"]]["name"] == row["name"]
    assert result[row["transaction_id"]]["amount_cents"] == -2234
    assert result["pending"]["state"] == "superseded"
    assert result["pending"]["enrichment"] == expected()


def test_rollback_lifecycle_retains_old_metadata_but_new_inserts_remain_null(tmp_path):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source("pending", pending=True, **metadata()))
    apply(store, connection, source("removed", **metadata()))
    rollback = BaselineWriter(store.root)
    apply(rollback, connection, source("replacement", pending_transaction_id="pending", **metadata()), kind="added")
    for transaction_id in ("pending", "removed", "replacement"):
        apply(rollback, connection, {"transaction_id": transaction_id}, kind="removed")
    before = records(store)
    store = Store(store.root)
    rows = {row["transaction_id"]: row for row in store.snapshot()["staged"]}
    assert rows["pending"]["state"] == "superseded" and rows["pending"]["enrichment"] == expected()
    assert rows["removed"]["state"] == "removed" and rows["removed"]["enrichment"] == expected()
    assert rows["replacement"]["state"] == "removed" and rows["replacement"]["enrichment"] is None
    assert records(store) == before


@pytest.mark.parametrize("recover_legacy", [False, True])
def test_lifecycle_retains_projection_and_source_without_cross_connection_leaks(tmp_path, recover_legacy):
    store = Store(tmp_path / "generated")
    first = connect(store)
    second = connect(store, "b")
    apply(store, first, source(**metadata(pending=True)))
    apply(store, second, source(**metadata(merchant_name="Other Item Merchant", pending=True)))
    pending = source("replacement", **metadata(merchant_name="Posted Merchant", pending_transaction_id="equal-id"))
    apply(store, first, pending, kind="added")
    apply(store, first, {"transaction_id": "equal-id"}, kind="removed")
    apply(store, first, {"transaction_id": "replacement"}, kind="removed")
    before = records(store)
    if recover_legacy:
        legacy(store)
        store = Store(store.root)
    rows = {(row["connection_id"], row["transaction_id"]): row for row in store.snapshot()["staged"]}
    assert rows[first, "equal-id"]["state"] == "superseded"
    assert rows[first, "equal-id"]["enrichment"] == expected()
    assert rows[first, "replacement"]["state"] == "removed"
    assert rows[first, "replacement"]["enrichment"] == expected(merchant_name="Posted Merchant")
    assert rows[second, "equal-id"]["state"] == "pending"
    assert rows[second, "equal-id"]["enrichment"] == expected(merchant_name="Other Item Merchant")
    assert store.activity(connection_id=first)["rows"] == []
    assert store.activity(connection_id=second)["total"] == 1
    assert records(store) == before
    store.disconnected(first, "generated-actor")
    store.disable("generated-actor")
    assert store.snapshot()["staged"] == list(rows.values())
    assert records(store)["observations"] == before["observations"]


def test_all_get_consumers_share_safe_projection_without_provider_or_database_mutation(tmp_path, monkeypatch):
    app = create_app(tmp_path / "generated", port=8772)
    store = app.state.store
    connection = connect(store)
    apply(store, connection, source(**metadata()))
    # Explicitly absent current metadata must not be recovered by reads from older evidence.
    apply(store, connection, source("absent", **metadata()))
    apply(store, connection, source("absent"))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")

        def forbidden(*args, **kwargs):
            raise AssertionError("GET must not load credentials, contact Plaid, or run recovery")

        monkeypatch.setattr(store, "credentials", forbidden)
        monkeypatch.setattr(app.state.service, "client", forbidden)
        monkeypatch.setattr(store, "_recover_enrichment", forbidden)
        before = records(store)
        for path, key in (("/api/sandbox/state", "staged"), ("/api/sandbox/overview", "recent"),
                          ("/api/sandbox/activity", "rows")):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            rows = {row["transaction_id"]: row for row in response.json()[key]}
            assert rows["equal-id"]["enrichment"] == expected()
            assert rows["absent"]["enrichment"] is None
            assert all("enrichment_json" not in row for row in rows.values())
            assert all(row["flow_kind"] == "unknown" and row["booked"] is False for row in rows.values())
            for hidden in ("forbidden-generated-token", "logo_url", "website", "access-sandbox", "link-sandbox"):
                assert hidden not in response.text
        assert records(store) == before


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{"merchant_name":{"wrong":"shape"}}'])
def test_response_projection_defensively_handles_invalid_private_json(tmp_path, raw):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source())
    with store.tx() as db:
        db.execute("UPDATE staged SET enrichment_json=?", (raw,))
    for row in (store.snapshot()["staged"][0], store.activity(q="provider")["rows"][0], store.overview()["recent"][0]):
        assert row["enrichment"] is None and "enrichment_json" not in row


@pytest.mark.parametrize("invalid", ["Generated\ud800Merchant", "Generated\x00Merchant"], ids=["surrogate", "nul"])
def test_invalid_optional_text_is_absent_without_losing_financial_update(tmp_path, invalid):
    store = Store(tmp_path / "generated")
    connection = connect(store)
    apply(store, connection, source(merchant_name=invalid, original_description=invalid))
    assert store.snapshot()["staged"][0]["enrichment"] is None
    assert store.snapshot()["staged"][0]["amount_cents"] == -1234
