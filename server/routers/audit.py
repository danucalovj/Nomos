"""Audit trail endpoints (issue #17).

Append-only by construction: there is no update or delete route in this file
and there must never be one — tamper-evidence relies on it."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import audit, fsmonitor
from ..auth import Actor, ActorDep, AdminDep, check_project_access
from ..db import query_all, transaction, utc_now
from ..errors import ApiError, ok
from ..events import notify
from ..services import get_project, pagination_window

router = APIRouter(tags=["audit"])

# Checked against the RESOLVED path (symlinks followed — on macOS /etc is
# really /private/etc). A path is refused if it equals a root or sits under a
# forbidden prefix.
FORBIDDEN_WATCH_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/System", "/Library", "/var",
    "/private/etc", "/private/var", "/Applications", "/opt",
)


ALLOWED_WATCH_PREFIXES = (  # user-space temp lives under /var on macOS
    "/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
)


def _forbidden_watch_path(resolved: Path) -> bool:
    s = str(resolved)
    if s == "/" or len(resolved.parts) <= 1:
        return True
    if any(s.startswith(p) for p in ALLOWED_WATCH_PREFIXES):
        return False
    return any(s == p or s.startswith(p + "/") for p in FORBIDDEN_WATCH_PREFIXES)


class SelfReport(BaseModel):
    action: str
    target: str = Field(default="", max_length=1024)
    summary: str = Field(min_length=1, max_length=2000)
    detail: dict[str, Any] = Field(default_factory=dict)
    diff: str | None = Field(default=None, max_length=256 * 1024)


class SelfReportBulk(BaseModel):
    items: list[SelfReport] = Field(min_length=1, max_length=50)


class WatchCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


def _validate_report(item: SelfReport) -> None:
    if item.action not in audit.ACTIONS:
        raise ApiError(
            422, "invalid_action",
            f"action must be one of: {', '.join(audit.ACTIONS)}.",
        )


def _insert_report(conn, project_id: int, actor: Actor, item: SelfReport) -> int:
    correlated = audit.find_recent_observation(conn, project_id, item.action, item.target)
    return audit.record(
        conn, project_id,
        actor=actor.alias,
        actor_type=actor.role_flag,
        source="self_report",
        action=item.action,
        summary=item.summary,
        target=item.target,
        detail=item.detail,
        diff=item.diff,
        correlated_id=correlated,
    )


@router.post("/projects/{project_id}/audit", status_code=201)
async def self_report(project_id: int, body: SelfReport, actor: Actor = ActorDep) -> dict:
    """Report one unit of work. Identity and timestamp are stamped
    server-side from the Bearer context — client-supplied values do not
    exist in this API's shape by design."""
    check_project_access(actor, project_id)
    get_project(project_id)
    _validate_report(body)
    with transaction() as conn:
        audit_id = _insert_report(conn, project_id, actor, body)
    await notify(project_id)
    row = query_all("SELECT * FROM audit_log WHERE id = ?", (audit_id,))[0]
    return ok(audit.serialize_row(row))


@router.post("/projects/{project_id}/audit/bulk", status_code=201)
async def self_report_bulk(project_id: int, body: SelfReportBulk, actor: Actor = ActorDep) -> dict:
    """Atomic batch for turn-based agents reporting a work session (≤50)."""
    check_project_access(actor, project_id)
    get_project(project_id)
    for item in body.items:
        _validate_report(item)
    ids: list[int] = []
    with transaction() as conn:
        for item in body.items:
            ids.append(_insert_report(conn, project_id, actor, item))
    await notify(project_id)
    rows = query_all(
        f"SELECT * FROM audit_log WHERE id IN ({', '.join('?' for _ in ids)}) ORDER BY id",
        tuple(ids),
    )
    return ok({"items": [audit.serialize_row(r) for r in rows]})


@router.get("/projects/{project_id}/audit")
async def list_audit(
    project_id: int,
    actor_filter: str | None = Query(default=None, alias="actor"),
    action: str | None = None,
    source: str | None = None,
    target: str | None = None,
    after: str | None = None,
    before: str | None = None,
    before_id: int | None = None,
    limit: int | None = None,
    actor: Actor = ActorDep,
) -> dict:
    """The merged trail, newest first. Monitor rows carry `claimed` resolved
    in BOTH correlation directions (append-only rows mean the link lives on
    whichever row landed second)."""
    check_project_access(actor, project_id)
    get_project(project_id)
    window = pagination_window(limit, max_limit=500)
    clauses = ["a.project_id = :pid"]
    params: dict[str, Any] = {"pid": project_id, "lim": window + 1}
    if actor_filter:
        clauses.append("a.actor = :actor COLLATE NOCASE")
        params["actor"] = actor_filter
    if action:
        clauses.append("a.action = :action")
        params["action"] = action
    if source:
        clauses.append("a.source = :source")
        params["source"] = source
    if target:
        clauses.append("a.target LIKE :target")
        params["target"] = f"%{target}%"
    if after:
        clauses.append("a.created_at >= :after")
        params["after"] = after
    if before:
        clauses.append("a.created_at <= :before")
        params["before"] = before
    if before_id is not None:
        clauses.append("a.id < :before_id")
        params["before_id"] = before_id
    rows = query_all(
        f"""
        SELECT a.*, (a.correlated_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM audit_log b
            WHERE b.project_id = a.project_id AND b.source = 'self_report'
              AND b.correlated_id = a.id
        )) AS claimed
        FROM audit_log a
        WHERE {' AND '.join(clauses)}
        ORDER BY a.id DESC LIMIT :lim
        """,
        params,
    )
    has_more = len(rows) > window
    items = []
    for r in rows[:window]:
        item = audit.serialize_row(r)
        if r["source"] == "monitor":
            item["claimed"] = bool(r["claimed"])
        items.append(item)
    return ok({"items": items, "has_more": has_more})


