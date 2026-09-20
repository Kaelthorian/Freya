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
from .planner import MAX_PLAN_TASKS, PLAN_SCHEMA_VERSION, validate_plan
from .recovery import allowed_replan_scope, validate_replan_revision
from .execution_graph import NODE_STATES, TERMINAL_NODE_STATES, graph_summary
from .integration_storage import IntegrationStoreMixin, migrate_integration_schema
from .agent_context import build_agent_context, build_effective_agent
from .skills import BUILTIN_SKILLS, MAX_SKILLS_IMPORT, normalize_skill, normalize_skill_assignments, resolve_agent_skills, skill_snapshot, skill_summary
from .security import sanitize
from .tools import argument_summary


TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused", "Success", "Failed", "Cancelled"}
AGENT_STATUSES = {"Idle", "Running", "Waiting", "Paused", "Error", "Offline"}
ACTIVE_TASK_STATUSES = ("Queued", "Running", "WaitingForApproval", "Paused")
ORCHESTRATION_ACTIVE_STATUSES = ("Queued", "Planning", "Planned", "Running", "Integrating")
ORCHESTRATION_TERMINAL_STATUSES = ("Success", "Failed", "Cancelled")
ORCHESTRATION_STATUSES = set(ORCHESTRATION_ACTIVE_STATUSES + ORCHESTRATION_TERMINAL_STATUSES)
EXECUTION_FIELDS = {
    "status", "started_at", "finished_at", "duration_seconds", "steps",
    "progress", "result", "verification", "error",
}
METRIC_FIELDS = {"model_calls", "tool_calls", "prompt_tokens", "generated_tokens", "total_tokens"}
SKILL_DEFINITION_FIELDS = (
    "id", "name", "description", "category", "version", "instructions", "procedures",
    "recommended_capabilities", "required_capabilities", "tags", "source", "metadata", "enabled",
)
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


