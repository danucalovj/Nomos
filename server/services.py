"""Shared domain logic used by multiple routers: project/channel bootstrap,
membership checks, message posting (mentions, ticket cross-links, events),
system messages, and serialization helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .auth import ADMIN_AGENT_ID, Actor, get_admin_alias
from .db import query_all, query_one, transaction, utc_now
from .errors import ApiError
from .events import append_event

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")
MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9][A-Za-z0-9_.-]*)")
# Bounded digits: an unbounded int overflows SQLite's int64 binding and a
# body like "#99999999999999999999" would 500 the whole request (issue #28 H11).
TICKET_REF_RE = re.compile(r"(?<![\w&])#(\d{1,9})\b")

DEFAULT_TICKET_STATUSES = ["open", "in-progress", "awaiting-human", "blocked", "done", "wontfix"]
DEFAULT_BOARD_COLUMNS: list[tuple[str, list[str]]] = [
    ("Backlog", ["open"]),
    ("In Progress", ["in-progress"]),
    ("Review", ["awaiting-human", "blocked"]),
    ("Done", ["done", "wontfix"]),
]
MAIN_CHANNEL_NAME = "general"


# ---------------------------------------------------------------- projects

def get_project(project_id: int) -> sqlite3.Row:
    row = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise ApiError(404, "not_found", f"Project {project_id} does not exist.")
    return row


def project_settings(project: sqlite3.Row) -> dict[str, Any]:
    settings = json.loads(project["settings"] or "{}")
    settings.setdefault("ticket_statuses", DEFAULT_TICKET_STATUSES)
    settings.setdefault("system_messages_enabled", True)
    return settings


def serialize_project(project: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": project["id"],
        "name": project["name"],
        "description": project["description"],
        "archived": bool(project["archived"]),
        "working_dir": project["working_dir"],
        "settings": project_settings(project),
        "created_by": project["created_by"],
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
    }


def create_project(name: str, description: str, created_by: str) -> dict[str, Any]:
    """Create a project with its main channel and default board columns."""
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_name", "Project name must not be empty.")
    now = utc_now()
    with transaction() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO projects (name, description, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, description, created_by, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "duplicate_name", f"A project named '{name}' already exists.") from exc
        project_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO conversations (project_id, type, name, topic, is_main, created_by, created_at) "
            "VALUES (?, 'channel', ?, 'Main project channel', 1, ?, ?)",
            (project_id, MAIN_CHANNEL_NAME, created_by, now),
        )
        for position, (col_name, statuses) in enumerate(DEFAULT_BOARD_COLUMNS):
            conn.execute(
                "INSERT INTO board_columns (project_id, name, position, statuses) VALUES (?, ?, ?, ?)",
                (project_id, col_name, position, json.dumps(statuses)),
            )
    return serialize_project(get_project(project_id))


def require_not_archived(project: sqlite3.Row) -> None:
    if project["archived"]:
        raise ApiError(409, "archived", "This project is archived (read-only).")


# ------------------------------------------------------------ conversations

def get_conversation(project_id: int, conversation_id: int) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM conversations WHERE id = ? AND project_id = ?",
        (conversation_id, project_id),
    )
    if row is None:
        raise ApiError(404, "not_found", f"Conversation {conversation_id} not found in project {project_id}.")
    return row


def get_main_channel(project_id: int) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM conversations WHERE project_id = ? AND is_main = 1", (project_id,)
    )
    if row is None:  # cannot happen for a live project
        raise ApiError(500, "missing_main_channel", "Project has no main channel.")
    return row


def is_member(conversation_id: int, agent_id: int) -> bool:
    return (
        query_one(
            "SELECT 1 FROM conversation_members WHERE conversation_id = ? AND agent_id = ?",
            (conversation_id, agent_id),
        )
        is not None
    )


def check_conversation_access(actor: Actor, conversation: sqlite3.Row) -> None:
    """Admin sees every conversation (including agent<->agent DMs). Agents must
    be members of a conversation to read or post."""
    if actor.is_admin:
        return
    if not is_member(conversation["id"], actor.agent_id):
        kind = "DM" if conversation["type"] == "dm" else "channel"
        raise ApiError(403, "not_a_member", f"You are not a member of this {kind}.")


def add_member(conn: sqlite3.Connection, conversation_id: int, agent_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO conversation_members (conversation_id, agent_id, joined_at) "
        "VALUES (?, ?, ?)",
        (conversation_id, agent_id, utc_now()),
    )


def serialize_conversation(conv: sqlite3.Row) -> dict[str, Any]:
    member_rows = query_all(
        "SELECT m.agent_id, a.alias FROM conversation_members m "
        "LEFT JOIN agents a ON a.id = m.agent_id WHERE m.conversation_id = ? ORDER BY m.agent_id",
        (conv["id"],),
    )
    members = [
        {"agent_id": r["agent_id"],
         "alias": r["alias"] if r["agent_id"] != ADMIN_AGENT_ID else (get_admin_alias() or "admin"),
         "role": "admin" if r["agent_id"] == ADMIN_AGENT_ID else "agent"}
        for r in member_rows
    ]
    return {
        "id": conv["id"],
        "project_id": conv["project_id"],
        "type": conv["type"],
        "name": conv["name"],
        "topic": conv["topic"],
        "is_main": bool(conv["is_main"]),
        "created_by": conv["created_by"],
        "created_at": conv["created_at"],
        "members": members,
    }


# ----------------------------------------------------------------- aliases

def resolve_alias(project_id: int, alias: str) -> tuple[int, str] | None:
    """Resolve an alias to (agent_id, alias); ADMIN_AGENT_ID for the admin.
    Returns None if the alias matches nobody in the project."""
    admin_alias = get_admin_alias()
    if admin_alias is not None and alias.lower() == admin_alias.lower():
        return (ADMIN_AGENT_ID, admin_alias)
    row = query_one(
        "SELECT id, alias FROM agents WHERE project_id = ? AND lower(alias) = lower(?)",
        (project_id, alias),
    )
    return (row["id"], row["alias"]) if row else None


# ---------------------------------------------------------------- messages

def reaction_summary(message_id: int) -> list[dict[str, Any]]:
    rows = query_all(
        "SELECT emoji, actor_alias FROM message_reactions WHERE message_id = ? ORDER BY id",
        (message_id,),
    )
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["emoji"], []).append(r["actor_alias"])
    return [
        {"emoji": emoji, "count": len(aliases), "by": aliases[:20]}
        for emoji, aliases in grouped.items()
    ]


def _conversation_label(conv: sqlite3.Row, viewer_is_member: bool) -> str:
    """Human label for a message's home. DM counterparts are never named to
    non-members — they just see it came from a direct message."""
    if conv["type"] == "channel":
        return f"#{conv['name']}"
    if not viewer_is_member:
        return "a direct message"
    parts = [
        m["alias"]
        for m in serialize_conversation(conv)["members"]
    ]
    return "DM: " + " ↔ ".join(parts) if parts else "a direct message"


def _forwarded_block(row: sqlite3.Row) -> dict[str, Any] | None:
    """Read-time resolution of the forwarded original: deletions tombstone,
    labels adapt to what exists now."""
    if row["forwarded_from_id"] is None:
        return None
    original = query_one("SELECT * FROM messages WHERE id = ?", (row["forwarded_from_id"],))
    if original is None:
        return {"missing": True}
    conv = query_one("SELECT * FROM conversations WHERE id = ?", (original["conversation_id"],))
    attachments = query_all(
        "SELECT id, filename, size, mime_type FROM attachments WHERE message_id = ?",
        (original["id"],),
    )
    return {
        "message_id": original["id"],
        "author": original["author_alias"],
        "role": original["author_type"],
        "body": original["body"] if not original["deleted"] else "",
        "deleted": bool(original["deleted"]),
        "created_at": original["created_at"],
        "conversation_id": original["conversation_id"],
        "conversation_type": conv["type"] if conv else None,
        "conversation_label": _conversation_label(conv, viewer_is_member=False)
        if conv is not None and conv["type"] == "dm"
        else (f"#{conv['name']}" if conv else "a deleted conversation"),
        "attachments": [
            dict(a) | {"url": f"/api/projects/{row['project_id']}/attachments/{a['id']}"}
            for a in attachments
        ],
    }


def _doc_card(row: sqlite3.Row) -> dict[str, Any] | None:
    if not row["doc_ref"]:
        return None
    doc = query_one(
        "SELECT slug, title, current_revision, updated_at FROM documents "
        "WHERE project_id = ? AND slug = ?",
        (row["project_id"], row["doc_ref"]),
    )
    if doc is None:
        return {"slug": row["doc_ref"], "missing": True}
    author = query_one(
        "SELECT author FROM document_revisions WHERE document_id = "
        "(SELECT id FROM documents WHERE project_id = ? AND slug = ?) AND revision = ?",
        (row["project_id"], row["doc_ref"], doc["current_revision"]),
    )
    return {
        "slug": doc["slug"],
        "title": doc["title"],
        "current_revision": doc["current_revision"],
        "updated_at": doc["updated_at"],
        "author": author["author"] if author else None,
    }


def author_avatar(author_type: str, author_agent_id: int | None) -> str:
    if author_type == "admin":
        row = query_one("SELECT avatar FROM admin_identity WHERE id = 1")
        return row["avatar"] if row else ""
    if author_type == "agent" and author_agent_id is not None:
        row = query_one("SELECT avatar FROM agents WHERE id = ?", (author_agent_id,))
        return row["avatar"] if row else ""
    return ""


def serialize_message(row: sqlite3.Row) -> dict[str, Any]:
    attachments = query_all(
        "SELECT id, filename, size, mime_type, uploader, created_at FROM attachments WHERE message_id = ?",
        (row["id"],),
    )
    reply_count = query_one(
        "SELECT COUNT(*) AS c FROM messages WHERE parent_id = ? AND deleted = 0", (row["id"],)
    )
    body = row["body"] if not row["deleted"] else ""
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "conversation_id": row["conversation_id"],
        "parent_id": row["parent_id"],
        "author": row["author_alias"],
        "role": row["author_type"],  # 'admin' | 'agent' | 'system'
        "type": row["type"],
        "body": body,
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "deleted": bool(row["deleted"]),
        "pinned": bool(row["pinned"]),
        "reply_count": reply_count["c"] if reply_count else 0,
        "avatar": author_avatar(row["author_type"], row["author_agent_id"]),
        "reactions": reaction_summary(row["id"]),
        "forwarded_from": _forwarded_block(row),
        "doc_card": _doc_card(row),
        "attachments": [dict(a) | {"url": f"/api/projects/{row['project_id']}/attachments/{a['id']}"}
                        for a in attachments],
        "ticket_refs": [
            r["number"] for r in query_all(
                "SELECT t.number FROM ticket_links l JOIN tickets t ON t.id = l.ticket_id "
                "WHERE l.source_type = 'message' AND l.source_id = ? ORDER BY t.number",
                (row["id"],),
            )
        ],
    }


def get_message(project_id: int, message_id: int) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM messages WHERE id = ? AND project_id = ?", (message_id, project_id)
    )
    if row is None:
        raise ApiError(404, "not_found", f"Message {message_id} not found.")
    return row


def mention_candidates(raw: str) -> list[str]:
    """A captured @mention may drag trailing sentence punctuation with it
    ("ping @alice." captures "alice."), but aliases may ALSO legitimately end
    in '.' or '-'. Try the longest form first, then progressively trim
    trailing punctuation so both "@alice." and a real "@qa-" resolve (#28)."""
    candidates = [raw]
    trimmed = raw
    while trimmed and trimmed[-1] in ".-":
        trimmed = trimmed[:-1]
        if trimmed:
            candidates.append(trimmed)
    return candidates


def _extract_mentions(project_id: int, body: str) -> set[int]:
    """Mention targets in body: agent ids plus ADMIN_AGENT_ID for the admin.
    @here expands to every non-revoked agent in the project."""
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
        for r in query_all("SELECT id FROM agents WHERE project_id = ? AND revoked = 0", (project_id,)):
            targets.add(r["id"])
    return targets


def record_ticket_links(
    conn: sqlite3.Connection, project_id: int, source_type: str, source_id: int, body: str
) -> list[int]:
    """Register #N cross-links found in body; returns ticket numbers linked.

    Links reflect the CURRENT body: the source's previous rows are dropped
    first, so editing "#12" to "#13" retires the stale backlink instead of
    accumulating both forever (issue #28 H10)."""
    conn.execute(
        "DELETE FROM ticket_links WHERE source_type = ? AND source_id = ? AND ticket_id IN "
        "(SELECT id FROM tickets WHERE project_id = ?)",
        (source_type, source_id, project_id),
    )
    numbers = sorted({int(n) for n in TICKET_REF_RE.findall(body)})
    linked: list[int] = []
    for number in numbers:
        ticket = conn.execute(
            "SELECT id FROM tickets WHERE project_id = ? AND number = ?", (project_id, number)
        ).fetchone()
        if ticket is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO ticket_links (ticket_id, source_type, source_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ticket["id"], source_type, source_id, utc_now()),
        )
        linked.append(number)
    return linked


