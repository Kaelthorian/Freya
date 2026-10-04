# Control Center API

All routes are same-origin under `/api` and return JSON errors as
`{"error":"message"}`. Mutations accept `application/json`. SSE is
`text/event-stream`.

`GET /api/health` includes `configuration`, the non-secret startup snapshot:
debug flag/source, orchestration budget/source, repository root, process cwd,
HEAD commit and entrypoint. It mirrors stdout `freya.runtime.configuration`;
changing environment/local settings requires server restart.

`GET /api/capabilities` returns the structured capability registry used by the
editor. `GET /api/config` includes it as `capability_catalog`. Agent JSON
includes `capability_policy`; action rules use `mode` (`allow`, `deny`, or
`ask`) and may include workspace-relative `paths`, case-insensitive
`extensions`, and `max_bytes`.

Agent configuration also accepts JSON blocks `identity`, `behavior`,
`autonomy`, `verification`, and `output`, or their top-level editor aliases.
Agent responses include `context_preview`, a read-only context rendering with
no secret values.

The active Skill catalog contains only `freya-core`, assigned automatically to
every dynamic agent. Its `tools` list declares all seven registered worker
tools; it grants no permission. Capability Policy filters the effective
surface and evaluates every invocation. Tasks store immutable Skill snapshots.

## Freya orchestration

`POST /api/orchestrations` with `{ "prompt": "...", "workspace_path": "..." }` queues a bounded run. An existing absolute workspace is used directly; when omitted or empty, Freya creates one isolated workspace for the orchestration and shares it across all planned nodes.
The trimmed prompt must be non-empty. The application does not impose an
artificial character limit before creating a run.
When the Analyst needs a high-impact answer, the run has status
`NeedsClarification` and `task_spec.clarification_questions` contains bounded
`{id, question, reason, field, required}` objects. Submit answers with
`POST /api/orchestrations/{id}/clarifications` and body
`{"answers":{"CQ-1":"Python de consola"}}`. This resumes the same run;
it creates neither an approval nor a new orchestration. The route rejects
unknown IDs, missing required answers and new answers on runs that are not
waiting. An exact replay of a recorded submission is idempotent. Question IDs
are runtime assigned and monotonic within the run: an unresolved question
keeps its ID, while a new semantic question gets the next ID. Answered or
optional model questions are removed before the next spec is saved. At most
three answer rounds are accepted; a remaining material ambiguity fails the run
with `task_analysis.clarification_cycle_detected` instead of asking again.
`task_spec`, `task_spec_revisions` and `clarification_answers` are returned
by the run detail API. While a ready run is still `Planning` and has no saved
plan, `POST /api/orchestrations/{id}/revise-spec` accepts
`{"field":"interface","value":"desktop_gui","user_message":"mejor quiero interfaz gráfica"}`.
It records Task Spec v2, queues replanning and rejects a late plan from the old
version. Once a plan exists, the route rejects the revision; active work is not
silently changed. The current spec is canonical; each revision is
immutable and versioned.

