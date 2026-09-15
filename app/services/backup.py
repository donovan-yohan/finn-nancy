"""Consistent on-disk snapshots of the SQLite ledger."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

_README = """finn-nancy backups
==================

Each file here is a point-in-time snapshot of the ledger database, taken with
SQLite's online backup API (safe to run while the app is serving traffic).

To restore:
  1. Stop the finn-nancy service.
  2. Copy the desired backup file over your live database (the path configured
     as DB_PATH / settings.db_path).
  3. Start the service again.

Blobs (receipt/statement originals) live under <data_dir>/originals and are
content-addressed by sha256. They are NOT included in these snapshots — back
that directory up separately if you need the source documents, not just the
ledger.
"""


def backup_now(db_path: str | Path, data_dir: str | Path) -> Path:
    """Snapshot ``db_path`` (consistent even mid-WAL) into <data_dir>/backups/."""
    backups_dir = Path(data_dir) / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"finn-nancy-{stamp}.sqlite"

    src = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    (backups_dir / "README.txt").write_text(_README)
    return target
