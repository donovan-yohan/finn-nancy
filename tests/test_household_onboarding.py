"""Generated household-first regressions; never opens operator/provider state."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.banking.service import BankingService
from app.db import engine, migrate, repo_budgets


MIGRATION = "042_household_onboarding.sql"


@pytest.fixture(autouse=True)
def no_provider_or_operator_settings(monkeypatch):
    import httpx
    from app.config import Settings, get_settings
    from app.banking import secrets as private

    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    get_settings.cache_clear()
    def forbidden(*args, **kwargs):
        pytest.fail('household operations must not read credentials or dispatch provider requests')
    from app.banking.client import PlaidClient
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(PlaidClient, 'post', forbidden)
    monkeypatch.setattr(private, 'private_read', forbidden)
    yield
    get_settings.cache_clear()


def service_for(path, **overrides):
    options = dict(db_path=path, private_dir=None, credentials_file=None,
                   origin="https://banking.example.invalid",
                   completion_uri="https://banking.example.invalid/connections",
                   enabled=lambda: False, strict_local=lambda: True, read_only=lambda: False)
    return BankingService(**(options | overrides))


def test_fresh_install_has_no_invented_people_and_replays(empty_db):
    service = service_for(empty_db)
    assert service.overview()["members"] == []
    with engine.read_conn(empty_db) as conn:
        assert repo_budgets.household_members(conn) == []
        assert conn.execute("SELECT count(*) FROM household_members WHERE is_active=1").fetchone()[0] == 0
    assert migrate.init_db(empty_db) == []
    assert service.overview()["members"] == []


def legacy_db(tmp_path, monkeypatch):
    canonical = migrate.MIGRATIONS_DIR
    old = tmp_path / "pre042-migrations"
    old.mkdir()
    for path in canonical.glob("*.sql"):
        if path.name < "042_":
            shutil.copy2(path, old / path.name)
    with monkeypatch.context() as patch:
        patch.setattr(migrate, "MIGRATIONS_DIR", old)
        path = tmp_path / "generated-legacy.sqlite"
        migrate.seed_sample(path)
    return path


@pytest.mark.parametrize("copied", [False, True])
@pytest.mark.parametrize("reference", ["unused", "budget", "consent", "connection", "binding", "session"])
@pytest.mark.parametrize("active", [0, 1])
def test_exact_seeds_retire_without_losing_history(tmp_path, monkeypatch, copied, reference, active):
    path = legacy_db(tmp_path, monkeypatch)
    from app.banking.service import CONSENT_VERSION
    with engine.write_tx(path) as conn:
        conn.execute("UPDATE household_members SET is_active=? WHERE id=1", (active,))
        if reference == "budget":
            conn.execute("INSERT INTO budgets(category_id,period_month,amount_cents,owner,owner_member_id) VALUES (5,'',15000,'sample_member_a',1)")
        if reference in {"consent", "session"}:
            conn.execute("INSERT INTO bank_consents(member_id,version,granted) VALUES (1,?,1)", (CONSENT_VERSION,))
        if reference == "binding":
            conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (2,'generated-item','generated-ref',?,'active')", (CONSENT_VERSION,))
        elif reference == "connection":
            conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (1,'generated-item','generated-ref',?,'active')", (CONSENT_VERSION,))
        if reference == "binding":
            conn.execute("INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (1,'generated','Generated bank','CAD','chequing')")
            conn.execute("INSERT INTO bank_bindings(bank_account_id,revision,account_id,included,ownership,owner_member_id,role,actor) VALUES (1,1,1,1,'personal',1,'primary','generated')")
        if reference == "session":
            conn.execute("INSERT INTO bank_link_sessions(id,browser_hash,member_id,token_ref,consent_id,state,expires_at) VALUES ('generated','generated',1,'generated',1,'pending',9999999999)")
        tables = ["budgets", "accounts", "transactions", "transaction_splits", "bank_consents", "bank_connections", "bank_accounts", "bank_bindings", "bank_link_sessions"]
        before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")] for t in tables}
        math = {v: [tuple(r) for r in conn.execute(f"SELECT month,category_id,budget_cents,actual_cents,remaining_cents,pct_used FROM {v} ORDER BY month,category_id")] for v in ["v_budget_vs_actual", "v_report_budget_vs_actual"]}
    source = path
    if copied:
        path = tmp_path / "generated-copy.sqlite"
        with sqlite3.connect(source) as src, sqlite3.connect(path) as dest:
            src.backup(dest)
    assert migrate.init_db(path) == [MIGRATION]
    with engine.read_conn(path) as conn:
        member = dict(conn.execute("SELECT * FROM household_members WHERE id=1").fetchone())
        assert member["name"].startswith("Name required")
        assert member["name_required"] == int(reference != "unused")
        assert member["is_active"] == (active if reference != "unused" else 0)
        audit = json.loads(conn.execute("SELECT value FROM app_settings WHERE key='__household_seed_retirement_042'").fetchone()[0])
        assert audit[0]["name"] == "Sample Member A"
        assert audit[0]["is_active"] == active
        assert {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")] for t in tables} == before
        assert {v: [tuple(r) for r in conn.execute(f"SELECT month,category_id,budget_cents,actual_cents,remaining_cents,pct_used FROM {v} ORDER BY month,category_id")] for v in math} == math
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "Sample Member" not in json.dumps(repo_budgets.budget_management_context(conn))
        assert "Sample Member" not in json.dumps(repo_budgets.insights_context(conn, "2026-03"))
    overview = service_for(path).overview()
    assert "Sample Member" not in json.dumps(overview)
    assert any(m["id"] == 1 and m["needs_name"] for m in overview["members"]) == (reference != "unused")
    assert migrate.init_db(path) == []
    if copied:
        with engine.read_conn(source) as conn:
            assert conn.execute("SELECT name FROM household_members WHERE id=1").fetchone()[0] == "Sample Member A"


@pytest.mark.parametrize("change", ["renamed", "manual", "slug"])
def test_migration_does_not_claim_user_people(tmp_path, monkeypatch, change):
    path = legacy_db(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        if change == "renamed":
            conn.execute("UPDATE household_members SET name='Éloïse <&>' WHERE id=1")
        elif change == "manual":
            conn.execute("UPDATE household_members SET source='manual' WHERE id=1")
        else:
            conn.execute("UPDATE household_members SET slug='actual-person' WHERE id=1")
        before = tuple(conn.execute("SELECT * FROM household_members WHERE id=1").fetchone())
    migrate.init_db(path)
    with engine.read_conn(path) as conn:
        after = tuple(conn.execute("SELECT * FROM household_members WHERE id=1").fetchone())
        assert after[:-1] == before
        assert after[-1] == 0


@pytest.mark.parametrize("name,code", [("", "member_name_required"), ("  ", "member_name_required"),
    ("a\nb", "member_name_control_characters"), ("a\x7fb", "member_name_control_characters"),
    ("a\x85b", "member_name_control_characters"), ("x" * 101, "member_name_too_long"),
    (None, "invalid_member_name")])
def test_repo_validates_member_names_for_every_caller(empty_db, name, code):
    with engine.write_tx(empty_db) as conn:
        with pytest.raises(ValueError, match=f"^{code}$"):
            repo_budgets.create_household_member(conn, name=name)
        assert repo_budgets.household_members(conn) == []


def test_member_names_remain_exact_and_duplicates_are_actionable(empty_db):
    from app.banking.client import BankingError
    service = service_for(empty_db)
    name = "  Éloïse O’Neil <&> 家庭  "
    member = service.add_member(name)["member_id"]
    assert next(m for m in service.overview()["members"] if m["id"] == member)["name"] == name
    with pytest.raises(BankingError, match="^member_name_exists$"):
        service.add_member(name.strip().upper())


@pytest.mark.parametrize("active", [0, 1])
def test_local_rename_uses_existing_identity_and_preserves_references(tmp_path, monkeypatch, active):
    path = legacy_db(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        conn.execute("UPDATE household_members SET is_active=? WHERE id=1", (active,))
        conn.execute("INSERT INTO budgets(category_id,period_month,amount_cents,owner,owner_member_id) VALUES (5,'',15000,'sample_member_a',1)")
        conn.execute("INSERT INTO bank_consents(member_id,version,granted) VALUES (1,'plaid-transactions-v1',1)")
        conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (1,'generated','generated','plaid-transactions-v1','active')")
        before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in ["budgets", "bank_consents", "bank_connections"]}
    migrate.init_db(path)
    service = service_for(path)
    assert service.rename_member(1, "李 <&> Éloïse") == {"member_id": 1}
    members = service.overview()["members"]
    if active:
        member = next(m for m in members if m["id"] == 1)
        assert member["name"] == "李 <&> Éloïse"
        assert member["needs_name"] is False
        assert member["consented"] is True
    else:
        assert all(m["id"] != 1 for m in members)
    with engine.read_conn(path) as conn:
        assert conn.execute("SELECT name FROM household_members WHERE id=1").fetchone()[0] == "李 <&> Éloïse"
        assert conn.execute("SELECT is_active,name_required FROM household_members WHERE id=1").fetchone()[:] == (active, 0)
        assert {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in before} == before
        assert conn.execute("SELECT slug FROM household_members WHERE id=1").fetchone()[0] == "sample_member_a"


def test_rename_validation_and_read_only_are_local(empty_db):
    from app.banking.client import BankingError
    service = service_for(empty_db)
    a = service.add_member("Generated Alex")["member_id"]
    service.add_member("Generated Jo")
    for name, code in [(" ", "member_name_required"), ("Generated Jo", "member_name_exists"),
                       ("Jo\n", "member_name_control_characters")]:
        with pytest.raises(BankingError, match=f"^{code}$"):
            service.rename_member(a, name)
        with engine.write_tx(empty_db) as conn:
            with pytest.raises(ValueError, match=f"^{code}$"):
                repo_budgets.rename_household_member(conn, member_id=a, name=name)
    with pytest.raises(BankingError, match="^member_not_found$"):
        service.rename_member(999, "Generated Nobody")
    service.read_only = lambda: True
    with pytest.raises(BankingError, match="^read_only$"):
        service.rename_member(a, "Generated New")
    assert next(m for m in service.overview()["members"] if m["id"] == a)["name"] == "Generated Alex"


def test_member_router_rename_authority_validation_and_readback(empty_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.banking.web import build_router
    service = service_for(empty_db)
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, base_url=service.origin) as client:
        csrf = client.get('/connections/data/overview').json()['csrf']
        headers = {'Origin': service.origin, 'X-Banking-CSRF': csrf}
        created = client.post('/connections/data/member', json={'name': 'Generated Alex'}, headers=headers)
        assert created.status_code == 200
        member_id = created.json()['member_id']
        target = f'/connections/data/member/{member_id}'
        assert client.post(target, json={'name': 'Generated Jo'}).status_code == 403
        assert client.post(target, json={'name': 'Generated Jo'}, headers=headers | {'Origin': 'https://evil.invalid'}).status_code == 403
        assert client.post(target, json={'name': 'Generated Jo'}, headers=headers | {'X-Banking-CSRF': 'invalid'}).status_code == 403
        renamed = client.post(target, json={'name': '李 <&> Éloïse'}, headers=headers)
        assert renamed.status_code == 200
        assert renamed.json() == {'member_id': member_id}
        assert next(m for m in client.get('/connections/data/overview').json()['members'] if m['id'] == member_id)['name'] == '李 <&> Éloïse'
        for name, code in [('', 'member_name_required'), (' ', 'member_name_required'), ('Jo\n', 'member_name_control_characters'), ('x'*101, 'member_name_too_long')]:
            for endpoint in ['/connections/data/member', target]:
                response = client.post(endpoint, json={'name': name}, headers=headers)
                assert response.status_code == 409
                assert response.json()['detail'] == code
        assert client.post(target, json={'name': 12}, headers=headers).status_code == 422
        assert client.post(target, json={'name': 'Jo', 'member_id': 4}, headers=headers).status_code == 422
        service.read_only = lambda: True
        response = client.post(target, json={'name': 'Generated New'}, headers=headers)
        assert response.status_code == 409 and response.json()['detail'] == 'read_only'


@pytest.mark.parametrize("flags,state", [
    ({"read_only": True, "enabled": False, "strict_local": True}, "read_only"),
    ({"enabled": False, "strict_local": True}, "disabled"),
    ({"enabled": True, "strict_local": True}, "strict_local"),
    ({"enabled": True, "strict_local": False}, "unconfigured"),
])
def test_setup_priorities_are_deterministic_and_do_not_touch_private_files(empty_db, monkeypatch, flags, state):
    service = service_for(empty_db, **{key: lambda value=value: value for key, value in flags.items()})
    def forbidden(*args, **kwargs):
        pytest.fail("overview must not inspect credential files or initialize private storage")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    from app.banking import secrets as private
    monkeypatch.setattr(private, "private_read", forbidden)
    result = service.overview()
    assert result["setup"]["state"] == state
    assert result["setup"]["ready"] is False
    assert result["network_allowed"] is False
    assert result["setup"]["message"]
    assert set(result["setup"]) == {"state", "ready", "message"}
    assert service._store is None


@pytest.mark.parametrize("changes,state", [
    ({}, "ready"), ({"private_dir": None}, "unconfigured"),
    ({"credentials_file": None}, "unconfigured"), ({"origin": ""}, "unconfigured"),
    ({"completion_uri": ""}, "unconfigured"),
    ({"origin": "https://banking.example.invalid/path"}, "invalid_configuration"),
    ({"origin": "https://" + "user" + "@" + "banking.example.invalid"}, "invalid_configuration"),
    ({"origin": "https://banking.example.invalid?query"}, "invalid_configuration"),
    ({"origin": "https://banking.example.invalid:bad"}, "invalid_configuration"),
    ({"completion_uri": "https://other.invalid/connections"}, "invalid_configuration"),
    ({"completion_uri": "http://banking.example.invalid/connections"}, "invalid_configuration"),
    ({"completion_uri": "https://banking.example.invalid/connections?code=1"}, "invalid_configuration"),
    ({"completion_uri": "https://banking.example.invalid/connections#finish"}, "invalid_configuration"),
    ({"completion_uri": "https://banking.example.invalid"}, "invalid_configuration"),
    ({"private_dir": Path("relative-private")}, "invalid_configuration"),
    ({"credentials_file": Path("relative-credentials")}, "invalid_configuration"),
])
def test_setup_requires_complete_valid_configuration_without_exposing_paths(empty_db, tmp_path, monkeypatch, changes, state):
    options = dict(private_dir=tmp_path / "not-created-private", credentials_file=tmp_path / "not-created-credentials",
                   enabled=lambda: True, strict_local=lambda: False)
    service = service_for(empty_db, **(options | changes))
    def forbidden(*args, **kwargs):
        pytest.fail("setup is configured-state only, never credential/file verification")
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    result = service.overview()
    assert result["setup"]["state"] == state
    assert result["setup"]["ready"] is (state == "ready")
    assert result["network_allowed"] is (state == "ready")
    assert str(tmp_path) not in json.dumps(result)
    assert service._store is None


def test_used_seed_cannot_start_link_until_named(tmp_path, monkeypatch):
    from app.banking.client import BankingError
    from app.banking.service import CONSENT_VERSION
    path = legacy_db(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        conn.execute("INSERT INTO bank_consents(member_id,version,granted) VALUES (1,?,1)", (CONSENT_VERSION,))
    migrate.init_db(path)
    calls = []
    service = service_for(path, enabled=lambda: True, strict_local=lambda: False,
                          private_dir=tmp_path / 'private', credentials_file=tmp_path / 'credentials')
    def provider(*args, **kwargs):
        calls.append(args)
        raise BankingError('generated_provider_boundary')
    monkeypatch.setattr(service, '_post', provider)
    with pytest.raises(BankingError, match='^member_name_required$'):
        service.start_link(member_id=1, browser='generated')
    assert calls == []
    service.rename_member(1, 'Generated Alex')
    with pytest.raises(BankingError, match='^generated_provider_boundary$'):
        service.start_link(member_id=1, browser='generated')
    assert len(calls) == 1


def test_rename_uses_real_validated_api_authority(empty_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.authority import RequestAuthorityMiddleware
    from app.config import Settings, get_settings
    from app.banking.web import build_router
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    monkeypatch.setenv('API_TOKEN', 'generated-member-test-key')
    get_settings.cache_clear()
    app = FastAPI()
    service = service_for(empty_db)
    app.include_router(build_router(service))
    app.add_middleware(RequestAuthorityMiddleware)
    try:
        with TestClient(app, base_url=service.origin) as client:
            headers = {'Authorization': 'Bearer generated-member-test-key'}
            created = client.post('/connections/data/member', json={'name': 'Generated API person'}, headers=headers)
            assert created.status_code == 200
            target = f"/connections/data/member/{created.json()['member_id']}"
            assert client.post(target, json={'name': 'Generated New'}, headers={'Authorization': 'Bearer incorrect'}).status_code == 401
            renamed = client.post(target, json={'name': 'Generated New'}, headers=headers)
            assert renamed.status_code == 200 and renamed.headers['x-fn-caller'] == 'api'
            assert service.overview()['members'][0]['name'] == 'Generated New'
    finally:
        get_settings.cache_clear()


def test_migration_neutral_label_collision_and_legacy_budget_fallback(tmp_path, monkeypatch):
    path = legacy_db(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        conn.execute("INSERT INTO household_members(name,slug,source) VALUES ('Name required (legacy 1)','actual-person','manual')")
        conn.execute("INSERT INTO budgets(category_id,period_month,amount_cents,owner) VALUES (5,'2026-03',15000,'sample_member_a')")
    migrate.init_db(path)
    with engine.read_conn(path) as conn:
        assert conn.execute("SELECT name FROM household_members WHERE id=1").fetchone()[0] == 'Name required (legacy 1) #1'
        assert conn.execute("SELECT name FROM household_members WHERE slug='actual-person'").fetchone()[0] == 'Name required (legacy 1)'
        for view in ['v_budget_vs_actual', 'v_report_budget_vs_actual', 'v_category_underspend_monthly']:
            assert conn.execute(f"SELECT budget_owner FROM {view} WHERE category_id=5 AND month='2026-03'").fetchone()[0] == 'Name required'
        row = next(r for r in repo_budgets.budget_management_context(conn)['budget_rows'] if r['category_id'] == 5)
        assert row['budget_owner'] == 'Shared'  # monthly override is not the default editor


def test_copied_migration_preserves_booked_bank_associations(tmp_path, monkeypatch):
    path = legacy_db(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        txn = conn.execute('SELECT * FROM transactions ORDER BY id LIMIT 1').fetchone()
        account = conn.execute('SELECT * FROM accounts WHERE id=?', (txn['account_id'],)).fetchone()
        conn.execute("INSERT INTO bank_consents(member_id,version,granted) VALUES (1,'plaid-transactions-v1',1)")
        conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (1,'generated','generated','plaid-transactions-v1','active')")
        conn.execute("INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (1,'generated','Generated',?,?)", (account['currency'], account['kind']))
        conn.execute("INSERT INTO bank_bindings(bank_account_id,revision,account_id,included,ownership,owner_member_id,role,actor) VALUES (1,1,?,1,'personal',1,'primary','generated')", (account['id'],))
        conn.execute("INSERT INTO bank_windows(connection_id,generation,from_cursor,to_cursor) VALUES (1,1,'','generated')")
        conn.execute("INSERT INTO bank_observations(window_id,connection_id,provider_transaction_id,provider_account_id,kind,amount_cents,currency,posted_on,pending,merchant,description) VALUES (1,1,'generated','generated','added',?,?,?,0,'Generated','Generated')", (txn['amount_cents'], account['currency'], txn['posted_on']))
        conn.execute("INSERT INTO bank_subjects(connection_id,provider_transaction_id,observation_id) VALUES (1,'generated',1)")
        conn.execute("INSERT INTO bank_operations(operation_key,request_json,result_json,actor,reason) VALUES ('generated','{}','{}','generated','Generated')")
        conn.execute("INSERT INTO bank_associations(subject_id,observation_id,binding_id,transaction_id,operation_key,actor,reason) VALUES (1,1,1,?,'generated','generated','Generated')", (txn['id'],))
        tables = ['transactions', 'transaction_splits', 'bank_associations', 'bank_observations', 'bank_operations', 'bank_bindings', 'bank_consents', 'bank_connections']
        before = {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in tables}
    copy = tmp_path / 'booked-copy.sqlite'
    with sqlite3.connect(path) as src, sqlite3.connect(copy) as dest:
        src.backup(dest)
    assert migrate.init_db(copy) == [MIGRATION]
    assert migrate.init_db(copy) == []
    with engine.read_conn(copy) as conn:
        assert {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in tables} == before
        assert conn.execute('SELECT count(*) FROM v_bank_associations').fetchone()[0] == 1
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    service = service_for(copy)
    assert service.feed()[0]['member_name'].startswith('Name required')
    service.rename_member(1, 'Generated Actual Person')
    assert service.feed()[0]['member_name'] == 'Generated Actual Person'