`GET /api/orchestrations` lists runs and `GET /api/orchestrations/{id}` returns
the run, immutable `plan`, `plan_schema_version`, `plan_created_at`,
`planning_metrics`, selection snapshots, delegations, execution attempts,
evaluations, Worker lifecycle rows, recovery actions, plan revisions, and events
needed to reconstruct it.
Freya also persists bounded per-run ProjectState metadata for generated agents.
The internal `project_context` worker tool can query artifact, symbol, task and
summary metadata from the latest dispatch snapshot; it never returns file
contents and has no HTTP route. ProjectState changes are parent-owned and worker
candidate updates are committed only after the Task's Worker Assignment
evaluation accepts the shared result.
The requester's observed artifact revision is recorded with cross-task
modification requests so the owner can inspect the current snapshot and reread
the file before changing it.
For generated tasks, `modify_file` and `overwrite_file` compile with
`filesystem.read` and `read_file`. The worker accepts an existing-file write
only after a current read in the same attempt; `READ_BEFORE_WRITE_REQUIRED`
and `STALE_ARTIFACT` are recoverable tool results. A zero-write mutating task
can return `already_satisfied_candidate` with observed path, SHA-256 and
revision when its declared targets were read and remain current. With compiled
Worker assignments, the source artifact must belong to an earlier accepted
task in that same assignment (or the current task's recovery attempt); another
Worker's artifact cannot satisfy the mutation contract. Evaluator accepts or
rejects the task against its criteria; otherwise the worker reports
`ExpectedWorkspaceMutationNotObserved`.
`GET /api/orchestrations/{id}/activity` returns a backend-derived chronological
timeline and performance read model. Its `events` include system actors even
when `agent_id` is null; `phases` pair persisted start/end events and calculate
duration from timestamps; `llm` aggregates recorded model calls and tokens.
The summary includes total elapsed, processing, worker execution, clarification
wait and approval wait seconds. Phase durations can overlap, LLM time is inside
processing, and parallel worker execution is a union, so these values must not
be added to produce wall-clock time. The endpoint does not accept client-computed
metrics.
`GET /api/orchestrations/{id}/plan` returns
the plan and its version metadata directly. Existing agent and task routes
remain compatible.
`GET /api/orchestrations/{id}/graph` returns durable nodes in immutable plan
order plus a summary with per-state counts, active/terminal totals, a separate
`runtime_successful` count and a `complete` flag. `runtime_success` is a
technically complete Task and releases its dependencies; it does not mean that
the Worker's semantic evaluation has been accepted. Historical pre-4.3 runs return an empty node list and null
summary. `GET /api/orchestrations/{id}` includes the same `graph_summary`.
Nodes expose nullable `evaluation_id` and `evaluation_status` references, not
the full evaluation. All Tasks evaluated together for one Worker Assignment
share those references. `GET /api/orchestrations/{id}/evaluations` returns the
immutable evaluation records with status, summary, confidence, per-criterion
decisions, issues, missing evidence, recommended action, version, independent
metrics, `worker_id`, and `evaluated_task_ids`, plus truncation/deterministic
flags. It never returns evaluator prompts or the private input snapshot.
`GET /api/orchestrations/{id}/integrations` returns immutable global
verification history ordered by round. Each item exposes `round`, `status`,
`summary`, criterion decisions, cross-task issues, missing evidence,
responsible active task IDs, `plan_revision`, integration version, metrics and
truncation/deterministic flags. The private bounded snapshot is not returned by
the API. Historical pre-4.6 runs return `[]`.

The activity projection is assembled from the persisted orchestration and
runtime event timelines, evaluations and integration metrics. It does not add
a second metrics table or treat a system actor as an agent.

```json
[{
  "round": 1,
  "status": "accepted",
  "summary": "The complete objective is satisfied.",
  "criteria": [{
    "criterion": "The application works end-to-end",
    "status": "satisfied",
    "reason": "The accepted integration evidence covers the complete flow.",
    "evidence": ["verification:evaluation-id:1"]
  }],
  "cross_task_issues": [],
  "missing_evidence": [],
  "responsible_task_ids": [],
  "recommended_action": "accept",
  "plan_revision": 0
}]
```

`POST /api/orchestrations/{id}/cancel` is idempotent. It returns the unchanged
terminal run when the orchestration has already completed; otherwise it stores
`Cancelled`, prevents further delegation and cancels children in Queued,
Running, Paused or WaitingForApproval.
`GET /api/orchestrations/{id}/attempts` returns each immutable real dispatch,
including attempt number, selected agent/selection, Runtime/delegation IDs,
prompt, evaluation/recovery references, state and timestamps.
`GET /api/orchestrations/{id}/recoveries` returns strict versioned recovery
decisions. `GET /api/orchestrations/{id}/plan-revisions` returns immutable
cumulative effective-plan revisions. `GET /api/orchestrations/{id}/effective-plan`
returns `{ "plan": ..., "revision": N }`; the original route always returns the
immutable initial plan.

Recovery events are `freya.recovery.started`, `freya.recovery.decided`,
`freya.recovery.retry_scheduled`, `freya.recovery.replan_created`, and
`freya.recovery.exhausted`. A cancellation or timeout prevents late recovery
results from creating a decision, retry, revision, or delegation.
Terminal task-graph failure diagnosis emits `freya.failure_analysis.started`
and `freya.failure_analysis.completed`. The completed payload contains
`analysis_version`, `analysis_mode`, `cause`, cited `evidence_log_ids`,
`retryable`, `recommended_action`, and bounded metrics. The final run `error`
and `response` expose the same cause and report. The analyzer receives only
sanitized events already persisted for that run, executes no tools, performs
no retry, and cannot change policy or permissions.
Dynamic-agent lifecycle events are `freya.agent_factory.started`,
`freya.agent_created`, `freya.agent_policy.validated`,
`freya.agent_factory.failed`, and `freya.dynamic_agent.archived`. Terminal
success, failure, cancellation and startup interruption all run idempotent
archive cleanup.
Integration events are `freya.integration.started`,
`freya.integration.completed`, `freya.integration.failed`,
`freya.integration.recovery_started`, `freya.integration.replan_created`, and
`freya.final_response.created`. They contain compact IDs/status/counts, not
model prompts or complete output.

Production runs pass through `Queued`, `Analyzing`, optional
`NeedsClarification`, `Planning`, `Planned`, `Running` and
`Integrating`. `task_analysis.started`, `task_analysis.updated`,
`task_analysis.clarification_required`,
`task_analysis.clarification_received` and `task_analysis.ready` expose
the Analyst's decisions. `task_analysis.clarification_resolved`,
`task_analysis.clarification_deduplicated`,
`task_analysis.clarification_rejected` and
`task_analysis.clarification_cycle_detected` explain answer reduction and the
round limit when applicable. A pending clarification creates no plan or worker.
A ready Task Spec is rendered deterministically for Planner and workers.
Planner model output uses semantic task keys; the Plan Compiler generates
all internal task/criterion IDs and validates dependency references. Before
resource validation, Freya removes optional external actions and deployment
criteria that explicit/clarified Task Spec intent does not support. It rewires
dependencies and rejects a removal that would lose a Task Spec validation
criterion. The bounded sanitized pre-resolution task view is available as
`planning_metrics.planner_semantic_plan` and on
`freya.plan_compiler.started`; `freya.plan.scope_adjusted` records omissions.
An unneeded `run_command` proposal is removed, while a specific semantic
operation can derive one capability for Policy. Ambiguous required commands
still fail with `ToolCapabilityMismatch`.

A new plan uses schema version 1 with `criterion_links`:
the plan's `success_criteria` remain authoritative if the model omits global link
rows: the harness fills missing rows in criterion order, then deterministically
fills absent, blank or duplicate global/local IDs before validation. It preserves
valid IDs and checks references against the normalized global IDs. Extra,
duplicate or unrelated global link rows still fail validation. Local IDs cannot
reuse global IDs. A copied global criterion is mapped to a concrete task check
only when unambiguous; unknown support IDs require an unambiguous text match.
An Analyst AC ID or description used as a global-row placeholder is removed
only when local links resolve to the plan's concrete criteria by exact text.
The count appears in `planning_metrics.normalization`.
Model plans declaring links must cover every executable global criterion before
delegation. When a Planner global ID reuses an Analyst `AC-N`, its criterion
text must match that Analyst acceptance criterion. When it assigns IDs, the run also emits
`freya.planner.normalized` with aggregate assignment/duplicate counts and the
affected criterion-link structures. An ID-only correction does not consume a
Planner repair call.

```json
{
  "summary": "Inspect, repair and verify authentication.",
  "success_criteria": ["Authentication is repaired and verified."],
  "tasks": [{
    "key": "repair-auth",
    "task_kind": "code_change",
    "objective": "Repair and verify authentication",
    "description": "Inspect the login flow, repair the fault and verify the result.",
    "depends_on": [],
    "semantic_needs": ["Read and modify the existing authentication files."],
    "operations": ["read_file", "modify_file", "run_pytest"],
    "success_criteria": ["A regression check confirms that login works after the fix."],
    "owned_paths": ["src/auth.py"],
    "write_targets": ["src/auth.py"]
  }],
  "unsupported_requirements": []
}
```

`unsupported_requirements` is a Planner proposal, not a resource decision.
The Plan Compiler checks it against explicit Task Spec obligations and the
runtime operation catalog. A false claim enters bounded Planner repair; the
repair input contains `canonical_task_spec`, `previous_semantic_plan`,
`rejected_semantic_plans`, `compiler_error` and preservation rules. The Planner
normally allows one repair. If its response repeats a previously rejected plan,
a canonical plan fingerprint or the same action graph with the same compiler
error rejects it and forces one additional full replan. A corrected proposal
still passes all Compiler checks before it can be persisted or dispatched.
Plan-wide `success_criteria` belong
to Global Verification; task-level `success_criteria` belong to the local
Evaluator. Exact matching text may create a proof link without copying a global
criterion into a task.

This Semantic Plan schema is version 5. Each task includes a `task_kind` for
AgentFactory. Its `operations` are registered semantic IDs, not runtime tools
or permissions. The Plan Compiler maps
`modify_file` to `filesystem.modify`/`edit_file`, `create_file` to
`filesystem.create`/`write_file`, and `run_pytest` to
`execution.pytest`/`run_command`. The compiled plan schema is version 4 and is
the only input to AgentFactory. Legacy capability and tool declarations are
ignored as non-authoritative hints. Unknown operations and unsupported external
actions fail closed; a rejected plan normally receives one bounded repair.
Repeating a previously rejected proposal raises
`RepeatedSemanticPlanError`, emits `planner.duplicate_plan_rejected` and forces
a complete replan; exhaustion fails closed. The Planner owns `task_kind`; the Task Analyst records only canonical
user intent. A write adds read-back verification, overwrite requires explicit
replacement intent, and a Python mention in a debugging task does not
reclassify it as program creation.

A Task may contain several `operations` and `semantic_needs`. Optional
`granularity` contains bounded explanatory text in `logical_outcome`,
`independent_value` and `preserve_boundary`; optional plan-level
`granularity_reason` justifies counts above normal ranges. Unjustified `simple`
excess after normalization raises `OverfragmentedPlan`, even for `single_worker`.
These declarations
do not grant access. Before runtime IDs and ownership, Compiler absorbs only
safe mechanical prerequisites into their direct logical successor. Meaningful
checkpoints and independent QA remain separate. Existing orchestration logs
publish `planner.granularity_summary`, `plan_compiler.granularity_analyzed`,
`plan_compiler.tasks_merged` and `plan_compiler.granularity_warning`. Merge events
include proposed positional `source_task_ids`, generated `result_task_id`,
reason and operations; analyzed/summary events include before/after counts,
complexity and ID mapping. `planner.duplicate_plan_rejected` records repeat
attempts without persisting raw model output. See
[PLAN_GRANULARITY.md](PLAN_GRANULARITY.md).

For example, the compiler persists a runtime task equivalent to:

```json
{
  "id": "task-1",
  "task_kind": "code_change",
  "semantic_operations": ["modify_file"],
  "required_tools": ["edit_file"],
  "required_capabilities": ["filesystem.modify"],
  "owned_paths": ["src/auth.py"],
  "write_targets": ["src/auth.py"],
  "foreign_write_targets": []
}
```
Low-risk file/program creation remains one task; interactive work can add a
dependent QA task. The plan also persists `write_owners`, mapping normalized
paths to permanent plan-task IDs. A separate task intending to modify this file
keeps its own node, declares `write_targets: ["src/auth.py"]`, and receives
`foreign_write_targets: [{"path": "src/auth.py", "owner_plan_task_id": "task-1"}]`.
Two creators of one path invalidate the plan; ambiguous modifier ownership gets
one bounded Planner repair.
The compiled plan also contains `execution_strategy`, `task_count`,
`worker_count` and `worker_assignments`, for example
`[{"worker_id":"worker-1","task_ids":["task-1","task-2"]}]`.
Task count and Worker count are independent. The compiler emits
`plan_compiler.worker_assignment_created` with the strategy and both counts.
Existing persisted runtime plans keep their compiled `required_capabilities` and
`required_tools` snapshots and need no database schema migration. New planning,
Recovery and Integration responses use semantic operation fields. A legacy
injected adapter may still return resource hints, but those hints are discarded
before runtime compilation. Newly appended or recovered tasks require a
registered `task_kind`; their resources are derived through the same catalog and
Recovery cannot exceed the superseded task resource budget.
For plans with assignments, Freya creates one validated agent per logical
Worker. The `agent_id` remains stable across its assigned tasks; each dispatch
replaces the effective policy, tools, write targets, and criteria with those of
the active compiled task. Plans without `worker_assignments` retain the legacy
per-task agent behavior. Each task's complete policy allows only compiled requirements (`ask` for dangerous
capabilities) and denies the rest. The factory assigns `freya-core` and cannot
add capabilities or tools. The effective Tool list remains the policy
projection; the Skill never adds capability authority. Unknown tool IDs and
incompatible tool/capability pairs remain errors.

