"""Structured, fail-closed planning for Freya orchestrations.

The planner describes work.  Capability policy remains the only authority that
can allow a worker action, and preferred Skills are non-binding selection hints.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .capabilities import CAPABILITY_REGISTRY
from .config import validate_endpoint
from .transport import request_json


PLAN_SCHEMA_VERSION = 1
MAX_PLAN_TASKS = 20
# User prompts are accepted without an application-level character limit.
# The model/provider context window and HTTP transport remain the practical
# boundaries for safely processing extremely large requests.
MAX_GOAL_CHARS: int | None = None
MAX_SUMMARY_CHARS = 4_000
MAX_OBJECTIVE_CHARS = 2_000
MAX_DESCRIPTION_CHARS = 4_000
MAX_CRITERIA = 20
MAX_CRITERION_CHARS = 1_000
MAX_DEPENDENCIES = 20
MAX_CAPABILITIES = 30
MAX_PREFERRED_SKILLS = 30
MAX_IDENTIFIER_CHARS = 64
MAX_MODEL_OUTPUT_CHARS = 128_000
DEFAULT_PLANNER_MODEL = "qwen2.5-coder:7b"
DEFAULT_PLANNER_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_PLANNER_TIMEOUT_SECONDS = 120.0
DEFAULT_PLANNER_CONTEXT_WINDOW = 8_192
DEFAULT_PLANNER_MAX_TOKENS = 2048

PLAN_FIELDS = {"goal", "summary", "complexity", "tasks", "success_criteria"}
TASK_FIELDS = {
    "id", "objective", "description", "depends_on", "required_capabilities",
    "preferred_skills", "success_criteria",
}

PLAN_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "summary": {"type": "string"},
        "complexity": {"type": "string", "enum": ["simple", "multi_step"]},
        "tasks": {
            "type": "array", "minItems": 1, "maxItems": MAX_PLAN_TASKS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "objective": {"type": "string"},
                    "description": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "required_capabilities": {
                        "type": "array", "items": {
                            "type": "string", "enum": sorted(CAPABILITY_REGISTRY),
                        },
                    },
                    "preferred_skills": {"type": "array", "items": {"type": "string"}},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": sorted(TASK_FIELDS),
                "additionalProperties": False,
            },
        },
        "success_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": sorted(PLAN_FIELDS),
    "additionalProperties": False,
}

_SIMPLE_ARTIFACT_HINT = re.compile(
    r"(?:\.[a-z0-9]{1,8}\b|\b(?:file|archivo|script|document|documento)\b)",
    re.IGNORECASE,
)


def _is_code_audit_task(task: dict[str, Any]) -> bool:
    return (
        "code-review" in task.get("preferred_skills", [])
        or bool(re.search(
            r"\b(?:code audit|code auditor|code review|audit code|review code|revis(?:e|ar) c[oó]digo)\b",
            str(task.get("objective", "")), re.I,
        ))
    )


def _is_trivial_task(plan: dict[str, Any], analysis: Any) -> bool:
    """Recognize a bounded artifact-plus-execution flow without weakening planning."""
    if not isinstance(analysis, dict) or plan.get("complexity") != "simple":
        return False
    tasks = plan.get("tasks")
    characteristics = analysis.get("task_characteristics")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(characteristics, dict):
        return False
    if any(characteristics.get(key) is True for key in (
        "interactive", "requires_user_input", "long_running", "requires_external_service",
        "requires_gui", "requires_elevated_privileges",
    )):
        return False
    if str(analysis.get("task_type") or "").casefold() not in {
        "program creation", "script creation", "file creation",
    }:
        return False
    task = tasks[0]
    if _is_code_audit_task(task) or any(
        re.search(r"\b(?:audit|review|revis(?:e|ar)|inspect code)\b", str(item), re.I)
        for item in task.get("success_criteria", [])
    ):
        return False
    allowed = {"filesystem.create", "filesystem.modify", "filesystem.read",
               "execution.python_script", "execution.py_compile"}
    return set(task.get("required_capabilities", [])) <= allowed


def _collapse_simple_artifact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Merge an over-decomposed linear file workflow into one worker task."""
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= 6:
        return plan
    combined_text = " ".join(
        [str(plan.get("goal", "")), str(plan.get("summary", ""))]
        + [str(task.get("objective", "")) + " " + str(task.get("description", ""))
           for task in tasks]
    )
    if not _SIMPLE_ARTIFACT_HINT.search(combined_text):
        return plan
    for index, task in enumerate(tasks):
        dependencies = set(task.get("depends_on", []))
        expected = set() if index == 0 else {tasks[index - 1]["id"]}
        if dependencies != expected:
            return plan
        if any(not str(capability).startswith(("filesystem.", "execution.", "git."))
               for capability in task.get("required_capabilities", [])):
            return plan

    def unique(values: list[str], maximum: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
            if len(result) >= maximum:
                break
        return result

    objective = str(plan.get("goal") or tasks[0]["objective"]).strip()
    if len(objective) > MAX_OBJECTIVE_CHARS:
        objective = str(tasks[0]["objective"]).strip()[:MAX_OBJECTIVE_CHARS]
    description = "Complete the requested artifact workflow in one pass: " + "; ".join(
        str(task["description"]).strip() for task in tasks
    )
    merged = {
        "id": tasks[0]["id"],
        "objective": objective,
        "description": description[:MAX_DESCRIPTION_CHARS],
        "depends_on": list(tasks[0].get("depends_on", [])),
        "required_capabilities": unique(
            [capability for task in tasks for capability in task.get("required_capabilities", [])],
            MAX_CAPABILITIES,
        ),
        "preferred_skills": unique(
            [skill for task in tasks for skill in task.get("preferred_skills", [])],
            MAX_PREFERRED_SKILLS,
        ),
        "success_criteria": unique(
            [criterion for task in tasks for criterion in task.get("success_criteria", [])],
            MAX_CRITERIA,
        ),
    }
    collapsed = dict(plan)
    collapsed["complexity"] = "simple"
    collapsed["tasks"] = [merged]
    return validate_plan(collapsed)

def _append_code_audit_task(plan: dict[str, Any], analysis: Any = None) -> dict[str, Any]:
    """Add exactly one read-only Code Review task after code/file changes."""
    if _is_trivial_task(plan, analysis):
        return plan
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) >= MAX_PLAN_TASKS:
        return plan
    capabilities = {
        str(capability) for task in tasks
        for capability in task.get("required_capabilities", [])
    }
    if not capabilities.intersection({"filesystem.create", "filesystem.modify", "filesystem.overwrite"}):
        return plan
    if any(_is_code_audit_task(task) for task in tasks):
        return plan
    ids = {task["id"] for task in tasks}
    audit_id = "code-audit"
    suffix = 2
    while audit_id in ids:
        audit_id = f"code-audit-{suffix}"
        suffix += 1
    audited = dict(plan)
    audited["complexity"] = "multi_step"
    audited["tasks"] = [*tasks, {
        "id": audit_id,
        "objective": "Audit the code produced for the user's request",
        "description": (
            "Perform a read-only code audit after implementation. Inspect the current workspace, "
            "the resulting diff when available, callers and relevant edge cases. Do not modify files "
            "or execute commands. Report confirmed findings and hypotheses separately."
        ),
        "depends_on": [task["id"] for task in tasks],
        "required_capabilities": ["filesystem.read"],
        "preferred_skills": ["code-review"],
        "success_criteria": [
            "The changed code is inspected with the Code Review skill.",
            "Findings include severity, evidence and file references, or clearly state that no findings were detected.",
        ],
    }]
    return validate_plan(audited)


