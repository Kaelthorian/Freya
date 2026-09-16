"""SQLite repository for durable agents, execution snapshots, events and metrics.

Every operation owns its connection; multi-table writes are single transactions.
Only sanitized values cross the persistence boundary. Agent deletion retains the
foreign-keyed task history, and each MVP task has exactly one execution.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .config import TOOL_CATALOG
from .security import sanitize


TASK_STATUSES = {"Queued", "Running", "Paused", "Success", "Failed", "Cancelled"}
AGENT_STATUSES = {"Idle", "Running", "Waiting", "Paused", "Error", "Offline"}
ACTIVE_TASK_STATUSES = ("Queued", "Running", "Paused")
EXECUTION_FIELDS = {
    "status", "started_at", "finished_at", "duration_seconds", "steps",
    "progress", "result", "error",
}
METRIC_FIELDS = {"model_calls", "tool_calls", "prompt_tokens", "generated_tokens", "total_tokens"}
TASK_SELECT = """
SELECT t.*, e.id AS execution_id, e.status, e.started_at, e.finished_at,
       e.duration_seconds, e.steps, e.progress, e.result_json, e.error,
       m.model_calls, m.tool_calls, m.prompt_tokens, m.generated_tokens, m.total_tokens
FROM tasks t JOIN task_executions e ON e.task_id = t.id
JOIN metrics m ON m.task_id = t.id
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            connection.executemany(
                "INSERT INTO tools(name, description, available, dangerous) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "available=excluded.available, dangerous=excluded.dangerous",
                [(tool["name"], tool["description"], int(tool.get("available", True)),
                  int(tool.get("dangerous", False))) for tool in TOOL_CATALOG],
            )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _agent(self, connection: sqlite3.Connection, agent_id: str) -> dict:
        row = connection.execute(
            "SELECT a.*, c.config_json FROM agents a JOIN agent_configs c ON c.agent_id=a.id "
            "WHERE a.id=? AND a.deleted_at IS NULL", (agent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        agent = dict(row)
        agent.pop("deleted_at")
        agent["enabled"] = bool(agent["enabled"])
        agent["config"] = _load(agent.pop("config_json"))
        agent["tools"] = [item[0] for item in connection.execute(
            "SELECT tool_name FROM agent_tools WHERE agent_id=? ORDER BY tool_name", (agent_id,),
        )]
        totals = connection.execute(
            "SELECT COUNT(*) AS task_count, SUM(e.status='Success') AS successes, "
            "SUM(e.status IN ('Success','Failed','Cancelled')) AS finished, "
            "COALESCE(SUM(m.total_tokens),0) AS total_tokens FROM tasks t "
            "JOIN task_executions e ON e.task_id=t.id JOIN metrics m ON m.task_id=t.id "
            "WHERE t.agent_id=?", (agent_id,),
        ).fetchone()
        agent.update(task_count=totals["task_count"], total_tokens=totals["total_tokens"],
                     success_rate=round(100 * (totals["successes"] or 0) / totals["finished"], 1)
                     if totals["finished"] else 0)
        current = connection.execute(
            "SELECT t.id,t.prompt,e.status,e.progress FROM tasks t "
            "JOIN task_executions e ON e.task_id=t.id WHERE t.agent_id=? "
            "AND e.status IN ('Queued','Running','Paused') "
            "ORDER BY CASE WHEN e.status='Queued' THEN 1 ELSE 0 END,t.created_at LIMIT 1",
            (agent_id,),
        ).fetchone()
        agent["current_task"] = dict(current) if current else None
        return agent

    def create_agent(self, data: dict) -> dict:
        clean = sanitize(data)
        agent_id, now = str(uuid4()), utcnow()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO agents(id,name,description,role,enabled,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (agent_id, clean["name"], clean.get("description", ""), clean.get("role", ""),
                 int(clean.get("enabled", True)), "Idle" if clean.get("enabled", True) else "Offline", now, now),
            )
            self._save_config(connection, agent_id, clean)
            return self._agent(connection, agent_id)

    def _save_config(self, connection: sqlite3.Connection, agent_id: str, data: dict) -> None:
        connection.execute(
            "INSERT INTO agent_configs(agent_id,config_json) VALUES(?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET config_json=excluded.config_json",
            (agent_id, _dump(data["config"])),
        )
        connection.execute("DELETE FROM agent_tools WHERE agent_id=?", (agent_id,))
        connection.executemany("INSERT INTO agent_tools(agent_id,tool_name) VALUES(?,?)",
                               [(agent_id, name) for name in dict.fromkeys(data.get("tools", []))])

    def update_agent(self, agent_id: str, data: dict) -> dict:
        clean = sanitize(data)
        with self._connection(write=True) as connection:
            previous = self._agent(connection, agent_id)
            enabled = bool(clean["enabled"])
            status = previous["status"]
            if not enabled:
                status = "Offline"
            elif status == "Offline":
                status = "Idle"
            connection.execute(
                "UPDATE agents SET name=?,description=?,role=?,enabled=?,status=?,updated_at=? WHERE id=?",
                (clean["name"], clean.get("description", ""), clean.get("role", ""),
                 int(enabled), status, utcnow(), agent_id),
            )
            self._save_config(connection, agent_id, clean)
            return self._agent(connection, agent_id)

    def get_agent(self, agent_id: str) -> dict:
        with self._connection() as connection:
            return self._agent(connection, agent_id)

    def list_agents(self) -> list[dict]:
        with self._connection() as connection:
            ids = connection.execute("SELECT id FROM agents WHERE deleted_at IS NULL ORDER BY created_at,id").fetchall()
            return [self._agent(connection, row["id"]) for row in ids]

    def delete_agent(self, agent_id: str) -> None:
        with self._connection(write=True) as connection:
            self._agent(connection, agent_id)
            now = utcnow()
            connection.execute("UPDATE agents SET deleted_at=?,updated_at=?,enabled=0,status='Offline' WHERE id=?",
                               (now, now, agent_id))

    def set_agent_state(self, agent_id: str, status: str) -> None:
        if status not in AGENT_STATUSES:
            raise ValueError("Unknown agent status")
        with self._connection(write=True) as connection:
            self._agent(connection, agent_id)
            connection.execute("UPDATE agents SET status=?,updated_at=?,last_activity=? WHERE id=?",
                               (status, utcnow(), utcnow(), agent_id))

    @staticmethod
    def _task(row: sqlite3.Row | None, task_id: str = "") -> dict:
        if row is None:
            raise KeyError(task_id)
        task = dict(row)
        for field in ("config", "tools", "result"):
            task[field] = _load(task.pop(field + "_json"))
        return task

    def create_task(self, agent_id: str, prompt: str, workspace: str) -> dict:
        task_id, execution_id, now = str(uuid4()), str(uuid4()), utcnow()
        with self._connection(write=True) as connection:
            agent = self._agent(connection, agent_id)
            connection.execute(
                "INSERT INTO tasks(id,agent_id,agent_name,prompt,workspace,config_json,tools_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (task_id, agent_id, agent["name"], sanitize(prompt),
                sanitize(workspace), _dump(agent["config"]), _dump(agent["tools"]), now),
            )
            connection.execute("INSERT INTO task_executions(id,task_id) VALUES(?,?)", (execution_id, task_id))
            connection.execute("INSERT INTO metrics(task_id) VALUES(?)", (task_id,))
            connection.execute("UPDATE agents SET last_activity=?,updated_at=? WHERE id=?", (now, now, agent_id))
            return self._task(connection.execute(TASK_SELECT + "WHERE t.id=?", (task_id,)).fetchone(), task_id)

    def list_tasks(self, agent_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
        where, params = [], []
        if agent_id:
            where.append("t.agent_id=?")
            params.append(agent_id)
        if status:
            where.append("e.status=?")
            params.append(status)
        query = TASK_SELECT + ("WHERE " + " AND ".join(where) if where else "")
        query += " ORDER BY t.created_at DESC,t.id DESC LIMIT ?"
        with self._connection() as connection:
            return [self._task(row) for row in connection.execute(query, [*params, max(1, min(int(limit), 10000))])]

    def get_task(self, task_id: str) -> dict:
        with self._connection() as connection:
            return self._task(connection.execute(TASK_SELECT + "WHERE t.id=?", (task_id,)).fetchone(), task_id)

    def update_task(self, task_id: str, **fields: Any) -> dict:
        unknown = fields.keys() - EXECUTION_FIELDS - METRIC_FIELDS
        if unknown:
            raise ValueError("Unknown task fields: " + ", ".join(sorted(unknown)))
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise ValueError("Unknown task status")
        clean = sanitize(fields)
        with self._connection(write=True) as connection:
            row = connection.execute(TASK_SELECT + "WHERE t.id=?", (task_id,)).fetchone()
            self._task(row, task_id)
            for table, allowed in (("task_executions", EXECUTION_FIELDS), ("metrics", METRIC_FIELDS)):
                values = {("result_json" if key == "result" else key):
                          (_dump(value) if key == "result" else value)
                          for key, value in clean.items() if key in allowed}
                if values:
                    connection.execute("UPDATE " + table + " SET " + ",".join(key + "=?" for key in values)
                                       + " WHERE task_id=?", [*values.values(), task_id])
            if clean.get("status") in {"Failed", "Cancelled"}:
                connection.execute(
                    "UPDATE execution_steps SET status=?,finished_at=?,error=? "
                    "WHERE task_id=? AND status IN ('Pending','Running')",
                    (clean["status"], clean.get("finished_at") or utcnow(),
                     clean.get("error") or "Execution " + clean["status"].lower(), task_id),
                )
            connection.execute("UPDATE agents SET last_activity=? WHERE id=?", (utcnow(), row["agent_id"]))
            return self._task(connection.execute(TASK_SELECT + "WHERE t.id=?", (task_id,)).fetchone(), task_id)

    def append_event(self, task_id: str | None, event: dict) -> dict:
        with self._connection(write=True) as connection:
            return self._append_event(connection, task_id, event)

    def _append_event(self, connection: sqlite3.Connection, task_id: str | None, event: dict) -> dict:
        if task_id is not None:
            task = connection.execute("SELECT agent_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            agent_id = task["agent_id"]
        else:
            agent_id = event.get("agent_id")
            if connection.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
                raise KeyError(agent_id)
        payload = sanitize(dict(event))
        payload.update(task_id=task_id, agent_id=agent_id,
                       timestamp=payload.get("timestamp") or utcnow(),
                       event_type=payload.get("event_type") or "log",
                       level=str(payload.get("level") or "INFO").upper())
        payload.pop("id", None)
        cursor = connection.execute(
            "INSERT INTO log_events(task_id,agent_id,timestamp,event_type,level,step_id,tool,status,error,"
            "duration_seconds,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [payload.get(key) for key in ("task_id", "agent_id", "timestamp", "event_type", "level", "step_id",
                                         "tool", "status", "error", "duration_seconds")] + [_dump(payload)],
        )
        payload["id"] = cursor.lastrowid
        if task_id and payload["event_type"] in {"step.started", "step.finished"} and payload.get("step_id"):
            self._upsert_step(connection, task_id, payload)
        connection.execute("UPDATE agents SET last_activity=? WHERE id=?", (payload["timestamp"], agent_id))
        return payload

    @staticmethod
    def _upsert_step(connection: sqlite3.Connection, task_id: str, event: dict) -> None:
        finished = event["event_type"] == "step.finished"
        connection.execute(
            "INSERT OR IGNORE INTO execution_steps(task_id,step_id,timestamp,status) VALUES(?,?,?,?)",
            (task_id, event["step_id"], event["timestamp"], "Success" if finished else "Running"),
        )
        values = {key: event[key] for key in ("step_number", "status", "tool", "reason", "error", "duration_seconds", "attempt")
                  if key in event}
        for key in ("input", "output"):
            if key in event:
                values[key + "_json"] = _dump(event[key])
        if finished:
            values["finished_at"] = event["timestamp"]
            values.setdefault("status", "Success")
        else:
            values.setdefault("status", "Running")
            # A retry reopens the same timeline step; the preceding attempt is
            # retained in log_events rather than displayed as a finished step.
            for key in ("finished_at", "duration_seconds", "output_json", "error"):
                values.setdefault(key, None)
        connection.execute("UPDATE execution_steps SET " + ",".join(key + "=?" for key in values)
                           + " WHERE task_id=? AND step_id=?", [*values.values(), task_id, event["step_id"]])

    def list_steps(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            result = []
            for row in connection.execute("SELECT * FROM execution_steps WHERE task_id=? ORDER BY step_number,timestamp", (task_id,)):
                step = dict(row)
                step.update(id=step["step_id"], input=_load(step.pop("input_json")), output=_load(step.pop("output_json")))
                result.append(step)
            return result

    def list_events(self, task_id: str | None = None, after: int = 0, limit: int = 200, **filters: Any) -> list[dict]:
        where, params = ["id > ?"], [max(0, int(after))]
        for column, value in (("task_id", task_id), ("agent_id", filters.get("agent_id")),
                              ("level", filters.get("level")), ("tool", filters.get("tool"))):
            if value:
                where.append(column + "=?")
                params.append(str(value).upper() if column == "level" else value)
        if filters.get("error_only"):
            where.append("(level IN ('ERROR','CRITICAL') OR (error IS NOT NULL AND error != ''))")
        if filters.get("date_from"):
            where.append("timestamp >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            end = str(filters["date_to"])
            if len(end) == 10:
                end = (datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat()
                where.append("timestamp < ?")
            else:
                where.append("timestamp <= ?")
            params.append(end)
        with self._connection() as connection:
            order = "DESC" if filters.get("newest") else "ASC"
            rows = connection.execute("SELECT id,payload_json FROM log_events WHERE " + " AND ".join(where)
                                      + " ORDER BY id " + order + " LIMIT ?", [*params, max(1, min(int(limit), 10000))])
            return [dict(_load(row["payload_json"]), id=row["id"]) for row in rows]

    def metrics(self, agent_id: str | None = None) -> dict:
        where, params = (" WHERE t.agent_id=?", [agent_id]) if agent_id else ("", [])
        with self._connection() as connection:
            agent_where = " WHERE deleted_at IS NULL" + (" AND id=?" if agent_id else "")
            agents = connection.execute(
                "SELECT COUNT(*) AS total_agents,COALESCE(SUM(enabled),0) AS active_agents,"
                "COALESCE(SUM(status='Running'),0) AS running_agents,COALESCE(SUM(status='Error'),0) AS error_agents "
                "FROM agents" + agent_where, params,
            ).fetchone()
            rows = connection.execute(TASK_SELECT + where, params).fetchall()
            result = dict(agents)
            terminal = [row for row in rows if row["status"] in {"Success", "Failed", "Cancelled"}]
            durations = [row["duration_seconds"] for row in terminal if row["duration_seconds"] is not None]
            result.update(total_tasks=len(rows), successful_tasks=sum(row["status"] == "Success" for row in rows),
                          failed_tasks=sum(row["status"] == "Failed" for row in rows),
                          cancelled_tasks=sum(row["status"] == "Cancelled" for row in rows),
                          running_tasks=sum(row["status"] == "Running" for row in rows),
                          avg_duration_seconds=sum(durations) / len(durations) if durations else 0,
                          total_tokens=sum(row["total_tokens"] for row in rows),
                          tool_calls=sum(row["tool_calls"] for row in rows),
                          model_calls=sum(row["model_calls"] for row in rows),
                          avg_steps=sum(row["steps"] for row in terminal) / len(terminal) if terminal else 0)
            event_filter = " AND agent_id=?" if agent_id else ""
            result["avg_model_seconds"] = connection.execute(
                "SELECT COALESCE(AVG(duration_seconds),0) FROM log_events "
                "WHERE event_type='model.finished' AND duration_seconds IS NOT NULL" + event_filter, params,
            ).fetchone()[0]
            result["tools"] = [dict(row) for row in connection.execute(
                "SELECT tool AS name,COUNT(*) AS calls,COALESCE(SUM(status='Failed' OR (error IS NOT NULL AND error!='')),0) AS errors "
                "FROM log_events WHERE event_type='step.finished' AND tool IS NOT NULL" + event_filter
                + " GROUP BY tool ORDER BY calls DESC,tool", params,
            )]
            result["errors_by_agent"] = [dict(row) for row in connection.execute(
                "SELECT a.name,COUNT(*) AS errors FROM tasks t JOIN agents a ON a.id=t.agent_id "
                "JOIN task_executions e ON e.task_id=t.id WHERE e.status='Failed'"
                + (" AND t.agent_id=?" if agent_id else "") + " GROUP BY t.agent_id ORDER BY errors DESC,a.name", params,
            )]
            today = datetime.now(timezone.utc).date()
            history = {}
            for offset in range(6, -1, -1):
                day = (today - timedelta(days=offset)).isoformat()
                history[day] = dict(date=day, tasks=0, errors=0, tokens=0, duration_seconds=0)
            for row in rows:
                item = history.get(row["created_at"][:10])
                if item is not None:
                    item["tasks"] += 1
                    item["errors"] += int(row["status"] == "Failed")
                    item["tokens"] += row["total_tokens"]
                    item["duration_seconds"] += row["duration_seconds"] or 0
            result["history"] = list(history.values())
            return result

    def recover_interrupted(self) -> int:
        """Fail abandoned executions atomically after the local runtime restarts."""
        now = utcnow()
        message = "Execution interrupted by a server restart. Assign a new task to retry."
        with self._connection(write=True) as connection:
            rows = connection.execute(TASK_SELECT + "WHERE e.status IN ('Queued','Running','Paused')").fetchall()
            for row in rows:
                duration = row["duration_seconds"] or 0
                if row["started_at"]:
                    duration = max(0, (datetime.fromisoformat(now) - datetime.fromisoformat(row["started_at"])).total_seconds())
                connection.execute(
                    "UPDATE task_executions SET status='Failed',finished_at=?,duration_seconds=?,error=? WHERE task_id=?",
                    (now, duration, message, row["id"]),
                )
                connection.execute(
                    "UPDATE execution_steps SET status='Failed',finished_at=?,error=? "
                    "WHERE task_id=? AND status IN ('Pending','Running')", (now, message, row["id"]),
                )
                connection.execute("UPDATE agents SET status=CASE WHEN enabled=1 THEN 'Error' ELSE 'Offline' END,"
                                   "updated_at=?,last_activity=? WHERE id=?", (now, now, row["agent_id"]))
                self._append_event(connection, row["id"], dict(event_type="execution.interrupted", level="ERROR",
                                   status="Failed", error=message, timestamp=now))
            return len(rows)
