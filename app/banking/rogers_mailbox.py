"""Opt-in read-only IMAP intake for separately reviewed authorization evidence."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import imaplib
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from ..config import Settings, get_settings
from ..db import engine, repo_rogers_alerts as repo
from .client import BankingError
from .rogers_imap import BoundedIMAP, _uid, fetch_message, search_uids
from .rogers_message import Alert, AlertFormat, MessageRejected, MessageRetry, parse_message
from .secrets import private_read

CONSENT_VERSION = 'rogers-forwarded-alerts-v1'
POLL_SECONDS = 60
POLL_BUDGET = 20
BATCH = 8
UID_WINDOW = 4096
WINDOWS_PER_POLL = 4
RETRY_SECONDS = 120


class MailboxConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)

    consent_version: Literal['rogers-forwarded-alerts-v1']
    host: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,252}$')
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(pattern=r'^[A-Za-z0-9._+@%\-]{1,254}$', repr=False)
    password: SecretStr = Field(repr=False)
    folder: str = Field(default='INBOX', pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,100}$')
    format: AlertFormat = Field(repr=False)
    card_accounts: dict[str, int] = Field(default_factory=dict, max_length=64, repr=False)

    @field_validator('password')
    @classmethod
    def imap_password(cls, value):
        if not re.fullmatch(r'[ -~]{1,1024}', value.get_secret_value()):
            raise ValueError('invalid IMAP password shape')
        return value

    @field_validator('card_accounts')
    @classmethod
    def card_scope(cls, value):
        if any(not re.fullmatch(r'[0-9]{4}', last4) or not 0 < account_id < 2**63
               for last4, account_id in value.items()):
            raise ValueError('invalid card mapping')
        return value

    @property
    def source_key(self):
        folder = 'INBOX' if self.folder.upper() == 'INBOX' else self.folder
        identity = json.dumps([self.host.lower().rstrip('.'), self.port, self.username, folder,
                               self.format.sender, self.format.recipient, self.format.signing_domain],
                              separators=(',', ':'))
        return hashlib.sha256(identity.encode()).hexdigest()


class _PollResult(TypedDict, total=False):
    alert: Alert
    rejected: bool
    retry_at: int


class RogersMailbox:
    def __init__(self, settings: Callable[[], Settings] = get_settings, *,
                 connect: Callable = BoundedIMAP, parse: Callable = parse_message, clock=time.time):
        self.settings = settings
        self.connect = connect
        self.parse = parse
        self.clock = clock
        self.stopping = threading.Event()
        self.outcome = None

    def _config(self, settings):
        path = settings.rogers_alerts_config_file
        if path is None:
            raise BankingError('setup_required')
        try:
            text = private_read(path)
            return MailboxConfig.model_validate_json(text), text
        except (BankingError, UnicodeError, ValidationError):
            raise BankingError('setup_required') from None

    def _gate(self, settings):
        if self.stopping.is_set() or not settings.rogers_alerts_enabled or settings.strict_local_mode or settings.read_only:
            raise BankingError('mailbox_disabled')

    def poll(self):
        settings = self.settings()
        self._gate(settings)
        config, snapshot = self._config(settings)
        config_path = settings.rogers_alerts_config_file
        assert config_path is not None  # _config requires a private file.
        source_key = config.source_key
        deadline = time.monotonic() + POLL_BUDGET

        def allowed():
            if time.monotonic() >= deadline:
                raise TimeoutError
            active = self.settings()
            self._gate(active)
            if (active.db_path != settings.db_path or active.rogers_alerts_config_file != config_path
                    or private_read(config_path) != snapshot):
                raise BankingError('mailbox_configuration_changed')

        with engine.read_conn(settings.db_path) as conn:
            expected = repo.cursor(conn, source_key)

        def advance(current, **kwargs):
            nonlocal expected
            with engine.write_tx(settings.db_path) as conn:
                allowed()
                repo.advance(conn, source_key, expected, current, **kwargs)
            expected = current

        def classify(imap, uid) -> _PollResult:
            allowed()
            raw = fetch_message(imap, uid)
            if raw is None:
                return {}
            try:
                item = self.parse(raw, config.format, check_allowed=allowed)
            except MessageRejected:
                return {'rejected': True}
            except MessageRetry:
                # Keep an opaque retry pointer, not mail bytes. A transient DNS
                # issue with this UID must not block subsequent messages.
                return {'retry_at': int(self.clock()) + RETRY_SECONDS}
            return {'alert': item}

        allowed()
        imap = self.connect(config.host, config.port, deadline=deadline, check_allowed=allowed)
        try:
            allowed()
            imap.login(config.username, config.password.get_secret_value())
            allowed()
            status, _ = imap.select(config.folder, readonly=True)
            if status != 'OK':
                raise BankingError('mailbox_protocol_error')
            validity = _uid(imap.response('UIDVALIDITY')[1][-1])
            upper = _uid(imap.response('UIDNEXT')[1][-1]) - 1
            if expected is None or expected[0] != validity:
                advance((validity, 0))  # Re-scan retained mail after a mailbox epoch change.
            assert expected is not None
            # One old retry per pass reserves room for new mail even on a
            # persistent DNS failure. Rotating retry_at provides fairness.
            with engine.read_conn(settings.db_path) as conn:
                retry = repo.due_retries(conn, source_key, validity, int(self.clock()), 1)
            if retry:
                result = classify(imap, retry[0])
                with engine.write_tx(settings.db_path) as conn:
                    allowed()
                    repo.settle_retry(conn, source_key, expected, retry[0], **result)

            remaining = BATCH
            for _ in range(WINDOWS_PER_POLL):
                lower = expected[1] + 1
                if not remaining or lower > upper:
                    break
                high = min(upper, lower + UID_WINDOW - 1)
                allowed()
                candidates = search_uids(imap, lower, high, config.format.sender)
                count = min(len(candidates), remaining)
                for uid in candidates[:count]:
                    advance((validity, uid), **classify(imap, uid))
                if count == len(candidates) and (not count or candidates[count - 1] < high):
                    advance((validity, high))
                remaining -= count
        finally:
            # No FLAGS, STORE, CLOSE or EXPUNGE. End the read-only TLS session locally.
            with contextlib.suppress(OSError):
                imap.shutdown()

    def poll_safely(self):
        try:
            self.poll()
        except BankingError as exc:
            self.outcome = str(exc) if str(exc) in {'setup_required', 'mailbox_disabled'} else 'retrying'
        except (MessageRetry, OSError, imaplib.IMAP4.error):
            self.outcome = 'retrying'
        else:
            self.outcome = 'checked'

    def _mapping(self, settings):
        if not settings.rogers_alerts_enabled:
            return None, {}
        try:
            config, _ = self._config(settings)
        except BankingError:
            return None, {}
        return config.source_key, config.card_accounts

    def page(self, before_id=None):
        settings = self.settings()
        scope, cards = self._mapping(settings)
        with engine.read_conn(settings.db_path) as conn:
            rows = repo.list_alerts(conn, scope, cards, before_id)
            counts = repo.counts(conn, scope)
        if not settings.rogers_alerts_enabled and not rows:
            return None
        paused = not settings.rogers_alerts_enabled or settings.strict_local_mode or settings.read_only
        return {'rows': rows[:repo.PAGE_SIZE], 'older': rows[repo.PAGE_SIZE - 1]['id'] if len(rows) > repo.PAGE_SIZE else None,
                'outcome': 'paused' if paused else ('setup_required' if scope is None else self.outcome),
                'writable': not settings.read_only, **counts}

    def decide(self, alert_id, **body):
        settings = self.settings()
        with engine.write_tx(settings.db_path) as conn:
            if self.settings().read_only:
                raise BankingError('read_only')
            scope, cards = self._mapping(settings)
            return repo.decide(conn, alert_id, source_key=scope, cards=cards, **body)


async def run_mailbox(stop: asyncio.Event, mailbox: RogersMailbox):
    while not stop.is_set():
        try:
            await asyncio.to_thread(mailbox.poll_safely)
        except Exception:  # Keep polling without exposing mailbox/server content in logs.
            mailbox.outcome = 'retrying'
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass
