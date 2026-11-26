"""Email alert evidence and read-only mailbox progress. No ledger writers here."""
from __future__ import annotations

import json

from ..banking.client import BankingError
from . import repo_period_policy

PAGE_SIZE = 30


def cursor(conn, source_key):
    row = conn.execute('SELECT uid_validity,last_uid FROM rogers_alert_mailboxes WHERE source_key=?',
                       (source_key,)).fetchone()
    return tuple(row) if row is not None else None


def advance(conn, source_key, expected, current, *, alert=None, rejected=False, retry_at=None):
    """Advance atomically with evidence. The caller holds BEGIN IMMEDIATE."""
    if cursor(conn, source_key) != expected:
        raise BankingError('mailbox_cursor_changed')
    if expected is not None and expected[0] == current[0] and current[1] <= expected[1]:
        raise BankingError('mailbox_cursor_changed')
    conn.execute('''INSERT INTO rogers_alert_mailboxes(source_key,uid_validity,last_uid) VALUES (?,?,?)
        ON CONFLICT(source_key) DO UPDATE SET uid_validity=excluded.uid_validity,last_uid=excluded.last_uid''',
        (source_key, *current))
    if expected and expected[0] != current[0]:
        # Old UIDs are no longer meaningful. The new epoch is re-scanned.
        conn.execute('DELETE FROM rogers_alert_retries WHERE source_key=? AND uid_validity<>?',
                     (source_key, current[0]))
    if retry_at is not None:
        conn.execute('INSERT INTO rogers_alert_retries(source_key,uid_validity,uid,retry_at) VALUES (?,?,?,?)',
                     (source_key, *current, retry_at))
        return 'deferred'
    return _record(conn, source_key, alert, rejected)


def due_retries(conn, source_key, validity, now, limit):
    return [r['uid'] for r in conn.execute('''SELECT uid FROM rogers_alert_retries
        WHERE source_key=? AND uid_validity=? AND retry_at<=? ORDER BY retry_at,uid LIMIT ?''',
        (source_key, validity, now, limit))]


def settle_retry(conn, source_key, expected, uid, *, alert=None, rejected=False, retry_at=None):
    if cursor(conn, source_key) != expected:
        raise BankingError('mailbox_cursor_changed')
    key = (source_key, expected[0], uid)
    if retry_at is not None:
        changed = conn.execute('''UPDATE rogers_alert_retries SET retry_at=?
            WHERE source_key=? AND uid_validity=? AND uid=?''', (retry_at, *key)).rowcount
    else:
        changed = conn.execute('DELETE FROM rogers_alert_retries WHERE source_key=? AND uid_validity=? AND uid=?',
                               key).rowcount
    if changed != 1:
        raise BankingError('mailbox_cursor_changed')
    return 'deferred' if retry_at is not None else _record(conn, source_key, alert, rejected)


def _record(conn, source_key, alert, rejected):
    if rejected:
        conn.execute('UPDATE rogers_alert_mailboxes SET rejected_count=rejected_count+1 WHERE source_key=?',
                     (source_key,))
        return 'rejected'
    if alert is None:
        return 'skipped'
    fields = (alert.authorized_on, alert.card_last4, alert.amount_cents, alert.currency, alert.merchant)
    original = conn.execute('''SELECT id,authorized_on,card_last4,amount_cents,currency,merchant
        FROM rogers_alerts WHERE message_key=?''', (alert.message_key,)).fetchone()
    if original is None:
        alert_id = conn.execute('''INSERT INTO rogers_alerts
            (source_key,message_key,authorized_on,card_last4,amount_cents,currency,merchant) VALUES (?,?,?,?,?,?,?)''',
            (source_key, alert.message_key, *fields)).lastrowid
        result = 'staged'
    elif tuple(original)[1:] == fields:
        alert_id = original['id']
        result = 'duplicate'
    else:
        conn.execute('UPDATE rogers_alert_mailboxes SET conflict_count=conflict_count+1 WHERE source_key=?',
                     (source_key,))
        return 'identity_conflict'
    conn.execute('''INSERT INTO rogers_alert_occurrences(alert_id,source_key) SELECT ?,?
        WHERE NOT EXISTS (SELECT 1 FROM rogers_alert_occurrences WHERE alert_id=? AND source_key=?)''',
        (alert_id, source_key, alert_id, source_key))
    return result