def _append_qa_task(plan: dict[str, Any], analysis: Any) -> dict[str, Any]:
    """Insert one independent QA node when observable input must be tested."""
    if not isinstance(analysis, dict):
        return plan
    characteristics = analysis.get("task_characteristics")
    validation = analysis.get("validation")
    interactive = (
        isinstance(characteristics, dict)
        and (characteristics.get("interactive") is True
             or characteristics.get("requires_user_input") is True)
    ) or (
        isinstance(validation, dict)
        and validation.get("interactive_validation_required") is True
    )
    tasks = plan.get("tasks", [])
    if not interactive or not isinstance(tasks, list) or len(tasks) >= MAX_PLAN_TASKS:
        return plan
    if any(
        "interactive-testing" in task.get("preferred_skills", [])
        or re.search(r"\b(?:qa|quality assurance|interactive test)\b", str(task.get("objective", "")), re.I)
        for task in tasks
    ):
        return plan

    audit_tasks = [task for task in tasks if _is_code_audit_task(task)]
    implementation_tasks = [task for task in tasks if not _is_code_audit_task(task)]
    if not implementation_tasks:
        return plan
    ids = {task["id"] for task in tasks}
    qa_id = "qa-interactive-test"
    suffix = 2
    while qa_id in ids:
        qa_id = f"qa-interactive-test-{suffix}"
        suffix += 1
    qa = {
        "id": qa_id,
        "objective": "QA-test the interactive behavior with controlled input",
        "description": (
            "Act as the independent QA Tester after implementation. Inspect the produced program, "
            "choose representative and edge-case inputs, and run supported Python programs with the "
            "run_command stdin field so execution cannot wait for a terminal. Verify exit status and "
            "logical output. Do not modify files. If the artifact cannot be executed by the restricted "
            "runtime, report the exact unsupported boundary instead of waiting or inventing success."
        ),
        "depends_on": [task["id"] for task in implementation_tasks],
        "required_capabilities": ["filesystem.read", "execution.python_script"],
        "preferred_skills": ["interactive-testing", "software-testing"],
        "success_criteria": [
            "Representative controlled input completes without timeout or crash.",
            "Observed output is logically correct for the supplied input.",
            "The QA report cites the command, bounded stdin case, exit status and observed output.",
        ],
    }
    updated = dict(plan)
    updated["complexity"] = "multi_step"
    # If the Planner model already emitted an audit node, normalize it behind
    # QA instead of allowing audit-before-behavior-test ordering.
    normalized_audits = []
    for task in audit_tasks:
        current = dict(task)
        current["depends_on"] = list(dict.fromkeys([*current.get("depends_on", []), qa_id]))
        normalized_audits.append(current)
    updated["tasks"] = [*implementation_tasks, qa, *normalized_audits]
    return validate_plan(updated)


