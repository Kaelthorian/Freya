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

Prompt-debug configuration lives in the immutable `settings.Settings` startup
snapshot, independent of per-agent configuration and capability policy. The CLI
loads recognized environment settings over ignored repository-root
`.freya-local.json` values and defaults before constructing Runtime or any model
component. It prints one body-free `freya.runtime.configuration` event with
effective sources, checkout root, cwd, HEAD commit, entrypoint and orchestration
budget. Health exposes the same snapshot. There is no dotenv or DB override.
Runtime passes the same snapshot explicitly
through multiprocessing spawn; the worker installs it before execution. All
model adapters share `llm_trace.py` through `transport.model_request`; no component
reads the flag independently. Changing the environment requires a server restart.
Tracing observes the provider's effective streamed request, redacted preparse
content and best-effort JSON; existing validators report normalized results.
Debug rejection events correlate contract versions, rule messages and the last
call's redacted raw content. Output-contract validators have a shared
observational wrapper that re-raises the original exception unchanged. Debug
never changes model input, output limits, tool policy or evaluation decisions.
See [Prompt debug diagnostics](DEBUG_LLM_PROMPTS.md) for setup and capture limits.

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
and classifies that candidate before dispatch. The deterministic Execution Graph
determines **when** dependency-ready tasks run. A logical Worker Assignment can
own several Tasks; the Worker determines **how** each selected Task executes
and prepares one assignment-wide Final State Snapshot. Capability Policy remains the
sole authority for **whether** each requested action is permitted. The
Evaluator determines **whether the produced result actually satisfied** the
criteria assigned to that Worker after all its Tasks reach technical success.
Criterion provenance maps every decision back to its originating Task.
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
| Worker Assignment acceptance | Evaluator | One decision with per-criterion Task provenance |
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
`orchestration_workers` stores the current compiled Worker ID, active assigned
Task IDs, lifecycle status and current evaluation reference. A validated plan
revision updates this set atomically and reopens a Worker when its assigned
Tasks change. `orchestration_evaluations` stores one immutable, versioned
decision per Worker evaluation, including `worker_id`, the complete
`evaluated_task_ids`, its anchor Runtime attempt, bounded evidence snapshot,
criterion origins, model metrics and truncation/deterministic flags. Every
assigned Task node references the same evaluation. Runtime success is persisted
as `runtime_success`; accepted evaluation preserves that technical state and
sets `evaluation_status=accepted`. For a non-accepted result, only the first
failed origin Task enters `recovery_pending`; sibling Task nodes retain the
shared evaluation reference until an atomic retry or observation action resets
them. The evaluation insert, Task references and Worker state transition share
one transaction.
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
write because the agent has not seen the full source. After the Evaluator
accepts the Worker Assignment, ProjectState accepts only artifact paths
present in the Worker action ledger, verifies the file still
exists under the assigned workspace, and records its current hash. Worker
symbol reports are checked against the current artifact bytes and declaration
line; dependency reports are stored as observed metadata. Rejected or
unverifiable claims do not become verified state. Cross-task requests retain
the requester's observed artifact revision, while the owner receives a fresh
snapshot and must inspect and read the current file before editing.

When a mutating task has no successful write, a current read of every declared
target permits an `already_satisfied_candidate` result with artifact hash and
revision evidence. This is technical completion only; Evaluator judges each
planned criterion. A successful `ALREADY_SATISFIED` write counts as technical
Runtime mutation observations after exact current-byte comparison, even though it creates
no artifact or diff; Evaluator judges only the final state and semantic criteria still require model review. For
compiled assignments, current bytes attributed to an earlier Task in the same
Worker assignment may also be cited after read-back; evidence from another
assignment is ineligible. Without a successful changed write, exact no-op
write, resulting artifact, or current observation candidate, the Worker retains
`ExpectedWorkspaceMutationNotObserved`.
When repeated reads have already
observed every declared target and every current criterion is a simple
file-presence check, the Worker stops successfully with an
`already_satisfied_candidate` in its audit result instead of entering a read loop;
Evaluator prepares a fresh current observation before judging presence;
other repeated reads still stop as `NoProgressDetected`. Dispatch prompts include a read-only,
bounded view of every task in the effective compiled plan, in plan order. Each
record includes status, kind, objective, description, dependencies, criteria,
write targets, owned paths, semantic operations and declared capabilities/tools.
The current task is highlighted separately, with non-goals derived from other
tasks. Direct successful dependencies contribute only a short result summary and
artifact paths, never action logs or file contents. The view is capped at
70,000 characters and truncates individual text fields; exceeding the cap fails
closed. `worker.plan_context_prepared` records IDs, counts, successors and size.
The view does not enter policy construction, so other tasks' declared tools
remain unavailable. Generated agents also receive `control_center/agent.md`
as global scope guidance. Agents can report relevant symbol kind, signature, path and
purpose; ProjectState verifies names against accepted artifact bytes when possible.


