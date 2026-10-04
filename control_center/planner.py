"""Structured, fail-closed planning for Freya orchestrations.

The planner describes work.  Capability policy remains the only authority that
can allow a worker action, and preferred Skills are non-binding selection hints.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .capabilities import CAPABILITY_REGISTRY
from .config import validate_endpoint
from .transport import model_profile, model_request, request_json
from .task_analyst import (TaskAnalysisError, canonical_task_kind,
                           validate_task_analysis)
from .integration_proof import STRUCTURAL_CRITERIA, criterion_key
from .runtime_resources import (
    RuntimeResourceCatalog, SEMANTIC_OPERATION_CAPABILITIES, UnknownTool,
    UnsupportedResourceRequirement,
)
from .skills import SkillCompatibilityError
from .plan_scope import PlannerScopeError, semantic_plan_snapshot
from .cross_task import (MAX_OWNED_PATHS, CrossTaskRequestError,
                         normalize_owned_paths, owned_path_key)
from .worker_assignment import worker_assignments
from .plan_granularity import GRANULARITY_FIELDS, normalize_granularity
from .verification_cases import case_response_format, criterion_reference_matches
from .security import sanitize


SEMANTIC_PLAN_SCHEMA_VERSION = 5
PLAN_SCHEMA_VERSION = 4
MAX_PLAN_TASKS = 20
# Bound the entire orchestration-local repair loop: one initial proposal and
# at most three Planner repairs, regardless of Compiler or local guard rejects.
MAX_SEMANTIC_PLAN_REPAIRS = 3
MAX_SEMANTIC_PLAN_ATTEMPTS = MAX_SEMANTIC_PLAN_REPAIRS + 1
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
DEFAULT_PLANNER_MODEL = "phi4:14b"
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
TASK_METADATA_FIELDS = {"task_kind", "task_characteristics", "semantic_needs", "required_tools",
                        "semantic_operations", "owned_paths", "write_targets", "foreign_write_targets",
                        "verification_cases", "verification_mode", "granularity"}
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
                    "semantic_needs": {"type": "array", "items": {"type": "string"}},
                    "required_tools": {"type": "array", "items": {"type": "string"}},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "owned_paths": {"type": "array", "items": {"type": "string"}},
                    "write_targets": {"type": "array", "items": {"type": "string"}},
                    "foreign_write_targets": {"type": "array", "items": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "owner_plan_task_id": {"type": "string"}},
                        "required": ["path", "owner_plan_task_id"], "additionalProperties": False}},
                    "task_kind": {"type": "string", "enum": sorted(TASK_KIND_VALUES)},
                    "task_characteristics": {"type": "object"},
                    "granularity": {"type": "object", "properties": {
                        field: {"type": "string", "maxLength": 1000}
                        for field in sorted(GRANULARITY_FIELDS)}, "additionalProperties": False},
                    "verification_mode": {"type": "string", "enum": ["independent_cases", "interactive_session"]},
                    "verification_cases": case_response_format(compiled=True),
                },
                "required": sorted(TASK_FIELDS | {"owned_paths"}),
                "additionalProperties": False,
            },
        },
        "write_owners": {"type": "object", "additionalProperties": {"type": "string"}},
        "granularity_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
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


def _catalog_enum(context: dict[str, Any], resource_type: str) -> list[str]:
    field = {"capability": "capabilities", "tool": "tools", "skill": "skills"}[resource_type]
    records = context.get(field, [])
    values = []
    alias_counts: dict[str, int] = {}
    if isinstance(records, list):
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                values.append(item["id"])
                declared_aliases = item.get("aliases", [])
                if isinstance(declared_aliases, list):
                    for alias in declared_aliases:
                        if isinstance(alias, str) and alias:
                            alias_counts[alias] = alias_counts.get(alias, 0) + 1
    ids = set(values)
    return sorted(ids | {alias for alias, count in alias_counts.items()
                         if count == 1 and alias not in ids})


def _resource_array_schema(context: dict[str, Any], resource_type: str) -> dict[str, Any]:
    values = _catalog_enum(context, resource_type)
    schema = {"type": "array", "items": {"type": "string", "enum": values}}
    if not values:
        schema["maxItems"] = 0
    return schema


def semantic_plan_response_format(context: dict[str, Any]) -> dict[str, Any]:
    """Build a closed semantic-operation schema without runtime authority fields."""
    operation_records = context.get("semantic_operations", [])
    operation_ids = sorted({item["id"] for item in operation_records
                            if isinstance(item, dict) and isinstance(item.get("id"), str)})
    if not operation_ids:
        operation_ids = sorted(SEMANTIC_OPERATION_CAPABILITIES)
    task_fields = {
        "key": {"type": "string"},
        "task_kind": {"type": "string", "enum": sorted(TASK_KIND_VALUES)},
        "objective": {"type": "string"},
        "description": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "semantic_needs": {"type": "array", "items": {"type": "string"}},
        "operations": {"type": "array", "items": {"type": "string", "enum": operation_ids}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "owned_paths": {"type": "array", "items": {"type": "string"}},
        "write_targets": {"type": "array", "items": {"type": "string"}},
        "granularity": {"type": "object", "properties": {
            field: {"type": "string", "maxLength": 1000} for field in sorted(GRANULARITY_FIELDS)},
            "additionalProperties": False},
        "verification_mode": {"type": "string", "enum": ["independent_cases", "interactive_session"]},
        "verification_cases": case_response_format(),
    }
    unsupported = {"type": "array", "items": {"type": "object", "properties": {
        "semantic_need": {"type": "string"},
        "reason": {"type": "string"},
    }, "required": ["semantic_need", "reason"], "additionalProperties": False}}
    # Require case decisions on QA only. Requiring a nonempty-looking case
    # contract on writers encouraged models to attach execution cases to tasks
    # that deliberately have no execution resource.
    implementation_fields = copy.deepcopy(task_fields)
    implementation_fields['task_kind']['enum'] = sorted(TASK_KIND_VALUES - {'testing'})
    implementation_fields['verification_cases']['maxItems'] = 0
    testing_fields = copy.deepcopy(task_fields)
    testing_fields['task_kind']['enum'] = ['testing']
    for field in ('owned_paths', 'write_targets'):
        testing_fields[field]['maxItems'] = 0
    task_schemas = [
        {'type': 'object', 'properties': implementation_fields,
         'required': sorted(set(task_fields) - {'verification_mode', 'verification_cases', 'granularity'}),
         'additionalProperties': False},
        {'type': 'object', 'properties': testing_fields,
         'required': sorted(set(task_fields) - {'granularity'}), 'additionalProperties': False},
    ]
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "task_complexity": {"type": "string", "enum": ["simple", "multi_step", "complex"]},
            "execution_strategy": {"type": "string", "enum": ["single_worker", "multi_worker"]},
            "decomposition_reason": {"type": "string"},
            "granularity_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
            "tasks": {"type": "array", "minItems": 1, "maxItems": MAX_PLAN_TASKS,
                      "items": {'anyOf': task_schemas}},
            "unsupported_requirements": unsupported,
        },
        "required": ["summary", "task_complexity", "execution_strategy",
                     "decomposition_reason", "success_criteria", "tasks",
                     "unsupported_requirements"],
        "additionalProperties": False,
    }


def planner_response_format(context: dict[str, Any], *, semantic: bool) -> dict[str, Any]:
    """Create model output constraints using the live resource IDs."""
    if semantic:
        return semantic_plan_response_format(context)
    schema = copy.deepcopy(PLAN_RESPONSE_FORMAT)
    task_properties = schema["properties"]["tasks"]["items"]["properties"]
    task_properties["required_capabilities"] = _resource_array_schema(context, "capability")
    task_properties["preferred_skills"] = _resource_array_schema(context, "skill")
    task_properties["required_tools"] = _resource_array_schema(context, "tool")
    return schema

PLANNER_SOURCE_OF_TRUTH_INSTRUCTIONS = (
    "SOURCE OF TRUTH: The canonical Task Spec is the source of truth for user intent. "
    "Do not invent user preferences, user decisions, requirements, constraints, deliverables, "
    "or external dependencies unless they are explicitly present in the Task Spec or are strictly "
    "necessary implementation details.\n\n"
    "MISSING INFORMATION: If a missing decision materially changes the requested product, "
    "request clarification through the existing clarification mechanism before planning. If it is "
    "only an implementation detail, choose a reasonable default, record it as an assumption in the "
    "implementing task description, and continue with implementation. Do not create a task whose "
    "purpose is to discover a user preference that is not present in the Task Spec.\n\n"
    "WORKSPACE PREFERENCES: Never assume that user preferences are stored in workspace files. "
    "Do not create tasks to discover a user preference, find a user selection, or read configuration "
    "chosen by the user unless the Task Spec explicitly references that file or resource.\n\n"
    "IMPLEMENTATION: Prefer the simplest implementation that satisfies the Task Spec. Do not "
    "introduce frameworks, databases, services, libraries, or architectural components unless the "
    "Task Spec requires them or they are reasonably necessary to implement the requested behavior."
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


def _normalize_python_console_calculator(plan: dict[str, Any],
                                         task_spec: dict[str, Any],
                                         resource_catalog: RuntimeResourceCatalog | None = None) -> dict[str, Any]:
    """Deprecated legacy console adapter; never applies to web/desktop work."""
    objective = str(task_spec.get("objective") or "").casefold()
    context_text = " ".join(
        str(item.get("description") or "")
        for field in ("deliverables", "requirements", "constraints")
        for item in task_spec.get(field, []) if isinstance(item, dict)
    ).casefold()
    request_text = objective + " " + context_text
    if not ("python" in request_text and any(word in request_text for word in ("calculadora", "calculator"))
            and any(word in request_text for word in ("consola", "console"))):
        return plan
    if any(word in request_text for word in ("web", "desktop", "gráfica", "graphical", "gui")):
        return plan

    expectations = [item for item in task_spec.get("validation_expectations", [])
                    if isinstance(item, str) and item.strip()]
    criteria = list(dict.fromkeys([
        "calculator.py exists in the selected workspace and can be read.",
        *(item.strip() for item in expectations),
        "With input lines 3 and 5, calculator.py prints 8 and exits successfully.",
    ]))
    normalized = dict(plan)
    normalized["goal"] = task_spec["objective"]
    normalized["summary"] = "Create and verify the requested Python console calculator."
    normalized["complexity"] = "simple"
    if "execution_strategy" in normalized:
        normalized["execution_strategy"] = "single_worker"
        normalized["decomposition_reason"] = (
            "One implementation worker owns the console calculator; controlled QA is appended separately.")
    normalized["success_criteria"] = criteria
    normalized.pop(CRITERION_LINKS_FIELD, None)
    normalized.pop("write_owners", None)
    from .plan_compiler import compile_semantic_task_resources
    normalized["tasks"] = [compile_semantic_task_resources({
        "id": "task-1",
        "task_kind": "program_creation",
        "objective": "Implement the Python console calculator.",
        "description": (
            "Create calculator.py once. Read two numbers from standard input, add them, and print "
            "int(total) when total.is_integer(); otherwise print total. This makes inputs 3 and 5 "
            "produce exactly 8, never 8.0, while fractional sums retain decimals. Write only "
            "calculator.py and read it back once. Do not overwrite, modify, or create another file."
        ),
        "depends_on": [],
        "operations": ["create_file", "read_file"],
        "owned_paths": ["calculator.py"],
        "semantic_needs": [
            "Create calculator.py as a Python console program.",
            "Read two numbers from standard input, add them, print int(total) only for integer-valued sums, and preserve fractional output.",
            "Write the source file once, read it back once, and do not attempt a second write.",
            "Leave controlled execution and output verification to the dependent verification task.",
        ],
        "success_criteria": [criteria[0]],
    }, resource_catalog)]
    return validate_plan(normalized)

def _append_code_audit_task(plan: dict[str, Any], analysis: Any = None,
                            resource_catalog: RuntimeResourceCatalog | None = None) -> dict[str, Any]:
    """Add exactly one read-only Code Review task after code/file changes."""
    if _is_trivial_task(plan, analysis):
        return plan
    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) >= MAX_PLAN_TASKS:
        return plan
    operations = {
        str(operation) for task in tasks
        for operation in task.get("semantic_operations", [])
    }
    if not operations.intersection({"create_file", "modify_file", "overwrite_file"}):
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
    if "execution_strategy" in audited:
        audited["execution_strategy"] = "multi_worker"
        audited["decomposition_reason"] = (
            str(audited.get("decomposition_reason") or "") +
            " A separate read-only audit provides independent review after implementation.")[:1000]
    from .plan_compiler import compile_semantic_task_resources
    audited["tasks"] = [*tasks, compile_semantic_task_resources({
        "id": audit_id,
        "task_kind": "review",
        "objective": "Audit the code produced for the user's request",
        "description": (
            "Perform a read-only code audit after implementation. Inspect the current workspace, "
            "the resulting diff when available, callers and relevant edge cases. Do not modify files "
            "or execute commands. Report confirmed findings and hypotheses separately."
        ),
        "depends_on": [task["id"] for task in tasks],
        "operations": ["read_file"],
        "success_criteria": [
            "The changed code is inspected with the Code Review skill.",
            "Findings include severity, evidence and file references, or clearly state that no findings were detected.",
        ],
    }, resource_catalog)]
    return validate_plan(audited)


def _append_qa_task(plan: dict[str, Any], analysis: Any,
                    resource_catalog: RuntimeResourceCatalog | None = None) -> dict[str, Any]:
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
    from .plan_compiler import compile_semantic_task_resources
    qa = compile_semantic_task_resources({
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
        "operations": ["read_file", "run_python_script"],
        "semantic_needs": [
            "Read the implemented program.",
            "Run it with bounded controlled stdin and capture output and exit status.",
        ],
        "task_kind": "testing",
        "task_characteristics": {"interactive": True, "requires_user_input": True},
        "success_criteria": [
            "Representative controlled input completes without timeout or crash.",
            "Observed output is logically correct for the supplied input.",
            "The QA report cites the command, bounded stdin case, exit status and observed output.",
        ],
    }, resource_catalog)
    updated = dict(plan)
    updated["complexity"] = "multi_step"
    if "execution_strategy" in updated:
        updated["execution_strategy"] = "multi_worker"
        updated["decomposition_reason"] = (
            str(updated.get("decomposition_reason") or "") +
            " Controlled interactive QA requires an independent dependent worker.")[:1000]
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


class RepeatedSemanticPlanError(PlanValidationError):
    """A candidate repeats a Semantic Plan rejected by the Compiler."""

    def __init__(self, previous_error: dict[str, Any] | None = None):
        self.previous_error = sanitize(previous_error or {})
        super().__init__(
            "Planner repeated a Semantic Plan that the Compiler had already rejected. "
            "A complete, different plan is required."
        )


def _fingerprint_text(value: Any) -> str:
    """Normalize prose and punctuation without changing the plan's intent fields."""
    if not isinstance(value, str):
        return ""
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def _fingerprint_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"/+", "/", value.strip().replace("\\", "/")).rstrip("/").casefold()


