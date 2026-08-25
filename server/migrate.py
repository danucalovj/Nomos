"""Idempotent ordered-SQL migration runner. Run as `python -m server.migrate`.

Each migration file is applied atomically: the script plus its
schema_migrations bookkeeping row run inside one explicit BEGIN/COMMIT (we
cannot rely on `with conn:` because executescript() commits any pending
transaction before running). Migration files must not contain their own
BEGIN/COMMIT statements.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def run_migrations(db_path: Path | None = None) -> list[str]:
    """Apply any migrations not yet recorded in schema_migrations. Returns the
    filenames applied."""
    settings = get_settings()
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.commit()
        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations")
        }
        done: list[str] = []
        for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if file.name in applied:
                continue
            if not _SAFE_FILENAME.match(file.name):
                raise ValueError(f"Unsafe migration filename: {file.name}")
            applied_at = datetime.now(timezone.utc).isoformat()
            script = (
                "BEGIN;\n"
                + file.read_text()
                + "\nINSERT INTO schema_migrations (filename, applied_at) "
                + f"VALUES ('{file.name}', '{applied_at}');\nCOMMIT;"
            )
            try:
                conn.executescript(script)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            done.append(file.name)
        return done
    finally:
        conn.close()


if __name__ == "__main__":
    applied_now = run_migrations()
    if applied_now:
        print(f"Applied migrations: {', '.join(applied_now)}")
    else:
        print("Database schema up to date.")
