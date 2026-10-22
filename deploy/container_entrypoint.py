"""Read-only admission for the immutable image; never initialize persistent state."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from app.config import get_settings


def main(args: list[str] | None = None) -> int:
    if args is None:
        args = sys.argv[1:]
    if args:
        print("container startup refused: arguments are not supported", file=sys.stderr)
        return 1
    try:
        settings = get_settings()
        host, separator, port = settings.addr.rpartition(":")
        if not separator or not host or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError
        db = settings.db_path
        data = settings.data_dir
        if not db.is_absolute() or not data.is_absolute():
            raise ValueError
        if any(part.is_symlink() for path in (db, data) for part in (path, *path.parents)):
            raise ValueError
        if data == Path(data.anchor) or not db.is_relative_to(data) or ".." in db.parts or ".." in data.parts:
            raise ValueError
        if not data.is_dir() or not db.is_file():
            raise ValueError
        if not os.access(data, os.W_OK | os.X_OK) or not os.access(db, os.R_OK | os.W_OK):
            raise ValueError
        if not os.access(db.parent, os.W_OK | os.X_OK):
            raise ValueError
        from app.db.migrate import MIGRATIONS_DIR, read_database_identity
        from app.diagnostics import applied_migration_sequence, migration_status

        expected = tuple(path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql")))
        if not expected or expected[:2] != ("001_tables.sql", "002_views.sql"):
            raise ValueError
        if any(not (MIGRATIONS_DIR / name).stat().st_size for name in expected):
            raise ValueError
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            if not migration_status(expected, applied_migration_sequence(conn))[1]:
                raise ValueError
            if read_database_identity(conn) is None:
                raise ValueError
        finally:
            conn.close()
    except Exception:
        print("container startup refused: invalid persistent state", file=sys.stderr)
        return 1
    os.execv(str(Path(sys.executable).parent / "fn"), [
        "fn", "serve", "--db", str(db), "--addr", settings.addr,
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
