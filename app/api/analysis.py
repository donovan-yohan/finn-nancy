"""Bounded read-only SQL over explicit financial projections, never raw storage."""
from __future__ import annotations

import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
from time import monotonic
from urllib.parse import quote

from ..config import get_settings

# Private connector, settings, raw evidence and filesystem references are not SQL surfaces.
RELATIONS = {
    'analysis_transactions': ('transactions', ('id', 'account_id', 'posted_on', 'description',
        'counterparty', 'amount_cents', 'flow_kind', 'recon_status', 'source', 'source_document_id')),
    'analysis_splits': ('transaction_splits', ('id', 'transaction_id', 'category_id', 'amount_cents', 'memo')),
    'analysis_accounts': ('accounts', ('id', 'name', 'kind', 'currency', 'is_active')),
    'analysis_categories': ('categories', ('id', 'name', 'kind')),
}
FUNCTIONS = frozenset(('abs', 'avg', 'count', 'sum', 'total', 'min', 'max', 'round',
    'coalesce', 'ifnull', 'nullif', 'lower', 'upper', 'length', 'substr', 'substring',
    'trim', 'ltrim', 'rtrim', 'instr', 'replace', 'typeof', 'date', 'strftime', 'julianday'))
CAPACITY = threading.BoundedSemaphore(4)
MAX_OUTPUT = 500_000
MAX_STEPS = 1_000_000


def schema() -> dict:
    return {'relations': {name: list(columns) for name, (_, columns) in RELATIONS.items()}}


def _authorize(internal, action, first, second, database, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # SQLite's non-flattened COUNT view read omits a database/column name.
        if database is None and first in RELATIONS and second == '':
            return sqlite3.SQLITE_OK
        if database == 'temp' and first in RELATIONS and (second in RELATIONS[first][1] or second == ''):
            return sqlite3.SQLITE_OK
        # Internal projections are connection-local capabilities, not caller-known
        # CTE names. Only their matching public wrapper may read them.
        if database == 'temp' and first in internal:
            public, _, columns = internal[first]
            if source == public and second in columns:
                return sqlite3.SQLITE_OK
        if source in internal:
            _, table, columns = internal[source]
            if database == 'main' and first == table and second in columns:
                return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query(*, sql: str, parameters: dict | None = None, limit: int = 100) -> dict:
    if not isinstance(sql, str) or not sql.strip() or len(sql.encode('utf-8')) > 16_000:
        raise ValueError('query SQL must contain 1 to 16000 bytes')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('query limit must be between 1 and 500')
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict) or len(parameters) > 50:
        raise ValueError('query parameters must be a bounded object')
    for name, value in parameters.items():
        if (not isinstance(name, str) or len(name) > 100 or type(value) not in (str, int, float, type(None))
                or isinstance(value, str) and len(value.encode('utf-8')) > 10_000
                or type(value) is int and not -(2**63) <= value < 2**63
                or type(value) is float and not math.isfinite(value)):
            raise ValueError('query parameter is invalid')
    if not CAPACITY.acquire(blocking=False):
        raise ValueError('query capacity exceeded')
    conn = None
    try:
        path = Path(get_settings().db_path).resolve()
        conn = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=.1)
        conn.enable_load_extension(False)
        # Mint fresh internal names so caller CTEs cannot forge raw-read provenance.
        internal = {}
        for name, (table, columns) in RELATIONS.items():
            private = 'financial_' + secrets.token_hex(16)
            internal[private] = (name, table, columns)
            # Prevent flattening from losing view provenance on row-count reads.
            conn.execute(f'CREATE TEMP VIEW {private} AS SELECT {",".join(columns)} FROM main.{table} LIMIT -1 OFFSET 0')
            conn.execute(f'CREATE TEMP VIEW {name} AS SELECT {",".join(columns)} FROM temp.{private} LIMIT -1 OFFSET 0')
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_OUTPUT)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        conn.set_authorizer(lambda *args: _authorize(internal, *args))
        deadline = monotonic() + 1
        steps = 0

        def progress():
            nonlocal steps
            steps += 1000
            return int(steps >= MAX_STEPS or monotonic() >= deadline)

        conn.set_progress_handler(progress, 1000)
        cursor = conn.execute(sql, parameters)
        if cursor.description is None:
            raise ValueError('query is not an allowed financial read')
        columns = [item[0] for item in cursor.description]
        rows = []
        # Include the entire response envelope before stepping even a zero-row
        # result. ASCII escaping conservatively bounds FastAPI's UTF-8 encoding.
        output_bytes = len(json.dumps({'columns': columns, 'rows': [],
            'row_count': 0, 'truncated': False}, ensure_ascii=True,
            allow_nan=False, separators=(',', ':')).encode('utf-8'))
        if output_bytes > MAX_OUTPUT:
            raise ValueError('query output limit exceeded')
        for row in cursor:
            if len(rows) == limit:
                return {'columns': columns, 'rows': rows, 'row_count': len(rows), 'truncated': True}
            if any(isinstance(v, bytes) or isinstance(v, float) and not math.isfinite(v) for v in row):
                raise ValueError('query result is not JSON financial data')
            encoded = json.dumps(list(row), ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            output_bytes += (len(encoded.encode('utf-8')) + bool(rows)
                + len(str(len(rows) + 1)) - len(str(len(rows))))
            if output_bytes > MAX_OUTPUT:
                raise ValueError('query output limit exceeded')
            rows.append(list(row))
        return {'columns': columns, 'rows': rows, 'row_count': len(rows), 'truncated': False}
    except sqlite3.Error as exc:
        code = getattr(exc, 'sqlite_errorcode', None)
        if code == sqlite3.SQLITE_INTERRUPT:
            raise ValueError('query work limit exceeded') from None
        if code == sqlite3.SQLITE_TOOBIG:
            raise ValueError('query output limit exceeded') from None
        # Never expose SQL text, schema errors, paths or SQLite diagnostics.
        raise ValueError('query is not an allowed financial read') from None
    finally:
        if conn is not None:
            conn.close()
        CAPACITY.release()
