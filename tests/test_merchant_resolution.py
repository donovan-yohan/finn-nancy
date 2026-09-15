from __future__ import annotations

import sqlite3

import pytest

from app.db import engine, repo_merchant_knowledge
from app.db.repo_merchant_knowledge import Evidence
from app.reconcile.automation_policy import (
    AutomationAuthorityDisabled,
    authority_manifest,
    require_automatic_assignment,
)
from app.reconcile.descriptor_normalization import normalize_descriptor_v2
from app.reconcile.merchant_resolution import resolve_descriptor


def _expense_fixture(conn, *, descriptor: str = "SQ *7-Eleven #042"):
    account_id = int(
        conn.execute(
            "INSERT INTO accounts(name, kind) VALUES ('Card', 'credit')"
        ).lastrowid
    )
    groceries_id = int(
        conn.execute(
            """
            INSERT INTO categories(name, kind, brand_owner)
            VALUES ('Groceries', 'expense', 'shared')
            """
        ).lastrowid
    )
    restaurants_id = int(
        conn.execute(
            """
            INSERT INTO categories(name, kind, brand_owner)
            VALUES ('Restaurants', 'expense', 'shared')
            """
        ).lastrowid
    )
    transaction_id = int(
        conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source, external_id, flow_kind
            )
            VALUES (?, '2026-01-04', ?, ?, -1299,
                    'receipt', 'receipt:merchant-resolution', 'purchase')
            """,
            (account_id, descriptor, "7-Eleven"),
        ).lastrowid
    )
    split_id = int(
        conn.execute(
            """
            INSERT INTO transaction_splits(
              transaction_id, category_id, amount_cents
            )
            VALUES (?, ?, -1299)
            """,
            (transaction_id, groceries_id),
        ).lastrowid
    )
    return {
        "account_id": account_id,
        "groceries_id": groceries_id,
        "restaurants_id": restaurants_id,
        "transaction_id": transaction_id,
        "split_id": split_id,
        "descriptor": descriptor,
    }


def _confirm_pair(conn, fixture, *, prefix: str = "test"):
    scope = repo_merchant_knowledge.scope_for(
        conn,
        account_id=fixture["account_id"],
        processor_family="square",
        region="ca-on",
    )
    merchant_claim_id = repo_merchant_knowledge.confirm_merchant(
        conn,
        descriptor=fixture["descriptor"],
        canonical_name="7-Eleven",
        scope=scope,
        operation_key=f"{prefix}:merchant",
        actor="operator:test",
        reason="operator confirmed canonical merchant",
        evidence=Evidence(transaction_id=fixture["transaction_id"]),
    )
    category_claim_id = repo_merchant_knowledge.confirm_category(
        conn,
        descriptor=fixture["descriptor"],
        category_id=fixture["groceries_id"],
        scope=scope,
        operation_key=f"{prefix}:category",
        actor="operator:test",
        reason="operator confirmed expense category",
        evidence=Evidence(
            transaction_id=fixture["transaction_id"],
            transaction_split_id=fixture["split_id"],
        ),
    )
    return scope, merchant_claim_id, category_claim_id


def test_descriptor_v2_preserves_legitimate_digits_and_unicode_identity():
    numbered = normalize_descriptor_v2("SQ *7-Eleven #042")
    unnumbered = normalize_descriptor_v2("SQ *Eleven #042")
    accented = normalize_descriptor_v2("Märket")
    ascii_only = normalize_descriptor_v2("Market")

    assert numbered.tokens == ("sq", "7", "eleven", "042")
    assert unnumbered.tokens == ("sq", "eleven", "042")
    assert numbered.fingerprint != unnumbered.fingerprint
    assert accented.tokens == ("märket",)
    assert accented.fingerprint != ascii_only.fingerprint


def test_resolver_keeps_merchant_and_category_claims_independent(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope, merchant_claim_id, category_claim_id = _confirm_pair(conn, fixture)

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )

        assert resolved.merchant.status == "resolved"
        assert resolved.merchant.target_name == "7-Eleven"
        assert resolved.merchant.claim_ids == (merchant_claim_id,)
        assert resolved.category.status == "resolved"
        assert resolved.category.target_name == "Groceries"
        assert resolved.category.claim_ids == (category_claim_id,)
        assert resolved.merchant.automatic_assignment_allowed is False
        assert resolved.category.automatic_assignment_allowed is False

        projection = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_split_id=?
            """,
            (fixture["split_id"],),
        ).fetchone()
        assert projection["resolution_status"] == "resolved"


