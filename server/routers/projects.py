"""Project lifecycle: list/create/get (open), admin-only rename, settings,
archive, and transactional cascade delete."""
from __future__ import annotations

import json
import shutil

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..auth import Actor, AdminDep, get_actor
from ..config import get_settings
from ..db import query_all, query_one, transaction, utc_now
from ..errors import ApiError, ok
from .. import audit
from ..logging_setup import kv, setup_logging
from ..services import (
    DEFAULT_TICKET_STATUSES,
    create_project,
    get_project,
    serialize_project,
)

router = APIRouter(tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    ticket_statuses: list[str] | None = None
    system_messages_enabled: bool | None = None


@router.get("/projects")
async def list_projects(include_archived: bool = False) -> dict:
    """Open endpoint: prospective agents must be able to discover projects
    before they hold a key."""
    where = "" if include_archived else "WHERE archived = 0"
    rows = query_all(f"SELECT * FROM projects {where} ORDER BY id")
    return ok({"items": [serialize_project(r) for r in rows]})


@router.post("/projects", status_code=201)
async def create_project_endpoint(body: ProjectCreate, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    if not actor.is_admin and not get_settings().agents_can_create_projects:
        raise ApiError(403, "forbidden", "Agents may not create projects on this server.")
    project = create_project(body.name, body.description, actor.alias)
    setup_logging().info("project created %s", kv(project=project["id"], by=actor.alias))
    return ok(project)


@router.get("/projects/{project_id}")
async def get_project_endpoint(project_id: int) -> dict:
    project = get_project(project_id)
    counts = query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM agents WHERE project_id = :pid AND revoked = 0) AS agents,
          (SELECT COUNT(*) FROM tickets WHERE project_id = :pid) AS tickets,
          (SELECT COUNT(*) FROM conversations WHERE project_id = :pid AND type = 'channel') AS channels,
          (SELECT COUNT(*) FROM documents WHERE project_id = :pid) AS documents
        """,
        {"pid": project_id},
    )
    data = serialize_project(project)
    data["counts"] = dict(counts) if counts else {}
    return ok(data)


@router.patch("/projects/{project_id}")
async def update_project(project_id: int, body: ProjectUpdate, _admin: Actor = AdminDep) -> dict:
    project = get_project(project_id)
    settings = json.loads(project["settings"] or "{}")
    if body.ticket_statuses is not None:
        statuses = [s.strip() for s in body.ticket_statuses if s.strip()]
        if not statuses:
            raise ApiError(422, "invalid_statuses", "At least one ticket status is required.")
        settings["ticket_statuses"] = statuses
    if body.system_messages_enabled is not None:
        settings["system_messages_enabled"] = body.system_messages_enabled
    new_name = body.name.strip() if body.name is not None else None
    if new_name is not None and not new_name:
        raise ApiError(422, "invalid_name", "Project name must not be empty.")
    with transaction() as conn:
        if new_name is not None:
            clash = conn.execute(
                "SELECT id FROM projects WHERE name = ? AND id != ?", (new_name, project_id)
            ).fetchone()
            if clash:
                raise ApiError(409, "duplicate_name", f"A project named '{new_name}' already exists.")
            conn.execute("UPDATE projects SET name = ? WHERE id = ?", (new_name, project_id))
        if body.description is not None:
            conn.execute(
                "UPDATE projects SET description = ? WHERE id = ?", (body.description, project_id)
            )
        conn.execute(
            "UPDATE projects SET settings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(settings), utc_now(), project_id),
        )
    return ok(serialize_project(get_project(project_id)))


@router.post("/projects/{project_id}/archive")
async def archive_project(project_id: int, _admin: Actor = AdminDep) -> dict:
    get_project(project_id)
    with transaction() as conn:
        conn.execute(
            "UPDATE projects SET archived = 1, updated_at = ? WHERE id = ?",
            (utc_now(), project_id),
        )
        audit.platform_record(conn, project_id, "other", "Project archived (read-only)", actor="admin")
    return ok(serialize_project(get_project(project_id)))


@router.post("/projects/{project_id}/unarchive")
async def unarchive_project(project_id: int, _admin: Actor = AdminDep) -> dict:
    get_project(project_id)
    with transaction() as conn:
        conn.execute(
            "UPDATE projects SET archived = 0, updated_at = ? WHERE id = ?",
            (utc_now(), project_id),
        )
        audit.platform_record(conn, project_id, "other", "Project unarchived", actor="admin")
    return ok(serialize_project(get_project(project_id)))


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int, _admin: Actor = AdminDep) -> dict:
    """Full cascade delete. FK ON DELETE CASCADE removes every dependent row
    in one transaction; FTS indexes are rebuilt afterwards inside the same
    transaction; attachment files are removed from disk after commit."""
    project = get_project(project_id)
    # Destruction tombstone survives the cascade: an append-only JSONL file in
    # the data dir (the audit rows themselves die with the project — the
    # "documented destruction event", issue #17 review).
    tombstone = {
        "event": "project_deleted", "project_id": project_id,
        "name": project["name"], "by": "admin", "deleted_at": utc_now(),
    }
    with transaction() as conn:
        doc_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE project_id = ?", (project_id,)
        ).fetchall()]
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        for doc_id in doc_ids:
            conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        for fts in ("messages_fts", "tickets_fts", "ticket_comments_fts"):
            conn.execute(f"INSERT INTO {fts} ({fts}) VALUES ('rebuild')")
    attachments_dir = get_settings().attachments_dir / str(project_id)
    attachments_removed = True
    if attachments_dir.exists():
        try:
            shutil.rmtree(attachments_dir)
        except OSError as exc:
            attachments_removed = False
            setup_logging().warning(
                "attachment cleanup failed %s", kv(project=project_id, error=exc)
            )
    try:
        with open(get_settings().data_dir / "deletions.log", "a") as fh:
            fh.write(json.dumps(tombstone) + "\n")
    except OSError as exc:
        setup_logging().warning("deletion tombstone write failed %s", kv(error=exc))
    setup_logging().info("project deleted %s", kv(project=project_id))
    return ok({"deleted": True, "project_id": project_id, "attachments_removed": attachments_removed})


@router.get("/projects/{project_id}/statuses")
async def ticket_statuses(project_id: int) -> dict:
    """The configured ticket status flow for a project (open endpoint —
    read-only config agents need before creating tickets)."""
    project = get_project(project_id)
    settings = json.loads(project["settings"] or "{}")
    return ok({"statuses": settings.get("ticket_statuses", DEFAULT_TICKET_STATUSES)})