class Store(IntegrationStoreMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            migrate_integration_schema(connection)
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
            orchestration_columns = {row[1] for row in connection.execute("PRAGMA table_info(orchestration_runs)")}
            for name, definition in (
                ("plan_json", "TEXT"),
                ("plan_schema_version", "INTEGER"),
                ("plan_created_at", "TEXT"),
                ("planning_metrics_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("effective_plan_json", "TEXT"),
                ("current_plan_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in orchestration_columns:
                    connection.execute(f"ALTER TABLE orchestration_runs ADD COLUMN {name} {definition}")
            node_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(orchestration_task_nodes)"
            )}
            for name, definition in (("evaluation_id", "TEXT"), ("evaluation_status", "TEXT")):
                if name not in node_columns:
                    connection.execute(
                        f"ALTER TABLE orchestration_task_nodes ADD COLUMN {name} {definition}"
                    )
            for name, definition in (("recovery_action_id", "TEXT"),
                                     ("attempt_prompt", "TEXT NOT NULL DEFAULT ''"),
                                     ("plan_revision", "INTEGER NOT NULL DEFAULT 0")):
                if name not in node_columns:
                    connection.execute(
                        f"ALTER TABLE orchestration_task_nodes ADD COLUMN {name} {definition}"
                    )
            selection_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(orchestration_selections)"
            )}
            if "attempt" not in selection_columns:
                connection.execute("ALTER TABLE orchestration_selections ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
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
            if "deleted_at" not in skill_columns:
                connection.execute("ALTER TABLE skills ADD COLUMN deleted_at TEXT")
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
            for row in connection.execute("SELECT * FROM skills"):
                legacy = self._skill(row)
                snapshot = {key: legacy[key] for key in SKILL_DEFINITION_FIELDS}
                connection.execute(
                    "INSERT OR IGNORE INTO skill_versions(skill_id,version,snapshot_json,created_at,reason) VALUES(?,?,?,?,?)",
                    (legacy["id"], legacy["version"], _dump(snapshot), legacy.get("updated_at") or utcnow(), "Migrated current definition"),
                )

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
            connection.execute("INSERT OR IGNORE INTO skill_versions(skill_id,version,snapshot_json,created_at,reason) VALUES(?,?,?,?,?)", (skill["id"], skill["version"], _dump(skill), now, "Initial builtin version"))

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
            if connection.execute("SELECT 1 FROM skills WHERE id=? AND deleted_at IS NULL AND enabled=1", (sid,)).fetchone() is None:
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

    def agent_name_exists(self, name: str, *, exclude_id: str | None = None) -> bool:
        """Return whether an active agent already uses this display name."""
        normalized = str(name or "").strip().casefold()
        if not normalized:
            return False
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id,name FROM agents WHERE deleted_at IS NULL"
            ).fetchall()
        return any(row["id"] != exclude_id and str(row["name"]).strip().casefold() == normalized
                   for row in rows)

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
            result["plan"] = _load(result.pop("plan_json"))
            result["effective_plan"] = _load(result.pop("effective_plan_json")) or result["plan"]
            result["planning_metrics"] = _load(result.pop("planning_metrics_json")) or {}
            result["selections"] = [dict(x) for x in c.execute(
                "SELECT * FROM orchestration_selections WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]
            for selection in result["selections"]:
                selection["snapshot"] = _load(selection.pop("snapshot_json")) or {}
            nodes = [self._execution_node(x) for x in c.execute(
                "SELECT * FROM orchestration_task_nodes WHERE orchestration_id=? "
                "ORDER BY plan_order,plan_task_id", (oid,),
            )]
            result["graph_summary"] = graph_summary(nodes) if nodes else None
            result["evaluations"] = [self._evaluation(x) for x in c.execute(
                "SELECT * FROM orchestration_evaluations WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]
            result["attempts"] = [dict(x) for x in c.execute(
                "SELECT * FROM orchestration_execution_attempts WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]
            result["recoveries"] = [self._recovery(x) for x in c.execute(
                "SELECT * FROM orchestration_recovery_actions WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]
            result["plan_revisions"] = [self._plan_revision(x) for x in c.execute(
                "SELECT * FROM orchestration_plan_revisions WHERE orchestration_id=? "
                "ORDER BY revision", (oid,),
            )]
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
                    source: str = "", include_deleted: bool = False) -> list[dict[str, Any]]:
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
        if not include_deleted:
            where.append("s.deleted_at IS NULL")
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
            if c.execute("SELECT 1 FROM skills WHERE lower(name)=lower(?)", (skill["name"],)).fetchone() is not None:
                raise ValueError("Skill name already exists: " + skill["name"])
            c.execute(
                "INSERT INTO skills(id,name,description,category,version,instructions,procedures_json,recommended_capabilities_json,required_capabilities_json,tags_json,source,metadata_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (skill["id"], skill["name"], skill["description"], skill["category"], skill["version"], _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]), _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]), int(skill["enabled"]), now, now),
            )
            c.execute("INSERT INTO skill_versions(skill_id,version,snapshot_json,created_at,reason) VALUES(?,?,?,?,?)", (skill["id"], skill["version"], _dump(skill), now, "Initial version"))
            c.execute("INSERT INTO skill_events(skill_id,version,timestamp,event_type,summary,payload_json) VALUES(?,?,?,?,?,?)", (skill["id"],skill["version"],now,"skill.created","Skill created",_dump({"id":skill["id"],"version":skill["version"]})))
            created = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill["id"],)).fetchone()
            return self._skill(created)

    def import_skills(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        """Validate and atomically create a batch of independent Skills."""
        if not isinstance(data, list):
            raise ValueError("skills must be a JSON array")
        if not data:
            raise ValueError("skills must contain at least one Skill")
        if len(data) > MAX_SKILLS_IMPORT:
            raise ValueError(f"skills cannot contain more than {MAX_SKILLS_IMPORT} items")
        normalized = [normalize_skill(item) for item in data]
        ids = [skill["id"] for skill in normalized]
        names = [skill["name"].casefold() for skill in normalized]
        if len(set(ids)) != len(ids):
            raise ValueError("The import contains duplicate Skill ids.")
        if len(set(names)) != len(names):
            raise ValueError("The import contains duplicate Skill names.")
        skipped: list[str] = []
        created_ids: list[str] = []
        with self._connection(write=True) as c:
            existing_rows = c.execute("SELECT * FROM skills").fetchall()
            existing = {}
            for row in existing_rows:
                definition = self._skill(row)
                existing[definition["id"]] = definition
            existing_names = {str(skill["name"]).casefold(): skill["id"] for skill in existing.values()}
            for skill in normalized:
                current = existing.get(skill["id"])
                if current is not None:
                    if all(current[key] == skill[key] for key in SKILL_DEFINITION_FIELDS):
                        skipped.append(skill["id"])
                        continue
                    raise ValueError("Skill id already exists with a different definition: " + skill["id"])
                name_owner = existing_names.get(skill["name"].casefold())
                if name_owner is not None:
                    raise ValueError("Skill name already exists: " + skill["name"])
                existing_names[skill["name"].casefold()] = skill["id"]
                created_ids.append(skill["id"])
            now = utcnow()
            try:
                for skill in normalized:
                    if skill["id"] in skipped:
                        continue
                    c.execute(
                        "INSERT INTO skills(id,name,description,category,version,instructions,procedures_json,recommended_capabilities_json,required_capabilities_json,tags_json,source,metadata_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (skill["id"], skill["name"], skill["description"], skill["category"], skill["version"], _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]), _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]), int(skill["enabled"]), now, now),
                    )
                    c.execute(
                        "INSERT INTO skill_versions(skill_id,version,snapshot_json,created_at,reason) VALUES(?,?,?,?,?)",
                        (skill["id"], skill["version"], _dump(skill), now, "Imported definition"),
                    )
                    c.execute(
                        "INSERT INTO skill_events(skill_id,version,timestamp,event_type,summary,payload_json) VALUES(?,?,?,?,?,?)",
                        (skill["id"], skill["version"], now, "skill.imported", "Skill imported", _dump({"id": skill["id"], "version": skill["version"]})),
                    )
            except sqlite3.IntegrityError as exc:
                raise ValueError("The import contains a Skill that already exists.") from exc
        return {"created": [self.get_skill(skill_id) for skill_id in created_ids], "skipped": skipped}
    def update_skill(self, skill_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._connection(write=True) as c:
            row = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if row is None:
                raise KeyError(skill_id)
            current_row = self._skill(row)
            current = {key: current_row[key] for key in SKILL_DEFINITION_FIELDS}
            incoming = dict(data)
            if "id" in incoming and incoming["id"] != skill_id:
                raise ValueError("Skill id is stable and cannot be changed")
            incoming.pop("version", None)
            incoming["id"] = skill_id
            candidate = normalize_skill(incoming, current)
            comparable = tuple(key for key in SKILL_DEFINITION_FIELDS if key != "version")
            if all(candidate[key] == current[key] for key in comparable):
                return self._skill(c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill_id,)).fetchone())
            skill = {**candidate, "version": current["version"] + 1}
            if c.execute("SELECT 1 FROM skills WHERE lower(name)=lower(?) AND id<>?", (skill["name"], skill_id)).fetchone() is not None:
                raise ValueError("Skill name already exists: " + skill["name"])
            now = utcnow()
            historical = c.execute("SELECT snapshot_json FROM skill_versions WHERE skill_id=? AND version=?", (skill_id, current["version"])).fetchone()
            if historical is None:
                raise RuntimeError("Current skill version is missing from immutable history")
            if _load(historical["snapshot_json"]) != current:
                raise RuntimeError("Immutable skill history conflicts with the current definition")
            if c.execute("SELECT 1 FROM skill_versions WHERE skill_id=? AND version=?", (skill_id, skill["version"])).fetchone():
                raise RuntimeError("Next skill version already exists")
            c.execute(
                "UPDATE skills SET name=?,description=?,category=?,version=?,instructions=?,procedures_json=?,recommended_capabilities_json=?,required_capabilities_json=?,tags_json=?,source=?,metadata_json=?,enabled=?,updated_at=? WHERE id=?",
                (skill["name"], skill["description"], skill["category"], skill["version"], _dump(skill["instructions"]), _dump(skill["procedures"]), _dump(skill["recommended_capabilities"]), _dump(skill["required_capabilities"]), _dump(skill["tags"]), skill["source"], _dump(skill["metadata"]), int(skill["enabled"]), now, skill_id),
            )
            c.execute("INSERT INTO skill_versions(skill_id,version,snapshot_json,created_at,reason) VALUES(?,?,?,?,?)", (skill_id, skill["version"], _dump(skill), now, "Updated definition"))
            c.execute("INSERT INTO skill_events(skill_id,version,timestamp,event_type,summary,payload_json) VALUES(?,?,?,?,?,?)", (skill_id,skill["version"],now,"skill.updated","Skill updated",_dump({"id":skill_id,"version":skill["version"]})))
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
            now = utcnow()
            c.execute("UPDATE skills SET enabled=0,deleted_at=?,updated_at=? WHERE id=?", (now,now,skill_id))
            c.execute("INSERT INTO skill_events(skill_id,version,timestamp,event_type,summary,payload_json) SELECT id,version,?,?,?,? FROM skills WHERE id=?", (now,"skill.archived","Skill archived",_dump({"id":skill_id}),skill_id))
            if assigned or used:
                disabled = c.execute("SELECT s.*,COUNT(a.agent_id) AS assigned_agents,GROUP_CONCAT(a.agent_id) AS assigned_agent_ids FROM skills s LEFT JOIN agent_skills a ON a.skill_id=s.id WHERE s.id=? GROUP BY s.id", (skill_id,)).fetchone()
                return self._skill(disabled)
            return {"id": skill_id, "deleted": True, "archived": True}

    def skill_versions(self, skill_id):
        with self._connection() as c:
            if c.execute("SELECT 1 FROM skills WHERE id=?", (skill_id,)).fetchone() is None: raise KeyError(skill_id)
            return [{**dict(r), "snapshot": _load(r["snapshot_json"])} for r in c.execute("SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version", (skill_id,))]

    def skill_version(self, skill_id, version):
        with self._connection() as c:
            r=c.execute("SELECT * FROM skill_versions WHERE skill_id=? AND version=?",(skill_id,int(version))).fetchone()
            if r is None: raise KeyError(f"{skill_id}:{version}")
            result=dict(r); result["snapshot"]=_load(result.pop("snapshot_json")); return result

    def skill_compatibility(self, agent_id: str) -> list[dict[str, Any]]:
        return [skill_summary(skill) for skill in self.get_agent(agent_id).get("skills", [])]

    def transition_orchestration(self, oid: str, expected_statuses, status: str, *,
                                 legacy_without_graph: bool = False, **fields) -> dict | None:
        """Atomically update a run only while it remains in an expected state."""
        expected = ((expected_statuses,) if isinstance(expected_statuses, str)
                    else tuple(dict.fromkeys(expected_statuses)))
        if not expected or any(item not in ORCHESTRATION_STATUSES for item in expected):
            raise ValueError("Expected orchestration statuses are invalid.")
        if status not in ORCHESTRATION_STATUSES:
            raise ValueError("Unknown orchestration status.")
        terminal_expected = [item for item in expected if item in ORCHESTRATION_TERMINAL_STATUSES]
        if terminal_expected and (len(expected) != 1 or terminal_expected[0] != status):
            raise ValueError("A terminal orchestration state is final.")
        allowed = {"response", "error", "planning_metrics"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Unknown orchestration fields: " + ", ".join(sorted(unknown)))
        values = {"status": status}
        for key, value in fields.items():
            values["planning_metrics_json" if key == "planning_metrics" else key] = (
                _dump(value) if key == "planning_metrics" else sanitize(value)
            )
        values["updated_at"] = utcnow()
        placeholders = ",".join(key + "=?" for key in values)
        expected_placeholders = ",".join("?" for _ in expected)
        with self._connection(write=True) as c:
            if status == "Success":
                run = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
                graph_exists = c.execute(
                    "SELECT 1 FROM orchestration_task_nodes WHERE orchestration_id=? LIMIT 1", (oid,),
                ).fetchone()
                if run is not None and run["status"] != "Success" and (graph_exists or not legacy_without_graph):
                    raise ValueError(
                        "Graph orchestration Success requires finalize_accepted_integration."
                    )
            cursor = c.execute(
                f"UPDATE orchestration_runs SET {placeholders} WHERE id=? AND status IN ({expected_placeholders})",
                [*values.values(), oid, *expected],
            )
            if cursor.rowcount != 1:
                if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                    raise KeyError(oid)
                return None
        return self.get_orchestration(oid)

    def update_orchestration(self, oid, **fields):
        """Compatibility update that still prevents terminal-state resurrection."""
        current = self.get_orchestration(oid)
        status = fields.pop("status", current["status"])
        updated = self.transition_orchestration(oid, (current["status"],), status, **fields)
        return updated or self.get_orchestration(oid)

    def save_orchestration_plan(self, oid: str, plan: dict[str, Any], schema_version: int,
                                planning_metrics: dict[str, Any] | None = None) -> dict | None:
        """Atomically persist the immutable snapshot and transition Planning to Planned."""
        if schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(f"Unsupported plan schema version: {schema_version}.")
        normalized = validate_plan(plan)
        now = utcnow()
        with self._connection(write=True) as c:
            cursor = c.execute(
                "UPDATE orchestration_runs SET plan_json=?,effective_plan_json=?,plan_schema_version=?,plan_created_at=?,"
                "planning_metrics_json=?,status='Planned',updated_at=? "
                "WHERE id=? AND status='Planning' AND plan_json IS NULL",
                (_dump(normalized), _dump(normalized), schema_version, now,
                 _dump(planning_metrics or {}), now, oid),
            )
            if cursor.rowcount != 1:
                row = c.execute("SELECT status,plan_json FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
                if row is None:
                    raise KeyError(oid)
                if row["status"] != "Planning":
                    return None
                raise ValueError("The orchestration plan snapshot already exists.")
        return self.get_orchestration(oid)

    @staticmethod
    def _execution_node(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        node = dict(row)
        node["depends_on"] = _load(node.pop("depends_on_json")) or []
        node["result"] = _load(node.pop("result_json"))
        return node

    @staticmethod
    def _evaluation(row: sqlite3.Row | dict[str, Any], *, include_snapshot: bool = False) -> dict[str, Any]:
        item = dict(row)
        evaluation = _load(item.pop("evaluation_json")) or {}
        item["metrics"] = _load(item.pop("metrics_json")) or {}
        snapshot = _load(item.pop("snapshot_json")) or {}
        item["context_truncated"] = bool(item["context_truncated"])
        item["deterministic"] = bool(item["deterministic"])
        for field in ("confidence", "criteria", "issues", "missing_evidence",
                      "recommended_action"):
            item[field] = evaluation.get(field)
        if include_snapshot:
            item["snapshot"] = snapshot
        return item

    @staticmethod
    def _recovery(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["exclude_agent_ids"] = _load(item.pop("exclude_agent_ids_json")) or []
        item["affected_task_ids"] = _load(item.pop("affected_task_ids_json")) or []
        item["metrics"] = _load(item.pop("metrics_json")) or {}
        item["snapshot"] = _load(item.pop("snapshot_json")) or {}
        return item

    @staticmethod
    def _plan_revision(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["plan"] = _load(item.pop("plan_json"))
        item["superseded_task_ids"] = _load(item.pop("superseded_task_ids_json")) or []
        item["metrics"] = _load(item.pop("metrics_json")) or {}
        return item

    def list_execution_attempts(self, oid: str) -> list[dict[str, Any]]:
        with self._connection() as c:
            if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            return [dict(row) for row in c.execute(
                "SELECT * FROM orchestration_execution_attempts WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]

    def list_recoveries(self, oid: str) -> list[dict[str, Any]]:
        with self._connection() as c:
            if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            return [self._recovery(row) for row in c.execute(
                "SELECT * FROM orchestration_recovery_actions WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]

    def get_recovery(self, recovery_id: str) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute("SELECT * FROM orchestration_recovery_actions WHERE id=?",
                            (recovery_id,)).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            return self._recovery(row)

    def list_plan_revisions(self, oid: str) -> list[dict[str, Any]]:
        with self._connection() as c:
            if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            return [self._plan_revision(row) for row in c.execute(
                "SELECT * FROM orchestration_plan_revisions WHERE orchestration_id=? "
                "ORDER BY revision", (oid,),
            )]

    def list_evaluations(self, oid: str) -> list[dict[str, Any]]:
        with self._connection() as c:
            if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            return [self._evaluation(row) for row in c.execute(
                "SELECT * FROM orchestration_evaluations WHERE orchestration_id=? "
                "ORDER BY created_at,id", (oid,),
            )]

    def get_evaluation(self, evaluation_id: str, *, include_snapshot: bool = False) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute(
                "SELECT * FROM orchestration_evaluations WHERE id=?", (evaluation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(evaluation_id)
            return self._evaluation(row, include_snapshot=include_snapshot)

    def initialize_execution_graph(self, oid: str, nodes: list[dict[str, Any]]) -> dict:
        """Atomically create exactly one durable node for every planned task."""
        now = utcnow()
        with self._connection(write=True) as c:
            run = c.execute(
                "SELECT status,plan_json FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            if run["status"] != "Planned":
                raise ValueError("Execution graph can only be initialized for a planned run.")
            plan = validate_plan(_load(run["plan_json"]))
            tasks = plan["tasks"]
            by_id = {node.get("plan_task_id"): node for node in nodes}
            if len(by_id) != len(nodes) or set(by_id) != {task["id"] for task in tasks}:
                raise ValueError("Execution graph nodes must match planned tasks exactly once.")
            existing = c.execute(
                "SELECT COUNT(*) FROM orchestration_task_nodes WHERE orchestration_id=?", (oid,),
            ).fetchone()[0]
            if existing:
                raise ValueError("Execution graph already exists.")
            for index, task in enumerate(tasks):
                node = by_id[task["id"]]
                expected_state = "ready" if not task["depends_on"] else "pending"
                if (node.get("state") != expected_state
                        or list(node.get("depends_on") or []) != task["depends_on"]
                        or int(node.get("plan_order", -1)) != index):
                    raise ValueError(f"Invalid initial execution node for {task['id']}.")
                c.execute(
                    "INSERT INTO orchestration_task_nodes("
                    "orchestration_id,plan_task_id,plan_order,depends_on_json,state,"
                    "attempt_prompt,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (oid, task["id"], index, _dump(task["depends_on"]), expected_state,
                     sanitize(task["objective"]), now),
                )
        return self.get_execution_graph(oid)

    def get_execution_graph(self, oid: str) -> dict[str, Any]:
        with self._connection() as c:
            if c.execute("SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            nodes = [self._execution_node(row) for row in c.execute(
                "SELECT * FROM orchestration_task_nodes WHERE orchestration_id=? "
                "ORDER BY plan_order,plan_task_id", (oid,),
            )]
        return {"orchestration_id": oid, "nodes": nodes,
                "summary": graph_summary(nodes) if nodes else None}

    def save_execution_graph(self, oid: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist an in-memory graph without allowing terminal nodes to reopen."""
        now = utcnow()
        by_id = {node.get("plan_task_id"): node for node in nodes}
        if len(by_id) != len(nodes) or None in by_id:
            raise ValueError("Execution graph contains duplicate or invalid task ids.")
        with self._connection(write=True) as c:
            rows = c.execute(
                "SELECT plan_task_id,state,evaluation_id,evaluation_status,recovery_action_id FROM orchestration_task_nodes "
                "WHERE orchestration_id=? ORDER BY plan_order", (oid,),
            ).fetchall()
            if not rows or {row["plan_task_id"] for row in rows} != set(by_id):
                raise ValueError("Execution graph nodes do not match durable graph state.")
            for row in rows:
                node = by_id[row["plan_task_id"]]
                state = node.get("state")
                if state not in NODE_STATES:
                    raise ValueError(f"Unknown execution node state: {state}.")
                if row["state"] in TERMINAL_NODE_STATES and state != row["state"]:
                    raise ValueError("A terminal execution node state is final.")
                if (node.get("evaluation_id") != row["evaluation_id"]
                        or node.get("evaluation_status") != row["evaluation_status"]):
                    raise ValueError(
                        "Evaluation references can only change through atomic evaluation commit."
                    )
                if node.get("recovery_action_id") != row["recovery_action_id"]:
                    raise ValueError(
                        "Recovery references can only change through atomic recovery commit."
                    )
                c.execute(
                    "UPDATE orchestration_task_nodes SET state=?,selected_agent_id=?,selection_id=?,"
                    "runtime_task_id=?,delegation_id=?,evaluation_id=?,evaluation_status=?,attempt=?,"
                    "recovery_action_id=?,attempt_prompt=?,plan_revision=?,waiting_reason=?,"
                    "result_json=?,error=?,"
                    "started_at=?,finished_at=?,updated_at=? WHERE orchestration_id=? AND plan_task_id=?",
                    (state, node.get("selected_agent_id"), node.get("selection_id"),
                     node.get("runtime_task_id"), node.get("delegation_id"),
                     node.get("evaluation_id"), node.get("evaluation_status"),
                     int(node.get("attempt", 0)), node.get("recovery_action_id"),
                     sanitize(node.get("attempt_prompt") or ""),
                     int(node.get("plan_revision", 0)),
                     sanitize(node.get("waiting_reason") or ""),
                     _dump(node.get("result")) if node.get("result") is not None else None,
                     sanitize(node.get("error")) if node.get("error") is not None else None,
                     node.get("started_at"), node.get("finished_at"), now,
                     oid, row["plan_task_id"]),
                )
        return self.get_execution_graph(oid)

    def commit_evaluation(self, evaluation_id: str, oid: str, plan_task_id: str, *,
                          runtime_task_id: str, agent_id: str, attempt: int,
                          evaluator_version: int, evaluation: dict[str, Any],
                          metrics: dict[str, Any], snapshot: dict[str, Any],
                          context_truncated: bool, deterministic: bool) -> dict[str, Any] | None:
        """Atomically persist one immutable evaluation and close its evaluating node."""
        status = evaluation.get("status")
        if status not in {"accepted", "needs_revision", "rejected", "blocked", "error"}:
            raise ValueError("Unknown persisted evaluation status.")
        target = "success" if status == "accepted" else "recovery_pending"
        error = None if status == "accepted" else {
            "needs_revision": "Semantic evaluation requires revision.",
            "rejected": "Semantic evaluation rejected the task result.",
            "blocked": "Semantic evaluation could not determine task success.",
            "error": "Semantic evaluator failed.",
        }[status]
        summary = str(evaluation.get("summary") or "").strip()
        if error and summary:
            error += " " + summary
        now = utcnow()
        with self._connection(write=True) as c:
            run = c.execute(
                "SELECT status FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            node = c.execute(
                "SELECT state,runtime_task_id,selected_agent_id,attempt,evaluation_id "
                "FROM orchestration_task_nodes WHERE orchestration_id=? AND plan_task_id=?",
                (oid, plan_task_id),
            ).fetchone()
            if node is None:
                raise KeyError(plan_task_id)
            if (run["status"] != "Running" or node["state"] != "evaluating"
                    or node["evaluation_id"] is not None):
                return None
            if (node["runtime_task_id"] != runtime_task_id
                    or node["selected_agent_id"] != agent_id
                    or int(node["attempt"]) != int(attempt)):
                return None
            c.execute(
                "INSERT INTO orchestration_evaluations("
                "id,orchestration_id,plan_task_id,runtime_task_id,agent_id,attempt,"
                "evaluator_version,status,summary,evaluation_json,metrics_json,snapshot_json,"
                "context_truncated,deterministic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation_id, oid, plan_task_id, runtime_task_id, agent_id, int(attempt),
                 int(evaluator_version), status, sanitize(summary), _dump(evaluation), _dump(metrics),
                 _dump(snapshot), int(bool(context_truncated)), int(bool(deterministic)), now),
            )
            cursor = c.execute(
                "UPDATE orchestration_task_nodes SET state=?,evaluation_id=?,evaluation_status=?,"
                "recovery_action_id=NULL,"
                "waiting_reason='',error=?,finished_at=?,updated_at=? "
                "WHERE orchestration_id=? AND plan_task_id=? AND state='evaluating' "
                "AND evaluation_id IS NULL",
                (target, evaluation_id, status, sanitize(error) if error else None,
                 now if target == "success" else None, now, oid, plan_task_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Evaluation node transition lost its atomic precondition.")
            c.execute(
                "UPDATE orchestration_execution_attempts SET evaluation_id=?,status=?,"
                "finished_at=? WHERE orchestration_id=? AND plan_task_id=? AND attempt=? "
                "AND runtime_task_id=?",
                (evaluation_id, target, now if target == "success" else None,
                 oid, plan_task_id, int(attempt), runtime_task_id),
            )
        return self.get_evaluation(evaluation_id)

    def record_execution_attempt(self, oid: str, plan_task_id: str, *,
                                 selected_agent_id: str, selection_id: str,
                                 runtime_task_id: str, delegation_id: str,
                                 attempt: int, prompt: str,
                                 recovery_action_id: str | None = None) -> dict[str, Any] | None:
        """Persist one immutable dispatch snapshot after its graph transition."""
        attempt_id, now = str(uuid4()), utcnow()
        with self._connection(write=True) as c:
            run = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if run is None:
                raise KeyError(oid)
            node = c.execute(
                "SELECT state,selected_agent_id,selection_id,runtime_task_id,delegation_id,attempt "
                "FROM orchestration_task_nodes WHERE orchestration_id=? AND plan_task_id=?",
                (oid, plan_task_id),
            ).fetchone()
            if (run["status"] != "Running" or node is None or node["state"] != "running"
                    or node["selected_agent_id"] != selected_agent_id
                    or node["selection_id"] != selection_id
                    or node["runtime_task_id"] != runtime_task_id
                    or node["delegation_id"] != delegation_id
                    or int(node["attempt"]) != int(attempt)):
                return None
            existing = c.execute(
                "SELECT * FROM orchestration_execution_attempts WHERE orchestration_id=? "
                "AND plan_task_id=? AND attempt=?", (oid, plan_task_id, int(attempt)),
            ).fetchone()
            if existing is not None:
                if existing["runtime_task_id"] == runtime_task_id:
                    return dict(existing)
                raise ValueError("A different execution already exists for this semantic attempt.")
            c.execute(
                "INSERT INTO orchestration_execution_attempts("
                "id,orchestration_id,plan_task_id,attempt,selected_agent_id,selection_id,"
                "runtime_task_id,delegation_id,recovery_action_id,status,prompt,created_at,started_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, oid, plan_task_id, int(attempt), selected_agent_id, selection_id,
                 runtime_task_id, delegation_id, recovery_action_id, "running",
                 sanitize(prompt), now, now),
            )
        return next(item for item in self.list_execution_attempts(oid) if item["id"] == attempt_id)

    def update_execution_attempt(self, oid: str, plan_task_id: str, attempt: int, *,
                                 status: str, finished_at: str | None = None) -> None:
        if finished_at is None and status in {
                "success", "failed", "blocked", "cancelled", "skipped", "superseded", "recovered"}:
            finished_at = utcnow()
        with self._connection(write=True) as c:
            c.execute(
                "UPDATE orchestration_execution_attempts SET status=?,finished_at=? "
                "WHERE orchestration_id=? AND plan_task_id=? AND attempt=?",
                (sanitize(status), finished_at, oid, plan_task_id, int(attempt)),
            )

    def commit_recovery_action(self, recovery_id: str, oid: str, plan_task_id: str, *,
                               source_attempt: int, source_evaluation_id: str,
                               decision: dict[str, Any], recovery_version: int,
                               prompt: str = "", snapshot: dict[str, Any] | None = None
                               ) -> dict[str, Any] | None:
        """Persist one decision and atomically fail, retry, or reserve replanning."""
        action = decision.get("action")
        if action not in {"retry_same_agent", "retry_different_agent", "replan_subgraph", "fail"}:
            raise ValueError("Unknown recovery action.")
        now = utcnow()
        with self._connection(write=True) as c:
            run = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if run is None:
                raise KeyError(oid)
            node = c.execute(
                "SELECT state,attempt,evaluation_id,error FROM orchestration_task_nodes "
                "WHERE orchestration_id=? AND plan_task_id=?", (oid, plan_task_id),
            ).fetchone()
            if (run["status"] != "Running" or node is None
                    or node["state"] != "recovery_pending"
                    or int(node["attempt"]) != int(source_attempt)
                    or node["evaluation_id"] != source_evaluation_id):
                return None
            c.execute(
                "INSERT INTO orchestration_recovery_actions("
                "id,orchestration_id,plan_task_id,source_attempt,source_evaluation_id,action,"
                "reason,instructions,exclude_agent_ids_json,affected_task_ids_json,fingerprint,"
                "recovery_version,metrics_json,snapshot_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (recovery_id, oid, plan_task_id, int(source_attempt), source_evaluation_id,
                 action, sanitize(decision.get("reason") or ""),
                 sanitize(decision.get("instructions") or ""),
                 _dump(decision.get("exclude_agent_ids") or []),
                 _dump(decision.get("affected_task_ids") or []),
                 sanitize(decision.get("fingerprint") or ""), int(recovery_version),
                 _dump(decision.get("metrics") or {}), _dump(snapshot or {}), now),
            )
            c.execute(
                "UPDATE orchestration_execution_attempts SET recovery_action_id=?,status='recovered',"
                "finished_at=? WHERE orchestration_id=? AND plan_task_id=? AND attempt=?",
                (recovery_id, now, oid, plan_task_id, int(source_attempt)),
            )
            if action == "fail":
                cursor = c.execute(
                    "UPDATE orchestration_task_nodes SET state='failed',recovery_action_id=?,"
                    "error=?,waiting_reason='',finished_at=?,updated_at=? WHERE orchestration_id=? "
                    "AND plan_task_id=? AND state='recovery_pending' AND evaluation_id=?",
                    (recovery_id, sanitize(" ".join(item for item in (
                        node["error"] or "", decision.get("reason") or "Recovery exhausted."
                    ) if item)),
                     now, now, oid, plan_task_id, source_evaluation_id),
                )
            elif action in {"retry_same_agent", "retry_different_agent"}:
                cursor = c.execute(
                    "UPDATE orchestration_task_nodes SET state='ready',selected_agent_id=NULL,"
                    "selection_id=NULL,runtime_task_id=NULL,delegation_id=NULL,evaluation_id=NULL,"
                    "evaluation_status=NULL,recovery_action_id=?,attempt_prompt=?,waiting_reason='',"
                    "result_json=NULL,error=NULL,started_at=NULL,finished_at=NULL,updated_at=? "
                    "WHERE orchestration_id=? AND plan_task_id=? AND state='recovery_pending' "
                    "AND evaluation_id=?",
                    (recovery_id, sanitize(prompt), now, oid, plan_task_id, source_evaluation_id),
                )
            else:
                cursor = c.execute(
                    "UPDATE orchestration_task_nodes SET recovery_action_id=?,updated_at=? "
                    "WHERE orchestration_id=? AND plan_task_id=? AND state='recovery_pending' "
                    "AND evaluation_id=?",
                    (recovery_id, now, oid, plan_task_id, source_evaluation_id),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("Recovery node transition lost its atomic precondition.")
        return self.get_recovery(recovery_id)

    def fail_recovery_pending(self, oid: str, plan_task_id: str, *, attempt: int,
                              evaluation_id: str, reason: str) -> bool:
        """Fail one exact recovery-pending attempt without creating an action row."""
        now = utcnow()
        with self._connection(write=True) as c:
            run = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if run is None:
                raise KeyError(oid)
            if run["status"] != "Running":
                return False
            cursor = c.execute(
                "UPDATE orchestration_task_nodes SET state='failed',error=?,waiting_reason='',"
                "finished_at=?,updated_at=? WHERE orchestration_id=? AND plan_task_id=? "
                "AND state='recovery_pending' AND attempt=? AND evaluation_id=? "
                "AND recovery_action_id IS NULL",
                (sanitize(reason), now, now, oid, plan_task_id, int(attempt), evaluation_id),
            )
            if cursor.rowcount != 1:
                return False
            c.execute(
                "UPDATE orchestration_execution_attempts SET status='failed',finished_at=? "
                "WHERE orchestration_id=? AND plan_task_id=? AND attempt=?",
                (now, oid, plan_task_id, int(attempt)),
            )
            return True

    def fail_recovery_action(self, recovery_id: str, reason: str) -> bool:
        now = utcnow()
        with self._connection(write=True) as c:
            recovery = c.execute(
                "SELECT orchestration_id,plan_task_id FROM orchestration_recovery_actions WHERE id=?",
                (recovery_id,),
            ).fetchone()
            if recovery is None:
                raise KeyError(recovery_id)
            cursor = c.execute(
                "UPDATE orchestration_task_nodes SET state='failed',error=?,finished_at=?,updated_at=? "
                "WHERE orchestration_id=? AND plan_task_id=? AND state='recovery_pending' "
                "AND recovery_action_id=?",
                (sanitize(reason), now, now, recovery["orchestration_id"],
                 recovery["plan_task_id"], recovery_id),
            )
            return cursor.rowcount == 1

    def commit_plan_revision(self, revision_id: str, oid: str, recovery_id: str, *,
                             summary: str, plan: dict[str, Any],
                             superseded_task_ids: list[str], metrics: dict[str, Any] | None = None
                             ) -> dict[str, Any] | None:
        """Persist a validated cumulative effective plan and update its durable graph."""
        normalized = validate_plan(plan)
        superseded = list(dict.fromkeys(superseded_task_ids))
        now = utcnow()
        with self._connection(write=True) as c:
            run = c.execute(
                "SELECT status,config_json,plan_json,effective_plan_json,current_plan_revision "
                "FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            recovery = c.execute(
                "SELECT plan_task_id,action,affected_task_ids_json "
                "FROM orchestration_recovery_actions WHERE id=? AND orchestration_id=?",
                (recovery_id, oid),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            if run["status"] != "Running" or recovery is None or recovery["action"] != "replan_subgraph":
                return None
            current_nodes = {row["plan_task_id"]: dict(row) for row in c.execute(
                "SELECT * FROM orchestration_task_nodes WHERE orchestration_id=?", (oid,),
            )}
            attempts = [dict(row) for row in c.execute(
                "SELECT plan_task_id,attempt FROM orchestration_execution_attempts "
                "WHERE orchestration_id=?", (oid,),
            )]
            current_plan = _load(run["effective_plan_json"]) or _load(run["plan_json"])
            source_task_id = recovery["plan_task_id"]
            allowed = allowed_replan_scope(
                source_task_id, current_plan, list(current_nodes.values()), attempts,
            )
            protected = set(current_nodes) - allowed
            affected = set(_load(recovery["affected_task_ids_json"]) or [])
            accepted = {task_id for task_id, node in current_nodes.items()
                        if node["state"] == "success"}
            normalized = validate_replan_revision(
                current_plan=current_plan, revised_plan=normalized,
                source_task_id=source_task_id, affected_task_ids=affected,
                superseded_task_ids=set(superseded), allowed_task_ids=allowed,
                protected_task_ids=protected, accepted_task_ids=accepted,
                historical_task_ids=set(current_nodes),
                max_tasks=int((_load(run["config_json"]) or {}).get(
                    "max_delegated_tasks", MAX_PLAN_TASKS)),
            )
            active_runtime = {
                task_id: node["runtime_task_id"] for task_id, node in current_nodes.items()
                if node["state"] in {"running", "waiting_for_approval", "evaluating"}
            }
            revision = int(run["current_plan_revision"] or 0) + 1
            c.execute(
                "INSERT INTO orchestration_plan_revisions("
                "id,orchestration_id,revision,source_recovery_action_id,source_plan_task_id,"
                "summary,plan_json,superseded_task_ids_json,metrics_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (revision_id, oid, revision, recovery_id, recovery["plan_task_id"],
                 sanitize(summary), _dump(normalized), _dump(superseded),
                 _dump(metrics or {}), now),
            )
            states = {task_id: row["state"] for task_id, row in current_nodes.items()}
            for index, task in enumerate(normalized["tasks"]):
                task_id = task["id"]
                if task_id in current_nodes:
                    if task_id in superseded:
                        state = "superseded"
                    else:
                        state = current_nodes[task_id]["state"]
                        if state in {"pending", "ready"}:
                            state = ("ready" if all(states.get(dep) == "success"
                                                    for dep in task["depends_on"]) else "pending")
                    c.execute(
                        "UPDATE orchestration_task_nodes SET plan_order=?,depends_on_json=?,state=?,"
                        "plan_revision=?,finished_at=CASE WHEN ?='superseded' THEN ? ELSE finished_at END,"
                        "updated_at=? WHERE orchestration_id=? AND plan_task_id=?",
                        (index, _dump(task["depends_on"]), state, revision, state, now, now,
                         oid, task_id),
                    )
                    states[task_id] = state
                else:
                    state = ("ready" if all(states.get(dep) == "success"
                                            for dep in task["depends_on"]) else "pending")
                    c.execute(
                        "INSERT INTO orchestration_task_nodes("
                        "orchestration_id,plan_task_id,plan_order,depends_on_json,state,"
                        "attempt_prompt,plan_revision,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (oid, task_id, index, _dump(task["depends_on"]), state,
                         sanitize(task["objective"]), revision, now),
                    )
                    states[task_id] = state
            for task_id, runtime_task_id in active_runtime.items():
                tracked = list(c.execute(
                    "SELECT plan_task_id,state FROM orchestration_task_nodes "
                    "WHERE orchestration_id=? AND runtime_task_id=? AND state IN "
                    "('running','waiting_for_approval','evaluating')", (oid, runtime_task_id),
                ))
                if (len(tracked) != 1 or tracked[0]["plan_task_id"] != task_id
                        or tracked[0]["state"] != current_nodes[task_id]["state"]):
                    raise RuntimeError("Plan revision orphaned an active Runtime task.")
            c.execute(
                "UPDATE orchestration_runs SET effective_plan_json=?,current_plan_revision=?,"
                "updated_at=? WHERE id=? AND status='Running'",
                (_dump(normalized), revision, now, oid),
            )
            c.execute("UPDATE orchestration_recovery_actions SET plan_revision=? WHERE id=?",
                      (revision, recovery_id))
        return next(item for item in self.list_plan_revisions(oid) if item["id"] == revision_id)


    def add_orchestration_event(self, oid, event):
        with self._connection(write=True) as c:
            c.execute("INSERT INTO orchestration_events(orchestration_id,timestamp,event_type,status,agent_id,task_id,message,payload_json) VALUES(?,?,?,?,?,?,?,?)", (oid,utcnow(),event.get("event_type","update"),event.get("status"),event.get("agent_id"),event.get("task_id"),event.get("message",event.get("reason","")),_dump(event)))

    def save_agent_selection(self, oid: str, selection: dict[str, Any]) -> str | None:
        """Persist an immutable selector snapshot while the orchestration is running."""
        if not isinstance(selection, dict):
            raise ValueError("Agent selection must be an object.")
        planned_task_id = selection.get("task_id")
        selected_agent_id = selection.get("selected_agent_id")
        attempt = selection.get("attempt", 1)
        status = selection.get("status")
        version = selection.get("selector_version")
        score = selection.get("score")
        if not isinstance(planned_task_id, str) or not planned_task_id.strip():
            raise ValueError("Agent selection requires a planned task id.")
        if selected_agent_id is not None and (not isinstance(selected_agent_id, str)
                                               or not selected_agent_id.strip()):
            raise ValueError("Selected agent id must be non-empty text or null.")
        if status not in {"selected", "approval_required", "no_eligible_agent"}:
            raise ValueError("Unknown agent selection status.")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("Selector version must be a positive integer.")
        if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
            raise ValueError("Selection score must be an integer or null.")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("Selection attempt must be a positive integer.")
        selection_id, now = str(uuid4()), utcnow()
        with self._connection(write=True) as c:
            state = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if state is None:
                raise KeyError(oid)
            if state["status"] != "Running":
                return None
            c.execute(
                "INSERT INTO orchestration_selections(id,orchestration_id,planned_task_id,"
                "selected_agent_id,status,selector_version,score,attempt,snapshot_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (selection_id, oid, planned_task_id.strip(), selected_agent_id, status,
                 version, score, attempt, _dump(selection), now),
            )
        return selection_id

    def add_delegation(self, oid, agent_id, objective, task_id=None):
        did=str(uuid4()); now=utcnow()
        with self._connection(write=True) as c:
            state = c.execute("SELECT status FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
            if state is None:
                raise KeyError(oid)
            if state["status"] != "Running":
                return None
            c.execute("INSERT INTO orchestration_delegations(id,orchestration_id,agent_id,task_id,objective,created_at) VALUES(?,?,?,?,?,?)",(did,oid,agent_id,task_id,sanitize(objective),now))
        return did

    def update_delegation(self, did, **fields):
        if "result" in fields:
            fields["result_json"] = _dump(fields.pop("result"))
        allowed={"task_id","status","result_json","finished_at"}; unknown=set(fields)-allowed
        if unknown:
            raise ValueError("Unknown delegation fields: " + ", ".join(sorted(unknown)))
        if not fields:
            return
        with self._connection(write=True) as c:
            c.execute("UPDATE orchestration_delegations SET "+",".join(k+"=?" for k in fields)+" WHERE id=?", [*fields.values(),did])

    def recover_interrupted_orchestrations(self) -> int:
        """Atomically fail active orchestration runs once after a server restart."""
        now = utcnow()
        message = "Orchestration interrupted by a server restart. Submit a new request to retry."
        with self._connection(write=True) as c:
            placeholders = ",".join("?" for _ in ORCHESTRATION_ACTIVE_STATUSES)
            rows = c.execute(
                f"SELECT id FROM orchestration_runs WHERE status IN ({placeholders}) ORDER BY created_at,id",
                ORCHESTRATION_ACTIVE_STATUSES,
            ).fetchall()
            for row in rows:
                cursor = c.execute(
                    f"UPDATE orchestration_runs SET status='Failed',error=?,updated_at=? "
                    f"WHERE id=? AND status IN ({placeholders})",
                    (message, now, row["id"], *ORCHESTRATION_ACTIVE_STATUSES),
                )
                if cursor.rowcount != 1:
                    continue
                c.execute(
                    "UPDATE orchestration_task_nodes SET state=CASE "
                    "WHEN state IN ('running','waiting_for_approval','evaluating') "
                    "THEN 'cancelled' ELSE 'skipped' END,"
                    "waiting_reason='',error=?,finished_at=?,updated_at=? "
                    "WHERE orchestration_id=? AND state NOT IN "
                    "('success','failed','blocked','cancelled','skipped','superseded')",
                    (message, now, now, row["id"]),
                )
                c.execute(
                    "UPDATE orchestration_execution_attempts SET status='cancelled',finished_at=? "
                    "WHERE orchestration_id=? AND status IN "
                    "('running','waiting_for_approval','evaluating','recovery_pending','ready','pending')",
                    (now, row["id"]),
                )
                event = {"event_type": "freya.interrupted", "status": "Failed", "message": message}
                c.execute(
                    "INSERT INTO orchestration_events(orchestration_id,timestamp,event_type,status,message,payload_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (row["id"], now, event["event_type"], event["status"], message, _dump(event)),
                )
            return len(rows)

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