def _reconcile_task_analysis(plan: dict[str, Any], analysis: Any) -> dict[str, Any]:
    """Apply concrete constraints from the Task Analyst operational brief."""
    if not isinstance(analysis, dict):
        return plan
    characteristics = analysis.get("task_characteristics")
    if not isinstance(characteristics, dict):
        return plan
    requires_write = characteristics.get("requires_filesystem_write") is True
    interactive = (characteristics.get("interactive") is True
                   or characteristics.get("requires_user_input") is True)
    task_type = str(analysis.get("task_type") or "").strip().casefold()
    windows_script = task_type == "windows_command_script"
    if not requires_write and not windows_script:
        return plan
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return plan
    reconciled = dict(plan)
    updated_tasks: list[dict[str, Any]] = []
    for task in tasks:
        current = dict(task)
        capabilities = list(current.get("required_capabilities", []))
        if requires_write and not any(capability in {
                "filesystem.create", "filesystem.modify", "filesystem.overwrite"
        } for capability in capabilities):
            capabilities.append("filesystem.create")
        if windows_script:
            # The runtime intentionally does not allow arbitrary cmd.exe
            # execution. Static creation/read-back is the safe validation path.
            capabilities = [capability for capability in capabilities
                             if capability != "execution.python_script"]
            description = str(current.get("description") or "").strip()
            if not re.search(r"\b(?:cmd|bat|batch|command prompt)\b|\.(?:cmd|bat)\b", description, re.I):
                description += " The artifact must be a Windows CMD/BAT script (.cmd or .bat), not Python."
            current["description"] = description[:MAX_DESCRIPTION_CHARS]
            criteria = list(current.get("success_criteria", []))
            artifact_criterion = "A .cmd or .bat file exists and contains the requested Windows command behavior."
            if not any(re.search(r"\.(?:cmd|bat)\b|CMD|BAT", str(item), re.I) for item in criteria):
                criteria.append(artifact_criterion)
            current["success_criteria"] = criteria[:MAX_CRITERIA]
            current["preferred_skills"] = [skill for skill in current.get("preferred_skills", [])
                                            if str(skill).casefold() not in {"python-development", "python"}]
        if interactive:
            description = str(current.get("description") or "").strip()
            instruction = (
                "Do not wait for interactive terminal input during implementation; "
                "the dependent QA Tester owns controlled-stdin behavioral verification."
            )
            if instruction.casefold() not in description.casefold():
                current["description"] = (description + " " + instruction).strip()[:MAX_DESCRIPTION_CHARS]
        current["required_capabilities"] = capabilities[:MAX_CAPABILITIES]
        updated_tasks.append(current)
    reconciled["tasks"] = updated_tasks
    return validate_plan(reconciled)


