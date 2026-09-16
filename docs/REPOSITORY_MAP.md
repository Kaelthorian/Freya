# Repository map

Mini-Coder is a local Ollama coding agent, benchmark, and persistent web control
center. The CLI and web runtime share the restricted tools but have separate
transports, execution policy, and result stores.

## Directory map

```text
.
├── agent.py                  single-task CLI and fixed acceptance gate
├── ollama_client.py          CLI Ollama transport
├── tools.py                  restricted workspace tools shared by both flows
├── benchmark.py              repeatable fresh-workspace model comparisons
├── hardware.py               optional NVIDIA telemetry for CLI benchmarks
├── task.txt                  fixed calculator task
├── control_center/           web API, scheduler, workers, SQLite and security
│   ├── __main__.py           local server entry point and instance lock
│   ├── http.py / api.py      HTTP/SSE adapter and application routes
│   ├── runtime.py            task queue and child-process lifecycle
│   ├── worker.py             bounded Ollama/tool loop and per-agent policy
│   ├── transport.py          non-redirecting local Ollama HTTP client
│   ├── storage.py            transactional SQLite repository and metrics
│   ├── schema.sql            nine persistent tables and indexes
│   ├── config.py             agent defaults, catalogue and validation
│   └── security.py           secret and private-thinking sanitization
├── frontend/                 dependency-free dark web client
│   ├── index.html / styles.css
│   ├── app.js / core.js      routing, state, API and SSE refresh
│   ├── views.js              dashboard and detail views
│   ├── dialogs.js            agent/task forms and confirmations
│   └── components.js / icons.js
├── evaluator/                calculator acceptance tests outside workspaces
├── tests/                    CLI, storage, API, runtime and policy tests
├── workspace/                default CLI model-writable directory
├── results/                  generated CLI/benchmark JSONL and CSV
├── data/                     generated web database and task workspaces
└── docs/                     architecture, API and operating instructions
```

`workspace/`, `results/` and `data/` are generated. `evaluator/` is source and
stays outside model-writable workspaces.

## Main flows

### Web task

1. `frontend/` calls `control_center/http.py`, which applies same-origin and
   loopback Host checks before dispatching to `api.py`.
2. `api.py` validates agent definitions and asks `runtime.py` to enqueue a task.
   `storage.py` saves an immutable configuration/tool snapshot.
3. The scheduler starts `worker.py` in a spawned process when a worker slot and
   the agent are free. A Windows Job Object or POSIX process group contains
   descendant processes.
4. `worker.py` calls local Ollama `/api/chat`, validates each requested tool
   against the snapshot, and sends sanitized events to the parent.
5. The parent persists events, steps, metrics and terminal state. SSE clients
   replay events using the monotonic event ID.

### CLI task and benchmark

`agent.py` runs one bounded loop and normally invokes the external calculator
evaluator. `benchmark.py` repeats the same prompt and evaluator in fresh
workspaces, with optional GPU samples from `hardware.py`.

## Where to make changes

| Change | Entry points and validation |
| --- | --- |
| Web endpoint or response | `control_center/api.py`, `http.py`, `tests/test_control_api.py` |
| Persistent field or metric | `schema.sql`, `storage.py`, `tests/test_control_storage.py` |
| Scheduling, pause or cancellation | `runtime.py`, `tests/test_control_runtime.py` |
| Tool policy or agent loop | `worker.py`, `config.py`, runtime tests |
| Secret handling | `security.py`, security/runtime/storage tests |
| Web UI | `frontend/app.js`, `views.js`, `dialogs.js`, `styles.css` |
| Shared filesystem/command tool | `tools.py`, `tests/test_tools.py`; review both runtimes |
| CLI model loop or transport | `agent.py`, `ollama_client.py`, CLI tests |
| Benchmark results | `benchmark.py`, `hardware.py`, benchmark tests |
| Commands or architecture | `README.md`, `docs/`, root and scoped `AGENTS.md` |

See [ARCHITECTURE.md](ARCHITECTURE.md), [DEVELOPMENT.md](DEVELOPMENT.md), and
[API.md](API.md).
