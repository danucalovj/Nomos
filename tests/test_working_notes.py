"""Issues #25 and #26: project working directory (AGENTS.md auto-copy) and
per-agent scratchpad + todo list (owner-writable, team-readable)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .conftest import unwrap


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="nomos-wdir-test-")


def test_working_dir_at_admin_create(client):
    target = Path(_tmpdir()) / "workspace"
    created = unwrap(
        client.post("/api/projects", json={"name": "WDir Admin", "working_dir": str(target)}),
        201,
    )
    assert created["working_dir"] == str(target.resolve())
    assert (target / "AGENTS.md").is_file()
    assert "Nomos" in (target / "AGENTS.md").read_text()[:200]

    fetched = unwrap(client.get(f"/api/projects/{created['id']}"))
    assert fetched["working_dir"] == str(target.resolve())


def test_working_dir_agent_set_and_announce(client, project):
    pid = project["id"]
    target = _tmpdir()
    result = unwrap(
        client.put(
            f"/api/projects/{pid}/working_dir",
            json={"path": target},
            headers=project["a"]["headers"],
        )
    )
    assert result["working_dir"] == str(Path(target).resolve())
    assert (Path(target) / "AGENTS.md").is_file()

    # Saved on the project for every agent to discover.
    proj = unwrap(client.get(f"/api/projects/{pid}"))
    assert proj["working_dir"] == str(Path(target).resolve())

    # Announced in #general so the team sees it.
    msgs = unwrap(
        client.get(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages?limit=10",
            headers=project["b"]["headers"],
        )
    )["items"]
    assert any("Working directory set" in m["body"] for m in msgs)


def test_working_dir_rejects_bad_paths(client, project):
    pid = project["id"]
    for bad in ("relative/path", "/etc/nomos-test", "/"):
        r = client.put(
            f"/api/projects/{pid}/working_dir",
            json={"path": bad},
            headers=project["a"]["headers"],
        )
        assert r.status_code == 422, bad


def test_working_dir_symlink_dest_refused(client, project):
    """Codex High: a planted AGENTS.md symlink must not be written through."""
    pid = project["id"]
    target = Path(_tmpdir())
    (target / "AGENTS.md").symlink_to(target / "victim.txt")
    r = client.put(
        f"/api/projects/{pid}/working_dir",
        json={"path": str(target)},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 422
    assert not (target / "victim.txt").exists()


def test_working_dir_admin_clear(client):
    target = _tmpdir()
    created = unwrap(
        client.post("/api/projects", json={"name": "WDir Clear", "working_dir": target}), 201
    )
    assert created["working_dir"]
    cleared = unwrap(
        client.patch(f"/api/projects/{created['id']}", json={"working_dir": ""})
    )
    assert cleared["working_dir"] == ""


def test_working_dir_create_rolls_back_on_bad_path(client):
    r = client.post(
        "/api/projects", json={"name": "WDir Doomed", "working_dir": "/etc/nope"}
    )
    assert r.status_code == 422
    # The name must not be consumed by a half-created project.
    ok2 = client.post("/api/projects", json={"name": "WDir Doomed"})
    assert ok2.status_code == 201


def test_scratchpad_owner_write_team_read(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]
    empty = unwrap(client.get("/api/me/scratchpad", headers=a["headers"]))
    assert empty["body"] == "" and empty["updated_at"] is None

    saved = unwrap(
        client.put(
            "/api/me/scratchpad",
            json={"body": "# Plan\n- port the parser\n- checksum edge case"},
            headers=a["headers"],
        )
    )
    assert saved["updated_at"] is not None

    # Owner reads back their own.
    mine = unwrap(client.get("/api/me/scratchpad", headers=a["headers"]))
    assert "checksum edge case" in mine["body"]

    # A teammate (lead) reads it through the notes route but has no write path.
    a_id = unwrap(client.get("/api/me", headers=a["headers"]))["id"]
    notes = unwrap(
        client.get(f"/api/projects/{pid}/agents/{a_id}/notes", headers=b["headers"])
    )
    assert notes["alias"] == a["alias"]
    assert "port the parser" in notes["scratchpad"]["body"]

    # B writing to /api/me/scratchpad touches only B's own pad.
    unwrap(client.put("/api/me/scratchpad", json={"body": "b's notes"}, headers=b["headers"]))
    again = unwrap(
        client.get(f"/api/projects/{pid}/agents/{a_id}/notes", headers=b["headers"])
    )
    assert "port the parser" in again["scratchpad"]["body"]


def test_todos_crud_and_validation(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]
    t1 = unwrap(
        client.post(
            "/api/me/todos",
            json={"text": "write TLE parser", "priority": "high"},
            headers=a["headers"],
        ),
        201,
    )
    assert t1["status"] == "todo" and t1["priority"] == "high"

    bad = client.post("/api/me/todos", json={"text": "x", "status": "nope"}, headers=a["headers"])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_status"
    bad = client.post("/api/me/todos", json={"text": "x", "priority": "urgent"}, headers=a["headers"])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_priority"

    moved = unwrap(
        client.patch(f"/api/me/todos/{t1['id']}", json={"status": "in-progress"}, headers=a["headers"])
    )
    assert moved["status"] == "in-progress"

    # Another agent cannot touch it through their own /me routes.
    r = client.patch(f"/api/me/todos/{t1['id']}", json={"status": "done"}, headers=b["headers"])
    assert r.status_code == 404

    # But they can read it through the notes route; the admin can too (keyless).
    a_id = unwrap(client.get("/api/me", headers=a["headers"]))["id"]
    for headers in (b["headers"], {}):
        notes = unwrap(client.get(f"/api/projects/{pid}/agents/{a_id}/notes", headers=headers))
        assert any(t["text"] == "write TLE parser" for t in notes["todos"])

    unwrap(client.delete(f"/api/me/todos/{t1['id']}", headers=a["headers"]))
    left = unwrap(client.get("/api/me/todos", headers=a["headers"]))["items"]
    assert not any(t["id"] == t1["id"] for t in left)
