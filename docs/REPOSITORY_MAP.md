# Repository map

Agent Control Center is a local Ollama agent platform with a persistent web UI,
bounded task runtime and workspace-scoped programming tools.

## Directory map

```text
.
├── control_center/           web API, scheduler, workers, tools and SQLite
│   ├── __main__.py           Planner/Evaluator/Recovery/Integration configuration and server lock
│   ├── http.py / api.py      HTTP/SSE adapter and application routes
│   ├── runtime.py            queue, workspace selection and process lifecycle
│   ├── task_spec.py          canonical intent, clarification questions, revisions and deterministic rendering
│   ├── activity.py           persisted orchestration timeline, phase durations and performance read model
│   ├── task_analyst.py       legacy version-3 rewrite compatibility
│   ├── planner.py            semantic strategy and legacy plan schema compatibility
│   ├── plan_scope.py         semantic scope guard and bounded pre-resolution plan snapshot
│   ├── runtime_resources.py  global resource catalog, exact resolution and tool/capability compatibility
│   ├── plan_compiler.py      resource derivation, deterministic IDs, semantic dependencies and DAG validation
│   ├── agent_factory.py      dynamic agents, freya-core assignment, task policy and provenance
│   ├── agent_selector.py     deterministic capability gates, scoring and explainable ranking
│   ├── execution_graph.py    deterministic DAG state transitions and dependency release
│   ├── evaluator.py          criterion evidence checks, decision schema and tool-free Ollama adapter
│   ├── recovery.py           recovery decisions, validated replanning and log-grounded failure diagnosis
│   ├── integration.py        global verifier contract, direct-proof decision, append-only replanner and grounded result integrator
│   ├── integration_proof.py  bounded evidence catalog and explicit local-to-global criterion proof mapping
│   ├── integration_orchestrator.py integration lifecycle and verifier validation events
│   ├── integration_storage.py integration persistence and compatible revision-table migration
│   ├── orchestrator.py       atomic lifecycle, bounded graph scheduling, cancellation and integration
│   ├── worker.py             bounded Ollama/tool loop, action fingerprints, verification ledger and per-agent policy
│   ├── tools.py              workspace-scoped filesystem, command and applicability-aware Git tools
│   ├── sandbox.py            disposable workspace copy and restricted Docker execution
│   ├── transport.py          streamed Ollama chat, per-component limits, provider health and call telemetry
│   ├── storage.py            transactional SQLite repository and metrics
│   ├── schema.sql            persistent tables and indexes
│   ├── config.py             agent defaults, catalogue and validation
│   ├── capabilities.py       capability registry and tool-to-action resolver
│   ├── policy.py             policy schema, legacy migration and engine
│   ├── skills.py             freya-core definition, tool validation and context rendering
│   ├── agent_context.py      structured agent defaults, effective config and policy/tool-aware worker context
│   ├── presets.py            manual/legacy Programmer, Task Analyst, QA Tester and Code Auditor presets
│   └── security.py           secret and private-thinking sanitization
├── frontend/                 dependency-free dark web client
│   ├── index.html / styles.css
│   ├── app.js / core.js      routing, state, API and SSE refresh
│   ├── views.js              dashboard and detail views
│   ├── dialogs.js            agent, Skill, task and workspace-folder forms
│   └── components.js / icons.js
├── tests/                    API, storage, runtime, activity, transport, policy, Skill and tool tests
├── sandbox/Dockerfile        local Python, pytest, Ruff and Git sandbox image
├── data/
│   ├── agents/              versioned, importable pipeline-agent definitions
│   └── (runtime files)      ignored databases, logs, locks and workspaces
└── docs/                     architecture, API and operating instructions
```

Only `data/agents/*.json` is versioned. These files contain shareable agent
configuration without runtime IDs, task history, prompts, metrics or secrets
and can be imported from the Agents page. All other `data/` content is generated
and ignored by Git. User-selected workspaces can live anywhere accessible to the
local server and are never treated as source files merely because an agent
selects them.

## Main flow

1. `frontend/` calls `control_center/http.py`, which applies same-origin and
   loopback Host checks before dispatching to `api.py`.
