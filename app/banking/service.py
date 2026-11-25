"""Canonical connection lifecycle and immutable complete-window ingestion."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import hashlib
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from datetime import datetime

import httpx

from ..db import engine, repo_budgets, repo_period_policy
from ..reconcile import feed
from .client import BankingError, PlaidClient
from .secrets import PrivateStore

logger = logging.getLogger('uvicorn.error.banking')

CONSENT_VERSION = 'plaid-transactions-v1'
CONSENT_TEXT = ('I authorize Plaid to access my selected bank accounts and transaction history for this household. '
    'The application sends connection identifiers and requests to Plaid in Production. '
    'Account access and inclusion are separate from economic ownership. Withdraw to stop further requests; '
    'disconnect while consent is active to revoke provider access. Retained evidence is not deleted.')


def text(value, maximum=500):
    if not isinstance(value, str) or not value or len(value)>maximum or any(ord(c)<32 for c in value):
        raise BankingError('invalid_provider_response')
    return value


def _safe_id(value, maximum=80):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:@-]{1,%d}' % maximum, value) else None


def _safe_label(value, maximum=80):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and all(ord(c) >= 32 for c in value) else None


def completion_tokens(session):
    """Public tokens from a Link session.

    Current integrations receive results.item_add_results; on_success is returned
    only for legacy integrations (Production returned results only, 2026-11-14).
    Malformed rows are ignored.
    """
    results = session.get('results')
    items = results.get('item_add_results') if isinstance(results, dict) else None
    found = {r['public_token'] for r in items if isinstance(r, dict) and isinstance(r.get('public_token'), str)
             and r['public_token']} if isinstance(items, list) else set()
    legacy = session.get('on_success')
    if isinstance(legacy, dict) and isinstance(legacy.get('public_token'), str) and legacy['public_token']:
        found.add(legacy['public_token'])
    return found


def link_exit(session):
    value = session.get('exit') if isinstance(session.get('exit'), dict) else session.get('on_exit')
    return value if isinstance(value, dict) else None


def link_diagnostics(response):
    """Content-free summary of /link/token/get: counts, ids and error codes only."""
    sessions = [s for s in response.get('link_sessions') or [] if isinstance(s, dict)]
    summary = []
    for s in sessions[:5]:
        results = s.get('results') if isinstance(s.get('results'), dict) else {}
        items = [i for i in results.get('item_add_results') or [] if isinstance(i, dict)]
        exit_ = link_exit(s) or {}
        error = exit_.get('error') if isinstance(exit_.get('error'), dict) else {}
        metadata = exit_.get('metadata') if isinstance(exit_.get('metadata'), dict) else {}
        events = []
        for event in s.get('events') or []:
            if not isinstance(event, dict) or event.get('event_name') not in {'ERROR', 'EXIT', 'SELECT_INSTITUTION', 'HANDOFF'}:
                continue
            meta = event.get('event_metadata') if isinstance(event.get('event_metadata'), dict) else {}
            events.append({k: v for k, v in {
                'event': _safe_id(event.get('event_name')), 'at': _safe_id(event.get('timestamp')),
                'institution_id': _safe_id(meta.get('institution_id')),
                'institution_name': _safe_label(meta.get('institution_name')),
                'error_code': _safe_id(meta.get('error_code')), 'error_type': _safe_id(meta.get('error_type')),
                'exit_status': _safe_id(meta.get('exit_status'))}.items() if v})
        summary.append({k: v for k, v in {
            'link_session_id': _safe_id(s.get('link_session_id')),
            'finished': bool(s.get('finished_at')),
            'items': len(items),
            'public_tokens': sum(1 for i in items if i.get('public_token')),
            'legacy_on_success': isinstance(s.get('on_success'), dict),
            'institutions': [i for i in (_safe_id((it.get('institution') or {}).get('institution_id'))
                                         if isinstance(it.get('institution'), dict) else None for it in items) if i],
            'exit_status': _safe_id(metadata.get('status')),
            'exit_error_code': _safe_id(error.get('error_code')),
            'events': sorted(events, key=lambda e: e.get('at', ''))[-12:],
        }.items() if v not in (None, [], False) or k in {'finished', 'items'}})
    return {'request_id': _safe_id(response.get('request_id')), 'plaid_sessions': len(sessions), 'sessions': summary}


class BankingService:
    def __init__(self, *, db_path: str | Path, private_dir: Path | None,
                 credentials_file: Path | None, origin: str, completion_uri: str,
                 enabled: Callable[[], bool], strict_local: Callable[[], bool],
                 read_only: Callable[[], bool], home_currency: str = 'CAD',
                 transport: httpx.BaseTransport | None = None):
        self.db_path = db_path
        self.private_dir = private_dir
        self.credentials_file = credentials_file
        self.origin = origin.rstrip('/')
        self.completion_uri = completion_uri
        self.enabled = enabled
        self.strict_local = strict_local
        self.read_only = read_only
        self.home_currency = home_currency
        self.transport = transport
        self._store = None
        self._finish_lock = threading.Lock()
        self._last_server_check = {}

    @property
    def store(self):
        if self.private_dir is None:
            raise BankingError('banking_not_configured')
        if self._store is None:
            self._store = PrivateStore(self.private_dir)
        return self._store

    def writable(self):
        if self.read_only():
            raise BankingError('read_only')

    def _check_consent(self, conn, member_id, consent_id=None):
        self.writable()
        if not self.enabled() or self.strict_local() or self.credentials_file is None:
            raise BankingError('egress_blocked')
        member = conn.execute('SELECT 1 FROM household_members WHERE id=? AND is_active=1', (member_id,)).fetchone()
        consent = conn.execute('SELECT * FROM bank_consents WHERE member_id=? ORDER BY id DESC LIMIT 1', (member_id,)).fetchone()
        if consent_id is not None and (not consent or consent['id'] != consent_id):
            raise BankingError('link_consent_changed_start_new_session')
        if not member or not consent or not consent['granted'] or consent['version'] != CONSENT_VERSION:
            raise BankingError('current_member_consent_required')

    def _policy(self, member_id, consent_id=None):
        with engine.read_conn(self.db_path) as conn:
            self._check_consent(conn, member_id, consent_id)

    def _check_connection(self, conn, snapshot):
        current = conn.execute('SELECT * FROM bank_connections WHERE id=?', (snapshot['id'],)).fetchone()
        if not current or any(current[key] != snapshot[key] for key in
                              ('member_id', 'state', 'generation', 'cursor', 'token_ref')):
            raise BankingError('stale_connection')

    def _post(self, member_id, endpoint, payload, *, connection=None, consent_id=None):
        def policy():
            self._policy(member_id, consent_id)
            self.store._check()
            if connection is not None:
                with engine.read_conn(self.db_path) as conn:
                    self._check_connection(conn, connection)
        policy()
        credentials_file = self.credentials_file
        if credentials_file is None:
            raise BankingError('egress_blocked')
        return PlaidClient(credentials_file, policy=policy,
                           transport=self.transport).post(endpoint, payload)

    def consent(self, member_id, *, granted, version):
        self.writable()
        if version != CONSENT_VERSION or type(granted) is not bool:
            raise BankingError('invalid_consent')
        with engine.write_tx(self.db_path) as conn:
            if not conn.execute('SELECT 1 FROM household_members WHERE id=? AND is_active=1', (member_id,)).fetchone():
                raise BankingError('member_not_found')
            conn.execute('INSERT INTO bank_consents(member_id,version,granted) VALUES (?,?,?)', (member_id, version, int(granted)))
        return {'status': 'granted' if granted else 'withdrawn'}

    def add_member(self, name):
        self.writable()
        try:
            with engine.write_tx(self.db_path) as conn:
                member_id = repo_budgets.create_household_member(conn, name=name)
        except repo_budgets.MemberError as exc:
            raise BankingError(str(exc)) from None
        return {'member_id': member_id}

    def rename_member(self, member_id, name):
        self.writable()
        try:
            with engine.write_tx(self.db_path) as conn:
                repo_budgets.rename_household_member(conn, member_id=member_id, name=name)
        except repo_budgets.MemberError as exc:
            raise BankingError(str(exc)) from None
        return {'member_id': member_id}

    def _check_link_configuration(self):
        """Validate configured values only; file/provider checks belong to dispatch."""
        if self.private_dir is None or self.credentials_file is None:
            raise BankingError('banking_not_configured')
        if not self.private_dir.is_absolute() or not self.credentials_file.is_absolute():
            raise BankingError('unsafe_private_path')
        try:
            if any(ord(c) < 32 or ord(c) == 127 for c in self.origin + self.completion_uri):
                raise ValueError()
            origin = urlsplit(self.origin)
            completion = urlsplit(self.completion_uri)
            # Accessing port also rejects malformed configured ports.
            origin.port
            completion.port
            if (not origin.hostname or origin.username is not None or origin.password is not None
                    or origin.path or origin.query or origin.fragment
                    or (completion.scheme, completion.netloc) != (origin.scheme, origin.netloc)
                    or not completion.path.startswith('/') or completion.path.startswith('//')
                    or completion.query or completion.fragment):
                raise ValueError()
        except ValueError:
            raise BankingError('invalid_completion_uri') from None
        if completion.scheme != 'https' and not (
                completion.scheme == 'http' and isinstance(self.transport, httpx.MockTransport)
                and completion.hostname in {'banking.example.invalid', '127.0.0.1', 'localhost'}):
            raise BankingError('https_completion_required')

    def setup(self):
        """Deterministic public configuration/permission snapshot, not a provider probe."""
        if self.read_only():
            state, message = 'read_only', 'Open a writable instance to edit members or connect banks.'
        elif not self.enabled():
            state, message = 'disabled', 'Add household members locally. Enable Plaid in server settings to connect banks.'
        elif self.strict_local():
            state, message = 'strict_local', 'Add household members locally. Turn off strict-local mode to allow Plaid connections.'
        elif (self.private_dir is None or self.credentials_file is None
              or not self.origin or not self.completion_uri):
            state, message = 'unconfigured', 'Configure private Plaid storage, credentials, trusted origin and completion URL on the server.'
        else:
            try:
                self._check_link_configuration()
            except BankingError:
                state, message = 'invalid_configuration', 'Correct the server’s private paths and matching HTTPS origin/completion URL.'
            else:
                state, message = 'ready', 'Choose a named member and grant consent to connect a bank. Private credentials are checked when connecting.'
        return {'state': state, 'ready': state == 'ready', 'message': message}

    def overview(self):
        with engine.read_conn(self.db_path) as conn:
            members = repo_budgets.household_members(conn)
            for member in members:
                c = conn.execute('SELECT * FROM bank_consents WHERE member_id=? ORDER BY id DESC LIMIT 1', (member['id'],)).fetchone()
                member['consented'] = bool(c and c['granted'] and c['version']==CONSENT_VERSION)
            connections = [dict(r) for r in conn.execute('''SELECT c.id,c.member_id,m.name AS member_name,c.state,c.generation,
                c.created_at FROM bank_connections c JOIN household_members m ON m.id=c.member_id ORDER BY c.id''')]
            for c in connections:
                c['accounts'] = [dict(r) for r in conn.execute('''SELECT a.id,a.provider_account_id,a.name,a.currency,a.kind,
                    b.account_id,b.included,b.ownership,b.owner_member_id,b.role,COALESCE(b.revision,0) AS revision
                    FROM bank_accounts a LEFT JOIN v_bank_bindings b ON b.bank_account_id=a.id WHERE a.connection_id=? ORDER BY a.id''', (c['id'],))]
            accounts = [dict(r) for r in conn.execute('SELECT id,name,kind,currency FROM accounts WHERE is_active=1 ORDER BY id')]
        setup = self.setup()
        return {'members': members, 'connections': connections, 'accounts': accounts, 'consent_version': CONSENT_VERSION,
            'consent_text': CONSENT_TEXT, 'setup': setup, 'network_allowed': setup['ready'],
            'read_only': self.read_only()}

    def bind(self, connection_id, provider_account_id, *, account_id=None, included,
             ownership, role, expected_revision, owner_member_id=None, new_name=None, new_kind=None):
        self.writable()
        if type(included) is not bool or ownership not in {'shared','personal'} or role not in {'primary','secondary'}:
            raise BankingError('invalid_binding')
        if (ownership=='shared' and owner_member_id is not None) or (ownership=='personal' and owner_member_id is None):
            raise BankingError('explicit_owner_required')
        with engine.write_tx(self.db_path) as conn:
            ba = conn.execute('SELECT * FROM bank_accounts WHERE connection_id=? AND provider_account_id=?', (connection_id, provider_account_id)).fetchone()
            if not ba:
                raise BankingError('bank_account_not_found')
            old = conn.execute('SELECT * FROM v_bank_bindings WHERE bank_account_id=?', (ba['id'],)).fetchone()
            if (old['revision'] if old else 0) != expected_revision:
                raise BankingError('stale_binding')
            if included and (old is None or not old['included']):
                months = [r['month'] for r in conn.execute('''SELECT DISTINCT substr(posted_on,1,7) AS month
                    FROM bank_observations WHERE connection_id=? AND provider_account_id=?''',
                    (connection_id, provider_account_id))]
                repo_period_policy.guard_months(conn, months)
            if owner_member_id is not None and not conn.execute('SELECT 1 FROM household_members WHERE id=? AND is_active=1', (owner_member_id,)).fetchone():
                raise BankingError('member_not_found')
            if account_id is None:
                if not new_name or len(new_name)>100 or new_kind not in {'chequing','savings','credit','cash','investment'}:
                    raise BankingError('explicit_account_required')
                account_id = conn.execute('INSERT INTO accounts(name,kind,currency) VALUES (?,?,?)', (new_name.strip(), new_kind, ba['currency'])).lastrowid
            account = conn.execute('SELECT * FROM accounts WHERE id=? AND is_active=1', (account_id,)).fetchone()
            if not account or account['currency'] != ba['currency']:
                raise BankingError('binding_currency_mismatch')
            conn.execute('''INSERT INTO bank_bindings(bank_account_id,revision,account_id,included,ownership,owner_member_id,role,actor)
                VALUES (?,?,?,?,?,?,?,'operator:household')''',
                (ba['id'], expected_revision+1, account_id, int(included), ownership, owner_member_id, role))
        return {'account_id': account_id, 'revision': expected_revision+1}

    def _connection(self, connection_id):
        with engine.read_conn(self.db_path) as conn:
            row = conn.execute('SELECT * FROM bank_connections WHERE id=?', (connection_id,)).fetchone()
            if not row:
                raise BankingError('connection_not_found')
            return dict(row)

    def _check_unresolved_link(self, conn, member_id):
        # Pending sessions may expire before exchange; exchange uncertainty is
        # durable and never becomes retry-safe merely because the clock moved.
        if conn.execute('''SELECT 1 FROM bank_link_sessions WHERE member_id=? AND
                (state IN ('exchanging','uncertain') OR (state='pending' AND expires_at>=?)) LIMIT 1''',
                (member_id, time.time())).fetchone():
            raise BankingError('member_link_unresolved_check_existing_session')

    def start_link(self, *, member_id, browser, connection_id=None):
        self._policy(member_id)
        with engine.read_conn(self.db_path) as conn:
            member = conn.execute('SELECT name_required FROM household_members WHERE id=?', (member_id,)).fetchone()
            if member and member['name_required']:
                raise BankingError('member_name_required')
            self._check_unresolved_link(conn, member_id)
        self._check_link_configuration()
        payload = {'client_name':'Finn Nancy','language':'en','country_codes':['CA'],
            'user':{'client_user_id':''},'hosted_link':{'completion_redirect_uri':self.completion_uri}}
        with engine.read_conn(self.db_path) as conn:
            from ..db.migrate import read_database_identity
            identity = read_database_identity(conn)
            consent = conn.execute('SELECT id FROM bank_consents WHERE member_id=? ORDER BY id DESC LIMIT 1', (member_id,)).fetchone()
        payload['user']['client_user_id']=f'{identity}:member:{member_id}'
        c = None
        if connection_id is not None:
            c = self._connection(connection_id)
            if c['member_id']!=member_id or c['state']!='needs_repair':
                raise BankingError('connection_not_repairable')
            payload['access_token']=self.store.get(c['token_ref'])
        else:
            payload['products']=['transactions']
        response = self._post(member_id, '/link/token/create',payload, connection=c, consent_id=consent['id'])
        link = text(response.get('link_token'),2000)
        hosted = text(response.get('hosted_link_url'),4000)
        parsed = urlsplit(hosted)
        if parsed.scheme!='https' or parsed.netloc!='secure.plaid.com' or not parsed.path.startswith('/hl/') or parsed.fragment:
            raise BankingError('invalid_hosted_link_url')
        try:
            expiration=int(datetime.fromisoformat(response['expiration'].replace('Z','+00:00')).timestamp())
        except (KeyError,ValueError,TypeError):
            raise BankingError('invalid_link_expiration') from None
        session_id=secrets.token_hex(24)
        ref=self.store.put(link)
        with engine.write_tx(self.db_path) as conn:
            self._check_consent(conn, member_id, consent['id'])
            self._check_unresolved_link(conn, member_id)
            if c is not None:
                self._check_connection(conn, c)
            conn.execute('''INSERT INTO bank_link_sessions(id,browser_hash,member_id,token_ref,consent_id,connection_id,state,expires_at)
                VALUES (?,?,?,?,?,?,'pending',?)''', (session_id,hashlib.sha256(browser.encode()).hexdigest(),member_id,ref,
                consent['id'],connection_id,min(expiration,int(time.time())+1800)))
        return {'session_id':session_id,'hosted_link_url':hosted}

    def finish_link(self, session_id, *, browser):
        """Browser-bound check. Completion itself is shared with the server poller."""
        self.writable()
        session = self._link_session(session_id)
        if not secrets.compare_digest(session['browser_hash'],hashlib.sha256(browser.encode()).hexdigest()):
            raise BankingError('browser_session_mismatch')
        return self._finish_session(session_id, source='browser')

    def complete_pending_links(self):
        """Server-side completion: a finished Plaid session never depends on a browser returning.

        Pending sessions are checked every call; exited ones are rechecked at most once a
        minute until expiry because the user can reopen the same Hosted Link URL.
        """
        now = time.time()
        with engine.read_conn(self.db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,state FROM bank_link_sessions WHERE state IN ('pending','exited') AND expires_at>=?", (now,))]
        open_ids = {row['id'] for row in rows}
        for stale in set(self._last_server_check) - open_ids:
            del self._last_server_check[stale]
        if not rows or self.read_only() or not self.setup()['ready']:
            return []
        results = []
        for row in rows:
            if row['state'] == 'exited' and now - self._last_server_check.get(row['id'], 0) < 60:
                continue
            self._last_server_check[row['id']] = now
            try:
                results.append(self._finish_session(row['id'], source='server'))
            except BankingError as exc:
                results.append({'status': 'error', 'code': str(exc)})
        return results

    def _link_session(self, session_id):
        with engine.read_conn(self.db_path) as conn:
            row=conn.execute('SELECT * FROM bank_link_sessions WHERE id=?',(session_id,)).fetchone()
        if row is None:
            raise BankingError('link_session_not_found')
        return dict(row)

    def _record_check(self, session_id, source, outcome, detail=None):
        detail = json.dumps(detail or {}, sort_keys=True, separators=(',', ':'))
        with engine.write_tx(self.db_path) as conn:
            last = conn.execute('SELECT outcome,detail FROM bank_link_checks WHERE session_id=? ORDER BY id DESC LIMIT 1',
                                (session_id,)).fetchone()
            # Repeated identical polls add no information; keep the first of each run.
            if last is None or (last['outcome'], last['detail']) != (outcome, detail):
                conn.execute('INSERT INTO bank_link_checks(session_id,source,outcome,detail) VALUES (?,?,?,?)',
                             (session_id, source, outcome, detail))
        logger.info(json.dumps({'event': 'bank_link_check', 'session': hashlib.sha256(session_id.encode()).hexdigest()[:12],
                                'source': source, 'outcome': outcome, 'detail': json.loads(detail)}, separators=(',', ':')))

    def _finish_session(self, session_id, *, source):
        # One completion at a time in this single-instance service, so a browser check
        # racing the server poller waits instead of observing a live 'exchanging' row.
        with self._finish_lock:
            session = self._link_session(session_id)
            try:
                result, detail = self._finish_locked(session)
            except BankingError as exc:
                detail = getattr(exc, 'diagnostics', None) or {}
                if getattr(exc, 'provider_code', None):
                    detail = {**detail, 'provider_error_code': exc.provider_code}
                self._record_check(session_id, source, str(exc), detail)
                raise
            self._record_check(session_id, source, result['status'], detail)
            return result

    def _finish_locked(self, session):
        session_id = session['id']
        if session['state']=='complete':
            return self._link_result(session['connection_id']), {}
        if session['state'] not in {'pending','exited'}:
            raise BankingError('link_outcome_uncertain_do_not_retry')
        if session['expires_at']<time.time():
            raise BankingError('link_session_expired')
        self._policy(session['member_id'], session['consent_id'])
        repair_connection = None
        if session['connection_id'] is not None:
            repair_connection = self._connection(session['connection_id'])
            if repair_connection['state'] != 'needs_repair':
                raise BankingError('connection_not_repairable')
        repair = repair_connection is not None
        link=self.store.get(session['token_ref'])
        response=self._post(session['member_id'],'/link/token/get',{'link_token':link},
                            connection=repair_connection, consent_id=session['consent_id'])
        if response.get('link_token')!=link or not isinstance(response.get('link_sessions'),list):
            raise BankingError('server_link_verification_unavailable')
        detail = link_diagnostics(response)
        sessions = [s for s in response['link_sessions'] if isinstance(s, dict)]
        finished = [(s, completion_tokens(s)) for s in sessions
                    if isinstance(s.get('finished_at'), str) and s['finished_at']]
        # A new Item needs a public token. Update mode (repair) adds no Item, so any
        # finished session that did not exit is its success signal.
        succeeded = [(s, t) for s, t in finished
                     if t or isinstance(s.get('on_success'), dict) or (repair and link_exit(s) is None)]
        if not succeeded:
            exited = [s for s, _ in finished if link_exit(s) is not None]
            if exited and len(finished) == len(sessions):
                with engine.write_tx(self.db_path) as conn:
                    conn.execute("UPDATE bank_link_sessions SET state='exited' WHERE id=? AND state='pending'", (session_id,))
                return {'status': 'exited'}, detail
            return {'status':'pending'}, detail
        if len(succeeded)!=1:
            error = BankingError('ambiguous_link_completion'); error.diagnostics = detail; raise error
        tokens=set(succeeded[0][1])
        if not repair and len(tokens)!=1:
            error = BankingError('server_link_verification_unavailable'); error.diagnostics = detail; raise error
        self._policy(session['member_id'])
        with engine.write_tx(self.db_path) as conn:
            self._check_consent(conn, session['member_id'], session['consent_id'])
            if repair_connection is not None:
                self._check_connection(conn, repair_connection)
            if conn.execute("UPDATE bank_link_sessions SET state='exchanging' WHERE id=? AND state IN ('pending','exited')",
                            (session_id,)).rowcount!=1:
                raise BankingError('link_already_consumed')
        # Once claimed, never repeat public-token exchange, including process crashes.
        try:
            if repair_connection is not None:
                connection_id=repair_connection['id']
                with engine.write_tx(self.db_path) as conn:
                    self._check_consent(conn, session['member_id'], session['consent_id'])
                    self._check_connection(conn, repair_connection)
                    if conn.execute("UPDATE bank_connections SET state='active',generation=generation+1 WHERE id=? AND state='needs_repair' AND generation=?",
                                    (connection_id,repair_connection['generation'])).rowcount != 1:
                        raise BankingError('stale_connection')
                    conn.execute("UPDATE bank_link_sessions SET state='complete' WHERE id=?",(session_id,))
            else:
                exchanged=self._post(session['member_id'],'/item/public_token/exchange',{'public_token':tokens.pop()},
                                     consent_id=session['consent_id'])
                access_ref=self.store.put(text(exchanged.get('access_token'),2000))
                item_id=text(exchanged.get('item_id'),200)
                with engine.write_tx(self.db_path) as conn:
                    self._check_consent(conn, session['member_id'], session['consent_id'])
                    connection_id=conn.execute('''INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state)
                        VALUES (?,?,?,?,'active')''',(session['member_id'],item_id,access_ref,CONSENT_VERSION)).lastrowid
                    conn.execute("UPDATE bank_link_sessions SET state='complete',connection_id=? WHERE id=?",(connection_id,session_id))
        except Exception as exc:
            with engine.write_tx(self.db_path) as conn:
                conn.execute("UPDATE bank_link_sessions SET state='uncertain' WHERE id=? AND state='exchanging'",(session_id,))
            error = BankingError('link_outcome_uncertain_do_not_retry')
            error.diagnostics = {**detail, 'exchange_error': str(exc) if isinstance(exc, BankingError) else type(exc).__name__}
            error.provider_code = getattr(exc, 'provider_code', None)
            raise error from None
        # Discovery is retryable independently; an exchange success is never reported as relink-safe.
        try:
            self.discover_accounts(connection_id)
        except BankingError as exc:
            detail = {**detail, 'discovery_error': str(exc)}
        return self._link_result(connection_id), detail

    def _link_result(self, connection_id):
        current = self._connection(connection_id)
        return {'status': 'connected' if current['state'] == 'active' else current['state'],
                'connection_id': connection_id}

    def browser_links(self, browser):
        with engine.read_conn(self.db_path) as conn:
            return [dict(r) for r in conn.execute('''SELECT s.id AS session_id,s.member_id,m.name AS member_name,s.state
                FROM bank_link_sessions s JOIN household_members m ON m.id=s.member_id
                WHERE browser_hash=? AND
                    (state IN ('exchanging','uncertain') OR (state='pending' AND expires_at>=?))
                ORDER BY s.created_at DESC LIMIT 20''',
                (hashlib.sha256(browser.encode()).hexdigest(),time.time()))]

    def discover_accounts(self, connection_id):
        c=self._connection(connection_id)
        self._policy(c['member_id'])
        if c['state'] not in {'active','needs_repair'}:
            raise BankingError('connection_not_active')
        response=self._post(c['member_id'],'/accounts/get',{'access_token':self.store.get(c['token_ref'])}, connection=c)
        rows=response.get('accounts')
        if not isinstance(rows,list) or len(rows)>200:
            raise BankingError('invalid_provider_accounts')
        staged=[]
        for a in rows:
            staged.append((text(a.get('account_id'),200),text(a.get('name'),200),
                (a.get('balances') or {}).get('iso_currency_code') or '',
                'credit' if a.get('type')=='credit' else ('savings' if a.get('subtype')=='savings' else 'chequing')))
        self._policy(c['member_id'])
        with engine.write_tx(self.db_path) as conn:
            self._check_consent(conn, c['member_id'])
            self._check_connection(conn, c)
            for ident,name,currency,kind in staged:
                old=conn.execute('SELECT * FROM bank_accounts WHERE connection_id=? AND provider_account_id=?',(connection_id,ident)).fetchone()
                if old and old['currency']!=currency:
                    raise BankingError('provider_account_currency_changed')
                conn.execute('''INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (?,?,?,?,?)
                    ON CONFLICT(connection_id,provider_account_id) DO NOTHING''',(connection_id,ident,name,currency,kind))
        return {'status':'accounts_loaded','count':len(staged)}

    def disconnect(self, connection_id):
        c=self._connection(connection_id)
        if c['state']=='removed':
            return {'status':'removed'}
        if c['state'] in {'removing','remove_uncertain'}:
            raise BankingError('revocation_uncertain_do_not_retry')
        self._policy(c['member_id'])
        with engine.write_tx(self.db_path) as conn:
            self._check_connection(conn, c)
            if conn.execute("UPDATE bank_connections SET state='removing',generation=generation+1 WHERE id=? AND state IN ('active','needs_repair')",(connection_id,)).rowcount!=1:
                raise BankingError('connection_not_active')
        removing = {**c, 'state': 'removing', 'generation': c['generation'] + 1}
        try:
            self._post(c['member_id'],'/item/remove',{'access_token':self.store.get(c['token_ref'])}, connection=removing)
        except BankingError:
            with engine.write_tx(self.db_path) as conn:
                conn.execute("UPDATE bank_connections SET state='remove_uncertain' WHERE id=?",(connection_id,))
            raise BankingError('revocation_uncertain_do_not_retry') from None
        with engine.write_tx(self.db_path) as conn:
            conn.execute("UPDATE bank_connections SET state='removed' WHERE id=?",(connection_id,))
        self.store.remove(c['token_ref'])
        return {'status':'removed'}

    def _normalize(self, raw, kind):
        ident = text(raw.get('transaction_id'), 200)
        if kind=='removed':
            return {'provider_transaction_id': ident, 'provider_account_id': raw.get('account_id') or '', 'kind': kind}
        try:
            amount = Decimal(str(raw['amount'])) * 100
            if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount)>9_000_000_000_000_000:
                raise ValueError()
            posted = date.fromisoformat(raw['date']).isoformat()
            if type(raw['pending']) is not bool:
                raise ValueError()
            return {'provider_transaction_id': ident, 'provider_account_id': text(raw['account_id'],200),
                'kind':kind, 'amount_cents':-int(amount), 'currency':raw.get('iso_currency_code') or '',
                'posted_on':posted, 'pending':int(raw['pending']), 'pending_predecessor':raw.get('pending_transaction_id'),
                'merchant':raw.get('merchant_name') or '', 'description':text(raw['name'],2000),
                'suggestion_json':json.dumps(raw.get('personal_finance_category') or {}, sort_keys=True)}
        except (KeyError, ValueError, TypeError, InvalidOperation):
            raise BankingError('invalid_provider_transaction') from None

    def sync(self, connection_id):
        c = self._connection(connection_id)
        self._policy(c['member_id'])
        if c['state'] != 'active':
            raise BankingError('connection_not_active')
        cursor = c['cursor']
        staged = []
        seen = set()
        for _ in range(50):
            payload = {'access_token': self.store.get(c['token_ref']), 'count': 500}
            if cursor:
                payload['cursor'] = cursor
            try:
                page = self._post(c['member_id'], '/transactions/sync', payload, connection=c)
            except BankingError as exc:
                if str(exc)=='ITEM_LOGIN_REQUIRED':
                    with engine.write_tx(self.db_path) as conn:
                        conn.execute("UPDATE bank_connections SET state='needs_repair',generation=generation+1 WHERE id=? AND state='active' AND generation=?",
                                     (connection_id,c['generation']))
                raise
            if type(page.get('has_more')) is not bool:
                raise BankingError('invalid_sync_window')
            next_cursor = text(page.get('next_cursor'),2000)
            if page['has_more'] and (next_cursor==cursor or next_cursor in seen):
                raise BankingError('invalid_sync_cursor')
            seen.add(next_cursor)
            for kind in ('added','modified','removed'):
                rows = page.get(kind)
                if not isinstance(rows,list) or len(rows)>500:
                    raise BankingError('invalid_sync_window')
                staged.extend(self._normalize(row,kind) for row in rows)
            if len(staged)>25000:
                raise BankingError('sync_window_too_large')
            cursor = next_cursor
            if not page['has_more']:
                break
        else:
            raise BankingError('sync_window_too_large')
        self._policy(c['member_id'])
        with engine.write_tx(self.db_path) as conn:
            current = conn.execute('SELECT * FROM bank_connections WHERE id=?', (connection_id,)).fetchone()
            if current['generation']!=c['generation'] or current['cursor']!=c['cursor'] or current['state']!='active':
                raise BankingError('stale_sync_window')
            window = conn.execute('INSERT INTO bank_windows(connection_id,generation,from_cursor,to_cursor) VALUES (?,?,?,?)',
                                  (connection_id,c['generation']+1,c['cursor'],cursor)).lastrowid
            for observation in staged:
                prior = conn.execute('''SELECT o.* FROM bank_subjects s JOIN bank_observations o ON o.id=s.observation_id
                    WHERE s.connection_id=? AND s.provider_transaction_id=?''', (connection_id,observation['provider_transaction_id'])).fetchone()
                if observation['kind']=='removed':
                    if prior is None:
                        raise BankingError('removed_transaction_unknown')
                    observation = {**dict(prior), **observation}
                    if not observation['provider_account_id']:
                        observation['provider_account_id']=prior['provider_account_id']
                if not conn.execute('SELECT 1 FROM bank_accounts WHERE connection_id=? AND provider_account_id=?',
                                    (connection_id,observation['provider_account_id'])).fetchone():
                    raise BankingError('unknown_provider_account_sync_accounts_first')
                fields = ('provider_transaction_id','provider_account_id','kind','amount_cents','currency','posted_on','pending','pending_predecessor','merchant','description','suggestion_json')
                oid = conn.execute('INSERT INTO bank_observations(window_id,connection_id,'+','.join(fields)+') VALUES ('+','.join('?' for _ in range(13))+')',
                    (window,connection_id,*(observation.get(k) for k in fields))).lastrowid
                conn.execute('''INSERT INTO bank_subjects(connection_id,provider_transaction_id,observation_id) VALUES (?,?,?)
                    ON CONFLICT(connection_id,provider_transaction_id) DO UPDATE SET observation_id=excluded.observation_id''',
                    (connection_id,observation['provider_transaction_id'],oid))

            conn.execute('UPDATE bank_connections SET cursor=?,generation=generation+1 WHERE id=?', (cursor,connection_id))
        return {'status':'synced','observations':len(staged),'generation':c['generation']+1}

    def feed(self):
        with engine.read_conn(self.db_path) as conn:
            return [dict(r) for r in conn.execute('''SELECT s.id,o.amount_cents,o.currency,o.posted_on,o.merchant,o.description,
                o.kind,o.pending,o.id AS observation_id,c.member_id,m.name AS member_name,
                ba.name AS account_name,b.account_id,b.included,b.role,
                (SELECT transaction_id FROM v_bank_associations WHERE subject_id=s.id) AS transaction_id,
                EXISTS(SELECT 1 FROM v_bank_feed_blockers WHERE subject_id=s.id) AS needs_review
                FROM bank_subjects s JOIN bank_observations o ON o.id=s.observation_id
                JOIN bank_connections c ON c.id=s.connection_id JOIN household_members m ON m.id=c.member_id
                JOIN bank_accounts ba ON ba.connection_id=s.connection_id AND ba.provider_account_id=o.provider_account_id
                LEFT JOIN v_bank_bindings b ON b.bank_account_id=ba.id ORDER BY o.posted_on DESC,s.id DESC''')]

    def review_plan(self, subject_id):
        with engine.read_conn(self.db_path) as conn:
            return feed.plan(conn, subject_id, self.home_currency)

    def decide(self, subject_id, **kwargs):
        self.writable()
        with engine.write_tx(self.db_path) as conn:
            return feed.decide(conn, subject_id, home_currency=self.home_currency, **kwargs)

    def attach_receipt(self, **kwargs):
        self.writable()
        with engine.write_tx(self.db_path) as conn:
            return feed.attach_receipt(conn, home_currency=self.home_currency, **kwargs)

    def pending_receipts(self):
        from ..ingest.schemas import ExtractedReceipt
        result=[]
        with engine.read_conn(self.db_path) as conn:
            for r in conn.execute("SELECT * FROM ingest_extractions WHERE doc_kind='receipt' AND review_status='pending' AND transaction_id IS NULL ORDER BY id LIMIT 200"):
                try:
                    receipt=ExtractedReceipt.model_validate_json(r['extracted_json'])
                    candidates=feed.receipt_candidates(conn,amount_cents=-abs(receipt.total_cents or 0),posted_on=receipt.purchased_on)
                    if candidates and receipt.currency==self.home_currency:
                        result.append({'extraction_id':r['id'],'document_id':r['source_document_id'],
                            'merchant':receipt.merchant,'amount_cents':-abs(receipt.total_cents or 0),
                            'posted_on':receipt.purchased_on,'candidates':candidates})
                except (ValueError,TypeError):
                    continue
        return result
