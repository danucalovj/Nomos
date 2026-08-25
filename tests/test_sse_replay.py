"""Reconnects never lose events — everything replayable via
since_id, with membership-scoped visibility."""
from __future__ import annotations

from .conftest import unwrap


def test_event_replay_since_id(client, project):
    pid = project["id"]
    a = project["a"]["headers"]
    main = project["main_channel_id"]

    first = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "one"},
            headers=a,
        ),
        201,
    )
    events = unwrap(client.get(f"/api/projects/{pid}/events?since_id=0", headers=a))["items"]
    assert any(e["type"] == "message" and e["payload"]["id"] == first["id"] for e in events)
    cursor = max(e["id"] for e in events)

    second = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "two"},
            headers=a,
        ),
        201,
    )
    replay = unwrap(client.get(f"/api/projects/{pid}/events?since_id={cursor}", headers=a))["items"]
    ids = [e["payload"].get("id") for e in replay if e["type"] == "message"]
    assert ids == [second["id"]], "replay must contain exactly the missed events, in order"


def test_replay_respects_dm_visibility(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]
    outsider = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": f"outsider{pid}"}), 201
    )
    out_headers = {"Authorization": f"Bearer {outsider['api_key']}"}

    dm = unwrap(client.post(f"/api/projects/{pid}/dms", json={"with": b["alias"]}, headers=a["headers"]), 201)
    secret = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "secret"},
            headers=a["headers"],
        ),
        201,
    )

    for headers, should_see in ((a["headers"], True), (b["headers"], True), (out_headers, False), (None, True)):
        kwargs = {"headers": headers} if headers else {}
        events = unwrap(client.get(f"/api/projects/{pid}/events?since_id=0", **kwargs))["items"]
        seen = any(e["type"] == "message" and e["payload"]["id"] == secret["id"] for e in events)
        assert seen is should_see, f"visibility wrong for {headers and 'agent' or 'admin'}"


# The SSE endpoint itself is exercised against a real uvicorn server in
# test_sse_live.py — sse-starlette deadlocks under TestClient.
