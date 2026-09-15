from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph")

from app.agents.recon_analyst import ReconciliationReview, review_reconciliation
from app.agents.recon_analyst.evidence import resolve_statement_run
from app.agents.recon_analyst.graph import AnalystOutput, SynthesisOutput, guard_review
from app.agents.recon_analyst.schemas import (
    ActionKind,
    Evidence,
    FindingKind,
    ProposedAction,
    ReviewFinding,
    StatementRun,
)
from app.agents.recon_analyst.tools import (
    IdAllowlist,
    ensure_read_only_connection,
    receipt_matcher_tool,
    statement_auditor_tool,
)
from app.db import engine, migrate, repo_ledger, repo_statements


def _insert_doc(conn: sqlite3.Connection, name: str) -> int:
    return int(
        conn.execute(
            """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
               VALUES ('statement', ?, ?, ?, 'application/pdf', 'matched')""",
            (name, f"blobs/{name}", f"sha-recon-analyst-{name}"),
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
    source_document_id: int | None = None,
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
        source_document_id=source_document_id,
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
    repo_statements.mark_cleared(conn, txn_id, posted_on)
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


def _seed_recon_analyst_fixture(db_path: str) -> dict[str, Any]:
    with engine.write_tx(db_path) as conn:
        account_id = int(
            conn.execute(
                "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Recon Analyst Card','Test','credit','CAD')"
            ).lastrowid
        )
        groceries_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Analyst Groceries','expense','nancy','#4EA1FF')"
            ).lastrowid
        )
        recurring_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Analyst Recurring','expense','nancy','#ff9f43')"
            ).lastrowid
        )
        dining_id = int(
            conn.execute(
                "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Analyst Dining','expense','nancy','#ff6b6b')"
            ).lastrowid
        )
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)

        doc_id = _insert_doc(conn, "analyst-june.pdf")
        prev_doc_id = _insert_doc(conn, "analyst-may.pdf")

        grocery_txn = _insert_expense(
            conn,
            account_id=account_id,
            category_id=groceries_id,
            posted_on="2026-06-03",
            merchant="Green Grocer",
            amount_cents=4200,
            external_id="analyst-grocery",
        )
        grocery_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-03",
            description="GREEN GROCER",
            amount_cents=-4200,
            row_hash="analyst-green-grocer",
            match_status="matched",
            matched_transaction_id=grocery_txn,
            match_method="exact",
            match_score=1.0,
        )

        suspicious_txn = _insert_expense(
            conn,
            account_id=account_id,
            category_id=uncategorized_id,
            posted_on="2026-06-04",
            merchant="Mystery Cloud",
            amount_cents=8800,
            external_id="analyst-suspicious",
        )
        suspicious_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-04",
            description="MYSTERY CLOUD",
            amount_cents=-8800,
            row_hash="analyst-mystery-cloud",
            match_status="matched",
            matched_transaction_id=suspicious_txn,
            match_method="llm",
            match_score=0.62,
            match_rationale="low confidence category",
        )

        candidate_txn = repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on="2026-06-06",
            description="Receipt candidate",
            counterparty="Hardware Depot",
            amount_cents=-6700,
            source="test",
            external_id="analyst-candidate",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert candidate_txn is not None
        candidate_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-06",
            description="HARDWARE DEPOT",
            amount_cents=-6700,
            row_hash="analyst-hardware",
            match_status="needs_review",
        )
        new_merchant_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-08",
            description="NEW APP STORE",
            amount_cents=-2499,
            row_hash="analyst-new-app",
            match_status="unmatched",
        )
        ignored_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-09",
            description="CARD PAYMENT",
            amount_cents=-5000,
            row_hash="analyst-payment",
            match_status="ignored",
        )
        income_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-10",
            description="REFUND",
            amount_cents=1500,
            row_hash="analyst-refund",
            match_status="ignored",
        )

        koodo_may = _insert_expense(
            conn,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-05-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="analyst-koodo-may",
        )
        koodo_jun = _insert_expense(
            conn,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-06-04",
            merchant="Koodo",
            amount_cents=5210,
            external_id="analyst-koodo-jun",
        )
        koodo_may_line = _insert_statement_line(
            conn,
            doc_id=prev_doc_id,
            account_id=account_id,
            posted_on="2026-05-04",
            description="KOODO",
            amount_cents=-4633,
            row_hash="analyst-koodo-may",
            match_status="matched",
            matched_transaction_id=koodo_may,
            match_method="exact",
            match_score=1.0,
        )
        koodo_jun_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-04",
            description="KOODO",
            amount_cents=-5210,
            row_hash="analyst-koodo-jun",
            match_status="matched",
            matched_transaction_id=koodo_jun,
            match_method="exact",
            match_score=1.0,
        )

        fresh_may = _insert_expense(
            conn,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-05-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="analyst-fresh-may",
        )
        fresh_jun = _insert_expense(
            conn,
            account_id=account_id,
            category_id=recurring_id,
            posted_on="2026-06-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="analyst-fresh-jun",
        )
        fresh_may_line = _insert_statement_line(
            conn,
            doc_id=prev_doc_id,
            account_id=account_id,
            posted_on="2026-05-15",
            description="FRESH STREAM",
            amount_cents=-1299,
            row_hash="analyst-fresh-may",
            match_status="matched",
            matched_transaction_id=fresh_may,
            match_method="exact",
            match_score=1.0,
        )
        fresh_jun_line = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-15",
            description="FRESH STREAM",
            amount_cents=-1299,
            row_hash="analyst-fresh-jun",
            match_status="matched",
            matched_transaction_id=fresh_jun,
            match_method="exact",
            match_score=1.0,
        )

        conn.execute(
            "INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at) "
            "VALUES (?, '', 10000, 'shared', NULL, CURRENT_TIMESTAMP)",
            (groceries_id,),
        )
        budget_txns = [
            _insert_expense(
                conn,
                account_id=account_id,
                category_id=groceries_id,
                posted_on=f"2026-06-1{idx}",
                merchant="Budget Market",
                amount_cents=5000,
                external_id=f"analyst-budget-{idx}",
            )
            for idx in range(3)
        ]
        dining_may_txns = [
            _insert_expense(
                conn,
                account_id=account_id,
                category_id=dining_id,
                posted_on=f"2026-05-0{idx}",
                merchant="Dining May",
                amount_cents=amount,
                external_id=f"analyst-dining-may-{idx}",
            )
            for idx, amount in enumerate((2000, 2000), start=1)
        ]
        dining_jun_txns = [
            _insert_expense(
                conn,
                account_id=account_id,
                category_id=dining_id,
                posted_on=f"2026-06-2{idx}",
                merchant="Dining Jun",
                amount_cents=5000,
                external_id=f"analyst-dining-jun-{idx}",
            )
            for idx in range(3)
        ]

    return {
        "doc_id": doc_id,
        "account_id": account_id,
        "category_ids": [groceries_id, recurring_id, dining_id, uncategorized_id],
        "transaction_ids": [
            grocery_txn,
            suspicious_txn,
            int(candidate_txn),
            koodo_may,
            koodo_jun,
            fresh_may,
            fresh_jun,
            *budget_txns,
            *dining_may_txns,
            *dining_jun_txns,
        ],
        "statement_line_ids": [
            grocery_line,
            suspicious_line,
            candidate_line,
            new_merchant_line,
            ignored_line,
            income_line,
            koodo_may_line,
            koodo_jun_line,
            fresh_may_line,
            fresh_jun_line,
        ],
        "grocery_txn": grocery_txn,
        "suspicious_txn": suspicious_txn,
        "candidate_txn": int(candidate_txn),
        "koodo_txns": [koodo_may, koodo_jun],
        "fresh_txns": [fresh_may, fresh_jun],
        "budget_txns": budget_txns,
        "dining_jun_txns": dining_jun_txns,
        "grocery_line": grocery_line,
        "suspicious_line": suspicious_line,
        "candidate_line": candidate_line,
        "new_merchant_line": new_merchant_line,
        "koodo_lines": [koodo_may_line, koodo_jun_line],
        "fresh_lines": [fresh_may_line, fresh_jun_line],
        "groceries_id": groceries_id,
        "recurring_id": recurring_id,
        "dining_id": dining_id,
    }


