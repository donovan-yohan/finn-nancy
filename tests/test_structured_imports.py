from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from app.db import (
    engine,
    repo_close,
    repo_jobs,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_structured_imports,
)
from app.ingest.structured.service import (
    confirm_import,
    load_preview,
    preview_import,
)
from app.ingest.structured.types import ImportMetadata, MappedCsvV1
from app.ingest.storage import ClientCaptureConflict, capture


CSV_MAPPING = MappedCsvV1(
    date_column="date",
    description_column="description",
    amount_column="amount",
    currency_column="currency",
    pending_column="pending",
    fitid_column="fitid",
)


def _csv(*rows: tuple[str, str, str, str, str, str]) -> bytes:
    body = ["date,description,amount,currency,pending,fitid"]
    body.extend(",".join(row) for row in rows)
    return ("\n".join(body) + "\n").encode()


def _metadata() -> ImportMetadata:
    return ImportMetadata(
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=10_000,
        closing_balance_cents=8_000,
    )


def _preview_csv(raw: bytes, *, account_id: int = 3):
    return preview_import(
        raw=raw,
        original_name="statement.csv",
        declared_mime="text/csv",
        account_id=account_id,
        adapter_kind="mapped_csv",
        mapping=CSV_MAPPING,
        metadata=_metadata(),
        actor="test:structured",
    )


def _ofx(
    *transactions: tuple[str, str, str, str],
    account_token: str = "provider-secret-account-4242",
    currency: str = "CAD",
) -> bytes:
    rows = "".join(
        f"""
        <STMTTRN>
          <DTPOSTED>{posted.replace("-", "")}120000.000[-5:EST]</DTPOSTED>
          <TRNAMT>{amount}</TRNAMT>
          <FITID>{fitid}</FITID>
          <NAME>{description}</NAME>
        </STMTTRN>"""
        for posted, description, amount, fitid in transactions
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE"
      OLDFILEUID="NONE" NEWFILEUID="NONE"?>
    <OFX>
      <SIGNONMSGSRSV1><SONRS><DTSERVER>20260701120000.000[-5:EST]</DTSERVER>
        <FI><ORG>Synthetic Bank</ORG><FID>9001</FID></FI>
      </SONRS></SIGNONMSGSRSV1>
      <BANKMSGSRSV1><STMTTRNRS><STMTRS>
        <CURDEF>{currency}</CURDEF>
        <BANKACCTFROM><BANKID>001</BANKID><ACCTID>{account_token}</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>
        <BANKTRANLIST><DTSTART>20260601120000.000[-5:EST]</DTSTART>
          <DTEND>20260630120000.000[-5:EST]</DTEND>{rows}
        </BANKTRANLIST>
        <LEDGERBAL><BALAMT>80.00</BALAMT>
          <DTASOF>20260630120000.000[-5:EST]</DTASOF></LEDGERBAL>
      </STMTRS></STMTTRNRS></BANKMSGSRSV1>
    </OFX>""".encode()


def test_preview_is_durable_non_llm_and_confirmation_preserves_repeated_rows(app_env):
    raw = _csv(
        ("2026-06-10", "Same fare", "-10.00", "CAD", "posted", "csv-1"),
        ("2026-06-10", "Same fare", "-10.00", "CAD", "posted", "csv-2"),
    )
    result = _preview_csv(raw)
    imported = result.imported
    assert imported["status"] == "preview_ready"
    assert result.parsed is not None
    assert len(result.parsed.rows) == 2

    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE source_document_id=?",
            (int(imported["source_document_id"]),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(imported["source_document_id"]),),
        ).fetchone()[0] == 0

    confirmed = confirm_import(
        int(imported["id"]),
        expected_revision=int(imported["revision"]),
        actor="test:structured",
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["staged_count"] == 2

    with engine.read_conn(app_env) as conn:
        review = conn.execute(
            "SELECT * FROM statement_reviews WHERE id=?",
            (int(confirmed["statement_review_id"]),),
        ).fetchone()
        assert review["source_kind"] == "structured_rows"
        completeness = repo_statement_reviews.completeness(
            conn, int(review["id"])
        )
        assert completeness["complete"] is True
        assert "page_sequence_incomplete" not in completeness["review_blockers"]
        assert review["review_state"] == "approved"
        link = repo_statement_expectations.active_link_for_document(
            conn, int(imported["source_document_id"])
        )
        assert link is not None
        assert link["lifecycle_state"] == "reviewed"
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE source_document_id=? AND type='reconcile_document'""",
            (int(imported["source_document_id"]),),
        ).fetchone()[0] == 1
        lines = conn.execute(
            """SELECT amount_cents, source_anchor_id FROM statement_lines
               WHERE source_document_id=? ORDER BY id""",
            (int(imported["source_document_id"]),),
        ).fetchall()
        assert [row["amount_cents"] for row in lines] == [-1000, -1000]
        assert all(row["source_anchor_id"] is not None for row in lines)
        identity_rows = repo_structured_imports.rows_for_import(
            conn, int(imported["id"])
        )
        assert [row["occurrence_ordinal"] for row in identity_rows] == [0, 1]
        assert {row["disposition"] for row in identity_rows} == {"staged"}
        raw_anchors = conn.execute(
            """SELECT locator_json FROM statement_source_anchors
               WHERE statement_review_id=? AND locator_kind='raw_row'
               ORDER BY id""",
            (int(review["id"]),),
        ).fetchall()
        assert len(raw_anchors) == 2
        assert json.loads(raw_anchors[0]["locator_json"])["row_number"] == 2

    repeated = confirm_import(
        int(imported["id"]),
        expected_revision=int(confirmed["revision"]),
        actor="test:retry",
    )
    assert repeated["id"] == confirmed["id"]
    assert repeated["revision"] == confirmed["revision"]


