from __future__ import annotations

import sqlite3

import pytest

from app.db import (
    engine,
    repo_actions,
    repo_documents,
    repo_ledger,
    repo_merchant_knowledge,
    repo_statements,
)
from app.db.repo_merchant_knowledge import Evidence
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.reconcile import apply as recon_apply
from app.reconcile.engine import reconcile_document
from app.reconcile.schemas import ReconBatchDecision, ReconDecision
from app.reconcile.scoring import composite, date_score, merchant_score


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, messages):
        return self._result


class _FakeReconLLM:
    """Returns a fixed ReconBatchDecision regardless of prompt content."""

    def __init__(self, decisions: list[ReconDecision]):
        self._result = ReconBatchDecision(decisions=decisions)

    def with_structured_output(self, schema, **kwargs):
        assert schema is ReconBatchDecision
        return _FakeStructured(self._result)


class _RaisingLLM:
    def with_structured_output(self, schema, **kwargs):
        raise RuntimeError("llm unavailable")


def _make_doc(conn: sqlite3.Connection, ref: str) -> int:
    return repo_documents.insert_source_document(
        conn, kind="statement", original_name=f"{ref}.pdf", storage_ref=f"blob/{ref}",
        sha256=f"sha-{ref}", mime_type="application/pdf", status="processed",
    )


def _stage(conn: sqlite3.Connection, doc_id: int, account_id: int | None,
          rows: list[StatementRow]) -> None:
    parsed = ExtractedStatement(institution="Test Bank", currency="CAD",
                                statement_period="2026-04", rows=rows)
    repo_statements.stage_lines(conn, source_document_id=doc_id, account_id=account_id, parsed=parsed)


def _line_for(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    lines = repo_statements.lines_for_document(conn, doc_id)
    assert len(lines) == 1
    return lines[0]


def _insert_candidate(conn: sqlite3.Connection, *, account_id: int, posted_on: str,
                      description: str, counterparty: str, amount_cents: int, ext: str) -> int:
    return repo_ledger.insert_transaction(
        conn, account_id=account_id, posted_on=posted_on, description=description,
        counterparty=counterparty, amount_cents=amount_cents, source="receipt",
        external_id=ext, source_document_id=None, source_confidence=0.9,
        flow_kind="purchase",
    )


# ---- auto-match paths ------------------------------------------------------

def test_exact_single_candidate_waits_for_review_while_authority_disabled(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t1")
        cand_id = _insert_candidate(conn, account_id=3, posted_on="2026-04-08",
                                    description="coffee", counterparty="Starbucks",
                                    amount_cents=-550, ext="rcpt:t1")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="STARBUCKS #4521", amount_cents=-550),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_method"] == ""
        assert line["matched_transaction_id"] is None
        assert "same-event automation disabled" in line["match_rationale"]
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (cand_id,)).fetchone()
        assert txn["account_id"] == 3
        assert txn["recon_status"] == "uncleared"
        assert txn["cleared_on"] == ""
        doc = repo_documents.get_document(conn, doc_id)
        assert doc["status"] == "needs_review"


