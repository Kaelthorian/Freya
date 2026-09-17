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
from .capabilities import effective_tools_for_policy
from .policy import policy_from_legacy, validate_policy
from .agent_context import build_agent_context, build_effective_agent
from .skills import BUILTIN_SKILLS, normalize_skill, normalize_skill_assignments, resolve_agent_skills, skill_snapshot, skill_summary
from .security import sanitize
from .tools import argument_summary


TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused", "Success", "Failed", "Cancelled"}
AGENT_STATUSES = {"Idle", "Running", "Waiting", "Paused", "Error", "Offline"}
ACTIVE_TASK_STATUSES = ("Queued", "Running", "WaitingForApproval", "Paused")
EXECUTION_FIELDS = {
    "status", "started_at", "finished_at", "duration_seconds", "steps",
    "progress", "result", "verification", "error",
}
METRIC_FIELDS = {"model_calls", "tool_calls", "prompt_tokens", "generated_tokens", "total_tokens"}
TASK_SELECT = """
SELECT t.*, e.id AS execution_id, e.status, e.started_at, e.finished_at,
       e.duration_seconds, e.steps, e.progress, e.result_json, e.verification_json, e.error,
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
            # In-place migrations keep existing local databases usable.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agents)")}
            if "instructions" not in columns:
                connection.execute("ALTER TABLE agents ADD COLUMN instructions TEXT NOT NULL DEFAULT ''")
            execution_columns = {row[1] for row in connection.execute("PRAGMA table_info(task_executions)")}
            if "verification_json" not in execution_columns:
                connection.execute("ALTER TABLE task_executions ADD COLUMN verification_json TEXT")
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, definition in (("agent_role", "TEXT NOT NULL DEFAULT ''"), ("agent_description", "TEXT NOT NULL DEFAULT ''"),
                                     ("agent_instructions", "TEXT NOT NULL DEFAULT ''"), ("skills_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            skill_columns = {row[1] for row in connection.execute("PRAGMA table_info(skills)")}
            for name, definition in (
                ("category", "TEXT NOT NULL DEFAULT 'General'"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
                ("procedures_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("recommended_capabilities_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("required_capabilities_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("source", "TEXT NOT NULL DEFAULT 'user'"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in skill_columns:
                    connection.execute(f"ALTER TABLE skills ADD COLUMN {name} {definition}")
            agent_skill_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_skills)")}
            if "priority" not in agent_skill_columns:
                connection.execute("ALTER TABLE agent_skills ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            connection.executemany(
                "INSERT INTO tools(name, description, available, dangerous) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "available=excluded.available, dangerous=excluded.dangerous",
                [(tool["name"], tool["description"], int(tool.get("available", True)),
                  int(tool.get("dangerous", False))) for tool in TOOL_CATALOG],
            )
            self._seed_builtin_skills(connection)

    @staticmethod
    def _seed_builtin_skills(connection: sqlite3.Connection) -> None:
        """Keep a small editable starter catalogue in new and old databases."""
        now = utcnow()
        for raw in BUILTIN_SKILLS:
            skill = normalize_skill(raw)
            connection.execute(
                "INSERT OR IGNORE INTO skills(id,name,description,category,version,instructions,procedures_json,"
                "recommended_capabilities_json,required_capabilities_json,tags_json,source,metadata_json,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (skill["id"], skill["name"], skill["description"], skill["category"], skill["version"],
                 _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]),
                 _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]),
                 int(skill["enabled"]), now, now),
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
        stored_tools = [item[0] for item in connection.execute(
            "SELECT tool_name FROM agent_tools WHERE agent_id=? ORDER BY tool_name", (agent_id,),
        )]
        policy = agent["config"].get("capability_policy") or policy_from_legacy(agent["config"], stored_tools)
        policy = validate_policy(policy)
        # The policy is the authority. Legacy agent_tools is only migration
        # input; explicit allow/ask rules determine the runtime tool surface.
        agent["capability_policy"] = policy
        agent["config"]["capability_policy"] = policy
        agent["tools"] = effective_tools_for_policy(policy)
        assigned = []
        for row_skill in connection.execute(
            "SELECT s.id,s.name,s.description,s.category,s.version,s.instructions,s.procedures_json,"
            "s.recommended_capabilities_json,s.required_capabilities_json,s.tags_json,s.source,s.metadata_json,"
            "s.enabled,a.priority FROM skills s JOIN agent_skills a ON a.skill_id=s.id "
            "WHERE a.agent_id=? ORDER BY a.priority DESC,s.id", (agent_id,),
        ):
            skill = dict(row_skill)
            for field in ("instructions", "procedures", "recommended_capabilities", "required_capabilities", "tags", "metadata"):
                encoded = skill.pop(field if field == "instructions" else field + "_json", None)
                if field == "instructions":
                    skill[field] = _load(encoded) if isinstance(encoded, str) and encoded.startswith(("[", "\"")) else (encoded or "")
                else:
                    skill[field] = _load(encoded) or []
            skill["enabled"] = bool(skill.get("enabled"))
            assigned.append(skill)
        agent["skills"] = resolve_agent_skills(agent, assigned, policy=agent["capability_policy"])
        try:
            preview_effective = build_effective_agent(agent)
            agent["context_preview"] = build_agent_context(preview_effective, "[task preview]", agent["config"].get("workspace_path", ""))
        except (TypeError, ValueError):
            agent["context_preview"] = "Context preview unavailable because the configuration is invalid."
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
            "AND e.status IN ('Queued','Running','WaitingForApproval','Paused') "
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
                "INSERT INTO agents(id,name,description,role,instructions,enabled,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (agent_id, clean["name"], clean.get("description", ""), clean.get("role", ""), clean.get("instructions", ""),
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
        connection.execute("DELETE FROM agent_skills WHERE agent_id=?", (agent_id,))
        assignments = normalize_skill_assignments(data.get("skills", []))
        for assignment in assignments:
            sid = assignment["skill_id"]
            if connection.execute("SELECT 1 FROM skills WHERE id=?", (sid,)).fetchone() is None:
                raise ValueError("Unknown skill: " + sid)
            connection.execute("INSERT INTO agent_skills(agent_id,skill_id,priority) VALUES(?,?,?)",
                               (agent_id, sid, assignment["priority"]))

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
                "UPDATE agents SET name=?,description=?,role=?,instructions=?,enabled=?,status=?,updated_at=? WHERE id=?",
                (clean["name"], clean.get("description", ""), clean.get("role", ""), clean.get("instructions", ""),
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
        for field in ("config", "tools", "result", "verification", "skills"):
            task[field] = _load(task.pop(field + "_json"))
        task["capability_policy"] = task["config"].get("capability_policy")
        return task

    def create_task(self, agent_id: str, prompt: str, workspace: str) -> dict:
        task_id, execution_id, now = str(uuid4()), str(uuid4()), utcnow()
        with self._connection(write=True) as connection:
            agent = self._agent(connection, agent_id)
            effective = build_effective_agent(agent)
            effective["skills"] = resolve_agent_skills(agent, agent.get("skills", []), prompt,
                                                        policy=effective["capability_policy"])
            snapshot_config = dict(agent["config"])
            snapshot_config.update({key: effective[key] for key in ("identity", "behavior", "autonomy", "verification", "output", "capability_policy")})
            skill_snapshots = [skill_snapshot(skill) for skill in effective["skills"]]
            connection.execute(
                "INSERT INTO tasks(id,agent_id,agent_name,prompt,workspace,config_json,tools_json,agent_role,agent_description,agent_instructions,skills_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, agent_id, agent["name"], sanitize(prompt),
                sanitize(workspace), _dump(snapshot_config), _dump(agent["tools"]), agent.get("role", ""), agent.get("description", ""), agent.get("instructions", ""), _dump(skill_snapshots), now),
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

    def create_orchestration(self, prompt: str, config: dict | None = None) -> dict:
        oid, now = str(uuid4()), utcnow()
        with self._connection(write=True) as c:
            c.execute("INSERT INTO orchestration_runs(id,prompt,config_json,created_at,updated_at) VALUES(?,?,?,?,?)", (oid, sanitize(prompt), _dump(config or {}), now, now))
        return self.get_orchestration(oid)

    def get_orchestration(self, oid: str) -> dict:
        with self._connection() as c:
            row = c.execute("SELECT * FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if row is None: raise KeyError(oid)
            result = dict(row); result["config"] = _load(result.pop("config_json")) or {}
            result["delegations"] = [dict(x) for x in c.execute("SELECT * FROM orchestration_delegations WHERE orchestration_id=? ORDER BY created_at", (oid,))]
            for d in result["delegations"]: d["result"] = _load(d.pop("result_json"))
            result["events"] = [dict(x) for x in c.execute("SELECT * FROM orchestration_events WHERE orchestration_id=? ORDER BY id", (oid,))]
            return result

    def list_orchestrations(self, limit=100):
        with self._connection() as c:
            bounded = max(1, min(int(limit), 1000))
            ids = [r[0] for r in c.execute("SELECT id FROM orchestration_runs ORDER BY created_at DESC LIMIT ?", (bounded,))]
        return [self.get_orchestration(i) for i in ids]

    @staticmethod
    def _skill(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise KeyError("skill")
        item = dict(row)
        for field in ("procedures", "recommended_capabilities", "required_capabilities", "tags", "metadata"):
            item[field] = _load(item.pop(field + "_json", None)) or ([] if field != "metadata" else {})
        raw_instructions = item.get("instructions", "")
        if isinstance(raw_instructions, str) and raw_instructions.startswith(("[", "\"")):
            try:
                item["instructions"] = _load(raw_instructions)
            except (TypeError, ValueError):
                pass
        if isinstance(item.get("instructions"), str):
            item["instructions"] = [line.strip() for line in item["instructions"].splitlines() if line.strip()]
        item["required_tools"] = _load(item.pop("required_tools_json", "[]")) or []
        item["enabled"] = bool(item.get("enabled"))
        item["assigned_agents"] = int(item.pop("assigned_agents", 0) or 0)
        assigned_ids = item.pop("assigned_agent_ids", "") or ""
        item["assigned_agent_ids"] = [value for value in str(assigned_ids).split(",") if value]
        return item

    def list_skills(self, *, query: str = "", category: str = "", enabled: bool | None = None,
                    source: str = "") -> list[dict[str, Any]]:
        where, params = [], []
        if query:
            where.append("(s.name LIKE ? OR s.id LIKE ? OR s.description LIKE ? OR s.category LIKE ? OR s.tags_json LIKE ?)")
            pattern = "%" + str(query).strip() + "%"
            params.extend([pattern] * 5)
        if category:
            where.append("s.category=?")
            params.append(category)
        if enabled is not None:
            where.append("s.enabled=?")
            params.append(int(enabled))
        if source:
            where.append("s.source=?")
            params.append(source)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connection() as c:
            rows = c.execute(
                "SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s "
                "LEFT JOIN agent_skills a ON a.skill_id=s.id" + clause + " GROUP BY s.id ORDER BY s.name,s.id", params,
            ).fetchall()
            return [self._skill(row) for row in rows]

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill_id,)).fetchone()
            if row is None:
                raise KeyError(skill_id)
            return self._skill(row)

    def create_skill(self, data: dict[str, Any]) -> dict[str, Any]:
        skill = normalize_skill(data)
        now = utcnow()
        with self._connection(write=True) as c:
            if c.execute("SELECT 1 FROM skills WHERE id=?", (skill["id"],)).fetchone() is not None:
                raise ValueError("Skill id already exists: " + skill["id"])
            if c.execute("SELECT 1 FROM skills WHERE name=?", (skill["name"],)).fetchone() is not None:
                raise ValueError("Skill name already exists: " + skill["name"])
            c.execute(
                "INSERT INTO skills(id,name,description,category,version,instructions,procedures_json,recommended_capabilities_json,required_capabilities_json,tags_json,source,metadata_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (skill["id"], skill["name"], skill["description"], skill["category"], skill["version"], _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]), _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]), int(skill["enabled"]), now, now),
            )
            created = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill["id"],)).fetchone()
            return self._skill(created)

    def update_skill(self, skill_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._connection(write=True) as c:
            row = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if row is None:
                raise KeyError(skill_id)
            current = self._skill(row)
            current.pop("assigned_agents", None)
            current.pop("assigned_agent_ids", None)
            current.pop("required_tools", None)
            incoming = dict(data)
            if "id" in incoming and incoming["id"] != skill_id:
                raise ValueError("Skill id is stable and cannot be changed")
            incoming["id"] = skill_id
            skill = normalize_skill(incoming, current)
            if c.execute("SELECT 1 FROM skills WHERE name=? AND id<>?", (skill["name"], skill_id)).fetchone() is not None:
                raise ValueError("Skill name already exists: " + skill["name"])
            now = utcnow()
            c.execute(
                "UPDATE skills SET name=?,description=?,category=?,version=?,instructions=?,procedures_json=?,recommended_capabilities_json=?,required_capabilities_json=?,tags_json=?,source=?,metadata_json=?,enabled=?,updated_at=? WHERE id=?",
                (skill["name"], skill["description"], skill["category"], skill["version"], _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]), _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]), int(skill["enabled"]), now, skill_id),
            )
            updated = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill_id,)).fetchone()
            return self._skill(updated)

    def delete_skill(self, skill_id: str) -> dict[str, Any]:
        with self._connection(write=True) as c:
            if c.execute("SELECT 1 FROM skills WHERE id=?", (skill_id,)).fetchone() is None:
                raise KeyError(skill_id)
            assigned = c.execute("SELECT COUNT(*) FROM agent_skills WHERE skill_id=?", (skill_id,)).fetchone()[0]
            used = False
            for row in c.execute("SELECT skills_json FROM tasks"):
                try:
                    used = any((item.get("id", item.get("skill_id")) if isinstance(item, dict) else item) == skill_id for item in (_load(row[0]) or []))
                except (TypeError, ValueError):
                    continue
                if used:
                    break
            if assigned or used:
                c.execute("UPDATE skills SET enabled=0,updated_at=? WHERE id=?", (utcnow(), skill_id))
                disabled = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill_id,)).fetchone()
                return self._skill(disabled)
            c.execute("DELETE FROM skills WHERE id=?", (skill_id,))
            return {"id": skill_id, "deleted": True}

    def skill_compatibility(self, agent_id: str) -> list[dict[str, Any]]:
        return [skill_summary(skill) for skill in self.get_agent(agent_id).get("skills", [])]

    def update_orchestration(self, oid, **fields):
        allowed={"status","response","error"}; unknown=set(fields)-allowed
        if unknown: raise ValueError("Unknown orchestration fields: " + ", ".join(sorted(unknown)))
        with self._connection(write=True) as c:
            values={**fields,"updated_at":utcnow()}; c.execute("UPDATE orchestration_runs SET "+",".join(k+"=?" for k in values)+" WHERE id=?", [*values.values(),oid])
        return self.get_orchestration(oid)

    def add_orchestration_event(self, oid, event):
        with self._connection(write=True) as c:
            c.execute("INSERT INTO orchestration_events(orchestration_id,timestamp,event_type,status,agent_id,task_id,message,payload_json) VALUES(?,?,?,?,?,?,?,?)", (oid,utcnow(),event.get("event_type","update"),event.get("status"),event.get("agent_id"),event.get("task_id"),event.get("message",event.get("reason","")),_dump(event)))

    def add_delegation(self, oid, agent_id, objective, task_id=None):
        did=str(uuid4()); now=utcnow()
        with self._connection(write=True) as c:
            c.execute("INSERT INTO orchestration_delegations(id,orchestration_id,agent_id,task_id,objective,created_at) VALUES(?,?,?,?,?,?)",(did,oid,agent_id,task_id,sanitize(objective),now))
        return did

    def update_delegation(self, did, **fields):
        allowed={"task_id","status","result_json","finished_at"}; fields={k:v for k,v in fields.items() if k in allowed}
        with self._connection(write=True) as c:
            c.execute("UPDATE orchestration_delegations SET "+",".join(k+"=?" for k in fields)+" WHERE id=?", [*fields.values(),did])

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
                values = {({"result": "result_json", "verification": "verification_json"}.get(key, key)):
                          (_dump(value) if key in {"result", "verification"} else value)
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


    @staticmethod
    def _approval(row: sqlite3.Row | None, approval_id: str = "") -> dict:
        if row is None:
            raise KeyError(approval_id)
        item = dict(row)
        item["arguments"] = _load(item.pop("arguments_json", "{}")) or {}
        return item

    def create_approval(self, task_id: str, agent_id: str, capability: str, tool: str,
                        arguments: dict[str, Any] | None = None, action_summary: str = "",
                        resource: str = "", reason: str = "", approval_id: str | None = None) -> dict:
        approval_id = approval_id or str(uuid4())
        now = utcnow()
        safe_arguments = argument_summary(arguments if isinstance(arguments, dict) else {})
        with self._connection(write=True) as connection:
            task = connection.execute("SELECT agent_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["agent_id"] != agent_id:
                raise ValueError("Approval agent does not own the task.")
            connection.execute(
                "INSERT INTO approval_requests(id,task_id,agent_id,capability,tool,arguments_json,action_summary,resource,reason,created_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (approval_id, task_id, agent_id, str(capability), str(tool),
                 _dump(safe_arguments), str(action_summary)[:1000], str(resource)[:1000],
                 str(reason)[:2000], now, "pending"),
            )
            return self._approval(connection.execute(
                "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone(), approval_id)

    def get_approval(self, approval_id: str) -> dict:
        with self._connection() as connection:
            return self._approval(connection.execute(
                "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone(), approval_id)

    def list_approvals(self, *, status: str | None = None, task_id: str | None = None,
                       limit: int = 200) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(str(status))
        if task_id:
            where.append("task_id=?")
            params.append(str(task_id))
        query = "SELECT * FROM approval_requests"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connection() as connection:
            return [self._approval(row) for row in connection.execute(query, params)]

    def resolve_approval(self, approval_id: str, status: str, resolution: str | None = None) -> dict:
        if status not in {"approved_once", "approved_task", "denied"}:
            raise ValueError("Unknown approval status")
        now = utcnow()
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] != "pending":
                raise ValueError("Approval request is already resolved.")
            task = connection.execute(
                "SELECT e.status FROM task_executions e WHERE e.task_id=?", (row["task_id"],)
            ).fetchone()
            if task is None:
                raise KeyError(row["task_id"])
            if task["status"] in {"Success", "Failed", "Cancelled"}:
                raise ValueError("Cannot resolve approval for a terminal task.")
            connection.execute(
                "UPDATE approval_requests SET status=?,resolution=?,resolved_at=? WHERE id=? AND status='pending'",
                (status, str(resolution or status)[:1000], now, approval_id),
            )
            return self._approval(connection.execute(
                "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone(), approval_id)

    def cancel_pending_approvals(self, task_id: str, reason: str = "cancelled") -> int:
        now = utcnow()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE approval_requests SET status='denied',resolution=?,resolved_at=? "
                "WHERE task_id=? AND status='pending'",
                (str(reason)[:1000], now, task_id),
            )
            return cursor.rowcount

    def recover_interrupted(self) -> int:
        """Fail abandoned executions atomically after the local runtime restarts."""
        now = utcnow()
        message = "Execution interrupted by a server restart. Assign a new task to retry."
        with self._connection(write=True) as connection:
            rows = connection.execute(TASK_SELECT + "WHERE e.status IN ('Queued','Running','WaitingForApproval','Paused')").fetchall()
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
                connection.execute(
                    "UPDATE approval_requests SET status='denied',resolution=?,resolved_at=? "
                    "WHERE task_id=? AND status='pending'", ("cancelled: " + message, now, row["id"]),
                )
                connection.execute("UPDATE agents SET status=CASE WHEN enabled=1 THEN 'Error' ELSE 'Offline' END,"
                                   "updated_at=?,last_activity=? WHERE id=?", (now, now, row["agent_id"]))
                self._append_event(connection, row["id"], dict(event_type="execution.interrupted", level="ERROR",
                                   status="Failed", error=message, timestamp=now))
            return len(rows)