def _seed_cross_month_doc_fixture(db_path: str) -> dict[str, Any]:
    with engine.write_tx(db_path) as conn:
        account_id = int(
            conn.execute(
                "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Cross Month Card','Test','credit','CAD')"
            ).lastrowid
        )
        doc_id = _insert_doc(conn, "cross-month.pdf")
        may_line_1 = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-05-28",
            description="MAY HARDWARE",
            amount_cents=-15000,
            row_hash="cross-may-hardware",
        )
        may_line_2 = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-05-29",
            description="MAY GROCERY",
            amount_cents=-15000,
            row_hash="cross-may-grocery",
        )
        june_lines = [
            _insert_statement_line(
                conn,
                doc_id=doc_id,
                account_id=account_id,
                posted_on=f"2026-06-0{day}",
                description=f"JUNE ITEM {day}",
                amount_cents=-4000,
                row_hash=f"cross-june-{day}",
            )
            for day in (1, 3, 5)
        ]
    return {
        "doc_id": doc_id,
        "account_id": account_id,
        "may_lines": [may_line_1, may_line_2],
        "june_lines": june_lines,
    }


class _FakeStructured:
    def __init__(self, llm: "_FakeReconLLM", schema: type[Any]):
        self._llm = llm
        self._schema = schema

    def invoke(self, messages):
        return self._llm.next(self._schema, messages)


