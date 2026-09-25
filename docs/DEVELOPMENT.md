# Development and operation

## Prerequisites

- Python 3.10 or later.
- Ollama running locally with at least one installed model.
- A modern browser. Node is needed only for JavaScript syntax validation.
- Docker Desktop with a running Linux engine for agent Python, test, Ruff and Git commands.

Build the local sandbox image before allowing agent command execution:

```powershell
docker build -t freya-sandbox:py311 .\sandbox
docker image inspect freya-sandbox:py311
```

If Docker or the image is unavailable, `run_command` and Git inspection fail
with `SandboxUnavailable`; Freya never executes agent code on the host.

The runtime uses the Python standard library. If `psutil` is installed,
`/api/health` reports CPU and RAM; otherwise telemetry is unavailable.

## Run the platform

From the repository root:

```powershell
ollama list
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

Graph scheduling defaults to four active nodes and accepts at most the
planner's twenty tasks. Configure both bounds explicitly when needed:

```powershell
python -m control_center --max-parallel-tasks 4 --max-delegated-tasks 20
```

If the total delegated-task limit is below the requested parallel value, the
effective graph parallelism is clamped to that total limit.

Structured planning uses local Ollama by default:

```powershell
python -m control_center --planner-model qwen2.5-coder:7b `
  --planner-endpoint http://127.0.0.1:11434 --planner-timeout 120
```

The planner endpoint must remain loopback-only. Use `--planner-offline` only
when explicitly choosing the deterministic one-task fallback, such as an
offline test environment. Provider errors fail planning and do not trigger the
fallback.

Before planning, production Freya uses `TaskSpecAnalyst` to build
one canonical, versioned Task Spec from the user's request. An enabled agent
with `config.orchestration_role=task_analyst` supplies the loopback model;
without one, conservative deterministic analysis still runs. The Analyst
has no tools and never assigns workers or capabilities. It records explicit,
clarified and assumed requirements, asks only high-impact questions and
leaves an ambiguous run in `NeedsClarification`. The user answers through
`POST /api/orchestrations/{id}/clarifications`; the same run resumes and
stores both question and answer. The runtime keeps question IDs across
versions, assigns a new ID only for a new semantic field, and discards answered
or optional model questions. `user_decisions` is a container, never a question
field. Exact answer submission replays are idempotent. After three answer
rounds, an unresolved material question fails with a cycle diagnostic rather
than opening another clarification. A ready spec can be revised through
`POST /api/orchestrations/{id}/revise-spec` only before a plan is persisted;
the store fences late output from a planner using the previous version. `TaskSpecAnalyst` repairs one malformed
model response, then falls back conservatively on technical failure. It sends
Ollama the same closed JSON Schema documented in `task_spec.py` and normalizes
only declared aliases and safe container/default differences before validation.
Inspect `task_analysis.contract_invalid` and the `fallback_*` Task Analyst
metrics to identify the exact field if a repair still fails.

The Planner receives the ready Task Spec and a fresh `RuntimeResourceCatalog`.
The catalog is rebuilt from `capabilities.py`, worker schemas exposed by
`Toolbox`, and the one active Skill, `freya-core`. It sends capability
operation summaries, tool purposes, and Skill descriptions/use cases plus
required/recommended capability metadata, without
Skill instruction bodies. Resource ID enums in the Ollama response schema are
dynamic. The Planner chooses semantic needs, capabilities, tool references,
preferred Skills and QA. `plan_scope.py` first removes optional unrequested
external actions/criteria and rewires dependencies. Mixed external/artifact
tasks or loss of a Task Spec validation check fail closed. Resource references
are then validated;
unknown or ambiguous IDs fail as `UnsupportedResourceRequirement` without a
second LLM repair call. `plan_compiler.py` checks those references again,
generates runtime IDs, links criteria and validates the DAG. Workers
receive a deterministic rendering of the Task Spec, never an independent
`operational_prompt`. The old Analyst v3 path is retained for injected
compatibility adapters and historical tests.

