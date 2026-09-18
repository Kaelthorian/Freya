"""Freya orchestration service: structured planning, delegation and integration."""
from __future__ import annotations

import threading
import time
from typing import Callable
from uuid import uuid4

from .agent_context import build_effective_agent, capability_summary
from .agent_selector import AgentSelector
from .capabilities import capability_catalog
from .execution_graph import ExecutionGraph
from .evaluator import EVALUATION_FIELDS, EVALUATOR_VERSION, Evaluator, technical_failure_evaluation
from .planner import MAX_GOAL_CHARS, MAX_PLAN_TASKS, PLAN_SCHEMA_VERSION, Planner
from .skills import skill_summary
from .storage import ORCHESTRATION_ACTIVE_STATUSES, ORCHESTRATION_TERMINAL_STATUSES, utcnow


ACTIVE_DELEGATED_TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused"}


class Orchestrator:
    def __init__(self, store, runtime, decide: Callable | None = None, config: dict | None = None,
                 planner: Planner | None = None, clock: Callable[[], float] | None = None,
                 wait: Callable[[float], None] | None = None,
                 selector: AgentSelector | None = None,
                 evaluator: Evaluator | None = None):
        self.store, self.runtime = store, runtime
        self.decide = decide
        self.planner = planner or Planner()
        self.selector = selector or AgentSelector()
        # Compatibility fallback is evidence-only: it cannot accept Runtime
        # Success unless configured objective verification actually passed.
        self.evaluator = evaluator or Evaluator(offline=True)
        self.clock = clock or time.monotonic
        self.wait = wait or time.sleep
        self.lock = threading.RLock()
        self.planner_lock = threading.Lock()
        self.evaluator_lock = threading.Lock()
        self.config = {"max_rounds": 6, "max_delegated_tasks": MAX_PLAN_TASKS,
                       "max_parallel_tasks": 4, "max_model_calls": 12,
                       "max_wallclock_seconds": 900}
        self.config.update(config or {})
        for field in ("max_delegated_tasks", "max_parallel_tasks"):
            value = self.config[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer.")
        self.config["max_parallel_tasks"] = min(
            self.config["max_parallel_tasks"], self.config["max_delegated_tasks"],
        )

    def submit(self, prompt: str, workspace_path: str | None = None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > MAX_GOAL_CHARS:
            raise ValueError(f"Orchestration prompt must contain 1-{MAX_GOAL_CHARS} characters.")
        config = {**self.config, "workspace_path": workspace_path or ""}
        run = self.store.create_orchestration(prompt.strip(), config)
        threading.Thread(target=self._run, args=(run["id"],), daemon=True,
                         name="freya-orchestrator").start()
        return run

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
                    graph = ExecutionGraph(cancelled["plan"], persisted["nodes"])
                    graph.cancel_nonterminal(utcnow(), "Cancelled by user.")
                    self.store.save_execution_graph(oid, graph.serialize())
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.graph.completed", "status": "Cancelled",
                        "message": "The execution graph was cancelled by the user.",
                        "summary": graph.summary(),
                    })
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

    def _planning_context(self, agents) -> dict:
        # Deliberately compact: no Skill procedures, logs, task output, policy
        # secrets or model transcripts cross the planning boundary.
        skills = []
        if self.store is not None:
            skills = [{key: item.get(key) for key in ("id", "name", "category", "tags")}
                      for item in self.store.list_skills(enabled=True)[:200]]
        planner_agents = []
        for candidate in self._agent_candidates(agents)[:100]:
            planner_agents.append({
                key: candidate.get(key) for key in
                ("id", "name", "role", "purpose", "description", "availability", "enabled")
            })
            planner_agents[-1]["skills"] = [
                {key: skill.get(key) for key in ("id", "name", "category", "tags")}
                for skill in candidate.get("skills", [])
            ]
            planner_agents[-1]["capabilities_summary"] = candidate.get("capabilities_summary", "")
        return {
            "capabilities": [{key: item[key] for key in ("id", "category", "description")}
                             for item in capability_catalog()],
            "agents": planner_agents,
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

    def _fail_running(self, oid: str, message: str) -> None:
        with self.lock:
            failed = self.store.transition_orchestration(oid, ("Running",), "Failed", error=message)
            if failed is not None:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.failed", "status": "Failed", "message": message,
                })

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

    def _timeout(self, oid: str) -> None:
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            self._cancel_active_children(oid)
            persisted = self.store.get_execution_graph(oid)
            if run.get("plan") and persisted["nodes"]:
                graph = ExecutionGraph(run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(
                    utcnow(), "Orchestration time limit reached.",
                )
                self.store.save_execution_graph(oid, graph.serialize())
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.completed", "status": "Failed",
                    "message": "The execution graph reached its time limit.",
                    "summary": graph.summary(),
                })
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
                graph = ExecutionGraph(run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(utcnow(), message)
                self.store.save_execution_graph(oid, graph.serialize())
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.completed", "status": "Failed",
                    "message": "The execution graph stopped after an internal scheduler error.",
                    "summary": graph.summary(),
                })
            self._fail_running(oid, message)

    def _select_graph_task(self, oid: str, task: dict, run: dict) -> dict | None:
        """Select once for a ready node; cancellation may safely win while ranking."""
        planned_task_id = task["id"]
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return None
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selection.started", "status": "Running",
                "task_id": planned_task_id,
                "message": "Freya is ranking existing agents for the ready planned task.",
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
            return {"error": "Agent selection failed: " + str(exc)}
        with self.lock:
            selection_id = self.store.save_agent_selection(oid, selection)
            if selection_id is None:
                return None
            selected_agent_id = selection["selected_agent_id"]
            if selected_agent_id is None:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selection.failed", "status": "Failed",
                    "task_id": planned_task_id, "selector_version": selection["selector_version"],
                    "selection_id": selection_id,
                    "message": "No eligible or conditional agent is available for the planned task.",
                })
            else:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selected", "status": "Running",
                    "task_id": planned_task_id, "agent_id": selected_agent_id,
                    "selection_id": selection_id, "score": selection["score"],
                    "selector_version": selection["selector_version"],
                    "classification": selection["classification"],
                    "approval_required": selection["approval_required"],
                    "message": "Freya selected an existing agent for the ready planned task.",
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
            self.store.add_orchestration_event(oid, {
                "event_type": ("freya.task.succeeded" if evaluation["status"] == "accepted"
                               else "freya.task.failed"),
                "status": "Success" if evaluation["status"] == "accepted" else "Failed",
                "task_id": task_id, "plan_task_id": task_id,
                "agent_id": target.get("selected_agent_id"),
                "runtime_task_id": target.get("runtime_task_id"),
                "evaluation_id": evaluation_id,
                "evaluation_status": evaluation["status"],
                "message": ("Semantic evaluation accepted the planned task."
                            if evaluation["status"] == "accepted" else evaluation["summary"]),
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
            completed = self.store.transition_orchestration(
                oid, ("Running",), "Failed", error=message,
            )
            status = "Failed"
        else:
            rendered = []
            for node in graph.serialize():
                if node.get("result") is not None:
                    rendered.append(f"{node['plan_task_id']}: {node['result']}")
            response = f"Freya completed and verified all {summary['total']} planned tasks."
            if rendered:
                response += "\n\n" + "\n\n".join(rendered)
            completed = self.store.transition_orchestration(
                oid, ("Running",), "Success", response=response,
            )
            message, status = response, "Success"
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

    def _run_graph(self, oid: str, running: dict, deadline: float) -> None:
        plan = running["plan"]
        while True:
            if self.clock() >= deadline:
                self._timeout(oid)
                return

            selection_target = None
            evaluation_target = None
            with self.lock:
                run = self.store.get_orchestration(oid)
                if run["status"] != "Running":
                    return
                persisted = self.store.get_execution_graph(oid)
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
                    if current != previous:
                        event_type = {
                            "waiting_for_approval": "freya.task.waiting_for_approval",
                            "failed": "freya.task.failed",
                            "cancelled": "freya.task.failed",
                        }.get(current)
                        if event_type:
                            self.store.add_orchestration_event(oid, {
                                "event_type": event_type,
                                "status": runtime_task["status"],
                                "task_id": node["plan_task_id"],
                                "agent_id": node.get("selected_agent_id"),
                                "runtime_task_id": node.get("runtime_task_id"),
                                "message": f"Planned task entered {current}.",
                            })

                transitions = graph.refresh_dependencies(utcnow())
                if transitions:
                    changed = True
                    self._record_graph_transitions(oid, transitions)
                if changed:
                    self.store.save_execution_graph(oid, graph.serialize())

                if graph.summary()["complete"]:
                    self._finish_graph(oid, graph)
                    return

                for node in graph.serialize():
                    if node["state"] == "evaluating" and not node.get("evaluation_id"):
                        evaluation_target = node
                        break

                if evaluation_target is None:
                    for task in graph.ready_tasks():
                        if not graph.node(task["id"]).get("selection_id"):
                            selection_target = task
                            break

            if evaluation_target is not None:
                self._evaluate_graph_node(oid, plan, evaluation_target, deadline)
                continue

            if selection_target is not None:
                selected = self._select_graph_task(oid, selection_target, running)
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
                            graph.mark_failed(
                                selection_target["id"],
                                "No eligible agent is available for this planned task.", utcnow(),
                            )
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.task.failed", "status": "Failed",
                                "task_id": selection_target["id"],
                                "message": "No eligible agent is available for this planned task.",
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
                    try:
                        runtime_task = self.runtime.submit(
                            agent_id, task["objective"],
                            running.get("config", {}).get("workspace_path") or None,
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
                            oid, agent_id, task["objective"], runtime_task["id"],
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
            context = self._planning_context(self.store.list_agents())
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(planning["prompt"], context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
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
            self._run_graph(oid, running, deadline)
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
            agents = self.store.list_agents()
            context = self._planning_context(agents)
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(planning["prompt"], context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
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
                    decision = self._decision(running["prompt"], self.store.list_agents(), results)
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
                        task = self.runtime.submit(
                            agent_id, objective, running.get("config", {}).get("workspace_path") or None,
                        )
                        delegation_id = self.store.add_delegation(
                            oid, agent_id, objective, task["id"],
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
