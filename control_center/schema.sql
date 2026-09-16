PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'Idle',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity TEXT,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_configs (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id),
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tools (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    available INTEGER NOT NULL,
    dangerous INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tools (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    tool_name TEXT NOT NULL REFERENCES tools(name),
    PRIMARY KEY (agent_id, tool_name)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    agent_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    workspace TEXT NOT NULL,
    config_json TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    agent_role TEXT NOT NULL DEFAULT '',
    agent_description TEXT NOT NULL DEFAULT '',
    agent_instructions TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    status TEXT NOT NULL DEFAULT 'Queued',
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    steps INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS execution_steps (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step_id TEXT NOT NULL,
    step_number INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Pending',
    timestamp TEXT NOT NULL,
    finished_at TEXT,
    tool TEXT,
    reason TEXT,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    duration_seconds REAL,
    attempt INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (task_id, step_id)
);
CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL,
    step_id TEXT,
    tool TEXT,
    status TEXT,
    error TEXT,
    duration_seconds REAL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    generated_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_created ON tasks(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_executions_status ON task_executions(status);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON log_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_agent_id ON log_events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_filters ON log_events(level, tool, timestamp);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    required_tools_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    skill_id TEXT NOT NULL REFERENCES skills(id),
    PRIMARY KEY (agent_id, skill_id)
);
CREATE TABLE IF NOT EXISTS orchestration_runs (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Queued',
    response TEXT NOT NULL DEFAULT '',
    error TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_delegations (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT REFERENCES tasks(id),
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Queued',
    result_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS orchestration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT,
    agent_id TEXT,
    task_id TEXT,
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_orch_events ON orchestration_events(orchestration_id,id);
