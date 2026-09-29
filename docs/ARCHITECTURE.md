# Architecture

Freya includes a first-class orchestration layer. New production runs enter
`control_center/orchestrator.py` and pass through the tool-free
`control_center/task_spec.py` Task Analyst. `TaskSpecAnalyst` is a built-in
system component with its own model, loopback endpoint, timeout and offline
fallback settings; persisted agents and presets do not select it. It records one
versioned `CanonicalTaskSpec`: the objective, deliverables, sourced requirements,
constraints, decisions, assumptions and validation expectations. Missing
high-impact choices put the existing run in `NeedsClarification`; the Planner
and workers do not start. Answers are stored against structured questions and
update the same run. A ready Task Spec is the sole downstream intent source.
`control_center/planner.py` decides the work strategy and
`control_center/plan_compiler.py` assigns internal IDs, criterion links and
dependency graph metadata before the immutable plan snapshot is saved.
For each dependency-ready
task, `control_center/agent_factory.py` creates a validated ephemeral agent from
the task type, required capabilities and enabled Skill registry. The complete
policy denies every undeclared capability, dangerous requirements remain
`ask`, and the concrete Tool surface is derived from that policy.
`control_center/agent_selector.py` then validates the generated candidate before
`control_center/execution_graph.py` delegates the bounded task through `Runtime`
and requires
`control_center/evaluator.py` to accept technical successes before integration.
Low-risk file and non-interactive Python artifact requests use a single dynamic
implementation task. The calculator console case uses one implementation
task plus its dependent controlled-input QA task; it does not create test-file
scaffolding or a code-audit task. The Planner derives the minimum read-back and
file-creation capabilities for implementation, while Python execution is
reserved for QA. QA runs the requested `3` and `5` input case once. The program
prints whole-number sums without a trailing `.0`; QA reports its bounded input,
output and exit status.
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

The Task Analyst determines **what** the user wants and whether a material
question remains. Its output cannot choose agents, tools, capabilities, Skills
or dependencies. The Planner determines the semantic decomposition of the
canonical Task Spec: task count, dependencies, outcomes, criteria, logical file
owners and registered semantic operations. It does not choose concrete tools,
capabilities or Skills. The Runtime Resource Catalog holds the canonical
semantic-operation-to-capability-to-tool mapping. The Plan Compiler alone
applies that mapping, assigns runtime IDs, validates ownership and the DAG, and
produces the durable execution plan.
The Agent Factory determines
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

| Decision | Primary owner | Output |
| --- | --- | --- |
| User intent and material clarification | Task Analyst | Canonical Task Spec |
| Task decomposition, task kind and semantic operations | Planner | Semantic Plan |
| Resource resolution, runtime IDs, dependencies and ownership validation | Plan Compiler + Runtime Resource Catalog | Compiled Runtime Plan |
| Runtime permission for each action | Capability Policy | Allow, ask or deny |
| Agent identity and execution contract | AgentFactory | Ephemeral agent |
| Tool execution and objective evidence | Worker Runtime | Technical result and evidence |
| Lifecycle, dispatch, approvals and coordination | Freya / Orchestrator | Orchestration state |
| Dependency readiness | Execution Graph | Ready task set |
| Local task acceptance | Evaluator | Evaluation decision |
| Bounded local failure strategy | Recovery / Replanner | Recovery action or revised plan |
| Whole-system correctness | Global Verifier | Global decision |
| Final response from accepted facts | Result Integrator | User-facing result |

Skills remain declarative guidance and never grant capabilities. During the
single-Skill configuration, `freya-core` is the only active Skill and is
assigned to every dynamic role. Planner Skill preferences are ignored with a
warning. Policy determines the actual tool surface and evaluates each action.

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
The Programmer, QA Tester and Code Auditor presets remain available for manual
or direct-task workflows. The versioned Task Analyst JSON file is retained as a
clearly marked legacy compatibility export, but it is not a creatable API preset
and modern orchestration never loads it. The built-in Task Analyst always runs
before planning; implementation, QA and audit roles are created dynamically
when the plan requires them.
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

