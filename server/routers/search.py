"""Search & observability: FTS5 full-text search, activity feed, lightweight
metrics, and the admin export tarball."""
from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from .. import audit as audit_mod
from ..auth import Actor, AdminDep, check_project_access, get_actor
from ..config import get_settings
from ..db import query_all, query_one, utc_now
from ..errors import ApiError, ok
from ..services import fts_quote, get_project, serialize_agent, serialize_conversation, serialize_message

router = APIRouter(tags=["search"])

SEARCH_TYPES = ("messages", "tickets", "comments", "documents")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")

# Visibility clause for agents; mirrors events.visible_events_since (which is
# forward-only and therefore unusable for newest-first pagination).
_AGENT_EVENT_VISIBILITY = (
    "((conversation_id IS NULL AND target_agent_id IS NULL) "
    "OR target_agent_id = :aid "
    "OR conversation_id IN (SELECT conversation_id FROM conversation_members WHERE agent_id = :aid))"
)


def _validate_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not _ISO_DATE_RE.match(value):
        raise ApiError(422, "invalid_date", f"'{name}' must be an ISO 8601 date or datetime.")
    return value


@router.get("/projects/{project_id}/search")
async def search(
    project_id: int,
    request: Request,
    q: str = "",
    type: list[str] = Query(default=[]),
    channel_id: int | None = None,
    author: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    get_project(project_id)
    offset = max(0, min(offset, 1000))  # capped: deep FTS offsets are wasteful

    # Slack-style typed operators (issue #15 S1): `from:alias` and
    # `in:#channel` inside q map onto the author / channel_id filters instead
    # of matching literally (which silently returned nothing).
    tokens = []
    for token in q.split():
        low = token.lower()
        if low.startswith("from:") and len(token) > 5:
            if author is None:
                author = token[5:]
            continue
        if low.startswith("in:") and len(token) > 3:
            name = token[3:].lstrip("#")
            chan = query_one(
                "SELECT id FROM conversations WHERE project_id = ? AND type = 'channel' "
                "AND lower(name) = lower(?)",
                (project_id, name),
            )
            if chan is None:
                raise ApiError(422, "unknown_channel", f"No channel named '#{name}' in this project.")
            if channel_id is None:
                channel_id = chan["id"]
            continue
        tokens.append(token)
    q = " ".join(tokens)

    if not q.strip():
        raise ApiError(
            422, "empty_query",
            "Search query 'q' must contain search terms (operators like from:/in: alone are not enough).",
        )
    types = type or list(SEARCH_TYPES)
    for t in types:
        if t not in SEARCH_TYPES:
            raise ApiError(422, "invalid_type", f"Unknown search type '{t}'.")
    if channel_id is not None:
        # A channel scope only means something for messages; silently
        # returning channel-agnostic ticket/doc hits misleads (issue #15
        # review). Explicit non-message types + channel filter is an error.
        if type and any(t != "messages" for t in type):
            raise ApiError(
                422, "invalid_filter",
                "channel_id / in:#channel only applies to type=messages.",
            )
        types = ["messages"]
    after = _validate_date(after, "after")
    before = _validate_date(before, "before")
    window = max(1, min(limit or 20, 100))
    fetch_n = offset + window + 1
    match = fts_quote(q)

    ranked: list[tuple[float, dict[str, Any]]] = []

    if "messages" in types:
        sql = (
            "SELECT m.id, m.conversation_id, m.author_alias, m.created_at, "
            "snippet(messages_fts, 0, '<mark>', '</mark>', '…', 12) AS snip, "
            "bm25(messages_fts) AS rank "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH :match AND m.project_id = :pid AND m.deleted = 0"
        )
        params: dict[str, Any] = {"match": match, "pid": project_id, "limit": fetch_n}
        if not actor.is_admin:
            sql += " AND m.conversation_id IN (SELECT conversation_id FROM conversation_members WHERE agent_id = :aid)"
            params["aid"] = actor.agent_id
        if channel_id is not None:
            sql += " AND m.conversation_id = :cid"
            params["cid"] = channel_id
        if author is not None:
            sql += " AND lower(m.author_alias) = lower(:author)"
            params["author"] = author
        if after is not None:
            sql += " AND m.created_at >= :after"
            params["after"] = after
        if before is not None:
            sql += " AND m.created_at <= :before"
            params["before"] = before
        sql += " ORDER BY rank LIMIT :limit"
        for r in query_all(sql, params):
            ranked.append((r["rank"], {
                "type": "message", "id": r["id"], "conversation_id": r["conversation_id"],
                "author": r["author_alias"], "created_at": r["created_at"], "snippet": r["snip"],
            }))

    if "tickets" in types:
        sql = (
            "SELECT t.id, t.number, t.status, t.reporter, t.created_at, "
            "snippet(tickets_fts, 0, '<mark>', '</mark>', '…', 12) AS title_snip, "
            "snippet(tickets_fts, 1, '<mark>', '</mark>', '…', 12) AS desc_snip, "
            "bm25(tickets_fts) AS rank "
            "FROM tickets_fts JOIN tickets t ON t.id = tickets_fts.rowid "
            "WHERE tickets_fts MATCH :match AND t.project_id = :pid"
        )
        params = {"match": match, "pid": project_id, "limit": fetch_n}
        if author is not None:
            sql += " AND lower(t.reporter) = lower(:author)"
            params["author"] = author
        if after is not None:
            sql += " AND t.created_at >= :after"
            params["after"] = after
        if before is not None:
            sql += " AND t.created_at <= :before"
            params["before"] = before
        sql += " ORDER BY rank LIMIT :limit"
        for r in query_all(sql, params):
            ranked.append((r["rank"], {
                "type": "ticket", "id": r["id"], "number": r["number"], "status": r["status"],
                "author": r["reporter"], "created_at": r["created_at"],
                "title_snippet": r["title_snip"], "snippet": r["desc_snip"],
            }))

    if "comments" in types:
        sql = (
            "SELECT c.id, c.author_alias, c.created_at, t.number, "
            "snippet(ticket_comments_fts, 0, '<mark>', '</mark>', '…', 12) AS snip, "
            "bm25(ticket_comments_fts) AS rank "
            "FROM ticket_comments_fts "
            "JOIN ticket_comments c ON c.id = ticket_comments_fts.rowid "
            "JOIN tickets t ON t.id = c.ticket_id "
            "WHERE ticket_comments_fts MATCH :match AND t.project_id = :pid"
        )
        params = {"match": match, "pid": project_id, "limit": fetch_n}
        if author is not None:
            sql += " AND lower(c.author_alias) = lower(:author)"
            params["author"] = author
        if after is not None:
            sql += " AND c.created_at >= :after"
            params["after"] = after
        if before is not None:
            sql += " AND c.created_at <= :before"
            params["before"] = before
        sql += " ORDER BY rank LIMIT :limit"
        for r in query_all(sql, params):
            ranked.append((r["rank"], {
                "type": "comment", "id": r["id"], "ticket_number": r["number"],
                "author": r["author_alias"], "created_at": r["created_at"], "snippet": r["snip"],
            }))

    if "documents" in types:
        sql = (
            "SELECT d.id, d.slug, d.updated_at, r.author, "
            "snippet(documents_fts, 0, '<mark>', '</mark>', '…', 12) AS title_snip, "
            "snippet(documents_fts, 1, '<mark>', '</mark>', '…', 12) AS body_snip, "
            "bm25(documents_fts) AS rank "
            "FROM documents_fts "
            "JOIN documents d ON d.id = documents_fts.rowid "
            "JOIN document_revisions r ON r.document_id = d.id AND r.revision = d.current_revision "
            "WHERE documents_fts MATCH :match AND d.project_id = :pid"
        )
        params = {"match": match, "pid": project_id, "limit": fetch_n}
        if author is not None:
            sql += " AND lower(r.author) = lower(:author)"
            params["author"] = author
        if after is not None:
            sql += " AND d.updated_at >= :after"
            params["after"] = after
        if before is not None:
            sql += " AND d.updated_at <= :before"
            params["before"] = before
        sql += " ORDER BY rank LIMIT :limit"
        for r in query_all(sql, params):
            ranked.append((r["rank"], {
                "type": "document", "id": r["id"], "slug": r["slug"],
                "author": r["author"], "created_at": r["updated_at"],
                "title_snippet": r["title_snip"], "snippet": r["body_snip"],
            }))

    ranked.sort(key=lambda pair: pair[0])  # bm25: lower is better
    return ok({
        "items": [item for _, item in ranked[offset:offset + window]],
        "has_more": len(ranked) > offset + window,
        "offset": offset,
    })


@router.get("/projects/{project_id}/activity")
async def activity_feed(
    project_id: int,
    request: Request,
    limit: int | None = None,
    before_id: int | None = None,
) -> dict:
    """Chronological project firehose (newest first), read from the events
    table with the same visibility rules as the SSE stream."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    window = max(1, min(limit or 50, 200))
    params: dict[str, Any] = {"pid": project_id, "limit": window + 1}
    sql = "SELECT * FROM events WHERE project_id = :pid"
    if before_id is not None:
        sql += " AND id < :before"
        params["before"] = before_id
    if not actor.is_admin:
        sql += f" AND {_AGENT_EVENT_VISIBILITY}"
        params["aid"] = actor.agent_id
    sql += " ORDER BY id DESC LIMIT :limit"
    rows = query_all(sql, params)
    has_more = len(rows) > window
    items = [
        {
            "id": r["id"],
            "type": r["type"],
            "conversation_id": r["conversation_id"],
            "target_agent_id": r["target_agent_id"],
            "payload": json.loads(r["payload"]),
            "created_at": r["created_at"],
        }
        for r in rows[:window]
    ]
    return ok({"items": items, "has_more": has_more})


@router.get("/projects/{project_id}/metrics")
async def metrics(project_id: int, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    messages_per_agent = {
        r["author_alias"]: r["c"]
        for r in query_all(
            "SELECT author_alias, COUNT(*) AS c FROM messages "
            "WHERE project_id = ? AND deleted = 0 AND author_type != 'system' "
            "GROUP BY author_alias ORDER BY c DESC",
            (project_id,),
        )
    }
    opened = {
        r["day"]: r["c"]
        for r in query_all(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c FROM tickets "
            "WHERE project_id = ? GROUP BY day ORDER BY day",
            (project_id,),
        )
    }
    # Approximation: a ticket counts as closed on the day of its last update
    # while in a terminal status (full status history is not tracked).
    closed = {
        r["day"]: r["c"]
        for r in query_all(
            "SELECT substr(updated_at, 1, 10) AS day, COUNT(*) AS c FROM tickets "
            "WHERE project_id = ? AND status IN ('done', 'wontfix') GROUP BY day ORDER BY day",
            (project_id,),
        )
    }
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    active = query_all(
        "SELECT * FROM agents WHERE project_id = ? AND revoked = 0 AND last_seen >= ? "
        "ORDER BY last_seen DESC",
        (project_id, threshold),
    )
    totals_row = query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM messages WHERE project_id = :pid AND deleted = 0) AS messages,
          (SELECT COUNT(*) FROM tickets WHERE project_id = :pid) AS tickets,
          (SELECT COUNT(*) FROM tickets WHERE project_id = :pid
              AND status NOT IN ('done', 'wontfix')) AS open_tickets,
          (SELECT COUNT(*) FROM documents WHERE project_id = :pid) AS documents,
          (SELECT COUNT(*) FROM agents WHERE project_id = :pid AND revoked = 0) AS agents
        """,
        {"pid": project_id},
    )
    return ok({
        "messages_per_agent": messages_per_agent,
        "tickets_opened_by_day": opened,
        "tickets_closed_by_day": closed,
        "active_agents": [serialize_agent(r) for r in active],
        "totals": dict(totals_row) if totals_row else {},
    })


