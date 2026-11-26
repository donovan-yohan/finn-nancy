"""Generated transport and protocol tests; no external network or credentials."""
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.banking import rogers_imap as transport
from app.banking.client import BankingError

_ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 993))


@pytest.mark.parametrize('revoke_at', ['dns', 'socket', 'failed_tcp', 'tcp', 'tls_wrap'])
def test_revocation_fences_the_next_wire_boundary(monkeypatch, revoke_at):
    calls = []
    allowed = True

    def check_allowed():
        if not allowed:
            raise BankingError('mailbox_disabled')

    def resolve(*args, **kwargs):
        nonlocal allowed
        calls.append('dns')
        if revoke_at == 'dns':
            allowed = False
        return [_ADDRESS, _ADDRESS]

    class Socket:
        def __init__(self):
            nonlocal allowed
            if revoke_at == 'socket':
                allowed = False

        def settimeout(self, _):
            pass

        def connect(self, address):
            nonlocal allowed
            assert address == _ADDRESS[-1]
            calls.append('tcp')
            if revoke_at in {'tcp', 'failed_tcp'}:
                allowed = False
            if revoke_at == 'failed_tcp':
                raise ConnectionRefusedError

        def close(self):
            calls.append('close')

    class TLS:
        def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect=True):
            nonlocal allowed
            assert server_hostname == 'fixture.invalid'
            if revoke_at == 'tls_wrap':
                allowed = False
            if do_handshake_on_connect:
                self.do_handshake()
            return self

        def settimeout(self, _):
            pass

        def do_handshake(self):
            calls.append('tls')
            raise AssertionError('TLS must not start after revocation')

        def close(self):
            calls.append('close')

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', lambda *args: Socket())
    monkeypatch.setattr(transport, 'create_default_context', TLS)
    with pytest.raises(BankingError, match='mailbox_disabled'):
        transport.BoundedIMAP('fixture.invalid', 993, deadline=time.monotonic() + 1, check_allowed=check_allowed)
    assert calls == {
        'dns': ['dns'], 'socket': ['dns', 'close'], 'failed_tcp': ['dns', 'tcp', 'close', 'close'],
        'tcp': ['dns', 'tcp', 'close'], 'tls_wrap': ['dns', 'tcp', 'close'],
    }[revoke_at]


@pytest.mark.parametrize('deadline_budget', [0.2, 3])
def test_stalled_dns_wait_leaves_peer_host_free_without_same_host_backlog(monkeypatch, deadline_budget):
    calls = []
    tcp = []
    resolving = threading.Event()
    release = threading.Event()
    blocked = []

    def resolve(host, port, **kwargs):
        calls.append((host, port))
        if len(calls) == 1:
            blocked.append(threading.current_thread())
            resolving.set()
            assert release.wait(3), 'resolver fixture was not released'
        ip = '127.0.0.2' if host == 'peer.invalid' else '127.0.0.1'
        return [(*_ADDRESS[:4], (ip, port))]

    def connect_fixture(host, port, budget):
        return transport.BoundedIMAP(host, port, deadline=time.monotonic() + budget,
                                     check_allowed=lambda: None)

    def fake_connect(address):
        tcp.append(address)
        raise ConnectionRefusedError('fixture reached TCP')

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', lambda *args: SimpleNamespace(
        connect=fake_connect, settimeout=lambda _: None, close=lambda: None,
    ))
    monkeypatch.setattr(transport, 'create_default_context', object)
    if deadline_budget > 1:
        monkeypatch.setattr(transport, '_CALL_TIMEOUT', 0.2)
    finished = threading.Event()
    errors = []

    def first_poll():
        try:
            connect_fixture('fixture.invalid', 993, deadline_budget)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=first_poll, daemon=True)
    caller.start()
    try:
        assert resolving.wait(1)
        assert finished.wait(1), 'poll is still waiting on DNS beyond its deadline'
        assert len(errors) == 1 and type(errors[0]) is TimeoutError
        assert str(errors[0]) == 'mailbox_resolver_timeout'
        assert not tcp
        # Same DNS name across configuration/port changes still owns one slot.
        for host, port in [('FIXTURE.invalid', 993), ('fixture.invalid.', 993), ('fixture.invalid', 1993)]:
            with pytest.raises(TimeoutError, match='mailbox_resolver_busy'):
                connect_fixture(host, port, 1)
        with pytest.raises(ConnectionRefusedError, match='fixture reached TCP'):
            connect_fixture('peer.invalid', 993, 1)
        assert calls == [('fixture.invalid', 993), ('peer.invalid', 993)]
        assert blocked[0].is_alive()
    finally:
        release.set()
        caller.join(2)
        if blocked:
            blocked[0].join(2)
    assert not blocked[0].is_alive()
    assert tcp == [('127.0.0.2', 993)], 'late DNS completion must not reach TCP'

    with pytest.raises(ConnectionRefusedError, match='fixture reached TCP'):
        connect_fixture('FIXTURE.invalid.', 1993, 1)
    assert calls[-1] == ('FIXTURE.invalid.', 1993)
    assert tcp == [('127.0.0.2', 993), ('127.0.0.1', 1993)]


