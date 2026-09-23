# Architecture

Freya includes a first-class orchestration layer. User prompts enter
`control_center/orchestrator.py`. When an enabled agent is marked with
`config.orchestration_role=task_analyst`, it first asks `control_center/task_analyst.py`
for a strict, tool-free interpretation of the original prompt. That result is
schema-validated and semantically reconciled into an `operational_prompt`; it
replaces the human wording for every downstream stage while the source remains
immutable audit evidence. Freya then asks `control_center/planner.py` for a
strict structured plan and persists that snapshot. For each dependency-ready
task, `control_center/agent_factory.py` creates a validated ephemeral agent from
the task type, required capabilities and enabled Skill registry. The complete
policy denies every undeclared capability, dangerous requirements remain
`ask`, and the concrete Tool surface is derived from that policy.
`control_center/agent_selector.py` then validates the generated candidate before
`control_center/execution_graph.py` delegates the bounded task through `Runtime`
and requires
`control_center/evaluator.py` to accept technical successes before integration.
Low-risk file and Python artifact requests use a single dynamic implementation
task; the Planner derives the minimum read-back and execution capabilities and
does not add a code audit or recovery stage merely because a file was created.
Workers remain the only components allowed to invoke tools;
Semantic non-acceptance enters bounded recovery in `control_center/recovery.py`
before a node can fail or a validated effective-plan revision can replace its subgraph.
After every active task is accepted, `control_center/integration.py` verifies
the Analyst-derived operational goal and every planned global criterion against a fingerprinted
effective-plan snapshot. Only global `accepted` permits final response
composition and `Success`; `needs_work` or resolvable `blocked` may append
bounded new tasks through the normal selector/policy/runtime/evaluator/recovery
pipeline. Each worker receives a generic policy plus its identity, instructions, skills,
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
       Task Analyst → Planner → dynamic implementation → conditional QA/audit
                              ↓                    ↓                 ↓
                       immutable plan ─────→ Execution Graph → Agent Factory → Agent Selector → Orchestrator
                                              ↓              ↓              ↓
                                      dependency state   Capability Policy  scheduler → spawned worker → local Ollama
                                              ↑                                  ↓
                                       Semantic Evaluator ← evidence/result ← Runtime
                                                            ↓
                                              capability resolver → policy engine → tools → workspace
