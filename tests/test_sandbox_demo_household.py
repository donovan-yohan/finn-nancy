"""Public-media household. All identities, records and originals are synthetic."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess

import httpx

# Story associations only. Local accounts have no member-owner FK or permissions.
# Do not confuse these test-manifest associations with provider bank_bindings.
STORY = {
    "alex": {"account": "alex", "income": 425000, "spending": 14050,
             "transactions": ("alex_salary", "transit", "cycling")},
    "jo": {"account": "jo", "income": 365000, "spending": 9140,
           "transactions": ("jo_salary", "books", "cafe")},
}
ACCOUNTS = {"Alex chequing": 260950, "Jo chequing": 230860,
            "Household expenses": 24043, "Household savings": 30000}
BUDGETS = {"Groceries": (65000, None), "Home and utilities": (200000, None),
           "Internet": (9000, None), "Transport": (15000, "Alex"),
           "Books and hobbies": (10000, "Jo"), "Dining out": (8000, "Jo")}


def _report(conn, fixture, *, matched):
    from app.db import repo_budgets, repo_statement_expectations
    from app.reporting.period_statements import build_period_statement

    report = build_period_statement(conn, month=fixture.month, home_currency="CAD")
    assert report.household.income.cents == 790000
    assert report.household.net_money_out.cents == 244147
    assert report.household.external_cash_movement.cents == 545853
    assert report.household.transfer_neutrality_control.cents == 0
    assert report.household.unclassified_movement.cents == 0
    # Ledger movement is not a verified liquid position without stated balances.
    assert report.household.closing_liquid_position.cents == 0
    assert {e.account_name: e.reason for e in report.liquid_position_exclusions} == {
        name: "missing_stated_as_of_balance" for name in ACCOUNTS}
    assert {a.name: a.ledger_closing_balance.cents for a in report.accounts} == ACCOUNTS
    assert all(a.asserted_statement_closing_balance is None for a in report.accounts)
    expenses = [r for r in report.rows if r.flow_kind == "purchase"]
    assert len(expenses) == 9
    assert sum(-r.amount_cents for r in expenses) == 244147
    assert all(r.resolution_disposition == "human_approved" and r.canonical_merchant
               and r.evidence.canonical_merchant_claim_ids and r.evidence.category_claim_ids
               for r in expenses)
    assert report.expense_resolution.human_approved.cents == 244147
    assert report.expense_resolution.unresolved.cents == 0
    control = dict(conn.execute("SELECT * FROM v_expense_resolution_monthly_control WHERE month=?",
                                (fixture.month,)).fetchone())
    assert control["resolved_split_count"] == 9
    assert control["unresolved_split_count"] == control["excluded_expense_cents"] == 0
    assert not any(report.automation_authority.values())
    assert report.close.state not in {"clean_closed", "closed_with_exceptions"}
    matrix = repo_statement_expectations.period_matrix(conn, fixture.month)
    assert len(matrix) == 4 and all(r["requirement_state"] == "unconfigured" for r in matrix)
    budgets = repo_budgets.budget_management_context(conn)["budget_rows"]
    assert {b["category_name"]: (b["budget_cents"], b["budget_owner"])
            for b in budgets} == {k: (v, owner or "Shared") for k, (v, owner) in BUDGETS.items()}
    for person, story in STORY.items():
        rows = [r for r in report.rows if r.account_id == fixture.ids.accounts[story["account"]]]
        assert sum(r.amount_cents for r in rows if r.flow_kind == "income") == story["income"]
        assert sum(-r.amount_cents for r in rows if r.flow_kind == "purchase") == story["spending"]
        assert {fixture.ids.transactions[k] for k in story["transactions"]} <= {r.transaction_id for r in rows}
    shared = [r for r in expenses if r.account_id == fixture.ids.accounts["household"]]
    assert len(shared) == 5 and sum(-r.amount_cents for r in shared) == 220957
    market = next(r for r in expenses if r.transaction_id == fixture.ids.transactions["market"])
    assert set(market.source_document_ids) == ({fixture.ids.documents["receipt"], fixture.ids.documents["statement"]}
                                              if matched else {fixture.ids.documents["receipt"]})


def _csv(client, fixture):
    response = client.get("/statements/export", params={"month": fixture.month, "format": "csv"})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    totals = {r["key"]: int(r["cents"]) for r in rows if r["record_type"] == "household_total"}
    assert totals["income"] == 790000 and totals["net_money_out"] == 244147
    assert totals["external_cash_movement"] == 545853 and totals["transfer_neutrality_control"] == 0


def test_household_real_reports_both_people_accepted_categories_match_and_cleanup():
    from tools.sandbox.runner import launch_sandbox
    from tools.sandbox.scenarios import ExpectedTotals, ScenarioFixture

    with launch_sandbox("household-v1") as run:
        fixture = run.fixture
        assert fixture.month == "2026-06"
        assert fixture.expected == ExpectedTotals(17, 17, 790000, 244147, 0, 545853)
        assert ScenarioFixture.from_payload(fixture.payload()) == fixture
        manifest = json.loads(run.manifest_path.read_text())
        assert manifest["network"]["no_packets"] and manifest["provider_refusal"] == "egress_blocked"
        db = fixture.root / "state/ledger.sqlite"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            assert [r[0] for r in conn.execute("SELECT name FROM household_members WHERE is_active=1 ORDER BY name")] == ["Alex", "Jo"]
            assert {r[0] for r in conn.execute("SELECT name FROM accounts")} == set(ACCOUNTS)
            assert all(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0 for table in
                       ("bank_connections", "bank_accounts", "bank_bindings", "bank_consents", "jobs"))
            transactions = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
            splits = [tuple(r) for r in conn.execute("SELECT * FROM transaction_splits ORDER BY id")]
            assert len(transactions) == len(splits) == 17
            assert sum(r["amount_cents"] for r in transactions) == 545853
            assert all(r["flow_kind"] in {"income", "purchase", "internal_transfer"} for r in transactions)
            assert all("Synthetic" in r["notes"] for r in transactions)
            assert not conn.execute("SELECT t.id FROM transactions t JOIN transaction_splits s ON s.transaction_id=t.id "
                                    "GROUP BY t.id HAVING t.amount_cents!=sum(s.amount_cents)").fetchall()
            relationships = list(conn.execute("SELECT * FROM transaction_relationships"))
            assert len(relationships) == 3
            assert {r["id"] for r in relationships} == set(fixture.ids.relationships.values())
            for edge in relationships:
                assert edge["status"] == "active" and edge["relationship_kind"] == "transfer_pair"
                legs = [r for r in transactions if r["id"] in (edge["source_transaction_id"], edge["target_transaction_id"])]
                assert len(legs) == 2 and sum(r["amount_cents"] for r in legs) == 0
                assert len({r["account_id"] for r in legs}) == 2
            documents = [dict(r) for r in conn.execute("SELECT * FROM source_documents ORDER BY id")]
            assert len(documents) == 2
            hashes = {r["id"]: hashlib.sha256((fixture.root / "state/data" / r["storage_ref"]).read_bytes()).hexdigest()
                      for r in documents}
            assert hashes == {r["id"]: r["sha256"] for r in documents}
            line = dict(conn.execute("SELECT * FROM statement_lines").fetchone())
            assert line["match_status"] == "needs_review" and line["matched_transaction_id"] is None
            _report(conn, fixture, matched=False)
        with httpx.Client(base_url=run.origin, trust_env=False, timeout=10) as client:
            _csv(client, fixture)
            for route in ("/activity", "/connections", "/categories", "/insights", "/statements", "/close", "/recon"):
                response = client.get(route, params={"month": fixture.month})
                assert response.status_code == 200, (route, response.text)
                assert not re.search(r"Generated |Sample ?[AB]|User ?[AB]", response.text)
            response = client.post(f"/recon/line/{fixture.ids.statement_lines['market']}/confirm",
                                   data={"month": fixture.month, "transaction_id": fixture.ids.transactions["market"]})
            assert response.status_code == 303
            assert client.get(response.headers["location"]).status_code == 200
            _csv(client, fixture)
            page = client.get("/activity", params={"month": fixture.month}).text
            row = re.search(r'id="transaction-' + str(fixture.ids.transactions["market"]) + r'">(.*?)</details>', page, re.S)
            assert row is not None
            assert re.findall(r'href="(/review/doc/\d+/file)"', row[1]) == [
                f"/review/doc/{fixture.ids.documents['receipt']}/file", f"/review/doc/{fixture.ids.documents['statement']}/file"]
            for doc in documents:
                original = client.get(f"/review/doc/{doc['id']}/file")
                assert original.status_code == 200 and hashlib.sha256(original.content).hexdigest() == hashes[doc["id"]]
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 17
            assert [tuple(r) for r in conn.execute("SELECT * FROM transaction_splits ORDER BY id")] == splits
            line = dict(conn.execute("SELECT * FROM statement_lines").fetchone())
            assert line["match_status"] == "matched" and line["matched_transaction_id"] == fixture.ids.transactions["market"]
            assert all(hashlib.sha256((fixture.root / "state/data" / d["storage_ref"]).read_bytes()).hexdigest() == hashes[d["id"]]
                       for d in documents)
            _report(conn, fixture, matched=True)
        from tools.sandbox.scenarios import verify_scenario

        assert verify_scenario(fixture, matched=True)["category_resolution"]["resolved_split_count"] == 9
        root = fixture.root
        child_pid = run._process.pid
    assert not root.exists()
    assert run.cleanup == {"process_reaped": True, "root_removed": True, "child_pid": child_pid}


def test_household_cli_replay_uses_scenario_expected_totals():
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run([str(repo / "scripts/sandbox"), "replay", "--scenario", "household-v1",
                             "--journey", "match-receipt-v1"], cwd=repo,
                            capture_output=True, text=True, timeout=80)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "passed"
    assert receipt["fixture"]["name"] == "household-v1"
    assert receipt["http"]["status_code"] == 303
    assert receipt["initial"]["transaction_count"] == receipt["persisted"]["transaction_count"] == 17
    assert receipt["initial"]["split_count"] == receipt["persisted"]["split_count"] == 17
    assert receipt["persisted"]["ledger_net_cents"] == 545853
    assert receipt["persisted"]["report_totals"]["income"] == 790000
    assert receipt["persisted"]["report_totals"]["net_money_out"] == 244147
    assert receipt["initial"]["report_totals"] == receipt["persisted"]["report_totals"]
    assert receipt["initial"]["originals"] == receipt["persisted"]["originals"]
    assert receipt["persisted"]["accounts"] == ACCOUNTS
    assert receipt["persisted"]["members"] == {"alex": "Alex", "jo": "Jo"}
    assert receipt["persisted"]["category_resolution"]["unresolved_split_count"] == 0
    assert receipt["persisted"]["category_resolution"]["excluded_expense_cents"] == 0
    assert receipt["persisted"]["category_resolution"]["resolved_split_count"] == 9
    assert receipt["persisted"]["budgets"] == {k: {"cents": v, "owner": owner or "Shared"}
                                              for k, (v, owner) in BUDGETS.items()}
    assert receipt["cleanup"]["process_reaped"] and receipt["cleanup"]["root_removed"]
    assert not Path(receipt["root"]).exists()
