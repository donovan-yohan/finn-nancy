"""Architecture backpressure for the FN-147 universal period guard.

The database triggers are the fail-closed boundary.  This source inventory is
the complementary review boundary: every direct SQLite writer of period-bound
accounting state is named here, so a new route, worker, action, or repository
write cannot silently join the mutation surface.

The allowlist is intentionally keyed by path and qualified function, never line
number.  Refactors therefore require a conscious inventory update without
making harmless formatting changes noisy.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from pathlib import Path
import re

from app.db import engine


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"

# Raw capture, documents, and extraction rows are deliberately absent: they may
# be preserved before a period can be derived.  Their later activation into a
# statement, ledger, disposition, assertion, or close record is guarded here.
SENSITIVE_TABLES = frozenset(
    {
        "transactions",
        "transaction_splits",
        "transaction_relationships",
        "transaction_flow_reviews",
        "transaction_flow_audit",
        "statement_lines",
        "statement_reviews",
        "statement_review_pages",
        "statement_source_anchors",
        "statement_field_evidence",
        "statement_review_audit",
        "account_statement_policies",
        "account_statement_expectations",
        "statement_expectation_documents",
        "statement_expectation_audit",
        "account_balance_assertions",
        "positive_flow_decision_events",
        "merchant_resolution_events",
        "proposed_actions",
        "proposed_action_audit",
        "goal_ledger",
        # Preview rows may be period-free, but activation and its audit are not.
        "structured_statement_imports",
        "structured_statement_import_audit",
    }
)

# The old projection remains available during migration, but only the policy
# repositories may own close-state mutation.  New period_close_* tables are
# covered automatically rather than relying on this list staying exhaustive.
PERIOD_STATE_TABLES = frozenset(
    {"closed_periods", "close_audit", "period_write_overrides"}
)
PERIOD_STATE_WRITER_MODULES = frozenset(
    {"app/db/repo_period_policy.py", "app/db/repo_close.py"}
)

SQL_CALLS = frozenset({"execute", "executemany", "executescript"})
MUTATION_RE = re.compile(
    r"""
    \b
    (?P<verb>
        INSERT(?:\s+OR\s+\w+)?\s+INTO
        |REPLACE\s+INTO
        |UPDATE
        |DELETE\s+FROM
    )
    \s+["`\[]?(?P<table>[a-z_][a-z0-9_]*)
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
UPSERT_RE = re.compile(
    r"\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\b",
    re.IGNORECASE | re.DOTALL,
)


# This is the reviewed production writer surface at FN-147.  Counts make a
# second mutation inside an already-allowed function visible to review.
EXPECTED_SENSITIVE_WRITERS: dict[str, dict[str, int]] = {
    "app/accounting/flows.py::create_relationship": {
        "insert:transaction_relationships": 1,
    },
    "app/accounting/flows.py::revoke_relationship": {
        "update:transaction_relationships": 1,
    },
    "app/accounting/flows.py::set_flow_kind": {
        "insert:transaction_flow_audit": 1,
        "update:transaction_flow_reviews": 2,
        "update:transactions": 1,
    },
    "app/actions/recategorization.py::RecategorizationHandler.apply": {
        "update:transaction_splits": 1,
    },
    "app/actions/recategorization.py::RecategorizationHandler.revert": {
        "update:transaction_splits": 1,
        "update:transactions": 1,
    },
    "app/db/repo_actions.py::_insert_audit": {
        "insert:proposed_action_audit": 1,
    },
    "app/db/repo_actions.py::edit_payload": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_actions.py::enqueue_proposal": {
        "insert:proposed_actions": 1,
    },
    "app/db/repo_actions.py::mark_applied": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_actions.py::mark_reverted": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_actions.py::reject": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_actions.py::request_evidence": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_actions.py::snooze": {
        "update:proposed_actions": 1,
    },
    "app/db/repo_admin.py::delete_document": {
        "delete:transactions": 1,
    },
    "app/db/repo_assertions.py::record_assertion": {
        "insert:account_balance_assertions": 1,
        "update:account_balance_assertions": 1,
    },
    "app/db/repo_goals.py::close_month": {
        "insert:goal_ledger": 1,
        "update:goal_ledger": 2,
    },
    "app/db/repo_ledger.py::insert_split": {
        "insert:transaction_splits": 1,
    },
    "app/db/repo_ledger.py::insert_transaction": {
        "insert:transactions": 1,
    },
    "app/db/repo_merchant_knowledge.py::_insert_event": {
        "insert:merchant_resolution_events": 1,
    },
    "app/db/repo_statement_expectations.py::_append_change_audit": {
        "insert:statement_expectation_audit": 1,
    },
    "app/db/repo_statement_expectations.py::_update_expectation": {
        "update:account_statement_expectations": 1,
    },
    "app/db/repo_statement_expectations.py::attach_document": {
        "insert:statement_expectation_documents": 1,
    },
    "app/db/repo_statement_expectations.py::detach_document": {
        "update:statement_expectation_documents": 1,
    },
    "app/db/repo_statement_expectations.py::prepare_account_period": {
        "insert:account_statement_expectations": 1,
    },
    "app/db/repo_statement_expectations.py::record_policy": {
        "insert:account_statement_policies": 1,
    },
    "app/db/repo_statement_reviews.py::_audit": {
        "insert:statement_review_audit": 1,
    },
    "app/db/repo_statement_reviews.py::_reopen_projection_if_needed": {
        "update:statement_reviews": 1,
    },
    "app/db/repo_statement_reviews.py::_set_row_disposition": {
        "update:statement_lines": 1,
    },
    "app/db/repo_statement_reviews.py::add_row": {
        "insert:statement_field_evidence": 1,
        "insert:statement_lines": 1,
    },
    "app/db/repo_statement_reviews.py::approve": {
        "update:statement_reviews": 1,
    },
    "app/db/repo_statement_reviews.py::correct_row": {
        "update:statement_lines": 1,
    },
    "app/db/repo_statement_reviews.py::create_from_extraction": {
        "insert:statement_field_evidence": 1,
        "insert:statement_review_audit": 1,
        "insert:statement_review_pages": 1,
        "insert:statement_reviews": 1,
        "insert:statement_source_anchors": 1,
    },
    "app/db/repo_statement_reviews.py::record_row_evidence": {
        "insert:statement_field_evidence": 1,
    },
    "app/db/repo_statement_reviews.py::update_metadata": {
        "insert:statement_field_evidence": 1,
        "update:statement_lines": 1,
        "update:statement_reviews": 1,
    },
    "app/db/repo_statements.py::_recompute_row_hashes": {
        "insert:statement_review_audit": 1,
        "update:statement_lines": 2,
    },
    "app/db/repo_statements.py::mark_cleared": {
        "update:transactions": 2,
    },
    "app/db/repo_statements.py::resolve_document_account": {
        "update:statement_lines": 1,
    },
    "app/db/repo_statements.py::set_flow_kind": {
        "update:statement_lines": 1,
    },
    "app/db/repo_statements.py::set_match": {
        "update:statement_lines": 1,
    },
    "app/db/repo_statements.py::stage_lines": {
        "insert:statement_lines": 1,
    },
    "app/db/repo_structured_imports.py::create_preview": {
        "insert:structured_statement_import_audit": 1,
        "insert:structured_statement_imports": 1,
    },
    "app/db/repo_structured_imports.py::finish_import": {
        "insert:structured_statement_import_audit": 1,
        "update:structured_statement_imports": 1,
    },
    "app/ingest/structured/service.py::_create_review": {
        "insert:statement_field_evidence": 1,
        "insert:statement_review_audit": 1,
        "insert:statement_reviews": 1,
        "insert:statement_source_anchors": 1,
    },
    "app/ingest/structured/service.py::confirm_import": {
        "insert:statement_field_evidence": 1,
    },
    "app/reconcile/adjust.py::create_adjustment": {
        "update:transaction_splits": 1,
        "update:transactions": 1,
    },
    "app/reconcile/apply.py::unreconcile_document": {
        "delete:transactions": 1,
        "update:transactions": 2,
    },
    "app/reconcile/engine.py::promote_from_line": {
        "update:transactions": 1,
    },
    "app/reconcile/positive_flows.py::_restore_subject_splits": {
        "update:transaction_splits": 1,
    },
    "app/reconcile/positive_flows.py::_retag_subject_splits": {
        "update:transaction_splits": 1,
    },
    "app/reconcile/positive_flows.py::accept_classification": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/reconcile/positive_flows.py::accept_pair": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/reconcile/positive_flows.py::recover_positive_line": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/reconcile/positive_flows.py::reject_proposal": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/reconcile/positive_flows.py::restore_proposal": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/reconcile/positive_flows.py::undo_acceptance": {
        "insert:positive_flow_decision_events": 1,
    },
    "app/web/routes/ledger.py::delete_txn": {
        "delete:transactions": 1,
    },
    "app/web/routes/ledger.py::update_txn": {
        "update:transaction_splits": 1,
        "update:transactions": 2,
    },
    "app/web/routes/manage.py::set_opening_balance": {
        "update:transaction_splits": 1,
        "update:transactions": 1,
    },
    "app/web/routes/review.py::approve": {
        "update:transaction_splits": 1,
    },
}