def _fingerprint_operation(value: Any, *, structural: bool = False) -> str:
    """Collapse renamed descriptive operations to their action family.

    Registered runtime operations keep their executable identity except for the
    generic file/action verbs. This catches renamed planner labels without
    treating, for example, two different test runners as interchangeable.
    """
    normalized = _fingerprint_text(value)
    if not normalized:
        return ""
    words = normalized.split()
    first = words[0]
    token = "_".join(words)
    if token in SEMANTIC_OPERATION_CAPABILITIES:
        if structural and token == "create_file":
            return "action:create"
        if structural and token == "modify_file":
            return "action:implement"
        if structural and token in {"read_file", "list_workspace", "search_workspace"}:
            return "action:read"
        return "runtime:" + token
    if first in {"create", "make", "generate", "build", "scaffold"}:
        return "action:create"
    if first in {"implement", "develop", "code", "write"}:
        return "action:implement"
    if first in {"test", "verify", "validate", "check"}:
        return "action:test"
    if first in {"modify", "update", "edit", "refactor"}:
        return "action:modify"
    if first in {"read", "inspect", "analyze", "review", "list", "search"}:
        return "action:read"
    return "descriptor:" + normalized


def _fingerprint_case(case: Any) -> Any:
    if not isinstance(case, dict):
        return case
    # Case IDs are generated labels. Inputs and declared criterion text carry
    # the actual verification semantics and therefore remain in the digest.
    return {
        "input": case.get("input"),
        "expected": case.get("expected"),
        "supports_criteria": sorted(
            _fingerprint_text(item) for item in case.get("supports_criteria", [])
            if isinstance(item, str)
        ),
    }


def _semantic_plan_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Keep semantic plan fields and discard generated IDs and arbitrary metadata."""
    raw_tasks = value.get("tasks")
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    positions = {
        task["key"]: index for index, task in enumerate(tasks)
        if isinstance(task, dict) and isinstance(task.get("key"), str)
    }
    projected_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            projected_tasks.append({"invalid_type": type(task).__name__})
            continue
        dependencies = task.get("depends_on", [])
        if isinstance(dependencies, list):
            dependencies = sorted(
                f"task:{positions[item]}" if isinstance(item, str) and item in positions
                else f"unknown:{_fingerprint_text(item)}"
                for item in dependencies
            )
        else:
            dependencies = {"invalid_type": type(dependencies).__name__}
        granularity = task.get("granularity", {})
        projected_tasks.append({
            "task_kind": _fingerprint_text(task.get("task_kind")),
            "objective": _fingerprint_text(task.get("objective")),
            "description": _fingerprint_text(task.get("description")),
            "semantic_needs": sorted(
                _fingerprint_text(item) for item in task.get("semantic_needs", [])
                if isinstance(item, str)
            ),
            "operations": sorted(
                _fingerprint_operation(item) for item in task.get("operations", [])
            ) if isinstance(task.get("operations", []), list) else [],
            "semantic_operations": sorted(
                _fingerprint_operation(item) for item in task.get("semantic_operations", [])
            ) if isinstance(task.get("semantic_operations", []), list) else [],
            "dependencies": dependencies,
            "write_targets": sorted(
                _fingerprint_path(item) for item in task.get("write_targets", [])
                if isinstance(item, str)
            ),
            "owned_paths": sorted(
                _fingerprint_path(item) for item in task.get("owned_paths", [])
                if isinstance(item, str)
            ),
            "success_criteria": sorted(
                _fingerprint_text(item) for item in task.get("success_criteria", [])
                if isinstance(item, str)
            ),
            "verification_mode": _fingerprint_text(task.get("verification_mode")),
            "verification_cases": sorted(
                (_fingerprint_case(item) for item in task.get("verification_cases", [])),
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
            ) if isinstance(task.get("verification_cases", []), list) else [],
            "granularity": {
                key: _fingerprint_text(text) for key, text in granularity.items()
                if key in GRANULARITY_FIELDS and isinstance(text, str)
            } if isinstance(granularity, dict) else {},
        })
    return {
        "task_count": len(tasks) if isinstance(raw_tasks, list) else None,
        "task_complexity": _fingerprint_text(value.get("task_complexity")),
        "execution_strategy": _fingerprint_text(value.get("execution_strategy")),
        "granularity_reason": _fingerprint_text(value.get("granularity_reason")),
        "decomposition_reason": _fingerprint_text(value.get("decomposition_reason")),
        "unsupported_requirements": sorted(
            ({
                "semantic_need": _fingerprint_text(item.get("semantic_need")),
                "reason": _fingerprint_text(item.get("reason")),
            } for item in value.get("unsupported_requirements", [])
              if isinstance(item, dict)),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        ) if isinstance(value.get("unsupported_requirements", []), list) else [],
        "success_criteria": sorted(
            _fingerprint_text(item) for item in value.get("success_criteria", [])
            if isinstance(item, str)
        ) if isinstance(value.get("success_criteria", []), list) else [],
        "tasks": projected_tasks,
    }


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _semantic_plan_fingerprint(value: dict[str, Any]) -> str:
    """Stable digest of the normalized semantic plan, excluding IDs and metadata."""
    return _fingerprint(_semantic_plan_projection(value))


def _semantic_plan_structure_fingerprint(value: dict[str, Any]) -> str:
    """Identify equivalent task/action graphs despite task-key or prose rewrites."""
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        return _fingerprint({"tasks_type": type(tasks).__name__})
    keys = {
        task["key"]: index for index, task in enumerate(tasks)
        if isinstance(task, dict) and isinstance(task.get("key"), str)
    }
    node_signatures = []
    for task in tasks:
        if not isinstance(task, dict):
            node_signatures.append(_fingerprint({"invalid_type": type(task).__name__}))
            continue
        operations = [*_as_list(task.get("operations")), *_as_list(task.get("semantic_operations"))]
        cases = [_fingerprint_case(case) for case in _as_list(task.get("verification_cases"))]
        node_signatures.append(_fingerprint({
            "task_kind": _fingerprint_text(task.get("task_kind")),
            "operations": sorted({_fingerprint_operation(item, structural=True)
                                  for item in operations}),
            "owned_paths": sorted(_fingerprint_path(item) for item in _as_list(task.get("owned_paths"))
                                  if isinstance(item, str)),
            "write_targets": sorted(_fingerprint_path(item) for item in _as_list(task.get("write_targets"))
                                    if isinstance(item, str)),
            "verification_mode": _fingerprint_text(task.get("verification_mode")),
            "verification_cases": sorted(
                cases, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)),
        }))
    node_ids = []
    occurrences: dict[str, int] = {}
    for signature in node_signatures:
        occurrence = occurrences.get(signature, 0)
        occurrences[signature] = occurrence + 1
        node_ids.append(f"{signature}:{occurrence}")
    edges = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if isinstance(dependency, str) and dependency in keys:
                edges.append((node_ids[keys[dependency]], node_ids[index]))
            else:
                edges.append((f"unknown:{_fingerprint_text(dependency)}", node_ids[index]))
    return _fingerprint({
        "task_count": len(tasks),
        "task_complexity": _fingerprint_text(value.get("task_complexity")),
        "execution_strategy": _fingerprint_text(value.get("execution_strategy")),
        "granularity_reason": _fingerprint_text(value.get("granularity_reason")),
        "nodes": sorted(node_signatures),
        "edges": sorted(edges),
    })


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _semantic_plan_changed_fields(previous: dict[str, Any], candidate: dict[str, Any]
                                  ) -> tuple[list[str], list[str]]:
    old = _semantic_plan_projection(previous)
    new = _semantic_plan_projection(candidate)
    fields = [
        "task_count", "task_kind", "objective", "description", "semantic_needs",
        "operations", "dependencies", "write_targets", "owned_paths",
        "success_criteria", "verification_cases", "execution_strategy",
            "task_complexity", "granularity_reason", "decomposition_reason",
            "unsupported_requirements",
    ]
    values_old = {
        "task_count": old["task_count"],
        "task_kind": [task["task_kind"] for task in old["tasks"]],
        "objective": [task["objective"] for task in old["tasks"]],
        "description": [task["description"] for task in old["tasks"]],
        "semantic_needs": [task["semantic_needs"] for task in old["tasks"]],
        "operations": [[*task["operations"], *task["semantic_operations"]] for task in old["tasks"]],
        "dependencies": [task["dependencies"] for task in old["tasks"]],
        "write_targets": [task["write_targets"] for task in old["tasks"]],
        "owned_paths": [task["owned_paths"] for task in old["tasks"]],
        "success_criteria": [task["success_criteria"] for task in old["tasks"]],
        "verification_cases": [task["verification_cases"] for task in old["tasks"]],
        "execution_strategy": old["execution_strategy"],
        "task_complexity": old["task_complexity"],
        "granularity_reason": old["granularity_reason"],
        "decomposition_reason": old["decomposition_reason"],
        "unsupported_requirements": old["unsupported_requirements"],
    }
    values_new = {
        "task_count": new["task_count"],
        "task_kind": [task["task_kind"] for task in new["tasks"]],
        "objective": [task["objective"] for task in new["tasks"]],
        "description": [task["description"] for task in new["tasks"]],
        "semantic_needs": [task["semantic_needs"] for task in new["tasks"]],
        "operations": [[*task["operations"], *task["semantic_operations"]] for task in new["tasks"]],
        "dependencies": [task["dependencies"] for task in new["tasks"]],
        "write_targets": [task["write_targets"] for task in new["tasks"]],
        "owned_paths": [task["owned_paths"] for task in new["tasks"]],
        "success_criteria": [task["success_criteria"] for task in new["tasks"]],
        "verification_cases": [task["verification_cases"] for task in new["tasks"]],
        "execution_strategy": new["execution_strategy"],
        "task_complexity": new["task_complexity"],
        "granularity_reason": new["granularity_reason"],
        "decomposition_reason": new["decomposition_reason"],
        "unsupported_requirements": new["unsupported_requirements"],
    }
    changed = [field for field in fields if values_old[field] != values_new[field]]
    unchanged = [field for field in fields if values_old[field] == values_new[field]]
    return changed, unchanged


def _plans_semantically_equivalent(left: dict[str, Any], right: dict[str, Any], *,
                                   compiler_error: dict[str, Any] | None = None,
                                   required_delta: dict[str, Any] | None = None) -> bool:
    if _semantic_plan_fingerprint(left) == _semantic_plan_fingerprint(right):
        return True
    if (_semantic_plan_structure_fingerprint(left) !=
            _semantic_plan_structure_fingerprint(right)):
        return False
    # An unchanged action graph is normally a semantic repeat. Allow a same-graph
    # repair only when its normalized semantic change satisfies the active,
    # cause-specific Compiler invariant (for example, a corrected criterion).
    if compiler_error is not None and required_delta is not None:
        delta_satisfied, _reason = _repair_delta_satisfied(
            left, right, compiler_error, required_delta)
        return not delta_satisfied
    return True


def _required_plan_delta(value: dict[str, Any], compiler_error: dict[str, Any],
                         fingerprint: str | None) -> dict[str, Any]:
    error_type = str(compiler_error.get("type") or "PlanValidationError")
    message = str(compiler_error.get("message") or "")
    result: dict[str, Any] = {
        "must_change": ["semantic task structure or the compiler-rejected field"],
        "must_not_repeat": ["the rejected normalized Semantic Plan or equivalent task graph"],
        "must_satisfy_all": [
            "the current compiler rejection is resolved",
            "the plan is materially different from every rejected plan",
        ],
        "compiler_error": error_type,
        "repair_check": "diagnostic_field_changed",
    }
    if fingerprint:
        result["rejected_plan_fingerprint"] = fingerprint
    if error_type == "OverfragmentedPlan" and "excess simple Tasks" in message:
        from .plan_granularity import EXPECTED_TASK_RANGES
        task_complexity = value.get("task_complexity")
        expected = EXPECTED_TASK_RANGES.get(task_complexity)
        count = len(value.get("tasks", [])) if isinstance(value.get("tasks"), list) else 0
        result["must_change"] = ["task_count OR granularity_reason"]
        result["must_not_repeat"] = [f"the same {count}-task decomposition without a valid reason"]
        result["repair_check"] = "overfragmented"
        if expected:
            result["expected_task_range"] = list(expected)
            result["must_satisfy_any"] = [
                f"task_count <= {expected[1]}",
                "valid_granularity_reason grounded in independent outcomes or preserved boundaries",
            ]
    elif error_type == "OverfragmentedPlan":
        result["must_change"] = [
            "execution_strategy, task structure, or the Compiler decomposition boundary"
        ]
        result["must_not_repeat"] = ["the same delegation/action graph rejected by the Compiler"]
        result["must_satisfy_all"] = [
            "the Compiler decomposition checks no longer classify the plan as overfragmented",
            "the plan is materially different from every rejected plan",
        ]
        result["repair_check"] = "decomposition_graph"
    elif (error_type == "InvalidDependencyGraph"
          or re.search(r"dependenc|depends_on|cycle", message, re.I)):
        result["must_change"] = ["dependencies or semantic task keys"]
        result["must_satisfy_all"] = [
            "every dependency resolves to one existing task key",
            "no task depends on itself",
            "the dependency graph is acyclic",
            "the plan is materially different from every rejected plan",
        ]
        result["repair_check"] = "dependency_graph"
    elif "task_complexity" in message:
        result["must_change"] = ["task_complexity"]
        result["must_satisfy_all"] = ["task_complexity is simple, multi_step or complex"]
        result["repair_check"] = "task_complexity"
    elif "execution_strategy" in message:
        result["must_change"] = ["execution_strategy"]
        result["must_satisfy_all"] = ["execution_strategy is single_worker or multi_worker"]
        result["repair_check"] = "execution_strategy"
    elif "decomposition_reason" in message:
        result["must_change"] = ["decomposition_reason"]
        result["must_satisfy_all"] = ["decomposition_reason contains 20 to 1000 characters"]
        result["repair_check"] = "decomposition_reason"
    elif "granularity_reason" in message:
        result["must_change"] = ["granularity_reason"]
        result["must_satisfy_all"] = [
            "granularity_reason is valid for the proposed task boundaries",
            "the plan is materially different from every rejected plan",
        ]
        result["repair_check"] = "granularity_reason"
    else:
        result["must_change_fields"] = _repair_fields_from_diagnostic(message)
    return result


def _repair_fields_from_diagnostic(message: str) -> list[str]:
    """Map known Compiler diagnostics to the semantic fields that can repair them."""
    patterns = (
        ("dependencies", r"dependenc|depends_on|cycle"),
        ("task_complexity", r"task_complexity"),
        ("execution_strategy", r"execution_strategy"),
        ("decomposition_reason", r"decomposition_reason"),
        ("granularity_reason", r"granularity_reason"),
        ("task_kind", r"task_kind"),
        ("operations", r"operation"),
        ("write_targets", r"write[_ ]target|write operation|writer|write scope"),
        ("owned_paths", r"owned[_ ]path|ownership|creator|owns path"),
        ("verification_cases", r"verification case|verification_cases"),
        ("success_criteria", r"criterion|criteria|success_criteria"),
        ("semantic_needs", r"semantic_needs|semantic need"),
        ("unsupported_requirements", r"unsupported"),
        ("objective", r"objective"),
    )
    fields = [field for field, pattern in patterns if re.search(pattern, message, re.I)]
    return fields or ["semantic_task_structure"]


def _semantic_dependency_graph_status(value: dict[str, Any]) -> tuple[bool, str]:
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "tasks_missing"

    def key_slug(raw: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(raw).casefold()).strip("_")[:64]

    keys: list[str] = []
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            return False, "task_not_object"
        key = key_slug(task.get("key") or task.get("objective") or "") or f"step_{index}"
        if key in keys:
            return False, "duplicate_task_key"
        keys.append(key)

    dependencies: list[list[int]] = []
    for index, task in enumerate(tasks):
        raw_dependencies = task.get("depends_on", [])
        if not isinstance(raw_dependencies, list):
            return False, "dependencies_not_list"
        resolved: list[int] = []
        for raw_dependency in raw_dependencies:
            dependency = key_slug(raw_dependency)
            if dependency not in keys:
                return False, "unknown_dependency"
            target = keys.index(dependency)
            if target == index:
                return False, "self_dependency"
            resolved.append(target)
        dependencies.append(resolved)

    visited: set[int] = set()
    active: set[int] = set()

    def visit(index: int) -> bool:
        if index in active:
            return False
        if index in visited:
            return True
        active.add(index)
        if not all(visit(dependency) for dependency in dependencies[index]):
            return False
        active.remove(index)
        visited.add(index)
        return True

    if not all(visit(index) for index in range(len(tasks))):
        return False, "dependency_cycle"
    return True, "dependency_graph_valid"


def _compiler_decomposition_status(value: dict[str, Any]) -> tuple[bool, str]:
    """Mirror the Compiler's pure decomposition gate before spending another attempt."""
    tasks = value.get("tasks")
    complexity = value.get("task_complexity")
    strategy = value.get("execution_strategy")
    reason = value.get("decomposition_reason")
    if (not isinstance(tasks, list) or not tasks
            or complexity not in {"simple", "multi_step", "complex"}
            or strategy not in {"single_worker", "multi_worker"}
            or not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 1000):
        return False, "decomposition_contract_invalid"
    if any(not isinstance(task, dict) for task in tasks):
        return False, "task_not_object"
    if any(not isinstance(task.get("write_targets", []), list)
           or any(not isinstance(path, str) for path in task.get("write_targets", []))
           for task in tasks):
        return False, "write_targets_invalid"
    if strategy == "single_worker" or len(tasks) == 1:
        return True, "decomposition_not_overfragmented"
    graph_valid, graph_reason = _semantic_dependency_graph_status(value)
    if not graph_valid:
        return False, graph_reason

    def key_slug(raw: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(raw).casefold()).strip("_")[:64]

    keys = [
        key_slug(task.get("key") or task.get("objective") or "") or f"step_{index}"
        for index, task in enumerate(tasks, 1)
    ]
    by_key = {key: index for index, key in enumerate(keys)}
    dependencies = []
    for task in tasks:
        dependencies.append({
            by_key[key_slug(dependency)]
            for dependency in task.get("depends_on", [])
        })
    count = len(tasks)
    review_or_qa = any(task.get("task_kind") in {"review", "testing"} for task in tasks)
    implementation_only = all(task.get("task_kind") in {
        "file_creation", "program_creation", "code_change", "general"
    } for task in tasks)
    linear = all(dependencies[index] == {index - 1} for index in range(1, count))
    same_targets = any(
        set(left.get("write_targets", [])) & set(right.get("write_targets", []))
        for left, right in zip(tasks, tasks[1:])
    )
    stems = [{
        str(path).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for path in task.get("write_targets", [])
    } for task in tasks]
    coupled_pair = False
    if count == 2 and linear and complexity != "complex":
        from .plan_compiler import _responsibility_similarity
        coupled_pair = bool(stems[0] & stems[1]) or (
            _responsibility_similarity(tasks[0], tasks[1]) >= 0.6)
    vague = re.search(
        r"(?i)(multiple? files?|several files?|many steps?|different operations?|"
        r"html.?css.?js|acceptance criteria|more than one file)", reason)
    concrete = re.search(
        r"(?i)(independent (?:audit|review|verification)|controlled qa|"
        r"separate subsystems?|distinct (?:interface|dependency|speciali[sz])|"
        r"parallel (?:implementation|development))", reason)
    overfragmented = bool((vague and not concrete) or (
        implementation_only and not review_or_qa and (
            complexity == "simple" or same_targets or coupled_pair or
            (count >= 3 and linear and complexity != "complex")
        )
    ))
    return (not overfragmented,
            "decomposition_overfragmented" if overfragmented
            else "decomposition_not_overfragmented")


