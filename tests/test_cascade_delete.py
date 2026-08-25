"""Project deletion is a full transactional cascade — no orphaned rows
in any table, attachment files removed from disk."""
from __future__ import annotations

from pathlib import Path

from server.config import get_settings
from server.db import query_all, query_one

from .conftest import unwrap

TABLES_WITH_PROJECT_ID = [
    "agents",
    "conversations",
    "messages",
    "mentions",
    "attachments",
    "tickets",
    "board_columns",
    "documents",
    "events",
]


def _populate(client, project) -> tuple[int, Path]:
    pid = project["id"]
    a = project["a"]["headers"]
    b = project["b"]["headers"]
    main = project["main_channel_id"]

    ch = unwrap(client.post(f"/api/projects/{pid}/channels", json={"name": "side"}, headers=a), 201)
    unwrap(client.post(f"/api/projects/{pid}/channels/{ch['id']}/join", headers=b))
    msg = unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": f"hello @{project['b']['alias']} see #1"},
            headers=a,
        ),
        201,
    )
    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "reply", "parent_id": msg["id"]},
            headers=b,
        ),
        201,
    )
    dm = unwrap(client.post(f"/api/projects/{pid}/dms", json={"with": project["b"]["alias"]}, headers=a), 201)
    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{dm['id']}/messages",
            json={"body": "psst"},
            headers=a,
        ),
        201,
    )
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "t1"}, headers=a), 201)
    unwrap(
        client.post(
            f"/api/projects/{pid}/tickets/{t['number']}/comments",
            json={"body": "comment"},
            headers=b,
        ),
        201,
    )
    unwrap(
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "Design Doc", "body": "v1 body"},
            headers=a,
        ),
        201,
    )
    upload = unwrap(
        client.post(
            f"/api/projects/{pid}/attachments",
            files={"file": ("note.txt", b"attachment-bytes", "text/plain")},
            headers=a,
        ),
        201,
    )
    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "with file", "attachment_ids": [upload["id"]]},
            headers=a,
        ),
        201,
    )
    att_dir = get_settings().attachments_dir / str(pid)
    assert att_dir.exists() and any(att_dir.iterdir())
    return pid, att_dir


def test_cascade_delete_leaves_no_orphans(client, project):
    pid, att_dir = _populate(client, project)

    conv_ids = [r["id"] for r in query_all("SELECT id FROM conversations WHERE project_id = ?", (pid,))]
    ticket_ids = [r["id"] for r in query_all("SELECT id FROM tickets WHERE project_id = ?", (pid,))]
    doc_ids = [r["id"] for r in query_all("SELECT id FROM documents WHERE project_id = ?", (pid,))]
    msg_ids = [r["id"] for r in query_all("SELECT id FROM messages WHERE project_id = ?", (pid,))]
    assert conv_ids and ticket_ids and doc_ids and msg_ids

    unwrap(client.delete(f"/api/projects/{pid}"))

    for table in TABLES_WITH_PROJECT_ID:
        rows = query_all(f"SELECT COUNT(*) AS c FROM {table} WHERE project_id = ?", (pid,))
        assert rows[0]["c"] == 0, f"orphans left in {table}"

    placeholders = ",".join("?" for _ in conv_ids)
    assert query_one(
        f"SELECT COUNT(*) AS c FROM conversation_members WHERE conversation_id IN ({placeholders})",
        tuple(conv_ids),
    )["c"] == 0
    ph = ",".join("?" for _ in msg_ids)
    assert query_one(
        f"SELECT COUNT(*) AS c FROM message_edits WHERE message_id IN ({ph})", tuple(msg_ids)
    )["c"] == 0
    ph = ",".join("?" for _ in ticket_ids)
    assert query_one(
        f"SELECT COUNT(*) AS c FROM ticket_comments WHERE ticket_id IN ({ph})", tuple(ticket_ids)
    )["c"] == 0
    assert query_one(
        f"SELECT COUNT(*) AS c FROM ticket_links WHERE ticket_id IN ({ph})", tuple(ticket_ids)
    )["c"] == 0
    ph = ",".join("?" for _ in doc_ids)
    assert query_one(
        f"SELECT COUNT(*) AS c FROM document_revisions WHERE document_id IN ({ph})", tuple(doc_ids)
    )["c"] == 0

    assert not att_dir.exists()

    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 404