After planning, `freya.agent_factory.started` precedes construction.
`freya.agent_created` identifies the generated agent, role, Planner preferred
Skills, assigned Skill IDs,
attempt and factory version; `freya.agent_policy.validated` records required
capabilities and effective Tools. `freya.agent_factory.failed` records a bounded
construction error. The compiler's completion event can include an ignored
Planner Skill preference warning before the graph is
created. A reused Worker emits `worker.reused` and, when it advances to a
different task, `worker.task_switched`; `worker.task_started` and
`worker.task_completed` record each Runtime task with the same `worker_id` and
`agent_id`. `worker.completed` records final cleanup. `worker.recreated` is
emitted only when explicit Recovery creates a replacement generation. Worker
events carry `orchestration_id`, `execution_strategy`, active tools, and active
capabilities; reuse events also carry `previous_task_id` and `current_task_id`.
The generated candidate then passes through
`freya.agent_selection.started`; `freya.agent_selected` records the planned task
ID, generated agent ID, score, classification and selector version, while
`freya.agent_selection.failed` records validation failure. The
orchestration response includes immutable `selections` entries. Each entry has
`planned_task_id`, nullable `selected_agent_id`, `status` (`selected`,
`approval_required`, or `no_eligible_agent`), nullable `score`,
`selector_version`, `created_at`, and a `snapshot` with the complete candidate
ranking.