```

The Task Analyst rewrites **what the user meant** without executing anything;
its validated operational prompt is authoritative for execution and the human
prompt remains available only for audit. Deterministic reconciliation prevents
schema-valid contradictions such as ignoring interactive input, assigns a
canonical internal `task_kind`, and makes one structured repair attempt when a
model response is blocked without a valid reason. The Planner determines **what**
work exists and derives safe verification needs: writes normally include
`filesystem.read`, while Python Hello World includes `execution.python_script`;
`filesystem.overwrite` is never added unless the task requires it. The Agent Factory determines
**who** executes each planned task by constructing a task-specific identity,
Skill set and least-privilege policy. The Agent Selector independently validates
and classifies that candidate before dispatch. The deterministic Execution Graph determines **when** dependency-ready
tasks run. The Worker determines **how** one selected task executes. Capability
Policy remains the sole authority for **whether** each requested action is
permitted. The Evaluator determines **whether the produced result actually
satisfied** the planned objective and criteria.
The Global Verifier determines **whether the complete accepted effective plan
satisfies the operational objective and global criteria**. The Integration
Replanner may only append new work for a global gap; it cannot edit, delete,
supersede, or rerun accepted tasks. The Result Integrator determines **what
grounded response to present**, but it cannot change correctness. These
orchestration-level components are tool-free and consume only bounded,
sanitized evidence.

Skills remain declarative guidance: they never grant capabilities. A primary
preferred Skill that is incompatible with the task policy rejects construction
so the task can be replanned; optional incompatible Skills are omitted. Generic
file creation selects the builtin `simple-file-artifact` Skill, whose required
capabilities are only `filesystem.create` and `filesystem.read`. Recovery retries
pass bounded reason/cause evidence into factory selection; only that genuine
recovery state or explicit diagnostic intent selects `debugging`.

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
Dynamic agents are normal validated agent rows with `config.provenance` containing
`generated_by_freya`, orchestration ID, plan-task ID, attempt, factory version
and `ephemeral=true`. Runtime task snapshots preserve their exact policy, Tools,
Skills and provenance. Terminal success, failure, cancellation and startup
recovery soft-archive every dynamic agent belonging to the orchestration; manual
agents and their direct-task API remain unchanged.
The Programmer, QA Tester, Code Auditor and Task Analyst JSON definitions remain
available as manual/legacy presets for direct tasks and compatibility. Modern
orchestration only needs a persistent Task Analyst when model-backed analysis is
desired; implementation, QA and audit roles are created dynamically when the
plan actually requires them.
Every Agent Selector decision, including `no_eligible_agent`, is stored in
`orchestration_selections` with the planned task ID, selected agent when any,
classification status, score, selector version, creation time and complete
explainable ranking snapshot. Historical decisions therefore retain the exact
scoring semantics and evidence used at selection time.
`orchestration_task_nodes` stores exactly one evolving node per planned task,
including immutable dependency/order data, selection and agent IDs, Runtime
task and delegation IDs, attempts, waiting reason, result/error, and timestamps.
The plan remains the immutable intent; node rows are the durable execution state.
`orchestration_evaluations` stores one immutable, versioned decision per
`(orchestration_id, plan_task_id, attempt)`, including the bounded input
snapshot, criterion-by-criterion result, independent model metrics and
truncation/deterministic flags. Nodes keep only `evaluation_id` and
`evaluation_status`. Insertion and the `evaluating → success|recovery_pending` transition
are one transaction.
`orchestration_execution_attempts` stores every real dispatch independently,
including its selection, agent, Runtime task, delegation, prompt, evaluation,
recovery action, status and timestamps. `orchestration_recovery_actions` stores
one immutable decision per source evaluation/attempt with strict action,
instructions, exclusions, affected tasks, fingerprint and metrics.
`orchestration_plan_revisions` stores cumulative effective plans while the
original `plan_json` remains immutable. Accepted tasks cannot be modified or
superseded; revision validation rejects cycles, unknown dependencies,
historical-ID reuse, task-limit overflow and active dependencies on superseded
tasks.
Revision rows identify `task_recovery` or `integration` as their real source;
integration never fabricates a recovery action. `orchestration_integrations`
stores one immutable row per `(orchestration_id, round)`: integration version,
effective-plan revision, strict result, metrics, bounded snapshot, truncation
flag, deterministic flag, graph fingerprint and global-problem fingerprint.
The snapshot retains the immutable Analyst-derived goal and criteria, active task IDs, accepted
evaluation IDs and attempts. Before inserting a result or applying its final
response/replan, Storage atomically rechecks `Integrating`, the plan revision
and the current graph fingerprint. Cancellation, timeout or any snapshot change
therefore wins over late model output.


Events receive a monotonic integer ID. `step.started` and `step.finished`
events build the reconstructable timeline while every attempt remains in `log_events`; successful file writes and edits additionally emit a bounded `workspace.diff` event so the created code is inspectable without relying on Git availability. The worker also persists every successful or denied runtime action in the structured result. A successful `run_command` whose output directly satisfies a quoted-output, exit-code, or JSON completion criterion becomes `command_execution` verification evidence; the final verification flags are derived from that evidence rather than from the model's prose. The persistence layer normalizes every runtime and orchestration event with `who`, `actor_name`, `actor_role`, `actor_type`, `where`, `workspace`, `when`, `phase`, `action`, `what`, `how` and a stable `trace_id`. This is derived centrally from the assigned agent, task snapshot and orchestration, so Task Analyst, Planner, Programmer, Code Auditor and other selected agents cannot disappear from the audit trail when an emitter omits a display field. `GET /logs?orchestration_id=...` merges runtime rows with the durable orchestration timeline, including Task Analyst and failure-analysis events, and labels their source. SSE accepts `Last-Event-ID`/`after`, replays later events and then
streams updates. On startup, abandoned Queued, Running, WaitingForApproval or Paused records become
Failed, pending approvals are denied as cancelled, and unfinished steps are closed.

Planning has explicit `Planning` and `Planned` states and emits
`freya.planning.started`, `freya.plan.created`, or `freya.planning.failed`.
An Analyst result with `ready_for_execution=false` is a hard gate: Freya emits
`freya.task_analysis.blocked` with its `blocking_reason` and stops before plan,
workspace, or delegation creation. The operational brief remains task context;
workers must not invent a hidden brief file or an implicit artifact producer.
The planner also collapses short linear create/write/verify workflows for one file-like artifact into a single implementation task. If the Analyst marks user input or interactive validation, it appends one dependent `qa-interactive-test` node with `interactive-testing`; QA may execute supported Python with bounded stdin but cannot modify files. For code/file mutation plans it then appends exactly one dependent, read-only `code-audit` task with the `code-review` Skill, so ordering is implementation → QA when required → Code Auditor. The created event contains only the goal, complexity, task count, task IDs and
schema version; the complete plan stays in its orchestration snapshot.
For an Analyst-confirmed simple, non-interactive program/script/file creation
with one implementation task and no audit/review criterion, the planner keeps
the implementation task as the complete plan; this bounded fast path avoids
inventing a second verification actor without changing capability policy.
Agent construction emits `freya.agent_factory.started`, `freya.agent_created`
and `freya.agent_policy.validated`; construction errors emit
`freya.agent_factory.failed`. Lifecycle cleanup emits
`freya.dynamic_agent.archived` with the archived IDs and count. These events
contain bounded metadata and never change capability authority.
Selection emits `freya.agent_selection.started`, followed by either
`freya.agent_selected` or `freya.agent_selection.failed`. The selected event
contains the planned task ID, agent ID, score, classification and selector
version; the complete ranking stays in the selection snapshot.
Graph execution emits `freya.graph.initialized`, `freya.task.ready`,
`freya.task.dispatched`, `freya.task.waiting_for_approval`, terminal task events,
and `freya.graph.completed`. Together with selection and delegation snapshots,
these events reconstruct Plan Task → Selection → Agent → Runtime Task → Result.
Semantic review emits `freya.evaluation.started` once and then exactly one
`freya.evaluation.completed` or `freya.evaluation.failed`. Completion events
carry the validated criterion-by-criterion decision and metrics; non-accepted
decisions also carry a bounded summary of the exact input evidence. Evaluator
prompts and private reasoning are never logged.

Orchestration transitions are conditional on the stored current state:
Recovery emits `freya.recovery.started`, `freya.recovery.decided`,
`freya.recovery.retry_scheduled`, `freya.recovery.replan_created`, or
`freya.recovery.exhausted`. Deterministic missing-file failures fail recovery
without selecting another agent. A write that repeats already verified content
is recorded as an already-satisfied no-op rather than an overwrite. Full
decisions remain in immutable storage. A late
recovery or replan is discarded if cancellation, timeout, restart, another
recovery, or a state/attempt change wins first.
Global integration emits `freya.integration.started`,
`freya.integration.completed`, `freya.integration.failed`,
`freya.integration.recovery_started`, `freya.integration.replan_created`, and
`freya.final_response.created`. Event payloads contain IDs, round, revision,
status and criterion count rather than model prompts or full output. Failed integration
events also include bounded per-criterion IDs, related local IDs and task IDs,
permitted proof refs, and rejected refs with reasons. Recovery revision events
record proposed, normalized, collided, and final new task IDs.


```text
Queued → Planning → Planned → Running → Integrating → Success
                                  ↑          │
                                  └──────────┘ append-only global recovery
                                             └→ Failed | Cancelled