def counts(conn, source_key, *, scopes=None):
    keys = tuple(scopes) if scopes is not None else (source_key,)
    row = conn.execute('''SELECT COALESCE(SUM(m.rejected_count),0) AS rejected_count,
        COALESCE(SUM(m.conflict_count),0) AS conflict_count,
        COALESCE(SUM((SELECT COUNT(*) FROM rogers_alert_retries r
          WHERE r.source_key=m.source_key AND r.uid_validity=m.uid_validity)),0) AS deferred_count
        FROM rogers_alert_mailboxes m WHERE m.source_key IN (SELECT value FROM json_each(?))''',
        (json.dumps(keys),)).fetchone()
    return dict(row)


def _review_scopes(source_key, cards, scopes):
    if scopes is not None:
        return scopes  # An explicit empty mapping retires all authority.
    return {source_key: cards} if source_key is not None else {}


def _account(conn, evidence, scopes):
    mappings = {scope: account_id for scope, cards in scopes.items()
                if (account_id := cards.get(evidence['card_last4'])) is not None}
    if not mappings:
        return None
    rows = conn.execute('''SELECT id,name FROM accounts
        WHERE currency=? AND kind='credit' AND is_active=1 AND id IN (
            SELECT c.value FROM json_each(?) c JOIN rogers_alert_occurrences o ON o.source_key=c.key
            WHERE o.alert_id=?
        ) LIMIT 2''', (evidence['currency'], json.dumps(mappings), evidence['id'])).fetchall()
    # Repeated evidence for one account agrees; two eligible accounts are ambiguous.
    return rows[0] if len(rows) == 1 else None


def list_alerts(conn, source_key, cards, before_id=None, *, scopes=None):
    scopes = _review_scopes(source_key, cards, scopes)
    rows = conn.execute('''SELECT e.id,e.authorized_on,e.card_last4,e.amount_cents,e.currency,e.merchant,
        r.id AS review_id,r.decision,r.reason,a.name AS reviewed_account
        FROM rogers_alerts e LEFT JOIN rogers_alert_reviews r ON r.id=(
            SELECT MAX(id) FROM rogers_alert_reviews WHERE alert_id=e.id)
        LEFT JOIN accounts a ON a.id=r.account_id
        WHERE (? IS NULL OR e.id<?) ORDER BY e.id DESC LIMIT ?''', (before_id, before_id, PAGE_SIZE + 1))
    result = []
    for row in rows:
        account = _account(conn, row, scopes)
        item = dict(row)
        item['suggested_account_id'] = account['id'] if account else None
        item['suggested_account'] = account['name'] if account else None
        result.append(item)
    return result


def decide(conn, alert_id, expected_review_id, action, account_id, reason, source_key, cards, *, scopes=None):
    if not reason.strip() or len(reason) > 500 or action not in {'confirm', 'dismiss'}:
        raise BankingError('invalid_alert_review')
    if (action == 'confirm') != (account_id is not None):
        raise BankingError('invalid_alert_review')
    row = conn.execute('''SELECT id,authorized_on,card_last4,currency FROM rogers_alerts
        WHERE id=?''', (alert_id,)).fetchone()
    if row is None:
        raise BankingError('alert_not_found')
    latest = conn.execute('''SELECT id,previous_id,decision,account_id,reason FROM rogers_alert_reviews
        WHERE alert_id=? ORDER BY id DESC LIMIT 1''', (alert_id,)).fetchone()
    previous_id = expected_review_id or None
    replay_id = latest['id'] if latest and tuple(latest)[1:] == (previous_id, action, account_id, reason) else None
    if replay_id is None and expected_review_id != (latest['id'] if latest else 0):
        raise BankingError('stale_alert_review')
    if action == 'confirm':
        account = _account(conn, row, _review_scopes(source_key, cards, scopes))
        if account is None or account['id'] != account_id:
            raise BankingError('alert_account_changed')
    if replay_id is not None:
        return {'status': action, 'review_id': replay_id}
    repo_period_policy.guard_months(conn, [row['authorized_on'][:7]])
    ident = conn.execute('''INSERT INTO rogers_alert_reviews(alert_id,previous_id,decision,account_id,reason)
        VALUES (?,?,?,?,?)''', (alert_id, previous_id, action, account_id, reason)).lastrowid
    return {'status': action, 'review_id': ident}
