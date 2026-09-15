"""reconcile_document: the sole promoter of statement_lines into transactions.

Two-phase per document: phase 1 (inside a write_tx) auto-matches/promotes what it can
and snapshots the ambiguous residue; the one optional batched LLM call happens with NO
lock held; phase 2 (a second write_tx) applies the LLM decisions and finalizes doc status.
Re-run safety: only 'unmatched' lines are ever selected, so a repeat call is a no-op.
"""
from __future__ import annotations

import sqlite3

from langchain_core.messages import HumanMessage, SystemMessage

from ..accounting.contract import currency_review_reason
from ..config import get_settings
from ..db import engine as db_engine
from ..db import (
    repo_actions,
    repo_documents,
    repo_embeddings,
    repo_ledger,
    repo_merchant_knowledge,
    repo_statement_expectations,
    repo_statements,
)
from .merchant_resolution import DescriptorResolution, resolve_descriptor
from .schemas import ReconBatchDecision, ReconDecision
from .scoring import AUTO_MERCHANT, LLM_CONF, composite, date_score, merchant_score
from .automation_policy import decision_for


def _txn_merchant(txn: sqlite3.Row) -> str:
    return txn["counterparty"] or txn["description"]


def _candidates(conn: sqlite3.Connection, line: sqlite3.Row, claimed: set[int]) -> list[sqlite3.Row]:
    # Receipts are the only txns with unreliable accounts (default-account guess at
    # capture time); anything else must stay confined to the line's own account, or a
    # line from account X could clear/reassign a txn deliberately recorded in account Y.
    rows = conn.execute(
        """SELECT * FROM transactions
           WHERE recon_status='uncleared'
             AND amount_cents=?
             AND posted_on BETWEEN date(?, '-7 day') AND date(?, '+1 day')
             AND (source = 'receipt' OR account_id = ?)
             AND id NOT IN (
               SELECT matched_transaction_id FROM statement_lines
               WHERE matched_transaction_id IS NOT NULL
                 AND review_disposition='active'
             )
           ORDER BY id""",
        (line["amount_cents"], line["posted_on"], line["posted_on"], line["account_id"]),
    ).fetchall()
    return [r for r in rows if r["id"] not in claimed]


def _fallback_category(conn: sqlite3.Connection, amount_cents: int) -> int:
    """Use the neutral review fallback until flow meaning is explicitly accepted.

    A positive sign cannot mint an income-purpose category.  FN-144 acceptance
    retags the unchanged split to income, expense-offset, or transfer purpose in
    the same transaction as the audited flow decision.
    """
    del amount_cents
    return repo_ledger.ensure_uncategorized(conn)


def promote_from_line(conn: sqlite3.Connection, line: sqlite3.Row) -> int | None:
    """Shared promotion routine: turn one statement line into a cleared ledger txn+split.

    Returns the new transaction id, or None if (source, external_id) already exists (the
    line is marked needs_review 'promotion collided' in that case).
    """
    settings = get_settings()
    currency_reason = currency_review_reason(line["currency"], settings.home_currency)
    if currency_reason is None and line["account_id"] is not None:
        account = conn.execute(
            "SELECT currency FROM accounts WHERE id=?", (line["account_id"],)
        ).fetchone()
        currency_reason = currency_review_reason(
            account["currency"] if account is not None else None,
            settings.home_currency,
        )
    if currency_reason is not None:
        repo_statements.set_match(
            conn,
            line["id"],
            status="needs_review",
            rationale=f"{currency_reason} — promotion blocked",
        )
        return None

    resolution: DescriptorResolution | None = None
    if int(line["amount_cents"]) < 0:
        resolution = resolve_descriptor(
            conn,
            descriptor=str(line["raw_description"] or ""),
            scope=repo_merchant_knowledge.scope_for_statement_line(
                conn,
                int(line["id"]),
            ),
        )
    category_id = _fallback_category(conn, line["amount_cents"])
    if (
        resolution is not None
        and resolution.category.status == "resolved"
        and resolution.category.target_id is not None
        and resolution.category.automatic_assignment_allowed
    ):
        category_id = int(resolution.category.target_id)

    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=line["account_id"],
        posted_on=line["posted_on"],
        description=line["raw_description"],
        counterparty="",
        amount_cents=line["amount_cents"],
        source="statement",
        external_id=line["row_hash"],
        source_document_id=line["source_document_id"],
        source_confidence=1.0,
        # Positive direction is never sufficient classification evidence.  A
        # promoted credit enters the explicit FN-144 review lifecycle even if a
        # stale/direct statement-line writer attempted to pre-tag it.
        flow_kind=(
            "unknown"
            if int(line["amount_cents"]) > 0
            else line["flow_kind"]
        ),
    )
    if txn_id is None:
        repo_statements.set_match(conn, line["id"], status="needs_review", rationale="promotion collided")
        return None

    split_id = repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=line["amount_cents"],
    )
    if (
        resolution is not None
        and resolution.category.status == "resolved"
        and resolution.category.target_id is not None
        and not resolution.category.automatic_assignment_allowed
    ):
        repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={
                "transaction_id": txn_id,
                "to_category_id": int(resolution.category.target_id),
            },
            evidence={
                "transaction_ids": [txn_id],
                "transaction_split_ids": [split_id],
                "statement_line_ids": [int(line["id"])],
                "category_ids": [int(resolution.category.target_id)],
                "trusted_claim_ids": list(resolution.category.claim_ids),
                "knowledge_version": resolution.knowledge_version,
                "source": "trusted_local_knowledge",
            },
            confidence=1.0,
            rationale=(
                "Accepted local merchant knowledge suggests this category; "
                "operator approval is required."
            ),
            agent_run_id="merchant-resolution",
        )
    conn.execute("UPDATE transactions SET recon_status='cleared', cleared_on=? WHERE id=?",
                (line["posted_on"], txn_id))
    repo_statements.set_match(conn, line["id"], status="promoted", transaction_id=txn_id)
    repo_embeddings.enqueue_embed_transactions(conn)
    return txn_id


