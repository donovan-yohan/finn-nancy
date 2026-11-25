"""Generated transport and protocol tests; no external network or credentials."""
import socket
import threading
import time

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
def test_stalled_dns_wait_is_bounded_without_resolver_backlog(monkeypatch, deadline_budget):
    calls = []
    resolving = threading.Event()
    release = threading.Event()
    blocked = []

    def resolve(*args, **kwargs):
        calls.append('dns')
        if len(calls) == 1:
            blocked.append(threading.current_thread())
            resolving.set()
            assert release.wait(2), 'resolver fixture was not released'
        return [_ADDRESS]

    def connect_fixture(budget):
        return transport.BoundedIMAP('fixture.invalid', 993, deadline=time.monotonic() + budget,
                                     check_allowed=lambda: None)

    def socket_forbidden(*args):
        pytest.fail('no address from an abandoned DNS lookup may reach TCP')

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', socket_forbidden)
    monkeypatch.setattr(transport, 'create_default_context', object)
    if deadline_budget > 1:
        monkeypatch.setattr(transport, '_CALL_TIMEOUT', 0.2)
    finished = threading.Event()
    errors = []

    def first_poll():
        try:
            connect_fixture(deadline_budget)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=first_poll, daemon=True)
    caller.start()
    try:
        assert resolving.wait(1)
        assert finished.wait(1), 'poll is still waiting on DNS beyond its deadline'
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        for _ in range(3):
            with pytest.raises(TimeoutError):
                connect_fixture(0.2)
        assert calls == ['dns']
        assert blocked[0].is_alive()
    finally:
        release.set()
        caller.join(2)
        if blocked:
            blocked[0].join(2)
    assert not blocked[0].is_alive()

    def socket_recovers(*args):
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, 'socket', socket_recovers)
    with pytest.raises(ConnectionRefusedError):
        connect_fixture(1)
    assert calls == ['dns', 'dns']


@pytest.fixture
def wire_imap(monkeypatch):
    class Wire:
        def __init__(self):
            self.incoming = bytearray(b'* PREAUTH fixture\r\n')
            self.responses = b''
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
                b'EXAMINE INBOX': b'* 1 EXISTS\r\n',
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