class _FakeReconLLM:
    def __init__(self, ids: dict[str, Any], *, invalid: bool = False, absurd_summary: bool = False):
        self.ids = ids
        self.invalid = invalid
        self.absurd_summary = absurd_summary
        self.methods: list[str] = []
        self.analyst_calls = 0

    def with_structured_output(self, schema, **kwargs):
        self.methods.append(kwargs["method"])
        return _FakeStructured(self, schema)

    def next(self, schema, messages):
        if schema is AnalystOutput:
            self.analyst_calls += 1
            return self._analyst_output(self.analyst_calls)
        if schema is SynthesisOutput:
            return self._synthesis_output()
        raise AssertionError(f"unexpected schema {schema}")

    def _analyst_output(self, call_number: int) -> AnalystOutput:
        if call_number == 1:
            return AnalystOutput(
                findings=[
                    ReviewFinding(
                        finding_id="coverage-summary",
                        kind="coverage_summary",
                        title="June statement has coverage gaps",
                        detail="Coverage includes matched, ignored, and unmatched lines from provided evidence.",
                        priority=5,
                        severity="watch",
                        confidence=0.8,
                        evidence=Evidence(statement_line_ids=[self.ids["grocery_line"], self.ids["candidate_line"]]),
                    )
                ],
                proposed_actions=[],
            )
        if call_number == 2:
            return AnalystOutput(
                findings=[
                    ReviewFinding(
                        finding_id="unmatched-hardware",
                        kind="unmatched_line",
                        title="Hardware Depot needs review",
                        detail="The unmatched line has a same-amount candidate transaction.",
                        priority=1,
                        severity="warn",
                        confidence=0.91,
                        evidence=Evidence(
                            transaction_ids=[self.ids["candidate_txn"]],
                            statement_line_ids=[self.ids["candidate_line"]],
                        ),
                    )
                ],
                proposed_actions=[
                    ProposedAction(
                        kind="confirm_match",
                        payload={
                            "statement_line_id": self.ids["candidate_line"],
                            "transaction_id": self.ids["candidate_txn"],
                        },
                        evidence=Evidence(
                            transaction_ids=[self.ids["candidate_txn"]],
                            statement_line_ids=[self.ids["candidate_line"]],
                        ),
                        confidence=0.9,
                        rationale="Same amount and nearby date.",
                        agent_run_id="pending",
                    )
                ],
            )
        if call_number == 3:
            return AnalystOutput(
                findings=[
                    ReviewFinding(
                        finding_id="koodo-change",
                        kind="recurring_change",
                        title="Koodo recurring charge increased",
                        detail="Koodo changed from the prior month according to recurring deltas.",
                        priority=3,
                        severity="watch",
                        confidence=0.86,
                        evidence=Evidence(
                            transaction_ids=self.ids["koodo_txns"],
                            statement_line_ids=self.ids["koodo_lines"],
                        ),
                    )
                ],
                proposed_actions=[],
            )
        return AnalystOutput(
            findings=[
                ReviewFinding(
                    finding_id="fresh-stream",
                    kind="new_subscription",
                    title="Fresh Stream looks newly recurring",
                    detail="Fresh Stream appears in two consecutive months.",
                    priority=4,
                    severity="watch",
                    confidence=0.82,
                    evidence=Evidence(
                        transaction_ids=self.ids["fresh_txns"],
                        statement_line_ids=self.ids["fresh_lines"],
                    ),
                ),
                ReviewFinding(
                    finding_id="groceries-overrun",
                    kind="budget_overrun",
                    title="Groceries are over budget",
                    detail="The planning card links the category overrun to June transactions.",
                    priority=6,
                    severity="warn",
                    confidence=0.79,
                    evidence=Evidence(
                        transaction_ids=self.ids["budget_txns"],
                        category_ids=[self.ids["groceries_id"]],
                    ),
                ),
            ],
            proposed_actions=[
                ProposedAction(
                    kind="add_subscription",
                    payload={"merchant": "Fresh Stream", "account_id": self.ids["account_id"]},
                    evidence=Evidence(
                        transaction_ids=self.ids["fresh_txns"],
                        statement_line_ids=self.ids["fresh_lines"],
                    ),
                    confidence=0.81,
                    rationale="Two consecutive equal monthly charges.",
                    agent_run_id="pending",
                )
            ],
        )

    def _synthesis_output(self) -> SynthesisOutput:
        invalid_id = 999999 if self.invalid else self.ids["candidate_line"]
        findings = [
            ReviewFinding(
                finding_id="fresh-stream",
                kind="new_subscription",
                title="Fresh Stream looks newly recurring",
                detail="Two consecutive months may affect planning.",
                priority=4,
                severity="watch",
                confidence=0.82,
                evidence=Evidence(
                    transaction_ids=self.ids["fresh_txns"],
                    statement_line_ids=self.ids["fresh_lines"],
                ),
            ),
            ReviewFinding(
                finding_id="unmatched-hardware",
                kind="unmatched_line",
                title="Hardware Depot needs review",
                detail="A candidate transaction exists for the unmatched statement line.",
                priority=1,
                severity="warn",
                confidence=0.91,
                evidence=Evidence(
                    transaction_ids=[self.ids["candidate_txn"]],
                    statement_line_ids=[invalid_id],
                ),
            ),
            ReviewFinding(
                finding_id="mystery-category",
                kind="suspicious_match",
                title="Mystery Cloud matched but remains uncategorized",
                detail="The matched line points to an Uncategorized split.",
                priority=2,
                severity="watch",
                confidence=0.88,
                evidence=Evidence(
                    transaction_ids=[self.ids["suspicious_txn"]],
                    statement_line_ids=[self.ids["suspicious_line"]],
                ),
            ),
            ReviewFinding(
                finding_id="koodo-change",
                kind="recurring_change",
                title="Koodo recurring charge increased",
                detail="Recurring deltas show a meaningful month-over-month change.",
                priority=3,
                severity="watch",
                confidence=0.86,
                evidence=Evidence(
                    transaction_ids=self.ids["koodo_txns"],
                    statement_line_ids=self.ids["koodo_lines"],
                ),
            ),
            ReviewFinding(
                finding_id="groceries-overrun",
                kind="budget_overrun",
                title="Groceries are over budget",
                detail="Planning evidence links the overrun to June grocery transactions.",
                priority=5,
                severity="warn",
                confidence=0.79,
                evidence=Evidence(
                    transaction_ids=self.ids["budget_txns"],
                    category_ids=[self.ids["groceries_id"]],
                ),
            ),
        ]
        actions = [
            ProposedAction(
                kind="confirm_match",
                payload={"statement_line_id": invalid_id, "transaction_id": self.ids["candidate_txn"]},
                evidence=Evidence(
                    transaction_ids=[self.ids["candidate_txn"]],
                    statement_line_ids=[invalid_id],
                ),
                confidence=0.9,
                rationale="Same amount and nearby date.",
                agent_run_id="pending",
            ),
            ProposedAction(
                kind="add_subscription",
                payload={"merchant": "Fresh Stream", "account_id": self.ids["account_id"]},
                evidence=Evidence(
                    transaction_ids=self.ids["fresh_txns"],
                    statement_line_ids=self.ids["fresh_lines"],
                ),
                confidence=0.81,
                rationale="Two consecutive equal monthly charges.",
                agent_run_id="pending",
            ),
            ProposedAction(
                kind="adjust_budget",
                payload={"category_id": self.ids["groceries_id"], "month": "2026-06"},
                evidence=Evidence(
                    transaction_ids=self.ids["budget_txns"],
                    category_ids=[self.ids["groceries_id"]],
                ),
                confidence=0.78,
                rationale="Budget card is linked to posted June transactions.",
                agent_run_id="pending",
            ),
        ]
        summary = (
            "June has unmatched statement spend, an uncategorized matched line, "
            "a Koodo recurring increase, and a new Fresh Stream watchlist item."
        )
        if self.absurd_summary:
            summary = "There are 999 lines, $999,999 unmatched, and 123% coverage."
        return SynthesisOutput(
            period_summary=summary,
            findings=findings,
            proposed_actions=actions,
        )


