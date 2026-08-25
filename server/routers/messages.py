"""Messages: posting (threads, decisions, attachments), listing with
`since_id` long-poll, edit/delete with history, pins, threads, decisions.

Listing choices (documented per contract): deleted messages are returned as
tombstones (`deleted: true`, empty body) so clients can render "message
removed" in place; thread replies are excluded from channel listings unless
`include_threads=true`."""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth import Actor, ActorDep, check_project_access
from ..db import query_all, query_one, transaction, utc_now
from ..emoji import emoji_suggestions, is_valid_emoji
from ..errors import ApiError, ok
from ..events import append_event, notify, wait_for_events
from ..services import (
    check_conversation_access,
    get_conversation,
    get_message,
    get_project,
    pagination_window,
    post_message,
    reaction_summary,
    record_ticket_links,
    require_not_archived,
    serialize_message,
)

router = APIRouter(tags=["messages"])

LONG_POLL_MAX_SECONDS = 60.0


class MessageCreate(BaseModel):
    body: str = Field(default="", max_length=64_000)
    type: str = Field(default="normal", pattern="^(normal|decision)$")
    parent_id: int | None = None
    attachment_ids: list[int] = Field(default_factory=list, max_length=20)
    doc_slug: str | None = Field(default=None, max_length=64)


class ReactionToggle(BaseModel):
    emoji: str = Field(min_length=1, max_length=64)


class ForwardRequest(BaseModel):
    to_conversation_id: int
    comment: str = Field(default="", max_length=64_000)


class MessageEdit(BaseModel):
    body: str = Field(min_length=1, max_length=64_000)


def _writable_conversation(actor: Actor, project_id: int, conversation_id: int) -> sqlite3.Row:
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    check_conversation_access(actor, conv)
    return conv


def _readable_message(actor: Actor, project_id: int, message_id: int) -> sqlite3.Row:
    check_project_access(actor, project_id)
    row = get_message(project_id, message_id)
    conv = get_conversation(project_id, row["conversation_id"])
    check_conversation_access(actor, conv)
    return row


def _is_author(actor: Actor, row: sqlite3.Row) -> bool:
    if actor.is_admin:
        return row["author_type"] == "admin"
    return row["author_type"] == "agent" and row["author_agent_id"] == actor.agent_id


@router.post("/projects/{project_id}/conversations/{conversation_id}/messages", status_code=201)
async def create_message(
    project_id: int, conversation_id: int, body: MessageCreate, actor: Actor = ActorDep
) -> dict:
    _writable_conversation(actor, project_id, conversation_id)
    if body.doc_slug is not None:
        doc = query_one(
            "SELECT 1 FROM documents WHERE project_id = ? AND slug = ?",
            (project_id, body.doc_slug),
        )
        if doc is None:
            raise ApiError(422, "unknown_document", f"No document '{body.doc_slug}' in this project.")
    payload = post_message(
        project_id=project_id,
        conversation_id=conversation_id,
        author_type=actor.role_flag,
        author_agent_id=actor.agent_id if not actor.is_admin else None,
        author_alias=actor.alias,
        body=body.body,
        msg_type=body.type,
        parent_id=body.parent_id,
        attachment_ids=body.attachment_ids,
        doc_ref=body.doc_slug,
    )
    await notify(project_id)
    return ok(payload)