def test_single_low_merchant_candidate_waits_for_review(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t1b")
        cand_id = _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                                    description="misc", counterparty="Totally Different Corp",
                                    amount_cents=-3300, ext="rcpt:t1b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="ACME WIDGETS INC", amount_cents=-3300),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result["matched"] == 0
    assert result["needs_review"] == 1

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_method"] == ""
        assert line["matched_transaction_id"] is None


def test_high_merchant_score_does_not_override_disabled_authority(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t2")
        winner_id = _insert_candidate(conn, account_id=1, posted_on="2026-04-09",
                                      description="online order", counterparty="ExampleMarket",
                                      amount_cents=-2000, ext="rcpt:t2a")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                          description="misc", counterparty="Random Store",
                          amount_cents=-2000, ext="rcpt:t2b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="AMAZON MKTPLACE", amount_cents=-2000),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result["matched"] == 0
    assert result["needs_review"] == 1

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_method"] == ""
        assert line["matched_transaction_id"] is None


def test_cross_account_non_receipt_txn_not_matched(app_env):
    """Fix 4 regression: receipts are the only txns with unreliable accounts. A manual/
    statement-sourced txn recorded in a DIFFERENT account must never be raided by a line."""
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t13")
        cand_id = repo_ledger.insert_transaction(
            conn, account_id=2, posted_on="2026-04-27", description="misc",
            counterparty="Some Vendor", amount_cents=-777, source="manual",
            external_id="manual:t13", source_document_id=None, source_confidence=1.0,
            flow_kind="purchase",
        )
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-28", description="UNRELATED PURCHASE", amount_cents=-777),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    # No in-account candidate found: the operator must decide whether this is new.
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        cand = conn.execute("SELECT recon_status FROM transactions WHERE id=?", (cand_id,)).fetchone()
        assert cand["recon_status"] == "uncleared"  # never touched
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["matched_transaction_id"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='statement'"
        ).fetchone()[0] == 0


# ---- LLM residue path -------------------------------------------------------

def test_ambiguous_llm_suggestion_cannot_mutate_while_authority_disabled(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t3")
        cand_a = _insert_candidate(conn, account_id=1, posted_on="2026-04-09",
                                   description="misc", counterparty="Some Shop",
                                   amount_cents=-1500, ext="rcpt:t3a")
        cand_b = _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                                   description="misc", counterparty="Other Shop",
                                   amount_cents=-1500, ext="rcpt:t3b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="GENERIC PURCHASE", amount_cents=-1500),
        ])
        line_id = _line_for(conn, doc_id)["id"]

    fake_llm = _FakeReconLLM([
        ReconDecision(statement_line_id=line_id, transaction_id=cand_b, confidence=0.9, reason="matches receipt"),
    ])
    result = reconcile_document(app_env, doc_id, fake_llm)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_method"] == ""
        assert line["matched_transaction_id"] is None
        txn_a = conn.execute("SELECT recon_status FROM transactions WHERE id=?", (cand_a,)).fetchone()
        assert txn_a["recon_status"] == "uncleared"  # the un-picked candidate is untouched


def test_llm_invalid_candidate_needs_review(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t4")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-09",
                          description="misc", counterparty="Some Shop",
                          amount_cents=-1600, ext="rcpt:t4a")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                          description="misc", counterparty="Other Shop",
                          amount_cents=-1600, ext="rcpt:t4b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="GENERIC PURCHASE", amount_cents=-1600),
        ])
        line_id = _line_for(conn, doc_id)["id"]

    # id=1 is a real sample txn but was never offered as a candidate for this line.
    fake_llm = _FakeReconLLM([
        ReconDecision(statement_line_id=line_id, transaction_id=1, confidence=0.9, reason="bogus"),
    ])
    result = reconcile_document(app_env, doc_id, fake_llm)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_rationale"] == (
            "same-event automation disabled; review 2 candidate(s)"
        )
        assert line["matched_transaction_id"] is None
        doc = repo_documents.get_document(conn, doc_id)
        assert doc["status"] == "needs_review"


def test_llm_batch_cannot_claim_any_candidate_while_authority_disabled(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t4c")
        cand_a = _insert_candidate(conn, account_id=1, posted_on="2026-04-09",
                                   description="misc", counterparty="Some Shop",
                                   amount_cents=-1800, ext="rcpt:t4ca")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                          description="misc", counterparty="Other Shop",
                          amount_cents=-1800, ext="rcpt:t4cb")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="GENERIC PURCHASE ONE", amount_cents=-1800),
            StatementRow(posted_on="2026-04-11", description="GENERIC PURCHASE TWO", amount_cents=-1800),
        ])
        lines = repo_statements.lines_for_document(conn, doc_id)
        line1_id, line2_id = lines[0]["id"], lines[1]["id"]

    fake_llm = _FakeReconLLM([
        ReconDecision(statement_line_id=line1_id, transaction_id=cand_a, confidence=0.9, reason="match"),
        ReconDecision(statement_line_id=line2_id, transaction_id=cand_a, confidence=0.9, reason="match too"),
    ])
    result = reconcile_document(app_env, doc_id, fake_llm)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 2, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line1 = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line1_id,)).fetchone()
        line2 = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line2_id,)).fetchone()
        assert line1["match_status"] == "needs_review"
        assert line1["matched_transaction_id"] is None
        assert line2["match_status"] == "needs_review"
        assert "same-event automation disabled" in line2["match_rationale"]
        assert line2["matched_transaction_id"] is None


