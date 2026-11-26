"""Generated IMAP and mounted app tests. No upstream mailbox or DNS is used."""
from __future__ import annotations

import base64
import functools
import json
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from types import SimpleNamespace

import dkim
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.banking import rogers_imap, rogers_mailbox as mailbox
from app.banking.rogers_message import MAX_MESSAGE_BYTES, MessageRetry, parse_message
from app.config import Settings, get_settings
from app.db import engine, repo_rogers_alerts as repo

SENDER = 'alerts@example.invalid'
RECIPIENT = 'original@example.invalid'
SUBJECT = 'Example purchase authorization'
ORIGIN = 'http://finance.example.invalid'
PATTERN = (r'Approved on (?P<authorized_on>[0-9-]{10})\nCard ending (?P<card_last4>[0-9]{4})'
           r'\nAmount: (?P<currency>[A-Z]{3}) (?P<amount>[0-9]+\.[0-9]{2})\nMerchant: (?P<merchant>[^\n]+)')


@pytest.fixture
def generated_mail(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def lookup(name, **kwargs):
        assert name.rstrip(b'.') == b'fixture._domainkey.example.invalid'
        return b'v=DKIM1; k=rsa; p=' + base64.b64encode(public)

    def signed(*, id='first', last4='0123', amount='7.65', sender=SENDER, recipient=RECIPIENT):
        msg = EmailMessage(policy=SMTP)
        msg['From'] = sender
        msg['To'] = recipient
        msg['Subject'] = SUBJECT
        msg['Message-ID'] = f'<{id}@example.invalid>'
        msg['Date'] = 'Sun, 05 Apr 2026 14:00:00 +0000'
        msg.set_content(f'Approved on 2026-04-05\nCard ending {last4}\nAmount: CAD {amount}\nMerchant: Example Store')
        raw = msg.as_bytes()
        return dkim.sign(raw, b'fixture', b'example.invalid', private, include_headers=[
            b'from', b'to', b'subject', b'message-id', b'date', b'content-type', b'content-transfer-encoding',
            b'mime-version']) + raw

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(
                x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False).sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / 'test-cert.pem', tmp_path / 'test-key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(private)
    return signed, lookup, certfile, keyfile


@dataclass
class Messages:
    items: dict
    validity: int = 41
    calls: list[bytes] = field(default_factory=list)
    logins: list[bytes] = field(default_factory=list)
    search_ranges: list[tuple[int, int]] = field(default_factory=list)
    extra_metadata: bytes = b''


@pytest.fixture
def wire(monkeypatch, generated_mail):
    signed, lookup, certfile, keyfile = generated_mail
    first = signed()
    messages = Messages({1: first, 2: first, 3: signed(id='second', last4='4567'),
                         4: first.replace(b'Example Store', b'Tampered Store'),
                         5: signed(id='foreign', recipient='other@example.invalid')})
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile, keyfile)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

        def get_request(self):
            sock, addr = super().get_request()
            sock.settimeout(3)
            return tls.wrap_socket(sock, server_side=True), addr

    class Handler(socketserver.StreamRequestHandler):
        def emit(self, payload):
            self.wfile.write(payload)

        def handle(self):
            self.emit(b'* OK test mailbox\r\n')
            while raw := self.rfile.readline(1024):
                tag, _, body = raw.strip().partition(b' ')
                command, _, rest = body.partition(b' ')
                messages.calls.append(command + (b' ' + rest.split(maxsplit=1)[0] if command == b'UID' else b''))
                if command == b'CAPABILITY':
                    self.emit(b'* CAPABILITY IMAP4rev1\r\n')
                elif command == b'LOGIN':
                    messages.logins.append(rest)
                elif command == b'EXAMINE':
                    assert rest == b'INBOX'
                    self.emit(f'* {len(messages.items)} EXISTS\r\n* OK [UIDVALIDITY {messages.validity}] epoch\r\n'
                              f'* OK [UIDNEXT {max(messages.items, default=0) + 1}] next\r\n'.encode())
                    self.emit(messages.extra_metadata)
                elif command == b'UID' and rest.startswith(b'SEARCH UID '):
                    _, _, interval, field, sender = rest.split()
                    assert field == b'FROM' and sender == b'"' + SENDER.encode() + b'"'
                    low, high = map(int, interval.split(b':'))
                    messages.search_ranges.append((low, high))
                    uids = b' '.join(str(uid).encode() for uid in sorted(messages.items) if low <= uid <= high)
                    self.emit(b'* SEARCH' + (b' ' + uids if uids else b'') + b'\r\n')
                elif command == b'UID' and rest.startswith(b'FETCH '):
                    _, requested, spec = rest.split(maxsplit=2)
                    assert spec == f'(UID BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)'.encode()
                    data = messages.items[int(requested)][:MAX_MESSAGE_BYTES + 1]
                    self.emit(f'* 1 FETCH (BODY[]<0> {{{len(data)}}}\r\n'.encode() + data + b' UID ' + requested + b')\r\n')
                else:
                    self.emit(tag + b' BAD unexpected command\r\n')
                    continue
                self.emit(tag + b' OK complete\r\n')

    client = ssl.create_default_context(cafile=str(certfile))
    monkeypatch.setattr(rogers_imap, 'create_default_context', lambda: client)
    with Server(('127.0.0.1', 0), Handler) as server:
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        try:
            yield server.server_address[1], messages, functools.partial(parse_message, dns_lookup=lookup), signed
        finally:
            server.shutdown()
            runner.join(timeout=5)


