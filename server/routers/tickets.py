"""Tickets: project-scoped issues with atomic claiming, threaded comments,
status flow, labels, and #N cross-link backlinks."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import audit
from ..auth import ADMIN_AGENT_ID, Actor, AdminDep, AgentDep, check_project_access, get_actor
from ..db import query_all, query_one, transaction, utc_now
from ..errors import ApiError, ok
from ..events import append_event, notify
from ..services import (
    MENTION_RE,
    get_project,
    mention_candidates,
    pagination_window,
    post_system_message,
    project_settings,
    record_ticket_links,
    require_not_archived,
    resolve_alias,
)

router = APIRouter(tags=["tickets"])

PRIORITIES = ("low", "medium", "high", "urgent")


class TicketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    priority: str = Field(default="medium")
    labels: list[str] = Field(default_factory=list)
    assignee: str | None = None


class TicketUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    priority: str | None = None
    labels: list[str] | None = None
    assignee: str | None = None
    status: str | None = None


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    parent_id: int | None = None
    attachment_ids: list[int] = Field(default_factory=list)


def serialize_ticket(row: sqlite3.Row) -> dict[str, Any]:
    comment_count = query_one(
        "SELECT COUNT(*) AS c FROM ticket_comments WHERE ticket_id = ?", (row["id"],)
    )
    return {
        "id": row["id"],
        "number": row["number"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "labels": json.loads(row["labels"] or "[]"),
        "assignee": row["assignee"],
        "reporter": row["reporter"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "comment_count": comment_count["c"] if comment_count else 0,
    }


def serialize_comment(row: sqlite3.Row) -> dict[str, Any]:
    attachments = query_all(
        "SELECT id, filename, size, mime_type, uploader, created_at FROM attachments WHERE comment_id = ?",
        (row["id"],),
    )
    ticket = query_one("SELECT project_id FROM tickets WHERE id = ?", (row["ticket_id"],))
    project_id = ticket["project_id"] if ticket else 0
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "parent_id": row["parent_id"],
        "author": row["author_alias"],
        "role": row["author_type"],
        "body": row["body"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "attachments": [
            dict(a) | {"url": f"/api/projects/{project_id}/attachments/{a['id']}"}
            for a in attachments
        ],
    }


def get_ticket_row(project_id: int, number: int) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM tickets WHERE project_id = ? AND number = ?", (project_id, number)
    )
    if row is None:
        raise ApiError(404, "not_found", f"Ticket #{number} not found in project {project_id}.")
    return row


def _validate_priority(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ApiError(422, "invalid_priority", f"Priority must be one of: {', '.join(PRIORITIES)}.")
    return priority


def _validate_labels(labels: list[str]) -> str:
    cleaned = [label.strip() for label in labels if label.strip()]
    if len(cleaned) > 20:
        raise ApiError(422, "too_many_labels", "At most 20 labels per ticket.")
    return json.dumps(cleaned)


def _validate_assignee(project_id: int, assignee: str | None) -> str | None:
    if assignee is None:
        return None
    resolved = resolve_alias(project_id, assignee)
    if resolved is None:
        raise ApiError(422, "unknown_assignee", f"'{assignee}' is not an agent or the admin in this project.")
    return resolved[1]


def _mention_targets(project_id: int, body: str) -> set[int]:
    """Same semantics as message mentions: @alias, @here, admin included."""
    targets: set[int] = set()
    here = False
    for raw in MENTION_RE.findall(body):
        for alias in mention_candidates(raw):
            if alias.lower() == "here":
                here = True
                break
            resolved = resolve_alias(project_id, alias)
            if resolved is not None:
                targets.add(resolved[0])
                break
    if here:
        for r in query_all(
            "SELECT id FROM agents WHERE project_id = ? AND revoked = 0", (project_id,)
        ):
            targets.add(r["id"])
    return targets


async def update_ticket_fields(
    project_id: int, number: int, actor: Actor, fields: TicketUpdate, fields_set: set[str]
) -> dict[str, Any]:
    """Shared update path for PATCH and board moves: validates, applies, emits
    events + system messages. Board moves call this so there is exactly one
    status-change code path."""
    project = get_project(project_id)
    require_not_archived(project)
    statuses = project_settings(project)["ticket_statuses"]

    if "status" in fields_set and fields.status is not None and fields.status not in statuses:
        raise ApiError(
            422, "invalid_status",
            f"Status '{fields.status}' is not in this project's flow: {', '.join(statuses)}.",
        )
    if "status" in fields_set and fields.status is None:
        raise ApiError(422, "invalid_status", "Status cannot be null.")
    if "priority" in fields_set and fields.priority is not None:
        _validate_priority(fields.priority)

    now = utc_now()
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE project_id = ? AND number = ?", (project_id, number)
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"Ticket #{number} not found in project {project_id}.")

        changes: dict[str, Any] = {}
        sets: list[str] = []
        params: list[Any] = []

        def apply(column: str, value: Any) -> None:
            sets.append(f"{column} = ?")
            params.append(value)

        if "title" in fields_set and fields.title is not None and fields.title != row["title"]:
            changes["title"] = {"from": row["title"], "to": fields.title}
            apply("title", fields.title)
        if "description" in fields_set and fields.description is not None and fields.description != row["description"]:
            changes["description"] = {"changed": True}
            apply("description", fields.description)
        if "priority" in fields_set and fields.priority is not None and fields.priority != row["priority"]:
            changes["priority"] = {"from": row["priority"], "to": fields.priority}
            apply("priority", fields.priority)
        if "labels" in fields_set and fields.labels is not None:
            new_labels = _validate_labels(fields.labels)
            if new_labels != row["labels"]:
                changes["labels"] = {"from": json.loads(row["labels"] or "[]"), "to": json.loads(new_labels)}
                apply("labels", new_labels)
        if "assignee" in fields_set:
            new_assignee = _validate_assignee(project_id, fields.assignee)
            if new_assignee != row["assignee"]:
                changes["assignee"] = {"from": row["assignee"], "to": new_assignee}
                apply("assignee", new_assignee)
        if "status" in fields_set and fields.status is not None and fields.status != row["status"]:
            changes["status"] = {"from": row["status"], "to": fields.status}
            apply("status", fields.status)

        if changes:
            apply("updated_at", now)
            params.extend([project_id, number])
            conn.execute(
                f"UPDATE tickets SET {', '.join(sets)} WHERE project_id = ? AND number = ?",
                params,
            )
        updated = conn.execute(
            "SELECT * FROM tickets WHERE project_id = ? AND number = ?", (project_id, number)
        ).fetchone()
        if changes:
            append_event(
                conn, project_id, "ticket_updated",
                {"ticket": serialize_ticket_row_in_txn(updated), "changes": changes, "by": actor.alias},
            )
            if changes.get("status"):
                audit.platform_record(
                    conn, project_id, "ticket",
                    f"Ticket #{number} status: {changes['status']['from']} -> {changes['status']['to']} (by {actor.alias})",
                    target=f"#{number}", actor=actor.alias,
                )
            if changes.get("status", {}).get("to") == "awaiting-human":
                append_event(
                    conn, project_id, "awaiting_human",
                    {"ticket_number": number, "title": updated["title"], "by": actor.alias},
                    target_agent_id=ADMIN_AGENT_ID,
                )

    if changes.get("status"):
        post_system_message(
            project_id,
            f"Ticket #{number} status: {changes['status']['from']} → {changes['status']['to']} (by {actor.alias})",
        )
    if changes.get("assignee"):
        target = changes["assignee"]["to"]
        text = (
            f"Ticket #{number} assigned to {target} (by {actor.alias})"
            if target else f"Ticket #{number} unassigned (by {actor.alias})"
        )
        post_system_message(project_id, text)
        # Targeted heads-up so the assignee learns directly instead of via
        # channel noise (issue #15 S8). Fires only when assigned by someone
        # else — self-claims are not news to the claimer.
        if target is not None:
            resolved = resolve_alias(project_id, target)
            if resolved is not None and resolved[1] != actor.alias:
                with transaction() as conn:
                    append_event(
                        conn, project_id, "ticket_assigned",
                        {"ticket_number": number, "title": row["title"],
                         "assignee": resolved[1], "by": actor.alias},
                        target_agent_id=resolved[0],
                    )
    if changes:
        await notify(project_id)
    return serialize_ticket(get_ticket_row(project_id, number))


def _emit_assigned_at_creation(
    conn: sqlite3.Connection, project_id: int, row: sqlite3.Row, by_alias: str
) -> None:
    """Targeted ticket_assigned for tickets born with an assignee (issue #28),
    mirroring the reassignment path. Self-assignment is not news."""
    if not row["assignee"]:
        return
    resolved = resolve_alias(project_id, row["assignee"])
    if resolved is not None and resolved[1] != by_alias:
        append_event(
            conn, project_id, "ticket_assigned",
            {"ticket_number": row["number"], "title": row["title"],
             "assignee": resolved[1], "by": by_alias},
            target_agent_id=resolved[0],
        )


def serialize_ticket_row_in_txn(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize without issuing new queries on other tables (safe mid-txn)."""
    return {
        "id": row["id"],
        "number": row["number"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "labels": json.loads(row["labels"] or "[]"),
        "assignee": row["assignee"],
        "reporter": row["reporter"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("/projects/{project_id}/tickets", status_code=201)
async def create_ticket(project_id: int, body: TicketCreate, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    _validate_priority(body.priority)
    labels = _validate_labels(body.labels)
    assignee = _validate_assignee(project_id, body.assignee)
    now = utc_now()
    with transaction() as conn:
        allocated = conn.execute(
            "UPDATE projects SET next_ticket_number = next_ticket_number + 1 "
            "WHERE id = ? RETURNING next_ticket_number",
            (project_id,),
        ).fetchone()
        number = int(allocated[0]) - 1
        cur = conn.execute(
            "INSERT INTO tickets (project_id, number, title, description, status, priority, "
            "labels, assignee, reporter, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
            (project_id, number, body.title, body.description, body.priority,
             labels, assignee, actor.alias, now, now),
        )
        ticket_id = int(cur.lastrowid or 0)
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        append_event(
            conn, project_id, "ticket_created",
            {"ticket": serialize_ticket_row_in_txn(row), "by": actor.alias},
        )
        # Assignment at creation must reach the assignee's targeted feed the
        # same way a later reassignment does (issue #28): an agent filtering
        # types=ticket_assigned otherwise never hears about it.
        _emit_assigned_at_creation(conn, project_id, row, actor.alias)
    post_system_message(project_id, f"Ticket #{number} created: {body.title} (by {actor.alias})")
    await notify(project_id)
    return ok(serialize_ticket(get_ticket_row(project_id, number)))


@router.get("/projects/{project_id}/tickets")
async def list_tickets(
    project_id: int,
    request: Request,
    status: str | None = None,
    assignee: str | None = None,
    reporter: str | None = None,
    priority: str | None = None,
    label: str | None = None,
    q: str | None = None,
    before_id: int | None = None,
    limit: int | None = None,
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    get_project(project_id)
    window = pagination_window(limit)
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if assignee is not None:
        # "me" answers "what am I holding?" without client-side filtering
        # (issue #15 S5). For the admin, "me" is the admin alias.
        clauses.append("assignee = ? COLLATE NOCASE")
        params.append(actor.alias if assignee == "me" else assignee)
    if reporter is not None:
        clauses.append("reporter = ? COLLATE NOCASE")
        params.append(actor.alias if reporter == "me" else reporter)
    if priority is not None:
        clauses.append("priority = ?")
        params.append(priority)
    if label is not None:
        clauses.append("EXISTS (SELECT 1 FROM json_each(tickets.labels) WHERE json_each.value = ?)")
        params.append(label)
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    params.append(window + 1)
    rows = query_all(
        f"SELECT * FROM tickets WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > window
    return ok({"items": [serialize_ticket(r) for r in rows[:window]], "has_more": has_more})


@router.get("/projects/{project_id}/tickets/{number}")
async def get_ticket(project_id: int, number: int, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    row = get_ticket_row(project_id, number)
    data = serialize_ticket(row)
    # Message-sourced backlinks are limited to conversations the caller may
    # see, so a ticket referenced inside a DM never leaks the DM's content.
    visibility = ""
    params: list = [row["id"]]
    if not actor.is_admin:
        visibility = (
            " AND (l.source_type != 'message' OR m.conversation_id IN "
            "(SELECT conversation_id FROM conversation_members WHERE agent_id = ?))"
        )
        params.append(actor.agent_id)
    backlinks = query_all(
        f"""
        SELECT l.source_type, l.source_id, l.created_at AS linked_at,
               m.conversation_id,
               COALESCE(m.body, c.body) AS body,
               COALESCE(m.author_alias, c.author_alias) AS author
        FROM ticket_links l
        LEFT JOIN messages m ON l.source_type = 'message' AND m.id = l.source_id
        LEFT JOIN ticket_comments c ON l.source_type = 'ticket_comment' AND c.id = l.source_id
        WHERE l.ticket_id = ?
          AND (l.source_type != 'message' OR m.deleted = 0){visibility}
        ORDER BY l.id DESC LIMIT 50
        """,
        tuple(params),
    )
    data["mentioned_in"] = [
        {
            "source_type": b["source_type"],
            "source_id": b["source_id"],
            "conversation_id": b["conversation_id"],
            "author": b["author"],
            "created_at": b["linked_at"],
            "excerpt": (b["body"] or "")[:160],
        }
        for b in backlinks
    ]
    return ok(data)


@router.patch("/projects/{project_id}/tickets/{number}")
async def patch_ticket(
    project_id: int, number: int, body: TicketUpdate, request: Request
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    data = await update_ticket_fields(
        project_id, number, actor, body, set(body.model_fields_set)
    )
    return ok(data)


@router.post("/projects/{project_id}/tickets/{number}/claim")
async def claim_ticket(project_id: int, number: int, agent: Actor = AgentDep) -> dict:
    """Atomic claim: exactly one winner under concurrency; losers get 409."""
    check_project_access(agent, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    now = utc_now()
    with transaction() as conn:
        before = conn.execute(
            "SELECT status FROM tickets WHERE project_id = ? AND number = ?",
            (project_id, number),
        ).fetchone()
        cur = conn.execute(
            "UPDATE tickets SET assignee = ?, "
            "status = CASE WHEN status = 'open' THEN 'in-progress' ELSE status END, "
            "updated_at = ? "
            "WHERE project_id = ? AND number = ? AND assignee IS NULL",
            (agent.alias, now, project_id, number),
        )
        if cur.rowcount == 0:
            existing = conn.execute(
                "SELECT assignee FROM tickets WHERE project_id = ? AND number = ?",
                (project_id, number),
            ).fetchone()
            if existing is None:
                raise ApiError(404, "not_found", f"Ticket #{number} not found in project {project_id}.")
            raise ApiError(
                409, "already_claimed",
                f"Ticket #{number} is already assigned to {existing['assignee']}.",
                {"assignee": existing["assignee"]},
            )
        row = conn.execute(
            "SELECT * FROM tickets WHERE project_id = ? AND number = ?", (project_id, number)
        ).fetchone()
        audit.platform_record(
            conn, project_id, "ticket",
            f"Ticket #{number} claimed by {agent.alias} (status: {row['status']})",
            target=f"#{number}", actor=agent.alias,
        )
        # The claim can flip status too (open -> in-progress); the event must
        # say so or SSE consumers watching status never see the move (#28).
        changes: dict = {"assignee": {"from": None, "to": agent.alias}}
        if before is not None and before["status"] != row["status"]:
            changes["status"] = {"from": before["status"], "to": row["status"]}
        append_event(
            conn, project_id, "ticket_updated",
            {"ticket": serialize_ticket_row_in_txn(row),
             "changes": changes,
             "by": agent.alias},
        )
    post_system_message(project_id, f"Ticket #{number} claimed by {agent.alias}")
    await notify(project_id)
    return ok(serialize_ticket(get_ticket_row(project_id, number)))


@router.delete("/projects/{project_id}/tickets/{number}")
async def delete_ticket(project_id: int, number: int, _admin: Actor = AdminDep) -> dict:
    row = get_ticket_row(project_id, number)
    with transaction() as conn:
        conn.execute("DELETE FROM tickets WHERE id = ?", (row["id"],))
        append_event(conn, project_id, "ticket_deleted", {"number": number})
    await notify(project_id)
    return ok({"deleted": True, "number": number})


@router.post("/projects/{project_id}/tickets/{number}/comments", status_code=201)
async def create_comment(
    project_id: int, number: int, body: CommentCreate, request: Request
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    ticket = get_ticket_row(project_id, number)
    now = utc_now()
    with transaction() as conn:
        if body.parent_id is not None:
            parent = conn.execute(
                "SELECT id, parent_id FROM ticket_comments WHERE id = ? AND ticket_id = ?",
                (body.parent_id, ticket["id"]),
            ).fetchone()
            if parent is None:
                raise ApiError(404, "not_found", "Parent comment not found on this ticket.")
            parent_id = parent["parent_id"] if parent["parent_id"] is not None else parent["id"]
        else:
            parent_id = None
        cur = conn.execute(
            "INSERT INTO ticket_comments (ticket_id, parent_id, author_type, author_alias, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticket["id"], parent_id, actor.role_flag, actor.alias, body.body, now),
        )
        comment_id = int(cur.lastrowid or 0)

        for att_id in body.attachment_ids:
            claimed = conn.execute(
                "UPDATE attachments SET comment_id = ? "
                "WHERE id = ? AND project_id = ? AND message_id IS NULL "
                "AND comment_id IS NULL AND document_id IS NULL AND uploader = ?",
                (comment_id, att_id, project_id, actor.alias),
            )
            if claimed.rowcount == 0:
                raise ApiError(
                    422, "bad_attachment",
                    f"Attachment {att_id} does not exist, is already attached, or is not yours.",
                )

        record_ticket_links(conn, project_id, "ticket_comment", comment_id, body.body)

        targets = _mention_targets(project_id, body.body)
        targets.discard(actor.agent_id)
        row = conn.execute(
            "SELECT * FROM ticket_comments WHERE id = ?", (comment_id,)
        ).fetchone()
        comment_payload = {
            "id": row["id"],
            "ticket_id": row["ticket_id"],
            "ticket_number": number,
            "parent_id": row["parent_id"],
            "author": row["author_alias"],
            "role": row["author_type"],
            "body": row["body"],
            "created_at": row["created_at"],
        }
        append_event(conn, project_id, "ticket_comment", comment_payload)
        for target in targets:
            conn.execute(
                "INSERT INTO mentions (project_id, comment_id, target_agent_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, comment_id, target, row["created_at"]),
            )
            append_event(
                conn, project_id, "mention",
                {"ticket_number": number, "comment_id": comment_id,
                 "by": actor.alias, "excerpt": body.body[:200]},
                target_agent_id=target,
            )
    await notify(project_id)
    return ok(serialize_comment(query_one("SELECT * FROM ticket_comments WHERE id = ?", (comment_id,))))


@router.get("/projects/{project_id}/tickets/{number}/comments")
async def list_comments(
    project_id: int,
    number: int,
    request: Request,
    since_id: int | None = None,
    limit: int | None = None,
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    ticket = get_ticket_row(project_id, number)
    window = pagination_window(limit, max_limit=500)
    params: list[Any] = [ticket["id"]]
    since_clause = ""
    if since_id is not None:
        since_clause = "AND id > ?"
        params.append(since_id)
    params.append(window + 1)
    rows = query_all(
        f"SELECT * FROM ticket_comments WHERE ticket_id = ? {since_clause} ORDER BY id LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > window
    return ok({"items": [serialize_comment(r) for r in rows[:window]], "has_more": has_more})


class TicketBulkCreate(BaseModel):
    tickets: list[TicketCreate] = Field(min_length=1, max_length=50)


@router.post("/projects/{project_id}/tickets/bulk", status_code=201)
async def create_tickets_bulk(project_id: int, body: TicketBulkCreate, request: Request) -> dict:
    """Create up to 50 tickets in one atomic call — leads seeding a sprint
    make one request instead of N (issue #15 S10). All-or-nothing: any
    validation failure creates no tickets."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    validated = []
    for item in body.tickets:
        _validate_priority(item.priority)
        validated.append(
            (item, _validate_labels(item.labels), _validate_assignee(project_id, item.assignee))
        )
    now = utc_now()
    numbers: list[int] = []
    with transaction() as conn:
        for item, labels, assignee in validated:
            allocated = conn.execute(
                "UPDATE projects SET next_ticket_number = next_ticket_number + 1 "
                "WHERE id = ? RETURNING next_ticket_number",
                (project_id,),
            ).fetchone()
            number = int(allocated[0]) - 1
            conn.execute(
                "INSERT INTO tickets (project_id, number, title, description, status, priority, "
                "labels, assignee, reporter, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (project_id, number, item.title, item.description, item.priority,
                 labels, assignee, actor.alias, now, now),
            )
            row = conn.execute(
                "SELECT * FROM tickets WHERE project_id = ? AND number = ?",
                (project_id, number),
            ).fetchone()
            append_event(
                conn, project_id, "ticket_created",
                {"ticket": serialize_ticket_row_in_txn(row), "by": actor.alias},
            )
            _emit_assigned_at_creation(conn, project_id, row, actor.alias)
            numbers.append(number)
    first, last = numbers[0], numbers[-1]
    label = f"#{first}" if first == last else f"#{first}–#{last}"
    post_system_message(project_id, f"{len(numbers)} tickets created ({label}) by {actor.alias}")
    await notify(project_id)
    return ok({
        "items": [serialize_ticket(get_ticket_row(project_id, n)) for n in numbers],
    })
