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
MAX_GOAL_CHARS = 2_000
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
DEFAULT_PLANNER_MAX_TOKENS = 768

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


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{label} must be a string.")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise PlanValidationError(f"{label} must not be empty.")
    if len(normalized) > maximum:
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
    def _parse_output(value: Any) -> dict[str, Any]:
        if isinstance(value, dict) and set(value) == {"message"} and isinstance(value["message"], dict):
            value = value["message"].get("content")
        if isinstance(value, str):
            if len(value) > MAX_MODEL_OUTPUT_CHARS:
                raise PlanValidationError("Planner output is too large.")
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PlanValidationError("Planner output is not valid JSON.") from exc
        return validate_plan(value)

    @staticmethod
    def _prompt(goal: str, context: dict[str, Any]) -> str:
        capability_ids = [item.get("id") for item in context.get("capabilities", [])
                          if isinstance(item, dict) and isinstance(item.get("id"), str)]
        return (
            "Create a work plan and return one JSON object only. Do not use Markdown. "
            "Required plan fields: goal, summary, complexity, tasks, success_criteria. "
            "complexity is simple or multi_step. Each task requires id, objective, description, "
            "depends_on, required_capabilities, preferred_skills, success_criteria. "
            "Use at most 20 tasks, unique stable IDs, existing dependency IDs, and an acyclic graph. "
            "Do not split trivial work artificially; multi-step plans should normally use 2-6 tasks. "
            "required_capabilities may only use these IDs: "
            + json.dumps(capability_ids, ensure_ascii=False)
            + ". Capabilities describe likely needs and never grant permission. Preferred Skills are hints only. "
            "User goal: " + json.dumps(goal, ensure_ascii=False)
        )

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        goal = _text(prompt, "prompt", MAX_GOAL_CHARS)
        self._reset_metrics()
        if self.decide is None:
            if self.offline:
                return fallback_plan(goal)
            raise PlanGenerationError("No planner model is configured. Use explicit offline mode for fallback planning.")
        limited_context = context if isinstance(context, dict) else {}
        request = self._prompt(goal, limited_context)
        try:
            output = self._call(request, limited_context)
        except Exception as exc:
            raise PlanGenerationError(f"Planner model call failed: {exc}") from exc
        try:
            return self._parse_output(output)
        except (PlanValidationError, TypeError, ValueError) as first_error:
            rendered = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            repair_prompt = (
                request + "\nThe previous response was invalid. Repair it once and return only the complete JSON object. "
                f"Validation error: {first_error}. Previous response: {rendered[:MAX_MODEL_OUTPUT_CHARS]}"
            )
            try:
                repaired = self._call(repair_prompt, limited_context)
                return self._parse_output(repaired)
            except Exception as second_error:
                raise PlanGenerationError(
                    f"Planner output remained invalid after one repair attempt: {second_error}"
                ) from second_error