def test_partial_overlap_fails_closed_without_staging_any_new_row(app_env):
    first = _preview_csv(
        _csv(
            ("2026-06-10", "Alpha", "-10.00", "CAD", "posted", ""),
            ("2026-06-11", "Bravo", "-20.00", "CAD", "posted", ""),
        )
    ).imported
    confirm_import(int(first["id"]), expected_revision=int(first["revision"]))

    second = _preview_csv(
        _csv(
            ("2026-06-11", "Bravo", "-20.00", "CAD", "posted", ""),
            ("2026-06-12", "Charlie", "-30.00", "CAD", "posted", ""),
        )
    ).imported
    blocked = confirm_import(
        int(second["id"]), expected_revision=int(second["revision"])
    )
    assert blocked["status"] == "needs_review"
    assert blocked["overlap_kind"] == "partial"
    assert blocked["staged_count"] == 0
    assert json.loads(blocked["review_reasons_json"]) == ["partial_overlap"]

    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(second["source_document_id"]),),
        ).fetchone()[0] == 0
        rows = repo_structured_imports.rows_for_import(conn, int(second["id"]))
        assert rows == []
        assert blocked["statement_review_id"] is None


def test_byte_different_cross_format_exact_duplicate_keeps_source_evidence(app_env):
    csv_import = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    ).imported
    confirm_import(
        int(csv_import["id"]), expected_revision=int(csv_import["revision"])
    )

    ofx_result = preview_import(
        raw=_ofx(("2026-06-10", "Coffee", "-10.00", "provider-row-1")),
        original_name="statement.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
        actor="test:structured",
    )
    duplicate = confirm_import(
        int(ofx_result.imported["id"]),
        expected_revision=int(ofx_result.imported["revision"]),
    )
    assert duplicate["status"] == "duplicate"
    assert duplicate["overlap_kind"] == "exact"
    assert duplicate["duplicate_count"] == 1
    assert duplicate["staged_count"] == 0

    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(ofx_result.imported["source_document_id"]),),
        ).fetchone()[0] == 0
        assert duplicate["statement_review_id"] is None
        assert conn.execute(
            """SELECT COUNT(*) FROM structured_statement_import_audit
               WHERE import_id=? AND event_kind='duplicate_confirmed'""",
            (int(duplicate["id"]),),
        ).fetchone()[0] == 1


