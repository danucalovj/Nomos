"""Versioned documents with optimistic concurrency — stale writes get
HTTP 409 carrying the current revision so agents can merge."""
from __future__ import annotations

from .conftest import unwrap


def test_revisions_append_only_and_conflict(client, project):
    pid = project["id"]
    a, b = project["a"]["headers"], project["b"]["headers"]

    doc = unwrap(
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "Spec", "body": "v1"},
            headers=a,
        ),
        201,
    )
    slug = doc["slug"]
    assert doc["current_revision"] == 1

    updated = unwrap(
        client.put(
            f"/api/projects/{pid}/documents/{slug}",
            json={"body": "v2", "base_revision": 1},
            headers=b,
        )
    )
    assert updated["current_revision"] == 2

    stale = client.put(
        f"/api/projects/{pid}/documents/{slug}",
        json={"body": "v2-from-stale-base", "base_revision": 1},
        headers=a,
    )
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["current_revision"] == 2
    assert err["current_body"] == "v2"

    merged = unwrap(
        client.put(
            f"/api/projects/{pid}/documents/{slug}",
            json={"body": "v3 merged", "base_revision": 2},
            headers=a,
        )
    )
    assert merged["current_revision"] == 3

    revisions = unwrap(client.get(f"/api/projects/{pid}/documents/{slug}/revisions", headers=a))["items"]
    assert [r["revision"] for r in revisions] == [3, 2, 1]

    old = unwrap(client.get(f"/api/projects/{pid}/documents/{slug}?revision=1", headers=a))
    assert old["body"] == "v1"
    current = unwrap(client.get(f"/api/projects/{pid}/documents/{slug}", headers=a))
    assert current["body"] == "v3 merged"
