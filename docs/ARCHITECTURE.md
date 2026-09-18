# Architecture

Freya includes a first-class orchestration layer. User prompts enter
`control_center/orchestrator.py`, which asks `control_center/planner.py` for a
strict structured plan and persists that snapshot before asking
`control_center/agent_selector.py` to rank compatible existing agents,
then uses `control_center/execution_graph.py` to release dependency-ready tasks,
delegating bounded tasks through `Runtime`, and integrating persisted results.
Workers remain the only components allowed to invoke tools;
each receives a generic policy plus its identity, instructions, skills,
workspace, and limits. Orchestration runs, plans, delegations, and events are
stored durably, and SQLite migrations preserve existing data.

## Boundaries

The browser, API, runtime, worker and database are separate responsibilities.
The browser never calls Ollama directly. The API validates agent definitions;
the worker revalidates the immutable task snapshot before every tool call. The
parent process alone writes execution events and state to SQLite.

```text
browser → HTTP API → SQLite
             ↓
       Planner → immutable plan → Execution Graph → Agent Selector → Orchestrator
                                              ↓              ↓              ↓
                                      dependency state   Capability Policy  scheduler → spawned worker → local Ollama
                                                            ↓
                                              capability resolver → policy engine → tools → workspace
```

The Planner determines **what** work exists. The Agent Selector determines
**who** is the safest and most suitable existing candidate for one planned
task. The deterministic Execution Graph determines **when** dependency-ready
tasks run. The Worker determines **how** one selected task executes. Capability
Policy remains the sole authority for **whether** each requested action is
permitted.

The worker uses `control_center/transport.py`, which disables proxies and redirects so an
authorization value cannot be forwarded to another destination.

## Persistence and events

SQLite runs in WAL mode with foreign keys, one connection per operation and
`BEGIN IMMEDIATE` for atomic writes. The schema holds agents, configs, tools,
agent/tool links, reusable skills and agent/skill links, tasks, executions, steps,
logs, approvals and metrics. Agent deletion is
soft so task history remains readable. Each MVP task has one execution and an
immutable copy of the agent config, effective capability policy, structured
agent blocks, verification state, effective policy, and enabled tools. Approval
requests are durable records linked to the task and agent, with sanitized
arguments and explicit pending/approved_once/approved_task/denied statuses.
Every orchestration stores its plan JSON, schema version, creation timestamp and
planning duration/token counters on `orchestration_runs`. Saving the snapshot
and changing `Planning → Planned` is one SQLite transaction. The plan can be
written once, so later edits to agents, Skills, capability definitions or
planner code cannot alter the plan used by an existing run.
Every Agent Selector decision, including `no_eligible_agent`, is stored in
`orchestration_selections` with the planned task ID, selected agent when any,
classification status, score, selector version, creation time and complete
explainable ranking snapshot. Historical decisions therefore retain the exact
scoring semantics and evidence used at selection time.
`orchestration_task_nodes` stores exactly one evolving node per planned task,
including immutable dependency/order data, selection and agent IDs, Runtime
task and delegation IDs, attempts, waiting reason, result/error, and timestamps.
The plan remains the immutable intent; node rows are the durable execution state.

Events receive a monotonic integer ID. `step.started` and `step.finished`
events build the reconstructable timeline while every attempt remains in
`log_events`. SSE accepts `Last-Event-ID`/`after`, replays later events and then
streams updates. On startup, abandoned Queued, Running, WaitingForApproval or Paused records become
Failed, pending approvals are denied as cancelled, and unfinished steps are closed.

Planning has explicit `Planning` and `Planned` states and emits
`freya.planning.started`, `freya.plan.created`, or `freya.planning.failed`.
The created event contains only the goal, complexity, task count, task IDs and
schema version; the complete plan stays in its orchestration snapshot.
Selection emits `freya.agent_selection.started`, followed by either
`freya.agent_selected` or `freya.agent_selection.failed`. The selected event
contains the planned task ID, agent ID, score, classification and selector
version; the complete ranking stays in the selection snapshot.
Graph execution emits `freya.graph.initialized`, `freya.task.ready`,
`freya.task.dispatched`, `freya.task.waiting_for_approval`, terminal task events,
and `freya.graph.completed`. Together with selection and delegation snapshots,
these events reconstruct Plan Task → Selection → Agent → Runtime Task → Result.

Orchestration transitions are conditional on the stored current state:

```text
Queued → Planning → Planned → Running → Success | Failed | Cancelled
```

`Queued`, `Planning`, `Planned` and `Running` are active. Terminal states never
become active again. The orchestrator serializes cancellation with task
submission; after cancellation returns, no later plan, event or delegation can
appear. Repeated cancellation of a terminal run is idempotent. Startup atomically
changes abandoned active runs to `Failed`, preserves their plan and writes one
`freya.interrupted` event; repeating recovery produces no duplicate event.