# A non-literal SQL expression is not assumed read-only.  Every existing site
# is counted so a newly assembled query is forced through review.  The central
# period policy repository is intentionally omitted because it is the sole
# capability boundary and is checked separately for state ownership.
EXPECTED_DYNAMIC_SQL_SITES = {
    "app/accounting/contract.py::assert_golden_month": 4,
    "app/accounting/contract.py::ledger_invariant_violations": 2,
    "app/agents/chat/tools.py::query_finances": 4,
    "app/agents/recon_analyst/evidence.py::resolve_statement_run": 1,
    "app/agents/recon_analyst/tools.py::receipt_matcher_tool": 1,
    "app/agents/recon_analyst/tools.py::statement_auditor_tool": 2,
    "app/api/service.py::_latest_month": 1,
    "app/backlog/suggest.py::list_uncategorized_expense_backlog": 1,
    "app/db/migrate.py::apply_migrations": 1,
    "app/db/migrate.py::seed_sample": 1,
    "app/db/repo_actions.py::list_proposals": 1,
    "app/db/repo_assertions.py::list_assertions": 1,
    "app/db/repo_budgets.py::_apply_planning_action_fields": 1,
    "app/db/repo_budgets.py::planning_statement_evidence_lines": 1,
    "app/db/repo_captures.py::capture_metrics": 13,
    "app/db/repo_period_statements.py::split_rows_for_transactions": 1,
    "app/db/repo_period_statements.py::statement_lines_through": 1,
    "app/db/repo_recon_coverage.py::_breakdown": 1,
    "app/db/repo_recon_coverage.py::_category_breakdown": 1,
    "app/db/repo_recon_coverage.py::attention_lines": 1,
    "app/db/repo_recon_coverage.py::coverage_dashboard": 3,
    "app/db/repo_statement_expectations.py::_update_expectation": 1,
    "app/db/repo_statement_reviews.py::update_metadata": 1,
    "app/db/repo_statements.py::lines_for_document": 1,
    "app/db/views.py::activity_results": 1,
    "app/db/views.py::dashboard_context": 3,
    "app/db/views.py::filter_options": 2,
    "app/evals/recon_insight.py::_existing_ids": 1,
    "app/evals/recon_insight.py::_rows_for_query": 1,
    "app/evals/recon_insight.py::coverage_ground_truth": 1,
    "app/evals/recon_insight.py::db_snapshot": 2,
    "app/web/routes/actions.py::_statement_line_months": 1,
}


