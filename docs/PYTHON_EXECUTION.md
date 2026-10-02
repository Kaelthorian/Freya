# Python execution runtime

`tools.Toolbox.tool_run_command` validates argv/paths/stdin after Worker policy
checks. Python scripts, pytest, unittest discovery, py_compile and Ruff then use
`python_execution.PythonExecutionManager` (`PythonVenvBackend`, implementing
`ExecutionBackend`). Read-only Git still uses the optional Docker backend;
Docker is never consulted for Python. Planner granularity is unchanged.

## Ownership and flow

- `Runtime.submit` passes its configured data directory to spawned Workers.
  `PolicyToolbox` uses the immutable agent provenance orchestration ID.
- One environment lives at `<data_dir>/runtime_envs/<orchestration_id>/venv/`.
  `environment.json` records owner, absolute interpreter, host version and
  dependency cache. It is infrastructure, not a deliverable or owned path.
- Creation is lazy on the first allowed Python invocation. File-only work
  creates no venv. Tasks, Workers, evaluation and Recovery share that owner.
- Host resolution probes `sys.executable`, then available `py`, `python`,
  `python3`, once per process. Python 3.10+ is required. All subsequent commands
  use the absolute `Scripts/python.exe` or `bin/python`; no activation scripts.
- Cross-process file locks and in-process locks serialize creation, dependency
  changes, execution and cleanup for each owner. Each verification case starts
  its own subprocess with bounded stdin, separate streams and timeout.
- `Orchestrator._run` cleans in `finally` only when status is `Success`,
  `Failed` or `Cancelled` and children have stopped. Cancellation also cleans.
  `Runtime` sweeps terminal environments at startup and every five seconds,
  covering late child termination. Active runs are preserved regardless of age.
  Unknown-owner directories expire after 24 hours. Cleanup failures emit an
  event and are retried by later sweeps. Small lock files persist outside venvs.
- Standalone Runtime tasks own an environment until task completion, including
  approval suspension. Direct library `Toolbox` users must explicitly call
  `box.python_execution.cleanup()` when their work ends.

## Dependencies

Runtime installs declarative sources through `<venv_python> -m pip install`:
`requirements.txt`; `requirements-test.txt` for pytest/unittest;
`requirements-dev.txt` for tests/Ruff; installable `pyproject.toml` (`[project]`
or `[build-system]`); then legacy `setup.py`/`setup.cfg` projects. It installs
the project with `pip install .`, selecting declared `test`/`tests`/`dev` extras
for the corresponding test/dev invocation. It does not scan arbitrary requirements files
or guess packages from imports. Trusted `runtime_context.python_dependencies`
can carry explicit package specifiers; it is not a Worker tool argument.
pytest/Ruff capabilities provision their corresponding runner when needed.
Stdlib scripts, unittest and py_compile require no pip call without manifests.

Successful setup records source/workspace/manifest SHA-256 (including referenced
requirements/constraints files) and installation
time. Unchanged sources skip setup across cases, processes and Recovery;
changed manifests synchronize again. A `ModuleNotFoundError` permits one
resynchronization/retry only when declarative sources exist. pip failures keep
manifest identity and captured stdout/stderr; they never fabricate a program
exit code. Dependency installation may use the network and execute project
build code under the same trusted execution permission.

## Evidence and configuration

`FREYA_PYTHON_EXECUTION_BACKEND=venv` is the default; local settings accept the
same key. Other values fail startup rather than implicitly selecting Docker.
The immutable `Settings` snapshot is propagated through spawn.

Program exits produce `command_execution` (or test/lint/compilation facts),
`program_started=true`, `environment_available=true`, captured streams and the
real exit code. Nonzero exit is `PROGRAM_FAILURE`. Invalid-input handling with
exit zero is successful execution; Evaluator interprets output semantically.

Missing Python (`PYTHON_NOT_AVAILABLE`), creation/launch failure
(`ENVIRONMENT_UNAVAILABLE`), dependency failure (`DEPENDENCY_SETUP_FAILED`),
and policy denial produce unavailable verification, not failed code evidence.
Final State Snapshot represents these as `execution_environment`,
`status=unavailable`, `program_started=false`, with no program exit code.
Evaluator keeps required runtime criteria unknown and routes resource review
to Orchestrator; Recovery cannot repair code on this basis. `PROCESS_TIMEOUT`
preserves started=true and streams, kills the process tree and has no exit code.

`python_env.*` events report creating/created, dependencies
started/completed/failed, execution started/completed/failed, cleanup
started/completed/failed and stale_cleanup. Owner/backend/version/hash/duration
are recorded where applicable. Events never contain credential values.

A venv is dependency isolation, **not an OS sandbox**. Python executes in the
real workspace with host user permissions. Policy validates tool calls but
cannot constrain filesystem/network access performed inside trusted Python.

## Validation

From the repository root in PowerShell:

```powershell
python -m unittest tests.test_python_execution tests.test_tools tests.test_evaluator tests.test_settings -v
python -m compileall -q control_center
python -m control_center --help
```

`test_python_execution` exercises real venv creation, independent converter
processes, stdlib without pip, timeout and cleanup. Dependency cache/failure
tests use controlled backend responses; a real local-wheel requirements test
also verifies pip installation and caching without a public package index.
Injected Planner/Worker fixtures do not establish live Ollama behavior.