def test_report_breakdown_excludes_unconfirmed_and_wrong_purpose_expenses(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        _confirm_pair(conn, fixture)
        unconfirmed_transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, counterparty,
                  amount_cents, source, external_id, flow_kind
                )
                VALUES (
                  ?, '2026-01-06', 'Unconfirmed cafe', 'Unconfirmed Cafe',
                  -301, 'manual', 'manual:unconfirmed-cafe', 'purchase'
                )
                """,
                (fixture["account_id"],),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO transaction_splits(
              transaction_id, category_id, amount_cents
            )
            VALUES (?, ?, -301)
            """,
            (unconfirmed_transaction_id, fixture["restaurants_id"]),
        )
        income_category_id = int(
            conn.execute(
                """
                INSERT INTO categories(name, kind, brand_owner)
                VALUES ('Salary', 'income', 'shared')
                """
            ).lastrowid
        )
        wrong_purpose_transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, counterparty,
                  amount_cents, source, external_id, flow_kind
                )
                VALUES (
                  ?, '2026-01-07', 'Wrong purpose', 'Wrong Purpose',
                  -200, 'manual', 'manual:wrong-purpose', 'purchase'
                )
                """,
                (fixture["account_id"],),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO transaction_splits(
              transaction_id, category_id, amount_cents
            )
            VALUES (?, ?, -200)
            """,
            (wrong_purpose_transaction_id, income_category_id),
        )

        control = conn.execute(
            """
            SELECT *
            FROM v_expense_resolution_monthly_control
            WHERE month='2026-01'
            """
        ).fetchone()
        resolved_rows = conn.execute(
            """
            SELECT category_name, magnitude_cents, resolved_split_count
            FROM v_resolved_expense_category_monthly
            WHERE month='2026-01'
            ORDER BY category_name
            """
        ).fetchall()
        report_rows = conn.execute(
            """
            SELECT category_name, category_kind, magnitude_cents
            FROM v_report_category_monthly
            WHERE month='2026-01'
            ORDER BY category_name
            """
        ).fetchall()
        cashflow = conn.execute(
            "SELECT expense_cents FROM v_cashflow_monthly WHERE month='2026-01'"
        ).fetchone()
        wrong_purpose_status = conn.execute(
            """
            SELECT category_kind, resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=?
            """,
            (wrong_purpose_transaction_id,),
        ).fetchone()

    assert dict(control) == {
        "month": "2026-01",
        "transaction_count": 3,
        "split_count": 3,
        "money_out_cents": 1800,
        "resolved_expense_cents": 1299,
        "excluded_expense_cents": 501,
        "resolved_split_count": 1,
        "unresolved_split_count": 2,
    }
    assert [tuple(row) for row in resolved_rows] == [
        ("Groceries", 1299, 1)
    ]
    assert [tuple(row) for row in report_rows] == [
        ("Groceries", "expense", 1299)
    ]
    assert cashflow["expense_cents"] == 1800
    assert tuple(wrong_purpose_status) == ("income", "unresolved")


def test_equal_scope_conflicts_abstain_without_cross_claim_contamination(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope, _, category_claim_id = _confirm_pair(conn, fixture)
        repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="Eleven Convenience",
            scope=scope,
            operation_key="conflict:merchant",
            actor="operator:test",
            reason="operator recorded conflicting merchant evidence",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )

        assert resolved.merchant.status == "abstained"
        assert resolved.merchant.reason == "conflicting trusted claims exist at equal scope"
        assert resolved.category.status == "resolved"
        assert resolved.category.claim_ids == (category_claim_id,)


def test_narrow_rejection_vetoes_broad_confirmation(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        broad_scope = repo_merchant_knowledge.scope_for(conn)
        narrow_scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
        )
        repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=broad_scope,
            operation_key="broad:merchant",
            actor="operator:test",
            reason="operator confirmed household merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )
        proposed_claim_id = repo_merchant_knowledge.propose_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="Different Merchant",
            scope=narrow_scope,
            operation_key="narrow:proposal",
            actor_kind="model",
            actor="model:local",
            reason="local model proposed a merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )
        repo_merchant_knowledge.reject_claim(
            conn,
            claim_id=proposed_claim_id,
            operation_key="narrow:reject",
            actor="operator:test",
            reason="operator rejected the narrow merchant proposal",
        )

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=narrow_scope,
        )

        assert resolved.merchant.status == "abstained"
        assert "active rejection" in resolved.merchant.reason


