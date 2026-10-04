# Repository map

Agent Control Center is a local Ollama agent platform with a persistent web UI,
bounded task runtime and workspace-scoped programming tools.

## Directory map

```text
.
├── control_center/           web API, scheduler, workers, tools and SQLite
│   ├── __main__.py           CLI, exclusive data-directory lock and system model configuration
│   ├── http.py / api.py      HTTP/SSE adapter and application routes
│   ├── runtime.py            queue, workspace selection and process lifecycle
│   ├── task_spec.py          canonical intent, clarification questions, revisions and deterministic rendering
│   ├── activity.py           persisted orchestration timeline, phase durations and performance read model
│   ├── task_analyst.py       legacy version-3 rewrite compatibility
│   ├── planner.py            semantic strategy, bounded repair with duplicate rejection and legacy plan schema compatibility
│   ├── plan_scope.py         semantic scope guard and bounded pre-resolution plan snapshot
│   ├── plan_evidence.py      criterion evidence/authority classification, shared mechanical grammar and capability compatibility
│   ├── plan_granularity.py   conservative mechanical-prerequisite fusion before runtime IDs and ownership
│   ├── runtime_resources.py  global resource catalog, exact resolution and tool/capability compatibility
│   ├── plan_compiler.py      scope/granularity/evidence reconciliation, write-overlap guard, ownership, resources, IDs and DAG validation
│   ├── worker_assignment.py deterministic logical Worker groups for compiled tasks
│   ├── cross_task.py         exact file ownership validation and bounded reusable-intent matching
│   ├── agent_factory.py      dynamic agents, freya-core assignment, task policy and provenance
│   ├── agent_selector.py     deterministic capability gates, scoring and explainable ranking
│   ├── execution_graph.py    deterministic DAG state transitions and dependency release
│   ├── project_state.py      per-orchestration artifact metadata, bounded context snapshots and accepted updates
│   ├── final_state.py        parent-owned current file snapshot and authoritative verification fact extraction
│   ├── evaluator.py          final-state criterion binding, mechanical decisions, semantic review and tool-free Ollama adapter
│   ├── recovery.py           origin-aware Worker recovery, read-only evidence gathering, validated replanning and log-grounded failure diagnosis
│   ├── integration.py        global verifier contract, direct-proof decision, append-only replanner and grounded result integrator
│   ├── integration_proof.py  bounded evidence catalog and explicit local-to-global criterion proof mapping
│   ├── integration_orchestrator.py integration lifecycle and verifier validation events
│   ├── integration_storage.py integration persistence and compatible revision-table migration
│   ├── orchestrator.py       atomic lifecycle, bounded graph scheduling, cancellation and integration
│   ├── worker.py             bounded Ollama/tool loop, action fingerprints, provenance-bearing verification ledger and per-agent policy
│   ├── worker_finalization.py strict tool-free execution termination contract and historical evidence context
│   ├── tools.py              workspace-scoped filesystem, command and applicability-aware Git tools
│   ├── python_execution.py   lazy orchestration venv, dependencies, processes and cleanup
│   ├── sandbox.py            disposable Docker copy for read-only Git inspection
│   ├── transport.py          streamed Ollama chat, per-component limits, provider health and call telemetry
│   ├── llm_trace.py          redacted, bounded model-call tracing and debug prompt capture
│   ├── settings.py           central startup debug/budget settings, local/env precedence and spawn snapshot
│   ├── verification_cases.py bounded case contracts, exact surface reference matcher, compatible QA grouping and batch expansion
│   ├── storage.py            transactional SQLite repository and metrics
│   ├── schema.sql            persistent tables and indexes
│   ├── config.py             agent defaults, catalogue and validation
│   ├── capabilities.py       capability registry and tool-to-action resolver
│   ├── policy.py             policy schema, legacy migration and engine
│   ├── skills.py             freya-core definition, tool validation and context rendering
│   ├── agent_context.py      structured agent defaults, effective config and policy/tool-aware worker context
│   ├── agent.md              global coordination rules for dynamic Workers
│   ├── plan_context.py       bounded read-only full-plan snapshot and current-task scope
│   ├── presets.py            creatable Programmer, QA Tester and Code Auditor presets
│   └── security.py           secret and private-thinking sanitization
├── frontend/                 dependency-free dark web client
│   ├── index.html / styles.css
│   ├── app.js / core.js      routing, state, API and SSE refresh
│   ├── views.js              dashboard and detail views
│   ├── dialogs.js            agent, Skill, task and workspace-folder forms
│   └── components.js / icons.js
├── tests/                    API, storage, runtime, activity, transport, policy, Skill and tool tests
├── sandbox/Dockerfile        optional legacy Docker image for Git inspection
├── data/
│   ├── agents/              versioned pipeline-agent presets and legacy Task Analyst export
│   ├── runtime_envs/        ignored orchestration venvs and dependency state (generated)
│   └── (runtime files)      ignored databases, logs, locks and workspaces
└── docs/                     architecture, API and operating instructions
```

