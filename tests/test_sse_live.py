"""SSE over a real uvicorn server (production path): backlog replay, live
push, and reconnect-from-since_id without event loss."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="nomos-sse-test-")
    env = os.environ | {
        "NOMOS_DATA_DIR": data_dir,
        "NOMOS_HOST": "127.0.0.1",
        "NOMOS_PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{base}/api/setup/status", timeout=1)
                break
            except httpx.TransportError:
                if proc.poll() is not None:
                    raise RuntimeError("uvicorn exited early")
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not come up")
        yield base
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


def _read_sse_events(resp: httpx.Response, want: int, deadline_s: float = 10) -> list[tuple[str, dict, int]]:
    """Collect (event_type, payload, event_id) triples from an open SSE response."""
    events: list[tuple[str, dict, int]] = []
    event_type, data, event_id = None, None, None
    start = time.monotonic()
    try:
        for line in resp.iter_lines():
            if time.monotonic() - start > deadline_s:
                break
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                event_id = int(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1])
            elif line == "" and event_type and event_type != "ping":
                events.append((event_type, data, event_id))
                event_type, data, event_id = None, None, None
                if len(events) >= want:
                    break
            elif line == "":
                event_type, data, event_id = None, None, None
    except httpx.ReadTimeout:
        pass  # stream went quiet before `want` events arrived; return what we have
    return events


def test_sse_backlog_live_and_reconnect(live_server):
    base = live_server
    with httpx.Client(base_url=base, timeout=10) as c:
        assert c.post("/api/setup", json={"alias": "overseer"}).status_code == 201
        pid = c.post("/api/projects", json={"name": "sse-live"}).json()["data"]["id"]
        joined = c.post(f"/api/projects/{pid}/agents/join", json={"alias": "streamer"}).json()["data"]
        headers = {"Authorization": f"Bearer {joined['api_key']}"}
        main = joined["main_channel_id"]

        posted_1 = c.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "backlog msg"}, headers=headers,
        ).json()["data"]

        # 1) Backlog replay from since_id=0 catches the pre-connect message.
        with c.stream("GET", f"/api/projects/{pid}/stream?since_id=0", headers=headers) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            replayed = _read_sse_events(resp, want=10, deadline_s=5)
        backlog_msg_events = [e for e in replayed if e[0] == "message"]
        assert any(e[1]["id"] == posted_1["id"] for e in backlog_msg_events)
        last_seen_event_id = max(e[2] for e in replayed)

        # 2) Live push: post while connected; the event arrives without reconnect.
        with c.stream(
            "GET", f"/api/projects/{pid}/stream?since_id={last_seen_event_id}", headers=headers
        ) as resp:
            posted_2 = c.post(
                f"/api/projects/{pid}/conversations/{main}/messages",
                json={"body": "live msg"}, headers=headers,
            ).json()["data"]
            live = _read_sse_events(resp, want=10, deadline_s=10)
        live_hits = [e for e in live if e[0] == "message" and e[1]["id"] == posted_2["id"]]
        assert live_hits, f"live event for message {posted_2['id']} not received: {live}"
        last_seen_event_id = live_hits[0][2]

        # 3) Reconnect replay: events posted while disconnected are not lost.
        posted_3 = c.post(
            f"/api/projects/{pid}/conversations/{main}/messages",
            json={"body": "posted while offline"}, headers=headers,
        ).json()["data"]
        with c.stream(
            "GET", f"/api/projects/{pid}/stream?since_id={last_seen_event_id}", headers=headers
        ) as resp:
            replay = _read_sse_events(resp, want=10, deadline_s=5)
        assert any(
            e[0] == "message" and e[1]["id"] == posted_3["id"] for e in replay
        ), f"reconnect replay lost message {posted_3['id']}: {replay}"