def _assert_all_evidence_allowed(review: ReconciliationReview, ids: dict[str, Any]) -> None:
    allowed_txns = set(ids["transaction_ids"])
    allowed_lines = set(ids["statement_line_ids"])
    allowed_categories = set(ids["category_ids"])
    for finding in review.findings:
        assert set(finding.evidence.transaction_ids) <= allowed_txns
        assert set(finding.evidence.statement_line_ids) <= allowed_lines
        assert set(finding.evidence.category_ids) <= allowed_categories
    for action in review.proposed_actions:
        assert set(action.evidence.transaction_ids) <= allowed_txns
        assert set(action.evidence.statement_line_ids) <= allowed_lines
        assert set(action.evidence.category_ids) <= allowed_categories


def _guard_statement_run() -> StatementRun:
    return StatementRun(source_document_id=1, month="2026-06", months=["2026-06"], account_ids=[1], document_name="doc.pdf")


def _action(
    kind: str,
    payload: dict[str, Any],
    *,
    evidence: Evidence,
    confidence: float = 0.7,
    rationale: str = "test action",
) -> ProposedAction:
    return ProposedAction(
        kind=kind,
        payload=payload,
        evidence=evidence,
        confidence=confidence,
        rationale=rationale,
        agent_run_id="pending",
    )


