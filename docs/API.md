# Control Center API

All routes are same-origin under `/api` and return JSON errors as
`{"error":"message"}`. Mutations accept `application/json`. SSE is
`text/event-stream`.

## Freya orchestration

`POST /api/orchestrations` with `{ "prompt": "..." }` queues a bounded run.
`GET /api/orchestrations` lists runs and `GET /api/orchestrations/{id}` returns
the run, delegations, and events needed to reconstruct it. Existing agent and
task routes remain compatible.

## Agents and catalogue

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/agents` | list or create agents |
| GET / PATCH / DELETE | `/api/agents/{id}` | read, edit or soft-delete an idle agent |
| POST | `/api/agents/{id}/duplicate` | copy definition without history |
| POST | `/api/agents/{id}/pause` | pause between actions |
| POST | `/api/agents/{id}/resume` | resume task dispatch/actions |
| POST | `/api/agents/{id}/restart` | cancel its tasks and reset runtime state |
| POST | `/api/agents/{id}/tasks` | assign a task with optional workspace override |
| GET | `/api/tools` | actual and explicitly unavailable tools |
| GET | `/api/models?endpoint=...` | installed models from local Ollama |
| GET | `/api/config` | defaults and MVP capability flags |
| GET | `/api/workspaces/browse?path=...` | list subdirectories of an absolute local path |

Create/patch fields are `name`, `description`, `role`, `enabled`, `tools` and
`config`. Configuration includes `model`, loopback `endpoint`, `temperature`,
`context_window`, step/time/token/model/tool limits, `retries`, `system_prompt`,
`permissions`, relative `allowed_directories`, `forbidden_commands`, and an
optional `secret_env` name. Agent `workspace_path` is either empty for a
generated workspace per task or an absolute existing directory used by default.

Task assignment accepts `{ "prompt": "...", "workspace_path": "..." }`.
When omitted, the agent's configured workspace applies. An absolute existing
directory overrides it for that task alone. An explicit empty string requests a
fresh generated workspace. Task retries reuse the original task's workspace.

The workspace browser defaults to the parent of the configured data directory
when `path` is omitted. It returns the resolved current path, parent, write-access hint, up
to 500 immediate subdirectories, and a `truncated` flag. Saving the agent is the
authoritative validation step.

## Tasks, observations and metrics

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/tasks?agent_id=&status=&limit=` | task history |
| GET | `/api/tasks/{id}` | task snapshot, events and merged timeline |
| POST | `/api/tasks/{id}/cancel` | terminate a live task |
| POST | `/api/tasks/{id}/retry` | create a new task from a terminal prompt |
| GET | `/api/logs` | filter by agent, task, level, tool, error and date |
| GET | `/api/metrics?agent_id=` | global or per-agent aggregates |
| GET | `/api/health` | API, runtime and optional host telemetry |
| GET | `/api/events?after=N` | replay/global SSE stream |
| GET | `/api/tasks/{id}/events?after=N` | replay/task SSE stream |

Task states are `Queued`, `Running`, `Paused`, `Success`, `Failed` and
`Cancelled`. Agent states are `Idle`, `Running`, `Waiting`, `Paused`, `Error`
and `Offline`. Step states use the corresponding running/terminal values.

Every SSE update has an integer `id`, `event_type`, timestamp, agent/task IDs
and relevant status/tool/input/output/error/duration fields. Clients should send
`Last-Event-ID` or `after` when reconnecting and refresh their current resource
from the JSON route; SSE is a change signal and durable event replay.
