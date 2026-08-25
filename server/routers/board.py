"""Kanban board: columns are views over ticket statuses — cards ARE tickets.
Moving a card delegates to the single ticket status-change code path."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..auth import Actor, AdminDep, check_project_access, get_actor
from ..db import query_all, query_one, transaction
from ..errors import ApiError, ok
from ..services import get_project, project_settings
from .tickets import TicketUpdate, serialize_ticket, update_ticket_fields

router = APIRouter(tags=["board"])


class ColumnSpec(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    statuses: list[str] = Field(default_factory=list)


class BoardUpdate(BaseModel):
    columns: list[ColumnSpec] = Field(min_length=1, max_length=12)


class MoveRequest(BaseModel):
    ticket_number: int
    column_id: int


def _board_state(project_id: int) -> dict[str, Any]:
    columns = query_all(
        "SELECT * FROM board_columns WHERE project_id = ? ORDER BY position", (project_id,)
    )
    tickets = query_all(
        "SELECT * FROM tickets WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)
    )
    status_to_column: dict[str, int] = {}
    out_columns: list[dict[str, Any]] = []
    for col in columns:
        statuses = json.loads(col["statuses"] or "[]")
        for s in statuses:
            status_to_column.setdefault(s, col["id"])
        out_columns.append(
            {"id": col["id"], "name": col["name"], "position": col["position"],
             "statuses": statuses, "cards": []}
        )
    by_id = {c["id"]: c for c in out_columns}
    unmapped: list[dict[str, Any]] = []
    for t in tickets:
        card = serialize_ticket(t)
        column_id = status_to_column.get(t["status"])
        if column_id is None:
            unmapped.append(card)
        else:
            by_id[column_id]["cards"].append(card)
    if unmapped:
        out_columns.append(
            {"id": None, "name": "Unmapped", "position": len(out_columns),
             "statuses": [], "cards": unmapped}
        )
    return {"columns": out_columns}


@router.get("/projects/{project_id}/board")
async def get_board(project_id: int, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    get_project(project_id)
    return ok(_board_state(project_id))


@router.put("/projects/{project_id}/board/columns")
async def replace_columns(
    project_id: int, body: BoardUpdate, _admin: Actor = AdminDep
) -> dict:
    project = get_project(project_id)
    valid_statuses = set(project_settings(project)["ticket_statuses"])
    seen: set[str] = set()
    for col in body.columns:
        for status in col.statuses:
            if status not in valid_statuses:
                raise ApiError(
                    422, "invalid_status",
                    f"Status '{status}' is not in this project's ticket statuses.",
                )
            if status in seen:
                raise ApiError(
                    422, "duplicate_status",
                    f"Status '{status}' is mapped to more than one column.",
                )
            seen.add(status)
    with transaction() as conn:
        conn.execute("DELETE FROM board_columns WHERE project_id = ?", (project_id,))
        for position, col in enumerate(body.columns):
            conn.execute(
                "INSERT INTO board_columns (project_id, name, position, statuses) VALUES (?, ?, ?, ?)",
                (project_id, col.name, position, json.dumps(col.statuses)),
            )
    return ok(_board_state(project_id))


@router.post("/projects/{project_id}/board/move")
async def move_card(project_id: int, body: MoveRequest, request: Request) -> dict:
    actor: Actor = await get_actor(request)
    check_project_access(actor, project_id)
    get_project(project_id)
    column = query_one(
        "SELECT * FROM board_columns WHERE id = ? AND project_id = ?",
        (body.column_id, project_id),
    )
    if column is None:
        raise ApiError(404, "not_found", f"Board column {body.column_id} not found in this project.")
    statuses = json.loads(column["statuses"] or "[]")
    if not statuses:
        raise ApiError(422, "unmapped_column", "Target column has no mapped statuses.")
    update = TicketUpdate(status=statuses[0])
    ticket = await update_ticket_fields(
        project_id, body.ticket_number, actor, update, {"status"}
    )
    return ok({"ticket": ticket, "column_id": body.column_id})
