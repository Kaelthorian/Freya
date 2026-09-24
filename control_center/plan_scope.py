"""Bound model-proposed semantic work to explicit canonical Task Spec intent."""
from __future__ import annotations

import copy
import re
from typing import Any

from .security import sanitize


class PlannerScopeError(ValueError):
    """Removing proposed work would lose an authoritative obligation."""

    error_type = "PlannerScopeError"


_EXTERNAL = re.compile(
    r"\b(?:deploy\w*|deployment|despleg\w*|publish\w*|publica\w*|"
    r"upload\w*|subir\s+(?:a|al)\s+(?:servidor|hosting|cloud)|"
    r"host(?:ing|ed)?|alojar|remote\s+host|servidor\s+remoto|"
    r"send\s+(?:an?\s+)?email|enviar\s+(?:un\s+)?correo|"
    r"push\s+(?:the\s+)?repositor\w*|open\s+ports?|abrir\s+puertos?|"
    r"call\s+(?:an?\s+)?external\s+api|send\s+(?:an?\s+)?http\s+request|"
    r"production\s+server|servidor\s+de\s+producci[oó]n)\b",
    re.I,
)
_NEGATED_EXTERNAL = re.compile(
    r"\b(?:do\s+not|don't|never|without|no|sin|nunca)\s+(?:\w+\s+){0,2}"
    r"(?:deploy\w*|despleg\w*|publish\w*|publica\w*|upload\w*|host\w*)\b",
    re.I,
)
_ARTIFACT = re.compile(
    r"\b(?:create|write|build|implement|generate|crea\w*|escrib\w*|"
    r"constru\w*|implement\w*|gener\w*)\b", re.I,
)
_CATEGORIES = {
    "artifact_creation": _ARTIFACT,
    "artifact_read": re.compile(r"\b(?:read|leer|inspect|inspeccion\w*)\b", re.I),
    "artifact_modify": re.compile(r"\b(?:edit|modify|patch|editar|modificar)\b", re.I),
    "local_execution": re.compile(r"\b(?:run|execute|ejecut\w*|compile|compilar)\b", re.I),
    "local_testing": re.compile(r"\b(?:test|pytest|unittest|probar|verificar)\b", re.I),
    "inspection": re.compile(r"\b(?:inspect|review|audit|revis\w*|auditar)\b", re.I),
    "deployment": re.compile(r"\b(?:deploy\w*|deployment|despleg\w*|hosting|"
                             r"host|hosted|alojar|remote\s+host|servidor\s+remoto|"
                             r"production\s+server|servidor\s+de\s+producci[oó]n)\b", re.I),
    "publication": re.compile(r"\b(?:publish\w*|publica\w*|upload\w*|"
                              r"subir\s+(?:a|al)\s+(?:servidor|hosting|cloud)|"
                              r"push\s+(?:the\s+)?repositor\w*)\b", re.I),
    "network_action": re.compile(r"\b(?:open\s+ports?|abrir\s+puertos?|"
                                 r"send\s+(?:an?\s+)?email|enviar\s+(?:un\s+)?correo|"
                                 r"call\s+(?:an?\s+)?external\s+api|"
                                 r"send\s+(?:an?\s+)?http\s+request)\b", re.I),
}
_EXTERNAL_CATEGORIES = frozenset({"deployment", "publication", "network_action"})


def _task_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:64]


def _external_intent(text: str) -> bool:
    return bool(_EXTERNAL.search(_NEGATED_EXTERNAL.sub("", text)))


def semantic_categories(text: str) -> set[str]:
    """Classify only clear operation cues; unknown wording remains unclassified."""
    scope_text = _NEGATED_EXTERNAL.sub("", text)
    categories = {name for name, pattern in _CATEGORIES.items()
                  if pattern.search(scope_text)}
    if _external_intent(text):
        categories.add("external_action")
    return categories


def _authorized_external_categories(spec: dict[str, Any]) -> set[str]:
    """Only explicit or clarified user intent can authorize each external kind."""
    sources = [str(spec.get("user_intent") or "")]
    for field in ("requirements", "constraints", "deliverables"):
        sources.extend(str(item.get("description") or "") for item in spec.get(field, [])
                       if isinstance(item, dict) and item.get("source") in {"explicit", "clarified"})
    sources.extend(str(value) for value in spec.get("user_decisions", {}).values())
    return set().union(*(semantic_categories(text) & _EXTERNAL_CATEGORIES
                         for text in sources))


def semantic_plan_snapshot(value: Any) -> dict[str, Any]:
    """Keep a bounded, redacted pre-resolution task view for failure diagnosis."""
    if not isinstance(value, dict):
        return {"invalid_type": type(value).__name__}
    tasks = value.get("tasks", [])
    if not isinstance(tasks, list):
        return {"invalid_tasks_type": type(tasks).__name__}
    fields = ("key", "objective", "description", "semantic_needs",
              "required_tools", "required_capabilities", "preferred_skills",
              "depends_on", "success_criteria")
    result = []
    for item in tasks[:20]:
        if not isinstance(item, dict):
            result.append({"invalid_type": type(item).__name__})
            continue
        compact = {}
        for field in fields:
            raw = item.get(field)
            if isinstance(raw, str):
                compact[field] = raw[:300]
            elif isinstance(raw, list):
                compact[field] = [str(value)[:200] for value in raw[:12]]
        result.append(compact)
    return sanitize({"tasks": result, "task_count": len(tasks)})