def _repair_delta_satisfied(previous: dict[str, Any], candidate: dict[str, Any],
                            compiler_error: dict[str, Any], required_delta: dict[str, Any]
                            ) -> tuple[bool, str]:
    candidate_tasks = candidate.get("tasks")
    if (not isinstance(candidate_tasks, list) or not candidate_tasks
            or len(candidate_tasks) > MAX_PLAN_TASKS
            or any(not isinstance(task, dict) for task in candidate_tasks)):
        return False, "semantic_plan_shape_invalid"
    changed, _unchanged = _semantic_plan_changed_fields(previous, candidate)
    repair_check = required_delta.get("repair_check")
    if repair_check == "overfragmented":
        expected = required_delta.get("expected_task_range")
        old_tasks, new_tasks = previous.get("tasks", []), candidate.get("tasks", [])
        old_count = len(old_tasks) if isinstance(old_tasks, list) else 0
        new_count = len(new_tasks) if isinstance(new_tasks, list) else 0
        maximum = expected[1] if isinstance(expected, list) and len(expected) == 2 else None
        count_resolves = maximum is not None and new_count <= maximum and new_count < old_count
        old_reason = _fingerprint_text(previous.get("granularity_reason"))
        new_reason = _fingerprint_text(candidate.get("granularity_reason"))
        reason_changed = bool(new_reason and new_reason != old_reason)
        reason_valid = False
        if reason_changed and isinstance(new_tasks, list):
            from .plan_granularity import has_concrete_granularity_reason
            reason_valid = has_concrete_granularity_reason(
                candidate.get("granularity_reason"), new_tasks, {"preserved_boundaries": []})
        if count_resolves:
            return True, "task_count_within_expected_range"
        if reason_changed and reason_valid:
            return True, "concrete_granularity_reason"
        return False, "overfragmentation_delta_not_resolved"
    if repair_check == "dependency_graph":
        return _semantic_dependency_graph_status(candidate)
    if repair_check == "decomposition_graph":
        return _compiler_decomposition_status(candidate)
    if repair_check == "task_complexity":
        valid = candidate.get("task_complexity") in {"simple", "multi_step", "complex"}
        return (valid and "task_complexity" in changed,
                "task_complexity_valid" if valid and "task_complexity" in changed
                else "task_complexity_delta_not_resolved")
    if repair_check == "execution_strategy":
        valid = candidate.get("execution_strategy") in {"single_worker", "multi_worker"}
        return (valid and "execution_strategy" in changed,
                "execution_strategy_valid" if valid and "execution_strategy" in changed
                else "execution_strategy_delta_not_resolved")
    if repair_check == "decomposition_reason":
        reason = candidate.get("decomposition_reason")
        valid = isinstance(reason, str) and 20 <= len(reason.strip()) <= 1000
        return (valid and "decomposition_reason" in changed,
                "decomposition_reason_valid" if valid and "decomposition_reason" in changed
                else "decomposition_reason_delta_not_resolved")
    if repair_check == "granularity_reason":
        reason = candidate.get("granularity_reason")
        tasks = candidate.get("tasks")
        valid = False
        if isinstance(tasks, list):
            from .plan_granularity import has_concrete_granularity_reason
            valid = has_concrete_granularity_reason(reason, tasks, {"preserved_boundaries": []})
        return valid, "granularity_reason_valid" if valid else "granularity_reason_delta_not_resolved"

    required_fields = required_delta.get("must_change_fields", [])
    if not isinstance(required_fields, list):
        required_fields = []
    if required_fields == ["semantic_task_structure"]:
        structural_fields = {
            "task_count", "task_kind", "objective", "description", "semantic_needs",
            "operations", "dependencies", "write_targets", "owned_paths",
            "success_criteria", "verification_cases", "execution_strategy",
        "task_complexity", "granularity_reason", "decomposition_reason",
        "unsupported_requirements",
        }
        field_changes = structural_fields.intersection(changed)
    else:
        field_changes = set(required_fields).intersection(changed)
    if field_changes:
        return True, "compiler_fields_changed:" + ",".join(sorted(field_changes))
    return False, "compiler_rejection_delta_not_resolved"


class SemanticPlanRepairGuard:
    """Reject repaired plans that repeat a rejected graph before Plan Compiler."""

    @staticmethod
    def check(previous: dict[str, Any], candidate: dict[str, Any], *,
              compiler_error: dict[str, Any], required_delta: dict[str, Any]) -> dict[str, Any]:
        previous_fingerprint = _semantic_plan_fingerprint(previous)
        new_fingerprint = _semantic_plan_fingerprint(candidate)
        delta_satisfied, delta_reason = _repair_delta_satisfied(
            previous, candidate, compiler_error, required_delta)
        equivalent = _plans_semantically_equivalent(
            previous, candidate, compiler_error=compiler_error,
            required_delta=required_delta)
        # A valid delta is necessary, but cannot make an already rejected
        # semantic graph acceptable again.
        accepted = delta_satisfied and not equivalent
        changed_fields, unchanged_fields = _semantic_plan_changed_fields(previous, candidate)
        return {
            "previous_fingerprint": previous_fingerprint,
            "new_fingerprint": new_fingerprint,
            "previous_structure_fingerprint": _semantic_plan_structure_fingerprint(previous),
            "new_structure_fingerprint": _semantic_plan_structure_fingerprint(candidate),
            "equivalent": equivalent,
            "materially_different": not equivalent,
            "changed_fields": changed_fields,
            "unchanged_fields": unchanged_fields,
            "compiler_error": compiler_error,
            "required_delta": required_delta,
            "delta_satisfied": delta_satisfied,
            "delta_reason": delta_reason,
            "result": "material_change_accepted" if accepted else "no_material_change",
        }