class PlanValidationError(ValueError):
    """The proposed plan does not satisfy the versioned plan contract."""


class PlanGenerationError(RuntimeError):
    """A configured planner model failed to produce a valid plan."""


class OllamaPlanner:
    """Loopback-only Ollama adapter that requests JSON without exposing tools."""

    def __init__(self, model: str = DEFAULT_PLANNER_MODEL,
                 endpoint: str = DEFAULT_PLANNER_ENDPOINT,
                 timeout_seconds: float = DEFAULT_PLANNER_TIMEOUT_SECONDS,
                 request: Callable[..., dict[str, Any]] = request_json):
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 200:
            raise ValueError("Planner model must contain 1-200 characters.")
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not 0.1 <= float(timeout_seconds) <= 120):
            raise ValueError("Planner timeout must be between 0.1 and 120 seconds.")
        self.model = model.strip()
        self.endpoint = validate_endpoint(endpoint)
        self.timeout_seconds = float(timeout_seconds)
        self.request = request
        self.last_call_metrics: dict[str, Any] = {}

    def __call__(self, prompt: str, context: dict[str, Any]) -> str:
        started = time.monotonic()
        self.last_call_metrics = {}
        try:
            response = self.request(
                "POST", self.endpoint + "/api/chat",
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": (
                            "You are Freya's planning component. Return only the requested JSON plan. "
                            "Do not include private reasoning, Markdown, tool calls, or extra fields."
                        )},
                        {"role": "user", "content": prompt + "\nPlanner context:\n" +
                         json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
                    ],
                    "tools": [],
                    "format": PLAN_RESPONSE_FORMAT,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": DEFAULT_PLANNER_CONTEXT_WINDOW,
                        "num_predict": DEFAULT_PLANNER_MAX_TOKENS,
                    },
                },
                timeout=self.timeout_seconds,
            )
            message = response.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise PlanGenerationError("Ollama returned no planner message content.")
            for target, source in (("prompt_tokens", "prompt_eval_count"),
                                   ("generated_tokens", "eval_count")):
                value = response.get(source, 0) or 0
                if not isinstance(value, (int, float)) or value < 0:
                    raise PlanGenerationError("Ollama returned invalid planner token metrics.")
                self.last_call_metrics[target] = int(value)
            self.last_call_metrics["total_tokens"] = (
                self.last_call_metrics["prompt_tokens"] + self.last_call_metrics["generated_tokens"]
            )
            return message["content"]
        finally:
            self.last_call_metrics["duration_seconds"] = round(time.monotonic() - started, 4)


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{label} must be an object.")
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing:
        raise PlanValidationError(f"{label} is missing fields: {', '.join(sorted(missing))}.")
    if unknown:
        raise PlanValidationError(f"{label} has unknown fields: {', '.join(sorted(unknown))}.")
    return value