```

`Queued`, `Planning`, `Planned`, `Running` and `Integrating` are active. Terminal states never
become active again. The orchestrator serializes cancellation with task
submission; after cancellation returns, no later plan, event or delegation can
appear. Repeated cancellation of a terminal run is idempotent. Startup atomically
changes abandoned active runs to `Failed`, preserves their plan and writes one
`freya.interrupted` event; repeating recovery produces no duplicate event.

## Structured planning

### Prompt interpretation

`task_analyst.py` validates a version-3 bounded JSON result containing the
self-contained `operational_prompt`, explicit and inferred requirements,
assumptions, risks, task characteristics, a
recommended role, acceptance criteria and validation strategy. The orchestrator
selects one enabled agent with `config.orchestration_role=task_analyst`; older
agents named or described as “Task Analyst” / “Analyst Planner” remain
discoverable through a compatibility fallback. It emits
`freya.task_analysis.started` and `freya.task_analysis.completed` before
planning. If no enabled Analyst exists, Freya emits a deterministic operational
brief rather than bypassing the phase.
The analyst adapter calls loopback Ollama with `tools=[]`, and a deterministic
interpretation is used if the model is unavailable. A standalone
`program_creation` request without a named language receives an explicit
Python 3.10+ assumption; a language named by the user is preserved. Other
missing-input blockers remain fail-closed. Deterministic facts can only
strengthen model characteristics and required interactive validation;
corrections are logged in `corrected_fields`. The role does not grant
capabilities, and a Skill is optional guidance only—not the routing or security
mechanism. Cancellation is rechecked after this phase so a late analyst result
cannot start a planner call or resurrect a terminal orchestration.

Plan schema version 1 requires a goal, summary, `simple` or `multi_step`
complexity, global success criteria and one to twenty tasks. Every task has a
normalized unique ID, objective, description, dependencies, required
capabilities, preferred Skills and success criteria. `criterion_links` records
stable global and local criterion IDs and explicit local-to-global references;
legacy plans are normalized with exact-text links only. Validation rejects unknown
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

`tools.py` supports optional `stdin` only on the restricted Python branch of
`run_command`, capped at 16,000 characters and never through a shell. Without
stdin, child stdin is closed, so an accidental `input()` raises immediately with
`interactive_input_required` instead of waiting until orchestration timeout.
Three consecutive denied or repeatedly blocked actions trigger
`task.blocked`; Freya then evaluates/replans or reports the cause. Graph timeouts
also pass through the persisted logs-only failure-analysis path.

Preferred Skills remain unvalidated semantic hints so planning is not coupled
to the mutable Skill registry. Required capabilities must exist in the platform
registry, but remain declarations: the planner never edits agent configuration
or policy. Replanning never grants capabilities and revalidates the complete effective plan.

## Execution graph and scheduling

`ExecutionGraph` is local, deterministic and model-free. It deep-copies the
validated plan and maintains `pending`, `ready`, `running`,
`waiting_for_approval`, `evaluating`, `recovery_pending`, `blocked`, `success`, `failed`,
`cancelled`, `skipped`, and `superseded` nodes. Only successful dependencies release work.
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
execution failure for that node. Only an explicit semantic recovery action may
reselect; ordinary scheduler failures never cause silent reselection.

Runtime `Queued` and `Running` map to graph `running`;
`WaitingForApproval` maps to `waiting_for_approval`; Runtime `Paused` remains a
nonterminal `running` node with an explicit reason. Runtime `Success` maps to
persistent `evaluating`; it never directly produces graph success. Only
evaluator `accepted` maps to `success`. `needs_revision`, `rejected`, `blocked`,
and evaluator infrastructure failure atomically map to `recovery_pending` while
preserving the evaluation reference. Recovery may retry the same revalidated
agent, retry with prior agents hard-excluded, create a validated effective-plan
revision, or fail. Retry clears only current-node references; immutable attempt,
evaluation, selection and recovery history remains. Other Runtime statuses map to their
graph equivalents. A fully accepted graph enters `Integrating`; it does not
directly produce orchestration success. The parent succeeds only after an
immutable global verification is `accepted`, and fails after all reachable work is terminal when any node failed,
was blocked, cancelled, or skipped. User cancellation remains `Cancelled`.
The wall-clock deadline includes planning and bounded polling uses the injected
orchestrator clock/wait functions.

Graph initialization is one SQLite transaction after the immutable plan is
saved. Plans larger than configured `max_delegated_tasks` fail during Planning,
before graph initialization or Runtime submission; the default is aligned with
the planner's 20-task maximum. Restart recovery preserves graph history but
changes unfinished running/waiting/evaluating nodes to cancelled and
recovery-pending or undispatched nodes to skipped. Recovery never resumes after
restart, so a failed recovered run exposes no ghost-active node.

## Semantic evaluation

`Evaluator` is read-only and receives only planned task fields, a bounded
Runtime result/error/verification record, and selected agent/runtime/attempt
IDs. Existing sanitization runs before model input and persistence. Result and
verification output, evidence counts and item lengths are bounded; any clipping
sets durable `context_truncated=true`. Agent output and verification text are
explicitly untrusted data and cannot alter the system prompt, schema or
configuration.

Deterministic checks run before any model call. Failed verification evidence
forces `rejected`; requested but unavailable or inconclusive verification
forces `blocked`; and test/lint/build criteria without passing objective
evidence are `blocked`. These outcomes cannot be overridden by agent claims or prompt injection.
For filesystem-only tasks, when Git diff and a permitted test suite are not
available, the Worker can use a policy-allowed `read_file` read-back for every
modified path as objective evidence. `write_file` content is compared exactly;
all modified paths must pass, otherwise the verification remains unavailable or
failed and the evaluator still fails closed. Otherwise the tool-free
`OllamaEvaluator` requests a strict JSON schema containing `accepted`,
`needs_revision`, `rejected`, or `blocked`,
with every planned success criterion represented exactly once. Invalid output
gets one repair attempt and then fails closed as evaluator infrastructure
`error`.

Evaluator calls are serialized to one model call at a time. Defaults are the
separately configurable local model `qwen2.5-coder:7b`, loopback endpoint
`http://127.0.0.1:11434`, and 120-second timeout. Explicit
`--evaluator-offline` uses deterministic evidence-only behavior for tests and
offline operation. It accepts only when configured verification was requested,
attempted and passed without contradictory evidence. Runtime result text is
untrusted agent output, not objective verification: a non-empty result, success
claim, or embedded instruction cannot produce acceptance. Without sufficient
objective evidence, offline evaluation returns `blocked` and marks every
criterion `unknown`. The Orchestrator's compatibility fallback uses this same
conservative evaluator; it never silently converts an unverified Runtime
success into semantic success. Cancellation, timeout, or restart wins over a late result;
the atomic commit rechecks orchestration state, node state, Runtime task and
attempt before persisting.