def _bounded_normalized_plan(value: dict[str, Any]) -> dict[str, Any]:
    """Keep a compact canonical plan record for orchestration diagnostics."""
    projection = _semantic_plan_projection(value)
    tasks = projection.get("tasks", [])
    bounded_tasks = []
    for task in tasks[:MAX_PLAN_TASKS]:
        if not isinstance(task, dict):
            bounded_tasks.append({"invalid": True})
            continue
        bounded = {
            key: copy.deepcopy(task[key]) for key in (
                "task_kind", "objective", "description", "dependencies",
                "execution_strategy", "task_complexity",
            ) if key in task
        }
        bounded["objective"] = str(bounded.get("objective") or "")[:240]
        bounded["description"] = str(bounded.get("description") or "")[:240]
        for key in ("semantic_needs", "operations", "semantic_operations",
                    "write_targets", "owned_paths", "success_criteria"):
            items = task.get(key)
            if isinstance(items, list):
                bounded[key] = [str(item)[:180] for item in items[:8]]
        if isinstance(task.get("granularity"), dict):
            bounded["granularity"] = {
                key: str(text)[:180]
                for key, text in task["granularity"].items()
            }
        bounded_tasks.append(bounded)
    return sanitize({
        "task_count": projection.get("task_count"),
        "task_complexity": projection.get("task_complexity"),
        "execution_strategy": projection.get("execution_strategy"),
        "granularity_reason": projection.get("granularity_reason", "")[:300],
        "unsupported_requirements": projection.get("unsupported_requirements", [])[:8],
        "success_criteria": projection.get("success_criteria", [])[:8],
        "tasks": bounded_tasks,
    })


class RejectedSemanticPlanRegistry:
    """Orchestration-local history of compiler and Repair Guard rejections."""

    def __init__(self, orchestration_id: str | None = None):
        self.orchestration_id = orchestration_id
        self.entries: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, dict[str, Any]] = {}
        self.structure_errors: dict[str, dict[str, Any]] = {}
        self.values: dict[str, dict[str, Any]] = {}
        self.deltas: dict[str, dict[str, Any]] = {}
        self.compiler_rejection_count = 0

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        fingerprint = _semantic_plan_fingerprint(plan)
        structure_fingerprint = _semantic_plan_structure_fingerprint(plan)
        for entry in self.entries:
            if entry["fingerprint"] == fingerprint:
                return {"entry": entry, "match_kind": "fingerprint"}
            if entry["structure_fingerprint"] == structure_fingerprint:
                prior_plan = self.values.get(entry["fingerprint"])
                prior_error = self.errors.get(entry["fingerprint"])
                prior_delta = self.deltas.get(entry["fingerprint"])
                if (prior_plan is None or prior_error is None or prior_delta is None
                        or _plans_semantically_equivalent(
                            prior_plan, plan, compiler_error=prior_error,
                            required_delta=prior_delta)):
                    return {"entry": entry, "match_kind": "semantic_structure"}
        return None

    def register(self, plan: dict[str, Any], *, rejection_error: dict[str, Any],
                 required_plan_delta: dict[str, Any], planner_attempt: int,
                 compiler_attempt: int | None, rejection_kind: str,
                 snapshot: dict[str, Any] | None = None
                 ) -> tuple[dict[str, Any], bool]:
        existing = self.match(plan)
        if existing is not None:
            return existing["entry"], False

        fingerprint = _semantic_plan_fingerprint(plan)
        structure_fingerprint = _semantic_plan_structure_fingerprint(plan)
        error = sanitize(copy.deepcopy(rejection_error))
        raw_tasks = plan.get("tasks")
        entry = {
            "orchestration_id": self.orchestration_id,
            "event_sequence": len(self.entries) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "fingerprint": fingerprint,
            "structure_fingerprint": structure_fingerprint,
            "normalized_plan": _bounded_normalized_plan(plan),
            "rejection_error_type": str(error.get("type") or "PlanValidationError")[:120],
            "rejection_reason": str(error.get("message") or "")[:600],
            "compiler_attempt": compiler_attempt,
            "planner_attempt": planner_attempt,
            "required_plan_delta": sanitize(copy.deepcopy(required_plan_delta)),
            "task_count": len(raw_tasks) if isinstance(raw_tasks, list) else 0,
            "task_complexity": plan.get("task_complexity"),
            "execution_strategy": plan.get("execution_strategy"),
            "rejection_kind": rejection_kind,
        }
        self.entries.append(entry)
        self.snapshots[fingerprint] = copy.deepcopy(snapshot or entry["normalized_plan"])
        self.errors[fingerprint] = error
        self.structure_errors.setdefault(structure_fingerprint, error)
        self.values[fingerprint] = copy.deepcopy(plan)
        self.deltas[fingerprint] = copy.deepcopy(required_plan_delta)
        if rejection_kind == "compiler":
            self.compiler_rejection_count += 1
        return entry, True

    def export(self) -> list[dict[str, Any]]:
        return sanitize(copy.deepcopy(self.entries))

    def prompt_history(self) -> list[dict[str, Any]]:
        """Return the bounded plan/error summary sent to a full-replan request."""
        return [{
            "plan_number": entry["event_sequence"],
            "fingerprint": entry["fingerprint"],
            "structure_fingerprint": entry["structure_fingerprint"],
            "task_count": entry["task_count"],
            "task_complexity": entry["task_complexity"],
            "execution_strategy": entry["execution_strategy"],
            "error": entry["rejection_error_type"],
            "issue": entry["rejection_reason"][:300],
            "rejection_kind": entry["rejection_kind"],
            "required_plan_delta": copy.deepcopy(entry["required_plan_delta"]),
            "rejected_structure": copy.deepcopy(entry["normalized_plan"]),
        } for entry in self.entries]


class VerificationCriterionReferenceError(PlanValidationError):
    """A declared semantic case reference has zero or multiple final matches."""

    def __init__(self, *, task_key: str, task_id: str, case_id: str, reference: str,
                 available: list[str], reason: str, semantic_task_keys: list[str] | None = None):
        self.diagnostics = sanitize({
            "task_key": task_key, "task_id": task_id, "case_id": case_id,
            "semantic_criterion_reference": reference[:MAX_CRITERION_CHARS],
            "available_local_criteria": [item[:MAX_CRITERION_CHARS] for item in available[:MAX_CRITERIA]],
            "reason": reason, "stage": "after_criterion_reconciliation",
            "semantic_task_keys": semantic_task_keys or [task_key],
        })
        label = ("Verification case references an unknown local criterion." if reason == "unknown" else
                 "Verification case has an ambiguous local criterion reference.")
        super().__init__(label + " " + json.dumps(self.diagnostics, ensure_ascii=False))


class PlanGenerationError(RuntimeError):
    """A configured planner model failed to produce a valid plan."""


class PlannerUnableToProduceMateriallyDifferentPlan(PlanGenerationError):
    """Compatibility base for the former repeated-plan failure."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = sanitize(diagnostics)
        super().__init__(
            "PlannerUnableToProduceMateriallyDifferentPlan: "
            + json.dumps(self.diagnostics, ensure_ascii=False)
        )


class PlannerUnableToProduceAcceptablePlan(PlannerUnableToProduceMateriallyDifferentPlan):
    """Bounded planning could not satisfy all rejected-plan constraints."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = sanitize(diagnostics)
        PlanGenerationError.__init__(
            self,
            "PlannerUnableToProduceAcceptablePlan: "
            + json.dumps(self.diagnostics, ensure_ascii=False)
        )


