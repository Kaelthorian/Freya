"""Canonical, versioned user intent and the tool-free clarification boundary."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .config import validate_endpoint
from .security import sanitize
from .transport import model_profile, model_request, request_json


TASK_SPEC_SCHEMA_VERSION = 1
SPEC_STATUSES = {"ANALYZING", "NEEDS_CLARIFICATION", "READY_FOR_PLANNING"}
REQUIREMENT_SOURCES = {"explicit", "clarified", "assumed"}
MAX_ITEMS = 30
MAX_TEXT = 4000


class TaskSpecError(ValueError):
    pass


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TaskSpecError(f"{name} must be text.")
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise TaskSpecError(f"{name} must not be empty.")
    if len(value) > MAX_TEXT:
        raise TaskSpecError(f"{name} is too long.")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise TaskSpecError(f"{name} must be a list with at most {MAX_ITEMS} items.")
    return value


def _entries(value: Any, name: str, *, source: bool = False) -> list[dict[str, str]]:
    result = []
    for item in _list(value, name):
        if not isinstance(item, dict):
            raise TaskSpecError(f"{name} entries must be objects.")
        if source:
            origin = _text(item.get("source"), name + ".source").casefold()
            if origin == "inferred":
                origin = "assumed"
            if origin not in REQUIREMENT_SOURCES:
                raise TaskSpecError(f"{name}.source is invalid.")
            result.append({"description": _text(item.get("description"), name + ".description"),
                           "source": origin})
        else:
            result.append({"description": _text(item.get("description"), name + ".description"),
                           "reason": _text(item.get("reason"), name + ".reason")})
    return result


def validate_task_spec(value: Any) -> dict[str, Any]:
    """Normalize harmless formatting while rejecting semantic or schema gaps."""
    if not isinstance(value, dict):
        raise TaskSpecError("Task Spec must be an object.")
    status = _text(value.get("status"), "status").upper()
    if status not in SPEC_STATUSES:
        raise TaskSpecError("Unknown Task Spec status.")
    schema = value.get("schema_version", TASK_SPEC_SCHEMA_VERSION)
    version = value.get("version", 1)
    if schema != TASK_SPEC_SCHEMA_VERSION or isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TaskSpecError("Invalid Task Spec version.")
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
    if not isinstance(decisions, dict) or not isinstance(context, dict):
        raise TaskSpecError("user_decisions and context must be objects.")
    if len(decisions) > MAX_ITEMS or len(context) > MAX_ITEMS:
        raise TaskSpecError("Task Spec context is too large.")
    clean_decisions = {_text(k, "decision field"): _text(v, "decision value") for k, v in decisions.items()}
    clean_context = {}
    for key, item in context.items():
        key = _text(key, "context field")
        if not isinstance(item, (str, bool, int, float, type(None))):
            raise TaskSpecError("Task Spec context values must be scalar.")
        clean_context[key] = _text(item, "context value", required=False) if isinstance(item, str) else item
    questions = []
    for index, item in enumerate(_list(value.get("clarification_questions", []), "clarification_questions"), 1):
        if not isinstance(item, dict):
            raise TaskSpecError("Clarification questions must be objects.")
        questions.append({"id": f"CQ-{index}",
                          "question": _text(item.get("question"), "question"),
                          "reason": _text(item.get("reason"), "question.reason"),
                          "field": _text(item.get("field"), "question.field"),
                          "required": bool(item.get("required", True))})
    changes = []
    for item in _list(value.get("revision_changes", []), "revision_changes"):
        if not isinstance(item, dict):
            raise TaskSpecError("Revision changes must be objects.")
        changes.append({key: _text(item.get(key), "revision_changes." + key)
                        for key in ("field", "from", "to", "source")})
        if changes[-1]["source"] != "user":
            raise TaskSpecError("A requirement revision needs a user source.")
    history = []
    for item in _list(value.get("clarification_history", []), "clarification_history"):
        if not isinstance(item, dict):
            raise TaskSpecError("Clarification history entries must be objects.")
        history.append({key: _text(item.get(key), "clarification_history." + key)
                        for key in ("question_id", "field", "question", "answer")})
    if status == "NEEDS_CLARIFICATION" and not questions:
        raise TaskSpecError("A pending Task Spec needs a question.")
    if status == "READY_FOR_PLANNING" and (not deliverables or not requirements or questions):
        raise TaskSpecError("A ready Task Spec needs a deliverable and requirements, without pending questions.")
    reason = _text(value.get("readiness_reason", ""), "readiness_reason", required=False)
    if status == "READY_FOR_PLANNING" and not reason:
        raise TaskSpecError("A ready Task Spec needs a readiness reason.")
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
    if spec["status"] != "READY_FOR_PLANNING":
        raise TaskSpecError("Only a ready Task Spec may be rendered for execution.")
    public = {key: spec[key] for key in ("objective", "user_intent", "deliverables",
              "requirements", "constraints", "user_decisions", "assumptions",
              "validation_expectations", "context")}
    return json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def initial_task_spec(prompt: str) -> dict[str, Any]:
    return validate_task_spec({"status": "ANALYZING", "source_prompt": prompt,
                               "objective": "", "user_intent": ""})


def deterministic_task_spec(prompt: str, previous: dict[str, Any] | None = None,
                            answers: dict[str, str] | None = None) -> dict[str, Any]:
    """Conservative local fallback; never turns vague input into invented work."""
    previous = validate_task_spec(previous) if previous else initial_task_spec(prompt)
    history = list(previous["clarification_history"])
    decisions = dict(previous["user_decisions"])
    answers = answers or {}
    for question in previous["clarification_questions"]:
        answer = str(answers.get(question["id"]) or "").strip()
        if answer:
            history.append({"question_id": question["id"], "field": question["field"],
                            "question": question["question"], "answer": answer})
            decisions[question["field"]] = answer
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
                          "field": "objective", "required": True})
    elif re.search(r"\b(?:app|aplicaci[oó]n)\b", lower) and len(lower.split()) < 7:
        questions.append({"question": "¿Qué función principal debe cumplir la aplicación?",
                          "reason": "La función principal no está especificada.",
                          "field": "main_function", "required": True})
    status = "NEEDS_CLARIFICATION" if questions else "READY_FOR_PLANNING"
    objective = (f"Crear una calculadora {('de consola' if interface == 'console' else 'web' if interface == 'web' else 'de escritorio')} "
                 f"en {language or 'un lenguaje elegido por Planner'} que {'sume' if operation == 'sum' else 'realice las operaciones solicitadas'}."
                 if calculator and not questions else previous["objective"] or prompt)
    assumptions = list(previous["assumptions"])
    if status == "READY_FOR_PLANNING" and calculator and "calculator.py" in lower and not any(
            "consola" in item["description"].casefold() for item in assumptions):
        assumptions.append({"description": "Usar interfaz de consola para el archivo Python solicitado.",
                            "reason": "Un script calculator.py sin interfaz indicada tiene un default local de bajo impacto."})
    if status == "READY_FOR_PLANNING" and default_python:
        assumptions.append({"description": "Usar Python 3.10+ para el programa de consola.",
                            "reason": "Es el default del proyecto para programas independientes sin lenguaje indicado."})
    requirements = list(previous["requirements"])
    if not requirements:
        requirements = [{"description": prompt, "source": "explicit"}]
    for item in history[len(previous["clarification_history"]):]:
        requirements.append({"description": item["answer"], "source": "clarified"})
    deliverables = ([{"description": "Programa de calculadora ejecutable", "source": "assumed"}]
                    if calculator else [{"description": objective, "source": "explicit"}]) if status == "READY_FOR_PLANNING" else []
    constraints = list(previous["constraints"])
    for field, val in (("interface", interface), ("language", language)):
        if val and not any(item["description"].casefold() == val.casefold() for item in constraints):
            constraints.append({"description": val, "source": "assumed" if field == "language" and default_python else "clarified" if answers else "explicit"})
    return validate_task_spec({**previous, "version": previous["version"] + (1 if answers else 0),
        "status": status, "objective": objective, "user_intent": prompt,
        "deliverables": deliverables, "requirements": requirements, "constraints": constraints,
        "assumptions": assumptions, "user_decisions": decisions,
        "clarification_questions": questions, "clarification_history": history,
        "validation_expectations": (["La calculadora produce la suma correcta para dos números."]
                                    if calculator and status == "READY_FOR_PLANNING" else []),
        "readiness_reason": ("Objetivo, entregable y decisiones de alto impacto están resueltos."
                             if status == "READY_FOR_PLANNING" else "Faltan decisiones de alto impacto.")})


def revise_ready_task_spec(spec: dict[str, Any], *, field: str, value: str,
                           user_message: str) -> dict[str, Any]:
    """Record a user change as a new canonical revision before replanning."""
    current = validate_task_spec(spec)
    if current["status"] != "READY_FOR_PLANNING":
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
                               {"description": message, "source": "clarified"}]
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

    def analyze_spec(self, prompt: str, agent: dict[str, Any] | None = None,
                     previous: dict[str, Any] | None = None,
                     answers: dict[str, str] | None = None) -> dict[str, Any]:
        previous = validate_task_spec(previous) if previous else initial_task_spec(prompt)
        answers = answers or {}
        self.metrics = {"model_calls": 0, "mode": "deterministic"}
        if self.offline or not agent:
            return deterministic_task_spec(prompt, previous, answers)
        config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
        model = str(config.get("model") or "").strip()
        endpoint = validate_endpoint(str(config.get("endpoint") or "http://127.0.0.1:11434"))
        timeout = config.get("max_seconds", 60.0)
        timeout = min(120.0, max(0.1, float(timeout))) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else 60.0
        system = ("You are Freya's Task Analyst. Determine WHAT the user wants, not HOW to execute it. "
                  "Return one JSON object with objective, user_intent, deliverables, requirements, constraints, "
                  "user_decisions, assumptions, validation_expectations, context, clarification_questions, "
                  "status and readiness_reason. Requirement/deliverable/constraint entries have description and "
                  "source: explicit, clarified, or assumed. Assumptions have description and reason. "
                  "Questions have question, reason, field and required. Use NEEDS_CLARIFICATION only for "
                  "material ambiguity without a safe default; ask few high-impact questions. Use READY_FOR_PLANNING "
                  "when objective and deliverable are clear. Do not choose agents, tools, capabilities, Skills, "
                  "task IDs, execution steps, dependencies, or QA agents. Preserve earlier decisions and apply "
                  "the new answers incrementally. Never invent a user answer. Return JSON only.")
        payload = {"source_prompt": prompt, "previous_task_spec": previous,
                   "answers_to_pending_questions": answers}
        started = time.monotonic()
        def call(content: str, repair: bool = False) -> str:
            self.metrics["model_calls"] += 1
            response = model_request(self.request, "task_analyst", "POST",
                endpoint.rstrip("/") + "/api/chat",
                {"model": model, "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": content}], "tools": [], "format": "json",
                 "stream": False, "think": False,
                 "options": {"temperature": 0, "num_ctx": int(config.get("context_window", 8192)),
                             "num_predict": model_profile("task_analyst").repair_output_tokens if repair else model_profile("task_analyst").max_output_tokens}},
                timeout=timeout)
            for target, source in (("prompt_tokens", "prompt_eval_count"), ("generated_tokens", "eval_count")):
                self.metrics[target] = self.metrics.get(target, 0) + int(response.get(source) or 0)
            return response["message"]["content"]
        try:
            raw = call(json.dumps(payload, ensure_ascii=False))
            for attempt in range(2):
                try:
                    candidate = json.loads(raw)
                    if not isinstance(candidate, dict):
                        raise TaskSpecError("Model response must be an object.")
                    forbidden = {"plan", "recommended_agent_role", "required_capabilities", "preferred_skills", "depends_on"}
                    if forbidden.intersection(candidate):
                        raise TaskSpecError("Task Analyst returned Planner-owned fields.")
                    history = list(previous["clarification_history"])
                    decisions = dict(previous["user_decisions"])
                    for question in previous["clarification_questions"]:
                        answer = str(answers.get(question["id"]) or "").strip()
                        if answer:
                            history.append({"question_id": question["id"], "field": question["field"],
                                            "question": question["question"], "answer": answer})
                            decisions[question["field"]] = answer
                    candidate.update({"source_prompt": prompt, "schema_version": TASK_SPEC_SCHEMA_VERSION,
                                      "version": previous["version"] + (1 if answers else 0),
                                      "clarification_history": history,
                                      "user_decisions": {**candidate.get("user_decisions", {}), **decisions}})
                    result = validate_task_spec(candidate)
                    if result["status"] == "ANALYZING":
                        raise TaskSpecError("Task Analyst must decide ready or clarification.")
                    floor = deterministic_task_spec(prompt, previous, answers)
                    if (floor["status"] == "NEEDS_CLARIFICATION"
                            and result["status"] == "READY_FOR_PLANNING"):
                        result = validate_task_spec({
                            **result, "status": "NEEDS_CLARIFICATION",
                            "clarification_questions": floor["clarification_questions"],
                            "readiness_reason": floor["readiness_reason"],
                        })
                        self.metrics["semantic_gate"] = "material_ambiguity"
                    self.metrics["mode"] = "model"
                    return result
                except (ValueError, KeyError, TypeError, TaskSpecError) as exc:
                    if attempt:
                        raise TaskSpecError("Task Analyst output remained invalid after repair.") from exc
                    raw = call(json.dumps({"error": str(exc), "previous_response": raw[:MAX_TEXT],
                                           "original_input": payload}, ensure_ascii=False), repair=True)
        except Exception as exc:
            self.metrics["mode"] = "deterministic_fallback"
            self.metrics["fallback_error"] = sanitize(str(exc))[:1000]
            return deterministic_task_spec(prompt, previous, answers)
        finally:
            self.metrics["duration_seconds"] = round(time.monotonic() - started, 4)
            self.metrics["total_tokens"] = self.metrics.get("prompt_tokens", 0) + self.metrics.get("generated_tokens", 0)