def _text(value: Any, label: str, maximum: int | None) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{label} must be a string.")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise PlanValidationError(f"{label} must not be empty.")
    if maximum is not None and len(normalized) > maximum:
        raise PlanValidationError(f"{label} exceeds {maximum} characters.")
    return normalized


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, 256).casefold()
    normalized = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not normalized:
        raise PlanValidationError(f"{label} must contain letters or numbers.")
    if len(normalized) > MAX_IDENTIFIER_CHARS:
        raise PlanValidationError(f"{label} exceeds {MAX_IDENTIFIER_CHARS} normalized characters.")
    return normalized


def _list(value: Any, label: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise PlanValidationError(f"{label} must be a list.")
    if len(value) > maximum:
        raise PlanValidationError(f"{label} exceeds the limit of {maximum} items.")
    return value


def _text_list(value: Any, label: str, maximum: int, *, allow_empty: bool = True,
               identifiers: bool = False) -> list[str]:
    items = _list(value, label, maximum)
    if not allow_empty and not items:
        raise PlanValidationError(f"{label} must not be empty.")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        normalized = (_identifier(item, f"{label}[{index}]") if identifiers else
                      _text(item, f"{label}[{index}]", MAX_CRITERION_CHARS))
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_plan(value: Any) -> dict[str, Any]:
    """Return a stable representation while enforcing field types and bounds."""
    raw = _object(value, PLAN_FIELDS, "plan")
    complexity = _text(raw["complexity"], "plan.complexity", 32).casefold().replace("-", "_")
    if complexity not in {"simple", "multi_step"}:
        raise PlanValidationError("plan.complexity must be simple or multi_step.")
    raw_tasks = _list(raw["tasks"], "plan.tasks", MAX_PLAN_TASKS)
    if not raw_tasks:
        raise PlanValidationError("plan.tasks must not be empty.")
    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks):
        task = _object(item, TASK_FIELDS, f"plan.tasks[{index}]")
        capabilities = _text_list(
            task["required_capabilities"], f"plan.tasks[{index}].required_capabilities",
            MAX_CAPABILITIES, identifiers=False,
        )
        for capability in capabilities:
            if capability not in CAPABILITY_REGISTRY:
                raise PlanValidationError(f"Unknown capability: {capability}.")
        tasks.append({
            "id": _identifier(task["id"], f"plan.tasks[{index}].id"),
            "objective": _text(task["objective"], f"plan.tasks[{index}].objective", MAX_OBJECTIVE_CHARS),
            "description": _text(task["description"], f"plan.tasks[{index}].description", MAX_DESCRIPTION_CHARS),
            "depends_on": _text_list(task["depends_on"], f"plan.tasks[{index}].depends_on",
                                      MAX_DEPENDENCIES, identifiers=True),
            "required_capabilities": capabilities,
            "preferred_skills": _text_list(task["preferred_skills"],
                                             f"plan.tasks[{index}].preferred_skills",
                                             MAX_PREFERRED_SKILLS, identifiers=True),
            "success_criteria": _text_list(task["success_criteria"],
                                            f"plan.tasks[{index}].success_criteria",
                                            MAX_CRITERIA, allow_empty=False),
        })
    # Task count is the authoritative structural signal. Models sometimes label
    # an otherwise valid multi-task graph as simple; canonicalize that harmless
    # inconsistency instead of preserving contradictory data.
    complexity = "simple" if len(tasks) == 1 else "multi_step"
    return {
        "goal": _text(raw["goal"], "plan.goal", MAX_GOAL_CHARS),
        "summary": _text(raw["summary"], "plan.summary", MAX_SUMMARY_CHARS),
        "complexity": complexity,
        "tasks": tasks,
        "success_criteria": _text_list(raw["success_criteria"], "plan.success_criteria",
                                        MAX_CRITERIA, allow_empty=False),
    }