@router.get("/projects/{project_id}/conversations/{conversation_id}/messages")
async def list_messages(
    project_id: int,
    conversation_id: int,
    since_id: int | None = None,
    before_id: int | None = None,
    limit: int | None = None,
    timeout: float = 0,
    include_threads: bool = False,
    mark_read: bool = False,
    actor: Actor = ActorDep,
) -> dict:
    """List messages ascending by id. `since_id` walks forward (and, with
    `timeout` > 0, long-polls until new messages arrive); `before_id` pages
    backwards; neither returns the latest page. `mark_read=true` (agents only)
    also advances the caller's read cursor to the newest returned message —
    the read-and-caught-up case in one call (issue #15 S7)."""
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    check_conversation_access(actor, conv)
    window = pagination_window(limit)

    thread_filter = "" if include_threads else "AND parent_id IS NULL"

    def fetch() -> tuple[list[sqlite3.Row], bool]:
        if since_id is not None:
            rows = query_all(
                f"SELECT * FROM messages WHERE conversation_id = ? AND id > ? {thread_filter} "
                "ORDER BY id LIMIT ?",
                (conversation_id, since_id, window + 1),
            )
            return rows[:window], len(rows) > window
        if before_id is not None:
            rows = query_all(
                f"SELECT * FROM messages WHERE conversation_id = ? AND id < ? {thread_filter} "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, before_id, window + 1),
            )
        else:
            rows = query_all(
                f"SELECT * FROM messages WHERE conversation_id = ? {thread_filter} "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, window + 1),
            )
        has_more = len(rows) > window
        return list(reversed(rows[:window])), has_more

    items, has_more = fetch()
    if not items and since_id is not None and timeout > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(timeout, LONG_POLL_MAX_SECONDS)
        while not items and loop.time() < deadline:
            await wait_for_events(project_id, min(5.0, max(0.05, deadline - loop.time())))
            items, has_more = fetch()
    if mark_read and not actor.is_admin and items:
        newest = max(r["id"] for r in items)
        with transaction() as conn:
            conn.execute(
                "UPDATE conversation_members SET last_read_message_id = ? "
                "WHERE conversation_id = ? AND agent_id = ? AND last_read_message_id < ?",
                (newest, conversation_id, actor.agent_id, newest),
            )
    return ok({"items": [serialize_message(r) for r in items], "has_more": has_more})


