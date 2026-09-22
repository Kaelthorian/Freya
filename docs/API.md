# Control Center API

All routes are same-origin under `/api` and return JSON errors as
`{"error":"message"}`. Mutations accept `application/json`. SSE is
`text/event-stream`.

`GET /api/capabilities` returns the structured capability registry used by the
editor. `GET /api/config` includes it as `capability_catalog`. Agent JSON
includes `capability_policy`; action rules use `mode` (`allow`, `deny`, or
`ask`) and may include workspace-relative `paths`, case-insensitive
`extensions`, and `max_bytes`.

Agent configuration also accepts JSON blocks `identity`, `behavior`,
`autonomy`, `verification`, and `output`, or their top-level editor aliases.
Agent responses include `context_preview`, a read-only context rendering with
no secret values.

Skills are reusable declarative knowledge. A Skill is not a Tool, Capability, or
Permission: its instructions and procedures guide the model, while
`required_capabilities` and `recommended_capabilities` are diagnostics only.
Required capabilities must be allowed by the agent policy for a Skill to be
operational. Assigned Skills use `{ "id": "python-development", "priority":
100 }`; priority orders context and never overrides policy. Tasks store
immutable Skill snapshots, including version and procedures.

## Freya orchestration

`POST /api/orchestrations` with `{ "prompt": "...", "workspace_path": "..." }` queues a bounded run. An existing absolute workspace is used directly; when omitted or empty, Freya creates one isolated workspace for the orchestration and shares it across all planned nodes.
The trimmed prompt must be non-empty. The application does not impose an
artificial character limit before creating a run.
`GET /api/orchestrations` lists runs and `GET /api/orchestrations/{id}` returns
the run, immutable `plan`, `plan_schema_version`, `plan_created_at`,
`planning_metrics`, selection snapshots, delegations, execution attempts,
evaluations, recovery actions, plan revisions, and events needed to reconstruct it.
`GET /api/orchestrations/{id}/plan` returns
the plan and its version metadata directly. Existing agent and task routes
remain compatible.
`GET /api/orchestrations/{id}/graph` returns durable nodes in immutable plan
order plus a summary with per-state counts, active/terminal totals and a
`complete` flag. Historical pre-4.3 runs return an empty node list and null
summary. `GET /api/orchestrations/{id}` includes the same `graph_summary`.
Nodes expose nullable `evaluation_id` and `evaluation_status` references, not
the full evaluation. `GET /api/orchestrations/{id}/evaluations` returns the
immutable evaluation records with status, summary, confidence, per-criterion
decisions, issues, missing evidence, recommended action, version, independent
metrics and truncation/deterministic flags. It never returns evaluator prompts
or the private input snapshot.
`GET /api/orchestrations/{id}/integrations` returns immutable global
verification history ordered by round. Each item exposes `round`, `status`,
`summary`, criterion decisions, cross-task issues, missing evidence,
responsible active task IDs, `plan_revision`, integration version, metrics and
truncation/deterministic flags. The private bounded snapshot is not returned by
the API. Historical pre-4.6 runs return `[]`.

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

The orchestration moves through `Queued`, `Planning`, `Planned`, `Running`, and
`Integrating` before success. A globally requested append-only revision returns
it to `Running`; `Integrating` may also end in `Failed` or `Cancelled`. Planning emits
`freya.planning.started`. Before the planner, configured Task Analyst runs emit
`freya.task_analysis.started` and `freya.task_analysis.completed`; without a
configured agent the completed event contains a deterministic operational brief.
The version-2 result includes `operational_prompt`, and `corrected_fields` lists
deterministic semantic corrections. Planning then emits either
`freya.plan.created` with a safe goal/complexity/task summary or
`freya.planning.failed`. A plan uses schema version 1:

```json
{
  "goal": "Repair authentication and verify the fix",
  "summary": "Inspect, diagnose, fix and verify authentication.",
  "complexity": "multi_step",
  "tasks": [{
    "id": "inspect-auth",
    "objective": "Inspect authentication",
    "description": "Identify the relevant files and login flow.",
    "depends_on": [],
    "required_capabilities": ["filesystem.read", "filesystem.search"],
    "preferred_skills": ["python-development"],
    "success_criteria": ["The current login flow is understood."]
  }],
  "success_criteria": ["The root cause and verification result are recorded."]
}
```