Runtime and orchestration events keep independent source-local integer IDs. The storage read model merges both tables by timestamp, source and source-local ID, and labels each normalized row with `source`, `source_id` and a collision-safe `log_id` (`runtime:<id>` or `orchestration:<id>`). `step.started` and `step.finished` events build the reconstructable timeline while every attempt remains in `log_events`; successful file writes and edits additionally emit a bounded `workspace.diff` event so the created code is inspectable without relying on Git availability. The worker also persists every successful or denied runtime action in the structured result. A successful `run_command` whose output directly satisfies a quoted-output, exit-code, or JSON completion criterion becomes `command_execution` verification evidence; final verification flags derive from that evidence rather than model prose. The persistence layer normalizes every runtime and orchestration event with `who`, `actor_name`, `actor_role`, `actor_type`, `where`, `workspace`, `when`, `phase`, `action`, `what`, `how` and a stable `trace_id`, derived centrally from the assigned agent, task snapshot and orchestration. `GET /logs` reads both sources by default; `orchestration_id` filters the merged stream, while task-specific log queries remain runtime-only unless explicitly asked to include orchestration rows. Global SSE uses a composite `runtime=<id>;orchestration=<id>` cursor so reconnection can resume both source sequences; task SSE retains its numeric runtime cursor. On startup, abandoned Queued, Running, WaitingForApproval or Paused records become Failed, pending approvals are denied as cancelled, and unfinished steps are closed.

When `write_file` returns `ALREADY_SATISFIED`, the action ledger records
`changed=false` and `already_satisfied=true`. The Worker may end execution when
that exact write was already changed in the current run, the same satisfied
write repeats, or it is the last tool call in the current model response. This
is execution completion only: the result, artifacts, workspace diffs and
verification evidence are preserved, no semantic criterion is marked satisfied
from the no-op, and the Evaluator reviews it with the rest of the Worker
Assignment evidence after all its Tasks finish.

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
Assigned Workers additionally emit `worker.created`, `worker.reused`,
`worker.task_switched`, `worker.task_started`, `worker.task_completed`, and
`worker.completed` with bounded Worker, agent, orchestration, task, strategy,
active-tool and active-capability metadata. Explicit Recovery replacement or
recreation emits `worker.recreated`.
The Orchestrator emits `worker.execution_completed` when every assigned Task
has technical success, or `worker.execution_failed` when Runtime failure
prevents evaluation. Each Runtime-successful
Task emits `worker.task_completed` with `evaluation_status=worker_pending`;
once an assignment is complete, `worker.evaluation_started`, per-criterion
completion/insufficient-evidence events, and `worker.evaluation.completed`
identify the Worker, assigned Task IDs, evaluation ID and origin Task IDs.
Legacy `freya.evaluation.*` events remain once per Worker Evaluation for older
activity clients. Recovery adds `worker.recovery_started` and
`worker.recovery_completed`, including failed criteria and affected Task IDs.
Recovery and Integration revision events include the semantic operation to
capability to tool resolution records for new tasks. Evaluation completion
events carry the validated criterion-by-criterion decision and metrics;
non-accepted decisions also carry a bounded summary of the exact input
evidence. Evaluator prompts are excluded by default. Optional debug tracing
stores bounded, redacted prompts and responses; private reasoning is stripped.

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
deterministic Task Spec floor before grounding validation runs. Grounding compares
reliable information: artifact paths, resolved languages/interfaces, stack
technologies, explicit numeric constraints, action groups and external scope.
Surrounding words and faithful translations do not invent scope. Unsupported
scope diagnostics include the statement, new and grounded facts, field and reason.
One unchanged rejected repair emits `task_analysis.repair_unchanged` and immediately
uses fallback; there is no additional grounding model call.
Canonical `source_prompt` preserves internal whitespace for exact comparison
with immutable orchestration audit text. Display/requirement normalization must
not alter that field. A pre-persistence error fails the current analysis run;
the compare-and-set guard uses the persisted baseline until save succeeds.

