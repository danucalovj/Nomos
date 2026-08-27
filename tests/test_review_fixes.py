"""Issue #28: regression tests for the confirmed review findings."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .conftest import unwrap


def _post(client, project, body, headers=None):
    return unwrap(
        client.post(
            f"/api/projects/{project['id']}/conversations/{project['main_channel_id']}/messages",
            json={"body": body},
            headers=headers or project["a"]["headers"],
        ),
        201,
    )


def test_legacy_db_adoption_renames_wal_first(tmp_path):
    """H3: the main .db must be the LAST file renamed so its existence is a
    completion flag; -wal must never be left behind."""
    from server.config import Settings

    (tmp_path / "agentcomms.db").write_bytes(b"main")
    (tmp_path / "agentcomms.db-wal").write_bytes(b"wal")
    (tmp_path / "agentcomms.db-shm").write_bytes(b"shm")
    settings = Settings(data_dir=tmp_path)
    resolved = settings.db_path
    assert resolved == tmp_path / "nomos.db"
    assert (tmp_path / "nomos.db").read_bytes() == b"main"
    assert (tmp_path / "nomos.db-wal").read_bytes() == b"wal"
    assert (tmp_path / "nomos.db-shm").read_bytes() == b"shm"
    assert not (tmp_path / "agentcomms.db-wal").exists()


def test_working_dir_never_clobbers_existing_agents_md(client, project):
    """H6: an existing, different AGENTS.md is protected behind an explicit
    overwrite flag; identical content stays idempotent."""
    pid = project["id"]
    target = Path(tempfile.mkdtemp(prefix="nomos-h6-"))
    (target / "AGENTS.md").write_text("# My repo's own agent instructions\n")
    r = client.put(
        f"/api/projects/{pid}/working_dir",
        json={"path": str(target)},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "agents_md_exists"
    assert (target / "AGENTS.md").read_text() == "# My repo's own agent instructions\n"

    ok = unwrap(
        client.put(
            f"/api/projects/{pid}/working_dir",
            json={"path": str(target), "overwrite_agents_md": True},
            headers=project["a"]["headers"],
        )
    )
    assert ok["working_dir"] == str(target.resolve())
    assert "Nomos" in (target / "AGENTS.md").read_text()[:200]

    # Identical content: re-setting without the flag succeeds (idempotent).
    unwrap(
        client.put(
            f"/api/projects/{pid}/working_dir",
            json={"path": str(target)},
            headers=project["a"]["headers"],
        )
    )


def test_backlinks_drop_deleted_and_stale_sources(client, project):
    """H9 + H10: deleted-message excerpts disappear, and editing a message
    retires the backlink on the ticket it no longer references."""
    pid = project["id"]
    a = project["a"]["headers"]
    t1 = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "one"}, headers=a), 201)
    t2 = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "two"}, headers=a), 201)

    secret = _post(client, project, f"the key is hunter2, see #{t1['number']}")
    linked = unwrap(client.get(f"/api/projects/{pid}/tickets/{t1['number']}", headers=a))
    assert any("hunter2" in b["excerpt"] for b in linked["mentioned_in"])

    unwrap(client.delete(f"/api/projects/{pid}/messages/{secret['id']}", headers=a))
    after = unwrap(client.get(f"/api/projects/{pid}/tickets/{t1['number']}", headers=a))
    assert not any("hunter2" in b["excerpt"] for b in after["mentioned_in"])

    moving = _post(client, project, f"blocked by #{t1['number']}")
    unwrap(
        client.patch(
            f"/api/projects/{pid}/messages/{moving['id']}",
            json={"body": f"blocked by #{t2['number']}"},
            headers=a,
        )
    )
    t1_links = unwrap(client.get(f"/api/projects/{pid}/tickets/{t1['number']}", headers=a))
    assert not any(b["source_id"] == moving["id"] for b in t1_links["mentioned_in"])
    t2_links = unwrap(client.get(f"/api/projects/{pid}/tickets/{t2['number']}", headers=a))
    assert any(b["source_id"] == moving["id"] for b in t2_links["mentioned_in"])


def test_huge_ticket_ref_is_not_a_500(client, project):
    """H11: an out-of-int64 #N in prose must not break the request."""
    msg = _post(client, project, "tracking number #99999999999999999999 arrived")
    assert msg["id"] > 0


def test_system_messages_no_longer_self_backlink(client, project):
    """Ticket lifecycle chatter must not fill its own backlinks."""
    pid = project["id"]
    a = project["a"]["headers"]
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "quiet"}, headers=a), 201)
    unwrap(client.post(f"/api/projects/{pid}/tickets/{t['number']}/claim", headers=a))
    unwrap(
        client.patch(
            f"/api/projects/{pid}/tickets/{t['number']}", json={"status": "done"}, headers=a
        )
    )
    data = unwrap(client.get(f"/api/projects/{pid}/tickets/{t['number']}", headers=a))
    assert data["mentioned_in"] == []


def test_system_message_can_mention_admin(client, project):
    """A system message naming the admin must create the admin mention."""
    from server.db import query_one
    from server.services import post_system_message

    admin_alias = unwrap(client.get("/api/setup/status"))["admin"]["alias"]
    posted = post_system_message(project["id"], f"escalation: @{admin_alias} needed")
    assert posted is not None
    row = query_one(
        "SELECT 1 FROM mentions WHERE message_id = ? AND target_agent_id = 0",
        (posted["id"],),
    )
    assert row is not None