Only `data/agents/*.json` is versioned. The Programmer, QA Tester and Code
Auditor files are shareable agent presets; `task-analyst.json` is retained only
as a marked legacy compatibility export and is not offered as a creatable preset.
These files contain no runtime IDs, task history, prompts, metrics or secrets.
All other `data/` content is generated
and ignored by Git. User-selected workspaces can live anywhere accessible to the
local server and are never treated as source files merely because an agent
selects them.

## Main flow

1. `frontend/` calls `control_center/http.py`, which applies same-origin and
   loopback Host checks before dispatching to `api.py`.
2. `api.py` validates agents and receives task requests. Production
   `orchestrator.py` runs the built-in, independently configured Task Analyst
   in `task_spec.py` to derive a canonical Task Spec; no persisted agent or
   preset participates in this stage.
   High-impact gaps persist `NeedsClarification` questions and pause the
   same run; `POST /orchestrations/{id}/clarifications` stores answers and
   resumes analysis. `task_spec.py` reduces proposed questions against answered
   fields and deterministic material gaps, assigns durable question IDs, and
   bounds clarification to three answered rounds. `storage.py` preserves
   versioned answers and accepts exact submission replays. Before planning,
   `orchestrator.py` builds a fresh
   `RuntimeResourceCatalog` from `capabilities.py` and `Toolbox` schemas.
   `planner.py` receives only semantic operation IDs and descriptions; it emits
   task meaning, kind, dependencies, outcomes, criteria, durable `owned_paths` and `write_targets`.
   `planner.py` keeps an orchestration-local `RejectedSemanticPlanRegistry` and
   runs `SemanticPlanRepairGuard` on every repair response before Plan Compiler.
   It rejects repeats across the full history, checks a cause-specific
   `required_plan_delta`, and permits at most three bounded Planner repairs;
   only a materially corrected and Compiler-validated result proceeds to
   runtime. `RepeatedSemanticPlanError` remains a secondary defense.
   `plan_compiler.py` calls `plan_scope.py` to remove work not supported by
   explicit Task Spec intent, then verifies unsupported claims and assigns one
   permanent plan-task owner per concrete write path. Before ID allocation, equivalent
   sibling Python QA tasks with typed independent cases can be grouped; their criteria
   and dependent links are preserved. Tasks with writes, ordered cases or distinct
   scripts remain separate. It derives
   an exact runtime write grant when a task declares a foreign target and depends
   transitively on its permanent owner; this never transfers ownership. It is the
   only component that maps semantic operations to concrete capabilities and tools,
   assigns runtime IDs and validates the owner map and DAG. Recovery and Integration
   keep existing owners fixed when they revise the compiled plan. Semantic Plan
   schema is version 3; compiled plan schema is
   version 4. The legacy `task_analyst.py` contract is retained for injected
   compatibility adapters.
   `execution_graph.py` releases ready tasks in plan order. Low-risk generic file
   and Python artifact requests remain one task. A Python console calculator
   that requires interactive input is normalized to one implementation node
   with file read/create access and one dependent QA node that alone receives
   Python execution. QA runs the exact bounded stdin case `3` and `5` once.
   File-only implementation stops on a matching read-back; single-case QA stops
   when that one command supplies evidence for every planned criterion.
   `worker_assignment.py` validates the compiler's authoritative assignment
   map. `agent_factory.py` creates one stable logical Worker per assignment;
   each task activation replaces its policy, tools, write scope, and criteria
   with that task's compiled requirements. Legacy plans without assignments
   keep the task-specific agent path. The factory also installs the internal,
   read-only `project_context` capability for generated agents. Modification
   and overwrite tasks derive `filesystem.read` and `read_file` through the
   catalog, including visibility of foreign-owned workspace artifacts;
   ownership still scopes writes. Pure creation does not imply read. Every dynamic agent receives
   `freya-core`; its seven declared tools are checked against `Toolbox` at
   startup. Planner Skill preferences are ignored with a warning. The scheduler
   still gates each task on DAG dependencies and runs at most one task in an
   assignment at once, while distinct Workers can run in parallel. Tool
   availability never grants a capability: Policy limits the active task's
   schemas and evaluates each invocation.
   The Runtime Resource Catalog provides one canonical semantic-operation to
   capability to tool mapping. Unknown operations and unsupported actions fail
   closed. The policy engine still decides each invocation.
   The Worker records current file hashes before existing-file writes and
   records zero-write Runtime candidates only when declared targets were
   read. `tools.py` compares `edit_file` candidate bytes before writing;
   `worker.py` records diffs and mutations only for material changes and stops
   repeated no-op edits on one artifact. `worker_finalization.py` handles the
   single tool-free terminal decision after no-progress, without semantic
   evaluation or additional actions. `orchestrator.py` adds a bounded
   snapshot of every effective plan task, current graph status, the active task
   scope, accepted summaries and relevant artifacts from the same assignment,
   direct predecessor outcome summaries, and current-task non-goals to each
   execution prompt. Ollama calls do not hold a persistent conversation
   session; this deterministic context is the continuity mechanism. A plan
   revision can extend the same Worker lineage when prior ordered task IDs
   remain a prefix. `tests/test_worker_assignments.py` covers identity, policy
   refresh, dependency gating, parallel assignments, recovery and extension.
   `agent_context.py` includes `agent.md` for generated agents;
   ProjectState verifies reported symbols after acceptance.
   `tools.py` resolves filesystem paths inside the assigned workspace. For
   Python, pytest, unittest, py_compile and Ruff, it sends the policy-approved
   command to `python_execution.py`, using an orchestration venv and workspace
   cwd. Read-only Git retains the Docker copy in `sandbox.py`. Persistent edits use
   `write_file` or `edit_file` through Policy. `write_file` creates parent
   directories for nested file paths and reports `ParentPathIsFile` when a file
   blocks one; the worker stops that call batch so it can change strategy. A
   missing Python/dependency setup produces unavailable environment evidence;
   Docker errors apply only to Git inspection. See [Python execution](PYTHON_EXECUTION.md).
   `agent_selector.py` then validates and classifies that generated
   candidate before delegation. Runtime `Success` enters `runtime_success` and
   releases DAG dependencies. `orchestrator.py` waits until all active Tasks in
   one compiled Worker Assignment finish, calls `final_state.build_final_state`
   for current files and authoritative verification facts, then calls `evaluator.py` once for that
   Worker. One immutable evaluation is referenced by every assigned Task node;
   failed criteria retain their origin Task so Recovery can select the right
   subgraph. A single Worker evaluation may also satisfy identical global
   criteria when it carries direct proof; multi-Worker plans still use the
   Global Verifier.
   A non-accepted evaluation enters bounded recovery; retries are reselected and
   recorded as new attempts, while deterministic DAG scope limits replanning to
   the recovery source and never-started descendants. Replanner compiles new
   semantic operations through the Plan Compiler and cannot exceed the
   superseded tasks' existing capability budget. It and Storage reject mutation
   of protected, historical or independent work before committing the effective
   plan, without changing the original snapshot.
   Runtime command output that directly satisfies an observable completion
   criterion is promoted to bounded verification evidence; Evaluator interprets
   its meaning semantically against the final result. Invalid structured final text receives
   one repair attempt; runtime exceptions instead build a factual contract from
   the action ledger without model repair. `task.result_contract` logs sanitized
   diagnostics, while `freya.evaluation.completed` logs Python-aggregated
   criterion decisions and bounded input evidence for non-accepted outcomes.
   Evaluator collects and binds full current facts by criterion ID before making
   decisions; `evaluator.evidence_prepared` records those IDs before deterministic
   evaluation or an LLM call. Only unresolved semantic criteria go to Ollama,
   each with its associated file/case facts and an explicit criterion-evidence
   map, not a global evidence bag. Exact and strong semantic/structural local-to-
   global links reuse the same fact IDs without extra executions. Bare
   existence/readability and explicitly mechanical run results remain
   deterministic; expected cases gate both mechanical and semantic criteria.
   Ledger case/step events reconstruct facts if final actions are absent.
   `tests/test_evidence_recovery.py` exercises the real Worker/Python pipeline,
   per-criterion evidence, missing-only case recovery and idempotent persistence.
   Binding errors and unchanged evidence stop collection instead of rerunning QA.
   Unavailable resources route to Orchestrator for
   resource review without granting permissions. Repair plus one retry is bounded
   to four calls on the same immutable final snapshot.
   The worker also stops byte-identical duplicate writes without requiring read-back and reports
   missing-file reads without repeating them unchanged; semantic recovery then
   fails deterministic absent-artifact inputs instead of rotating agents.
   Recovery carries bounded workspace state into the next attempt and
   reselects under the same compiled task policy; it never grants a new
   capability or overwrite.
   When every active effective task is accepted, the run enters `Integrating`.
   `integration.py` checks the original global criteria against a bounded,
   fingerprinted snapshot. Only global `accepted` creates the grounded final
   response and `Success`. A bounded global gap may append new semantic tasks;
   the Plan Compiler maps them within the existing capability budget without
   changing existing work. Those tasks return through the same Selector,
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
   evaluates the immutable policy in `policy.py`, checks task write ownership,
   and only then dispatches to `tools.py` inside the configured workspace root.
   Unknown and unavailable tools are distinct feedback classes; deterministic
   denials are not retried. Repeated identical incomplete cross-task requests
   stop as no-progress after the second attempt.
   `project_state.py` supplies generated workers a bounded metadata snapshot
   and a read-only metadata query; only the parent persists it, and only after
   Evaluator acceptance of grounded worker candidates.