`Planner.create_plan_for_spec` receives the canonical Task Spec and emits
Semantic Plan schema version 5. The plan records `task_complexity`,
`execution_strategy` and a concrete `decomposition_reason` independently of
task count. One worker handles cohesive work even across several files or steps;
multiple workers require a benefit beyond delegation and integration cost.
Each task contains `task_kind`, semantic needs,
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
registered operation raises `UnsupportedResourceRequirement`; an incorrect
supported claim enters bounded repair. The repair payload contains the previous
Semantic Plan, structured compiler diagnostics, rejected-plan snapshots,
preservation rules and an explicit `required_plan_delta`. An orchestration-local
`RejectedSemanticPlanRegistry` records every Compiler and local guard rejection.
Before another Compiler call, `SemanticPlanRepairGuard` compares normalized
fingerprints and the task/action graph against the entire history. IDs, JSON
field order, timestamps, metadata and wording-only edits do not make a proposal
new. A rotation back to any rejected plan is rejected locally. A distinct plan
must also satisfy the latest cause-specific delta; overfragmentation checks the
Compiler decomposition invariant, while dependency repairs check resolution,
self-dependencies and cycles. Each repair prompt includes compact rejected-plan
history and the explicit delta, and a full replan is requested after an
equivalent or otherwise unusable repair. The loop permits one initial proposal
and at most three Planner repairs. Exhaustion raises
`PlannerUnableToProduceAcceptablePlan` with a reason and rejected-plan
diagnostics, without sending an equivalent or cause-unresolved proposal to the
Compiler. `PlannerUnableToProduceMateriallyDifferentPlan` remains a compatibility
base class and `RepeatedSemanticPlanError` a secondary exact-repeat defense. A
materially corrected proposal still passes normal Compiler validation before
reaching Runtime.

The Compiler rejects a declared multi-worker graph whose tasks are a simple
implementation chain, overlap the same write target, or cite only file/step
count as justification. Before every repair reaches the Compiler, the guard
checks both the complete rejection history and the active cause-specific delta.
In particular, an overfragmented `simple` plan must reduce its task count to the
rejected plan's expected maximum or add a concrete reason grounded in independent
outcomes or preserved boundaries. A valid alternative still passes through all
normal Compiler checks. Fingerprint, registration, delta-check, repeated-plan,
required-delta-failure and material-acceptance events make this decision
observable.
For `single_worker`, semantic task count does not imply delegation count: the
Compiler retains independently meaningful checkpoints and assigns them to one
logical worker slot. Before assigning runtime IDs, `plan_granularity.py`
absorbs only mechanical, single-consumer filesystem prerequisites into their
direct logical successor. It unions operations/resources, preserves meaningful
final criteria and regenerates ownership, criterion links and assignments from
the normalized graph. QA, parallel work, independently useful producers and
explicit approval/security/phase/recovery boundaries remain separate. Existing
compiled graphs and Recovery references are untouched. Advisory Task ranges and
granularity events are described in [PLAN_GRANULARITY.md](PLAN_GRANULARITY.md).
`worker_assignment.py` deterministically groups dependent steps
for `multi_worker` and separates independent branches or QA/review roles.
The compiled plan persists `execution_strategy`, `task_count`, `worker_count`
and `worker_assignments` (`worker_id` plus ordered `task_ids`).
`plan_compiler.worker_assignment_created` records those counts and groups.
Assignments are recomputed from validated tasks when a plan transform adds or
removes a task. The runtime validates that declared assignments cover every
task exactly once and does not infer missing groups. They grant no capabilities:
AgentFactory creates one stable agent record per Worker assignment, then
`activate_task` replaces its effective policy, tools, write scope, and
verification criteria before each dispatch. Runtime task snapshots freeze that
per-task policy while keeping the Worker `agent_id` stable across its assigned
Tasks. If a later Integration revision appends work to an assignment, the
runtime reuses that Worker only when the same Worker ID retains its prior
ordered task IDs as a prefix; a reordered or unrelated assignment is a new
lineage. Only an explicit Recovery action can create a replacement generation.
For accepted graphs it assigns one permanent owner
per normalized path: the unique creator, otherwise one explicit owner, otherwise
the sole writer. A duplicate ownership claim from a noncreator is converted to
a foreign write when the creator is unique. Before ownership assignment,
unordered tasks that share a write target fail with `WriteScopeOverlap`; shared
targets are accepted only when the dependency graph establishes a sequential
handoff. Two creators invalidate the plan. The overlap diagnostic records the
path, both task IDs, dependency ordering and bounded responsibility similarity
for one Planner repair. It validates every operation against the catalog and derives
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
criteria pass through `plan_evidence.py`, which classifies the minimum evidence
as artifact existence, static content/structure, runtime/test, compilation,
visual, external state or task output. The Compiler compares that requirement
with derived capabilities. It moves an incompatible criterion only when one
dependent verifier can prove it, otherwise it raises
`CriterionEvidenceMismatch` for bounded Planner repair. Every classification,
reassignment and rejection is recorded without storing model prompts. The
Compiler links an exact global/local match but never appends a global criterion
to the final task by position.
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
The Evaluator accepts evidence once per Worker Assignment and retains each
criterion's origin Task. The Orchestrator owns lifecycle and dependency
coordination. Recovery may replan only its deterministic local
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
`waiting_for_approval`, legacy `evaluating`, `runtime_success`,
`recovery_pending`, `blocked`, `success`, `failed`, `cancelled`, `skipped`, and
`superseded` nodes. A technically successful Task in `runtime_success` releases
its dependencies before its Worker Assignment is semantically evaluated.
Failure or cancellation blocks descendants transitively while unrelated
branches continue. Join nodes wait for every dependency.

