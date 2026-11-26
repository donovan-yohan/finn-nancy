"""Bounded IMAP/TLS transport for read-only Rogers alert intake."""
from __future__ import annotations

import contextlib
import imaplib
import re
import socket
import threading
import time
from ssl import create_default_context

from .client import BankingError
from .rogers_message import MAX_MESSAGE_BYTES

MAX_UID = 2**32 - 1
WIRE_LIMIT = 3 * 1024 * 1024
_CALL_TIMEOUT = 4
# System getaddrinfo cannot be interrupted. Cap outstanding work across config
# changes, without letting one stuck host occupy every slot or queueing lookups.
_DNS_LIMIT = 4
_DNS_LOCK = threading.Lock()
_DNS_HOSTS: set[str] = set()
_DATA_FRAME = re.compile(rb'\* (?:(\d+) )?(FETCH|SEARCH)(?![A-Z-])')
_MISFRAMED_CODE = re.compile(rb'[^ ]+ (?:OK|NO|BAD) \[(?:FETCH|SEARCH)(?=[ \]])')
_FETCH_START = re.compile(rb'([1-9][0-9]{0,9}) \(')
_FETCH_ATTR = re.compile(rb'(UID|FLAGS|BODY\[\](?:<0>)?) ', re.I)
_UID_TOKEN = re.compile(rb'[1-9][0-9]{0,9}')
_FLAG = rb'\\?[^\x00-\x20\x7f-\xff(){%*"\\\]]++'
_FLAGS = re.compile(rb'\((?:' + _FLAG + rb'(?: ' + _FLAG + rb')*+)?\)')
_QUOTED = re.compile(rb'"([^"\\\x00\r\n\x80-\xff]*+(?:\\["\\][^"\\\x00\r\n\x80-\xff]*+)*+)"')
_QUOTED_PAIR = re.compile(rb'\\(["\\])')
_LITERAL = re.compile(rb'\{([0-9]{1,9})\}')
_MAILBOX_CODES = ('UIDVALIDITY', 'UIDNEXT')
_MAILBOX_CODE = re.compile(rb'[^ ]+ (OK|NO|BAD|PREAUTH|BYE) \[(UIDVALIDITY|UIDNEXT)(?=[ \]])', re.I)
_MAILBOX_VALUE = re.compile(rb' ([1-9][0-9]{0,9})\] [^\x00\r\n]+')


def _protocol_error():
    return BankingError('mailbox_protocol_error')


def _check_frame(line):
    # imaplib conflates response codes with data frames and can accept a
    # data frame under the wrong token or with the wrong numbering form.
    if _MISFRAMED_CODE.match(line):
        raise _protocol_error()
    frame = _DATA_FRAME.match(line)
    if frame:
        end = frame.end()
        if ((frame[1] is not None) != (frame[2] == b'FETCH')
                or line[end:end + 1] not in (b'', b' ') or end + 1 == len(line)):
            raise _protocol_error()


def _resolve(host, port, deadline, check_allowed):
    # Key by DNS name alone, not port or intake. Keep the configured spelling
    # for getaddrinfo and TLS; the key is only for admission.
    key = host.lower().rstrip('.')
    done = threading.Event()
    result = []
    errors = []

    def release():
        with _DNS_LOCK:
            _DNS_HOSTS.remove(key)

    def lookup():
        try:
            check_allowed()
            if time.monotonic() >= deadline:
                raise TimeoutError('mailbox_resolver_timeout')
            result.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
        except Exception as exc:
            errors.append(exc)
        finally:
            release()
            done.set()

    with _DNS_LOCK:
        if key in _DNS_HOSTS or len(_DNS_HOSTS) >= _DNS_LIMIT:
            raise TimeoutError('mailbox_resolver_busy')
        _DNS_HOSTS.add(key)
    try:
        threading.Thread(target=lookup, name='rogers-imap-dns', daemon=True).start()
    except Exception:
        release()
        raise
    # Only the waiting caller can use the address list. Timing out does not
    # release the reservation; the worker still owns it until it finishes.
    left = deadline - time.monotonic()
    if left <= 0 or not done.wait(left) or time.monotonic() >= deadline:
        raise TimeoutError('mailbox_resolver_timeout')
    if errors:
        raise errors[0]
    return result