Within a snapshot, candidates have `classification`/`eligibility` of
`eligible`, `conditional`, or `ineligible`, an explainable score, reasons,
warnings, workload, preferred-Skill matches, and capability buckets:

```json
{
  "allowed": ["filesystem.read"],
  "approval_required": ["filesystem.modify"],
  "denied": [],
  "runtime_unavailable": []
}
```

`allow` satisfies a requirement, `ask` makes the candidate conditional, and
`deny` makes it ineligible. Eligibility is evaluated before score, so Skill,
role, or workload relevance cannot override a denied capability. If no eligible
candidate exists, a conditional candidate may be selected and will still enter
the existing durable approval flow when it requests the protected action.

The Freya view renders the plan summary, complexity, tasks, dependencies,
required capabilities and current orchestration state with escaped text. It
keeps recent terminal runs visible alongside active runs. Failed run rows show
the persisted cause and a **Diagnosis** link to the run's generated logs.

The execution graph runs the complete validated DAG. Dependency-ready tasks use stable
plan-order fairness, selection is persisted once per task, independent branches
may run in parallel within `max_parallel_tasks`, joins wait for all parents, and
failure blocks descendants without stopping independent work. Node states are
`pending`, `ready`, `running`, `waiting_for_approval`, legacy `evaluating`,
`runtime_success`, `recovery_pending`, `blocked`, `success`, `failed`,
`cancelled`, `skipped`, and `superseded`. Runtime `Success` enters
`runtime_success` and releases dependencies. After every active Task assigned
to a Worker reaches that state, one `accepted` Worker evaluation is shared by
its Task nodes; non-accepted criteria route recovery to their origin Task.
Evaluator resolves objective criteria first and sends only unresolved criteria
to the tool-free semantic model. The model returns only per-criterion
`criterion`, `status`, `reason`, `evidence`, and `confidence`; Python computes the
public status, action, issues, and missing evidence. The persisted public
criterion shape remains `criterion`, `status`, `reason`, and `evidence`.
One response repair and one bounded semantic retry (with its own repair) use at
most four model calls on the same immutable Final State Snapshot. Metrics include
`criteria_total`, `criteria_deterministic`, `criteria_semantic`, `model_calls`,
`repairs`, `evaluator_retries`, and `final_status`.
Evaluator infrastructure failures persist as `status=error` with
`evaluation_status=error`, `failure_class=evaluator_infrastructure`, and
`recommended_runtime_action=retry_evaluation`; they have no semantic
`recommended_action` and do not rerun the Worker. The existing recovery path
closes the affected attempt without an automatic evaluator retry.
Retry decisions create a new selection, Runtime task, delegation, evaluation
and attempt record. Same-agent retry revalidates and reuses the exact generated
agent ID. Different-agent retry creates a new generated identity/Skill variant,
hard-excludes prior IDs and preserves the same task-derived policy ceiling. Recovery Advisor `affected_task_ids` are only
a proposal: deterministic DAG traversal permits the `recovery_pending` source
and never-started `pending`/`ready` descendants. Independent, active, accepted
and historically attempted tasks remain structurally immutable. The Replanner
validates this allowed/protected split and Storage recomputes it transactionally
before updating the effective plan. The original plan and all history remain
unchanged. Recovery creates an agent only for a bounded different-agent retry; it cannot
auto-approve capabilities, expand the task policy, bypass policy, or provide
direct agent-to-agent messaging.
A graph with every active effective task in accepted `success` enters
`Integrating`; node success alone never produces orchestration `Success`.
`GlobalVerifier` evaluates the Analyst operational prompt, immutable operational
goal/global criteria, current effective plan, accepted results/evaluations and bounded
verification evidence. Its strict status is `accepted`, `needs_work`, `blocked`,
or `error`, and each original global criterion appears exactly once.
Their canonical actions are respectively `accept`, `add_work`, `add_evidence`
and `fail`. Model actions are normalized to this matrix before strict validation;
one repair receives the original invalid output, exact error and schema. An
exact Planner-scoped local check with satisfied evaluation and its own direct
permitted proof can make global verification deterministic. A model validation
problem emits `freya.global_verifier.validation` with proposed status/action,
error and repair/normalization flags. `criteria_diagnostics.status=unknown`
after a technical verifier failure means unverified, even if proof candidates
were found. A successful `freya.dynamic_agent.archived` event has
`status=Success` and records the final run state separately as
`orchestration_status`.

Version-3 satisfied criteria must cite nonempty criterion-specific permitted
proof refs. Context refs (`task:*`, `evaluation:*`) cannot authorize acceptance.
Missing proof is blocked before inference. Explicit local criterion IDs and
`supports_global_criteria` links permit differently worded criteria to share
proof without fuzzy matching. Legacy plans get only exact-text links. The private snapshot includes
`evidence_catalog` and `proof_refs_by_criterion`; the history API still omits
the snapshot. Historical version-1 records remain readable. Final-response
event payloads include composition metrics. See
[Integration proof contract](INTEGRATION_PROOF.md) for proof and legacy rules.

Only global `accepted` permits final response creation and the conditional
`Integrating → Success` commit. `needs_work`, and resolvable `blocked`, may ask
`IntegrationReplanner` for new tasks only. Existing tasks cannot change or
disappear, accepted tasks cannot rerun, historical IDs cannot be reused, and
new dependencies may reference only accepted existing work or new tasks in the
same acyclic revision. New work follows the normal selector, policy, approval,
Runtime, Evaluator and Recovery pipeline. `error` does not rerun workers.
Repeated global-problem fingerprints and exhausted round/revision/task/model
budgets fail the run with a global explanation.

