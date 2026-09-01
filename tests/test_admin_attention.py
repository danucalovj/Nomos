"""Issue #32: unseen admin @mentions ride every agent response envelope, and
the admin roster shows per-agent unread counts."""
from __future__ import annotations

from .conftest import unwrap


def _post(client, project, body, headers=None):
    return unwrap(
        client.post(
            f"/api/projects/{project['id']}/conversations/{project['main_channel_id']}/messages",
            json={"body": body},
            headers=headers,  # None = admin
        ),
        201,
    )


def test_attention_rides_unrelated_agent_responses(client, project):
    pid = project["id"]
    a = project["a"]

    # Baseline: no unseen admin mentions, no attention field.
    before = client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    assert "attention" not in before

    _post(client, project, f"@{a['alias']} what's pending?")

    # The very next UNRELATED call the agent makes carries the signal.
    resp = client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    assert resp["ok"] is True
    assert resp["attention"]["admin_mentions_unseen"] == 1
    assert resp["attention"]["oldest_at"]

    # Writes carry it too (the incident was outbound-only traffic).
    posted = client.post(
        f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
        json={"body": "status update"},
        headers=a["headers"],
    ).json()
    assert posted["attention"]["admin_mentions_unseen"] == 1

    # Marking seen clears it ON THE MARKING CALL ITSELF (live verification
    # caught a one-call-late clear: auth computed the payload before the
    # write), and on everything after.
    seen_resp = client.post(
        f"/api/projects/{pid}/mentions/seen", json={"all": True}, headers=a["headers"]
    ).json()
    assert seen_resp["ok"] is True and "attention" not in seen_resp
    after = client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    assert "attention" not in after


def test_attention_only_counts_admin_and_only_for_agents(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]

    # A TEAMMATE mention does not trigger the backstop (that is what normal
    # polling is for); only the human's voice escalates.
    _post(client, project, f"hey @{b['alias']} can you look?", headers=a["headers"])
    resp = client.get(f"/api/projects/{pid}/tickets", headers=b["headers"]).json()
    assert "attention" not in resp

    # Admin (keyless) responses never carry the field, even mid-incident.
    _post(client, project, f"@{b['alias']} ping from the human")
    admin_resp = client.get(f"/api/projects/{pid}/tickets").json()
    assert "attention" not in admin_resp
    unwrap(client.post(f"/api/projects/{pid}/mentions/seen", json={"all": True}, headers=b["headers"]))


def test_attention_counts_ticket_comment_mentions(client, project):
    pid = project["id"]
    a = project["a"]
    t = unwrap(
        client.post(f"/api/projects/{pid}/tickets", json={"title": "att"}, headers=a["headers"]), 201
    )
    unwrap(
        client.post(
            f"/api/projects/{pid}/tickets/{t['number']}/comments",
            json={"body": f"@{a['alias']} please re-check this"},
        ),
        201,
    )
    resp = client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    assert resp["attention"]["admin_mentions_unseen"] == 1
    unwrap(client.post(f"/api/projects/{pid}/mentions/seen", json={"all": True}, headers=a["headers"]))


def test_attention_ignores_deleted_admin_mentions(client, project):
    """A soft-deleted admin message must not keep the signal alive: the
    mention feed hides it, so the backstop must agree with the feed."""
    pid = project["id"]
    a = project["a"]
    msg = _post(client, project, f"@{a['alias']} scratch that")
    assert "attention" in client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    unwrap(client.delete(f"/api/projects/{pid}/messages/{msg['id']}"))
    resp = client.get(f"/api/projects/{pid}/tickets", headers=a["headers"]).json()
    assert "attention" not in resp
    unwrap(client.post(f"/api/projects/{pid}/mentions/seen", json={"all": True}, headers=a["headers"]))


def test_roster_shows_unread_counts_to_admin_only(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]

    _post(client, project, f"@{a['alias']} unread one")                       # admin -> a
    _post(client, project, f"also @{a['alias']} unread two")                  # admin -> a
    _post(client, project, f"fyi @{b['alias']}", headers=a["headers"])        # teammate -> b

    roster = unwrap(client.get(f"/api/projects/{pid}/agents"))["items"]
    by_alias = {r["alias"]: r for r in roster}
    assert by_alias[a["alias"]]["admin_mentions_unseen"] == 2
    assert by_alias[a["alias"]]["mentions_unseen"] == 2
    assert by_alias[b["alias"]]["admin_mentions_unseen"] == 0
    assert by_alias[b["alias"]]["mentions_unseen"] == 1

    # Agent callers do not get the counts (they are the admin's console view).
    agent_view = unwrap(client.get(f"/api/projects/{pid}/agents", headers=a["headers"]))["items"]
    assert all("mentions_unseen" not in r for r in agent_view)

    for h in (a["headers"], b["headers"]):
        unwrap(client.post(f"/api/projects/{pid}/mentions/seen", json={"all": True}, headers=h))
    cleared = unwrap(client.get(f"/api/projects/{pid}/agents"))["items"]
    assert all(r["mentions_unseen"] == 0 for r in cleared)
