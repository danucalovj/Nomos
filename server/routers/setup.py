"""First-time admin setup. The setup screen appears only while the database
has no admin identity; afterwards these endpoints refuse re-setup."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth import Actor, AdminDep
from ..avatars import ADMIN_AVATAR, is_valid_admin_avatar
from ..db import query_one, transaction, utc_now
from ..errors import ApiError, ok
from ..services import ALIAS_RE

router = APIRouter(tags=["setup"])


class SetupRequest(BaseModel):
    alias: str = Field(min_length=2, max_length=32)
    color: str = Field(default="#e0b040", max_length=16)
    avatar: str = Field(default=ADMIN_AVATAR, max_length=32)


class AdminIdentityUpdate(BaseModel):
    alias: str | None = Field(default=None, min_length=2, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    avatar: str | None = Field(default=None, max_length=32)


def _admin_row() -> dict | None:
    row = query_one("SELECT alias, color, avatar, created_at FROM admin_identity WHERE id = 1")
    return dict(row) if row else None


@router.get("/setup/status")
async def setup_status() -> dict:
    admin = _admin_row()
    return ok({"setup_complete": admin is not None, "admin": admin})


@router.post("/setup", status_code=201)
async def complete_setup(body: SetupRequest) -> dict:
    alias = body.alias.strip()
    if not ALIAS_RE.match(alias):
        raise ApiError(
            422, "invalid_alias",
            "Alias must be 2-32 chars: letters, digits, '_', '-', '.', starting alphanumeric.",
        )
    if alias.lower() in ("here", "system"):
        raise ApiError(422, "reserved_alias", f"'{alias}' is a reserved alias.")
    if not is_valid_admin_avatar(body.avatar):
        raise ApiError(422, "invalid_avatar", "Unknown avatar.")
    with transaction() as conn:
        existing = conn.execute("SELECT 1 FROM admin_identity WHERE id = 1").fetchone()
        if existing is not None:
            raise ApiError(409, "already_setup", "Admin setup was already completed.")
        conn.execute(
            "INSERT INTO admin_identity (id, alias, color, avatar, created_at) VALUES (1, ?, ?, ?, ?)",
            (alias, body.color, body.avatar, utc_now()),
        )
    return ok({"setup_complete": True, "admin": _admin_row()})


@router.patch("/admin/identity")
async def update_admin_identity(body: AdminIdentityUpdate, _admin: Actor = AdminDep) -> dict:
    """Admin-only: change the admin alias, display color, or avatar after
    setup. Alias rules match agent aliases; 'admin' avatar is the reserved
    amber mark."""
    current = _admin_row()
    if current is None:
        raise ApiError(409, "setup_required", "Complete first-time setup before editing the identity.")
    if body.alias is not None:
        alias = body.alias.strip()
        if not ALIAS_RE.match(alias):
            raise ApiError(
                422, "invalid_alias",
                "Alias must be 2-32 chars: letters, digits, '_', '-', '.', starting alphanumeric.",
            )
        if alias.lower() in ("here", "system"):
            raise ApiError(422, "reserved_alias", f"'{alias}' is a reserved alias.")
        clash = query_one("SELECT 1 FROM agents WHERE lower(alias) = lower(?)", (alias,))
        if clash is not None:
            raise ApiError(409, "alias_taken", "An agent already uses that alias.")
    if body.avatar is not None and not is_valid_admin_avatar(body.avatar):
        raise ApiError(422, "invalid_avatar", "Unknown avatar.")
    changed = [c for c, v in (("alias", body.alias), ("color", body.color), ("avatar", body.avatar)) if v is not None]
    with transaction() as conn:
        for column, value in (("alias", body.alias), ("color", body.color), ("avatar", body.avatar)):
            if value is not None:
                conn.execute(f"UPDATE admin_identity SET {column} = ? WHERE id = 1", (value,))
        # Governance mirror: identity changes touch every project's trail.
        from .. import audit

        for proj in conn.execute("SELECT id FROM projects").fetchall():
            audit.platform_record(
                conn, proj["id"], "other",
                f"Admin identity changed ({', '.join(changed)})", actor="admin",
            )
    return ok({"admin": _admin_row()})
