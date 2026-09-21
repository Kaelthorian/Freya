# Control Center subsystem

This directory owns the local web API, SQLite state and spawned execution
runtime. `frontend/` is its browser client and `tools.py` owns workspace-scoped
filesystem, command and Git implementations.

`task_analyst.py` owns the tool-free prompt-rewrite contract and semantic
reconciliation against observable source-prompt facts.
`planner.py` owns the versioned orchestration-plan contract, normalization and
DAG validation. `orchestrator.py` runs the Task Analyst first when an enabled
agent has `config.orchestration_role=task_analyst` (with a legacy name/role
fallback), then persists that snapshot before it selects and
delegates to existing agents through `Runtime`; workers cannot create agents or
bypass tool policy.

## Boundaries

- Keep HTTP parsing/same-origin checks in `http.py` and business validation in
  `api.py`/`config.py`.
- Keep all durable state changes in `storage.py`; schema changes must preserve
  existing databases and update storage tests.
- The parent scheduler owns SQLite. Workers report sanitized events over their
  queue and must not write the database.
- Recheck tool permission and path policy inside `worker.py`; API validation is
  not the worker trust boundary.
- Keep `capabilities.py` as the tool-to-action registry and `policy.py` as the
  single decision point. Store effective policies in task snapshots.
- Planner `required_capabilities` are declarative requirements. They must use
  the capability registry but must never mutate or bypass agent policy.
- Use `storage.py` conditional transitions for orchestration state. Save the
  plan with `Planning → Planned` atomically, never reactivate a terminal run,
  and keep cancellation serialized with task submission.
- The production Planner calls loopback Ollama without tools. Deterministic
  fallback requires the explicit `--planner-offline` mode.
- Task Analyst output is strictly validated JSON. It receives the original
  prompt, has no tools or workspace authority, and may use a deterministic
  fallback if its model call fails. Its `operational_prompt` replaces the human
  wording for Planner and workers; the original remains immutable audit evidence.
- Interactive plans append a dependent QA node with `interactive-testing`, then
  the normal read-only Code Auditor. `run_command` accepts bounded stdin for
  Python only and closes stdin otherwise so `input()` cannot consume the wall-clock deadline.
- Keep context assembly in `agent_context.py`; do not add role-specific global
  prompts to the worker. Repeated non-recoverable tool failures must be
  bounded before consuming the task step budget.
- `worker.py` must stop repeated successful read-only actions when no workspace
  progress is observed and bound consecutive denied/repeatedly blocked actions.
  `tools.py` and `agent_context.py` must filter Git
  inspection when the task workspace is not inside a checkout.
- `storage.py`'s orchestration log query merges runtime and orchestration event
  timelines without exposing private model reasoning; preserve source labels
  and stable evidence IDs when extending it. Every persisted row must retain
  the normalized actor/workspace trace (`who`, `where`, `when`, `what`, `how`,
  `phase`, `trace_id`) so Task Analyst and every delegated agent are auditable.
- Keep Skill validation, resolution, compatibility diagnostics and compact
  rendering in `skills.py`; Skills never execute tools or modify policy. Task
  records must retain immutable Skill snapshots and versions.
- Preserve spawned-process containment and descendant cleanup for cancel,
  restart, timeout and shutdown.
- Never persist secret values or private model thinking. `secret_env` contains
  a variable name only, and allowed names begin with `ACC_SECRET_`.
- The web server remains loopback-only unless authentication, CSRF, deployment
  and multiuser threat models are designed and documented together.
- Parent-owned approval requests are durable, sanitized and fail-closed; Autonomy and Skills never grant capabilities.

## Validation

From the repository root:

```powershell
python -m unittest tests.test_control_security tests.test_control_storage tests.test_control_runtime tests.test_control_api -v
python -m unittest tests.test_capabilities -v
python -m unittest tests.test_agent_context -v
python -m unittest tests.test_skills -v
python -m compileall -q control_center
node --check frontend\app.js
python -m control_center --help
```

Update `docs/REPOSITORY_MAP.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`
and `docs/API.md` when responsibilities, routes, state, limits or commands
change.