def test_llm_unavailable_routes_queued_lines_to_needs_review(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t4b")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-09",
                          description="misc", counterparty="Some Shop",
                          amount_cents=-1700, ext="rcpt:t4ba")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-08",
                          description="misc", counterparty="Other Shop",
                          amount_cents=-1700, ext="rcpt:t4bb")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-10", description="GENERIC PURCHASE", amount_cents=-1700),
        ])

    result = reconcile_document(app_env, doc_id, _RaisingLLM())
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["match_rationale"] == (
            "same-event automation disabled; review 2 candidate(s)"
        )


# ---- promotion --------------------------------------------------------------

def test_zero_candidates_waits_for_review_until_operator_promotes(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t5")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-15", description="UNIQUE MERCHANT XYZ", amount_cents=-999),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 1, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["matched_transaction_id"] is None
        assert line["match_rationale"] == (
            "no same-event candidates; review and promote if this is a new transaction"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='statement'"
        ).fetchone()[0] == 0

    with engine.write_tx(app_env) as conn:
        line_id = int(_line_for(conn, doc_id)["id"])
        transaction_id = recon_apply.promote_line(conn, line_id)
        assert transaction_id is not None

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "promoted"
        txn = conn.execute(
            "SELECT * FROM transactions WHERE id=?", (line["matched_transaction_id"],)
        ).fetchone()
        assert txn["amount_cents"] == -999
        assert txn["source"] == "statement"
        assert txn["external_id"] == line["row_hash"]
        assert txn["recon_status"] == "cleared"
        assert txn["cleared_on"] == "2026-04-15"
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn["id"],)
        ).fetchone()
        assert split["amount_cents"] == -999
        cat = conn.execute("SELECT name FROM categories WHERE id=?", (split["category_id"],)).fetchone()
        assert cat["name"] == "Uncategorized"


def test_legacy_alias_learning_is_frozen(app_env):
    with engine.write_tx(app_env) as conn:
        with pytest.raises(ValueError, match="merchant_aliases is frozen"):
            repo_ledger.learn_merchant_alias(
                conn,
                "FANCY GYM",
                "Fancy Gym",
                category_id=6,
            )
        doc_id = _make_doc(conn, "t6")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-16", description="FANCY GYM #99", amount_cents=-4500),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result["promoted"] == 0
    assert result["needs_review"] == 1

    with engine.write_tx(app_env) as conn:
        line_id = int(_line_for(conn, doc_id)["id"])
        assert recon_apply.promote_line(conn, line_id) is not None

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?",
            (line["matched_transaction_id"],),
        ).fetchone()
        category = conn.execute(
            "SELECT name FROM categories WHERE id=?",
            (split["category_id"],),
        ).fetchone()
        assert category["name"] == "Uncategorized"


