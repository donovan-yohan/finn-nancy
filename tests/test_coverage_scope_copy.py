"""Truthful coverage copy through generated-only reconciliation GETs."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, repo_ledger, repo_recon_coverage, repo_statements
from app.ingest.schemas import ExtractedStatement, StatementRow
from tests.test_statement_expectations import _required_policy


MONTH = "2026-06"


def seed_scope_case(db: str, variant: str, *, month: str = MONTH) -> dict:
    """Build real staged evidence; enroll only explicitly named control cases."""
    with engine.write_tx(db) as conn:
        account = conn.execute(
            "INSERT INTO accounts(name,institution,kind,currency) "
            "VALUES ('Generated scope card','Generated bank','credit','CAD')"
        ).lastrowid
        _required_policy(conn, account)
        if variant == "empty":
            return {"account": account}
        amounts = {
            "staged": [-16243], "matched": [-16243], "unmatched": [-16243],
            "mixed": [-16243, -4000, 2500, -500], "credit": [2500],
            "ignored": [-500],
        }[variant]
        doc = conn.execute(
            "INSERT INTO source_documents(kind,original_name,storage_ref,sha256,mime_type,status) "
            "VALUES ('statement',?,?,?,'text/csv','needs_review')",
            (f"Generated {variant}.csv", f"generated/{variant}.csv",
             hashlib.sha256(variant.encode()).hexdigest()),
        ).lastrowid
        parsed = ExtractedStatement(
            institution="Generated bank", currency="CAD", statement_period=month,
            confidence=1.0,
            rows=[StatementRow(posted_on=f"{month}-{20 + i}",
                               description=f"Generated {variant} line {i + 1}",
                               amount_cents=amount, is_pending=False)
                  for i, amount in enumerate(amounts)],
        )
        repo_statements.stage_lines(conn, source_document_id=doc, account_id=account,
                                    parsed=parsed)
        lines = repo_statements.lines_for_document(conn, doc)
        for index, line in enumerate(lines):
            status = "needs_review"
            transaction = None
            if index == 0 and variant in {"staged", "matched", "mixed"}:
                category = repo_ledger.ensure_uncategorized(conn)
                transaction = repo_ledger.insert_transaction(
                    conn, account_id=account, posted_on=line["posted_on"],
                    description=line["raw_description"], counterparty="Generated market",
                    amount_cents=line["amount_cents"], source="manual",
                    external_id=f"scope-{variant}", source_document_id=None,
                    source_confidence=1.0, flow_kind="purchase",
                )
                assert transaction is not None
                repo_ledger.insert_split(conn, transaction_id=transaction,
                                         category_id=category, amount_cents=line["amount_cents"])
                if variant != "staged":
                    status = "matched"
                    repo_statements.mark_cleared(conn, transaction, line["posted_on"])
            if variant == "ignored" or (variant == "mixed" and index == 3):
                status = "ignored"
            repo_statements.set_match(
                conn, line["id"], status=status,
                transaction_id=transaction if status == "matched" else None,
                method="manual" if status in {"matched", "ignored"} else "",
            )
        if variant != "staged":
            from app.db import repo_statement_expectations
            expectation, attached = repo_statement_expectations.attach_exact_document(
                conn, doc, actor="test:scope", reason="Generated enrolled coverage control",
            )
            assert attached and expectation is not None
        return {"account": account, "document": doc, "line": lines[0]["id"]}


@pytest.fixture
def scope_app(empty_db, tmp_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name, value in {
        "DB_PATH": empty_db, "DATA_DIR": str(tmp_path / "data"),
        "STRICT_LOCAL_MODE": "true", "READ_ONLY": "false", "API_TOKEN": "",
        "PLAID_ENABLED": "false", "EMBEDDINGS_ENABLED": "false",
        "HOME_CURRENCY": "CAD",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    from app.web.app import create_app
    client = TestClient(create_app())  # Do not enter lifespan or start workers.
    try:
        yield client, empty_db
    finally:
        client.close()
        get_settings.cache_clear()


def coverage_section(html: str) -> str:
    return re.search(r'<section[^>]*>\s*<div class="evidence-head">\s*<div>\s*'
                     r'(?:<p[^>]*>.*?</p>\s*)?<h2>Matched vs unmatched spend</h2>'
                     r'.*?</section>', html, re.S)[0]


def query_only_get(client, db, monkeypatch, route=f"/recon?month={MONTH}"):
    with engine.read_conn(db) as conn:
        before = "\n".join(conn.iterdump())
    original = engine.connect

    def query_only(path, *, read_only=False):
        assert Path(path).resolve() == Path(db).resolve()
        conn = original(path, read_only=True)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        return conn

    with monkeypatch.context() as guard:
        guard.setattr(engine, "connect", query_only)
        response = client.get(route)
    assert response.status_code == 200
    with engine.read_conn(db) as conn:
        assert "\n".join(conn.iterdump()) == before
    directory = os.getenv("COVERAGE_SCOPE_ARTIFACT_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / (route.replace("/", "_").replace("?", "_") + ".html")).write_text(response.text)
    return response


def test_staged_matching_outside_expected_coverage_is_not_claimed_absent(scope_app, monkeypatch):
    client, db = scope_app
    ids = seed_scope_case(db, "staged")
    response = query_only_get(client, db, monkeypatch)
    section = coverage_section(response.text)
    # The actual candidate and native POST authority must remain present.
    assert "Generated staged line 1" in response.text
    assert f'action="/recon/line/{ids["line"]}/confirm"' in response.text
    assert "Confirm match" in response.text
    assert "$162.43" in response.text
    with engine.read_conn(db) as conn:
        assert repo_recon_coverage.coverage_dashboard(conn, month=MONTH)["summary"]["line_count"] == 0
        assert conn.execute("SELECT COUNT(*) FROM statement_expectation_documents").fetchone()[0] == 0
        assert conn.execute("SELECT match_status FROM statement_lines WHERE id=?", (ids["line"],)).fetchone()[0] == "needs_review"
    assert f"Expected-statement coverage · {MONTH}" in section
    assert f"No statement lines in expected-statement coverage for {MONTH}." in section
    assert "Staged lines awaiting a match can exist outside this coverage." in section
    assert "No eligible spend" in section
    assert "0.0% covered" not in section
    assert "No statement lines yet." not in response.text


@pytest.mark.parametrize("variant", ["credit", "ignored"])
def test_nonempty_zero_denominator_never_looks_like_measured_zero(scope_app, monkeypatch, variant):
    client, db = scope_app
    seed_scope_case(db, variant)
    response = query_only_get(client, db, monkeypatch)
    section = coverage_section(response.text)
    with engine.read_conn(db) as conn:
        summary = repo_recon_coverage.coverage_dashboard(conn, month=MONTH)["summary"]
    assert summary["line_count"] == 1
    assert summary["covered_spend_cents"] + summary["unmatched_spend_cents"] == 0
    assert summary["coverage_pct"] == 0.0  # Backend contract is deliberately unchanged.
    assert "No eligible spend" in section
    assert "1 line" in section
    assert "No statement lines" not in section
    # Every document and group cell must distinguish an absent spend denominator.
    assert "0.0%" not in section
    if variant == "credit":
        assert summary["income_cents"] == 0
        assert summary["positive_review_cents"] == 2500
        assert "$25.00" in section
        assert "excluded from income until reviewed" in section
        assert "Money in to explain" in response.text
        assert f"/review/statement/" in response.text
    else:
        assert summary["statement_spend_cents"] == 500
        assert summary["ignored_spend_cents"] == 500
        assert "$5.00" in section
        assert "transfers or exclusions" in section


@pytest.mark.parametrize("variant,pct,count,spend,covered,unmatched", [
    ("matched", "100.0", 1, 16243, 16243, 0),
    ("unmatched", "0.0", 1, 16243, 0, 16243),
    ("mixed", "80.2", 4, 20743, 16243, 4000),
])
def test_real_spend_denominator_keeps_measured_coverage(scope_app, monkeypatch, variant,
                                                      pct, count, spend, covered, unmatched):
    client, db = scope_app
    seed_scope_case(db, variant)
    response = query_only_get(client, db, monkeypatch)
    section = coverage_section(response.text)
    with engine.read_conn(db) as conn:
        summary = repo_recon_coverage.coverage_dashboard(conn, month=MONTH)["summary"]
    assert (summary["line_count"], summary["statement_spend_cents"],
            summary["covered_spend_cents"], summary["unmatched_spend_cents"]) == (
                count, spend, covered, unmatched)
    assert f"{pct}% covered" in section
    assert f"{count} line" in section
    assert f"Expected-statement coverage · {MONTH}" in section
    assert "No statement lines" not in section
    assert 'href="/close?month=2026-06"' in response.text
    assert 'href="/statements?month=2026-06"' in response.text
    if variant == "mixed":
        # Overall spend is measured, while the credit and ignored categories are not.
        assert "No eligible spend" in section
        assert summary["income_cents"] == 0
        assert summary["positive_review_cents"] == 2500
        assert summary["ignored_spend_cents"] == 500
        for amount in ["$207.43", "$162.43", "$40.00", "$25.00", "$5.00"]:
            assert amount in section


@pytest.mark.parametrize("route,scoped", [(f"/recon?month={MONTH}", True), ("/recon", False)])
def test_genuinely_empty_population_names_only_its_actual_scope(scope_app, monkeypatch, route, scoped):
    client, db = scope_app
    response = query_only_get(client, db, monkeypatch, route)
    section = coverage_section(response.text)
    assert "No eligible spend" in section
    assert "0.0%" not in section
    assert "all statements" not in section.lower()
    if scoped:
        assert f"No statement lines in expected-statement coverage for {MONTH}." in section
    else:
        assert "Statement coverage · no month filter" in section
        assert "No expected-statement month scope is applied." in section
        assert "No statement lines in this coverage." in section
        assert "Only statement documents actively linked" not in section


def test_unscoped_renderer_does_not_claim_expectation_enrollment(scope_app, monkeypatch):
    from app.web.templating import templates
    client, db = scope_app
    seed_scope_case(db, "staged")
    response = query_only_get(client, db, monkeypatch)
    with engine.read_conn(db) as conn:
        unscoped = repo_recon_coverage.coverage_dashboard(conn, month=None)
    assert unscoped["summary"]["line_count"] == 1
    html = templates.get_template("recon_review.html").render(
        **{**response.context, "selected_month": None, "coverage": unscoped})
    section = coverage_section(html)
    assert "Statement coverage · no month filter" in section
    assert "No expected-statement month scope is applied." in section
    assert "Only statement documents actively linked" not in section
    assert "Expected-statement coverage ·" not in section
    assert "0.0% covered" in section
    assert "No eligible spend" not in section
    assert "Generated staged line 1" in section