`orchestration_project_states` stores one versioned ProjectState per run. Its
bounded manifest records workspace-relative paths, file sizes and SHA-256
hashes, plus permanent plan-task ownership; it never stores file contents.
Manifest traversal skips common generated directories and links, stops at 500
files or depth 8 by default, and hashes only regular files up to 1 MB. Project
state writes use an expected-revision transaction and append traceable events.
Snapshots are refreshed before each dispatch, recovery retry, and cross-task
owner action. A changed or missing tracked artifact advances its artifact
revision and marks prior verification and symbol metadata stale. The snapshot
sent to a worker is relevance-ranked and bounded to 500 artifacts and 12,000
prompt characters by default; it contains the current task and direct
dependencies, not file contents.

Every generated agent gets the internal `project_context` tool under
`project.read_context`. It queries only the dispatch snapshot (`summary`,
`artifact`, `symbol`, `task` or metadata `search`), passes through the same
capability policy engine, and is omitted from the Planner catalog and manual
agent configuration. The Runtime Resource Catalog derives `filesystem.read`
and `read_file` for `modify_file` and `overwrite_file`; pure `create_file` does
not acquire read access. Read policy covers permitted workspace artifacts,
including foreign-owned ones. Ownership restricts writes only. The Worker
requires a successful read of current bytes in the same attempt before editing
or overwriting an existing file; changed hashes return `STALE_ARTIFACT` and
require a fresh read. A truncated `read_file` response does not authorize a
write because the agent has not seen the full source. After the Evaluator accepts a task, ProjectState accepts only
artifact paths present in the Worker action ledger, verifies the file still
exists under the assigned workspace, and records its current hash. Worker
symbol reports are checked against the current artifact bytes and declaration
line; dependency reports are stored as observed metadata. Rejected or
unverifiable claims do not become verified state. Cross-task requests retain
the requester's observed artifact revision, while the owner receives a fresh
snapshot and must inspect and read the current file before editing.

When a mutating task has no successful write, a current read of every declared
target permits an `already_satisfied_candidate` result with artifact hash and
revision evidence. This is technical completion only; Evaluator judges each
planned criterion. Without current target reads, the Worker retains
`ExpectedWorkspaceMutationNotObserved`. Dispatch prompts include bounded direct
dependency, dependent and shared-path responsibilities to keep each worker on
its assigned step. Agents can report relevant symbol kind, signature, path and
purpose; ProjectState verifies names against accepted artifact bytes when possible.


Events receive a monotonic integer ID. `step.started` and `step.finished`
events build the reconstructable timeline while every attempt remains in `log_events`; successful file writes and edits additionally emit a bounded `workspace.diff` event so the created code is inspectable without relying on Git availability. The worker also persists every successful or denied runtime action in the structured result. A successful `run_command` whose output directly satisfies a quoted-output, exit-code, or JSON completion criterion becomes `command_execution` verification evidence; the final verification flags are derived from that evidence rather than from the model's prose. The persistence layer normalizes every runtime and orchestration event with `who`, `actor_name`, `actor_role`, `actor_type`, `where`, `workspace`, `when`, `phase`, `action`, `what`, `how` and a stable `trace_id`. This is derived centrally from the assigned agent, task snapshot and orchestration, so Task Analyst, Planner, Programmer, Code Auditor and other selected agents cannot disappear from the audit trail when an emitter omits a display field. `GET /logs?orchestration_id=...` merges runtime rows with the durable orchestration timeline, including Task Analyst and failure-analysis events, and labels their source. SSE accepts `Last-Event-ID`/`after`, replays later events and then
streams updates. On startup, abandoned Queued, Running, WaitingForApproval or Paused records become
Failed, pending approvals are denied as cancelled, and unfinished steps are closed.

When `write_file` returns `ALREADY_SATISFIED`, the action ledger records
`changed=false` and `already_satisfied=true`. The Worker may end execution when
that exact write was already changed in the current run, the same satisfied
write repeats, or it is the last tool call in the current model response. This
is execution completion only: the result, artifacts, workspace diffs and
verification evidence are preserved, no semantic criterion is marked satisfied
from the no-op, and the Evaluator remains responsible for task acceptance.

