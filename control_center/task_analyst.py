"""Prompt interpretation before structured orchestration planning.

The Task Analyst is advisory only. It receives the original user prompt, has no
tools or workspace authority, and returns a bounded JSON specification for the
planner. Capability policy remains the only authority for execution.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterable

from .config import validate_endpoint
from .security import sanitize
from .transport import request_json


TASK_ANALYSIS_VERSION = 1
MAX_ANALYSIS_TEXT_CHARS = 4000
MAX_ANALYSIS_INPUT_CHARS = 12000
MAX_ANALYSIS_ITEMS = 30
DEFAULT_ANALYST_TIMEOUT_SECONDS = 60.0
TASK_ANALYST_ROLE = "task_analyst"

ANALYSIS_FIELDS = {
    "objective", "task_type", "requirements", "assumptions", "task_characteristics",
    "risks", "plan", "acceptance_criteria", "validation", "recommended_agent_role",
    "ready_for_execution", "blocking_reason",
}
CHARACTERISTIC_FIELDS = {
    "interactive", "requires_user_input", "requires_filesystem_read",
    "requires_filesystem_write", "requires_code_execution", "requires_network",
    "requires_gui", "requires_external_service", "requires_elevated_privileges",
    "potentially_destructive", "long_running", "requires_human_approval",
}

ANALYSIS_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "task_type": {"type": "string"},
        "requirements": {"type": "array", "items": {"type": "object"}},
        "assumptions": {"type": "array", "items": {"type": "object"}},
        "task_characteristics": {
            "type": "object",
            "properties": {key: {"type": "boolean"} for key in sorted(CHARACTERISTIC_FIELDS)},
            "required": sorted(CHARACTERISTIC_FIELDS),
            "additionalProperties": False,
        },
        "risks": {"type": "array", "items": {"type": "object"}},
        "plan": {"type": "array", "items": {"type": "object"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "object"}},
        "validation": {"type": "object"},
        "recommended_agent_role": {"type": "string"},
        "ready_for_execution": {"type": "boolean"},
        "blocking_reason": {"type": ["string", "null"]},
    },
    "required": sorted(ANALYSIS_FIELDS),
    "additionalProperties": False,
}


class TaskAnalysisError(RuntimeError):
    """The Task Analyst could not produce a valid interpretation."""


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TaskAnalysisError(f"{field} must be text.")
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise TaskAnalysisError(f"{field} must not be empty.")
    if len(value) > MAX_ANALYSIS_TEXT_CHARS:
        raise TaskAnalysisError(f"{field} is too long.")
    return value


def _items(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ANALYSIS_ITEMS:
        raise TaskAnalysisError(f"{field} must contain at most {MAX_ANALYSIS_ITEMS} items.")
    if any(not isinstance(item, dict) for item in value):
        raise TaskAnalysisError(f"{field} must contain objects.")
    return value


def _text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ANALYSIS_ITEMS:
        raise TaskAnalysisError(f"{field} must contain at most {MAX_ANALYSIS_ITEMS} items.")
    result = []
    for item in value:
        result.append(_text(item, field))
    return result


def _bounded_prompt(prompt: str, limit: int) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise TaskAnalysisError("prompt must not be empty.")
    prompt = prompt.strip()
    if len(prompt) <= limit:
        return prompt
    head = max(1, (limit - 40) // 2)
    tail = max(1, limit - 40 - head)
    return prompt[:head] + "\n...[prompt truncated for analysis]...\n" + prompt[-tail:]


def validate_task_analysis(value: Any) -> dict[str, Any]:
    """Validate and bound the advisory interpretation returned by the analyst."""
    if not isinstance(value, dict):
        raise TaskAnalysisError("Task analysis must be an object.")
    value = dict(value)
    # The version is added to normalized results so persisted events and
    # planner context can identify the contract. Accept it on revalidation,
    # but never allow another version through the boundary.
    supplied_version = value.pop("analysis_version", TASK_ANALYSIS_VERSION)
    if supplied_version != TASK_ANALYSIS_VERSION:
        raise TaskAnalysisError("Unsupported task analysis version.")
    missing = ANALYSIS_FIELDS - value.keys()
    unknown = value.keys() - ANALYSIS_FIELDS
    if missing:
        raise TaskAnalysisError("Task analysis is missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise TaskAnalysisError("Task analysis has unknown fields: " + ", ".join(sorted(unknown)))

    requirements = _items(value["requirements"], "requirements")
    normalized_requirements = []
    for item in requirements:
        if set(item) != {"id", "description", "source"}:
            raise TaskAnalysisError("Each requirement must contain id, description and source.")
        source = _text(item["source"], "requirement.source")
        if source not in {"explicit", "inferred"}:
            raise TaskAnalysisError("requirement.source must be explicit or inferred.")
        normalized_requirements.append({
            "id": _text(item["id"], "requirement.id"),
            "description": _text(item["description"], "requirement.description"),
            "source": source,
        })
    if not normalized_requirements:
        raise TaskAnalysisError("Task analysis must preserve at least one requirement.")

    assumptions = _items(value["assumptions"], "assumptions")
    normalized_assumptions = []
    for item in assumptions:
        if set(item) != {"description", "reason"}:
            raise TaskAnalysisError("Each assumption must contain description and reason.")
        normalized_assumptions.append({
            "description": _text(item["description"], "assumption.description"),
            "reason": _text(item["reason"], "assumption.reason"),
        })

    characteristics = value["task_characteristics"]
    if not isinstance(characteristics, dict) or set(characteristics) != CHARACTERISTIC_FIELDS:
        raise TaskAnalysisError("task_characteristics has an invalid schema.")
    if any(not isinstance(item, bool) for item in characteristics.values()):
        raise TaskAnalysisError("task_characteristics values must be boolean.")

    risks = _items(value["risks"], "risks")
    normalized_risks = []
    for item in risks:
        if set(item) != {"risk", "prevention"}:
            raise TaskAnalysisError("Each risk must contain risk and prevention.")
        normalized_risks.append({
            "risk": _text(item["risk"], "risk.risk"),
            "prevention": _text(item["prevention"], "risk.prevention"),
        })

    plan = _items(value["plan"], "plan")
    normalized_plan = []
    for index, item in enumerate(plan, 1):
        if set(item) != {"step", "agent_role", "objective", "expected_result"}:
            raise TaskAnalysisError("Each analysis plan step has an invalid schema.")
        step = item["step"]
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            raise TaskAnalysisError("plan.step must be a positive integer.")
        normalized_plan.append({
            "step": step,
            "agent_role": _text(item["agent_role"], "plan.agent_role"),
            "objective": _text(item["objective"], "plan.objective"),
            "expected_result": _text(item["expected_result"], "plan.expected_result"),
        })
    if not normalized_plan:
        raise TaskAnalysisError("Task analysis must contain at least one plan step.")

    criteria = _items(value["acceptance_criteria"], "acceptance_criteria")
    normalized_criteria = []
    requirement_ids = {item["id"] for item in normalized_requirements}
    for item in criteria:
        if set(item) != {"id", "description", "verifies"}:
            raise TaskAnalysisError("Each acceptance criterion has an invalid schema.")
        verifies = _text_list(item["verifies"], "acceptance_criterion.verifies")
        if any(ref not in requirement_ids for ref in verifies):
            raise TaskAnalysisError("Acceptance criteria may only reference known requirements.")
        normalized_criteria.append({
            "id": _text(item["id"], "acceptance_criterion.id"),
            "description": _text(item["description"], "acceptance_criterion.description"),
            "verifies": verifies,
        })
    if not normalized_criteria:
        raise TaskAnalysisError("Task analysis must contain acceptance criteria.")

    validation = value["validation"]
    if not isinstance(validation, dict):
        raise TaskAnalysisError("validation must be an object.")
    if set(validation) != {"strategy", "interactive_validation_required", "tests", "avoid"}:
        raise TaskAnalysisError("validation has an invalid schema.")
    if not isinstance(validation["interactive_validation_required"], bool):
        raise TaskAnalysisError("validation.interactive_validation_required must be boolean.")
    validation_tests = _items(validation["tests"], "validation.tests")
    normalized_tests = []
    for item in validation_tests:
        if set(item) != {"description", "expected_result"}:
            raise TaskAnalysisError("Each validation test has an invalid schema.")
        normalized_tests.append({
            "description": _text(item["description"], "validation.test.description"),
            "expected_result": _text(item["expected_result"], "validation.test.expected_result"),
        })

    ready = value["ready_for_execution"]
    if not isinstance(ready, bool):
        raise TaskAnalysisError("ready_for_execution must be boolean.")
    blocking_reason = value["blocking_reason"]
    if blocking_reason is not None:
        blocking_reason = _text(blocking_reason, "blocking_reason")
    if not ready and not blocking_reason:
        raise TaskAnalysisError("A blocked analysis requires blocking_reason.")

    return sanitize({
        "analysis_version": TASK_ANALYSIS_VERSION,
        "objective": _text(value["objective"], "objective"),
        "task_type": _text(value["task_type"], "task_type"),
        "requirements": normalized_requirements,
        "assumptions": normalized_assumptions,
        "task_characteristics": dict(characteristics),
        "risks": normalized_risks,
        "plan": normalized_plan,
        "acceptance_criteria": normalized_criteria,
        "validation": {
            "strategy": _text(validation["strategy"], "validation.strategy"),
            "interactive_validation_required": validation["interactive_validation_required"],
            "tests": normalized_tests,
            "avoid": _text_list(validation["avoid"], "validation.avoid"),
        },
        "recommended_agent_role": _text(value["recommended_agent_role"], "recommended_agent_role"),
        "ready_for_execution": ready,
        "blocking_reason": blocking_reason,
    })


def is_task_analyst(agent: dict[str, Any]) -> bool:
    """Identify an analyst by explicit role first, with a legacy-safe fallback."""
    if not isinstance(agent, dict) or agent.get("enabled") is not True:
        return False
    config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
    explicit = str(config.get("orchestration_role") or "").strip().casefold()
    if explicit:
        return explicit == TASK_ANALYST_ROLE
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    values = [agent.get("name"), agent.get("role"), agent.get("description"),
              identity.get("name"), identity.get("role"), identity.get("purpose")]
    text = " ".join(str(value or "") for value in values).casefold()
    return "task analyst" in text or ("analyst" in text and "planner" in text)


def select_task_analyst(agents: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [agent for agent in agents if is_task_analyst(agent)]
    if not candidates:
        return None

    def rank(agent: dict[str, Any]) -> tuple[int, str]:
        config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
        explicit = str(config.get("orchestration_role") or "").strip().casefold()
        name = str(agent.get("name") or "").casefold()
        role = str(agent.get("role") or "").casefold()
        return (0 if explicit == TASK_ANALYST_ROLE else 1 if name == "task analyst" else 2,
                str(agent.get("id") or role))

    return sorted(candidates, key=rank)[0]


def deterministic_task_analysis(prompt: str) -> dict[str, Any]:
    """Produce a conservative interpretation if the analyst model is unavailable."""
    objective = _text(_bounded_prompt(prompt, MAX_ANALYSIS_TEXT_CHARS), "prompt")
    lowered = objective.casefold()
    windows_script = bool(re.search(r"\b(?:cmd|cdm|bat|batch|command prompt)\b", lowered))
    interactive = bool(re.search(r"\b(?:interactiv|input|ingres|introdu|no cierre|pause|pausa)\w*\b", lowered))
    calculator = "calculadora" in lowered or "calculator" in lowered
    requires_input = interactive or calculator
    task_type = "windows_command_script" if windows_script else "general_task"
    requirements = [{"id": "REQ-1", "description": objective, "source": "explicit"}]
    assumptions = []
    if windows_script:
        assumptions.append({
            "description": "El artefacto objetivo es un script CMD/BAT de Windows, no un script Python.",
            "reason": "El prompt menciona CMD, CDM, BAT o batch.",
        })
    characteristics = {
        "interactive": interactive,
        "requires_user_input": requires_input,
        "requires_filesystem_read": False,
        "requires_filesystem_write": True,
        "requires_code_execution": calculator or windows_script or "script" in lowered,
        "requires_network": False,
        "requires_gui": False,
        "requires_external_service": False,
        "requires_elevated_privileges": False,
        "potentially_destructive": False,
        "long_running": False,
        "requires_human_approval": False,
    }
    risks = []
    avoid = []
    if requires_input:
        risk = "La validación puede bloquearse si se ejecuta el programa sin stdin."
        prevention = "Usar argumentos o una entrada controlada; no repetir el comando aumentando solo el timeout."
        risks.append({"risk": risk, "prevention": prevention})
        avoid.append("Lanzar un programa interactivo sin una estrategia de entrada controlada.")
    if windows_script:
        avoid.append("Sustituir silenciosamente el artefacto CMD/BAT por un archivo Python.")
    plan = [{
        "step": 1,
        "agent_role": "Programmer",
        "objective": "Implementar el resultado solicitado respetando el tipo de artefacto y sus requisitos.",
        "expected_result": "El artefacto requerido existe y cumple los criterios observables.",
    }]
    criteria = [{"id": "AC-1", "description": "El resultado cumple el objetivo original del usuario.", "verifies": ["REQ-1"]}]
    return validate_task_analysis({
        "objective": objective,
        "task_type": task_type,
        "requirements": requirements,
        "assumptions": assumptions,
        "task_characteristics": characteristics,
        "risks": risks,
        "plan": plan,
        "acceptance_criteria": criteria,
        "validation": {
            "strategy": "Validación determinista compatible con las características detectadas.",
            "interactive_validation_required": requires_input,
            "tests": [{"description": "Comprobar el artefacto y su comportamiento observable.",
                        "expected_result": "La comprobación termina sin bloquearse."}],
            "avoid": avoid,
        },
        "recommended_agent_role": "Programmer",
        "ready_for_execution": True,
        "blocking_reason": None,
    })


class OllamaTaskAnalyst:
    """Run one tool-free analysis using the selected Task Analyst's model config."""

    def __init__(self, request: Callable[..., dict[str, Any]] = request_json):
        self.request = request
        self.metrics: dict[str, Any] = {}

    def analyze(self, prompt: str, agent: dict[str, Any]) -> dict[str, Any]:
        config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
        model = str(config.get("model") or "").strip()
        endpoint = validate_endpoint(str(config.get("endpoint") or "http://127.0.0.1:11434"))
        timeout = config.get("max_seconds", DEFAULT_ANALYST_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            timeout = DEFAULT_ANALYST_TIMEOUT_SECONDS
        timeout = max(0.1, min(float(timeout), 120.0))
        instructions = str(agent.get("instructions") or "").strip()
        system = (
            "You are Freya's Task Analyst. Interpret the original user prompt before execution. "
            "Return only the strict JSON schema requested. Preserve explicit requirements, label "
            "assumptions, detect interactive input and risks, and never execute tools or claim work "
            "was completed. The interpretation is advisory; the original prompt remains authoritative.\n\n"
            + instructions[:16000]
        )
        started = time.monotonic()
        self.metrics = {"model_calls": 1, "prompt_tokens": 0, "generated_tokens": 0,
                        "total_tokens": 0, "duration_seconds": 0.0}
        response = self.request(
            "POST", endpoint.rstrip("/") + "/api/chat",
            {"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Original user prompt:\n" +
                 _bounded_prompt(prompt, MAX_ANALYSIS_INPUT_CHARS)},
            ], "tools": [], "format": ANALYSIS_RESPONSE_FORMAT, "stream": False,
             "think": False, "options": {"temperature": 0, "num_ctx": int(config.get("context_window", 8192)),
                                          "num_predict": 4096}},
            timeout=timeout,
        )
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise TaskAnalysisError("Task Analyst returned no content.")
        for target, source in (("prompt_tokens", "prompt_eval_count"),
                               ("generated_tokens", "eval_count")):
            value = response.get(source, 0) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise TaskAnalysisError("Task Analyst returned invalid token metrics.")
            self.metrics[target] = int(value)
        self.metrics["total_tokens"] = self.metrics["prompt_tokens"] + self.metrics["generated_tokens"]
        self.metrics["duration_seconds"] = round(time.monotonic() - started, 4)
        try:
            value = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise TaskAnalysisError("Task Analyst output is not valid JSON.") from exc
        return validate_task_analysis(value)


class TaskAnalyst:
    """Wrapper that supports explicit offline mode and deterministic fallback."""

    def __init__(self, adapter: OllamaTaskAnalyst | None = None, *, offline: bool = False):
        self.adapter = adapter
        self.offline = bool(offline)
        self.metrics: dict[str, Any] = {}

    def analyze(self, prompt: str, agent: dict[str, Any]) -> dict[str, Any]:
        if self.offline or self.adapter is None:
            self.metrics = {"model_calls": 0, "mode": "deterministic"}
            return deterministic_task_analysis(prompt)
        try:
            result = self.adapter.analyze(prompt, agent)
            self.metrics = dict(self.adapter.metrics)
            self.metrics["mode"] = "model"
            return result
        except Exception:
            self.metrics = dict(self.adapter.metrics)
            self.metrics["mode"] = "deterministic_fallback"
            raise