def _finalize_doc_status(conn: sqlite3.Connection, source_document_id: int) -> None:
    remaining = conn.execute(
        """SELECT COUNT(*) FROM statement_lines
           WHERE source_document_id=? AND match_status='needs_review'
             AND review_disposition='active'""",
        (source_document_id,),
    ).fetchone()[0]
    repo_documents.set_status(conn, source_document_id, "needs_review" if remaining else "matched")


def _llm_messages(queued: list[dict]) -> list:
    parts = []
    for item in queued:
        line = item["line"]
        parts.append(
            f"LINE {line['id']}: date={line['posted_on']} desc={line['raw_description']!r} "
            f"amount_cents={line['amount_cents']}"
        )
        for cand in item["candidates"]:
            t = cand["txn"]
            parts.append(
                f"  candidate TXN {t['id']}: date={t['posted_on']} "
                f"desc={_txn_merchant(t)!r} amount_cents={t['amount_cents']} "
                f"merchant_score={cand['merchant']:.2f}"
            )
    prompt = (
        "Each LINE is a bank/card statement row awaiting reconciliation. Each candidate TXN "
        "already has the same signed amount and sits within the line's date window. Decide, "
        "for each LINE, which candidate TXN (if any) is the SAME real-world purchase. If none "
        "of the candidates corroborate the line, set transaction_id to null for that line. "
        "Return one decision per LINE.\n\n" + "\n".join(parts)
    )
    return [
        SystemMessage(content="You reconcile bank/card statement lines against candidate ledger transactions."),
        HumanMessage(content=prompt),
    ]


def _run_llm(llm, queued: list[dict], settings) -> dict[int, ReconDecision] | None:
    messages = _llm_messages(queued)
    try:
        structured = llm.with_structured_output(ReconBatchDecision, method=settings.structured_method)
        result = structured.invoke(messages)
    except Exception:
        return None
    return {d.statement_line_id: d for d in result.decisions}


