"""Recording research findings into scoped merchant knowledge.

The point of persistence is that research volume decays: a descriptor that has
been answered is never searched again. Findings are recorded as untrusted
proposals, so nothing here promotes a web guess into a fact.
"""
from __future__ import annotations

from app.agents.merchant_research.models import MerchantFinding
from app.agents.merchant_research.persistence import (
    already_known,
    operation_key,
    record_finding,
)
from app.db import engine, repo_merchant_knowledge
from app.db.repo_merchant_knowledge import Evidence


def _scope(db_path):
    with engine.read_conn(db_path) as conn:
        return repo_merchant_knowledge.scope_for(conn)


def _web_finding(descriptor="SP SYNTHETICINK", merchant="SyntheticInk"):
    return MerchantFinding(
        descriptor=descriptor, resolved=True, source="web_evidence",
        canonical_merchant=merchant, category="shopping", confidence=0.95,
        citations=("https://www.syntheticink.com/",), searched=True, processor="stripe",
    )


def test_a_web_finding_is_recorded_with_its_citation(empty_db):
    scope = _scope(empty_db)
    with engine.write_tx(empty_db) as conn:
        claim_id = record_finding(conn, _web_finding(), scope=scope)
    assert claim_id is not None

    with engine.read_conn(empty_db) as conn:
        row = conn.execute(
            """SELECT entity.canonical_name
               FROM merchant_resolution_claims claim
               JOIN merchant_entities entity
                 ON entity.id = claim.merchant_entity_id
               WHERE claim.id=?""",
            (claim_id,),
        ).fetchone()
    assert row["canonical_name"] == "SyntheticInk"


def test_lookup_reports_unknown_before_anything_is_recorded(empty_db):
    scope = _scope(empty_db)
    with engine.read_conn(empty_db) as conn:
        assert already_known(conn, "SP SYNTHETICINK", scope=scope) is False


def test_abstentions_are_never_recorded(empty_db):
    scope = _scope(empty_db)
    finding = MerchantFinding(descriptor="SP SYNTHETIC NOISE", resolved=False,
                              abstention_reason="model_abstained")
    with engine.write_tx(empty_db) as conn:
        assert record_finding(conn, finding, scope=scope) is None
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM merchant_resolution_claims"
        ).fetchone()["n"]
    assert count == 0


def test_a_model_finding_records_without_a_citation(empty_db):
    """Only a *web* claim needs a citation; a local-model claim does not."""
    scope = _scope(empty_db)
    finding = MerchantFinding(
        descriptor="SP MYSTERY", resolved=True, source="local_model",
        canonical_merchant="Mystery", category="shopping", searched=True,
    )
    with engine.write_tx(empty_db) as conn:
        assert record_finding(conn, finding, scope=scope) is not None


def test_an_unaccepted_proposal_does_not_count_as_known(empty_db):
    """A proposal is not knowledge until a human accepts it.

    If merely proposing a merchant made the descriptor "known", research would
    stop revisiting it and an unreviewed model guess would quietly become the
    answer.
    """
    scope = _scope(empty_db)
    with engine.write_tx(empty_db) as conn:
        assert record_finding(conn, _web_finding(), scope=scope) is not None
    with engine.read_conn(empty_db) as conn:
        assert already_known(conn, "SP SYNTHETICINK", scope=scope) is False


def test_operation_key_is_stable_for_the_same_answer(empty_db):
    scope = _scope(empty_db)
    first = operation_key(_web_finding(), scope=scope)
    second = operation_key(_web_finding(), scope=scope)
    assert first == second
    # A different answer for the same descriptor is a different operation.
    other = operation_key(_web_finding(merchant="Something Else"), scope=scope)
    assert other != first


def test_recording_the_same_finding_twice_is_idempotent(empty_db):
    scope = _scope(empty_db)
    with engine.write_tx(empty_db) as conn:
        first = record_finding(conn, _web_finding(), scope=scope)
    with engine.write_tx(empty_db) as conn:
        second = record_finding(conn, _web_finding(), scope=scope)
    assert first == second

    with engine.read_conn(empty_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM merchant_resolution_claims"
        ).fetchone()["n"]
    assert count == 1