def test_incomparable_applicable_scopes_abstain_even_for_same_target(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        account_scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
        )
        processor_scope = repo_merchant_knowledge.scope_for(
            conn,
            processor_family="square",
        )
        query_scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
            processor_family="square",
        )
        for ordinal, scope in enumerate((account_scope, processor_scope), start=1):
            repo_merchant_knowledge.confirm_merchant(
                conn,
                descriptor=fixture["descriptor"],
                canonical_name="7-Eleven",
                scope=scope,
                operation_key=f"incomparable:{ordinal}",
                actor="operator:test",
                reason="operator confirmed merchant in one bounded scope",
                evidence=Evidence(transaction_id=fixture["transaction_id"]),
            )

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=query_scope,
        )

        assert resolved.merchant.status == "abstained"
        assert resolved.merchant.reason == "applicable claim scopes are incomparable"


def test_provider_and_region_scopes_do_not_bleed(empty_db):
    provider_hash = "a" * 64
    other_provider_hash = "b" * 64
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        document_id = int(
            conn.execute(
                """
                INSERT INTO source_documents(
                  kind, original_name, storage_ref, sha256, status
                )
                VALUES (
                  'statement', 'synthetic.csv', 'structured:scope-test',
                  ?, 'processed'
                )
                """,
                ("c" * 64,),
            ).lastrowid
        )
        import_id = int(
            conn.execute(
                """
                INSERT INTO structured_statement_imports(
                  source_document_id, attempt_number, config_fingerprint,
                  account_id, source_sha256, adapter_id, adapter_version,
                  provider_identity_hash
                )
                VALUES (?, 1, ?, ?, ?, 'mapped_csv', 'test-v1', ?)
                """,
                (
                    document_id,
                    "d" * 64,
                    fixture["account_id"],
                    "c" * 64,
                    provider_hash,
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO structured_provider_account_bindings(
              provider_identity_hash, account_id, first_import_id,
              verified_by, verification_reason
            )
            VALUES (?, ?, ?, 'test:operator', 'synthetic binding')
            """,
            (provider_hash, fixture["account_id"], import_id),
        )
        scoped = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
            provider_identity_hash=provider_hash,
            region="ca-on",
        )
        claim_id = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scoped,
            operation_key="test:provider-region-merchant",
            actor="operator:test",
            reason="operator confirmed provider and region scope",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )

        exact = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scoped,
        )
        no_provider = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=repo_merchant_knowledge.scope_for(
                conn,
                account_id=fixture["account_id"],
                region="ca-on",
            ),
        )
        wrong_provider = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=repo_merchant_knowledge.scope_for(
                conn,
                account_id=fixture["account_id"],
                provider_identity_hash=other_provider_hash,
                region="ca-on",
            ),
        )
        wrong_region = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=repo_merchant_knowledge.scope_for(
                conn,
                account_id=fixture["account_id"],
                provider_identity_hash=provider_hash,
                region="us-ny",
            ),
        )

    assert exact.merchant.status == "resolved"
    assert exact.merchant.claim_ids == (claim_id,)
    assert no_provider.merchant.status == "no_evidence"
    assert wrong_provider.merchant.status == "no_evidence"
    assert wrong_region.merchant.status == "no_evidence"


def test_undo_retires_exact_category_claim_and_blocks_resolution(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope, _, category_claim_id = _confirm_pair(conn, fixture)
        repo_merchant_knowledge.undo_claim(
            conn,
            claim_id=category_claim_id,
            operation_key="undo:category",
            actor="operator:test",
            reason="operator undid the category acceptance",
        )

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )
        projection = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_split_id=?
            """,
            (fixture["split_id"],),
        ).fetchone()

        assert resolved.merchant.status == "resolved"
        assert resolved.category.status == "no_evidence"
        assert projection["resolution_status"] == "unresolved"
        with pytest.raises(ValueError, match="not active"):
            repo_merchant_knowledge.undo_claim(
                conn,
                claim_id=category_claim_id,
                operation_key="undo:category:again",
                actor="operator:test",
                reason="operator repeated an undo",
            )


def test_model_and_web_proposals_never_become_trusted_or_mutate_projection(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
        )
        repo_merchant_knowledge.propose_category(
            conn,
            descriptor=fixture["descriptor"],
            category_id=fixture["groceries_id"],
            scope=scope,
            operation_key="model:category",
            actor_kind="model",
            actor="model:local",
            reason="local model proposed a category",
            evidence=Evidence(
                transaction_id=fixture["transaction_id"],
                transaction_split_id=fixture["split_id"],
            ),
        )
        repo_merchant_knowledge.propose_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scope,
            operation_key="web:merchant",
            actor_kind="web",
            actor="web:search",
            reason="web provider proposed a canonical merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
            citation_url="https://merchant.example/reference",
        )

        resolved = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )
        projection = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_split_id=?
            """,
            (fixture["split_id"],),
        ).fetchone()

        assert resolved.merchant.status == "no_evidence"
        assert resolved.category.status == "no_evidence"
        assert projection["resolution_status"] == "unresolved"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_active_merchant_resolution_claims"
            ).fetchone()[0]
            == 0
        )


def test_web_proposal_requires_structured_citation_and_consent_on_acceptance(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
        )
        with pytest.raises(ValueError, match="requires a citation"):
            repo_merchant_knowledge.propose_merchant(
                conn,
                descriptor=fixture["descriptor"],
                canonical_name="7-Eleven",
                scope=scope,
                operation_key="web:missing-citation",
                actor_kind="web",
                actor="web:test",
                reason="web provider proposed a merchant",
                evidence=Evidence(transaction_id=fixture["transaction_id"]),
            )
        with pytest.raises(ValueError, match="without credentials, query"):
            repo_merchant_knowledge.propose_merchant(
                conn,
                descriptor=fixture["descriptor"],
                canonical_name="7-Eleven",
                scope=scope,
                operation_key="web:unsafe-citation",
                actor_kind="web",
                actor="web:test",
                reason="web provider proposed a merchant",
                evidence=Evidence(transaction_id=fixture["transaction_id"]),
                citation_url="https://merchant.example/reference?raw=snippet",
            )
        claim_id = repo_merchant_knowledge.propose_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scope,
            operation_key="web:valid-proposal",
            actor_kind="web",
            actor="web:test",
            reason="web provider proposed a merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
            provenance_ref="provider-result:synthetic-1",
            citation_url="https://merchant.example/reference",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo_merchant_knowledge.accept_proposal(
                conn,
                claim_id=claim_id,
                operation_key="web:accept-without-consent",
                actor="operator:test",
                reason="operator attempted an incomplete web acceptance",
                evidence=Evidence(transaction_id=fixture["transaction_id"]),
            )
        repo_merchant_knowledge.accept_proposal(
            conn,
            claim_id=claim_id,
            operation_key="web:accept-with-consent",
            actor="operator:test",
            reason="operator accepted cited web evidence",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
            citation_url="https://merchant.example/reference",
            consent_version="web-search-consent.v1",
        )
        current = repo_merchant_knowledge.current_claim(conn, claim_id)

    assert current["event_kind"] == "accepted"
    assert current["provenance_kind"] == "web_search"
    assert current["citation_url"] == "https://merchant.example/reference"
    assert current["consent_version"] == "web-search-consent.v1"


def test_human_acceptance_requires_real_evidence_and_tables_are_immutable(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope = repo_merchant_knowledge.scope_for(conn)
        with pytest.raises(ValueError, match="durable decision subject"):
            repo_merchant_knowledge.confirm_merchant(
                conn,
                descriptor=fixture["descriptor"],
                canonical_name="7-Eleven",
                scope=scope,
                operation_key="missing:evidence",
                actor="operator:test",
                reason="operator attempted an unanchored confirmation",
                evidence=Evidence(),
            )

        claim_id = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scope,
            operation_key="immutable:merchant",
            actor="operator:test",
            reason="operator confirmed merchant with transaction evidence",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )
        with pytest.raises(sqlite3.IntegrityError, match="claims are immutable"):
            conn.execute(
                """
                UPDATE merchant_resolution_claims
                SET claim_key='changed'
                WHERE id=?
                """,
                (claim_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            conn.execute(
                "DELETE FROM merchant_resolution_events WHERE claim_id=?",
                (claim_id,),
            )


def test_operation_key_collision_is_rejected_and_knowledge_digest_is_semantic(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=fixture["account_id"],
        )
        repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scope,
            operation_key="test:stable-operation",
            actor="operator:test",
            reason="operator confirmed canonical merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )
        digest_before_duplicate = repo_merchant_knowledge.knowledge_digest(conn)
        repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=fixture["descriptor"],
            canonical_name="7-Eleven",
            scope=scope,
            operation_key="test:semantic-duplicate",
            actor="operator:test",
            reason="a second subject confirmed the same mapping",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )
        digest_after_duplicate = repo_merchant_knowledge.knowledge_digest(conn)
        with pytest.raises(ValueError, match="operation key was reused"):
            repo_merchant_knowledge.confirm_merchant(
                conn,
                descriptor=fixture["descriptor"],
                canonical_name="Different Merchant",
                scope=scope,
                operation_key="test:stable-operation",
                actor="operator:test",
                reason="conflicting retry payload",
                evidence=Evidence(transaction_id=fixture["transaction_id"]),
            )
        repo_merchant_knowledge.propose_category(
            conn,
            descriptor=fixture["descriptor"],
            category_id=fixture["restaurants_id"],
            scope=scope,
            operation_key="test:untrusted-digest-input",
            actor_kind="model",
            actor="model:test",
            reason="untrusted proposal must not alter knowledge version",
            evidence=Evidence(
                transaction_id=fixture["transaction_id"],
                transaction_split_id=fixture["split_id"],
            ),
        )
        digest_after_proposal = repo_merchant_knowledge.knowledge_digest(conn)
        conn.execute(
            "UPDATE categories SET name='Dining' WHERE id=?",
            (fixture["restaurants_id"],),
        )
        digest_after_unrelated_rename = (
            repo_merchant_knowledge.knowledge_digest(conn)
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=fixture["descriptor"],
            category_id=fixture["groceries_id"],
            scope=scope,
            operation_key="test:trusted-category-digest",
            actor="operator:test",
            reason="operator confirmed category for digest test",
            evidence=Evidence(
                transaction_id=fixture["transaction_id"],
                transaction_split_id=fixture["split_id"],
            ),
        )
        digest_before_trusted_rename = (
            repo_merchant_knowledge.knowledge_digest(conn)
        )
        conn.execute(
            "UPDATE categories SET name='Food' WHERE id=?",
            (fixture["groceries_id"],),
        )
        digest_after_trusted_rename = (
            repo_merchant_knowledge.knowledge_digest(conn)
        )

    assert digest_after_duplicate == digest_before_duplicate
    assert digest_after_proposal == digest_before_duplicate
    # The renamed category has only an untrusted proposal, so it is not part of
    # the semantic accepted/rejected knowledge digest.
    assert digest_after_unrelated_rename == digest_before_duplicate
    assert digest_before_trusted_rename != digest_before_duplicate
    assert digest_after_trusted_rename != digest_before_trusted_rename


def test_correction_and_undo_never_resurrect_the_prior_claim(empty_db):
    with engine.write_tx(empty_db) as conn:
        fixture = _expense_fixture(conn)
        scope, prior_claim_id, _ = _confirm_pair(conn, fixture)
        replacement_claim_id = repo_merchant_knowledge.correct_merchant(
            conn,
            prior_claim_id=prior_claim_id,
            descriptor=fixture["descriptor"],
            canonical_name="Seven Eleven",
            scope=scope,
            operation_key="correct:merchant",
            actor="operator:test",
            reason="operator corrected the canonical merchant",
            evidence=Evidence(transaction_id=fixture["transaction_id"]),
        )

        corrected = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )
        assert corrected.merchant.status == "resolved"
        assert corrected.merchant.target_name == "Seven Eleven"

        repo_merchant_knowledge.undo_claim(
            conn,
            claim_id=replacement_claim_id,
            operation_key="correct:merchant:undo",
            actor="operator:test",
            reason="operator undid the corrected merchant claim",
        )
        after_undo = resolve_descriptor(
            conn,
            descriptor=fixture["descriptor"],
            scope=scope,
        )
        assert after_undo.merchant.status == "no_evidence"
        assert (
            conn.execute(
                """
                SELECT event_kind
                FROM v_current_merchant_resolution_claims
                WHERE claim_id=?
                """,
                (prior_claim_id,),
            ).fetchone()["event_kind"]
            == "retired"
        )


def test_automation_authority_is_independently_disabled_for_every_claim():
    manifest = authority_manifest()

    assert manifest["assignments"] == {
        "same_event": False,
        "canonical_merchant": False,
        "expense_category": False,
    }
    for assignment_kind in manifest["assignments"]:
        with pytest.raises(AutomationAuthorityDisabled):
            require_automatic_assignment(assignment_kind)