Production runs transition `Queued → Analyzing → NeedsClarification` when
the Analyst must ask the user. Each response returns that same run to
`Analyzing`. A user change to a ready spec may create a new version while
planning has not yet saved its immutable plan. The old in-flight plan is
fenced by the Task Spec snapshot; changes after plan creation are rejected. A ready Task Spec enters `Planning → Planned`; no plan or worker
exists while clarification is pending. The Task Spec and its revisions are
persisted before planning; responses are stored separately from approvals.
Events include `task_analysis.started`, `task_analysis.updated`,
`task_analysis.clarification_required`,
`task_analysis.clarification_received`, and `task_analysis.ready`. The
clarification reducer records an answer once in history and `user_decisions`,
filters answered or repeated questions by semantic field and normalized text,
and rejects optional questions and Task Spec container fields. An exact answer
submission replay is idempotent. After three answered rounds, unresolved
material questions fail with `task_analysis.clarification_cycle_detected`;
resolved questions proceed to planning.
The Planner emits `freya.planning.started` and `freya.plan.created`.
`freya.planner.semantic_plan_proposed` records task meaning and semantic
operations. `freya.plan.resources_resolved`,
`freya.plan.ownership_resolved` and `freya.plan.compiled` record the compiler's
derived runtime requirements, normalized owners and compiled plan separately.
Workers receive only a deterministic rendering of the Task Spec plus their
compiled task step. The planner may add controlled Python QA for an interactive
calculator; a simple task need not create an auditor.
Agent construction emits `freya.agent_factory.started`, `freya.agent_created`
and `freya.agent_policy.validated`. Planner Skill preferences are ignored;
`freya-core` is assigned directly. Construction errors emit
`freya.agent_factory.failed`. Lifecycle cleanup emits
one `freya.dynamic_agent.archived` event per archived agent. Its `status=Success`
describes successful cleanup; `orchestration_status` retains the final run state.
These events contain bounded metadata and never change capability authority.
Selection emits `freya.agent_selection.started`, followed by either
`freya.agent_selected` or `freya.agent_selection.failed`. The selected event
contains the planned task ID, agent ID, score, classification and selector
version; the complete ranking stays in the selection snapshot.
Graph execution emits `freya.graph.initialized`, `freya.task.ready`,
`freya.task.dispatched`, `freya.task.waiting_for_approval`, terminal task events,
and `freya.graph.completed`. Together with selection and delegation snapshots,
these events reconstruct Plan Task → Selection → Agent → Runtime Task → Result.
The Worker emits `worker.execution.completed` or
`worker.execution.failed` for technical execution only; Evaluator acceptance
is persisted separately. Recovery and Integration revision events include the
semantic operation to capability to tool resolution records for new tasks.
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
Queued → Analyzing → NeedsClarification ──answer──→ Analyzing
                   └→ Planning → Planned → Running → Integrating → Success
                                        ↑                     │
                                        └─────────────────────┘ append-only recovery