def test_ofx_identity_is_account_scoped_and_raw_provider_ids_never_enter_db(app_env):
    account_token = "full-secret-provider-account-4242"
    fitid = "full-secret-provider-fitid-777"
    result = preview_import(
        raw=_ofx(
            ("2026-06-10", "Coffee", "-10.00", fitid),
            account_token=account_token,
        ),
        original_name="statement.qfx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    )
    imported = result.imported
    assert imported["provider_identity_hash"]
    assert imported["account_last4"] == "4242"
    confirmed = confirm_import(
        int(imported["id"]), expected_revision=int(imported["revision"])
    )
    assert confirmed["status"] == "confirmed"

    with sqlite3.connect(app_env) as conn:
        for table in (
            "structured_statement_imports",
            "structured_statement_import_headers",
            "structured_statement_import_rows",
            "structured_statement_row_supersessions",
            "structured_provider_account_bindings",
            "structured_statement_import_audit",
            "statement_source_anchors",
            "statement_field_evidence",
        ):
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                if row[2].upper().startswith("TEXT")
            ]
            if not columns:
                continue
            expression = " || ' ' || ".join(
                f"COALESCE(CAST({column} AS TEXT),'')" for column in columns
            )
            persisted = conn.execute(
                f"SELECT {expression} FROM {table}"
            ).fetchall()
            flattened = "\n".join(str(value[0]) for value in persisted)
            assert account_token not in flattened
            assert fitid not in flattened


def test_foreign_or_mixed_currency_import_keeps_rows_out_of_ledger(app_env):
    result = _preview_csv(
        _csv(
            ("2026-06-10", "Euro", "-10.00", "EUR", "posted", ""),
            ("2026-06-11", "Dollar", "-20.00", "CAD", "posted", ""),
        )
    )
    assert result.imported["status"] == "needs_review"
    assert "mixed_currency" in json.loads(result.imported["review_reasons_json"])
    blocked = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    assert blocked["status"] == "needs_review"
    assert blocked["staged_count"] == 0
    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(result.imported["source_document_id"]),),
        ).fetchone()[0] == 0


def test_malformed_preview_preserves_original_with_actionable_diagnostics(app_env):
    result = _preview_csv(
        b"date,description,amount,currency,pending,fitid\n"
        b"not-a-date,Coffee,-10.00,CAD,posted,row-1\n"
    )
    assert result.parsed is None
    assert result.imported["status"] == "needs_review"
    diagnostics = json.loads(result.imported["diagnostics_json"])
    assert diagnostics == [
        {
            "code": "date_invalid",
            "field": "date",
            "message": "A transaction date does not match the selected format.",
            "row_number": 2,
        }
    ]
    loaded = load_preview(int(result.imported["id"]))
    assert loaded.parsed is None
    with engine.read_conn(app_env) as conn:
        document = conn.execute(
            "SELECT * FROM source_documents WHERE id=?",
            (int(result.imported["source_document_id"]),),
        ).fetchone()
        assert document["kind"] == "statement"
        assert document["status"] == "needs_review"
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE source_document_id=?",
            (int(document["id"]),),
        ).fetchone()[0] == 0


def test_confirmation_rejects_stale_preview_revision(app_env):
    result = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    )
    with pytest.raises(ValueError, match="revision conflict"):
        confirm_import(
            int(result.imported["id"]),
            expected_revision=int(result.imported["revision"]) + 1,
        )