## Semantic recovery and replanning

`RecoveryController` is separate from Planner and Evaluator. Its strict schema
allows only `retry_same_agent`, `retry_different_agent`, `replan_subgraph`, or
`fail`; invalid model output gets one repair. Its Ollama adapter is loopback-only,
tool-free, non-streaming and independently metered. `--recovery-offline` makes
no model call and applies deterministic recovery: `needs_revision` and `blocked`
reuse the exact generated agent after revalidation, while `rejected` requests a
new generated variant with the same task-derived policy ceiling. Evaluator
`error` fails. The normal attempt, action, fingerprint and
wall-clock limits still apply.

Defaults allow three semantic attempts per task, two plan revisions, eight
recovery actions and sixteen total recovery/replanning model calls per orchestration.
The orchestration wall-clock deadline is rechecked after every recovery call. Stable
fingerprints stop repeated equivalent
failures. Same-agent retry revalidates and reuses the exact dynamic agent ID.
Different-agent retry creates a new dynamic identity/Skill variant, excludes
every prior agent ID, and derives the same capability ceiling from the unchanged
plan task; recovery inspection may derive only the safe `filesystem.read`
prerequisite and never `filesystem.overwrite`. There is no silent same-agent
fallback or policy expansion. Retry prompts include bounded evaluator issues,
missing evidence, and the previous workspace state so the new attempt can
inspect existing artifacts before creating or modifying files; raw prior model
transcripts and private reasoning are not reused.