Ready-node fairness follows the immutable plan order. Selection occurs exactly
once when a node first becomes ready and its durable snapshot is reused while
waiting. The scheduler admits at most `max_parallel_tasks` active graph nodes
(default 4), never submits two tasks concurrently to one Worker assignment, and also honors
Runtime's agent/workspace serialization. A selected Paused, Offline, or busy
agent leaves the node ready with a `waiting_reason`; it is not submitted
repeatedly or silently reselected. Disabled and deleted agents fail as described
below.
Selection and dispatch are interleaved: after selecting a ready task, the
scheduler reserves and attempts to dispatch it before selecting the next task.
The next Agent Selector call receives workload from active Runtime tasks,
active graph nodes, and selected ready-node reservations, so parallel branches
do not share stale workload information.
Dependency readiness remains authoritative within one Worker assignment;
assigning two tasks to the same Worker never releases a task early. Distinct
assignments remain independently schedulable and can execute in parallel.

Paused and Offline are temporary scheduling waits: the selected node remains
ready with one stable `waiting_reason` and dispatches once after availability
returns. A selected agent that becomes disabled or is deleted is a durable
execution failure for that node. Only an explicit semantic recovery action may
reselect; ordinary scheduler failures never cause silent reselection.

Runtime `Queued` and `Running` map to graph `running`;
`WaitingForApproval` maps to `waiting_for_approval`; Runtime `Paused` remains a
nonterminal `running` node with an explicit reason. Runtime `Success` maps to
terminal technical state `runtime_success` and immediately releases dependency
readiness; it does not mark semantic acceptance. The Scheduler invokes one
Evaluator call when every active Task in a Worker Assignment is
`runtime_success`. An accepted decision leaves those nodes in `runtime_success`
with a shared accepted evaluation reference. `needs_revision`, `rejected`,
`blocked`, and Evaluator infrastructure failure put the criterion's origin
Task in `recovery_pending` and preserve the shared evaluation reference.
Recovery maps that origin to bounded retry/replan scope, or schedules an
existing same-Worker read-only Task to gather evidence. Retry clears current
Worker references while immutable attempt, evaluation, selection and recovery
history remains. Other Runtime statuses map to their graph equivalents. A
fully accepted graph enters `Integrating`; it does not
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

`Evaluator` version 11 judges the current final result once all Tasks in a Worker
Assignment reach `runtime_success`. It is tool-free. The parent Orchestrator
first calls `final_state.build_final_state` with the selected workspace and
completed Runtime records in durable dispatch insertion order. The snapshot
(version 1) contains current files (`path`, `exists`, `readable`, UTF-8 content,
hash and explicit truncation), authoritative `verification_facts`, and bounded
final task outputs marked as agent claims. Filesystem targets come from planned
write/owned/read paths, named criterion paths and observed artifact paths;
historical paths nominate files to inspect but never prove their current state.
The reader reuses `Toolbox.safe_path`, rejects workspace escapes, never executes
commands and never creates a workspace. Binary/oversized/unreadable files retain
objective metadata and explicit content limitations.