```

`NeedsClarification` survives a server restart and can be cancelled. In-flight
analysis and execution are interrupted on restart. Terminal states are final.
Cancellation serializes with planning and delegation.

## Structured planning

### Canonical intent and semantic compilation

`task_spec.py` validates Task Spec schema version 1 and sequential revisions.
Each requirement, deliverable and constraint has an `explicit`, `clarified`
or `assumed` source. Questions have a runtime-assigned monotonic ID, question, reason, semantic leaf field
and required flag. The Analyst asks only about material ambiguity without a
safe default; internal engineering decisions stay with Planner and workers.
`TaskSpecStatus` and `RequirementSource` define the canonical status and source
enums. The Ollama response schema and prompt use those same values. A declared
`inferred` compatibility alias normalizes to `assumed`; casing and whitespace,
null list/map containers, omitted optional collections and runtime clarification
IDs are assigned after question reduction, before validation. Existing IDs are
preserved by validation; it never renumbers them. These transformations
do not invent product scope. If the model omits a deliverable or requirement
collection, source-grounded deterministic entries can restore it before the
scope guard checks every recognized explicit action, previous requirement and
clarification answer. The model returns intent fields only; source prompt,
schema/revision metadata, clarification history and revision changes remain
runtime-owned. Unknown fields and invalid cross-field readiness combinations
fail validation. One bounded repair receives the original candidate,
the exact validation path/message and the expected schema; a still-invalid
candidate uses the conservative deterministic fallback.

Contract failures are recorded as sanitized `task_analysis.contract_invalid`
events with the initial/repair stage, error type/path, expected and received
values, validation message, bounded response excerpt and normalization flag.
`task_analysis.normalization_succeeded`, `task_analysis.repair_started`,
`task_analysis.repair_succeeded` and `task_analysis.fallback_used` expose the
corresponding transitions. Task Analyst metrics include `model_calls`,
`fallback_used`, `fallback_reason`, `initial_validation_error`,
`repair_validation_error` and `repair_attempted`. A model response that passes
after safe normalization or source-grounded completion completes after one call.
The deterministic Task Analyst fallback preserves recognized multi-part actions
and does not infer an unspecified language or interface. The legacy version-3
`task_analyst.py` contract remains for injected compatibility adapters only.
Registered action, interface and language tokens may be corrected by one edit
only when exactly one canonical token matches. The original wording remains in
requirements and a `task_analysis.lexical_normalization` event records the
canonical interpretation. A clarified interface or language modifier in a
model-proposed explicit entry is separated using clarification history and the
deterministic Task Spec floor before the unchanged scope validator runs.

`Planner.create_plan_for_spec` receives the canonical Task Spec and emits
Semantic Plan schema version 3. Each task contains `task_kind`, semantic needs,
registered operation IDs, dependencies, outcomes, criteria, exact `owned_paths`
and `write_targets`; it
contains no tool, capability or Skill IDs. Before planning,
`orchestrator.py` creates a fresh `RuntimeResourceCatalog` from the capability
registry and `Toolbox`. It exposes semantic operation IDs and descriptions,
not concrete runtime resources. The catalog has a stable content hash.

After JSON parsing, the Plan Compiler invokes `plan_scope.py` to compare the semantic proposal with explicit
or clarified Task Spec intent. An optional external-only task or model-created
deployment criterion is removed before resource resolution; dependent tasks
inherit its prerequisites. Mixed artifact/external tasks and removal that
would leave a Task Spec validation expectation uncovered fail as
`PlannerScopeError`. A web artifact request alone does not authorize deployment,
publication, hosting, upload or remote actions. Explicitly requested external
work remains in the plan and still needs supported resources and Policy.
The bounded, sanitized `planner_semantic_plan` snapshot records proposed task
keys, objectives, needs, semantic operations, owners and dependencies before
resolution. The original human prompt remains audit evidence and is not a
downstream intent source; workers receive a deterministic rendering of the
validated CanonicalTaskSpec.

The Plan Compiler checks each `unsupported_requirements` proposal against the
Task Spec and registered runtime operations. Product behavior such as arithmetic
is implemented through file operations and cannot be declared an unsupported
runtime resource by the Planner. A requested runtime action without a matching
registered operation raises `UnsupportedResourceRequirement`; an incorrect supported claim
causes one bounded repair. The repair payload contains the exact previous
Semantic Plan, structured compiler diagnostics and preservation rules.

The Compiler preserves every distinct plan task. It assigns one permanent owner
per normalized path: the unique creator, otherwise one explicit owner, otherwise
the sole writer. A duplicate ownership claim from a noncreator is converted to
a foreign write when the creator is unique. Two creators invalidate the plan;
two modifier owners without a creator raise `OwnershipAmbiguous` for one bounded
Planner repair. It validates every operation against the catalog and derives
the complete runtime requirement set. For example, `modify_file` maps to
`filesystem.modify` and `edit_file`; `create_file` maps to
`filesystem.create` and `write_file`; `run_python_script` maps to
`execution.python_script` and `run_command`; `run_pytest` maps to
`execution.pytest` and `run_command`. Unknown operations or unsupported
external actions fail closed. Planner-supplied legacy capability and tool
fields are ignored; they never create or widen authority. The Plan Compiler
assigns task and criterion IDs, resolves dependencies, rejects cycles and
invalid ownership, and produces compiled plan schema version 4. The compiled
plan persists `write_owners`; each task records `foreign_write_targets`.
Global success criteria remain on the plan for Global Verification. Local task
criteria remain on their own tasks for the Evaluator; the Compiler links an exact
match but never appends a global criterion to the final task by position.
Unknown resources and genuinely unsupported actions do not trigger repair.

The Agent Factory consumes the compiled plan and cannot add capabilities or
tools. It uses compiled `task_kind` to assign worker, QA or auditor role;
legacy plans without that field retain their prior deterministic role fallback.
It assigns `freya-core`; the Skill declares available tools, while
capability policy projects the effective worker schemas. No Skill adds a
capability to task policy. Startup validates every declared `freya-core.tools`
ID against `Toolbox`; unknown IDs fail with `SkillConfigurationError`.

Tools remain concrete worker operations; capabilities remain declared action
requirements in the compiled plan; semantic operations remain planning intent;
Skills remain guidance. The Worker reports `execution_complete` or
`execution_failed` as technical outcomes only. It cannot accept its own result.
The Evaluator alone accepts local task evidence. The Orchestrator owns lifecycle
and dependency coordination. Recovery may replan only its deterministic local
scope and may compile new operations only within the superseded tasks' existing
capability budget. Integration Replanner applies the same rule against the
compiled plan budget. The Global Verifier accepts the whole objective from
grounded evidence, and Result Integrator can only render accepted facts.

## Activity and performance read model

`control_center/activity.py` projects the existing persisted event stream into
the `GET /api/orchestrations/{id}/activity` response; `storage.py` supplies the
run, merged runtime/orchestration events, evaluations and integration metrics.
The projection labels actors separately from agents, so Planner, Task Analyst,
orchestration and other system events stay visible with a null `agent_id`.
It derives Task Analyst, Planner, compiler, dynamic-agent, worker/QA, evaluator,
graph, integration and final-response phase intervals from event timestamps and
emits the original events in chronological order. `freya.plan_compiler.*`
events expose compiler attempts, duration and terminal status. Result Integrator
started, failed and created events bound final-response duration.

`total_elapsed_seconds` is wall clock from orchestration creation through its
terminal event (or current time while active). `processing_seconds` excludes
the union of clarification and approval waits. `execution_seconds` is the union
of worker intervals, less those waits; overlapping workers count once. `llm`
aggregates recorded model-call durations, calls and tokens. Component phase
durations may overlap or nest, and LLM time is included in processing, so phase
and LLM durations are not additive totals. The read model is calculated from
persisted timestamps and metrics rather than frontend input.

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

`Evaluator` is read-only. Before checks or model inference,
`normalize_execution_evidence` builds a bounded catalog from verification
records, tool outcomes, artifact changes and workspace diffs. It retains
available content/output, path, operation, status, source, tool, capability,
event ID, timestamp, hash and check condition. Deterministic evidence IDs
deduplicate identical records. The Orchestrator supplies each task's stable
local criterion IDs from `criterion_links.local`; legacy criterion-text links
are normalized to those IDs. Explicit verification links and criterion IDs are
authoritative, while path or file-kind associations are marked as inferred.
Evidence with no grounded link remains global.

The model context contains the evidence catalog, per-criterion references and
global evidence IDs, so it can inspect a relevant diff/read-back without
reconstructing runtime provenance from agent prose. Sanitization runs before
model input and persistence. Result, verification output, evidence counts and
item lengths are bounded; clipping sets durable `context_truncated=true`.
Agent output and verification text are untrusted data and cannot alter the
system prompt, schema or configuration.

Deterministic checks run before any model call and apply per criterion. A pure
file creation/presence criterion is proven by a successful scoped create,
created artifact or workspace diff, or successful read-back for every relevant
planned target. Denied later writes do not undo that evidence; later successful
removal does. Relevant failed tests reject their criterion, while a missing
required test/lint/build result blocks it. Mixed tasks retain proven facts and
send only semantic questions to the LLM. Agent claims cannot override these
facts. File creation, action success or a path association alone cannot satisfy
a semantic criterion. A matching file_content_match only proves the exact
check that generated it; it does not prove unrelated behavior. A directly
linked passing test or exact typed verification is evaluated before semantic
inference. Unmatched evidence can still be considered globally when its path
or content is relevant, but an unrelated diff is not attached to a criterion.
For filesystem-only tasks, when Git diff and a permitted test suite are not
available, the Worker can use a policy-allowed `read_file` read-back for every
modified path as objective evidence. `write_file` content is compared exactly;
all modified paths must pass, otherwise the verification remains unavailable or
failed and the evaluator still fails closed. Otherwise the tool-free
`OllamaEvaluator` receives only unresolved semantic criteria with their
grouped evidence references and returns one
strict `criteria` array. Each entry contains `criterion`, `status`, `reason`,
`evidence`, and `confidence`; global status and actions are forbidden in model
output. Python combines canonical criterion records with this precedence:
evidenced `unsatisfied` → `rejected`, otherwise `unknown` → `blocked`, otherwise
`partial` → `needs_revision`, otherwise all satisfied → `accepted`. Python sets
`recommended_action`, `issues`, and `missing_evidence`. One invalid response
gets one repair. If both calls fail, the Evaluator retries once from the same
immutable bounded Runtime evidence, with one repair available on that retry.
Four model calls are the maximum. Exhaustion persists evaluator infrastructure
`error` without a semantic rejection or Worker retry.

Evaluator calls are serialized to one model call at a time. Defaults are the
separately configurable local model `qwen2.5-coder:7b`, loopback endpoint
`http://127.0.0.1:11434`, and 120-second timeout. Explicit
`--evaluator-offline` uses deterministic evidence-only behavior for tests and
offline operation. It accepts only criteria with direct, criterion-specific
objective proof. Runtime result text is
untrusted agent output, not objective verification: a non-empty result, success
claim, or embedded instruction cannot produce acceptance. Without sufficient
objective evidence, offline evaluation returns `blocked`; already proven
criteria remain satisfied. The Orchestrator's compatibility fallback uses this same
conservative evaluator; it never silently converts an unverified Runtime
success into semantic success. Cancellation, timeout, or restart wins over a late result;
the atomic commit rechecks orchestration state, node state, Runtime task and
attempt before persisting.

