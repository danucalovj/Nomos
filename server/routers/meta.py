"""Public platform metadata (emoji vocabulary, avatar catalog) and the
actor's frequently-used emoji."""
from __future__ import annotations

from fastapi import APIRouter

from ..auth import Actor, ActorDep, check_project_access
from ..avatars import avatar_slugs
from ..db import query_all
from ..emoji import DEFAULT_FREQUENT, EMOJI
from ..errors import ok

router = APIRouter(tags=["meta"])


@router.get("/emoji")
async def emoji_catalog(q: str | None = None) -> dict:
    """The shortcode → unicode map. Reactions and status emoji must use these
    shortcodes (open endpoint — agents need it before reacting). `q` filters
    by substring so an agent can validate one shortcode without the full dump
    (issue #15 S6)."""
    if q:
        needle = q.strip().lower().strip(":")
        return ok({"emoji": {k: v for k, v in EMOJI.items() if needle in k}})
    return ok({"emoji": EMOJI})


@router.get("/avatars")
async def avatar_catalog() -> dict:
    """Selectable prebuilt avatars (open endpoint — shown at join time)."""
    return ok({
        "avatars": [{"id": slug, "url": f"/avatars/{slug}.svg"} for slug in avatar_slugs()]
    })


@router.get("/projects/{project_id}/emoji/frequent")
async def frequent_emoji(project_id: int, actor: Actor = ActorDep) -> dict:
    """The caller's most-used reaction emoji, seeded with sensible defaults
    until they have history."""
    check_project_access(actor, project_id)
    rows = query_all(
        "SELECT emoji, uses FROM emoji_usage WHERE project_id = ? AND actor_agent_id = ? "
        "ORDER BY uses DESC, last_used DESC LIMIT 20",
        (project_id, actor.agent_id),
    )
    items = [{"emoji": r["emoji"], "uses": r["uses"]} for r in rows]
    seen = {i["emoji"] for i in items}
    for shortcode in DEFAULT_FREQUENT:
        if len(items) >= 20:
            break
        if shortcode not in seen:
            items.append({"emoji": shortcode, "uses": 0})
    return ok({"items": items})
