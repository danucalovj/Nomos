"""Out-of-band file monitor (issue #17).

One asyncio task per registered watch, living in the single server process so
`./start.sh` stays the only command. Every 3s it snapshots the watched tree
(mtime, size, sha256), diffs against the previous snapshot, and appends
monitor records to the audit trail — with unified diffs for text files and a
silent first scan as the baseline. Observations with no recent matching
self-report raise `audit_anomaly` events targeted at the admin.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
from pathlib import Path
from typing import Any

from . import audit
from .db import transaction, utc_now
from .events import append_event, notify
from .logging_setup import kv, setup_logging

POLL_SECONDS = 3.0
MAX_TEXT_BYTES = 256 * 1024
IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", "data", ".pytest_cache"}
IGNORED_FILES = {".DS_Store"}
IGNORED_SUFFIXES = {".pyc"}

_tasks: dict[int, asyncio.Task] = {}
_last_scan: dict[int, str] = {}


def _ignored(path: Path) -> bool:
    if path.name in IGNORED_FILES or path.suffix in IGNORED_SUFFIXES:
        return True
    return any(part in IGNORED_DIRS for part in path.parts)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(
    root: Path, prev: dict[str, tuple[int, int, str, int]] | None = None
) -> dict[str, tuple[int, int, str, int]]:
    """Symlink-safe: directory symlinks are not followed and symlinked files
    are skipped, so a link planted inside the watched tree cannot pull
    outside content into the (project-readable) audit trail.

    Hashing is gated on cheap stats: a file whose (mtime_ns, size, ctime_ns)
    all match the previous snapshot reuses its digest instead of being
    re-read — without this, a watched tree with one large artifact is fully
    re-read every poll (issue #28 H5). ctime_ns is included because mtime can
    be restored deliberately (utimes) while ctime cannot, so a same-size edit
    with a forged mtime still gets re-hashed. Hashes stream in 1MB chunks."""
    prev = prev or {}
    snap: dict[str, tuple[int, int, str, int]] = {}
    if not root.is_dir():
        return snap
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if not _ignored(rel_dir / d)]
        for name in filenames:
            path = Path(dirpath) / name
            rel = rel_dir / name
            if _ignored(rel) or path.is_symlink():
                continue
            try:
                stat = path.stat()
                key = str(rel)
                known = prev.get(key)
                if (
                    known is not None
                    and known[0] == stat.st_mtime_ns
                    and known[1] == stat.st_size
                    and known[3] == stat.st_ctime_ns
                ):
                    snap[key] = known
                    continue
                snap[key] = (stat.st_mtime_ns, stat.st_size, _hash_file(path), stat.st_ctime_ns)
            except OSError:
                continue
    return snap


def _read_text(root: Path, rel: str) -> str | None:
    path = root / rel
    if path.is_symlink():
        return None
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            return None  # binary
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _unified_diff(rel: str, old: str | None, new: str | None) -> str | None:
    if old is None or new is None:
        return None
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}",
    )
    text = "".join(lines)
    return text or None


def _record_changes(
    project_id: int, changes: list[tuple[str, str, str, str | None, str | None]]
) -> None:
    """Blocking (call via to_thread): one write transaction per SCAN, not per
    file — a git checkout touching 500 files is one lock acquisition instead
    of 500 serial ones on the event loop (issue #28 H4)."""
    with transaction() as conn:
        for action, rel, summary, diff, digest in changes:
            claim_id = audit.find_recent_claim(conn, project_id, rel)
            detail: dict[str, Any] = {"sha256": digest} if digest else {}
            audit_id = audit.record(
                conn, project_id, "monitor", "monitor", "monitor", action, summary,
                target=rel, detail=detail, diff=diff, correlated_id=claim_id,
            )
            if claim_id is None:
                append_event(
                    conn, project_id, "audit_anomaly",
                    {"audit_id": audit_id, "path": rel,
                     "summary": f"Unattributed change: {rel} ({action})"},
                    target_agent_id=0,
                )


async def _watch_loop(project_id: int, root: Path) -> None:
    logger = setup_logging()
    logger.info("fsmonitor started %s", kv(project=project_id, path=root))
    # Silent baseline (the first scan is not an anomaly wall) — off-loop.
    baseline = await asyncio.to_thread(_snapshot, root)
    text_cache: dict[str, str | None] = {}
    cache_bytes = 0  # running total: the O(n)-sum-per-insert was quadratic

    def _cache_put(rel: str, text: str | None) -> None:
        nonlocal cache_bytes
        old = text_cache.pop(rel, None)
        cache_bytes -= len(old) if old else 0
        text_cache[rel] = text
        cache_bytes += len(text) if text else 0
        # Aggregate bound: evict oldest entries beyond ~32MB of cached text
        # (dict preserves insertion order; eviction only costs the diff for
        # that file's next change).
        while cache_bytes > 32 * 1024 * 1024 and len(text_cache) > 1:
            oldest = next(iter(text_cache))
            dropped = text_cache.pop(oldest)
            cache_bytes -= len(dropped) if dropped else 0

    def _cache_drop(rel: str) -> None:
        nonlocal cache_bytes
        old = text_cache.pop(rel, None)
        cache_bytes -= len(old) if old else 0

    def _load_baseline_texts() -> None:
        for rel in baseline:
            _cache_put(rel, _read_text(root, rel))

    await asyncio.to_thread(_load_baseline_texts)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            current = await asyncio.to_thread(_snapshot, root, baseline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — monitor must not die on FS weirdness
            logger.warning("fsmonitor scan failed %s", kv(project=project_id, error=exc))
            continue
        _last_scan[project_id] = utc_now()
        # Collect the whole scan's changes, then write them in ONE off-thread
        # transaction. On write failure the baseline does NOT advance, so the
        # changes are re-detected next poll instead of silently vanishing
        # from the trail (issue #28).
        changes: list[tuple[str, str, str, str | None, str | None]] = []
        cache_updates: list[tuple[str, str | None]] = []
        deleted: list[str] = []
        for rel, meta in current.items():
            try:
                if rel not in baseline:
                    new_text = await asyncio.to_thread(_read_text, root, rel)
                    diff = _unified_diff(rel, "", new_text) if new_text is not None else None
                    changes.append(("file_create", rel, f"File created ({meta[1]} bytes)", diff, meta[2]))
                    cache_updates.append((rel, new_text))
                elif baseline[rel][2] != meta[2]:
                    new_text = await asyncio.to_thread(_read_text, root, rel)
                    diff = await asyncio.to_thread(
                        _unified_diff, rel, text_cache.get(rel), new_text
                    )
                    changes.append(("file_edit", rel, f"File modified ({meta[1]} bytes)", diff, meta[2]))
                    cache_updates.append((rel, new_text))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad file must not kill the watch
                logger.warning("fsmonitor diff failed %s", kv(project=project_id, path=rel, error=exc))
        for rel in baseline:
            if rel not in current:
                changes.append(("file_delete", rel, "File deleted", None, None))
                deleted.append(rel)
        if changes:
            try:
                await asyncio.to_thread(_record_changes, project_id, changes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("fsmonitor record failed %s", kv(project=project_id, error=exc))
                continue  # retry the same changes next poll
            for rel, text in cache_updates:
                _cache_put(rel, text)
            for rel in deleted:
                _cache_drop(rel)
            await notify(project_id)
        baseline = current


def start_watch(project_id: int, path: str) -> None:
    stop_watch(project_id)
    _tasks[project_id] = asyncio.get_running_loop().create_task(
        _watch_loop(project_id, Path(path))
    )


def stop_watch(project_id: int) -> None:
    task = _tasks.pop(project_id, None)
    if task is not None:
        task.cancel()


def watch_status(project_id: int) -> dict[str, Any] | None:
    row = audit.get_watch(project_id)
    if row is None:
        return None
    root = Path(row["path"])
    active = project_id in _tasks and not _tasks[project_id].done()
    files = sum(1 for p in root.rglob("*") if p.is_file() and not _ignored(p.relative_to(root))) if root.is_dir() else 0
    return {"path": row["path"], "active": active, "files": files,
            "last_scan": _last_scan.get(project_id),
            "registered_at": row["created_at"]}


def start_all_watches() -> None:
    """Called from the app lifespan: resume persisted watches."""
    from .db import query_all

    for row in query_all("SELECT project_id, path FROM audit_watches"):
        start_watch(row["project_id"], row["path"])


def stop_all_watches() -> None:
    for pid in list(_tasks):
        stop_watch(pid)