## Semantic recovery and replanning

`RecoveryController` is separate from Planner and Evaluator. Its strict schema
allows only `retry_same_agent`, `retry_different_agent`, `replan_subgraph`, or
`fail`; invalid model output gets one repair. Its Ollama adapter is loopback-only,
tool-free, streamed through `transport.py`, and independently metered. `--recovery-offline` makes
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
Different-agent retry creates a new dynamic identity, excludes
every prior agent ID, and derives the same capability ceiling from the unchanged
plan task; recovery inspection may derive only the safe `filesystem.read`
prerequisite and never `filesystem.overwrite`. There is no silent same-agent
fallback or policy expansion. Retry prompts include bounded evaluator issues,
missing evidence, and the previous workspace state so the new attempt can
inspect existing artifacts before creating or modifying files; raw prior model
transcripts and private reasoning are not reused.

Replanning produces a complete cumulative effective plan. Accepted and
superseded historical snapshots remain unchanged and new work uses new task IDs.
Every new Recovery task must carry a registered `task_kind` and semantic
operation IDs; the shared Plan Compiler helper resolves them through the same
Runtime Resource Catalog and enforces the superseded-task resource budget.
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
tool-free, streamed call. Its strict result contains `cause`, one or more
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

Append-only Integration tasks also require a registered `task_kind` and
semantic operations. The Runtime Resource Catalog resolves their capabilities
and tools, and the append-only validator rejects work outside the compiled
resource budget.

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
objective passing verification evidence. Exact Planner-scoped local checks with
satisfied local decisions and criterion-specific direct proof can resolve all
global criteria deterministically; broader semantics still use the model.
`GLOBAL_ACTIONS` supplies the sole `accepted/needs_work/blocked/error` action
mapping, which is applied to model output before strict validation and after
repair. Invalid responses emit bounded `freya.global_verifier.validation`
diagnostics. The tool-free `OllamaGlobalVerifier` uses the
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
task, assigns `freya-core`, ignores Planner Skill preferences with a warning,
and never turns its tool declarations into capability grants. Manual agents
remain available for direct task submission and
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
runtime resource use. Even in that mode, each Worker model call is capped at
2048 output tokens (768 for structured-output repair); the cumulative task
token budget remains unlimited.