@router.get("/projects/{project_id}/audit/verify")
async def verify(project_id: int, actor: Actor = ActorDep) -> dict:
    """Recompute the hash chain. Exposed to every project reader — integrity
    checking is not a privilege."""
    check_project_access(actor, project_id)
    get_project(project_id)
    return ok(audit.verify_chain(project_id))


@router.get("/projects/{project_id}/audit/coverage")
async def coverage(project_id: int, actor: Actor = ActorDep) -> dict:
    """Per-actor accounting: self-reports vs monitor observations vs
    unattributed changes (the honesty dashboard)."""
    check_project_access(actor, project_id)
    get_project(project_id)
    reports = query_all(
        "SELECT actor, COUNT(*) AS n FROM audit_log "
        "WHERE project_id = ? AND source = 'self_report' GROUP BY actor",
        (project_id,),
    )
    observed = query_all(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN correlated_id IS NOT NULL OR EXISTS (
                   SELECT 1 FROM audit_log b
                   WHERE b.project_id = a.project_id AND b.source = 'self_report'
                     AND b.correlated_id = a.id
               ) THEN 1 ELSE 0 END) AS claimed
        FROM audit_log a WHERE a.project_id = ? AND a.source = 'monitor'
        """,
        (project_id,),
    )
    total_observed = observed[0]["total"] if observed else 0
    total_claimed = observed[0]["claimed"] or 0 if observed else 0
    return ok({
        "actors": [{"actor": r["actor"], "self_reports": r["n"]} for r in reports],
        "observed": total_observed,
        "correlated": total_claimed,
        "unattributed": total_observed - total_claimed,
    })


@router.get("/projects/{project_id}/audit/export")
async def export(project_id: int, format: str = "jsonl", actor: Actor = ActorDep):
    """Full-trail export with chain fields for offline re-verification."""
    check_project_access(actor, project_id)
    get_project(project_id)
    rows = query_all(
        "SELECT * FROM audit_log WHERE project_id = ? ORDER BY id", (project_id,)
    )
    stamp = utc_now()[:10]
    if format == "csv":
        buf = io.StringIO()
        fields = ["id", "created_at", "actor", "actor_type", "source", "action",
                  "target", "summary", "detail", "correlated_id", "prev_hash", "entry_hash"]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in fields})
        return PlainTextResponse(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="audit-{project_id}-{stamp}.csv"'},
        )
    if format != "jsonl":
        raise ApiError(422, "invalid_format", "format must be jsonl or csv.")

    def lines():
        for r in rows:
            yield json.dumps(audit.serialize_row(r), sort_keys=True) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="audit-{project_id}-{stamp}.jsonl"'},
    )


# ------------------------------------------------------------- watches

@router.get("/projects/{project_id}/audit/watch")
async def get_watch(project_id: int, actor: Actor = ActorDep) -> dict:
    check_project_access(actor, project_id)
    get_project(project_id)
    import asyncio

    return ok({"watch": await asyncio.to_thread(fsmonitor.watch_status, project_id)})


@router.post("/projects/{project_id}/audit/watch", status_code=201)
async def register_watch(project_id: int, body: WatchCreate, _admin: Actor = AdminDep) -> dict:
    """Admin-only: the monitor reads the host filesystem. One watch per
    project (v1); registering replaces any existing watch."""
    get_project(project_id)
    path = Path(body.path).expanduser()
    if not path.is_absolute():
        raise ApiError(422, "invalid_path", "Watch path must be absolute.")
    resolved = path.resolve()
    if _forbidden_watch_path(resolved):
        raise ApiError(422, "invalid_path", "Refusing to watch a system directory.")
    if not resolved.is_dir():
        raise ApiError(422, "invalid_path", f"'{resolved}' is not a directory.")
    with transaction() as conn:
        conn.execute(
            "INSERT INTO audit_watches (project_id, path, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (project_id) DO UPDATE SET path = excluded.path, created_at = excluded.created_at",
            (project_id, str(resolved), utc_now()),
        )
        audit.platform_record(
            conn, project_id, "other",
            f"File monitor registered on {resolved}", target=str(resolved),
            actor="admin",
        )
    fsmonitor.start_watch(project_id, str(resolved))
    await notify(project_id)
    import asyncio

    return ok({"watch": await asyncio.to_thread(fsmonitor.watch_status, project_id)})


@router.delete("/projects/{project_id}/audit/watch")
async def remove_watch(project_id: int, _admin: Actor = AdminDep) -> dict:
    get_project(project_id)
    with transaction() as conn:
        existing = conn.execute(
            "SELECT path FROM audit_watches WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing is None:
            raise ApiError(404, "not_found", "No watch is registered for this project.")
        conn.execute("DELETE FROM audit_watches WHERE project_id = ?", (project_id,))
        audit.platform_record(
            conn, project_id, "other",
            f"File monitor removed from {existing['path']}", target=existing["path"],
            actor="admin",
        )
    fsmonitor.stop_watch(project_id)
    await notify(project_id)
    return ok({"removed": True})
