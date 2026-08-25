-- Slack-parity additions (issue #13): reactions, emoji usage, saved items,
-- profile fields (avatar / custom status), message forwarding + doc cards.
-- Conventions: ISO 8601 UTC text timestamps, 0/1 booleans, agent_id 0 = admin.

-- Emoji reactions on messages (thread replies included). v1 scope: messages
-- only, not ticket comments (no conversation scope for visibility/eventing;
-- extension path mirrors migration 0002 if agents ever need it).
CREATE TABLE message_reactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    emoji           TEXT NOT NULL,          -- shortcode without colons
    actor_agent_id  INTEGER NOT NULL,       -- 0 = admin
    actor_alias     TEXT NOT NULL,          -- denormalized, survives removal
    created_at      TEXT NOT NULL,
    UNIQUE (message_id, emoji, actor_agent_id)
);
CREATE INDEX idx_reactions_message ON message_reactions (message_id);

-- Frequently-used emoji per actor (drives picker ordering).
CREATE TABLE emoji_usage (
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor_agent_id  INTEGER NOT NULL,
    emoji           TEXT NOT NULL,
    uses            INTEGER NOT NULL DEFAULT 0,
    last_used       TEXT NOT NULL,
    PRIMARY KEY (project_id, actor_agent_id, emoji)
);

-- Personal saved-for-later bookmarks (Slack "Saved items").
CREATE TABLE saved_items (
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor_agent_id  INTEGER NOT NULL,       -- 0 = admin
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (actor_agent_id, message_id)
);
CREATE INDEX idx_saved_actor ON saved_items (project_id, actor_agent_id);

-- Profile: prebuilt avatar slug + Slack-style custom status.
ALTER TABLE agents ADD COLUMN avatar TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN status_text TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN status_emoji TEXT NOT NULL DEFAULT '';

-- Forwarding + document share cards.
ALTER TABLE messages ADD COLUMN forwarded_from_id INTEGER REFERENCES messages(id) ON DELETE SET NULL;
ALTER TABLE messages ADD COLUMN doc_ref TEXT;