def test_incomplete_confirm_uses_canonical_completeness_and_stays_received(app_env):
    result = preview_import(
        raw=_csv(
            ("2026-06-10", "Coffee", "-10.00", "CAD", "posted", "")
        ),
        original_name="incomplete.csv",
        declared_mime="text/csv",
        account_id=3,
        adapter_kind="mapped_csv",
        mapping=CSV_MAPPING,
    )
    confirmed = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["staged_count"] == 1
    with engine.read_conn(app_env) as conn:
        review = repo_statement_reviews.get_review(
            conn, int(confirmed["statement_review_id"])
        )
        assert review["review_state"] == "pending"
        completeness = repo_statement_reviews.completeness(
            conn, int(review["id"])
        )
        assert "missing_statement_issued_on" in completeness["review_blockers"]
        link = repo_statement_expectations.active_link_for_document(
            conn, int(result.imported["source_document_id"])
        )
        assert link is not None
        assert link["lifecycle_state"] == "received"
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE source_document_id=? AND type='reconcile_document'""",
            (int(result.imported["source_document_id"]),),
        ).fetchone()[0] == 0


def test_closed_period_blocks_confirmation_before_review_or_rows_are_written(app_env):
    result = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    )
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(
            conn, "2026-06", reason="test closed-period contract"
        )
    with pytest.raises(repo_close.MonthLockedError, match="2026-06 is closed"):
        confirm_import(
            int(result.imported["id"]),
            expected_revision=int(result.imported["revision"]),
        )
    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_reviews WHERE source_document_id=?",
            (int(result.imported["source_document_id"]),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(result.imported["source_document_id"]),),
        ).fetchone()[0] == 0
        current = repo_structured_imports.get_import(
            conn, int(result.imported["id"])
        )
        assert current["status"] == "preview_ready"
        assert current["revision"] == result.imported["revision"]


def test_blocked_attempt_is_immutable_and_corrected_attempt_can_stage(app_env):
    raw = _csv(
        ("2026-06-10", "Coffee", "-10.00", "EUR", "posted", "")
    )
    blocked_preview = _preview_csv(raw)
    blocked = confirm_import(
        int(blocked_preview.imported["id"]),
        expected_revision=int(blocked_preview.imported["revision"]),
    )
    assert blocked["status"] == "needs_review"
    assert blocked["evaluated_at"] is not None
    assert blocked["statement_review_id"] is None

    corrected_mapping = replace(CSV_MAPPING, currency_column="")
    corrected = preview_import(
        raw=raw,
        original_name="statement.csv",
        declared_mime="text/csv",
        account_id=3,
        adapter_kind="mapped_csv",
        mapping=corrected_mapping,
        metadata=_metadata(),
    ).imported
    assert corrected["id"] != blocked["id"]
    assert corrected["supersedes_import_id"] == blocked["id"]
    assert corrected["attempt_number"] == 2
    confirmed = confirm_import(
        int(corrected["id"]), expected_revision=int(corrected["revision"])
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["staged_count"] == 1

    exact_replay = preview_import(
        raw=raw,
        original_name="renamed.csv",
        declared_mime="text/csv",
        account_id=3,
        adapter_kind="mapped_csv",
        mapping=corrected_mapping,
        metadata=_metadata(),
    ).imported
    assert exact_replay["id"] == confirmed["id"]
    assert confirm_import(
        int(exact_replay["id"]),
        expected_revision=int(exact_replay["revision"]),
    )["revision"] == confirmed["revision"]


def test_digit_preserving_weak_identity_routes_descriptor_collision_to_review(app_env):
    first = _preview_csv(
        _csv(("2026-06-10", "UBER TRIP 123", "-10.00", "CAD", "posted", ""))
    ).imported
    confirm_import(int(first["id"]), expected_revision=int(first["revision"]))

    second = _preview_csv(
        _csv(("2026-06-10", "UBER TRIP 456", "-10.00", "CAD", "posted", ""))
    ).imported
    blocked = confirm_import(
        int(second["id"]), expected_revision=int(second["revision"])
    )
    assert blocked["status"] == "needs_review"
    assert blocked["overlap_kind"] == "ambiguous"
    assert "identity_conflict" in json.loads(blocked["review_reasons_json"])
    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (int(second["source_document_id"]),),
        ).fetchone()[0] == 0


def test_subset_and_cross_format_memo_variation_are_not_exact_duplicates(app_env):
    first = _preview_csv(
        _csv(
            ("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""),
            ("2026-06-11", "Lunch", "-20.00", "CAD", "posted", ""),
        )
    ).imported
    confirm_import(int(first["id"]), expected_revision=int(first["revision"]))

    subset = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    ).imported
    subset_result = confirm_import(
        int(subset["id"]), expected_revision=int(subset["revision"])
    )
    assert subset_result["status"] == "needs_review"
    assert subset_result["overlap_kind"] == "ambiguous"

    memo = preview_import(
        raw=_ofx(("2026-06-10", "Coffee memo changed", "-10.00", "fit-1")),
        original_name="memo.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    memo_result = confirm_import(
        int(memo["id"]), expected_revision=int(memo["revision"])
    )
    assert memo_result["status"] == "needs_review"
    assert memo_result["overlap_kind"] == "ambiguous"


def test_pending_fitid_is_atomically_superseded_by_posted_successor(app_env):
    pending_raw = _ofx(
        ("2026-06-10", "Pending coffee", "-10.00", "same-fitid")
    ).replace(b"<STMTTRN>", b"<STMTTRNP>").replace(
        b"</STMTTRN>", b"</STMTTRNP>"
    )
    pending = preview_import(
        raw=pending_raw,
        original_name="pending.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    pending_confirmed = confirm_import(
        int(pending["id"]), expected_revision=int(pending["revision"])
    )
    assert pending_confirmed["status"] == "confirmed"

    posted = preview_import(
        raw=_ofx(
            ("2026-06-12", "Final coffee", "-11.00", "same-fitid")
        ),
        original_name="posted.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    posted_confirmed = confirm_import(
        int(posted["id"]), expected_revision=int(posted["revision"])
    )
    assert posted_confirmed["status"] == "confirmed"
    assert posted_confirmed["overlap_kind"] == "supersession"

    with engine.read_conn(app_env) as conn:
        old_line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?",
            (int(pending["source_document_id"]),),
        ).fetchone()
        new_line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?",
            (int(posted["source_document_id"]),),
        ).fetchone()
        assert old_line["review_disposition"] == "excluded"
        assert old_line["is_pending"] == 1
        assert new_line["review_disposition"] == "active"
        assert new_line["is_pending"] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_lines
               WHERE account_id=3 AND review_disposition='active'
                 AND raw_description IN ('Pending coffee', 'Final coffee')"""
        ).fetchone()[0] == 1
        supersession = conn.execute(
            "SELECT * FROM structured_statement_row_supersessions"
        ).fetchone()
        assert supersession is not None
        audit = conn.execute(
            """SELECT reason FROM statement_review_audit
               WHERE statement_line_id=? AND event_kind='row_excluded'""",
            (int(old_line["id"]),),
        ).fetchone()
        assert f"structured import {int(posted['id'])}" in audit["reason"]
        assert "same-fitid" not in audit["reason"]

    replay = preview_import(
        raw=_ofx(
            ("2026-06-12", "Final coffee", "-11.00", "same-fitid")
        ),
        original_name="posted-copy.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    assert replay["id"] == posted_confirmed["id"]

    stale_raw = _ofx(
        ("2026-06-13", "Stale pending", "-11.00", "same-fitid")
    ).replace(b"<STMTTRN>", b"<STMTTRNP>").replace(
        b"</STMTTRN>", b"</STMTTRNP>"
    )
    stale = preview_import(
        raw=stale_raw,
        original_name="stale.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    stale_result = confirm_import(
        int(stale["id"]), expected_revision=int(stale["revision"])
    )
    assert stale_result["status"] == "needs_review"
    assert stale_result["staged_count"] == 0


def test_repeated_fitid_in_one_import_fails_closed(app_env):
    raw = _ofx(
        ("2026-06-10", "First", "-10.00", "repeat-fitid"),
        ("2026-06-11", "Second", "-20.00", "repeat-fitid"),
    )
    result = preview_import(
        raw=raw,
        original_name="repeat.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    blocked = confirm_import(
        int(result["id"]), expected_revision=int(result["revision"])
    )
    assert blocked["status"] == "needs_review"
    assert blocked["overlap_kind"] == "ambiguous"
    assert blocked["staged_count"] == 0


def test_provider_account_binding_is_normalized_private_and_cross_account_safe(app_env):
    first = preview_import(
        raw=_ofx(
            ("2026-06-10", "Coffee", "-10.00", "fit-one"),
            account_token="12-34 4242",
        ),
        original_name="first.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
    ).imported
    confirm_import(int(first["id"]), expected_revision=int(first["revision"]))

    conflict_preview = preview_import(
        raw=_ofx(
            ("2026-06-14", "Lunch", "-20.00", "fit-two"),
            account_token="12344242",
        ),
        original_name="conflict.ofx",
        declared_mime="application/x-ofx",
        account_id=1,
        adapter_kind="ofx",
    )
    conflict = conflict_preview.imported
    assert conflict["provider_identity_hash"] == first["provider_identity_hash"]
    assert "provider_account_binding_conflict" in json.loads(
        conflict["review_reasons_json"]
    )
    blocked = confirm_import(
        int(conflict["id"]), expected_revision=int(conflict["revision"])
    )
    assert blocked["status"] == "needs_review"
    assert blocked["staged_count"] == 0
    with engine.write_tx(app_env) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured provider account binding is missing",
        ):
            conn.execute(
                """UPDATE structured_statement_imports
                   SET status='confirmed', revision=revision+1,
                       confirmed_at=CURRENT_TIMESTAMP, confirmed_by='test'
                   WHERE id=?""",
                (int(conflict["id"]),),
            )

    different_provider = preview_import(
        raw=_ofx(
            ("2026-06-15", "Lunch", "-20.00", "fit-three"),
            account_token="12344242",
        ).replace(b"<FID>9001</FID>", b"<FID>9002</FID>"),
        original_name="other-provider.ofx",
        declared_mime="application/x-ofx",
        account_id=1,
        adapter_kind="ofx",
    ).imported
    assert different_provider["provider_identity_hash"] != first[
        "provider_identity_hash"
    ]
    with engine.read_conn(app_env) as conn:
        binding = conn.execute(
            """SELECT * FROM structured_provider_account_bindings
               WHERE provider_identity_hash=?""",
            (str(first["provider_identity_hash"]),),
        ).fetchone()
        assert binding["account_id"] == 3
        assert binding["verified_by"]