def configure(tmp_path, empty_db, port, **overrides):
    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES('Example card','credit','CAD')").lastrowid
    data = {'host': 'localhost', 'port': port, 'username': 'bank-alerts@example.invalid',
            'password': 'generated mailbox password', 'consent_version': mailbox.CONSENT_VERSION,
            'format': {'sender': SENDER, 'recipient': RECIPIENT, 'subject': SUBJECT, 'signing_domain': 'example.invalid',
                       'body_pattern': PATTERN}, 'card_accounts': {'0123': account}}
    path = tmp_path.resolve() / 'mailbox.json'
    path.write_text(json.dumps(data | overrides))
    path.chmod(0o600)
    return account, path


def app_settings(empty_db, path, **overrides):
    return Settings(_env_file=None, **({'db_path': Path(empty_db), 'rogers_alerts_enabled': True,
                        'rogers_alerts_config_file': path, 'strict_local_mode': False} | overrides))


def await_check(box):
    deadline = time.monotonic() + 8
    while box.outcome is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert box.outcome == 'checked'


@pytest.mark.parametrize('generic', [False, True])
def test_tls_mailbox_to_normal_feed_and_review(monkeypatch, tmp_path, empty_db, wire, generic):
    from app.web import app as web_module

    port, messages, parse, _ = wire
    account, path = configure(tmp_path, empty_db, port)
    prefix = 'EMAIL_ALERTS' if generic else 'ROGERS_ALERTS'
    if generic:
        convert_email_alerts(path)
    for name, value in {'DB_PATH': empty_db, 'DATA_DIR': str(tmp_path), f'{prefix}_ENABLED': 'true',
                        f'{prefix}_CONFIG_FILE': str(path), 'STRICT_LOCAL_MODE': 'false',
                        'PLAID_ORIGIN': ORIGIN}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    async def no_warm():
        return False

    monkeypatch.setattr(web_module, 'warm', no_warm)
    try:
        app = web_module.create_app()
        box = app.state.rogers_mailbox
        box.parse = parse
        with TestClient(app, base_url=ORIGIN) as client:
            await_check(box)
            view = client.get('/bank-feed')
            assert view.status_code == 200 and view.headers['cache-control'] == 'no-store'
            assert view.text.count('Example Store') == 2
            assert 'Email alerts' in view.text and 'Not booked' in view.text and 'Intake rejections: 2' in view.text
            assert 'generated mailbox password' not in view.text and SENDER not in view.text
            state = box.page()
            mapped = next(row for row in state['rows'] if row['card_last4'] == '0123')
            unbound = next(row for row in state['rows'] if row['card_last4'] == '4567')
            action = {'expected_review_id': 0, 'action': 'confirm', 'account_id': account, 'reason': 'Verified card'}
            url = '/connections/data/rogers-alert/'
            assert client.post(url + str(mapped['id']), json=action).status_code == 403
            csrf = client.get('/connections/data/overview').json()['csrf']
            headers = {'Origin': ORIGIN, 'X-Banking-CSRF': csrf}
            assert client.post(url + str(unbound['id']), headers=headers, json=action).status_code == 409
            assert client.post(url + str(mapped['id']), headers=headers, json=action).json()['status'] == 'confirm'
            assert 'Account confirmed' in client.get('/bank-feed').text
            with engine.read_conn(empty_db) as conn:
                assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
                assert conn.execute('SELECT COUNT(*) FROM bank_observations').fetchone()[0] == 0
    finally:
        get_settings.cache_clear()
    assert messages.calls.count(b'UID FETCH') == 5
    assert set(messages.calls) == {b'CAPABILITY', b'LOGIN', b'EXAMINE', b'UID SEARCH', b'UID FETCH'}