def test_claim_event_reports_status_change(client, project):
    pid = project["id"]
    a = project["a"]
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "ev"}, headers=a["headers"]), 201)
    unwrap(client.post(f"/api/projects/{pid}/tickets/{t['number']}/claim", headers=a["headers"]))
    events = unwrap(
        client.get(f"/api/projects/{pid}/events?since_id=0&types=ticket_updated", headers=a["headers"])
    )["items"]
    claim_ev = next(
        e for e in events
        if e["payload"]["ticket"]["number"] == t["number"]
        and e["payload"]["changes"].get("assignee")
    )
    assert claim_ev["payload"]["changes"]["status"] == {"from": "open", "to": "in-progress"}


def test_board_intra_column_move_is_noop(client, project):
    pid = project["id"]
    a = project["a"]["headers"]
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "still"}, headers=a), 201)
    unwrap(client.patch(f"/api/projects/{pid}/tickets/{t['number']}", json={"status": "blocked"}, headers=a))
    board = unwrap(client.get(f"/api/projects/{pid}/board", headers=a))
    review = next(c for c in board["columns"] if "blocked" in c["statuses"])
    moved = unwrap(
        client.post(
            f"/api/projects/{pid}/board/move",
            json={"ticket_number": t["number"], "column_id": review["id"]},
            headers=a,
        )
    )
    assert moved["ticket"]["status"] == "blocked"  # NOT rewritten to statuses[0]


def test_admin_identity_alias_stored_stripped(client):
    before = unwrap(client.get("/api/setup/status"))["admin"]["alias"]
    updated = unwrap(client.patch("/api/admin/identity", json={"alias": f"  {before}  "}))
    assert updated["admin"]["alias"] == before


def test_deleted_message_probe_and_history(client, project):
    """Unauthorized delete gets 403 regardless of state; deleted-message edit
    history is closed to non-authors."""
    pid = project["id"]
    a, b = project["a"]["headers"], project["b"]["headers"]
    msg = _post(client, project, "short-lived")
    unwrap(client.delete(f"/api/projects/{pid}/messages/{msg['id']}", headers=a))
    probe = client.delete(f"/api/projects/{pid}/messages/{msg['id']}", headers=b)
    assert probe.status_code == 403
    hist = client.get(f"/api/projects/{pid}/messages/{msg['id']}/edits", headers=b)
    assert hist.status_code == 404
    own = unwrap(client.get(f"/api/projects/{pid}/messages/{msg['id']}/edits", headers=a))
    assert any("short-lived" in e["prev_body"] for e in own["items"])


def test_csv_export_carries_chain_fields(client, project):
    pid = project["id"]
    a = project["a"]["headers"]
    unwrap(
        client.post(
            f"/api/projects/{pid}/audit",
            json={"action": "file_edit", "target": "x.py", "summary": "s", "diff": "+x"},
            headers=a,
        ),
        201,
    )
    resp = client.get(f"/api/projects/{pid}/audit/export?format=csv", headers=a)
    header = resp.text.splitlines()[0]
    assert "diff" in header.split(",") and "project_id" in header.split(",")


def test_working_dir_hidden_cross_project(client, project):
    """An agent's key must not read another project's absolute host path."""
    target = tempfile.mkdtemp(prefix="nomos-scope-")
    other = unwrap(client.post("/api/projects", json={"name": "Scoped", "working_dir": target}), 201)
    assert other["working_dir"]  # admin sees it
    as_agent = unwrap(
        client.get(f"/api/projects/{other['id']}", headers=project["a"]["headers"])
    )
    assert as_agent["working_dir"] == ""
    own = unwrap(client.get(f"/api/projects/{project['id']}", headers=project["a"]["headers"]))
    assert isinstance(own["working_dir"], str)  # own project stays readable


def test_create_with_assignee_emits_targeted_event(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]
    t = unwrap(
        client.post(
            f"/api/projects/{pid}/tickets",
            json={"title": "born assigned", "assignee": b["alias"]},
            headers=a["headers"],
        ),
        201,
    )
    events = unwrap(
        client.get(f"/api/projects/{pid}/events?since_id=0&types=ticket_assigned", headers=b["headers"])
    )["items"]
    assert any(e["payload"]["ticket_number"] == t["number"] for e in events)


def test_mention_with_trailing_period(client, project):
    pid = project["id"]
    b = project["b"]
    _post(client, project, f"please review this @{b['alias']}.")
    mentions = unwrap(client.get(f"/api/projects/{pid}/mentions?unseen=true", headers=b["headers"]))
    assert any(
        "review this" in ((m.get("message") or {}).get("body") or "")
        for m in mentions["items"]
    )

    # An alias that legitimately ENDS in '-' must stay mentionable too.
    edgy = unwrap(
        client.post(f"/api/projects/{pid}/agents/join", json={"alias": "qa-"}), 201
    )
    _post(client, project, "handing off to @qa- now")
    got = unwrap(
        client.get(
            f"/api/projects/{pid}/mentions?unseen=true",
            headers={"Authorization": f"Bearer {edgy['api_key']}"},
        )
    )
    assert any(
        "handing off" in ((m.get("message") or {}).get("body") or "")
        for m in got["items"]
    )