class BoundedIMAP(imaplib.IMAP4_SSL):
    """Stdlib protocol with operation, byte, and elapsed-time bounds."""

    def __init__(self, host, port, *, deadline, check_allowed):
        self.deadline = deadline
        self.check_allowed = check_allowed
        self.remaining_bytes = WIRE_LIMIT
        self.buffer = bytearray()
        self._examine_codes = None
        super().__init__(host, port, ssl_context=create_default_context(), timeout=_CALL_TIMEOUT)

    def _timeout(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return min(_CALL_TIMEOUT, left)

    def _open_tcp(self):
        self.check_allowed()
        addresses = _resolve(self.host, self.port, min(self.deadline, time.monotonic() + self._timeout()),
                             self.check_allowed)
        self.check_allowed()
        error = OSError('mailbox_no_address')
        for family, kind, protocol, _, address in addresses:
            self._timeout()
            try:
                sock = socket.socket(family, kind, protocol)
            except OSError as exc:
                error = exc
                continue
            connected = False
            try:
                self.check_allowed()
                sock.settimeout(self._timeout())
                try:
                    sock.connect(address)
                except OSError as exc:
                    error = exc
                    continue
                connected = True
                return sock
            finally:
                if not connected:
                    with contextlib.suppress(OSError):
                        sock.close()
        raise error

    def _create_socket(self, timeout):
        sock = self._open_tcp()
        try:
            sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.check_allowed()
            sock.settimeout(self._timeout())
            sock.do_handshake()
        except BaseException:
            with contextlib.suppress(OSError):
                sock.close()
            raise
        return sock

    def _log(self, *args):
        pass  # imaplib's debug buffer would hold LOGIN and message fragments.

    def _mesg(self, *args):
        pass

    def _limit(self):
        self.sock.settimeout(self._timeout())

    def _receive(self):
        self._limit()
        if self.remaining_bytes <= 0:
            raise imaplib.IMAP4.abort('mailbox_response_limit')
        chunk = self.sock.recv(min(16_384, self.remaining_bytes))
        if not chunk:
            raise imaplib.IMAP4.abort('mailbox_response_ended')
        self.remaining_bytes -= len(chunk)
        self.buffer.extend(chunk)

    def send(self, data):
        self.check_allowed()
        self._limit()
        super().send(data)

    def read(self, size):
        if size > MAX_MESSAGE_BYTES + 1:
            raise imaplib.IMAP4.abort('mailbox_literal_limit')
        while len(self.buffer) < size:
            self._receive()
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def readline(self):
        while True:
            index = self.buffer.find(b'\n')
            if index != -1:
                if index > MAX_MESSAGE_BYTES:
                    raise imaplib.IMAP4.abort('mailbox_line_limit')
                value = bytes(self.buffer[:index + 1])
                del self.buffer[:index + 1]
                if value.endswith(b'\r\n'):
                    _check_frame(value[:-2])
                return value
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                raise imaplib.IMAP4.abort('mailbox_line_limit')
            self._receive()

    def _get_response(self):
        line = super()._get_response()  # type: ignore[reportAttributeAccessIssue]  # imaplib private hook
        if self._examine_codes is not None and line is not None:
            marker = _MAILBOX_CODE.match(line)
            if marker:
                value = _MAILBOX_VALUE.fullmatch(line, marker.end())
                if marker[1] != b'OK' or not value or not (line.startswith(b'* ') or self.tagre.match(line)):
                    raise _protocol_error()
                self._examine_codes[marker[2].decode().upper()].append(value[1])
        return line

    def examine_epoch(self, folder):
        # imaplib folds untagged data and response codes into the same buckets.
        # Cross-check those buckets against actual OK frames from this EXAMINE.
        codes: dict[str, list[bytes]] = {name: [] for name in _MAILBOX_CODES}
        self._examine_codes = codes
        try:
            status, _ = self.select(folder, readonly=True)
            if status != 'OK':
                raise _protocol_error()
            values = []
            for name, observed in codes.items():
                _, data = self.response(name)
                if data != observed or len(set(observed)) != 1:
                    raise _protocol_error()
                values.append(_uid(observed[0]))
            return values[0], values[1]
        finally:
            self._examine_codes = None


def _uid(value):
    if not isinstance(value, bytes) or not _UID_TOKEN.fullmatch(value):
        raise _protocol_error()
    number = int(value)
    if number > MAX_UID:
        raise _protocol_error()
    return number


def search_uids(imap, lo, hi, sender):
    if type(lo) is not int or type(hi) is not int or not 1 <= lo <= hi <= MAX_UID:
        raise _protocol_error()
    if not isinstance(sender, str) or not re.fullmatch(r'[ -~]+', sender):
        raise _protocol_error()
    # Server-side filtering only avoids unrelated fetches; it proves no origin.
    quoted = '"' + sender.replace('\\', '\\\\').replace('"', '\\"') + '"'
    status, data = imap.uid('SEARCH', None, 'UID', f'{lo}:{hi}', 'FROM', quoted)
    if status != 'OK' or len(data) != 1 or not isinstance(data[0], bytes):
        raise _protocol_error()
    candidates = sorted({_uid(value) for value in data[0].split(b' ')}) if data[0] else []
    if candidates and (candidates[0] < lo or candidates[-1] > hi):
        raise _protocol_error()
    return candidates


def _fetch_row(row, uid, *, literal=None, literal_end=None):
    start = _FETCH_START.match(row)
    if not start:
        raise _protocol_error()
    _uid(start[1])  # Validate the sequence number, not the body owner.
    pos = start.end()
    owner, body = None, None
    flags_seen = body_seen = literal_used = False
    while True:
        attribute = _FETCH_ATTR.match(row, pos)
        if not attribute:
            raise _protocol_error()
        kind, pos = attribute[1].upper(), attribute.end()
        if kind == b'UID':
            value = _UID_TOKEN.match(row, pos)
            if owner is not None or not value:
                raise _protocol_error()
            owner, pos = _uid(value[0]), value.end()
        elif kind == b'FLAGS':
            value = _FLAGS.match(row, pos)
            if flags_seen or not value:
                raise _protocol_error()
            flags_seen, pos = True, value.end()
        else:
            if body_seen:
                raise _protocol_error()
            body_seen = True
            if row[pos:pos + 3].upper() == b'NIL':
                pos += 3
            elif kind != b'BODY[]<0>':
                raise _protocol_error()
            elif value := _QUOTED.match(row, pos):
                body, pos = _QUOTED_PAIR.sub(rb'\1', value[1]), value.end()
            elif value := _LITERAL.match(row, pos):
                if (literal is None or value.end() != literal_end
                        or int(value[1]) != len(literal)):
                    raise _protocol_error()
                body, pos, literal_used = literal, value.end(), True
            else:
                raise _protocol_error()
        if row[pos:pos + 1] == b' ':
            pos += 1
        elif pos + 1 == len(row) and row[pos:] == b')':
            break
        else:
            raise _protocol_error()
    if literal is not None and not literal_used:
        raise _protocol_error()
    if body_seen:
        if owner != uid or (body is not None and len(body) > MAX_MESSAGE_BYTES + 1):
            raise _protocol_error()
    elif not flags_seen:  # Only a well-formed unsolicited flags update is ignorable.
        raise _protocol_error()
    return body_seen, body


def fetch_message(imap, uid):
    if type(uid) is not int or not 1 <= uid <= MAX_UID:
        raise _protocol_error()
    request = f'(UID BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)'
    status, data = imap.uid('FETCH', str(uid), request)
    if status != 'OK' or not data:
        raise _protocol_error()
    if data == [None]:
        return None
    rows = iter(data)
    found, body = False, None
    for row in rows:
        if isinstance(row, tuple) and len(row) == 2:
            # imaplib emits one (prefix, literal) tuple and one trailer for a
            # single-literal row. Never borrow a UID from a later FETCH row.
            header, literal = row
            trailer = next(rows, None)
            if not all(isinstance(value, bytes) for value in (header, literal, trailer)):
                raise _protocol_error()
            has_body, value = _fetch_row(header + trailer, uid, literal=literal, literal_end=len(header))
        elif isinstance(row, bytes):
            has_body, value = _fetch_row(row, uid)
        else:
            raise _protocol_error()
        if has_body:
            if found:
                raise _protocol_error()
            found, body = True, value
    return body