def test_manual_metadata_fills_only_missing_fields_and_preserves_provenance(app_env):
    conflict = preview_import(
        raw=_ofx(("2026-06-10", "Coffee", "-10.00", "fit-1")),
        original_name="conflict.ofx",
        declared_mime="application/x-ofx",
        account_id=3,
        adapter_kind="ofx",
        metadata=ImportMetadata(
            period_start_on="2026-07-01",
            period_end_on="2026-07-31",
            statement_issued_on="2026-07-02",
            opening_balance_cents=9_000,
            closing_balance_cents=7_000,
        ),
    )
    assert conflict.parsed is not None
    assert conflict.parsed.period_start_on == "2026-06-01"
    assert conflict.parsed.period_end_on == "2026-06-30"
    assert conflict.parsed.closing_balance_cents == 8_000
    reasons = set(json.loads(conflict.imported["review_reasons_json"]))
    assert "metadata_period_start_on_conflict" in reasons
    assert "metadata_period_end_on_conflict" in reasons
    assert "metadata_statement_issued_on_conflict" in reasons
    assert "metadata_closing_balance_cents_conflict" in reasons
    assert {"opening_balance_cents"} == set(conflict.parsed.manual_fields)

    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(
            conn, "2026-06", reason="prove manual metadata cannot bypass lock"
        )
    with pytest.raises(repo_close.MonthLockedError, match="2026-06 is closed"):
        confirm_import(
            int(conflict.imported["id"]),
            expected_revision=int(conflict.imported["revision"]),
        )

    with engine.write_tx(app_env) as conn:
        repo_close.reopen(conn, "2026-06", reason="continue provenance test")
    csv_result = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    )
    confirmed = confirm_import(
        int(csv_result.imported["id"]),
        expected_revision=int(csv_result.imported["revision"]),
    )
    with engine.read_conn(app_env) as conn:
        origins = {
            row["field_name"]: row["origin"]
            for row in conn.execute(
                """SELECT field_name, origin FROM statement_field_evidence
                   WHERE statement_review_id=? AND statement_line_id IS NULL""",
                (int(confirmed["statement_review_id"]),),
            )
        }
        assert origins["period_start_on"] == "manual"
        assert origins["period_end_on"] == "manual"
        assert origins["statement_issued_on"] == "manual"
        assert origins["opening_balance_cents"] == "manual"
        assert origins["closing_balance_cents"] == "manual"
        assert origins["currency"] == "extractor"
        header = conn.execute(
            """SELECT * FROM structured_statement_import_headers
               WHERE import_id=?""",
            (int(csv_result.imported["id"]),),
        ).fetchone()
        assert header["locator_kind"] == "structured_header"
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_source_anchors
               WHERE statement_review_id=? AND locator_kind='raw_row'""",
            (int(confirmed["statement_review_id"]),),
        ).fetchone()[0] == 1


def test_manual_period_that_excludes_a_transaction_is_review_only(app_env):
    result = preview_import(
        raw=_csv(
            ("2026-06-10", "Coffee", "-10.00", "CAD", "posted", "")
        ),
        original_name="bad-period.csv",
        declared_mime="text/csv",
        account_id=3,
        adapter_kind="mapped_csv",
        mapping=CSV_MAPPING,
        metadata=ImportMetadata(
            period_start_on="2026-06-11",
            period_end_on="2026-06-30",
        ),
    )
    assert "transaction_outside_period" in json.loads(
        result.imported["review_reasons_json"]
    )
    blocked = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    assert blocked["status"] == "needs_review"
    assert blocked["staged_count"] == 0


@pytest.mark.parametrize("job_status", ["pending", "running", "done"])
def test_structured_import_fails_closed_when_generic_ingest_owns_bytes(
    app_env, job_status
):
    raw = _csv(
        ("2026-06-10", f"Owned {job_status}", "-10.00", "CAD", "posted", "")
    )
    captured = capture(
        raw=raw,
        original_name="generic.csv",
        channel="web",
        declared_mime="text/csv",
    )
    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (job_status, int(captured["job_id"])),
        )
    with pytest.raises(
        ClientCaptureConflict, match="already have generic ingest work"
    ):
        _preview_csv(raw)
    with engine.read_conn(app_env) as conn:
        document = conn.execute(
            "SELECT * FROM source_documents WHERE id=?",
            (int(captured["source_document_id"]),),
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (int(captured["job_id"]),)
        ).fetchone()
        assert document["kind"] == "upload"
        assert job["status"] == job_status
        assert conn.execute(
            """SELECT COUNT(*) FROM structured_statement_imports
               WHERE source_document_id=?""",
            (int(document["id"]),),
        ).fetchone()[0] == 0
    with engine.write_tx(app_env) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="generic ingest job already owns structured import source",
        ):
            conn.execute(
                """INSERT INTO structured_statement_imports(
                     source_document_id, attempt_number, config_fingerprint,
                     account_id, source_sha256, adapter_id, adapter_version
                   )
                   VALUES (?,1,?,3,?,'mapped_csv','mapped-csv/v1')""",
                (
                    int(captured["source_document_id"]),
                    "e" * 64,
                    str(captured["sha256"]),
                ),
            )


def test_generic_ingest_cannot_claim_a_structured_source_after_preview(app_env):
    result = _preview_csv(
        _csv(("2026-06-10", "Deterministic owner", "-10.00", "CAD", "posted", ""))
    )
    with engine.write_tx(app_env) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured import already owns generic ingest source",
        ):
            repo_jobs.enqueue(
                conn,
                "ingest_document",
                {"source_document_id": int(result.imported["source_document_id"])},
                source_document_id=int(result.imported["source_document_id"]),
            )


def test_import_attempt_has_one_terminal_evaluation_write(app_env):
    result = _preview_csv(
        _csv(("2026-06-10", "Immutable evaluation", "-10.00", "CAD", "posted", ""))
    )
    with engine.write_tx(app_env) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured import must be evaluated exactly once",
        ):
            conn.execute(
                """UPDATE structured_statement_imports
                   SET staged_count=1, revision=revision+1 WHERE id=?""",
                (int(result.imported["id"]),),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured import must be evaluated exactly once",
        ):
            conn.execute(
                """UPDATE structured_statement_imports
                   SET staged_count=1, evaluated_at=CURRENT_TIMESTAMP,
                       revision=revision+1 WHERE id=?""",
                (int(result.imported["id"]),),
            )

    confirmed = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    with engine.write_tx(app_env) as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured import must be evaluated exactly once",
        ):
            conn.execute(
                """UPDATE structured_statement_imports
                   SET staged_count=0, revision=revision+1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (int(confirmed["id"]),),
            )


