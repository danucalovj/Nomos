"""SQLite access layer: thread-local connections, WAL mode, write transactions.

Every thread gets its own connection (SQLite WAL supports concurrent readers
with a single writer). Writes go through `transaction()`, which issues
BEGIN IMMEDIATE so the write lock is taken up front — combined with
busy_timeout this serializes concurrent writers without "database is locked"
errors and makes multi-statement operations (ticket claim, cascade delete)
atomic.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .config import get_settings

_local = threading.local()


def utc_now() -> str:
    """ISO 8601 UTC timestamp with microseconds (monotonic enough for ordering
    together with autoincrement ids, which are the true ordering key)."""
    return datetime.now(UTC).isoformat()


def get_conn() -> sqlite3.Connection:
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def close_conn() -> None:
    """Close this thread's connection (used on shutdown and in tests)."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def checkpoint() -> None:
    """WAL checkpoint — called on graceful shutdown so the .db file is complete."""
    get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Atomic write transaction. BEGIN IMMEDIATE acquires the write lock at
    entry; commit/rollback on exit. Nesting is a programming error."""
    conn = get_conn()
    if conn.in_transaction:
        raise RuntimeError("nested transaction() call")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        # commit() lives INSIDE the try: if it fails, the rollback below runs
        # and the connection leaves the transaction. Otherwise one failed
        # commit would poison this thread's connection forever (issue #28).
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            close_conn()  # unrecoverable connection state: recycle it
        raise


def query_all(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
