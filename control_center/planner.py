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
from .transport import model_profile, model_request, request_json
from .task_analyst import (TaskAnalysisError, canonical_task_kind,
                           validate_task_analysis)
from .integration_proof import STRUCTURAL_CRITERIA, criterion_key


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
CRITERION_LINKS_FIELD = "criterion_links"
TASK_FIELDS = {
    "id", "objective", "description", "depends_on", "required_capabilities",
    "preferred_skills", "success_criteria",
}
TASK_METADATA_FIELDS = {"task_kind", "task_characteristics"}
TASK_KIND_VALUES = {
    "file_creation", "program_creation", "code_change", "review", "testing",
    "analysis", "external_action", "general",
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
                    "task_kind": {"type": "string", "enum": sorted(TASK_KIND_VALUES)},
                    "task_characteristics": {"type": "object"},
                },
                "required": sorted(TASK_FIELDS),
                "additionalProperties": False,
            },
        },
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "criterion_links": {"type": "object", "properties": {
            "global": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": ["string", "null"]}, "criterion": {"type": "string"},
            }, "required": ["criterion"], "additionalProperties": False}},
            "local": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": ["string", "null"]}, "task_id": {"type": "string"},
                "criterion": {"type": "string"},
                "supports_global_criteria": {"type": "array", "items": {"type": "string"}},
            }, "required": ["task_id", "criterion", "supports_global_criteria"],
               "additionalProperties": False}},
        }, "required": ["global", "local"], "additionalProperties": False},
    },
    "required": sorted(PLAN_FIELDS | {CRITERION_LINKS_FIELD}),
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
    task_kind = str(analysis.get("task_kind") or "").casefold()
    if not task_kind:
        task_kind = canonical_task_kind(
            analysis.get("task_type"), analysis.get("objective"), characteristics,
        )
    if task_kind not in {
        "program_creation", "file_creation",
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
    links = plan.get(CRITERION_LINKS_FIELD)
    if isinstance(links, dict):
        merged_links = []
        for criterion in merged["success_criteria"]:
            sources = [item for item in links["local"]
                       if item["criterion"].casefold() == criterion.casefold()]
            if not sources:
                continue
            supported = list(dict.fromkeys(
                global_id for item in sources
                for global_id in item["supports_global_criteria"]
            ))
            merged_links.append({**sources[0], "task_id": merged["id"],
                                 "supports_global_criteria": supported})
        collapsed[CRITERION_LINKS_FIELD] = {"global": links["global"], "local": merged_links}
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
    requires_read = characteristics.get("requires_filesystem_read") is True
    interactive = (characteristics.get("interactive") is True
                   or characteristics.get("requires_user_input") is True)
    task_type = str(analysis.get("task_type") or "").strip().casefold()
    task_kind = str(analysis.get("task_kind") or "").strip().casefold()
    windows_script = task_type == "windows_command_script"
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return plan

    source_text = " ".join(
        [str(analysis.get(key) or "") for key in ("operational_prompt", "objective", "task_type")]
        + [str(item.get("description") or "") for item in analysis.get("requirements", [])
           if isinstance(item, dict)]
    ).strip()
    plan_text = " ".join(
        [str(plan.get("goal") or ""), str(plan.get("summary") or "")]
        + [" ".join([
            str(item.get("objective") or ""), str(item.get("description") or ""),
            " ".join(str(skill) for skill in item.get("preferred_skills", [])),
        ]) for item in tasks]
    ).strip()
    all_capabilities = {
        str(capability) for item in tasks
        for capability in item.get("required_capabilities", [])
    }
    plan_has_write = bool(all_capabilities.intersection({
        "filesystem.create", "filesystem.modify", "filesystem.overwrite",
    }))
    plan_has_execution = any(capability.startswith("execution.") for capability in all_capabilities)
    python_hint = bool(re.search(r"\bpython\b|execution\.python_script|python-development",
                                 source_text + " " + plan_text, re.I))
    source_requests_write = bool(re.search(
        r"\b(?:create|crear|crea|write|escrib|implement|generate|gener|make|hacer)\w*\b",
        source_text, re.I,
    ))
    explicit_overwrite = bool(re.search(
        r"\b(?:overwrite|sobrescrib|reemplaz|replace)\w*\b|\bexisting\s+(?:file|artifact)|archivo\s+existente",
        source_text, re.I,
    ))
    if not task_kind:
        task_kind = canonical_task_kind(task_type, source_text, characteristics)
    elif task_kind not in {"file_creation", "program_creation", "code_change", "review", "testing", "analysis", "external_action", "general"}:
        task_kind = "general"
    if task_kind == "file_creation" and (python_hint or "execution.python_script" in all_capabilities):
        # Model labels sometimes call a Python artifact a generic file. The
        # executable capability is stronger evidence for Skill and fast-path
        # selection than that loose label.
        task_kind = "program_creation"
    requires_write = requires_write or plan_has_write or (
        task_kind in {"file_creation", "program_creation"} and source_requests_write
    )
    requires_read = requires_read or requires_write or plan_has_execution or task_kind == "program_creation"
    if not requires_write and not requires_read and not windows_script and not interactive:
        return plan
    requires_code_execution = (
        characteristics.get("requires_code_execution") is True
        or plan_has_execution
        or task_kind == "program_creation"
    )
    reconciled = dict(plan)
    updated_tasks: list[dict[str, Any]] = []
    for task in tasks:
        current = dict(task)
        capabilities = list(current.get("required_capabilities", []))
        if not explicit_overwrite:
            capabilities = [capability for capability in capabilities
                            if capability != "filesystem.overwrite"]
        if requires_write and not any(capability in {
                "filesystem.create", "filesystem.modify", "filesystem.overwrite"
        } for capability in capabilities):
            capabilities.append("filesystem.create")
        if requires_read and "filesystem.read" not in capabilities:
            # Read-back is observation inside the selected workspace, not an
            # overwrite escalation. Plan it before AgentFactory derives tools.
            capabilities.append("filesystem.read")
        if (task_kind == "program_creation"
                and requires_code_execution
                and python_hint
                and not windows_script
                and not any(capability.startswith("execution.") for capability in capabilities)):
            capabilities.append("execution.python_script")
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
        if task_kind == "file_creation" and not windows_script:
            preferred = list(current.get("preferred_skills", []))
            preferred = [item for item in preferred if str(item).casefold() not in {
                "python-development", "debugging",
            }]
            if "simple-file-artifact" not in preferred:
                preferred.insert(0, "simple-file-artifact")
            current["preferred_skills"] = preferred[:MAX_PREFERRED_SKILLS]
        elif task_kind == "program_creation" and python_hint:
            preferred = list(current.get("preferred_skills", []))
            if "python-development" not in preferred:
                preferred.insert(0, "python-development")
            current["preferred_skills"] = preferred[:MAX_PREFERRED_SKILLS]
        # Preserve the analyst's normalized semantic hints in the durable plan
        # so AgentFactory does not have to rediscover them from loose prose.
        current["task_kind"] = task_kind
        current["task_characteristics"] = {
            key: value for key, value in characteristics.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        current["required_capabilities"] = capabilities[:MAX_CAPABILITIES]
        updated_tasks.append(current)

    # A model may append a Code Auditor even after describing a trivial artifact
    # as multi-step. Once the implementation task is normalized to the safe
    # single-task shape, remove only that generated audit node; explicit QA and
    # genuinely multi-part work remain intact.
    explicit_audit = bool(re.search(
        r"\b(?:audit|review|code review|auditor|revis(?:a|ar))\b", source_text, re.I,
    ))
    if (not interactive and not explicit_audit
            and task_kind in {"file_creation", "program_creation"}
            and len(updated_tasks) > 1):
        implementation = [item for item in updated_tasks if not _is_code_audit_task(item)]
        if len(implementation) == 1:
            updated_tasks = implementation
            reconciled["complexity"] = "simple"
    reconciled["tasks"] = updated_tasks
    if CRITERION_LINKS_FIELD in reconciled:
        remaining = {task["id"] for task in updated_tasks}
        links = dict(reconciled[CRITERION_LINKS_FIELD])
        links["local"] = [item for item in links["local"] if item["task_id"] in remaining]
        reconciled[CRITERION_LINKS_FIELD] = links
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
        repair = context.get("_freya_repair") is True
        model_context = {key: value for key, value in context.items() if key != "_freya_repair"}
        try:
            response = model_request(self.request, "planner",
                "POST", self.endpoint + "/api/chat",
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": (
                            "You are Freya's planning component. Return only the requested JSON plan. "
                            "Do not include private reasoning, Markdown, tool calls, or extra fields."
                        )},
                        {"role": "user", "content": prompt + "\nPlanner context:\n" +
                         json.dumps(model_context, ensure_ascii=False, separators=(",", ":"))},
                    ],
                    "tools": [],
                    "format": PLAN_RESPONSE_FORMAT,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": DEFAULT_PLANNER_CONTEXT_WINDOW,
                        "num_predict": (model_profile("planner").repair_output_tokens
                                        if repair else model_profile("planner").max_output_tokens),
                    },
                },
                timeout=self.timeout_seconds, telemetry=self.last_call_metrics,
            )
            if isinstance(response.get("_freya_transport"), dict):
                self.last_call_metrics["transport"] = response["_freya_transport"]
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


