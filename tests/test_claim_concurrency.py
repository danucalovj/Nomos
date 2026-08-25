"""Hard requirement: concurrent claims have exactly one winner;
losers receive HTTP 409."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from server.main import app

from .conftest import unwrap

N_CONTENDERS = 12


def test_exactly_one_claim_winner(client, project):
    pid = project["id"]
    ticket = unwrap(
        client.post(
            f"/api/projects/{pid}/tickets",
            json={"title": "contended work"},
            headers=project["a"]["headers"],
        ),
        201,
    )
    number = ticket["number"]

    contenders = []
    for i in range(N_CONTENDERS):
        data = unwrap(
            client.post(
                f"/api/projects/{pid}/agents/join", json={"alias": f"claimer{pid}x{i}"}
            ),
            201,
        )
        contenders.append(data["api_key"])

    def claim(key: str) -> int:
        with TestClient(app) as c:
            r = c.post(
                f"/api/projects/{pid}/tickets/{number}/claim",
                headers={"Authorization": f"Bearer {key}"},
            )
            return r.status_code

    with ThreadPoolExecutor(max_workers=N_CONTENDERS) as pool:
        results = list(pool.map(claim, contenders))

    assert results.count(200) == 1, results
    assert results.count(409) == N_CONTENDERS - 1, results

    final = unwrap(client.get(f"/api/projects/{pid}/tickets/{number}"))
    assert final["assignee"] is not None
    assert final["status"] == "in-progress"


def test_claim_after_assignment_conflicts(client, project):
    pid = project["id"]
    number = unwrap(
        client.post(
            f"/api/projects/{pid}/tickets",
            json={"title": "single claim"},
            headers=project["a"]["headers"],
        ),
        201,
    )["number"]
    unwrap(client.post(f"/api/projects/{pid}/tickets/{number}/claim", headers=project["a"]["headers"]))
    r = client.post(f"/api/projects/{pid}/tickets/{number}/claim", headers=project["b"]["headers"])
    assert r.status_code == 409
    assert r.json()["error"]["assignee"] == project["a"]["alias"]
