# Development and operation

## Prerequisites

- Python 3.10 or later.
- Ollama running locally with at least one installed model.
- A modern browser. Node is needed only for JavaScript syntax validation.

The application runtime uses the Python standard library. If `psutil` is
installed, `/api/health` also reports CPU and RAM; otherwise telemetry is
explicitly unavailable.

## Run the web control center

From the repository root:

```powershell
ollama list
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

Open `http://127.0.0.1:8765`. Valid worker counts at the CLI are 1–8. A lock in
the selected data directory prevents two schedulers from claiming one database.
Stop with `Ctrl+C`; active and queued tasks are cancelled and logged.

The default database is `data/control_center.sqlite3`. SQLite may also create
`-wal` and `-shm` files. Workspaces are `data/workspaces/<random-id>/`. The
database, locks and generated workspaces are ignored by Git.

Each web task starts with an empty workspace. There is no upload/import endpoint
in this MVP. For deterministic work on an existing folder, use the CLI with
`--workspace` after reviewing that command's evaluator behavior.

### Secrets

Set a server environment variable whose name starts with `ACC_SECRET_`, then
enter only that variable name in the agent configuration:

```powershell
$env:ACC_SECRET_OLLAMA = "value-not-stored-in-sqlite"
python -m control_center
```

The endpoint validator accepts only loopback Ollama base URLs without embedded
credentials, paths, query strings or fragments.

## CLI and benchmark

```powershell
python agent.py --model qwen2.5-coder:7b
python agent.py --task "Create a module that ..." --skip-final-tests
python benchmark.py --models qwen2.5-coder:7b gemma4:latest --runs 5
```

The CLI normally runs `evaluator/test_calculator.py`; `--skip-final-tests`
changes success to mean normal model completion without an independent quality
gate. The benchmark uses the same prompt/evaluator and a fresh workspace for
every measured run.

## Validate changes

```powershell
python -m unittest discover -s tests -v
python -m py_compile agent.py benchmark.py hardware.py ollama_client.py tools.py
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\core.js
node --check frontend\components.js
node --check frontend\views.js
node --check frontend\dialogs.js
node --check frontend\icons.js
python -m control_center --help
```

Runtime tests use a local fake Ollama HTTP server and spawned worker processes.
The symlink-escape regression skips when the Windows account cannot create
symlinks. End-to-end validation additionally requires the actual local Ollama
service and an installed tool-capable model.

## Debugging

- An agent remains `Waiting` when all worker slots are occupied or that same
  agent already has an active task.
- Pause applies after the current call; its deadline still advances.
- After an unclean server exit, startup marks unfinished tasks Failed rather
  than silently resuming them.
- A web `Success` record is a normal model finish, not an evaluator guarantee.
- If Ollama returns several textual JSON actions at once, only the first runs;
  subsequent actions must be regenerated after the real tool observation.
- `run_tests` is the fixed calculator evaluator. Do not present it as a generic
  test runner.

See [API.md](API.md) for routes and [ARCHITECTURE.md](ARCHITECTURE.md) for the
trust and process boundaries.
