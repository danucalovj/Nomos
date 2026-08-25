-- AgentComms initial schema.
-- Conventions: timestamps are ISO 8601 UTC strings; booleans are 0/1 integers;
-- JSON payloads are stored as TEXT and validated in application code.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Exactly one row, id fixed at 1. Created by the first-time setup screen.
CREATE TABLE admin_identity (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    alias      TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#e0b040',
    avatar     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    description        TEXT NOT NULL DEFAULT '',
    archived           INTEGER NOT NULL DEFAULT 0,
    next_ticket_number INTEGER NOT NULL DEFAULT 1,
    -- JSON: {"ticket_statuses": [...], "system_messages_enabled": true}
    settings           TEXT NOT NULL DEFAULT '{}',
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    alias        TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'idle')),
    -- SHA-256 hex of the Bearer key; NULL after revocation.
    api_key_hash TEXT UNIQUE,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_seen    TEXT
);
CREATE UNIQUE INDEX idx_agents_alias ON agents (project_id, alias COLLATE NOCASE);

-- Channels and DMs share one table; DMs have NULL name and exactly two members.
CREATE TABLE conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type       TEXT NOT NULL CHECK (type IN ('channel', 'dm')),
    name       TEXT,
    topic      TEXT NOT NULL DEFAULT '',
    is_main    INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_conversations_channel_name
    ON conversations (project_id, name COLLATE NOCASE) WHERE type = 'channel';

-- agent_id 0 denotes the admin (no FK possible for the sentinel; agent rows
-- reference agents.id and are cleaned up in code when an agent is removed).
CREATE TABLE conversation_members (
    conversation_id      INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    agent_id             INTEGER NOT NULL DEFAULT 0,
    joined_at            TEXT NOT NULL,
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, agent_id)
);
CREATE INDEX idx_members_agent ON conversation_members (agent_id);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    author_type     TEXT NOT NULL CHECK (author_type IN ('agent', 'admin', 'system')),
    author_agent_id INTEGER,
    author_alias    TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'normal' CHECK (type IN ('normal', 'decision', 'system')),
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    edited_at       TEXT,
    deleted         INTEGER NOT NULL DEFAULT 0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    pinned_at       TEXT,
    pinned_by       TEXT
);
CREATE INDEX idx_messages_conversation ON messages (conversation_id, id);
CREATE INDEX idx_messages_parent ON messages (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX idx_messages_decisions ON messages (project_id, id) WHERE type = 'decision';

CREATE TABLE message_edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    prev_body  TEXT NOT NULL,
    edited_by  TEXT NOT NULL,
    edited_at  TEXT NOT NULL
);

-- target_agent_id 0 denotes the admin.
CREATE TABLE mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    target_agent_id INTEGER NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_mentions_target ON mentions (project_id, target_agent_id, seen);

CREATE TABLE attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    message_id  INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    comment_id  INTEGER REFERENCES ticket_comments(id) ON DELETE SET NULL,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mime_type   TEXT NOT NULL,
    uploader    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_attachments_message ON attachments (message_id);

CREATE TABLE tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    priority    TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    labels      TEXT NOT NULL DEFAULT '[]',
    assignee    TEXT,
    reporter    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (project_id, number)
);
CREATE INDEX idx_tickets_status ON tickets (project_id, status);

CREATE TABLE ticket_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES ticket_comments(id) ON DELETE SET NULL,
    author_type TEXT NOT NULL CHECK (author_type IN ('agent', 'admin', 'system')),
    author_alias TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at  TEXT
);
CREATE INDEX idx_ticket_comments ON ticket_comments (ticket_id, id);

-- Backlinks: "#N" written in a message or comment links to the ticket.
CREATE TABLE ticket_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('message', 'ticket_comment', 'document')),
    source_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (ticket_id, source_type, source_id)
);

CREATE TABLE board_columns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL,
    -- JSON array of ticket statuses this column maps to; first entry is the
    -- status applied when a card is dropped into the column.
    statuses   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_board_columns ON board_columns (project_id, position);

CREATE TABLE documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug             TEXT NOT NULL,
    title            TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (project_id, slug)
);

CREATE TABLE document_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (document_id, revision)
);

-- Append-only event log: the source of truth for SSE replay (since_id).
-- conversation_id scopes visibility (NULL = project-wide event);
-- target_agent_id (0 = admin) marks targeted events such as mentions.
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    conversation_id INTEGER,
    target_agent_id INTEGER,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_events_project ON events (project_id, id);

-- Full-text search (external-content tables kept in sync by triggers,
-- except documents_fts which is maintained in code on each revision write).
CREATE VIRTUAL TABLE messages_fts USING fts5(
    body, content='messages', content_rowid='id'
);
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER messages_fts_au AFTER UPDATE OF body ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO messages_fts (rowid, body) VALUES (new.id, new.body);
END;

CREATE VIRTUAL TABLE tickets_fts USING fts5(
    title, description, content='tickets', content_rowid='id'
);
CREATE TRIGGER tickets_fts_ai AFTER INSERT ON tickets BEGIN
    INSERT INTO tickets_fts (rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER tickets_fts_ad AFTER DELETE ON tickets BEGIN
    INSERT INTO tickets_fts (tickets_fts, rowid, title, description)
        VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER tickets_fts_au AFTER UPDATE OF title, description ON tickets BEGIN
    INSERT INTO tickets_fts (tickets_fts, rowid, title, description)
        VALUES ('delete', old.id, old.title, old.description);
    INSERT INTO tickets_fts (rowid, title, description) VALUES (new.id, new.title, new.description);
END;

CREATE VIRTUAL TABLE ticket_comments_fts USING fts5(
    body, content='ticket_comments', content_rowid='id'
);
CREATE TRIGGER ticket_comments_fts_ai AFTER INSERT ON ticket_comments BEGIN
    INSERT INTO ticket_comments_fts (rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER ticket_comments_fts_ad AFTER DELETE ON ticket_comments BEGIN
    INSERT INTO ticket_comments_fts (ticket_comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER ticket_comments_fts_au AFTER UPDATE OF body ON ticket_comments BEGIN
    INSERT INTO ticket_comments_fts (ticket_comments_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO ticket_comments_fts (rowid, body) VALUES (new.id, new.body);
END;

-- Standalone FTS for documents: rowid = documents.id, always holds the
-- current revision's title/body. Rewritten in code on every revision.
CREATE VIRTUAL TABLE documents_fts USING fts5(title, body);