2. `api.py` validates agents and receives task requests. Production
   `orchestrator.py` asks `task_spec.py` to derive a canonical Task Spec.
   High-impact gaps persist `NeedsClarification` questions and pause the
   same run; `POST /orchestrations/{id}/clarifications` stores answers and
   resumes analysis. `task_spec.py` reduces proposed questions against answered
   fields and deterministic material gaps, assigns durable question IDs, and
   bounds clarification to three answered rounds. `storage.py` preserves
   versioned answers and accepts exact submission replays. Before planning,
   `orchestrator.py` builds a fresh
   `RuntimeResourceCatalog` from `capabilities.py`, `Toolbox` schemas and the
   enabled Skill registry in SQLite. `planner.py` receives compact descriptions
   and dynamic closed enums for those resources; exact IDs and declared unique
   aliases are validated after `plan_scope.py` removes unrequested external
   work and invented external criteria. It preserves authoritative Task Spec
   checks and rewires dependencies. `plan_compiler.py` then assigns internal IDs and
   builds the durable plan and DAG. Planner decides
   whether QA or audit is needed. The legacy `task_analyst.py` contract is
   retained for injected compatibility adapters.
   `execution_graph.py` releases ready tasks in plan order. Low-risk generic file
   and Python artifact requests remain one task. A Python console calculator
   that requires interactive input is normalized to one implementation node
   with file read/create access and one dependent QA node that alone receives
   Python execution. QA runs the exact bounded stdin case `3` and `5` once.
   File-only implementation stops on a matching read-back; single-case QA stops
   when that one command supplies evidence for every planned criterion.
   For each ready task,
   `agent_factory.py` creates one validated ephemeral agent whose complete policy
   comes only from `required_capabilities`. Every dynamic agent receives
   `freya-core`; its seven declared tools are checked against `Toolbox` at
   startup. Planner Skill preferences are ignored with a warning. Tool
   availability never grants a capability: Policy limits the worker schemas
   and evaluates each invocation.
   The resource resolver checks tool IDs against the global Toolbox
   catalog. Tool proposals cannot create authority by themselves: a specific
   semantic operation must justify any inferred capability. Unneeded
   `run_command` is removed; an ambiguous required command fails with
   `ToolCapabilityMismatch`. The policy engine still
   decides each action.
   `tools.py` resolves filesystem paths inside the assigned workspace. For
   Python, pytest, unittest, py_compile, Ruff and read-only Git, it sends an
   allowlisted command to `sandbox.py`; Docker receives only a disposable
   workspace copy. `run_command` changes are discarded. Persistent edits use
   `write_file` or `edit_file` through Policy. A missing Docker daemon/image
   produces `SandboxUnavailable`.
   `agent_selector.py` then validates and classifies that generated
   candidate before delegation. A technical Runtime success
   enters `evaluating`; `evaluator.py` must accept it before dependencies unlock.
   A non-accepted evaluation enters bounded recovery; retries are reselected and
   recorded as new attempts, while deterministic DAG scope limits replanning to
   the recovery source and never-started descendants. Replanner and Storage both
   reject mutation of active, historical or independent work before committing
   the effective plan, without changing the original plan snapshot.
   Runtime command output that directly satisfies an observable completion
   criterion is promoted to bounded verification evidence and can satisfy that
   exact criterion deterministically. Invalid structured final text receives
   one repair attempt; runtime exceptions instead build a factual contract from
   the action ledger without model repair. `task.result_contract` logs sanitized
   diagnostics, while `freya.evaluation.completed` logs criterion decisions and
   bounded input evidence for non-accepted outcomes.
   The worker also stops duplicate writes after successful read-back and reports
   missing-file reads without repeating them unchanged; semantic recovery then
   fails deterministic absent-artifact inputs instead of rotating agents.
   Recovery carries bounded workspace state into the next attempt and derives
   only read inspection for a new agent; it never grants overwrite.
   When every active effective task is accepted, the run enters `Integrating`.
   `integration.py` checks the original global criteria against a bounded,
   fingerprinted snapshot. Only global `accepted` creates the grounded final
   response and `Success`. A bounded global gap may append new tasks without
   changing existing work; those tasks return through the same Selector,
   policy, Runtime, Evaluator and Recovery path.
   If a task graph still terminates with a failure, `orchestrator.py` performs
   one tool-free diagnosis over bounded, sanitized events already persisted by
   `storage.py`. `recovery.py` validates that the report cites only supplied log
   IDs, or builds a deterministic report when offline or the model call fails.
3. `runtime.py` resolves the
   selected workspace or creates an automatic one for the task.
4. `storage.py` stores an immutable configuration/tool snapshot. The scheduler
   waits for a worker slot and exclusive access to the agent and workspace.
5. `worker.py` builds the AVAILABLE TOOLS prompt from the same effective
   schemas sent to Ollama, resolves each request through `capabilities.py`,
   evaluates the immutable policy in `policy.py`, and only then dispatches to
   `tools.py` inside the configured workspace root. Unknown and unavailable
   tools are distinct feedback classes; deterministic denials are not retried.
6. The parent persists events, steps, metrics, approvals and terminal state. SSE clients
   replay changes using monotonic event IDs; WaitingForApproval blocks the worker
   until a durable once/task/deny resolution arrives.

## Where to make changes

