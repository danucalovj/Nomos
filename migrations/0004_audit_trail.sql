-- Audit trail (issue #17).
-- audit_log is APPEND-ONLY by policy: no update/delete endpoint exists for
-- anyone including the admin; the per-project sha256 chain (prev_hash /
-- entry_hash) makes mutation tamper-evident via GET .../audit/verify.

CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor         TEXT NOT NULL,             -- alias; server-derived, never client-supplied
    actor_type    TEXT NOT NULL CHECK (actor_type IN ('agent', 'admin', 'monitor', 'platform')),
    source        TEXT NOT NULL CHECK (source IN ('self_report', 'monitor', 'platform')),
    action        TEXT NOT NULL,             -- controlled vocabulary (server/audit.py)
    target        TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}',
    diff          TEXT,                      -- unified diff, capped; NULL when none
    correlated_id INTEGER,                   -- monitor <-> self_report link
    prev_hash     TEXT NOT NULL,
    entry_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL              -- server UTC
);
CREATE INDEX idx_audit_project ON audit_log (project_id, id);
CREATE INDEX idx_audit_target ON audit_log (project_id, target);

-- Registered file-monitor watches (one per project, v1).
CREATE TABLE audit_watches (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
