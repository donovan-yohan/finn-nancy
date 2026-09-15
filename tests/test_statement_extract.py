from __future__ import annotations

import fitz  # PyMuPDF

from app.db import engine
from app.ingest.schemas import ExtractedStatement, StatementRow


def _make_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=10)
        y += 14
    return doc.tobytes()


class _RecordingStructured:
    def __init__(self, parent, schema):
        self._parent = parent
        self._schema = schema

    def invoke(self, messages):
        self._parent.calls.append((self._schema, messages))
        return self._parent.results[self._schema]


class _RecordingLLM:
    """Fake LLM: records (schema, messages) per call; returns a canned object keyed by schema."""

    def __init__(self, results: dict):
        self.results = results
        self.calls: list[tuple[type, list]] = []

    def with_structured_output(self, schema, **kwargs):
        return _RecordingStructured(self, schema)


# ---------------------------------------------------------------------------
# digital-vs-scanned routing
# ---------------------------------------------------------------------------

def test_digital_statement_routes_to_text_path(app_env):
    from app.ingest.extract.statement import extract_statement

    lines = ["MONTHLY STATEMENT", "Opening balance 1,000.00", "Closing balance 950.00"]
    lines += [f"2026-06-{i:02d}  Purchase at Merchant {i}   -{i}.00   bal {1000 - i}.00"
              for i in range(1, 20)]
    raw = _make_pdf(lines)  # plenty of text -> digital path

    canned = ExtractedStatement(confidence=0.5)
    llm = _RecordingLLM({ExtractedStatement: canned})

    result = extract_statement(llm, raw)
    assert result.confidence == canned.confidence
    assert result.observed_page_count == 1
    assert result.extracted_page_count == 1
    assert result.extraction_truncated is False
    assert len(llm.calls) == 1
    schema, messages = llm.calls[0]
    assert schema is ExtractedStatement
    human = messages[-1]
    assert isinstance(human.content, str)  # TEXT-only message, no image blocks
    assert "Merchant 1" in human.content


def test_sparse_statement_routes_to_vision_path(app_env):
    from app.ingest.extract.statement import extract_statement

    raw = _make_pdf(["scan"])  # almost no text -> scanned/sparse path
    canned = ExtractedStatement(confidence=0.5)
    llm = _RecordingLLM({ExtractedStatement: canned})

    result = extract_statement(llm, raw)
    assert result.confidence == canned.confidence
    assert result.observed_page_count == 1
    assert result.extracted_page_count == 1
    assert result.extraction_truncated is False
    schema, messages = llm.calls[0]
    human = messages[-1]
    assert isinstance(human.content, list)
    blocks = [b.get("type") for b in human.content]
    assert blocks[0] == "text"
    assert "image_url" in blocks
    assert human.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_statement_extractor_persists_only_sanitized_last_four(app_env):
    from app.ingest.extract.statement import extract_statement

    raw = _make_pdf(["scan"])
    canned = ExtractedStatement(
        account_last4="account 0000 1111 2222 9003",
        confidence=0.5,
    )
    llm = _RecordingLLM({ExtractedStatement: canned})

    result = extract_statement(llm, raw)

    assert result.account_last4 == "9003"


# ---------------------------------------------------------------------------
# checksum_ok
# ---------------------------------------------------------------------------

def test_checksum_ok_true_when_balances_consistent():
    from app.ingest.extract.statement import checksum_ok

    st = ExtractedStatement(
        opening_balance_cents=1000, closing_balance_cents=500,
        rows=[StatementRow(amount_cents=-300), StatementRow(amount_cents=-200)],
    )
    assert checksum_ok(st) is True


def test_checksum_ok_false_when_balances_inconsistent():
    from app.ingest.extract.statement import checksum_ok

    st = ExtractedStatement(
        opening_balance_cents=1000, closing_balance_cents=999,
        rows=[StatementRow(amount_cents=-300), StatementRow(amount_cents=-200)],
    )
    assert checksum_ok(st) is False


def test_checksum_ok_true_when_balances_missing():
    from app.ingest.extract.statement import checksum_ok

    st = ExtractedStatement(rows=[StatementRow(amount_cents=-300)])
    assert checksum_ok(st) is True


def test_checksum_ok_true_for_debt_style_credit_card_signs():
    """Fix 6 regression: credit-card statements print the balance as debt, which RISES
    with spending even though our rows stay debit-negative (opening 1000.00 debt + 250.00
    of purchases -> closing 1250.00 debt). checksum_ok must accept this sign convention
    too, not just the asset-style opening+sum==closing one."""
    from app.ingest.extract.statement import checksum_ok

    st = ExtractedStatement(
        opening_balance_cents=100000, closing_balance_cents=125000,
        rows=[StatementRow(amount_cents=-25000)],
    )
    assert checksum_ok(st) is True


def test_digital_statement_text_truncated_at_30000_chars(app_env, monkeypatch):
    """Fix 7a regression: the digital-path statement text handed to the LLM is capped so a
    huge statement can't blow the context window; truncation is flagged so checksum_ok can
    catch any row loss it causes."""
    from app.ingest.extract import statement as statement_mod

    huge_text = "X" * 40000
    monkeypatch.setattr(statement_mod, "extract_text", lambda raw: huge_text)
    monkeypatch.setattr(statement_mod, "page_count", lambda raw: 1)

    canned = ExtractedStatement(confidence=0.5)
    llm = _RecordingLLM({ExtractedStatement: canned})

    result = statement_mod.extract_statement(llm, b"%PDF-fake%")
    assert result.confidence == canned.confidence
    assert result.observed_page_count == 1
    assert result.extracted_page_count == 1
    assert result.extraction_truncated is True
    schema, messages = llm.calls[0]
    human = messages[-1]
    assert isinstance(human.content, str)
    assert human.content.endswith("[TRUNCATED]")
    assert len(human.content) < len(huge_text)  # capped well below the original 40000 chars


# ---------------------------------------------------------------------------
# pipeline statement flow
# ---------------------------------------------------------------------------

_STATEMENT_MARKER_LINES = [
    "MONTHLY STATEMENT", "Account ending 9003", "Statement Period June 2026",
    "Opening balance   1,000.00", "Closing balance     950.00", "Minimum payment       25.00",
]


def _clean_statement(account_last4: str = "9003") -> ExtractedStatement:
    field_confidence = {
        field: 0.95
        for field in (
            "period_start_on",
            "period_end_on",
            "statement_issued_on",
            "opening_balance_cents",
            "closing_balance_cents",
            "currency",
            "account_fingerprint",
            "zero_activity",
        )
    }
    return ExtractedStatement(
        institution="Synthetic Bank", account_hint="Chequing", account_last4=account_last4,
        currency="CAD", statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=100000, closing_balance_cents=95000,
        declared_page_count=1,
        declared_row_count=2,
        field_confidence=field_confidence,
        field_pages={field: 1 for field in field_confidence},
        rows=[
            StatementRow(posted_on="2026-06-03", description="Loblaws #123",
                        amount_cents=-4200, balance_cents=95800, page_number=1,
                        field_confidence={"posted_on": 0.95, "description": 0.95,
                                          "amount_cents": 0.95}),
            StatementRow(posted_on="2026-06-10", description="Payroll Deposit",
                        amount_cents=-800, balance_cents=95000, page_number=1,
                        field_confidence={"posted_on": 0.95, "description": 0.95,
                                          "amount_cents": 0.95}),
        ],
        confidence=0.9,
    )


def test_pipeline_stages_statement_and_enqueues_reconcile(app_env):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET external_ref=? WHERE id=?", ("Synthetic Bank:chequing:9003", 1))

    raw = _make_pdf(_STATEMENT_MARKER_LINES)
    cap = capture(raw=raw, original_name="statement.pdf", channel="web")
    assert cap["kind"] == "statement"

    statement = _clean_statement()
    llm = _RecordingLLM({ExtractedStatement: statement})

    res = process_document(app_env, cap["source_document_id"], llm)
    assert res["status"] == "staged"
    assert res["lines"] == 2
    assert res["duplicates"] == 0
    assert res["reconcile_job"]

    with engine.read_conn(app_env) as conn:
        lines = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=? ORDER BY posted_on",
            (cap["source_document_id"],),
        ).fetchall()
        assert len(lines) == 2
        for line, row in zip(lines, statement.rows):
            assert line["amount_cents"] == row.amount_cents  # signed cents preserved
            assert line["account_id"] == 1
            assert line["row_hash"]  # computed
            assert line["match_status"] == "unmatched"

        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "processed"

        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()
        assert job is not None

    # Restaging/re-export reproduces identical row_hashes (occ = in-batch ordinal),
    # so UNIQUE(account_id, row_hash) collapses every line. No-double-count invariant.
    res2 = process_document(app_env, cap["source_document_id"], llm)
    assert res2["status"] == "staged"
    assert res2["lines"] == 0  # restage reproduces identical row_hashes -> all dupes
    assert res2["duplicates"] == 2

    with engine.read_conn(app_env) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()[0]
        assert n == 2  # restage is a no-op; line count unchanged