| Change | Entry points and validation |
| --- | --- |
| Web endpoint or folder browsing | `control_center/api.py`, `http.py`, `tests/test_control_api.py` |
| Freya Activity timeline or performance calculation | `control_center/activity.py`, `storage.py`, `api.py`, `frontend/views.js`, `frontend/app.js`, `frontend/styles.css`, `tests/test_activity.py`, `tests/test_control_api.py` |
| Persistent field or metric | `schema.sql`, `storage.py`, `tests/test_control_storage.py` |
| Scheduling, workspaces, pause or cancellation | `runtime.py`, `tests/test_control_runtime.py` |
| Ollama streaming, timeouts, output limits or provider telemetry | `transport.py`, adapter callers in `task_analyst.py`, `planner.py`, `worker.py`, `evaluator.py`, `recovery.py`, `integration.py`, `tests/test_transport.py` |
| Tool implementation, dynamic tool prompt, Docker isolation or controlled stdin | `tools.py`, `sandbox.py`, `sandbox/Dockerfile`, `worker.py`, `agent_context.py`, `tests/test_tools.py`, `tests/test_core_sandbox.py` |
| Runtime evidence, structured response diagnostics or repeated policy denial | `worker.py`, `evaluator.py`, `recovery.py`, `tests/test_control_runtime.py`, `tests/test_evaluator.py`, `tests/test_recovery.py` |
| Capability mapping or authorization | `capabilities.py`, `policy.py`, `worker.py`, `tests/test_capabilities.py` |
| Planner scope, resource descriptions, global tool IDs, capability compatibility, aliases or unsupported needs | `plan_scope.py`, `runtime_resources.py`, `capabilities.py`, `tools.py`, `skills.py`, `planner.py`, `plan_compiler.py`, `agent_factory.py`, `orchestrator.py`, `tests/test_plan_scope.py`, `tests/test_runtime_resources.py`, `tests/test_task_spec.py` |
| Agent identity, behavior or context | `agent_context.py`, `config.py`, `worker.py`, `tests/test_agent_context.py` |
| Universal Skill definition, startup tool validation or automatic assignment | `skills.py`, `storage.py`, `runtime_resources.py`, `agent_factory.py`, `agent_context.py`, `tests/test_core_sandbox.py` |
| Structured plans and lifecycle | `planner.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `__main__.py`, `tests/test_planner.py` |
| Canonical Task Analyst contract, normalization or repair | `task_spec.py`, `orchestrator.py`, `storage.py`, `tests/test_task_spec.py` |
| Legacy prompt rewrite or task kinds | `task_analyst.py`, `planner.py`, `config.py`, `frontend/dialogs.js`, `tests/test_task_analyst.py`, `tests/test_planner.py` |
| Pipeline agent presets or QA routing | `presets.py`, `skills.py`, `api.py`, `planner.py`, `tests/test_agent_presets.py`, `tests/test_control_api.py` |
| Semantic recovery, retries or plan revisions | `recovery.py`, `orchestrator.py`, `execution_graph.py`, `agent_selector.py`, `storage.py`, `schema.sql`, `api.py`, `tests/test_recovery.py` |
| Recovery workspace context or safe retry capabilities | `orchestrator.py`, `recovery.py`, `agent_factory.py`, `agent_selector.py`, `tests/test_agent_factory.py`, `tests/test_recovery.py` |
| Terminal failure diagnosis, no-progress causes or merged orchestration logs | `recovery.py`, `orchestrator.py`, `storage.py` (normalized actor/workspace traces), `worker.py`, `__main__.py`, `frontend/views.js`, `frontend/components.js`, `tests/test_recovery.py`, `tests/test_control_storage.py`, `tests/test_control_runtime.py` |
| Dynamic agent construction, freya-core assignment, provenance or lifecycle | `agent_factory.py`, `orchestrator.py`, `skills.py`, `storage.py`, `config.py`, `tests/test_agent_factory.py`, `tests/test_core_sandbox.py` |
| Agent classification, scoring or selection snapshots | `agent_selector.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `tests/test_agent_selector.py` |
| DAG state, dependency scheduling or graph API | `execution_graph.py`, `orchestrator.py`, `storage.py`, `api.py`, `schema.sql`, `tests/test_execution_graph.py` |
| Semantic result evaluation or evaluation API | `evaluator.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `api.py`, `tests/test_evaluator.py` |
| Global integration, append-only recovery or final response | `integration.py`, `integration_orchestrator.py`, `integration_storage.py`, `orchestrator.py`, `schema.sql`, `api.py`, `tests/test_integration.py` |
| Agent configuration validation | `config.py`, `tests/test_control_security.py` |
| Secret handling | `security.py`, security/runtime/storage tests |
| Web UI and orchestration status display | `frontend/app.js`, `core.js`, `views.js`, `dialogs.js`, `styles.css` |
| Commands or architecture | `README.md`, `docs/`, root and scoped `AGENTS.md` |

See [ARCHITECTURE.md](ARCHITECTURE.md), [DEVELOPMENT.md](DEVELOPMENT.md), and
[API.md](API.md).
