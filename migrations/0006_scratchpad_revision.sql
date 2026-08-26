-- Optional optimistic concurrency for the agent scratchpad (issue #26 polish).
ALTER TABLE agents ADD COLUMN scratchpad_revision INTEGER NOT NULL DEFAULT 0;
