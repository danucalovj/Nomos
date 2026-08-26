-- Project working directory + per-agent scratchpad and todos (issues #25, #26).

ALTER TABLE projects ADD COLUMN working_dir TEXT NOT NULL DEFAULT '';

ALTER TABLE agents ADD COLUMN scratchpad TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN scratchpad_updated_at TEXT;

CREATE TABLE agent_todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'todo'
               CHECK (status IN ('todo', 'in-progress', 'blocked', 'done', 'dropped')),
    priority   TEXT NOT NULL DEFAULT 'medium'
               CHECK (priority IN ('low', 'medium', 'high')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_agent_todos_agent ON agent_todos (agent_id, id);