`final_verification_facts` selects the last observation of each stable identity:
explicit verification/test/check ID, otherwise command argv plus stdin digest,
otherwise the check label. Event IDs are provenance, not check identities.
Distinct stdin cases remain distinct. Legacy redacted stdin without a digest is
marked unidentified and cannot safely supersede another case. An action and its verification row are
merged by event ID before supersession. No timestamp determines identity;
serialized Task dispatch insertion order (including Recovery attempts) determines
which observation is final.
Parent-owned `verification.case_completed` and `step.finished` ledger events
also reconstruct these observations when structured result actions are absent.
The case's exact stdin and criterion IDs survive the later step event, whose
`input` is command metadata. Diffs/readbacks nominate current file targets only.
Older failures remain in Runtime logs for audit and
Recovery and cannot enter normal semantic input. Facts preserve exit code,
stdout/stderr (when available), merged legacy output, status and source Task/
Runtime/Worker. The parent's Runtime assignment takes precedence over claimed
record provenance. Final agent outputs use `final_output:<runtime-id>` references
and remain claims. Test runner summaries expose counts and failed case IDs; these
facts never decide semantic correctness. Legacy merged streams are explicitly
marked unavailable rather than fabricated.

`plan_evidence.verification_mode` separates evidence availability from decision
authority: bare file presence (`file_exists`) and readability (`file_readable`)
inspect current snapshot metadata. Full-match grammars also identify explicit
command/suite/lint/compiler success. Execution decisions need declared exact
criterion links or an unambiguous legacy runner-wide fact. Missing case links
never imply positional matching. Unavailable infrastructure produces `unknown`
and Orchestrator routing. Content equality, symbol presence, modification history,
`already_satisfied` and generic correctness remain semantic. `verifiable` in Compiler
telemetry means the compiled resources can produce evidence, not that Evaluator
can interpret the criterion without a model. Compiler events also report the
verification mode and decision authority. Passing facts never prove arbitrary
semantic behavior. Historical actions remain audit/Recovery data.

Evaluator version 11 receives the compiled expected cases and local-to-global
criterion links for the whole assignment. Coverage checks require the declared
case ID, source Task and exact input digest. Missing cases gate even semantic
criteria and are reported by ID; failed cases are contradictory evidence.
An explicitly mechanical aggregate execution criterion (`case_set_success`)
requires its complete bound case set, passed statuses, integer zero exits and
available stdout/stderr. Output correctness remains semantic. Global criteria
reference the same fact IDs through `global_local_link`; equivalent surface
wording is evaluated once. No duplicate facts are manufactured.
Existing relevant unbound cases produce `evidence_binding_error`, not missing
evidence; insufficient associated observations remain distinct from absent or
contradictory ones. Binding errors route to Orchestrator and never execute QA.

Explicitly required test/lint/build/compilation/command evidence is a hard gate.
A missing run with available resources produces `missing_required_evidence`,
`blocked`, `gather_evidence`, and routing to `worker`. An unavailable capability,
tool, denied check or missing sandbox produces `required_capability_unavailable`
and `routing_target=orchestrator`. Resource checks use immutable task activation
resources where available. Freya records the system routing and conservatively
fails the current run for resource review; it never grants capabilities or
retries the same incapable Worker. The Orchestrator may gather missing evidence
with an existing read-only observer or testing Task inside the same assignment.
Successful mutation Tasks are retained. No model guesses unexecuted results.

Evidence collection and binding complete before any decision. `normalize_final_state_evidence`,
called by `Evaluator._bounded_context`, builds full facts and an explicit
`criterion_evidence` map keyed by local/global criterion ID. Facts retain their
stable evidence ID, criterion ID, type, source, case ID, status, input, streams,
exit code and current file path/content as available. Declared case bindings use
the compiled IDs; file readbacks bind by an explicit path or a single planned
write target. The Compiler populates `supports_global_criteria` from exact text,
shared artifact paths, strong shared semantic concepts or an aggregate global
criterion. Global checks reuse those local evidence IDs; they do not create new
Runtime executions. `evaluator.evidence_prepared` records these ID associations
before deterministic evaluation and before any semantic model call.

The Evaluator resolves objective checks first. Only unresolved semantic criteria
enter `_semantic_context`, each with its criterion ID/text and full associated
facts. The model receives the criterion-ID-to-evidence-ID map and these grouped
facts, without a global `final_state` evidence bag. The context has a
20,000-character budget and 4,000-character content excerpts; omitted/truncated
observations remain explicit. Actions, diffs, superseded readbacks, aggregate
historical verification flags, dependency result histories and `worker_context`
do not enter semantic input. The durable evaluation catalog is derived
exclusively from the same final snapshot; separate Runtime/Worker history remains
available to audit and Recovery. Sanitization runs before model input, logging
and persistence. Evidence and agent claims are untrusted data.

