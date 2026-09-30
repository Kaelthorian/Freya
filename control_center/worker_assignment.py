"""Deterministic worker assignments for semantic plan tasks.

An assignment is an execution identity, not a capability grant. A Worker can
execute multiple assigned Tasks, while each active Task gets a fresh
least-privilege policy and immutable Runtime snapshot.
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
    """Resolve the authoritative Worker for a task, if this plan has assignments."""
    for assignment in plan.get("worker_assignments", []):
        if task_id in assignment["task_ids"]:
            return assignment["worker_id"]
    return None


def worker_assignment_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate compiler output and map every assigned plan task to one Worker.

    Plans persisted before worker assignments were introduced remain on the
    legacy per-task-agent path. Once a plan declares assignments, they must
    cover the plan exactly once; the runtime never infers or repairs them.
    """
    if "worker_assignments" not in plan:
        return {}
    raw = plan.get("worker_assignments")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("Compiled worker_assignments must be a list of objects.")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Compiled plan tasks must be a list.")
    if not raw and tasks:
        raise ValueError("Compiled worker_assignments cannot be empty when the plan has tasks.")
    task_order = [str(item.get("id") or "") for item in tasks if isinstance(item, dict)]
    if len(task_order) != len(tasks) or not all(task_order):
        raise ValueError("Compiled plan tasks must have unique non-empty IDs.")
    if len(set(task_order)) != len(task_order):
        raise ValueError("Compiled plan task IDs must be unique.")

    by_task: dict[str, dict[str, Any]] = {}
    worker_ids: set[str] = set()
    for assignment in raw:
        worker_id = assignment.get("worker_id")
        task_ids = assignment.get("task_ids")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Every worker assignment requires a non-empty worker_id.")
        if worker_id in worker_ids:
            raise ValueError("Compiled worker IDs must be unique.")
        worker_ids.add(worker_id)
        if worker_id in task_order:
            raise ValueError("A task_id cannot also be a worker_id.")
        if (not isinstance(task_ids, list) or not task_ids
                or any(not isinstance(item, str) or not item for item in task_ids)):
            raise ValueError(f"Worker {worker_id} requires a non-empty task_ids list.")
        for task_id in task_ids:
            if task_id not in task_order:
                raise ValueError(f"Worker {worker_id} references unknown task {task_id}.")
            if task_id in by_task:
                raise ValueError(f"Task {task_id} is assigned to more than one Worker.")
            by_task[task_id] = {"worker_id": worker_id, "task_ids": list(task_ids)}
    if set(by_task) != set(task_order):
        missing = [task_id for task_id in task_order if task_id not in by_task]
        raise ValueError("Compiled worker assignments omit tasks: " + ", ".join(missing))
    return by_task


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
