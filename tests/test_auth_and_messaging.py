"""Cross-cutting auth and messaging behavior: role flags (admin impersonation
impossible), project scoping, mentions, cursors/unread, decisions, pins."""
from __future__ import annotations

from .conftest import unwrap


def test_admin_role_flag_and_no_impersonation(client, project):
    pid = project["id"]
    main = project["main_channel_id"]

    agent_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "I am just an agent"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert agent_msg["role"] == "agent"

    admin_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "the human speaks"},
        ),
        201,
    )
    assert admin_msg["role"] == "admin"
    assert admin_msg["author"] == "overseer"

    r = client.post(f"/api/projects/{pid}/agents/join", json={"alias": "overseer"})
    assert r.status_code == 409  # admin alias is reserved


def test_agent_cannot_cross_projects(client, project):
    other_pid = unwrap(client.post("/api/projects", json={"name": f"other-{project['id']}"}), 201)["id"]
    r = client.get(f"/api/projects/{other_pid}/agents", headers=project["a"]["headers"])
    assert r.status_code == 403


def test_admin_can_post_into_agent_dm(client, project):
    pid = project["id"]
    dm = unwrap(
        client.post(f"/api/projects/{pid}/dms", json={"with": project["b"]["alias"]},
                    headers=project["a"]["headers"]),
        201,
    )
    joined = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "I can see this DM"},
        ),
        201,
    )
    assert joined["role"] == "admin"
    listed = unwrap(client.get(f"/api/projects/{pid}/dms"))["items"]
    assert any(d["id"] == dm["id"] for d in listed)  # admin sees all DMs


def test_mentions_and_unread_cursor(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    a, b = project["a"], project["b"]

    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": f"hey @{b['alias']} look"},
            headers=a["headers"],
        ),
        201,
    )
    mentions = unwrap(
        client.get(f"/api/projects/{pid}/mentions?unseen=true", headers=b["headers"])
    )["items"]
    assert any(
        m.get("source") == "message" and m["message"]["id"] == msg["id"] for m in mentions
    )

    cursors = unwrap(client.get(f"/api/projects/{pid}/read_cursors", headers=b["headers"]))["items"]
    main_cursor = next(c for c in cursors if c["conversation_id"] == main)
    assert main_cursor["unread"] >= 1

    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/read_cursor",
            json={"last_read_message_id": msg["id"]},
            headers=b["headers"],
        )
    )
    cursors = unwrap(client.get(f"/api/projects/{pid}/read_cursors", headers=b["headers"]))["items"]
    main_cursor = next(c for c in cursors if c["conversation_id"] == main)
    assert main_cursor["unread"] == 0


def test_decisions_are_queryable_and_pins_listed(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    a = project["a"]["headers"]

    decision = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "We will use SQLite.", "type": "decision"},
            headers=a,
        ),
        201,
    )
    decisions = unwrap(client.get(f"/api/projects/{pid}/decisions", headers=a))["items"]
    assert any(d["id"] == decision["id"] for d in decisions)

    unwrap(client.post(f"/api/projects/{pid}/messages/{decision['id']}/pin", headers=a))
    pins = unwrap(client.get(f"/api/projects/{pid}/conversations/{main}/pins", headers=a))["items"]
    assert any(p["id"] == decision["id"] for p in pins)