def test_trusted_statement_category_knowledge_only_enqueues_review_proposal(
    app_env,
):
    with engine.write_tx(app_env) as conn:
        evidence_txn_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=1,
                posted_on="2026-03-16",
                description="FANCY GYM #99",
                counterparty="FANCY GYM #99",
                amount_cents=-3200,
                source="manual",
                external_id="manual:fancy-gym-evidence",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        evidence_split_id = repo_ledger.insert_split(
            conn,
            transaction_id=evidence_txn_id,
            category_id=6,
            amount_cents=-3200,
        )
        trusted_claim_id = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="FANCY GYM #99",
            category_id=6,
            scope=repo_merchant_knowledge.scope_for(conn),
            operation_key="test:fancy-gym-category-evidence",
            actor="test:operator",
            reason="operator confirmed the gym category",
            evidence=Evidence(
                transaction_id=evidence_txn_id,
                transaction_split_id=evidence_split_id,
            ),
        )
        doc_id = _make_doc(conn, "t6-knowledge")
        _stage(
            conn,
            doc_id,
            account_id=1,
            rows=[
                StatementRow(
                    posted_on="2026-04-16",
                    description="FANCY GYM #99",
                    amount_cents=-4500,
                )
            ],
        )

    result = reconcile_document(app_env, doc_id, llm=None)

    assert result == {
        "matched": 0,
        "promoted": 0,
        "needs_review": 1,
        "ignored_pending": 0,
    }
    with engine.write_tx(app_env) as conn:
        line_id = int(_line_for(conn, doc_id)["id"])
        assert recon_apply.promote_line(conn, line_id) is not None

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        transaction_id = int(line["matched_transaction_id"])
        split = conn.execute(
            """
            SELECT split.id, split.category_id, category.name
            FROM transaction_splits split
            JOIN categories category ON category.id=split.category_id
            WHERE split.transaction_id=?
            """,
            (transaction_id,),
        ).fetchone()
        proposals = [
            proposal
            for proposal in repo_actions.list_proposals(
                conn,
                statuses=("proposed",),
            )
            if proposal["payload"].get("transaction_id") == transaction_id
        ]
        claim = repo_merchant_knowledge.current_claim(
            conn,
            trusted_claim_id,
        )

    assert split["name"] == "Uncategorized"
    assert len(proposals) == 1
    assert proposals[0]["payload"] == {
        "transaction_id": transaction_id,
        "to_category_id": 6,
    }
    assert proposals[0]["agent_run_id"] == "merchant-resolution"
    assert proposals[0]["evidence"]["trusted_claim_ids"] == [
        trusted_claim_id
    ]
    assert proposals[0]["evidence"]["transaction_split_ids"] == [
        int(split["id"])
    ]
    assert claim["event_kind"] == "accepted"
    assert claim["trust_state"] == "human_confirmed"


# ---- pending lines ------------------------------------------------------------

def test_pending_line_needs_review_only(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t7")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-17", description="PENDING AUTH", amount_cents=-1234,
                        is_pending=True),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result == {"matched": 0, "promoted": 0, "needs_review": 0, "ignored_pending": 1}

    with engine.read_conn(app_env) as conn:
        line = _line_for(conn, doc_id)
        assert line["match_status"] == "needs_review"
        assert line["matched_transaction_id"] is None
        assert "pending" in line["match_rationale"]
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='statement'").fetchone()[0]
        assert n == 0
        doc = repo_documents.get_document(conn, doc_id)
        assert doc["status"] == "needs_review"


# ---- idempotency / re-run safety --------------------------------------------

def test_rerun_is_idempotent(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t8")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-18", description="ONE OFF MERCHANT", amount_cents=-321),
        ])

    first = reconcile_document(app_env, doc_id, llm=None)
    assert first == {
        "matched": 0,
        "promoted": 0,
        "needs_review": 1,
        "ignored_pending": 0,
    }

    with engine.read_conn(app_env) as conn:
        txn_count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    second = reconcile_document(app_env, doc_id, llm=None)
    assert second == {"matched": 0, "promoted": 0, "needs_review": 0, "ignored_pending": 0}

    with engine.read_conn(app_env) as conn:
        txn_count_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert txn_count_after == txn_count_before


