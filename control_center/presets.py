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
        "max_steps": 40, "max_seconds": 900, "max_tokens": 48000,
        "max_model_calls": 40, "max_tool_calls": 100, "retries": 1,
    },
}


def programmer_agent_payload(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a normalized generic Programmer config with safe override merging."""
    payload = copy.deepcopy(PROGRAMMER_PRESET)
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
