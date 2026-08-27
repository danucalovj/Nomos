"""Issue #29 test-hygiene sweep: the admin kill switches (previously zero
coverage) and concurrent optimistic document writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from server.db import query_one

from .conftest import unwrap


def _join(client, pid, alias):
    data = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": alias}), 201
    )
    return data["agent"]["id"], {"Authorization": f"Bearer {data['api_key']}"}


def test_revoke_kills_key_and_admin_routes_reject_agents(client, project):
    pid = project["id"]
    agent_id, headers = _join(client, pid, "condemned")

    ok_before = client.get(f"/api/projects/{pid}/tickets", headers=headers)
    assert ok_before.status_code == 200

    # An agent key must not reach the kill switch itself.
    probe = client.post(
        f"/api/projects/{pid}/agents/{agent_id}/revoke", headers=project["a"]["headers"]
    )
    assert probe.status_code == 403
    assert probe.json()["error"]["code"] == "admin_only"

    unwrap(client.post(f"/api/projects/{pid}/agents/{agent_id}/revoke"))
    after = client.get(f"/api/projects/{pid}/tickets", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "invalid_key"


def test_remove_agent_cleans_personal_state(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    agent_id, headers = _join(client, pid, "leaver")

    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "waving at @leaver"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{msg['id']}/reactions",
            json={"emoji": "thumbsup"},
            headers=headers,
        )
    )
    unwrap(client.post(f"/api/projects/{pid}/messages/{msg['id']}/save", headers=headers))

    unwrap(client.delete(f"/api/projects/{pid}/agents/{agent_id}"))
    for table, col in (
        ("conversation_members", "agent_id"),
        ("mentions", "target_agent_id"),
        ("emoji_usage", "actor_agent_id"),
        ("saved_items", "actor_agent_id"),
        ("agent_todos", "agent_id"),
    ):
        row = query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE {col} = ?", (agent_id,))
        assert row["c"] == 0, f"{table} not cleaned"
    # Their key is dead too.
    assert client.get(f"/api/projects/{pid}/tickets", headers=headers).status_code == 401


def test_concurrent_doc_writes_one_wins_one_clean_409(client, project):
    """Two simultaneous PUTs from the same base_revision: exactly one 200 and
    one enveloped 409, never an unhandled 500 from the UNIQUE constraint."""
    pid = project["id"]
    a, b = project["a"]["headers"], project["b"]["headers"]
    doc = unwrap(
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "Race Target", "body": "base"},
            headers=a,
        ),
        201,
    )

    def put(headers, body):
        return client.put(
            f"/api/projects/{pid}/documents/{doc['slug']}",
            json={"body": body, "base_revision": 1},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = pool.map(lambda args: put(*args), [(a, "mine"), (b, "theirs")])

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], codes
    loser = r1 if r1.status_code == 409 else r2
    err = loser.json()["error"]
    assert err["code"] == "revision_conflict" and "current_body" in err