def validate_plan(value: Any) -> dict[str, Any]:
    """Normalize and validate IDs, references and the dependency DAG."""
    plan = normalize_plan(value)
    if plan["complexity"] == "simple" and len(plan["tasks"]) != 1:
        raise PlanValidationError("A simple plan must contain exactly one task.")
    if plan["complexity"] == "multi_step" and len(plan["tasks"]) < 2:
        raise PlanValidationError("A multi_step plan must contain at least two tasks.")

    ids = [task["id"] for task in plan["tasks"]]
    if len(ids) != len(set(ids)):
        raise PlanValidationError("Task IDs must be unique after normalization.")
    known = set(ids)
    graph: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        task_id = task["id"]
        for dependency in task["depends_on"]:
            if dependency == task_id:
                raise PlanValidationError(f"Task {task_id} cannot depend on itself.")
            if dependency not in known:
                raise PlanValidationError(f"Task {task_id} depends on unknown task {dependency}.")
        graph[task_id] = task["depends_on"]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanValidationError("Task dependencies contain a cycle.")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)
    return plan


def fallback_plan(prompt: str) -> dict[str, Any]:
    """Build one safe deterministic task when no model planner is configured."""
    goal = _text(prompt, "prompt", MAX_GOAL_CHARS)
    return validate_plan({
        "goal": goal,
        "summary": f"Complete the user request: {goal}",
        "complexity": "simple",
        "tasks": [{
            "id": "task-1",
            "objective": goal,
            "description": "Complete the requested work within the selected workspace and report the result.",
            "depends_on": [],
            "required_capabilities": [],
            "preferred_skills": [],
            "success_criteria": ["The requested outcome is completed and the result is reported."],
        }],
        "success_criteria": ["The requested outcome is completed and the result is reported."],
    })


