"""Bounded, read-only execution-plan view for one delegated worker."""
from __future__ import annotations

import json
from typing import Any


MAX_PLAN_CONTEXT_CHARS = 70_000


def _clip(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text, False
    return text[:limit - 1] + "…", True


def _list(value: Any, budget: int, item_limit: int = 160) -> tuple[list[str], bool]:
    items = value if isinstance(value, list) else []
    width = max(12, min(item_limit, budget // max(1, len(items))))
    clipped = [_clip(item, width) for item in items]
    return [item for item, _ in clipped], any(shortened for _, shortened in clipped)


def _status(value: Any) -> str:
    state = str(value or "pending").casefold()
    return {
        "success": "SUCCESS", "running": "RUNNING", "evaluating": "RUNNING",
        "waiting_for_approval": "BLOCKED", "blocked": "BLOCKED",
        "failed": "FAILED", "cancelled": "FAILED", "skipped": "BLOCKED",
        "superseded": "BLOCKED", "ready": "PENDING", "pending": "PENDING",
        "recovery_pending": "PENDING",
    }.get(state, "PENDING")


def _result_summary(result: Any, *, dense: bool = False) -> dict[str, Any] | None:
    """Select only concise outcome fields; never carry actions or tool output."""
    if not isinstance(result, dict):
        return None
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    summary, _ = _clip(payload.get("summary"), 120 if dense else 300)
    artifacts = payload.get("artifacts")
    paths = []
    if isinstance(artifacts, list):
        for artifact in artifacts[:3 if dense else 8]:
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                paths.append(_clip(artifact["path"], 80 if dense else 120)[0])
    return {key: value for key, value in {"summary": summary, "artifact_paths": paths}.items() if value} or None


def render_plan_context(plan: dict[str, Any], current: dict[str, Any],
                        nodes: list[dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
    """Render every plan node in order without adding any action authority."""
    tasks = [item for item in plan.get("tasks", []) if isinstance(item, dict)]
    current_id = str(current.get("id") or "")
    by_id = {str(node.get("plan_task_id") or ""): node for node in (nodes or [])
             if isinstance(node, dict)}
    task_ids = [str(item.get("id") or "") for item in tasks]
    if current_id not in task_ids:
        raise ValueError("Current task is absent from the effective plan.")
    dense = len(tasks) > 10
    truncated = False
    records = []
    for index, task in enumerate(tasks, 1):
        task_id = str(task.get("id") or "")
        objective, a = _clip(task.get("objective"), 160 if dense else 240)
        description, b = _clip(task.get("description"), 260 if dense else 420)
        criteria, c = _list(task.get("success_criteria"), 500 if dense else 900, 180)
        targets, d = _list(task.get("write_targets"), 240 if dense else 480, 180)
        owned, e = _list(task.get("owned_paths"), 240 if dense else 480, 180)
        operations, f = _list(task.get("semantic_operations"), 160 if dense else 280, 100)
        capabilities, g = _list(task.get("required_capabilities"), 180 if dense else 300, 100)
        tools, h = _list(task.get("required_tools"), 160 if dense else 240, 80)
        truncated |= any((a, b, c, d, e, f, g, h))
        node = by_id.get(task_id, {})
        record = {
            "order": index, "id": task_id,
            "status": "RUNNING" if task_id == current_id else _status(node.get("state")),
            "task_kind": task.get("task_kind") or "unspecified",
            "objective": objective, "description": description,
            "depends_on": list(task.get("depends_on") or []),
            "success_criteria": criteria, "write_targets": targets,
            "owned_paths": owned, "semantic_operations": operations,
            "required_capabilities": capabilities, "required_tools": tools,
        }
        if task_id in set(current.get("depends_on") or []) and record["status"] == "SUCCESS":
            summary = _result_summary(node.get("result"), dense=dense)
            if summary:
                record["result_summary"] = summary
        records.append(record)

    own_objective, a = _clip(current.get("objective"), 800)
    own_description, b = _clip(current.get("description"), 1400)
    own_criteria, c = _list(current.get("success_criteria"), 6000, 300)
    own_targets, d = _list(current.get("write_targets"), 1600, 240)
    truncated |= any((a, b, c, d))
    successors = [item["id"] for item in records if current_id in item["depends_on"]]
    non_goals = [{"reserved_by": item["id"], "responsibility": item["objective"]}
                 for item in records if item["id"] != current_id]
    sections = [
        "CURRENT TASK RESPONSIBILITY",
        "CURRENT TASK: " + current_id,
        json.dumps({"id": current_id, "objective": own_objective,
                    "description": own_description, "success_criteria": own_criteria,
                    "write_targets": own_targets}, ensure_ascii=False, separators=(",", ":")),
        "CURRENT TASK NON-GOALS",
        "Do not perform responsibilities assigned to another task. Write targets authorize paths, "
        "not all behavior possible in those files. A file-creation task with existence-only "
        "criteria should create the minimum valid scaffold. Do not preempt successor tasks. "
        "Sequential tasks may legitimately modify the same file for different responsibilities.",
        json.dumps(non_goals, ensure_ascii=False, separators=(",", ":")),
        "RELATED TASK RESPONSIBILITIES / ORCHESTRATION PLAN (read-only, plan order)",
        json.dumps(records, ensure_ascii=False, separators=(",", ":")),
        "If your criteria are already met, verify with permitted tools and finish with evidence; "
        "do not invent writes or repeat reads. Only the current task policy grants actions.",
    ]
    rendered = "\n".join(sections)
    if len(rendered) > MAX_PLAN_CONTEXT_CHARS:
        # Validated plans currently contain at most 20 tasks. Fail closed if a
        # future schema exceeds this budget instead of silently hiding nodes.
        raise ValueError("Plan context exceeds its bounded prompt budget.")
    metadata = {
        "current_task_id": current_id, "visible_task_ids": task_ids,
        "task_count": len(records), "dependency_ids": list(current.get("depends_on") or []),
        "successor_ids": successors, "non_goal_count": len(non_goals),
        "context_truncated": truncated, "context_chars": len(rendered),
    }
    return rendered, metadata
