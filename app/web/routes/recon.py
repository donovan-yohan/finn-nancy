"""Reconciliation review UI: confirm/promote/ignore staged statement lines, resolve
account-less statement documents, and manage already-reconciled statement documents.

The apply-side mutators (confirm_match/promote_line/ignore_line/unreconcile_document) are
owned by a parallel builder at app.reconcile.apply; import is best-effort so this module
still loads (and its own logic stays testable) before that file lands.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from rapidfuzz import fuzz

from ...accounting.contract import FlowKind
from ...config import get_settings
from ...db import (
    engine,
    repo_actions,
    repo_budgets,
    repo_close,
    repo_documents,
    repo_jobs,
    repo_period_policy,
    repo_recon_coverage,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from ...ingest.normalize import norm_merchant
from ...reconcile import adjust, positive_flows
from ..templating import templates

try:
    from ...reconcile import apply
except ImportError:  # pragma: no cover — see module docstring
    apply = None  # tests monkeypatch this attribute with a fake exposing the same functions

router = APIRouter()


def _candidates(conn: sqlite3.Connection, line: sqlite3.Row) -> list[dict]:
    """Uncleared txns on the line's account with equal amount within -7/+1 days of posted_on."""
    rows = conn.execute(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE t.recon_status='uncleared' AND t.amount_cents=? AND t.account_id IS ?
             AND t.posted_on BETWEEN date(?, '-7 days') AND date(?, '+1 days')
           ORDER BY t.posted_on""",
        (line["amount_cents"], line["account_id"], line["posted_on"], line["posted_on"]),
    ).fetchall()
    out = []
    for t in rows:
        score = fuzz.token_set_ratio(line["norm_merchant"], norm_merchant(t["description"]))
        out.append({"txn": t, "score": round(score)})
    out.sort(key=lambda c: -c["score"])
    return out


def _review_items(conn: sqlite3.Connection, month: str) -> list[dict]:
    return [{"line": line, "candidates": _candidates(conn, line)}
            for line in repo_statements.review_queue(conn, month=month)]


def _unresolved_docs(
    conn: sqlite3.Connection, month: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT DISTINCT sd.* FROM source_documents sd
           JOIN statement_lines sl ON sl.source_document_id = sd.id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=sd.id AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE sd.kind='statement' AND sd.status='needs_review' AND sl.account_id IS NULL
             AND sl.review_disposition='active'
             AND COALESCE(expectation.period_month, sl.statement_period)=?
           ORDER BY sd.created_at DESC, sd.id DESC"""
        ,
        (month,),
    ).fetchall()