### Cross-task file ownership

Every write task declares exact workspace-relative `write_targets`. A path's
permanent plan-task owner is recorded in `owned_paths` and `write_owners`; other
writers carry a `foreign_write_targets` entry with the owner ID. Plan validation
rejects inconsistent mappings, and generated agents check both their exact
scope and the owner index before mutation. The compiler derives a task-scoped
write grant only when the exact foreign path is in that task's declared
`write_targets` and its permanent owner is a dependency ancestor. The grant does
not change ownership or capability policy; existing files still require a
successful current read before modification. Other foreign writes stop before
the tool handler and require `requested_change`, `reason`, `needed_for` and a
boolean `blocking` field before Freya creates a structured request. An
incomplete request changes no file, receives an explicit retry contract, and an
identical incomplete retry stops as no-progress. The Worker finishes valid
cross-task requests as `WaitingForApproval`; it does not wait for an agent or
send a direct agent-to-agent message.

Freya resolves the owner only inside the same orchestration and checks the
dependency graph for cycles. Reusable human grants are scoped to that
orchestration, requester task, owner task, exact file and requested operation.
The operation is preserved as `create`, `modify` or `overwrite` from the
capability resolved before the ownership check.
`Approve once` covers only the current request. `Approve similar purpose for
this file` creates a reusable intent grant. An exact normalized repeat may be
matched deterministically; a bounded, tool-free local intent comparison may
accept a semantically equivalent purpose only at high confidence. A mismatch,
uncertain result, or unavailable matcher remains a human approval. Neither a
grant nor an approval changes capability policy.

