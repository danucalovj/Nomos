"""Profiles (avatars, custom status), admin identity, doc-share cards,
mention badges, and saved items."""
from __future__ import annotations

from .conftest import unwrap


def test_avatar_and_status_roundtrip(client, project):
    pid = project["id"]
    catalog = unwrap(client.get("/api/avatars"))["avatars"]
    assert len(catalog) == 24
    slug = catalog[0]["id"]

    me = unwrap(
        client.patch(
            "/api/me",
            json={"avatar": slug, "status_text": "building the parser", "status_emoji": "hammer"},
            headers=project["a"]["headers"],
        )
    )
    assert me["avatar"] == slug
    assert me["status_text"] == "building the parser"
    assert me["status_emoji"] == "hammer"
    assert me["online"] is True

    r = client.patch("/api/me", json={"avatar": "admin"}, headers=project["a"]["headers"])
    assert r.status_code == 422  # reserved
    r = client.patch("/api/me", json={"status_emoji": "nope"}, headers=project["a"]["headers"])
    assert r.status_code == 422

    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
            json={"body": "with avatar"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert msg["avatar"] == slug

    join = client.post(
        f"/api/projects/{pid}/agents/join", json={"alias": f"badav{pid}", "avatar": "bogus"}
    )
    assert join.status_code == 422


def test_admin_identity_patch(client, project):
    updated = unwrap(client.patch("/api/admin/identity", json={"color": "#f0b64a"}))
    assert updated["admin"]["color"] == "#f0b64a"
    assert updated["admin"]["avatar"] == "admin"
    r = client.patch(
        "/api/admin/identity", json={"alias": "x"},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 403  # agent keys rejected outright


def test_doc_card_on_message(client, project):
    pid = project["id"]
    doc = unwrap(
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "Share Me", "body": "content"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
            json={"body": "", "doc_slug": doc["slug"]},
            headers=project["b"]["headers"],
        ),
        201,
    )
    assert msg["doc_card"]["title"] == "Share Me"
    assert msg["doc_card"]["current_revision"] == 1

    r = client.post(
        f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
        json={"body": "", "doc_slug": "does-not-exist"},
        headers=project["b"]["headers"],
    )
    assert r.status_code == 422


def test_mention_badges_in_cursors(client, project):
    pid = project["id"]
    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
            json={"body": f"ping @{project['b']['alias']}"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    cursors = unwrap(client.get(f"/api/projects/{pid}/read_cursors", headers=project["b"]["headers"]))
    main_row = next(i for i in cursors["items"] if i["conversation_id"] == project["main_channel_id"])
    assert main_row["mentions_unseen"] == 1
    assert cursors["total_mentions_unseen"] >= 1


def test_saved_items_toggle_and_privacy(client, project):
    pid = project["id"]
    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
            json={"body": "worth saving"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert unwrap(
        client.post(f"/api/projects/{pid}/messages/{msg['id']}/save", headers=project["b"]["headers"])
    )["saved"] is True
    saved_b = unwrap(client.get(f"/api/projects/{pid}/saved", headers=project["b"]["headers"]))["items"]
    assert [m["id"] for m in saved_b] == [msg["id"]]

    saved_a = unwrap(client.get(f"/api/projects/{pid}/saved", headers=project["a"]["headers"]))["items"]
    assert saved_a == []  # personal, not shared

    assert unwrap(
        client.post(f"/api/projects/{pid}/messages/{msg['id']}/save", headers=project["b"]["headers"])
    )["saved"] is False
    assert unwrap(client.get(f"/api/projects/{pid}/saved", headers=project["b"]["headers"]))["items"] == []


def test_saved_items_respect_current_membership(client, project):
    """Codex review fix: leaving a channel revokes access to messages saved
    from it."""
    pid = project["id"]
    b = project["b"]["headers"]
    ch = unwrap(
        client.post(
            f"/api/projects/{pid}/channels",
            json={"name": f"leaveme{pid}", "invite": [project["b"]["alias"]]},
            headers=project["a"]["headers"],
        ),
        201,
    )
    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{ch['id']}/messages",
            json={"body": "channel-scoped"},
            headers=b,
        ),
        201,
    )
    unwrap(client.post(f"/api/projects/{pid}/messages/{msg['id']}/save", headers=b))
    assert any(
        m["id"] == msg["id"]
        for m in unwrap(client.get(f"/api/projects/{pid}/saved", headers=b))["items"]
    )
    unwrap(client.post(f"/api/projects/{pid}/channels/{ch['id']}/leave", headers=b))
    assert not any(
        m["id"] == msg["id"]
        for m in unwrap(client.get(f"/api/projects/{pid}/saved", headers=b))["items"]
    )


def test_forwarded_attachment_downloadable_by_target_members(client, project):
    """Codex review fix: a live forward grants the target conversation's
    members access to the original's attachments."""
    pid = project["id"]
    a, b = project["a"]["headers"], project["b"]["headers"]
    dm = unwrap(
        client.post(f"/api/projects/{pid}/dms", json={"with": project["b"]["alias"]}, headers=a),
        201,
    )
    upload = unwrap(
        client.post(
            f"/api/projects/{pid}/attachments",
            files={"file": ("plan.txt", b"secret bytes", "text/plain")},
            headers=a,
        ),
        201,
    )
    dm_msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "see file", "attachment_ids": [upload["id"]]},
            headers=a,
        ),
        201,
    )
    outsider = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": f"dl{pid}"}), 201
    )
    out = {"Authorization": f"Bearer {outsider['api_key']}"}
    assert client.get(f"/api/projects/{pid}/attachments/{upload['id']}", headers=out).status_code == 403

    unwrap(
        client.post(
            f"/api/projects/{pid}/messages/{dm_msg['id']}/forward",
            json={"to_conversation_id": project["main_channel_id"]},
            headers=a,
        ),
        201,
    )
    assert client.get(f"/api/projects/{pid}/attachments/{upload['id']}", headers=out).status_code == 200
