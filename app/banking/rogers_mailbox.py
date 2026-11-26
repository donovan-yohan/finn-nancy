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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..config import Settings, get_settings
from ..db import engine, repo_rogers_alerts as repo
from .client import BankingError
from .rogers_imap import BoundedIMAP, fetch_message, search_uids
from .rogers_message import Alert, AlertFormat, MessageRejected, MessageRetry, parse_message
from .secrets import private_read

CONSENT_VERSION = 'rogers-forwarded-alerts-v1'
POLL_SECONDS = 60
POLL_BUDGET = 20
BATCH = 8
UID_WINDOW = 4096
WINDOWS_PER_POLL = 4
RETRY_SECONDS = 120


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate JSON key')
        result[name] = value
    return result


class _CardPurchaseMailbox(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)

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


class MailboxConfig(_CardPurchaseMailbox):
    consent_version: Literal['rogers-forwarded-alerts-v1']


class EmailAlertIntake(_CardPurchaseMailbox):
    transport: Literal['imap']
    kind: Literal['card_purchase']


class EmailAlertsConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)

    consent_version: Literal['forwarded-email-alerts-v1']
    intakes: list[EmailAlertIntake] = Field(min_length=1, max_length=4, repr=False)

    @model_validator(mode='after')
    def unique_sources(self):
        if len({intake.source_key for intake in self.intakes}) != len(self.intakes):
            raise ValueError('duplicate mailbox intake scope')
        return self


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

    @staticmethod
    def _enabled(settings):
        return settings.email_alerts_enabled or settings.rogers_alerts_enabled

    @staticmethod
    def _location(settings):
        generic = settings.email_alerts_enabled
        if generic:
            if settings.rogers_alerts_enabled or settings.rogers_alerts_config_file is not None:
                raise BankingError('setup_required')
            path = settings.email_alerts_config_file
        else:
            if settings.email_alerts_config_file is not None:
                raise BankingError('setup_required')
            path = settings.rogers_alerts_config_file
        if path is None:
            raise BankingError('setup_required')
        return path, generic

    def _config(self, path, generic):
        try:
            text = private_read(path)
            data = json.loads(text, object_pairs_hook=_unique_object)
            intakes = (EmailAlertsConfig.model_validate(data).intakes if generic else
                       [MailboxConfig.model_validate(data)])
            return intakes, text
        except (BankingError, ValueError, RecursionError):
            raise BankingError('setup_required') from None

    def _gate(self, settings):
        if self.stopping.is_set() or not self._enabled(settings) or settings.strict_local_mode or settings.read_only:
            raise BankingError('mailbox_disabled')

    def poll(self):
        deadline = time.monotonic() + POLL_BUDGET
        settings = self.settings()
        self._gate(settings)
        path, generic = self._location(settings)
        intakes, snapshot = self._config(path, generic)
        if time.monotonic() >= deadline:
            raise TimeoutError

        def permitted():
            active = self.settings()
            self._gate(active)
            if active.db_path != settings.db_path or self._location(active) != (path, generic):
                raise BankingError('mailbox_configuration_changed')
            try:
                current = private_read(path)
            except (BankingError, UnicodeError):
                raise BankingError('mailbox_configuration_changed') from None
            if current != snapshot:
                raise BankingError('mailbox_configuration_changed')

        def poll_intake(intake, deadline):
            def allowed():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                permitted()
                if time.monotonic() >= deadline:
                    raise TimeoutError

            self._poll_intake(settings.db_path, intake, allowed, deadline)

        outcome = 'checked'
        for index, intake in enumerate(intakes):
            try:
                poll_intake(intake, deadline if index == 0 else time.monotonic() + POLL_BUDGET)
            except BankingError as exc:
                if len(intakes) == 1 or str(exc) in {'mailbox_configuration_changed', 'mailbox_disabled', 'setup_required'}:
                    raise
                outcome = 'retrying'
            except (MessageRetry, OSError, imaplib.IMAP4.error):
                if len(intakes) == 1:
                    raise
                outcome = 'retrying'
        return outcome

    def _poll_intake(self, db_path, config, allowed, deadline):
        source_key = config.source_key
        with engine.read_conn(db_path) as conn:
            expected = repo.cursor(conn, source_key)

        def advance(current, **kwargs):
            nonlocal expected
            with engine.write_tx(db_path) as conn:
                allowed()
                repo.advance(conn, source_key, expected, current, **kwargs)
            expected = current

        def classify(imap, uid) -> _PollResult:
            allowed()
            raw = fetch_message(imap, uid)
            if raw is None:
                return {}
            try:
                item = self.parse(raw, config.format, check_allowed=allowed, deadline=deadline)
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
            # imaplib quotes only the password. The username allowlist excludes
            # quoted-string escapes but permits atom specials such as percent.
            imap.login(f'"{config.username}"', config.password.get_secret_value())
            allowed()
            validity, next_uid = imap.examine_epoch(config.folder)
            upper = next_uid - 1
            if expected is not None and expected[0] == validity and upper < expected[1]:
                raise BankingError('mailbox_protocol_error')
            if expected is None or expected[0] != validity:
                advance((validity, 0))  # Re-scan retained mail after a mailbox epoch change.
            assert expected is not None
            with engine.read_conn(db_path) as conn:
                due = repo.due_retries(conn, source_key, validity, int(self.clock()), 1)

            def retry_once():
                if not due:
                    return
                uid = due.pop()
                result = classify(imap, uid)
                with engine.write_tx(db_path) as conn:
                    allowed()
                    repo.settle_retry(conn, source_key, expected, uid, **result)

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
                    # Commit fresh progress before a slow retry can exhaust the
                    # poll. Give one due pointer a turn before the rest of the batch.
                    retry_once()
                if count == len(candidates) and (not count or candidates[count - 1] < high):
                    advance((validity, high))
                remaining -= count
            retry_once()  # Also retry when no new candidate was found.
        finally:
            # No FLAGS, STORE, CLOSE or EXPUNGE. End the read-only TLS session locally.
            with contextlib.suppress(OSError):
                imap.shutdown()

    def poll_safely(self):
        try:
            self.outcome = self.poll()
        except BankingError as exc:
            self.outcome = str(exc) if str(exc) in {'setup_required', 'mailbox_disabled'} else 'retrying'
        except (MessageRetry, OSError, imaplib.IMAP4.error):
            self.outcome = 'retrying'

    def _scopes(self, settings):
        if not self._enabled(settings):
            return {}
        try:
            intakes, _ = self._config(*self._location(settings))
        except BankingError:
            return {}
        return {intake.source_key: intake.card_accounts for intake in intakes}

    def page(self, before_id=None):
        settings = self.settings()
        scopes = self._scopes(settings)
        with engine.read_conn(settings.db_path) as conn:
            rows = repo.list_alerts(conn, None, {}, before_id, scopes=scopes)
            counts = repo.counts(conn, None, scopes=scopes)
        if not self._enabled(settings) and not rows:
            return None
        paused = not self._enabled(settings) or settings.strict_local_mode or settings.read_only
        return {'rows': rows[:repo.PAGE_SIZE], 'older': rows[repo.PAGE_SIZE - 1]['id'] if len(rows) > repo.PAGE_SIZE else None,
                'outcome': 'paused' if paused else ('setup_required' if not scopes else self.outcome),
                'writable': not settings.read_only, **counts}

    def decide(self, alert_id, **body):
        settings = self.settings()
        with engine.write_tx(settings.db_path) as conn:
            active = self.settings()
            if active.read_only:
                raise BankingError('read_only')
            if active.db_path != settings.db_path:
                raise BankingError('alert_account_changed')
            return repo.decide(conn, alert_id, source_key=None, cards={}, scopes=self._scopes(active), **body)


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
