"""Finite generated fixtures. App imports belong only in the isolated child."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ScenarioName = Literal["empty-v1", "showcase-v1", "household-v1"]
SCENARIOS = ("empty-v1", "showcase-v1", "household-v1")


@dataclass(frozen=True)
class FixtureIds:
    accounts: dict[str, int] = field(default_factory=dict)
    categories: dict[str, int] = field(default_factory=dict)
    members: dict[str, int] = field(default_factory=dict)
    transactions: dict[str, int] = field(default_factory=dict)
    documents: dict[str, int] = field(default_factory=dict)
    statement_lines: dict[str, int] = field(default_factory=dict)
    relationships: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    goals: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedTotals:
    transaction_count: int = 0
    split_count: int = 0
    income_cents: int = 0
    money_out_cents: int = 0
    transfer_net_cents: int = 0
    ledger_net_cents: int = 0


@dataclass(frozen=True)
class ScenarioFixture:
    schema_version: int
    name: ScenarioName
    root: Path
    month: str
    ids: FixtureIds
    expected: ExpectedTotals
    routes: tuple[str, ...]

    def payload(self) -> dict:
        return {**asdict(self), "root": str(self.root)}

    @classmethod
    def from_payload(cls, value: dict) -> ScenarioFixture:
        return cls(**{**value, "root": Path(value["root"]),
                      "ids": FixtureIds(**value["ids"]),
                      "expected": ExpectedTotals(**value["expected"]),
                      "routes": tuple(value["routes"])})


def build_scenario(root: Path, name: ScenarioName) -> ScenarioFixture:
    if name not in SCENARIOS:
        raise ValueError("unknown scenario")
    from app.db import migrate

    db = root / "state/ledger.sqlite"
    if db.exists():
        raise ValueError("scenario requires a new database")
    migrate.init_db(db)
    fixture = ScenarioFixture(1, name, root, "2026-06", FixtureIds(),
                              ExpectedTotals(), ("/manage", "/activity?month=2026-06"))
    if name == "showcase-v1":
        fixture = _showcase(fixture)
    elif name == "household-v1":
        fixture = _household(fixture)
    verify_scenario(fixture)
    return fixture


def _showcase(empty: ScenarioFixture) -> ScenarioFixture:
    # Adapted from the generated media recipe. No ingestion/model results invented.
    import hashlib
    from PIL import Image, ImageDraw
    from app.accounting.flows import create_relationship
    from app.db import engine, repo_budgets, repo_documents, repo_ledger, repo_statements
    from app.db import repo_merchant_knowledge as knowledge
    from app.ingest.schemas import ExtractedStatement, StatementRow

    root = empty.root
    ids = empty.ids
    assets = root / "artifacts"
    for label, merchant, cents in (("market", "Cedar Market", 6842),
                                    ("pantry", "Willow Pantry", 2375),
                                    ("cafe", "Moss Cafe", 1290)):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.multiline_text((45, 70), f"SYNTHETIC RECEIPT\n\nGenerated {merchant}\n2026-06-12\n\n"
                            f"Total CAD {cents // 100}.{cents % 100:02}\n\n"
                            "Generated demonstration\nNot a real purchase",
                            fill="black", spacing=18)
        image.save(assets / f"generated-{label}-receipt.png")
    blobdir = root / "state/data/blobs"
    blobdir.mkdir()
    with engine.write_tx(root / "state/ledger.sqlite") as conn:
        # Account/category management routes use these same inserts, no repo writer exists.
        for label, name, kind in (("cash", "Generated household cash", "cash"),
                                   ("savings", "Generated savings", "savings")):
            ids.accounts[label] = int(conn.execute(
                "INSERT INTO accounts(name,institution,kind,external_ref) VALUES (?,'',?,'')",
                (name, kind)).lastrowid)
        for name, kind in (("Groceries", "expense"), ("Home and utilities", "expense"),
                           ("Transport", "expense"), ("Salary", "income"), ("Transfers", "transfer")):
            ids.categories[name] = int(conn.execute(
                "INSERT INTO categories(name,kind,brand_owner,color) VALUES (?,?,?,?)",
                (name, kind, "nancy" if kind == "expense" else "finn", "#2475d0")).lastrowid)
        for label, name in (("alex", "Generated Alex"), ("jo", "Generated Jo")):
            ids.members[label] = repo_budgets.create_household_member(conn, name=name)
        for name, cents in (("Groceries", 65000), ("Home and utilities", 180000), ("Transport", 15000)):
            repo_budgets.set_category_budget(conn, category_id=ids.categories[name],
                                            amount_cents=cents, owner_member_id=ids.members["alex"])
        original = assets / "generated-market-receipt.png"
        content = original.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        target = blobdir / (sha + ".png")
        target.write_bytes(content)
        ids.documents["receipt"] = repo_documents.insert_source_document(
            conn, kind="receipt", original_name=original.name,
            storage_ref=str(target.relative_to(root / "state/data")), sha256=sha,
            mime_type="image/png", status="processed")
        rows = (
            ("salary", "03", "Harbor Studio payroll", 425000, "income", "Salary", "cash"),
            ("rent", "05", "Alder Homes rent", -145000, "purchase", "Home and utilities", "cash"),
            ("transit", "09", "Fern Transit", -9200, "purchase", "Transport", "cash"),
            ("market", "12", "Cedar Market", -6842, "purchase", "Groceries", "cash"),
            ("utilities", "15", "Rain Utilities", -8745, "purchase", "Home and utilities", "cash"),
            ("transfer_out", "18", "savings allocation", -50000, "internal_transfer", "Transfers", "cash"),
            ("transfer_in", "18", "savings allocation", 50000, "internal_transfer", "Transfers", "savings"),
        )
        for label, day, description, cents, flow, category, account in rows:
            descriptor = "Generated " + description
            txn = repo_ledger.insert_transaction(
                conn, account_id=ids.accounts[account], posted_on=f"2026-06-{day}",
                description=descriptor, counterparty=descriptor, amount_cents=cents,
                source="manual", external_id=f"generated-showcase-{label}",
                source_document_id=ids.documents["receipt"] if label == "market" else None,
                source_confidence=1.0, flow_kind=flow, notes="Synthetic demonstration")
            assert txn is not None
            ids.transactions[label] = txn
            split = repo_ledger.insert_split(conn, transaction_id=txn,
                                            category_id=ids.categories[category], amount_cents=cents)
            if flow == "purchase":
                scope = knowledge.scope_for_transaction(conn, txn)
                evidence = knowledge.Evidence(transaction_id=txn, transaction_split_id=split)
                knowledge.confirm_merchant(conn, descriptor=descriptor, canonical_name=descriptor,
                    scope=scope, operation_key=f"generated-merchant-{label}", actor="generated-alex",
                    reason="Explicit synthetic fixture decision", evidence=evidence)
                knowledge.confirm_category(conn, descriptor=descriptor, category_id=ids.categories[category],
                    scope=scope, operation_key=f"generated-category-{label}", actor="generated-alex",
                    reason="Explicit synthetic fixture decision", evidence=evidence)
        ids.relationships["savings"] = create_relationship(
            conn, relationship_kind="transfer_pair",
            source_transaction_id=ids.transactions["transfer_out"],
            target_transaction_id=ids.transactions["transfer_in"], actor="generated-alex",
            reason="Explicit synthetic owned-account savings allocation")
        content = b"date,description,amount,currency\n2026-06-12,Generated Cedar Market,-68.42,CAD\n"
        statement = blobdir / "generated-june-statement.csv"
        statement.write_bytes(content)
        ids.documents["statement"] = repo_documents.insert_source_document(
            conn, kind="statement", original_name=statement.name,
            storage_ref=str(statement.relative_to(root / "state/data")),
            sha256=hashlib.sha256(content).hexdigest(), mime_type="text/csv", status="needs_review")
        parsed = ExtractedStatement(currency="CAD", statement_period="2026-06", rows=[
            StatementRow(posted_on="2026-06-12", description="Generated Cedar Market", amount_cents=-6842)],
            confidence=1.0)
        assert repo_statements.stage_lines(conn, source_document_id=ids.documents["statement"],
                                          account_id=ids.accounts["cash"], parsed=parsed) == {"staged": 1, "duplicates": 0}
        ids.statement_lines["market"] = int(conn.execute("SELECT id FROM statement_lines").fetchone()[0])
        repo_statements.set_match(conn, ids.statement_lines["market"], status="needs_review",
                                  rationale="Synthetic pre-staged statement, not booked")
    return ScenarioFixture(1, empty.name, root, empty.month, ids,
                           ExpectedTotals(7, 7, 425000, 169787, 0, 255213),
                           ("/activity?month=2026-06", "/recon?month=2026-06",
                            "/close?month=2026-06", "/manage", "/upload"))


def _household(empty: ScenarioFixture) -> ScenarioFixture:
    """Ordinary local records, not provider observations or authenticated owners."""
    import hashlib
    from PIL import Image, ImageDraw
    from app.accounting.flows import create_relationship
    from app.db import engine, repo_budgets, repo_documents, repo_ledger, repo_statements
    from app.db import repo_merchant_knowledge as knowledge
    from app.ingest.schemas import ExtractedStatement, StatementRow

    root, ids = empty.root, empty.ids
    assets = root / "artifacts"
    for label, merchant, cents, day in (("market", "Cedar Market", 6842, "12"),
                                       ("pantry", "Willow Pantry", 12375, "22"),
                                       ("cafe", "Moss Cafe", 1890, "13")):
        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).multiline_text(
            (45, 70), f"SYNTHETIC RECEIPT\n\n{merchant}\n2026-06-{day}\n\n"
            f"Total CAD {cents // 100}.{cents % 100:02}\n\n"
            "Fictional demonstration\nNot a real purchase", fill="black", spacing=18)
        image.save(assets / f"{label}-receipt.png")
    blobdir = root / "state/data/blobs"
    blobdir.mkdir()
    with engine.write_tx(root / "state/ledger.sqlite") as conn:
        # Same local management writes as showcase-v1. No account-owner FK exists.
        for label, name, kind in (("alex", "Alex chequing", "chequing"),
                                  ("jo", "Jo chequing", "chequing"),
                                  ("household", "Household expenses", "chequing"),
                                  ("savings", "Household savings", "savings")):
            ids.accounts[label] = int(conn.execute(
                "INSERT INTO accounts(name,institution,kind,external_ref) VALUES (?,'',?,'')",
                (name, kind)).lastrowid)
        for name, kind in (("Groceries", "expense"), ("Home and utilities", "expense"),
                           ("Internet", "expense"), ("Transport", "expense"),
                           ("Books and hobbies", "expense"), ("Dining out", "expense"),
                           ("Salary", "income"), ("Transfers", "transfer")):
            ids.categories[name] = int(conn.execute(
                "INSERT INTO categories(name,kind,brand_owner,color) VALUES (?,?,?,?)",
                (name, kind, "nancy" if kind == "expense" else "finn", "#2475d0")).lastrowid)
        for label, name in (("alex", "Alex"), ("jo", "Jo")):
            ids.members[label] = repo_budgets.create_household_member(conn, name=name)
        for category, cents, owner in (("Groceries", 65000, None),
                                       ("Home and utilities", 200000, None),
                                       ("Internet", 9000, None), ("Transport", 15000, "alex"),
                                       ("Books and hobbies", 10000, "jo"), ("Dining out", 8000, "jo")):
            repo_budgets.set_category_budget(conn, category_id=ids.categories[category],
                amount_cents=cents, owner_member_id=ids.members[owner] if owner else None)
        original = assets / "market-receipt.png"
        content = original.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target = blobdir / (digest + ".png")
        target.write_bytes(content)
        ids.documents["receipt"] = repo_documents.insert_source_document(
            conn, kind="receipt", original_name=original.name,
            storage_ref=str(target.relative_to(root / "state/data")), sha256=digest,
            mime_type="image/png", status="processed")
        # Tuple fields match showcase-v1: key, day, descriptor, cents, flow, category, account.
        rows = (
            ("alex_salary", "03", "Harbor Studio payroll", 425000, "income", "Salary", "alex"),
            ("jo_salary", "03", "Juniper Library payroll", 365000, "income", "Salary", "jo"),
            ("alex_household_out", "04", "Household contribution", -150000, "internal_transfer", "Transfers", "alex"),
            ("alex_household_in", "04", "Household contribution", 150000, "internal_transfer", "Transfers", "household"),
            ("jo_household_out", "04", "Household contribution", -125000, "internal_transfer", "Transfers", "jo"),
            ("jo_household_in", "04", "Household contribution", 125000, "internal_transfer", "Transfers", "household"),
            ("rent", "05", "Alder Homes rent", -185000, "purchase", "Home and utilities", "household"),
            ("transit", "09", "Fern Transit", -9200, "purchase", "Transport", "alex"),
            ("books", "10", "Birch Books", -7250, "purchase", "Books and hobbies", "jo"),
            ("cycling", "11", "Elm Cycle repairs", -4850, "purchase", "Transport", "alex"),
            ("market", "12", "Cedar Market", -6842, "purchase", "Groceries", "household"),
            ("cafe", "13", "Moss Cafe", -1890, "purchase", "Dining out", "jo"),
            ("utilities", "15", "Rain Utilities", -8745, "purchase", "Home and utilities", "household"),
            ("internet", "16", "Meadow Internet", -7995, "purchase", "Internet", "household"),
            ("transfer_out", "18", "Household savings allocation", -30000, "internal_transfer", "Transfers", "household"),
            ("transfer_in", "18", "Household savings allocation", 30000, "internal_transfer", "Transfers", "savings"),
            ("pantry", "22", "Willow Pantry", -12375, "purchase", "Groceries", "household"),
        )
        for label, day, descriptor, cents, flow, category, account in rows:
            txn = repo_ledger.insert_transaction(
                conn, account_id=ids.accounts[account], posted_on=f"2026-06-{day}",
                description=descriptor, counterparty=descriptor, amount_cents=cents,
                source="manual", external_id=f"household-v1-{label}",
                source_document_id=ids.documents["receipt"] if label == "market" else None,
                source_confidence=1.0, flow_kind=flow, notes="Synthetic household demonstration")
            assert txn is not None
            ids.transactions[label] = txn
            split = repo_ledger.insert_split(conn, transaction_id=txn,
                category_id=ids.categories[category], amount_cents=cents)
            if flow == "purchase":
                scope = knowledge.scope_for_transaction(conn, txn)
                evidence = knowledge.Evidence(transaction_id=txn, transaction_split_id=split)
                knowledge.confirm_merchant(conn, descriptor=descriptor, canonical_name=descriptor,
                    scope=scope, operation_key=f"household-v1-merchant-{label}", actor="sandbox-fixture",
                    reason="Explicit synthetic fixture decision", evidence=evidence)
                knowledge.confirm_category(conn, descriptor=descriptor, category_id=ids.categories[category],
                    scope=scope, operation_key=f"household-v1-category-{label}", actor="sandbox-fixture",
                    reason="Explicit synthetic fixture decision", evidence=evidence)
        for label, source, target in (("alex_household", "alex_household_out", "alex_household_in"),
                                      ("jo_household", "jo_household_out", "jo_household_in"),
                                      ("savings", "transfer_out", "transfer_in")):
            ids.relationships[label] = create_relationship(
                conn, relationship_kind="transfer_pair", source_transaction_id=ids.transactions[source],
                target_transaction_id=ids.transactions[target], actor="sandbox-fixture",
                reason="Synthetic local-account transfer, no personal ownership assertion")
        # Structured staging, not an assertion that a parser or model ran.
        content = b"date,description,amount,currency\n2026-06-12,Cedar Market,-68.42,CAD\n"
        statement = blobdir / "june-statement.csv"
        statement.write_bytes(content)
        ids.documents["statement"] = repo_documents.insert_source_document(
            conn, kind="statement", original_name=statement.name,
            storage_ref=str(statement.relative_to(root / "state/data")),
            sha256=hashlib.sha256(content).hexdigest(), mime_type="text/csv", status="needs_review")
        parsed = ExtractedStatement(currency="CAD", statement_period="2026-06", rows=[
            StatementRow(posted_on="2026-06-12", description="Cedar Market", amount_cents=-6842)],
            confidence=1.0)
        assert repo_statements.stage_lines(conn, source_document_id=ids.documents["statement"],
            account_id=ids.accounts["household"], parsed=parsed) == {"staged": 1, "duplicates": 0}
        ids.statement_lines["market"] = int(conn.execute(
            "SELECT id FROM statement_lines WHERE source_document_id=?",
            (ids.documents["statement"],)).fetchone()[0])
        repo_statements.set_match(conn, ids.statement_lines["market"], status="needs_review",
                                  rationale="Synthetic pre-staged statement, not booked")
    return ScenarioFixture(1, empty.name, root, empty.month, ids,
        ExpectedTotals(17, 17, 790000, 244147, 0, 545853),
        ("/activity?month=2026-06", "/recon?month=2026-06", "/statements?month=2026-06",
         "/close?month=2026-06", "/connections", "/categories", "/insights?month=2026-06", "/upload"))


def verify_scenario(fixture: ScenarioFixture, *, matched: bool = False) -> dict:
    from app.db import engine, migrate
    from app.reporting.period_statements import build_period_statement

    with engine.read_conn(fixture.root / "state/ledger.sqlite") as conn:
        counts = tuple(conn.execute(
            "SELECT (SELECT count(*) FROM transactions),"
            "(SELECT count(*) FROM transaction_splits)").fetchone())
        expected = fixture.expected
        assert counts == (expected.transaction_count, expected.split_count), counts
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert not conn.execute("SELECT t.id FROM transactions t LEFT JOIN transaction_splits s "
                                "ON s.transaction_id=t.id GROUP BY t.id "
                                "HAVING t.amount_cents != coalesce(sum(s.amount_cents),0)").fetchall()
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        report = build_period_statement(conn, month=fixture.month, home_currency="CAD")
        totals = dict(income_cents=report.household.income.cents,
                      money_out_cents=report.household.net_money_out.cents,
                      transfer_net_cents=report.household.transfer_neutrality_control.cents,
                      ledger_net_cents=sum(r.amount_cents for r in report.rows))
        assert totals == {key: getattr(expected, key) for key in totals}, totals
        import hashlib

        originals = []
        for document in conn.execute("SELECT id,storage_ref,sha256 FROM source_documents ORDER BY id"):
            path = fixture.root / "state/data" / document["storage_ref"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == document["sha256"]
            originals.append(dict(document))
        if fixture.name == "showcase-v1":
            ids = fixture.ids
            line = conn.execute("SELECT * FROM statement_lines WHERE id=?",
                                (ids.statement_lines["market"],)).fetchone()
            assert line["match_status"] == ("matched" if matched else "needs_review")
            assert line["matched_transaction_id"] == (ids.transactions["market"] if matched else None)
            market = conn.execute("SELECT * FROM transactions WHERE id=?", (ids.transactions["market"],)).fetchone()
            assert market["amount_cents"] == -6842
            assert market["source_document_id"] == ids.documents["receipt"]
            assert market["recon_status"] == ("cleared" if matched else "uncleared")
            relationship = conn.execute("SELECT * FROM transaction_relationships WHERE id=?",
                                        (ids.relationships["savings"],)).fetchone()
            assert relationship["status"] == "active" and relationship["relationship_kind"] == "transfer_pair"
            assert relationship["source_transaction_id"] == ids.transactions["transfer_out"]
            assert relationship["target_transaction_id"] == ids.transactions["transfer_in"]
            assert [row[0] for row in conn.execute("SELECT amount_cents FROM budgets ORDER BY amount_cents")] == [15000, 65000, 180000]
            assert conn.execute("SELECT count(*) FROM household_members WHERE is_active=1").fetchone()[0] == 2
            assert report.close.state not in {"clean_closed", "closed_with_exceptions"}
            assert {a.account_id: a.ledger_closing_balance.cents for a in report.accounts} == {
                ids.accounts["cash"]: 205213, ids.accounts["savings"]: 50000}
        if fixture.name == "household-v1":
            ids = fixture.ids
            assert {a.name: a.ledger_closing_balance.cents for a in report.accounts} == {
                "Alex chequing": 260950, "Jo chequing": 230860,
                "Household expenses": 24043, "Household savings": 30000}
            assert report.household.external_cash_movement.cents == expected.ledger_net_cents
            assert report.household.unclassified_movement.cents == 0
            assert report.expense_resolution.human_approved.cents == expected.money_out_cents
            assert report.expense_resolution.unresolved.cents == 0
            expenses = [r for r in report.rows if r.flow_kind == "purchase"]
            assert len(expenses) == 9
            assert all(r.resolution_disposition == "human_approved" and r.canonical_merchant
                       and r.evidence.canonical_merchant_claim_ids and r.evidence.category_claim_ids
                       for r in expenses)
            assert report.household.closing_liquid_position.cents == 0
            assert len(report.liquid_position_exclusions) == 4
            assert all(e.reason == "missing_stated_as_of_balance" for e in report.liquid_position_exclusions)
            assert not any(report.automation_authority.values())
            assert report.close.state not in {"clean_closed", "closed_with_exceptions"}
            line = conn.execute("SELECT * FROM statement_lines WHERE id=?",
                                (ids.statement_lines["market"],)).fetchone()
            assert line["match_status"] == ("matched" if matched else "needs_review")
            assert line["matched_transaction_id"] == (ids.transactions["market"] if matched else None)
            market = next(r for r in expenses if r.transaction_id == ids.transactions["market"])
            assert market.amount_cents == -6842
            assert set(market.source_document_ids) == (
                {ids.documents["receipt"], ids.documents["statement"]} if matched else {ids.documents["receipt"]})
            assert market.reconciliation_state == ("cleared" if matched else "uncleared")
            assert conn.execute("SELECT count(*) FROM transaction_relationships WHERE status='active' "
                                "AND relationship_kind='transfer_pair'").fetchone()[0] == 3
            assert all(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                       for table in ("bank_connections", "bank_accounts", "bank_bindings", "bank_consents"))
        from app.db import repo_budgets, repo_statement_expectations

        control = conn.execute("SELECT * FROM v_expense_resolution_monthly_control WHERE month=?",
                               (fixture.month,)).fetchone()
        category_resolution = dict(control) if control else {}
        budgets = {r["category_name"]: {"cents": r["budget_cents"], "owner": r["budget_owner"]}
                   for r in repo_budgets.budget_management_context(conn)["budget_rows"]}
        members = {key: conn.execute("SELECT name FROM household_members WHERE id=?", (value,)).fetchone()[0]
                   for key, value in fixture.ids.members.items()}
        matrix = repo_statement_expectations.period_matrix(conn, fixture.month)
        if fixture.name == "household-v1":
            assert category_resolution["resolved_split_count"] == 9
            assert category_resolution["unresolved_split_count"] == category_resolution["excluded_expense_cents"] == 0
            assert members == {"alex": "Alex", "jo": "Jo"}
            assert budgets == {
                "Groceries": {"cents": 65000, "owner": "Shared"},
                "Home and utilities": {"cents": 200000, "owner": "Shared"},
                "Internet": {"cents": 9000, "owner": "Shared"},
                "Transport": {"cents": 15000, "owner": "Alex"},
                "Books and hobbies": {"cents": 10000, "owner": "Jo"},
                "Dining out": {"cents": 8000, "owner": "Jo"}}
            assert len(matrix) == 4 and all(r["requirement_state"] == "unconfigured" for r in matrix)
        return {"totals": totals, "transaction_count": counts[0], "split_count": counts[1],
                "database_identity": migrate.read_database_identity(conn), "originals": originals,
                "accounts": {a.name: a.ledger_closing_balance.cents for a in report.accounts},
                "members": members, "budgets": budgets, "category_resolution": category_resolution,
                "close_state": report.close.state, "automation_authority": report.automation_authority,
                "statement_requirements": {r["account_name"]: r["requirement_state"] for r in matrix},
                "liquid_position_exclusions": {e.account_name: e.reason for e in report.liquid_position_exclusions},
                "matched": matched}
