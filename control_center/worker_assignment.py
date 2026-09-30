"""Deterministic execution slots for semantic plan tasks.

An assignment is a scheduling resource, not a capability grant. Each task
continues to receive its own task-scoped agent and policy at dispatch.
"""
from __future__ import annotations

from typing import Any


def worker_assignments(tasks: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    """Group related tasks while keeping their DAG edges unchanged."""
    if strategy == "single_worker":
        groups = [[task["id"] for task in tasks]]
    elif strategy == "multi_worker":
        by_id = {task["id"]: task for task in tasks}
        children: dict[str, list[str]] = {task_id: [] for task_id in by_id}
        for task in tasks:
            for dependency in task["depends_on"]:
                children[dependency].append(task["id"])

        def role(task: dict[str, Any]) -> str:
            kind = task.get("task_kind")
            return kind if kind in {"review", "testing"} else "implementation"

        parent_of = {task_id: task_id for task_id in by_id}

        def root(task_id: str) -> str:
            while parent_of[task_id] != task_id:
                task_id = parent_of[task_id]
            return task_id

        for task in tasks:
            same_role_parents = [parent for parent in task["depends_on"]
                                 if role(by_id[parent]) == role(task)]
            parent = same_role_parents[0] if len(same_role_parents) == 1 else None
            if parent is not None:
                same_role_children = [child for child in children[parent]
                                      if role(by_id[child]) == role(task)]
                if len(same_role_children) == 1:
                    parent_of[root(task["id"])] = root(parent)
        grouped: dict[str, list[str]] = {}
        for task in tasks:
            grouped.setdefault(root(task["id"]), []).append(task["id"])
        groups = list(grouped.values())
        if len(groups) == 1 and len(tasks) > 1:
            midpoint = len(tasks) // 2
            groups = [groups[0][:midpoint], groups[0][midpoint:]]
    else:
        raise ValueError("Unknown execution strategy.")
    return [{"worker_id": f"worker-{index}", "task_ids": task_ids}
            for index, task_ids in enumerate(groups, 1)]


def worker_id_for_task(plan: dict[str, Any], task_id: str) -> str | None:
    """Legacy plans without assignments keep their existing scheduling."""
    for assignment in plan.get("worker_assignments", []):
        if task_id in assignment["task_ids"]:
            return assignment["worker_id"]
    return None


def occupied_workers(plan: dict[str, Any], nodes: list[dict[str, Any]], *,
                     include_selected_ready: bool = True) -> set[str]:
    """Return assignment slots reserved by active or selected graph nodes."""
    occupied = set()
    for node in nodes:
        state = node["state"]
        if (state in {"running", "evaluating", "waiting_for_approval"}
                or (include_selected_ready and state == "ready"
                    and node.get("selection_id"))):
            worker_id = worker_id_for_task(plan, node["plan_task_id"])
            if worker_id is not None:
                occupied.add(worker_id)
    return occupied
