"""Compile a semantic model plan into the existing durable execution contract."""
from __future__ import annotations

import re
from typing import Any

from .planner import MAX_PLAN_TASKS, PlanValidationError, validate_plan
from .runtime_resources import RuntimeResourceCatalog
from .task_spec import validate_task_spec


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:64]


def compile_semantic_plan(value: Any, task_spec: dict[str, Any], *,
                          resource_catalog=None) -> dict[str, Any]:
    """Assign every internal ID and link; model supplies only task meaning."""
    spec = validate_task_spec(task_spec)
    if spec["status"] != "READY_FOR_PLANNING":
        raise PlanValidationError("Planning requires a ready Task Spec.")
    resource_catalog = resource_catalog or RuntimeResourceCatalog.build()
    value = resource_catalog.validate_semantic_plan(value, allow_aliases=False)
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        raise PlanValidationError("Semantic plan must contain tasks.")
    raw_tasks = value["tasks"]
    if not 1 <= len(raw_tasks) <= MAX_PLAN_TASKS:
        raise PlanValidationError("Semantic plan task count is invalid.")
    keys: list[str] = []
    for index, item in enumerate(raw_tasks, 1):
        if not isinstance(item, dict):
            raise PlanValidationError("Semantic tasks must be objects.")
        objective = str(item.get("objective") or "").strip()
        if not objective:
            raise PlanValidationError("Semantic task objective is missing.")
        key = _slug(str(item.get("key") or objective)) or f"step_{index}"
        if key in keys:
            raise PlanValidationError("Semantic task references are ambiguous.")
        keys.append(key)
    tasks = []
    for index, item in enumerate(raw_tasks, 1):
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise PlanValidationError("Semantic dependencies must be a list.")
        resolved = []
        for dependency in dependencies:
            key = _slug(str(dependency))
            if key not in keys:
                raise PlanValidationError(f"Unknown semantic dependency: {dependency}.")
            identifier = f"task-{keys.index(key) + 1}"
            if identifier not in resolved:
                resolved.append(identifier)
        criteria = item.get("success_criteria") or []
        if not isinstance(criteria, list) or any(not isinstance(x, str) or not x.strip() for x in criteria):
            raise PlanValidationError("Semantic task checks must be text.")
        capabilities = item.get("required_capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(x, str) for x in capabilities):
            raise PlanValidationError("Semantic task required_capabilities must be a list of strings.")
        required_tools = item.get("required_tools", [])
        if not isinstance(required_tools, list) or any(not isinstance(x, str) for x in required_tools):
            raise PlanValidationError("Semantic task required_tools must be a list of strings.")
        if not required_tools:
            required_tools = resource_catalog.tools_for_capabilities(capabilities)
        semantic_needs = item.get("semantic_needs", [])
        if not isinstance(semantic_needs, list) or any(not isinstance(x, str) or not x.strip()
                                                        for x in semantic_needs):
            raise PlanValidationError("Semantic task semantic_needs must be non-empty strings.")
        tasks.append({"id": f"task-{index}", "objective": str(item["objective"]).strip(),
                      "description": str(item.get("description") or item["objective"]).strip(),
                      "depends_on": resolved,
                      "required_capabilities": capabilities,
                      "required_tools": required_tools,
                      "semantic_needs": list(dict.fromkeys(semantic_needs)),
                      "preferred_skills": item.get("preferred_skills", []),
                      "success_criteria": list(dict.fromkeys(criteria)) or
                                          [f"The result of {item['objective']} is verified."]})
    # The global obligations come from the canonical user intent. Model-proposed
    # checks may add detail, but cannot replace the user's validation expectation.
    global_criteria = list(dict.fromkeys([
        *spec["validation_expectations"],
        *[str(item).strip() for item in value.get("success_criteria", [])
          if isinstance(item, str) and item.strip()],
    ]))
    if not global_criteria:
        global_criteria = ["The requested outcome is completed and validated."]
    if len(global_criteria) > 20:
        raise PlanValidationError("Too many global criteria.")
    # Attach a concrete local check for each global obligation to the final
    # semantic task. The verifier still requires observed evidence; a link is
    # never itself proof that a criterion passed.
    final = tasks[-1]
    for criterion in global_criteria:
        if criterion not in final["success_criteria"]:
            final["success_criteria"].append(criterion)
    if len(final["success_criteria"]) > 20:
        raise PlanValidationError("Too many local checks for the final task.")
    links = {"global": [{"id": f"GC-{index}", "criterion": criterion}
                         for index, criterion in enumerate(global_criteria, 1)],
             "local": []}
    local_number = 0
    for task in tasks:
        for criterion in task["success_criteria"]:
            local_number += 1
            links["local"].append({
                "id": f"LC-{local_number}", "task_id": task["id"], "criterion": criterion,
                "supports_global_criteria": [f"GC-{index}" for index, global_item in
                                             enumerate(global_criteria, 1) if global_item == criterion],
            })
    return validate_plan({"goal": spec["objective"],
                          "summary": str(value.get("summary") or spec["objective"]).strip(),
                          "complexity": "simple" if len(tasks) == 1 else "multi_step",
                          "tasks": tasks, "success_criteria": global_criteria,
                          "criterion_links": links})