def _kind_enums(schema: dict[str, Any]) -> list[set[str]]:
    enums: list[set[str]] = []
    for definition in schema.get("$defs", {}).values():
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties", {})
        if not isinstance(properties, dict):
            continue
        kind_schema = properties.get("kind", {})
        if isinstance(kind_schema, dict) and "enum" in kind_schema:
            enums.append(set(kind_schema["enum"]))
    return enums


def _guard_allowed() -> IdAllowlist:
    return IdAllowlist(
        transaction_ids={20, 21},
        statement_line_ids={10, 11},
        category_ids={30},
        account_ids={1},
        merchants={"Fresh Stream", "GREEN GROCER"},
    )


def test_recon_analyst_structured_output_schema_pins_kind_enums():
    expected_finding_kinds = set(get_args(FindingKind))
    expected_action_kinds = set(get_args(ActionKind))

    for output_model in (AnalystOutput, SynthesisOutput):
        kind_enums = _kind_enums(output_model.model_json_schema())

        assert expected_finding_kinds in kind_enums
        assert expected_action_kinds in kind_enums


def test_recon_analyst_llm_output_rejects_unknown_kind():
    finding = {
        "finding_id": "unknown-kind",
        "kind": "investigate",
        "title": "Unknown kind",
        "detail": "This should fail on the LLM output path.",
        "priority": 1,
        "severity": "watch",
        "confidence": 0.7,
    }
    action = {
        "kind": "wire_money",
        "payload": {},
        "confidence": 0.7,
        "rationale": "This should fail on the LLM output path.",
        "agent_run_id": "pending",
    }

    with pytest.raises(ValidationError):
        AnalystOutput.model_validate({"findings": [finding], "proposed_actions": []})
    with pytest.raises(ValidationError):
        AnalystOutput.model_validate({"findings": [], "proposed_actions": [action]})


