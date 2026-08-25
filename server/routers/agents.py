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
