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
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\core.js
node --check frontend\components.js
node --check frontend\views.js
node --check frontend\dialogs.js
node --check frontend\icons.js
python -m control_center --help
```

Runtime tests use a local fake Ollama server and spawned worker processes. The
symlink regression skips when the Windows account cannot create symlinks. A
full end-to-end task additionally requires local Ollama and an installed model.

## Debugging

- An agent remains `Waiting` when worker slots are occupied, the agent already
  has an active task, or another task owns the same workspace.
- Pause applies after the current call and its deadline still advances.
- Startup marks unfinished tasks Failed after an unclean server exit.
- A `Success` record means the model finished normally; quality depends on the
  task's own validation commands and evidence.
- When textual model output contains several JSON actions, only the first runs;
  later actions are regenerated after the actual tool result.
- `run_command` is allowlisted and uses argv without a shell. Permission
  `execute` still runs code with the local Windows user's privileges.

See [API.md](API.md) for routes and [ARCHITECTURE.md](ARCHITECTURE.md) for trust
and process boundaries.