6. The parent persists events, steps, metrics, approvals and terminal state. SSE clients
   replay changes using monotonic event IDs; WaitingForApproval blocks the worker
   until a durable once/task/deny resolution arrives.

## Where to make changes

| Change | Entry points and validation |
| --- | --- |
| Web endpoint or folder browsing | `control_center/api.py`, `http.py`, `tests/test_control_api.py` |
| Freya Activity timeline or performance calculation | `control_center/activity.py`, `storage.py`, `api.py`, `frontend/views.js`, `frontend/app.js`, `frontend/styles.css`, `tests/test_activity.py`, `tests/test_control_api.py` |
| Persistent field or metric | `schema.sql`, `storage.py`, `tests/test_control_storage.py` |
| Orchestration project metadata, artifact revisions or context queries | `project_state.py`, `storage.py`, `schema.sql`, `worker.py`, `agent_factory.py`, `orchestrator.py`, `tests/test_project_state.py` |
| Scheduling, workspaces, pause or cancellation | `runtime.py`, `tests/test_control_runtime.py` |
| Startup configuration, debug propagation or active orchestration budget | `settings.py`, `__main__.py`, `runtime.py`, `orchestrator.py`, `tests/test_settings.py`, `tests/test_run_contracts.py`, `docs/DEBUG_LLM_PROMPTS.md` |
| Independent test inputs or multiline interactive sessions | `verification_cases.py`, `planner.py`, `plan_compiler.py`, `agent_factory.py`, `worker.py`, `final_state.py`, `tests/test_run_contracts.py` |
| Ollama streaming, timeouts, output limits or provider telemetry | `transport.py`, adapter callers in `task_analyst.py`, `planner.py`, `worker.py`, `evaluator.py`, `recovery.py`, `integration.py`, `tests/test_transport.py` |
| Tool implementation, Python environment lifecycle, dependencies or controlled stdin | `tools.py`, `python_execution.py`, `runtime.py`, `orchestrator.py`, `worker.py`, `final_state.py`, `tests/test_python_execution.py`, `tests/test_tools.py` |
| Optional Git Docker isolation | `sandbox.py`, `sandbox/Dockerfile`, `tests/test_core_sandbox.py` |
| No-op file edits, mutation accounting or repeated edit loops | `tools.py` (`tool_edit_file`), `worker.py` (`PolicyToolbox.invoke`, `run_task`), `tests/test_tools.py`, `tests/test_capabilities.py`, `tests/test_control_runtime.py` |
| Tool-free Worker termination after no-progress | `worker_finalization.py`, `worker.py`, `llm_trace.py`, `tests/test_worker_finalization.py`, `tests/test_worker_assignments.py`, `docs/WORKER_FINALIZATION.md` |
| Runtime evidence, structured response diagnostics or repeated policy denial | `worker.py`, `final_state.py`, `evaluator.py`, `recovery.py`, `tests/test_control_runtime.py`, `tests/test_evaluator.py`, `tests/test_recovery.py` |
| Capability mapping or authorization | `capabilities.py`, `policy.py`, `worker.py`, `tests/test_capabilities.py` |
| Planner scope, resource descriptions, global tool IDs, capability compatibility, aliases or unsupported needs | `plan_scope.py`, `runtime_resources.py`, `capabilities.py`, `tools.py`, `skills.py`, `planner.py`, `plan_compiler.py`, `agent_factory.py`, `orchestrator.py`, `tests/test_plan_scope.py`, `tests/test_runtime_resources.py`, `tests/test_task_spec.py` |
| Task responsibility overlap, shared write targets or criterion verifiability | `planner.py`, `plan_evidence.py`, `plan_compiler.py`, `runtime_resources.py`, `tests/test_semantic_pipeline.py` |
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
| Cross-task file ownership, planned dependent writes, intent grants, cycle checks or owner handoffs | `cross_task.py`, `planner.py`, `plan_compiler.py`, `config.py`, `agent_factory.py`, `integration.py`, `worker.py`, `orchestrator.py`, `runtime.py`, `storage.py`, `api.py`, `schema.sql`, `frontend/views.js`, `frontend/app.js`, `tests/test_cross_task.py`, `tests/test_semantic_pipeline.py`, `tests/test_agent_factory.py`, `tests/test_control_runtime.py` |
| Semantic result evaluation or evaluation API | `evaluator.py`, `orchestrator.py`, `storage.py`, `schema.sql`, `api.py`, `tests/test_evaluator.py` |
| Global integration, append-only recovery or final response | `integration.py`, `integration_orchestrator.py`, `integration_storage.py`, `orchestrator.py`, `schema.sql`, `api.py`, `tests/test_integration.py` |
| Agent configuration validation | `config.py`, `tests/test_control_security.py` |
| Secret handling | `security.py`, security/runtime/storage tests |
| Web UI and orchestration status display | `frontend/app.js`, `core.js`, `views.js`, `dialogs.js`, `styles.css` |
| Commands or architecture | `README.md`, `docs/`, root and scoped `AGENTS.md` |

See [ARCHITECTURE.md](ARCHITECTURE.md), [DEVELOPMENT.md](DEVELOPMENT.md), and
[API.md](API.md).
