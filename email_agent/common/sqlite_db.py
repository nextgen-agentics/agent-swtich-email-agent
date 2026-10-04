"""SQLite helpers for the run store, the local mailbox copy and long-term memory (Revision 12).

Standard library `sqlite3` only. Every file is opened the same way: WAL (readers never block the one writer), foreign
keys on, a busy timeout, and schema migrations numbered with `PRAGMA user_version`. One process writes each file; slow
bulk work is moved off the event loop by the caller with `asyncio.to_thread`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: Path, migrations: list[str]) -> sqlite3.Connection:
    """Open (or create) `path` and bring its schema up to date. `migrations[i]` is the SQL that moves the file from
    version i to i + 1; applied ones are skipped, so a file opened by newer code is upgraded in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)   # autocommit; we open transactions
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for n, sql in enumerate(migrations[version:], start=version + 1):
        con.execute("BEGIN")
        try:
            for statement in _statements(sql):
                con.execute(statement)
            con.execute(f"PRAGMA user_version={n}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return con


# Vectors for meaning-based search (Stage 9, search.py), added as a migration by each store that has them.
VECTORS_SQL = """
    CREATE TABLE vectors (rid INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL UNIQUE, grp TEXT,
        scope TEXT, content_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, vector BLOB NOT NULL);
    CREATE INDEX vectors_grp ON vectors(grp)
"""


def _statements(sql: str) -> list[str]:
    """Split a migration into statements (no triggers with inner semicolons are used in our schemas)."""
    return [s.strip() for s in sql.split(";") if s.strip()]


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[None]:
    """One atomic write (the connection is in autocommit mode, so `with con:` alone would not group statements)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
