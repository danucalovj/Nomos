"""Issue #15 improvements: search operators + offset, doc-conflict base_body,
event type filters, assignee=me, mark_read, emoji q + suggestions, MIME
fallback, bulk ticket creation, targeted assignment events."""
from __future__ import annotations

from .conftest import unwrap


def _post(client, project, body, **extra):
    return unwrap(
        client.post(
            f"/api/projects/{project['id']}/conversations/{project['main_channel_id']}/messages",
            json={"body": body, **extra},
            headers=project["a"]["headers"],
        ),
        201,
    )


def test_search_operators_and_offset(client, project):
    pid = project["id"]
    a = project["a"]["headers"]
    for i in range(3):
        _post(client, project, f"quasar telemetry sweep {i}")
    unwrap(
        client.post(
            f"/api/projects/{pid}/conversations/{project['main_channel_id']}/messages",
            json={"body": "quasar note from the other side"},
            headers=project["b"]["headers"],
        ),
        201,
    )

    hits = unwrap(
        client.get(
            f"/api/projects/{pid}/search?q=from:{project['a']['alias']} quasar", headers=a
        )
    )
    assert hits["items"] and all(h["author"] == project["a"]["alias"] for h in hits["items"])

    hits = unwrap(client.get(f"/api/projects/{pid}/search?q=in:%23general quasar", headers=a))
    assert len(hits["items"]) == 4

    r = client.get(f"/api/projects/{pid}/search?q=in:%23nope quasar", headers=a)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_channel"

    r = client.get(f"/api/projects/{pid}/search?q=from:{project['a']['alias']}", headers=a)
    assert r.status_code == 422  # operators alone carry no search terms

    page1 = unwrap(client.get(f"/api/projects/{pid}/search?q=quasar&limit=2", headers=a))
    assert len(page1["items"]) == 2 and page1["has_more"] is True
    page2 = unwrap(client.get(f"/api/projects/{pid}/search?q=quasar&limit=2&offset=2", headers=a))
    assert len(page2["items"]) == 2
    assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page2["items"]})


def test_doc_conflict_includes_base_body(client, project):
    pid = project["id"]
    a, b = project["a"]["headers"], project["b"]["headers"]
    doc = unwrap(
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "Merge Target", "body": "base text"},
            headers=a,
        ),
        201,
    )
    unwrap(
        client.put(
            f"/api/projects/{pid}/documents/{doc['slug']}",
            json={"body": "their text", "base_revision": 1},
            headers=b,
        )
    )
    stale = client.put(
        f"/api/projects/{pid}/documents/{doc['slug']}",
        json={"body": "my text", "base_revision": 1},
        headers=a,
    )
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["current_body"] == "their text"
    assert err["base_body"] == "base text"  # 3-way merge material


def test_event_type_filter(client, project):
    pid = project["id"]
    a = project["a"]["headers"]
    _post(client, project, "noise message one")
    unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "filter target"}, headers=a), 201)
    _post(client, project, "noise message two")

    filtered = unwrap(
        client.get(f"/api/projects/{pid}/events?since_id=0&types=ticket_created", headers=a)
    )
    assert filtered["items"] and all(e["type"] == "ticket_created" for e in filtered["items"])

    both = unwrap(
        client.get(
            f"/api/projects/{pid}/events?since_id=0&types=ticket_created,message", headers=a
        )
    )
    assert {e["type"] for e in both["items"]} == {"ticket_created", "message"}


def test_assignee_me_and_assignment_event(client, project):
    pid = project["id"]
    a, b = project["a"], project["b"]
    t = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "hold this"}, headers=a["headers"]), 201)
    unwrap(client.post(f"/api/projects/{pid}/tickets/{t['number']}/claim", headers=a["headers"]))

    mine = unwrap(client.get(f"/api/projects/{pid}/tickets?assignee=me", headers=a["headers"]))["items"]
    assert any(x["number"] == t["number"] for x in mine)
    theirs = unwrap(client.get(f"/api/projects/{pid}/tickets?assignee=me", headers=b["headers"]))["items"]
    assert not any(x["number"] == t["number"] for x in theirs)

    unwrap(
        client.patch(
            f"/api/projects/{pid}/tickets/{t['number']}",
            json={"assignee": b["alias"]},
            headers=a["headers"],
        )
    )
    events = unwrap(
        client.get(f"/api/projects/{pid}/events?since_id=0&types=ticket_assigned", headers=b["headers"])
    )["items"]
    assert any(
        e["payload"]["ticket_number"] == t["number"] and e["payload"]["assignee"] == b["alias"]
        for e in events
    )
    # Targeted: the other agent must NOT receive it.
    other = unwrap(
        client.get(f"/api/projects/{pid}/events?since_id=0&types=ticket_assigned", headers=a["headers"])
    )["items"]
    assert not any(e["payload"].get("ticket_number") == t["number"] for e in other)