def test_recon_analyst_offline_contract_returns_typed_review(empty_db):
    ids = _seed_recon_analyst_fixture(empty_db)
    fake_llm = _FakeReconLLM(ids)

    review = review_reconciliation(
        empty_db,
        source_document_id=ids["doc_id"],
        llm_factory=lambda: fake_llm,
    )

    assert isinstance(review, ReconciliationReview)
    assert review.agent_run_id
    assert review.statement_run == StatementRun(
        source_document_id=ids["doc_id"],
        month="2026-06",
        account_ids=[ids["account_id"]],
        document_name="analyst-june.pdf",
    )
    assert review.coverage is not None
    assert review.coverage.line_count == 8
    assert review.coverage.unmatched_spend_cents == 9199
    assert [finding.priority for finding in review.findings] == sorted(
        finding.priority for finding in review.findings
    )
    assert {finding.kind for finding in review.findings} >= {
        "unmatched_line",
        "suspicious_match",
        "recurring_change",
        "new_subscription",
        "budget_overrun",
    }
    assert all(0 <= finding.confidence <= 1 for finding in review.findings)
    assert all(0 <= action.confidence <= 1 for action in review.proposed_actions)
    _assert_all_evidence_allowed(review, ids)

    action_keys = ["kind", "payload", "evidence", "confidence", "rationale", "agent_run_id"]
    dumped_actions = [action.model_dump(mode="json") for action in review.proposed_actions]
    assert dumped_actions
    assert all(list(action.keys()) == action_keys for action in dumped_actions)
    assert {action["agent_run_id"] for action in dumped_actions} == {review.agent_run_id}
    assert fake_llm.methods == ["json_schema"] * 5


def test_recon_analyst_payload_schema_is_flat_and_sanitizes_nested_values():
    payload_schema = ProposedAction.model_json_schema()["properties"]["payload"]
    assert payload_schema["additionalProperties"] is not True

    action = _action(
        "confirm_match",
        payload={
            "statement_line_id": 10,
            "transaction_id": 20,
            "match": {"transaction_id": 999999},
            "matches": [{"transaction_id": 999999}],
            "transaction_ids": [20, "21", {"id": 999999}],
        },
        evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
    )

    assert "match" not in action.payload
    assert "matches" not in action.payload
    assert action.payload["transaction_ids"] == [20, 21]


def test_recon_analyst_guard_blocks_bypass_payload_shapes():
    allowed = _guard_allowed()
    evidence = Evidence(transaction_ids=[20], statement_line_ids=[10])
    actions = [
        _action("confirm_match", {"match": {"transaction_id": 999999}, "statement_line_id": 10}, evidence=evidence),
        _action("confirm_match", {"matches": [{"transaction_id": 999999, "statement_line_id": 888888}], "transaction_id": 20}, evidence=evidence),
        _action("confirm_match", {"statement_line_id": 10, "transactionId": 999999}, evidence=evidence),
        _action("confirm_match", {"statement_line_id": 10, "transaction_id": 20, "id": 999999}, evidence=evidence),
    ]

    review = guard_review(
        agent_run_id="run-1",
        statement_run=_guard_statement_run(),
        coverage=None,
        period_summary="summary",
        findings=[],
        proposed_actions=actions,
        allowed_ids=allowed,
    )

    assert review.proposed_actions == []
    assert len(review.guard_notes) == 4
    assert all("dropped action confirm_match" in note for note in review.guard_notes)


def test_recon_analyst_guard_never_throws_on_uncoercible_payload_ids():
    allowed = _guard_allowed()
    actions = [
        _action(
            "confirm_match",
            {"statement_line_id": 10, "transaction_id": "see evidence"},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "confirm_match",
            {"statement_line_id": 10, "transaction_id": {"id": 999}},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "adjust_budget",
            {"category_id": "Groceries"},
            evidence=Evidence(category_ids=[30]),
        ),
    ]

    review = guard_review(
        agent_run_id="run-1",
        statement_run=_guard_statement_run(),
        coverage=None,
        period_summary="summary",
        findings=[],
        proposed_actions=actions,
        allowed_ids=allowed,
    )

    assert isinstance(review, ReconciliationReview)
    assert review.proposed_actions == []
    assert len([note for note in review.guard_notes if note.startswith("dropped action")]) == 3


