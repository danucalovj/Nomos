"""Audit trail core (issue #17).

Append-only, per-project sha256 hash chain. `record()` MUST run inside an
open BEGIN IMMEDIATE transaction: the prev-hash read and the insert share the
write lock, so the chain is race-free by construction. Every record also
emits an `audit` event through the existing events pipeline so the UI stays
live over SSE with no extra plumbing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from datetime import datetime, timedelta, timezone

from .db import query_all, query_one, utc_now
from .events import append_event

ACTIONS = (
    "file_edit", "file_create", "file_delete",
    "command", "test_run", "decision", "research", "other",
)
GENESIS = "0" * 64
MAX_SUMMARY = 500
MAX_DIFF = 64 * 1024
CORRELATION_WINDOW_S = 120

# Canonical, pinned field order for entry hashing. The canonical
# form is a compact JSON array — type-preserving and delimiter-unambiguous
# (None != "", embedded separators cannot shift field boundaries). Changing
# this breaks verification of existing chains — never alter it.
def _entry_hash(prev_hash: str, fields: list[Any]) -> str:
    canonical = json.dumps([prev_hash, *fields], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def record(
    conn: sqlite3.Connection,
    project_id: int,
    actor: str,
    actor_type: str,
    source: str,
    action: str,
    summary: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    diff: str | None = None,
    correlated_id: int | None = None,
) -> int:
    """Append one audit record inside the caller's write transaction and emit
    its `audit` event. Returns the record id. Caller must notify() after
    commit."""
    detail_map = dict(detail or {})
    summary = summary[:MAX_SUMMARY]
    if diff is not None and len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF]
        detail_map["diff_truncated"] = True
    detail_json = json.dumps(detail_map, sort_keys=True)
    created_at = utc_now()

    prev = conn.execute(
        "SELECT entry_hash FROM audit_log WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    prev_hash = prev["entry_hash"] if prev else GENESIS
    entry_hash = _entry_hash(
        prev_hash,
        [project_id, actor, actor_type, source, action, target, summary,
         detail_json, diff, correlated_id, created_at],
    )
    cur = conn.execute(
        "INSERT INTO audit_log (project_id, actor, actor_type, source, action, target, "
        "summary, detail, diff, correlated_id, prev_hash, entry_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, actor, actor_type, source, action, target, summary,
         detail_json, diff, correlated_id, prev_hash, entry_hash, created_at),
    )
    audit_id = int(cur.lastrowid or 0)
    append_event(
        conn, project_id, "audit",
        serialize_row_dict({
            "id": audit_id, "project_id": project_id, "actor": actor,
            "actor_type": actor_type, "source": source, "action": action,
            "target": target, "summary": summary, "detail": detail_json,
            "diff": diff, "correlated_id": correlated_id,
            "prev_hash": prev_hash, "entry_hash": entry_hash,
            "created_at": created_at,
        }),
    )
    return audit_id


def platform_record(
    conn: sqlite3.Connection,
    project_id: int,
    action: str,
    summary: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    actor: str = "platform",
) -> int:
    """Governance-act mirror rows. action uses the same vocabulary
    ('other' for acts without a file/command shape)."""
    return record(
        conn, project_id, actor, "platform", "platform", action, summary,
        target=target, detail=detail,
    )


def serialize_row(row: sqlite3.Row) -> dict[str, Any]:
    return serialize_row_dict(dict(row))


def serialize_row_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d["id"],
        "actor": d["actor"],
        "actor_type": d["actor_type"],
        "source": d["source"],
        "action": d["action"],
        "target": d["target"],
        "summary": d["summary"],
        "detail": d["detail"],
        "diff": d["diff"],
        "correlated_id": d["correlated_id"],
        "prev_hash": d["prev_hash"],
        "entry_hash": d["entry_hash"],
        "created_at": d["created_at"],
    }


# Correlation and append-only hashing: rows are immutable, so
# the monitor<->self_report link always lives on whichever row was inserted
# SECOND (set at insert time, hashed with the row). Readers resolve both
# directions: a monitor row counts as claimed if it carries correlated_id OR a
# later self_report points back at it.

def _window_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=CORRELATION_WINDOW_S)).isoformat()


def find_recent_observation(
    conn: sqlite3.Connection, project_id: int, action: str, target: str
) -> int | None:
    """A file_* self-report is being inserted: find the most recent monitor
    observation of the same target within the window, to store on the new
    self-report row."""
    if not target or not action.startswith("file_"):
        return None
    row = conn.execute(
        """
        SELECT id FROM audit_log
        WHERE project_id = ? AND source = 'monitor' AND target = ?
          AND created_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (project_id, target, _window_cutoff()),
    ).fetchone()
    return int(row["id"]) if row else None


def find_recent_claim(
    conn: sqlite3.Connection, project_id: int, target: str
) -> int | None:
    """A monitor observation just landed: find a recent file_* self-report
    claiming the same target (forward correlation pass)."""
    row = conn.execute(
        """
        SELECT id FROM audit_log
        WHERE project_id = ? AND source = 'self_report' AND target = ?
          AND action IN ('file_edit', 'file_create', 'file_delete')
          AND created_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (project_id, target, _window_cutoff()),
    ).fetchone()
    return int(row["id"]) if row else None


def verify_chain(project_id: int) -> dict[str, Any]:
    """Recompute the whole chain; report ok / first divergence.
    Cheap single scan, exposed to every reader."""
    rows = query_all(
        "SELECT * FROM audit_log WHERE project_id = ? ORDER BY id", (project_id,)
    )
    prev = GENESIS
    for r in rows:
        expected = _entry_hash(
            prev,
            [r["project_id"], r["actor"], r["actor_type"], r["source"], r["action"],
             r["target"], r["summary"], r["detail"], r["diff"], r["correlated_id"],
             r["created_at"]],
        )
        if r["prev_hash"] != prev or r["entry_hash"] != expected:
            return {"ok": False, "checked": len(rows), "first_divergence": r["id"]}
        prev = r["entry_hash"]
    return {"ok": True, "checked": len(rows), "first_divergence": None}


def get_watch(project_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM audit_watches WHERE project_id = ?", (project_id,))