The semantic response remains a strict `criteria` array (`criterion`, `status`,
`reason`, `evidence`, `confidence`). Python aggregates independently judged
criteria: `unsatisfied` → `rejected`, otherwise `unknown` → `blocked`, otherwise
`partial` → `needs_revision`, otherwise `accepted`. An unavailable resource
forces system routing to Orchestrator. The public status/action contract remains
compatible; routing fields are system metadata. Failed criteria retain their
origin Task, cited failed facts and affected artifact paths for granular Recovery.
Satisfied Tasks/criteria are preserved; a failed test does not itself rerun the
entire assignment.

Each invalid semantic response gets one repair; one retry uses the same immutable
final snapshot, for at most four model calls. Exhaustion is infrastructure
`error`, not a Worker retry. Existing model/endpoint settings, loopback restrictions,
serialization, cancellation and atomic commit checks are unchanged. Offline mode
resolves presence/readability and evidence gates; unresolved semantics stay
`unknown`. Evaluation rows use version 9 and JSON snapshot version 1; previous
rows remain immutable and readable. No SQL schema migration is required.

## Semantic recovery and replanning

`RecoveryController` is separate from Planner and Evaluator. Its strict schema
allows `gather_evidence`, `retry_same_agent`, `retry_different_agent`,
`replan_subgraph`, or `fail`; invalid model output gets one repair. Its Ollama
adapter is loopback-only, tool-free, streamed through `transport.py`, and
independently metered. `--recovery-offline` makes no model call. A blocked Worker
evaluation that recommends `gather_evidence` with missing evidence chooses a
completed same-assignment Task whose compiled operation/capability/tools are
read-only; that Task is rerun with its existing read surface while the failed
origin Task remains technically complete. If no observer Task qualifies,
recovery fails closed. Other `needs_revision` and `blocked` decisions reuse the
exact generated agent after revalidation, while `rejected` requests a new
generated variant with the same task-derived policy ceiling. Evaluator `error`
fails. The normal attempt, action, fingerprint and wall-clock limits still apply.

Recovery version 5 reuses independent facts from previous assignment attempts,
superseding repeated check identities. A later material workspace mutation
invalidates earlier executable checks, including checks of dependency modules.
For declared cases, the existing QA
activation is narrowed to `missing_verification_cases`; already observed cases
are not rerun. The failed QA Task may itself be the observer. Its real dispatch
increments its execution attempt; an unchanged successful mutation attempt does
not gain an artificial attempt. Material snapshot fingerprints exclude delivery
IDs/timestamps. Reproducing identical evidence stops with
`recovery.evidence_no_progress`, before a second gather action. Reusing an already
consumed source attempt reports `recovery.source_conflict` without an SQL failure.
`commit_recovery_action` compares an existing source key in the same write
transaction: equivalent decisions return that row; conflicting decisions raise
`recovery_source_conflict`. The UNIQUE constraint remains intact.

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

Integration version 6 requires criterion-specific grounded proof, not merely
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
`superseded` history) must be `success` or `runtime_success`, retain an
`accepted` evaluation and have no pending, ready, running, approval, evaluating,
recovery or failure state. The context contains the original prompt/goal/global criteria, current
effective plan revision, active task objective/result summary, accepted
evaluation summary/evidence/verification and a compact revision history. Text
and list bounds set `context_truncated`; the serialized model context is capped
at 48,000 characters and fails closed if it cannot be reduced safely. Result
and evidence text are always untrusted data and never instructions.

When exactly one active Worker Assignment has an accepted Worker Evaluation
that already covers each exact global criterion with direct evidence,
`integration_orchestrator.py` reuses those proof refs and records the
integration result without another verifier call. Multiple active Workers,
cross-Worker criteria, or missing exact proof continue through global
verification.

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
QA Tester or Code Auditor agents. For plans with `worker_assignments`,
`AgentFactory` creates one candidate per logical Worker and `Orchestrator`
reactivates that same `agent_id` for each assigned task. Every activation
recomputes policy from the current compiled task; Skills and Worker identity do
not widen capabilities. Legacy plans without assignments retain task-specific
agents. The factory assigns `freya-core`, ignores Planner Skill preferences
with a warning, and never turns Skill tool declarations into capability grants.
Manual agents remain available for direct task submission and compatibility
tests.
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
agent and waits for a global slot, per-agent availability and its logical worker
assignment. Only one task in an assignment can be selected or dispatched at a
time; dependency edges still control when the next task becomes ready. Approval is
the existing durable Runtime flow and does not fail the graph while pending.
At timeout, active children are cancelled and all remaining graph nodes become
terminal before the parent becomes Failed. The selector itself does not create
agents, replan, retry semantic revisions, or enable agent-to-agent messaging;
agent construction remains the factory's separate responsibility.