def convert_email_alerts(path, extra=None):
    source = json.loads(path.read_text())
    del source['consent_version']
    source.update(transport='imap', kind='card_purchase')
    intakes = [source]
    if extra is not None:
        intakes.append(source | extra(source))
    path.write_text(json.dumps({'consent_version': 'forwarded-email-alerts-v1', 'intakes': intakes}))


def generic_settings(empty_db, path, **overrides):
    return app_settings(empty_db, None, **({'rogers_alerts_enabled': False, 'email_alerts_enabled': True,
                        'email_alerts_config_file': path} | overrides))


def test_email_alerts_file_runs_multiple_qualified_intakes(tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    first, path = configure(tmp_path, empty_db, port)
    with engine.write_tx(empty_db) as conn:
        second = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES('Second card','credit','CAD')").lastrowid
    second_recipient = 'other@example.invalid'
    convert_email_alerts(path, lambda source: {'format': source['format'] | {'recipient': second_recipient},
                                              'card_accounts': {'0123': second}})
    messages.items = {1: signed(id='first-intake'), 2: signed(id='second-intake', recipient=second_recipient)}
    settings = generic_settings(empty_db, path)
    box = mailbox.RogersMailbox(lambda: settings, parse=parse)
    box.poll_safely()
    assert box.outcome == 'checked'
    page = box.page()
    assert page is not None
    assert [row['suggested_account_id'] for row in page['rows']] == [second, first]
    with engine.read_conn(empty_db) as conn:
        cursors = conn.execute('SELECT uid_validity,last_uid FROM rogers_alert_mailboxes ORDER BY source_key').fetchall()
        assert [tuple(row) for row in cursors] == [(41, 2), (41, 2)]
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


@pytest.mark.parametrize('invalid', ['duplicate', 'unsupported_kind', 'unsupported_transport', 'mixed_settings',
                                     'strict_local', 'read_only', 'no_consent'])
def test_email_alerts_rejects_ambiguous_or_unapproved_configs_before_network(tmp_path, empty_db, invalid):
    _, path = configure(tmp_path, empty_db, 993)
    overrides = {}
    extra = None
    if invalid == 'duplicate':
        extra = lambda intake: {'format': intake['format'] | {'subject': 'Another approval'}}
    elif invalid == 'unsupported_kind':
        extra = lambda _: {'kind': 'receipt'}
    elif invalid == 'unsupported_transport':
        extra = lambda _: {'transport': 'gmail_api'}
    elif invalid == 'mixed_settings':
        overrides['rogers_alerts_config_file'] = path
    elif invalid == 'strict_local':
        overrides['strict_local_mode'] = True
    elif invalid == 'read_only':
        overrides['read_only'] = True
    convert_email_alerts(path, extra)
    if invalid == 'no_consent':
        data = json.loads(path.read_text())
        del data['consent_version']
        path.write_text(json.dumps(data))
    settings = generic_settings(empty_db, path, **overrides)
    box = mailbox.RogersMailbox(lambda: settings, connect=lambda *a, **k: pytest.fail('unexpected dispatch'))
    box.poll_safely()
    assert box.outcome == ('mailbox_disabled' if invalid in {'strict_local', 'read_only'} else 'setup_required')


def test_generic_email_alerts_failure_of_one_intake_does_not_block_peer(tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    account, path = configure(tmp_path, empty_db, port, host='offline.example.invalid')
    convert_email_alerts(path, lambda _: {'host': 'localhost'})
    messages.items = {1: signed(id='available')}
    settings = generic_settings(empty_db, path)
    calls = []

    def connect(host, *args, **kwargs):
        calls.append(host)
        if host == 'offline.example.invalid':
            raise TimeoutError
        return rogers_imap.BoundedIMAP(host, *args, **kwargs)

    box = mailbox.RogersMailbox(lambda: settings, connect=connect, parse=parse)
    box.poll_safely()
    assert calls == ['offline.example.invalid', 'localhost']
    assert box.outcome == 'retrying'
    page = box.page()
    assert page is not None and [row['suggested_account_id'] for row in page['rows']] == [account]


@pytest.mark.parametrize('generic', [False, True])
def test_duplicate_card_suffix_in_private_json_never_dispatches(tmp_path, empty_db, generic):
    account, path = configure(tmp_path, empty_db, 993)
    if generic:
        convert_email_alerts(path)
    text = path.read_text()
    original = f'"0123": {account}'
    assert text.count(original) == 1
    path.write_text(text.replace(original, original + ', "0123": 123'))
    settings = generic_settings(empty_db, path) if generic else app_settings(empty_db, path)
    box = mailbox.RogersMailbox(lambda: settings, connect=lambda *a, **k: pytest.fail('ambiguous intake dispatched'))
    box.poll_safely()
    assert box.outcome == 'setup_required'


def test_converting_legacy_file_preserves_cursor_and_retries(tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    account, path = configure(tmp_path, empty_db, port)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key
    messages.items = {1: signed(id='retained'), 2: signed(id='retry')}

    def defer_second(raw, fmt, **kwargs):
        if raw == messages.items[2]:
            raise MessageRetry()
        return parse(raw, fmt, **kwargs)

    box = mailbox.RogersMailbox(lambda: app_settings(empty_db, path), parse=defer_second, clock=lambda: 100)
    box.poll_safely()
    assert box.outcome == 'checked'
    convert_email_alerts(path)
    before = messages.calls.count(b'UID SEARCH')
    box = mailbox.RogersMailbox(lambda: generic_settings(empty_db, path), parse=parse,
                                clock=lambda: 100 + mailbox.RETRY_SECONDS)
    box.poll_safely()
    assert box.outcome == 'checked'
    assert messages.calls.count(b'UID SEARCH') == before
    page = box.page()
    assert page is not None and [row['suggested_account_id'] for row in page['rows']] == [account, account]
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 2)
        assert repo.counts(conn, source)['deferred_count'] == 0


def test_removing_shared_consent_stops_current_commit_and_remaining_intakes(tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    _, path = configure(tmp_path, empty_db, port)
    convert_email_alerts(path, lambda intake: {'format': intake['format'] | {'recipient': 'other@example.invalid'}})
    messages.items = {1: signed(id='not-committed')}
    settings = generic_settings(empty_db, path)

    def revoke(raw, fmt, **kwargs):
        alert = parse(raw, fmt, **kwargs)
        path.unlink()
        return alert

    box = mailbox.RogersMailbox(lambda: settings, parse=revoke)
    box.poll_safely()
    assert box.outcome == 'retrying'
    assert messages.calls.count(b'LOGIN') == 1
    with engine.read_conn(empty_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM rogers_alerts').fetchone()[0] == 0
        assert [row[0] for row in conn.execute('SELECT last_uid FROM rogers_alert_mailboxes')] == [0]


def test_login_quotes_configured_username_on_wire(tmp_path, empty_db, wire):
    port, messages, _, _ = wire
    messages.items.clear()
    _, path = configure(tmp_path, empty_db, port, username='alerts%desk@example.invalid')
    settings = app_settings(empty_db, path)
    box = mailbox.RogersMailbox(lambda: settings)
    box.poll_safely()
    assert box.outcome == 'checked'
    assert messages.logins == [b'"alerts%desk@example.invalid" "generated mailbox password"']


def test_restart_uid_epoch_and_transient_failure_keep_progress(tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    _, path = configure(tmp_path, empty_db, port)
    settings = app_settings(empty_db, path)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key
    messages.items = {1: signed(), 3: signed(id='new'), 4: signed(id='later')}

    def transient(raw, *args, **kwargs):
        if b'<new@example.invalid>' in raw:
            raise MessageRetry()
        return parse(raw, *args, **kwargs)

    mailbox.RogersMailbox(lambda: settings, parse=transient, clock=lambda: 100).poll()
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 4)
        assert repo.counts(conn, source) == {'conflict_count': 0, 'rejected_count': 0, 'deferred_count': 1}
        assert len(repo.list_alerts(conn, source, {})) == 2
    replacement = mailbox.RogersMailbox(lambda: settings, parse=parse, clock=lambda: 100 + mailbox.RETRY_SECONDS)
    for metadata in (b'* OK [UIDVALIDITY 42] conflict\r\n', b'* OK [UIDNEXT 6] conflict\r\n'):
        calls = len(messages.calls)
        messages.extra_metadata = metadata
        replacement.poll_safely()
        assert replacement.outcome == 'retrying'
        assert messages.calls[calls:] == [b'CAPABILITY', b'LOGIN', b'EXAMINE']
        with engine.read_conn(empty_db) as conn:
            assert repo.cursor(conn, source) == (41, 4)
            assert repo.due_retries(conn, source, 41, 100 + mailbox.RETRY_SECONDS, 1) == [3]
    messages.extra_metadata = b''
    replacement.poll_safely()
    assert replacement.outcome == 'checked'
    with engine.read_conn(empty_db) as conn:
        assert repo.counts(conn, source)['deferred_count'] == 0
        assert len(repo.list_alerts(conn, source, {})) == 3
    messages.validity = 92
    messages.items = {2: signed(id='new'), 5: signed(id='epoch')}
    replacement.poll_safely()
    assert replacement.outcome == 'checked'
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (92, 5)
        assert conn.execute('SELECT COUNT(*) FROM rogers_alerts').fetchone()[0] == 4


def test_fresh_alert_stages_before_slow_due_retry(monkeypatch, tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    _, path = configure(tmp_path, empty_db, port)
    settings = app_settings(empty_db, path)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key
    with engine.write_tx(empty_db) as conn:
        repo.advance(conn, source, None, (41, 1), retry_at=100)
    messages.items = {1: signed(id='old', amount='1.00'), 2: signed(id='fresh', amount='2.00')}
    started = now = time.monotonic()
    monkeypatch.setattr(mailbox, 'time', SimpleNamespace(monotonic=lambda: now))
    attempts = []
    deadlines = []

    def slow_retry(raw, *args, deadline=None, **kwargs):
        nonlocal now
        deadlines.append(deadline)
        if b'<old@example.invalid>' in raw:
            attempts.append('old')
            now += mailbox.POLL_BUDGET  # A retry uses the rest of this poll's time.
            raise MessageRetry()
        attempts.append('fresh')
        return parse(raw, *args, deadline=deadline, **kwargs)

    box = mailbox.RogersMailbox(lambda: settings, parse=slow_retry, clock=lambda: 100)
    box.poll_safely()
    assert attempts == ['fresh', 'old']
    assert deadlines == [started + mailbox.POLL_BUDGET] * 2
    assert box.outcome == 'retrying'
    assert messages.calls[-3:] == [b'UID SEARCH', b'UID FETCH', b'UID FETCH']
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 2)
        assert [row['amount_cents'] for row in repo.list_alerts(conn, source, {})] == [-200]
        assert repo.due_retries(conn, source, 41, 100, 2) == [1]

    # The expired attempt never takes ownership of the pointer; the next poll
    # can settle it, even with no new mail left to search.
    box.parse = parse
    box.poll_safely()
    assert box.outcome == 'checked'
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 2)
        assert [row['amount_cents'] for row in repo.list_alerts(conn, source, {})] == [-100, -200]
        assert repo.due_retries(conn, source, 41, 100, 2) == []


def test_one_due_retry_gets_a_turn_during_fresh_backlog(monkeypatch, tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    _, path = configure(tmp_path, empty_db, port)
    settings = app_settings(empty_db, path)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key
    with engine.write_tx(empty_db) as conn:
        repo.advance(conn, source, None, (41, 1), retry_at=100)
        repo.advance(conn, source, (41, 1), (41, 2), retry_at=100)
    monkeypatch.setattr(mailbox, 'BATCH', 2)
    messages.items = {uid: signed(id=str(uid), amount=f'{uid}.00') for uid in range(1, 6)}
    box = mailbox.RogersMailbox(lambda: settings, parse=parse, clock=lambda: 100)

    box.poll()
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 4)
        assert [row['amount_cents'] for row in repo.list_alerts(conn, source, {})] == [-400, -100, -300]
        assert repo.due_retries(conn, source, 41, 100, 2) == [2]

    box.poll()
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 5)
        assert [row['amount_cents'] for row in repo.list_alerts(conn, source, {})] == [-200, -500, -400, -100, -300]
        assert repo.due_retries(conn, source, 41, 100, 2) == []


def test_uid_windows_make_bounded_progress_on_a_sparse_mailbox(monkeypatch, tmp_path, empty_db, wire):
    port, messages, parse, signed = wire
    _, path = configure(tmp_path, empty_db, port)
    settings = app_settings(empty_db, path)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key
    monkeypatch.setattr(mailbox, 'UID_WINDOW', 4)
    messages.items = {10: signed(), 21: signed(id='later')}
    box = mailbox.RogersMailbox(lambda: settings, parse=parse)
    box.poll()
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 16)
        assert len(repo.list_alerts(conn, source, {})) == 1
    box.poll()
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 21)
        assert len(repo.list_alerts(conn, source, {})) == 2
    assert messages.search_ranges == [(1, 4), (5, 8), (9, 12), (13, 16), (17, 20), (21, 21)]


