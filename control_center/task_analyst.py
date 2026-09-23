"""Prompt rewriting before structured orchestration planning.

The Task Analyst receives the original user prompt, has no tools or workspace
authority, and returns a bounded operational brief.  That brief replaces the
human prompt for planning and delegation while the original remains persisted
as audit evidence. Capability policy remains the only authority for execution.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterable

from .config import validate_endpoint
from .security import sanitize
from .transport import model_profile, model_request, request_json


TASK_ANALYSIS_VERSION = 3
MAX_ANALYSIS_TEXT_CHARS = 4000
MAX_ANALYSIS_INPUT_CHARS = 12000
MAX_ANALYSIS_ITEMS = 30
DEFAULT_ANALYST_TIMEOUT_SECONDS = 60.0
TASK_ANALYST_ROLE = "task_analyst"
TASK_KINDS = {
    "file_creation", "program_creation", "code_change", "analysis",
    "testing", "review", "external_action", "general",
}

ANALYSIS_FIELDS = {
    "operational_prompt", "objective", "task_type", "task_kind", "requirements", "assumptions", "task_characteristics",
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
        "operational_prompt": {"type": "string"},
        "objective": {"type": "string"},
        "task_type": {"type": "string"},
        "task_kind": {"type": "string", "enum": sorted(TASK_KINDS)},
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


def _stable_analysis_ids(items: list[dict[str, Any]], prefix: str
                         ) -> tuple[list[str], dict[str, str], set[str]]:
    """Allocate namespace IDs while retaining unique references to old IDs."""
    pattern = re.compile(rf"{prefix}-([1-9][0-9]*)\Z", re.I)
    raw_ids = [item.get("id") for item in items]
    counts: dict[str, int] = {}
    for raw_id in raw_ids:
        if isinstance(raw_id, str) and raw_id.strip():
            key = raw_id.strip().casefold()
            counts[key] = counts.get(key, 0) + 1
    assigned: list[str | None] = [None] * len(items)
    reserved: set[str] = set()
    for index, raw_id in enumerate(raw_ids):
        match = (pattern.fullmatch(raw_id.strip())
                 if isinstance(raw_id, str) and len(raw_id.strip()) <= 64 else None)
        if match is None:
            continue
        candidate = f"{prefix}-{int(match.group(1))}"
        if candidate not in reserved:
            assigned[index] = candidate
            reserved.add(candidate)
    next_number = 1
    for index, identifier in enumerate(assigned):
        if identifier is not None:
            continue
        while f"{prefix}-{next_number}" in reserved:
            next_number += 1
        assigned[index] = f"{prefix}-{next_number}"
        reserved.add(assigned[index])
        next_number += 1
    lookup = {
        raw_id.strip().casefold(): assigned[index]
        for index, raw_id in enumerate(raw_ids)
        if isinstance(raw_id, str) and raw_id.strip()
        and counts[raw_id.strip().casefold()] == 1
    }
    ambiguous = {key for key, count in counts.items() if count > 1}
    return [str(identifier) for identifier in assigned], lookup, ambiguous


def _bounded_prompt(prompt: str, limit: int) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise TaskAnalysisError("prompt must not be empty.")
    prompt = prompt.strip()
    if len(prompt) <= limit:
        return prompt
    head = max(1, (limit - 40) // 2)
    tail = max(1, limit - 40 - head)
    return prompt[:head] + "\n...[prompt truncated for analysis]...\n" + prompt[-tail:]


def canonical_task_kind(task_type: Any = "", text: Any = "",
                        characteristics: dict[str, Any] | None = None) -> str:
    """Map human labels to a stable category shared by planning and factory."""
    value = " ".join([str(task_type or ""), str(text or "")]).casefold()
    characteristics = characteristics if isinstance(characteristics, dict) else {}
    if characteristics.get("requires_external_service") or characteristics.get("requires_network"):
        return "external_action"
    if re.search(r"\b(?:audit|review|code review|revis(?:a|ar)|inspecciona el c[oó]digo)\b", value):
        return "review"
    if re.search(r"\b(?:test|testing|qa|prueba|probar|validar tests?)\b", value):
        return "testing"
    if re.search(r"\b(?:debug|diagnos|root cause|fix|bug|error|arregla)\b", value):
        return "analysis"
    if re.search(r"\b(?:python|programa|program|script|c[oó]digo ejecutable|imprima|print)\b", value):
        return "program_creation"
    if re.search(r"\b(?:crea|crear|create|archivo|file|documento|document|config|notes?|hola mundo|hello world)\b", value):
        return "file_creation"
    if re.search(r"\b(?:implement|modific|modify|edit|refactor|cambio|change)\b", value):
        return "code_change"
    return "general"


def validate_task_analysis(value: Any) -> dict[str, Any]:
    """Validate and bound the operational rewrite returned by the analyst."""
    if not isinstance(value, dict):
        raise TaskAnalysisError("Task analysis must be an object.")
    value = dict(value)
    # The version is added to normalized results so persisted events and
    # planner context can identify the contract. Accept it on revalidation,
    # but never allow another version through the boundary.
    supplied_version = value.pop("analysis_version", TASK_ANALYSIS_VERSION)
    if supplied_version != TASK_ANALYSIS_VERSION:
        raise TaskAnalysisError("Unsupported task analysis version.")
    if "task_kind" not in value:
        value["task_kind"] = canonical_task_kind(
            value.get("task_type"), value.get("objective") or value.get("operational_prompt"),
            value.get("task_characteristics"),
        )
    missing = ANALYSIS_FIELDS - value.keys()
    unknown = value.keys() - ANALYSIS_FIELDS
    if missing:
        raise TaskAnalysisError("Task analysis is missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise TaskAnalysisError("Task analysis has unknown fields: " + ", ".join(sorted(unknown)))

    requirements = _items(value["requirements"], "requirements")
    requirement_ids, requirement_lookup, ambiguous_requirements = _stable_analysis_ids(
        requirements, "REQ")
    normalized_requirements = []
    for index, item in enumerate(requirements):
        if not {"description", "source"} <= set(item) or set(item) - {"id", "description", "source"}:
            raise TaskAnalysisError("Each requirement must contain description and source.")
        source = _text(item["source"], "requirement.source")
        if source not in {"explicit", "inferred"}:
            raise TaskAnalysisError("requirement.source must be explicit or inferred.")
        normalized_requirements.append({
            "id": requirement_ids[index],
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
    criterion_ids, _, _ = _stable_analysis_ids(criteria, "AC")
    normalized_criteria = []
    for index, item in enumerate(criteria):
        if not {"description", "verifies"} <= set(item) or set(item) - {"id", "description", "verifies"}:
            raise TaskAnalysisError("Each acceptance criterion has an invalid schema.")
        verifies = []
        for ref in _text_list(item["verifies"], "acceptance_criterion.verifies"):
            key = ref.casefold()
            if key in ambiguous_requirements:
                raise TaskAnalysisError("Acceptance criterion reference is ambiguous after requirement ID repair.")
            mapped = requirement_lookup.get(key)
            if mapped is None and len(normalized_requirements) == 1 and ref.upper() == requirement_ids[0]:
                mapped = requirement_ids[0]
            if mapped is None:
                raise TaskAnalysisError("Acceptance criteria may only reference known requirements.")
            if mapped not in verifies:
                verifies.append(mapped)
        normalized_criteria.append({
            "id": criterion_ids[index],
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
    if value["task_kind"] not in TASK_KINDS:
        raise TaskAnalysisError("task_kind must be a canonical task category.")

    return sanitize({
        "analysis_version": TASK_ANALYSIS_VERSION,
        "operational_prompt": _text(value["operational_prompt"], "operational_prompt"),
        "objective": _text(value["objective"], "objective"),
        "task_type": _text(value["task_type"], "task_type"),
        "task_kind": value["task_kind"],
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
    requires_write = windows_script or calculator or bool(re.search(
        r"\b(?:crea|crear|create|escrib|write|implement|program|codig|code|archivo|file|script|modific|edit)\w*\b",
        lowered,
    ))
    requires_read = requires_write or bool(re.search(
        r"\b(?:lee|leer|read|inspect|review|revis|analiz|analy|debug|diagnos)\w*\b",
        lowered,
    ))
    task_type = "windows_command_script" if windows_script else "general_task"
    task_kind = canonical_task_kind(task_type, objective, {
        "requires_external_service": False, "requires_network": False,
    })
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
        "requires_filesystem_read": requires_read,
        "requires_filesystem_write": requires_write,
        "requires_code_execution": calculator or windows_script or "script" in lowered or "python" in lowered,
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
    operational_prompt = _bounded_prompt((
        "Implement the following request exactly:\n" + objective
        + ("\n\nArtifact constraint: create a Windows CMD/BAT script, not a Python substitute."
           if windows_script else "")
        + ("\n\nValidation constraint: the program requires user input. Do not launch it without "
           "controlled stdin; a QA Tester must exercise representative input and verify the output."
           if requires_input else "")
    ), MAX_ANALYSIS_TEXT_CHARS)
    return validate_task_analysis({
        "operational_prompt": operational_prompt,
        "objective": objective,
        "task_type": task_type,
        "task_kind": task_kind,
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
            "You are Freya's Task Analyst and prompt engineer. Rewrite the human request into a precise, "
            "self-contained operational_prompt for the planner and delegated agents. Return only the strict "
            "JSON schema requested. Preserve every explicit requirement, label assumptions, detect interactive "
            "input and risks, and include concrete acceptance and validation instructions. Give requirements "
            "unique REQ-N IDs and acceptance criteria unique AC-N IDs; verifies may name only existing REQ-N IDs. "
            "For a standalone "
            "program with no language named, assume Python 3.10+ and state that assumption. For a code change, "
            "preserve the target project's existing language. Do not block only to ask for a language when "
            "these safe choices apply. Preserve an explicitly named language. Never execute tools "
            "or claim work was completed. Your operational_prompt replaces the human wording downstream.\n\n"
            + instructions[:16000]
        )
        started = time.monotonic()
        self.metrics = {"model_calls": 0, "prompt_tokens": 0, "generated_tokens": 0,
                        "total_tokens": 0, "duration_seconds": 0.0}

        def call(user_content: str, *, repair: bool = False) -> str:
            call_metrics: dict[str, Any] = {}
            self.metrics["model_calls"] += 1
            try:
                response = model_request(self.request, "task_analyst",
                    "POST", endpoint.rstrip("/") + "/api/chat",
                    {"model": model, "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ], "tools": [], "format": ANALYSIS_RESPONSE_FORMAT, "stream": False,
                     "think": False, "options": {
                         "temperature": 0,
                         "num_ctx": int(config.get("context_window", 8192)),
                         "num_predict": (model_profile("task_analyst").repair_output_tokens
                                         if repair else model_profile("task_analyst").max_output_tokens),
                     }},
                    timeout=timeout, telemetry=call_metrics,
                )
            finally:
                if isinstance(call_metrics.get("transport"), dict):
                    self.metrics.setdefault("model_call_details", []).append(call_metrics["transport"])
            for target, source in (("prompt_tokens", "prompt_eval_count"),
                                   ("generated_tokens", "eval_count")):
                value = response.get(source, 0) or 0
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise TaskAnalysisError("Task Analyst returned invalid token metrics.")
                self.metrics[target] = self.metrics.get(target, 0) + int(value)
            message = response.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise TaskAnalysisError("Task Analyst returned no content.")
            return message["content"]

        try:
            content = call("Original user prompt:\n" + _bounded_prompt(prompt, MAX_ANALYSIS_INPUT_CHARS))
            try:
                result = validate_task_analysis(json.loads(content))
            except (json.JSONDecodeError, TaskAnalysisError) as first_error:
                repair_prompt = (
                    "Repair only the Task Analyst contract inconsistencies in the previous JSON. "
                    "Return the complete JSON object using exactly the requested schema. "
                    "Do not invent work, permissions, requirements, or a blocking_reason. "
                    "If the task is executable, set ready_for_execution true; otherwise provide a "
                    "blocking_reason grounded in the original prompt. Validation error: "
                    + str(first_error) + "\nPrevious response:\n" + str(content)[:MAX_ANALYSIS_TEXT_CHARS]
                )
                try:
                    repaired_content = call(repair_prompt, repair=True)
                    result = validate_task_analysis(json.loads(repaired_content))
                except (json.JSONDecodeError, TaskAnalysisError) as second_error:
                    raise TaskAnalysisError(
                        "Task Analyst output remained invalid after one repair attempt: " + str(second_error)
                    ) from second_error
            return result
        finally:
            self.metrics["total_tokens"] = self.metrics["prompt_tokens"] + self.metrics["generated_tokens"]
            self.metrics["duration_seconds"] = round(time.monotonic() - started, 4)


_PROGRAMMING_LANGUAGES = (
    (r"\bpython(?:\s*3(?:\.\d+)?)?\b", "Python"),
    (r"\bjavascript\b|\bjs\b", "JavaScript"),
    (r"\btypescript\b|\bts\b", "TypeScript"),
    (r"\bjava\b", "Java"),
    (r"(?<!\w)c\+\+(?!\w)", "C++"),
    (r"(?<!\w)c#(?!\w)|\bcsharp\b", "C#"),
    (r"\brust\b", "Rust"),
    (r"\bgolang\b|\bgo\s+(?:language|program|script|code)\b|"
     r"\b(?:in|using|with|use|usar|en)\s+go\b", "Go"),
    (r"\bruby\b", "Ruby"),
    (r"\bphp\b", "PHP"),
    (r"\bkotlin\b", "Kotlin"),
    (r"\bswift\b", "Swift"),
    (r"\bpowershell\b|\bpwsh\b", "PowerShell"),
    (r"\bbash\b", "Bash"),
    (r"\bsql\b", "SQL"),
    (r"\bc\b", "C"),
)


def _requested_programming_language(prompt: str) -> str | None:
    lowered = str(prompt or "").casefold()
    return next((name for pattern, name in _PROGRAMMING_LANGUAGES
                 if re.search(pattern, lowered)), None)


def _only_language_is_blocking(reason: str) -> bool:
    """Recognize a single missing-language question, not compound blockers."""
    clauses = [item.strip() for item in re.split(
        r"[.;\n]+|\b(?:and|also|plus|as well|y|adem[aá]s|tambi[eé]n)\b",
        str(reason or ""), flags=re.I,
    ) if item.strip()]
    if len(clauses) != 1:
        return False
    clause = clauses[0].casefold()
    return (bool(re.search(r"\b(?:language|idioma)\b", clause))
            and bool(re.search(
                r"\b(?:specif\w*|clarif\w*|missing|unspecified|need\w*|require\w*|"
                r"especific\w*|indic\w*|defin\w*|falt\w*)\b", clause,
            )))


def reconcile_task_analysis(prompt: str, analysis: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Enforce prompt-observable safety facts on a schema-valid model result.

    A model can satisfy the JSON schema while contradicting obvious facts in the
    source prompt.  The deterministic interpretation is therefore used as a
    one-way floor: it may turn requirements on, but never remove model-detected
    requirements.  The corrected operational prompt is the only prompt sent
    downstream.
    """
    normalized = validate_task_analysis(analysis)
    detected = deterministic_task_analysis(prompt)
    corrected = dict(normalized)
    changes: list[str] = []

    model_characteristics = dict(normalized["task_characteristics"])
    detected_characteristics = detected["task_characteristics"]
    for field, required in detected_characteristics.items():
        if required and not model_characteristics.get(field):
            model_characteristics[field] = True
            changes.append("task_characteristics." + field)
    corrected["task_characteristics"] = model_characteristics

    if detected["task_type"] != "general_task" and normalized["task_type"] != detected["task_type"]:
        corrected["task_type"] = detected["task_type"]
        changes.append("task_type")

    model_text = " ".join([
        str(normalized.get("operational_prompt") or ""),
        str(normalized.get("objective") or ""),
        str(normalized.get("task_type") or ""),
        " ".join(str(item.get("description") or "") for item in normalized.get("requirements", [])
                  if isinstance(item, dict)),
        str(normalized.get("validation", {}).get("strategy") or ""),
        " ".join(str(item.get("description") or "") for item in normalized.get("validation", {}).get("tests", [])
                  if isinstance(item, dict)),
    ]).casefold()
    model_describes_program = (
        normalized.get("task_kind") == "program_creation"
        or (
            normalized.get("task_kind") in {"general", "file_creation"}
            and model_characteristics.get("requires_code_execution") is True
            and bool(re.search(
                r"\b(?:program|programa|script|execute|execut|run|stdout|output|salida|print|imprima)\b",
                model_text,
            ))
        )
    )
    # The model's structured evidence is stronger than a broad lexical
    # fallback.  This preserves a model-backed program interpretation even
    # when the human wording is intentionally terse (without assuming a
    # language from words such as "hello world").
    if model_describes_program and normalized.get("task_kind") != "program_creation":
        corrected["task_kind"] = "program_creation"
        changes.append("task_kind")
    elif (normalized.get("task_kind") != detected.get("task_kind")
          and detected.get("task_kind") != "general"):
        corrected["task_kind"] = detected["task_kind"]
        changes.append("task_kind")

    if corrected.get("task_kind") in {"program_creation", "code_change"}:
        requested_language = _requested_programming_language(prompt)
        if requested_language is not None:
            language_instruction = (
                "Programming language: " + requested_language + " (explicitly requested)."
            )
        elif corrected.get("task_kind") == "code_change":
            language_instruction = (
                "Programming language: inspect and preserve the existing project's language; "
                "use Python 3.10+ only for standalone work without an established project stack."
            )
        else:
            language_instruction = (
                "Programming language: Python 3.10+ "
                "(assumption because the standalone program request did not specify one)."
            )
        if requested_language is None:
            assumptions = list(corrected.get("assumptions", []))
            if corrected.get("task_kind") == "code_change":
                assumption = {
                    "description": "Preserve the existing project's language; use Python 3.10+ only if no project stack exists.",
                    "reason": "The request did not name a language; code changes should follow the target project's existing technology.",
                }
            else:
                assumption = {
                    "description": "Use Python 3.10+ for this standalone program.",
                    "reason": "The request did not name a programming language; Python is Freya's default for standalone program creation.",
                }
            if assumption not in assumptions:
                assumptions.append(assumption)
                corrected["assumptions"] = assumptions[:MAX_ANALYSIS_ITEMS]
                changes.append("assumptions")
        if (corrected.get("ready_for_execution") is False
                and _only_language_is_blocking(corrected.get("blocking_reason", ""))):
            corrected["ready_for_execution"] = True
            corrected["blocking_reason"] = None
            changes.extend(["ready_for_execution", "blocking_reason"])
        operational = str(corrected.get("operational_prompt") or "").strip()
        if language_instruction.casefold() not in operational.casefold():
            suffix = "\n\n" + language_instruction
            operational = operational[:max(1, MAX_ANALYSIS_TEXT_CHARS - len(suffix))] + suffix
            corrected["operational_prompt"] = operational
            changes.append("operational_prompt")

    validation = dict(normalized["validation"])
    if detected["validation"]["interactive_validation_required"] and not validation["interactive_validation_required"]:
        validation["interactive_validation_required"] = True
        changes.append("validation.interactive_validation_required")
    validation["avoid"] = list(dict.fromkeys([
        *validation["avoid"], *detected["validation"]["avoid"],
    ]))[:MAX_ANALYSIS_ITEMS]
    corrected["validation"] = validation
    corrected["risks"] = list(dict.fromkeys(
        (item["risk"], item["prevention"])
        for item in [*normalized["risks"], *detected["risks"]]
    ))
    corrected["risks"] = [
        {"risk": risk, "prevention": prevention}
        for risk, prevention in corrected["risks"][:MAX_ANALYSIS_ITEMS]
    ]

    additions: list[str] = []
    if corrected["task_type"] == "windows_command_script":
        additions.append("Create a Windows CMD/BAT artifact; do not substitute Python.")
    if validation["interactive_validation_required"]:
        additions.append(
            "The result is interactive. The implementation agent must not wait for terminal input; "
            "a QA Tester must run it with bounded controlled stdin and verify logical output."
        )
    operational = str(corrected["operational_prompt"] or "").strip()
    if operational.casefold() in {"{}", "[]", "null"}:
        # Some local models satisfy the string schema with a JSON placeholder.
        # Keep the deterministic operational brief authoritative instead of
        # sending that placeholder to Planner and workers.
        operational = detected["operational_prompt"]
        changes.append("operational_prompt")
    for addition in additions:
        if addition.casefold() not in operational.casefold():
            suffix = "\n\nMandatory constraint: " + addition
            operational = operational[:max(1, MAX_ANALYSIS_TEXT_CHARS - len(suffix))] + suffix
            changes.append("operational_prompt")
    corrected["operational_prompt"] = operational
    return validate_task_analysis(corrected), list(dict.fromkeys(changes))


