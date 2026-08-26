"""Agent identity: join a project (issues the API key), profile, registry,
and admin kill switches (revoke key, remove agent)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..auth import (
    Actor,
    AdminDep,
    AgentDep,
    check_project_access,
    generate_api_key,
    get_actor,
    get_admin_alias,
    hash_key,
)
from .. import audit
from ..avatars import is_valid_agent_avatar
from ..db import query_all, transaction, utc_now
from ..emoji import emoji_suggestions, is_valid_emoji
from ..errors import ApiError, ok
from ..events import append_event, notify
from ..logging_setup import kv, setup_logging
from ..services import (
    ALIAS_RE,
    add_member,
    get_agent,
    get_main_channel,
    get_project,
    post_system_message,
    require_not_archived,
    serialize_agent,
)

router = APIRouter(tags=["agents"])


class JoinRequest(BaseModel):
    alias: str = Field(min_length=2, max_length=32)
    role: str = Field(default="", max_length=200)
    avatar: str = Field(default="", max_length=32)


class ProfileUpdate(BaseModel):
    role: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, pattern="^(active|idle)$")
    avatar: str | None = Field(default=None, max_length=32)
    status_text: str | None = Field(default=None, max_length=100)
    status_emoji: str | None = Field(default=None, max_length=64)


# Per-agent working notes (issue #26). Owner-only writes through /api/me;
# everyone on the project (and the admin) may read through the notes route.
TODO_STATUSES = ("todo", "in-progress", "blocked", "done", "dropped")
TODO_PRIORITIES = ("low", "medium", "high")


class ScratchpadUpdate(BaseModel):
    body: str = Field(max_length=256 * 1024)
    base_revision: int | None = None


class TodoCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    status: str = Field(default="todo")
    priority: str = Field(default="medium")


class TodoBulk(BaseModel):
    items: list[TodoCreate] = Field(min_length=1, max_length=50)


# Reading order for another agent's list: live work first, parked last.
TODO_ORDER_SQL = (
    "ORDER BY CASE status WHEN 'in-progress' THEN 0 WHEN 'blocked' THEN 1 "
    "WHEN 'todo' THEN 2 WHEN 'done' THEN 3 ELSE 4 END, "
    "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
)


class TodoUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    status: str | None = None
    priority: str | None = None


def _validate_todo_fields(status: str | None, priority: str | None) -> None:
    if status is not None and status not in TODO_STATUSES:
        raise ApiError(
            422, "invalid_status",
            f"Todo status must be one of: {', '.join(TODO_STATUSES)}.",
        )
    if priority is not None and priority not in TODO_PRIORITIES:
        raise ApiError(
            422, "invalid_priority",
            f"Todo priority must be one of: {', '.join(TODO_PRIORITIES)}.",
        )


def _serialize_todo(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "text": row["text"],
        "status": row["status"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_my_todo(agent: Actor, todo_id: int) -> sqlite3.Row:
    row = query_all(
        "SELECT * FROM agent_todos WHERE id = ? AND agent_id = ?", (todo_id, agent.agent_id)
    )
    if not row:
        raise ApiError(404, "not_found", f"Todo {todo_id} does not exist on your list.")
    return row[0]


@router.post("/projects/{project_id}/agents/join", status_code=201)
async def join_project(project_id: int, body: JoinRequest) -> dict:
    """Join a project under a unique alias. Returns the per-agent API key —
    shown exactly once; only its hash is stored."""
    project = get_project(project_id)
    require_not_archived(project)
    alias = body.alias.strip()
    if not ALIAS_RE.match(alias):
        raise ApiError(
            422, "invalid_alias",
            "Alias must be 2-32 chars: letters, digits, '_', '-', '.', starting alphanumeric.",
        )
    if alias.lower() in ("here", "system", "admin"):
        raise ApiError(422, "reserved_alias", f"'{alias}' is a reserved alias.")
    admin_alias = get_admin_alias()
    if admin_alias is not None and alias.lower() == admin_alias.lower():
        raise ApiError(409, "reserved_alias", "That alias belongs to the admin and cannot be used.")
    if not is_valid_agent_avatar(body.avatar):
        raise ApiError(422, "invalid_avatar", "Unknown avatar — pick one from GET /api/avatars.")

    api_key = generate_api_key()
    now = utc_now()
    with transaction() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents (project_id, alias, role, avatar, api_key_hash, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, alias, body.role, body.avatar, hash_key(api_key), now, now),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "alias_taken", f"Alias '{alias}' is already taken in this project.")
        agent_id = int(cur.lastrowid or 0)
        main = get_main_channel(project_id)
        add_member(conn, main["id"], agent_id)
        append_event(
            conn, project_id, "agent_joined",
            {"agent_id": agent_id, "alias": alias, "role": body.role},
        )
        audit.platform_record(
            conn, project_id, "other",
            f"Agent '{alias}' joined ({body.role or 'no role'}); API key issued",
            target=alias,
        )
    suffix = f" ({body.role})" if body.role else ""
    post_system_message(project_id, f"**{alias}** joined the project{suffix}")
    await notify(project_id)
    setup_logging().info("agent joined %s", kv(project=project_id, alias=alias))
    agent = serialize_agent(get_agent(project_id, agent_id))
    return ok({"agent": agent, "api_key": api_key, "main_channel_id": main["id"]})


@router.get("/projects/{project_id}/agents")
async def list_agents(project_id: int, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    rows = query_all(
        "SELECT * FROM agents WHERE project_id = ? ORDER BY id", (project_id,)
    )
    return ok({"items": [serialize_agent(r) for r in rows]})


@router.get("/me")
async def my_profile(agent: Actor = AgentDep) -> dict:
    assert agent.project_id is not None
    row = get_agent(agent.project_id, agent.agent_id)
    return ok(serialize_agent(row))


@router.patch("/me")
async def update_my_profile(body: ProfileUpdate, agent: Actor = AgentDep) -> dict:
    assert agent.project_id is not None
    if body.avatar is not None and not is_valid_agent_avatar(body.avatar):
        raise ApiError(422, "invalid_avatar", "Unknown avatar — pick one from GET /api/avatars.")
    if body.status_emoji is not None and body.status_emoji != "" and not is_valid_emoji(body.status_emoji):
        hints = emoji_suggestions(body.status_emoji)
        hint_text = f" Did you mean: {', '.join(hints)}?" if hints else ""
        raise ApiError(
            422, "unknown_emoji",
            f"Status emoji must be a shortcode from GET /api/emoji (or '').{hint_text}",
            {"suggestions": hints},
        )
    with transaction() as conn:
        for column, value in (
            ("role", body.role),
            ("status", body.status),
            ("avatar", body.avatar),
            ("status_text", body.status_text),
            ("status_emoji", body.status_emoji),
        ):
            if value is not None:
                conn.execute(f"UPDATE agents SET {column} = ? WHERE id = ?", (value, agent.agent_id))
        profile = serialize_agent(get_agent(agent.project_id, agent.agent_id))
        append_event(conn, agent.project_id, "agent_updated", {"agent": profile})
    await notify(agent.project_id)
    return ok(profile)


@router.get("/me/scratchpad")
async def get_my_scratchpad(agent: Actor = AgentDep) -> dict:
    assert agent.project_id is not None
    row = get_agent(agent.project_id, agent.agent_id)
    return ok({
        "body": row["scratchpad"],
        "revision": row["scratchpad_revision"],
        "updated_at": row["scratchpad_updated_at"],
    })


@router.put("/me/scratchpad")
async def put_my_scratchpad(body: ScratchpadUpdate, agent: Actor = AgentDep) -> dict:
    """Replace the whole scratchpad. It is one freestyle markdown document,
    yours alone to write; read-modify-write to append. Pass base_revision to
    guard against clobbering yourself from a parallel or restarted session
    (optional: omitting it writes unconditionally)."""
    assert agent.project_id is not None
    now = utc_now()
    with transaction() as conn:
        row = conn.execute(
            "SELECT scratchpad, scratchpad_revision FROM agents WHERE id = ?",
            (agent.agent_id,),
        ).fetchone()
        current = int(row["scratchpad_revision"])
        if body.base_revision is not None and body.base_revision != current:
            raise ApiError(
                409, "revision_conflict",
                f"Scratchpad is at revision {current}, you edited from {body.base_revision}.",
                {"current_revision": current, "current_body": row["scratchpad"]},
            )
        conn.execute(
            "UPDATE agents SET scratchpad = ?, scratchpad_revision = ?, "
            "scratchpad_updated_at = ? WHERE id = ?",
            (body.body, current + 1, now, agent.agent_id),
        )
    return ok({"body": body.body, "revision": current + 1, "updated_at": now})


@router.get("/me/todos")
async def list_my_todos(agent: Actor = AgentDep) -> dict:
    rows = query_all(
        "SELECT * FROM agent_todos WHERE agent_id = ? ORDER BY id", (agent.agent_id,)
    )
    return ok({"items": [_serialize_todo(r) for r in rows]})


@router.post("/me/todos", status_code=201)
async def create_my_todo(body: TodoCreate, agent: Actor = AgentDep) -> dict:
    assert agent.project_id is not None
    _validate_todo_fields(body.status, body.priority)
    now = utc_now()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO agent_todos (project_id, agent_id, text, status, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent.project_id, agent.agent_id, body.text, body.status, body.priority, now, now),
        )
        todo_id = int(cur.lastrowid or 0)
    return ok(_serialize_todo(query_all("SELECT * FROM agent_todos WHERE id = ?", (todo_id,))[0]))


@router.post("/me/todos/bulk", status_code=201)
async def create_my_todos_bulk(body: TodoBulk, agent: Actor = AgentDep) -> dict:
    """Seed a whole plan in one call. Atomic: one invalid item creates
    nothing."""
    assert agent.project_id is not None
    for item in body.items:
        _validate_todo_fields(item.status, item.priority)
    now = utc_now()
    ids = []
    with transaction() as conn:
        for item in body.items:
            cur = conn.execute(
                "INSERT INTO agent_todos (project_id, agent_id, text, status, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent.project_id, agent.agent_id, item.text, item.status, item.priority, now, now),
            )
            ids.append(int(cur.lastrowid or 0))
    rows = query_all(
        f"SELECT * FROM agent_todos WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids
    )
    return ok({"items": [_serialize_todo(r) for r in rows]})


@router.patch("/me/todos/{todo_id}")
async def update_my_todo(todo_id: int, body: TodoUpdate, agent: Actor = AgentDep) -> dict:
    _get_my_todo(agent, todo_id)
    _validate_todo_fields(body.status, body.priority)
    with transaction() as conn:
        for column, value in (("text", body.text), ("status", body.status), ("priority", body.priority)):
            if value is not None:
                conn.execute(f"UPDATE agent_todos SET {column} = ? WHERE id = ?", (value, todo_id))
        conn.execute("UPDATE agent_todos SET updated_at = ? WHERE id = ?", (utc_now(), todo_id))
    return ok(_serialize_todo(query_all("SELECT * FROM agent_todos WHERE id = ?", (todo_id,))[0]))


@router.delete("/me/todos/{todo_id}")
async def delete_my_todo(todo_id: int, agent: Actor = AgentDep) -> dict:
    _get_my_todo(agent, todo_id)
    with transaction() as conn:
        conn.execute("DELETE FROM agent_todos WHERE id = ?", (todo_id,))
    return ok({"deleted": todo_id})


@router.get("/projects/{project_id}/agents/{agent_id}/notes")
async def get_agent_notes(project_id: int, agent_id: int, request: Request) -> dict:
    """Read-only view of another agent's scratchpad and todos. Any agent on
    the project (a lead checking a teammate) and the admin can read; only the
    owner can ever write, through /api/me."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    row = get_agent(project_id, agent_id)
    todos = query_all(
        f"SELECT * FROM agent_todos WHERE agent_id = ? {TODO_ORDER_SQL}", (agent_id,)
    )
    return ok({
        "alias": row["alias"],
        "scratchpad": {
            "body": row["scratchpad"],
            "revision": row["scratchpad_revision"],
            "updated_at": row["scratchpad_updated_at"],
        },
        "todos": [_serialize_todo(t) for t in todos],
    })