The server exposes the bounds through `--max-parallel-tasks` (default 4) and
`--max-delegated-tasks` (default 20); both accept 1–20.
Recovery uses `--max-semantic-attempts` (default 3), `--max-plan-revisions`
(default 2), `--max-recovery-actions` (default 8), and
`--max-recovery-model-calls` (default 16) across the initial advice call,
its optional repair, and replanning. Exhausting the action budget fails the
pending node and emits `freya.recovery.exhausted` without persisting an extra
recovery row. Offline mode performs deterministic retries without a model call.
Model selection uses the separate `--recovery-model`, `--recovery-endpoint`,
`--recovery-timeout`, and `--recovery-offline` options.
Global verification, append-only integration replanning and optional final
composition share the separately audited `--max-integration-model-calls`
budget (default 12). `--max-integration-rounds` defaults to 2; integration
revisions also consume `--max-plan-revisions` and `--max-delegated-tasks`.
Model configuration uses `--integration-model`, `--integration-endpoint`,
`--integration-timeout`, and conservative `--integration-offline`. All three
integration adapters are loopback-only and tool-free.

## Agents and catalogue

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/agents` | list or create agents |
| GET / PATCH / DELETE | `/api/agents/{id}` | read, edit or soft-delete an idle agent |
| POST | `/api/agents/{id}/duplicate` | copy definition without history |
| POST | `/api/agents/{id}/pause` | pause between actions |
| POST | `/api/agents/{id}/resume` | resume task dispatch/actions |
| POST | `/api/agents/{id}/restart` | cancel its tasks and reset runtime state |
| POST | `/api/agents/{id}/tasks` | assign a task with optional workspace override |
| GET | `/api/agent-presets` | list safe built-in presets |
| POST | `/api/agent-presets/programmer` | create a generic Programmer agent |
| POST | `/api/agent-presets/qa-tester` | create a non-writing QA agent with controlled Python execution |
| POST | `/api/agent-presets/code-auditor` | create a read-only Code Auditor |
| GET | `/api/tools` | actual and explicitly unavailable tools (advanced mapping) |
| GET | `/api/capabilities` | structured Filesystem, Execution, and Git actions |
| GET | `/api/models?endpoint=...` | installed models from local Ollama |
| GET | `/api/config` | defaults and MVP capability flags |
| GET | `/api/workspaces/browse?path=...` | list subdirectories of an absolute local path |

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/skills` | list `freya-core`; creation of other Skills is disabled |
| POST | `/api/skills/import` | rejected during the single-Skill configuration |
| GET / PATCH / DELETE | `/api/skills/{id}` | inspect or edit `freya-core`; deletion of it is rejected |
| POST | `/api/skills/{id}/duplicate` | rejected during the single-Skill configuration |
| GET | `/api/agents/{id}/skills` | resolved Skill compatibility summaries |
| GET | `/api/approvals?status=pending&task_id=` | list durable approval requests |
| GET | `/api/approvals/{id}` | read one sanitized approval request |
| POST | `/api/approvals/{id}/approve-once` | approve the exact action once |
| POST | `/api/approvals/{id}/approve-task` | approve matching actions for this task |
| POST | `/api/approvals/{id}/approve-file-intent` | approve a cross-task change and allow high-confidence same-purpose reuse for this exact file and orchestration scope |
| POST | `/api/approvals/{id}/deny` | deny the pending action |

Cross-task approvals appear in the same pending-approval list and include
`cross_task_modification` details: requester and owner plan-task IDs, exact
target path, requested change, reason, `needed_for`, and `blocking`. For these
requests, `approve-once` authorizes only this handoff; `approve-file-intent`
creates a reusable grant scoped to the orchestration, requester task, owner
task, exact path and requested `create`, `modify` or `overwrite` operation. `approve-task` is not available for a
cross-task request. Grant reuse never grants a capability, and ambiguous intent
matches remain pending for the operator. Owner changes run through an ephemeral
owner-scoped agent and must pass semantic evaluation before the requester
resumes.

Create/patch fields are `name`, `description`, `role`, `enabled`, `tools`, `skills`,
and `config`. Configuration includes `model`, loopback `endpoint`, `temperature`,
`context_window`, step/time/token/model/tool limits, `retries`, `system_prompt`,
`permissions`, relative `allowed_directories`, `forbidden_commands`, and an
optional `secret_env` name. `config.orchestration_role` may be `worker`,
`task_analyst`, `planner`, `qa`, or `auditor`; `task_analyst` remains accepted as a legacy config value but does not select or configure the built-in Task Analyst; that component uses separate `--task-analyst-*` server options and grants no capability. The `capability_policy` belongs inside `config`
and is also accepted as a top-level compatibility alias. Agent `workspace_path` is either empty for a
generated workspace per task or an absolute existing directory used by default.
Freya-generated agents additionally expose validated `config.provenance` with
the orchestration ID, plan-task ID, attempt, factory version and ephemeral flag.
They are soft-archived at terminal cleanup; task snapshots and attempt history
remain readable. Non-empty provenance is reserved to the internal factory and is
rejected on manual create/import/update. Manual agent CRUD and direct task
assignment are otherwise unchanged.
The structured blocks are validated against their supported modes and limits;
`autonomy` never overrides capability policy. Allow/ask rules derive effective tools; legacy permissions and advanced tool selections do not add authority.

Task assignment accepts `{ "prompt": "...", "workspace_path": "..." }`.
When omitted, the agent's configured workspace applies. An absolute existing
directory overrides it for that task alone. An explicit empty string requests a
fresh generated workspace. Task retries reuse the original task's workspace.

The workspace browser defaults to the parent of the configured data directory
when `path` is omitted. It returns the resolved current path, parent, write-access hint, up
to 500 immediate subdirectories, and a `truncated` flag. Saving the agent is the
authoritative validation step.

