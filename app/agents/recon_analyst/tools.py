"""Typed read-only tools over reconciliation and planning views."""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.repo_budgets import (
    planning_insight_cards,
    recurring_delta_rows,
    subscription_watchlist_rows,
)

from .schemas import CoverageSummary, StatementRun, _coerce_int_list


class IdAllowlist(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    transaction_ids: set[int] = Field(default_factory=set)
    statement_line_ids: set[int] = Field(default_factory=set)
    category_ids: set[int] = Field(default_factory=set)
    account_ids: set[int] = Field(default_factory=set)
    merchants: set[str] = Field(default_factory=set)

    def merge(self, other: "IdAllowlist") -> "IdAllowlist":
        return IdAllowlist(
            transaction_ids=self.transaction_ids | other.transaction_ids,
            statement_line_ids=self.statement_line_ids | other.statement_line_ids,
            category_ids=self.category_ids | other.category_ids,
            account_ids=self.account_ids | other.account_ids,
            merchants=self.merchants | other.merchants,
        )

    def as_prompt_dict(self) -> dict[str, list[int] | list[str]]:
        return {
            "transaction_ids": sorted(self.transaction_ids),
            "statement_line_ids": sorted(self.statement_line_ids),
            "category_ids": sorted(self.category_ids),
            "account_ids": sorted(self.account_ids),
            "merchants": sorted(self.merchants),
        }


class StatementLineEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: int
    source_document_id: int
    document_name: str | None = None
    account_id: int | None = None
    account_name: str | None = None
    posted_on: str
    month: str
    raw_description: str
    norm_merchant: str
    amount_cents: int
    spend_cents: int
    income_cents: int
    match_status: str
    matched_transaction_id: int | None = None
    match_method: str = ""
    match_score: float = 0.0
    match_rationale: str = ""
    coverage_bucket: str
    category_names: str = ""
    attention_reason: str = ""


class StatementAuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: CoverageSummary
    lines: list[StatementLineEvidence] = Field(default_factory=list)
    attention_lines: list[StatementLineEvidence] = Field(default_factory=list)
    suspicious_matches: list[StatementLineEvidence] = Field(default_factory=list)
    allowed_ids: IdAllowlist = Field(default_factory=IdAllowlist)


class ReceiptCandidateLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: StatementLineEvidence
    candidate_transaction_ids: list[int] = Field(default_factory=list)


class ReceiptMatcherResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unmatched_lines: list[ReceiptCandidateLine] = Field(default_factory=list)
    allowed_ids: IdAllowlist = Field(default_factory=IdAllowlist)


class RecurringChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant: str
    account_id: int
    account_name: str
    month: str
    previous_month: str
    current_amount_cents: int
    previous_amount_cents: int
    amount_delta_cents: int
    pct_change: float | None = None
    direction: str
    current_transaction_ids: list[int] = Field(default_factory=list)
    previous_transaction_ids: list[int] = Field(default_factory=list)
    current_statement_line_ids: list[int] = Field(default_factory=list)
    previous_statement_line_ids: list[int] = Field(default_factory=list)
    category_names: str = ""


class RecurringAnalystResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[RecurringChange] = Field(default_factory=list)
    allowed_ids: IdAllowlist = Field(default_factory=IdAllowlist)


class SubscriptionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant: str
    account_id: int
    account_name: str
    candidate_type: str
    first_month: str
    last_month: str
    first_seen_on: str
    last_seen_on: str
    expected_next_charge_on: str
    estimated_amount_cents: int
    transaction_ids: list[int] = Field(default_factory=list)
    statement_line_ids: list[int] = Field(default_factory=list)
    category_names: str = ""
    decision_label: str = ""


class PlanningCardEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    card_key: str
    title: str
    body: str
    reason_code: str
    confidence_label: str
    suggested_action: str
    current_action: str | None = None
    transaction_ids: list[int] = Field(default_factory=list)
    statement_line_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)


class PlanningAnalystResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_candidates: list[SubscriptionCandidate] = Field(default_factory=list)
    planning_cards: list[PlanningCardEvidence] = Field(default_factory=list)
    allowed_ids: IdAllowlist = Field(default_factory=IdAllowlist)