def test_pipeline_accepts_explicit_zero_activity_by_balance_proof(app_env):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE accounts SET external_ref=? WHERE id=?",
            ("Synthetic Bank:chequing:9003", 1),
        )

    parsed = _clean_statement().model_copy(
        update={
            "opening_balance_cents": 100000,
            "closing_balance_cents": 100000,
            "declared_row_count": 0,
            "zero_activity": True,
            "rows": [],
        }
    )
    cap = capture(
        raw=_make_pdf(_STATEMENT_MARKER_LINES),
        original_name="zero-activity-statement.pdf",
        channel="web",
    )

    result = process_document(
        app_env,
        cap["source_document_id"],
        _RecordingLLM({ExtractedStatement: parsed}),
    )

    assert result == {
        "status": "staged",
        "lines": 0,
        "duplicates": 0,
        "reconcile_job": None,
    }
    with engine.read_conn(app_env) as conn:
        review = conn.execute(
            """SELECT * FROM statement_reviews
               WHERE source_document_id=?""",
            (cap["source_document_id"],),
        ).fetchone()
        assert review["review_state"] == "approved"
        link = conn.execute(
            """SELECT expectation.lifecycle_state
               FROM statement_expectation_documents link
               JOIN account_statement_expectations expectation
                 ON expectation.id=link.expectation_id
               WHERE link.source_document_id=? AND link.status='active'""",
            (cap["source_document_id"],),
        ).fetchone()
        assert link is not None and link["lifecycle_state"] == "reconciled"


def test_resolve_account_sanitizes_last4_and_rejects_wildcard_junk(app_env):
    """Fix 7b regression: account_last4 is LLM output dropped into a SQL LIKE pattern.
    Non-digit noise (including literal '%'/'_' wildcards) must be stripped and the result
    must be exactly 4 digits, or we refuse to guess rather than let a wildcard reach the
    LIKE pattern."""
    from app.db.repo_statements import match_account_by_last4

    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET external_ref=? WHERE id=?", ("Synthetic Bank:chequing:9003", 1))

        # Stripping '%' leaves only 3 digits ("903") -> refuse to guess.
        bad = ExtractedStatement(account_last4="90%3")
        assert match_account_by_last4(conn, bad) is None

        # Legitimately formatted last4 (dashes) still resolves after sanitizing.
        good = ExtractedStatement(account_last4="90-03")
        assert match_account_by_last4(conn, good) == 1


def test_account_unresolved_routes_to_review_without_reconcile(app_env):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    # No account has external_ref set -> last4 can never resolve.
    raw = _make_pdf(_STATEMENT_MARKER_LINES)
    cap = capture(raw=raw, original_name="statement2.pdf", channel="web")
    assert cap["kind"] == "statement"

    statement = _clean_statement(account_last4="9999")
    llm = _RecordingLLM({ExtractedStatement: statement})

    res = process_document(app_env, cap["source_document_id"], llm)
    assert res["status"] == "needs_review"
    assert res["reason"] == "account_unresolved"

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "needs_review"
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()
        assert job is None


def test_duplicate_last4_accounts_stay_in_review_and_unattached(app_env):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE accounts SET external_ref='account:9003' WHERE id=1"
        )
        conn.execute(
            "UPDATE accounts SET external_ref='other:9003' WHERE id=2"
        )

    cap = capture(
        raw=_make_pdf(_STATEMENT_MARKER_LINES),
        original_name="ambiguous-last4.pdf",
        channel="web",
    )
    statement = _clean_statement(account_last4="9003")
    result = process_document(
        app_env,
        cap["source_document_id"],
        _RecordingLLM({ExtractedStatement: statement}),
    )

    assert result["status"] == "needs_review"
    assert result["reason"] == "account_unresolved"
    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_expectation_documents
               WHERE source_document_id=? AND status='active'""",
            (cap["source_document_id"],),
        ).fetchone()[0] == 0
        assert {
            row["account_id"]
            for row in conn.execute(
                """SELECT account_id FROM statement_lines
                   WHERE source_document_id=?""",
                (cap["source_document_id"],),
            )
        } == {None}


def test_checksum_mismatch_routes_to_review_without_reconcile(app_env):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET external_ref=? WHERE id=?", ("Synthetic Bank:chequing:9003", 1))

    raw = _make_pdf(_STATEMENT_MARKER_LINES)
    cap = capture(raw=raw, original_name="statement3.pdf", channel="web")
    assert cap["kind"] == "statement"

    statement = _clean_statement()
    statement.closing_balance_cents = 1  # now inconsistent with opening + sum(rows)
    llm = _RecordingLLM({ExtractedStatement: statement})

    res = process_document(app_env, cap["source_document_id"], llm)
    assert res["status"] == "needs_review"
    assert res["reason"] == "balance_checksum_mismatch"

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "needs_review"
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()
        assert job is None
        # lines still staged for later manual reconciliation
        n = conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()[0]
        assert n == 2