def test_mark_read_advances_cursor(client, project):
    pid = project["id"]
    main = project["main_channel_id"]
    b = project["b"]["headers"]
    _post(client, project, "unread one")
    _post(client, project, "unread two")
    unwrap(client.get(f"/api/projects/{pid}/conversations/{main}/messages?mark_read=true", headers=b))
    cursors = unwrap(client.get(f"/api/projects/{pid}/read_cursors", headers=b))
    row = next(i for i in cursors["items"] if i["conversation_id"] == main)
    assert row["unread"] == 0


def test_emoji_filter_and_suggestions(client, project):
    catalog = unwrap(client.get("/api/emoji?q=check"))["emoji"]
    assert "white_check_mark" in catalog and "rocket" not in catalog

    msg = _post(client, project, "react target")
    r = client.post(
        f"/api/projects/{project['id']}/messages/{msg['id']}/reactions",
        json={"emoji": "thumsup"},
        headers=project["a"]["headers"],
    )
    assert r.status_code == 422
    assert "thumbsup" in r.json()["error"]["suggestions"]


def test_attachment_mime_guessed(client, project):
    upload = unwrap(
        client.post(
            f"/api/projects/{project['id']}/attachments",
            files={"file": ("data.csv", b"a,b\n1,2\n", "application/octet-stream")},
            headers=project["a"]["headers"],
        ),
        201,
    )
    assert upload["mime_type"] == "text/csv"


def test_bulk_ticket_create(client, project):
    pid = project["id"]
    created = unwrap(
        client.post(
            f"/api/projects/{pid}/tickets/bulk",
            json={"tickets": [
                {"title": "sprint item A", "priority": "high"},
                {"title": "sprint item B"},
                {"title": "sprint item C", "labels": ["batch"]},
            ]},
            headers=project["a"]["headers"],
        ),
        201,
    )["items"]
    assert len(created) == 3
    numbers = [t["number"] for t in created]
    assert numbers == sorted(numbers)

    bad = client.post(
        f"/api/projects/{pid}/tickets/bulk",
        json={"tickets": [{"title": "ok"}, {"title": "bad", "priority": "nope"}]},
        headers=project["a"]["headers"],
    )
    assert bad.status_code == 422
    after = unwrap(client.get(f"/api/projects/{pid}/tickets?q=ok", headers=project["a"]["headers"]))["items"]
    assert not any(t["title"] == "ok" for t in after)  # atomic: nothing created


def test_ticket_reporter_filter(client, project):
    """Issue #18: drill-down needs tickets-by-reporter."""
    pid = project["id"]
    a, b = project["a"], project["b"]
    mine = unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "opened by a"},
                              headers=a["headers"]), 201)
    unwrap(client.post(f"/api/projects/{pid}/tickets", json={"title": "opened by b"},
                       headers=b["headers"]), 201)
    listed = unwrap(client.get(f"/api/projects/{pid}/tickets?reporter={a['alias']}",
                               headers=a["headers"]))["items"]
    assert any(t["number"] == mine["number"] for t in listed)
    assert all(t["reporter"] == a["alias"] for t in listed)
    me = unwrap(client.get(f"/api/projects/{pid}/tickets?reporter=me", headers=b["headers"]))["items"]
    assert all(t["reporter"] == b["alias"] for t in me)


def test_documents_author_filter(client, project):
    """Issue #18: drill-down needs docs-by-creator; list includes author."""
    pid = project["id"]
    a, b = project["a"], project["b"]
    doc_a = unwrap(client.post(f"/api/projects/{pid}/documents",
                               json={"title": "Doc By A", "body": "x"}, headers=a["headers"]), 201)
    unwrap(client.post(f"/api/projects/{pid}/documents",
                       json={"title": "Doc By B", "body": "y"}, headers=b["headers"]), 201)
    # revision by B on A's doc must NOT change creating-author attribution
    unwrap(client.put(f"/api/projects/{pid}/documents/{doc_a['slug']}",
                      json={"body": "x2", "base_revision": 1}, headers=b["headers"]))
    listed = unwrap(client.get(f"/api/projects/{pid}/documents?author={a['alias']}",
                               headers=a["headers"]))["items"]
    assert [d["slug"] for d in listed] == [doc_a["slug"]]
    assert listed[0]["author"] == a["alias"]
    everything = unwrap(client.get(f"/api/projects/{pid}/documents", headers=a["headers"]))["items"]
    assert all("author" in d for d in everything)
