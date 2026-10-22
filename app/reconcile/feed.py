"""Reviewed bank subjects. All ledger writes reuse signed-split and period services."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta

from ..db import repo_ledger, repo_period_policy
from ..banking.client import BankingError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def plan(conn: sqlite3.Connection, subject_id: int, home_currency: str) -> dict:
    subject = conn.execute('''SELECT s.id,s.observation_id,o.* FROM bank_subjects s
        JOIN bank_observations o ON o.id=s.observation_id WHERE s.id=?''', (subject_id,)).fetchone()
    if subject is None:
        raise BankingError('subject_not_found')
    o = dict(subject)
    # SELECT duplicate id is subject id; observation identity is explicit.
    o['id'] = subject_id
    binding = conn.execute('''SELECT b.*, a.currency,a.is_active FROM bank_accounts ba
        JOIN v_bank_bindings b ON b.bank_account_id=ba.id JOIN accounts a ON a.id=b.account_id
        WHERE ba.connection_id=? AND ba.provider_account_id=?''',
        (o['connection_id'], o['provider_account_id'])).fetchone()
    b = dict(binding) if binding else None
    blockers = []
    if not b or not b['included'] or not b['is_active']:
        blockers.append('account_not_included')
    if o['pending']:
        blockers.append('pending')
    if o['kind'] == 'removed':
        blockers.append('removed')
    if o['currency'] != home_currency or (b and b['currency'] != home_currency):
        blockers.append('currency_review')
    association = conn.execute('SELECT * FROM v_bank_associations WHERE subject_id=?', (subject_id,)).fetchone()
    if association:
        blockers.append('already_associated' if association['observation_id'] == o['observation_id'] else 'changed_associated_evidence')
    candidates = []
    if b and o['amount_cents'] is not None:
        center = date.fromisoformat(o['posted_on'])
        candidates = [dict(r) for r in conn.execute('''SELECT t.*,
            (SELECT group_concat(id) FROM transaction_splits WHERE transaction_id=t.id) AS split_ids
            FROM transactions t WHERE account_id=? AND amount_cents=? AND posted_on BETWEEN ? AND ? ORDER BY posted_on,id''',
            (b['account_id'], o['amount_cents'], (center-timedelta(days=7)).isoformat(), (center+timedelta(days=7)).isoformat()))]
    for candidate in candidates:
        candidate['splits'] = [dict(r) for r in conn.execute(
            'SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id', (candidate['id'],))]
    result = {'subject': o, 'binding': b, 'association': dict(association) if association else None,
              'candidates': candidates, 'blockers': blockers}
    result['fingerprint'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result


def decide(conn, subject_id, *, home_currency, fingerprint, operation_key, action,
           reason, flow_kind='unknown', transaction_id=None, acknowledge_candidates=False):
    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key)>180:
        raise BankingError('operation_key_required')
    if not isinstance(reason, str) or not reason.strip() or len(reason)>500:
        raise BankingError('reason_required')
    request = canonical(dict(subject_id=subject_id, fingerprint=fingerprint, action=action, reason=reason,
        flow_kind=flow_kind, transaction_id=transaction_id, acknowledge_candidates=acknowledge_candidates))
    replay = conn.execute('SELECT * FROM bank_operations WHERE operation_key=?', (operation_key,)).fetchone()
    if replay:
        if replay['request_json'] != request:
            raise BankingError('operation_key_conflict')
        return json.loads(replay['result_json'])
    p = plan(conn, subject_id, home_currency)
    if p['fingerprint'] != fingerprint:
        raise BankingError('stale_review_plan')
    o, b = p['subject'], p['binding']
    actor = 'operator:household'
    if action == 'revoke':
        a = p['association']
        if not a:
            raise BankingError('association_not_found')
        repo_period_policy.guard_transaction_write(conn, a['transaction_id'], extra_month=o['posted_on'][:7])
        conn.execute('INSERT INTO bank_revocations(association_id,actor,reason,operation_key) VALUES (?,?,?,?)',
                     (a['id'], actor, reason, operation_key))
        result = {'status': 'revoked', 'transaction_id': a['transaction_id']}
    elif action == 'exclude':
        if p['association']:
            raise BankingError('revoke_association_first')
        repo_period_policy.guard_months(conn, [o['posted_on'][:7]])
        conn.execute('INSERT INTO bank_exclusions(subject_id,observation_id,actor,reason,operation_key) VALUES (?,?,?,?,?)',
                     (subject_id, o['observation_id'], actor, reason, operation_key))
        result = {'status': 'excluded'}
    else:
        if p['blockers']:
            raise BankingError(p['blockers'][0])
        repo_period_policy.guard_months(conn, [o['posted_on'][:7]])
        if action == 'match':
            if transaction_id not in [c['id'] for c in p['candidates']]:
                raise BankingError('invalid_match_target')
            repo_period_policy.guard_transaction_write(conn, transaction_id)
        elif action == 'new':
            if b['role'] != 'primary':
                raise BankingError('secondary_feed_cannot_book')
            if p['candidates'] and acknowledge_candidates is not True:
                raise BankingError('candidate_acknowledgement_required')
            if flow_kind not in {'purchase','fee','income','interest'}:
                raise BankingError('flow_requires_reviewed_relationship')
            from ..accounting.flows import validate_flow_amount
            try:
                validate_flow_amount(flow_kind, o['amount_cents'])
            except ValueError:
                raise BankingError('flow_amount_mismatch') from None
            transaction_id = repo_ledger.insert_transaction(conn, account_id=b['account_id'], posted_on=o['posted_on'],
                description=o['description'], counterparty=o['merchant'], amount_cents=o['amount_cents'],
                source='bank_feed', external_id=f"subject:{subject_id}", source_document_id=None,
                source_confidence=1, flow_kind=flow_kind)
            if transaction_id is None:
                raise BankingError('booking_exists_review_required')
            repo_ledger.insert_split(conn, transaction_id=transaction_id,
                category_id=repo_ledger.ensure_uncategorized(conn), amount_cents=o['amount_cents'])
        else:
            raise BankingError('invalid_decision')
        result = {'status': 'matched' if action == 'match' else 'booked', 'transaction_id': transaction_id}
    conn.execute('INSERT INTO bank_operations(operation_key,request_json,result_json,actor,reason) VALUES (?,?,?,?,?)',
                 (operation_key, request, canonical(result), actor, reason))
    if action in {'new','match'}:
        conn.execute('''INSERT INTO bank_associations(subject_id,observation_id,binding_id,transaction_id,operation_key,actor,reason)
            VALUES (?,?,?,?,?,?,?)''', (subject_id, o['observation_id'], b['id'], transaction_id, operation_key, actor, reason))
    return result


def receipt_candidates(conn, *, amount_cents: int, posted_on: str,
                       home_currency: str | None = None) -> list[dict]:
    """All equal recent statement/feed evidence, never ranked or auto-associated.

    Account hints do not establish bank identity. Cleared statement origins and
    historical feed associations remain eligible for explicit receipt review.
    """
    center = date.fromisoformat(posted_on)
    return [dict(r) for r in conn.execute('''SELECT t.* FROM transactions t
        JOIN accounts a ON a.id=t.account_id
        WHERE (t.source IN ('bank_feed','statement') OR EXISTS (
            SELECT 1 FROM bank_associations ba WHERE ba.transaction_id=t.id))
        AND a.currency GLOB '[A-Z][A-Z][A-Z]'
        AND (? IS NULL OR a.currency=?)
        AND t.amount_cents=? AND t.posted_on BETWEEN ? AND ? ORDER BY t.posted_on,t.id''',
        (home_currency, home_currency, amount_cents,
         (center-timedelta(days=7)).isoformat(), (center+timedelta(days=7)).isoformat()))]


def attach_receipt(conn, *, extraction_id: int, transaction_id: int, operation_key: str, reason: str,
                   home_currency: str):
    from ..ingest.schemas import ExtractedReceipt
    from ..db import repo_documents
    existing = conn.execute('SELECT * FROM bank_receipt_evidence WHERE extraction_id=?', (extraction_id,)).fetchone()
    if existing:
        if existing['transaction_id'] != transaction_id or existing['operation_key'] != operation_key or existing['reason'] != reason:
            raise BankingError('receipt_evidence_conflict')
        return {'status': 'attached', 'transaction_id': transaction_id}
    if not operation_key.strip() or len(operation_key)>180 or not reason.strip() or len(reason)>500:
        raise BankingError('reason_and_operation_required')
    extraction = conn.execute('SELECT * FROM ingest_extractions WHERE id=?', (extraction_id,)).fetchone()
    if not extraction or extraction['doc_kind'] != 'receipt' or extraction['review_status'] != 'pending' or extraction['transaction_id']:
        raise BankingError('receipt_not_pending')
    try:
        receipt = ExtractedReceipt.model_validate_json(extraction['extracted_json'])
        receipt_day = date.fromisoformat(receipt.purchased_on)
        if receipt_day.isoformat() != receipt.purchased_on:
            raise ValueError('noncanonical receipt date')
    except ValueError:
        raise BankingError('invalid_receipt_extraction') from None
    if receipt.currency != home_currency or not receipt.total_cents:
        raise BankingError('currency_review')
    candidates = receipt_candidates(conn, amount_cents=-abs(receipt.total_cents),
                                    posted_on=receipt.purchased_on, home_currency=home_currency)
    if transaction_id not in [r['id'] for r in candidates]:
        raise BankingError('invalid_receipt_target')
    repo_period_policy.guard_transaction_write(conn, transaction_id, extra_month=receipt.purchased_on[:7])
    conn.execute('''INSERT INTO bank_receipt_evidence(extraction_id,source_document_id,transaction_id,actor,reason,operation_key)
        VALUES (?,?,?,'operator:household',?,?)''', (extraction_id, extraction['source_document_id'], transaction_id, reason, operation_key))
    repo_documents.link_extraction_txn(conn, extraction_id, transaction_id, review_status='approved')
    repo_documents.set_status(conn, extraction['source_document_id'], 'processed')
    # No category assertion: attaching a receipt does not approve its model guess.
    return {'status': 'attached', 'transaction_id': transaction_id}
