"""Canonical, versioned user intent and the tool-free clarification boundary."""
from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from .config import validate_endpoint
from .security import sanitize
from .transport import model_profile, model_request, request_json


TASK_SPEC_SCHEMA_VERSION = 1


class TaskSpecStatus(str, Enum):
    ANALYZING = "ANALYZING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_PLANNING = "READY_FOR_PLANNING"


class RequirementSource(str, Enum):
    EXPLICIT = "explicit"
    CLARIFIED = "clarified"
    ASSUMED = "assumed"


TASK_SPEC_STATUSES = tuple(item.value for item in TaskSpecStatus)
TASK_ANALYST_OUTPUT_STATUSES = (
    TaskSpecStatus.NEEDS_CLARIFICATION.value,
    TaskSpecStatus.READY_FOR_PLANNING.value,
)
REQUIREMENT_SOURCES = tuple(item.value for item in RequirementSource)
# `inferred` appeared in older Analyst responses. It is a declared compatibility
# alias only; the canonical Task Spec persists `assumed`.
REQUIREMENT_SOURCE_ALIASES = {"inferred": RequirementSource.ASSUMED.value}
MAX_ITEMS = 30
MAX_TEXT = 4000
MAX_REPAIR_INPUT_CHARS = 12000
MAX_CLARIFICATION_ROUNDS = 3
QUESTION_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RESERVED_QUESTION_FIELDS = frozenset({
    "schema_version", "version", "status", "source_prompt", "objective",
    "user_intent", "deliverables", "requirements", "constraints",
    "user_decisions", "assumptions", "validation_expectations", "context",
    "clarification_questions", "clarification_history", "revision_changes",
    "readiness_reason",
})


def _source_entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "source": {"type": "string", "enum": list(REQUIREMENT_SOURCES)},
        },
        "required": ["description", "source"],
        "additionalProperties": False,
    }


TASK_SPEC_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(TASK_ANALYST_OUTPUT_STATUSES)},
        "objective": {"type": "string"},
        "user_intent": {"type": "string"},
        "deliverables": {"type": "array", "items": _source_entry_schema()},
        "requirements": {"type": "array", "items": _source_entry_schema()},
        "constraints": {"type": "array", "items": _source_entry_schema()},
        "user_decisions": {"type": "object", "additionalProperties": {"type": "string"}},
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["description", "reason"],
                "additionalProperties": False,
            },
        },
        "validation_expectations": {"type": "array", "items": {"type": "string"}},
        "context": {"type": "object", "additionalProperties": {
            "type": ["string", "boolean", "number", "null"],
        }},
        "clarification_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"}, "reason": {"type": "string"},
                    "field": {"type": "string"}, "required": {"type": "boolean"},
                },
                "required": ["question", "reason", "field"],
                "additionalProperties": False,
            },
        },
        "readiness_reason": {"type": "string"},
    },
    # Omitted intent collections have deterministic empty defaults. Cross-field
    # readiness invariants are enforced by the shared manual validator below.
    "required": ["status"],
    "additionalProperties": False,
}
TASK_SPEC_MODEL_FIELDS = frozenset(TASK_SPEC_RESPONSE_FORMAT["properties"])


class TaskSpecError(ValueError):
    def __init__(self, message: str, *, path: str = "$", expected: str = "valid Task Spec value",
                 received: Any = None, error_type: str = "validation_error"):
        super().__init__(message)
        self.path = path
        self.expected = expected
        self.received = _received_summary(received)
        self.error_type = error_type

    def diagnostic(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "error_path": self.path,
            "expected": self.expected,
            "received": self.received,
            "validation_message": sanitize(str(self))[:500],
        }


class ClarificationCycleError(TaskSpecError):
    def __init__(self):
        super().__init__("Clarification limit reached with material questions unresolved.",
                         path="clarification_questions", expected="at most three clarification rounds",
                         error_type="clarification_cycle_detected")


