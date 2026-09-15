import pytest

pytest.importorskip("langgraph")

import sqlite3
from typing import Any

from app.agents.recon_analyst import review_reconciliation
from app.agents.recon_analyst.evidence import gather_all_evidence, resolve_statement_run
from app.agents.recon_analyst.graph import AnalystOutput, SynthesisOutput
from app.agents.recon_analyst.schemas import Evidence, ProposedAction, ReviewFinding
from app.db import engine, repo_ledger, repo_statements
from app.evals import (
    check_action_safety,
    check_citations,
    check_coverage,
    check_emission_read_only,
    check_recurring_insights,
    check_subscriptions,
    check_trajectory,
    db_snapshot,
    enqueue_review_actions,
    expected_recurring_merchants,
    run_traced_review,
)

_RECURRING_FINDING_KINDS = {
    "recurring_change",
    "recurring_price_increase",
    "recurring_price_decrease",
}


def _insert_doc(conn: sqlite3.Connection, name: str) -> int:
    return int(
        conn.execute(
            """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
               VALUES ('statement', ?, ?, ?, 'application/pdf', 'matched')""",
            (name, f"eval-recon/{name}", f"sha-eval-recon-{name}"),
        ).lastrowid
    )


def _insert_expense(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
    cleared: bool = True,
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} payment",
        counterparty=merchant,
        amount_cents=-abs(amount_cents),
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=-abs(amount_cents),
    )
    if cleared:
        repo_statements.mark_cleared(conn, txn_id, posted_on)
    return int(txn_id)


def _insert_receipt_candidate(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} receipt",
        counterparty=merchant,
        amount_cents=-abs(amount_cents),
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert txn_id is not None
    return int(txn_id)