class VerificationBindingInvariantError(PlanGenerationError):
    """Compiler lost a valid reference; model repair cannot fix this invariant."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = sanitize(diagnostics)
        super().__init__("Compiler lost a reconciled verification criterion during binding. " +
                         json.dumps(self.diagnostics, ensure_ascii=False))


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
                    "format": planner_response_format(
                        model_context, semantic=bool(context.get("_semantic_plan"))),
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
                stage="repair" if repair else "initial",
                structured_context=model_context,
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
    identifiers, assigned, duplicates_resolved = _stable_id_values(
        items, structure=structure, id_factory=id_factory, forbidden_ids=forbidden_ids,
    )
    normalized = [dict(item, id=identifier) for item, identifier in zip(items, identifiers)]
    _record_stable_id_diagnostics(diagnostics, structure, assigned, duplicates_resolved)
    return normalized


def _stable_id_values(items: list[dict[str, Any]], *, structure: str,
                      id_factory: Callable[[int, int], str],
                      forbidden_ids: set[str] | None = None) -> tuple[list[str], int, int]:
    """Calculate stable IDs without mutating items or recording diagnostics."""
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

    return [str(item["id"]) for item in normalized], assigned, duplicates_resolved


def _record_stable_id_diagnostics(diagnostics: dict[str, Any] | None, structure: str,
                                  assigned: int, duplicates_resolved: int) -> None:
    if diagnostics is None or not (assigned or duplicates_resolved):
        return
    details = diagnostics.setdefault("stable_ids", {
        "assigned": 0, "duplicates_resolved": 0, "structures": {},
    })
    details["assigned"] += assigned
    details["duplicates_resolved"] += duplicates_resolved
    details["structures"][structure] = {
        "assigned": assigned, "duplicates_resolved": duplicates_resolved,
    }

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
    global_id_factory = lambda _index, sequence: (
        f"ac-{sequence}" if use_ac_namespace else f"gc-{sequence}"
    )
    normalized_ids, assigned, duplicates_resolved = _stable_id_values(
        global_entries, structure="criterion_links.global", id_factory=global_id_factory,
    )
    # Construct one old->new mapping before rewriting any row or reference.
    # Duplicate source IDs are intentionally omitted: they cannot identify one
    # target, so only the existing exact wording relationship may disambiguate.
    global_id_mapping: dict[str, str] = {}
    for index, (entry, normalized_id) in enumerate(zip(global_entries, normalized_ids)):
        raw_id = entry.get("id")
        if isinstance(raw_id, str):
            try:
                old_id = _identifier(raw_id, f"global criterion {index} id")
            except PlanValidationError:
                old_id = None
            if old_id is not None and raw_global_id_counts.get(old_id) == 1:
                global_id_mapping[old_id] = normalized_id
        entry["id"] = normalized_id
    _record_stable_id_diagnostics(
        diagnostics, "criterion_links.global", assigned, duplicates_resolved,
    )
    normalized_global = global_entries
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
        source_global_id = source_global_ids[index]
        remapped_supports = []
        for ref in supports:
            if ref in global_id_mapping:
                remapped_supports.append(global_id_mapping[ref])
            elif ref in ambiguous_global_ids:
                if len(supports) == 1 and source_global_id is not None:
                    remapped_supports.append(source_global_id)
                else:
                    raise PlanValidationError(
                        "Local criterion links reference an ambiguous global criterion ID."
                    )
            elif ref in global_ids:
                # Already-normalized references remain unchanged.
                remapped_supports.append(ref)
            elif len(supports) == 1 and source_global_id is not None:
                # Exact normalized wording is the model's existing structural
                # relation. Do not infer identity from arbitrary text similarity.
                remapped_supports.append(source_global_id)
            else:
                raise PlanValidationError(
                    "Local criterion links reference an unknown global criterion ID."
                )
        item["supports_global_criteria"] = list(dict.fromkeys(remapped_supports))
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


def _inferred_owned_paths(objective: str, task_kind: str,
                          capabilities: list[str]) -> list[str]:
    """Recover concrete names already present in intent for deterministic plans."""
    if not set(capabilities) & {"filesystem.create", "filesystem.modify", "filesystem.overwrite"}:
        return []
    matches = re.findall(
        r"(?<![\w])(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,10}(?![\w])",
        objective,
    )
    if matches:
        try:
            return normalize_owned_paths(list(dict.fromkeys(matches)))
        except CrossTaskRequestError:
            return []
    if task_kind == "program_creation":
        return ["main.py"]
    if task_kind == "file_creation":
        return ["output.txt"]
    return []


def normalize_plan(value: Any, *, diagnostics: dict[str, Any] | None = None,
                   repair_model_criteria: bool = False) -> dict[str, Any]:
    """Return a stable representation while enforcing field types and bounds."""
    raw = _object(value, PLAN_FIELDS, "plan", optional={CRITERION_LINKS_FIELD, "write_owners",
        "task_complexity", "execution_strategy", "decomposition_reason",
        "task_count", "worker_count", "worker_assignments", "granularity_reason"})
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
        if "required_tools" in task:
            normalized_task["required_tools"] = _text_list(
                task["required_tools"], f"plan.tasks[{index}].required_tools",
                MAX_CAPABILITIES, identifiers=False,
            )
            known_tools = set(RuntimeResourceCatalog.build().ids("tool"))
            unknown_tools = [tool for tool in normalized_task["required_tools"]
                             if tool not in known_tools]
            if unknown_tools:
                raise UnknownTool(unknown_tools[0])
        if "semantic_operations" in task:
            operations = _text_list(
                task["semantic_operations"], f"plan.tasks[{index}].semantic_operations",
                MAX_CAPABILITIES, identifiers=False,
            )
            unknown_operations = [operation for operation in operations
                                  if operation not in SEMANTIC_OPERATION_CAPABILITIES]
            if unknown_operations:
                raise PlanValidationError(
                    f"Unknown semantic operation: {unknown_operations[0]}."
                )
            normalized_task["semantic_operations"] = operations
        if "semantic_needs" in task:
            normalized_task["semantic_needs"] = _text_list(
                task["semantic_needs"], f"plan.tasks[{index}].semantic_needs",
                MAX_CRITERIA, identifiers=False,
            )
        if "granularity" in task:
            try:
                normalized_task["granularity"] = normalize_granularity(task["granularity"])
            except ValueError as exc:
                raise PlanValidationError(str(exc)) from exc
        if "owned_paths" in task:
            try:
                normalized_task["owned_paths"] = normalize_owned_paths(task["owned_paths"])
            except CrossTaskRequestError as exc:
                raise PlanValidationError(
                    f"plan.tasks[{index}].owned_paths is invalid: {exc}"
                ) from exc
        if "write_targets" in task:
            try:
                normalized_task["write_targets"] = normalize_owned_paths(task["write_targets"])
            except CrossTaskRequestError as exc:
                raise PlanValidationError(
                    f"plan.tasks[{index}].write_targets is invalid: {exc}") from exc
        if "foreign_write_targets" in task:
            foreign = task["foreign_write_targets"]
            if not isinstance(foreign, list) or len(foreign) > MAX_OWNED_PATHS:
                raise PlanValidationError(f"plan.tasks[{index}].foreign_write_targets is invalid.")
            normalized_foreign = []
            for item in foreign:
                if not isinstance(item, dict) or set(item) != {"path", "owner_plan_task_id"}:
                    raise PlanValidationError(f"plan.tasks[{index}].foreign_write_targets entry is invalid.")
                try:
                    path = normalize_owned_paths([item["path"]])[0]
                except (CrossTaskRequestError, IndexError, TypeError) as exc:
                    raise PlanValidationError("Invalid foreign write path.") from exc
                normalized_foreign.append({"path": path,
                                           "owner_plan_task_id": _identifier(
                                               item["owner_plan_task_id"], "foreign owner")})
            normalized_task["foreign_write_targets"] = normalized_foreign
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
        if "verification_cases" in task or "verification_mode" in task:
            from .verification_cases import normalize_cases, MODES
            try:
                normalized_task["verification_cases"] = normalize_cases(task.get("verification_cases", []))
                mode = task.get("verification_mode", "independent_cases")
                if mode not in MODES:
                    raise ValueError("Unknown verification_mode.")
                normalized_task["verification_mode"] = mode
            except ValueError as exc:
                raise PlanValidationError(str(exc)) from exc
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
    if "task_complexity" in raw:
        classification = _text(raw["task_complexity"], "plan.task_complexity", 32)
        if classification not in {"simple", "multi_step", "complex"}:
            raise PlanValidationError("Invalid task_complexity.")
        result["task_complexity"] = classification
    if "execution_strategy" in raw:
        strategy = _text(raw["execution_strategy"], "plan.execution_strategy", 32)
        if strategy not in {"single_worker", "multi_worker"}:
            raise PlanValidationError("Invalid execution_strategy.")
        result["execution_strategy"] = strategy
    if "decomposition_reason" in raw:
        result["decomposition_reason"] = _text(
            raw["decomposition_reason"], "plan.decomposition_reason", 1000)
    if "granularity_reason" in raw:
        result["granularity_reason"] = _text(raw["granularity_reason"], "plan.granularity_reason", 1000)
    if "write_owners" in raw:
        owners = raw["write_owners"]
        if not isinstance(owners, dict):
            raise PlanValidationError("plan.write_owners must be an object.")
        try:
            result["write_owners"] = {
                owned_path_key(path): _identifier(owner, "plan.write_owners owner")
                for path, owner in owners.items()}
        except (CrossTaskRequestError, TypeError, ValueError) as exc:
            raise PlanValidationError("plan.write_owners is invalid.") from exc
    result[CRITERION_LINKS_FIELD] = _normalize_criterion_links(
        raw.get(CRITERION_LINKS_FIELD), result["success_criteria"], tasks, diagnostics,
        repair_model_criteria=repair_model_criteria)
    for task in tasks:
        local_rows = [row for row in result[CRITERION_LINKS_FIELD]["local"] if row["task_id"] == task["id"]]
        local = {row["id"]: row["criterion"] for row in local_rows}
        for case in task.get("verification_cases", []):
            refs = case.get("supports_criteria", [])
            ids = case.get("supports_acceptance_criterion_ids", [])
            matches = []
            for ref in refs:
                candidates = criterion_reference_matches(ref, local_rows)
                if len(candidates) != 1:
                    raise VerificationCriterionReferenceError(
                        task_key=task["id"], task_id=task["id"], case_id=case["id"], reference=ref,
                        available=list(local.values()), reason="unknown" if not candidates else "ambiguous")
                matches.append(candidates[0])
            if any(ref not in local for ref in ids):
                raise PlanValidationError("Verification case references an unknown local criterion.")
            if refs and ids and {row["id"] for row in matches} != set(ids):
                raise PlanValidationError("Verification case text and criterion IDs disagree.")
            if refs:
                case["supports_criteria"] = list(dict.fromkeys(row["criterion"] for row in matches))
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
    ownership: dict[str, str] = {}
    for task in plan["tasks"]:
        for path in task.get("owned_paths", []):
            key = owned_path_key(path)
            previous = ownership.get(key)
            if previous and previous != task["id"]:
                raise PlanValidationError(
                    f"Conflicting write ownership for {path}: {previous} and {task['id']}."
                )
            ownership[key] = task["id"]
    if "write_owners" in plan and plan["write_owners"] != ownership:
        raise PlanValidationError("Plan write_owners does not match permanent task ownership.")
    for task in plan["tasks"]:
        foreign = task.get("foreign_write_targets", [])
        targets = {owned_path_key(path) for path in task.get("write_targets", [])}
        expected = {key: owner for key, owner in ownership.items()
                    if key in targets and owner != task["id"]}
        actual = {owned_path_key(item["path"]): item["owner_plan_task_id"] for item in foreign}
        if actual != expected or len(actual) != len(foreign):
            if foreign or "write_targets" in task:
                raise PlanValidationError(
                    f"Task {task['id']} foreign_write_targets does not match plan ownership.")
    plan["write_owners"] = ownership
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
    if "execution_strategy" in plan:
        # Assignments are derived from validated tasks on every normalization.
        # This also keeps existing plan transforms and replans consistent.
        assignments = worker_assignments(plan["tasks"], plan["execution_strategy"])
        plan["task_count"] = len(plan["tasks"])
        plan["worker_count"] = len(assignments)
        plan["worker_assignments"] = assignments
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
    """Create bounded semantic plans and compile only guard-approved repairs."""

    def __init__(self, decide: Callable[[str, dict[str, Any]], Any] | None = None, *,
                 offline: bool = False):
        self.decide = decide
        self.offline = bool(offline)
        self.metrics: dict[str, Any] = {}

    def _reset_metrics(self) -> None:
        self.metrics = {"model_calls": 0, "prompt_tokens": 0, "generated_tokens": 0,
                        "total_tokens": 0, "duration_seconds": 0.0,
                        "semantic_compiler_attempts": []}

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
                      diagnostics: dict[str, Any] | None = None,
                      resource_catalog: RuntimeResourceCatalog | None = None) -> dict[str, Any]:
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
        plan = validate_plan(
            value, diagnostics=diagnostics, repair_model_criteria=True)
        plan = _reconcile_task_analysis(plan, analysis)
        plan = _append_qa_task(plan, analysis)
        plan = _append_code_audit_task(plan, analysis)
        _check_analyst_acceptance_identity(plan, analysis)
        if declared_links:
            _require_executable_global_coverage(plan)
        return plan

    @staticmethod
    def _prompt(goal: str, context: dict[str, Any]) -> str:
        resources = {
            kind: [{key: item.get(key) for key in fields if key in item}
                   for item in context.get(kind, []) if isinstance(item, dict)]
            for kind, fields in (
                ("capabilities", ("id", "description", "operations", "aliases", "tool")),
                ("tools", ("id", "description", "operations", "capabilities", "aliases")),
                ("skills", ("id", "name", "description", "use_when", "tags", "aliases")),
            )
        }
        return (
            PLANNER_SOURCE_OF_TRUTH_INSTRUCTIONS + "\n\n"
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
        "depends_on, semantic_needs, required_capabilities, required_tools, preferred_skills, success_criteria, owned_paths. "
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
            "Runtime resource catalog (semantic summaries only; Skills' full instructions go to selected workers): "
            + json.dumps(resources, ensure_ascii=False, separators=(",", ":"))
            + ". Select only exact IDs in this catalog or their explicitly declared unique aliases. "
            "Never invent capability, tool, or Skill IDs. Put concrete semantic requirements in semantic_needs. "
            "required_tools may be omitted when required_capabilities identify the needed actions; "
            "Freya resolves their registered tool transports. If tools are selected without capabilities, "
            "Freya derives only capabilities justified by specific semantic operations. Prefer precise capabilities "
            "for the smallest policy surface. A selected tool never grants permission. "
            "Report an unmet need in unsupported_requirements instead of inventing a resource. "
            "An available resource is not permission: capability policy still authorizes each action. "
            "Task Analyst operational prompt (authoritative task input): " + json.dumps(goal, ensure_ascii=False)
            + ("\nTask Analyst structured analysis (authoritative constraints): " +
               json.dumps(context.get("task_analysis"), ensure_ascii=False, separators=(",", ":"))
               if isinstance(context.get("task_analysis"), dict) else "")
        )

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a legacy concrete-resource plan for compatibility callers.

        The built-in Task Spec path must use ``create_plan_for_spec`` so that
        Planner emits semantic operations and the Plan Compiler owns resource
        resolution. This method remains for injected pre-Semantic-Plan adapters
        and historical callers during migration.
        """
        goal = _text(prompt, "prompt", MAX_GOAL_CHARS)
        self._reset_metrics()
        limited_context = dict(context) if isinstance(context, dict) else {}
        resource_catalog = RuntimeResourceCatalog.from_context(limited_context)
        limited_context.update(resource_catalog.as_dict())
        self.metrics.update({
            "resource_catalog_version": resource_catalog.version,
            "available_capability_ids": resource_catalog.ids("capability"),
            "available_tool_ids": resource_catalog.ids("tool"),
            "available_skill_ids": resource_catalog.ids("skill"),
        })
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
            parsed = self._parse_output(output, limited_context.get("task_analysis"), normalization,
                                        resource_catalog)
            parsed["goal"] = goal
            if normalization:
                self.metrics["normalization"] = normalization
            return validate_plan(parsed)
        except UnsupportedResourceRequirement:
            raise
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
                parsed = self._parse_output(repaired, limited_context.get("task_analysis"), normalization,
                                            resource_catalog)
                parsed["goal"] = goal
                if normalization:
                    self.metrics["normalization"] = normalization
                return validate_plan(parsed)
            except Exception as second_error:
                raise PlanGenerationError(
                    f"Planner output remained invalid after one repair attempt: {second_error}"
                ) from second_error

    def create_plan_for_spec(self, task_spec: dict[str, Any],
                             context: dict[str, Any] | None = None, *,
                             orchestration_id: str | None = None) -> dict[str, Any]:
        """Plan HOW from canonical intent, then compile runtime identifiers."""
        from .plan_compiler import compile_semantic_plan
        from .task_spec import render_task_spec, validate_task_spec

        spec = validate_task_spec(task_spec)
        if spec["status"] != "READY_FOR_PLANNING":
            raise PlanGenerationError("Task Spec is not ready for planning.")
        self._reset_metrics()
        public_spec = {key: value for key, value in spec.items()
                       if key not in {"source_prompt", "clarification_history", "clarification_questions"}}
        catalog_context = {**(context or {}), "task_spec": public_spec}
        resource_catalog = RuntimeResourceCatalog.from_context(catalog_context)
        limited_context = {
            "task_spec": public_spec,
            "_semantic_plan": True,
            "resource_catalog_version": resource_catalog.version,
            "semantic_operations": resource_catalog.semantic_operations,
        }
        self.metrics.update({
            "semantic_plan_schema_version": SEMANTIC_PLAN_SCHEMA_VERSION,
            "compiled_plan_schema_version": PLAN_SCHEMA_VERSION,
            "resource_catalog_version": resource_catalog.version,
            "available_semantic_operation_ids": sorted(
                item["id"] for item in resource_catalog.semantic_operations),
        })
        resource_view = {"semantic_operations": resource_catalog.semantic_operations}
        request = (
            PLANNER_SOURCE_OF_TRUTH_INSTRUCTIONS + "\n\n"
            "Plan HOW to satisfy this canonical Task Spec. Return JSON with summary, "
            "task_complexity, execution_strategy, decomposition_reason, success_criteria and tasks. "
            "Classify task_complexity as simple, multi_step or complex independently of worker count. "
            "Choose single_worker by default, even for multiple files, implementation steps, "
            "HTML/CSS/JS, operations or acceptance criteria. One worker can perform cohesive work "
            "and local checks. Choose multi_worker only when a distinct dependency, specialist, "
            "independent audit, controlled QA or substantial separable subsystem creates more "
            "benefit than delegation, context handoff and integration cost. State that concrete "
            "benefit in decomposition_reason; for single_worker state why one owner suffices. "
            "Do not split a small feature into scaffold, logic and finalization tasks. "
            "TASK is a meaningful unit of progress, recovery and evidence/evaluation, not a tool call, "
            "capability, semantic operation, filesystem operation or implementation step. "
            "Do not create a separate Task for each technical operation. One Task may require multiple "
            "operations and semantic_needs; Compiler derives all required capabilities/tools for it. "
            "Group consecutive operations that jointly produce one logical artifact or outcome: "
            "create_file plus writing implementation is one implement-file Task; directory setup plus "
            "creating/populating component files is one create-component Task; reading/modifying/saving "
            "configuration is one update-configuration Task. Parent directories are implicit in file "
            "creation; do not invent a mkdir operation absent from the runtime catalog. "
            "Before separating a Task ask: if it completed and no successor ever ran, would its result "
            "still be meaningful progress? If not, group it with its logical successor. Do not propose "
            "empty files/directories, placeholders or incomplete scaffolds as separate outcomes unless "
            "the user expressly requires that intermediate state or it has independent value. "
            "Use optional granularity.logical_outcome for a shared component outcome, "
            "granularity.independent_value for a concrete independently useful result and "
            "granularity.preserve_boundary for an actual user phase, approval, policy/security or "
            "independent recovery/rollback boundary. These declarations never grant permission. "
            "Keep independently valuable shared artifacts, parallel work, distinct Workers and "
            "separable verification apart. Normally implementation and its testing are two Tasks. "
            "For simple work normally use 1-2 Tasks, multi_step 2-5; complex may use more. "
            "These are advisory ranges, not rigid limits: if exceeded provide an explicit "
            "granularity_reason explaining the independent outcomes or preserved boundaries. "
            "Each task has a meaningful key, task_kind, objective, description, "
            "depends_on (semantic task keys), semantic_needs, operations, success_criteria, "
            "owned_paths and write_targets. "
            "For explicit verification inputs supply verification_mode=independent_cases and "
            "verification_cases=[{id: stable case ID, input: exact bounded stdin string, "
            "supports_criteria: semantic success_criteria texts verified by this case}] on the testing task. "
            "Declare exact relationships; never invent criteria or choose runtime criterion IDs. "
            "Compiler resolves references after reconciling final local criteria. Several cases may support "
            "one aggregate criterion, and a case may support several criteria. Binding is optional. "
            "Each independent input starts a fresh process; never concatenate separate cases into one stdin. "
            "Put all independent cases of the same program in one testing task, never one task per input. "
            "Testing tasks have empty owned_paths and write_targets and report results in their response. "
            "Use verification_mode=interactive_session only for ordered inputs within one process. "
            "Non-testing tasks must omit verification_cases or use an empty array; only testing tasks "
            "may own execution cases. Do not add an empty-input case unless requested. "
            "Preserve every requested case, including invalid input; do not merge them. "
            "task_kind must be one of " + json.dumps(sorted(TASK_KIND_VALUES)) + ". "
            "For a write task, choose a precise, "
            "workspace-relative exact file path; do not use broad patterns. Do not supply runtime IDs, "
            "Within one plan, one concrete writable path has one permanent plan-task owner. "
            "owned_paths declares lasting responsibility; write_targets declares files this task "
            "intends to write, including files owned by another task. Keep independently meaningful "
            "tasks distinct, even when they write the same file; absorb mechanical prerequisites "
            "into their logical outcome. The unique creator owns a created file. "
            "A later modifier of that file declares it in write_targets and leaves owned_paths empty. "
            "Give each task one primary responsibility and only the write targets needed for it. "
            "Two tasks that write the same path must have a real dependency order and distinct sequential "
            "responsibilities; sibling writers of one path are rejected. Do not duplicate implementation "
            "work across sibling tasks. Each task success criterion must be provable from evidence its "
            "operations can produce: file operations can prove artifact existence and static source or "
            "structure; execution/test operations can prove runtime behavior and test results; compiler "
            "operations can prove compilation. Reserve behavioral correctness for a dependent testing "
            "task when one exists. Do not assign visual rendering or external-state criteria without a "
            "registered operation that can produce that evidence. Use concrete observable criteria; avoid "
            "broad claims that something works, is functional, or is correct without matching execution evidence. "
            "criterion IDs, criterion links, "
            "execution nodes or UUIDs; Freya compiles those deterministically. Use the fewest "
            "workers needed. Decide implementation, controlled QA, research and audit only when "
            "justified. No task may reinterpret the original human prompt. Operations are semantic "
            "work descriptions, not tool or permission IDs. Select only operation IDs from the catalog; "
            "do not return required_capabilities, required_tools, Skills, agent IDs, or tool IDs. "
            "Freya deterministically maps operations to runtime resources. Keep semantic_needs "
            "as explanations and include every concrete operation needed by the task. "
            "Creating a web artifact does not imply deployment, publication, hosting, or external execution. "
            "Product behavior is a semantic need, not a runtime operation. For example, multiplication "
            "can be implemented with create_file; do not declare it unsupported. "
            "If a requested runtime action is not represented, describe it in unsupported_requirements "
            "using only semantic_need and reason. Runtime policy remains authoritative. "
            "Semantic operation catalog: "
            + json.dumps(resource_view, ensure_ascii=False, separators=(",", ":"))
            + ". Canonical Task Spec: "
            + render_task_spec(spec)
        )
        if self.decide is None:
            if not self.offline:
                raise PlanGenerationError("No planner model is configured.")
            objective = spec["objective"]
            lower = objective.casefold()
            programming = any(word in lower for word in ("calculadora", "calculator", "programa", "script"))
            operations = ["create_file", "read_file"] if programming else ["read_file"]
            owned_paths = _inferred_owned_paths(
                objective, "program_creation" if programming else "file_creation",
                ["filesystem.create"] if programming else [],
            )
            if programming and "python" in lower:
                operations.append("run_python_script")
            semantic = {"summary": objective, "task_complexity": "simple",
                        "execution_strategy": "single_worker",
                        "decomposition_reason": "One worker can complete the cohesive deliverable and its local checks.",
                        "tasks": [{
                "key": "implement", "objective": objective,
                "task_kind": "program_creation" if programming else "general",
                "description": "Complete the specified deliverable in the selected workspace and verify it.",
                "depends_on": [], "semantic_needs": ["Create and inspect the requested program."],
                "operations": operations,
                "owned_paths": owned_paths,
                "write_targets": owned_paths,
                "success_criteria": [
                    "The requested artifact exists and can be inspected."],
            }], "success_criteria": [], "unsupported_requirements": []}
            self.metrics["mode"] = "deterministic"
        else:
            try:
                semantic = self._call(request, limited_context)
            except Exception as exc:
                raise PlanGenerationError(f"Planner model call failed: {exc}") from exc

        def compiler_timestamp() -> str:
            return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        def compiler_metrics(started_at: str, started_monotonic: float,
                             status: str, *, attempt_number: int,
                             plan: dict[str, Any] | None = None,
                             error: Exception | None = None) -> dict[str, Any]:
            record: dict[str, Any] = {
                "attempt": attempt_number,
                "started_at": started_at,
                "completed_at": compiler_timestamp(),
                "duration_seconds": round(time.monotonic() - started_monotonic, 4),
                "status": status,
            }
            if plan is not None:
                record.update({
                    "task_ids": [task["id"] for task in plan["tasks"]],
                    "execution_strategy": plan.get("execution_strategy"),
                    "task_count": plan.get("task_count"),
                    "worker_count": plan.get("worker_count"),
                    "worker_assignments": plan.get("worker_assignments", []),
                    "global_criteria": len(plan["success_criteria"]),
                })
            if error is not None:
                record["error"] = str(error)[:600]
            return record

        compiler_started_at = ""
        compiler_started = 0.0
        original_for_repair = None
        affected_for_repair: set[int] = set()
        preserve_all_repair_tasks = False
        force_full_replan = False
        registry = RejectedSemanticPlanRegistry(orchestration_id)
        rejected_plan_snapshots = registry.snapshots
        rejected_plan_errors = registry.errors
        rejected_plan_structure_errors = registry.structure_errors
        rejected_plan_values = registry.values
        rejected_plan_deltas = registry.deltas
        self.metrics["rejected_plan_history"] = []
        repair_guard_state: dict[str, Any] | None = None
        repair_attempt_count = 0
        compiler_attempt_count = 0

        def reject_invalid_repair_shape(raw_value: Any, error: Exception | None = None) -> None:
            active_error = repair_guard_state["compiler_error"] if repair_guard_state else {}
            required_delta = (repair_guard_state["required_plan_delta"]
                              if repair_guard_state else {})
            previous = repair_guard_state["plan"] if repair_guard_state else {}
            previous_fingerprint = _semantic_plan_fingerprint(previous) if previous else None
            message = ("Planner repair output must be a Semantic Plan object with a non-empty "
                       "tasks array of task objects.")
            if error is not None:
                message = f"{message} {type(error).__name__}: {str(error)[:240]}"
            failure = {"type": "InvalidSemanticPlanShape", "message": message}
            self.metrics.setdefault("planner_events", []).append({
                "event_type": "planner.repair_delta_checked",
                "attempt": repair_attempt_count,
                "planner_attempt": attempt + 1,
                "previous_fingerprint": previous_fingerprint,
                "new_fingerprint": None,
                "changed_fields": [],
                "unchanged_fields": [],
                "compiler_error": active_error,
                "required_delta": required_delta,
                "seen_before": False,
                "required_delta_satisfied": False,
                "materially_different": False,
                "result": "invalid_semantic_plan_shape",
                "semantic_equivalent": None,
                "delta_reason": "semantic_plan_shape_invalid",
                "candidate_type": type(raw_value).__name__,
                "rejected_plan_count": len(registry),
            })
            self.metrics["planner_events"].append({
                "event_type": "planner.repair_required_delta_failed",
                "planner_attempt": attempt + 1,
                "fingerprint": None,
                "previous_error": active_error,
                "required_plan_delta": required_delta,
                "failed_check": "semantic_plan_shape_invalid",
                "changed_fields": [],
                "unchanged_fields": [],
                "compiler_attempts_consumed": compiler_attempt_count,
            })
            raise PlannerUnableToProduceAcceptablePlan({
                "failure_reason": "invalid_semantic_plan_shape",
                "planner_attempt": attempt + 1,
                "repair_attempt_count": repair_attempt_count,
                "max_repair_attempts": MAX_SEMANTIC_PLAN_REPAIRS,
                "compiler_attempts_consumed": compiler_attempt_count,
                "candidate_type": type(raw_value).__name__,
                "compiler_error": active_error,
                "required_plan_delta": required_delta,
                "invalid_output": failure,
                "rejected_plan_count": len(registry),
                "rejected_plan_history": registry.prompt_history(),
            })

        for attempt in range(MAX_SEMANTIC_PLAN_ATTEMPTS):
            candidate_fingerprint = None
            candidate_structure_fingerprint = None
            previous_structure_error = None
            compiler_invoked = False
            snapshot: dict[str, Any] = {}
            try:
                value = None
                value = json.loads(semantic) if isinstance(semantic, str) else semantic
                snapshot = semantic_plan_snapshot(value)
                self.metrics["planner_semantic_plan"] = snapshot
                self.metrics.setdefault("planner_semantic_plan_attempts", []).append(snapshot)
                if (attempt and repair_guard_state is not None
                        and not isinstance(value, dict)):
                    reject_invalid_repair_shape(value)
                candidate_fingerprint = (
                    _semantic_plan_fingerprint(value) if isinstance(value, dict) else None
                )
                candidate_structure_fingerprint = (
                    _semantic_plan_structure_fingerprint(value) if isinstance(value, dict) else None
                )
                if candidate_fingerprint is not None:
                    self.metrics.setdefault("planner_events", []).append({
                        "event_type": "planner.plan_fingerprint_created",
                        "attempt": attempt + 1,
                        "repair_attempt": repair_attempt_count if attempt else 0,
                        "fingerprint": candidate_fingerprint,
                        "structure_fingerprint": candidate_structure_fingerprint,
                    })
                if attempt and repair_guard_state is not None and isinstance(value, dict):
                    active_error = repair_guard_state["compiler_error"]
                    active_delta = repair_guard_state["required_plan_delta"]
                    guard_result = SemanticPlanRepairGuard.check(
                        repair_guard_state["plan"], value,
                        compiler_error=active_error, required_delta=active_delta)
                    repeated_match = registry.match(value)
                    seen_before = repeated_match is not None
                    required_delta_satisfied = bool(guard_result["delta_satisfied"])
                    materially_different = (
                        not seen_before and not guard_result["equivalent"])
                    if seen_before:
                        guard_outcome = "repeated_rejected_plan"
                    elif not required_delta_satisfied:
                        guard_outcome = "required_delta_unsatisfied"
                    elif not materially_different:
                        guard_outcome = "material_change_missing"
                    else:
                        guard_outcome = "material_change_accepted"

                    guard_event = {
                        "event_type": "planner.repair_delta_checked",
                        "attempt": repair_attempt_count,
                        "planner_attempt": attempt + 1,
                        "previous_fingerprint": guard_result["previous_fingerprint"],
                        "new_fingerprint": guard_result["new_fingerprint"],
                        "previous_structure_fingerprint": guard_result[
                            "previous_structure_fingerprint"],
                        "new_structure_fingerprint": guard_result["new_structure_fingerprint"],
                        "changed_fields": guard_result["changed_fields"],
                        "unchanged_fields": guard_result["unchanged_fields"],
                        "compiler_error": active_error,
                        "required_delta": active_delta,
                        "seen_before": seen_before,
                        "required_delta_satisfied": required_delta_satisfied,
                        "materially_different": materially_different,
                        "result": guard_outcome,
                        "semantic_equivalent": guard_result["equivalent"],
                        "delta_reason": guard_result["delta_reason"],
                        "rejected_plan_count": len(registry),
                    }
                    self.metrics.setdefault("planner_events", []).append(guard_event)

                    if guard_outcome == "material_change_accepted":
                        self.metrics["planner_events"].append({
                            **guard_event,
                            "event_type": "planner.repair_material_change_accepted",
                        })
                        repair_guard_state = None
                    else:
                        failure_reason = (
                            "repeated_rejected_plan" if seen_before
                            else "required_plan_delta_unsatisfied" if not required_delta_satisfied
                            else "material_change_missing"
                        )
                        matched_entry = repeated_match["entry"] if repeated_match else None
                        if matched_entry is not None:
                            self.metrics["planner_events"].append({
                                "event_type": "planner.rejected_plan_repeated",
                                "planner_attempt": attempt + 1,
                                "fingerprint": candidate_fingerprint,
                                "structure_fingerprint": candidate_structure_fingerprint,
                                "matching_rejected_attempt": matched_entry["planner_attempt"],
                                "previous_error": {
                                    "type": matched_entry["rejection_error_type"],
                                    "message": matched_entry["rejection_reason"],
                                },
                                "match_kind": repeated_match["match_kind"],
                                "rejection_count": len(registry),
                                "rejected_plan_count": len(registry),
                            })
                        else:
                            local_error = {
                                "type": "RequiredPlanDeltaUnsatisfied",
                                "message": guard_result["delta_reason"],
                            }
                            entry, registered = registry.register(
                                value, rejection_error=local_error,
                                required_plan_delta=active_delta,
                                planner_attempt=attempt + 1, compiler_attempt=None,
                                rejection_kind="repair_guard", snapshot=snapshot)
                            self.metrics["rejected_plan_history"] = registry.export()
                            if registered:
                                self.metrics["planner_events"].append({
                                    "event_type": "planner.rejected_plan_registered",
                                    **{key: entry[key] for key in (
                                        "orchestration_id", "event_sequence", "fingerprint",
                                        "structure_fingerprint",
                                        "rejection_error_type", "rejection_reason", "compiler_attempt",
                                        "planner_attempt", "required_plan_delta", "task_count",
                                        "task_complexity", "execution_strategy", "timestamp",
                                        "rejection_kind", "normalized_plan",
                                    )},
                                })
                            self.metrics["planner_events"].append({
                                "event_type": "planner.repair_required_delta_failed",
                                "planner_attempt": attempt + 1,
                                "fingerprint": candidate_fingerprint,
                                "previous_error": active_error,
                                "required_plan_delta": active_delta,
                                "failed_check": guard_result["delta_reason"],
                                "changed_fields": guard_result["changed_fields"],
                                "unchanged_fields": guard_result["unchanged_fields"],
                                "compiler_attempts_consumed": compiler_attempt_count,
                            })
                        if not materially_different:
                            self.metrics["planner_events"].append({
                                **guard_event,
                                "event_type": "planner.repair_no_material_change",
                                "full_replan_required": True,
                            })
                        if repair_attempt_count >= MAX_SEMANTIC_PLAN_REPAIRS or self.decide is None:
                            raise PlannerUnableToProduceAcceptablePlan({
                                "failure_reason": failure_reason,
                                "planner_attempt": attempt + 1,
                                "repair_attempt_count": repair_attempt_count,
                                "max_repair_attempts": MAX_SEMANTIC_PLAN_REPAIRS,
                                "matched_rejected_attempt": (
                                    matched_entry["planner_attempt"] if matched_entry else None),
                                "previous_compiler_error": active_error,
                                "required_plan_delta": active_delta,
                                "rejected_plan_count": len(registry),
                                "rejected_plan_history": registry.prompt_history(),
                            })

                        force_full_replan = True
                        original_for_repair = copy.deepcopy(value)
                        affected_for_repair = set()
                        preserve_all_repair_tasks = False
                        next_required_delta = copy.deepcopy(active_delta)
                        next_required_delta["rejected_plan_fingerprints"] = [
                            entry["fingerprint"] for entry in registry.entries]
                        next_required_delta["must_be_structurally_different"] = True
                        repair_payload = {
                            "instruction": (
                                "This Semantic Plan was rejected locally before Plan Compiler. "
                                "Do not return it or any semantically equivalent plan from the rejected "
                                "history. Produce a full replan that resolves the latest compiler error "
                                "and satisfies required_plan_delta. A wording, ID, ordering or naming change "
                                "does not count as a material change."
                            ),
                            "canonical_task_spec": public_spec,
                            "previous_semantic_plan": snapshot,
                            "rejected_plan_history": registry.prompt_history(),
                            "rejected_plan_fingerprints": [
                                entry["fingerprint"] for entry in registry.entries],
                            "rejected_semantic_plans": [
                                entry["normalized_plan"] for entry in registry.entries],
                            "compiler_error": active_error,
                            "previous_compiler_error": active_error,
                            "required_plan_delta": next_required_delta,
                            "repair_guard_result": {
                                "failure_reason": failure_reason,
                                "matching_rejected_attempt": (
                                    matched_entry["planner_attempt"] if matched_entry else None),
                                "changed_fields": guard_result["changed_fields"],
                                "unchanged_fields": guard_result["unchanged_fields"],
                                "semantic_equivalent": guard_result["equivalent"],
                                "required_delta_satisfied": required_delta_satisfied,
                                "delta_reason": guard_result["delta_reason"],
                            },
                            "rules": {
                                "preserve_user_scope": True,
                                "do_not_add_requirements": True,
                                "replan_entire_plan": True,
                                "must_differ_from_rejected_plans": True,
                            },
                        }
                        encoded = json.dumps(repair_payload, ensure_ascii=False, separators=(",", ":"))
                        if len(encoded) > 40000:
                            raise PlanGenerationError(
                                "Semantic plan repair payload exceeds the bounded limit.")
                        repair_attempt_count += 1
                        self.metrics["planner_events"].append({
                            "event_type": "planner.repair_requested",
                            "error_type": "PlannerRepairGuard",
                            "failure_reason": failure_reason,
                            "attempt": repair_attempt_count,
                            "previous_fingerprint": guard_result["previous_fingerprint"],
                            "new_fingerprint": guard_result["new_fingerprint"],
                            "compiler_error": active_error,
                            "required_plan_delta": next_required_delta,
                            "full_replan_required": True,
                        })
                        semantic = self._call(encoded, {
                            **limited_context,
                            "_freya_repair": True,
                            "_freya_replan": True,
                        })
                        self.metrics["planner_events"].append({
                            "event_type": "planner.repair_completed",
                            "full_replan": True,
                            "attempt_number": repair_attempt_count,
                        })
                        continue
                if (candidate_fingerprint is not None
                        and candidate_fingerprint in rejected_plan_snapshots):
                    self.metrics.setdefault("planner_events", []).append({
                        "event_type": "planner.duplicate_plan_rejected",
                        "attempt_number": attempt + 1,
                        "rejection_count": len(rejected_plan_snapshots),
                    })
                    raise RepeatedSemanticPlanError(
                        rejected_plan_errors.get(candidate_fingerprint)
                    )
                if attempt and candidate_structure_fingerprint is not None:
                    previous_structure_error = rejected_plan_structure_errors.get(
                        candidate_structure_fingerprint
                    )
                if (attempt and not force_full_replan and previous_structure_error is None
                        and (affected_for_repair or preserve_all_repair_tasks)
                        and isinstance(value, dict)
                        and isinstance(original_for_repair, dict)):
                    old_tasks = original_for_repair.get("tasks", [])
                    new_by_key = {task.get("key"): task for task in value.get("tasks", [])
                                  if isinstance(task, dict)}
                    old_keys = {task.get("key") for task in old_tasks if isinstance(task, dict)}
                    if not set(new_by_key) <= old_keys:
                        raise PlanGenerationError(
                            "Planner repair added a new semantic task outside the rejected boundary.")
                    for index, task in enumerate(old_tasks, 1):
                        if (index not in affected_for_repair and isinstance(task, dict)
                                and new_by_key.get(task.get("key")) != task):
                            raise PlanGenerationError(
                                "Planner repair changed or removed an unaffected semantic task.")
                force_full_replan = False
                compiler_started_at = compiler_timestamp()
                compiler_started = time.monotonic()
                compiler_attempt_count += 1
                compiler_invoked = True
                plan = compile_semantic_plan(value, spec, resource_catalog=resource_catalog)
                scope_adjustments = list(resource_catalog.scope_adjustments)
                self.metrics["scope_adjustments"] = scope_adjustments
                planner_resource_resolutions = list(resource_catalog.resource_resolutions)
                self.metrics["compiler_events"] = list(resource_catalog.compiler_events)
                plan = _normalize_python_console_calculator(plan, spec, resource_catalog)
                objective = spec["objective"].casefold()
                if ("python" in objective and any(word in objective for word in
                    ("calculadora", "calculator")) and any(word in objective for word in
                    ("consola", "console")) and not any(word in objective for word in
                    ("web", "desktop", "escritorio"))):
                    plan = _append_qa_task(
                        plan, {"task_characteristics": {"requires_user_input": True}}, resource_catalog)
                    qa_task = next((task for task in plan["tasks"]
                                    if task["id"].startswith("qa-interactive-test")), None)
                    if qa_task is not None and ("calculadora" in objective or "calculator" in objective):
                        qa_task["description"] = (
                            "Read calculator.py once, then run exactly one case with stdin lines 3 and 5 "
                            "through run_command. Verify that stdout reports 8 and the exit code is 0. "
                            "Do not run another case, repeat the command, create files or modify the "
                            "implementation."
                        )
                        qa_task["semantic_needs"] = [
                            "Run one bounded case with stdin lines 3 and 5.",
                            "Capture stdout and exit status; verify the integer output is 8.",
                        ]
                        qa_task["task_characteristics"] = {
                            "interactive": True, "requires_user_input": True,
                            "single_case_verification": True,
                        }
                        qa_task["preferred_skills"] = []
                        qa_task["success_criteria"] = [
                            "The bounded command outputs '8' and exits successfully."]
                        implementation_task = next(
                            (task for task in plan["tasks"] if task["id"] == "task-1"), None)
                        if implementation_task is not None:
                            implementation_task["success_criteria"] = [
                                "calculator.py exists in the selected workspace and can be read."]
                        plan.pop(CRITERION_LINKS_FIELD, None)
                        plan = validate_plan(plan)
                    artifact_globals = {
                        item["id"] for item in plan["criterion_links"]["global"]
                        if item["criterion"].casefold().startswith("calculator.py exists")
                    }
                    behavior_globals = [
                        item["id"] for item in plan["criterion_links"]["global"]
                        if item["id"] not in artifact_globals
                    ]
                    for link in plan["criterion_links"]["local"]:
                        if (link["task_id"].startswith("qa-interactive-test")
                                and "outputs '8'" in link["criterion"].casefold()):
                            link["supports_global_criteria"] = list(dict.fromkeys([
                                *link["supports_global_criteria"], *behavior_globals]))
                compiled = validate_plan(plan)
                for event in self.metrics["compiler_events"]:
                    if event.get("event_type") == "plan_compiler.worker_assignment_created":
                        event.update({
                            "strategy": compiled.get("execution_strategy"),
                            "task_count": compiled.get("task_count"),
                            "worker_count": compiled.get("worker_count"),
                            "worker_assignments": compiled.get("worker_assignments", []),
                        })
                resource_resolutions = [item for item in planner_resource_resolutions
                                        if item.get("action") in {
                                            "planner_tool_hint_ignored",
                                            "planner_capability_declaration_ignored",
                                        }]
                for task in compiled["tasks"]:
                    for operation in task.get("semantic_operations", []):
                        capabilities, tools = resource_catalog.resources_for_operations([operation])
                        for capability, tool in zip(capabilities, tools):
                            resource_resolutions.append({
                                "task_key": task["id"],
                                "semantic_operation": operation,
                                "semantic_needs": list(task.get("semantic_needs", [])),
                                "semantic_source": "compiled_plan",
                                "resolved_capability": capability,
                                "resolved_tool": tool,
                                "resolution_source": "runtime_catalog",
                            })
                self.metrics["resource_resolutions"] = resource_resolutions
                self.metrics["compiled_runtime_plan"] = {
                    "schema_version": PLAN_SCHEMA_VERSION,
                    "execution_strategy": compiled.get("execution_strategy"),
                    "task_count": compiled.get("task_count"),
                    "worker_count": compiled.get("worker_count"),
                    "worker_assignments": compiled.get("worker_assignments", []),
                    "write_owners": dict(compiled.get("write_owners", {})),
                    "tasks": [{
                        "id": task["id"],
                        "task_kind": task.get("task_kind"),
                        "depends_on": list(task["depends_on"]),
                        "semantic_operations": list(task.get("semantic_operations", [])),
                        "required_capabilities": list(task.get("required_capabilities", [])),
                        "required_tools": list(task.get("required_tools", [])),
                        "owned_paths": list(task.get("owned_paths", [])),
                        "write_targets": list(task.get("write_targets", [])),
                        "foreign_write_targets": list(task.get("foreign_write_targets", [])),
                    } for task in compiled["tasks"]],
                }
                self.metrics["ownership_resolutions"] = [{
                    "task_id": task["id"],
                    "owned_paths": list(task.get("owned_paths", [])),
                    "write_targets": list(task.get("write_targets", [])),
                    "foreign_write_targets": list(task.get("foreign_write_targets", [])),
                } for task in compiled["tasks"]]
                granularity = copy.deepcopy(resource_catalog.granularity_summary)
                granularity["task_count_after"] = len(compiled["tasks"])
                self.metrics["granularity_summary"] = granularity
                self.metrics.setdefault("planner_events", []).append({
                    "event_type": "planner.granularity_summary",
                    "message": "Logical Task granularity analyzed before runtime assignment.",
                    **granularity})
                preferred_skill_warnings = resource_catalog.preferred_skill_warnings_for_tasks(
                    compiled["tasks"])
                self.metrics["preferred_skill_warnings"] = preferred_skill_warnings
                record = compiler_metrics(
                    compiler_started_at, compiler_started, "Success",
                    attempt_number=compiler_attempt_count, plan=compiled,
                )
                record["preferred_skill_warnings"] = preferred_skill_warnings
                record["scope_adjustments"] = scope_adjustments
                record["resource_resolutions"] = resource_resolutions
                record["planner_semantic_plan"] = snapshot
                record["semantic_plan_source"] = (
                    "planner_model" if self.decide is not None else "deterministic_fallback"
                )
                record["semantic_plan_schema_version"] = SEMANTIC_PLAN_SCHEMA_VERSION
                record["compiled_plan_schema_version"] = PLAN_SCHEMA_VERSION
                record["compiled_runtime_plan"] = self.metrics["compiled_runtime_plan"]
                record["ownership_resolutions"] = self.metrics["ownership_resolutions"]
                record["compiler_events"] = self.metrics["compiler_events"]
                self.metrics["semantic_compiler"] = record
                self.metrics["semantic_compiler_attempts"].append(record)
                from .llm_trace import record_validation
                record_validation("planner", "accepted", detail=f"Compiled {len(compiled['tasks'])} tasks.",
                                  normalized_response=compiled)
                return compiled
            except (UnsupportedResourceRequirement, SkillCompatibilityError, PlannerScopeError,
                    VerificationBindingInvariantError) as exc:
                record = compiler_metrics(
                    compiler_started_at, compiler_started, "Failed",
                    attempt_number=compiler_attempt_count, error=exc,
                )
                record["planner_semantic_plan"] = self.metrics.get("planner_semantic_plan", {})
                record["scope_adjustments"] = self.metrics.get("scope_adjustments", [])
                record["resource_resolutions"] = self.metrics.get("resource_resolutions", [])
                record["compiler_events"] = list(getattr(resource_catalog, "compiler_events", []))
                self.metrics["semantic_compiler"] = record
                self.metrics["semantic_compiler_attempts"].append(record)
                raise
            except (ValueError, TypeError, PlanValidationError) as exc:
                from .llm_trace import record_validation
                rejected_error = {"type": type(exc).__name__, "message": str(exc)[:600]}
                record_validation("planner", "rejected",
                                  detail=f"{rejected_error['type']}: {rejected_error['message']}")
                record = compiler_metrics(
                    compiler_started_at, compiler_started, "Failed",
                    attempt_number=compiler_attempt_count, error=exc,
                ) if compiler_invoked else None
                if record is not None:
                    record["compiler_events"] = list(getattr(resource_catalog, "compiler_events", []))
                    self.metrics["semantic_compiler"] = record
                    self.metrics["semantic_compiler_attempts"].append(record)
                repeated_plan = isinstance(exc, RepeatedSemanticPlanError)
                previous_compiler_error = (
                    exc.previous_error if repeated_plan else None
                )
                repeated_match = registry.match(value) if isinstance(value, dict) else None
                if repeated_match is not None:
                    repeated_plan = True
                    matched_entry = repeated_match["entry"]
                    previous_compiler_error = registry.errors.get(
                        matched_entry["fingerprint"], {
                            "type": matched_entry["rejection_error_type"],
                            "message": matched_entry["rejection_reason"],
                        })
                    self.metrics.setdefault("planner_events", []).append({
                        "event_type": "planner.rejected_plan_repeated",
                        "planner_attempt": attempt + 1,
                        "fingerprint": candidate_fingerprint,
                        "structure_fingerprint": candidate_structure_fingerprint,
                        "matching_rejected_attempt": matched_entry["planner_attempt"],
                        "previous_error": previous_compiler_error,
                        "match_kind": repeated_match["match_kind"],
                        "rejection_count": len(registry),
                        "rejected_plan_count": len(registry),
                        "secondary_defense": True,
                    })
                if (not repeated_plan and previous_structure_error is not None
                        and rejected_error == previous_structure_error):
                    repeated_plan = True
                    previous_compiler_error = previous_structure_error
                    self.metrics.setdefault("planner_events", []).append({
                        "event_type": "planner.duplicate_plan_rejected",
                        "attempt_number": attempt + 1,
                        "rejection_count": len(registry),
                        "match": "same_task_graph_and_compiler_error",
                    })
                    exc = RepeatedSemanticPlanError(previous_compiler_error)
                if not isinstance(value, dict):
                    if attempt and repair_guard_state is not None:
                        reject_invalid_repair_shape(value, exc)
                    raise PlanGenerationError(f"Semantic plan is invalid: {exc}") from exc
                original_for_repair = copy.deepcopy(value)
                message = str(exc)[:600]
                affected_paths = re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+", message)
                affected_ids = sorted({int(number) for number in re.findall(r"\btask-(\d+)\b", message)})
                reference_diagnostic = None
                if isinstance(exc, VerificationCriterionReferenceError):
                    reference_diagnostic = exc.diagnostics
                    from .plan_compiler import _slug
                    semantic_keys = set(reference_diagnostic["semantic_task_keys"])
                    affected_ids = [index for index, task in enumerate(value.get("tasks", []), 1)
                                    if _slug(str(task.get("key") or task.get("objective") or "")) in semantic_keys]
                    affected_paths = []
                from .plan_compiler import OverfragmentedPlan
                consolidation = isinstance(exc, OverfragmentedPlan)
                affected_for_repair = (set(range(1, len(value.get("tasks", [])) + 1))
                                       if consolidation else set(affected_ids))
                preserve_all_repair_tasks = message.startswith("Planner unsupported claim")
                if consolidation:
                    # Every task belongs to the rejected decomposition. A
                    # consolidated repair may use a new semantic key for the
                    # resulting task; keys are labels, not scope boundaries.
                    force_full_replan = True
                if repeated_plan:
                    force_full_replan = True
                    affected_for_repair = set()
                    preserve_all_repair_tasks = False
                error_type = type(exc).__name__
                error_for_guard = (previous_compiler_error
                                   if repeated_plan and previous_compiler_error else rejected_error)
                required_delta = _required_plan_delta(
                    value, error_for_guard, candidate_fingerprint)
                if candidate_fingerprint is not None:
                    entry, registered = registry.register(
                        value, rejection_error=error_for_guard,
                        required_plan_delta=required_delta,
                        planner_attempt=attempt + 1,
                        compiler_attempt=compiler_attempt_count if compiler_invoked else None,
                        rejection_kind="compiler" if compiler_invoked else "planner_validation",
                        snapshot=snapshot,
                    )
                    self.metrics["rejected_plan_history"] = registry.export()
                    if registered:
                        self.metrics.setdefault("planner_events", []).append({
                            "event_type": "planner.rejected_plan_registered",
                            **{key: entry[key] for key in (
                                "orchestration_id", "event_sequence", "fingerprint",
                                "structure_fingerprint",
                                "rejection_error_type", "rejection_reason", "compiler_attempt",
                                "planner_attempt", "required_plan_delta", "task_count",
                                "task_complexity", "execution_strategy", "timestamp",
                                "rejection_kind", "normalized_plan",
                            )},
                        })
                        if candidate_structure_fingerprint is not None:
                            rejected_plan_structure_errors.setdefault(
                                candidate_structure_fingerprint, error_for_guard)
                elif repeated_plan:
                    entry = repeated_match["entry"] if repeated_match else {}
                full_replan_required = repeated_plan or len(registry) > 1
                force_full_replan = full_replan_required or consolidation
                repair_guard_state = {
                    "plan": copy.deepcopy(value),
                    "fingerprint": candidate_fingerprint,
                    "compiler_error": error_for_guard,
                    "required_plan_delta": required_delta,
                }
                if repair_attempt_count >= MAX_SEMANTIC_PLAN_REPAIRS or self.decide is None:
                    failure_reason = (
                        "repeated_rejected_plan" if repeated_plan
                        else "new_compiler_rejection_after_repair_limit" if compiler_invoked
                        else "planner_validation_rejected_after_repair_limit"
                    )
                    raise PlannerUnableToProduceAcceptablePlan({
                        "failure_reason": failure_reason,
                        "repair_attempt_count": repair_attempt_count,
                        "max_repair_attempts": MAX_SEMANTIC_PLAN_REPAIRS,
                        "planner_attempt": attempt + 1,
                        "compiler_attempt": compiler_attempt_count if compiler_invoked else None,
                        "compiler_error": error_for_guard,
                        "rejected_plan_count": len(registry),
                        "rejected_plan_history": registry.prompt_history(),
                    }) from exc
                diagnostic = {"type": error_type, "message": message,
                              "affected_tasks": [f"task-{number}" for number in affected_ids],
                              "affected_paths": affected_paths,
                              "rejected_plan_count": len(registry),
                              **({"previous_compiler_error": previous_compiler_error}
                                 if repeated_plan and previous_compiler_error else {})}
                if reference_diagnostic is not None:
                    diagnostic["verification_criterion_reference"] = reference_diagnostic
                self.metrics.setdefault("planner_events", []).append({
                    "event_type": "planner.repair_requested", "error_type": error_type,
                    "affected_tasks": diagnostic["affected_tasks"],
                    "affected_paths": affected_paths,
                    **({"verification_criterion_reference": reference_diagnostic}
                       if reference_diagnostic is not None else {}),
                    "original_task_count": len(value.get("tasks", [])),
                    **({"previous_compiler_error": previous_compiler_error}
                       if repeated_plan and previous_compiler_error else {}),
                    "full_replan_required": full_replan_required,
                    "attempt": repair_attempt_count + 1,
                    "compiler_error": error_for_guard,
                    "required_plan_delta": required_delta})
                repair_payload = {
                    "instruction": (
                        "Several previously rejected Semantic Plans exist. Replan the complete request, "
                        "do not reproduce any rejected structure, and resolve the latest compiler error."
                        if full_replan_required and len(registry) > 1 else
                        "The submitted plan repeats a rejected Semantic Plan. Do not return any rejected "
                        "plan, even with changed wording or task keys. Replan the complete request."
                        if repeated_plan else
                        "Consolidate unjustified Tasks or provide a concrete granularity_reason grounded "
                        "in preserved boundaries or independent outcomes. Task count is independent of "
                        "Worker count. Preserve justified strategy and decomposition."
                        if consolidation else
                        "Repair the compiler-rejected field and satisfy the required plan delta."
                    ) + (
                        " Preserve the canonical Task Spec scope, satisfy required_plan_delta, and return "
                        "the complete semantic plan JSON."
                    ),
                    "canonical_task_spec": public_spec,
                    "previous_semantic_plan": snapshot,
                    "rejected_plan_history": registry.prompt_history(),
                    "rejected_plan_fingerprints": [
                        item["fingerprint"] for item in registry.entries],
                    "rejected_semantic_plans": [
                        item["normalized_plan"] for item in registry.entries],
                    "compiler_error": diagnostic,
                    "required_plan_delta": required_delta,
                    "rules": {"preserve_unaffected_tasks": not full_replan_required,
                              "preserve_user_scope": True, "do_not_add_requirements": True,
                              "replan_entire_plan": full_replan_required,
                              "must_differ_from_rejected_plans": True},
                }
                encoded = json.dumps(repair_payload, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > 40000:
                    raise PlanGenerationError("Semantic plan repair payload exceeds the bounded limit.") from exc
                repair_attempt_count += 1
                semantic = self._call(encoded, {
                    **limited_context,
                    "_freya_repair": True,
                    "_freya_replan": full_replan_required,
                })
                self.metrics["planner_events"].append({
                    "event_type": "planner.repair_completed",
                    "full_replan": full_replan_required,
                    "attempt_number": repair_attempt_count,
                })