def test_promote_twice_is_a_noop_collision(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t9")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-19", description="ONE TIME THING", amount_cents=-741),
        ])

    reconcile_document(app_env, doc_id, llm=None)

    with engine.write_tx(app_env) as conn:
        line_id = _line_for(conn, doc_id)["id"]
        first = recon_apply.promote_line(conn, line_id)
        assert first is not None
        again = recon_apply.promote_line(conn, line_id)
        assert again is None
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "needs_review"
        assert line["match_rationale"] == "promotion collided"

    with engine.read_conn(app_env) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='statement' AND amount_cents=-741"
        ).fetchone()[0]
        assert n == 1


# ---- manual actions -----------------------------------------------------------

def test_unreconcile_document(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t10")
        cand_id = _insert_candidate(conn, account_id=3, posted_on="2026-04-18",
                                    description="coffee", counterparty="Beanery",
                                    amount_cents=-660, ext="rcpt:t10")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-20", description="BEANERY #7", amount_cents=-660),
            StatementRow(posted_on="2026-04-21", description="NEW MERCHANT ABC", amount_cents=-825),
        ])

    result = reconcile_document(app_env, doc_id, llm=None)
    assert result["matched"] == 0
    assert result["promoted"] == 0
    assert result["needs_review"] == 2

    with engine.write_tx(app_env) as conn:
        lines = conn.execute(
            """
            SELECT id, amount_cents FROM statement_lines
            WHERE source_document_id=?
            """,
            (doc_id,),
        ).fetchall()
        by_amount = {int(line["amount_cents"]): int(line["id"]) for line in lines}
        assert recon_apply.promote_line(conn, by_amount[-825]) is not None
        recon_apply.confirm_match(conn, by_amount[-660], cand_id)

    with engine.write_tx(app_env) as conn:
        summary = recon_apply.unreconcile_document(conn, doc_id)
    assert summary == {"lines": 2, "reset": 1, "removed": 1}

    with engine.read_conn(app_env) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (cand_id,)).fetchone()
        assert txn["recon_status"] == "uncleared"
        assert txn["cleared_on"] == ""
        lines = repo_statements.lines_for_document(conn, doc_id)
        assert len(lines) == 2  # lines are PRESERVED, just reset — not deleted
        assert all(l["match_status"] == "unmatched" for l in lines)
        assert all(l["matched_transaction_id"] is None for l in lines)
        assert all(l["match_method"] == "" and l["match_score"] == 0 and l["match_rationale"] == ""
                   for l in lines)
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='statement'").fetchone()[0]
        assert n == 0
        split_n = conn.execute(
            "SELECT COUNT(*) FROM transaction_splits WHERE transaction_id NOT IN (SELECT id FROM transactions)"
        ).fetchone()[0]
        assert split_n == 0  # promoted txn's split cascaded away
        doc = repo_documents.get_document(conn, doc_id)
        assert doc["status"] == "processed"


def test_unreconcile_document_preserves_lines_for_rerun(app_env):
    """Fix 5 regression: unreconcile must not delete the doc's lines — the recon UI has no
    in-app recovery for that. Lines stay visible ('unmatched') and reconcile_document can
    simply be re-run against them."""
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t10b")
        _insert_candidate(conn, account_id=1, posted_on="2026-04-18",
                          description="coffee", counterparty="Beanery",
                          amount_cents=-660, ext="rcpt:t10b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-20", description="BEANERY #7", amount_cents=-660),
        ])

    first = reconcile_document(app_env, doc_id, llm=None)
    assert first["needs_review"] == 1
    with engine.write_tx(app_env) as conn:
        line_id = _line_for(conn, doc_id)["id"]
        candidate_id = conn.execute(
            """
            SELECT id FROM transactions
            WHERE external_id='rcpt:t10b'
            """
        ).fetchone()["id"]
        recon_apply.confirm_match(conn, int(line_id), int(candidate_id))
    with engine.write_tx(app_env) as conn:
        recon_apply.unreconcile_document(conn, doc_id)

    with engine.read_conn(app_env) as conn:
        lines = repo_statements.lines_for_document(conn, doc_id)
        assert len(lines) == 1
        assert lines[0]["match_status"] == "unmatched"

    second = reconcile_document(app_env, doc_id, llm=None)
    assert second["matched"] == 0
    assert second["needs_review"] == 1


