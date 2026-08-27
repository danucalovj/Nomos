"""Channels and DMs: listing, creation, membership, and DM open/get.

DMs are conversations of type 'dm' with exactly two member rows (agent ids,
0 = admin). The admin can list and read every DM in a project (full
observability)."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ..auth import ADMIN_AGENT_ID, Actor, ActorDep, AgentDep, check_project_access
from ..db import query_all, query_one, transaction, utc_now
from ..errors import ApiError, ok
from ..events import append_event, notify
from ..services import (
    add_member,
    check_conversation_access,
    get_conversation,
    get_project,
    is_member,
    require_not_archived,
    resolve_alias,
    serialize_conversation,
)

router = APIRouter(tags=["channels"])

CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,49}$")


class ChannelCreate(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    topic: str = Field(default="", max_length=500)
    invite: list[str] = Field(default_factory=list, max_length=50)


class InviteRequest(BaseModel):
    aliases: list[str] = Field(min_length=1, max_length=50)


class DmOpen(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    with_alias: str = Field(alias="with", min_length=1, max_length=32)


def _unread_count(conversation_id: int, last_read: int) -> int:
    row = query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ? AND id > ? AND deleted = 0",
        (conversation_id, last_read),
    )
    return row["c"] if row else 0


def _resolve_or_422(project_id: int, alias: str) -> tuple[int, str]:
    resolved = resolve_alias(project_id, alias)
    if resolved is None:
        raise ApiError(422, "unknown_alias", f"No such alias in this project: '{alias}'.")
    return resolved


@router.get("/projects/{project_id}/channels")
async def list_channels(project_id: int, actor: Actor = ActorDep) -> dict:
    """All channels in the project. Agents get a `member` flag and, for
    channels they belong to, an `unread` count (all non-deleted messages after
    their read cursor, thread replies included)."""
    check_project_access(actor, project_id)
    rows = query_all(
        "SELECT * FROM conversations WHERE project_id = ? AND type = 'channel' ORDER BY id",
        (project_id,),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        data = serialize_conversation(row)
        last_msg = query_one(
            "SELECT MAX(id) AS m FROM messages WHERE conversation_id = ? AND deleted = 0",
            (row["id"],),
        )
        data["last_message_id"] = last_msg["m"] if last_msg else None
        if actor.is_admin:
            data["member"] = True
            data["unread"] = 0
        else:
            cursor = query_one(
                "SELECT last_read_message_id FROM conversation_members "
                "WHERE conversation_id = ? AND agent_id = ?",
                (row["id"], actor.agent_id),
            )
            data["member"] = cursor is not None
            data["unread"] = (
                _unread_count(row["id"], cursor["last_read_message_id"]) if cursor else 0
            )
        items.append(data)
    return ok({"items": items})


@router.post("/projects/{project_id}/channels", status_code=201)
async def create_channel(project_id: int, body: ChannelCreate, actor: Actor = ActorDep) -> dict:
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(actor, project_id)
    name = body.name.strip()
    if not CHANNEL_NAME_RE.match(name):
        raise ApiError(
            422, "invalid_name",
            "Channel name must be 2-50 chars: letters, digits, '_', '-', starting alphanumeric.",
        )
    invitees = [_resolve_or_422(project_id, a) for a in body.invite]
    now = utc_now()
    with transaction() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO conversations (project_id, type, name, topic, created_by, created_at) "
                "VALUES (?, 'channel', ?, ?, ?, ?)",
                (project_id, name, body.topic, actor.alias, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "duplicate_name", f"A channel named '{name}' already exists.") from exc
        conversation_id = int(cur.lastrowid or 0)
        if not actor.is_admin:
            add_member(conn, conversation_id, actor.agent_id)
        for agent_id, _alias in invitees:
            if agent_id != ADMIN_AGENT_ID:
                add_member(conn, conversation_id, agent_id)
        conv_row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        append_event(
            conn, project_id, "channel_created",
            {"conversation_id": conversation_id, "name": name, "by": actor.alias},
        )
    await notify(project_id)
    return ok(serialize_conversation(conv_row))


@router.get("/projects/{project_id}/channels/{conversation_id}")
async def get_channel(project_id: int, conversation_id: int, actor: Actor = ActorDep) -> dict:
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    if conv["type"] != "channel":
        raise ApiError(404, "not_found", "Not a channel.")
    check_conversation_access(actor, conv)
    return ok(serialize_conversation(conv))


@router.post("/projects/{project_id}/channels/{conversation_id}/join")
async def join_channel(project_id: int, conversation_id: int, agent: Actor = AgentDep) -> dict:
    """Channels are open: any project agent may join. DMs cannot be joined."""
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(agent, project_id)
    conv = get_conversation(project_id, conversation_id)
    if conv["type"] != "channel":
        raise ApiError(403, "not_a_channel", "DMs cannot be joined.")
    with transaction() as conn:
        add_member(conn, conversation_id, agent.agent_id)
        append_event(
            conn, project_id, "channel_member_joined",
            {"conversation_id": conversation_id, "alias": agent.alias},
            conversation_id=conversation_id,
        )
    await notify(project_id)
    return ok(serialize_conversation(get_conversation(project_id, conversation_id)))


@router.post("/projects/{project_id}/channels/{conversation_id}/leave")
async def leave_channel(project_id: int, conversation_id: int, agent: Actor = AgentDep) -> dict:
    check_project_access(agent, project_id)
    conv = get_conversation(project_id, conversation_id)
    if conv["type"] != "channel":
        raise ApiError(403, "not_a_channel", "DMs cannot be left.")
    if conv["is_main"]:
        raise ApiError(403, "main_channel", "The main channel cannot be left.")
    if not is_member(conversation_id, agent.agent_id):
        raise ApiError(409, "not_a_member", "You are not a member of this channel.")
    with transaction() as conn:
        conn.execute(
            "DELETE FROM conversation_members WHERE conversation_id = ? AND agent_id = ?",
            (conversation_id, agent.agent_id),
        )
        append_event(
            conn, project_id, "channel_member_left",
            {"conversation_id": conversation_id, "alias": agent.alias},
            conversation_id=conversation_id,
        )
    await notify(project_id)
    return ok({"left": True, "conversation_id": conversation_id})


@router.post("/projects/{project_id}/channels/{conversation_id}/invite")
async def invite_to_channel(
    project_id: int, conversation_id: int, body: InviteRequest, actor: Actor = ActorDep
) -> dict:
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    if conv["type"] != "channel":
        raise ApiError(403, "not_a_channel", "Use the DM endpoints for direct conversations.")
    check_conversation_access(actor, conv)
    resolved = [_resolve_or_422(project_id, a) for a in body.aliases]
    with transaction() as conn:
        for agent_id, alias in resolved:
            if agent_id == ADMIN_AGENT_ID:
                continue  # the admin has implicit access to every channel
            add_member(conn, conversation_id, agent_id)
            append_event(
                conn, project_id, "channel_member_joined",
                {"conversation_id": conversation_id, "alias": alias, "invited_by": actor.alias},
                conversation_id=conversation_id,
            )
    await notify(project_id)
    return ok(serialize_conversation(get_conversation(project_id, conversation_id)))


def _serialize_dm(conv: sqlite3.Row, viewer: Actor) -> dict[str, Any]:
    """DM payload: `participants` always; `with` is the other party's alias
    when the viewer is one of the participants (None for an admin observing an
    agent-to-agent DM)."""
    data = serialize_conversation(conv)
    last_msg = query_one(
        "SELECT MAX(id) AS m FROM messages WHERE conversation_id = ? AND deleted = 0",
        (conv["id"],),
    )
    data["last_message_id"] = last_msg["m"] if last_msg else None
    data["participants"] = [m["alias"] for m in data["members"]]
    viewer_is_participant = any(m["agent_id"] == viewer.agent_id for m in data["members"])
    data["with"] = (
        next((m["alias"] for m in data["members"] if m["agent_id"] != viewer.agent_id), None)
        if viewer_is_participant
        else None
    )
    del data["name"]
    return data


@router.get("/projects/{project_id}/dms")
async def list_dms(project_id: int, actor: Actor = ActorDep) -> dict:
    """An agent sees their own DMs; the admin sees every DM in the project."""
    check_project_access(actor, project_id)
    if actor.is_admin:
        rows = query_all(
            "SELECT * FROM conversations WHERE project_id = ? AND type = 'dm' ORDER BY id",
            (project_id,),
        )
    else:
        rows = query_all(
            "SELECT c.* FROM conversations c JOIN conversation_members m ON m.conversation_id = c.id "
            "WHERE c.project_id = ? AND c.type = 'dm' AND m.agent_id = ? ORDER BY c.id",
            (project_id, actor.agent_id),
        )
    return ok({"items": [_serialize_dm(r, actor) for r in rows]})


@router.post("/projects/{project_id}/dms", status_code=201)
async def open_dm(project_id: int, body: DmOpen, actor: Actor = ActorDep) -> dict:
    """Open (or return the existing) DM between the caller and another
    participant. Pairs are order-independent and unique."""
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(actor, project_id)
    target_id, target_alias = _resolve_or_422(project_id, body.with_alias)
    if target_id == actor.agent_id:
        raise ApiError(422, "self_dm", "You cannot open a DM with yourself.")

    now = utc_now()
    with transaction() as conn:
        # Existence check runs inside the write transaction: BEGIN IMMEDIATE
        # serializes writers, so concurrent open_dm calls cannot both create
        # the pair.
        existing = conn.execute(
            """
            SELECT c.* FROM conversations c
            WHERE c.project_id = ? AND c.type = 'dm'
              AND EXISTS (SELECT 1 FROM conversation_members m
                          WHERE m.conversation_id = c.id AND m.agent_id = ?)
              AND EXISTS (SELECT 1 FROM conversation_members m
                          WHERE m.conversation_id = c.id AND m.agent_id = ?)
            """,
            (project_id, actor.agent_id, target_id),
        ).fetchone()
        if existing is not None:
            conversation_id = existing["id"]
            created = False
        else:
            created = True
            cur = conn.execute(
                "INSERT INTO conversations (project_id, type, topic, created_by, created_at) "
                "VALUES (?, 'dm', '', ?, ?)",
                (project_id, actor.alias, now),
            )
            conversation_id = int(cur.lastrowid or 0)
            add_member(conn, conversation_id, actor.agent_id)
            add_member(conn, conversation_id, target_id)
            append_event(
                conn, project_id, "dm_opened",
                {"conversation_id": conversation_id, "between": [actor.alias, target_alias]},
                conversation_id=conversation_id,
            )
    if created:
        await notify(project_id)
    return ok(_serialize_dm(get_conversation(project_id, conversation_id), actor))
