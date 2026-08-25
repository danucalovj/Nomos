"""Emoji reactions: toggle semantics, validation, visibility, concurrency."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from server.db import query_all
from server.main import app

from .conftest import unwrap


def _post(client, project, body="react to me"):
    return unwrap(
        client.post(
            f"/api/projects/{project['id']}/conversations/{project['main_channel_id']}/messages",
            json={"body": body},
            headers=project["a"]["headers"],
        ),
        201,
    )


def test_reaction_toggle_and_summary(client, project):
    pid = project["id"]
    msg = _post(client, project)
    r1 = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{msg['id']}/reactions",
            json={"emoji": "thumbsup"},
            headers=project["b"]["headers"],
        )
    )
    assert r1["reacted"] is True
    assert r1["reactions"] == [
        {"emoji": "thumbsup", "count": 1, "by": [project["b"]["alias"]]}
    ]

    admin = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{msg['id']}/reactions",
            json={"emoji": "thumbsup"},
        )
    )
    assert admin["reactions"][0]["count"] == 2
    assert "overseer" in admin["reactions"][0]["by"]

    r2 = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{msg['id']}/reactions",
            json={"emoji": "thumbsup"},
            headers=project["b"]["headers"],
        )
    )
    assert r2["reacted"] is False
    assert r2["reactions"][0]["count"] == 1

    serialized = unwrap(
        client.get(f"/api/projects/{pid}/messages/{msg['id']}", headers=project["a"]["headers"])
    )
    assert serialized["reactions"][0]["emoji"] == "thumbsup"

    frequent = unwrap(
        client.get(f"/api/projects/{pid}/emoji/frequent", headers=project["b"]["headers"])
    )["items"]
    assert frequent[0]["emoji"] == "thumbsup" and frequent[0]["uses"] == 1


def test_reaction_validation(client, project):
    pid = project["id"]
    msg = _post(client, project)
    r = client.post(
        f"/api/projects/{pid}/messages/{msg['id']}/reactions",
        json={"emoji": "not_a_real_emoji"},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 422
    unwrap(client.delete(f"/api/projects/{pid}/messages/{msg['id']}", headers=project["a"]["headers"]))
    r = client.post(
        f"/api/projects/{pid}/messages/{msg['id']}/reactions",
        json={"emoji": "thumbsup"},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 410


def test_reaction_requires_membership(client, project):
    pid = project["id"]
    dm = unwrap(
        client.post(
            f"/api/projects/{pid}/dms",
            json={"with": project["b"]["alias"]},
            headers=project["a"]["headers"],
        ),
        201,
    )
    dm_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "private"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    outsider = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": f"reactor{pid}"}), 201
    )
    r = client.post(
        f"/api/projects/{pid}/messages/{dm_msg['id']}/reactions",
        json={"emoji": "eyes"},
        headers={"Authorization": f"Bearer {outsider['api_key']}"},
    )
    assert r.status_code == 403


def test_concurrent_toggles_never_duplicate(client, project):
    pid = project["id"]
    msg = _post(client, project, "contended reaction")
    key = project["b"]["key"]

    def toggle(_: int) -> int:
        with TestClient(app) as c:
            return c.post(
                f"/api/projects/{pid}/messages/{msg['id']}/reactions",
                json={"emoji": "fire"},
                headers={"Authorization": f"Bearer {key}"},
            ).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(toggle, range(10)))
    assert all(code == 200 for code in results), results

    rows = query_all(
        "SELECT COUNT(*) AS c FROM message_reactions WHERE message_id = ? AND emoji = 'fire'",
        (msg["id"],),
    )
    assert rows[0]["c"] in (0, 1)  # even toggle count -> 0, odd -> 1; never dupes
