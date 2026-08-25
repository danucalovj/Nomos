"""Attachments: streamed multipart upload (size-capped) and download.

Uploads start unbound; `post_message` / ticket comments / documents claim
them via `attachment_ids`. Files live at
`{data_dir}/attachments/{project_id}/{uuid}{ext}`."""
from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile
from fastapi.responses import FileResponse

from ..auth import Actor, ActorDep, check_project_access
from ..config import get_settings
from ..db import query_one, transaction, utc_now
from ..errors import ApiError, ok
from ..services import get_project, require_not_archived

router = APIRouter(tags=["attachments"])


def _resolve_mime(declared: str | None, filename: str) -> str:
    """Clients (curl -F in particular) often send application/octet-stream;
    fall back to guessing from the filename so the UI can render the type
    tile correctly (issue #15 S4)."""
    if declared and declared != "application/octet-stream":
        return declared
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or declared or "application/octet-stream"

_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_CHUNK = 1024 * 1024


def _safe_ext(filename: str) -> str:
    ext = Path(filename).suffix
    return ext if _EXT_RE.match(ext) else ""


def _serialize(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "message_id": row["message_id"],
        "filename": row["filename"],
        "size": row["size"],
        "mime_type": row["mime_type"],
        "uploader": row["uploader"],
        "created_at": row["created_at"],
        "url": f"/api/projects/{row['project_id']}/attachments/{row['id']}",
    }


@router.post("/projects/{project_id}/attachments", status_code=201)
async def upload_attachment(
    project_id: int, file: UploadFile, actor: Actor = ActorDep
) -> dict:
    project = get_project(project_id)
    require_not_archived(project)
    check_project_access(actor, project_id)
    settings = get_settings()
    max_bytes = settings.max_upload_bytes

    original_name = Path(file.filename or "upload.bin").name or "upload.bin"
    stored_name = uuid.uuid4().hex + _safe_ext(original_name)
    project_dir = settings.attachments_dir / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    dest = project_dir / stored_name

    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(
                        413, "too_large",
                        f"Attachment exceeds the {settings.max_upload_mb} MB limit.",
                    )
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO attachments (project_id, filename, stored_name, size, mime_type, "
            "uploader, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                original_name,
                stored_name,
                size,
                _resolve_mime(file.content_type, original_name),
                actor.alias,
                utc_now(),
            ),
        )
        attachment_id = int(cur.lastrowid or 0)
    row = query_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    return ok(_serialize(row))


@router.get("/projects/{project_id}/attachments/{attachment_id}")
async def download_attachment(
    project_id: int, attachment_id: int, actor: Actor = ActorDep
) -> FileResponse:
    check_project_access(actor, project_id)
    row = query_one(
        "SELECT * FROM attachments WHERE id = ? AND project_id = ?",
        (attachment_id, project_id),
    )
    if row is None:
        raise ApiError(404, "not_found", f"Attachment {attachment_id} not found.")
    if not actor.is_admin:
        if row["message_id"] is not None:
            message = query_one(
                "SELECT conversation_id FROM messages WHERE id = ?", (row["message_id"],)
            )
            direct_access = message is not None and query_one(
                "SELECT 1 FROM conversation_members WHERE conversation_id = ? AND agent_id = ?",
                (message["conversation_id"], actor.agent_id),
            ) is not None
            # Forwarded embeds copy content into the target conversation, so a
            # member of any conversation holding a live forward of this message
            # may fetch its attachments too.
            forwarded_access = query_one(
                """
                SELECT 1 FROM messages f
                JOIN conversation_members cm ON cm.conversation_id = f.conversation_id
                WHERE f.forwarded_from_id = ? AND f.deleted = 0 AND cm.agent_id = ?
                """,
                (row["message_id"], actor.agent_id),
            ) is not None
            if not direct_access and not forwarded_access:
                raise ApiError(403, "forbidden", "You do not have access to this attachment.")
        elif row["comment_id"] is None and row["document_id"] is None:
            # Unbound upload: only the uploader may fetch it until it is
            # attached to something (ids are sequential and guessable).
            if row["uploader"] != actor.alias:
                raise ApiError(403, "forbidden", "This attachment is not bound to anything yet.")
    path = get_settings().attachments_dir / str(project_id) / row["stored_name"]
    if not path.is_file():
        raise ApiError(410, "file_missing", "Attachment file is missing from disk.")
    return FileResponse(path, filename=row["filename"], media_type=row["mime_type"])