`required_capabilities` are validated registry IDs that describe likely task
needs; they do not grant permission. `preferred_skills` are non-binding semantic
hints and may name a Skill that is not currently installed. For plans that mutate file/code artifacts, Freya appends one read-only `code-audit` task with `preferred_skills: ["code-review"]` after the implementation tasks.
For every ready plan task, Freya creates a validated ephemeral agent. Its complete
policy allows only the declared requirements (`ask` for dangerous capabilities)
and denies the rest; its Tool list is the deduplicated projection of that policy.
The factory selects at most eight enabled Skills. Unknown or incompatible
preferred Skills produce diagnostics and never add capability authority.

After planning, `freya.agent_factory.started` precedes construction.
`freya.agent_created` identifies the generated agent, role, assigned Skill IDs,
attempt and factory version; `freya.agent_policy.validated` records required
capabilities and effective Tools. `freya.agent_factory.failed` records a bounded
construction error. The generated candidate then passes through
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
`pending`, `ready`, `running`, `waiting_for_approval`, `evaluating`,
`recovery_pending`, `blocked`, `success`, `failed`, `cancelled`, `skipped`, and
`superseded`. Runtime `Success` enters `evaluating`; only an `accepted`
evaluation becomes node `success`. Other semantic outcomes enter
`recovery_pending` and receive exactly one bounded decision for that attempt.
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

Version-2 satisfied criteria must cite nonempty criterion-specific permitted
proof refs. Context refs (`task:*`, `evaluation:*`) cannot authorize acceptance.
Missing proof is blocked before inference. The private snapshot now includes
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
| POST | `/api/agent-presets/task-analyst` | create a tool-free prompt-rewrite agent |
| POST | `/api/agent-presets/qa-tester` | create a non-writing QA agent with controlled Python execution |
| POST | `/api/agent-presets/code-auditor` | create a read-only Code Auditor |
| GET | `/api/tools` | actual and explicitly unavailable tools (advanced mapping) |
| GET | `/api/capabilities` | structured Filesystem, Execution, and Git actions |
| GET | `/api/models?endpoint=...` | installed models from local Ollama |
| GET | `/api/config` | defaults and MVP capability flags |
| GET | `/api/workspaces/browse?path=...` | list subdirectories of an absolute local path |

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/skills` | list/filter or create reusable Skills |
| POST | `/api/skills/import` | atomically import one or many Skills from a JSON array |
| GET / PATCH / DELETE | `/api/skills/{id}` | inspect, edit, or delete/disable a Skill |
| POST | `/api/skills/{id}/duplicate` | create a user copy with a new stable ID |
| GET | `/api/agents/{id}/skills` | resolved Skill compatibility summaries |
| GET | `/api/approvals?status=pending&task_id=` | list durable approval requests |
| GET | `/api/approvals/{id}` | read one sanitized approval request |
| POST | `/api/approvals/{id}/approve-once` | approve the exact action once |
| POST | `/api/approvals/{id}/approve-task` | approve matching actions for this task |
| POST | `/api/approvals/{id}/deny` | deny the pending action |

Create/patch fields are `name`, `description`, `role`, `enabled`, `tools`, `skills`,
and `config`. Configuration includes `model`, loopback `endpoint`, `temperature`,
`context_window`, step/time/token/model/tool limits, `retries`, `system_prompt`,
`permissions`, relative `allowed_directories`, `forbidden_commands`, and an
optional `secret_env` name. `config.orchestration_role` may be `worker`,
`task_analyst`, `planner`, `qa`, or `auditor`; `task_analyst` runs before planning and
does not grant any capability. The `capability_policy` belongs inside `config`
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

Skill creation accepts `id`, `name`, `description`, `category`, positive
`version`, list-valued `instructions`, structured `procedures`, capability
metadata, `tags`, `source` (`builtin` or `user`), `metadata`, and `enabled`.
IDs are lowercase stable identifiers. Procedures are recommended operating
guidance and are adapted when a step is unavailable. List filtering accepts
`q` (name, ID, description, category, or tags), `category`, `enabled`, and
`source`. Compatibility summaries include operational state, priority, missing required or recommended capability IDs, and missing concrete tools/runtime support.

`run_command` accepts optional `{ "stdin": "..." }` only for restricted Python
commands. Input is capped at 16,000 characters and is redacted to a character
count in logs. Omitting it closes child stdin; a Python `input()` therefore
fails immediately with `interactive_input_required` instead of hanging. The
Planner adds `qa-interactive-test` when the Analyst marks a request interactive,
followed by `code-audit` for mutation plans.

## Tasks, observations and metrics

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/tasks?agent_id=&status=&limit=` | task history |
| GET | `/api/tasks/{id}` | task snapshot, events and merged timeline |
| POST | `/api/tasks/{id}/cancel` | terminate a live task |
| POST | `/api/tasks/{id}/retry` | create a new task from a terminal prompt |
| GET | `/api/logs` | filter by agent, task, orchestration, level, tool, error and date |
| GET | `/api/metrics?agent_id=` | global or per-agent aggregates |
| GET | `/api/health` | API, runtime and optional host telemetry |
| GET | `/api/events?after=N` | replay/global SSE stream |
| GET | `/api/tasks/{id}/events?after=N` | replay/task SSE stream |