Planner `preferred_skills` values, including obsolete IDs, are normalized to
`freya-core`; the resolver can record one ignored-preference warning. Every
dynamic agent receives that Skill. Its declared tool list is validated against
the global `Toolbox` registry at Store startup. Task capabilities and Policy
still determine the effective tool surface. Unknown tools and incompatible
tool/capability pairs remain errors.

Resource Resolver derives a capability only from a specific task operation
(for example,
`run_command` plus “Execute a Python script” yields
`execution.python_script`; “Run pytest” yields `execution.pytest`). A tool
proposal alone gives no authority. An unneeded `run_command` is removed;
an ambiguous needed command raises `ToolCapabilityMismatch` when its declared
capabilities do not support it. `planning_metrics.planner_semantic_plan` and
`freya.plan_compiler.started.planner_semantic_plan` retain a bounded sanitized
pre-resolution task view. `freya.plan.scope_adjusted` records omitted external
work and compiler details include resource resolutions.

Planner reconciliation is a safety floor over both sources: if the model plan
contains a write or local execution, it derives `filesystem.read` for
read-back; it removes `filesystem.overwrite` unless the user explicitly asked
to replace an existing artifact; and it collapses an accidental audit node from
an otherwise simple file/program task. A language mention in a debugging task
does not turn that task into Python program creation.

After planning, Freya creates one ephemeral least-privilege agent for each ready
plan task. No Programmer, QA Tester or Code Auditor preset needs to exist first.
The factory assigns `freya-core` to every dynamic worker, QA and auditor. Its
seven declared tools come from the Skill, while Policy selects the effective
worker schemas and evaluates every invocation. Planner Skill preferences do
not affect assignment. Provenance is persisted for audit and terminal cleanup.

For a generic file, the Planner creates one dynamic worker with
`filesystem.create` plus read-back `filesystem.read`. A Python Hello World
task additionally needs `execution.python_script` to run the result.
These low-risk flows finish after the requested artifact is written, read back,
and, for Python, executed with exit code 0 and expected output. Their worker
prompt is generated from the exact effective `box.schemas` surface, so an
unassigned operation is neither described nor suggested. They do not need
Git, a test suite, QA, or a Code Auditor. Skill tools never expand task policy.

For a Python console calculator that needs interactive input, the semantic
plan normalizer collapses model-created file/function/test subtasks into one
implementation node, then appends one dependent QA node. QA receives
`filesystem.read` and `execution.python_script`, with `freya-core` assigned.
For this exact one-case calculator request, Freya uses `run_command` once with
bounded stdin lines `3` and `5` to verify output `8` and exit code `0`. The
implementation receives only `filesystem.create` and `filesystem.read`;
execution authority stays with QA. Whole-number results omit a trailing `.0`.
The QA worker stops after one successful command supplies evidence for every
configured acceptance criterion. Neither task creates helper or test files,
and this simple request does not add a code-audit node.
For file-only implementation criteria, the worker also finishes immediately
after a matching read-back of the new artifact.

Semantic evaluation has separate local-model configuration:

```powershell
python -m control_center --evaluator-model qwen2.5-coder:7b `
  --evaluator-endpoint http://127.0.0.1:11434 --evaluator-timeout 120