def test_four_distinct_lookups_exhaust_global_capacity_until_a_worker_finishes(monkeypatch):
    hosts = [f'fixture-{index}.invalid' for index in range(5)]
    held = {host: (threading.Event(), threading.Event()) for host in hosts[:4]}
    calls = []

    def resolve(host, port, **kwargs):
        calls.append(host)
        if host in held:
            started, release = held[host]
            started.set()
            assert release.wait(5), 'resolver fixture was not released'
        return [_ADDRESS]

    def lookup(host):
        return transport._resolve(host, 993, time.monotonic() + 3, lambda: None)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with ThreadPoolExecutor(max_workers=4) as callers:
        pending = []
        try:
            for host, (started, _) in held.items():
                result = callers.submit(lookup, host)
                pending.append(result)
                assert started.wait(1), f'{host} could not start while peer lookups were held'
                assert not result.done()
            with pytest.raises(TimeoutError, match='mailbox_resolver_busy'):
                lookup(hosts[4])
            assert calls == hosts[:4]

            held[hosts[0]][1].set()
            assert pending[0].result(timeout=1) == [_ADDRESS]
            assert lookup(hosts[4]) == [_ADDRESS]
            assert not any(result.done() for result in pending[1:])
        finally:
            for _, release in held.values():
                release.set()
    assert all(result.result() == [_ADDRESS] for result in pending)
    assert calls == hosts  # Releasing capacity did not run the refused request.


@pytest.mark.parametrize('guard', ['revoked', 'expired'])
def test_delayed_resolver_rechecks_before_lookup(monkeypatch, guard):
    now = 0
    revoked = False

    class DelayedStart:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            nonlocal now, revoked
            revoked = guard == 'revoked'
            now = 5 if guard == 'expired' else 0
            self.target()

    def check_allowed():
        if revoked:
            raise BankingError('mailbox_disabled')

    monkeypatch.setattr(transport, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(transport.threading, 'Thread', DelayedStart)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('forbidden lookup'))
    with pytest.raises(BankingError if guard == 'revoked' else TimeoutError):
        transport._resolve('fixture.invalid', 993, 2, check_allowed)


@pytest.fixture
def wire_imap(monkeypatch):
    class Wire:
        def __init__(self):
            self.incoming = bytearray(b'* PREAUTH fixture\r\n')
            self.responses = b''
            self.examine = b'* 1 EXISTS\r\n'
            self.commands = []

        def makefile(self, _):
            return self

        def settimeout(self, _):
            pass

        def sendall(self, frame):
            tag, command = frame[:-2].split(b' ', 1)
            self.commands.append(command)
            responses = {
                b'CAPABILITY': b'* CAPABILITY IMAP4rev1\r\n',
                b'EXAMINE INBOX': self.examine,
            }
            self.incoming.extend(responses.get(command, self.responses) + tag + b' OK done\r\n')

        def recv(self, size):
            chunk = bytes(self.incoming[:size])
            del self.incoming[:size]
            return chunk

    wire = Wire()
    monkeypatch.setattr(transport.BoundedIMAP, '_create_socket', lambda *_: wire)
    imap = transport.BoundedIMAP('fixture.invalid', 993, deadline=time.monotonic() + 2, check_allowed=lambda: None)
    assert imap.select('INBOX', readonly=True)[0] == 'OK'
    return imap, wire


def test_examine_reads_only_valid_metadata_for_this_selection(wire_imap):
    imap, wire = wire_imap
    wire.examine += b'* OK [UIDVALIDITY 9] epoch\r\n* OK [UIDNEXT 20] next\r\n* OK [UIDNEXT 20] next\r\n'
    assert imap.examine_epoch('INBOX') == (9, 20)
    wire.examine = b'* 1 EXISTS\r\n'
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        imap.examine_epoch('INBOX')


@pytest.mark.parametrize('metadata', [
    b'* OK [UIDNEXT 20] next\r\n* OK [UIDNEXT 21] next\r\n',
    b'* NO [UIDNEXT 20] next\r\n',
    b'* UIDNEXT 20\r\n',
    b'* OK [UIDNEXT 20] next\r\n* UIDNEXT 20\r\n',
    b'* OK [UIDNEXT 0] next\r\n',
])
def test_examine_refuses_ambiguous_metadata(wire_imap, metadata):
    imap, wire = wire_imap
    wire.examine += b'* OK [UIDVALIDITY 9] epoch\r\n' + metadata
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        imap.examine_epoch('INBOX')