def _object(value: Any, fields: set[str], label: str, *, optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{label} must be an object.")
    missing = fields - value.keys()
    unknown = value.keys() - (fields | (optional or set()))
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



def _ensure_stable_ids(items: list[dict[str, Any]], *, structure: str,
                       id_factory: Callable[[int, int], str],
                       forbidden_ids: set[str] | None = None,
                       diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Preserve valid unique IDs and deterministically fill absent or conflicting IDs."""
    normalized = [dict(item) for item in items]
    identifiers: list[str | None] = [None] * len(normalized)
    seen: set[str] = set(forbidden_ids or ())
    duplicates: set[int] = set()

    # Reserve every valid model ID before assigning any missing IDs, so an early
    # generated value cannot displace a valid ID that appears later in the list.
    for index, item in enumerate(normalized):
        raw_id = item.get("id")
        if raw_id is None:
            continue
        if not isinstance(raw_id, str):
            raise PlanValidationError(f"{structure}[{index}].id must be a string or null.")
        try:
            identifier = _identifier(raw_id, f"{structure}[{index}].id")
        except PlanValidationError:
            # Empty, whitespace-only, or otherwise unusable strings are
            # structural metadata and can be replaced deterministically.
            continue
        if identifier in seen:
            duplicates.add(index)
            continue
        identifiers[index] = identifier
        seen.add(identifier)

    assigned = 0
    next_sequence = 1
    duplicates_resolved = 0
    for index, identifier in enumerate(identifiers):
        if identifier is not None:
            normalized[index]["id"] = identifier
            continue
        if index in duplicates:
            duplicates_resolved += 1
        base = _identifier(id_factory(index, next_sequence), f"{structure}[{index}].id")
        candidate = base
        suffix = 2
        while candidate in seen:
            next_sequence += 1
            next_base = _identifier(id_factory(index, next_sequence), f"{structure}[{index}].id")
            if next_base != base:
                base = next_base
                candidate = base
                suffix = 2
            else:
                tail = f"-{suffix}"
                candidate = f"{base[:MAX_IDENTIFIER_CHARS - len(tail)]}{tail}"
                suffix += 1
        normalized[index]["id"] = candidate
        seen.add(candidate)
        assigned += 1
        next_sequence += 1

    if diagnostics is not None and (assigned or duplicates_resolved):
        details = diagnostics.setdefault("stable_ids", {
            "assigned": 0, "duplicates_resolved": 0, "structures": {},
        })
        details["assigned"] += assigned
        details["duplicates_resolved"] += duplicates_resolved
        details["structures"][structure] = {
            "assigned": assigned, "duplicates_resolved": duplicates_resolved,
        }
    return normalized


def _normalize_criterion_links(raw: Any, criteria: list[str], tasks: list[dict[str, Any]],
                               diagnostics: dict[str, Any] | None = None, *,
                               repair_model_criteria: bool = False) -> dict[str, Any]:
    """Persist identity separately from display text; migrate legacy exact links only."""
    if raw is None:
        global_links = [{"criterion": criterion} for criterion in criteria]
        local_links = []
    else:
        links = _object(raw, {"global", "local"}, "plan.criterion_links")
        global_links = _list(links["global"], "plan.criterion_links.global", MAX_CRITERIA)
        local_links = _list(links["local"], "plan.criterion_links.local", MAX_PLAN_TASKS * MAX_CRITERIA)
    # The plan's success_criteria list is authoritative. Models can omit one or
    # more redundant link rows, so retain supplied rows by exact criterion text
    # and deterministically synthesize only the missing rows. Extra, duplicate,
    # or unrelated rows still indicate a malformed plan and fail closed.
    supplied_by_criterion: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(global_links):
        item = _object(entry, {"criterion"}, f"global criterion {index}", optional={"id"})
        criterion = _text(item["criterion"], "global criterion", MAX_CRITERION_CHARS)
        if criterion not in criteria or criterion in supplied_by_criterion:
            raise PlanValidationError(
                "Global criterion links must each match one unique plan criterion."
            )
        supplied_by_criterion[criterion] = {**item, "criterion": criterion}
    global_entries = [
        supplied_by_criterion.get(criterion, {"criterion": criterion})
        for criterion in criteria
    ]
    raw_global_id_counts: dict[str, int] = {}
    for index, entry in enumerate(global_entries):
        raw_id = entry.get("id")
        if not isinstance(raw_id, str):
            continue
        try:
            identifier = _identifier(raw_id, f"global criterion {index} id")
        except PlanValidationError:
            continue
        raw_global_id_counts[identifier] = raw_global_id_counts.get(identifier, 0) + 1
    ambiguous_global_ids = {identifier for identifier, count in raw_global_id_counts.items()
                            if count > 1}
    use_ac_namespace = any(re.fullmatch(r"AC-[1-9][0-9]*", str(entry.get("id") or "").strip(), re.I)
                           for entry in global_entries)
    normalized_global = _ensure_stable_ids(
        global_entries, structure="criterion_links.global",
        id_factory=lambda _index, sequence: (
            f"ac-{sequence}" if use_ac_namespace else f"gc-{sequence}"
        ), diagnostics=diagnostics,
    )
    global_ids = set()
    for index, criterion in enumerate(criteria):
        item = normalized_global[index]
        ident = item["id"]
        if ident in global_ids or item["criterion"] != criterion:
            raise PlanValidationError("Global criterion IDs must be unique and match plan criteria.")
        global_ids.add(ident)
        item["criterion"] = criterion
    global_by_text = {item["criterion"].casefold(): item["id"] for item in normalized_global}
    if len(global_by_text) != len(normalized_global):
        raise PlanValidationError("Global criteria must have distinct text after normalization.")
    task_by_id = {task["id"]: task for task in tasks}
    global_by_wording: dict[str, list[str]] = {}
    for item in normalized_global:
        wording = re.sub(r"[\W_]+", " ", item["criterion"], flags=re.UNICODE).strip().casefold()
        if wording:
            global_by_wording.setdefault(wording, []).append(item["id"])

    parsed_local = []
    source_global_ids: list[str | None] = []
    for index, entry in enumerate(local_links):
        item = _object(entry, {"task_id", "criterion", "supports_global_criteria"},
                       f"local criterion {index}", optional={"id"})
        criterion = _text(item["criterion"], "local criterion", MAX_CRITERION_CHARS)
        wording = re.sub(r"[\W_]+", " ", criterion, flags=re.UNICODE).strip().casefold()
        matching_globals = global_by_wording.get(wording, []) if wording else []
        source_global_ids.append(matching_globals[0] if len(matching_globals) == 1 else None)
        parsed_local.append({
            **item,
            "task_id": _identifier(item["task_id"], f"local criterion {index} task_id"),
            "criterion": criterion,
            "supports_global_criteria": _text_list(
                item["supports_global_criteria"], "supports_global_criteria",
                MAX_CRITERIA, identifiers=True,
            ),
        })
    # An explicit copy of a global obligation is not a task-level check. Scope
    # it to the task when this is new model output, or when a reused global ID
    # makes the mistaken identity explicit. Legacy persisted links stay stable.
    for index, item in enumerate(parsed_local):
        task = task_by_id.get(item["task_id"])
        if task is None or source_global_ids[index] is None:
            continue
        raw_id = item.get("id")
        collides = False
        if isinstance(raw_id, str):
            try:
                collides = _identifier(raw_id, f"local criterion {index} id") in global_ids
            except PlanValidationError:
                pass
        if not (repair_model_criteria or collides):
            continue
        matching = [criterion for criterion in task["success_criteria"]
                    if criterion.casefold() == item["criterion"].casefold()]
        if len(matching) != 1:
            continue
        prefix = f"Verify the result of task '{task['objective']}': "
        specialized = prefix + matching[0]
        if len(specialized) > MAX_CRITERION_CHARS:
            raise PlanValidationError("Task-specific local criterion exceeds the text limit.")
        task["success_criteria"] = [specialized if criterion == matching[0] else criterion
                                    for criterion in task["success_criteria"]]
        item["criterion"] = specialized

    expected = {(task["id"], criterion.casefold()): criterion
                for task in tasks for criterion in task["success_criteria"]}
    exact_covered = {(item["task_id"], item["criterion"].casefold()) for item in parsed_local
                     if (item["task_id"], item["criterion"].casefold()) in expected}
    repaired_covered: set[tuple[str, str]] = set()
    for index, item in enumerate(parsed_local):
        key = (item["task_id"], item["criterion"].casefold())
        if key in expected or source_global_ids[index] is None:
            continue
        task = task_by_id.get(item["task_id"])
        if task is None:
            continue
        candidates = [criterion for criterion in task["success_criteria"]
                      if (task["id"], criterion.casefold()) not in exact_covered | repaired_covered]
        if len(task["success_criteria"]) == 1 and len(candidates) == 1:
            item["criterion"] = candidates[0]
            repaired_covered.add((task["id"], candidates[0].casefold()))

    for index, item in enumerate(parsed_local):
        supports = item["supports_global_criteria"]
        if any(ref in ambiguous_global_ids for ref in supports):
            source_global_id = source_global_ids[index]
            if len(supports) == 1 and source_global_id is not None:
                item["supports_global_criteria"] = [source_global_id]
            else:
                raise PlanValidationError("Local criterion links reference an ambiguous global criterion ID.")
            continue
        if any(ref not in global_ids for ref in supports):
            source_global_id = source_global_ids[index]
            if len(supports) == 1 and source_global_id is not None:
                item["supports_global_criteria"] = [source_global_id]
            else:
                raise PlanValidationError("Local criterion links reference an unknown global criterion ID.")
    normalized_local = _ensure_stable_ids(
        parsed_local, structure="criterion_links.local",
        id_factory=lambda index, sequence: (
            f"lc-{sequence}" if use_ac_namespace
            else f"tc-{parsed_local[index]['task_id'][:48]}-{index + 1}"
        ), diagnostics=diagnostics,
        forbidden_ids=global_ids,
    )
    local_ids = set()
    covered = set()
    validated_local = []
    for index, item in enumerate(normalized_local):
        ident = item["id"]
        task_id = item["task_id"]
        criterion = item["criterion"]
        key = (task_id, criterion.casefold())
        if ident in local_ids or key in covered or key not in expected:
            raise PlanValidationError("Local criterion links duplicate or substitute a task criterion.")
        supports = item["supports_global_criteria"]
        if any(ref not in global_ids for ref in supports):
            raise PlanValidationError("Local criterion links reference an unknown global criterion ID.")
        local_ids.add(ident)
        covered.add(key)
        validated_local.append({"id": ident, "task_id": task_id,
                                "criterion": expected[key], "supports_global_criteria": supports})
    next_local_sequence = 1
    for task in tasks:
        for index, criterion in enumerate(task["success_criteria"], 1):
            key = (task["id"], criterion.casefold())
            if key in covered:
                continue
            base = (f"lc-{next_local_sequence}" if use_ac_namespace
                    else f"tc-{task['id'][:48]}-{index}")
            ident = base
            suffix = 2
            while ident in local_ids or ident in global_ids:
                if use_ac_namespace:
                    next_local_sequence += 1
                    ident = f"lc-{next_local_sequence}"
                else:
                    ident = f"{base[:MAX_IDENTIFIER_CHARS - len(str(suffix)) - 1]}-{suffix}"
                    suffix += 1
            if use_ac_namespace:
                next_local_sequence += 1
            local_ids.add(ident)
            validated_local.append({"id": ident, "task_id": task["id"],
                                    "criterion": criterion,
                                    "supports_global_criteria": ([global_by_text[criterion.casefold()]]
                                                                 if criterion.casefold() in global_by_text else [])})
    return {"global": normalized_global, "local": validated_local}


def normalize_plan(value: Any, *, diagnostics: dict[str, Any] | None = None,
                   repair_model_criteria: bool = False) -> dict[str, Any]:
    """Return a stable representation while enforcing field types and bounds."""
    raw = _object(value, PLAN_FIELDS, "plan", optional={CRITERION_LINKS_FIELD})
    complexity = _text(raw["complexity"], "plan.complexity", 32).casefold().replace("-", "_")
    if complexity not in {"simple", "multi_step"}:
        raise PlanValidationError("plan.complexity must be simple or multi_step.")
    raw_tasks = _list(raw["tasks"], "plan.tasks", MAX_PLAN_TASKS)
    if not raw_tasks:
        raise PlanValidationError("plan.tasks must not be empty.")
    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks):
        task = _object(item, TASK_FIELDS, f"plan.tasks[{index}]", optional=TASK_METADATA_FIELDS)
        capabilities = _text_list(
            task["required_capabilities"], f"plan.tasks[{index}].required_capabilities",
            MAX_CAPABILITIES, identifiers=False,
        )
        for capability in capabilities:
            if capability not in CAPABILITY_REGISTRY:
                raise PlanValidationError(f"Unknown capability: {capability}.")
        normalized_task = {
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
        }
        if "task_kind" in task:
            task_kind = _text(task["task_kind"], f"plan.tasks[{index}].task_kind", 64).casefold()
            if task_kind not in TASK_KIND_VALUES:
                raise PlanValidationError(f"Unknown task kind: {task_kind}.")
            normalized_task["task_kind"] = task_kind
        if "task_characteristics" in task:
            characteristics = task["task_characteristics"]
            if not isinstance(characteristics, dict) or any(
                    not isinstance(key, str) or not isinstance(value, bool)
                    for key, value in characteristics.items()):
                raise PlanValidationError(
                    f"plan.tasks[{index}].task_characteristics must be a boolean object."
                )
            normalized_task["task_characteristics"] = dict(characteristics)
        tasks.append(normalized_task)
    # Task count is the authoritative structural signal. Models sometimes label
    # an otherwise valid multi-task graph as simple; canonicalize that harmless
    # inconsistency instead of preserving contradictory data.
    complexity = "simple" if len(tasks) == 1 else "multi_step"
    result = {
        "goal": _text(raw["goal"], "plan.goal", MAX_GOAL_CHARS),
        "summary": _text(raw["summary"], "plan.summary", MAX_SUMMARY_CHARS),
        "complexity": complexity,
        "tasks": tasks,
        "success_criteria": _text_list(raw["success_criteria"], "plan.success_criteria",
                                        MAX_CRITERIA, allow_empty=False),
    }
    result[CRITERION_LINKS_FIELD] = _normalize_criterion_links(
        raw.get(CRITERION_LINKS_FIELD), result["success_criteria"], tasks, diagnostics,
        repair_model_criteria=repair_model_criteria)
    return result


def validate_plan(value: Any, *, diagnostics: dict[str, Any] | None = None,
                  repair_model_criteria: bool = False) -> dict[str, Any]:
    """Normalize and validate IDs, references and the dependency DAG."""
    plan = normalize_plan(value, diagnostics=diagnostics,
                          repair_model_criteria=repair_model_criteria)
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


def _require_executable_global_coverage(plan: dict[str, Any]) -> None:
    """Reject model plans whose executable global obligations have no task link."""
    covered = {ref for item in plan[CRITERION_LINKS_FIELD]["local"]
               for ref in item["supports_global_criteria"]}
    for item in plan[CRITERION_LINKS_FIELD]["global"]:
        if item["id"] not in covered and criterion_key(item["criterion"]) not in STRUCTURAL_CRITERIA:
            raise PlanValidationError(
                f"Global criterion {item['id']} has no explicit local task coverage."
            )


def _check_analyst_acceptance_identity(plan: dict[str, Any], analysis: Any) -> None:
    """An AC-N reused by the Planner must still name the Analyst's obligation."""
    if not isinstance(analysis, dict) or not isinstance(analysis.get("acceptance_criteria"), list):
        return
    analyst_criteria = {item["id"].casefold(): criterion_key(item["description"])
                        for item in analysis["acceptance_criteria"]}
    for item in plan[CRITERION_LINKS_FIELD]["global"]:
        if item["id"].startswith("ac-") and (
                analyst_criteria.get(item["id"]) != criterion_key(item["criterion"])):
            raise PlanValidationError(
                f"Planner global {item['id']} does not match a Task Analyst acceptance criterion."
            )


def _remove_analyst_acceptance_placeholders(value: Any, analysis: Any,
                                            diagnostics: dict[str, Any] | None = None) -> Any:
    """Keep plan criteria authoritative when a global row repeats the Analyst AC."""
    if not isinstance(value, dict) or not isinstance(analysis, dict):
        return value
    analyst_rows = analysis.get("acceptance_criteria")
    links = value.get(CRITERION_LINKS_FIELD)
    criteria = value.get("success_criteria")
    if (not isinstance(analyst_rows, list) or not isinstance(links, dict)
            or not isinstance(links.get("global"), list) or not isinstance(criteria, list)):
        return value
    analyst_values = {criterion_key(value)
                      for item in analyst_rows if isinstance(item, dict)
                      for value in (item.get("id"), item.get("description"))
                      if isinstance(value, str)}
    criterion_texts = {criterion_key(item)
                       for item in criteria if isinstance(item, str)}
    retained = []
    removed = 0
    for entry in links["global"]:
        if isinstance(entry, dict) and set(entry) <= {"id", "criterion"}:
            text = entry.get("criterion")
            raw_id = entry.get("id")
            if (isinstance(text, str) and (raw_id is None or isinstance(raw_id, str))
                    and criterion_key(text) in analyst_values
                    and criterion_key(text) not in criterion_texts):
                removed += 1
                continue
        retained.append(entry)
    if not removed:
        return value
    if diagnostics is not None:
        diagnostics["analyst_acceptance_placeholders_removed"] = removed
    return {**value, CRITERION_LINKS_FIELD: {**links, "global": retained}}


def _expand_model_task_ids(value: Any, diagnostics: dict[str, Any] | None = None) -> Any:
    """Expand model shorthand T-N and its references without renaming other IDs."""
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return value
    replacements: dict[str, str] = {}
    for task in value["tasks"]:
        if not isinstance(task, dict):
            continue
        raw_id = task.get("id")
        match = (re.fullmatch(r"T-([1-9][0-9]*)", raw_id.strip(), re.I)
                 if isinstance(raw_id, str) and len(raw_id.strip()) <= MAX_IDENTIFIER_CHARS else None)
        if match:
            replacements[f"t-{int(match.group(1))}"] = f"task-{int(match.group(1))}"
    if not replacements:
        return value

    def rewrite(ref: Any) -> Any:
        if not isinstance(ref, str):
            return ref
        try:
            return replacements.get(_identifier(ref, "task reference"), ref)
        except PlanValidationError:
            return ref

    tasks = []
    for task in value["tasks"]:
        if not isinstance(task, dict):
            tasks.append(task)
            continue
        updated = dict(task)
        updated["id"] = rewrite(task.get("id"))
        if isinstance(task.get("depends_on"), list):
            updated["depends_on"] = [rewrite(ref) for ref in task["depends_on"]]
        tasks.append(updated)
    updated_plan = {**value, "tasks": tasks}
    links = value.get(CRITERION_LINKS_FIELD)
    if isinstance(links, dict) and isinstance(links.get("local"), list):
        updated_plan[CRITERION_LINKS_FIELD] = {
            **links,
            "local": [{**entry, "task_id": rewrite(entry.get("task_id"))}
                      if isinstance(entry, dict) else entry for entry in links["local"]],
        }
    if diagnostics is not None:
        diagnostics["task_ids_expanded"] = len(replacements)
    return updated_plan



def allocate_new_task_ids(tasks: list[dict[str, Any]], reserved_ids: set[str],
                          protected_ids: set[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Assign deterministic unique IDs to proposed additions without changing existing IDs."""
    reserved = set(reserved_ids)
    proposed, normalized, final, collisions = [], [], [], []
    allocated = []
    for index, source in enumerate(tasks):
        if not isinstance(source, dict):
            raise PlanValidationError("New recovery task must be an object.")
        item = dict(source)
        raw = item.get("id")
        base = _identifier(raw, f"new task {index} id")
        ident = base
        suffix = 2
        if ident in reserved:
            collisions.append({"proposed": str(raw)[:MAX_IDENTIFIER_CHARS], "normalized": base})
        while ident in reserved:
            tail = f"-{suffix}"
            ident = base[:MAX_IDENTIFIER_CHARS - len(tail)] + tail
            suffix += 1
        reserved.add(ident)
        item["id"] = ident
        allocated.append(item)
        proposed.append(str(raw)[:MAX_IDENTIFIER_CHARS])
        normalized.append(base)
        final.append(ident)
    protected = set(protected_ids or ())
    unique = {base: final[index] for index, base in enumerate(normalized)
              if normalized.count(base) == 1 and base not in protected}
    for item in allocated:
        dependencies = item.get("depends_on")
        if isinstance(dependencies, list):
            item["depends_on"] = [
                unique.get(_identifier(dep, "new task dependency"), dep) for dep in dependencies
            ]
    return allocated, {"proposed_ids": proposed, "normalized_ids": normalized,
                       "collisions": collisions, "final_ids": final}


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
            if isinstance(call_metrics.get("transport"), dict):
                self.metrics.setdefault("model_call_details", []).append(call_metrics["transport"])
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
    def _parse_output(value: Any, analysis: Any = None,
                      diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(value, dict) and set(value) == {"message"} and isinstance(value["message"], dict):
            value = value["message"].get("content")
        if isinstance(value, str):
            if len(value) > MAX_MODEL_OUTPUT_CHARS:
                raise PlanValidationError("Planner output is too large.")
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise PlanValidationError("Planner output is not valid JSON.") from exc
        value = _expand_model_task_ids(value, diagnostics)
        value = _remove_analyst_acceptance_placeholders(value, analysis, diagnostics)
        declared_links = isinstance(value, dict) and CRITERION_LINKS_FIELD in value
        plan = _collapse_simple_artifact_plan(validate_plan(
            value, diagnostics=diagnostics, repair_model_criteria=True))
        plan = _reconcile_task_analysis(plan, analysis)
        plan = _append_qa_task(plan, analysis)
        plan = _append_code_audit_task(plan, analysis)
        _check_analyst_acceptance_identity(plan, analysis)
        if declared_links:
            _require_executable_global_coverage(plan)
        return plan

    @staticmethod
    def _prompt(goal: str, context: dict[str, Any]) -> str:
        capability_ids = [item.get("id") for item in context.get("capabilities", [])
                          if isinstance(item, dict) and isinstance(item.get("id"), str)]
        skill_ids = [item.get("id") for item in context.get("skills", [])
                     if isinstance(item, dict) and isinstance(item.get("id"), str)]
        return (
            "Create a work plan and return one JSON object only. Do not use Markdown. "
            "Required plan fields: goal, summary, complexity, tasks, success_criteria, criterion_links. "
            "Freya assigns stable IDs to global and local criteria in criterion_links; "
            "success_criteria and criterion_links.global are plan-wide acceptance obligations. "
            "Each task's success_criteria are concrete checks of that task's result, and each "
            "criterion_links.local row must name exactly one of those task checks plus its task_id. "
            "Global and local rows are different objects with different IDs; never copy a global "
            "row or its ID into a local row. supports_global_criteria may name only existing global IDs. "
            "Explicitly link every plan-wide obligation that needs task execution to at least one "
            "task check whose evidence can prove it; wording may differ. "
            "The Task Analyst's REQ-N requirements and AC-N acceptance criteria are upstream "
            "constraints; their verifies references may name only existing REQ-N IDs. "
            "Reuse an Analyst AC-N as a global link ID only for that exact acceptance criterion; "
            "use a plan-specific ID for a different global criterion. Each global row's criterion "
            "must repeat one complete success_criteria text exactly; never put an ID such as AC-1 "
            "in the criterion text field or copy the Analyst's generic AC text unless that full text "
            "is also a plan success_criteria entry. "
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
            "The Task Analyst brief is supplied in the structured context; do not invent a prerequisite "
            "brief file or a hidden producer task unless the user explicitly requests that artifact. "
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
        limited_context = dict(context) if isinstance(context, dict) else {}
        analysis = limited_context.get("task_analysis")
        if isinstance(analysis, dict) and ("requirements" in analysis or "acceptance_criteria" in analysis):
            try:
                limited_context["task_analysis"] = validate_task_analysis(analysis)
            except TaskAnalysisError as exc:
                raise PlanGenerationError(f"Task Analyst references are invalid: {exc}") from exc
        if self.decide is None:
            if self.offline:
                analysis = limited_context.get("task_analysis")
                plan = _reconcile_task_analysis(fallback_plan(goal), analysis)
                plan = _append_qa_task(plan, analysis)
                plan = _append_code_audit_task(plan, analysis)
                _check_analyst_acceptance_identity(plan, analysis)
                _require_executable_global_coverage(plan)
                return plan
            raise PlanGenerationError("No planner model is configured. Use explicit offline mode for fallback planning.")
        request = self._prompt(goal, limited_context)
        try:
            output = self._call(request, limited_context)
        except Exception as exc:
            raise PlanGenerationError(f"Planner model call failed: {exc}") from exc
        try:
            normalization = {}
            parsed = self._parse_output(output, limited_context.get("task_analysis"), normalization)
            parsed["goal"] = goal
            if normalization:
                self.metrics["normalization"] = normalization
            return validate_plan(parsed)
        except (PlanValidationError, TypeError, ValueError) as first_error:
            rendered = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            repair_prompt = (
                request + "\nThe previous response was invalid. Repair only the field or criterion-link "
                "structure named by the validation error, preserving the other tasks, criteria, "
                "dependencies, capabilities, and the Task Analyst's meaning. Return only the complete JSON object. "
                f"Validation error: {first_error}. Previous response: {rendered[:MAX_MODEL_OUTPUT_CHARS]}"
            )
            try:
                repaired = self._call(repair_prompt, {**limited_context, "_freya_repair": True})
                normalization = {}
                parsed = self._parse_output(repaired, limited_context.get("task_analysis"), normalization)
                parsed["goal"] = goal
                if normalization:
                    self.metrics["normalization"] = normalization
                return validate_plan(parsed)
            except Exception as second_error:
                raise PlanGenerationError(
                    f"Planner output remained invalid after one repair attempt: {second_error}"
                ) from second_error