The `freya-core` definition includes `tools` alongside `id`, `name`,
`description`, `category`, positive `version`, instructions, procedures,
capability metadata, tags, source, metadata and enabled state. Startup validates
its seven tool IDs; other Skill creation and import are disabled. When startup
finds that a stored `freya-core` lacks the required `write_file` guidance, it adds
the instructions as a new version and keeps earlier snapshots unchanged.
IDs are lowercase stable identifiers. Procedures are recommended operating
guidance and are adapted or omitted when a step needs a tool/capability not in
the worker's effective toolbox. The worker sees no `recommended_capabilities`,
`missing_recommended_capabilities` or `missing_recommended_tools` fields. List filtering accepts
`q` (name, ID, description, category, or tags), `category`, `enabled`, and
`source`. Compatibility summaries include operational state, priority, missing required or recommended capability IDs, and missing concrete tools/runtime support.

`run_command` accepts optional `{ "stdin": "..." }` only for restricted Python
commands. Input is capped at 16,000 characters and is redacted to a character
count in logs. Omitting it closes child stdin; a Python `input()` therefore
fails immediately with `interactive_input_required` instead of hanging. `write_file`
creates a file even when `content` is empty, and creates missing parent directories
automatically. If a parent path is already a file, it fails with
`error_class=ParentPathIsFile` and names the blocking path. The worker stops the
current tool-call batch at that error so the model can change strategy; another
write under the same blocker is not executed. Python/tests/Ruff use the runtime
venv with workspace cwd; Git retains Docker inspection. Infrastructure failures
have no program exit code, and produce `execution_environment` facts with
`status=unavailable`, `program_started=false`, `environment_available=false`. A
successful command can add a `command_execution` item to
`verification.evidence` when its bounded output directly supports a quoted
output, exit-code, or JSON completion criterion. The Planner adds
`qa-interactive-test` when the Analyst marks a request interactive, followed by
`code-audit` for complex mutation plans, except for the bounded simple
non-interactive single-task file/program fast path. Policy denials and
deterministic unavailability are not retried. An unknown tool is reported as
`error_class=unknown_tool`; a registered but unassigned tool is
`error_class=tool_unavailable`. Three consecutive blocked model decisions
terminate the worker with `BlockedActionCycle`; an internal retry of one
recoverable read does not count as an additional blocked decision.