## Structured planning

Plan schema version 1 requires a goal, summary, `simple` or `multi_step`
complexity, global success criteria and one to twenty tasks. Every task has a
normalized unique ID, objective, description, dependencies, required
capabilities, preferred Skills and success criteria. Validation rejects unknown
fields, wrong types, empty or excessive content, unknown capabilities, missing
dependencies, self-dependencies and cycles. A depth-first traversal validates
the complete dependency graph before persistence.
Complexity is canonicalized from task count: one task is `simple`; two or more
are `multi_step`.

Production uses `OllamaPlanner` through the shared non-redirecting transport.
It calls the validated loopback endpoint `/api/chat` with no tools,
`stream=false`, `think=false`, temperature `0.1`, an explicit JSON Schema,
8192 context tokens, at most 768 generated tokens and a bounded timeout. The
defaults are model `qwen2.5-coder:7b`, endpoint `http://127.0.0.1:11434` and a
120-second timeout; command-line options may change them while endpoint
validation remains loopback-only.

The model receives only the current goal, platform capability catalogue,
compact agent summaries and compact Skill summaries (bounded to 100 agents and
200 Skills). It receives no Skill procedures, logs, task outputs, secret
configuration or previous results. Its output must be a JSON object; one
controlled repair call is allowed, then planning fails explicitly. Provider
errors never fall back silently. Deterministic one-task planning exists only for
tests and the explicit `--planner-offline` mode. Planner calls are serialized per
orchestrator so concurrent runs cannot mix provider metrics; cancellation uses a
separate lifecycle lock and remains responsive while a planner call is pending.

Preferred Skills remain unvalidated semantic hints so planning is not coupled
to the mutable Skill registry. Required capabilities must exist in the platform
registry, but remain declarations: the planner never edits agent configuration
or policy. Replanning and semantic evaluation remain outside Stage 4.3.

## Execution graph and scheduling

`ExecutionGraph` is local, deterministic and model-free. It deep-copies the
validated plan and maintains `pending`, `ready`, `running`,
`waiting_for_approval`, `blocked`, `success`, `failed`, `cancelled`, and
`skipped` nodes. Only nodes whose dependencies all succeeded become ready.
Failure or cancellation blocks descendants transitively while unrelated
branches continue. Join nodes wait for every dependency.

Ready-node fairness follows the immutable plan order. Selection occurs exactly
once when a node first becomes ready and its durable snapshot is reused while
waiting. The scheduler admits at most `max_parallel_tasks` active graph nodes
(default 4), never submits two tasks concurrently to one agent, and also honors
Runtime's agent/workspace serialization. A selected Paused, Offline, or busy
agent leaves the node ready with a `waiting_reason`; it is not submitted
repeatedly or silently reselected. Disabled and deleted agents fail as described
below.
Selection and dispatch are interleaved: after selecting a ready task, the
scheduler reserves and attempts to dispatch it before selecting the next task.
The next Agent Selector call receives workload from active Runtime tasks,
active graph nodes, and selected ready-node reservations, so parallel branches
do not share stale workload information.

Paused and Offline are temporary scheduling waits: the selected node remains
ready with one stable `waiting_reason` and dispatches once after availability
returns. A selected agent that becomes disabled or is deleted is a durable
execution failure for that node; descendants are blocked normally and 4.3 does
not reselect another agent. Recovery/reselection remains future-stage work.

Runtime `Queued` and `Running` map to graph `running`;
`WaitingForApproval` maps to `waiting_for_approval`; Runtime `Paused` remains a
nonterminal `running` node with an explicit reason. Runtime terminal statuses
map to their graph equivalents. The parent succeeds only when every node
succeeds and fails after all reachable work is terminal when any node failed,
was blocked, cancelled, or skipped. User cancellation remains `Cancelled`.
The wall-clock deadline includes planning and bounded polling uses the injected
orchestrator clock/wait functions.

Graph initialization is one SQLite transaction after the immutable plan is
saved. Plans larger than configured `max_delegated_tasks` fail during Planning,
before graph initialization or Runtime submission; the default is aligned with
the planner's 20-task maximum. Restart recovery preserves graph history but
changes unfinished running/waiting nodes to cancelled and undispatched nodes to
skipped, so a failed recovered run never exposes ghost-running nodes.

## Agent selection

`AgentSelector.select_agent(task, agents, context=None)` is local,
deterministic and model-free. It deep-copies its inputs, resolves each agent's
effective structured configuration, evaluates every required capability through
`PolicyEngine`, and resolves assigned Skills through `resolve_agent_skills`.
It never changes an agent, assigns a Skill, approves a request, grants a
capability, invokes a tool, or touches a workspace.