def reconcile_document(db_path, source_document_id: int, llm) -> dict:
    settings = get_settings()
    same_event_authority = decision_for("same_event")
    counts = {"matched": 0, "promoted": 0, "needs_review": 0, "ignored_pending": 0}
    queued: list[dict] = []
    # Shared across BOTH write_tx phases: a txn claimed by phase-1 auto-match (or by an
    # earlier decision in phase-2's own batch) must not be claimed again by the LLM residue.
    claimed: set[int] = set()

    with db_engine.write_tx(db_path) as conn:
        lines = repo_statements.lines_for_document(conn, source_document_id, match_status="unmatched")
        for line in lines:
            currency_reason = currency_review_reason(line["currency"], settings.home_currency)
            if currency_reason is not None:
                repo_statements.set_match(
                    conn,
                    line["id"],
                    status="needs_review",
                    rationale=f"{currency_reason} — promotion blocked",
                )
                counts["needs_review"] += 1
                continue
            if line["is_pending"]:
                repo_statements.set_match(conn, line["id"], status="needs_review",
                                          rationale="pending — will clear when posted")
                counts["ignored_pending"] += 1
                continue

            candidates = _candidates(conn, line, claimed)
            if not candidates:
                repo_statements.set_match(
                    conn,
                    line["id"],
                    status="needs_review",
                    rationale=(
                        "no same-event candidates; review and promote if this "
                        "is a new transaction"
                    ),
                )
                counts["needs_review"] += 1
                continue

            if not same_event_authority.allowed:
                repo_statements.set_match(
                    conn,
                    line["id"],
                    status="needs_review",
                    rationale=(
                        "same-event automation disabled; "
                        f"review {len(candidates)} candidate(s)"
                    ),
                )
                counts["needs_review"] += 1
                continue

            scored = [
                (c, merchant_score(line["raw_description"], _txn_merchant(c)),
                 date_score(line["posted_on"], c["posted_on"]))
                for c in candidates
            ]
            best_txn, best_merchant, best_date = max(scored, key=lambda t: t[1])

            if len(candidates) == 1 or best_merchant >= AUTO_MERCHANT:
                method = "exact" if best_merchant >= AUTO_MERCHANT else "heuristic"
                score = composite(1.0, best_merchant, best_date)
                repo_statements.set_match(
                    conn, line["id"], status="matched", method=method,
                    transaction_id=best_txn["id"], score=score,
                    rationale=f"amount+date match, merchant {best_merchant:.2f}",
                )
                # Only a receipt's account was ever a guess; same-account matches leave it be.
                resolved_account = line["account_id"] if best_txn["source"] == "receipt" else None
                repo_statements.mark_cleared(conn, best_txn["id"], line["posted_on"],
                                             account_id=resolved_account)
                claimed.add(best_txn["id"])
                counts["matched"] += 1
            else:
                queued.append({
                    "line": line,
                    "candidates": [{"txn": c, "merchant": m, "date": d} for c, m, d in scored],
                })

        if not queued:
            _finalize_doc_status(conn, source_document_id)
            repo_statement_expectations.sync_document_reconciliation(
                conn,
                source_document_id,
                actor="reconcile:engine",
                reason="document reconciliation completed",
            )

    if not queued:
        return counts

    decisions = _run_llm(llm, queued, settings)

    with db_engine.write_tx(db_path) as conn:
        for item in queued:
            line = item["line"]
            candidates = item["candidates"]
            candidate_ids = {c["txn"]["id"] for c in candidates}

            if decisions is None:
                repo_statements.set_match(conn, line["id"], status="needs_review", rationale="llm unavailable")
                counts["needs_review"] += 1
                continue

            decision = decisions.get(line["id"])
            if decision is None:
                repo_statements.set_match(conn, line["id"], status="needs_review",
                                          rationale="llm returned no decision")
                counts["needs_review"] += 1
                continue

            if decision.transaction_id is not None and decision.transaction_id not in candidate_ids:
                repo_statements.set_match(conn, line["id"], status="needs_review",
                                          rationale="llm returned invalid candidate")
                counts["needs_review"] += 1
                continue

            if decision.transaction_id is None:
                repo_statements.set_match(
                    conn,
                    line["id"],
                    status="needs_review",
                    rationale=(
                        decision.reason
                        or "no matching transaction proposed; review and promote if new"
                    ),
                )
                counts["needs_review"] += 1
                continue

            if decision.transaction_id in claimed:
                # Another line already claimed this txn this run — either an earlier
                # auto-match, or an earlier decision in this same LLM batch (a hallucinated
                # double-assignment). Never let two lines land on one transaction.
                repo_statements.set_match(conn, line["id"], status="needs_review",
                                          rationale="candidate already matched this run")
                counts["needs_review"] += 1
                continue

            if (
                decision.confidence >= LLM_CONF
                and same_event_authority.allowed
            ):
                match = next(c for c in candidates if c["txn"]["id"] == decision.transaction_id)
                score = composite(1.0, match["merchant"], match["date"])
                repo_statements.set_match(
                    conn, line["id"], status="matched", method="llm",
                    transaction_id=decision.transaction_id, score=score,
                    rationale=decision.reason or "llm match",
                )
                resolved_account = line["account_id"] if match["txn"]["source"] == "receipt" else None
                repo_statements.mark_cleared(conn, decision.transaction_id, line["posted_on"],
                                             account_id=resolved_account)
                claimed.add(decision.transaction_id)
                counts["matched"] += 1
            else:
                repo_statements.set_match(conn, line["id"], status="needs_review",
                                          rationale="llm low confidence")
                counts["needs_review"] += 1

        _finalize_doc_status(conn, source_document_id)
        repo_statement_expectations.sync_document_reconciliation(
            conn,
            source_document_id,
            actor="reconcile:engine",
            reason="document reconciliation decisions applied",
        )

    return counts