def test_unreconcile_undoes_only_manual_match_merchant_knowledge(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t10c")
        candidate_id = _insert_candidate(
            conn,
            account_id=3,
            posted_on="2026-04-18",
            description="coffee",
            counterparty="Beanery",
            amount_cents=-660,
            ext="rcpt:t10c",
        )
        _stage(
            conn,
            doc_id,
            account_id=1,
            rows=[
                StatementRow(
                    posted_on="2026-04-20",
                    description="BEANERY #7",
                    amount_cents=-660,
                )
            ],
        )

    assert reconcile_document(app_env, doc_id, llm=None)["needs_review"] == 1
    with engine.write_tx(app_env) as conn:
        line_id = int(_line_for(conn, doc_id)["id"])
        recon_apply.confirm_match(conn, line_id, candidate_id)
        manual_claim = conn.execute(
            """
            SELECT claim_id, acceptance_event_id
            FROM v_active_merchant_resolution_claims
            WHERE provenance_kind='manual_match'
              AND statement_line_id=?
              AND transaction_id=?
            """,
            (line_id, candidate_id),
        ).fetchone()
        assert manual_claim is not None
        unrelated_claim_id = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor="UNRELATED LOYALTY SUBSCRIPTION",
            canonical_name="Unrelated Loyalty",
            scope=repo_merchant_knowledge.scope_for(conn, account_id=1),
            operation_key="test:unrelated-merchant-evidence",
            actor="test:operator",
            reason="operator confirmed independent transaction evidence",
            evidence=Evidence(transaction_id=candidate_id),
        )

    with engine.write_tx(app_env) as conn:
        summary = recon_apply.unreconcile_document(conn, doc_id)

    assert summary == {"lines": 1, "reset": 1, "removed": 0}
    with engine.read_conn(app_env) as conn:
        manual_event = conn.execute(
            """
            SELECT event_kind, trust_state, actor, reason, reverses_event_id
            FROM merchant_resolution_events
            WHERE claim_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(manual_claim["claim_id"]),),
        ).fetchone()
        unrelated = repo_merchant_knowledge.current_claim(
            conn,
            unrelated_claim_id,
        )
        active_manual_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM v_active_merchant_resolution_claims
            WHERE provenance_kind='manual_match'
              AND statement_line_id=?
              AND transaction_id=?
            """,
            (line_id, candidate_id),
        ).fetchone()[0]

    assert dict(manual_event) == {
        "event_kind": "undo",
        "trust_state": "retired",
        "actor": "operator:reconciliation",
        "reason": "operator unreconciled the supporting statement-line match",
        "reverses_event_id": int(manual_claim["acceptance_event_id"]),
    }
    assert active_manual_count == 0
    assert unrelated["event_kind"] == "accepted"
    assert unrelated["trust_state"] == "human_confirmed"

    assert reconcile_document(app_env, doc_id, llm=None)["needs_review"] == 1
    with engine.write_tx(app_env) as conn:
        recon_apply.confirm_match(conn, line_id, candidate_id)
        reactivated = conn.execute(
            """
            SELECT claim_id
            FROM v_active_merchant_resolution_claims
            WHERE provenance_kind='manual_match'
              AND statement_line_id=?
              AND transaction_id=?
            """,
            (line_id, candidate_id),
        ).fetchall()
    assert len(reactivated) == 1
    assert int(reactivated[0]["claim_id"]) != int(manual_claim["claim_id"])