Task states are Queued, Running, WaitingForApproval, Paused, Success, Failed and Cancelled. Approval statuses are pending, approved_once, approved_task and denied. Agent states are `Idle`, `Running`, `Waiting`, `Paused`, `Error`
and `Offline`. Step states use the corresponding running/terminal values.

Logs group delegated runtime events by `orchestration_id` when present, so one Freya request is shown in one expandable group while each row keeps its responsible `agent_name`. `GET /api/logs?orchestration_id=...` also merges the persisted orchestration timeline (for example `freya.task_analysis.completed` and failure-analysis events); those rows use a string `id` such as `orchestration:12` and `source=orchestration`. Every log row includes a normalized trace: `who`, `actor_name`, `actor_role`, `actor_type`, `where`, `workspace`, `when`, `phase`, `action`, `what`, `how`, `trace_id`, plus the relevant status/tool/input/output/error/duration fields. This makes Task Analyst, Planner, Programmer, Code Auditor and any other participating actor visible in the same audit trail. `how` is bounded to operational metadata (tool, capability, policy, attempt and related IDs), never private model reasoning. Every SSE update has an integer `id`, `event_type`, timestamp, agent/task IDs
and relevant status/tool/input/output/error/duration fields. Approval events include a sanitized action summary, capability, tool, resource and approval ID. Successful `write_file` and `edit_file` actions also emit a `workspace.diff` event with a bounded unified diff preview in `output`; Logs render it as Code diff. A no-progress stop emits `task.no_progress` and the terminal task event includes `failure_class`, `stop_reason`, `no_progress_detected`, `no_progress_actions` and `workspace_changes`. Completed task JSON includes verification with requested, attempted, passed, failed, unavailable and skipped reason evidence. Clients should send
`Last-Event-ID` or `after` when reconnecting and refresh their current resource
from the JSON route; SSE is a change signal and durable event replay.

Skills support `GET /api/skills/{id}/versions` and `GET /api/skills/{id}/versions/{version}` for immutable history. `DELETE /api/skills/{id}` archives a skill and records an audit event; archived skills are excluded from `/api/skills` unless `include_deleted=true` is requested.

`POST /api/skills/import` accepts { "skills": [ ... ] } (optionally with a version field), validates every definition with the same registry schema, rejects conflicting duplicate IDs or case-insensitive names, skips identical existing definitions, and commits the full batch atomically. The frontend exports a single definition as a JSON object and bulk exports as { "version": 1, "skills": [ ... ] }.

`PATCH /api/skills/{id}` ignores a client-supplied version and assigns the next version when versioned definition fields change. A no-op PATCH preserves the current version and emits no `skill.updated` event. Historical snapshots cannot be overwritten.
