"""Audit trail (issue #17): self-reports with server-stamped identity, hash
chain + tamper detection, append-only surface, correlation, coverage,
platform mirror, export re-verification, watch validation."""
from __future__ import annotations

import hashlib
import json

from server.db import get_conn, query_all

from .conftest import unwrap


def _report(client, project, headers, **kw):
    body = {"action": "file_edit", "target": "src/app.py", "summary": "did work"} | kw
    return unwrap(
        client.post(f"/api/projects/{project['id']}/audit", json=body, headers=headers), 201
    )


def test_self_report_identity_is_server_stamped(client, project):
    pid = project["id"]
    rec = _report(client, project, project["a"]["headers"], summary="edited the parser")
    assert rec["actor"] == project["a"]["alias"]
    assert rec["actor_type"] == "agent"
    assert rec["source"] == "self_report"
    assert rec["entry_hash"] and rec["prev_hash"]

    admin_rec = unwrap(
        client.post(
            f"/api/projects/{pid}/audit",
            json={"action": "decision", "summary": "approved the plan"},
        ),
        201,
    )
    assert admin_rec["actor_type"] == "admin"

    bad = client.post(
        f"/api/projects/{pid}/audit",
        json={"action": "hacking", "summary": "x"},
        headers=project["a"]["headers"],
    )
    assert bad.status_code == 422


def test_chain_verifies_and_detects_tamper(client, project):
    pid = project["id"]
    for i in range(4):
        _report(client, project, project["a"]["headers"], summary=f"step {i}")
    v = unwrap(client.get(f"/api/projects/{pid}/audit/verify", headers=project["b"]["headers"]))
    assert v["ok"] is True and v["checked"] >= 4

    # Tamper directly in SQLite (bypassing the API, which has no mutation route)
    conn = get_conn()
    row = query_all(
        "SELECT id FROM audit_log WHERE project_id = ? ORDER BY id LIMIT 1", (pid,)
    )[0]
    conn.execute("UPDATE audit_log SET summary = 'rewritten history' WHERE id = ?", (row["id"],))
    conn.commit()
    v = unwrap(client.get(f"/api/projects/{pid}/audit/verify"))
    assert v["ok"] is False
    assert v["first_divergence"] == row["id"]
    conn.execute("UPDATE audit_log SET summary = 'step 0' WHERE id = ?", (row["id"],))
    conn.commit()


def test_append_only_surface(client, project):
    pid = project["id"]
    rec = _report(client, project, project["a"]["headers"])
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(
            f"/api/projects/{pid}/audit/{rec['id']}", headers=project["a"]["headers"]
        )
        assert r.status_code in (404, 405), f"{method} must not exist"


def test_bulk_atomic(client, project):
    pid = project["id"]
    good = {"action": "command", "summary": "ran the linter"}
    bad = {"action": "nope", "summary": "invalid"}
    r = client.post(
        f"/api/projects/{pid}/audit/bulk",
        json={"items": [good, bad]},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 422
    listed = unwrap(client.get(f"/api/projects/{pid}/audit?action=command", headers=project["a"]["headers"]))
    assert not any(i["summary"] == "ran the linter" for i in listed["items"])

    okr = unwrap(
        client.post(
            f"/api/projects/{pid}/audit/bulk",
            json={"items": [good, {"action": "test_run", "summary": "13/13 green"}]},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert len(okr["items"]) == 2


def test_filters_and_platform_mirror(client, project):
    pid = project["id"]
    a = project["a"]
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "mirror me"},
                           headers=a["headers"]), 201)
    unwrap(client.post(f"/api/projects/{pid}/tickets/{t['number']}/claim", headers=a["headers"]))
    doc = unwrap(client.post(f"/api/projects/{pid}/documents",
                             json={"title": "Mirror Doc", "body": "v1"}, headers=a["headers"]), 201)

    platform_rows = unwrap(
        client.get(f"/api/projects/{pid}/audit?source=platform", headers=a["headers"])
    )["items"]
    summaries = " | ".join(r["summary"] for r in platform_rows)
    assert f"Ticket #{t['number']} claimed by {a['alias']}" in summaries
    assert f"Document '{doc['slug']}' created" in summaries
    assert any("joined" in r["summary"] for r in platform_rows)  # join mirror

    only_actor = unwrap(
        client.get(f"/api/projects/{pid}/audit?actor={a['alias']}&source=platform",
                   headers=a["headers"])
    )["items"]
    assert all(r["actor"] == a["alias"] for r in only_actor)


def test_export_jsonl_reverifies_offline(client, project):
    pid = project["id"]
    _report(client, project, project["a"]["headers"], summary="export me")
    resp = client.get(f"/api/projects/{pid}/audit/export?format=jsonl",
                      headers=project["a"]["headers"])
    assert resp.status_code == 200
    lines = [json.loads(ln) for ln in resp.text.strip().splitlines()]
    assert lines
    # offline re-verification exactly as documented: canonical form is a
    # compact JSON array [prev_hash, project_id, actor, actor_type, source,
    # action, target, summary, detail, diff, correlated_id, created_at]
    prev = "0" * 64
    for rec in lines:
        fields = [prev, pid, rec["actor"], rec["actor_type"], rec["source"],
                  rec["action"], rec["target"], rec["summary"], rec["detail"],
                  rec["diff"], rec["correlated_id"], rec["created_at"]]
        canonical = json.dumps(fields, separators=(",", ":"), ensure_ascii=False)
        assert rec["prev_hash"] == prev
        assert rec["entry_hash"] == hashlib.sha256(canonical.encode()).hexdigest()
        prev = rec["entry_hash"]

    csv_resp = client.get(f"/api/projects/{pid}/audit/export?format=csv",
                          headers=project["a"]["headers"])
    assert csv_resp.status_code == 200 and csv_resp.text.startswith("id,")


def test_watch_validation_and_permissions(client, project):
    pid = project["id"]
    r = client.post(f"/api/projects/{pid}/audit/watch", json={"path": "/etc"},
                    headers=None)
    assert r.status_code == 422  # system root refused
    r = client.post(f"/api/projects/{pid}/audit/watch", json={"path": "relative/path"})
    assert r.status_code == 422
    r = client.post(f"/api/projects/{pid}/audit/watch", json={"path": "/tmp"},
                    headers=project["a"]["headers"])
    assert r.status_code == 403  # agent keys rejected — admin-only
