"""Safe, generic agent presets exposed by the local Control Center API."""
from __future__ import annotations

import copy
from typing import Any

from .agent_context import DEFAULT_AUTONOMY, DEFAULT_BEHAVIOR, DEFAULT_IDENTITY, DEFAULT_OUTPUT, DEFAULT_VERIFICATION
from .capabilities import CAPABILITIES, effective_tools_for_policy
from .config import normalize_agent


def _programmer_policy() -> dict[str, Any]:
    categories: dict[str, dict[str, dict[str, str]]] = {}
    for capability in CAPABILITIES:
        category, action = capability.id.split(".", 1)
        mode = "ask" if capability.id == "filesystem.overwrite" or category == "execution" else "allow"
        categories.setdefault(category, {})[action] = {"mode": mode}
    return {"capabilities": categories}


def _restricted_policy(allowed: set[str]) -> dict[str, Any]:
    categories: dict[str, dict[str, dict[str, str]]] = {}
    for capability in CAPABILITIES:
        category, action = capability.id.split(".", 1)
        categories.setdefault(category, {})[action] = {
            "mode": "allow" if capability.id in allowed else "deny",
        }
    return {"capabilities": categories}


PROGRAMMER_PRESET: dict[str, Any] = {
    "name": "Programmer",
    "description": "Generic software engineering agent for implementation, debugging and verification.",
    "role": "Software Engineer",
    "instructions": "Inspect the repository before modifying it. Keep changes minimal, preserve security boundaries, and report evidence.",
    "skills": [
        {"id": "python-development", "priority": 100},
        {"id": "software-testing", "priority": 90},
        {"id": "debugging", "priority": 80},
        {"id": "git-inspection", "priority": 70},
    ],
    "config": {
        "permissions": "execute",
        "capability_policy": _programmer_policy(),
        "autonomy": {**copy.deepcopy(DEFAULT_AUTONOMY), "destructive_actions": "ask",
                     "request_missing_capabilities": "ask", "request_new_capabilities": "ask"},
        "behavior": copy.deepcopy(DEFAULT_BEHAVIOR),
        "identity": copy.deepcopy(DEFAULT_IDENTITY),
        "verification": copy.deepcopy(DEFAULT_VERIFICATION),
        "output": copy.deepcopy(DEFAULT_OUTPUT),
        "max_steps": 40, "max_seconds": 900, "max_tokens": 0,
        "max_model_calls": 40, "max_tool_calls": 100, "retries": 1,
    },
}

TASK_ANALYST_PRESET: dict[str, Any] = {
    "name": "Task Analyst",
    "description": "Prompt-engineering specialist that rewrites human requests into operational briefs.",
    "role": "Task Analyst Planner",
    "instructions": (
        "Preserve every explicit requirement. Produce a precise self-contained operational prompt, "
        "identify assumptions, interactive behavior, risks, acceptance criteria and a validation strategy."
    ),
    "skills": [],
    "config": {
        "permissions": "read_only", "orchestration_role": "task_analyst",
        "capability_policy": _restricted_policy(set()),
        "autonomy": copy.deepcopy(DEFAULT_AUTONOMY), "behavior": copy.deepcopy(DEFAULT_BEHAVIOR),
        "identity": copy.deepcopy(DEFAULT_IDENTITY), "verification": copy.deepcopy(DEFAULT_VERIFICATION),
        "output": copy.deepcopy(DEFAULT_OUTPUT), "max_steps": 1, "max_seconds": 120,
        "max_tokens": 0, "max_model_calls": 1, "max_tool_calls": 0, "retries": 0,
    },
}

QA_TESTER_PRESET: dict[str, Any] = {
    "name": "QA Tester",
    "description": "Independent behavioral tester for automated and interactive programs.",
    "role": "Quality Assurance Tester",
    "instructions": (
        "Validate observable behavior without modifying implementation files. For interactive Python "
        "programs, pass bounded newline-delimited stdin to run_command and report the exact evidence."
    ),
    "skills": [
        {"id": "interactive-testing", "priority": 120},
        {"id": "software-testing", "priority": 100},
        {"id": "debugging", "priority": 80},
    ],
    "config": {
        "permissions": "execute", "orchestration_role": "qa",
        "capability_policy": _restricted_policy({
            "filesystem.list", "filesystem.read", "filesystem.search",
            "execution.python_script", "execution.pytest", "execution.unittest",
            "execution.py_compile", "execution.ruff",
        }),
        "autonomy": {**copy.deepcopy(DEFAULT_AUTONOMY), "destructive_actions": "deny",
                     "request_missing_capabilities": "deny", "request_new_capabilities": "deny"},
        "behavior": copy.deepcopy(DEFAULT_BEHAVIOR), "identity": copy.deepcopy(DEFAULT_IDENTITY),
        "verification": copy.deepcopy(DEFAULT_VERIFICATION), "output": copy.deepcopy(DEFAULT_OUTPUT),
        "max_steps": 24, "max_seconds": 300, "max_tokens": 0,
        "max_model_calls": 24, "max_tool_calls": 40, "retries": 0,
    },
}

CODE_AUDITOR_PRESET: dict[str, Any] = {
    "name": "Code Auditor",
    "description": "Independent read-only reviewer for correctness, requirements and security boundaries.",
    "role": "Senior Code Auditor",
    "instructions": "Audit the resulting implementation and evidence. Do not modify files or claim unobserved behavior.",
    "skills": [{"id": "code-review", "priority": 120}],
    "config": {
        "permissions": "read_only", "orchestration_role": "auditor",
        "capability_policy": _restricted_policy({
            "filesystem.list", "filesystem.read", "filesystem.search", "git.diff",
        }),
        "autonomy": {**copy.deepcopy(DEFAULT_AUTONOMY), "destructive_actions": "deny",
                     "request_missing_capabilities": "deny", "request_new_capabilities": "deny"},
        "behavior": copy.deepcopy(DEFAULT_BEHAVIOR), "identity": copy.deepcopy(DEFAULT_IDENTITY),
        "verification": copy.deepcopy(DEFAULT_VERIFICATION), "output": copy.deepcopy(DEFAULT_OUTPUT),
        "max_steps": 24, "max_seconds": 300, "max_tokens": 0,
        "max_model_calls": 24, "max_tool_calls": 40, "retries": 0,
    },
}

AGENT_PRESETS: dict[str, dict[str, Any]] = {
    "programmer": PROGRAMMER_PRESET,
    "task-analyst": TASK_ANALYST_PRESET,
    "qa-tester": QA_TESTER_PRESET,
    "code-auditor": CODE_AUDITOR_PRESET,
}


def agent_preset_payload(preset_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one normalized built-in pipeline agent without granting extra policy."""
    if preset_id not in AGENT_PRESETS:
        raise ValueError("Unknown agent preset.")
    payload = copy.deepcopy(AGENT_PRESETS[preset_id])
    if overrides:
        if not isinstance(overrides, dict):
            raise ValueError("Preset overrides must be an object.")
        for key, value in overrides.items():
            if key == "config" and isinstance(value, dict):
                payload["config"].update(copy.deepcopy(value))
            else:
                payload[key] = copy.deepcopy(value)
    normalized = normalize_agent(payload)
    normalized["tools"] = effective_tools_for_policy(normalized["config"]["capability_policy"])
    return normalized


def programmer_agent_payload(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a normalized generic Programmer config with safe override merging."""
    return agent_preset_payload("programmer", overrides)