Replanning produces a complete cumulative effective plan. Accepted and
superseded historical snapshots remain unchanged and new work uses new task IDs.
The Recovery Advisor may propose `affected_task_ids`, but deterministic DAG logic
computes `allowed_replan_scope`: the `recovery_pending` source plus only its
transitive descendants that remain `pending` or `ready` and have no execution
attempt. Independent branches are never mutable merely because they have not
started. Every task outside that set is protected and must remain structurally
identical and in the same protected order. Running, `waiting_for_approval`,
`evaluating`, `success`, terminal history, and any historically attempted task
outside the source are immutable across revisions.

The Replanner validates the explicit allowed/protected sets, and Storage
recomputes and validates them again in the transaction that persists the
revision, updates the effective plan, and mutates the graph. That transaction
also verifies that every previously active Runtime task is still tracked by the
same graph node and runtime ID. Replanning neither cancels nor mutates active
work in an independent branch, so evaluation continues against the exact task
snapshot used to start the attempt. The original plan remains available
separately from the effective plan. Retry recovery may create only the new
task-specific variant described above; it never auto-approves capabilities,
weakens policy, or adds a free-form shell.

## Post-failure log diagnosis

When an execution graph reaches a terminal failure, `Orchestrator` makes one
diagnostic pass before committing the orchestration's `Failed` state. The input
comes only from already-persisted orchestration and delegated-task events. Each
entry is sanitized and reduced to a stable `log_id`, source, timestamp,
event/status identifiers, IDs, policy fields, and bounded
message/reason/error text. File contents, model transcripts, private reasoning,
tool inputs, and workspace snapshots are excluded; at most 250 entries are sent.

