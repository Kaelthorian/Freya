# Architecture

## Boundaries

The browser, API, runtime, worker and database are separate responsibilities.
The browser never calls Ollama directly. The API validates agent definitions;
the worker revalidates the immutable task snapshot before every tool call. The
parent process alone writes execution events and state to SQLite.

```text
browser → HTTP API → SQLite
             ↓
         scheduler → spawned worker → local Ollama
                         ↓
                  policy → shared Toolbox → task workspace
```

`agent.py` remains a separate CLI flow using `ollama_client.py`. The web worker
uses `control_center/transport.py`, which disables proxies and redirects so an
authorization value cannot be forwarded to another destination.

## Persistence and events

SQLite runs in WAL mode with foreign keys, one connection per operation and
`BEGIN IMMEDIATE` for atomic writes. Nine tables hold agents, configs, tools,
agent/tool links, tasks, executions, steps, logs and metrics. Agent deletion is
soft so task history remains readable. Each MVP task has one execution and an
immutable copy of the agent config and enabled tools.

Events receive a monotonic integer ID. `step.started` and `step.finished`
events build the reconstructable timeline while every attempt remains in
`log_events`. SSE accepts `Last-Event-ID`/`after`, replays later events and then
streams updates. On startup, abandoned Queued, Running or Paused records become
Failed and unfinished steps are closed.

## Runtime and control semantics

The scheduler supports bounded concurrency and serializes tasks per agent.
Each task gets a fresh directory under `data/workspaces/` and a spawned process.
On Windows the worker is assigned to a kill-on-close Job Object before tool
execution; POSIX uses a process session. Cancel, restart and shutdown terminate
the worker tree and persist a terminal event.

Pause is cooperative: an in-flight model or tool call can finish, then the
worker pauses between actions. The total wall-clock deadline continues while
paused. Progress is the greatest fraction of the configured step, model-call,
tool-call and token budgets and reaches 100 only at termination.

Token usage comes from the provider response. `num_predict` bounds generated
tokens, but provider-reported prompt usage can make a response cross the total
budget; in that case no tool from that response executes. Read-only tool calls
may retry within the configured bound. Writes and process execution are never
automatically retried.

Text-mode models may emit JSON actions instead of native tool calls. When a
model concatenates several action objects, the worker executes only the first,
returns its actual observation to the model and waits for a new action. This
prevents later actions from relying on invented tool results.

## Security limits

- HTTP listens on `127.0.0.1` and checks `Host`, `Origin` and cross-site fetch
  metadata. It has no accounts or production authentication and must remain a
  local single-user service.
- Model filesystem paths resolve inside the task workspace and then inside the
  configured relative allowlist. Absolute paths, traversal and symlink escapes
  are rejected.
- Tool dispatch is allowlisted. `run_command` uses argv with `shell=False` and
  accepts only workspace Python/tests, Ruff, and scoped read-only Git commands.
- `execute` is a trust grant, not an OS sandbox. A Python file run inside the
  workspace has the Windows user's process privileges and may access host
  resources. Use disposable inputs and trusted local models.
- Secrets are environment references named `ACC_SECRET_...`. The worker reads
  the selected value for the Ollama Authorization header, registers it for
  redaction, then removes credential-like variables before tool subprocesses.
- Sanitization occurs before persistence and again before HTTP output. It
  removes credential patterns, registered secret values, private-key blocks,
  bearer values, URL credentials and private thinking fields/tags.

These are application safeguards, not a security boundary against a hostile
local user or hostile executable code.

## Feature scope

The MVP implements local Ollama, local process workers, persistent observations
and manual tasks. Remote/distributed workers, arbitrary model endpoints, queues
outside this process, RAG, long-term memory, agent teams, schedules, browser,
web search, generic HTTP and database tools remain unavailable.

Ollama request fields and usage counters follow its official
[chat API](https://docs.ollama.com/api/chat); context and temperature map to
documented model parameters in the [Modelfile reference](https://docs.ollama.com/modelfile).
