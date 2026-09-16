# Mini-Coder repository instructions

Mini-Coder provides a local Ollama coding-agent CLI, repeatable model benchmark,
and a persistent Agent Control Center web application. The CLI flow is
`agent.py` → `ollama_client.py` → Ollama → `tools.py` → workspace/evaluator.
The web flow is `frontend/` → `control_center/` API/SQLite/scheduler → spawned
worker → Ollama/tools → isolated task workspace.

## Boundaries

- Keep all model-controlled file reads and writes inside the selected task
  workspace. The fixed evaluator belongs in `evaluator/` and must stay outside
  that writable workspace.
- Keep tool dispatch allowlisted. Do not add `shell=True` or a free-form shell
  tool. `run_command` accepts argv arrays and a small command set.
- Model inference defaults to Ollama's loopback API. Preserve the API token and
  timing metrics used to compare models.
- Benchmark runs must use fresh workspaces, the same task prompt, and the same
  evaluator. Do not silently pull models or change the prompt per model.
- Keep Python support at 3.10+ and avoid runtime dependencies unless the docs
  and setup steps are updated.
- Keep the web server loopback-only and its worker/database boundaries intact.
  See `control_center/AGENTS.md` for the subsystem rules.

## Validation

Run from the repository root in PowerShell:

```powershell
python -m unittest discover -s tests -v
python -m py_compile agent.py benchmark.py hardware.py ollama_client.py tools.py
python -m compileall -q control_center
node --check frontend\app.js
```

For end-to-end validation, use `python agent.py --model <installed-model>` and
confirm the final acceptance tests pass. Model availability depends on the local
Ollama installation.

See [docs/REPOSITORY_MAP.md](docs/REPOSITORY_MAP.md) for entry points and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for data flow and safety limits,
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for commands, and
[docs/API.md](docs/API.md) for web routes.

## Documentation maintenance

Keep the repository map and relevant operational docs current whenever tools,
module responsibilities, safety boundaries, commands, or result formats
change. Review documentation paths and commands before completing a change.