def _received_summary(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize(value)[:160]
    if isinstance(value, dict):
        return f"object with {len(value)} fields"
    if isinstance(value, list):
        return f"list with {len(value)} items"
    if value is None:
        return "null"
    return sanitize(repr(value))[:160]


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TaskSpecError(f"{name} must be text.", path=name, expected="string", received=value,
                            error_type="type_mismatch")
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise TaskSpecError(f"{name} must not be empty.", path=name, expected="non-empty string",
                            received=value, error_type="missing_value")
    if len(value) > MAX_TEXT:
        raise TaskSpecError(f"{name} is too long.", path=name,
                            expected=f"string with at most {MAX_TEXT} characters",
                            received=f"string with {len(value)} characters", error_type="value_too_long")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise TaskSpecError(f"{name} must be a list with at most {MAX_ITEMS} items.", path=name,
                            expected=f"list with at most {MAX_ITEMS} items", received=value,
                            error_type="type_or_length_mismatch")
    return value


def _question_field(value: Any, path: str) -> str:
    field = _text(value, path)
    if not QUESTION_FIELD_PATTERN.fullmatch(field) or field in RESERVED_QUESTION_FIELDS:
        raise TaskSpecError("Clarification field must be a semantic leaf key.", path=path,
                            expected="non-reserved snake_case leaf key", received=field,
                            error_type="invalid_question_field")
    return field


def _question_fingerprint(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


def _optional_question(question: dict[str, Any]) -> bool:
    if question.get("required") is False:
        return True
    field = str(question.get("field") or "").casefold()
    if any(token in field for token in ("additional", "extra", "optional", "bonus")) or field == "enhancements":
        return True
    wording = f"{question.get('question', '')} {question.get('reason', '')}".casefold()
    return bool(re.search(r"\b(?:additional|extra|optional|adicionales|extras|opcionales)\b", wording)
                or re.search(r"\b(?:agregar|añadir|add|include)\b.{0,40}\b(?:funcionalidad|feature)\b", wording))


def _next_question_id(previous: dict[str, Any]) -> int:
    ids = [item.get("id", "") for item in previous.get("clarification_questions", [])]
    ids += [item.get("question_id", "") for item in previous.get("clarification_history", [])]
    numbers = [int(match.group(1)) for value in ids
               if (match := re.fullmatch(r"CQ-(\d+)", str(value)))]
    return max(numbers, default=0) + 1


def _assign_question_ids(previous: dict[str, Any], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old = {item["field"]: item for item in previous["clarification_questions"]}
    next_id = _next_question_id(previous)
    assigned = []
    for question in questions:
        field = _question_field(question.get("field"), "clarification_questions.field")
        prior = old.get(field)
        assigned.append({**question, "id": prior["id"] if prior else f"CQ-{next_id}"})
        if prior is None:
            next_id += 1
    return assigned


def _apply_clarification_answers(previous: dict[str, Any], answers: dict[str, str]
                                 ) -> tuple[list[dict[str, str]], dict[str, str]]:
    history = list(previous["clarification_history"])
    decisions = dict(previous["user_decisions"])
    recorded = {item["question_id"] for item in history}
    for question in previous["clarification_questions"]:
        answer = str(answers.get(question["id"]) or "").strip()
        if not answer or question["id"] in recorded:
            continue
        # An expression of uncertainty is audit evidence, not a semantic decision.
        if _question_fingerprint(answer) in {"no se", "no sé", "decidilo vos", "lo vemos despues",
                                             "lo vemos después"}:
            continue
        history.append({"question_id": question["id"], "field": question["field"],
                        "question": question["question"], "answer": answer})
        decisions[question["field"]] = answer
        recorded.add(question["id"])
    return history, decisions


def _entries(value: Any, name: str, *, source: bool = False) -> list[dict[str, str]]:
    result = []
    for index, item in enumerate(_list(value, name)):
        item_path = f"{name}[{index}]"
        if not isinstance(item, dict):
            raise TaskSpecError(f"{name} entries must be objects.", path=item_path,
                                expected="object", received=item, error_type="type_mismatch")
        if source:
            origin = _text(item.get("source"), item_path + ".source").casefold()
            origin = REQUIREMENT_SOURCE_ALIASES.get(origin, origin)
            if origin not in REQUIREMENT_SOURCES:
                raise TaskSpecError(f"{item_path}.source is invalid.", path=item_path + ".source",
                                    expected="one of " + ", ".join(REQUIREMENT_SOURCES),
                                    received=origin, error_type="enum_mismatch")
            result.append({"description": _text(item.get("description"), item_path + ".description"),
                           "source": origin})
        else:
            result.append({"description": _text(item.get("description"), item_path + ".description"),
                           "reason": _text(item.get("reason"), item_path + ".reason")})
    return result


_ARRAY_FIELDS = (
    "deliverables", "requirements", "constraints", "assumptions",
    "validation_expectations", "clarification_questions", "clarification_history",
    "revision_changes",
)
_OBJECT_FIELDS = ("user_decisions", "context")


def normalize_task_spec_candidate(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Apply only format, container, enum-case and declared-alias corrections."""
    if not isinstance(value, dict):
        raise TaskSpecError("Task Spec must be an object.", path="$", expected="object",
                            received=value, error_type="type_mismatch")
    candidate = deepcopy(value)
    changes: list[str] = []
    for field in _ARRAY_FIELDS:
        if field in candidate and candidate[field] is None:
            candidate[field] = []
            changes.append(f"{field}:null_to_empty_list")
    for field in _OBJECT_FIELDS:
        if field in candidate and candidate[field] is None:
            candidate[field] = {}
            changes.append(f"{field}:null_to_empty_object")
    status = candidate.get("status")
    if isinstance(status, str):
        normalized = status.strip().upper()
        if normalized != status:
            candidate["status"] = normalized
            changes.append("status:enum_case_or_whitespace")
    for field in ("deliverables", "requirements", "constraints"):
        entries = candidate.get(field)
        if not isinstance(entries, list):
            continue
        for index, item in enumerate(entries):
            if not isinstance(item, dict) or not isinstance(item.get("source"), str):
                continue
            original = item["source"]
            normalized = original.strip().casefold()
            normalized = REQUIREMENT_SOURCE_ALIASES.get(normalized, normalized)
            if normalized != original:
                item["source"] = normalized
                changes.append(f"{field}[{index}].source:enum_case_or_alias")
    questions = candidate.get("clarification_questions")
    if isinstance(questions, list):
        for index, question in enumerate(questions):
            if isinstance(question, dict) and "id" in question:
                question.pop("id")
                changes.append(f"clarification_questions[{index}].id:runtime_assigned")
    return candidate, changes


def validate_task_spec(value: Any) -> dict[str, Any]:
    """Normalize harmless formatting while rejecting semantic or schema gaps."""
    if not isinstance(value, dict):
        raise TaskSpecError("Task Spec must be an object.", expected="object", received=value,
                            error_type="type_mismatch")
    status_value = _text(value.get("status"), "status")
    status = status_value.upper()
    if status not in TASK_SPEC_STATUSES:
        raise TaskSpecError("Unknown Task Spec status.", path="status",
                            expected="one of " + ", ".join(TASK_SPEC_STATUSES),
                            received=status_value, error_type="enum_mismatch")
    schema = value.get("schema_version", TASK_SPEC_SCHEMA_VERSION)
    version = value.get("version", 1)
    if schema != TASK_SPEC_SCHEMA_VERSION:
        raise TaskSpecError("Invalid Task Spec schema version.", path="schema_version",
                            expected=str(TASK_SPEC_SCHEMA_VERSION), received=schema,
                            error_type="version_mismatch")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TaskSpecError("Invalid Task Spec version.", path="version",
                            expected="positive integer", received=version,
                            error_type="version_mismatch")
    source_prompt = _text(value.get("source_prompt"), "source_prompt")
    objective = _text(value.get("objective", ""), "objective", required=status == "READY_FOR_PLANNING")
    user_intent = _text(value.get("user_intent", objective), "user_intent", required=False)
    deliverables = _entries(value.get("deliverables", []), "deliverables", source=True)
    requirements = _entries(value.get("requirements", []), "requirements", source=True)
    constraints = _entries(value.get("constraints", []), "constraints", source=True)
    assumptions = _entries(value.get("assumptions", []), "assumptions")
    expectations = [_text(item, "validation_expectation") for item in
                    _list(value.get("validation_expectations", []), "validation_expectations")]
    decisions = value.get("user_decisions", {})
    context = value.get("context", {})
    if not isinstance(decisions, dict):
        raise TaskSpecError("user_decisions must be an object.", path="user_decisions",
                            expected="object", received=decisions, error_type="type_mismatch")
    if not isinstance(context, dict):
        raise TaskSpecError("context must be an object.", path="context",
                            expected="object", received=context, error_type="type_mismatch")
    if len(decisions) > MAX_ITEMS:
        raise TaskSpecError("user_decisions has too many fields.", path="user_decisions",
                            expected=f"object with at most {MAX_ITEMS} fields",
                            received=f"object with {len(decisions)} fields", error_type="value_too_large")
    if len(context) > MAX_ITEMS:
        raise TaskSpecError("Task Spec context is too large.", path="context",
                            expected=f"object with at most {MAX_ITEMS} fields",
                            received=f"object with {len(context)} fields", error_type="value_too_large")
    clean_decisions = {}
    for key, item in decisions.items():
        clean_key = _text(key, "user_decisions key")
        clean_decisions[clean_key] = _text(item, f"user_decisions.{clean_key}")
    clean_context = {}
    for key, item in context.items():
        key = _text(key, "context key")
        if not isinstance(item, (str, bool, int, float, type(None))):
            raise TaskSpecError("Task Spec context values must be scalar.", path=f"context.{key}",
                                expected="string, boolean, number, or null", received=item,
                                error_type="type_mismatch")
        clean_context[key] = _text(item, f"context.{key}", required=False) if isinstance(item, str) else item
    questions = []
    for index, item in enumerate(_list(value.get("clarification_questions", []), "clarification_questions"), 1):
        if not isinstance(item, dict):
            raise TaskSpecError("Clarification questions must be objects.",
                                path=f"clarification_questions[{index - 1}]", expected="object",
                                received=item, error_type="type_mismatch")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise TaskSpecError("Clarification question required must be boolean.",
                                path=f"clarification_questions[{index - 1}].required",
                                expected="boolean", received=required, error_type="type_mismatch")
        question_id = _text(item.get("id"), f"clarification_questions[{index - 1}].id")
        if not re.fullmatch(r"CQ-[1-9]\d*", question_id):
            raise TaskSpecError("Clarification ID must be runtime-owned.",
                                path=f"clarification_questions[{index - 1}].id",
                                expected="CQ-<positive integer>", received=question_id,
                                error_type="invalid_question_id")
        questions.append({"id": question_id,
                          "question": _text(item.get("question"), f"clarification_questions[{index - 1}].question"),
                          "reason": _text(item.get("reason"), f"clarification_questions[{index - 1}].reason"),
                          "field": _question_field(item.get("field"), f"clarification_questions[{index - 1}].field"),
                          "required": required})
    if len({item["id"] for item in questions}) != len(questions) or len({item["field"] for item in questions}) != len(questions):
        raise TaskSpecError("Pending clarification questions must have unique IDs and fields.",
                            path="clarification_questions", expected="unique IDs and fields",
                            error_type="duplicate_question")
    changes = []
    for item in _list(value.get("revision_changes", []), "revision_changes"):
        if not isinstance(item, dict):
            raise TaskSpecError("Revision changes must be objects.",
                                path=f"revision_changes[{len(changes)}]", expected="object",
                                received=item, error_type="type_mismatch")
        changes.append({key: _text(item.get(key), "revision_changes." + key)
                        for key in ("field", "from", "to", "source")})
        if changes[-1]["source"] != "user":
            raise TaskSpecError("A requirement revision needs a user source.",
                                path=f"revision_changes[{len(changes) - 1}].source",
                                expected="user", received=changes[-1]["source"], error_type="enum_mismatch")
    history = []
    for item in _list(value.get("clarification_history", []), "clarification_history"):
        if not isinstance(item, dict):
            raise TaskSpecError("Clarification history entries must be objects.",
                                path=f"clarification_history[{len(history)}]", expected="object",
                                received=item, error_type="type_mismatch")
        index = len(history)
        history.append({key: _text(item.get(key), f"clarification_history[{index}].{key}")
                        for key in ("question_id", "field", "question", "answer")})
        _question_field(history[-1]["field"], f"clarification_history[{index}].field")
    if len({item["question_id"] for item in history}) != len(history):
        raise TaskSpecError("Clarification history must contain one answer per question.",
                            path="clarification_history", expected="unique question IDs",
                            error_type="duplicate_answer")
    if status == TaskSpecStatus.NEEDS_CLARIFICATION.value and not questions:
        raise TaskSpecError("A pending Task Spec needs at least one clarification question.",
                            path="clarification_questions", expected="non-empty list",
                            received=questions, error_type="missing_value")
    if status == TaskSpecStatus.READY_FOR_PLANNING.value:
        if not deliverables:
            raise TaskSpecError("A ready Task Spec needs a deliverable.", path="deliverables",
                                expected="non-empty list", received=deliverables,
                                error_type="missing_value")
        if not requirements:
            raise TaskSpecError("A ready Task Spec needs at least one requirement.", path="requirements",
                                expected="non-empty list", received=requirements,
                                error_type="missing_value")
        if questions:
            raise TaskSpecError("A ready Task Spec cannot have pending questions.",
                                path="clarification_questions", expected="empty list",
                                received=questions, error_type="invalid_state")
    reason = _text(value.get("readiness_reason", ""), "readiness_reason", required=False)
    if status == TaskSpecStatus.READY_FOR_PLANNING.value and not reason:
        raise TaskSpecError("A ready Task Spec needs a readiness reason.", path="readiness_reason",
                            expected="non-empty string", received=reason, error_type="missing_value")
    return sanitize({"schema_version": schema, "version": version, "status": status,
                     "source_prompt": source_prompt, "objective": objective,
                     "user_intent": user_intent, "deliverables": deliverables,
                     "requirements": requirements, "constraints": constraints,
                     "user_decisions": clean_decisions, "assumptions": assumptions,
                     "validation_expectations": expectations, "context": clean_context,
                     "clarification_questions": questions, "clarification_history": history,
                     "revision_changes": changes, "readiness_reason": reason})


def render_task_spec(spec: dict[str, Any]) -> str:
    """Single deterministic textual view for model and worker context."""
    spec = validate_task_spec(spec)
    if spec["status"] != TaskSpecStatus.READY_FOR_PLANNING.value:
        raise TaskSpecError("Only a ready Task Spec may be rendered for execution.")
    public = {key: spec[key] for key in ("objective", "user_intent", "deliverables",
              "requirements", "constraints", "user_decisions", "assumptions",
              "validation_expectations", "context")}
    return json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def initial_task_spec(prompt: str) -> dict[str, Any]:
    return validate_task_spec({"status": TaskSpecStatus.ANALYZING.value, "source_prompt": prompt,
                               "objective": "", "user_intent": ""})


def deterministic_task_spec(prompt: str, previous: dict[str, Any] | None = None,
                            answers: dict[str, str] | None = None) -> dict[str, Any]:
    """Conservative local fallback; never turns vague input into invented work."""
    previous = validate_task_spec(previous) if previous else initial_task_spec(prompt)
    answers = answers or {}
    history, decisions = _apply_clarification_answers(previous, answers)
    combined = " ".join([prompt, *[item["answer"] for item in history]])
    lower = combined.casefold()
    calculator = bool(re.search(r"\b(?:calculadora|calculator)\b", lower))
    interface = "console" if re.search(r"\b(?:consola|console|cli)\b|calculator\.py\b", lower) else (
        "web" if re.search(r"\bweb\b", lower) else (
        "desktop_gui" if re.search(r"\b(?:escritorio|gui|gr[aá]fica)\b", lower) else ""))
    language = ""
    for pattern, name in (
        (r"\bpython\b|\.py\b", "Python"),
        (r"\b(?:javascript|node\.?js)\b|\.js\b", "JavaScript"),
        (r"\btypescript\b|\.ts\b", "TypeScript"),
        (r"\bjava\b", "Java"),
        (r"\brust\b", "Rust"),
        (r"\bgolang\b|\b(?:in|using|en)\s+go\b", "Go"),
        (r"\bpowershell\b|\.ps1\b", "PowerShell"),
    ):
        if re.search(pattern, lower):
            language = name
            break
    default_python = calculator and interface == "console" and not language
    if default_python:
        language = "Python 3.10+"
    operation = "sum" if re.search(r"\b(?:sumar|suma|sume|sum|add)\b", lower) else ""
    questions = []
    if calculator:
        if not interface:
            questions.append({"question": "¿La calculadora debe ser de consola, escritorio o web?",
                              "reason": "La interfaz cambia materialmente el producto.",
                              "field": "interface", "required": True})
        if not operation:
            questions.append({"question": "¿Qué operaciones debe realizar la calculadora?",
                              "reason": "Las operaciones determinan su comportamiento principal.",
                              "field": "operations", "required": True})
    elif (re.search(r"\b(?:zomboid|project zomboid)\b", lower)
          and re.search(r"\bservidor\b", lower)
          and not re.search(r"\b(?:local|remoto|remote|vps|hosting|este equipo|mi pc)\b", lower)):
        questions.append({"question": "¿El servidor de Zomboid debe correr en este equipo o en un host remoto?",
                          "reason": "El destino cambia la configuración y los accesos necesarios.",
                          "field": "deployment_target", "required": True})
    elif re.search(r"\b(?:automatiza|automate) esto\b", lower) and len(lower.split()) < 6:
        questions.append({"question": "¿Qué proceso concreto quieres automatizar?",
                          "reason": "El proceso referido como 'esto' no está identificado.",
                          "field": "workflow", "required": True})
    elif not re.search(r"\b(?:crea|crear|haz|hacer|monta|revisa|automatiza|build|create|review|fix|implement)\b", lower):
        questions.append({"question": "¿Qué resultado concreto necesitas que produzca Freya?",
                          "reason": "No se identifica un objetivo verificable.",
                          "field": "desired_outcome", "required": True})
    elif re.search(r"\b(?:app|aplicaci[oó]n)\b", lower) and len(lower.split()) < 7:
        questions.append({"question": "¿Qué función principal debe cumplir la aplicación?",
                          "reason": "La función principal no está especificada.",
                          "field": "main_function", "required": True})
    status = (TaskSpecStatus.NEEDS_CLARIFICATION.value if questions
              else TaskSpecStatus.READY_FOR_PLANNING.value)
    objective = (f"Crear una calculadora {('de consola' if interface == 'console' else 'web' if interface == 'web' else 'de escritorio')} "
                 f"en {language or 'un lenguaje elegido por Planner'} que {'sume' if operation == 'sum' else 'realice las operaciones solicitadas'}."
                 if calculator and not questions else previous["objective"] or prompt)
    assumptions = list(previous["assumptions"])
    if status == TaskSpecStatus.READY_FOR_PLANNING.value and calculator and "calculator.py" in lower and not any(
            "consola" in item["description"].casefold() for item in assumptions):
        assumptions.append({"description": "Usar interfaz de consola para el archivo Python solicitado.",
                            "reason": "Un script calculator.py sin interfaz indicada tiene un default local de bajo impacto."})
    if status == TaskSpecStatus.READY_FOR_PLANNING.value and default_python:
        assumptions.append({"description": "Usar Python 3.10+ para el programa de consola.",
                            "reason": "Es el default del proyecto para programas independientes sin lenguaje indicado."})
    requirements = list(previous["requirements"])
    if not requirements:
        requirements = [{"description": prompt, "source": RequirementSource.EXPLICIT.value}]
    for item in history[len(previous["clarification_history"]):]:
        requirements.append({"description": item["answer"], "source": RequirementSource.CLARIFIED.value})
    deliverables = ([{"description": "Programa de calculadora ejecutable",
                      "source": RequirementSource.ASSUMED.value}]
                    if calculator else [{"description": objective,
                                         "source": RequirementSource.EXPLICIT.value}]) if status == TaskSpecStatus.READY_FOR_PLANNING.value else []
    constraints = list(previous["constraints"])
    for field, val in (("interface", interface), ("language", language)):
        if val and not any(item["description"].casefold() == val.casefold() for item in constraints):
            constraints.append({
                "description": val,
                "source": RequirementSource.ASSUMED.value if field == "language" and default_python
                else RequirementSource.CLARIFIED.value if answers else RequirementSource.EXPLICIT.value,
            })
    questions = _assign_question_ids(previous, questions)
    return validate_task_spec({**previous, "version": previous["version"] + (1 if answers else 0),
        "status": status, "objective": objective, "user_intent": prompt,
        "deliverables": deliverables, "requirements": requirements, "constraints": constraints,
        "assumptions": assumptions, "user_decisions": decisions,
        "clarification_questions": questions, "clarification_history": history,
        "validation_expectations": (["La calculadora produce la suma correcta para dos números."]
                                    if calculator and status == TaskSpecStatus.READY_FOR_PLANNING.value else []),
        "readiness_reason": ("Objetivo, entregable y decisiones de alto impacto están resueltos."
                             if status == TaskSpecStatus.READY_FOR_PLANNING.value else "Faltan decisiones de alto impacto.")})


def revise_ready_task_spec(spec: dict[str, Any], *, field: str, value: str,
                           user_message: str) -> dict[str, Any]:
    """Record a user change as a new canonical revision before replanning."""
    current = validate_task_spec(spec)
    if current["status"] != TaskSpecStatus.READY_FOR_PLANNING.value:
        raise TaskSpecError("Only a ready Task Spec may be revised this way.")
    field = _text(field, "revision.field")
    value = _text(value, "revision.value")
    message = _text(user_message, "revision.user_message")
    old = current["user_decisions"].get(field, "")
    if field == "interface" and not old:
        old = next((item["description"] for item in current["constraints"]
                    if item["description"] in {"console", "desktop_gui", "web"}), "")
    if old == value:
        return current
    updated = dict(current)
    updated["version"] = current["version"] + 1
    updated["user_decisions"] = {**current["user_decisions"], field: value}
    updated["requirements"] = [*current["requirements"],
                               {"description": message, "source": RequirementSource.CLARIFIED.value}]
    updated["revision_changes"] = [*current["revision_changes"],
                                   {"field": field, "from": old or "(unspecified)",
                                    "to": value, "source": "user"}]
    if field == "interface":
        updated["constraints"] = [
            item for item in current["constraints"]
            if item["description"].casefold() not in {"console", "desktop_gui", "web"}
        ] + [{"description": value, "source": "clarified"}]
        description = {"console": "de consola", "desktop_gui": "con interfaz gráfica",
                       "web": "web"}.get(value, value)
        updated["objective"] = re.sub(r"\b(?:de consola|con interfaz gráfica|web)\b",
                                      description, current["objective"], count=1, flags=re.I)
    updated["readiness_reason"] = "El usuario modificó un requisito explícitamente; requiere nuevo plan."
    return validate_task_spec(updated)


class TaskSpecAnalyst:
    """Model-backed incremental intent analysis with conservative fallback."""

    def __init__(self, *, offline: bool = False,
                 request: Callable[..., dict[str, Any]] = request_json):
        self.offline, self.request = offline, request
        self.metrics: dict[str, Any] = {}
        self.diagnostic_events: list[dict[str, Any]] = []

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _event(self, event_type: str, status: str, message: str, **fields: Any) -> None:
        self.diagnostic_events.append(sanitize({
            "event_type": event_type, "timestamp": self._now(), "status": status,
            "phase": "task_analysis", "actor_type": "task_analyst",
            "message": message, **fields,
        }))

    def _record_clarification_lifecycle(self, previous: dict[str, Any], result: dict[str, Any],
                                        repeated_fields: list[str],
                                        repeated_ids: list[str], rejected_fields: list[str] | None = None) -> None:
        prior_ids = {item["question_id"] for item in previous["clarification_history"]}
        newly_resolved = [item for item in result["clarification_history"]
                          if item["question_id"] not in prior_ids]
        if newly_resolved:
            self._event("task_analysis.clarification_resolved", "Success",
                        "Clarification answers resolved semantic fields.",
                        question_ids=[item["question_id"] for item in newly_resolved],
                        resolved_fields=[item["field"] for item in newly_resolved])
        if repeated_fields:
            self._event("task_analysis.clarification_deduplicated", "Success",
                        "Repeated clarification questions were discarded.",
                        repeated_fields=list(dict.fromkeys(repeated_fields)),
                        repeated_question_ids=list(dict.fromkeys(repeated_ids)))
        if rejected_fields:
            self._event("task_analysis.clarification_rejected", "Warning",
                        "Non-material or invalid clarification fields were discarded.",
                        rejected_fields=list(dict.fromkeys(rejected_fields)))
        rounds = result["version"] - 1
        if (rounds >= MAX_CLARIFICATION_ROUNDS and
                (result["clarification_questions"] or repeated_fields or rejected_fields)):
            self._event("task_analysis.clarification_cycle_detected",
                        "Failed" if result["clarification_questions"] else "Success",
                        "Clarification round limit reached.", rounds=rounds,
                        resolved_fields=list(result["user_decisions"]),
                        pending_fields=[item["field"] for item in result["clarification_questions"]],
                        repeated_fields=list(dict.fromkeys(repeated_fields)),
                        repeated_question_ids=list(dict.fromkeys(repeated_ids)))
            if result["clarification_questions"]:
                raise ClarificationCycleError()

    @staticmethod
    def _contract_error(exc: Exception, raw: str,
                        normalization_attempted: bool) -> dict[str, Any]:
        if isinstance(exc, TaskSpecError):
            diagnostic = exc.diagnostic()
        elif isinstance(exc, json.JSONDecodeError):
            diagnostic = TaskSpecError(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}.",
                path=f"$@{exc.lineno}:{exc.colno}", expected="valid JSON object",
                received=raw[exc.pos:exc.pos + 100], error_type="json_parse_error",
            ).diagnostic()
        else:
            diagnostic = TaskSpecError(
                sanitize(str(exc))[:300] or "Task Analyst output could not be validated.",
                path="$", expected="valid Task Spec response", received=type(exc).__name__,
                error_type=type(exc).__name__,
            ).diagnostic()
        excerpt_source = raw
        if isinstance(exc, json.JSONDecodeError):
            excerpt_source = raw[max(0, exc.pos - 80):exc.pos + 160]
        elif isinstance(exc, TaskSpecError):
            try:
                value: Any = json.loads(raw)
                if isinstance(value, dict):
                    for part in re.findall(r"[^.\[\]]+|\[\d+\]", exc.path):
                        if part.startswith("["):
                            value = value[int(part[1:-1])]
                        elif part != "$":
                            value = value[part]
                    excerpt_source = json.dumps({exc.path: value}, ensure_ascii=False)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                excerpt_source = raw[:240]
        excerpt = sanitize(excerpt_source[:MAX_REPAIR_INPUT_CHARS])
        if not isinstance(excerpt, str):
            excerpt = ""
        diagnostic["raw_response_excerpt"] = excerpt[:240]
        diagnostic["normalization_attempted"] = bool(normalization_attempted)
        return diagnostic

    @staticmethod
    def _diagnostic_message(diagnostic: dict[str, Any]) -> str:
        return (f"{diagnostic.get('error_path')}: expected {diagnostic.get('expected')}, "
                f"received {diagnostic.get('received')}. "
                f"{diagnostic.get('validation_message')}")[:800]

    @staticmethod
    def _repair_content(original: str, payload: dict[str, Any],
                        diagnostic: dict[str, Any]) -> str:
        return json.dumps({
            "instruction": (
                "Correct only the fields identified by the validation error. Preserve all other "
                "user intent and return the complete corrected JSON object, with no prose."
            ),
            "original_response": sanitize(original[:MAX_REPAIR_INPUT_CHARS]),
            "validation_error": {
                key: diagnostic.get(key) for key in (
                    "error_type", "error_path", "expected", "received", "validation_message",
                )
            },
            "expected_schema": TASK_SPEC_RESPONSE_FORMAT,
            "original_input": payload,
        }, ensure_ascii=False, separators=(",", ":"))

    def analyze_spec(self, prompt: str, agent: dict[str, Any] | None = None,
                     previous: dict[str, Any] | None = None,
                     answers: dict[str, str] | None = None) -> dict[str, Any]:
        previous = validate_task_spec(previous) if previous else initial_task_spec(prompt)
        answers = answers or {}
        self.diagnostic_events = []
        self.metrics = {
            "model_calls": 0, "mode": "deterministic", "fallback_used": False,
            "fallback_reason": None, "initial_validation_error": None,
            "repair_validation_error": None, "repair_attempted": False,
            "normalization_attempted": False, "normalization_changes": [],
            "prompt_tokens": 0, "generated_tokens": 0, "total_tokens": 0,
            "llm_duration_seconds": 0.0,
        }
        if self.offline or not agent:
            started = time.monotonic()
            result = deterministic_task_spec(prompt, previous, answers)
            self._record_clarification_lifecycle(previous, result, [], [])
            self.metrics.update(mode="deterministic", model_calls=0,
                                duration_seconds=round(time.monotonic() - started, 4))
            return result
        config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
        model = str(config.get("model") or "").strip()
        self.metrics["model"] = model
        endpoint = validate_endpoint(str(config.get("endpoint") or "http://127.0.0.1:11434"))
        timeout = config.get("max_seconds", 60.0)
        timeout = min(120.0, max(0.1, float(timeout))) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else 60.0
        schema_text = json.dumps(TASK_SPEC_RESPONSE_FORMAT, ensure_ascii=False, separators=(",", ":"))
        system = (
            "You are Freya's Task Analyst. Determine WHAT the user wants, not HOW to execute it. "
            "Return one JSON object matching the supplied schema. This response contains user intent only. "
            "Do not return source_prompt, schema_version, version, clarification_history or revision_changes; "
            "Freya supplies those audit/runtime fields. For each ready response, provide at least one "
            "deliverable and one requirement, no pending questions, and a non-empty readiness_reason. "
            "For NEEDS_CLARIFICATION, provide at least one focused question. Entries in deliverables, "
            "requirements and constraints use only explicit, clarified, or assumed. Assumptions have "
            "description and reason. Question IDs are assigned by Freya. Use NEEDS_CLARIFICATION only "
            "for material ambiguity without a safe default; ask few high-impact questions. Never invent "
            "a user answer. Do not choose agents, tools, capabilities, Skills, task IDs, plans, execution "
            "steps, dependencies, or worker assignments. Preserve earlier decisions and apply new answers "
            "incrementally. The canonical requirement source values are "
            + ", ".join(REQUIREMENT_SOURCES) + ". The JSON Schema is: " + schema_text
        )
        payload = {"source_prompt": prompt, "previous_task_spec": previous,
                   "answers_to_pending_questions": answers}
        started = time.monotonic()
        def call(content: str, repair: bool = False) -> str:
            call_started = time.monotonic()
            self.metrics["model_calls"] += 1
            response = model_request(self.request, "task_analyst", "POST",
                endpoint.rstrip("/") + "/api/chat",
                {"model": model, "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": content}], "tools": [],
                 "format": deepcopy(TASK_SPEC_RESPONSE_FORMAT),
                 "stream": False, "think": False,
                 "options": {"temperature": 0, "num_ctx": int(config.get("context_window", 8192)),
                             "num_predict": model_profile("task_analyst").repair_output_tokens if repair else model_profile("task_analyst").max_output_tokens}},
                timeout=timeout)
            for target, source in (("prompt_tokens", "prompt_eval_count"), ("generated_tokens", "eval_count")):
                self.metrics[target] = self.metrics.get(target, 0) + int(response.get(source) or 0)
            self.metrics["total_tokens"] = self.metrics["prompt_tokens"] + self.metrics["generated_tokens"]
            measured = response.get("_freya_transport")
            details = dict(measured) if isinstance(measured, dict) else {}
            elapsed = round(time.monotonic() - call_started, 4)
            details.update({
                "component": "task_analyst", "model": model,
                "stage": "repair" if repair else "initial",
                "duration_seconds": elapsed,
                "prompt_tokens": int(response.get("prompt_eval_count") or 0),
                "generated_tokens": int(response.get("eval_count") or 0),
                "total_tokens": int(response.get("prompt_eval_count") or 0)
                + int(response.get("eval_count") or 0),
                "streaming": False,
            })
            self.metrics.setdefault("model_call_details", []).append(sanitize(details))
            call_duration = details.get("total_duration")
            if not isinstance(call_duration, (int, float)) or isinstance(call_duration, bool):
                call_duration = elapsed
            self.metrics["llm_duration_seconds"] += max(0.0, float(call_duration))
            return response["message"]["content"]

        def parse_and_validate(raw_response: str) -> tuple[dict[str, Any], list[str]]:
            try:
                candidate = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                raise exc
            candidate, changes = normalize_task_spec_candidate(candidate)
            self.metrics["normalization_attempted"] = True
            forbidden = {
                "plan", "recommended_agent_role", "required_capabilities",
                "preferred_skills", "depends_on", "tools", "capabilities", "skills",
                "worker_assignment", "task_ids", "execution_steps",
            }
            extras = forbidden.intersection(candidate)
            if extras:
                field = sorted(extras)[0]
                raise TaskSpecError("Task Analyst returned Planner-owned fields.", path=field,
                                    expected="user-intent fields only", received="present",
                                    error_type="forbidden_field")
            unknown = set(candidate) - TASK_SPEC_MODEL_FIELDS
            if unknown:
                field = sorted(unknown)[0]
                raise TaskSpecError("Task Analyst returned a field outside its output schema.",
                                    path=field, expected="one of the Task Analyst schema fields",
                                    received="present", error_type="unexpected_field")
            proposed_status = _text(candidate.get("status"), "status")
            if proposed_status not in TASK_ANALYST_OUTPUT_STATUSES:
                raise TaskSpecError("Task Analyst must decide ready or clarification.", path="status",
                                    expected="NEEDS_CLARIFICATION or READY_FOR_PLANNING",
                                    received=proposed_status, error_type="invalid_state")
            floor = deterministic_task_spec(prompt, previous, answers)
            history, decisions = _apply_clarification_answers(previous, answers)
            proposed = candidate.get("clarification_questions", [])
            if not isinstance(proposed, list):
                raise TaskSpecError("Clarification questions must be a list.",
                                    path="clarification_questions", expected="list", received=proposed,
                                    error_type="type_mismatch")
            resolved_fields = set(decisions) | {item["field"] for item in history}
            if re.search(r"\b(?:calculadora|calculator)\b", prompt, flags=re.I):
                if "platform" in resolved_fields:
                    resolved_fields.add("interface")
                if "interface" in resolved_fields:
                    resolved_fields.add("platform")
                # The deterministic calculator floor only reaches READY after
                # resolving its interface and operations, including safe defaults.
                if floor["status"] == TaskSpecStatus.READY_FOR_PLANNING.value:
                    resolved_fields.update({"platform", "interface", "operations", "language"})
            seen_fields: set[str] = set()
            seen_questions: set[str] = set()
            historical_questions = {_question_fingerprint(item["question"]) for item in history}
            pending = []
            repeated_fields = []
            repeated_ids = []
            rejected_fields = []
            for question in [*floor["clarification_questions"], *proposed]:
                if not isinstance(question, dict):
                    raise TaskSpecError("Clarification question must be an object.",
                                        path="clarification_questions", expected="object", received=question,
                                        error_type="type_mismatch")
                try:
                    field = _question_field(question.get("field"), "clarification_questions.field")
                except TaskSpecError:
                    rejected_fields.append(str(question.get("field") or "")[:64])
                    continue
                fingerprint = _question_fingerprint(str(question.get("question") or ""))
                if (field in resolved_fields or field in seen_fields or
                        fingerprint in historical_questions or fingerprint in seen_questions):
                    repeated_fields.append(field)
                    repeated_ids.extend(item["question_id"] for item in history if item["field"] == field)
                    continue
                if _optional_question(question):
                    rejected_fields.append(field)
                    continue
                prior = next((item for item in previous["clarification_questions"]
                              if item["field"] == field and field not in resolved_fields), None)
                pending.append(prior or question)
                seen_fields.add(field)
                seen_questions.add(fingerprint)
            pending = _assign_question_ids(previous, pending)
            candidate.update({
                "source_prompt": prompt,
                "schema_version": TASK_SPEC_SCHEMA_VERSION,
                "version": previous["version"] + (1 if answers else 0),
                "clarification_history": history,
                "user_decisions": decisions,
                "clarification_questions": pending,
                "status": (TaskSpecStatus.NEEDS_CLARIFICATION.value if pending
                           else TaskSpecStatus.READY_FOR_PLANNING.value),
            })
            if not pending and (proposed_status == TaskSpecStatus.NEEDS_CLARIFICATION.value
                                or candidate.get("readiness_reason", "") == ""):
                candidate["readiness_reason"] = floor["readiness_reason"]
            if (not pending and floor["status"] == TaskSpecStatus.READY_FOR_PLANNING.value
                    and proposed_status == TaskSpecStatus.NEEDS_CLARIFICATION.value):
                for field in ("deliverables", "requirements"):
                    if not candidate.get(field):
                        candidate[field] = floor[field]
            result = validate_task_spec(candidate)
            if floor["status"] == TaskSpecStatus.NEEDS_CLARIFICATION.value:
                self.metrics["semantic_gate"] = "material_ambiguity"
            self._record_clarification_lifecycle(previous, result,
                                                  repeated_fields, repeated_ids, rejected_fields)
            return result, changes

        def note_normalization(stage: str, changes: list[str]) -> None:
            if not changes:
                return
            self.metrics["normalization_changes"] = list(dict.fromkeys(
                [*self.metrics["normalization_changes"], *changes],
            ))
            self._event(
                "task_analysis.normalization_succeeded", "Success",
                "Task Analyst output passed after safe deterministic normalization.",
                stage=stage, normalization_changes=changes,
                normalization_attempted=True,
            )

        def record_invalid(stage: str, exc: Exception, raw_response: str) -> dict[str, Any]:
            diagnostic = self._contract_error(
                exc, raw_response, bool(self.metrics.get("normalization_attempted")),
            )
            metric_key = "initial_validation_error" if stage == "initial" else "repair_validation_error"
            self.metrics[metric_key] = {k: v for k, v in diagnostic.items()
                                        if k != "raw_response_excerpt"}
            self._event(
                "task_analysis.contract_invalid", "Failed",
                "Task Analyst output violated the canonical Task Spec contract.",
                stage=stage, **diagnostic,
            )
            return diagnostic

        try:
            raw = call(json.dumps(payload, ensure_ascii=False))
            try:
                result, changes = parse_and_validate(raw)
            except ClarificationCycleError:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                initial_error = record_invalid("initial", exc, raw)
                self.metrics["repair_attempted"] = True
                self._event(
                    "task_analysis.repair_started", "Running",
                    "Task Analyst is correcting only the fields that failed contract validation.",
                    stage="repair", error_path=initial_error.get("error_path"),
                    expected=initial_error.get("expected"), received=initial_error.get("received"),
                    fields_to_change=[initial_error.get("error_path")],
                )
                original = raw
                raw = call(self._repair_content(original, payload, initial_error), repair=True)
                try:
                    result, changes = parse_and_validate(raw)
                except ClarificationCycleError:
                    raise
                except (ValueError, KeyError, TypeError) as repair_exc:
                    repair_error = record_invalid("repair", repair_exc, raw)
                    self.metrics["fallback_used"] = True
                    self.metrics["mode"] = "deterministic_fallback"
                    self.metrics["fallback_reason"] = self._diagnostic_message(repair_error)
                    self.metrics["fallback_error"] = self.metrics["fallback_reason"]
                    self._event(
                        "task_analysis.fallback_used", "Warning",
                        "Task Analyst used the deterministic fallback after repair remained invalid.",
                        reason=self.metrics["fallback_reason"],
                        initial_validation_error=self.metrics["initial_validation_error"],
                        repair_validation_error=self.metrics["repair_validation_error"],
                        model_calls=self.metrics["model_calls"], fallback_used=True,
                    )
                    result = deterministic_task_spec(prompt, previous, answers)
                else:
                    self.metrics["mode"] = "llm"
                    self._event(
                        "task_analysis.repair_succeeded", "Success",
                        "Task Analyst repair passed canonical Task Spec validation.",
                        stage="repair", repaired_fields=[initial_error.get("error_path")],
                        normalization_changes=changes,
                    )
                    note_normalization("repair", changes)
            else:
                self.metrics["mode"] = "llm"
                note_normalization("initial", changes)
            if self.metrics["fallback_used"]:
                self._record_clarification_lifecycle(previous, result, [], [])
            return result
        except ClarificationCycleError:
            raise
        except Exception as exc:
            self.metrics["mode"] = "deterministic_fallback"
            self.metrics["fallback_used"] = True
            reason = sanitize(str(exc))[:1000]
            self.metrics["fallback_reason"] = reason
            self.metrics["fallback_error"] = reason
            self._event(
                "task_analysis.fallback_used", "Warning",
                "Task Analyst used the deterministic fallback after model processing failed.",
                reason=reason, model_calls=self.metrics["model_calls"], fallback_used=True,
            )
            result = deterministic_task_spec(prompt, previous, answers)
            self._record_clarification_lifecycle(previous, result, [], [])
            return result
        finally:
            self.metrics["duration_seconds"] = round(time.monotonic() - started, 4)
            self.metrics["total_tokens"] = self.metrics.get("prompt_tokens", 0) + self.metrics.get("generated_tokens", 0)
            self.metrics["llm_duration_seconds"] = round(
                float(self.metrics.get("llm_duration_seconds", 0)), 4,
            )
