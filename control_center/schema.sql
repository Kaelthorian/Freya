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
    verification_json TEXT,
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
CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    capability TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    action_summary TEXT NOT NULL DEFAULT '',
    resource TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status_created ON approval_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approval_requests(task_id);

CREATE INDEX IF NOT EXISTS idx_executions_status ON task_executions(status);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON log_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_agent_id ON log_events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_filters ON log_events(level, tool, timestamp);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'General',
    version INTEGER NOT NULL DEFAULT 1,
    instructions TEXT NOT NULL DEFAULT '',
    procedures_json TEXT NOT NULL DEFAULT '[]',
    recommended_capabilities_json TEXT NOT NULL DEFAULT '[]',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'user',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    required_tools_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
    ,deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    skill_id TEXT NOT NULL REFERENCES skills(id),
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, skill_id)
);
CREATE TABLE IF NOT EXISTS orchestration_runs (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Queued',
    response TEXT NOT NULL DEFAULT '',
    error TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT,
    plan_schema_version INTEGER,
    plan_created_at TEXT,
    planning_metrics_json TEXT NOT NULL DEFAULT '{}',
    effective_plan_json TEXT,
    current_plan_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_selections (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
    planned_task_id TEXT NOT NULL,
    selected_agent_id TEXT REFERENCES agents(id),
    status TEXT NOT NULL,
    selector_version INTEGER NOT NULL,
    score INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_selections
    ON orchestration_selections(orchestration_id,created_at,id);
CREATE TABLE IF NOT EXISTS orchestration_task_nodes (
    orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
    plan_task_id TEXT NOT NULL,
    plan_order INTEGER NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    selected_agent_id TEXT REFERENCES agents(id),
    selection_id TEXT REFERENCES orchestration_selections(id),
    runtime_task_id TEXT REFERENCES tasks(id),
    delegation_id TEXT,
    evaluation_id TEXT,
    evaluation_status TEXT,
    recovery_action_id TEXT,
    attempt_prompt TEXT NOT NULL DEFAULT '',
    plan_revision INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    waiting_reason TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (orchestration_id, plan_task_id),
    UNIQUE (runtime_task_id)
);
CREATE INDEX IF NOT EXISTS idx_orch_task_nodes_state
    ON orchestration_task_nodes(orchestration_id,state,plan_order);
CREATE TABLE IF NOT EXISTS orchestration_evaluations (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL,
    plan_task_id TEXT NOT NULL,
    runtime_task_id TEXT NOT NULL REFERENCES tasks(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    attempt INTEGER NOT NULL,
    evaluator_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    snapshot_json TEXT NOT NULL,
    context_truncated INTEGER NOT NULL DEFAULT 0,
    deterministic INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (orchestration_id, plan_task_id)
        REFERENCES orchestration_task_nodes(orchestration_id, plan_task_id),
    UNIQUE (orchestration_id, plan_task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_orch_evaluations
    ON orchestration_evaluations(orchestration_id,created_at,id);
CREATE TABLE IF NOT EXISTS orchestration_execution_attempts (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL,
    plan_task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    selected_agent_id TEXT NOT NULL REFERENCES agents(id),
    selection_id TEXT NOT NULL REFERENCES orchestration_selections(id),
    runtime_task_id TEXT NOT NULL REFERENCES tasks(id),
    delegation_id TEXT NOT NULL,
    evaluation_id TEXT,
    recovery_action_id TEXT,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (orchestration_id, plan_task_id)
        REFERENCES orchestration_task_nodes(orchestration_id, plan_task_id),
    UNIQUE (orchestration_id, plan_task_id, attempt),
    UNIQUE (runtime_task_id)
);
CREATE INDEX IF NOT EXISTS idx_orch_attempts
    ON orchestration_execution_attempts(orchestration_id,plan_task_id,attempt);
CREATE TABLE IF NOT EXISTS orchestration_recovery_actions (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL,
    plan_task_id TEXT NOT NULL,
    source_attempt INTEGER NOT NULL,
    source_evaluation_id TEXT NOT NULL REFERENCES orchestration_evaluations(id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    instructions TEXT NOT NULL,
    exclude_agent_ids_json TEXT NOT NULL DEFAULT '[]',
    affected_task_ids_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    recovery_version INTEGER NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    plan_revision INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (orchestration_id, plan_task_id)
        REFERENCES orchestration_task_nodes(orchestration_id, plan_task_id),
    UNIQUE (orchestration_id, plan_task_id, source_attempt),
    UNIQUE (source_evaluation_id)
);
CREATE INDEX IF NOT EXISTS idx_orch_recoveries
    ON orchestration_recovery_actions(orchestration_id,created_at,id);
CREATE TABLE IF NOT EXISTS orchestration_plan_revisions (
    id TEXT PRIMARY KEY,
    orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
    revision INTEGER NOT NULL,
    source_recovery_action_id TEXT NOT NULL REFERENCES orchestration_recovery_actions(id),
    source_plan_task_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    superseded_task_ids_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (orchestration_id, revision),
    UNIQUE (source_recovery_action_id)
);
CREATE INDEX IF NOT EXISTS idx_orch_plan_revisions
    ON orchestration_plan_revisions(orchestration_id,revision);
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
CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT NOT NULL REFERENCES skills(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (skill_id, version)
);
CREATE TABLE IF NOT EXISTS skill_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_skill_versions ON skill_versions(skill_id,version);