```

The evaluator endpoint is also loopback-only and the adapter exposes no tools.
Use `--evaluator-offline` explicitly for deterministic evidence-only evaluation.
Offline evaluation is conservative: acceptance requires configured verification
to be requested, attempted and passed. Runtime result prose and agent claims are
not objective evidence, but a successful controlled `run_command` can emit
bounded `command_execution` evidence when its output directly satisfies a
quoted-output, exit-code, or JSON completion criterion. Missing evidence returns
`blocked`, including through the Orchestrator's default compatibility fallback.
For filesystem-only tasks without Git or tests, the Worker may instead verify
each modified file with an allowed `read_file` read-back; missing or mismatched
read-back evidence remains blocked or failed.

Task Analyst language policy applies in model, offline, no-analyst and deterministic
fallback paths: standalone program creation defaults to Python 3.10+ when no
language is named; code changes preserve the detected project stack. A language
assumption clears only a language-only blocker, never unrelated missing inputs.

For each non-accepted evaluation, `freya.evaluation.completed` includes the
validated decision by criterion and a bounded summary of the exact task and
verification evidence shown to the evaluator. `GET /api/logs` exposes the same
event output. Structured final responses receive one repair attempt even when
the original answer is prose; if normalization is needed, `task.result_contract`
stores a sanitized response preview, validation error, repair outcome and
fallback status. A failed format repair is diagnostic and does not itself
invalidate objective evidence. Exact criterion links from successful controlled
commands are accepted deterministically.

Semantic recovery has its own local, tool-free model and hard budgets:

```powershell
python -m control_center --recovery-model qwen2.5-coder:7b `
  --recovery-endpoint http://127.0.0.1:11434 --recovery-timeout 120 `
  --max-semantic-attempts 3 --max-plan-revisions 2 --max-recovery-actions 8 `
  --max-recovery-model-calls 16
```

The recovery endpoint remains loopback-only. Invalid JSON gets at most one repair
without exceeding the total model-call budget. The orchestration wall-clock deadline
is rechecked after each recovery or replanning call before more work starts.
Use `--recovery-offline` for model-free deterministic behavior: same-agent
retry for `needs_revision`/`blocked`, a new dynamic agent variant for `rejected`,
and fail for evaluator errors. Same-agent retry reuses the exact generated ID;
different-agent retry creates a new identity/Skill combination while preserving
the same task-derived capability ceiling. Every retry creates a persisted attempt
and reruns Agent Selector and capability-policy checks. Recovery never grants capabilities or
resolves approvals. Replanning stores an effective-plan revision without
overwriting the original plan or rerunning accepted tasks. Its deterministic
scope contains only the recovery source and never-started descendants; active,
historical and independent work is protected and revalidated again by Storage.

The same recovery model, endpoint, and timeout also power exactly one
post-failure diagnosis after a task graph can no longer progress. That call
receives only bounded, sanitized persisted log entries, has no tools, and must
cite supplied `log_id` values. `--recovery-offline` uses the deterministic
logs-only diagnosis; any provider or validation error falls back to that same
deterministic path. The report is stored in the orchestration response and
`freya.failure_analysis.completed`. It explains the failure but never retries
work or changes policy. Use the failed run's **Diagnosis** link to inspect its
generated logs.

