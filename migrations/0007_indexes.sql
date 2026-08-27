-- Missing indexes (issue #28, verified with EXPLAIN QUERY PLAN: every listed
-- column produced a SCAN on its hot path). Existing partial indexes on
-- messages/conversations (WHERE type = ...) cannot serve the general
-- project-scoped queries used by stats, export, and cascade delete.

CREATE INDEX idx_attachments_comment ON attachments (comment_id);
CREATE INDEX idx_attachments_document ON attachments (document_id);
CREATE INDEX idx_attachments_project ON attachments (project_id);
CREATE INDEX idx_messages_project ON messages (project_id);
CREATE INDEX idx_messages_forwarded_from ON messages (forwarded_from_id);
-- (messages.parent_id already has a partial index in 0001 that serves
-- thread lookups; not duplicated here.)
CREATE INDEX idx_conversations_project ON conversations (project_id);
CREATE INDEX idx_ticket_links_source ON ticket_links (source_type, source_id);
CREATE INDEX idx_audit_correlated ON audit_log (correlated_id);
CREATE INDEX idx_message_edits_message ON message_edits (message_id);
CREATE INDEX idx_ticket_comments_parent ON ticket_comments (parent_id);
CREATE INDEX idx_mentions_message ON mentions (message_id);
CREATE INDEX idx_mentions_comment ON mentions (comment_id);
CREATE INDEX idx_saved_items_message ON saved_items (message_id);
CREATE INDEX idx_reactions_project ON message_reactions (project_id);
CREATE INDEX idx_agent_todos_project ON agent_todos (project_id);

-- Redundant with the UNIQUE(message_id, actor_agent_id, emoji) implicit index.
DROP INDEX IF EXISTS idx_reactions_message;
