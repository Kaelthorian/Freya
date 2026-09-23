"""SQLite persistence mixin for orchestration-level integration history."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from .integration import (GLOBAL_STATUSES, stable_graph_fingerprint,
                          validate_integration_revision, validate_global_result,
                          build_integration_input, GlobalVerifier)
from .planner import MAX_PLAN_TASKS, validate_plan
from .security import sanitize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def migrate_integration_schema(connection) -> None:
    """Rebuild the 4.5 revision table once so integration can be a real source."""
    columns = list(connection.execute("PRAGMA table_info(orchestration_plan_revisions)"))
    if not columns:
        return
    by_name = {row[1]: row for row in columns}
    recovery_not_null = bool(by_name.get("source_recovery_action_id", [None] * 4)[3])
    if "revision_source_type" in by_name and "source_integration_id" in by_name and not recovery_not_null:
        return
    connection.execute("DROP INDEX IF EXISTS idx_orch_plan_revisions")
    connection.execute(
        "ALTER TABLE orchestration_plan_revisions RENAME TO orchestration_plan_revisions_45"
    )
    connection.execute("""
        CREATE TABLE orchestration_plan_revisions (
            id TEXT PRIMARY KEY,
            orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id),
            revision INTEGER NOT NULL,
            revision_source_type TEXT NOT NULL DEFAULT 'task_recovery',
            source_recovery_action_id TEXT REFERENCES orchestration_recovery_actions(id),
            source_integration_id TEXT REFERENCES orchestration_integrations(id),
            source_plan_task_id TEXT,
            summary TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            superseded_task_ids_json TEXT NOT NULL DEFAULT '[]',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE (orchestration_id, revision),
            UNIQUE (source_recovery_action_id),
            UNIQUE (source_integration_id),
            CHECK (
                (revision_source_type='task_recovery' AND source_recovery_action_id IS NOT NULL
                    AND source_integration_id IS NULL AND source_plan_task_id IS NOT NULL)
                OR
                (revision_source_type='integration' AND source_recovery_action_id IS NULL
                    AND source_integration_id IS NOT NULL AND source_plan_task_id IS NULL)
            )
        )
    """)
    connection.execute(
        "INSERT INTO orchestration_plan_revisions("
        "id,orchestration_id,revision,revision_source_type,source_recovery_action_id,"
        "source_integration_id,source_plan_task_id,summary,plan_json,"
        "superseded_task_ids_json,metrics_json,created_at) "
        "SELECT id,orchestration_id,revision,'task_recovery',source_recovery_action_id,NULL,"
        "source_plan_task_id,summary,plan_json,superseded_task_ids_json,metrics_json,created_at "
        "FROM orchestration_plan_revisions_45"
    )
    connection.execute("DROP TABLE orchestration_plan_revisions_45")
    connection.execute(
        "CREATE INDEX idx_orch_plan_revisions "
        "ON orchestration_plan_revisions(orchestration_id,revision)"
    )


class IntegrationStoreMixin:
    @staticmethod
    def _integration(row, *, include_snapshot: bool = False) -> dict[str, Any]:
        item = dict(row)
        value = _load(item.pop("integration_json")) or {}
        item["metrics"] = _load(item.pop("metrics_json")) or {}
        snapshot = _load(item.pop("snapshot_json")) or {}
        item["context_truncated"] = bool(item["context_truncated"])
        item["deterministic"] = bool(item["deterministic"])
        for field in ("criteria", "cross_task_issues", "missing_evidence",
                      "responsible_task_ids", "recommended_action"):
            item[field] = value.get(field)
        if include_snapshot:
            item["snapshot"] = snapshot
        return item

    def list_integrations(self, oid: str, *, include_snapshot: bool = False) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if connection.execute(
                    "SELECT 1 FROM orchestration_runs WHERE id=?", (oid,)).fetchone() is None:
                raise KeyError(oid)
            return [self._integration(row, include_snapshot=include_snapshot) for row in connection.execute(
                "SELECT * FROM orchestration_integrations WHERE orchestration_id=? ORDER BY round",
                (oid,),
            )]

    def get_integration(self, integration_id: str, *, include_snapshot: bool = False) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM orchestration_integrations WHERE id=?", (integration_id,),
            ).fetchone()
            if row is None:
                raise KeyError(integration_id)
            return self._integration(row, include_snapshot=include_snapshot)

    @staticmethod
    def _current_integration_fingerprint(connection, oid: str, revision: int) -> str:
        nodes = [dict(row) for row in connection.execute(
            "SELECT plan_task_id,state,evaluation_id,evaluation_status,attempt "
            "FROM orchestration_task_nodes WHERE orchestration_id=? AND state!='superseded' "
            "ORDER BY plan_order,plan_task_id", (oid,),
        )]
        return stable_graph_fingerprint(revision, nodes)

    def _validate_accepted_proof(self, connection, oid, result, snapshot):
        """Rebuild proof authority from persisted plans/evaluations inside the write transaction."""
        run = connection.execute("SELECT * FROM orchestration_runs WHERE id=?", (oid,)).fetchone()
        nodes = [self._execution_node(row) for row in connection.execute(
            "SELECT * FROM orchestration_task_nodes WHERE orchestration_id=? "
            "ORDER BY plan_order,plan_task_id", (oid,),
        )]
        evaluations = {}
        for node in nodes:
            if node["state"] == "superseded":
                continue
            row = connection.execute(
                "SELECT * FROM orchestration_evaluations WHERE id=? AND orchestration_id=? "
                "AND plan_task_id=? AND attempt=?",
                (node.get("evaluation_id"), oid, node["plan_task_id"], node["attempt"]),
            ).fetchone()
            if row is not None:
                evaluations[row["id"]] = self._evaluation(row, include_snapshot=True)
        original_plan = _load(run["plan_json"])
        prepared = build_integration_input(
            original_user_prompt=original_plan["goal"], original_plan=original_plan,
            effective_plan=_load(run["effective_plan_json"]) or _load(run["plan_json"]),
            plan_revision=int(run["current_plan_revision"] or 0),
            graph_nodes=nodes, evaluations=evaluations, revision_history=[],
        )
        if snapshot != prepared["snapshot"]:
            raise ValueError("Integration proof snapshot differs from persisted authority.")
        if GlobalVerifier(offline=True)._hard_check(prepared) is not None:
            raise ValueError("Accepted integration contradicts deterministic evidence checks.")
        validate_global_result(
            result, snapshot["global_criteria"], snapshot["active_task_ids"],
            set(snapshot["evidence_catalog"]), snapshot["proof_refs_by_criterion"],
        )

    def commit_integration(self, integration_id: str, oid: str, *, round_number: int,
                           plan_revision: int, integration_version: int,
                           result: dict[str, Any], metrics: dict[str, Any],
                           snapshot: dict[str, Any], context_truncated: bool,
                           deterministic: bool, problem_fingerprint: str) -> dict[str, Any] | None:
        status = result.get("status")
        if status not in GLOBAL_STATUSES:
            raise ValueError("Unknown global integration status.")
        expected_fingerprint = str(snapshot.get("graph_fingerprint") or "")
        if not expected_fingerprint:
            raise ValueError("Integration snapshot requires a graph fingerprint.")
        now = _now()
        with self._connection(write=True) as connection:
            run = connection.execute(
                "SELECT status,current_plan_revision FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            if (run["status"] != "Integrating"
                    or int(run["current_plan_revision"] or 0) != int(plan_revision)
                    or self._current_integration_fingerprint(connection, oid, plan_revision)
                    != expected_fingerprint):
                return None
            if status == "accepted":
                self._validate_accepted_proof(connection, oid, result, snapshot)
            connection.execute(
                "INSERT INTO orchestration_integrations("
                "id,orchestration_id,round,plan_revision,integration_version,status,summary,"
                "integration_json,metrics_json,snapshot_json,graph_fingerprint,problem_fingerprint,"
                "context_truncated,deterministic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (integration_id, oid, int(round_number), int(plan_revision),
                 int(integration_version), status, sanitize(result.get("summary") or ""),
                 _dump(result), _dump(metrics), _dump(snapshot), expected_fingerprint,
                 sanitize(problem_fingerprint),
                 int(bool(context_truncated)), int(bool(deterministic)), now),
            )
        return self.get_integration(integration_id, include_snapshot=True)

    def finalize_accepted_integration(self, oid: str, integration_id: str,
                                      response: str) -> dict[str, Any] | None:
        now = _now()
        with self._connection(write=True) as connection:
            run = connection.execute(
                "SELECT status,current_plan_revision FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            integration = connection.execute(
                "SELECT * FROM orchestration_integrations "
                "WHERE id=? AND orchestration_id=?", (integration_id, oid),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            if (run["status"] != "Integrating" or integration is None
                    or integration["status"] != "accepted"
                    or int(run["current_plan_revision"] or 0) != int(integration["plan_revision"])
                    or self._current_integration_fingerprint(
                        connection, oid, int(integration["plan_revision"])
                    ) != integration["graph_fingerprint"]):
                return None
            self._validate_accepted_proof(
                connection, oid, _load(integration["integration_json"]),
                _load(integration["snapshot_json"]),
            )
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status='Success',response=?,error=NULL,updated_at=? "
                "WHERE id=? AND status='Integrating'", (sanitize(response), now, oid),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_orchestration(oid)

    def commit_integration_plan_revision(self, revision_id: str, oid: str,
                                         integration_id: str, *, summary: str,
                                         plan: dict[str, Any], new_task_ids: list[str],
                                         metrics: dict[str, Any] | None = None
                                         ) -> dict[str, Any] | None:
        normalized = validate_plan(plan)
        now = _now()
        with self._connection(write=True) as connection:
            run = connection.execute(
                "SELECT status,config_json,plan_json,effective_plan_json,current_plan_revision "
                "FROM orchestration_runs WHERE id=?", (oid,),
            ).fetchone()
            integration = connection.execute(
                "SELECT status,plan_revision,graph_fingerprint FROM orchestration_integrations "
                "WHERE id=? AND orchestration_id=?", (integration_id, oid),
            ).fetchone()
            if run is None:
                raise KeyError(oid)
            if (run["status"] != "Integrating" or integration is None
                    or integration["status"] not in {"needs_work", "blocked"}
                    or int(run["current_plan_revision"] or 0) != int(integration["plan_revision"])
                    or self._current_integration_fingerprint(
                        connection, oid, int(integration["plan_revision"])
                    ) != integration["graph_fingerprint"]):
                return None
            config = _load(run["config_json"]) or {}
            revision = int(run["current_plan_revision"] or 0) + 1
            if revision > int(config.get("max_plan_revisions", 2)):
                raise ValueError("The plan-revision budget is exhausted.")
            current_plan = _load(run["effective_plan_json"]) or _load(run["plan_json"])
            nodes = {row["plan_task_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM orchestration_task_nodes WHERE orchestration_id=?", (oid,),
            )}
            received_by_id = {task["id"]: task for task in plan["tasks"]}
            if any(received_by_id.get(task["id"]) != task for task in current_plan["tasks"]):
                raise ValueError("Integration revision cannot modify or delete an existing task.")
            for field in ("goal", "summary", "success_criteria"):
                if normalized[field] != current_plan[field]:
                    raise ValueError("Integration revision cannot modify existing plan fields.")

            accepted = {task_id for task_id, node in nodes.items()
                        if node["state"] == "success" and node["evaluation_status"] == "accepted"}
            submitted_links = normalized["criterion_links"]
            normalized = validate_integration_revision(
                current_plan=current_plan,
                new_tasks=[task for task in normalized["tasks"] if task["id"] not in {
                    item["id"] for item in current_plan["tasks"]
                }],
                accepted_task_ids=accepted, historical_task_ids=set(nodes),
                max_tasks=int(config.get("max_delegated_tasks", MAX_PLAN_TASKS)),
                criterion_links=submitted_links,
            )
            actual_new_ids = [task["id"] for task in normalized["tasks"] if task["id"] not in nodes]
            if actual_new_ids != list(new_task_ids):
                raise ValueError("Integration revision new task IDs changed before commit.")
            connection.execute(
                "INSERT INTO orchestration_plan_revisions("
                "id,orchestration_id,revision,revision_source_type,source_recovery_action_id,"
                "source_integration_id,source_plan_task_id,summary,plan_json,"
                "superseded_task_ids_json,metrics_json,created_at) "
                "VALUES(?,?,?,'integration',NULL,?,NULL,?,?,?, ?,?)",
                (revision_id, oid, revision, integration_id, sanitize(summary), _dump(normalized),
                 _dump([]), _dump(metrics or {}), now),
            )
            states = {task_id: node["state"] for task_id, node in nodes.items()}
            by_id = {task["id"]: task for task in normalized["tasks"]}
            for index, task_id in enumerate(actual_new_ids):
                task = by_id[task_id]
                state = ("ready" if all(states.get(dependency) == "success"
                                        for dependency in task["depends_on"]) else "pending")
                connection.execute(
                    "INSERT INTO orchestration_task_nodes("
                    "orchestration_id,plan_task_id,plan_order,depends_on_json,state,"
                    "attempt_prompt,plan_revision,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (oid, task_id, len(nodes) + index, _dump(task["depends_on"]), state,
                     sanitize(task["objective"]), revision, now),
                )
                states[task_id] = state
            cursor = connection.execute(
                "UPDATE orchestration_runs SET effective_plan_json=?,current_plan_revision=?,"
                "status='Running',updated_at=? WHERE id=? AND status='Integrating' "
                "AND current_plan_revision=?",
                (_dump(normalized), revision, now, oid, int(integration["plan_revision"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Integration revision lost its state precondition.")
        return next(item for item in self.list_plan_revisions(oid) if item["id"] == revision_id)
