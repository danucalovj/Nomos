"""Project lifecycle: list/create/get (open), admin-only rename, settings,
archive, working directory, and transactional cascade delete."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import audit, fsmonitor
from ..auth import Actor, AdminDep, check_project_access, get_actor
from ..config import get_settings
from ..db import query_all, query_one, transaction, utc_now
from ..errors import ApiError, ok
from ..logging_setup import kv, setup_logging
from ..services import (
    DEFAULT_TICKET_STATUSES,
    create_project,
    get_project,
    post_system_message,
    require_not_archived,
    serialize_project,
)
from .audit import _forbidden_watch_path

# The onboarding manual shipped at the repo root; copied into a project's
# working directory when one is set so agents find it where they work.
AGENTS_MD_PATH = Path(__file__).resolve().parents[2] / "AGENTS.md"

router = APIRouter(tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    working_dir: str = Field(default="", max_length=1024)
    overwrite_agents_md: bool = False


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    ticket_statuses: list[str] | None = None
    system_messages_enabled: bool | None = None
    working_dir: str | None = Field(default=None, max_length=1024)
    overwrite_agents_md: bool = False


class WorkingDirSet(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    overwrite_agents_md: bool = False


def _validate_working_dir(raw_path: str) -> Path:
    """Path rules only, no filesystem writes. Absolute, non-system."""
    if not raw_path.strip().startswith(("/", "~")):
        raise ApiError(422, "invalid_path", "Working directory must be an absolute path.")
    resolved = Path(raw_path).expanduser().resolve()
    if _forbidden_watch_path(resolved):
        raise ApiError(
            422, "forbidden_path",
            f"'{resolved}' is a system path and cannot be a working directory.",
        )
    return resolved


def _apply_working_dir(
    project_id: int, raw_path: str, actor_alias: str, overwrite: bool = False
) -> str:
    """Validate the directory, create it if needed, copy AGENTS.md into it,
    and persist it on the project. Returns the resolved path."""
    resolved = _validate_working_dir(raw_path)
    dest = resolved / "AGENTS.md"
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        # Never write through a symlink someone planted at the destination:
        # copy to a fresh temp file, then rename over (replaces a symlink
        # itself rather than following it).
        if dest.is_symlink():
            raise ApiError(
                422, "invalid_path",
                f"'{dest}' is a symlink; refusing to write through it.",
            )
        # Never silently destroy somebody's existing AGENTS.md (issue #28 H6).
        # The common target is a real repo that already carries one. Identical
        # content is fine (idempotent re-set); different content requires the
        # caller to say overwrite_agents_md explicitly.
        ours = AGENTS_MD_PATH.read_bytes()
        if dest.exists() and not overwrite and dest.read_bytes() != ours:
            raise ApiError(
                409, "agents_md_exists",
                f"'{dest}' already exists with different content. Pass "
                "overwrite_agents_md=true to replace it, or move it aside first.",
            )
        # Unguessable, exclusively-created temp file (mkstemp uses O_EXCL and
        # never follows a pre-planted symlink), then atomic rename over dest.
        fd, tmp_name = tempfile.mkstemp(prefix=".AGENTS.md.", dir=resolved)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(ours)
            os.replace(tmp_name, dest)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    except OSError as exc:
        raise ApiError(422, "invalid_path", f"Cannot prepare '{resolved}': {exc}") from exc
    with transaction() as conn:
        conn.execute(
            "UPDATE projects SET working_dir = ?, updated_at = ? WHERE id = ?",
            (str(resolved), utc_now(), project_id),
        )
        audit.platform_record(
            conn, project_id, "other",
            f"Working directory set to {resolved} (by {actor_alias}); AGENTS.md copied there",
            target=str(resolved), actor=actor_alias,
        )
    post_system_message(
        project_id, f"Working directory set to `{resolved}` (by {actor_alias})"
    )
    return str(resolved)


async def _scoped_serialize(request: Request, row) -> dict:
    """Open discovery is by design (the join flow needs it), but working_dir
    is an absolute host path: only the admin and the project's own agents get
    to see it (issue #28). Everyone else gets the row with it blanked."""
    try:
        actor: Actor | None = await get_actor(request)
    except ApiError:
        actor = None  # pre-setup or bad key: discovery still works, path hidden
    data = serialize_project(row)
    if actor is None or (not actor.is_admin and actor.project_id != row["id"]):
        data["working_dir"] = ""
    return data


@router.get("/projects")
async def list_projects(request: Request, include_archived: bool = False) -> dict:
    """Open endpoint: prospective agents must be able to discover projects
    before they hold a key."""
    where = "" if include_archived else "WHERE archived = 0"
    rows = query_all(f"SELECT * FROM projects {where} ORDER BY id")
    return ok({"items": [await _scoped_serialize(request, r) for r in rows]})


@router.post("/projects", status_code=201)
async def create_project_endpoint(body: ProjectCreate, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    if not actor.is_admin and not get_settings().agents_can_create_projects:
        raise ApiError(403, "forbidden", "Agents may not create projects on this server.")
    if body.working_dir.strip():
        _validate_working_dir(body.working_dir.strip())  # fail before creating
    project = create_project(body.name, body.description, actor.alias)
    if body.working_dir.strip():
        try:
            _apply_working_dir(project["id"], body.working_dir.strip(), actor.alias, body.overwrite_agents_md)
        except ApiError:
            # Filesystem prep failed after the insert: roll the project back
            # so a failed create does not consume the name.
            with transaction() as conn:
                conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
            raise
        project = serialize_project(get_project(project["id"]))
    setup_logging().info("project created %s", kv(project=project["id"], by=actor.alias))
    return ok(project)


@router.get("/projects/{project_id}")
async def get_project_endpoint(project_id: int, request: Request) -> dict:
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
    data = await _scoped_serialize(request, project)
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
    if body.working_dir is not None:
        if body.working_dir.strip():
            _apply_working_dir(project_id, body.working_dir.strip(), "admin", body.overwrite_agents_md)
        elif project["working_dir"]:
            with transaction() as conn:
                conn.execute(
                    "UPDATE projects SET working_dir = '', updated_at = ? WHERE id = ?",
                    (utc_now(), project_id),
                )
                audit.platform_record(
                    conn, project_id, "other",
                    "Working directory cleared (by admin)",
                    target=project["working_dir"], actor="admin",
                )
    return ok(serialize_project(get_project(project_id)))


@router.get("/fs/dirs")
async def browse_dirs(path: str = "", _admin: Actor = AdminDep) -> dict:
    """Admin-only directory listing backing the working-directory Browse
    control in the web UI. Never exposed to agent keys: no agent may walk
    the host filesystem. Directories under the system-path denylist are
    listed but marked unselectable, matching what set_working_dir accepts."""
    base = (Path(path).expanduser() if path.strip() else Path.home()).resolve()
    if not base.is_dir():
        raise ApiError(404, "not_found", f"'{base}' is not a directory.")

    def _scan() -> tuple[list[dict], bool]:
        # Capped and stat-lazy: sort names first (cheap), stat until the cap.
        dirs: list[dict] = []
        names = sorted((c.name for c in base.iterdir()), key=str.lower)
        for name in names:
            if name.startswith("."):
                continue
            child = base / name
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if len(dirs) >= 500:
                return dirs, True
            dirs.append({
                "name": name,
                "path": str(child),
                "selectable": not _forbidden_watch_path(child.resolve()),
            })
        return dirs, False

    try:
        dirs, truncated = await asyncio.to_thread(_scan)
    except PermissionError as exc:
        raise ApiError(403, "forbidden", f"Cannot read '{base}': permission denied.") from exc
    return ok({
        "path": str(base),
        "parent": str(base.parent) if base != base.parent else None,
        "home": str(Path.home()),
        "selectable": not _forbidden_watch_path(base),
        "truncated": truncated,
        "dirs": dirs,
    })


@router.put("/projects/{project_id}/working_dir")
async def set_working_dir(project_id: int, body: WorkingDirSet, request: Request) -> dict:
    """Set (or move) the project working directory. Open to the admin and,
    when the server allows it, to any agent on the project (typically the
    lead at kickoff). Copies AGENTS.md into the directory and announces the
    change in #general."""
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    if not actor.is_admin and not get_settings().agents_can_set_working_dir:
        raise ApiError(403, "forbidden", "Agents may not set the working directory on this server.")
    require_not_archived(get_project(project_id))
    resolved = _apply_working_dir(project_id, body.path.strip(), actor.alias, body.overwrite_agents_md)
    return ok({"working_dir": resolved})


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
    # The live monitor task would otherwise keep scanning (and failing to
    # write audit rows for a dead project id) until shutdown — issue #28 H8.
    fsmonitor.stop_watch(project_id)
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
