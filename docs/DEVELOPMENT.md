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
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\core.js
node --check frontend\components.js
node --check frontend\views.js
node --check frontend\dialogs.js
node --check frontend\icons.js
python -m control_center --help
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

## Debugging

- An agent remains `Waiting` when worker slots are occupied, the agent already
  has an active task, or another task owns the same workspace.
- Pause applies after the current call and its deadline still advances.
- Startup marks unfinished tasks Failed after an unclean server exit.
- Startup also marks Queued, Planning, Planned and Running orchestrations Failed
  once and records `freya.interrupted` without changing persisted plans. Durable
  graph nodes are closed as cancelled/skipped so none remain apparently active.
- A planner timeout or invalid repaired response leaves the orchestration Failed;
  inspect `planning_metrics` and `freya.planning.failed` on the run.
- A Success record means the model finished and configured verification did not fail. Verification evidence and explicit unavailable/skipped reasons remain in the task result; failed checks produce Failed.
- When textual model output contains several JSON actions, only the first runs;
  later actions are regenerated after the actual tool result.
- `run_command` is allowlisted and uses argv without a shell. Permission
  `execute` still runs code with the local Windows user's privileges.

See [API.md](API.md) for routes and [ARCHITECTURE.md](ARCHITECTURE.md) for trust
and process boundaries.
The Freya overview refreshes orchestration cards and shows each delegated
agent's objective, status, duration, token usage, and model-call count. Keep
these values sourced from persisted task snapshots when changing the view.
Queued, Planning, Planned and Running orchestrations remain visible while the
run is active, and recent terminal runs retain their plan and child results.