def _insert_statement_line(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    account_id: int,
    posted_on: str,
    description: str,
    amount_cents: int,
    row_hash: str,
    match_status: str = "unmatched",
    matched_transaction_id: int | None = None,
    match_method: str = "",
    match_score: float = 0.0,
    match_rationale: str = "",
) -> int:
    return int(
        conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, is_pending, row_hash, match_status,
                 matched_transaction_id, match_method, match_score, match_rationale)
               VALUES (?,?,?,?,?,?, 'CAD',0,?,?,?,?,?,?)""",
            (
                doc_id,
                account_id,
                posted_on,
                description,
                description,
                amount_cents,
                row_hash,
                match_status,
                matched_transaction_id,
                match_method,
                match_score,
                match_rationale,
            ),
        ).lastrowid
    )


def _insert_matched_charge(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
    row_hash: str,
) -> tuple[int, int]:
    txn_id = _insert_expense(
        conn,
        account_id=account_id,
        category_id=category_id,
        posted_on=posted_on,
        merchant=merchant,
        amount_cents=amount_cents,
        external_id=external_id,
    )
    line_id = _insert_statement_line(
        conn,
        doc_id=doc_id,
        account_id=account_id,
        posted_on=posted_on,
        description=merchant.upper(),
        amount_cents=-abs(amount_cents),
        row_hash=row_hash,
        match_status="matched",
        matched_transaction_id=txn_id,
        match_method="exact",
        match_score=1.0,
    )
    return txn_id, line_id


def _base_review_ids(ids: dict[str, Any]) -> tuple[list[ReviewFinding], list[ProposedAction]]:
    findings = [
        ReviewFinding(
            finding_id="coverage-gaps",
            kind="coverage_summary",
            title="June statement has grounded coverage",
            detail="Coverage cites matched and unmatched statement rows.",
            priority=5,
            severity="watch",
            confidence=0.82,
            evidence=Evidence(
                statement_line_ids=[ids["grocery_line"], ids["unmatched_line"]],
            ),
        ),
        ReviewFinding(
            finding_id="unmatched-hardware",
            kind="unmatched_line",
            title="Hardware Depot needs receipt review",
            detail="The unmatched statement line has a same-amount transaction candidate.",
            priority=1,
            severity="warn",
            confidence=0.9,
            evidence=Evidence(
                transaction_ids=[ids["candidate_txn"]],
                statement_line_ids=[ids["unmatched_line"]],
            ),
        ),
    ]
    actions = [
        ProposedAction(
            kind="confirm_match",
            payload={
                "statement_line_id": ids["unmatched_line"],
                "transaction_id": ids["candidate_txn"],
            },
            evidence=Evidence(
                transaction_ids=[ids["candidate_txn"]],
                statement_line_ids=[ids["unmatched_line"]],
            ),
            confidence=0.88,
            rationale="Same amount and nearby posting date.",
            agent_run_id="pending",
        )
    ]
    return findings, actions


class _FakeStructured:
    def __init__(self, llm: "_FaithfulReconLLM", schema: type[Any]):
        self._llm = llm
        self._schema = schema

    def invoke(self, messages):
        return self._llm.next(self._schema)


class _FaithfulReconLLM:
    def __init__(self, ids: dict[str, Any], *, fixture: str):
        self.ids = ids
        self.fixture = fixture
        self.methods: list[str] = []
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        self.methods.append(kwargs["method"])
        return _FakeStructured(self, schema)

    def next(self, schema):
        if schema is AnalystOutput:
            self.calls += 1
            return self._analyst_output(self.calls)
        if schema is SynthesisOutput:
            return self._synthesis_output()
        raise AssertionError(f"unexpected schema {schema}")

    def _analyst_output(self, call_number: int) -> AnalystOutput:
        findings, actions = _base_review_ids(self.ids)
        if call_number == 1:
            return AnalystOutput(findings=[findings[0]], proposed_actions=[])
        if call_number == 2:
            return AnalystOutput(
                findings=[findings[1]],
                proposed_actions=actions if self.fixture == "warranted" else [],
            )
        if call_number == 3 and self.fixture == "warranted":
            return AnalystOutput(findings=[self._recurring_finding()], proposed_actions=[])
        if call_number == 4 and self.fixture == "warranted":
            return AnalystOutput(
                findings=[self._subscription_finding()],
                proposed_actions=[self._subscription_action()],
            )
        return AnalystOutput(findings=[], proposed_actions=[])

    def _recurring_finding(self) -> ReviewFinding:
        return ReviewFinding(
            finding_id="koodo-change",
            kind="recurring_change",
            title="Koodo recurring charge increased",
            detail="Koodo changed by a meaningful amount in the tool evidence.",
            priority=3,
            severity="watch",
            confidence=0.87,
            evidence=Evidence(
                transaction_ids=self.ids["koodo_change_txns"],
                statement_line_ids=self.ids["koodo_change_lines"],
            ),
        )

    def _subscription_finding(self) -> ReviewFinding:
        return ReviewFinding(
            finding_id="fresh-stream",
            kind="new_subscription",
            title="Fresh Stream looks newly recurring",
            detail="Fresh Stream appears in exactly two consecutive months.",
            priority=4,
            severity="watch",
            confidence=0.84,
            evidence=Evidence(
                transaction_ids=self.ids["fresh_txns"],
                statement_line_ids=self.ids["fresh_lines"],
            ),
        )

    def _subscription_action(self) -> ProposedAction:
        return ProposedAction(
            kind="add_subscription",
            payload={"merchant": "Fresh Stream", "account_id": self.ids["account_id"]},
            evidence=Evidence(
                transaction_ids=self.ids["fresh_txns"],
                statement_line_ids=self.ids["fresh_lines"],
            ),
            confidence=0.83,
            rationale="Two consecutive equal monthly charges.",
            agent_run_id="pending",
        )

    def _synthesis_output(self) -> SynthesisOutput:
        findings, actions = _base_review_ids(self.ids)
        if self.fixture == "warranted":
            findings.extend([self._recurring_finding(), self._subscription_finding()])
            actions.append(self._subscription_action())
        else:
            actions = []
        return SynthesisOutput(
            period_summary="Grounded reconciliation review for June.",
            findings=findings,
            proposed_actions=actions,
        )


def _seed_warranted_fixture(db_path: str) -> dict[str, Any]:
    with engine.write_tx(db_path) as conn:
        account_id = int(
            conn.execute(
                "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Eval Card','Test','credit','CAD')"
            ).lastrowid
        )
        groceries_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Eval Groceries','expense','nancy','#4EA1FF')"
            ).lastrowid
        )
        recurring_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Eval Recurring','expense','nancy','#ff9f43')"
            ).lastrowid
        )
        household_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Eval Household','expense','shared','#2ecc71')"
            ).lastrowid
        )

        apr_doc = _insert_doc(conn, "eval-april.pdf")
        may_doc = _insert_doc(conn, "eval-may.pdf")
        jun_doc = _insert_doc(conn, "eval-june.pdf")

        grocery_txn, grocery_line = _insert_matched_charge(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            category_id=groceries_id,
            posted_on="2026-06-03",
            merchant="Green Grocer",
            amount_cents=4200,
            external_id="eval-grocery-jun",
            row_hash="eval-grocery-jun",
        )
        candidate_txn = _insert_receipt_candidate(
            conn,
            account_id=account_id,
            posted_on="2026-06-06",
            merchant="Hardware Depot",
            amount_cents=6700,
            external_id="eval-hardware-candidate",
        )
        unmatched_line = _insert_statement_line(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            posted_on="2026-06-06",
            description="HARDWARE DEPOT",
            amount_cents=-6700,
            row_hash="eval-hardware-line",
            match_status="unmatched",
        )
        ignored_line = _insert_statement_line(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            posted_on="2026-06-09",
            description="CARD PAYMENT",
            amount_cents=-5000,
            row_hash="eval-card-payment",
            match_status="ignored",
        )
        income_line = _insert_statement_line(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            posted_on="2026-06-10",
            description="REFUND",
            amount_cents=1500,
            row_hash="eval-refund",
            match_status="ignored",
        )

        koodo_apr, koodo_apr_line = _insert_matched_charge(
            conn,
            doc_id=apr_doc,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-04-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="eval-koodo-apr",
            row_hash="eval-koodo-apr",
        )
        koodo_may, koodo_may_line = _insert_matched_charge(
            conn,
            doc_id=may_doc,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-05-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="eval-koodo-may",
            row_hash="eval-koodo-may",
        )
        koodo_jun, koodo_jun_line = _insert_matched_charge(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-06-04",
            merchant="Koodo",
            amount_cents=5210,
            external_id="eval-koodo-jun",
            row_hash="eval-koodo-jun",
        )

        tiny_txns: list[int] = []
        tiny_lines: list[int] = []
        for month, date, amount in [
            ("apr", "2026-04-08", 30000),
            ("may", "2026-05-08", 30000),
            ("jun", "2026-06-08", 30450),
        ]:
            txn, line = _insert_matched_charge(
                conn,
                doc_id={"apr": apr_doc, "may": may_doc, "jun": jun_doc}[month],
                account_id=account_id,
                category_id=recurring_id,
                posted_on=date,
                merchant="Tiny Variance Cloud",
                amount_cents=amount,
                external_id=f"eval-tiny-{month}",
                row_hash=f"eval-tiny-{month}",
            )
            tiny_txns.append(txn)
            tiny_lines.append(line)

        flat_txns: list[int] = []
        flat_lines: list[int] = []
        for month, date in [
            ("apr", "2026-04-12"),
            ("may", "2026-05-12"),
            ("jun", "2026-06-12"),
        ]:
            txn, line = _insert_matched_charge(
                conn,
                doc_id={"apr": apr_doc, "may": may_doc, "jun": jun_doc}[month],
                account_id=account_id,
                category_id=recurring_id,
                posted_on=date,
                merchant="Flat Gym",
                amount_cents=1999,
                external_id=f"eval-flat-{month}",
                row_hash=f"eval-flat-{month}",
            )
            flat_txns.append(txn)
            flat_lines.append(line)

        fresh_may, fresh_may_line = _insert_matched_charge(
            conn,
            doc_id=may_doc,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-05-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="eval-fresh-may",
            row_hash="eval-fresh-may",
        )
        fresh_jun, fresh_jun_line = _insert_matched_charge(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-06-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="eval-fresh-jun",
            row_hash="eval-fresh-jun",
        )

    return {
        "doc_id": jun_doc,
        "account_id": account_id,
        "groceries_id": groceries_id,
        "recurring_id": recurring_id,
        "household_id": household_id,
        "grocery_txn": grocery_txn,
        "grocery_line": grocery_line,
        "candidate_txn": candidate_txn,
        "unmatched_line": unmatched_line,
        "ignored_line": ignored_line,
        "income_line": income_line,
        "koodo_txns": [koodo_apr, koodo_may, koodo_jun],
        "koodo_lines": [koodo_apr_line, koodo_may_line, koodo_jun_line],
        "koodo_change_txns": [koodo_may, koodo_jun],
        "koodo_change_lines": [koodo_may_line, koodo_jun_line],
        "tiny_txns": tiny_txns,
        "tiny_lines": tiny_lines,
        "flat_txns": flat_txns,
        "flat_lines": flat_lines,
        "fresh_txns": [fresh_may, fresh_jun],
        "fresh_lines": [fresh_may_line, fresh_jun_line],
        "june_lines": [
            grocery_line,
            unmatched_line,
            ignored_line,
            income_line,
            koodo_jun_line,
            tiny_lines[-1],
            flat_lines[-1],
            fresh_jun_line,
        ],
    }


def _seed_negative_space_fixture(db_path: str) -> dict[str, Any]:
    with engine.write_tx(db_path) as conn:
        account_id = int(
            conn.execute(
                "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Negative Space Card','Test','credit','CAD')"
            ).lastrowid
        )
        groceries_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Negative Groceries','expense','nancy','#4EA1FF')"
            ).lastrowid
        )
        recurring_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Negative Recurring','expense','nancy','#ff9f43')"
            ).lastrowid
        )

        apr_doc = _insert_doc(conn, "negative-april.pdf")
        may_doc = _insert_doc(conn, "negative-may.pdf")
        jun_doc = _insert_doc(conn, "negative-june.pdf")

        flat_txns: list[int] = []
        flat_lines: list[int] = []
        for month, date in [
            ("apr", "2026-04-12"),
            ("may", "2026-05-12"),
            ("jun", "2026-06-12"),
        ]:
            txn, line = _insert_matched_charge(
                conn,
                doc_id={"apr": apr_doc, "may": may_doc, "jun": jun_doc}[month],
                account_id=account_id,
                category_id=recurring_id,
                posted_on=date,
                merchant="Steady Cloud",
                amount_cents=2500,
                external_id=f"negative-steady-{month}",
                row_hash=f"negative-steady-{month}",
            )
            flat_txns.append(txn)
            flat_lines.append(line)

        grocery_txn, grocery_line = _insert_matched_charge(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            category_id=groceries_id,
            posted_on="2026-06-03",
            merchant="Corner Market",
            amount_cents=2600,
            external_id="negative-grocery",
            row_hash="negative-grocery",
        )
        one_off_txn, one_off_line = _insert_matched_charge(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            category_id=groceries_id,
            posted_on="2026-06-18",
            merchant="One Off Shop",
            amount_cents=4200,
            external_id="negative-one-off",
            row_hash="negative-one-off",
        )
        unmatched_line = _insert_statement_line(
            conn,
            doc_id=jun_doc,
            account_id=account_id,
            posted_on="2026-06-20",
            description="UNMATCHED CAFE",
            amount_cents=-3300,
            row_hash="negative-unmatched",
            match_status="unmatched",
        )

    return {
        "doc_id": jun_doc,
        "account_id": account_id,
        "groceries_id": groceries_id,
        "recurring_id": recurring_id,
        "grocery_txn": grocery_txn,
        "grocery_line": grocery_line,
        "candidate_txn": one_off_txn,
        "unmatched_line": unmatched_line,
        "one_off_txn": one_off_txn,
        "one_off_line": one_off_line,
        "flat_txns": flat_txns,
        "flat_lines": flat_lines,
    }


def _review(db_path: str, ids: dict[str, Any], *, fixture: str):
    fake_llm = _FaithfulReconLLM(ids, fixture=fixture)
    review = review_reconciliation(
        db_path,
        source_document_id=ids["doc_id"],
        llm_factory=lambda: fake_llm,
    )
    assert fake_llm.methods == ["json_schema"] * 5
    return review


def _finding_by_id(review: Any, finding_id: str) -> ReviewFinding:
    return next(finding for finding in review.findings if finding.finding_id == finding_id)


def _evidence_bundle(db_path: str, ids: dict[str, Any]):
    with engine.read_conn(db_path) as conn:
        run = resolve_statement_run(conn, source_document_id=ids["doc_id"])
        return gather_all_evidence(conn, run)


def _install_buggy_coverage_view(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_statement_coverage_by_doc;
        DROP VIEW IF EXISTS v_statement_coverage_lines;

        CREATE VIEW v_statement_coverage_lines AS
        SELECT
          sl.id AS line_id,
          sl.source_document_id,
          sd.original_name AS document_name,
          sd.status AS document_status,
          sl.account_id,
          COALESCE(a.name, 'Unassigned') AS account_name,
          sl.posted_on,
          strftime('%Y-%m', sl.posted_on) AS month,
          sl.raw_description,
          sl.norm_merchant,
          sl.amount_cents,
          CASE WHEN sl.amount_cents < 0 THEN ABS(sl.amount_cents) ELSE 0 END AS spend_cents,
          CASE WHEN sl.amount_cents > 0 THEN sl.amount_cents ELSE 0 END AS income_cents,
          sl.match_status,
          sl.matched_transaction_id,
          sl.match_method,
          sl.match_score,
          sl.match_rationale,
          CASE
            WHEN sl.amount_cents > 0 THEN 'income'
            WHEN sl.match_status IN ('matched', 'promoted') THEN 'covered'
            ELSE 'unmatched'
          END AS coverage_bucket,
          COALESCE(
            (
              SELECT GROUP_CONCAT(c.name, ', ')
              FROM transaction_splits ts
              JOIN categories c ON c.id = ts.category_id
              WHERE ts.transaction_id = sl.matched_transaction_id
            ),
            ''
          ) AS category_names,
          CASE
            WHEN sl.amount_cents >= 0 THEN ''
            WHEN sl.match_status = 'ignored' THEN 'ignored/internal transfer'
            WHEN sl.account_id IS NULL THEN 'needs account'
            WHEN sl.match_status IN ('matched', 'promoted')
              AND EXISTS (
                SELECT 1
                FROM transaction_splits ts
                JOIN categories c ON c.id = ts.category_id
                WHERE ts.transaction_id = sl.matched_transaction_id
                  AND c.name = 'Uncategorized'
              )
              THEN 'needs category'
            WHEN sl.match_status IN ('unmatched', 'needs_review')
              AND EXISTS (
                SELECT 1
                FROM transactions t
                WHERE t.account_id IS sl.account_id
                  AND t.amount_cents = sl.amount_cents
                  AND t.recon_status = 'cleared'
                  AND t.posted_on BETWEEN date(sl.posted_on, '-2 days') AND date(sl.posted_on, '+2 days')
              )
              THEN 'possible duplicate'
            WHEN sl.match_status IN ('unmatched', 'needs_review')
              AND EXISTS (
                SELECT 1
                FROM transactions t
                WHERE t.account_id IS sl.account_id
                  AND t.amount_cents = sl.amount_cents
                  AND t.recon_status = 'uncleared'
                  AND t.posted_on BETWEEN date(sl.posted_on, '-7 days') AND date(sl.posted_on, '+1 days')
              )
              THEN 'ambiguous match'
            WHEN sl.match_status IN ('unmatched', 'needs_review')
              AND NOT EXISTS (
                SELECT 1
                FROM transactions t
                WHERE lower(t.description) LIKE '%' || lower(sl.norm_merchant) || '%'
                   OR lower(t.counterparty) LIKE '%' || lower(sl.norm_merchant) || '%'
              )
              THEN 'new merchant'
            WHEN sl.match_status IN ('unmatched', 'needs_review') THEN 'missing receipt'
            ELSE ''
          END AS attention_reason
        FROM statement_lines sl
        JOIN source_documents sd ON sd.id = sl.source_document_id
        LEFT JOIN accounts a ON a.id = sl.account_id;

        CREATE VIEW v_statement_coverage_by_doc AS
        WITH grouped AS (
          SELECT
            source_document_id,
            document_name,
            document_status,
            MIN(posted_on) AS first_posted_on,
            MAX(posted_on) AS last_posted_on,
            COUNT(*) AS line_count,
            SUM(spend_cents) AS statement_spend_cents,
            SUM(CASE WHEN coverage_bucket = 'covered' THEN spend_cents ELSE 0 END) AS covered_spend_cents,
            SUM(CASE WHEN coverage_bucket = 'unmatched' THEN spend_cents ELSE 0 END) AS unmatched_spend_cents,
            SUM(CASE WHEN coverage_bucket = 'ignored' THEN spend_cents ELSE 0 END) AS ignored_spend_cents,
            SUM(income_cents) AS income_cents,
            SUM(CASE WHEN attention_reason <> '' AND attention_reason <> 'ignored/internal transfer' THEN spend_cents ELSE 0 END) AS attention_spend_cents
          FROM v_statement_coverage_lines
          GROUP BY source_document_id
        )
        SELECT
          *,
          CASE
            WHEN covered_spend_cents + unmatched_spend_cents = 0 THEN 0
            ELSE ROUND(100.0 * covered_spend_cents / (covered_spend_cents + unmatched_spend_cents), 1)
          END AS coverage_pct
        FROM grouped;
        """
    )


