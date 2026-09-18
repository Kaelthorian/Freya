"""Deterministic execution graph for validated Freya plans.

This module contains no model calls, persistence, threads or runtime dispatch.
It only applies explicit state transitions to a DAG whose task order is the
immutable order captured in the plan snapshot.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .planner import validate_plan


NODE_STATES = {
    "pending", "ready", "running", "waiting_for_approval", "evaluating",
    "recovery_pending", "blocked", "success", "failed", "cancelled", "skipped",
    "superseded",
}
TERMINAL_NODE_STATES = {"blocked", "success", "failed", "cancelled", "skipped", "superseded"}
DEPENDENCY_FAILURE_STATES = {"blocked", "failed", "cancelled", "skipped"}
ACTIVE_NODE_STATES = {"running", "waiting_for_approval", "evaluating"}


def graph_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {state: 0 for state in sorted(NODE_STATES)}
    for node in nodes:
        state = node.get("state")
        if state in counts:
            counts[state] += 1
    terminal = sum(counts[state] for state in TERMINAL_NODE_STATES)
    total = len(nodes)
    return {
        "total": total,
        "terminal": terminal,
        "complete": terminal == total and total > 0,
        "successful": counts["success"],
        "failed": counts["failed"],
        "blocked": counts["blocked"],
        "cancelled": counts["cancelled"],
        "skipped": counts["skipped"],
        "superseded": counts["superseded"],
        "active": sum(counts[state] for state in ACTIVE_NODE_STATES),
        "counts": counts,
    }


class ExecutionGraph:
    """Mutable execution state over an immutable, validated plan snapshot."""

    def __init__(self, plan: dict[str, Any], nodes: list[dict[str, Any]] | None = None):
        self.plan = deepcopy(validate_plan(deepcopy(plan)))
        self.tasks = {task["id"]: deepcopy(task) for task in self.plan["tasks"]}
        self.order = [task["id"] for task in self.plan["tasks"]]
        if nodes is None:
            self._nodes = {
                task_id: {
                    "plan_task_id": task_id,
                    "plan_order": index,
                    "depends_on": list(self.tasks[task_id]["depends_on"]),
                    "state": "ready" if not self.tasks[task_id]["depends_on"] else "pending",
                    "selected_agent_id": None,
                    "selection_id": None,
                    "runtime_task_id": None,
                    "delegation_id": None,
                    "evaluation_id": None,
                    "evaluation_status": None,
                    "recovery_action_id": None,
                    "attempt_prompt": self.tasks[task_id]["objective"],
                    "plan_revision": 0,
                    "attempt": 0,
                    "waiting_reason": "",
                    "result": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": None,
                }
                for index, task_id in enumerate(self.order)
            }
        else:
            supplied = {node.get("plan_task_id"): deepcopy(node) for node in nodes}
            if set(supplied) != set(self.order) or None in supplied or len(supplied) != len(nodes):
                raise ValueError("Execution graph nodes must match planned tasks exactly once.")
            self._nodes = {}
            for index, task_id in enumerate(self.order):
                node = supplied[task_id]
                if node.get("state") not in NODE_STATES:
                    raise ValueError(f"Unknown execution node state: {node.get('state')}.")
                if list(node.get("depends_on") or []) != self.tasks[task_id]["depends_on"]:
                    raise ValueError(f"Execution node dependencies changed for {task_id}.")
                if int(node.get("plan_order", -1)) != index:
                    raise ValueError(f"Execution node order changed for {task_id}.")
                self._nodes[task_id] = node

    def node(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._nodes:
            raise KeyError(task_id)
        return self._nodes[task_id]

    def serialize(self) -> list[dict[str, Any]]:
        return [deepcopy(self._nodes[task_id]) for task_id in self.order]

    def ready_tasks(self) -> list[dict[str, Any]]:
        return [deepcopy(self.tasks[task_id]) for task_id in self.order
                if self._nodes[task_id]["state"] == "ready"]

    def active_nodes(self) -> list[dict[str, Any]]:
        return [deepcopy(self._nodes[task_id]) for task_id in self.order
                if self._nodes[task_id]["state"] in ACTIVE_NODE_STATES]

    def summary(self) -> dict[str, Any]:
        return graph_summary(self.serialize())

    def mark_selected(self, task_id: str, agent_id: str, selection_id: str,
                      updated_at: str | None = None) -> bool:
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            raise ValueError("A terminal execution node state is final.")
        if node["state"] != "ready":
            raise ValueError("Only a ready node can be selected.")
        if node.get("selection_id"):
            if (node["selection_id"], node.get("selected_agent_id")) == (selection_id, agent_id):
                return False
            raise ValueError("A planned task can only be selected once.")
        node.update(selected_agent_id=agent_id, selection_id=selection_id,
                    waiting_reason="", updated_at=updated_at)
        return True

    def set_waiting_reason(self, task_id: str, reason: str,
                           updated_at: str | None = None) -> bool:
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            raise ValueError("A terminal execution node state is final.")
        if node["state"] != "ready":
            raise ValueError("Only a ready node can wait before dispatch.")
        reason = str(reason).strip()
        changed = node.get("waiting_reason", "") != reason
        node.update(waiting_reason=reason, updated_at=updated_at)
        return changed

    def mark_running(self, task_id: str, runtime_task_id: str, delegation_id: str,
                     started_at: str | None = None) -> None:
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            raise ValueError("A terminal execution node state is final.")
        if node["state"] != "ready" or not node.get("selection_id"):
            raise ValueError("A node must be ready and selected before dispatch.")
        if node.get("runtime_task_id"):
            raise ValueError("A planned task can only be dispatched once.")
        node.update(
            state="running", runtime_task_id=runtime_task_id,
            delegation_id=delegation_id, attempt=int(node.get("attempt", 0)) + 1,
            waiting_reason="", started_at=started_at, updated_at=started_at,
        )

    def apply_runtime_status(self, task_id: str, status: str, *, result: Any = None,
                             error: str | None = None, timestamp: str | None = None) -> bool:
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            return False
        if node["state"] == "evaluating":
            if status != "Success":
                raise ValueError("A technically successful node cannot leave evaluation through Runtime.")
            return False
        if node["state"] not in {"running", "waiting_for_approval"}:
            raise ValueError("Only a dispatched node can receive runtime status.")
        if status in {"Queued", "Running"}:
            target, reason = "running", ""
        elif status == "WaitingForApproval":
            target, reason = "waiting_for_approval", "Runtime task is waiting for approval."
        elif status == "Paused":
            target, reason = "running", "Runtime task is paused."
        elif status == "Success":
            target, reason = "evaluating", "Semantic evaluation is pending."
        elif status == "Failed":
            target, reason = "failed", ""
        elif status == "Cancelled":
            target, reason = "cancelled", ""
        else:
            raise ValueError(f"Unknown runtime task status: {status}.")
        changed = (node["state"], node.get("waiting_reason"), node.get("result"),
                   node.get("error")) != (target, reason, result, error)
        node.update(state=target, waiting_reason=reason, result=result, error=error,
                    updated_at=timestamp)
        if target in TERMINAL_NODE_STATES:
            node["finished_at"] = timestamp
        return changed

    def apply_evaluation(self, task_id: str, evaluation_id: str, status: str,
                         summary: str, timestamp: str | None = None) -> bool:
        """Apply one immutable semantic decision to a technically successful node."""
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            return False
        if node["state"] != "evaluating":
            raise ValueError("Only an evaluating node can receive semantic evaluation.")
        if node.get("evaluation_id"):
            if (node["evaluation_id"], node.get("evaluation_status")) == (evaluation_id, status):
                return False
            raise ValueError("A planned task attempt can only be evaluated once.")
        if status == "accepted":
            target, error = "success", None
        elif status == "needs_revision":
            target, error = "recovery_pending", "Semantic evaluation requires revision."
        elif status == "rejected":
            target, error = "recovery_pending", "Semantic evaluation rejected the task result."
        elif status == "blocked":
            target, error = "recovery_pending", "Semantic evaluation could not determine task success."
        elif status == "error":
            target, error = "recovery_pending", "Semantic evaluator failed."
        else:
            raise ValueError(f"Unknown evaluation status: {status}.")
        node.update(
            state=target, evaluation_id=evaluation_id, evaluation_status=status,
            waiting_reason="", error=error,
            finished_at=timestamp if target == "success" else None, updated_at=timestamp,
        )
        if error and summary:
            node["error"] += " " + str(summary)
        return True

    def prepare_retry(self, task_id: str, recovery_action_id: str, prompt: str,
                      timestamp: str | None = None) -> None:
        """Open a fresh attempt while preserving the prior attempt in durable history."""
        node = self.node(task_id)
        if node["state"] != "recovery_pending" or not node.get("evaluation_id"):
            raise ValueError("Only a recovery-pending evaluated node can be retried.")
        node.update(
            state="ready", selected_agent_id=None, selection_id=None,
            runtime_task_id=None, delegation_id=None, evaluation_id=None,
            evaluation_status=None, recovery_action_id=recovery_action_id,
            attempt_prompt=str(prompt), waiting_reason="", result=None, error=None,
            started_at=None, finished_at=None, updated_at=timestamp,
        )

    def fail_recovery(self, task_id: str, recovery_action_id: str, reason: str,
                      timestamp: str | None = None) -> None:
        node = self.node(task_id)
        if node["state"] != "recovery_pending":
            raise ValueError("Only a recovery-pending node can be failed by recovery.")
        node.update(state="failed", recovery_action_id=recovery_action_id,
                    waiting_reason="", error=str(reason), finished_at=timestamp,
                    updated_at=timestamp)

    def mark_superseded(self, task_id: str, recovery_action_id: str,
                        timestamp: str | None = None) -> None:
        node = self.node(task_id)
        if node["state"] == "success":
            raise ValueError("Accepted nodes cannot be superseded.")
        if node["state"] in {"running", "waiting_for_approval", "evaluating"}:
            raise ValueError("Active nodes cannot be superseded.")
        node.update(state="superseded", recovery_action_id=recovery_action_id,
                    waiting_reason="", finished_at=timestamp, updated_at=timestamp)

    def mark_failed(self, task_id: str, error: str, timestamp: str | None = None) -> bool:
        node = self.node(task_id)
        if node["state"] in TERMINAL_NODE_STATES:
            if node["state"] == "failed":
                return False
            raise ValueError("A terminal execution node state is final.")
        node.update(state="failed", error=str(error), waiting_reason="",
                    finished_at=timestamp, updated_at=timestamp)
        return True

    def refresh_dependencies(self, timestamp: str | None = None) -> list[dict[str, str]]:
        transitions: list[dict[str, str]] = []
        changed = True
        while changed:
            changed = False
            for task_id in self.order:
                node = self._nodes[task_id]
                if node["state"] != "pending":
                    continue
                dependency_states = [self._nodes[item]["state"] for item in node["depends_on"]]
                target = None
                if any(state in DEPENDENCY_FAILURE_STATES for state in dependency_states):
                    target = "blocked"
                elif all(state == "success" for state in dependency_states):
                    target = "ready"
                if target:
                    node.update(state=target, updated_at=timestamp)
                    if target == "blocked":
                        node.update(error="A dependency did not complete successfully.",
                                    finished_at=timestamp)
                    transitions.append({"task_id": task_id, "from": "pending", "to": target})
                    changed = True
        return transitions

    def cancel_nonterminal(self, timestamp: str | None = None,
                           reason: str = "Orchestration cancelled.") -> list[str]:
        changed = []
        for task_id in self.order:
            node = self._nodes[task_id]
            if node["state"] not in TERMINAL_NODE_STATES:
                node.update(state="cancelled", error=reason, waiting_reason="",
                            finished_at=timestamp, updated_at=timestamp)
                changed.append(task_id)
        return changed