def _doc_summaries(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Statement docs with line counts grouped by match_status."""
    rows = conn.execute(
        """SELECT sd.id, sd.original_name, sd.status, sl.match_status, COUNT(*) AS n
           FROM source_documents sd JOIN statement_lines sl ON sl.source_document_id = sd.id
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=sd.id AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE sd.kind='statement'
             AND sl.review_disposition='active'
             AND COALESCE(expectation.period_month, sl.statement_period)=?
           GROUP BY sd.id, sl.match_status
           ORDER BY sd.created_at DESC, sd.id DESC""",
        (month,),
    ).fetchall()
    docs: dict[int, dict] = {}
    for r in rows:
        entry = docs.setdefault(
            r["id"], {"id": r["id"], "original_name": r["original_name"], "status": r["status"], "counts": {}}
        )
        entry["counts"][r["match_status"]] = r["n"]
    return list(docs.values())


def _maybe_finalize_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    """Flip a statement doc to 'matched' once its queue of needs_review lines empties."""
    remaining = conn.execute(
        """SELECT COUNT(*) FROM statement_lines
           WHERE source_document_id=? AND match_status='needs_review'
             AND review_disposition='active'""",
        (doc_id,),
    ).fetchone()[0]
    if remaining == 0:
        repo_documents.set_status(conn, doc_id, "matched")
    repo_statement_expectations.sync_document_reconciliation(
        conn,
        doc_id,
        actor="reconcile:operator",
        reason="reconciliation line disposition changed",
    )


def _line_doc_id(
    conn: sqlite3.Connection, line_id: int, month: str | None = None
) -> int:
    row = conn.execute(
        """SELECT line.source_document_id,
                  COALESCE(expectation.period_month, line.statement_period) AS period_month
           FROM statement_lines line
           LEFT JOIN statement_expectation_documents link
             ON link.source_document_id=line.source_document_id
            AND link.status='active'
           LEFT JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE line.id=? AND line.review_disposition='active'""",
        (line_id,),
    ).fetchone()
    if row is None or (month and row["period_month"] != month):
        raise HTTPException(404, "statement line not found")
    return int(row["source_document_id"])


def _require_doc(
    conn: sqlite3.Connection, doc_id: int, month: str | None = None
) -> sqlite3.Row:
    doc = repo_documents.get_document(conn, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if month:
        scoped = conn.execute(
            """SELECT 1
               FROM statement_lines line
               LEFT JOIN statement_expectation_documents link
                 ON link.source_document_id=line.source_document_id
                AND link.status='active'
               LEFT JOIN account_statement_expectations expectation
                 ON expectation.id=link.expectation_id
               WHERE line.source_document_id=?
                 AND line.review_disposition='active'
                 AND COALESCE(expectation.period_month, line.statement_period)=?
               LIMIT 1""",
            (doc_id, month),
        ).fetchone()
        if scoped is None:
            raise HTTPException(404, "document not found for selected month")
    return doc


def _selected_month(
    conn: sqlite3.Connection, raw_month: str | None
) -> tuple[str, list[str]]:
    try:
        selected = (
            repo_statement_expectations.normalize_month(raw_month)
            if raw_month
            else None
        )
    except ValueError:
        selected = None
    months = repo_statement_expectations.reconciliation_months(conn)
    if selected is not None and selected not in months:
        months.append(selected)
        months.sort(reverse=True)
    if selected is None:
        selected = months[0] if months else ""
    return selected, months


def _recon_redirect(month: str, notice: str = "") -> RedirectResponse:
    try:
        selected = repo_statement_expectations.normalize_month(month)
    except ValueError:
        return RedirectResponse("/recon", status_code=303)
    query = {"month": selected}
    if notice:
        query["notice"] = notice
    return RedirectResponse(f"/recon?{urlencode(query)}", status_code=303)


def _required_form_month(raw_month: str) -> str:
    try:
        return repo_statement_expectations.normalize_month(raw_month)
    except ValueError as exc:
        raise HTTPException(400, "month must be YYYY-MM") from exc


def _required_form_text(value: str, *, field: str, maximum: int) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(400, f"{field} is required")
    if len(text) > maximum:
        raise HTTPException(400, f"{field} exceeds {maximum} characters")
    return text


@router.get("/recon", response_class=HTMLResponse)
def recon_review(
    request: Request,
    month: str | None = None,
    notice: str | None = None,
):
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        selected_month, months = _selected_month(conn, month)
        review_items = _review_items(conn, selected_month) if selected_month else []
        unresolved_docs = (
            _unresolved_docs(conn, selected_month) if selected_month else []
        )
        accounts = conn.execute("SELECT * FROM accounts WHERE is_active=1 ORDER BY name").fetchall()
        doc_summaries = (
            _doc_summaries(conn, selected_month) if selected_month else []
        )
        coverage = repo_recon_coverage.coverage_dashboard(
            conn, month=selected_month or None
        )
        review_line_ids = {int(item["line"]["id"]) for item in review_items}
        scoped_document_ids = {
            int(row["source_document_id"])
            for row in conn.execute(
                """SELECT link.source_document_id
                   FROM statement_expectation_documents link
                   JOIN account_statement_expectations expectation
                     ON expectation.id=link.expectation_id
                   WHERE link.status='active'
                     AND expectation.period_month=?""",
                (selected_month,),
            ).fetchall()
        } if selected_month else set()
        planning_evidence_lines = [
            line
            for line in repo_budgets.planning_statement_evidence_lines(conn, selected_month)
            if int(line["line_id"]) not in review_line_ids
            and int(line["source_document_id"]) in scoped_document_ids
        ] if selected_month else []
        active_proposal_count = len(repo_actions.list_proposals(conn))
        positive_flow_reviews = (
            positive_flows.list_positive_flow_reviews(
                conn,
                selected_month,
                include_suppressed=True,
            )
            if selected_month
            else []
        )
        positive_flow_intake = (
            positive_flows.list_positive_flow_intake(conn, selected_month)
            if selected_month
            else []
        )
    return templates.TemplateResponse(
        request,
        "recon_review.html",
        {
            "review_items": review_items,
            "unresolved_docs": unresolved_docs,
            "accounts": accounts,
            "doc_summaries": doc_summaries,
            "coverage": coverage,
            "months": months,
            "selected_month": selected_month,
            "planning_evidence_lines": planning_evidence_lines,
            "active_proposal_count": active_proposal_count,
            "positive_flow_reviews": positive_flow_reviews,
            "positive_flow_intake": positive_flow_intake,
            "notice": notice,
            "flow_kinds": list(FlowKind),
            "active": "more",
            "brand": "finn",
        },
    )


@router.post("/recon/line/{line_id}/confirm")
def confirm_line(
    line_id: int,
    transaction_id: int = Form(...),
    month: str = Form(...),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        doc_id = _line_doc_id(conn, line_id, selected_month)
        try:
            apply.confirm_match(conn, line_id, transaction_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _maybe_finalize_doc(conn, doc_id)
    return _recon_redirect(selected_month)


@router.post("/recon/line/{line_id}/promote")
def promote_line_route(
    line_id: int,
    flow_kind: str = Form(FlowKind.UNKNOWN.value),
    month: str = Form(...),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        doc_id = _line_doc_id(conn, line_id, selected_month)
        try:
            line = conn.execute(
                "SELECT amount_cents FROM statement_lines WHERE id=?",
                (line_id,),
            ).fetchone()
            if (
                line is not None
                and int(line["amount_cents"]) > 0
                and flow_kind != FlowKind.UNKNOWN.value
            ):
                raise ValueError(
                    "positive statement credits must be created as unknown, "
                    "then classified in Money in to explain"
                )
            repo_statements.set_flow_kind(conn, line_id, flow_kind)
            apply.promote_line(conn, line_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _maybe_finalize_doc(conn, doc_id)
    return _recon_redirect(selected_month)


@router.post("/recon/line/{line_id}/ignore")
def ignore_line_route(line_id: int, month: str = Form(...)):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        doc_id = _line_doc_id(conn, line_id, selected_month)
        line = conn.execute(
            "SELECT amount_cents FROM statement_lines WHERE id=?",
            (line_id,),
        ).fetchone()
        if line is not None and int(line["amount_cents"]) > 0:
            raise HTTPException(
                400,
                "positive statement credits cannot be ignored; create the "
                "transaction and explain its flow",
            )
        apply.ignore_line(conn, line_id)
        _maybe_finalize_doc(conn, doc_id)
    return _recon_redirect(selected_month)


def _positive_flow_failure(month: str, exc: ValueError) -> RedirectResponse:
    return _recon_redirect(month, f"No changes made: {exc}")


@router.post("/recon/positive/line/{line_id}/recover")
def recover_positive_flow_line(
    line_id: int,
    month: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator moved positive row into flow review"),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.recover_positive_line(
                conn,
                statement_line_id=line_id,
                month=selected_month,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/positive/{subject_transaction_id}/reject")
def reject_positive_flow_proposal(
    subject_transaction_id: int,
    month: str = Form(...),
    proposal_key: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator rejected this proposed match"),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.reject_proposal(
                conn,
                subject_transaction_id=subject_transaction_id,
                month=selected_month,
                proposal_key=proposal_key,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/positive/{subject_transaction_id}/restore")
def restore_positive_flow_proposal(
    subject_transaction_id: int,
    month: str = Form(...),
    proposal_key: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator restored this proposed match"),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.restore_proposal(
                conn,
                subject_transaction_id=subject_transaction_id,
                month=selected_month,
                proposal_key=proposal_key,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/positive/{subject_transaction_id}/accept-classification")
def accept_positive_flow_classification(
    subject_transaction_id: int,
    month: str = Form(...),
    flow_kind: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator confirmed positive-flow classification"),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.accept_classification(
                conn,
                subject_transaction_id=subject_transaction_id,
                month=selected_month,
                flow_kind=flow_kind,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/positive/{subject_transaction_id}/accept-pair")
def accept_positive_flow_pair(
    subject_transaction_id: int,
    month: str = Form(...),
    proposal_key: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator confirmed positive-flow pair"),
    selected_category_id: int | None = Form(None),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject_transaction_id,
                month=selected_month,
                proposal_key=proposal_key,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
                selected_category_id=selected_category_id,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/positive/acceptance/{accept_event_id}/undo")
def undo_positive_flow_acceptance(
    accept_event_id: int,
    month: str = Form(...),
    evidence_fingerprint: str = Form(...),
    operation_key: str = Form(...),
    reason: str = Form("operator undid positive-flow decision"),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    try:
        with engine.write_tx(settings.db_path) as conn:
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=accept_event_id,
                month=selected_month,
                evidence_fingerprint=evidence_fingerprint,
                operation_key=operation_key,
                actor="reconcile:operator",
                reason=reason,
            )
    except ValueError as exc:
        return _positive_flow_failure(selected_month, exc)
    return _recon_redirect(selected_month)


@router.post("/recon/doc/{doc_id}/assign-account")
def assign_account(
    doc_id: int,
    account_id: int = Form(...),
    month: str = Form(...),
):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _require_doc(conn, doc_id, selected_month)
        try:
            review = repo_statement_reviews.get_for_document(conn, doc_id)
            if review is None:
                raise ValueError(
                    "statement has no canonical review envelope; re-ingest it"
                )
            page = conn.execute(
                """SELECT page_number FROM statement_review_pages
                   WHERE statement_review_id=?
                   ORDER BY page_number LIMIT 1""",
                (int(review["id"]),),
            ).fetchone()
            if page is None:
                raise ValueError(
                    "statement has no source page evidence; re-ingest it"
                )
            repo_statement_reviews.update_metadata(
                conn,
                int(review["id"]),
                expected_revision=int(review["revision"]),
                actor="reconcile:operator",
                reason="operator confirmed statement account identity",
                source_page=int(page["page_number"]),
                values={"account_id": account_id},
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(
        f"/review/statement/{doc_id}", status_code=303
    )


@router.post("/recon/doc/{doc_id}/unreconcile")
def unreconcile_doc(doc_id: int, month: str = Form(...)):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _require_doc(conn, doc_id, selected_month)
        try:
            apply.unreconcile_document(conn, doc_id)
        except repo_close.MonthLockedError as exc:
            raise HTTPException(409, str(exc)) from exc
    return _recon_redirect(selected_month)


def _close_notice(month: str, notice: str) -> RedirectResponse:
    """POST-redirect-GET back to the Close Inbox carrying an inline notice for /close."""
    return RedirectResponse(
        f"/close?{urlencode({'month': month, 'notice': notice})}", status_code=303
    )


@router.post("/recon/assertion/adjust")
def adjust_assertion(
    account_id: int = Form(...),
    asof_date: str = Form(...),
    override: str | None = Form(None),
    actor: str = Form(""),
    reason: str = Form(""),
    confirm_adjustment: str | None = Form(None),
    operation_key: str = Form(""),
):
    """FN-107: one-click forced reconciliation adjustment for a balance-assertion
    exception. Books (or re-books, when later drift reopened it) a tagged, reversible
    adjustment that zeroes the delta and audits it. A closed-month override requires an
    explicit actor, reason, confirmation, and stable operation key; the shared period
    policy then reopens the month and records the override before mutation. Returns to
    the Close Inbox on /close for its month; a lock refusal or a residual failure comes
    back as an inline notice instead of a raw 400 or a silent success redirect."""
    settings = get_settings()
    month = asof_date[:7]
    audited_override = override == "1"
    if audited_override:
        actor = _required_form_text(actor, field="actor", maximum=160)
        reason = _required_form_text(reason, field="reason", maximum=500)
        operation_key = _required_form_text(
            operation_key,
            field="operation key",
            maximum=180,
        )
        if confirm_adjustment != "1":
            raise HTTPException(400, "adjustment confirmation is required")
    with engine.write_tx(settings.db_path) as conn:
        try:
            result = adjust.create_adjustment(
                conn, account_id=account_id, asof_date=asof_date,
                override=audited_override,
                actor=actor,
                override_reason=reason,
                operation_key=operation_key,
            )
        except repo_close.MonthLockedError as exc:
            # A locked month is a friendly, actionable message, not a raw 400 that
            # dead-ends the user.
            return _close_notice(
                month,
                f"{exc.month} is closed — use the audited override on Close to adjust.",
            )
        except (
            repo_period_policy.PeriodOperationConflict,
            repo_period_policy.PeriodTransitionError,
        ) as exc:
            raise HTTPException(409, str(exc)) from exc
        except repo_period_policy.PeriodPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc
    if result.status == "no_assertion":
        raise HTTPException(404, "no balance assertion for that account and date")
    if result.status == "exists":
        # The adjustment could not be (re)booked, so the exception is still open. Surface
        # it rather than 303'ing as though it succeeded.
        return _close_notice(
            month,
            "Could not book the reconciliation adjustment — the exception is still open. "
            "Please try again.",
        )
    return RedirectResponse(f"/close?month={month}", status_code=303)


@router.post("/recon/doc/{doc_id}/rerun")
def rerun_doc(doc_id: int, month: str = Form(...)):
    selected_month = _required_form_month(month)
    settings = get_settings()
    with engine.write_tx(settings.db_path) as conn:
        _require_doc(conn, doc_id, selected_month)
        repo_jobs.enqueue(conn, "reconcile_document", {"source_document_id": doc_id},
                          source_document_id=doc_id)
    return _recon_redirect(selected_month)