def test_recon_analyst_guard_drops_incomplete_required_payload_after_repair():
    allowed = _guard_allowed()

    review = guard_review(
        agent_run_id="run-1",
        statement_run=_guard_statement_run(),
        coverage=None,
        period_summary="summary",
        findings=[],
        proposed_actions=[
            _action(
                "confirm_match",
                {"statement_line_id": 888888, "transaction_id": 20},
                evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
            )
        ],
        allowed_ids=allowed,
    )

    assert review.proposed_actions == []
    assert any("payload statement_line_id 888888 not in evidence allowlist" in note for note in review.guard_notes)


def test_recon_analyst_guard_preserves_plural_id_lists_when_repaired():
    allowed = _guard_allowed()

    review = guard_review(
        agent_run_id="run-1",
        statement_run=_guard_statement_run(),
        coverage=None,
        period_summary="summary",
        findings=[],
        proposed_actions=[
            _action(
                "review_unmatched",
                {"statement_line_id": 10, "statement_line_ids": [10, 888888]},
                evidence=Evidence(statement_line_ids=[10]),
            )
        ],
        allowed_ids=allowed,
    )

    assert len(review.proposed_actions) == 1
    assert review.proposed_actions[0].payload["statement_line_ids"] == [10]
    assert any("payload statement_line_ids removed invalid ids" in note for note in review.guard_notes)


def test_recon_analyst_guard_vets_account_merchant_decision_and_strips_name_aliases():
    allowed = _guard_allowed()
    actions = [
        _action(
            "add_subscription",
            {"merchant": "fresh stream", "account_id": 1, "decision": "subscription"},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "add_subscription",
            {"merchant": "Fresh Stream", "account_id": 999},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "add_subscription",
            {"merchant": "Made Up", "account_id": 1},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "add_subscription",
            {"merchant": "Fresh Stream", "account_id": 1, "decision": "approve_anyway"},
            evidence=Evidence(transaction_ids=[20], statement_line_ids=[10]),
        ),
        _action(
            "categorize",
            {"transaction_id": 20, "category_id": 30, "category_name": "Groceries"},
            evidence=Evidence(transaction_ids=[20], category_ids=[30]),
        ),
        _action(
            "categorize",
            {"transaction_id": 20, "to_category_name": "Groceries"},
            evidence=Evidence(transaction_ids=[20], category_ids=[30]),
        ),
    ]

    review = guard_review(
        agent_run_id="run-1",
        statement_run=_guard_statement_run(),
        coverage=None,
        period_summary="summary",
        findings=[],
        proposed_actions=actions,
        allowed_ids=allowed,
    )

    assert [action.kind for action in review.proposed_actions] == ["add_subscription", "categorize"]
    assert review.proposed_actions[0].payload == {
        "merchant": "Fresh Stream",
        "account_id": 1,
        "decision": "subscription",
    }
    assert review.proposed_actions[1].payload == {"transaction_id": 20, "category_id": 30}
    assert any("payload account_id 999 not in evidence allowlist" in note for note in review.guard_notes)
    assert any("payload merchant 'Made Up' not in evidence merchant allowlist" in note for note in review.guard_notes)
    assert any("not a known subscription decision" in note for note in review.guard_notes)
    assert any("stripped payload category_name" in note for note in review.guard_notes)
    assert any("payload missing required category_id" in note for note in review.guard_notes)