class Planner:
    """Create plans from a structured model callback, with exactly one repair."""

    def __init__(self, decide: Callable[[str, dict[str, Any]], Any] | None = None, *,
                 offline: bool = False):
        self.decide = decide
        self.offline = bool(offline)
        self.metrics: dict[str, Any] = {}

    def _reset_metrics(self) -> None:
        self.metrics = {"model_calls": 0, "prompt_tokens": 0, "generated_tokens": 0,
                        "total_tokens": 0, "duration_seconds": 0.0}

    def _call(self, prompt: str, context: dict[str, Any]) -> Any:
        started = time.monotonic()
        self.metrics["model_calls"] += 1
        try:
            return self.decide(prompt, context)
        finally:
            elapsed = round(time.monotonic() - started, 4)
            call_metrics = getattr(self.decide, "last_call_metrics", {})
            if not isinstance(call_metrics, dict):
                call_metrics = {}
            for key in ("prompt_tokens", "generated_tokens", "total_tokens"):
                value = call_metrics.get(key, 0)
                if isinstance(value, (int, float)) and value >= 0:
                    self.metrics[key] += int(value)
            duration = call_metrics.get("duration_seconds", elapsed)
            self.metrics["duration_seconds"] = round(
                self.metrics["duration_seconds"] +
                (float(duration) if isinstance(duration, (int, float)) and duration >= 0 else elapsed), 4
            )

    @staticmethod
    def _parse_output(value: Any, analysis: Any = None) -> dict[str, Any]:
        if isinstance(value, dict) and set(value) == {"message"} and isinstance(value["message"], dict):
            value = value["message"].get("content")
        if isinstance(value, str):
            if len(value) > MAX_MODEL_OUTPUT_CHARS:
                raise PlanValidationError("Planner output is too large.")
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PlanValidationError("Planner output is not valid JSON.") from exc
        plan = _collapse_simple_artifact_plan(validate_plan(value))
        plan = _reconcile_task_analysis(plan, analysis)
        plan = _append_qa_task(plan, analysis)
        return _append_code_audit_task(plan, analysis)

    @staticmethod
    def _prompt(goal: str, context: dict[str, Any]) -> str:
        capability_ids = [item.get("id") for item in context.get("capabilities", [])
                          if isinstance(item, dict) and isinstance(item.get("id"), str)]
        skill_ids = [item.get("id") for item in context.get("skills", [])
                     if isinstance(item, dict) and isinstance(item.get("id"), str)]
        return (
            "Create a work plan and return one JSON object only. Do not use Markdown. "
            "Required plan fields: goal, summary, complexity, tasks, success_criteria. "
            "complexity is simple or multi_step. Each task requires id, objective, description, "
            "depends_on, required_capabilities, preferred_skills, success_criteria. "
            "Use at most 20 tasks, unique stable IDs, existing dependency IDs, and an acyclic graph. "
            "Prefer the smallest plan that can completely satisfy the user goal. "
            "Trivial work MUST remain a single task when one agent can complete it directly. "
            "Creating one file, writing its requested contents, making one small edit, reading one file, "
            "or executing one straightforward operation is normally a simple one-task plan. "
            "Do not create separate tasks for locating a workspace, choosing a filename, creating a file, "
            "writing its contents, or verifying it when those actions can naturally be performed by the same agent. "
            "The task workspace already exists and its root is available as '.'. "
            "Do not create a task whose only purpose is to identify where inside the workspace an output should go "
            "unless the user's request genuinely requires choosing among multiple existing locations. "
            "A task may require multiple capabilities when one agent needs them to complete the objective. "
            "Use multi_step only when there are genuinely distinct pieces of work, dependencies, or specialized agents. "
            "Multi-step plans should normally use 2-6 tasks. "
            "Do not add QA or code-audit tasks yourself; Freya appends and orders those deterministic stages. "
            "required_capabilities may only use these IDs: "
            + json.dumps(capability_ids, ensure_ascii=False)
            + ". Capabilities describe likely needs and never grant permission. "
            "Use preferred_skills IDs from this enabled Skill list when possible: "
            + json.dumps(skill_ids, ensure_ascii=False)
            + ". Unknown Skill hints do not grant permission and may be ignored. "
            "Task Analyst operational prompt (authoritative task input): " + json.dumps(goal, ensure_ascii=False)
            + ("\nTask Analyst structured analysis (authoritative constraints): " +
               json.dumps(context.get("task_analysis"), ensure_ascii=False, separators=(",", ":"))
               if isinstance(context.get("task_analysis"), dict) else "")
        )

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        goal = _text(prompt, "prompt", MAX_GOAL_CHARS)
        self._reset_metrics()
        if self.decide is None:
            if self.offline:
                analysis = (context or {}).get("task_analysis")
                plan = _reconcile_task_analysis(fallback_plan(goal), analysis)
                plan = _append_qa_task(plan, analysis)
                return _append_code_audit_task(plan, analysis)
            raise PlanGenerationError("No planner model is configured. Use explicit offline mode for fallback planning.")
        limited_context = context if isinstance(context, dict) else {}
        request = self._prompt(goal, limited_context)
        try:
            output = self._call(request, limited_context)
        except Exception as exc:
            raise PlanGenerationError(f"Planner model call failed: {exc}") from exc
        try:
            parsed = self._parse_output(output, limited_context.get("task_analysis"))
            parsed["goal"] = goal
            return validate_plan(parsed)
        except (PlanValidationError, TypeError, ValueError) as first_error:
            rendered = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            repair_prompt = (
                request + "\nThe previous response was invalid. Repair it once and return only the complete JSON object. "
                f"Validation error: {first_error}. Previous response: {rendered[:MAX_MODEL_OUTPUT_CHARS]}"
            )
            try:
                repaired = self._call(repair_prompt, limited_context)
                parsed = self._parse_output(repaired, limited_context.get("task_analysis"))
                parsed["goal"] = goal
                return validate_plan(parsed)
            except Exception as second_error:
                raise PlanGenerationError(
                    f"Planner output remained invalid after one repair attempt: {second_error}"
                ) from second_error