def reconcile_plan_scope(task_spec: dict[str, Any], value: Any
                         ) -> tuple[Any, list[dict[str, str]]]:
    """Omit unsupported external-only work before resource resolution.

    Mixed tasks and loss of a Task Spec validation criterion fail closed. A
    dropped optional node's prerequisites replace that node in dependents.
    """
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return value, []
    authorized = _authorized_external_categories(task_spec)
    for criterion in task_spec.get("validation_expectations", []):
        if (isinstance(criterion, str) and _external_intent(criterion)
                and not (semantic_categories(criterion) & _EXTERNAL_CATEGORIES) <= authorized):
            raise PlannerScopeError(
                "Task Spec validation requires an external action without an explicit or clarified request."
            )
    plan = copy.deepcopy(value)
    tasks = plan["tasks"]
    removed: dict[str, dict[str, Any]] = {}
    adjustments: list[dict[str, str]] = []
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            continue
        key = str(task.get("key") or task.get("objective") or f"step_{index}")
        text = " ".join(str(item) for item in (
            key, task.get("objective") or "", task.get("description") or "",
            *(task.get("semantic_needs") if isinstance(task.get("semantic_needs"), list) else []),
        ))
        categories = semantic_categories(text)
        unsupported = (categories & _EXTERNAL_CATEGORIES) - authorized
        if str(task.get("task_kind") or "").casefold() == "external_action":
            categories.add("external_action")
            if not categories & _EXTERNAL_CATEGORIES:
                unsupported.add("external_action")
        if "external_action" not in categories or not unsupported:
            continue
        if "artifact_creation" in categories:
            raise PlannerScopeError(
                f"Task '{key}' mixes requested artifact work with unrequested external action."
            )
        removed[_task_key(key)] = task
        adjustments.append({"task_key": key, "action": "removed",
                            "reason": "external_action_not_supported_by_task_spec",
                            "category": sorted(unsupported)[0]})
    if removed:
        kept = [task for task in tasks if not isinstance(task, dict)
                or _task_key(str(task.get("key") or task.get("objective") or "")) not in removed]
        if not kept:
            raise PlannerScopeError("The proposed plan has no task left after removing out-of-scope work.")
        for criterion in task_spec.get("validation_expectations", []):
            if not isinstance(criterion, str):
                continue
            removed_covers = any(criterion.casefold() == str(local).casefold()
                                 for task in removed.values()
                                 for local in task.get("success_criteria", []))
            kept_covers = any(criterion.casefold() == str(local).casefold()
                              for task in kept if isinstance(task, dict)
                              for local in task.get("success_criteria", []))
            if removed_covers and not kept_covers:
                raise PlannerScopeError(
                    "Removing out-of-scope work would leave a Task Spec validation criterion uncovered: "
                    + criterion[:200]
                )

        def surviving_dependencies(key: str, visiting: set[str]) -> list[str]:
            normalized = _task_key(key)
            if normalized not in removed:
                return [key]
            if normalized in visiting:
                raise PlannerScopeError("Semantic dependencies contain a cycle in removed work.")
            result = []
            for predecessor in removed[normalized].get("depends_on", []):
                for dependency in surviving_dependencies(str(predecessor), visiting | {normalized}):
                    if dependency not in result:
                        result.append(dependency)
            return result

        for task in kept:
            if not isinstance(task, dict) or not isinstance(task.get("depends_on", []), list):
                continue
            dependencies = []
            for dependency in task.get("depends_on", []):
                for survivor in surviving_dependencies(str(dependency), set()):
                    if survivor not in dependencies:
                        dependencies.append(survivor)
            task["depends_on"] = dependencies
        plan["tasks"] = kept
    criteria = plan.get("success_criteria")
    if isinstance(criteria, list):
        filtered = []
        for criterion in criteria:
            if (isinstance(criterion, str) and _external_intent(criterion)
                    and not (semantic_categories(criterion) & _EXTERNAL_CATEGORIES) <= authorized):
                adjustments.append({"task_key": "", "action": "criterion_removed",
                                    "reason": "external_action_not_supported_by_task_spec"})
            else:
                filtered.append(criterion)
        plan["success_criteria"] = filtered
    for task in plan["tasks"]:
        if isinstance(task, dict) and isinstance(task.get("success_criteria"), list):
            filtered = []
            for criterion in task["success_criteria"]:
                if (isinstance(criterion, str) and _external_intent(criterion)
                        and not (semantic_categories(criterion) & _EXTERNAL_CATEGORIES) <= authorized):
                    adjustments.append({"task_key": str(task.get("key") or ""),
                                        "action": "criterion_removed",
                                        "reason": "external_action_not_supported_by_task_spec"})
                else:
                    filtered.append(criterion)
            task["success_criteria"] = filtered
    unsupported = plan.get("unsupported_requirements")
    if isinstance(unsupported, list):
        filtered = []
        for item in unsupported:
            description = (" ".join(str(item.get(field) or "")
                           for field in ("semantic_need", "reason"))
                           if isinstance(item, dict) else "")
            if (_external_intent(description)
                    and not (semantic_categories(description) & _EXTERNAL_CATEGORIES) <= authorized):
                adjustments.append({"task_key": "", "action": "unsupported_need_removed",
                                    "reason": "external_action_not_supported_by_task_spec"})
            else:
                filtered.append(item)
        plan["unsupported_requirements"] = filtered
    if adjustments:
        plan["summary"] = str(task_spec.get("objective") or "")
    return plan, adjustments
