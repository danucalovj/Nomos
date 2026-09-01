"""Authentication and actor resolution.

Two kinds of caller:
  * Agents  — `Authorization: Bearer <key>`; key hash maps to exactly one
              (project, alias). Agents only ever act inside their project.
  * Admin   — the web UI sends no Authorization header. Admin-only routes
              REJECT requests that carry an agent key, so an agent key can
              never invoke admin actions. (Network-level access control is the
              admin's responsibility by design.)

`Actor` unifies both for endpoints usable by either party. The admin is
represented by agent_id 0 throughout the schema (members, mentions, events).
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, Request

from .db import get_conn, query_one, utc_now
from .errors import ApiError, attention_var

ADMIN_AGENT_ID = 0


@dataclass(frozen=True)
class Actor:
    kind: str  # 'agent' | 'admin'
    agent_id: int  # 0 for admin
    project_id: int | None  # None for admin (admin spans all projects)
    alias: str

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def role_flag(self) -> str:
        return "admin" if self.is_admin else "agent"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    return f"ac_{secrets.token_hex(24)}"


def get_admin_alias() -> str | None:
    row = query_one("SELECT alias FROM admin_identity WHERE id = 1")
    return row["alias"] if row else None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "bad_authorization", "Malformed Authorization header; expected 'Bearer <key>'.")
    return token.strip()


# last_seen presence writes are throttled: one real write per agent per
# interval instead of a write transaction on every authenticated request
# (issue #28 H2). The dict is touched from threadpool workers; a lost update
# just means one extra presence write, which is harmless.
_LAST_SEEN_INTERVAL_S = 30.0
_last_seen_flushed: dict[int, float] = {}


def _admin_attention(project_id: int, agent_id: int) -> dict | None:
    """Unseen @mentions authored by the ADMIN for this agent (issue #32).
    Rides the response envelope so a non-polling agent still sees the human.
    project_id is included so idx_mentions_target serves the lookup, and
    soft-deleted messages are excluded to match what the mention feed shows."""
    row = query_one(
        """
        SELECT COUNT(*) AS c, MIN(m.created_at) AS oldest
        FROM mentions m
        LEFT JOIN messages msg ON m.message_id = msg.id
        LEFT JOIN ticket_comments tc ON m.comment_id = tc.id
        WHERE m.project_id = ? AND m.target_agent_id = ? AND m.seen = 0
          AND (m.message_id IS NULL OR msg.deleted = 0)
          AND (msg.author_type = 'admin' OR tc.author_type = 'admin')
        """,
        (project_id, agent_id),
    )
    if row is not None and row["c"]:
        return {"admin_mentions_unseen": row["c"], "oldest_at": row["oldest"]}
    return None


def _agent_from_token(token: str) -> tuple[Actor, dict | None]:
    """Blocking: always call via asyncio.to_thread from async code. Returns
    the actor plus its attention payload — computed here in the worker and
    handed back because a ContextVar set inside to_thread's copied context
    would never reach the request context."""
    row = query_one(
        "SELECT id, project_id, alias, revoked FROM agents WHERE api_key_hash = ?",
        (hash_key(token),),
    )
    if row is None or row["revoked"]:
        raise ApiError(401, "invalid_key", "Unknown or revoked API key.")
    now_m = time.monotonic()
    if now_m - _last_seen_flushed.get(row["id"], 0.0) >= _LAST_SEEN_INTERVAL_S:
        _last_seen_flushed[row["id"]] = now_m
        get_conn().execute(
            "UPDATE agents SET last_seen = ? WHERE id = ?", (utc_now(), row["id"])
        )
        get_conn().commit()
    actor = Actor(kind="agent", agent_id=row["id"], project_id=row["project_id"], alias=row["alias"])
    return actor, _admin_attention(row["project_id"], row["id"])


async def get_actor(request: Request) -> Actor:
    """Resolve the caller: agent if a Bearer key is presented, otherwise admin.
    Admin resolution requires first-time setup to have been completed."""
    token = _bearer_token(request)
    if token is not None:
        actor, attention = await asyncio.to_thread(_agent_from_token, token)
        attention_var.set(attention)
        return actor
    attention_var.set(None)
    alias = get_admin_alias()
    if alias is None:
        raise ApiError(409, "setup_required", "First-time admin setup has not been completed.")
    return Actor(kind="admin", agent_id=ADMIN_AGENT_ID, project_id=None, alias=alias)


async def require_admin(request: Request) -> Actor:
    """Admin-only routes: any agent Bearer key is rejected outright."""
    attention_var.set(None)
    if _bearer_token(request) is not None:
        raise ApiError(403, "admin_only", "This action is admin-only; agent keys are not accepted.")
    alias = get_admin_alias()
    if alias is None:
        raise ApiError(409, "setup_required", "First-time admin setup has not been completed.")
    return Actor(kind="admin", agent_id=ADMIN_AGENT_ID, project_id=None, alias=alias)


async def require_agent(request: Request) -> Actor:
    """Agent-only routes (e.g. claiming tickets is meaningless for the admin
    only when explicitly restricted; most routes accept both via get_actor)."""
    token = _bearer_token(request)
    if token is None:
        raise ApiError(401, "agent_key_required", "This endpoint requires an agent API key.")
    actor, attention = await asyncio.to_thread(_agent_from_token, token)
    attention_var.set(attention)
    return actor


def check_project_access(actor: Actor, project_id: int) -> None:
    """Agents may only touch their own project; the admin may touch any."""
    if actor.is_admin:
        if query_one("SELECT id FROM projects WHERE id = ?", (project_id,)) is None:
            raise ApiError(404, "not_found", f"Project {project_id} does not exist.")
        return
    if actor.project_id != project_id:
        raise ApiError(403, "forbidden", "Your API key is not valid for this project.")


ActorDep = Depends(get_actor)
AdminDep = Depends(require_admin)
AgentDep = Depends(require_agent)
