"""Validated configuration shared by the API, tool registry and workers."""

from __future__ import annotations

import copy
import ipaddress
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from .capabilities import effective_tools_for_policy
from .policy import policy_from_legacy, validate_policy
from .agent_context import (DEFAULT_AUTONOMY, DEFAULT_BEHAVIOR, DEFAULT_IDENTITY,
                             DEFAULT_OUTPUT, DEFAULT_VERIFICATION, normalize_autonomy,
                             normalize_behavior, normalize_identity, normalize_output,
                             normalize_verification)
from .skills import normalize_skill_assignments

TOOL_CATALOG = [
    {"name": name, "description": description, "available": available, "dangerous": dangerous}
    for name, description, available, dangerous in (
        ("list_files", "List files in the allowed workspace", True, False),
        ("read_file", "Read a text file", True, False),
        ("write_file", "Create or overwrite a file", True, False),
        ("edit_file", "Replace an exact match", True, False),
        ("search_code", "Search for text in the workspace", True, False),
        ("git_diff", "Review Git changes in the workspace", True, False),
        ("run_command", "Run allowed Python scripts, tests, Ruff commands, or Git queries", True, True),
        ("web_search", "Web search · not available yet", False, False),
        ("browser", "Browser · not available yet", False, False),
        ("http_request", "Generic HTTP · not available yet", False, False),
        ("database", "Database queries · not available yet", False, False),
    )
]
DEFAULT_TOOLS = ["list_files", "read_file", "write_file", "edit_file", "search_code"]
DEFAULT_CONFIG = {
    # Optional orchestration role. ``worker`` keeps the normal selector path;
    # ``task_analyst`` marks the agent that may interpret prompts before planning.
    "orchestration_role": "worker",
    "model": "qwen2.5-coder:7b",
    "endpoint": "http://127.0.0.1:11434",
    "temperature": 0.0,
    "context_window": 8192,
    # Zero means unlimited cumulative provider tokens. Runtime safety remains bounded by max_steps, max_model_calls, max_tool_calls and max_seconds.
    "max_tokens": 0,
    "max_steps": 20,
    "max_seconds": 600,
    "max_model_calls": 20,
    "max_tool_calls": 40,
    "retries": 1,
    "system_prompt": "",
    "permissions": "workspace",
    "allowed_directories": ["."],
    "forbidden_commands": [],
    "secret_env": "",
    "workspace_path": "",
    "capability_policy": None,
    "identity": DEFAULT_IDENTITY,
    "behavior": DEFAULT_BEHAVIOR,
    "autonomy": DEFAULT_AUTONOMY,
    "verification": DEFAULT_VERIFICATION,
    # Raw worker callers historically returned text. New normalized agents
    # receive the structured default below; this keeps direct legacy tasks
    # compatible while allowing the new contract by default.
    "output": {"format": "text", "include": DEFAULT_OUTPUT["include"]},
}
LIMITS = {
    "context_window": (512, 131072), "max_tokens": (0, 1000000),
    "max_steps": (1, 100), "max_seconds": (1, 86400),
    "max_model_calls": (1, 100), "max_tool_calls": (0, 1000), "retries": (0, 3),
}


def validate_endpoint(value: str) -> str:
    """MVP inference may contact only local Ollama, never arbitrary remote URLs."""
    if not isinstance(value, str):
        raise ValueError("endpoint must be a local Ollama URL.")
    parsed = urlsplit(value.strip())
    try:
        hostname = parsed.hostname or ""
        local = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = False
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid endpoint port.") from exc
    if (parsed.scheme not in {"http", "https"} or not local or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or port == 0):
        raise ValueError("Use the local Ollama base URL without credentials or a path (http://127.0.0.1:11434).")
    return value.strip().rstrip("/")


