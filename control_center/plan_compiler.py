"""Compile a semantic model plan into the existing durable execution contract."""
from __future__ import annotations

import re
from typing import Any

from .planner import MAX_PLAN_TASKS, TASK_KIND_VALUES, PlanValidationError, validate_plan
from .plan_scope import reconcile_plan_scope
from .runtime_resources import RuntimeResourceCatalog
from .task_spec import validate_task_spec
from .cross_task import CrossTaskRequestError, normalize_owned_paths


WRITE_CAPABILITIES = {"filesystem.create", "filesystem.modify", "filesystem.overwrite"}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:64]


def compile_semantic_task_resources(task: dict[str, Any], resource_catalog=None, *,
                                   require_task_kind: bool = False) -> dict[str, Any]:
    """Derive runtime resources from a validated semantic task in one place."""
    if not isinstance(task, dict):
        raise PlanValidationError("Semantic task must be an object.")
    resource_catalog = resource_catalog or RuntimeResourceCatalog.build()
    operations = task.get("operations", task.get("semantic_operations", []))
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        raise PlanValidationError("Semantic task operations must be a list of strings.")
    operations = list(dict.fromkeys(operations))
    capabilities, required_tools = resource_catalog.resources_for_operations(operations)
    task_kind = task.get("task_kind")
    if require_task_kind and task_kind is None:
        raise PlanValidationError("Semantic task task_kind is missing.")
    if task_kind is not None and (
            not isinstance(task_kind, str) or task_kind not in TASK_KIND_VALUES):
        raise PlanValidationError("Semantic task task_kind is not registered.")
    if task_kind in {"testing", "review"} and set(capabilities) & WRITE_CAPABILITIES:
        raise PlanValidationError(
            f"A {task_kind} task cannot own file-write capabilities."
        )
    if task_kind in {"file_creation", "program_creation", "code_change"} and not (
            set(capabilities) & WRITE_CAPABILITIES):
        raise PlanValidationError(
            f"A {task_kind} task must declare a semantic file-write operation."
        )
    if task_kind == "review" and not set(capabilities) & {
            "filesystem.read", "filesystem.list", "filesystem.search", "git.diff"}:
        raise PlanValidationError("A review task must declare a read-only inspection operation.")
    if task_kind == "testing" and not any(
            capability.startswith("execution.") for capability in capabilities):
        raise PlanValidationError("A testing task must declare a registered execution operation.")
    try:
        owned_paths = normalize_owned_paths(task.get("owned_paths", []))
    except CrossTaskRequestError as exc:
        raise PlanValidationError(f"Invalid semantic task owned_paths: {exc}") from exc
    if set(capabilities) & WRITE_CAPABILITIES and not owned_paths:
        raise PlanValidationError(
            "Every task with file-write capabilities must declare concrete owned_paths."
        )
    compiled = {key: value for key, value in task.items()
                if key not in {"operations", "required_capabilities", "required_tools",
                               "preferred_skills"}}
    compiled["owned_paths"] = owned_paths
    compiled["semantic_operations"] = operations
    compiled["required_capabilities"] = capabilities
    compiled["required_tools"] = required_tools
    compiled["preferred_skills"] = []
    return compiled


def compile_semantic_plan(value: Any, task_spec: dict[str, Any], *,
                          resource_catalog=None) -> dict[str, Any]:
    """Assign every internal ID and link; model supplies only task meaning."""
    spec = validate_task_spec(task_spec)
    if spec["status"] != "READY_FOR_PLANNING":
        raise PlanValidationError("Planning requires a ready Task Spec.")
    resource_catalog = resource_catalog or RuntimeResourceCatalog.build()
    value, scope_adjustments = reconcile_plan_scope(spec, value)
    resource_catalog.scope_adjustments = scope_adjustments
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
        operations = item.get("operations", [])
        if not isinstance(operations, list) or any(not isinstance(x, str) for x in operations):
            raise PlanValidationError("Semantic task operations must be a list of strings.")
        task_kind = item.get("task_kind")
        if not isinstance(task_kind, str) or task_kind not in TASK_KIND_VALUES:
            raise PlanValidationError("Semantic task task_kind is missing or not registered.")
        runtime_resources = compile_semantic_task_resources(
            {**item, "operations": operations}, resource_catalog)
        capabilities = runtime_resources["required_capabilities"]
        required_tools = runtime_resources["required_tools"]
        task_kind = runtime_resources["task_kind"]
        semantic_needs = item.get("semantic_needs", [])
        if not isinstance(semantic_needs, list) or any(not isinstance(x, str) or not x.strip()
                                                        for x in semantic_needs):
            raise PlanValidationError("Semantic task semantic_needs must be non-empty strings.")
        try:
            owned_paths = normalize_owned_paths(item.get("owned_paths", []))
        except CrossTaskRequestError as exc:
            raise PlanValidationError(f"Invalid semantic task owned_paths: {exc}") from exc
        if set(capabilities) & WRITE_CAPABILITIES and not owned_paths:
            raise PlanValidationError(
                "Every task with file-write capabilities must declare concrete owned_paths."
            )
        compiled_task = {"id": f"task-{index}", "objective": str(item["objective"]).strip(),
                      "description": str(item.get("description") or item["objective"]).strip(),
                      "depends_on": resolved,
                      "semantic_operations": runtime_resources["semantic_operations"],
                      "required_capabilities": capabilities,
                      "required_tools": required_tools,
                      "semantic_needs": list(dict.fromkeys(semantic_needs)),
                      "owned_paths": owned_paths,
                      "preferred_skills": [],
                      "success_criteria": list(dict.fromkeys(criteria)) or
                                          [f"The result of {item['objective']} is verified."]}
        if task_kind is not None:
            compiled_task["task_kind"] = task_kind
        tasks.append(compiled_task)
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
    for warning in resource_catalog.preferred_skill_warnings_for_tasks(tasks):
        if warning not in resource_catalog.preferred_skill_warnings:
            resource_catalog.preferred_skill_warnings.append(warning)
    return validate_plan({"goal": spec["objective"],
                          "summary": str(value.get("summary") or spec["objective"]).strip(),
                          "complexity": "simple" if len(tasks) == 1 else "multi_step",
                          "tasks": tasks, "success_criteria": global_criteria,
                          "criterion_links": links})
