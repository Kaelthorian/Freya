"""Freya orchestration service: structured planning, delegation and integration."""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from .agent_context import build_effective_agent, capability_summary
from .capabilities import capability_catalog
from .planner import MAX_GOAL_CHARS, PLAN_SCHEMA_VERSION, Planner
from .skills import skill_summary
from .storage import ORCHESTRATION_ACTIVE_STATUSES, ORCHESTRATION_TERMINAL_STATUSES


ACTIVE_DELEGATED_TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused"}


class Orchestrator:
    def __init__(self, store, runtime, decide: Callable | None = None, config: dict | None = None,
                 planner: Planner | None = None, clock: Callable[[], float] | None = None,
                 wait: Callable[[float], None] | None = None):
        self.store, self.runtime = store, runtime
        self.decide = decide
        self.planner = planner or Planner()
        self.clock = clock or time.monotonic
        self.wait = wait or time.sleep
        self.lock = threading.RLock()
        self.planner_lock = threading.Lock()
        self.config = {"max_rounds": 6, "max_delegated_tasks": 8, "max_model_calls": 12,
                       "max_wallclock_seconds": 900}
        self.config.update(config or {})

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
        enabled = [agent for agent in candidates if agent.get("enabled") is True]
        words = set(re.findall(r"[a-z0-9_+-]{3,}", str(prompt).casefold()))

        def score(candidate):
            value = 0
            for skill in candidate.get("skills", []):
                haystack = " ".join([skill.get("name", ""), skill.get("category", ""),
                                     *skill.get("tags", [])]).casefold()
                value += sum(2 if word in haystack else 0 for word in words)
                value += 1 if skill.get("operational") else 0
            return value

        enabled.sort(key=lambda candidate: -score(candidate))
        if not enabled:
            return {"action": "respond", "message": "No enabled agent is available for this request."}
        return {"action": "delegate", "tasks": [{"agent_id": enabled[0]["id"], "objective": prompt}]}

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
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            self._cancel_active_children(oid)
            self._fail_running(oid, "Orchestration time limit reached; active delegated tasks were cancelled.")

    def _run(self, oid):
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
                decision = self._decision(running["prompt"], self.store.list_agents(), results)
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
                            "task_id": task["id"], "message": "Delegated objective to agent.",
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