def ensure_read_only_connection(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA query_only").fetchone()
    if int(row[0]) != 1:
        raise RuntimeError("reconciliation analyst requires a query_only connection")


def _row_to_line(row: sqlite3.Row) -> StatementLineEvidence:
    item = dict(row)
    return StatementLineEvidence(
        line_id=int(item["line_id"]),
        source_document_id=int(item["source_document_id"]),
        document_name=item.get("document_name"),
        account_id=item.get("account_id"),
        account_name=item.get("account_name"),
        posted_on=item["posted_on"],
        month=item["month"],
        raw_description=item["raw_description"],
        norm_merchant=item["norm_merchant"],
        amount_cents=int(item["amount_cents"]),
        spend_cents=int(item["spend_cents"] or 0),
        income_cents=int(item["income_cents"] or 0),
        match_status=item["match_status"],
        matched_transaction_id=item.get("matched_transaction_id"),
        match_method=item.get("match_method") or "",
        match_score=float(item.get("match_score") or 0),
        match_rationale=item.get("match_rationale") or "",
        coverage_bucket=item["coverage_bucket"],
        category_names=item.get("category_names") or "",
        attention_reason=item.get("attention_reason") or "",
    )


def _line_allowed_ids(lines: list[StatementLineEvidence]) -> IdAllowlist:
    return IdAllowlist(
        transaction_ids={line.matched_transaction_id for line in lines if line.matched_transaction_id},
        statement_line_ids={line.line_id for line in lines},
        account_ids={line.account_id for line in lines if line.account_id is not None},
        merchants={line.norm_merchant for line in lines if line.norm_merchant},
    )


def _where_for_run(run: StatementRun) -> tuple[str, list[Any]]:
    if run.source_document_id is not None:
        return "source_document_id = ?", [run.source_document_id]
    return "month = ?", [run.month]


def _months_for_run(run: StatementRun) -> list[str]:
    return sorted(set(run.months or [run.month]))


def _dedupe_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get(item) for item in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def statement_auditor_tool(conn: sqlite3.Connection, run: StatementRun) -> StatementAuditResult:
    ensure_read_only_connection(conn)
    where, args = _where_for_run(run)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS line_count,
          COALESCE(SUM(spend_cents), 0) AS statement_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='covered' THEN spend_cents ELSE 0 END), 0) AS covered_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='unmatched' THEN spend_cents ELSE 0 END), 0) AS unmatched_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='ignored' THEN spend_cents ELSE 0 END), 0) AS ignored_spend_cents,
          COALESCE(SUM(income_cents), 0) AS income_cents,
          COALESCE(SUM(CASE WHEN attention_reason <> ''
                            AND attention_reason <> 'ignored/internal transfer'
                            THEN spend_cents ELSE 0 END), 0) AS attention_spend_cents
        FROM v_statement_coverage_lines
        WHERE {where}
        """,
        args,
    ).fetchone()
    summary = dict(row)
    denominator = int(summary["covered_spend_cents"]) + int(summary["unmatched_spend_cents"])
    summary["coverage_pct"] = round((100.0 * int(summary["covered_spend_cents"]) / denominator), 1) if denominator else 0.0

    rows = conn.execute(
        f"""
        SELECT *
        FROM v_statement_coverage_lines
        WHERE {where}
        ORDER BY spend_cents DESC, posted_on DESC, line_id DESC
        LIMIT 40
        """,
        args,
    ).fetchall()
    lines = [_row_to_line(row) for row in rows]
    attention = [
        line
        for line in lines
        if line.attention_reason and line.attention_reason != "ignored/internal transfer"
    ][:12]
    suspicious = [
        line
        for line in lines
        if line.coverage_bucket == "covered"
        and (line.attention_reason or (line.match_method == "llm" and line.match_score < 0.75))
    ][:8]
    return StatementAuditResult(
        coverage=CoverageSummary.model_validate(summary),
        lines=lines,
        attention_lines=attention,
        suspicious_matches=suspicious,
        allowed_ids=_line_allowed_ids(lines),
    )


def receipt_matcher_tool(conn: sqlite3.Connection, run: StatementRun) -> ReceiptMatcherResult:
    ensure_read_only_connection(conn)
    where, args = _where_for_run(run)
    rows = conn.execute(
        f"""
        SELECT *
        FROM v_statement_coverage_lines
        WHERE {where}
          AND coverage_bucket = 'unmatched'
          AND spend_cents > 0
        ORDER BY spend_cents DESC, posted_on DESC, line_id DESC
        LIMIT 12
        """,
        args,
    ).fetchall()
    out: list[ReceiptCandidateLine] = []
    allowed = IdAllowlist()
    for row in rows:
        line = _row_to_line(row)
        candidate_rows = conn.execute(
            """
            SELECT t.id
            FROM transactions t
            WHERE t.account_id IS ?
              AND t.amount_cents = ?
              AND t.posted_on BETWEEN date(?, '-7 days') AND date(?, '+1 days')
            ORDER BY
              CASE WHEN t.recon_status = 'uncleared' THEN 0 ELSE 1 END,
              ABS(julianday(t.posted_on) - julianday(?)),
              t.id
            LIMIT 5
            """,
            (line.account_id, line.amount_cents, line.posted_on, line.posted_on, line.posted_on),
        ).fetchall()
        candidate_ids = [int(candidate["id"]) for candidate in candidate_rows]
        out.append(ReceiptCandidateLine(line=line, candidate_transaction_ids=candidate_ids))
        allowed.statement_line_ids.add(line.line_id)
        if line.account_id is not None:
            allowed.account_ids.add(line.account_id)
        if line.norm_merchant:
            allowed.merchants.add(line.norm_merchant)
        allowed.transaction_ids.update(candidate_ids)
    return ReceiptMatcherResult(unmatched_lines=out, allowed_ids=allowed)


def recurring_analyst_tool(conn: sqlite3.Connection, run: StatementRun) -> RecurringAnalystResult:
    ensure_read_only_connection(conn)
    changes: list[RecurringChange] = []
    allowed = IdAllowlist()
    rows = _dedupe_rows(
        [
            row
            for month in _months_for_run(run)
            for row in recurring_delta_rows(conn, month)
        ],
        ("merchant", "account_id", "previous_month", "month", "direction"),
    )
    for row in rows:
        change = RecurringChange(
            merchant=row["merchant"],
            account_id=int(row["account_id"]),
            account_name=row["account_name"],
            month=row["month"],
            previous_month=row["previous_month"],
            current_amount_cents=int(row["current_amount_cents"]),
            previous_amount_cents=int(row["previous_amount_cents"]),
            amount_delta_cents=int(row["amount_delta_cents"]),
            pct_change=row["pct_change"],
            direction=row["direction"],
            current_transaction_ids=_coerce_int_list(row.get("current_transaction_ids")),
            previous_transaction_ids=_coerce_int_list(row.get("previous_transaction_ids")),
            current_statement_line_ids=_coerce_int_list(row.get("current_statement_line_ids")),
            previous_statement_line_ids=_coerce_int_list(row.get("previous_statement_line_ids")),
            category_names=row.get("category_names") or "",
        )
        changes.append(change)
        allowed.account_ids.add(change.account_id)
        if change.merchant:
            allowed.merchants.add(change.merchant)
        allowed.transaction_ids.update(change.current_transaction_ids)
        allowed.transaction_ids.update(change.previous_transaction_ids)
        allowed.statement_line_ids.update(change.current_statement_line_ids)
        allowed.statement_line_ids.update(change.previous_statement_line_ids)
    return RecurringAnalystResult(changes=changes, allowed_ids=allowed)


def planning_analyst_tool(conn: sqlite3.Connection, run: StatementRun) -> PlanningAnalystResult:
    ensure_read_only_connection(conn)
    subscriptions: list[SubscriptionCandidate] = []
    cards: list[PlanningCardEvidence] = []
    allowed = IdAllowlist()

    subscription_rows = _dedupe_rows(
        [
            row
            for month in _months_for_run(run)
            for row in subscription_watchlist_rows(conn, month)
        ],
        ("merchant", "account_id", "candidate_type", "first_month", "last_month"),
    )
    for row in subscription_rows:
        candidate = SubscriptionCandidate(
            merchant=row["merchant"],
            account_id=int(row["account_id"]),
            account_name=row["account_name"],
            candidate_type=row["candidate_type"],
            first_month=row["first_month"],
            last_month=row["last_month"],
            first_seen_on=row["first_seen_on"],
            last_seen_on=row["last_seen_on"],
            expected_next_charge_on=row["expected_next_charge_on"],
            estimated_amount_cents=int(row["estimated_amount_cents"]),
            transaction_ids=_coerce_int_list(row.get("transaction_ids")),
            statement_line_ids=_coerce_int_list(row.get("statement_line_ids")),
            category_names=row.get("category_names") or "",
            decision_label=row.get("decision_label") or "",
        )
        subscriptions.append(candidate)
        allowed.account_ids.add(candidate.account_id)
        if candidate.merchant:
            allowed.merchants.add(candidate.merchant)
        allowed.transaction_ids.update(candidate.transaction_ids)
        allowed.statement_line_ids.update(candidate.statement_line_ids)

    card_rows = _dedupe_rows(
        [
            row
            for month in _months_for_run(run)
            for row in planning_insight_cards(conn, month)[:16]
        ],
        ("card_key",),
    )
    for row in card_rows:
        card = PlanningCardEvidence(
            **{
                **row,
                "transaction_ids": _coerce_int_list(row.get("transaction_ids")),
                "statement_line_ids": _coerce_int_list(row.get("statement_line_ids")),
                "category_ids": _coerce_int_list(row.get("category_ids")),
            }
        )
        cards.append(card)
        allowed.transaction_ids.update(card.transaction_ids)
        allowed.statement_line_ids.update(card.statement_line_ids)
        allowed.category_ids.update(card.category_ids)

    return PlanningAnalystResult(
        subscription_candidates=subscriptions,
        planning_cards=cards,
        allowed_ids=allowed,
    )
