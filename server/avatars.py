"""Prebuilt avatar catalog. The SVGs live in webui/avatars/ (served by the
static mount); manifest.json lists the selectable slugs. 'admin' is reserved
for the human admin and is never selectable by agents."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

AVATARS_DIR = Path(__file__).resolve().parent.parent / "webui" / "avatars"
ADMIN_AVATAR = "admin"
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,32}$")


@lru_cache
def avatar_slugs() -> tuple[str, ...]:
    manifest = AVATARS_DIR / "manifest.json"
    if not manifest.is_file():
        return ()
    slugs = json.loads(manifest.read_text())
    return tuple(s for s in slugs if _SLUG_RE.match(s) and s != ADMIN_AVATAR)


def is_valid_agent_avatar(slug: str) -> bool:
    """'' (unset → initials fallback) or a selectable manifest slug."""
    return slug == "" or slug in avatar_slugs()


def is_valid_admin_avatar(slug: str) -> bool:
    return slug == "" or slug == ADMIN_AVATAR or slug in avatar_slugs()
