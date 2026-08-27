"""Event log + in-process pub/sub.

Events are appended to the `events` table inside the caller's write
transaction (so an event exists iff its underlying change committed), then
`notify(project_id)` wakes SSE / long-poll waiters. Replay is a plain SELECT
with `id > since_id` — reconnects never lose events. Single uvicorn worker
required: the Condition objects live in this process.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3
from collections import deque
from typing import Any

from .auth import ADMIN_AGENT_ID, Actor
from .db import query_all, utc_now

_conditions: dict[int, asyncio.Condition] = {}

# Transient events (typing indicators): delivered over SSE only, never
# persisted, never part of since_id replay. Per-project ring buffer — a slow
# consumer missing old entries is harmless by definition.
_transient: dict[int, deque[tuple[int, dict[str, Any]]]] = {}
_transient_seq = itertools.count(1)


def _condition(project_id: int) -> asyncio.Condition:
    cond = _conditions.get(project_id)
    if cond is None:
        cond = _conditions[project_id] = asyncio.Condition()
    return cond


def append_event(
    conn: sqlite3.Connection,
    project_id: int,
    type: str,
    payload: dict[str, Any],
    conversation_id: int | None = None,
    target_agent_id: int | None = None,
) -> int:
    """Insert an event inside an open write transaction. Call notify() after
    the transaction commits."""
    cur = conn.execute(
        "INSERT INTO events (project_id, type, conversation_id, target_agent_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, type, conversation_id, target_agent_id, json.dumps(payload), utc_now()),
    )
    return int(cur.lastrowid or 0)


async def notify(project_id: int) -> None:
    cond = _condition(project_id)
    async with cond:
        cond.notify_all()


async def publish_transient(
    project_id: int,
    type: str,
    payload: dict[str, Any],
    conversation_id: int | None = None,
) -> None:
    """Emit an ephemeral event (e.g. typing). Reaches live SSE subscribers
    only; restarts and polling never see it."""
    ring = _transient.setdefault(project_id, deque(maxlen=64))
    ring.append(
        (
            next(_transient_seq),
            {
                "type": type,
                "conversation_id": conversation_id,
                "payload": payload,
                "created_at": utc_now(),
            },
        )
    )
    await notify(project_id)


def transient_since(
    actor: Actor, project_id: int, seq: int
) -> tuple[list[dict[str, Any]], int]:
    """Transient events after `seq` visible to the actor (same membership rule
    as durable conversation-scoped events). Returns (events, newest_seq)."""
    ring = _transient.get(project_id)
    if not ring:
        return [], seq
    member_convs: set[int] | None = None
    out: list[dict[str, Any]] = []
    newest = seq
    for item_seq, event in ring:
        if item_seq <= seq:
            continue
        newest = max(newest, item_seq)
        conv = event["conversation_id"]
        if not actor.is_admin and conv is not None:
            if member_convs is None:
                member_convs = {
                    r["conversation_id"]
                    for r in query_all(
                        "SELECT conversation_id FROM conversation_members WHERE agent_id = ?",
                        (actor.agent_id,),
                    )
                }
            if conv not in member_convs:
                continue
        out.append(event)
    return out, newest


def latest_transient_seq(project_id: int) -> int:
    ring = _transient.get(project_id)
    return ring[-1][0] if ring else 0


async def wait_for_events(project_id: int, timeout: float) -> None:
    """Block until notify() fires for this project or the timeout elapses."""
    cond = _condition(project_id)
    async with cond:
        try:
            await asyncio.wait_for(cond.wait(), timeout=timeout)
        except TimeoutError:
            pass


def visible_events_since(
    actor: Actor,
    project_id: int,
    since_id: int,
    limit: int = 500,
    types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Events after since_id that the actor may see: project-wide events,
    events in conversations they are a member of, and events targeted at them.
    The admin sees everything. `types` optionally restricts to the named event
    types (issue #15 S3 — spares consumers hand-rolled filtering)."""
    type_clause = ""
    type_params: dict[str, Any] = {}
    if types:
        placeholders = ", ".join(f":t{i}" for i in range(len(types)))
        type_clause = f" AND type IN ({placeholders})"
        type_params = {f"t{i}": t for i, t in enumerate(types)}
    if actor.is_admin:
        rows = query_all(
            f"SELECT * FROM events WHERE project_id = :pid AND id > :since{type_clause} "
            "ORDER BY id LIMIT :limit",
            {"pid": project_id, "since": since_id, "limit": limit, **type_params},
        )
    else:
        rows = query_all(
            f"""
            SELECT * FROM events
            WHERE project_id = :pid AND id > :since{type_clause}
              AND (
                    (conversation_id IS NULL AND target_agent_id IS NULL)
                 OR target_agent_id = :aid
                 OR conversation_id IN (
                        SELECT conversation_id FROM conversation_members
                        WHERE agent_id = :aid
                    )
              )
            ORDER BY id LIMIT :limit
            """,
            {"pid": project_id, "since": since_id, "aid": actor.agent_id,
             "limit": limit, **type_params},
        )
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "type": r["type"],
                "conversation_id": r["conversation_id"],
                "target_agent_id": r["target_agent_id"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
        )
    return out


__all__ = [
    "ADMIN_AGENT_ID",
    "append_event",
    "notify",
    "wait_for_events",
    "visible_events_since",
]