Candidates are classified before scoring:

- `eligible`: every required capability evaluates to `allow`;
- `conditional`: no capability is denied or missing runtime support, but at
  least one evaluates to `approval_required` because its policy mode is `ask`;
- `ineligible`: `enabled=false`, archived/deleted, invalid, explicitly unusable,
  administratively disabled/unavailable, workspace-incompatible, missing the
  concrete tool runtime, or denied any required capability.

`enabled` is the durable availability control. `status` is an ephemeral
operational signal used primarily for ranking and diagnostics. Consequently,
enabled agents in `Running`, `Waiting`, `Paused`, `Offline`, or `Error` remain
potential candidates when their configuration and capability requirements are
valid. `Waiting`, `Paused`, `Offline`, and `Error` receive explicit warnings and
centralized score penalties; `Running` is primarily represented by workload.
Selection eligibility does not bypass Runtime lifecycle rules: a selected
paused agent must still be resumed before Runtime accepts a new task.

Eligibility class is a hard gate: `eligible` always ranks before `conditional`,
and `ineligible` candidates have a null score and can never be selected. If no
eligible candidate exists, the best conditional candidate may be selected with
`approval_required=true`. If neither class exists, selection returns
`selected_agent_id=null` and `status=no_eligible_agent`; it never chooses the
least-bad denied candidate.

Selector version 1 centralizes this scoring formula:

```text
+20  per operational preferred Skill
 +4  per assigned but non-operational preferred Skill
 +3  per role/identity token match, plus +5 per shared domain topic (max +15)
 +2  per relevant operational Skill token (max +10)
+10  when idle with zero active tasks
 -2  when temporarily Waiting
 -8  when temporarily Paused
-10  when temporarily Offline
-12  when reporting an operational Error
-15  per required capability needing approval
 -5  per active task
```

Preferred Skill and identity relevance are ranking signals only. A Skill never
substitutes for a required capability. Tie-breaking is deterministic: class,
score descending, workload ascending, operational preferred-Skill matches
descending, then `agent_id` ascending. The ranking includes reasons, warnings,
capability buckets, workload and Skill-match details for every candidate.
Candidate IDs must be unique; duplicate valid IDs reject the selection input
before scoring because they make the ranking ambiguous.

Selection does not dispatch by itself. The execution graph retains the selected
agent and waits for both a global slot and per-agent availability. Approval is
the existing durable Runtime flow and does not fail the graph while pending.
At timeout, active children are cancelled and all remaining graph nodes become
terminal before the parent becomes Failed. Stage 4.3 does not replan, create
agents, perform semantic result evaluation, or enable agent-to-agent messaging.

## Runtime and control semantics

The scheduler supports bounded concurrency and serializes tasks per agent and
per resolved workspace. An agent can set an existing absolute default directory;
task submission can override that path for one run, and an explicit empty
override creates a fresh directory under `data/workspaces/`. Every task records
the resolved workspace in its immutable snapshot and runs in a spawned process.
On Windows the worker is assigned to a kill-on-close Job Object before tool
execution; POSIX uses a process session. Cancel, restart and shutdown terminate
the worker tree and persist a terminal event.

Pause is cooperative: an in-flight model or tool call can finish, then the
worker pauses between actions. When a capability or autonomy rule is ask, the
worker emits approval.requested, the parent persists the request, changes the
task to WaitingForApproval, and blocks the worker until once/task/deny is
resolved. Cancellation denies pending requests and terminates the worker. The total wall-clock deadline continues while
paused. Progress is the greatest fraction of the configured step, model-call,
tool-call and token budgets and reaches 100 only at termination.

Token usage comes from the provider response. `num_predict` bounds generated
tokens, but provider-reported prompt usage can make a response cross the total
budget; in that case no tool from that response executes. Read-only tool calls
may retry within the configured bound. Writes and process execution are never
automatically retried.

Text-mode models may emit JSON actions instead of native tool calls. When a
model concatenates several action objects, the worker executes only the first,
returns its actual observation to the model and waits for a new action. This
prevents later actions from relying on invented tool results.

## Security limits

- HTTP listens on `127.0.0.1` and checks `Host`, `Origin` and cross-site fetch
  metadata. It has no accounts or production authentication and must remain a
  local single-user service.
- Model filesystem paths resolve inside the task workspace and then inside the
  configured relative allowlist. Absolute paths, traversal and symlink escapes
  are rejected.
- The folder browser lists directories visible to the local server. Saving an
  agent validates that its selected workspace is absolute, existing and a
  directory; task submission checks it again.