def _qualified_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    cursor = node
    while cursor in parents:
        cursor = parents[cursor]
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(cursor.name)
    return ".".join(reversed(names)) or "<module>"


def _static_sql(expression: ast.AST) -> tuple[str | None, bool]:
    """Return recoverable SQL plus whether any part was dynamically assembled."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value, False
    if isinstance(expression, ast.JoinedStr):
        chunks: list[str] = []
        for value in expression.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                chunks.append(value.value)
            else:
                chunks.append("{}")
        return "".join(chunks), True
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left, left_dynamic = _static_sql(expression.left)
        right, right_dynamic = _static_sql(expression.right)
        if left is not None and right is not None:
            return left + right, left_dynamic or right_dynamic
    return None, True


def _operation(verb: str, table: str) -> str:
    normalized = verb.lower()
    if normalized.startswith(("insert", "replace")):
        return f"insert:{table.lower()}"
    if normalized.startswith("delete"):
        return f"delete:{table.lower()}"
    return f"update:{table.lower()}"


def _source_inventory() -> tuple[
    dict[str, dict[str, int]],
    dict[str, int],
    list[tuple[str, str, str]],
]:
    sensitive: dict[str, Counter[str]] = defaultdict(Counter)
    dynamic: Counter[str] = Counter()
    state_writes: list[tuple[str, str, str]] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not node.args
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr not in SQL_CALLS
            ):
                continue
            function = _qualified_function(node, parents)
            site = f"{relative}::{function}"
            sql, is_dynamic = _static_sql(node.args[0])
            if is_dynamic and relative != "app/db/repo_period_policy.py":
                dynamic[site] += 1
            if sql is None:
                continue
            for match in MUTATION_RE.finditer(sql):
                table = match.group("table").lower()
                operation = _operation(match.group("verb"), table)
                if table in SENSITIVE_TABLES:
                    sensitive[site][operation] += 1
                    # SQLite UPSERT may execute an UPDATE even though the source
                    # statement begins with INSERT.
                    if operation.startswith("insert:") and UPSERT_RE.search(sql):
                        sensitive[site][f"update:{table}"] += 1
                if table in PERIOD_STATE_TABLES or table.startswith("period_close_"):
                    state_writes.append((relative, function, operation))

    return (
        {site: dict(sorted(operations.items())) for site, operations in sensitive.items()},
        dict(sorted(dynamic.items())),
        sorted(state_writes),
    )


def test_sensitive_sql_writer_inventory_is_explicit_and_complete():
    actual, _, _ = _source_inventory()
    assert actual == EXPECTED_SENSITIVE_WRITERS, (
        "The period-bound SQL writer surface changed. Route the write through "
        "an existing guarded repository or review and update the explicit "
        f"allowlist.\nactual={actual!r}"
    )


def test_dynamic_sql_surface_is_explicit_and_complete():
    _, actual, _ = _source_inventory()
    assert actual == EXPECTED_DYNAMIC_SQL_SITES, (
        "A dynamic SQLite call was added or removed. Treat it as a possible "
        "period-policy bypass until its table and mutation behavior are "
        f"reviewed.\nactual={actual!r}"
    )


def test_only_policy_repositories_write_period_close_state():
    _, _, state_writes = _source_inventory()
    violations = [
        (path, function, operation)
        for path, function, operation in state_writes
        if path not in PERIOD_STATE_WRITER_MODULES
    ]
    assert not violations, (
        "Period-close state is a single capability boundary; direct writes "
        f"outside the policy repositories are forbidden: {violations!r}"
    )


# Deleting a transaction cascades through splits, deleting a statement document
# cascades through its staged lines, and deleting an account cascades through
# its balance assertions. Those implicit writes must meet the same fail-closed
# boundary even though no Python DELETE names the child.
CASCADE_SENSITIVE_OPERATIONS = frozenset(
    {
        "delete:account_balance_assertions",
        "delete:statement_lines",
        "delete:transaction_splits",
    }
)


def test_every_sensitive_sql_operation_has_a_fail_closed_database_trigger(empty_db):
    actual, _, _ = _source_inventory()
    required_operations = {
        operation
        for operations in actual.values()
        for operation in operations
    } | CASCADE_SENSITIVE_OPERATIONS

    with engine.read_conn(empty_db) as conn:
        trigger_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }

    required_triggers = {
        f"period_policy_guard_{table}_{verb}"
        for verb, table in (operation.split(":", 1) for operation in required_operations)
    }
    missing = sorted(required_triggers - trigger_names)
    assert not missing, (
        "Every reviewed direct or cascading sensitive operation needs a "
        "conventionally named DB trigger so queued jobs, admin deletes, and "
        f"future call paths fail closed: {missing!r}"
    )