def test_structured_scope_and_raw_row_correspondence_triggers_reject_cross_links(
    app_env,
):
    result = _preview_csv(
        _csv(("2026-06-10", "Coffee", "-10.00", "CAD", "posted", ""))
    )
    confirmed = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    with engine.write_tx(app_env) as conn:
        imported = repo_structured_imports.get_import(
            conn, int(confirmed["id"])
        )
        review = repo_statement_reviews.get_review(
            conn, int(imported["statement_review_id"])
        )
        identity = repo_structured_imports.rows_for_import(
            conn, int(imported["id"])
        )[0]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured import source or review scope mismatch",
        ):
            conn.execute(
                """INSERT INTO structured_statement_imports(
                     source_document_id, statement_review_id,
                     supersedes_import_id, attempt_number, config_fingerprint,
                     account_id, source_sha256, adapter_id, adapter_version
                   )
                   VALUES (?,?,?,?,?,?,?,'mapped_csv','mapped-csv/v1')""",
                (
                    int(imported["source_document_id"]),
                    int(review["id"]),
                    int(imported["id"]),
                    2,
                    "f" * 64,
                    1,
                    str(imported["source_sha256"]),
                ),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match=(
                "structured import (attempt is immutable"
                "|must be evaluated exactly once)"
            ),
        ):
            conn.execute(
                """UPDATE structured_statement_imports
                   SET account_id=1, revision=revision+1 WHERE id=?""",
                (int(imported["id"]),),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="structured review import scope mismatch",
        ):
            conn.execute(
                """UPDATE statement_reviews
                   SET account_id=1, revision=revision+1,
                       last_operation_key='invalid:scope'
                   WHERE id=?""",
                (int(review["id"]),),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="structured import row scope mismatch"
        ):
            conn.execute(
                """INSERT INTO structured_statement_import_rows(
                     import_id, source_row_number, source_anchor_id,
                     statement_line_id, account_id, provider_identity_hash,
                     fitid_hash, weak_key_hash, coarse_key_hash,
                     occurrence_ordinal, is_pending, currency,
                     disposition, overlap_state
                   )
                   VALUES (?,?,?,?,?,'','',?,?,0,0,'CAD','staged','new')""",
                (
                    int(imported["id"]),
                    999,
                    int(identity["source_anchor_id"]),
                    int(identity["statement_line_id"]),
                    int(imported["account_id"]),
                    "a" * 64,
                    "b" * 64,
                ),
            )