`FailureAnalyzer` uses the recovery model configuration but a separate,
tool-free, non-streaming call. Its strict result contains `cause`, one or more
`evidence_log_ids`, `retryable`, and `recommended_action`. Validation rejects
unknown evidence IDs. `--recovery-offline` skips the call, and provider,
transport, schema, or citation failures fall back to a deterministic diagnosis
from the same log set. The fallback metrics retain that a model call was
attempted.

The lifecycle emits `freya.failure_analysis.started` and
`freya.failure_analysis.completed`. The completed event and final orchestration
response retain the grounded cause, cited IDs, retryability, recommended action,
mode, and metrics. This pass is explanatory only: it does not invoke tools,
retry a Runtime task, create a delegation, alter capabilities, resolve
approvals, mutate the plan, or reopen a terminal state. Cancellation is
rechecked before the final failure commit.

The deterministic diagnosis classifies `NoProgressDetected` and maximum-step
failures separately and recommends changing the action strategy instead of
blindly increasing the step limit. Runtime terminal events also retain the
failure class, stop reason, workspace-change count and no-progress flag.


## Global integration and result composition

Integration version 3 requires criterion-specific grounded proof, not merely
known evidence refs. The bounded catalog, exact association rules, Storage
reconstruction and the explicitly isolated legacy exception are specified in
[Integration proof contract](INTEGRATION_PROOF.md). Generic state updates cannot
grant production Success; finalization revalidates proof against persisted
authority in its write transaction.

`build_integration_input` runs deterministic preconditions before any global
model call. Every active effective task (all effective-plan tasks except
`superseded` history) must be `success`, retain an `accepted` evaluation and
have no pending, ready, running, approval, evaluating, recovery or failure
state. The context contains the original prompt/goal/global criteria, current
effective plan revision, active task objective/result summary, accepted
evaluation summary/evidence/verification and a compact revision history. Text
and list bounds set `context_truncated`; the serialized model context is capped
at 48,000 characters and fails closed if it cannot be reduced safely. Result
and evidence text are always untrusted data and never instructions.

`GlobalVerifier` first applies evidence-first hard checks. Failed objective
verification cannot be overridden by an accepting model; unavailable required
evidence blocks acceptance, and test/integration criteria cannot pass without
objective passing verification evidence. The tool-free `OllamaGlobalVerifier` uses the
separate integration model/endpoint/timeout configuration and a strict schema:
`accepted`, `needs_work`, `blocked`, or `error`, with each original global
criterion exactly once, bounded known evidence refs and existing responsible
task IDs. One repair is allowed within `max_integration_model_calls`. Offline
mode accepts only criteria provable from deterministic accepted records;
semantic uncertainty remains `blocked`.

For `needs_work`, or `blocked` when a new evidence-producing task is safe,
`IntegrationReplanner` returns only new tasks. Validation builds `current plan
+ new tasks`, calls normal plan/DAG validation, rejects historical IDs, limits,
cycles and dependencies on anything except accepted existing work or new work
in the same revision. The Storage transaction repeats those checks, leaves all
existing nodes untouched, creates only pending/ready nodes, records an
`integration` plan revision, and returns the run to `Running`. Those tasks use
the normal Agent Selector, capability policy, approvals, Runtime, Evaluator and
4.5 Recovery. Stable global-problem fingerprints, `max_integration_rounds`, the
shared `max_plan_revisions`, `max_delegated_tasks`, the wall-clock deadline and
`max_integration_model_calls` prevent unbounded loops.

Only global `accepted` reaches `ResultIntegrator`. Its model may select only
exact grounded statements derived from accepted active tasks, accepted
evaluations and the global result. Unsupported or malformed composition gets
one repair and then a deterministic fallback; a presentation failure never
changes an accepted correctness result. A final conditional commit rechecks the
same revision and graph fingerprint before `Integrating → Success`.

This stage does not create agents, grant capabilities, mutate policy, resolve
approvals, expose tools to verifier/composer models, or implement long-term
memory.


## Agent selection

Normal Freya orchestration does not depend on a preconfigured pool of Programmer,
QA Tester or Code Auditor agents. `AgentFactory` creates one candidate per ready
task, selects a minimal primary Skill (with at most one additional
task-justified specialty), records warnings for unknown or irrelevant preferred
Skills, and never turns Skill requirements into capability grants. Eight is only
a safety ceiling. Manual agents remain available for direct task submission and
compatibility tests.
`AgentSelector.select_agent(task, agents, context=None)` is local,
deterministic and model-free. It deep-copies its inputs, resolves each agent's
effective structured configuration, evaluates every required capability through
`PolicyEngine`, and resolves assigned Skills through `resolve_agent_skills`.
It never changes an agent, assigns a Skill, approves a request, grants a
capability, invokes a tool, or touches a workspace.

