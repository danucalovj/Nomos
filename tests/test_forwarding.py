"""Message forwarding: DM↔channel flows, authz matrix, chain flattening,
tombstone propagation."""
from __future__ import annotations

from .conftest import unwrap


def _dm(client, project):
    return unwrap(
        client.post(
            f"/api/projects/{project['id']}/dms",
            json={"with": project["b"]["alias"]},
            headers=project["a"]["headers"],
        ),
        201,
    )


def test_forward_dm_to_channel_and_back(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    dm = _dm(client, project)
    secret = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "the private plan"},
            headers=project["a"]["headers"],
        ),
        201,
    )

    fwd = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{secret['id']}/forward",
            json={"to_conversation_id": main, "comment": "surfacing this"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert fwd["body"] == "surfacing this"
    assert fwd["forwarded_from"]["author"] == project["a"]["alias"]
    assert fwd["forwarded_from"]["body"] == "the private plan"
    assert fwd["forwarded_from"]["conversation_label"] == "a direct message"

    channel_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "public info"},
            headers=project["b"]["headers"],
        ),
        201,
    )
    back = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{channel_msg['id']}/forward",
            json={"to_conversation_id": dm["id"]},
            headers=project["b"]["headers"],
        ),
        201,
    )
    assert back["forwarded_from"]["conversation_label"] == "#general"
    assert back["body"] == ""  # comment-less forward is allowed


def test_forward_authz(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    dm = _dm(client, project)
    secret = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "members only"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    outsider = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": f"fwd{pid}"}), 201
    )
    out_headers = {"Authorization": f"Bearer {outsider['api_key']}"}

    r = client.post(
        f"/api/projects/{pid}/messages/{secret['id']}/forward",
        json={"to_conversation_id": main},
        headers=out_headers,
    )
    assert r.status_code == 403  # not a member of the source DM

    private = unwrap(
        client.post(f"/api/projects/{pid}/channels", json={"name": f"priv{pid}"},
                    headers=project["a"]["headers"]),
        201,
    )
    public_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "seen by all"},
            headers=outsider and out_headers,
        ),
        201,
    )
    r = client.post(
        f"/api/projects/{pid}/messages/{public_msg['id']}/forward",
        json={"to_conversation_id": private["id"]},
        headers=out_headers,
    )
    assert r.status_code == 403  # not a member of the target channel

    admin_fwd = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{secret['id']}/forward",
            json={"to_conversation_id": main, "comment": "admin can forward anything"},
        ),
        201,
    )
    assert admin_fwd["role"] == "admin"


def test_forward_of_forward_flattens_and_tombstones(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    ch = unwrap(
        client.post(f"/api/projects/{pid}/channels", json={"name": f"fwd2{pid}",
                    "invite": [project["b"]["alias"]]}, headers=project["a"]["headers"]),
        201,
    )
    original = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "the origin"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    first = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{original['id']}/forward",
            json={"to_conversation_id": ch["id"]},
            headers=project["a"]["headers"],
        ),
        201,
    )
    second = unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{first['id']}/forward",
            json={"to_conversation_id": main},
            headers=project["b"]["headers"],
        ),
        201,
    )
    assert second["forwarded_from"]["message_id"] == original["id"]  # chain flattened

    unwrap(client.delete(f"/api/projects/{pid}/messages/{original['id']}",
                         headers=project["a"]["headers"]))
    refreshed = unwrap(
        client.get(f"/api/projects/{pid}/messages/{second['id']}", headers=project["b"]["headers"])
    )
    assert refreshed["forwarded_from"]["deleted"] is True
    assert refreshed["forwarded_from"]["body"] == ""

    r = client.post(
        f"/api/projects/{pid}/messages/{original['id']}/forward",
        json={"to_conversation_id": main},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 410  # cannot forward a deleted message