def test_structured_add_and_edit_can_select_only_transaction_raw_rows(app_env):
    result = preview_import(
        raw=_csv(
            ("2026-06-10", "Coffee", "-10.00", "CAD", "posted", "")
        ),
        original_name="editable.csv",
        declared_mime="text/csv",
        account_id=3,
        adapter_kind="mapped_csv",
        mapping=CSV_MAPPING,
    )
    confirmed = confirm_import(
        int(result.imported["id"]),
        expected_revision=int(result.imported["revision"]),
    )
    with engine.write_tx(app_env) as conn:
        review = repo_statement_reviews.get_review(
            conn, int(confirmed["statement_review_id"])
        )
        added = repo_statement_reviews.add_row(
            conn,
            int(review["id"]),
            expected_review_revision=int(review["revision"]),
            actor="test:operator",
            reason="missing row confirmed against imported raw row",
            source_page=1,
            posted_on="2026-06-11",
            description="Added row",
            amount_cents=-500,
            currency="CAD",
        )
        anchor = conn.execute(
            "SELECT * FROM statement_source_anchors WHERE id=?",
            (int(added["source_anchor_id"]),),
        ).fetchone()
        assert anchor["locator_kind"] == "raw_row"
        assert json.loads(anchor["locator_json"])["row_number"] == 2
        current_review = repo_statement_reviews.get_review(
            conn, int(review["id"])
        )
        corrected = repo_statement_reviews.correct_row(
            conn,
            int(added["id"]),
            expected_review_revision=int(current_review["revision"]),
            expected_line_revision=int(added["review_revision"]),
            actor="test:operator",
            reason="corrected against the same imported raw row",
            source_page=1,
            posted_on="2026-06-11",
            description="Added row corrected",
            amount_cents=-500,
            currency="CAD",
            balance_cents=None,
            is_pending=False,
        )
        assert corrected["source_anchor_id"] == anchor["id"]
        assert conn.execute(
            """SELECT COUNT(*) FROM structured_statement_import_headers
               WHERE statement_review_id=?""",
            (int(review["id"]),),
        ).fetchone()[0] == 1