@pytest.mark.parametrize(('responses', 'expected'), [
    (b'', None),
    (b'* 1 FETCH (UID 17 BODY[]<0> NIL)\r\n', None),
    (b'* 1 FETCH (BODY[] NIL UID 17)\r\n', None),
    (b'* 2 FETCH (FLAGS (\\Seen))\r\n* 3 FETCH (FLAGS () UID 29)\r\n', None),
    (b'* 1 FETCH (UID 17 BODY[]<0> "NIL")\r\n', b'NIL'),
    (b'* 1 FETCH (BODY[]<0> "a\\\"b\\\\c" UID 17)\r\n', b'a"b\\c'),
    (b'* 1 FETCH (UID 17 BODY[]<0> {3}\r\nabc)\r\n', b'abc'),
    (b'* 2 FETCH (UID 29 FLAGS (\\Seen))\r\n* 1 FETCH (BODY[]<0> {3}\r\nabc UID 17 FLAGS (UID 32))\r\n'
     b'* 4 FETCH (FLAGS ())\r\n', b'abc'),
])
def test_fetch_accepts_only_body_bound_to_the_requested_uid(wire_imap, responses, expected):
    imap, wire = wire_imap
    wire.responses = responses
    assert transport.fetch_message(imap, 17) == expected
    assert wire.commands == [b'CAPABILITY', b'EXAMINE INBOX',
                             f'UID FETCH 17 (UID BODY.PEEK[]<0.{transport.MAX_MESSAGE_BYTES + 1}>)'.encode()]


@pytest.mark.parametrize('responses', [
    b'* 1 FETCH (UID 18 BODY[]<0> {3}\r\nabc)\r\n',
    b'* 1 FETCH (FLAGS (UID 17) BODY[]<0> {3}\r\nabc)\r\n',
    b'* 1 FETCH (UID 17 UID 17 BODY[]<0> {3}\r\nabc)\r\n',
    b'* 1 FETCH (UID 17 BODY[]<0> NIL)\r\n* 2 FETCH (UID 17 BODY[]<0> {3}\r\nabc)\r\n',
    b'* 1 FETCH (UID 17 BODY[]<0> NIL BODY[]<0> {3}\r\nabc)\r\n',
    b'* 1 FETCH (BODY[]<0> {3}\r\nabc UID 17\r\n',
    b'* 1 FETCH (UID 17 BODY[]<0> "a\\qb")\r\n',
    b'* 1 FETCH (UID 17 BODY[]<1> "abc")\r\n',
    b'* 1 FETCH (UID 17)\r\n',
    b'* FETCH 1 (UID 17 BODY[]<0> {3}\r\nabc)\r\n',
    b'* OK [FETCH 1 (UID 17 BODY[]<0> "abc")] ignored\r\n',
])
def test_fetch_fails_closed_on_ambiguous_or_malformed_rows(wire_imap, responses):
    imap, wire = wire_imap
    wire.responses = responses
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        transport.fetch_message(imap, 17)


def test_search_uses_only_the_explicit_uid_range(wire_imap):
    imap, wire = wire_imap
    wire.responses = b'* SEARCH 19 17 19\r\n'
    assert transport.search_uids(imap, 17, 19, 'fixture@example.invalid') == [17, 19]
    assert wire.commands[-1] == b'UID SEARCH UID 17:19 FROM "fixture@example.invalid"'


def test_search_accepts_a_legitimate_empty_result(wire_imap):
    imap, wire = wire_imap
    wire.responses = b'* SEARCH\r\n'
    assert transport.search_uids(imap, 17, 19, 'fixture@example.invalid') == []


@pytest.mark.parametrize(('lo', 'hi'), [(0, 1), (2, 1), (1, '*'), (1, 2**32)])
def test_search_rejects_invalid_ranges_before_sending(wire_imap, lo, hi):
    imap, wire = wire_imap
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        transport.search_uids(imap, lo, hi, 'fixture@example.invalid')
    assert wire.commands == [b'CAPABILITY', b'EXAMINE INBOX']


@pytest.mark.parametrize('responses', [
    b'* SEARCH 16 17\r\n', b'* SEARCH 20\r\n', b'* SEARCH 017\r\n', b'* SEARCH 17\t18\r\n',
    b'* SEARCH 17\r\n* SEARCH 18\r\n', b'* OK [SEARCH 17] ignored\r\n', b'* SEARCHwrong\r\n', b'* SEARCH \r\n',
])
def test_search_rejects_out_of_range_and_malformed_results(wire_imap, responses):
    imap, wire = wire_imap
    wire.responses = responses
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        transport.search_uids(imap, 17, 19, 'fixture@example.invalid')


def test_search_rejects_numbered_response_even_with_in_range_uids(wire_imap):
    imap, wire = wire_imap
    wire.responses = b'* 1 SEARCH 17\r\n'
    with pytest.raises(BankingError, match='mailbox_protocol_error'):
        transport.search_uids(imap, 1, 19, 'fixture@example.invalid')