def _text(value, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{name}: {'non-empty ' if required else ''}text of up to {maximum} characters.")
    return value.strip()


def normalize_workspace_path(value: str, *, allow_empty: bool = True) -> str:
    """Resolve and validate a selected existing workspace directory."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("workspace_path must be a path of at most 2048 characters.")
    value = value.strip()
    if not value:
        if allow_empty:
            return ""
        raise ValueError("Choose a workspace folder.")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("workspace_path must be an absolute path.")
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("The selected workspace folder does not exist or is inaccessible.") from exc
    if not candidate.is_dir():
        raise ValueError("workspace_path must point to an existing folder.")
    return str(candidate)


def normalize_agent(data: dict, existing: dict | None = None) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object.")
    allowed = {"name", "description", "role", "purpose", "responsibilities", "constraints",
               "instructions", "enabled", "config", "tools", "skills", "capability_policy",
               "identity", "behavior", "autonomy", "verification", "output"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Unknown fields: " + ", ".join(sorted(unknown)))
    baseline = existing or {}
    result = {key: copy.deepcopy(baseline.get(key, default)) for key, default in (
        ("name", ""), ("description", ""), ("role", ""), ("instructions", ""),
        ("enabled", True), ("tools", DEFAULT_TOOLS), ("skills", []),
    )}
    result.update({key: value for key, value in data.items() if key != "config"})
    for name, maximum, required in (("name", 100, True), ("description", 2000, False), ("role", 100, False), ("instructions", 16000, False)):
        result[name] = _text(result[name], name, maximum, required)
    if not isinstance(result["enabled"], bool):
        raise ValueError("enabled must be a boolean.")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(baseline.get("config", {}))
    incoming = data.get("config", {})
    if not isinstance(incoming, dict) or set(incoming) - set(DEFAULT_CONFIG):
        raise ValueError("config contains unknown fields. Refer to secrets with secret_env.")
    config.update(incoming)
    if "output" not in incoming and "output" not in (baseline.get("config") or {}):
        config["output"] = copy.deepcopy(DEFAULT_OUTPUT)
    # Step 2 blocks live in the JSON config. Top-level aliases keep the API
    # convenient while preserving the existing agents table shape.
    for block in ("identity", "behavior", "autonomy", "verification", "output"):
        if block in data:
            config[block] = copy.deepcopy(data[block])
    identity_input = copy.deepcopy(config.get("identity") or {})
    if not isinstance(identity_input, dict):
        raise ValueError("identity must be an object")
    incoming_identity = incoming.get("identity") if isinstance(incoming.get("identity"), dict) else {}
    # Keep the legacy columns and structured identity synchronized while they
    # remain in the storage shape for backwards compatibility.
    if "name" in data:
        identity_input["name"] = result["name"]
    elif "name" in incoming_identity:
        result["name"] = incoming_identity["name"]
    if "role" in data:
        identity_input["role"] = result["role"]
    elif "role" in incoming_identity:
        result["role"] = incoming_identity["role"]
    if "description" in data:
        identity_input["description"] = result["description"]
    elif "description" in incoming_identity:
        result["description"] = incoming_identity["description"]
    for key in ("purpose", "responsibilities", "constraints"):
        if key in data:
            identity_input[key] = data[key]
    config["identity"] = normalize_identity(identity_input, name=result["name"], role=result["role"], description=result["description"])
    result["name"] = config["identity"]["name"]
    result["role"] = config["identity"]["role"]
    result["description"] = config["identity"].get("description", "")
    config["behavior"] = normalize_behavior(config.get("behavior"))
    config["autonomy"] = normalize_autonomy(config.get("autonomy"))
    config["verification"] = normalize_verification(config.get("verification"))
    config["output"] = normalize_output(config.get("output"))
    # The granular policy is stored with the existing JSON agent config.  A
    # top-level alias is accepted for API/UI ergonomics and normalized here.
    policy_input = data.get("capability_policy", config.get("capability_policy"))
    if policy_input is None:
        config["capability_policy"] = policy_from_legacy(config, result["tools"])
    else:
        config["capability_policy"] = validate_policy(policy_input)
    config["model"] = _text(config["model"], "model", 200, True)
    if config.get("orchestration_role") not in {"worker", "task_analyst", "planner", "qa", "auditor"}:
        raise ValueError("orchestration_role must be worker, task_analyst, planner, qa, or auditor.")
    if re.search(r"[\s\x00-\x1f]", config["model"]):
        raise ValueError("model must not contain whitespace or control characters.")
    config["system_prompt"] = _text(config["system_prompt"], "system_prompt", 16000)
    config["workspace_path"] = normalize_workspace_path(config["workspace_path"])
    config["endpoint"] = validate_endpoint(config["endpoint"])
    temp = config["temperature"]
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not math.isfinite(temp) or not 0 <= temp <= 2:
        raise ValueError("temperature must be between 0 and 2.")
    for key, (lower, upper) in LIMITS.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"{key} must be an integer between {lower} and {upper}.")
    if config["permissions"] not in {"read_only", "workspace", "execute"}:
        raise ValueError("permissions must be read_only, workspace, or execute.")
    for key in ("allowed_directories", "forbidden_commands"):
        values = config[key]
        if not isinstance(values, list) or len(values) > 40 or any(not isinstance(x, str) or not x.strip() or len(x) > 240 for x in values):
            raise ValueError(f"{key} must be a list of non-empty strings (up to 40).")
        config[key] = list(dict.fromkeys(x.strip() for x in values))
    if not config["allowed_directories"]:
        raise ValueError("At least one allowed directory is required.")
    normalized_dirs = []
    for directory in config["allowed_directories"]:
        path = PurePosixPath(directory.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in directory or "\x00" in directory:
            raise ValueError("Allowed directories must be relative to the workspace and cannot contain '..'.")
        normalized_dirs.append(str(path))
    config["allowed_directories"] = list(dict.fromkeys(normalized_dirs))
    env_name = config["secret_env"]
    if not isinstance(env_name, str) or (env_name and not re.fullmatch(r"ACC_SECRET_[A-Z0-9_]{1,100}", env_name)):
        raise ValueError("secret_env must be empty or an ACC_SECRET_... variable name; never a secret value.")
    selected = result["tools"]
    available = {tool["name"] for tool in TOOL_CATALOG if tool["available"]}
    if not isinstance(selected, list) or any(not isinstance(x, str) or x not in available for x in selected):
        raise ValueError("tools may only contain names of available tools.")
    result["tools"] = list(dict.fromkeys(selected))
    # The policy is the authority. Legacy tool selections are only migration
    # input and cannot expose a tool without an allow/ask capability.
    result["tools"] = effective_tools_for_policy(config["capability_policy"])
    # Preserve the legacy configuration contract for agents that have no
    # explicit capability policy. The policy remains the runtime authority.
    if policy_input is None and "run_command" in selected and config["permissions"] != "execute":
        raise ValueError("run_command requires execute permission in legacy configuration.")
    result["skills"] = normalize_skill_assignments(result.get("skills", []))
    result["config"] = config
    result["capability_policy"] = copy.deepcopy(config.get("capability_policy")) if config.get("capability_policy") is not None else None
    return result
