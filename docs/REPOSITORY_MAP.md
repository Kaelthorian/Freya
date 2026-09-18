# Repository map

Agent Control Center is a local Ollama agent platform with a persistent web UI,
bounded task runtime and workspace-scoped programming tools.

## Directory map

```text
.
├── control_center/           web API, scheduler, workers, tools and SQLite
│   ├── __main__.py           recovery, Planner/Evaluator configuration, server and instance lock
│   ├── http.py / api.py      HTTP/SSE adapter and application routes
│   ├── runtime.py            queue, workspace selection and process lifecycle
│   ├── planner.py            plan schema, Ollama adapter, validation and explicit offline fallback
│   ├── agent_selector.py     deterministic capability gates, scoring and explainable ranking
│   ├── execution_graph.py    deterministic DAG state transitions and dependency release
│   ├── evaluator.py          evidence-first checks, schema and tool-free Ollama adapter
│   ├── orchestrator.py       atomic lifecycle, bounded graph scheduling, cancellation and integration
│   ├── worker.py             bounded Ollama/tool loop and per-agent policy
│   ├── tools.py              workspace-scoped filesystem, command and Git tools
│   ├── transport.py          non-redirecting local Ollama HTTP client
│   ├── storage.py            transactional SQLite repository and metrics
│   ├── schema.sql            persistent tables and indexes
│   ├── config.py             agent defaults, catalogue and validation
│   ├── capabilities.py       capability registry and tool-to-action resolver
│   ├── policy.py             policy schema, legacy migration and engine
│   ├── skills.py             reusable Skill registry, validation and resolution
│   ├── agent_context.py      structured agent defaults, effective config and worker context
│   ├── presets.py            safe generic Programmer preset definitions
│   └── security.py           secret and private-thinking sanitization
├── frontend/                 dependency-free dark web client
│   ├── index.html / styles.css
│   ├── app.js / core.js      routing, state, API and SSE refresh
│   ├── views.js              dashboard and detail views
│   ├── dialogs.js            agent, Skill, task and workspace-folder forms
│   └── components.js / icons.js
├── tests/                    API, storage, runtime, policy, Skill and tool tests
├── data/                     generated database and automatic workspaces
└── docs/                     architecture, API and operating instructions
```

`data/` is generated and ignored by Git. User-selected workspaces can live
anywhere accessible to the local server and are never treated as source files
of this repository merely because an agent selects them.

## Main flow

1. `frontend/` calls `control_center/http.py`, which applies same-origin and
   loopback Host checks before dispatching to `api.py`.
2. `api.py` validates agents and browses local folders. Freya requests first go
   through the loopback Ollama adapter in `planner.py`; `orchestrator.py` stores
   the validated plan snapshot atomically. `execution_graph.py` releases ready
   tasks in plan order and `agent_selector.py` classifies and ranks an existing
   agent once for each ready task before delegation. A technical Runtime success
   enters `evaluating`; `evaluator.py` must accept it before dependencies unlock.
3. `runtime.py` resolves the
   selected workspace or creates an automatic one for the task.
4. `storage.py` stores an immutable configuration/tool snapshot. The scheduler
   waits for a worker slot and exclusive access to the agent and workspace.
5. `worker.py` calls local Ollama, resolves each tool request through
   `capabilities.py`, evaluates the immutable policy in `policy.py`, and only
   then dispatches to `tools.py` inside the configured workspace root.
6. The parent persists events, steps, metrics, approvals and terminal state. SSE clients
   replay changes using monotonic event IDs; WaitingForApproval blocks the worker
   until a durable once/task/deny resolution arrives.

## Where to make changes

| Change | Entry points and validation |
| --- | --- |
| Web endpoint or folder browsing | `control_center/api.py`, `http.py`, `tests/test_control_api.py` |
| Persistent field or metric | `schema.sql`, `storage.py`, `tests/test_control_storage.py` |
| Scheduling, workspaces, pause or cancellation | `runtime.py`, `tests/test_control_runtime.py` |
| Tool implementation | `tools.py`, runtime/tool tests |
| Capability mapping or authorization | `capabilities.py`, `policy.py`, `worker.py`, `tests/test_capabilities.py` |
| Agent identity, behavior or context | `agent_context.py`, `config.py`, `worker.py`, `tests/test_agent_context.py` |
| Reusable Skills or compatibility | `skills.py`, `storage.py`, `api.py`, `agent_context.py`, `tests/test_skills.py` |
| Structured plans, lifecycle or recovery | `planner.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `__main__.py`, `tests/test_planner.py` |
| Agent classification, scoring or selection snapshots | `agent_selector.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `tests/test_agent_selector.py` |
| DAG state, dependency scheduling or graph API | `execution_graph.py`, `orchestrator.py`, `storage.py`, `api.py`, `schema.sql`, `tests/test_execution_graph.py` |
| Semantic result evaluation or evaluation API | `evaluator.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `api.py`, `tests/test_evaluator.py` |
| Agent configuration validation | `config.py`, `tests/test_control_security.py` |
| Secret handling | `security.py`, security/runtime/storage tests |
| Web UI and orchestration status display | `frontend/app.js`, `core.js`, `views.js`, `dialogs.js`, `styles.css` |
| Commands or architecture | `README.md`, `docs/`, root and scoped `AGENTS.md` |

See [ARCHITECTURE.md](ARCHITECTURE.md), [DEVELOPMENT.md](DEVELOPMENT.md), and
[API.md](API.md).
