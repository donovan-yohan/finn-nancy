"""Statement-policy and account-period expectation truth.

This module is the single production mutation path for FN-141A.  It keeps the
three independent axes separate:

* immutable, effective-dated account policy versions;
* one per-account/per-month requirement state; and
* the finite lifecycle for required statement evidence.

The defining state-machine invariant is that a lifecycle value can change only
through ``LEGAL_LIFECYCLE_TRANSITIONS``.  Every real expectation mutation first
appends an audit row with a unique operation key; migration 030 requires the
subsequent row update to present that same key.  Invalid and duplicate
operations therefore leave both current state and history unchanged.
"""
from __future__ import annotations

import calendar
import sqlite3
import uuid
from datetime import date
from enum import StrEnum
from typing import Mapping

from . import repo_close


class PolicyConfiguration(StrEnum):
    CONFIGURED = "configured"
    UNCONFIGURED = "unconfigured"


class RequirementMode(StrEnum):
    REQUIRED = "required"
    NO_STATEMENT = "no_statement"


class StatementCadence(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    NONE = "none"


class RequirementState(StrEnum):
    REQUIRED = "required"
    WAIVED = "waived"
    NOT_DUE = "not_due"
    EXEMPT = "exempt"
    UNCONFIGURED = "unconfigured"


class LifecycleState(StrEnum):
    EXPECTED = "expected"
    RECEIVED = "received"
    REVIEWED = "reviewed"
    RECONCILED = "reconciled"


class LifecycleEvent(StrEnum):
    DOCUMENT_ATTACHED = "document_attached"
    REVIEW_APPROVED = "review_approved"
    RECONCILED = "reconciled"
    UNRECONCILED = "unreconciled"
    NEW_EVIDENCE = "new_evidence"
    LAST_DOCUMENT_REMOVED = "last_document_removed"


LEGAL_LIFECYCLE_TRANSITIONS: dict[
    tuple[LifecycleState, LifecycleEvent], LifecycleState
] = {
    (LifecycleState.EXPECTED, LifecycleEvent.DOCUMENT_ATTACHED): LifecycleState.RECEIVED,
    (LifecycleState.RECEIVED, LifecycleEvent.REVIEW_APPROVED): LifecycleState.REVIEWED,
    (LifecycleState.REVIEWED, LifecycleEvent.RECONCILED): LifecycleState.RECONCILED,
    (LifecycleState.RECONCILED, LifecycleEvent.UNRECONCILED): LifecycleState.REVIEWED,
    (LifecycleState.REVIEWED, LifecycleEvent.NEW_EVIDENCE): LifecycleState.RECEIVED,
    (LifecycleState.RECONCILED, LifecycleEvent.NEW_EVIDENCE): LifecycleState.RECEIVED,
    (
        LifecycleState.RECEIVED,
        LifecycleEvent.LAST_DOCUMENT_REMOVED,
    ): LifecycleState.EXPECTED,
    (
        LifecycleState.REVIEWED,
        LifecycleEvent.LAST_DOCUMENT_REMOVED,
    ): LifecycleState.EXPECTED,
    (
        LifecycleState.RECONCILED,
        LifecycleEvent.LAST_DOCUMENT_REMOVED,
    ): LifecycleState.EXPECTED,
}

TERMINAL_LINE_STATUSES = frozenset({"matched", "promoted", "ignored"})


def _require_text(value: str, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required for auditability")
    return text


def normalize_month(value: str) -> str:
    month = (value or "").strip()
    if len(month) != 7:
        raise ValueError("month must be YYYY-MM")
    try:
        parsed = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    if parsed.strftime("%Y-%m") != month:
        raise ValueError("month must be YYYY-MM")
    return month


def _normalize_optional_date(value: str | None, field: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return text


def _enum_value(enum_type, value, field: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"invalid {field}; expected one of: {allowed}") from exc


def _guard_month(conn: sqlite3.Connection, month: str) -> None:
    if repo_close.is_month_locked(conn, month):
        raise repo_close.MonthLockedError(month)


def record_policy(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    effective_from_month: str,
    configuration_state: PolicyConfiguration | str,
    requirement_mode: RequirementMode | str | None = None,
    cadence: StatementCadence | str | None = None,
    anchor_month: int | str | None = None,
    active_from: str | None = None,
    active_to: str | None = None,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    """Append one immutable policy version without refreshing prepared rows."""
    effective_from_month = normalize_month(effective_from_month)
    configuration = _enum_value(
        PolicyConfiguration, configuration_state, "configuration_state"
    )
    active_from = _normalize_optional_date(active_from, "active_from")
    active_to = _normalize_optional_date(active_to, "active_to")
    if active_from and active_to and active_from > active_to:
        raise ValueError("active_from must be on or before active_to")
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")

    account = conn.execute(
        "SELECT id FROM accounts WHERE id=?", (int(account_id),)
    ).fetchone()
    if account is None:
        raise ValueError(f"unknown account_id: {account_id}")

    # A new version is prospective for every not-yet-prepared period.  Do not
    # let one reach back across a currently closed month.
    closed = conn.execute(
        """SELECT month
           FROM closed_periods
           WHERE status='closed' AND month >= ?
           ORDER BY month
           LIMIT 1""",
        (effective_from_month,),
    ).fetchone()
    if closed is not None:
        raise repo_close.MonthLockedError(str(closed["month"]))

    mode: RequirementMode | None
    normalized_cadence: StatementCadence | None
    normalized_anchor: int | None
    if configuration == PolicyConfiguration.UNCONFIGURED:
        if requirement_mode not in (None, "") or cadence not in (None, ""):
            raise ValueError("unconfigured policy cannot declare requirement or cadence")
        if anchor_month not in (None, ""):
            raise ValueError("unconfigured policy cannot declare anchor_month")
        mode = None
        normalized_cadence = None
        normalized_anchor = None
    else:
        if requirement_mode in (None, ""):
            raise ValueError("configured policy requires requirement_mode")
        mode = _enum_value(RequirementMode, requirement_mode, "requirement_mode")
        if cadence in (None, ""):
            raise ValueError("configured policy requires cadence")
        normalized_cadence = _enum_value(StatementCadence, cadence, "cadence")
        if mode == RequirementMode.NO_STATEMENT:
            if normalized_cadence != StatementCadence.NONE:
                raise ValueError("no_statement policy requires cadence none")
            if anchor_month not in (None, ""):
                raise ValueError("no_statement policy cannot declare anchor_month")
            normalized_anchor = None
        else:
            if normalized_cadence not in {
                StatementCadence.MONTHLY,
                StatementCadence.QUARTERLY,
                StatementCadence.ANNUAL,
            }:
                raise ValueError("required policy needs monthly, quarterly, or annual cadence")
            if normalized_cadence == StatementCadence.MONTHLY:
                if anchor_month not in (None, ""):
                    raise ValueError("monthly policy cannot declare anchor_month")
                normalized_anchor = None
            else:
                try:
                    normalized_anchor = int(anchor_month)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "quarterly and annual policies require anchor_month 1-12"
                    ) from exc
                if not 1 <= normalized_anchor <= 12:
                    raise ValueError(
                        "quarterly and annual policies require anchor_month 1-12"
                    )

    cursor = conn.execute(
        """INSERT INTO account_statement_policies(
             account_id, effective_from_month, configuration_state,
             requirement_mode, cadence, anchor_month, active_from, active_to,
             created_by, reason
           )
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            int(account_id),
            effective_from_month,
            configuration.value,
            mode.value if mode is not None else None,
            normalized_cadence.value if normalized_cadence is not None else None,
            normalized_anchor,
            active_from,
            active_to,
            actor,
            reason,
        ),
    )
    row = conn.execute(
        "SELECT * FROM account_statement_policies WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()
    assert row is not None
    return row


def policy_for_month(
    conn: sqlite3.Connection, account_id: int, month: str
) -> sqlite3.Row | None:
    month = normalize_month(month)
    return conn.execute(
        """SELECT *
           FROM account_statement_policies
           WHERE account_id=? AND effective_from_month <= ?
           ORDER BY effective_from_month DESC, id DESC
           LIMIT 1""",
        (int(account_id), month),
    ).fetchone()


def latest_policies(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """SELECT policy.*
           FROM account_statement_policies policy
           WHERE NOT EXISTS (
             SELECT 1
             FROM account_statement_policies newer
             WHERE newer.account_id=policy.account_id
               AND (
                 newer.effective_from_month > policy.effective_from_month
                 OR (
                   newer.effective_from_month=policy.effective_from_month
                   AND newer.id > policy.id
                 )
               )
           )
           ORDER BY policy.account_id"""
    ).fetchall()
    return {int(row["account_id"]): row for row in rows}


def requirement_for_month(
    policy: Mapping[str, object], month: str
) -> RequirementState:
    """Derive one requirement state, with activity evaluated before cadence."""
    month = normalize_month(month)
    year, month_number = (int(part) for part in month.split("-"))
    period_start = date(year, month_number, 1)
    period_end = date(year, month_number, calendar.monthrange(year, month_number)[1])

    active_from_raw = policy["active_from"]
    active_to_raw = policy["active_to"]
    active_from = (
        date.fromisoformat(str(active_from_raw)) if active_from_raw is not None else None
    )
    active_to = date.fromisoformat(str(active_to_raw)) if active_to_raw is not None else None
    if (active_from is not None and active_from > period_end) or (
        active_to is not None and active_to < period_start
    ):
        return RequirementState.NOT_DUE

    configuration = _enum_value(
        PolicyConfiguration, policy["configuration_state"], "configuration_state"
    )
    if configuration == PolicyConfiguration.UNCONFIGURED:
        return RequirementState.UNCONFIGURED

    mode = _enum_value(RequirementMode, policy["requirement_mode"], "requirement_mode")
    if mode == RequirementMode.NO_STATEMENT:
        return RequirementState.EXEMPT

    cadence = _enum_value(StatementCadence, policy["cadence"], "cadence")
    if cadence == StatementCadence.MONTHLY:
        return RequirementState.REQUIRED
    anchor = int(policy["anchor_month"])
    if cadence == StatementCadence.QUARTERLY:
        return (
            RequirementState.REQUIRED
            if (month_number - anchor) % 3 == 0
            else RequirementState.NOT_DUE
        )
    if cadence == StatementCadence.ANNUAL:
        return (
            RequirementState.REQUIRED
            if month_number == anchor
            else RequirementState.NOT_DUE
        )
    raise ValueError("required policy cannot use cadence none")


def prepare_account_period(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    month: str,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row, bool]:
    """Materialize one immutable policy snapshot; duplicate preparation is a no-op."""
    month = normalize_month(month)
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    existing = conn.execute(
        """SELECT *
           FROM account_statement_expectations
           WHERE account_id=? AND period_month=?""",
        (int(account_id), month),
    ).fetchone()
    if existing is not None:
        return existing, False
    _guard_month(conn, month)
    policy = policy_for_month(conn, int(account_id), month)
    if policy is None:
        raise ValueError(f"account {account_id} has no policy effective for {month}")
    requirement = requirement_for_month(policy, month)
    lifecycle = (
        LifecycleState.EXPECTED.value
        if requirement == RequirementState.REQUIRED
        else None
    )
    cursor = conn.execute(
        """INSERT OR IGNORE INTO account_statement_expectations(
             account_id, period_month, policy_id, origin,
             requirement_state, lifecycle_state, created_by, reason
           )
           VALUES (?,?,?,'policy',?,?,?,?)""",
        (
            int(account_id),
            month,
            int(policy["id"]),
            requirement.value,
            lifecycle,
            actor,
            reason,
        ),
    )
    row = conn.execute(
        """SELECT *
           FROM account_statement_expectations
           WHERE account_id=? AND period_month=?""",
        (int(account_id), month),
    ).fetchone()
    assert row is not None
    return row, bool(cursor.rowcount)


def prepare_period(
    conn: sqlite3.Connection, *, month: str, actor: str, reason: str
) -> list[sqlite3.Row]:
    month = normalize_month(month)
    rows: list[sqlite3.Row] = []
    for account in conn.execute("SELECT id FROM accounts ORDER BY id"):
        row, _ = prepare_account_period(
            conn,
            account_id=int(account["id"]),
            month=month,
            actor=actor,
            reason=reason,
        )
        rows.append(row)
    return rows


def expectation(
    conn: sqlite3.Connection, expectation_id: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM account_statement_expectations WHERE id=?",
        (int(expectation_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown expectation_id: {expectation_id}")
    return row


def exact_document_identity(
    conn: sqlite3.Connection, source_document_id: int
) -> tuple[int, str] | None:
    """Return the one proven ``(account_id, period_month)`` for a statement.

    Identity is deliberately derived from the staged statement contract, not
    transaction dates or document names.  Every line must agree on one
    non-null account and one valid declared closing period.  ``None`` means the
    source must remain unattached in review.
    """
    lines = conn.execute(
        """SELECT account_id, statement_period
           FROM statement_lines
           WHERE source_document_id=?
             AND review_disposition='active'
           ORDER BY id""",
        (int(source_document_id),),
    ).fetchall()
    if not lines:
        review = conn.execute(
            """SELECT account_id, period_month, activity_kind
               FROM statement_reviews
               WHERE source_document_id=?""",
            (int(source_document_id),),
        ).fetchone()
        if (
            review is not None
            and review["activity_kind"] == "zero_activity"
            and review["account_id"] is not None
            and review["period_month"] is not None
        ):
            return int(review["account_id"]), normalize_month(
                str(review["period_month"])
            )
        return None
    if any(line["account_id"] is None for line in lines):
        return None
    accounts = {int(line["account_id"]) for line in lines}
    if len(accounts) != 1:
        return None
    periods: set[str] = set()
    for line in lines:
        raw_period = str(line["statement_period"] or "")
        try:
            periods.add(normalize_month(raw_period))
        except ValueError:
            return None
    if len(periods) != 1:
        return None
    return next(iter(accounts)), next(iter(periods))


def active_link_for_document(
    conn: sqlite3.Connection, source_document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT link.*, expectation.account_id, expectation.period_month,
                  expectation.requirement_state, expectation.lifecycle_state
           FROM statement_expectation_documents link
           JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE link.source_document_id=? AND link.status='active'""",
        (int(source_document_id),),
    ).fetchone()


def attach_exact_document(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Prepare and auto-attach one exactly identified, required source.

    An already-materialized account-period snapshot is authoritative even when
    a newer policy version now applies; policy changes affect only unprepared
    periods until an explicit refresh.  When no snapshot exists, a non-required
    or unconfigured policy is not materialized merely because a document
    arrived.
    """
    existing = active_link_for_document(conn, source_document_id)
    if existing is not None:
        return expectation(conn, int(existing["expectation_id"])), False
    identity = exact_document_identity(conn, source_document_id)
    if identity is None:
        return None, False
    account_id, month = identity
    row = conn.execute(
        """SELECT *
           FROM account_statement_expectations
           WHERE account_id=? AND period_month=?""",
        (int(account_id), month),
    ).fetchone()
    if row is None:
        policy = policy_for_month(conn, account_id, month)
        if (
            policy is None
            or requirement_for_month(policy, month)
            != RequirementState.REQUIRED
        ):
            return None, False
        row, _ = prepare_account_period(
            conn,
            account_id=account_id,
            month=month,
            actor=actor,
            reason=reason,
        )
    if row["requirement_state"] != RequirementState.REQUIRED.value:
        return row, False
    return attach_document(
        conn,
        int(row["id"]),
        int(source_document_id),
        actor=actor,
        reason=reason,
        automatic=True,
    )


def mark_document_reviewed(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Advance an actively linked document's expectation after human/auto review."""
    link = active_link_for_document(conn, source_document_id)
    if link is None:
        return None, False
    row = expectation(conn, int(link["expectation_id"]))
    if row["lifecycle_state"] in {
        LifecycleState.REVIEWED.value,
        LifecycleState.RECONCILED.value,
    }:
        return row, False
    if row["lifecycle_state"] != LifecycleState.RECEIVED.value:
        raise ValueError(
            "statement review requires a received account-period expectation"
        )
    return (
        mark_reviewed(
            conn,
            int(row["id"]),
            actor=actor,
            reason=reason,
        ),
        True,
    )


def detach_document_source(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Detach a source through the audited link path before reject/delete."""
    link = active_link_for_document(conn, source_document_id)
    if link is None:
        return None, False
    return detach_document(
        conn,
        int(link["id"]),
        actor=actor,
        reason=reason,
    )


def _reconciliation_evidence_summary(
    conn: sqlite3.Connection, expectation_id: int
) -> tuple[int, int, bool]:
    """Return active line count, unresolved count, and valid zero proof."""
    summary = conn.execute(
        """SELECT
             COUNT(line.id) AS line_count,
             COALESCE(SUM(
               CASE
                 WHEN line.id IS NOT NULL
                  AND (
                    line.is_pending=1
                    OR line.match_status NOT IN ('matched','promoted','ignored')
                  )
                 THEN 1 ELSE 0
               END
             ), 0) AS unresolved_count
           FROM statement_expectation_documents evidence
           LEFT JOIN statement_lines line
             ON line.source_document_id=evidence.source_document_id
            AND line.review_disposition='active'
           WHERE evidence.expectation_id=? AND evidence.status='active'""",
        (int(expectation_id),),
    ).fetchone()
    assert summary is not None
    zero = conn.execute(
        """SELECT
             COUNT(evidence.id) AS document_count,
             COALESCE(SUM(
               CASE
                 WHEN review.review_state IN (
                        'approved', 'approved_with_override'
                      )
                  AND review.activity_kind='zero_activity'
                  AND review.account_id=expectation.account_id
                  AND review.period_month=expectation.period_month
                  AND review.opening_balance_cents IS NOT NULL
                  AND review.closing_balance_cents IS NOT NULL
                  AND review.opening_balance_cents=review.closing_balance_cents
                 THEN 0 ELSE 1
               END
             ), 0) AS invalid_count
           FROM account_statement_expectations expectation
           JOIN statement_expectation_documents evidence
             ON evidence.expectation_id=expectation.id
            AND evidence.status='active'
           LEFT JOIN statement_reviews review
             ON review.source_document_id=evidence.source_document_id
           WHERE expectation.id=?""",
        (int(expectation_id),),
    ).fetchone()
    assert zero is not None
    zero_complete = (
        int(zero["document_count"]) > 0
        and int(zero["invalid_count"]) == 0
        and int(summary["line_count"]) == 0
    )
    return (
        int(summary["line_count"]),
        int(summary["unresolved_count"]),
        zero_complete,
    )


def sync_document_reconciliation(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Synchronize the linked expectation from all of its line dispositions.

    The operation is idempotent: only ``reviewed`` can advance to
    ``reconciled`` and only ``reconciled`` can regress to ``reviewed``.
    ``received`` remains received until statement identity has been reviewed.
    """
    link = active_link_for_document(conn, source_document_id)
    if link is None:
        return None, False
    row = expectation(conn, int(link["expectation_id"]))
    line_count, unresolved_count, zero_complete = (
        _reconciliation_evidence_summary(conn, int(row["id"]))
    )
    complete = (
        (line_count > 0 and unresolved_count == 0)
        or zero_complete
    )
    if row["lifecycle_state"] == LifecycleState.REVIEWED.value and complete:
        return (
            mark_reconciled(
                conn,
                int(row["id"]),
                actor=actor,
                reason=reason,
            ),
            True,
        )
    if row["lifecycle_state"] == LifecycleState.RECONCILED.value and not complete:
        return (
            unreconcile(
                conn,
                int(row["id"]),
                actor=actor,
                reason=reason,
            ),
            True,
        )
    return row, False


def unreconcile_document(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Explicitly downgrade a reconciled expectation before resetting its lines."""
    link = active_link_for_document(conn, source_document_id)
    if link is None:
        return None, False
    row = expectation(conn, int(link["expectation_id"]))
    if row["lifecycle_state"] != LifecycleState.RECONCILED.value:
        return row, False
    return (
        unreconcile(
            conn,
            int(row["id"]),
            actor=actor,
            reason=reason,
        ),
        True,
    )


def reopen_document_review(
    conn: sqlite3.Connection,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Move reviewed evidence back to received before an audited correction.

    Ledger-affecting rows must be explicitly unreconciled by the caller before
    editing.  This function only synchronizes the expectation lifecycle.
    """
    link = active_link_for_document(conn, source_document_id)
    if link is None:
        return None, False
    row = expectation(conn, int(link["expectation_id"]))
    if row["lifecycle_state"] == LifecycleState.RECEIVED.value:
        return row, False
    if row["lifecycle_state"] == LifecycleState.RECONCILED.value:
        row = _transition(
            conn,
            row,
            LifecycleEvent.UNRECONCILED,
            actor=actor,
            reason=reason,
        )
    if row["lifecycle_state"] == LifecycleState.REVIEWED.value:
        return (
            _transition(
                conn,
                row,
                LifecycleEvent.NEW_EVIDENCE,
                actor=actor,
                reason=reason,
            ),
            True,
        )
    return row, False


def period_matrix(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Project every account's materialized or virtual statement truth for a month."""
    month = normalize_month(month)
    accounts = conn.execute(
        """SELECT id, name, institution, kind, is_active
           FROM accounts
           ORDER BY name, id"""
    ).fetchall()
    rows: list[dict] = []
    for account in accounts:
        stored = conn.execute(
            """SELECT *
               FROM account_statement_expectations
               WHERE account_id=? AND period_month=?""",
            (int(account["id"]), month),
        ).fetchone()
        if stored is None:
            policy = policy_for_month(conn, int(account["id"]), month)
            requirement = (
                requirement_for_month(policy, month)
                if policy is not None
                else RequirementState.UNCONFIGURED
            )
            lifecycle = (
                LifecycleState.EXPECTED.value
                if requirement == RequirementState.REQUIRED
                else None
            )
            item = {
                "id": None,
                "account_id": int(account["id"]),
                "period_month": month,
                "policy_id": int(policy["id"]) if policy is not None else None,
                "origin": "virtual",
                "requirement_state": requirement.value,
                "lifecycle_state": lifecycle,
                "waived_by": None,
                "waiver_reason": None,
                "materialized": False,
            }
        else:
            item = dict(stored)
            item["materialized"] = True
        evidence = conn.execute(
            """SELECT
                 COUNT(DISTINCT link.id) AS document_count,
                 COUNT(line.id) AS line_count,
                 COALESCE(SUM(
                   CASE
                     WHEN line.is_pending=1
                       OR line.match_status NOT IN ('matched','promoted','ignored')
                     THEN 1 ELSE 0
                   END
                 ), 0) AS unresolved_line_count
               FROM statement_expectation_documents link
               LEFT JOIN statement_lines line
                 ON line.source_document_id=link.source_document_id
                AND line.review_disposition='active'
               WHERE link.expectation_id=? AND link.status='active'""",
            (int(item["id"]) if item["id"] is not None else -1,),
        ).fetchone()
        assert evidence is not None
        item.update(
            {
                "account_name": str(account["name"]),
                "institution": str(account["institution"] or ""),
                "account_kind": str(account["kind"]),
                "document_count": int(evidence["document_count"]),
                "line_count": int(evidence["line_count"]),
                "unresolved_line_count": int(evidence["unresolved_line_count"]),
            }
        )
        item["blocking"] = (
            item["requirement_state"] == RequirementState.UNCONFIGURED.value
            or (
                item["requirement_state"] == RequirementState.REQUIRED.value
                and item["lifecycle_state"] != LifecycleState.RECONCILED.value
            )
        )
        rows.append(item)
    return rows


def signoff_blockers(conn: sqlite3.Connection, month: str) -> list[dict]:
    return [row for row in period_matrix(conn, month) if row["blocking"]]


def reconciliation_months(conn: sqlite3.Connection) -> list[str]:
    """Months declared by statement truth/evidence, newest first."""
    values = {
        str(row[0])
        for row in conn.execute(
            """SELECT period_month FROM account_statement_expectations
               UNION
               SELECT statement_period FROM statement_lines
               WHERE statement_period IS NOT NULL
                 AND review_disposition='active'
               UNION
               SELECT period_month FROM statement_reviews
               WHERE period_month IS NOT NULL"""
        ).fetchall()
        if row[0] is not None
    }
    months: list[str] = []
    for value in values:
        try:
            months.append(normalize_month(value))
        except ValueError:
            continue
    return sorted(set(months), reverse=True)


def _append_change_audit(
    conn: sqlite3.Connection,
    row: Mapping[str, object],
    *,
    event_kind: str,
    new_requirement_state: RequirementState,
    new_lifecycle_state: LifecycleState | None,
    actor: str,
    reason: str,
    policy_id: int | None = None,
) -> str:
    operation_key = f"expectation:{row['id']}:{uuid.uuid4()}"
    conn.execute(
        """INSERT INTO statement_expectation_audit(
             operation_key, event_kind, policy_id, expectation_id, account_id,
             period_month, old_requirement_state, new_requirement_state,
             old_lifecycle_state, new_lifecycle_state, actor, reason
           )
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_key,
            event_kind,
            int(policy_id if policy_id is not None else row["policy_id"]),
            int(row["id"]),
            int(row["account_id"]),
            str(row["period_month"]),
            str(row["requirement_state"]),
            new_requirement_state.value,
            row["lifecycle_state"],
            new_lifecycle_state.value if new_lifecycle_state is not None else None,
            _require_text(actor, "actor"),
            _require_text(reason, "reason"),
        ),
    )
    return operation_key


def _update_expectation(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    event_kind: str,
    new_requirement_state: RequirementState,
    new_lifecycle_state: LifecycleState | None,
    actor: str,
    reason: str,
    policy_id: int | None = None,
    origin: str | None = None,
    waived_at_sql: str = "waived_at",
    waived_by: str | None = None,
    waiver_reason: str | None = None,
) -> sqlite3.Row:
    operation_key = _append_change_audit(
        conn,
        row,
        event_kind=event_kind,
        new_requirement_state=new_requirement_state,
        new_lifecycle_state=new_lifecycle_state,
        actor=actor,
        reason=reason,
        policy_id=policy_id,
    )
    conn.execute(
        f"""UPDATE account_statement_expectations
            SET policy_id=?, origin=?, requirement_state=?, lifecycle_state=?,
                waived_at={waived_at_sql}, waived_by=?, waiver_reason=?,
                last_transition_key=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (
            int(policy_id if policy_id is not None else row["policy_id"]),
            origin or str(row["origin"]),
            new_requirement_state.value,
            new_lifecycle_state.value if new_lifecycle_state is not None else None,
            waived_by,
            waiver_reason,
            operation_key,
            int(row["id"]),
        ),
    )
    return expectation(conn, int(row["id"]))


def _transition(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    event: LifecycleEvent,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    if row["requirement_state"] != RequirementState.REQUIRED.value:
        raise ValueError("only a required expectation has a processing lifecycle")
    current = _enum_value(LifecycleState, row["lifecycle_state"], "lifecycle_state")
    next_state = LEGAL_LIFECYCLE_TRANSITIONS.get((current, event))
    if next_state is None:
        raise ValueError(f"event {event.value} is invalid from lifecycle {current.value}")
    return _update_expectation(
        conn,
        row,
        event_kind="lifecycle_transition",
        new_requirement_state=RequirementState.REQUIRED,
        new_lifecycle_state=next_state,
        actor=actor,
        reason=reason,
        waived_at_sql="NULL",
    )


def waive(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    row = expectation(conn, expectation_id)
    _guard_month(conn, str(row["period_month"]))
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    if (
        row["requirement_state"] != RequirementState.REQUIRED.value
        or row["lifecycle_state"] != LifecycleState.EXPECTED.value
    ):
        raise ValueError("only a required expected period can be waived")
    linked = conn.execute(
        """SELECT 1
           FROM statement_expectation_documents
           WHERE expectation_id=? AND status='active'
           LIMIT 1""",
        (int(expectation_id),),
    ).fetchone()
    if linked is not None:
        raise ValueError("an expectation with active statement documents cannot be waived")
    return _update_expectation(
        conn,
        row,
        event_kind="requirement_waived",
        new_requirement_state=RequirementState.WAIVED,
        new_lifecycle_state=None,
        actor=actor,
        reason=reason,
        waived_at_sql="CURRENT_TIMESTAMP",
        waived_by=actor,
        waiver_reason=reason,
    )


def restore_waiver(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    row = expectation(conn, expectation_id)
    _guard_month(conn, str(row["period_month"]))
    if row["requirement_state"] != RequirementState.WAIVED.value:
        raise ValueError("only a waived period can be restored")
    return _update_expectation(
        conn,
        row,
        event_kind="waiver_restored",
        new_requirement_state=RequirementState.REQUIRED,
        new_lifecycle_state=LifecycleState.EXPECTED,
        actor=actor,
        reason=reason,
        waived_at_sql="NULL",
    )


def refresh_from_policy(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row, bool]:
    """Explicitly refresh one unprocessed snapshot; closed or evidenced rows fail."""
    row = expectation(conn, expectation_id)
    month = str(row["period_month"])
    _guard_month(conn, month)
    linked = conn.execute(
        """SELECT 1
           FROM statement_expectation_documents
           WHERE expectation_id=? AND status='active'
           LIMIT 1""",
        (int(expectation_id),),
    ).fetchone()
    if linked is not None:
        raise ValueError("an expectation with active evidence cannot be refreshed")
    if row["requirement_state"] == RequirementState.WAIVED.value:
        raise ValueError("restore the waiver before refreshing policy")
    if row["lifecycle_state"] not in (None, LifecycleState.EXPECTED.value):
        raise ValueError("a processed expectation cannot be refreshed")
    policy = policy_for_month(conn, int(row["account_id"]), month)
    if policy is None:
        raise ValueError("no policy is effective for the expectation month")
    requirement = requirement_for_month(policy, month)
    lifecycle = (
        LifecycleState.EXPECTED
        if requirement == RequirementState.REQUIRED
        else None
    )
    if (
        int(row["policy_id"]) == int(policy["id"])
        and row["origin"] == "policy"
        and row["requirement_state"] == requirement.value
        and row["lifecycle_state"] == (
            lifecycle.value if lifecycle is not None else None
        )
    ):
        return row, False
    updated = _update_expectation(
        conn,
        row,
        event_kind="expectation_refreshed",
        new_requirement_state=requirement,
        new_lifecycle_state=lifecycle,
        actor=actor,
        reason=reason,
        policy_id=int(policy["id"]),
        origin="policy",
        waived_at_sql="NULL",
    )
    return updated, True


def _validate_document_identity(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    account_id: int,
    period_month: str,
    automatic: bool,
) -> None:
    document = conn.execute(
        "SELECT kind FROM source_documents WHERE id=?",
        (int(source_document_id),),
    ).fetchone()
    if document is None:
        raise ValueError(f"unknown source_document_id: {source_document_id}")
    if document["kind"] != "statement":
        raise ValueError("only a statement document/import can attach to an expectation")

    lines = conn.execute(
        """SELECT account_id, statement_period
           FROM statement_lines
           WHERE source_document_id=?
             AND review_disposition='active'
           ORDER BY id""",
        (int(source_document_id),),
    ).fetchall()
    if not lines:
        review = conn.execute(
            """SELECT account_id, period_month, activity_kind
               FROM statement_reviews WHERE source_document_id=?""",
            (int(source_document_id),),
        ).fetchone()
        if (
            review is not None
            and review["activity_kind"] == "zero_activity"
            and review["account_id"] is not None
            and int(review["account_id"]) == int(account_id)
            and review["period_month"] == period_month
        ):
            return
        raise ValueError(
            "statement identity requires active rows or explicit zero-activity metadata"
        )

    observed_accounts = {
        int(line["account_id"]) for line in lines if line["account_id"] is not None
    }
    observed_periods: set[str] = set()
    for line in lines:
        raw_period = str(line["statement_period"] or "")
        if raw_period:
            observed_periods.add(normalize_month(raw_period))
    if observed_accounts and observed_accounts != {int(account_id)}:
        raise ValueError("statement lines do not agree with the expectation account")
    if observed_periods and observed_periods != {period_month}:
        raise ValueError("statement lines do not agree with the expectation period")
    if automatic and (
        any(line["account_id"] is None for line in lines)
        or any(not str(line["statement_period"] or "") for line in lines)
        or observed_accounts != {int(account_id)}
        or observed_periods != {period_month}
    ):
        raise ValueError(
            "automatic attachment requires exactly one account and closing period"
        )


def attach_document(
    conn: sqlite3.Connection,
    expectation_id: int,
    source_document_id: int,
    *,
    actor: str,
    reason: str,
    automatic: bool = False,
) -> tuple[sqlite3.Row, bool]:
    row = expectation(conn, expectation_id)
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    if row["requirement_state"] != RequirementState.REQUIRED.value:
        raise ValueError("statement documents can attach only to required periods")
    existing = conn.execute(
        """SELECT expectation_id
           FROM statement_expectation_documents
           WHERE source_document_id=? AND status='active'""",
        (int(source_document_id),),
    ).fetchone()
    if existing is not None:
        if int(existing["expectation_id"]) == int(expectation_id):
            return row, False
        raise ValueError("statement document is already attached to another account-period")

    _guard_month(conn, str(row["period_month"]))
    _validate_document_identity(
        conn,
        source_document_id=int(source_document_id),
        account_id=int(row["account_id"]),
        period_month=str(row["period_month"]),
        automatic=automatic,
    )
    conn.execute(
        """INSERT INTO statement_expectation_documents(
             expectation_id, source_document_id, attached_by, attach_reason
           )
           VALUES (?,?,?,?)""",
        (int(expectation_id), int(source_document_id), actor, reason),
    )
    current = _enum_value(LifecycleState, row["lifecycle_state"], "lifecycle_state")
    if current == LifecycleState.EXPECTED:
        row = _transition(
            conn,
            row,
            LifecycleEvent.DOCUMENT_ATTACHED,
            actor=actor,
            reason=reason,
        )
    elif current in {LifecycleState.REVIEWED, LifecycleState.RECONCILED}:
        row = _transition(
            conn,
            row,
            LifecycleEvent.NEW_EVIDENCE,
            actor=actor,
            reason=reason,
        )
    return row, True


def detach_document(
    conn: sqlite3.Connection,
    link_id: int,
    *,
    actor: str,
    reason: str,
) -> tuple[sqlite3.Row, bool]:
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")
    link = conn.execute(
        """SELECT link.*, expectation.period_month
           FROM statement_expectation_documents link
           JOIN account_statement_expectations expectation
             ON expectation.id=link.expectation_id
           WHERE link.id=?""",
        (int(link_id),),
    ).fetchone()
    if link is None:
        raise ValueError(f"unknown statement expectation link: {link_id}")
    row = expectation(conn, int(link["expectation_id"]))
    if link["status"] == "detached":
        return row, False
    _guard_month(conn, str(link["period_month"]))
    conn.execute(
        """UPDATE statement_expectation_documents
           SET status='detached', detached_at=CURRENT_TIMESTAMP,
               detached_by=?, detach_reason=?
           WHERE id=?""",
        (actor, reason, int(link_id)),
    )
    remaining = conn.execute(
        """SELECT COUNT(*)
           FROM statement_expectation_documents
           WHERE expectation_id=? AND status='active'""",
        (int(row["id"]),),
    ).fetchone()[0]
    if int(remaining) == 0 and row["lifecycle_state"] != LifecycleState.EXPECTED.value:
        row = _transition(
            conn,
            row,
            LifecycleEvent.LAST_DOCUMENT_REMOVED,
            actor=actor,
            reason=reason,
        )
    return row, True


def mark_reviewed(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    row = expectation(conn, expectation_id)
    _guard_month(conn, str(row["period_month"]))
    links = conn.execute(
        """SELECT source_document_id
           FROM statement_expectation_documents
           WHERE expectation_id=? AND status='active'
           ORDER BY id""",
        (int(expectation_id),),
    ).fetchall()
    if not links:
        raise ValueError("review requires at least one active statement document")
    for link in links:
        _validate_document_identity(
            conn,
            source_document_id=int(link["source_document_id"]),
            account_id=int(row["account_id"]),
            period_month=str(row["period_month"]),
            automatic=False,
        )
    return _transition(
        conn,
        row,
        LifecycleEvent.REVIEW_APPROVED,
        actor=actor,
        reason=reason,
    )


def mark_reconciled(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    row = expectation(conn, expectation_id)
    _guard_month(conn, str(row["period_month"]))
    if row["lifecycle_state"] != LifecycleState.REVIEWED.value:
        raise ValueError(
            "event reconciled is invalid from lifecycle "
            f"{row['lifecycle_state']}"
        )
    for link in conn.execute(
        """SELECT source_document_id
           FROM statement_expectation_documents
           WHERE expectation_id=? AND status='active'
           ORDER BY id""",
        (int(expectation_id),),
    ):
        _validate_document_identity(
            conn,
            source_document_id=int(link["source_document_id"]),
            account_id=int(row["account_id"]),
            period_month=str(row["period_month"]),
            automatic=False,
        )
    line_count, unresolved_count, zero_complete = (
        _reconciliation_evidence_summary(conn, int(expectation_id))
    )
    if line_count == 0 and not zero_complete:
        raise ValueError(
            "reconciliation requires terminal statement rows or approved "
            "zero-activity metadata proof"
        )
    if line_count > 0 and unresolved_count:
        raise ValueError("every linked statement line must have a terminal disposition")
    return _transition(
        conn,
        row,
        LifecycleEvent.RECONCILED,
        actor=actor,
        reason=reason,
    )


def unreconcile(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    actor: str,
    reason: str,
) -> sqlite3.Row:
    row = expectation(conn, expectation_id)
    _guard_month(conn, str(row["period_month"]))
    return _transition(
        conn,
        row,
        LifecycleEvent.UNRECONCILED,
        actor=actor,
        reason=reason,
    )


def audit_for_expectation(
    conn: sqlite3.Connection, expectation_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT *
           FROM statement_expectation_audit
           WHERE expectation_id=?
           ORDER BY id""",
        (int(expectation_id),),
    ).fetchall()
