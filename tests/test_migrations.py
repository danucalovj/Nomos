"""The migration runner itself: idempotency, ordering, and resulting schema
(previously untested — issue #29)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(data_dir: str) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "server.migrate"],
        cwd=REPO,
        env=os.environ | {"NOMOS_DATA_DIR": data_dir},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout + out.stderr


def test_migrations_apply_in_order_and_are_idempotent():
    data_dir = tempfile.mkdtemp(prefix="nomos-mig-test-")
    first = _run(data_dir)
    files = sorted(p.name for p in (REPO / "migrations").glob("*.sql"))
    for name in files:
        assert name in first, f"{name} not applied on fresh DB"

    second = _run(data_dir)
    for name in files:
        assert name not in second, f"{name} re-applied on second run"

    conn = sqlite3.connect(Path(data_dir) / "nomos.db")
    try:
        applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
        assert applied == files
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for required in (
            "projects", "agents", "conversations", "messages", "tickets",
            "documents", "events", "audit_log", "agent_todos",
        ):
            assert required in tables, f"table {required} missing after migrations"
    finally:
        conn.close()