@pytest.mark.parametrize('override', [{'rogers_alerts_enabled': False}, {'strict_local_mode': True}, {'read_only': True}])
def test_policy_blocks_connect(tmp_path, empty_db, override):
    _, path = configure(tmp_path, empty_db, 993)
    settings = app_settings(empty_db, path, **override)
    box = mailbox.RogersMailbox(lambda: settings, connect=lambda *a, **k: pytest.fail('unexpected dispatch'))
    box.poll_safely()
    assert box.outcome == 'mailbox_disabled'


@pytest.mark.parametrize('mode,consent', [(0o644, mailbox.CONSENT_VERSION), (0o600, 'stale')])
def test_private_file_and_current_consent_are_required(tmp_path, empty_db, mode, consent):
    _, path = configure(tmp_path, empty_db, 993, consent_version=consent)
    path.chmod(mode)
    box = mailbox.RogersMailbox(lambda: app_settings(empty_db, path),
                               connect=lambda *a, **k: pytest.fail('unexpected dispatch'))
    box.poll_safely()
    assert box.outcome == 'setup_required'


@pytest.mark.parametrize('slow_read', [1, 2], ids=['initial-consent', 'dispatch-recheck'])
def test_consent_read_that_exhausts_deadline_blocks_connect(monkeypatch, tmp_path, empty_db, slow_read):
    _, path = configure(tmp_path, empty_db, 993)
    settings = app_settings(empty_db, path)
    now = 100.0
    reads = 0
    dispatched = []
    read = mailbox.private_read

    def read_consent(path):
        nonlocal now, reads
        snapshot = read(path)
        reads += 1
        if reads == slow_read:
            now += mailbox.POLL_BUDGET
        return snapshot

    def connect(*args, **kwargs):
        dispatched.append(True)
        raise TimeoutError

    monkeypatch.setattr(mailbox, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(mailbox, 'private_read', read_consent)
    box = mailbox.RogersMailbox(lambda: settings, connect=connect)
    box.poll_safely()
    assert not dispatched
    assert reads == slow_read
    assert box.outcome == 'retrying'


def test_consent_change_while_fetching_blocks_commit(tmp_path, empty_db, wire):
    port, _, parse, _ = wire
    _, path = configure(tmp_path, empty_db, port)
    settings = app_settings(empty_db, path)
    source = mailbox.MailboxConfig.model_validate_json(path.read_text()).source_key

    def revoke(raw, *args, **kwargs):
        item = parse(raw, *args, **kwargs)
        replacement = path.with_suffix('.next')
        replacement.write_text(json.dumps(json.loads(path.read_text()) | {'consent_version': 'revoked'}))
        replacement.chmod(0o600)
        replacement.replace(path)
        return item

    box = mailbox.RogersMailbox(lambda: settings, parse=revoke)
    box.poll_safely()
    assert box.outcome == 'retrying'
    with engine.read_conn(empty_db) as conn:
        assert repo.cursor(conn, source) == (41, 0)
        assert repo.list_alerts(conn, source, {}) == []