def reconciled_deterministic_task_analysis(prompt: str) -> tuple[dict[str, Any], list[str]]:
    """Apply the same prompt-grounded corrections to deterministic analysis."""
    return reconcile_task_analysis(prompt, deterministic_task_analysis(prompt))


class TaskAnalyst:
    """Wrapper that supports explicit offline mode and deterministic fallback."""

    def __init__(self, adapter: OllamaTaskAnalyst | None = None, *, offline: bool = False):
        self.adapter = adapter
        self.offline = bool(offline)
        self.metrics: dict[str, Any] = {}

    def analyze(self, prompt: str, agent: dict[str, Any]) -> dict[str, Any]:
        if self.offline or self.adapter is None:
            result, corrections = reconciled_deterministic_task_analysis(prompt)
            self.metrics = {
                "model_calls": 0, "mode": "deterministic",
                "corrected_fields": corrections,
            }
            return result
        try:
            result, corrections = reconcile_task_analysis(prompt, self.adapter.analyze(prompt, agent))
            self.metrics = dict(self.adapter.metrics)
            self.metrics["mode"] = "model"
            self.metrics["corrected_fields"] = corrections
            return result
        except Exception:
            self.metrics = dict(self.adapter.metrics)
            self.metrics["mode"] = "deterministic_fallback"
            raise