For troubleshooting, use `GET /api/logs?orchestration_id=<id>&limit=10000` (or
the run's Diagnosis link). The response combines worker events with the
orchestration timeline, so Task Analyst interpretation, plan/selection events,
and every selected agent's normalized `who`/`where`/`when`/`what`/`how` trace,
`task.no_progress`, terminal `failure_class` and the final failure diagnosis
are visible together. A `NoProgressDetected` report means the worker repeated
successful read-only actions without changing the workspace; raising
`max_steps` alone is not a corrective action.
`BlockedActionCycle` stops a worker after it repeats a policy-denied action
under unchanged state, or after the existing bounded guard sees three blocked
decisions. The first materially identical request after a denial is intercepted
by a fingerprint of tool, capability, normalized target and relevant arguments;
it receives `ACTION_BLOCKED_PERMANENTLY_FOR_CURRENT_STATE` without another tool
execution or tool-call budget charge. The worker rechecks policy so a changed
permission state, target or strategy can proceed normally. Policy denials,
unavailable or unknown tools, invalid requests and approval denials are not
automatically retried. A recovery retry receives bounded prior workspace state
and may derive read-only inspection, but never overwrite authority. A
deterministic missing-file result is terminal for semantic recovery; selecting
another agent does not make an absent artifact appear.

`write_file` creates files only; empty content creates an empty file, not a
directory. It creates missing parent directories when given a nested path. If
an existing parent is a file, the result is `ParentPathIsFile` and identifies
that path. This error is not retried: the worker stops sibling calls in the
current batch so the model can change strategy, and it rejects a later child
write under the same blocker without another filesystem attempt. The Skill also
spells out this rule and the complete-path pattern.

`write_file` compares exact UTF-8 bytes before overwrite policy. Identical
content returns `already_satisfied=true` and `changed=false`, emits no workspace
diff and does not count as workspace progress. The action ledger records this
state for the model. A created artifact plus successful command evidence linked
to every configured completion criterion can satisfy a task and stop further
model actions. A no-op alone does not satisfy unrelated configured criteria.
Orchestration timeouts also emit failure analysis.

Docker receives an ephemeral workspace copy for every Python, pytest, unittest,
py_compile, Ruff or Git command. The container has no network, Docker socket,
host HOME/USERPROFILE, inherited secrets or host project mount. Its root is
read-only, and memory, PID, CPU and time limits apply. Only `HOME=/tmp`,
`TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE`, `PYTHONNOUSERSITE`,
`PYTHONPATH=/workspace` and Git isolation variables are passed. Command writes
are discarded with the copy; use `write_file` or `edit_file` for persistent
changes. The snapshot omits symlinks and large generated dependency folders
and is limited to 10,000 files and 256 MiB. A Git checkout must be rooted in
the assigned workspace; a parent repository is outside the allowed boundary.

Interactive Python QA uses `run_command` with a bounded `stdin` string. Without
stdin the worker closes the child stream, so `input()` fails immediately rather
than waiting for a human terminal. This does not add shell access and QA Tester
cannot modify files.

Git Inspection is only prompt-visible when the task workspace is inside a Git
checkout. Non-Git workspaces omit `git_diff` from the worker schemas and report
direct requests as `not_applicable`, avoiding misleading successful Git checks.

Plan schema version 1 persists global and local criterion IDs plus explicit
local-to-global references. Old plans receive deterministic IDs and only their
previous exact-text associations. Recovery and integration replanning assign
unique new task IDs after normalization and log the proposed and final IDs.

Global integration uses its own tool-free local-model configuration and
independent budget:

```powershell
python -m control_center --integration-model qwen2.5-coder:7b `
  --integration-endpoint http://127.0.0.1:11434 --integration-timeout 120 `
  --max-integration-rounds 2 --max-integration-model-calls 12
```

The integration endpoint is loopback-only. The model-call budget covers global
verification and its repair, append-only replanning and its repair, and final
response composition and its repair. Integration revisions also consume the
shared `--max-plan-revisions` and `--max-delegated-tasks` limits. Use
`--integration-offline` for conservative evidence-only operation: structurally
provable criteria may be accepted, but semantic global criteria without enough
objective evidence remain `blocked`. Offline mode never equates accepted child
tasks with global success.

After every active effective task is accepted, the run enters `Integrating`.
An accepted global result creates a grounded final response and then commits
`Success`. `needs_work`, or safely resolvable `blocked`, appends tasks without
changing accepted work and sends them through normal selection, policy,
approval, Runtime, evaluation and recovery. Repeated gaps or exhausted budgets
fail closed. Cancellation, timeout, restart, revision changes and evaluation
fingerprint changes invalidate late global results.

Operational history is available at `/api/orchestrations/{id}/attempts`,
`/recoveries`, `/plan-revisions`, `/effective-plan`, and `/integrations`.
Recovery or integration is not resumed after a server restart; unfinished state
is closed with the run.

Open `http://127.0.0.1:8765`. Worker counts may be 1–8. A lock in the selected
data directory prevents two schedulers from using one database. Stop with
`Ctrl+C`; active and queued tasks are cancelled and logged.

SQLite uses `data/control_center.sqlite3` by default and may create `-wal` and
`-shm` files. Automatic workspaces use `data/workspaces/<random-id>/`. For a Freya orchestration, that directory is allocated once and shared by all dependent nodes (including any conditional QA or audit task); direct task submissions still get one directory per task. These
generated paths are ignored by Git.

Shareable manual-agent definitions remain available in the versioned data/agents
directory. They are useful for direct task submission and compatibility
workflows, but normal Freya orchestration no longer requires Programmer, QA
Tester or Code Auditor imports. Definitions contain configuration and Skill
assignments only; runtime state, prompts, logs, approvals, metrics and generated
workspaces remain ignored.

In agent settings, **Choose folder** chooses that agent's default folder. In the Freya form, selecting an existing folder uses it directly for the orchestration, so agents can act on files already there; leaving it empty creates an isolated workspace.
The **Assign a task** form also has its own folder selector. It starts with
the agent default, can override it for one execution, and uses a fresh generated
workspace when left empty. Task retries reuse the original folder. Editing an
agent requires it to be idle. If a chosen folder is removed later, submissions
that select it fail. Tasks that resolve to the same folder run serially.

Interactive plans append a dependent QA task. More complex mutation plans may
append a dependent read-only audit task; every dynamic role uses `freya-core`.
The bounded simple file/program path remains one implementation
task. The factory creates any QA or Auditor agent at dispatch time, so no
preconfigured pipeline agents are required.
The agent editor uses progressive disclosure: identity fields stay visible for
quick setup, while Skills, model, workspace, capabilities, tools, behavior,
verification, autonomy, output, and limits are compact expandable sections. The
API presets cover Programmer, Task Analyst, QA Tester and Code Auditor for
manual/direct-task and legacy compatibility workflows. The Agents page keeps the
existing Programmer quick-create action, but those presets are not prerequisites
for modern dynamic orchestration.

The active Skills catalog contains only `freya-core`. The server archives older
Skill rows on startup without deleting historical snapshots. Creation/import
of other Skills is disabled during this configuration. `freya-core.tools`
lists every registered tool; startup rejects unknown entries. Manual agents
still use their own policy. Tasks snapshot the assigned Skill version/content.

The Skills page can export the visible `freya-core` definition. Import requests
are rejected while the single-Skill configuration is active.

### Secrets

Set an environment variable beginning with `ACC_SECRET_`, then enter only its
name in the agent configuration:

```powershell
$env:ACC_SECRET_OLLAMA = "value-not-stored-in-sqlite"
python -m control_center
```

The endpoint validator accepts only loopback Ollama base URLs without embedded
credentials, paths, query strings or fragments.

## Validate changes

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p "test_planner.py" -v
python -m unittest discover -s tests -p "test_execution_graph.py" -v
python -m unittest discover -s tests -p "test_evaluator.py" -v
python -m unittest discover -s tests -p "test_integration.py" -v
python -m unittest tests.test_task_spec tests.test_activity tests.test_control_api -v
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\core.js
node --check frontend\components.js
python -m unittest discover -s tests -p "test_recovery.py" -v
node --check frontend\views.js
node --check frontend\dialogs.js
node --check frontend\icons.js
python -m control_center --help
python -m pytest -q
```

Capability, structured-agent and Skill behavior is covered by
`tests/test_capabilities.py`, `tests/test_agent_context.py`,
`tests/test_skills.py` and `tests/test_agent_factory.py`. The worker
resolves and evaluates a capability before every tool invocation. A policy or autonomy ask creates a durable approval request, moves the task to WaitingForApproval, and the Approvals page resolves it once, for the task, or denies it.

Runtime tests use a local fake Ollama server and spawned worker processes. The
symlink regression skips when the Windows account cannot create symlinks. A
full end-to-end task additionally requires local Ollama and an installed model.
`tests/test_task_spec.py` covers valid one-call output, safe normalization,
source aliases, one targeted repair, exact invalid-field diagnostics and
deterministic fallback. `tests/test_activity.py` covers system actors, timestamp
durations, clarification/approval waits, failed phases, LLM metrics and
non-additive overlapping intervals; `tests/test_control_api.py` checks the
activity endpoint against persisted orchestration data. In Freya Activity, the
top summary shows total elapsed, processing, worker execution, clarification
wait, approval wait and aggregate LLM time; the phase table and chronological
event list expose each component and expandable event details.
Planner tests cover atomic transitions, cancellation races, restart recovery,
approval waiting, child failure propagation, deadlines and simulated Ollama.
They also verify synthesis of omitted global-link rows from success criteria,
deterministic global/local criterion-ID assignment for missing, blank and
duplicate IDs, preservation of valid IDs, cross-namespace collisions, safe
local-criterion reconciliation, safe reference resolution or rejection, and explicit
coverage before execution. A regression reproduces the Ollama output where one
Analyst AC placeholder stands in for two concrete plan criteria; each local
link must resolve by exact text. Another checks `T-N` expansion across task IDs,
dependencies and local links. Task Analyst tests check REQ/AC normalization and
`verifies` references. ID-only normalizations do not call the model repair path. Corrected IDs
are reported as counts in `freya.planner.normalized`; inspect its `stable_ids`
payload alongside `planning_metrics`.
Execution-graph tests cover pure DAG transitions, sequential and parallel
scheduling, joins, branch-local failure propagation, approval waits, paused
agents, per-agent serialization, concurrency limits, cancellation and migration.
Evaluator tests cover hard evidence precedence, prompt injection, strict schema,
one repair, per-criterion coverage, immutable persistence, API exposure, graph
gating, duplicate prevention, technical failure, cancellation, timeout and
restart recovery.
Integration tests cover deterministic preconditions, exact global criteria,
hard evidence precedence, prompt injection, strict schema/one repair,
canonical status/action normalization, exact direct-proof fast path, safe
verifier validation events, separate archival and orchestration statuses,
fingerprints, immutable snapshots, duplicate rounds, stale/cancel/timeout
discard, restart and migration, append-only validation, shared limits, global
loop detection, normal selector/evaluator execution for appended work, grounded
final responses, API history, and the invariant that accepted child tasks do
not imply orchestration success.

Run `python -m unittest tests.test_integration_grounding -v` for adversarial
proof, Storage, race, budget, 4.5 retry and policy/approval coverage. Its approval
test uses a real spawned Runtime and a loopback fake provider with disposable
workspace data. Run `python -m pytest -q` as well when pytest is installed.
Fixture model acceptances must cite permitted proof; do not restore empty
evidence to make a regression pass. See [Integration proof contract](INTEGRATION_PROOF.md).

## Debugging

### Ollama calls

Production chat requests stream through `control_center/transport.py`. Set
`--planner-timeout`, `--evaluator-timeout`, `--recovery-timeout`, or
`--integration-timeout` as inactivity limits; they do not replace the hard
limits in `MODEL_PROFILES`. The Task Analyst reads its bounded agent setting,
and the Worker also obeys its remaining task wall-clock budget. Adjust output
and repair token caps together in `MODEL_PROFILES` when a validated response
needs more room. The transport reconstructs the existing response shape for
structured parsers and tool calls.

Current profile defaults (seconds / output tokens; repair column applies only
to components with a repair call):

| Component | Connect | Inactivity | Hard | Normal | Repair |
| --- | ---: | ---: | ---: | ---: | ---: |
| Task Analyst | 5 | 90 | 600 | 4096 | 3072 |
| Planner | 5 | 120 | 600 | 4096 | 2048 |
| Worker | 5 | 90 | 600 | 2048 | 768 |
| Evaluator | 5 | 90 | 360 | 1024 | 512 |
| Global Verifier, Integration Replanner, Result Integrator | 5 | 120 | 600 | 1024 | 512 |
| Failure Analyzer | 5 | 120 | 360 | 768 | 512 |
| Recovery Replanner | 5 | 120 | 600 | 768 | 512 |

Inspect `planning_metrics.model_call_details`, Analyst/evaluation/recovery/
integration metrics, or Worker `model.finished`/`model.failed` events for the
body-free transport record. `stop_reason` distinguishes `OLLAMA_UNREACHABLE`,
`OLLAMA_REQUEST_TIMEOUT`, `OLLAMA_GENERATION_TIMEOUT`, `OLLAMA_HTTP_ERROR`,
and `OLLAMA_INVALID_RESPONSE`; `timeout_type` identifies connect, inactivity,
or hard. Connection refusal opens a five-second provider circuit. A slow but
active generation leaves it healthy. The `freya.ollama` logger writes one JSON
metadata record per chat call and excludes prompts, responses and credentials.
If a call ends with `stop_reason=stop` but Analyst falls back or Planner fails,
inspect the schema/validation error: the provider completed generation, so a
network timeout change will not repair that model output.
`stop_reason=length` means Ollama reached `num_predict`; if repaired JSON is
cut off, inspect the component's normal and repair limits in `MODEL_PROFILES`.

Run `python -m unittest tests.test_transport -v` from the repository root for
connection refusal, slow streaming, hard timeout, HTTP error, tool-call and
Planner repair coverage. The live model check still requires local Ollama.

Recovery tests cover strict schema/one repair, deterministic offline fallback,
exact action/model/revision/task budgets, failure fingerprints, same- and
different-agent retries, immutable attempts, safe DAG scope, active and
independent-branch protection, Runtime tracking, API exposure, plan revision
constraints, policy/approval boundaries, cancellation and timeout races, and
restart/migration cleanup.
They do not prove behavior of an installed Ollama model; that still requires a
local end-to-end run.

- An agent remains `Waiting` when worker slots are occupied, the agent already
  has an active task, or another task owns the same workspace.
- Pause applies after the current call and its deadline still advances.
- Startup marks unfinished tasks Failed after an unclean server exit.
- Startup also marks Queued, Planning, Planned, Running and `Integrating`
  orchestrations Failed once and records `freya.interrupted` without changing persisted
  plans. Durable
  graph nodes are closed as cancelled/skipped so none remain apparently active.
  Dynamic agents for the interrupted orchestration are soft-archived and an
  idempotent `freya.dynamic_agent.archived` event records cleanup.
- A planner timeout or invalid repaired response leaves the orchestration Failed;
  inspect `planning_metrics` and `freya.planning.failed` on the run.
- A Runtime task `Success` means execution finished technically. Its graph node
  remains `evaluating` until semantic evidence is accepted; failed, missing or
  contradictory evidence fails closed. Inspect the orchestration evaluations
  endpoint and `freya.evaluation.*` events.
- When textual model output contains several JSON actions, only the first runs;
  later actions are regenerated after the actual tool result.
- `run_command` is allowlisted and uses argv without a shell. Agent code runs
  inside Docker on a disposable copy. If Docker cannot start, inspect
  `SandboxUnavailable` and check the daemon and local image.
- A non-accepted evaluation briefly enters `recovery_pending`. Inspect the
  recovery action and attempt history before treating it as terminal. Offline
  mode records a conservative `fail`; online mode may schedule a bounded retry
  or validated revision. Repeated equivalent failures and exhausted budgets
  produce `freya.recovery.exhausted`.
- If a worker completed a command but evaluation is blocked, inspect the
  `command_execution` item in `verification.evidence`; do not infer success from
  the final answer alone. If a retry targets an existing artifact, inspect the
  persisted workspace state and prefer read/execute/validate over recreating it.

See [API.md](API.md) for routes and [ARCHITECTURE.md](ARCHITECTURE.md) for trust
and process boundaries.
The Freya overview refreshes orchestration cards and shows each delegated
agent's objective, status, duration, token usage, and model-call count. Keep
these values sourced from persisted task snapshots when changing the view.
Queued, Planning, Planned, Running and Integrating orchestrations remain visible while the
run is active, and recent terminal runs retain their plan and child results.