Candidates are classified before scoring:
`context.excluded_agent_ids` is a hard recovery gate: excluded candidates are
reported as ineligible and cannot win by score.


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
terminal before the parent becomes Failed. The selector itself does not create
agents, replan, retry semantic revisions, or enable agent-to-agent messaging;
agent construction remains the factory's separate responsibility.

## Runtime and control semantics

The scheduler supports bounded concurrency and serializes tasks per agent and
per resolved workspace. An agent can set an existing absolute default directory;
task submission can override that path for one run, and an explicit empty
override creates a fresh directory under `data/workspaces/`. Every task records
the resolved workspace in its immutable snapshot and runs in a spawned process. A Freya orchestration allocates an empty-selection workspace once, persists it in the orchestration config, and passes that same path to every dependent node so implementation, verification, and the read-only Code Auditor observe the same files.
On Windows the worker is assigned to a kill-on-close Job Object before tool
execution; POSIX uses a process session. Cancel, restart and shutdown terminate
the worker tree and persist a terminal event.

Pause is cooperative: an in-flight model or tool call can finish, then the
worker pauses between actions. When a capability or autonomy rule is ask, the
worker emits approval.requested, the parent persists the request, changes the
task to WaitingForApproval, and blocks the worker until once/task/deny is
resolved. Cancellation denies pending requests and terminates the worker. The total wall-clock deadline continues while
paused. Progress is the greatest fraction of the configured step, model-call and
tool-call budgets and reaches 100 only at termination. Token usage remains
visible in metrics, but the task token budget is unlimited when `max_tokens=0`
(the default); wall-clock, step, model-call and tool-call limits still bound
runtime resource use. Ollama receives `num_predict=-1` in that mode.

The worker tracks successful post-write validation actions. Ten consecutive
successful validations, or ten identical successful actions, produce an
`task.auto_completed` event and close the task without another model call.
Recoverable/transient read-only tool calls may retry within the configured bound;
policy denials, unavailable or unknown tools, invalid requests, approval
denials, non-applicable tools and forbidden paths do not retry. If the worker has
not changed the workspace and repeats the same read-only action three times,
or alternates the same two read-only actions for three cycles, it emits
`task.no_progress` and fails early with `NoProgressDetected`; increasing the
step budget is not treated as a fix. Writes and process execution are never
automatically retried.

Git inspection is applicability-aware. A task workspace outside a Git checkout
does not advertise `git_diff`, and the assigned Git Inspection Skill is removed
from the prompt-visible skill context while the immutable assignment remains in
the task snapshot. A direct non-applicable request returns a failed result with
`error_class=not_applicable` rather than a misleading success.

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
tools. Workers receive compact active Skill context; the dynamic AgentFactory
selects a minimal primary Skill and filters irrelevant preferred Skills. The
worker renderer exposes purpose, relevant instructions/procedures and only
required capabilities that exist in the effective toolbox; recommended and
missing-recommended fields remain internal diagnostics. Procedures that require
unavailable tools or capabilities are omitted/adapted, and the rendered
context has a 64,000-character budget. Each task stores an immutable copy of
every resolved Skill, including its version.

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
default output remains text for legacy compatibility, while structured
output is strictly validated as summary/actions/artifacts/verification/limitations.
Any invalid structured response, including prose, receives one repair attempt.
If fallback normalization is needed, `task.result_contract` logs a bounded,
sanitized preview and repair details; format failure stays separate from task
limitations and objective success. Verification state is persisted separately.

The worker classifies recoverable, environment, policy, approval, invalid,
unavailable and unknown-tool requests. A repeated policy denial with the same
tool, capability and arguments is intercepted before the underlying tool is
invoked again and is recorded as `repeated_policy_denied`; an unregistered name
is `unknown_tool`, while a registered but unassigned name is `tool_unavailable`.
Neither emits a false capability request. `BlockedActionCycle` counts blocked
model decisions, not internal retries, so three distinct/repeated blocked
decisions still stop the worker. If structured
model output is malformed, the worker merges the runtime actions, artifacts,
workspace diffs and verification evidence into the explicit fallback result
instead of discarding technical work.

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