def post_message(
    project_id: int,
    conversation_id: int,
    author_type: str,
    author_agent_id: int | None,
    author_alias: str,
    body: str,
    msg_type: str = "normal",
    parent_id: int | None = None,
    attachment_ids: list[int] | None = None,
    forwarded_from_id: int | None = None,
    doc_ref: str | None = None,
) -> dict[str, Any]:
    """Insert a message with mentions, ticket cross-links, attachment binding
    and events, in one transaction. Caller must have checked access (including
    forward-source access and doc existence) and must
    `await events.notify(project_id)` afterwards."""
    if not body.strip() and not attachment_ids and forwarded_from_id is None and not doc_ref:
        raise ApiError(422, "empty_message", "Message body must not be empty.")
    if msg_type not in ("normal", "decision", "system"):
        raise ApiError(422, "invalid_type", "Message type must be 'normal' or 'decision'.")
    now = utc_now()
    with transaction() as conn:
        if forwarded_from_id is not None:
            # Atomic re-check: the caller validated before the write lock, but
            # a concurrent delete may have landed since.
            original = conn.execute(
                "SELECT deleted FROM messages WHERE id = ? AND project_id = ?",
                (forwarded_from_id, project_id),
            ).fetchone()
            if original is None or original["deleted"]:
                raise ApiError(410, "gone", "The original message was deleted.")
        if parent_id is not None:
            parent = conn.execute(
                "SELECT id, conversation_id, parent_id FROM messages WHERE id = ? AND project_id = ?",
                (parent_id, project_id),
            ).fetchone()
            if parent is None or parent["conversation_id"] != conversation_id:
                raise ApiError(404, "not_found", "Thread parent message not found in this conversation.")
            if parent["parent_id"] is not None:
                parent_id = parent["parent_id"]  # keep threads one level deep
        cur = conn.execute(
            "INSERT INTO messages (project_id, conversation_id, parent_id, author_type, "
            "author_agent_id, author_alias, type, body, created_at, forwarded_from_id, doc_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, conversation_id, parent_id, author_type, author_agent_id,
             author_alias, msg_type, body, now, forwarded_from_id, doc_ref),
        )
        message_id = int(cur.lastrowid or 0)

        for att_id in attachment_ids or []:
            claimed = conn.execute(
                "UPDATE attachments SET message_id = ? "
                "WHERE id = ? AND project_id = ? AND message_id IS NULL "
                "AND comment_id IS NULL AND document_id IS NULL AND uploader = ?",
                (message_id, att_id, project_id, author_alias),
            )
            if claimed.rowcount == 0:
                raise ApiError(422, "bad_attachment",
                               f"Attachment {att_id} does not exist, is already attached, or is not yours.")

        # System messages narrate the board ("Ticket #12 status: ..."), so
        # linking them would fill every ticket's backlinks with its own
        # lifecycle chatter (issue #28). Only people-authored text links.
        if msg_type != "system":
            record_ticket_links(conn, project_id, "message", message_id, body)

        targets = _extract_mentions(project_id, body)
        # Self-mention suppression only: an agent doesn't get notified about
        # its own message, nor the admin about theirs. System messages have
        # no self — discarding ADMIN_AGENT_ID for them meant the escalation
        # notices could never reach the admin (issue #28).
        if author_type == "agent":
            targets.discard(author_agent_id)
        elif author_type == "admin":
            targets.discard(ADMIN_AGENT_ID)
        for target in targets:
            conn.execute(
                "INSERT INTO mentions (project_id, message_id, target_agent_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, message_id, target, now),
            )

        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        payload = serialize_message(row)
        append_event(conn, project_id, "message", payload, conversation_id=conversation_id)
        for target in targets:
            append_event(
                conn, project_id, "mention",
                {"message_id": message_id, "conversation_id": conversation_id,
                 "by": author_alias, "excerpt": body[:200]},
                target_agent_id=target,
            )
    return payload


