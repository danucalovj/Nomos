-- Allow mentions to originate from ticket comments as well as messages.
-- SQLite cannot relax a NOT NULL FK in place, so rebuild the table with
-- nullable message_id plus a nullable comment_id (exactly one must be set).

CREATE TABLE mentions_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    comment_id      INTEGER REFERENCES ticket_comments(id) ON DELETE CASCADE,
    target_agent_id INTEGER NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    CHECK ((message_id IS NULL) != (comment_id IS NULL))
);
INSERT INTO mentions_new (id, project_id, message_id, target_agent_id, seen, created_at)
    SELECT id, project_id, message_id, target_agent_id, seen, created_at FROM mentions;
DROP TABLE mentions;
ALTER TABLE mentions_new RENAME TO mentions;
CREATE INDEX idx_mentions_target ON mentions (project_id, target_agent_id, seen);