def _tar_add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _tar_add_json(tar: tarfile.TarFile, name: str, payload: Any) -> None:
    _tar_add_bytes(tar, name, json.dumps(payload, indent=2, ensure_ascii=False).encode())


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value) or "unnamed"


@router.post("/projects/{project_id}/export")
async def export_project(project_id: int, _admin: Actor = AdminDep) -> FileResponse:
    """Build a downloadable tarball of the whole project: conversations with
    messages/threads, tickets with comments, documents (current + revisions),
    decisions, agents, and attachment files. The tarball also stays on disk
    under the data directory (path in X-Export-Path)."""
    project = get_project(project_id)
    settings = get_settings()
    exports_dir = settings.data_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"nomos-export-{project_id}-{stamp}.tar.gz"
    tar_path = exports_dir / filename

    conversations = query_all(
        "SELECT * FROM conversations WHERE project_id = ? ORDER BY id", (project_id,)
    )
    agents = query_all("SELECT * FROM agents WHERE project_id = ? ORDER BY id", (project_id,))
    tickets = query_all("SELECT * FROM tickets WHERE project_id = ? ORDER BY number", (project_id,))
    documents = query_all("SELECT * FROM documents WHERE project_id = ? ORDER BY id", (project_id,))
    attachments = query_all(
        "SELECT * FROM attachments WHERE project_id = ? ORDER BY id", (project_id,)
    )
    decisions = query_all(
        "SELECT * FROM messages WHERE project_id = ? AND type = 'decision' AND deleted = 0 ORDER BY id",
        (project_id,),
    )

    with tarfile.open(tar_path, "w:gz") as tar:
        _tar_add_json(tar, "manifest.json", {
            "project": {
                "id": project["id"], "name": project["name"],
                "description": project["description"], "created_at": project["created_at"],
            },
            "generated_at": utc_now(),
            "counts": {
                "conversations": len(conversations), "agents": len(agents),
                "tickets": len(tickets), "documents": len(documents),
                "attachments": len(attachments), "decisions": len(decisions),
            },
        })
        _tar_add_json(tar, "agents.json", [serialize_agent(a) for a in agents])
        _tar_add_json(tar, "decisions.json", [serialize_message(m) for m in decisions])
        # Audit trail with chain fields — re-verifiable offline (issue #17).
        audit_rows = query_all(
            "SELECT * FROM audit_log WHERE project_id = ? ORDER BY id", (project_id,)
        )
        audit_jsonl = "".join(
            json.dumps(audit_mod.serialize_row(r), sort_keys=True) + "\n" for r in audit_rows
        )
        _tar_add_bytes(tar, "audit.jsonl", audit_jsonl.encode())

        for conv in conversations:
            messages = query_all(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv["id"],)
            )
            name = _safe_name(conv["name"] or "dm")
            _tar_add_json(
                tar, f"conversations/{conv['id']}-{conv['type']}-{name}.json",
                {"conversation": serialize_conversation(conv),
                 "messages": [serialize_message(m) for m in messages]},
            )

        ticket_dump = []
        for t in tickets:
            comments = query_all(
                "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id", (t["id"],)
            )
            entry = dict(t)
            entry["labels"] = json.loads(t["labels"] or "[]")
            entry["comments"] = [dict(c) for c in comments]
            ticket_dump.append(entry)
        _tar_add_json(tar, "tickets.json", ticket_dump)

        for d in documents:
            revisions = query_all(
                "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision",
                (d["id"],),
            )
            for rev in revisions:
                body = rev["body"].encode()
                if rev["revision"] == d["current_revision"]:
                    _tar_add_bytes(tar, f"documents/{d['slug']}.md", body)
                _tar_add_bytes(tar, f"documents/revisions/{d['slug']}/r{rev['revision']}.md", body)

        for att in attachments:
            src = Path(settings.attachments_dir) / str(project_id) / att["stored_name"]
            if src.is_file():
                tar.add(src, arcname=f"attachments/{att['id']}-{_safe_name(att['filename'])}")

    return FileResponse(
        tar_path,
        media_type="application/gzip",
        filename=filename,
        headers={"X-Export-Path": str(tar_path)},
    )