- Tool dispatch is allowlisted. `run_command` uses argv with `shell=False` and
  accepts only workspace Python/tests, Ruff, and scoped read-only Git commands.
- `execute` is a trust grant, not an OS sandbox. A Python file run inside the
  workspace has the Windows user's process privileges and may access host
  resources. Use disposable inputs and trusted local models.
- Secrets are environment references named `ACC_SECRET_...`. The worker reads
  the selected value for the Ollama Authorization header, registers it for
  redaction, then removes credential-like variables before tool subprocesses.
- Sanitization occurs before persistence and again before HTTP output. It
  removes credential patterns, registered secret values, private-key blocks,
  bearer values, URL credentials and private thinking fields/tags.

## Capability authorization

`capabilities.py` is the registry and `CapabilityResolver` maps each tool call
to one concrete action. `write_file` becomes `filesystem.create` or
`filesystem.overwrite` after inspecting the workspace target; `run_command`
maps only to supported Python, pytest, unittest, py_compile, Ruff, or Git
actions. `policy.py` validates the per-agent JSON policy and returns explicit
`allow`, `deny`, or `approval_required` decisions. Allow and ask rules are the only source used to derive the model-visible tool list; stale legacy tool selections cannot expose a capability. Deny and approval results never invoke the underlying tool. Filesystem rules support paths, extensions and max_bytes for every filesystem action. Agents with
legacy `permissions` are converted to the same engine, and the effective
policy is copied into every task snapshot.

These are application safeguards, not a security boundary against a hostile
local user or hostile executable code.

## Reusable skills

`control_center/skills.py` is the single registry and validation layer for
declarative Skills. A Skill contains specialty knowledge, instructions,
adaptable procedures, tags, a stable ID and a positive version.
`resolve_agent_skills` orders assigned Skills by per-agent priority, marks each
Skill operational only when every required capability is allowed and its concrete tool is available; diagnostics distinguish missing capability from missing tool/runtime support and expose recommended-capability warnings. Skills never grant capabilities or execute
tools. Workers receive compact active Skill context; task text selects up to
eight active specialties by simple name/category/tag relevance and the rendered
context has a 64,000-character budget. Each task stores an immutable copy of
every resolved Skill, including its version. Procedures are guidance; unavailable
or irrelevant steps are adapted by the model.

The orchestrator sends only bounded Skill summaries to the loopback Ollama
planner; full instructions and procedures remain outside planning context.
Deterministic fallback planning is available only through the explicit
`--planner-offline` development mode.

Conflicting guidance follows this precedence:

```text
System Policy > Capability Policy > Task boundaries > Agent constraints >
Agent instructions > Skill priority > Skill instructions > Skill procedures > Task content
```

## Structured agent configuration

`agent_context.py` normalizes and merges legacy fields with JSON blocks for
`identity`, `behavior`, `autonomy`, `verification`, and `output`. The worker
uses `build_agent_context` to produce one structured context after the
immutable system policy. Identity includes purpose, responsibilities, and
constraints; behavior controls planning, ambiguity, evidence, and repeated
failure handling; autonomy records decision preferences without granting
capabilities; verification and output define evidence and result shape. The
default output remains text for legacy compatibility, while structured output is strictly validated as summary/actions/artifacts/verification/limitations. A JSON-looking invalid result receives one repair attempt; otherwise an explicit fallback and limitation are returned. Verification state is persisted separately.

The worker classifies recoverable, environment, policy, approval, and invalid
requests. A repeated non-recoverable action with the same capability,
arguments, and error reaches the configured limit and returns
`REPEATED_ACTION_BLOCKED` so model-call and step budgets are not wasted.

## Feature scope

The MVP implements local Ollama, local process workers, persistent observations
and manual tasks. Remote/distributed workers, arbitrary model endpoints, queues
outside this process, RAG, long-term memory, agent teams, schedules, browser,
web search, generic HTTP and database tools remain unavailable.

Ollama request fields and usage counters follow its official
[chat API](https://docs.ollama.com/api/chat); context and temperature map to
documented model parameters in the [Modelfile reference](https://docs.ollama.com/modelfile).

Skills are versioned in immutable `skill_versions` snapshots. Updates retain prior definitions, while API deletion archives the row with `deleted_at` and records a `skill.archived` audit event. Context precedence is explicit: System Policy > Capability Policy > Current User Task > Agent Constraints > Agent Instructions > Skill Priority > Skill Instructions > Skill Procedures. Skills provide guidance only and never grant capabilities.

Skill IDs remain stable. A meaningful definition change increments `version` automatically and writes the next immutable snapshot; unchanged PATCH requests create neither a version nor an update event. Assignment priority is rendered in model context and resolves conflicts only between Skills. It cannot override the current task, agent constraints, or capability policy.