## Runtime and control semantics

The scheduler supports bounded concurrency and serializes tasks per Worker
assignment and per resolved workspace. An agent can set an existing absolute default directory;
task submission can override that path for one run, and an explicit empty
override creates a fresh directory under `data/workspaces/`. Every task records
the resolved workspace in its immutable snapshot and runs in a spawned process. A Freya orchestration allocates an empty-selection workspace once, persists it in the orchestration config, and passes that same path to every dependent node so implementation, verification, and the read-only Code Auditor observe the same files.
On Windows the worker is assigned to a kill-on-close Job Object before tool
execution; POSIX uses a process session. Cancel, restart and shutdown terminate
the worker tree and persist a terminal event.

Worker identity and Runtime process lifetime are separate: the same generated
agent record can serve several plan tasks, while each Runtime task still runs in
its own spawned process and receives an immutable task snapshot. Ollama's current
chat transport is stateless across calls, so context continues through a bounded
deterministic prompt containing the active task, prior accepted summaries,
relevant same-assignment artifacts, predecessor summaries, and plan context;
obsolete task instructions are not reused as the active task.

Pause is cooperative: an in-flight model or tool call can finish, then the
worker pauses between actions. When a capability or autonomy rule is ask, the
worker emits approval.requested, the parent persists the request, changes the
task to WaitingForApproval, and blocks the worker until once/task/deny is
resolved. Cancellation denies pending requests and terminates the worker. Ordinary
pause still consumes execution time. Human approval waits suspend the Worker
guard and parent scheduler budget; resolution rebases the parent start time and
the Worker deadline. Orchestrator excludes whole polling intervals, including
database reconciliation, only when every
live graph task is WaitingForApproval; another executing branch still consumes
budget. Approvals have no implicit expiration. The orchestration default is
1800 seconds, exactly twice the previous effective 900, configured centrally by
`FREYA_ORCHESTRATION_TIMEOUT_SECONDS` or `--orchestration-timeout`.
Progress is the greatest fraction of the configured step, model-call and
tool-call budgets and reaches 100 only at termination. Token usage remains
visible in metrics, but the task token budget is unlimited when `max_tokens=0`
(the default); wall-clock, step, model-call and tool-call limits still bound
runtime resource use. Even in that mode, each Worker model call is capped at
2048 output tokens (768 for structured-output repair); the cumulative task
token budget remains unlimited.

### Independent verification cases

Planner tasks may supply `verification_mode=independent_cases` and bounded
`verification_cases=[{id,input,supports_criteria?}]`. Optional `supports_criteria`
names semantic success-criterion texts. Before grouping, Compiler validates only
the case shape, bounds and absence of model-chosen runtime IDs. After granularity
normalization, resource compilation and criterion evidence reconciliation, it
allocates local IDs and resolves references against the final local criteria as
`supports_acceptance_criterion_ids`. The shared exact surface matcher normalizes
whitespace and paired Markdown code around paths; zero matches produce a typed
unknown-reference error and multiple matches produce an ambiguity error.
Reference identity retains diacritics and uses the existing case-insensitive
text convention; it does not apply the mechanical grammar's accent folding.
The relation is many-to-many: three cases may support one aggregate criterion,
and the invalid case may additionally support a semantic behavior criterion.
Support identifies relevant evidence, never complete proof or a permission.
Compiler validates unique IDs, preserves
exact stdin and requires a derived `run_command` resource. A conservative legacy
bridge recognizes explicit comma/conjunction input lists, never splits arbitrary
multiline stdin. `interactive_session` preserves ordered inputs in one process.
`agent_factory.py` refreshes this contract on each activation of the same Worker.
Worker actions carry resolved links into final facts. Evidence normalization
retains case/input/digest/check identities and associates each fact directly with
its criterion using `declared_verification_case`, including semantic criteria.
Resolved IDs take priority over stale text and contextual aliases in case facts.
Cases without references remain valid, execute normally and enter the unbound
evidence pool. Binding events report Task/case/reference/resolved IDs, never stdin.
`VerificationCriterionReferenceError` supplies final available criteria and
original semantic Task keys for narrowly scoped Planner repair. If a previously
reconciled reference disappears before binding, the technical
`VerificationBindingInvariantError` stops planning without model repair.
The model schema requires case decisions on testing tasks and permits only empty
case arrays on other kinds. Older Planner output can inherit a clear canonical
input list when exactly one Python verifier exists; ambiguity is not guessed.
Equivalent sibling testing tasks with typed independent cases, the same Python
script, operations and dependencies coalesce into one batch task. Compiler
preserves all case IDs/criteria and rewires dependent semantic keys before DAG
validation, emitting `plan_compiler.verification_cases_grouped`. Different scripts,
ordered sessions, write declarations and different resources are never merged.

