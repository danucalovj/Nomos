"""Test fixtures. The data dir is pointed at a temp location BEFORE any server
module is imported so the cached Settings pick it up."""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="nomos-test-")
os.environ["NOMOS_DATA_DIR"] = _TMP

import pytest
from fastapi.testclient import TestClient

from server.main import app  # noqa: E402  (import after env var on purpose)

_counter = {"n": 0}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        status = c.get("/api/setup/status").json()["data"]
        if not status["setup_complete"]:
            assert c.post("/api/setup", json={"alias": "overseer"}).status_code == 201
        yield c


@pytest.fixture()
def project(client):
    """A fresh project per test, with two joined agents."""
    _counter["n"] += 1
    n = _counter["n"]
    pid = client.post(
        "/api/projects", json={"name": f"proj-{n}", "description": "test"}
    ).json()["data"]["id"]
    agents = {}
    for alias in (f"alpha{n}", f"beta{n}"):
        data = client.post(
            f"/api/projects/{pid}/agents/join", json={"alias": alias, "role": "tester"}
        ).json()["data"]
        agents[alias] = {
            "key": data["api_key"],
            "id": data["agent"]["id"],
            "headers": {"Authorization": f"Bearer {data['api_key']}"},
            "alias": alias,
        }
    a, b = agents.values()
    return {
        "id": pid,
        "main_channel_id": data["main_channel_id"],
        "a": a,
        "b": b,
    }


def unwrap(resp, status: int = 200):
    assert resp.status_code == status, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["ok"] is True
    return body["data"]