@router.get("/projects/{project_id}/messages/{message_id}")
async def get_message_endpoint(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    row = _readable_message(actor, project_id, message_id)
    return ok(serialize_message(row))


@router.get("/projects/{project_id}/messages/{message_id}/thread")
async def get_thread(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    root = _readable_message(actor, project_id, message_id)
    if root["parent_id"] is not None:
        root = get_message(project_id, root["parent_id"])
    replies = query_all(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY id", (root["id"],)
    )
    return ok({
        "root": serialize_message(root),
        "replies": [serialize_message(r) for r in replies],
    })


@router.patch("/projects/{project_id}/messages/{message_id}")
async def edit_message(
    project_id: int, message_id: int, body: MessageEdit, actor: Actor = ActorDep
) -> dict:
    require_not_archived(get_project(project_id))
    row = _readable_message(actor, project_id, message_id)
    if row["deleted"]:
        raise ApiError(409, "deleted", "Deleted messages cannot be edited.")
    if not _is_author(actor, row):
        raise ApiError(403, "not_author", "Only the author may edit a message.")
    now = utc_now()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO message_edits (message_id, prev_body, edited_by, edited_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, row["body"], actor.alias, now),
        )
        conn.execute(
            "UPDATE messages SET body = ?, edited_at = ? WHERE id = ?",
            (body.body, now, message_id),
        )
        record_ticket_links(conn, project_id, "message", message_id, body.body)
        updated = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        payload = serialize_message(updated)
        append_event(
            conn, project_id, "message_edited", payload,
            conversation_id=row["conversation_id"],
        )
    await notify(project_id)
    return ok(payload)


@router.delete("/projects/{project_id}/messages/{message_id}")
async def delete_message(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    """Soft delete. Authors may delete their own messages; the admin may
    delete anything. The prior body is retained in the edit history."""
    row = _readable_message(actor, project_id, message_id)
    if row["deleted"]:
        return ok({"deleted": True, "id": message_id})
    if not (actor.is_admin or _is_author(actor, row)):
        raise ApiError(403, "not_author", "Only the author or the admin may delete a message.")
    now = utc_now()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO message_edits (message_id, prev_body, edited_by, edited_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, row["body"], actor.alias, now),
        )
        conn.execute(
            "UPDATE messages SET deleted = 1, pinned = 0, pinned_at = NULL, pinned_by = NULL "
            "WHERE id = ?",
            (message_id,),
        )
        append_event(
            conn, project_id, "message_deleted",
            {"id": message_id, "conversation_id": row["conversation_id"], "by": actor.alias},
            conversation_id=row["conversation_id"],
        )
    await notify(project_id)
    return ok({"deleted": True, "id": message_id})


@router.get("/projects/{project_id}/messages/{message_id}/edits")
async def message_edit_history(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    _readable_message(actor, project_id, message_id)
    rows = query_all(
        "SELECT id, prev_body, edited_by, edited_at FROM message_edits "
        "WHERE message_id = ? ORDER BY id",
        (message_id,),
    )
    return ok({"items": [dict(r) for r in rows]})


@router.post("/projects/{project_id}/messages/{message_id}/pin")
async def pin_message(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    require_not_archived(get_project(project_id))
    row = _readable_message(actor, project_id, message_id)
    if row["deleted"]:
        raise ApiError(409, "deleted", "Deleted messages cannot be pinned.")
    with transaction() as conn:
        conn.execute(
            "UPDATE messages SET pinned = 1, pinned_at = ?, pinned_by = ? WHERE id = ?",
            (utc_now(), actor.alias, message_id),
        )
        updated = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        payload = serialize_message(updated)
        append_event(
            conn, project_id, "message_pinned", payload,
            conversation_id=row["conversation_id"],
        )
    await notify(project_id)
    return ok(payload)


@router.post("/projects/{project_id}/messages/{message_id}/unpin")
async def unpin_message(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    require_not_archived(get_project(project_id))
    row = _readable_message(actor, project_id, message_id)
    with transaction() as conn:
        conn.execute(
            "UPDATE messages SET pinned = 0, pinned_at = NULL, pinned_by = NULL WHERE id = ?",
            (message_id,),
        )
        append_event(
            conn, project_id, "message_unpinned",
            {"id": message_id, "conversation_id": row["conversation_id"], "by": actor.alias},
            conversation_id=row["conversation_id"],
        )
    await notify(project_id)
    return ok({"pinned": False, "id": message_id})


@router.get("/projects/{project_id}/conversations/{conversation_id}/pins")
async def list_pins(project_id: int, conversation_id: int, actor: Actor = ActorDep) -> dict:
    check_project_access(actor, project_id)
    conv = get_conversation(project_id, conversation_id)
    check_conversation_access(actor, conv)
    rows = query_all(
        "SELECT * FROM messages WHERE conversation_id = ? AND pinned = 1 AND deleted = 0 "
        "ORDER BY pinned_at",
        (conversation_id,),
    )
    return ok({"items": [serialize_message(r) for r in rows]})


@router.get("/projects/{project_id}/decisions")
async def list_decisions(
    project_id: int,
    before_id: int | None = None,
    limit: int | None = None,
    actor: Actor = ActorDep,
) -> dict:
    """Decision-type messages, most recent first. Agents see decisions from
    conversations they belong to; the admin sees all."""
    check_project_access(actor, project_id)
    window = pagination_window(limit)
    before_filter = "AND m.id < :before" if before_id is not None else ""
    if actor.is_admin:
        member_filter = ""
    else:
        member_filter = (
            "AND m.conversation_id IN (SELECT conversation_id FROM conversation_members "
            "WHERE agent_id = :aid)"
        )
    rows = query_all(
        f"SELECT m.* FROM messages m WHERE m.project_id = :pid AND m.type = 'decision' "
        f"AND m.deleted = 0 {before_filter} {member_filter} ORDER BY m.id DESC LIMIT :lim",
        {"pid": project_id, "before": before_id, "aid": actor.agent_id, "lim": window + 1},
    )
    has_more = len(rows) > window
    return ok({"items": [serialize_message(r) for r in rows[:window]], "has_more": has_more})


# ------------------------------------------------------------- reactions

@router.post("/projects/{project_id}/messages/{message_id}/reactions")
async def toggle_reaction(
    project_id: int, message_id: int, body: ReactionToggle, actor: Actor = ActorDep
) -> dict:
    """Slack semantics: reacting with an emoji you already used removes it."""
    if not is_valid_emoji(body.emoji):
        hints = emoji_suggestions(body.emoji)
        hint_text = f" Did you mean: {', '.join(hints)}?" if hints else ""
        raise ApiError(
            422, "unknown_emoji",
            f"'{body.emoji}' is not in the emoji set (see GET /api/emoji).{hint_text}",
            {"suggestions": hints},
        )
    row = _readable_message(actor, project_id, message_id)
    if row["deleted"]:
        raise ApiError(410, "gone", "You cannot react to a deleted message.")
    now = utc_now()
    with transaction() as conn:
        # Re-check deletion inside the write lock: a delete may have committed
        # between the read above and BEGIN IMMEDIATE.
        live = conn.execute(
            "SELECT deleted FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if live is None or live["deleted"]:
            raise ApiError(410, "gone", "You cannot react to a deleted message.")
        try:
            conn.execute(
                "INSERT INTO message_reactions (project_id, message_id, emoji, actor_agent_id, "
                "actor_alias, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, message_id, body.emoji, actor.agent_id, actor.alias, now),
            )
            reacted = True
            conn.execute(
                "INSERT INTO emoji_usage (project_id, actor_agent_id, emoji, uses, last_used) "
                "VALUES (?, ?, ?, 1, ?) ON CONFLICT (project_id, actor_agent_id, emoji) "
                "DO UPDATE SET uses = uses + 1, last_used = excluded.last_used",
                (project_id, actor.agent_id, body.emoji, now),
            )
        except sqlite3.IntegrityError:
            conn.execute(
                "DELETE FROM message_reactions WHERE message_id = ? AND emoji = ? AND actor_agent_id = ?",
                (message_id, body.emoji, actor.agent_id),
            )
            reacted = False
        summary = reaction_summary(message_id)
        append_event(
            conn, project_id, "reaction",
            {"message_id": message_id, "conversation_id": row["conversation_id"],
             "emoji": body.emoji, "actor": actor.alias, "role": actor.role_flag,
             "reacted": reacted, "reactions": summary},
            conversation_id=row["conversation_id"],
        )
    await notify(project_id)
    return ok({"emoji": body.emoji, "reacted": reacted, "reactions": summary})


@router.get("/projects/{project_id}/messages/{message_id}/reactions")
async def list_reactions(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    _readable_message(actor, project_id, message_id)
    return ok({"reactions": reaction_summary(message_id)})


# ------------------------------------------------------------ forwarding

@router.post("/projects/{project_id}/messages/{message_id}/forward", status_code=201)
async def forward_message(
    project_id: int, message_id: int, body: ForwardRequest, actor: Actor = ActorDep
) -> dict:
    """Forward a message you can read into a conversation you belong to
    (DM→channel and channel→DM included). The embed is a read-time-resolved
    copy attributed to the original author; forwarding a forward re-anchors to
    the ORIGINAL message, so there are never chains of chains."""
    source = _readable_message(actor, project_id, message_id)
    if source["deleted"]:
        raise ApiError(410, "gone", "The original message was deleted.")
    original_id = source["forwarded_from_id"] or source["id"]
    original = query_one("SELECT * FROM messages WHERE id = ?", (original_id,))
    if original is None or original["deleted"]:
        raise ApiError(410, "gone", "The original message was deleted.")
    _writable_conversation(actor, project_id, body.to_conversation_id)
    payload = post_message(
        project_id=project_id,
        conversation_id=body.to_conversation_id,
        author_type=actor.role_flag,
        author_agent_id=actor.agent_id if not actor.is_admin else None,
        author_alias=actor.alias,
        body=body.comment,
        forwarded_from_id=original_id,
    )
    await notify(project_id)
    return ok(payload)


# ------------------------------------------------------------ saved items

@router.post("/projects/{project_id}/messages/{message_id}/save")
async def toggle_saved(project_id: int, message_id: int, actor: Actor = ActorDep) -> dict:
    """Personal saved-for-later flag (toggle). Private to the caller — no
    event is emitted."""
    _readable_message(actor, project_id, message_id)
    with transaction() as conn:
        try:
            conn.execute(
                "INSERT INTO saved_items (project_id, actor_agent_id, message_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, actor.agent_id, message_id, utc_now()),
            )
            saved = True
        except sqlite3.IntegrityError:
            conn.execute(
                "DELETE FROM saved_items WHERE actor_agent_id = ? AND message_id = ?",
                (actor.agent_id, message_id),
            )
            saved = False
    return ok({"message_id": message_id, "saved": saved})


@router.get("/projects/{project_id}/saved")
async def list_saved(
    project_id: int,
    before_id: int | None = None,
    limit: int | None = None,
    actor: Actor = ActorDep,
) -> dict:
    check_project_access(actor, project_id)
    window = pagination_window(limit)
    before_filter = "AND s.message_id < :before" if before_id is not None else ""
    # Saved rows are filtered through CURRENT membership: leaving a channel
    # (or being removed from a DM) revokes access to messages saved from it.
    member_filter = (
        "" if actor.is_admin
        else "AND m.conversation_id IN (SELECT conversation_id FROM conversation_members WHERE agent_id = :aid)"
    )
    rows = query_all(
        f"""
        SELECT m.* FROM saved_items s JOIN messages m ON m.id = s.message_id
        WHERE s.project_id = :pid AND s.actor_agent_id = :aid {before_filter} {member_filter}
        ORDER BY s.message_id DESC LIMIT :lim
        """,
        {"pid": project_id, "aid": actor.agent_id, "before": before_id, "lim": window + 1},
    )
    has_more = len(rows) > window
    return ok({"items": [serialize_message(r) for r in rows[:window]], "has_more": has_more})