After one model decision selects argv, `verification_cases.py` expands that
command into one process per case. The Worker dispatches each through the
existing policy and orchestration venv without model calls between cases. A nonzero
exit retains its result and permits the remaining cases to run; inability to
execute, denied permissions or cancellation still use normal failure handling.
`verification.case_completed` and runtime actions retain case ID, exact input,
status, exit code and separate streams. `final_state.py` carries these facts into
the assignment snapshot and observes the current Python script referenced by
executed argv through the same workspace safe-path boundary, including QA-only
assignments. The semantic Evaluator decides whether the observed
behavior meets the requested criteria. Technical command status cannot accept
semantic behavior. Sandbox subprocess pipes use UTF-8 bytes so Windows text mode
cannot convert LF input into unintended CRLF bytes in Linux.

A write task that returns fenced code without any tool action receives at most
one bounded action correction within its existing model/time budgets. The code
is never written automatically: only actual allowlisted tool calls can change
the workspace, and the missing-mutation guard remains enforced.

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
`task.no_progress` and stops the action loop with `NoProgressDetected`; increasing
the step budget is not treated as a fix. Three successful no-op `edit_file` calls
on the same artifact also stop early, even when their replacement arguments
differ. A new file read, newly supported acceptance criterion, or material
write resets that edit counter. Writes and process execution are never
automatically retried.

`NoProgressDetected` enters the [Worker forced-finalization contract](WORKER_FINALIZATION.md).
The Worker makes one terminal LLM call with `tools=[]` and a strict
`COMPLETED`/`BLOCKED` JSON schema, using accumulated, sanitized evidence only.
No verification tool or format-repair call follows it. `COMPLETED` returns
technical Runtime `Success`, which becomes `runtime_success` and releases DAG
dependents; it does not establish semantic correctness. Evaluator still runs
once after every Task in that Worker Assignment reaches `runtime_success`.
`BLOCKED` returns `Failed` with its operational reason and missing capability,
without granting permissions. Invalid output fails as
`ForcedFinalizationInvalidOutput`. The terminal call consumes remaining call,
token and time budgets; exhausted budgets never extend execution. Other failure
paths, including `BlockedActionCycle`, retain their existing behavior.

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
- Python, tests and Ruff use the orchestration-owned venv in
  `python_execution.py`, absolute interpreter paths and workspace cwd. A venv
  provides dependency isolation, not filesystem/network/resource containment.
  Execute permission therefore trusts code with the host account permissions.
  Git inspection retains the Docker copy in `sandbox.py`. See
  [Python execution](PYTHON_EXECUTION.md) for lifecycle and evidence boundaries.
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
path is returned as `ParentPathIsFile` before filesystem mutation.
`edit_file` checks its exact candidate bytes against the original after the
normal unique-match and `filesystem.modify` policy checks. An identical result
returns `already_satisfied=true`, `changed=false`, and no file write or workspace
diff. The Worker records artifacts and workspace changes only for `changed=true`.
`run_command` maps only to supported Python, pytest, unittest, py_compile, Ruff, or Git
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
An ordinary invalid structured response, including prose, receives one repair attempt.
If fallback normalization is needed, `task.result_contract` logs a bounded,
sanitized preview and repair details; format failure stays separate from task
limitations and objective success. Runtime exceptions skip model repair and
build the factual contract from the action ledger, artifacts, verification,
workspace diffs and failure class. Verification state is persisted separately.
Forced-finalization decisions instead build the factual result directly from
the ledger plus validated terminal metadata, with zero repair attempts.

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