## Tasks, observations and metrics

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/tasks?agent_id=&status=&limit=` | task history |
| GET | `/api/tasks/{id}` | task snapshot, events and merged timeline |
| POST | `/api/tasks/{id}/cancel` | terminate a live task |
| POST | `/api/tasks/{id}/retry` | create a new task from a terminal prompt |
| GET | `/api/logs` | filter by agent, task, orchestration, level, tool, error and date |
| GET | `/api/orchestrations/{id}/logs` | export every persisted runtime and orchestration event for one Freya run |
| GET | `/api/metrics?agent_id=` | global or per-agent aggregates |
| GET | `/api/health` | API, runtime and optional host telemetry |
| GET | `/api/events?after=N` | replay/global SSE stream |
| GET | `/api/tasks/{id}/events?after=N` | replay/task SSE stream |

Ollama chat-call metrics in orchestration stage metrics and Worker
`model.finished`/`model.failed`/`model.repair.finished` events include provider, model, component,
connection state/time, first-token state/latency, generation and total time,
token counts/rate, streaming flag, HTTP status, stop reason and timeout kind.
These fields contain no prompt, response body or credential. `model_call_details`
preserves each attempt, including failed calls. Provider errors use stable
`OLLAMA_UNREACHABLE`, `OLLAMA_REQUEST_TIMEOUT`,
`OLLAMA_GENERATION_TIMEOUT`, `OLLAMA_HTTP_ERROR`, and
`OLLAMA_INVALID_RESPONSE` categories; the Analyst fallback remains indicated
by `analysis_mode=deterministic_fallback`.

`llm.call` events appear in the same orchestration and delegated-task log streams.
By default they contain component, model, stage, prompt name/version, `llm_call_id`,
hash, size, duration, status and available token counts. With
`FREYA_DEBUG_LLM_PROMPTS=true`, they additionally contain a redacted structured
`request_body`, component `structured_context`, `raw_response`, best-effort
`parsed_response` and truncation metadata. `FREYA_DEBUG_PROMPT_MAX_CHARS`
limits retained value characters (1000-4000000; default 4000000). It is loaded
with the flag once at startup using environment over ignored `.freya-local.json`
over defaults, then explicitly propagated to workers. `.env` files are not loaded.
The stdout startup event `freya.runtime.configuration` reports the effective boolean and source; it is independent
of orchestration/task log streams.
`llm.validation` events use the same call ID for Task Analyst and Planner
contract decisions and other model-backed validators. With debug enabled,
they include the redacted normalized result or validation error; rejected events
include `error_type`, `validation_message`, `raw_response` when available,
`contract_name`, `contract_version`, `validation_stage` and `call_stage`. The
`raw_response` is preparse model message content, while `parsed_response` is a
best-effort JSON decode and `normalized_response` is the validator's result.
Otherwise
they keep status and error type only. Repair calls carry
`repair_of_llm_call_id` to link them to the rejected call. Debug content is
persisted and returned by log APIs, so
enable it only for a local diagnostic session.

Task states are Queued, Running, WaitingForApproval, Paused, Success, Failed and Cancelled. Approval statuses are pending, approved_once, approved_task and denied. Agent states are `Idle`, `Running`, `Waiting`, `Paused`, `Error`
and `Offline`. Step states use the corresponding running/terminal values.

Logs read both persisted event tables as one normalized stream, regardless of filters. Each row includes `source` (`runtime` or `orchestration`), a collision-safe `log_id` (`runtime:<id>` or `orchestration:<id>`), a source-local `source_id`, and the compatible `id` field. `GET /api/logs?orchestration_id=...` filters the unified stream to that run; task-specific `/api/logs?task_id=...` remains runtime-only unless `include_orchestration=true` is supplied. Logs group delegated runtime events by `orchestration_id` when present, so one Freya request is shown in one expandable group while each row keeps its responsible `agent_name`. Orchestration statuses `Failed` and `Denied` normalize to level `ERROR`, while the original `status` remains available. Every log row includes a normalized trace: `who`, `actor_name`, `actor_role`, `actor_type`, `where`, `workspace`, `when`, `phase`, `action`, `what`, `how`, `trace_id`, plus the relevant status/tool/input/output/error/duration fields. This makes Task Analyst, Planner, Programmer, Code Auditor and any other participating actor visible in the same audit trail. `how` is bounded to operational metadata (tool, capability, policy, attempt and related IDs), never private model reasoning. Global `/api/events` SSE IDs are composite source cursors (`runtime=<id>;orchestration=<id>`); task event streams keep their numeric runtime IDs. Both accept `Last-Event-ID` or `after` when reconnecting and replay later events. A legacy numeric global cursor resumes runtime rows and starts the previously invisible orchestration stream from the beginning. SSE is a change signal; clients should refresh their current resource from the JSON route. Approval events include a sanitized action summary, capability, tool, resource and approval ID. Successful `write_file` and `edit_file` actions also emit a `workspace.diff` event with a bounded unified diff preview in `output`; Logs render it as Code diff. An identical `write_file` produces no `workspace.diff`, no workspace progress and an action result with `already_satisfied=true`, `changed=false`. A no-progress stop emits `task.no_progress` and the terminal task event includes `failure_class`, `stop_reason`, `no_progress_detected`, `no_progress_actions` and `workspace_changes`. Completed task JSON includes verification with requested, attempted, passed, failed, unavailable and skipped reason evidence.

No-progress detection also emits `worker.no_progress_detected` and
`worker.forced_finalization.started` before the one tool-free terminal decision.
`worker.forced_finalization.completed` contains `decision`, `summary`, `reason`,
`evidence_refs`, `missing_capability`, `trigger`, `steps`, `workspace_changes`
and `llm_call_id`. `COMPLETED` is technical Runtime `Success` and releases
dependents through `runtime_success`; semantic acceptance stays pending until
assignment-wide evaluation. `BLOCKED` is Runtime `Failed` with the operational
reason and optional missing capability, without a permission grant. Invalid
output fails explicitly as `ForcedFinalizationInvalidOutput`; failures emit
`worker.forced_finalization.failed` with `error_type` and
`repair_attempted=false`. Structured results retain the validated decision in
`forced_finalization` alongside the actual action ledger and existing
verification evidence. See [Worker forced finalization](WORKER_FINALIZATION.md).

`GET /api/orchestrations/{id}/logs` returns the complete persisted event stream
for the specified Freya run, combining all delegated runtime logs and
orchestration events in chronological order. Unlike the filtered Logs view, this
export does not apply the 10,000-event display limit. Standard event
sanitization still applies, so this export cannot restore content redacted or
bounded when logged.

Planning audit events separate semantic proposals from compiler decisions:
`freya.planner.semantic_plan_proposed` records operations and ownership,
`freya.plan.resources_resolved` records derived capability/tool pairs,
`freya.plan.ownership_resolved` records normalized file owners, and
`freya.plan.compiled` records the validated runtime plan. Recovery and
Integration replan events include resource resolutions for newly added tasks.
`worker.execution_completed` and `worker.execution_failed` report technical
execution outcomes; `worker.evaluation.*` records the assignment-wide semantic
decision with criterion origin Task IDs.
The activity endpoint includes Worker lifecycle identity and task-scoped tools
and capabilities in event details.
Related events include `artifact.read_observed`,
`artifact.read_before_write_required`, `artifact.stale_read_detected`,
`task.already_satisfied_candidate`, `worker.plan_context_prepared`,
`worker.created`, `worker.reused`, `worker.task_switched`,
`worker.task_started`, `worker.task_completed`, `worker.completed`,
`worker.recreated`, `worker.write_already_satisfied`,
`worker.evaluation_started`, `worker.evaluation.criterion_completed`,
`worker.evaluation.insufficient_evidence`, `worker.evaluation.completed`,
`worker.recovery_started`, `worker.recovery_completed`,
`evaluation.infrastructure_failed`,
`evaluation.criterion.deterministic`, `evaluation.criterion.semantic_started`,
`evaluation.criterion.semantic_completed`, `evaluation.aggregate.completed`,
`evaluation.semantic_contract_repaired`, `evaluation.semantic_retry_started`,
`evaluation.semantic_retry_completed`,
`evaluator.evidence_prepared` and `evaluation.insufficient_evidence`,
`project_context.symbol_reported` and `project_context.symbol_verified`.

For structured worker output, `task.result_contract` records the required
fields, validation error, repair attempt/result, whether fallback normalization
was used, and a sanitized preview (up to 4,000 characters) of malformed final
text. The evaluator completion event exposes its validated field-by-field
decision and metrics; when it does not accept the task, it also exposes a
bounded summary of the exact planned criteria and verification evidence it saw.
It does not expose evaluator prompts or private reasoning.

Structured task results preserve runtime truth even when the model's final
structured response is invalid: `actions` contains bounded tool outcomes,
including `changed` and `already_satisfied` for no-op writes; `artifacts` contains
successful file changes, and `verification.evidence` may contain
`command_execution` records with `command`, `exit_code`, `output` and
`supports_acceptance_criteria`. Evaluator version 11 uses a parent-prepared `final_state` snapshot (version 1).
The durable input includes current files, authoritative verification facts,
criterion associations and explicit content limits. Current existence/readability
for every required target are deterministic success checks. Explicit mechanical
command/suite/lint/compiler success uses exactly bound or unambiguous final
facts. Content, structure, modification claims and behavior go to the semantic
model. `exit_code=0`, typed content checks and `symbol_presence` cannot
accept a semantic criterion automatically. Historical actions, diffs,
`already_satisfied`, old readbacks and superseded checks remain audit data.

Before deterministic evaluation or an LLM call, `evaluator.evidence_prepared`
records the stable criterion ID and its associated evidence IDs in
`criterion_evidence`. The semantic request contains only unresolved criteria;
each entry includes its ID/text and full associated facts, while the
`criterion_evidence` map provides the explicit ID binding. It does not include a
global `final_state` evidence bucket. A fact carries `evidence_id`,
`criterion_id`, `type`, `source`, optional `verification_case_id`, `status`,
input, stdout/stderr, exit code, and current file content/path when applicable.
`evaluator.criterion_evidence_decided` remains the post-decision audit event.

Independent verification tasks carry `verification_mode=independent_cases` and
`verification_cases=[{"id":"case-id","input":"0\n","supports_criteria":["The script executes successfully with temperature 0."]}]` through semantic and
compiled plans into task config. IDs must be unique and bounded; at most 20
cases and 16000 characters per input are allowed. Each case runs the same
model-selected argv in a fresh venv Python process through normal capability policy.
`interactive_session` keeps a single multiline stdin invocation.
`supports_criteria` is optional semantic criterion text. Compiler resolves it
only after Task normalization, resource compilation and evidence reconciliation,
against the final local criteria. Safe surface equality normalizes whitespace
and paired inline-code paths; unknown and ambiguous references fail explicitly.
Resolved `supports_acceptance_criterion_ids` are Compiler-owned. Each case may
support several criteria and each criterion may receive several cases, including
one aggregate execution criterion for all inputs. Worker actions, final facts
and normalized evidence preserve the
resolved links plus `case_id`, `input`, `stdin_sha256`, `verification_id` and
`check_id`. No input-position or stdout association is inferred. Unlinked legacy
commands remain evidence for semantic review. `evaluator.verification_case_bound`
records criterion/case/evidence IDs and the declared association method.
Resolved criterion IDs in case facts override text/context associations. Compiler events
`verification_case_binding_started`, `verification_case_bound`,
`verification_case_unbound` and `verification_case_binding_failed` use the
`plan_compiler.` prefix and contain Task/case/reference identities without stdin.
Reference errors include bounded available final criteria and original semantic
Task keys for targeted repair; internal binding invariants fail without Planner repair.
Runtime `verification.case_completed` events and structured actions expose
`case_id`, exact sanitized `input`, technical status, `exit_code`, `stdout` and
`stderr`. Evaluator receives these fields in final verification facts; a failed
case does not erase other results or automatically decide semantic correctness.

Final facts supersede by explicit case/verification/test/check ID or command signature
(argv plus stdin digest), then check label. Only the latest observation enters
evaluation; distinct cases remain distinct. Facts retain source Task/Runtime/
Worker, exit code, stdout/stderr and runner-reported counts/failed cases. Older
records without separate streams retain merged output with `streams_unavailable`.

A missing explicit verification produces `blocked` and `gather_evidence`.
Evaluation APIs additionally expose system-owned `reason` and `routing_target`:
`missing_required_evidence` routes to `worker` when current resources can gather
it; `required_capability_unavailable` routes to `orchestrator`. Freya records this
block and fails conservatively for resource review without granting permissions
or automatically retrying the incapable Worker. Failed criterion rows retain
`origin_task_id`, cited `failed_facts` and `affected_artifacts` for granular Recovery.
Existing JSON evaluation rows remain readable; no SQL migration is needed.

Evaluation rows additionally expose `missing_verification_cases` (`task_id`,
`case_id`), `evidence_states` (`criterion_id`, state: `missing`, `unbound`,
`insufficient`, `contradictory`, `associated`) and `evidence_fingerprint`.
Absent declared cases are coverage gates even for semantic criteria. Complete
bound case sets prove explicit aggregate execution success deterministically;
semantic output assertions still require review. Parent-owned case/step events
can supply facts independently of serialized final actions. Global links reuse
the same IDs with association method `global_local_link`.
`evidence_binding_error` produces evaluation `error`, `reject` and Orchestrator
routing, with no `gather_evidence`. `evaluator.criterion_evidence_decided` exposes
criterion/evidence IDs, evidence type, case ID, association method, decision
authority and evaluation result.
Recovery v5 snapshots include `worker_id`, `evidence_before` and the missing
case IDs; subsequent evaluations carry the material after fingerprint.
`recovery.evidence_no_progress` and `recovery.source_conflict` stop repeated
collection. Equivalent source-key decisions reuse the existing action; conflicting
ones fail explicitly as `recovery_source_conflict`. No UNIQUE constraint is removed.

Events distinguish `evaluation.final_state_snapshot_created`,
`evaluation.deterministic_fact`, `evaluation.criterion.deterministic`,
`evaluation.criterion.semantic_started/completed`,
`evaluation.missing_required_evidence`, `evaluation.capability_unavailable`,
`evaluation.routed_to_orchestrator` and `evaluation.insufficient_evidence`.
Snapshot events expose paths, existence/readability, hashes and truncation;
content and output in stored snapshots/logs are bounded and sanitized.
Runtime exceptions build
this contract from the action ledger and skip model repair. An identical denied
action is fingerprinted and answered locally with
`ACTION_BLOCKED_PERMANENTLY_FOR_CURRENT_STATE`; the response does not execute
the tool or increment `tool_calls`. Recovery snapshots and retry prompts carry
a bounded `workspace_state` with prior verification, actions, artifacts, diffs
and error; a newly generated recovery agent may gain only `filesystem.read`
for that inspection, never `filesystem.overwrite`.

Skills support `GET /api/skills/{id}/versions` and `GET /api/skills/{id}/versions/{version}` for immutable history. `DELETE /api/skills/{id}` archives a skill and records an audit event; archived skills are excluded from `/api/skills` unless `include_deleted=true` is requested.

`POST /api/skills/import` currently rejects imports. The frontend can export the visible `freya-core` definition; older archived definitions remain available only through historical queries.

`PATCH /api/skills/{id}` ignores a client-supplied version and assigns the next version when versioned definition fields change. A no-op PATCH preserves the current version and emits no `skill.updated` event. Historical snapshots cannot be overwritten.
