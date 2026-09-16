# Control Center API

All routes are same-origin under `/api` and return JSON errors as
`{"error":"message"}`. Mutations accept `application/json`. SSE is
`text/event-stream`.

## Agents and catalogue

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/agents` | list or create agents |
| GET / PATCH / DELETE | `/api/agents/{id}` | read, edit or soft-delete an idle agent |
| POST | `/api/agents/{id}/duplicate` | copy definition without history |
| POST | `/api/agents/{id}/pause` | pause between actions |
| POST | `/api/agents/{id}/resume` | resume task dispatch/actions |
| POST | `/api/agents/{id}/restart` | cancel its tasks and reset runtime state |
| POST | `/api/agents/{id}/tasks` | assign `{ "prompt": "..." }` |
| GET | `/api/tools` | actual and explicitly unavailable tools |
| GET | `/api/models?endpoint=...` | installed models from local Ollama |
| GET | `/api/config` | defaults and MVP capability flags |

Create/patch fields are `name`, `description`, `role`, `enabled`, `tools` and
`config`. Configuration includes `model`, loopback `endpoint`, `temperature`,
`context_window`, step/time/token/model/tool limits, `retries`, `system_prompt`,
`permissions`, relative `allowed_directories`, `forbidden_commands`, and an
optional `secret_env` name.

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
