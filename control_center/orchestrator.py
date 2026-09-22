"""Freya orchestration service: structured planning, delegation and integration."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .agent_context import build_effective_agent, capability_summary
from .agent_factory import AgentFactory
from .agent_selector import AgentSelector
from .capabilities import capability_catalog
from .execution_graph import ExecutionGraph
from .integration import GlobalVerifier, IntegrationReplanner, ResultIntegrator
from .integration_orchestrator import IntegrationOrchestrationMixin
from .recovery import (FAILURE_ANALYSIS_VERSION, RECOVERY_VERSION,
                       FailureAnalyzer, RecoveryController, Replanner,
                       allowed_replan_scope, build_retry_prompt,
                       deterministic_failure_diagnosis,
                       semantic_failure_fingerprint)
from .security import sanitize
from .evaluator import EVALUATION_FIELDS, EVALUATOR_VERSION, Evaluator, technical_failure_evaluation
from .planner import MAX_PLAN_TASKS, PLAN_SCHEMA_VERSION, Planner
from .skills import skill_summary
from .task_analyst import (TaskAnalyst, deterministic_task_analysis,
                            select_task_analyst)
from .storage import ORCHESTRATION_ACTIVE_STATUSES, ORCHESTRATION_TERMINAL_STATUSES, utcnow


ACTIVE_DELEGATED_TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused"}


class Orchestrator(IntegrationOrchestrationMixin):
    def __init__(self, store, runtime, decide: Callable | None = None, config: dict | None = None,
                 planner: Planner | None = None, clock: Callable[[], float] | None = None,
                 wait: Callable[[float], None] | None = None,
                 selector: AgentSelector | None = None,
                 evaluator: Evaluator | None = None,
                 recovery: RecoveryController | None = None,
                 replanner: Replanner | None = None,
                 failure_analyzer: FailureAnalyzer | None = None,
                 task_analyst: TaskAnalyst | None = None,
                 global_verifier: GlobalVerifier | None = None,
                 integration_replanner: IntegrationReplanner | None = None,
                 result_integrator: ResultIntegrator | None = None,
                 agent_factory: AgentFactory | None = None):
        self.store, self.runtime = store, runtime
        self.decide = decide
        self.planner = planner or Planner()
        self.selector = selector or AgentSelector()
        # Compatibility fallback is evidence-only: it cannot accept Runtime
        # Success unless configured objective verification actually passed.
        self.evaluator = evaluator or Evaluator(offline=True)
        self.clock = clock or time.monotonic
        self.wait = wait or time.sleep
        self.recovery = recovery or RecoveryController(offline=True)
        self.replanner = replanner or Replanner()
        self.failure_analyzer = failure_analyzer or FailureAnalyzer(offline=True)
        self.task_analyst = task_analyst
        self.global_verifier = global_verifier or GlobalVerifier(offline=True)
        self.integration_replanner = integration_replanner or IntegrationReplanner()
        self.result_integrator = result_integrator or ResultIntegrator()
        self.agent_factory = agent_factory or AgentFactory(store)
        self.lock = threading.RLock()
        self._orchestration_workspaces: dict[str, str] = {}
        self.planner_lock = threading.Lock()
        self.evaluator_lock = threading.Lock()
        self.integration_lock = threading.Lock()
        self.config = {"max_rounds": 6, "max_delegated_tasks": MAX_PLAN_TASKS,
                       "max_parallel_tasks": 4, "max_model_calls": 12,
                       "max_wallclock_seconds": 900,
                       "max_semantic_attempts_per_task": 3,
                       "max_plan_revisions": 2, "max_recovery_actions": 8,
                       "max_recovery_model_calls": 16, "max_integration_rounds": 2,
                       "max_integration_model_calls": 12}
        self.recovery_lock = threading.Lock()
        self.failure_analysis_lock = threading.Lock()
        self.config.update(config or {})
        for field in ("max_delegated_tasks", "max_parallel_tasks", "max_semantic_attempts_per_task",
                      "max_plan_revisions", "max_recovery_actions",
                      "max_recovery_model_calls", "max_integration_rounds",
                      "max_integration_model_calls"):
            value = self.config[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer.")
        self.config["max_parallel_tasks"] = min(
            self.config["max_parallel_tasks"], self.config["max_delegated_tasks"],
        )

    def submit(self, prompt: str, workspace_path: str | None = None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Orchestration prompt must contain at least one character.")
        config = {**self.config, "workspace_path": workspace_path or ""}
        run = self.store.create_orchestration(prompt.strip(), config)
        threading.Thread(target=self._run, args=(run["id"],), daemon=True,
                         name="freya-orchestrator").start()
        return run

    def _workspace_for_run(self, run: dict) -> str | None:
        """Return the one workspace shared by every node in an orchestration."""
        config = run.get("config") or {}
        configured = str(config.get("workspace_path") or "").strip()
        if configured:
            return configured
        oid = str(run.get("id") or "")
        cached = self._orchestration_workspaces.get(oid)
        if cached:
            return cached
        data_dir = getattr(self.runtime, "data_dir", None)
        if data_dir is None:
            # Lightweight test runtimes and extension hooks may allocate their
            # own workspace when no production data directory is available.
            return None
        root = Path(data_dir) / "workspaces"
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        selected = str(workspace)
        setter = getattr(self.store, "set_orchestration_workspace", None)
        if callable(setter):
            persisted = setter(oid, selected)
            persisted_path = str(((persisted or {}).get("config") or {}).get("workspace_path") or "").strip()
            if persisted_path:
                selected = persisted_path
        self._orchestration_workspaces[oid] = selected
        return selected
    @staticmethod
    def _execution_prompt(operational_prompt: str, planned_task: dict,
                          attempt_prompt: str = "") -> str:
        """Send the Task Analyst's operational brief to every delegated worker."""
        sections = [
            "TASK ANALYST OPERATIONAL BRIEF (authoritative for execution):\n"
            + str(operational_prompt or "").strip(),
            "DELEGATED PLAN STEP:\n" + str(planned_task.get("objective") or "").strip(),
        ]
        description = str(planned_task.get("description") or "").strip()
        criteria = planned_task.get("success_criteria") or []
        if description:
            sections.append("Step context:\n" + description)
        if criteria:
            sections.append("Step success criteria:\n" + "\n".join("- " + str(item) for item in criteria))
        if attempt_prompt.strip():
            sections.append("RECOVERY INSTRUCTIONS:\n" + attempt_prompt.strip())
        sections.append("Complete this step without inventing requirements outside the Task Analyst operational brief.")
        return "\n\n".join(section for section in sections if section.strip())

    def _snapshot_delegation(self, delegation_id: str, task: dict) -> None:
        result = {
            "result": task.get("result"),
            "error": task.get("error") or "",
            "verification": task.get("verification"),
        }
        self.store.update_delegation(
            delegation_id, status=task["status"], result=result,
            finished_at=task.get("finished_at"),
        )

    @staticmethod
    def _recovery_workspace_state(task: dict | None) -> dict[str, Any]:
        """Build bounded, auditable state for a subsequent recovery attempt."""
        if not isinstance(task, dict):
            return {}
        result = task.get("result")
        result_dict = result if isinstance(result, dict) else {}
        return sanitize({
            "workspace": task.get("workspace") or "",
            "status": task.get("status") or "",
            "verification": task.get("verification") or {},
            "workspace_diffs": result_dict.get("workspace_diffs") or [],
            "artifacts": result_dict.get("artifacts") or [],
            "actions": result_dict.get("actions") or [],
            "error": task.get("error") or "",
        })

    @staticmethod
    def _selection_failure_message(selection: dict, agents: list[dict]) -> str:
        """Turn the persisted selector snapshot into a concise log-visible cause."""
        names = {str(agent.get("id")): str(agent.get("name") or agent.get("id"))
                 for agent in agents if isinstance(agent, dict) and agent.get("id")}
        details: list[str] = []
        for candidate in selection.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            label = names.get(str(candidate.get("agent_id")), str(candidate.get("agent_id") or "Agent"))
            warnings = [str(item).strip() for item in candidate.get("warnings", [])
                        if str(item).strip()]
            if warnings:
                details.append(label + ": " + "; ".join(warnings[:4]))
        if not details:
            details = [str(item).strip() for item in selection.get("warnings", [])
                       if str(item).strip()]
        base = "No eligible or conditional agent is available for the planned task."
        return sanitize(base + ((" Cause: " + " | ".join(details[:5])) if details else ""))[:4000]

    @staticmethod
    def _compact_failure_log(source: str, event: dict, *, task_id: str = "") -> dict:
        stored_payload = event.get("payload_json") if isinstance(event, dict) else None
        if isinstance(stored_payload, str) and stored_payload.strip():
            try:
                decoded_payload = json.loads(stored_payload)
            except (TypeError, ValueError):
                decoded_payload = {}
            if isinstance(decoded_payload, dict):
                decoded_payload.update({key: value for key, value in event.items()
                                        if key != "payload_json"})
                event = decoded_payload
        event_id = event.get("id")
        log_id = (f"orchestration:{event_id}" if source == "orchestration" else
                  f"task:{task_id}:{event_id}")
        compact = {"log_id": log_id, "source": source}
        for key in ("timestamp", "event_type", "level", "status", "task_id", "agent_id",
                    "runtime_task_id", "tool", "capability", "requested_capability",
                    "policy_decision", "policy_reason", "error_class", "step_id",
                    "step_number", "attempt", "message", "reason", "error", "resource"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                compact[key] = str(value)[:2000]
        for key in ("output", "input"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                bounded = sanitize(value)
                if isinstance(bounded, (dict, list)):
                    bounded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), default=str)
                compact[key] = str(bounded)[:4000]
        return sanitize(compact)

    def _failure_logs(self, oid: str, graph: ExecutionGraph) -> list[dict]:
        """Return a bounded log-only diagnostic context for a failed graph."""
        run = self.store.get_orchestration(oid)
        logs = [self._compact_failure_log("orchestration", event)
                for event in run.get("events", [])]
        for node in graph.serialize():
            runtime_task_id = str(node.get("runtime_task_id") or "")
            if not runtime_task_id:
                continue
            for event in self.store.list_events(task_id=runtime_task_id, limit=200):
                logs.append(self._compact_failure_log(
                    "task", event, task_id=runtime_task_id,
                ))
        logs.sort(key=lambda item: (str(item.get("timestamp") or ""), str(item["log_id"])))
        return logs[-250:]

    def _analyze_graph_failure(self, oid: str, graph: ExecutionGraph) -> tuple[dict, dict, str]:
        """Run one model review from persisted logs, then fail over deterministically."""
        logs = self._failure_logs(oid, graph)
        mode = "model"
        try:
            with self.failure_analysis_lock:
                diagnosis = self.failure_analyzer.analyze(logs)
            metrics = dict(self.failure_analyzer.metrics)
            if self.failure_analyzer.deterministic:
                mode = "deterministic"
        except Exception as exc:
            diagnosis = deterministic_failure_diagnosis(logs)
            metrics = dict(getattr(self.failure_analyzer, "metrics", {}) or {})
            metrics.setdefault("model_calls", 1)
            metrics["fallback_error"] = sanitize(str(exc))[:1000]
            mode = "deterministic_fallback"
        return diagnosis, metrics, mode

    def cancel(self, oid: str) -> dict:
        """Idempotently cancel a run and every active child under one process lock."""
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] in ORCHESTRATION_TERMINAL_STATUSES:
                return run
            cancelled = self.store.transition_orchestration(
                oid, ORCHESTRATION_ACTIVE_STATUSES, "Cancelled", error="Cancelled by user.",
            )
            if cancelled is None:
                return self.store.get_orchestration(oid)
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.cancelled", "status": "Cancelled",
                "message": "Freya orchestration cancelled by user.",
            })
            for delegation in cancelled.get("delegations", []):
                task_id = delegation.get("task_id")
                if task_id and delegation.get("status") in ACTIVE_DELEGATED_TASK_STATUSES:
                    try:
                        task = self.runtime.cancel(task_id)
                        self._snapshot_delegation(delegation["id"], task)
                    except (KeyError, ValueError):
                        pass
            if cancelled.get("plan"):
                persisted = self.store.get_execution_graph(oid)
                if persisted["nodes"]:
                    graph = ExecutionGraph(cancelled.get("effective_plan") or cancelled["plan"], persisted["nodes"])
                    graph.cancel_nonterminal(utcnow(), "Cancelled by user.")
                    self.store.save_execution_graph(oid, graph.serialize())
                    self._close_terminal_attempts(oid, graph)
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.graph.completed", "status": "Cancelled",
                        "message": "The execution graph was cancelled by the user.",
                        "summary": graph.summary(),
                    })
            self._archive_dynamic_agents(oid)
            return self.store.get_orchestration(oid)

    @staticmethod
    def _agent_candidates(agents):
        candidates = []
        for agent in agents:
            try:
                effective = build_effective_agent(agent)
                identity = effective["identity"]
                candidates.append({
                    "id": agent["id"], "name": identity["name"], "role": identity["role"],
                    "purpose": identity["purpose"], "description": identity["description"],
                    "skills": [skill_summary(item) for item in effective["skills"] if isinstance(item, dict)],
                    "capabilities_summary": capability_summary(effective["capability_policy"]),
                    "availability": agent.get("status", "Offline"), "enabled": bool(agent.get("enabled")),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return candidates

    def _planning_context(self, agents=None) -> dict:
        # Deliberately compact: no Skill procedures, logs, task output, policy
        # secrets, model transcripts, or preconfigured-agent availability cross
        # the planning boundary.
        skills = []
        if self.store is not None:
            skills = [{key: item.get(key) for key in ("id", "name", "category", "tags")}
                      for item in self.store.list_skills(enabled=True)[:200]]
        return {
            "capabilities": [{key: item[key] for key in ("id", "category", "description")}
                             for item in capability_catalog()],
            "skills": skills,
        }

    def _decision(self, prompt, agents, results):
        candidates = self._agent_candidates(agents)
        if self.decide:
            return self.decide(prompt, candidates, results)
        task = (prompt if isinstance(prompt, dict) else {
            "id": "compatibility-task", "objective": str(prompt), "description": str(prompt),
            "required_capabilities": [], "preferred_skills": [],
        })
        selection = self.selector.select_agent(task, agents)
        if selection["selected_agent_id"] is None:
            return {"action": "respond", "message": "No eligible agent is available for this request.",
                    "selection": selection}
        return {"action": "delegate", "tasks": [{
            "agent_id": selection["selected_agent_id"],
            "objective": task["objective"], "selection": selection,
        }]}

    def _selection_context(self, run: dict) -> dict:
        workloads: dict[str, int] = {}
        active_runtime_task_ids: set[str] = set()
        for task in self.store.list_tasks(limit=10000):
            if task.get("status") in ACTIVE_DELEGATED_TASK_STATUSES:
                agent_id = task.get("agent_id")
                if isinstance(agent_id, str):
                    workloads[agent_id] = workloads.get(agent_id, 0) + 1
                task_id = task.get("id")
                if isinstance(task_id, str):
                    active_runtime_task_ids.add(task_id)
        # A selected ready node is a scheduling reservation. Active graph nodes
        # are also included when their Runtime task is not yet visible, while
        # Runtime task IDs prevent double-counting the normal persisted path.
        orchestration_id = run.get("id")
        if isinstance(orchestration_id, str):
            for node in self.store.get_execution_graph(orchestration_id)["nodes"]:
                agent_id = node.get("selected_agent_id")
                if not isinstance(agent_id, str):
                    continue
                reserved = (node.get("state") == "ready" and node.get("selection_id"))
                missing_runtime = (
                    node.get("state") in {"running", "waiting_for_approval", "evaluating"}
                    and node.get("runtime_task_id") not in active_runtime_task_ids
                )
                if reserved or missing_runtime:
                    workloads[agent_id] = workloads.get(agent_id, 0) + 1
        return {
            "workspace_path": run.get("config", {}).get("workspace_path") or "",
            "workloads": workloads,
        }

    def _create_dynamic_agent(self, oid: str, task: dict, attempt: int,
                              *, variant: int = 0) -> dict:
        planned_task_id = str(task.get("id") or "")
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.agent_factory.started", "status": "Running",
            "task_id": planned_task_id, "attempt": attempt,
            "required_capabilities": list(task.get("required_capabilities", [])),
            "message": "Freya is building a least-privilege agent for the planned task.",
        })
        try:
            created = self.agent_factory.create(
                task, orchestration_id=oid, attempt=attempt, variant=variant,
            )
        except Exception as exc:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_factory.failed", "status": "Failed",
                "task_id": planned_task_id, "attempt": attempt,
                "message": "Dynamic agent creation failed: " + str(exc),
            })
            raise
        agent = created["agent"]
        event = {
            "event_type": "freya.agent_created", "status": "Running",
            "task_id": planned_task_id, "plan_task_id": planned_task_id,
            "agent_id": agent["id"], "role": created["role"],
            "skill_ids": created["skill_ids"],
            "required_capabilities": created["required_capabilities"],
            "effective_tools": created["effective_tools"],
            "attempt": attempt, "factory_version": created["factory_version"],
            "warnings": created["warnings"],
            "message": "Freya created a task-specific ephemeral agent.",
        }
        self.store.add_orchestration_event(oid, event)
        self.store.add_orchestration_event(oid, {
            **event,
            "event_type": "freya.agent_policy.validated",
            "message": "The dynamic agent policy and derived tool surface were validated.",
        })
        return created

    def _archive_dynamic_agents(self, oid: str) -> list[dict]:
        archiver = getattr(self.store, "archive_dynamic_agents", None)
        if not callable(archiver):
            return []
        try:
            archived = archiver(oid)
        except Exception as exc:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_factory.failed",
                "status": self.store.get_orchestration(oid)["status"],
                "message": "Ephemeral agent archival failed: " + str(exc),
            })
            return []
        status = self.store.get_orchestration(oid)["status"]
        for agent in archived:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.dynamic_agent.archived", "status": status,
                "agent_id": agent["agent_id"],
                "task_id": agent.get("plan_task_id"),
                "plan_task_id": agent.get("plan_task_id"),
                "attempt": agent.get("attempt"),
                "factory_version": agent.get("factory_version"),
                "message": "Freya archived an ephemeral agent after orchestration completion.",
            })
        return archived

    def _analyze_prompt(self, oid: str, prompt: str) -> tuple[dict | None, dict]:
        """Rewrite the human prompt once before any downstream planning."""
        analyst = select_task_analyst(self.store.list_agents())
        if analyst is None:
            analysis = deterministic_task_analysis(prompt)
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Planning":
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.task_analysis.completed", "status": "Planning",
                        "agent_name": "Freya deterministic Task Analyst",
                        "analysis_version": analysis.get("analysis_version", 2),
                        "analysis_mode": "deterministic_no_agent",
                        "metrics": {"mode": "deterministic_no_agent", "model_calls": 0},
                        "task_analysis": analysis,
                        "message": "No enabled Task Analyst was configured; Freya produced a deterministic operational brief.",
                    })
            return analysis, {"mode": "deterministic_no_agent", "model_calls": 0}
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Planning":
                return None, {"mode": "cancelled", "model_calls": 0}
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.task_analysis.started", "status": "Planning",
                "agent_id": analyst["id"], "agent_name": analyst.get("name", "Task Analyst"),
                "message": "Task Analyst is rewriting the human prompt into the operational task brief.",
            })
        try:
            analyzer = self.task_analyst or TaskAnalyst(offline=True)
            analysis = analyzer.analyze(prompt, analyst)
            metrics = dict(getattr(analyzer, "metrics", {}) or {})
            mode = str(metrics.get("mode") or "model")
        except Exception as exc:
            analysis = deterministic_task_analysis(prompt)
            metrics = dict(getattr(self.task_analyst, "metrics", {}) or {})
            metrics["fallback_error"] = sanitize(str(exc))[:1000]
            metrics["mode"] = "deterministic_fallback"
            mode = "deterministic_fallback"
        with self.lock:
            if self.store.get_orchestration(oid)["status"] == "Planning":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task_analysis.completed", "status": "Planning",
                    "agent_id": analyst["id"], "agent_name": analyst.get("name", "Task Analyst"),
                    "analysis_version": analysis.get("analysis_version", 1),
                    # This is a bounded operational interpretation, not
                    # hidden chain-of-thought. Keep the key explicit so the
                    # persistence sanitizer does not remove it.
                    "analysis_mode": mode, "metrics": metrics, "task_analysis": analysis,
                    "corrected_fields": metrics.get("corrected_fields", []),
                    "message": "Task Analyst produced the operational prompt used by the planner and workers.",
                })
        return analysis, metrics

    def _select_planned_task(self, oid: str, task: dict, run: dict) -> dict | None:
        planned_task_id = task["id"]
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return None
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selection.started", "status": "Running",
                "task_id": planned_task_id,
                "message": "Freya is ranking existing agents for the planned task.",
            })
        try:
            selection = self.selector.select_agent(
                task, self.store.list_agents(), self._selection_context(run),
            )
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running":
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.agent_selection.failed", "status": "Failed",
                        "task_id": planned_task_id,
                        "message": "Agent selection failed: " + str(exc),
                    })
            raise
        with self.lock:
            selection_id = self.store.save_agent_selection(oid, selection)
            if selection_id is None:
                return None
            selected_agent_id = selection["selected_agent_id"]
            if selected_agent_id is None:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selection.failed", "status": "Failed",
                    "task_id": planned_task_id, "selector_version": selection["selector_version"],
                    "message": "No eligible or conditional agent is available for the planned task.",
                })
                self._fail_running(
                    oid, f"No eligible agent is available for planned task {planned_task_id}.",
                )
                return None
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selected", "status": "Running",
                "task_id": planned_task_id, "agent_id": selected_agent_id,
                "score": selection["score"], "selector_version": selection["selector_version"],
                "classification": selection["classification"],
                "approval_required": selection["approval_required"],
                "message": "Freya selected an existing agent for the planned task.",
            })
        return {
            "agent_id": selected_agent_id,
            "objective": task["objective"],
            "planned_task_id": planned_task_id,
            "selection_id": selection_id,
            "selection": selection,
        }

    def _fail_planning(self, oid: str, exc: Exception,
                       planning_metrics: dict | None = None) -> None:
        with self.lock:
            message = str(exc)
            metrics = planning_metrics or {}
            failed = self.store.transition_orchestration(
                oid, ("Planning",), "Failed", error=message,
                planning_metrics=metrics,
            )
            if failed is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.failed", "status": "Failed", "message": message,
                "planning_metrics": metrics,
            })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.failed", "status": "Failed", "message": message,
            })
            self._archive_dynamic_agents(oid)

    def _fail_running(self, oid: str, message: str) -> None:
        with self.lock:
            failed = self.store.transition_orchestration(oid, ("Running",), "Failed", error=message)
            if failed is not None:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.failed", "status": "Failed", "message": message,
                })
                self._archive_dynamic_agents(oid)

    def _cancel_active_children(self, oid: str) -> None:
        for delegation in self.store.get_orchestration(oid).get("delegations", []):
            task_id = delegation.get("task_id")
            if not task_id:
                continue
            try:
                task = self.store.get_task(task_id)
                if task["status"] in ACTIVE_DELEGATED_TASK_STATUSES:
                    task = self.runtime.cancel(task_id)
                self._snapshot_delegation(delegation["id"], task)
            except (KeyError, ValueError):
                continue

    def _close_terminal_attempts(self, oid: str, graph: ExecutionGraph) -> None:
        """Keep the append-only attempt ledger consistent with terminal graph state."""
        terminal = {"success", "failed", "blocked", "cancelled", "skipped", "superseded"}
        for node in graph.serialize():
            attempt = int(node.get("attempt", 0))
            if attempt > 0 and node.get("state") in terminal:
                self.store.update_execution_attempt(
                    oid, node["plan_task_id"], attempt, status=node["state"],
                    finished_at=node.get("finished_at") or utcnow(),
                )

    def _timeout(self, oid: str) -> None:
        graph = None
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            self._cancel_active_children(oid)
            persisted = self.store.get_execution_graph(oid)
            if run.get("plan") and persisted["nodes"]:
                graph = ExecutionGraph(run.get("effective_plan") or run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(
                    utcnow(), "Orchestration time limit reached.",
                )
                self.store.save_execution_graph(oid, graph.serialize())
                self._close_terminal_attempts(oid, graph)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.timeout", "status": "Failed",
                    "message": "Orchestration time limit reached; active delegated tasks were cancelled.",
                    "summary": graph.summary(),
                })
        if graph is not None:
            # Use the ordinary graph failure path so a timeout receives the
            # same bounded root-cause report as every other terminal failure.
            self._finish_graph(oid, graph)
        else:
            self._fail_running(oid, "Orchestration time limit reached; active delegated tasks were cancelled.")

    def _abort_graph(self, oid: str, message: str) -> None:
        """Fail closed on an internal scheduler error without ghost-active nodes."""
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            self._cancel_active_children(oid)
            persisted = self.store.get_execution_graph(oid)
            if run.get("plan") and persisted["nodes"]:
                graph = ExecutionGraph(run.get("effective_plan") or run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(utcnow(), message)
                self.store.save_execution_graph(oid, graph.serialize())
                self._close_terminal_attempts(oid, graph)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.completed", "status": "Failed",
                    "message": "The execution graph stopped after an internal scheduler error.",
                    "summary": graph.summary(),
                })
            self._fail_running(oid, message)

    def _select_graph_task(self, oid: str, task: dict, run: dict) -> dict | None:
        """Create or reuse one dynamic agent, then validate it with AgentSelector."""
        planned_task_id = task["id"]
        with self.lock:
            current_run = self.store.get_orchestration(oid)
            if current_run["status"] != "Running":
                return None
            node = next(
                item for item in self.store.get_execution_graph(oid)["nodes"]
                if item["plan_task_id"] == planned_task_id
            )
            selection_attempt = int(node.get("attempt", 0)) + 1
            context = self._selection_context(current_run)
            required_agent_id = None
            excluded_agent_ids: list[str] = []
            recovery_workspace_state: dict[str, Any] = {}
            if node.get("recovery_action_id"):
                recovery_action = self.store.get_recovery(node["recovery_action_id"])
                recovery_workspace_state = dict(
                    (recovery_action.get("snapshot") or {}).get("workspace_state") or {}
                )
                prior = next((
                    item for item in self.store.list_execution_attempts(oid)
                    if item["plan_task_id"] == planned_task_id
                    and int(item["attempt"]) == int(recovery_action["source_attempt"])
                ), None)
                prior_agent_id = prior.get("selected_agent_id") if prior else None
                if recovery_action["action"] == "retry_same_agent":
                    required_agent_id = prior_agent_id
                elif recovery_action["action"] == "retry_different_agent":
                    prior_agent_ids = [
                        item.get("selected_agent_id")
                        for item in self.store.list_execution_attempts(oid)
                        if item["plan_task_id"] == planned_task_id
                    ]
                    excluded_agent_ids = list(dict.fromkeys([
                        *recovery_action.get("exclude_agent_ids", []),
                        *prior_agent_ids,
                    ]))
                    excluded_agent_ids = [item for item in excluded_agent_ids if item]
                    context["excluded_agent_ids"] = excluded_agent_ids

            try:
                if required_agent_id:
                    agents = [self.store.get_agent(required_agent_id)]
                else:
                    factory_task = dict(task)
                    if recovery_workspace_state:
                        factory_task["_recovery_workspace_state"] = recovery_workspace_state
                    created = self._create_dynamic_agent(
                        oid, factory_task, selection_attempt,
                        variant=max(0, selection_attempt - 1),
                    )
                    agents = [created["agent"]]
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                return {"error": "Dynamic agent creation or reuse failed: " + str(exc)}

            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selection.started", "status": "Running",
                "task_id": planned_task_id,
                "agent_id": agents[0].get("id") if agents else None,
                "message": (
                    "Freya is validating the task-specific agent against "
                    "capabilities, Skills, runtime tools and policy."
                ),
            })
        try:
            selection = self.selector.select_agent(task, agents, context)
            selected = selection.get("selected_agent_id")
            if ((required_agent_id and selected not in {None, required_agent_id})
                    or selected in excluded_agent_ids):
                raise ValueError("Agent selector violated semantic recovery constraints.")
            selection = dict(selection)
            selection["attempt"] = selection_attempt
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running":
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.agent_selection.failed", "status": "Failed",
                        "task_id": planned_task_id,
                        "message": "Agent selection failed: " + str(exc),
                    })
            return {"error": "Agent selection failed: " + str(exc)}
        with self.lock:
            if selection["selected_agent_id"] is None:
                selection["failure_reason"] = self._selection_failure_message(
                    selection, agents
                )
            selection_id = self.store.save_agent_selection(oid, selection)
            if selection_id is None:
                return None
            selected_agent_id = selection["selected_agent_id"]
            if selected_agent_id is None:
                failure_reason = selection["failure_reason"]
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selection.failed", "status": "Failed",
                    "task_id": planned_task_id,
                    "selector_version": selection["selector_version"],
                    "selection_id": selection_id,
                    "message": failure_reason,
                })
            else:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selected", "status": "Running",
                    "task_id": planned_task_id, "agent_id": selected_agent_id,
                    "selection_id": selection_id, "score": selection["score"],
                    "selector_version": selection["selector_version"],
                    "classification": selection["classification"],
                    "approval_required": selection["approval_required"],
                    "message": "Freya validated and selected the dynamic agent.",
                })
        return {"selection_id": selection_id, "selection": selection}

    def _record_graph_transitions(self, oid: str, transitions: list[dict]) -> None:
        for transition in transitions:
            task_id = transition["task_id"]
            if transition["to"] == "ready":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.ready", "status": "Running", "task_id": task_id,
                    "message": "All dependencies succeeded; the planned task is ready.",
                })
            elif transition["to"] == "blocked":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.blocked", "status": "Failed", "task_id": task_id,
                    "message": "The planned task was blocked by a failed dependency.",
                })

    def _evaluate_graph_node(self, oid: str, plan: dict, target: dict,
                             deadline: float) -> None:
        """Evaluate one technical success without holding the orchestration lock."""
        task_id = target["plan_task_id"]
        planned_task = next(task for task in plan["tasks"] if task["id"] == task_id)
        evaluation_id = str(uuid4())
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            current = ExecutionGraph(
                plan, self.store.get_execution_graph(oid)["nodes"],
            ).node(task_id)
            if (current["state"] != "evaluating" or current.get("evaluation_id")
                    or current.get("runtime_task_id") != target.get("runtime_task_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))):
                return
            runtime_task = self.store.get_task(target["runtime_task_id"])
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.evaluation.started", "status": "Running",
                "task_id": task_id, "plan_task_id": task_id,
                "agent_id": target.get("selected_agent_id"),
                "runtime_task_id": target.get("runtime_task_id"),
                "evaluation_id": evaluation_id,
                "evaluation_status": "evaluating",
                "evaluator_version": EVALUATOR_VERSION,
                "message": "Freya started evidence-first semantic evaluation.",
            })
        technical_error = None
        try:
            with self.evaluator_lock:
                outcome = self.evaluator.evaluate(
                    planned_task=planned_task, runtime_task=runtime_task,
                    execution_node=target,
                )
        except Exception as exc:
            technical_error = str(exc)
            outcome = technical_failure_evaluation(
                technical_error, list(planned_task.get("success_criteria") or []),
            )
            outcome.update(
                metrics=dict(getattr(self.evaluator, "metrics", {}) or {}),
                context_truncated=bool(
                    getattr(self.evaluator, "last_context", {}).get("context_truncated", False)
                ),
                deterministic=False,
                context_snapshot=dict(getattr(self.evaluator, "last_context", {}) or {}),
            )
        if self.clock() >= deadline:
            self._timeout(oid)
            return

        evaluation = {key: outcome[key] for key in EVALUATION_FIELDS if key in outcome}
        if outcome.get("status") == "error":
            evaluation = {key: outcome[key] for key in (
                "status", "confidence", "summary", "criteria", "issues",
                "missing_evidence", "recommended_action",
            )}
        metrics = dict(outcome.get("metrics") or {})
        snapshot = {
            "evaluator_version": EVALUATOR_VERSION,
            "input": outcome.get("context_snapshot") or {},
            "evaluation": evaluation,
        }
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
            current = graph.node(task_id)
            if (current["state"] != "evaluating" or current.get("evaluation_id")
                    or current.get("runtime_task_id") != target.get("runtime_task_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))):
                return
            graph.apply_evaluation(
                task_id, evaluation_id, evaluation["status"], evaluation["summary"], utcnow(),
            )
            record = self.store.commit_evaluation(
                evaluation_id, oid, task_id,
                runtime_task_id=target["runtime_task_id"],
                agent_id=target["selected_agent_id"], attempt=int(target["attempt"]),
                evaluator_version=EVALUATOR_VERSION, evaluation=evaluation,
                metrics=metrics, snapshot=snapshot,
                context_truncated=bool(outcome.get("context_truncated")),
                deterministic=bool(outcome.get("deterministic")),
            )
            if record is None:
                return
            event_type = ("freya.evaluation.failed" if technical_error
                          else "freya.evaluation.completed")
            self.store.add_orchestration_event(oid, {
                "event_type": event_type,
                "status": "Failed" if evaluation["status"] != "accepted" else "Success",
                "task_id": task_id, "plan_task_id": task_id,
                "agent_id": target.get("selected_agent_id"),
                "runtime_task_id": target.get("runtime_task_id"),
                "evaluation_id": evaluation_id,
                "evaluation_status": evaluation["status"],
                "evaluator_version": EVALUATOR_VERSION,
                "message": evaluation["summary"],
            })
            if evaluation["status"] == "accepted":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.succeeded", "status": "Success",
                    "task_id": task_id, "plan_task_id": task_id,
                    "agent_id": target.get("selected_agent_id"),
                    "runtime_task_id": target.get("runtime_task_id"),
                    "evaluation_id": evaluation_id,
                    "evaluation_status": evaluation["status"],
                    "message": "Semantic evaluation accepted the planned task.",
                })


    def _recover_graph_node(self, oid: str, plan: dict, target: dict,
                            deadline: float) -> None:
        """Decide and commit recovery without allowing a late result to reopen state."""
        task_id = target["plan_task_id"]
        planned_task = next(task for task in plan["tasks"] if task["id"] == task_id)
        evaluation = self.store.get_evaluation(target["evaluation_id"])
        previous_runtime_task = self.store.get_task(target.get("runtime_task_id")) if target.get("runtime_task_id") else None
        workspace_state = self._recovery_workspace_state(previous_runtime_task)
        recoveries = self.store.list_recoveries(oid)
        revisions = self.store.list_plan_revisions(oid)
        history = [item for item in recoveries
                   if item["plan_task_id"] == task_id]
        def recorded_model_calls(item: dict) -> int:
            value = (item.get("metrics") or {}).get("model_calls", 0)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        model_calls_used = sum(recorded_model_calls(item) for item in [*recoveries, *revisions])
        if len(recoveries) >= int(self.config["max_recovery_actions"]):
            reason = "Recovery action budget exhausted."
            with self.lock:
                if self.store.fail_recovery_pending(
                        oid, task_id, attempt=int(target["attempt"]),
                        evaluation_id=target["evaluation_id"], reason=reason):
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.recovery.exhausted", "status": "Failed",
                        "task_id": task_id, "evaluation_id": target["evaluation_id"],
                        "message": reason,
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.task.failed", "status": "Failed",
                        "task_id": task_id, "evaluation_id": target["evaluation_id"],
                        "message": reason,
                    })
            return
        recovery_id = str(uuid4())
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.recovery.started", "status": "Running",
                "task_id": task_id, "evaluation_id": target["evaluation_id"],
                "attempt": target["attempt"], "recovery_id": recovery_id,
                "message": "Freya started bounded semantic recovery.",
            })
        limits = {
            "max_semantic_attempts_per_task": self.config["max_semantic_attempts_per_task"],
            "max_plan_revisions": self.config["max_plan_revisions"],
            "max_recovery_actions": self.config["max_recovery_actions"],
            "max_recovery_model_calls": self.config["max_recovery_model_calls"],
            "recovery_action_count": len(recoveries),
            "plan_revision_count": len(revisions),
            "recovery_model_calls_used": model_calls_used,
        }
        try:
            current_agents = []
            current_agent_id = target.get("selected_agent_id")
            if current_agent_id:
                try:
                    current_agents = [self.store.get_agent(current_agent_id)]
                except KeyError:
                    current_agents = []
            with self.recovery_lock:
                decision = self.recovery.decide(
                    planned_task=planned_task, execution_node=target,
                    evaluation=evaluation, history=history,
                    available_agents=current_agents, plan=plan, limits=limits,
                    can_create_agent=True,
                    workspace_state=workspace_state,
                )
        except Exception as exc:
            decision = {
                "action": "fail", "reason": "Recovery decision failed strict validation: " + str(exc),
                "instructions": "", "exclude_agent_ids": [],
                "affected_task_ids": [task_id],
                "fingerprint": semantic_failure_fingerprint(
                    task_id, target.get("selected_agent_id") or "", evaluation,
                ),
                "metrics": dict(getattr(self.recovery, "metrics", {}) or {}),
            }
        if self.clock() >= deadline:
            self._timeout(oid)
            return
        allowed_scope: set[str] = set()
        protected_scope: set[str] = set()
        if decision["action"] == "replan_subgraph":
            graph_snapshot = self.store.get_execution_graph(oid)["nodes"]
            try:
                allowed_scope = allowed_replan_scope(
                    task_id, plan, graph_snapshot, self.store.list_execution_attempts(oid),
                )
                protected_scope = {item["plan_task_id"] for item in graph_snapshot} - allowed_scope
                requested = set(decision["affected_task_ids"])
                if task_id not in requested or not requested <= allowed_scope:
                    raise ValueError("Recovery requested tasks outside the safe replanning scope.")
            except ValueError as exc:
                decision = {
                    **decision, "action": "fail", "instructions": "",
                    "exclude_agent_ids": [], "affected_task_ids": [task_id],
                    "reason": str(exc),
                }
        retry_prompt = ""
        if decision["action"] in {"retry_same_agent", "retry_different_agent"}:
            retry_prompt = build_retry_prompt(
                planned_task, evaluation, decision["instructions"],
                attempt=int(target["attempt"]) + 1,
                workspace_state=workspace_state,
            )
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            current = next(item for item in self.store.get_execution_graph(oid)["nodes"]
                           if item["plan_task_id"] == task_id)
            if (current["state"] != "recovery_pending"
                    or current.get("evaluation_id") != target.get("evaluation_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))
                    or current.get("recovery_action_id")):
                return
            record = self.store.commit_recovery_action(
                recovery_id, oid, task_id, source_attempt=int(target["attempt"]),
                source_evaluation_id=target["evaluation_id"], decision=decision,
                recovery_version=RECOVERY_VERSION, prompt=retry_prompt,
                snapshot={"evaluation_status": evaluation.get("status"), "limits": limits,
                          "allowed_replan_scope": sorted(allowed_scope),
                          "workspace_state": workspace_state},
            )
            if record is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.recovery.decided", "status": "Running",
                "task_id": task_id, "recovery_id": recovery_id,
                "action": decision["action"], "reason": decision["reason"],
                "workspace_state": workspace_state,
                "selected_strategy": decision["action"],
                "message": "Freya committed a bounded recovery decision.",
            })
            if decision["action"] == "fail":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.exhausted", "status": "Failed",
                    "task_id": task_id, "recovery_id": recovery_id,
                    "message": decision["reason"],
                })
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.failed", "status": "Failed",
                    "task_id": task_id, "evaluation_id": target["evaluation_id"],
                    "message": decision["reason"],
                })
                return
            if decision["action"] in {"retry_same_agent", "retry_different_agent"}:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.retry_scheduled", "status": "Running",
                    "task_id": task_id, "recovery_id": recovery_id,
                    "action": decision["action"], "next_attempt": int(target["attempt"]) + 1,
                    "message": "Freya scheduled a new, independently selected execution attempt.",
                })
                return

        try:
            accepted = {item["plan_task_id"] for item in self.store.get_execution_graph(oid)["nodes"]
                        if item["state"] == "success"}
            historical = {item["plan_task_id"] for item in self.store.get_execution_graph(oid)["nodes"]}
            with self.recovery_lock:
                revision = self.replanner.create_revision(
                    current_plan=plan, source_task_id=task_id,
                    affected_task_ids=decision["affected_task_ids"],
                    allowed_task_ids=allowed_scope, protected_task_ids=protected_scope,
                    accepted_task_ids=accepted, historical_task_ids=historical,
                    context={"evaluation": evaluation, "instructions": decision["instructions"]},
                    max_tasks=int(self.config["max_delegated_tasks"]),
                    max_model_calls=min(
                        2, int(self.config["max_recovery_model_calls"])
                        - model_calls_used - recorded_model_calls(decision)
                    ),
                )
            if self.clock() >= deadline:
                self._timeout(oid)
                return
            revision_id = str(uuid4())
            with self.lock:
                run = self.store.get_orchestration(oid)
                current = next(item for item in self.store.get_execution_graph(oid)["nodes"]
                               if item["plan_task_id"] == task_id)
                if (run["status"] != "Running" or current["state"] != "recovery_pending"
                        or current.get("recovery_action_id") != recovery_id):
                    return
                saved = self.store.commit_plan_revision(
                    revision_id, oid, recovery_id, summary=revision["summary"],
                    plan=revision["plan"], superseded_task_ids=revision["superseded_task_ids"],
                    metrics=revision.get("metrics"),
                )
                if saved is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.replan_created", "status": "Running",
                    "task_id": task_id, "recovery_id": recovery_id,
                    "plan_revision_id": revision_id, "revision": saved["revision"],
                    "message": "Freya committed a validated effective-plan revision.",
                })
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running" and self.store.fail_recovery_action(
                        recovery_id, "Subgraph replanning failed: " + str(exc)):
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.recovery.exhausted", "status": "Failed",
                        "task_id": task_id, "recovery_id": recovery_id,
                        "message": "Subgraph replanning failed: " + str(exc),
                    })

    def _finish_graph(self, oid: str, graph: ExecutionGraph) -> None:
        summary = graph.summary()
        unsuccessful = (summary["failed"] + summary["blocked"] + summary["cancelled"]
                        + summary["skipped"])
        if unsuccessful:
            semantic_failures = [
                node for node in graph.serialize()
                if node.get("evaluation_status") not in (None, "accepted")
            ]
            if semantic_failures:
                message = (
                    "Execution completed, but semantic evaluation rejected one or more "
                    "planned tasks. "
                )
            else:
                message = "Execution graph completed unsuccessfully. "
            message += (
                f"{summary['failed']} failed, {summary['blocked']} blocked, "
                f"{summary['cancelled']} cancelled and {summary['skipped']} skipped nodes."
            )
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.failure_analysis.started", "status": "Running",
                "analysis_version": FAILURE_ANALYSIS_VERSION,
                "message": "Freya started one bounded review using persisted sanitized logs only.",
            })
            diagnosis, analysis_metrics, analysis_mode = self._analyze_graph_failure(oid, graph)
            report = (
                "Failure diagnosis\n"
                f"Cause: {diagnosis['cause']}\n"
                f"Evidence logs: {', '.join(diagnosis['evidence_log_ids'])}\n"
                f"Retryable: {'yes' if diagnosis['retryable'] else 'no'}\n"
                f"Recommended action: {diagnosis['recommended_action']}"
            )
            message += " Cause: " + diagnosis["cause"]
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.failure_analysis.completed", "status": "Success",
                    "analysis_version": FAILURE_ANALYSIS_VERSION,
                    "analysis_mode": analysis_mode,
                    "cause": diagnosis["cause"],
                    "evidence_log_ids": diagnosis["evidence_log_ids"],
                    "retryable": diagnosis["retryable"],
                    "recommended_action": diagnosis["recommended_action"],
                    "metrics": analysis_metrics,
                    "message": report,
                })
                completed = self.store.transition_orchestration(
                    oid, ("Running",), "Failed", error=message, response=report,
                )
            status = "Failed"
        else:
            raise ValueError("Successful graphs must pass through global integration.")
        if completed is None:
            return
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.graph.completed", "status": status,
            "message": "The execution graph reached a terminal state.", "summary": summary,
        })
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.completed" if status == "Success" else "freya.failed",
            "status": status, "message": message,
        })

        self._archive_dynamic_agents(oid)

    def _run_graph(self, oid: str, running: dict, deadline: float,
                   operational_prompt: str = "") -> None:
        operational_prompt = (
            str(operational_prompt).strip()
            or str((running.get("plan") or {}).get("goal") or "").strip()
        )
        if not operational_prompt:
            raise ValueError("Execution graph has no Task Analyst operational prompt.")
        while True:
            if self.clock() >= deadline:
                self._timeout(oid)
                return

            selection_target = None
            evaluation_target = None
            recovery_target = None
            completed_graph = None
            with self.lock:
                run = self.store.get_orchestration(oid)
                if run["status"] != "Running":
                    return
                persisted = self.store.get_execution_graph(oid)
                plan = run.get("effective_plan") or run["plan"]
                graph = ExecutionGraph(plan, persisted["nodes"])
                changed = False

                for node in graph.active_nodes():
                    if node["state"] == "evaluating":
                        continue
                    runtime_task = self.store.get_task(node["runtime_task_id"])
                    previous = node["state"]
                    if graph.apply_runtime_status(
                            node["plan_task_id"], runtime_task["status"],
                            result=runtime_task.get("result"), error=runtime_task.get("error"),
                            timestamp=utcnow()):
                        changed = True
                    if node.get("delegation_id"):
                        self._snapshot_delegation(node["delegation_id"], runtime_task)
                    current = graph.node(node["plan_task_id"])["state"]
                    self.store.update_execution_attempt(
                        oid, node["plan_task_id"], int(node.get("attempt", 0)),
                        status=current,
                    )
                    if current != previous:
                        event_type = {
                            "waiting_for_approval": "freya.task.waiting_for_approval",
                            "failed": "freya.task.failed",
                            "cancelled": "freya.task.failed",
                        }.get(current)
                        if event_type:
                            event_message = f"Planned task entered {current}."
                            event_error = ""
                            if current == "failed" and runtime_task.get("error"):
                                event_error = str(runtime_task["error"])
                                event_message += " Cause: " + event_error
                            self.store.add_orchestration_event(oid, {
                                "event_type": event_type,
                                "status": runtime_task["status"],
                                "task_id": node["plan_task_id"],
                                "agent_id": node.get("selected_agent_id"),
                                "runtime_task_id": node.get("runtime_task_id"),
                                "message": event_message,
                                "error": event_error,
                            })

                transitions = graph.refresh_dependencies(utcnow())
                if transitions:
                    changed = True
                    self._record_graph_transitions(oid, transitions)
                if changed:
                    self.store.save_execution_graph(oid, graph.serialize())

                if graph.summary()["complete"]:
                    completed_graph = graph

                for node in graph.serialize():
                    if node["state"] == "recovery_pending" and not node.get("recovery_action_id"):
                        recovery_target = node
                        break

                if recovery_target is None:
                    for node in graph.serialize():
                        if node["state"] == "evaluating" and not node.get("evaluation_id"):
                            evaluation_target = node
                            break


                if recovery_target is None and evaluation_target is None:
                    for task in graph.ready_tasks():
                        if not graph.node(task["id"]).get("selection_id"):
                            selection_target = task
                            break

            if completed_graph is not None:
                self._complete_or_integrate(oid, completed_graph, deadline)
                return

            if evaluation_target is not None:
                self._evaluate_graph_node(oid, plan, evaluation_target, deadline)
                continue

            if recovery_target is not None:
                self._recover_graph_node(oid, plan, recovery_target, deadline)
                continue

            if selection_target is not None:
                selected = self._select_graph_task(oid, selection_target, run)
                if selected is None:
                    return
                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Running":
                        return
                    graph = ExecutionGraph(
                        plan, self.store.get_execution_graph(oid)["nodes"],
                    )
                    node = graph.node(selection_target["id"])
                    if node["state"] != "ready" or node.get("selection_id"):
                        continue
                    if selected.get("error"):
                        graph.mark_failed(selection_target["id"], selected["error"], utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": selection_target["id"], "message": selected["error"],
                        })
                    else:
                        selection = selected["selection"]
                        if selection["selected_agent_id"] is None:
                            node["selection_id"] = selected["selection_id"]
                            failure_reason = selection.get("failure_reason") or (
                                "No eligible agent is available for this planned task."
                            )
                            graph.mark_failed(
                                selection_target["id"], failure_reason, utcnow(),
                            )
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.task.failed", "status": "Failed",
                                "task_id": selection_target["id"],
                                "message": failure_reason,
                                "error": failure_reason,
                            })
                        else:
                            graph.mark_selected(
                                selection_target["id"], selection["selected_agent_id"],
                                selected["selection_id"], utcnow(),
                            )
                    self.store.save_execution_graph(oid, graph.serialize())

            dispatched = 0
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    return
                graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
                active_nodes = graph.active_nodes()
                slots = max(0, int(self.config["max_parallel_tasks"]) - len(active_nodes))
                active_agents = {node.get("selected_agent_id") for node in active_nodes}
                active_agents.update(
                    task.get("agent_id") for task in self.store.list_tasks(limit=10000)
                    if task.get("status") in ACTIVE_DELEGATED_TASK_STATUSES
                )
                for task in graph.ready_tasks():
                    if slots <= 0:
                        break
                    node = graph.node(task["id"])
                    agent_id = node.get("selected_agent_id")
                    if not node.get("selection_id") or not agent_id:
                        continue
                    try:
                        agent = self.store.get_agent(agent_id)
                    except KeyError:
                        graph.mark_failed(task["id"], "Selected agent no longer exists.", utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": "Selected agent no longer exists.",
                        })
                        continue
                    if agent.get("enabled") is not True:
                        message = "Selected agent became disabled after selection."
                        graph.mark_failed(task["id"], message, utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": message,
                        })
                        continue
                    if agent.get("status") in {"Paused", "Offline"}:
                        graph.set_waiting_reason(
                            task["id"], f"Selected agent is {agent['status']}.", utcnow(),
                        )
                        continue
                    if agent_id in active_agents:
                        graph.set_waiting_reason(
                            task["id"], "Selected agent is executing another task.", utcnow(),
                        )
                        continue
                    execution_prompt = self._execution_prompt(
                        operational_prompt, task, node.get("attempt_prompt") or "",
                    )
                    try:
                        runtime_task = self.runtime.submit(
                            agent_id, execution_prompt,
                            self._workspace_for_run(run),
                        )
                    except ValueError as exc:
                        refreshed = self.store.get_agent(agent_id)
                        if refreshed.get("status") in {"Paused", "Offline"}:
                            graph.set_waiting_reason(
                                task["id"], f"Selected agent is {refreshed['status']}.", utcnow(),
                            )
                        else:
                            graph.mark_failed(task["id"], str(exc), utcnow())
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.task.failed", "status": "Failed",
                                "task_id": task["id"], "agent_id": agent_id,
                                "message": "Runtime submission failed: " + str(exc),
                            })
                        continue
                    except Exception as exc:
                        graph.mark_failed(task["id"], str(exc), utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": "Runtime submission failed: " + str(exc),
                        })
                        continue
                    try:
                        delegation_id = self.store.add_delegation(
                            oid, agent_id, execution_prompt, runtime_task["id"],
                        )
                        if delegation_id is None:
                            self.runtime.cancel(runtime_task["id"])
                            return
                        graph.mark_running(
                            task["id"], runtime_task["id"], delegation_id, utcnow(),
                        )
                        # Persist the dispatch before another selection can run.
                        # If any post-submit step fails, cancel the created task
                        # and fail closed instead of leaving a ready duplicate.
                        self.store.save_execution_graph(oid, graph.serialize())
                        attempt_record = self.store.record_execution_attempt(
                            oid, task["id"], selected_agent_id=agent_id,
                            selection_id=node["selection_id"],
                            runtime_task_id=runtime_task["id"], delegation_id=delegation_id,
                            attempt=int(graph.node(task["id"])["attempt"]),
                            prompt=execution_prompt,
                            recovery_action_id=graph.node(task["id"]).get("recovery_action_id"),
                        )
                        if attempt_record is None:
                            raise RuntimeError("Execution attempt persistence lost its precondition.")
                    except Exception:
                        try:
                            self.runtime.cancel(runtime_task["id"])
                        except (KeyError, ValueError):
                            pass
                        raise
                    active_agents.add(agent_id)
                    slots -= 1
                    dispatched += 1
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.task.dispatched", "status": runtime_task["status"],
                        "task_id": task["id"], "runtime_task_id": runtime_task["id"],
                        "agent_id": agent_id, "selection_id": node["selection_id"],
                        "message": "Ready planned task was dispatched exactly once.",
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.delegated", "status": runtime_task["status"],
                        "task_id": runtime_task["id"], "planned_task_id": task["id"],
                        "agent_id": agent_id, "selection_id": node["selection_id"],
                        "message": "Delegated objective to agent.",
                    })
                self.store.save_execution_graph(oid, graph.serialize())

            if self.clock() >= deadline:
                self._timeout(oid)
                return
            # Interleave selection and dispatch. A newly dispatched task is now
            # visible to the next AgentSelector workload calculation, so fill
            # remaining slots before yielding to the polling wait.
            if dispatched and slots > 0:
                continue
            self.wait(min(.2, max(0, deadline - self.clock())))

    def _run(self, oid):
        # Explicitly injected decision functions retain the pre-4.3 test and
        # extension contract. The production path is the durable DAG scheduler.
        if self.decide is not None:
            return self._run_legacy(oid)
        started = self.clock()
        deadline = started + float(self.config["max_wallclock_seconds"])
        with self.lock:
            planning = self.store.transition_orchestration(oid, ("Queued",), "Planning")
            if planning is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.started", "status": "Planning",
                "message": "Freya is creating a structured plan.",
            })
        planning_metrics: dict = {}
        try:
            analysis, analysis_metrics = self._analyze_prompt(oid, planning["prompt"])
            # A Task Analyst call may be in flight when cancellation arrives;
            # never start a planner call after the run has left Planning.
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Planning":
                    return
            context = self._planning_context()
            if analysis is None:
                raise ValueError("Task Analyst did not produce an operational prompt.")
            context["task_analysis"] = analysis
            operational_prompt = str(analysis.get("operational_prompt") or "").strip()
            if not operational_prompt:
                raise ValueError("Task Analyst operational prompt is empty.")
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(operational_prompt, context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
            planning_metrics["task_analysis"] = {
                key: value for key, value in analysis_metrics.items()
                if key in {"mode", "model_calls", "prompt_tokens", "generated_tokens",
                           "total_tokens", "duration_seconds"}
            }
            if len(plan["tasks"]) > int(self.config["max_delegated_tasks"]):
                raise ValueError(
                    f"Plan contains {len(plan['tasks'])} tasks but max_delegated_tasks is "
                    f"{self.config['max_delegated_tasks']}.",
                )
        except Exception as exc:
            self._fail_planning(oid, exc, planning_metrics)
            return

        try:
            with self.lock:
                planned = self.store.save_orchestration_plan(
                    oid, plan, PLAN_SCHEMA_VERSION, planning_metrics,
                )
                if planned is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.created", "status": "Planned",
                    "message": "Freya created and saved the structured plan.",
                    "goal": plan["goal"], "complexity": plan["complexity"],
                    "task_count": len(plan["tasks"]),
                    "task_ids": [task["id"] for task in plan["tasks"]],
                    "plan_schema_version": PLAN_SCHEMA_VERSION,
                    "planning_metrics": planning_metrics,
                })
                self._workspace_for_run(planned)
                graph = ExecutionGraph(plan)
                self.store.initialize_execution_graph(oid, graph.serialize())
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.initialized", "status": "Planned",
                    "message": "Freya initialized the durable execution graph.",
                    "summary": graph.summary(),
                })
                self._record_graph_transitions(oid, [
                    {"task_id": task["id"], "from": "pending", "to": "ready"}
                    for task in graph.ready_tasks()
                ])
                running = self.store.transition_orchestration(oid, ("Planned",), "Running")
                if running is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.analyzing", "status": "Running",
                    "message": "Freya is scheduling ready tasks from the execution graph.",
                })
            self._run_graph(oid, running, deadline, operational_prompt)
        except Exception as exc:
            self._abort_graph(oid, str(exc))

    def _run_legacy(self, oid):
        started = self.clock()
        deadline = started + float(self.config["max_wallclock_seconds"])
        results: list[dict] = []
        delegated = 0

        with self.lock:
            planning = self.store.transition_orchestration(oid, ("Queued",), "Planning")
            if planning is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.started", "status": "Planning",
                "message": "Freya is creating a structured plan.",
            })
        planning_metrics: dict = {}
        try:
            analysis, analysis_metrics = self._analyze_prompt(oid, planning["prompt"])
            if analysis is None:
                raise ValueError("Task Analyst did not produce an operational prompt.")
            operational_prompt = str(analysis.get("operational_prompt") or "").strip()
            agents = self.store.list_agents()
            context = self._planning_context(agents)
            context["task_analysis"] = analysis
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(operational_prompt, context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
            planning_metrics["task_analysis"] = {
                key: value for key, value in analysis_metrics.items()
                if key in {"mode", "model_calls", "prompt_tokens", "generated_tokens",
                           "total_tokens", "duration_seconds"}
            }
        except Exception as exc:
            self._fail_planning(oid, exc, planning_metrics)
            return

        with self.lock:
            planned = self.store.save_orchestration_plan(
                oid, plan, PLAN_SCHEMA_VERSION, planning_metrics,
            )
            if planned is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.plan.created", "status": "Planned",
                "message": "Freya created and saved the structured plan.",
                "goal": plan["goal"], "complexity": plan["complexity"],
                "task_count": len(plan["tasks"]),
                "task_ids": [task["id"] for task in plan["tasks"]],
                "plan_schema_version": PLAN_SCHEMA_VERSION,
                "planning_metrics": planning_metrics,
            })
            running = self.store.transition_orchestration(oid, ("Planned",), "Running")
            if running is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.analyzing", "status": "Running",
                "message": "Freya is selecting how to handle the planned work.",
            })

        try:
            for _round in range(int(self.config["max_rounds"])):
                if self.clock() >= deadline:
                    self._timeout(oid)
                    return
                if self.decide:
                    decision = self._decision(operational_prompt, self.store.list_agents(), results)
                else:
                    # Compatibility path for explicitly injected legacy decision hooks.
                    selected_task = self._select_planned_task(
                        oid, running["plan"]["tasks"][0], running,
                    )
                    if selected_task is None:
                        return
                    decision = {"action": "delegate", "tasks": [selected_task]}
                action = decision.get("action") if isinstance(decision, dict) else None
                if action == "respond":
                    with self.lock:
                        message = str(decision.get("message", ""))
                        completed = self.store.transition_orchestration(
                            oid, ("Running",), "Success", response=message,
                            legacy_without_graph=True,
                        )
                        if completed is not None:
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.completed", "status": "Success", "message": message,
                            })
                    return
                if action not in {"delegate", "continue"}:
                    raise ValueError("Freya returned an invalid action.")
                tasks = decision.get("tasks", [])
                if not isinstance(tasks, list) or delegated + len(tasks) > self.config["max_delegated_tasks"]:
                    raise ValueError("Delegation limit reached.")
                for item in tasks:
                    with self.lock:
                        if self.clock() >= deadline:
                            self._timeout(oid)
                            return
                        if self.store.get_orchestration(oid)["status"] != "Running":
                            return
                        agent_id = item.get("agent_id")
                        objective = str(item.get("objective", "")).strip()
                        agent = self.store.get_agent(agent_id)
                        if agent.get("enabled") is not True:
                            raise ValueError("Selected agent is disabled or unavailable.")
                        execution_prompt = self._execution_prompt(operational_prompt, item, objective)
                        task = self.runtime.submit(
                            agent_id, execution_prompt, self._workspace_for_run(running),
                        )
                        delegation_id = self.store.add_delegation(
                            oid, agent_id, execution_prompt, task["id"],
                        )
                        if delegation_id is None:
                            self.runtime.cancel(task["id"])
                            return
                        delegated += 1
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.delegated", "status": "Queued", "agent_id": agent_id,
                            "task_id": task["id"], "planned_task_id": item.get("planned_task_id"),
                            "selection_id": item.get("selection_id"),
                            "message": "Delegated objective to agent.",
                        })
                        results.append({"delegation_id": delegation_id, "task_id": task["id"],
                                        "agent_id": agent_id})

                while True:
                    if self.store.get_orchestration(oid)["status"] == "Cancelled":
                        return
                    active = [item for item in results
                              if self.store.get_task(item["task_id"])["status"] in
                              ACTIVE_DELEGATED_TASK_STATUSES]
                    if not active:
                        break
                    if self.clock() >= deadline:
                        self._timeout(oid)
                        return
                    self.wait(min(.2, max(0, deadline - self.clock())))

                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Running":
                        return
                    child_tasks = []
                    for item in results:
                        child = self.store.get_task(item["task_id"])
                        self._snapshot_delegation(item["delegation_id"], child)
                        item["status"] = child["status"]
                        item["result"] = child.get("result")
                        child_tasks.append(child)
                    failed_ids = [task["id"] for task in child_tasks if task["status"] == "Failed"]
                    cancelled_ids = [task["id"] for task in child_tasks if task["status"] == "Cancelled"]
                    if failed_ids or cancelled_ids:
                        detail = ("Delegated task failure: " + ", ".join(failed_ids) if failed_ids else
                                  "Delegated task cancellation: " + ", ".join(cancelled_ids))
                        self._fail_running(oid, detail)
                        return
                    if any(task["status"] != "Success" for task in child_tasks):
                        raise RuntimeError("A delegated task ended in an unknown non-success state.")
                    if action == "delegate" and results:
                        message = "Freya completed the delegated work.\n\n" + "\n\n".join(
                            str(item.get("result") or item.get("status")) for item in results
                        )
                        completed = self.store.transition_orchestration(
                            oid, ("Running",), "Success", response=message,
                            legacy_without_graph=True,
                        )
                        if completed is not None:
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.completed", "status": "Success",
                                "message": "Freya integrated successful agent results.",
                            })
                        return
            raise RuntimeError("Maximum orchestration rounds reached.")
        except Exception as exc:
            self._fail_running(oid, str(exc))
