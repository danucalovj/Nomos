"""Real-time delivery: SSE stream with since_id replay, polled/long-polled
event fetch, read cursors with unread counts, and mention feeds.

Unread counts include every non-deleted message after the cursor (own
messages and thread replies included) — predictable and cheap to reason
about for turn-based agents."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from ..auth import Actor, ActorDep, AgentDep, check_project_access
from ..db import query_all, query_one, transaction
from ..errors import ok
from ..events import (
    latest_transient_seq,
    publish_transient,
    transient_since,
    visible_events_since,
    wait_for_events,
)
from ..services import (
    check_conversation_access,
    get_conversation,
    get_project,
    pagination_window,
    serialize_message,
)

router = APIRouter(tags=["stream"])

LONG_POLL_MAX_SECONDS = 60.0
SSE_WAIT_SECONDS = 20.0


class ReadCursorSet(BaseModel):
    last_read_message_id: int = Field(ge=0)


class MentionsSeen(BaseModel):
    mention_ids: list[int] = Field(default_factory=list)
    all: bool = False

    @model_validator(mode="after")
    def _some_target(self) -> MentionsSeen:
        if not self.all and not self.mention_ids:
            raise ValueError("Provide mention_ids or all=true.")
        return self


def _max_event_id(project_id: int) -> int:
    row = query_one("SELECT MAX(id) AS m FROM events WHERE project_id = ?", (project_id,))
    return row["m"] or 0


@router.get("/projects/{project_id}/stream")
async def sse_stream(
    project_id: int,
    request: Request,
    since_id: int | None = None,
    types: str | None = None,
    actor: Actor = ActorDep,
) -> EventSourceResponse:
    """SSE stream of project events visible to the caller. Replays everything
    after `since_id` (default: only new events from connect time), then pushes
    live. Reconnect with the last received event id — nothing is lost."""
    check_project_access(actor, project_id)
    get_project(project_id)
    start_id = since_id if since_id is not None else _max_event_id(project_id)
    type_filter = [t.strip() for t in types.split(",") if t.strip()] if types else None

    # Transient (typing) events start from "now" — they are never replayed.
    start_transient_seq = latest_transient_seq(project_id)

    async def generator():
        last_id = start_id
        t_seq = start_transient_seq
        while True:
            if await request.is_disconnected():
                return
            # The Actor was resolved once at connect; re-check revocation on
            # every wake-up so the kill switch actually severs live streams
            # (issue #28 H7). Removal deletes the row, which also ends here.
            if actor.kind == "agent":
                row = query_one(
                    "SELECT revoked FROM agents WHERE id = ?", (actor.agent_id,)
                )
                if row is None or row["revoked"]:
                    return
            events = visible_events_since(actor, project_id, last_id, types=type_filter)
            for event in events:
                last_id = event["id"]
                yield {
                    "event": event["type"],
                    "id": str(event["id"]),
                    # data carries the event payload itself; the SSE `event`
                    # and `id` fields carry type and replay cursor (contract).
                    "data": json.dumps(event["payload"]),
                }
            transients, t_seq = transient_since(actor, project_id, t_seq)
            if type_filter is not None:
                # The types filter applies to ephemeral events too — a
                # consumer asking for ticket events must not receive typing.
                transients = [e for e in transients if e["type"] in type_filter]
            for event in transients:
                # No `id:` field: replay cursors must never advance past
                # durable events because of an ephemeral one.
                yield {
                    "event": event["type"],
                    "data": json.dumps(
                        event["payload"] | {"conversation_id": event["conversation_id"]}
                    ),
                }
            if not events and not transients:
                await wait_for_events(project_id, SSE_WAIT_SECONDS)

    return EventSourceResponse(generator(), ping=15)


@router.post("/projects/{project_id}/conversations/{conversation_id}/typing")
async def typing(project_id: int, conversation_id: int, actor: Actor = ActorDep) -> dict:
    """Ephemeral typing signal: reaches live SSE subscribers only (never
    persisted, never in /events). Send every ~4 seconds while composing;
    clients clear the indicator ~6 seconds after the last signal."""
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    check_conversation_access(actor, conv)
    await publish_transient(
        project_id, "typing",
        {"alias": actor.alias, "role": actor.role_flag},
        conversation_id=conversation_id,
    )
    return ok({"expires_in": 6})


@router.get("/projects/{project_id}/events")
async def poll_events(
    project_id: int,
    since_id: int = 0,
    limit: int | None = None,
    timeout: float = 0,
    types: str | None = None,
    actor: Actor = ActorDep,
) -> dict:
    """Non-blocking event fetch for turn-based agents (`timeout=0`), or
    long-poll (`timeout` seconds, capped at 60) until something arrives.
    `types` (comma-separated) restricts to those event types; the cursor then
    advances only through matching events, so nothing is skipped.
    `last_event_id` is the cursor to pass as `since_id` next call."""
    check_project_access(actor, project_id)
    get_project(project_id)
    window = pagination_window(limit, max_limit=500)
    type_filter = [t.strip() for t in types.split(",") if t.strip()] if types else None
    items = visible_events_since(actor, project_id, since_id, limit=window, types=type_filter)
    if not items and timeout > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(timeout, LONG_POLL_MAX_SECONDS)
        while not items and loop.time() < deadline:
            await wait_for_events(project_id, min(5.0, max(0.05, deadline - loop.time())))
            items = visible_events_since(actor, project_id, since_id, limit=window, types=type_filter)
    last_event_id = items[-1]["id"] if items else since_id
    return ok({"items": items, "last_event_id": last_event_id})


@router.get("/projects/{project_id}/read_cursors")
async def get_read_cursors(project_id: int, agent: Actor = AgentDep) -> dict:
    check_project_access(agent, project_id)
    rows = query_all(
        """
        SELECT m.conversation_id, m.last_read_message_id, c.type, c.name,
               (SELECT COUNT(*) FROM messages msg
                WHERE msg.conversation_id = m.conversation_id
                  AND msg.id > m.last_read_message_id AND msg.deleted = 0) AS unread,
               (SELECT COUNT(*) FROM mentions mn
                JOIN messages mm ON mm.id = mn.message_id
                WHERE mm.conversation_id = m.conversation_id
                  AND mn.target_agent_id = ? AND mn.seen = 0 AND mm.deleted = 0) AS mentions_unseen
        FROM conversation_members m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.agent_id = ? AND c.project_id = ?
        ORDER BY m.conversation_id
        """,
        (agent.agent_id, agent.agent_id, project_id),
    )
    comment_mentions = query_one(
        "SELECT COUNT(*) AS c FROM mentions WHERE project_id = ? AND target_agent_id = ? "
        "AND seen = 0 AND comment_id IS NOT NULL",
        (project_id, agent.agent_id),
    )
    items = [dict(r) for r in rows]
    return ok({
        "items": items,
        "comment_mentions_unseen": comment_mentions["c"] if comment_mentions else 0,
        "total_mentions_unseen": (comment_mentions["c"] if comment_mentions else 0)
        + sum(i["mentions_unseen"] for i in items),
    })


@router.post("/projects/{project_id}/conversations/{conversation_id}/read_cursor")
async def set_read_cursor(
    project_id: int, conversation_id: int, body: ReadCursorSet, agent: Actor = AgentDep
) -> dict:
    """Set the caller's read cursor. Values beyond the newest message id are
    clamped; the cursor never moves backwards past 0."""
    check_project_access(agent, project_id)
    conv = get_conversation(project_id, conversation_id)
    check_conversation_access(agent, conv)
    max_row = query_one(
        "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    cursor = min(body.last_read_message_id, max_row["m"] if max_row else 0)
    with transaction() as conn:
        conn.execute(
            "UPDATE conversation_members SET last_read_message_id = ? "
            "WHERE conversation_id = ? AND agent_id = ?",
            (cursor, conversation_id, agent.agent_id),
        )
    unread = query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ? AND id > ? AND deleted = 0",
        (conversation_id, cursor),
    )
    return ok({
        "conversation_id": conversation_id,
        "last_read_message_id": cursor,
        "unread": unread["c"] if unread else 0,
    })


@router.get("/projects/{project_id}/mentions")
async def list_mentions(
    project_id: int,
    unseen: bool = False,
    before_id: int | None = None,
    limit: int | None = None,
    actor: Actor = ActorDep,
) -> dict:
    """Mentions targeting the caller (admin included: mentions of the admin
    alias), most recent first, joined with the mentioning message."""
    check_project_access(actor, project_id)
    get_project(project_id)
    window = pagination_window(limit)
    filters = ["mn.project_id = :pid", "mn.target_agent_id = :aid"]
    params: dict = {"pid": project_id, "aid": actor.agent_id, "lim": window + 1}
    if unseen:
        filters.append("mn.seen = 0")
    if before_id is not None:
        filters.append("mn.id < :before")
        params["before"] = before_id
    rows = query_all(
        f"""
        SELECT mn.id AS mention_id, mn.seen, mn.created_at AS mentioned_at,
               mn.message_id, mn.comment_id
        FROM mentions mn
        LEFT JOIN messages msg ON msg.id = mn.message_id
        WHERE {' AND '.join(filters)} AND (mn.message_id IS NULL OR msg.deleted = 0)
        ORDER BY mn.id DESC LIMIT :lim
        """,
        params,
    )
    has_more = len(rows) > window
    items = []
    for r in rows[:window]:
        item: dict = {
            "mention_id": r["mention_id"],
            "seen": bool(r["seen"]),
            "mentioned_at": r["mentioned_at"],
        }
        if r["message_id"] is not None:
            msg = query_one("SELECT * FROM messages WHERE id = ?", (r["message_id"],))
            item["source"] = "message"
            item["message"] = serialize_message(msg)
        else:
            comment = query_one(
                "SELECT c.*, t.number AS ticket_number FROM ticket_comments c "
                "JOIN tickets t ON t.id = c.ticket_id WHERE c.id = ?",
                (r["comment_id"],),
            )
            item["source"] = "ticket_comment"
            item["comment"] = {
                "id": comment["id"],
                "ticket_number": comment["ticket_number"],
                "author": comment["author_alias"],
                "role": comment["author_type"],
                "body": comment["body"],
                "created_at": comment["created_at"],
            }
        items.append(item)
    return ok({"items": items, "has_more": has_more})


@router.post("/projects/{project_id}/mentions/seen")
async def mark_mentions_seen(
    project_id: int, body: MentionsSeen, actor: Actor = ActorDep
) -> dict:
    check_project_access(actor, project_id)
    get_project(project_id)
    with transaction() as conn:
        if body.all:
            cur = conn.execute(
                "UPDATE mentions SET seen = 1 WHERE project_id = ? AND target_agent_id = ? AND seen = 0",
                (project_id, actor.agent_id),
            )
        else:
            placeholders = ",".join("?" for _ in body.mention_ids)
            cur = conn.execute(
                f"UPDATE mentions SET seen = 1 WHERE project_id = ? AND target_agent_id = ? "
                f"AND id IN ({placeholders})",
                (project_id, actor.agent_id, *body.mention_ids),
            )
    return ok({"marked_seen": cur.rowcount})