def test_recon_analyst_guard_repairs_or_drops_hallucinated_ids():
    valid_line = 10
    valid_txn = 20
    allowed = IdAllowlist(transaction_ids={valid_txn}, statement_line_ids={valid_line}, category_ids={30})
    review = guard_review(
        agent_run_id="run-1",
        statement_run=StatementRun(source_document_id=1, month="2026-06", account_ids=[1], document_name="doc.pdf"),
        coverage=None,
        period_summary="summary",
        findings=[
            ReviewFinding(
                finding_id="mixed",
                kind="unmatched_line",
                title="Mixed evidence",
                detail="One real line and one fake line.",
                priority=2,
                severity="watch",
                confidence=0.7,
                evidence=Evidence(transaction_ids=[valid_txn, 999], statement_line_ids=[valid_line, 888]),
            ),
            ReviewFinding(
                finding_id="fake-only",
                kind="unmatched_line",
                title="Fake evidence",
                detail="No cited ids survive.",
                priority=1,
                severity="warn",
                confidence=0.7,
                evidence=Evidence(statement_line_ids=[888]),
            ),
        ],
        proposed_actions=[
            ProposedAction(
                kind="confirm_match",
                payload={"statement_line_id": 888, "transaction_id": valid_txn},
                evidence=Evidence(transaction_ids=[valid_txn], statement_line_ids=[888]),
                confidence=0.7,
                rationale="bad line id",
                agent_run_id="pending",
            ),
            ProposedAction(
                kind="review_unmatched",
                payload={"statement_line_id": valid_line},
                evidence=Evidence(statement_line_ids=[valid_line]),
                confidence=0.7,
                rationale="real line id",
                agent_run_id="pending",
            ),
        ],
        allowed_ids=allowed,
    )

    assert [finding.finding_id for finding in review.findings] == ["mixed"]
    assert review.findings[0].evidence == Evidence(transaction_ids=[valid_txn], statement_line_ids=[valid_line])
    assert [action.kind for action in review.proposed_actions] == ["review_unmatched"]
    assert review.proposed_actions[0].agent_run_id == "run-1"


def test_recon_analyst_read_connection_is_query_only(empty_db):
    _seed_recon_analyst_fixture(empty_db)
    with engine.read_conn(empty_db) as conn:
        ensure_read_only_connection(conn)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Nope','Test','cash','CAD')"
            )


def test_recon_analyst_document_run_covers_all_cross_month_lines(empty_db):
    ids = _seed_cross_month_doc_fixture(empty_db)

    with engine.read_conn(empty_db) as conn:
        run = resolve_statement_run(conn, source_document_id=ids["doc_id"])
        by_doc = conn.execute(
            """
            SELECT line_count, statement_spend_cents, covered_spend_cents,
                   unmatched_spend_cents, ignored_spend_cents, income_cents,
                   attention_spend_cents, coverage_pct
            FROM v_statement_coverage_by_doc
            WHERE source_document_id = ?
            """,
            (ids["doc_id"],),
        ).fetchone()
        audit = statement_auditor_tool(conn, run)
        receipts = receipt_matcher_tool(conn, run)

    assert run.month == "2026-06"
    assert run.months == ["2026-05", "2026-06"]
    assert audit.coverage.line_count == by_doc["line_count"] == 5
    assert audit.coverage.statement_spend_cents == by_doc["statement_spend_cents"] == 42000
    assert audit.coverage.unmatched_spend_cents == by_doc["unmatched_spend_cents"] == 42000
    assert set(ids["may_lines"]) <= audit.allowed_ids.statement_line_ids
    receipt_line_ids = {item.line.line_id for item in receipts.unmatched_lines}
    assert set(ids["may_lines"]) <= receipt_line_ids


def test_recon_analyst_coverage_comes_from_tool_not_synthesis_text(empty_db):
    ids = _seed_recon_analyst_fixture(empty_db)
    with engine.read_conn(empty_db) as conn:
        expected = statement_auditor_tool(conn, resolve_statement_run(conn, source_document_id=ids["doc_id"])).coverage
    review = review_reconciliation(
        empty_db,
        source_document_id=ids["doc_id"],
        llm_factory=lambda: _FakeReconLLM(ids, absurd_summary=True),
    )

    assert review.period_summary == "There are 999 lines, $999,999 unmatched, and 123% coverage."
    assert review.coverage == expected


@pytest.mark.llm
def test_recon_analyst_live_e2e_returns_guarded_review(empty_db):
    ids = _seed_recon_analyst_fixture(empty_db)
    review = review_reconciliation(empty_db, source_document_id=ids["doc_id"])

    assert isinstance(review, ReconciliationReview)
    assert review.statement_run.source_document_id == ids["doc_id"]
    assert all(0 <= finding.confidence <= 1 for finding in review.findings)
    assert all(0 <= action.confidence <= 1 for action in review.proposed_actions)
    _assert_all_evidence_allowed(review, ids)


def build_sample_review_json() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "sample.sqlite")
        migrate.init_db(db_path)
        ids = _seed_recon_analyst_fixture(db_path)
        review = review_reconciliation(
            db_path,
            source_document_id=ids["doc_id"],
            llm_factory=lambda: _FakeReconLLM(ids),
        )
        return review.model_dump_json(indent=2)
