"""Typing indicators are ephemeral: SSE-only, no `id:` field, never persisted,
absent from the polled events feed."""
from __future__ import annotations

import threading
import time

import httpx

from .test_sse_live import live_server  # noqa: F401  (fixture reuse)


def test_typing_is_sse_only(live_server):  # noqa: F811
    base = live_server
    with httpx.Client(base_url=base, timeout=10) as c:
        assert c.post("/api/setup", json={"alias": "overseer"}).status_code == 201
        pid = c.post("/api/projects", json={"name": "typing-test"}).json()["data"]["id"]
        joined = c.post(
            f"/api/projects/{pid}/agents/join", json={"alias": "typist"}
        ).json()["data"]
        headers = {"Authorization": f"Bearer {joined['api_key']}"}
        main = joined["main_channel_id"]

        events_before = c.get(
            f"/api/projects/{pid}/events?since_id=0", headers=headers
        ).json()["data"]["last_event_id"]

        got: dict = {"lines": []}

        def listen():
            with c.stream(
                "GET", f"/api/projects/{pid}/stream?since_id={events_before}", headers=headers
            ) as resp:
                deadline = time.monotonic() + 8
                try:
                    for line in resp.iter_lines():
                        got["lines"].append(line)
                        if "typing" in line or time.monotonic() > deadline:
                            if any("data:" in ln for ln in got["lines"][-3:]):
                                break
                except httpx.ReadTimeout:
                    pass

        t = threading.Thread(target=listen)
        t.start()
        time.sleep(0.8)  # let the stream connect
        r = c.post(f"/api/projects/{pid}/conversations/{main}/typing", headers=headers)
        assert r.status_code == 200
        assert r.json()["data"]["expires_in"] == 6
        t.join(timeout=12)

        lines = got["lines"]
        typing_idx = next(i for i, ln in enumerate(lines) if ln.startswith("event:") and "typing" in ln)
        # The typing event block must contain data but NO id: field.
        block = lines[typing_idx : typing_idx + 3]
        assert any(ln.startswith("data:") and "typist" in ln for ln in block), block
        assert not any(ln.startswith("id:") for ln in block), block

        # Never persisted: the polled feed and the events table see nothing.
        after = c.get(f"/api/projects/{pid}/events?since_id={events_before}", headers=headers).json()[
            "data"
        ]
        assert all(e["type"] != "typing" for e in after["items"])
        assert after["last_event_id"] == events_before