After approval, Freya builds an ephemeral owner-side agent with read access and
only the approved filesystem operation for the exact path. It waits until the
original owner node has reached a terminal graph state so its later writes
cannot overwrite the coordinated change. The derived agent's write scope is
the one requested file. It goes through the normal
Agent Factory, Agent Selector, Runtime and Evaluator; only evaluator acceptance
marks the handoff complete and resumes the requester in a fresh attempt.
Denial, owner failure, failed evaluation, invalid ownership and detected cycles
resume the requester with the recorded outcome so it can choose another
permitted approach or report the blocker. The cross-task tables and events keep
the request, durable approval, scoped grant, owner runtime task and evaluation
result auditable. The original owner node remains terminal, including `success`.
Owner actions for the same normalized path are serialized within an orchestration;
actions on different paths remain independent in the scheduler.
Recovery and Integration preserve every existing `write_owners` entry. New
revision tasks may own only newly assigned paths; writes to an existing owner's
path become `foreign_write_targets` and use the same handoff.
If restart recovery fails the orchestration, it also closes its unfinished
cross-task requests and pending approvals in the same database transaction;
no owner handoff resumes from a partially dispatched state.

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

Git inspection is applicability-aware. Only a checkout rooted in the assigned
workspace advertises `git_diff`; a parent checkout is outside its boundary.
A direct non-applicable request returns a failed result with
`error_class=not_applicable` rather than a misleading success.

`write_file` always writes a file, including for empty content, and creates
missing parent directories. Before writing, `tools.py` checks every existing
parent; a file in that chain returns `error_class=ParentPathIsFile` with the
blocking workspace-relative path. The worker treats this as non-retryable,
returns only the processed portion of a batched tool-call message, and waits for
the model to change strategy before another action. A later nested write under
the recorded blocker stops without a tool call or another failed-write charge.

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
- `run_command` and `git_diff` execute only in a disposable Docker copy of the
  workspace. The container has no network or Docker socket, a read-only root,
  no host HOME/USERPROFILE or inherited credentials, and memory, CPU, PID and
  time limits. It mounts only the copy; results are never copied back. Docker
  or image failure is `SandboxUnavailable`, without host fallback. Persistent
  edits use Policy-checked filesystem tools.
