"""File monitor end-to-end on a real server: silent baseline, create/modify/
delete observations with diffs, self-report correlation, anomaly events."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import httpx

from .test_sse_live import live_server  # noqa: F401  (fixture reuse)


def _audit(c, pid, headers, **params):
    return c.get(f"/api/projects/{pid}/audit", params=params, headers=headers).json()["data"]["items"]


def _wait_for(fn, timeout=15.0, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return fn()


def test_monitor_end_to_end(live_server):  # noqa: F811
    base = live_server
    workdir = Path(tempfile.mkdtemp(prefix="audit-watch-"))
    (workdir / "pre_existing.txt").write_text("already here\n")

    # Failure-path teardown note: the live_server fixture SIGTERMs uvicorn at
    # module end, which cancels the watch task regardless of assertion
    # outcomes; the explicit delete below covers the success path.
    with httpx.Client(base_url=base, timeout=15) as c:
        assert c.post("/api/setup", json={"alias": "overseer"}).status_code == 201
        pid = c.post("/api/projects", json={"name": "watched"}).json()["data"]["id"]
        joined = c.post(f"/api/projects/{pid}/agents/join", json={"alias": "worker"}).json()["data"]
        auth = {"Authorization": f"Bearer {joined['api_key']}"}

        reg = c.post(f"/api/projects/{pid}/audit/watch", json={"path": str(workdir)})
        assert reg.status_code == 201, reg.text
        assert reg.json()["data"]["watch"]["active"] is True

        # Wait until the monitor has PROVABLY scanned at least once (the old
        # all-negative assertion passed identically when the watch never
        # started), then assert baseline silence.
        scanned = _wait_for(lambda: (
            c.get(f"/api/projects/{pid}/audit/watch", headers=auth)
            .json()["data"]["watch"] or {}).get("last_scan"))
        assert scanned, "watch never completed a scan"
        assert not _audit(c, pid, auth, source="monitor"), "baseline must be silent"

        # 1. Claimed change: self-report FIRST, then touch the file.
        c.post(f"/api/projects/{pid}/audit", headers=auth,
               json={"action": "file_edit", "target": "pre_existing.txt",
                     "summary": "appending a line"})
        (workdir / "pre_existing.txt").write_text("already here\nplus a new line\n")
        observed = _wait_for(lambda: _audit(c, pid, auth, source="monitor", target="pre_existing"))
        assert observed, "monitor must observe the modification"
        assert observed[0]["action"] == "file_edit"
        assert observed[0]["claimed"] is True
        assert "+plus a new line" in (observed[0]["diff"] or "")

        # 2. Unattributed change in a NESTED directory: no self-report, and
        # the monitor target must carry the relative sub-path.
        (workdir / "sub" / "dir").mkdir(parents=True)
        (workdir / "sub" / "dir" / "sneaky.py").write_text("print('nobody claimed this')\n")
        sneaky = _wait_for(lambda: _audit(c, pid, auth, source="monitor", target="sneaky"))
        assert sneaky and sneaky[0]["action"] == "file_create"
        assert sneaky[0]["claimed"] is False
        assert sneaky[0]["target"] == "sub/dir/sneaky.py"

        anomalies = _wait_for(lambda: [
            e for e in c.get(f"/api/projects/{pid}/events?since_id=0&types=audit_anomaly").json()["data"]["items"]
            if "sneaky.py" in e["payload"].get("path", "")
        ])
        assert anomalies, "unattributed change must raise an admin-targeted anomaly"

        # Agents must NOT receive the admin-targeted anomaly.
        agent_events = c.get(
            f"/api/projects/{pid}/events?since_id=0&types=audit_anomaly", headers=auth
        ).json()["data"]["items"]
        assert not agent_events

        # 3. Deletion observed.
        (workdir / "sub" / "dir" / "sneaky.py").unlink()
        deleted = _wait_for(lambda: [
            r for r in _audit(c, pid, auth, source="monitor", target="sneaky")
            if r["action"] == "file_delete"
        ])
        assert deleted

        # Coverage adds up.
        cov = c.get(f"/api/projects/{pid}/audit/coverage", headers=auth).json()["data"]
        assert cov["observed"] >= 3
        assert cov["correlated"] >= 1
        assert cov["unattributed"] >= 1
        assert any(a["actor"] == "worker" for a in cov["actors"])

        # Chain still verifies with monitor + self-report rows interleaved.
        assert c.get(f"/api/projects/{pid}/audit/verify", headers=auth).json()["data"]["ok"] is True

        c.delete(f"/api/projects/{pid}/audit/watch")