def post_system_message(project_id: int, body: str) -> dict[str, Any] | None:
    """Post a system message to the project's main channel (if enabled in
    project settings). Caller must `await events.notify(project_id)` after."""
    project = get_project(project_id)
    if not project_settings(project).get("system_messages_enabled", True):
        return None
    main = get_main_channel(project_id)
    return post_message(
        project_id, main["id"], "system", None, "system", body, msg_type="system"
    )


# ------------------------------------------------------------------ agents

def serialize_agent(row: sqlite3.Row) -> dict[str, Any]:
    online = False
    if row["last_seen"]:
        try:
            seen = datetime.fromisoformat(row["last_seen"])
            online = datetime.now(UTC) - seen < timedelta(minutes=5)
        except ValueError:
            online = False
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "alias": row["alias"],
        "role": row["role"],
        "status": row["status"],
        "avatar": row["avatar"],
        "status_text": row["status_text"],
        "status_emoji": row["status_emoji"],
        "online": online,
        "revoked": bool(row["revoked"]),
        "created_at": row["created_at"],
        "last_seen": row["last_seen"],
    }


def get_agent(project_id: int, agent_id: int) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM agents WHERE id = ? AND project_id = ?", (agent_id, project_id)
    )
    if row is None:
        raise ApiError(404, "not_found", f"Agent {agent_id} not found in project {project_id}.")
    return row


# -------------------------------------------------------------- pagination

def pagination_window(limit: int | None, max_limit: int = 200) -> int:
    if limit is None:
        return 50
    return max(1, min(limit, max_limit))


def touch_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute(
        "UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now(), project_id)
    )


def fts_quote(query: str) -> str:
    """Make arbitrary user input safe for FTS5 MATCH by quoting each token."""
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    if not tokens:
        # An empty MATCH expression is an FTS5 syntax error (500); a query of
        # pure whitespace should behave like "matches nothing" instead.
        raise ApiError(422, "empty_query", "Search query must contain at least one term.")
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def update_documents_fts(conn: sqlite3.Connection, document_id: int, title: str, body: str) -> None:
    conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (document_id,))
    conn.execute(
        "INSERT INTO documents_fts (rowid, title, body) VALUES (?, ?, ?)",
        (document_id, title, body),
    )