def test_confirm_match_records_only_scoped_merchant_claim(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t11")
        cand_id = _insert_candidate(conn, account_id=3, posted_on="2026-04-01",
                                    description="dinner", counterparty="Old School Diner",
                                    amount_cents=-2200, ext="rcpt:t11")
        repo_ledger.insert_split(conn, transaction_id=cand_id, category_id=5, amount_cents=-2200)
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-25", description="OLD SCHOOL DINER #3", amount_cents=-2200),
        ])
        line_id = _line_for(conn, doc_id)["id"]

    with engine.write_tx(app_env) as conn:
        recon_apply.confirm_match(conn, line_id, cand_id)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "matched"
        assert line["match_method"] == "manual"
        assert line["matched_transaction_id"] == cand_id
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (cand_id,)).fetchone()
        assert txn["recon_status"] == "cleared"
        assert txn["account_id"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases"
        ).fetchone()[0] == 0
        claims = conn.execute(
            """
            SELECT claim_kind, event_kind, trust_state, category_id
            FROM v_current_merchant_resolution_claims
            WHERE transaction_id=?
            """,
            (cand_id,),
        ).fetchall()
        assert [tuple(row) for row in claims] == [
            ("canonical_merchant", "accepted", "human_confirmed", None)
        ]


def test_confirm_match_second_line_on_already_matched_txn_rejected(app_env):
    """Fix 2 regression: manual confirm must not attach a second line to an already-
    reconciled txn."""
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t11b")
        cand_id = _insert_candidate(conn, account_id=1, posted_on="2026-04-01",
                                    description="dinner", counterparty="Old School Diner",
                                    amount_cents=-2200, ext="rcpt:t11b")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-25", description="OLD SCHOOL DINER #3", amount_cents=-2200),
            StatementRow(posted_on="2026-04-26", description="OLD SCHOOL DINER #4", amount_cents=-2200),
        ])
        lines = repo_statements.lines_for_document(conn, doc_id)
        line1_id, line2_id = lines[0]["id"], lines[1]["id"]

    with engine.write_tx(app_env) as conn:
        recon_apply.confirm_match(conn, line1_id, cand_id)

    with pytest.raises(ValueError, match="already reconciled"):
        with engine.write_tx(app_env) as conn:
            recon_apply.confirm_match(conn, line2_id, cand_id)

    with engine.read_conn(app_env) as conn:
        line2 = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line2_id,)).fetchone()
        assert line2["match_status"] == "unmatched"  # untouched by the rejected confirm
        assert line2["matched_transaction_id"] is None


def test_ignore_line(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _make_doc(conn, "t12")
        _stage(conn, doc_id, account_id=1, rows=[
            StatementRow(posted_on="2026-04-26", description="SKIP ME", amount_cents=-111),
        ])
        line_id = _line_for(conn, doc_id)["id"]
        recon_apply.ignore_line(conn, line_id)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "ignored"


# ---- scoring unit tests -------------------------------------------------------

def test_date_score_shape():
    assert date_score("2026-04-10", "2026-04-08") == pytest.approx(1.0)      # lag=2, peak
    assert date_score("2026-04-10", "2026-04-10") == pytest.approx(1 - 2 / 8)  # lag=0
    assert date_score("2026-04-10", "2026-04-11") == pytest.approx(1 - 3 / 8)  # lag=-1
    assert date_score("2026-04-10", "2026-04-01") == pytest.approx(1 - 7 / 8)  # lag=9
    assert date_score("2026-01-01", "2026-06-01") >= 0.0  # never negative


def test_composite_weights():
    assert composite(1.0, 1.0, 1.0) == pytest.approx(1.0)
    assert composite(1.0, 0.0, 0.0) == pytest.approx(0.55)
    assert composite(0.0, 1.0, 0.0) == pytest.approx(0.30)
    assert composite(0.0, 0.0, 1.0) == pytest.approx(0.15)


def test_merchant_score_blank_and_exact():
    assert merchant_score("", "ExampleMarket") == 0.0
    assert merchant_score("Loblaws #123", "Loblaws Store 123") == pytest.approx(1.0)
