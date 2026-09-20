# Development and operation

## Prerequisites

- Python 3.10 or later.
- Ollama running locally with at least one installed model.
- A modern browser. Node is needed only for JavaScript syntax validation.

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

Semantic evaluation has separate local-model configuration:

```powershell
python -m control_center --evaluator-model qwen2.5-coder:7b `
  --evaluator-endpoint http://127.0.0.1:11434 --evaluator-timeout 120
```

The evaluator endpoint is also loopback-only and the adapter exposes no tools.
Use `--evaluator-offline` explicitly for deterministic evidence-only evaluation.
Offline evaluation is conservative: acceptance requires configured verification
to be requested, attempted and passed. Runtime result text and agent claims are
not objective evidence. Missing evidence returns `blocked`, including through
the Orchestrator's default compatibility fallback.
For filesystem-only tasks without Git or tests, the Worker may instead verify
each modified file with an allowed `read_file` read-back; missing or mismatched
read-back evidence remains blocked or failed.

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
retry for `needs_revision`/`blocked`, different-agent retry for `rejected` when
an enabled alternative exists, and fail for evaluator errors or unavailable
alternatives. Retries always create a new persisted attempt and rerun Agent
Selector and capability-policy checks. Recovery never grants capabilities or
resolves approvals. Replanning stores an effective-plan revision without
overwriting the original plan or rerunning accepted tasks. Its deterministic
scope contains only the recovery source and never-started descendants; active,
historical and independent work is protected and revalidated again by Storage.

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
`-shm` files. Automatic workspaces use `data/workspaces/<random-id>/`. These
generated paths are ignored by Git.

In agent settings, **Choose folder** chooses that agent's default folder.
The **Assign a task** form also has its own folder selector. It starts with
the agent default, can override it for one execution, and uses a fresh generated
workspace when left empty. Task retries reuse the original folder. Editing an
agent requires it to be idle. If a chosen folder is removed later, submissions
that select it fail. Tasks that resolve to the same folder run serially.

The agent editor uses progressive disclosure: identity fields stay visible for
quick setup, while Skills, model, workspace, capabilities, tools, behavior,
verification, autonomy, output, and limits are compact expandable sections. The
Agents page also exposes the generic Programmer preset.

The **Skills** page manages reusable declarative knowledge. Create or edit a
Skill with a stable lowercase ID, version, instructions, adaptable procedures,
tags, and required/recommended capabilities. Assign it to an agent with a
priority from the agent editor. Required capability diagnostics never change
the agent's policy; a Skill is operational only when its requirements are
allowed. Tasks snapshot the resolved Skill version and content.

The Skills page can export one Skill or the whole visible catalogue as JSON. Import accepts either one Skill object or a skills array; the server validates the complete batch, skips identical existing definitions, rejects conflicting IDs or names, and never partially applies a failing import.

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
`tests/test_capabilities.py`, `tests/test_agent_context.py` and
`tests/test_skills.py`. The worker
resolves and evaluates a capability before every tool invocation. A policy or autonomy ask creates a durable approval request, moves the task to WaitingForApproval, and the Approvals page resolves it once, for the task, or denies it.

Runtime tests use a local fake Ollama server and spawned worker processes. The
symlink regression skips when the Windows account cannot create symlinks. A
full end-to-end task additionally requires local Ollama and an installed model.
Planner tests cover atomic transitions, cancellation races, restart recovery,
approval waiting, child failure propagation, deadlines and simulated Ollama.
Execution-graph tests cover pure DAG transitions, sequential and parallel
scheduling, joins, branch-local failure propagation, approval waits, paused
agents, per-agent serialization, concurrency limits, cancellation and migration.
Evaluator tests cover hard evidence precedence, prompt injection, strict schema,
one repair, per-criterion coverage, immutable persistence, API exposure, graph
gating, duplicate prevention, technical failure, cancellation, timeout and
restart recovery.
Integration tests cover deterministic preconditions, exact global criteria,
hard evidence precedence, prompt injection, strict schema/one repair,
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
- A planner timeout or invalid repaired response leaves the orchestration Failed;
  inspect `planning_metrics` and `freya.planning.failed` on the run.
- A Runtime task `Success` means execution finished technically. Its graph node
  remains `evaluating` until semantic evidence is accepted; failed, missing or
  contradictory evidence fails closed. Inspect the orchestration evaluations
  endpoint and `freya.evaluation.*` events.
- When textual model output contains several JSON actions, only the first runs;
  later actions are regenerated after the actual tool result.
- `run_command` is allowlisted and uses argv without a shell. Permission
  `execute` still runs code with the local Windows user's privileges.
- A non-accepted evaluation briefly enters `recovery_pending`. Inspect the
  recovery action and attempt history before treating it as terminal. Offline
  mode records a conservative `fail`; online mode may schedule a bounded retry
  or validated revision. Repeated equivalent failures and exhausted budgets
  produce `freya.recovery.exhausted`.

See [API.md](API.md) for routes and [ARCHITECTURE.md](ARCHITECTURE.md) for trust
and process boundaries.
The Freya overview refreshes orchestration cards and shows each delegated
agent's objective, status, duration, token usage, and model-call count. Keep
these values sourced from persisted task snapshots when changing the view.
Queued, Planning, Planned, Running and Integrating orchestrations remain visible while the
run is active, and recent terminal runs retain their plan and child results.