def _assert_common_checks_pass(db_path: str, review: Any, evidence_bundle: Any) -> None:
    with engine.read_conn(db_path) as conn:
        assert check_coverage(review, conn).ok
        assert check_citations(review, conn).ok
    assert check_recurring_insights(review, evidence_bundle).ok
    assert check_subscriptions(review, evidence_bundle).ok
    assert check_action_safety(review, db_path).ok


def test_warranted_fixture_passes_all_recon_insight_evals(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    before = db_snapshot(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)

    assert expected_recurring_merchants(evidence_bundle) == {"Koodo"}
    recurring_findings = [item for item in review.findings if item.kind == "recurring_change"]
    assert len(recurring_findings) == 1
    assert set(recurring_findings[0].evidence.transaction_ids) == set(ids["koodo_change_txns"])
    assert "Tiny Variance Cloud" not in expected_recurring_merchants(evidence_bundle)
    assert "Flat Gym" not in expected_recurring_merchants(evidence_bundle)
    recurring_text = " ".join(
        f"{finding.title} {finding.detail}"
        for finding in review.findings
        if finding.kind in {"recurring_change", "recurring_price_increase", "recurring_price_decrease"}
    )
    assert "Tiny Variance Cloud" not in recurring_text
    assert "Flat Gym" not in recurring_text

    assert [
        (candidate.merchant, candidate.account_id)
        for candidate in evidence_bundle.planning.subscription_candidates
    ] == [("Fresh Stream", ids["account_id"])]
    assert [finding.kind for finding in review.findings].count("new_subscription") == 1
    assert [action.kind for action in review.proposed_actions] == [
        "confirm_match",
        "add_subscription",
    ]

    with engine.read_conn(empty_db) as conn:
        assert check_coverage(review, conn).ok
        assert check_citations(review, conn).ok
    assert check_recurring_insights(review, evidence_bundle).ok
    assert check_subscriptions(review, evidence_bundle).ok
    safety = check_action_safety(review, empty_db)
    assert safety.ok
    assert [item["kind"] for item in safety.details["checked_actions"]] == [
        "receipt_match",
        "subscription_label",
    ]
    assert check_emission_read_only(empty_db, before).ok

    traced_review, traces = run_traced_review(
        empty_db,
        source_document_id=ids["doc_id"],
        llm=_FaithfulReconLLM(ids, fixture="warranted"),
    )
    with engine.read_conn(empty_db) as conn:
        run = resolve_statement_run(conn, source_document_id=ids["doc_id"])
        trajectory = check_trajectory(traces, conn, run)
    assert traced_review.coverage == review.coverage
    assert trajectory.ok


def test_negative_space_fixture_has_no_recurring_or_subscription_findings(empty_db):
    ids = _seed_negative_space_fixture(empty_db)
    before = db_snapshot(empty_db)
    review = _review(empty_db, ids, fixture="negative")
    evidence_bundle = _evidence_bundle(empty_db, ids)

    assert evidence_bundle.recurring.changes == []
    assert evidence_bundle.planning.subscription_candidates == []
    assert [finding for finding in review.findings if finding.kind.startswith("recurring")] == []
    assert [finding for finding in review.findings if finding.kind == "new_subscription"] == []
    assert review.proposed_actions == []

    _assert_common_checks_pass(empty_db, review, evidence_bundle)
    assert check_emission_read_only(empty_db, before).ok


def test_negative_control_hallucinated_citation_fails_check(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    with engine.read_conn(empty_db) as conn:
        assert check_citations(review, conn).ok

        violated = review.model_copy(deep=True)
        _finding_by_id(violated, "coverage-gaps").evidence = Evidence(transaction_ids=[999999])
        result = check_citations(violated, conn)

    assert result.ok is False
    assert "finding coverage-gaps transaction_id 999999 not found" in result.violations


def test_negative_control_empty_citation_fails_check(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    with engine.read_conn(empty_db) as conn:
        assert check_citations(review, conn).ok

        violated = review.model_copy(deep=True)
        _finding_by_id(violated, "coverage-gaps").evidence = Evidence()
        result = check_citations(violated, conn)

    assert result.ok is False
    assert "finding coverage-gaps has empty evidence" in result.violations


def test_negative_control_wrong_coverage_totals_fail_check(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    with engine.read_conn(empty_db) as conn:
        assert check_coverage(review, conn).ok

        violated = review.model_copy(deep=True)
        assert violated.coverage is not None
        violated.coverage = violated.coverage.model_copy(
            update={
                "unmatched_spend_cents": violated.coverage.unmatched_spend_cents + 1234,
                "coverage_pct": 12.3,
            }
        )
        result = check_coverage(violated, conn)

    assert result.ok is False
    assert any("coverage unmatched_spend_cents" in item for item in result.violations)
    assert any("coverage coverage_pct" in item for item in result.violations)


def test_negative_control_buggy_coverage_view_fails_independent_ground_truth(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    with engine.read_conn(empty_db) as conn:
        assert check_coverage(review, conn).ok

    with engine.write_tx(empty_db) as conn:
        _install_buggy_coverage_view(conn)

    buggy_review = _review(empty_db, ids, fixture="warranted")
    with engine.read_conn(empty_db) as conn:
        result = check_coverage(buggy_review, conn)

    assert result.ok is False
    assert any("coverage ignored_spend_cents" in item for item in result.violations)
    assert any("coverage unmatched_spend_cents" in item for item in result.violations)


def test_negative_control_omitted_recurring_change_fails_recall(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_recurring_insights(review, evidence_bundle).ok

    violated = review.model_copy(deep=True)
    violated.findings = [
        finding
        for finding in violated.findings
        if finding.kind not in _RECURRING_FINDING_KINDS
    ]
    result = check_recurring_insights(violated, evidence_bundle)

    assert result.ok is False
    assert (
        "meaningful recurring change Koodo 2026-05->2026-06 not represented in findings"
    ) in result.violations


def test_negative_control_recurring_direction_mismatch_fails_check(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_recurring_insights(review, evidence_bundle).ok

    increase = review.model_copy(deep=True)
    _finding_by_id(increase, "koodo-change").kind = "recurring_price_increase"
    assert check_recurring_insights(increase, evidence_bundle).ok

    decrease = review.model_copy(deep=True)
    _finding_by_id(decrease, "koodo-change").kind = "recurring_price_decrease"
    result = check_recurring_insights(decrease, evidence_bundle)

    assert result.ok is False
    assert any(
        "claims decrease" in item and "direction is increase" in item
        for item in result.violations
    )


def test_negative_control_subscription_candidate_omission_fails_recall(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_subscriptions(review, evidence_bundle).ok

    no_finding = review.model_copy(deep=True)
    no_finding.findings = [
        finding
        for finding in no_finding.findings
        if finding.kind != "new_subscription"
    ]
    assert check_subscriptions(no_finding, evidence_bundle).ok

    no_action = review.model_copy(deep=True)
    no_action.proposed_actions = [
        action
        for action in no_action.proposed_actions
        if action.kind != "add_subscription"
    ]
    assert check_subscriptions(no_action, evidence_bundle).ok

    silent = review.model_copy(deep=True)
    silent.findings = [
        finding
        for finding in silent.findings
        if finding.kind != "new_subscription"
    ]
    silent.proposed_actions = [
        action
        for action in silent.proposed_actions
        if action.kind != "add_subscription"
    ]
    result = check_subscriptions(silent, evidence_bundle)

    assert result.ok is False
    assert (
        f"subscription candidate Fresh Stream account {ids['account_id']} "
        "not represented in findings or actions"
    ) in result.violations


def test_negative_control_subscription_action_requires_candidate_evidence(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_subscriptions(review, evidence_bundle).ok

    violated = review.model_copy(deep=True)
    action = next(action for action in violated.proposed_actions if action.kind == "add_subscription")
    action.evidence = Evidence(
        transaction_ids=[ids["grocery_txn"]],
        statement_line_ids=[ids["grocery_line"]],
    )
    result = check_subscriptions(violated, evidence_bundle)

    assert result.ok is False
    assert (
        f"subscription action 1 add_subscription evidence does not cite "
        f"candidate rows for Fresh Stream account {ids['account_id']}"
    ) in result.violations


def test_negative_control_hallucinated_recurring_insight_fails_check(empty_db):
    ids = _seed_negative_space_fixture(empty_db)
    review = _review(empty_db, ids, fixture="negative")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_recurring_insights(review, evidence_bundle).ok

    violated = review.model_copy(deep=True)
    violated.findings.append(
        ReviewFinding(
            finding_id="steady-cloud-change",
            kind="recurring_change",
            title="Steady Cloud changed",
            detail="This is not warranted by the recurring delta tool.",
            priority=3,
            severity="watch",
            confidence=0.7,
            evidence=Evidence(
                transaction_ids=ids["flat_txns"][:2],
                statement_line_ids=ids["flat_lines"][:2],
            ),
        )
    )
    result = check_recurring_insights(violated, evidence_bundle)

    assert result.ok is False
    assert "recurring finding steady-cloud-change cites no meaningful recurring change" in result.violations


def test_negative_control_warranted_recurring_hallucination_exercises_overlap_predicate(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    faithful = check_recurring_insights(review, evidence_bundle)
    assert faithful.ok
    assert faithful.details["matched_findings"] == {"koodo-change": "Koodo"}

    violated = review.model_copy(deep=True)
    violated.findings.append(
        ReviewFinding(
            finding_id="tiny-cloud-change",
            kind="recurring_change",
            title="Tiny Variance Cloud changed",
            detail="This cites real rows, but not the meaningful Koodo delta.",
            priority=3,
            severity="watch",
            confidence=0.7,
            evidence=Evidence(
                transaction_ids=ids["tiny_txns"][:2],
                statement_line_ids=ids["tiny_lines"][:2],
            ),
        )
    )
    result = check_recurring_insights(violated, evidence_bundle)

    assert result.ok is False
    assert "recurring finding tiny-cloud-change cites no meaningful recurring change" in result.violations
    assert result.details["matched_findings"]["koodo-change"] == "Koodo"
    assert not any("Koodo 2026-05->2026-06 not represented" in item for item in result.violations)


def test_negative_control_hallucinated_subscription_fails_check(empty_db):
    ids = _seed_negative_space_fixture(empty_db)
    review = _review(empty_db, ids, fixture="negative")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    assert check_subscriptions(review, evidence_bundle).ok

    violated = review.model_copy(deep=True)
    violated.findings.append(
        ReviewFinding(
            finding_id="made-up-subscription",
            kind="new_subscription",
            title="Made Up Stream is new",
            detail="This merchant is fabricated relative to the watchlist.",
            priority=4,
            severity="watch",
            confidence=0.7,
            evidence=Evidence(
                transaction_ids=[ids["one_off_txn"]],
                statement_line_ids=[ids["one_off_line"]],
            ),
        )
    )
    violated.proposed_actions.append(
        ProposedAction(
            kind="add_subscription",
            payload={"merchant": "Made Up Stream", "account_id": ids["account_id"]},
            evidence=Evidence(
                transaction_ids=[ids["one_off_txn"]],
                statement_line_ids=[ids["one_off_line"]],
            ),
            confidence=0.7,
            rationale="Fabricated subscription.",
            agent_run_id=review.agent_run_id,
        )
    )
    result = check_subscriptions(violated, evidence_bundle)

    assert result.ok is False
    assert "subscription finding made-up-subscription does not match a subscription candidate" in result.violations
    assert "subscription action 0 add_subscription does not match a subscription candidate" in result.violations


def test_negative_control_warranted_subscription_hallucination_exercises_candidate_matcher(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    evidence_bundle = _evidence_bundle(empty_db, ids)
    faithful = check_subscriptions(review, evidence_bundle)
    assert faithful.ok
    assert faithful.details["matched"] == {
        "finding:fresh-stream": "Fresh Stream",
        "action:1": "Fresh Stream",
    }

    violated = review.model_copy(deep=True)
    violated.findings.append(
        ReviewFinding(
            finding_id="green-grocer-subscription",
            kind="new_subscription",
            title="Green Grocer is new",
            detail="This cites real grocery rows, but not the Fresh Stream candidate.",
            priority=4,
            severity="watch",
            confidence=0.7,
            evidence=Evidence(
                transaction_ids=[ids["grocery_txn"]],
                statement_line_ids=[ids["grocery_line"]],
            ),
        )
    )
    violated.proposed_actions.append(
        ProposedAction(
            kind="add_subscription",
            payload={"merchant": "Green Grocer", "account_id": ids["account_id"]},
            evidence=Evidence(
                transaction_ids=[ids["grocery_txn"]],
                statement_line_ids=[ids["grocery_line"]],
            ),
            confidence=0.7,
            rationale="Fabricated subscription.",
            agent_run_id=review.agent_run_id,
        )
    )
    result = check_subscriptions(violated, evidence_bundle)

    assert result.ok is False
    assert (
        "subscription finding green-grocer-subscription does not match a subscription candidate"
    ) in result.violations
    assert "subscription action 2 add_subscription does not match a subscription candidate" in result.violations
    assert result.details["matched"]["finding:fresh-stream"] == "Fresh Stream"
    assert result.details["matched"]["action:1"] == "Fresh Stream"


def test_negative_control_unsafe_actions_and_write_detection_fail_checks(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    before = db_snapshot(empty_db)
    review = _review(empty_db, ids, fixture="warranted")
    assert check_action_safety(review, empty_db).ok
    assert check_emission_read_only(empty_db, before).ok

    unknown = review.model_copy(deep=True)
    unknown.proposed_actions = [
        ProposedAction(
            kind="wire_money",
            payload={"transaction_id": ids["grocery_txn"]},
            evidence=Evidence(transaction_ids=[ids["grocery_txn"]]),
            confidence=0.7,
            rationale="Unknown action kind.",
            agent_run_id=review.agent_run_id,
        )
    ]
    unknown_result = check_action_safety(unknown, empty_db)
    assert unknown_result.ok is False
    assert (
        "action 0 wire_money unknown analyst action kind wire_money "
        "with structured payload keys ['transaction_id']"
    ) in unknown_result.violations
    with pytest.raises(ValueError, match="wire_money.*structured payload keys.*transaction_id"):
        with engine.write_tx(empty_db) as conn:
            enqueue_review_actions(conn, unknown)

    informational = review.model_copy(deep=True)
    informational.proposed_actions = [
        ProposedAction(
            kind="investigate",
            payload={"notes": "check this"},
            evidence=Evidence(transaction_ids=[ids["grocery_txn"]]),
            confidence=0.7,
            rationale="Unknown informational action kind.",
            agent_run_id=review.agent_run_id,
        )
    ]
    informational_result = check_action_safety(informational, empty_db)
    assert informational_result.ok
    assert informational_result.details["checked_actions"] == []
    assert informational_result.details["unrouted_informational"] == [
        {"label": "action 0 investigate", "kind": "investigate"}
    ]
    with engine.write_tx(empty_db) as conn:
        assert enqueue_review_actions(conn, informational) == []

    invalid_payload = review.model_copy(deep=True)
    invalid_payload.proposed_actions = [
        ProposedAction(
            kind="categorize",
            payload={"transaction_id": 999999, "category_id": ids["groceries_id"]},
            evidence=Evidence(category_ids=[ids["groceries_id"]]),
            confidence=0.7,
            rationale="Invalid transaction id.",
            agent_run_id=review.agent_run_id,
        )
    ]
    invalid_result = check_action_safety(invalid_payload, empty_db)
    assert invalid_result.ok is False
    assert any("did not enqueue cleanly" in item for item in invalid_result.violations)

    with engine.write_tx(empty_db) as conn:
        conn.execute(
            "UPDATE transactions SET notes='mutated by eval negative control' WHERE id=?",
            (ids["grocery_txn"],),
        )
    readonly_result = check_emission_read_only(empty_db, before)
    assert readonly_result.ok is False
    assert "db mutated during emission: transactions" in readonly_result.violations


@pytest.mark.llm
def test_live_recon_insight_review_satisfies_deterministic_invariants(empty_db):
    ids = _seed_warranted_fixture(empty_db)
    before = db_snapshot(empty_db)
    review = review_reconciliation(empty_db, source_document_id=ids["doc_id"])
    evidence_bundle = _evidence_bundle(empty_db, ids)

    with engine.read_conn(empty_db) as conn:
        assert check_coverage(review, conn).ok
        assert check_citations(review, conn).ok
    assert check_recurring_insights(review, evidence_bundle).ok
    assert check_subscriptions(review, evidence_bundle).ok
    assert check_action_safety(review, empty_db).ok
    assert check_emission_read_only(empty_db, before).ok
