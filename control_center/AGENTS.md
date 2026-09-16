# Control Center subsystem

This directory owns the local web API, SQLite state and spawned execution
runtime. `frontend/` is its browser client and `tools.py` owns workspace-scoped
filesystem, command and Git implementations.

`orchestrator.py` is the Freya-only coordination boundary. It may select and
delegate to existing agents through `Runtime`, but workers cannot create agents
or bypass tool policy.

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
- Keep context assembly in `agent_context.py`; do not add role-specific global
  prompts to the worker. Repeated non-recoverable tool failures must be
  bounded before consuming the task step budget.
- Keep Skill validation, resolution, compatibility diagnostics and compact
  rendering in `skills.py`; Skills never execute tools or modify policy. Task
  records must retain immutable Skill snapshots and versions.
- Preserve spawned-process containment and descendant cleanup for cancel,
  restart, timeout and shutdown.
- Never persist secret values or private model thinking. `secret_env` contains
  a variable name only, and allowed names begin with `ACC_SECRET_`.
- The web server remains loopback-only unless authentication, CSRF, deployment
  and multiuser threat models are designed and documented together.

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
