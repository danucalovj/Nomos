"""Document repository: versioned markdown documents with append-only
revisions and optimistic concurrency (stale writes get 409 + current state)."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..auth import Actor, check_project_access, get_actor
from ..db import query_all, query_one, transaction, utc_now
from ..errors import ApiError, ok
from .. import audit
from ..events import append_event, notify
from ..services import (
    get_project,
    record_ticket_links,
    require_not_archived,
    update_documents_fts,
)

router = APIRouter(tags=["documents"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class DocumentCreate(BaseModel):
    slug: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=1_000_000)


class DocumentWrite(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(max_length=1_000_000)
    base_revision: int = Field(ge=0)


def _slug_from_title(conn: sqlite3.Connection, project_id: int, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "doc"
    slug = base
    n = 2
    while conn.execute(
        "SELECT 1 FROM documents WHERE project_id = ? AND slug = ?", (project_id, slug)
    ).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _get_document(project_id: int, slug: str) -> sqlite3.Row:
    row = query_one(
        "SELECT * FROM documents WHERE project_id = ? AND slug = ?", (project_id, slug)
    )
    if row is None:
        raise ApiError(404, "not_found", f"Document '{slug}' not found in project {project_id}.")
    return row


def _serialize_doc(doc: sqlite3.Row, revision: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": doc["id"],
        "project_id": doc["project_id"],
        "slug": doc["slug"],
        "title": doc["title"],
        "current_revision": doc["current_revision"],
        "revision": revision["revision"],
        "body": revision["body"],
        "author": revision["author"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


@router.post("/projects/{project_id}/documents", status_code=201)
async def create_document(project_id: int, body: DocumentCreate, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    title = body.title.strip()
    if not title:
        raise ApiError(422, "invalid_title", "Document title must not be empty.")
    if body.slug is not None and not SLUG_RE.match(body.slug):
        raise ApiError(
            422, "invalid_slug",
            "Slug must be 1-64 chars: lowercase letters, digits, '-', starting alphanumeric.",
        )
    now = utc_now()
    with transaction() as conn:
        slug = body.slug or _slug_from_title(conn, project_id, title)
        try:
            cur = conn.execute(
                "INSERT INTO documents (project_id, slug, title, current_revision, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (project_id, slug, title, now, now),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "duplicate_slug", f"Document '{slug}' already exists in this project.")
        doc_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO document_revisions (document_id, revision, title, body, author, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (doc_id, title, body.body, actor.alias, now),
        )
        update_documents_fts(conn, doc_id, title, body.body)
        record_ticket_links(conn, project_id, "document", doc_id, body.body)
        audit.platform_record(
            conn, project_id, "other",
            f"Document '{slug}' created (revision 1) by {actor.alias}",
            target=slug, actor=actor.alias,
        )
        append_event(
            conn, project_id, "document_created",
            {"id": doc_id, "slug": slug, "title": title, "revision": 1, "author": actor.alias},
        )
    await notify(project_id)
    doc = _get_document(project_id, slug)
    revision = query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? AND revision = 1", (doc_id,)
    )
    assert revision is not None
    return ok(_serialize_doc(doc, revision))


@router.get("/projects/{project_id}/documents")
async def list_documents(
    project_id: int,
    request: Request,
    limit: int | None = None,
    before_id: int | None = None,
    author: str | None = None,
) -> dict:
    """`author` filters by the CREATING author (revision 1), which is also
    included in every list item (issue #18 drill-downs)."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    window = max(1, min(limit or 50, 200))
    params: list[Any] = [project_id]
    where = "WHERE d.project_id = ?"
    if before_id is not None:
        where += " AND d.id < ?"
        params.append(before_id)
    if author is not None:
        where += " AND r1.author = ? COLLATE NOCASE"
        params.append(author)
    rows = query_all(
        f"""
        SELECT d.*, r1.author AS created_by FROM documents d
        JOIN document_revisions r1 ON r1.document_id = d.id AND r1.revision = 1
        {where} ORDER BY d.id DESC LIMIT ?
        """,
        (*params, window + 1),
    )
    has_more = len(rows) > window
    items = [
        {
            "id": r["id"],
            "slug": r["slug"],
            "title": r["title"],
            "current_revision": r["current_revision"],
            "author": r["created_by"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows[:window]
    ]
    return ok({"items": items, "has_more": has_more})


@router.get("/projects/{project_id}/documents/{slug}")
async def read_document(
    project_id: int, slug: str, request: Request, revision: int | None = None
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    doc = _get_document(project_id, slug)
    rev_number = revision if revision is not None else doc["current_revision"]
    rev = query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? AND revision = ?",
        (doc["id"], rev_number),
    )
    if rev is None:
        raise ApiError(404, "not_found", f"Revision {rev_number} of '{slug}' does not exist.")
    return ok(_serialize_doc(doc, rev))


@router.put("/projects/{project_id}/documents/{slug}")
async def write_document(
    project_id: int, slug: str, body: DocumentWrite, request: Request
) -> dict:
    """Optimistic concurrency: `base_revision` must equal the document's
    current revision, otherwise 409 with the current revision and body so the
    caller can merge and retry."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    project = get_project(project_id)
    require_not_archived(project)
    now = utc_now()
    with transaction() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE project_id = ? AND slug = ?", (project_id, slug)
        ).fetchone()
        if doc is None:
            raise ApiError(404, "not_found", f"Document '{slug}' not found in project {project_id}.")
        current = doc["current_revision"]
        if body.base_revision != current:
            current_row = conn.execute(
                "SELECT body FROM document_revisions WHERE document_id = ? AND revision = ?",
                (doc["id"], current),
            ).fetchone()
            # base_body enables a true 3-way merge (base / theirs / yours)
            # without a second round trip (dogfood finding, issue #15 S2).
            base_row = conn.execute(
                "SELECT body FROM document_revisions WHERE document_id = ? AND revision = ?",
                (doc["id"], body.base_revision),
            ).fetchone()
            raise ApiError(
                409, "revision_conflict",
                f"Document '{slug}' is at revision {current}; you based your write on {body.base_revision}.",
                {
                    "current_revision": current,
                    "current_body": current_row["body"] if current_row else "",
                    "base_body": base_row["body"] if base_row else None,
                },
            )
        new_revision = current + 1
        title = (body.title.strip() if body.title else None) or doc["title"]
        conn.execute(
            "INSERT INTO document_revisions (document_id, revision, title, body, author, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc["id"], new_revision, title, body.body, actor.alias, now),
        )
        conn.execute(
            "UPDATE documents SET current_revision = ?, title = ?, updated_at = ? WHERE id = ?",
            (new_revision, title, now, doc["id"]),
        )
        update_documents_fts(conn, doc["id"], title, body.body)
        record_ticket_links(conn, project_id, "document", doc["id"], body.body)
        audit.platform_record(
            conn, project_id, "other",
            f"Document '{slug}' revised to r{new_revision} by {actor.alias}",
            target=slug, actor=actor.alias,
        )
        append_event(
            conn, project_id, "document_updated",
            {"id": doc["id"], "slug": slug, "title": title, "revision": new_revision,
             "author": actor.alias},
        )
    await notify(project_id)
    doc_row = _get_document(project_id, slug)
    rev = query_one(
        "SELECT * FROM document_revisions WHERE document_id = ? AND revision = ?",
        (doc_row["id"], new_revision),
    )
    assert rev is not None
    return ok(_serialize_doc(doc_row, rev))


@router.get("/projects/{project_id}/documents/{slug}/revisions")
async def list_revisions(
    project_id: int,
    slug: str,
    request: Request,
    limit: int | None = None,
    before_id: int | None = None,
) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    doc = _get_document(project_id, slug)
    window = max(1, min(limit or 50, 200))
    params: list[Any] = [doc["id"]]
    where = "WHERE document_id = ?"
    if before_id is not None:
        where += " AND id < ?"
        params.append(before_id)
    rows = query_all(
        f"SELECT id, revision, title, author, created_at FROM document_revisions "
        f"{where} ORDER BY revision DESC LIMIT ?",
        (*params, window + 1),
    )
    has_more = len(rows) > window
    return ok({
        "items": [dict(r) for r in rows[:window]],
        "has_more": has_more,
        "current_revision": doc["current_revision"],
    })