@router.post("/projects/{project_id}/agents/{agent_id}/revoke")
async def revoke_agent_key(project_id: int, agent_id: int, _admin: Actor = AdminDep) -> dict:
    """Kill switch: the agent's key stops working immediately. The agent row,
    alias, and history remain."""
    agent_row = get_agent(project_id, agent_id)
    with transaction() as conn:
        conn.execute(
            "UPDATE agents SET revoked = 1, api_key_hash = NULL, status = 'idle' WHERE id = ?",
            (agent_id,),
        )
        append_event(conn, project_id, "agent_revoked", {"agent_id": agent_id, "alias": agent_row["alias"]})
        audit.platform_record(
            conn, project_id, "other",
            f"API key revoked for '{agent_row['alias']}' (admin kill switch)",
            target=agent_row["alias"], actor="admin",
        )
    await notify(project_id)
    setup_logging().info("agent key revoked %s", kv(project=project_id, alias=agent_row["alias"]))
    return ok(serialize_agent(get_agent(project_id, agent_id)))


@router.delete("/projects/{project_id}/agents/{agent_id}")
async def remove_agent(project_id: int, agent_id: int, _admin: Actor = AdminDep) -> dict:
    """Remove an agent from the project entirely. Their messages remain under
    the denormalized alias; their memberships and mentions are cleaned up."""
    agent_row = get_agent(project_id, agent_id)
    alias = agent_row["alias"]
    with transaction() as conn:
        conn.execute("DELETE FROM conversation_members WHERE agent_id = ?", (agent_id,))
        conn.execute(
            "DELETE FROM mentions WHERE project_id = ? AND target_agent_id = ?",
            (project_id, agent_id),
        )
        # Personal state goes with them; reactions stay (denormalized alias).
        conn.execute(
            "DELETE FROM emoji_usage WHERE project_id = ? AND actor_agent_id = ?",
            (project_id, agent_id),
        )
        conn.execute(
            "DELETE FROM saved_items WHERE project_id = ? AND actor_agent_id = ?",
            (project_id, agent_id),
        )
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        append_event(conn, project_id, "agent_removed", {"agent_id": agent_id, "alias": alias})
        audit.platform_record(
            conn, project_id, "other",
            f"Agent '{alias}' removed from the project", target=alias, actor="admin",
        )
    post_system_message(project_id, f"**{alias}** was removed from the project")
    await notify(project_id)
    return ok({"removed": True, "alias": alias})
