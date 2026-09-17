# Architecture

Freya includes a first-class orchestration layer. User prompts enter `control_center/orchestrator.py`, which persists a run, selects enabled existing agents, delegates bounded tasks through `Runtime`, and integrates persisted results. Workers remain the only components allowed to invoke tools; each receives a generic policy plus its identity, instructions, skills, workspace, and limits. Orchestration runs, delegations, and events are stored durably, and SQLite migrations preserve existing data.

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
                  capability resolver → policy engine → platform tools → selected workspace
```

The worker uses `control_center/transport.py`, which disables proxies and redirects so an
authorization value cannot be forwarded to another destination.

## Persistence and events

SQLite runs in WAL mode with foreign keys, one connection per operation and
`BEGIN IMMEDIATE` for atomic writes. The schema holds agents, configs, tools,
agent/tool links, reusable skills and agent/skill links, tasks, executions, steps,
logs, approvals and metrics. Agent deletion is
soft so task history remains readable. Each MVP task has one execution and an
immutable copy of the agent config, effective capability policy, structured
agent blocks, verification state, effective policy, and enabled tools. Approval
requests are durable records linked to the task and agent, with sanitized
arguments and explicit pending/approved_once/approved_task/denied statuses.

Events receive a monotonic integer ID. `step.started` and `step.finished`
events build the reconstructable timeline while every attempt remains in
`log_events`. SSE accepts `Last-Event-ID`/`after`, replays later events and then
streams updates. On startup, abandoned Queued, Running, WaitingForApproval or Paused records become
Failed, pending approvals are denied as cancelled, and unfinished steps are closed.

## Runtime and control semantics

The scheduler supports bounded concurrency and serializes tasks per agent and
per resolved workspace. An agent can set an existing absolute default directory;
task submission can override that path for one run, and an explicit empty
override creates a fresh directory under `data/workspaces/`. Every task records
the resolved workspace in its immutable snapshot and runs in a spawned process.
On Windows the worker is assigned to a kill-on-close Job Object before tool
execution; POSIX uses a process session. Cancel, restart and shutdown terminate
the worker tree and persist a terminal event.

Pause is cooperative: an in-flight model or tool call can finish, then the
worker pauses between actions. When a capability or autonomy rule is ask, the
worker emits approval.requested, the parent persists the request, changes the
task to WaitingForApproval, and blocks the worker until once/task/deny is
resolved. Cancellation denies pending requests and terminates the worker. The total wall-clock deadline continues while
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
- The folder browser lists directories visible to the local server. Saving an
  agent validates that its selected workspace is absolute, existing and a
  directory; task submission checks it again.
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

## Capability authorization

`capabilities.py` is the registry and `CapabilityResolver` maps each tool call
to one concrete action. `write_file` becomes `filesystem.create` or
`filesystem.overwrite` after inspecting the workspace target; `run_command`
maps only to supported Python, pytest, unittest, py_compile, Ruff, or Git
actions. `policy.py` validates the per-agent JSON policy and returns explicit
`allow`, `deny`, or `approval_required` decisions. Allow and ask rules are the only source used to derive the model-visible tool list; stale legacy tool selections cannot expose a capability. Deny and approval results never invoke the underlying tool. Filesystem rules support paths, extensions and max_bytes for every filesystem action. Agents with
legacy `permissions` are converted to the same engine, and the effective
policy is copied into every task snapshot.

These are application safeguards, not a security boundary against a hostile
local user or hostile executable code.

## Reusable skills

`control_center/skills.py` is the single registry and validation layer for
declarative Skills. A Skill contains specialty knowledge, instructions,
adaptable procedures, tags, a stable ID and a positive version.
`resolve_agent_skills` orders assigned Skills by per-agent priority, marks each
Skill operational only when every required capability is allowed and its concrete tool is available; diagnostics distinguish missing capability from missing tool/runtime support and expose recommended-capability warnings. Skills never grant capabilities or execute
tools. Workers receive compact active Skill context; task text selects up to
eight active specialties by simple name/category/tag relevance and the rendered
context has a 64,000-character budget. Each task stores an immutable copy of
every resolved Skill, including its version. Procedures are guidance; unavailable
or irrelevant steps are adapted by the model.

The orchestrator receives Skill summaries instead of full procedures. When no
external planner callback is configured, its bounded fallback gives enabled
agents with matching Skill names, categories, or tags priority over role-only
ordering.

Conflicting guidance follows this precedence:

```text
System Policy > Capability Policy > Task boundaries > Agent constraints >
Agent instructions > Skill priority > Skill instructions > Skill procedures > Task content
```

## Structured agent configuration

`agent_context.py` normalizes and merges legacy fields with JSON blocks for
`identity`, `behavior`, `autonomy`, `verification`, and `output`. The worker
uses `build_agent_context` to produce one structured context after the
immutable system policy. Identity includes purpose, responsibilities, and
constraints; behavior controls planning, ambiguity, evidence, and repeated
failure handling; autonomy records decision preferences without granting
capabilities; verification and output define evidence and result shape. The
default output remains text for legacy compatibility, while structured output is strictly validated as summary/actions/artifacts/verification/limitations. A JSON-looking invalid result receives one repair attempt; otherwise an explicit fallback and limitation are returned. Verification state is persisted separately.

The worker classifies recoverable, environment, policy, approval, and invalid
requests. A repeated non-recoverable action with the same capability,
arguments, and error reaches the configured limit and returns
`REPEATED_ACTION_BLOCKED` so model-call and step budgets are not wasted.

## Feature scope

The MVP implements local Ollama, local process workers, persistent observations
and manual tasks. Remote/distributed workers, arbitrary model endpoints, queues
outside this process, RAG, long-term memory, agent teams, schedules, browser,
web search, generic HTTP and database tools remain unavailable.

Ollama request fields and usage counters follow its official
[chat API](https://docs.ollama.com/api/chat); context and temperature map to
documented model parameters in the [Modelfile reference](https://docs.ollama.com/modelfile).

Skills are versioned in immutable `skill_versions` snapshots. Updates retain prior definitions, while API deletion archives the row with `deleted_at` and records a `skill.archived` audit event. Context precedence is explicit: System Policy > Capability Policy > Current User Task > Agent Constraints > Agent Instructions > Skill Priority > Skill Instructions > Skill Procedures. Skills provide guidance only and never grant capabilities.

Skill IDs remain stable. A meaningful definition change increments `version` automatically and writes the next immutable snapshot; unchanged PATCH requests create neither a version nor an update event. Assignment priority is rendered in model context and resolves conflicts only between Skills. It cannot override the current task, agent constraints, or capability policy.