- Secrets are environment references named `ACC_SECRET_...`. The worker reads
  the selected value for the Ollama Authorization header, registers it for
  redaction, then removes credential-like variables before tool subprocesses.
- Sanitization occurs before persistence and again before HTTP output. It
  removes credential patterns, registered secret values, private-key blocks,
  bearer values, URL credentials and private thinking fields/tags.

## Capability authorization

`capabilities.py` is the registry and `CapabilityResolver` maps each tool call
to one concrete action. `PolicyToolbox` checks an in-scope `write_file` target
for exact UTF-8 byte equality before policy classification. A match returns an
`already_satisfied` no-op with `changed=false`, no overwrite capability, and no
workspace mutation; different bytes remain `filesystem.overwrite` and use normal
policy. Parent directories are created automatically; a file occupying a parent
path is returned as `ParentPathIsFile` before filesystem mutation. `run_command`
maps only to supported Python, pytest, unittest, py_compile, Ruff, or Git
actions. `policy.py` validates the per-agent JSON policy and returns explicit
`allow`, `deny`, or `approval_required` decisions. Allow and ask rules are the only source used to derive the model-visible tool list; stale legacy tool selections cannot expose a capability. Deny and approval results never invoke the underlying tool. Filesystem rules support paths, extensions and max_bytes for every filesystem action. Agents with
legacy `permissions` are converted to the same engine, and the effective
policy is copied into every task snapshot.

These are application safeguards, not a security boundary against a hostile
local user or hostile executable code.

## Reusable skills

`control_center/skills.py` defines `freya-core` with all seven registered tools.
`storage.py` archives prior active Skills on startup but keeps their historical
versions. It also adds the required `write_file` guidance to existing core
definitions as a new immutable version, preserving their previous instructions.
New Skill imports are disabled during this temporary configuration.
`resolve_agent_skills` renders the assigned Skill without requiring a matching
capability. Its tools remain declarations only; Policy determines effective
schemas and checks each operation. The
worker renderer exposes purpose, relevant instructions/procedures and only
required capabilities that exist in the effective toolbox; recommended and
missing-recommended fields remain internal diagnostics. Procedures that require
unavailable tools or capabilities are omitted/adapted, and the rendered
context has a 64,000-character budget. Each task stores an immutable copy of
the resolved Skill, including its version.

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
limitations and objective success. Runtime exceptions skip model repair and
build the factual contract from the action ledger, artifacts, verification,
workspace diffs and failure class. Verification state is persisted separately.

The worker classifies recoverable, environment, policy, approval, invalid,
unavailable and unknown-tool requests. A fingerprint includes tool, capability,
normalized target and material arguments. After a denial, the worker rechecks
current policy state; an unchanged denied fingerprint receives
`ACTION_BLOCKED_PERMANENTLY_FOR_CURRENT_STATE` without invoking the tool or
charging another tool call. A changed target/argument or policy state can proceed
through normal authorization. Repeating that local block terminates as
`BlockedActionCycle`; distinct blocked actions retain the bounded cycle guard.
An unregistered name is `unknown_tool`, while a registered but unassigned name
is `tool_unavailable`. Neither emits a false capability request. Successful
commands become verification evidence only when their exit/output directly
supports configured completion criteria. Once the action ledger contains a
created artifact and evidence for every configured criterion, an already-
satisfied duplicate write can end the task without consuming more model steps.
When every criterion is an explicit artifact-presence/readability requirement,
the worker also ends after a matching read-back of the created file. This keeps
simple file tasks from looping through duplicate writes and reads.
For a task explicitly marked as single-case QA, the Agent Factory sets
`verification.stop_after_acceptance_evidence`; the worker then ends after one
successful controlled command supports every configured criterion. Output
assertions and exit-status assertions in the same criterion must both pass, and
quoted numeric output is matched as a complete number (`8.0` does not satisfy
`8`).

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
