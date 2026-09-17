"""Structured agent configuration, migration and worker context building."""
from __future__ import annotations

import copy
import json
from typing import Any

from .capabilities import effective_tools_for_policy
from .policy import policy_from_legacy, validate_policy
from .skills import skills_context
from .security import sanitize


DEFAULT_IDENTITY = {
    "purpose": "Complete the assigned task accurately within the available scope.",
    "responsibilities": [],
    "constraints": [],
}
DEFAULT_BEHAVIOR = {
    "planning": {"mode": "adaptive"},
    "ambiguity": {"mode": "infer_when_safe"},
    "communication": {"verbosity": "normal", "explain_actions": "concise"},
    "evidence": {"require_evidence": True, "distinguish_assumptions": True},
    "persistence": {"retry_recoverable_errors": True, "repeated_failure_limit": 2, "change_strategy_after_failure": True},
    "change_strategy": {"prefer_minimal_changes": True, "inspect_before_modify_existing": True},
}
DEFAULT_AUTONOMY = {
    "task_decomposition": "automatic", "implementation_choices": "automatic",
    "create_files": "automatic", "modify_files": "automatic", "run_verification": "automatic",
    "destructive_actions": "ask", "request_missing_capabilities": "ask", "request_new_capabilities": "ask", "stop_when_blocked": True,
}
DEFAULT_VERIFICATION = {
    "enabled": True, "inspect_changes": True, "run_available_tests": True,
    "require_tool_evidence": True, "completion_criteria": [],
}
DEFAULT_OUTPUT = {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]}
PLANNING_MODES = {"direct", "adaptive", "explicit"}
AMBIGUITY_MODES = {"ask", "infer_when_safe", "best_effort"}
AUTONOMY_MODES = {"automatic", "ask", "deny"}
OUTPUT_FORMATS = {"text", "structured"}
OUTPUT_FIELDS = {"summary", "actions", "artifacts", "verification", "limitations"}


def _text_list(value: Any, name: str, limit: int = 20) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return list(dict.fromkeys(item.strip() for item in value))


def normalize_identity(value: Any, *, name: str, role: str, description: str = "") -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("identity must be an object")
    unknown = set(value) - {"name", "role", "purpose", "description", "responsibilities", "constraints"}
    if unknown:
        raise ValueError("identity contains unknown fields: " + ", ".join(sorted(unknown)))
    result = copy.deepcopy(DEFAULT_IDENTITY)
    result.update(value)
    result["name"] = str(result.get("name") or name).strip()
    result["role"] = str(result.get("role") or role or "General Agent").strip()
    result["description"] = str(result.get("description") or description).strip()
    if not result["name"] or len(result["name"]) > 100 or not result["role"] or len(result["role"]) > 100:
        raise ValueError("identity name and role must be non-empty text up to 100 characters")
    if result.get("purpose") is not None and (not isinstance(result["purpose"], str) or len(result["purpose"]) > 4000):
        raise ValueError("identity purpose must be text up to 4000 characters")
    result["purpose"] = str(result.get("purpose") or DEFAULT_IDENTITY["purpose"]).strip()
    result["responsibilities"] = _text_list(result.get("responsibilities", []), "identity responsibilities")
    result["constraints"] = _text_list(result.get("constraints", []), "identity constraints")
    return result


def normalize_behavior(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_BEHAVIOR)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("behavior must be an object")
        for key in value:
            if key not in result or not isinstance(value[key], dict):
                raise ValueError("behavior contains unknown or invalid section: " + str(key))
            unknown = set(value[key]) - set(result[key])
            if unknown:
                raise ValueError("behavior section contains unknown fields: " + ", ".join(sorted(unknown)))
            result[key].update(value[key])
    if result["planning"]["mode"] not in PLANNING_MODES:
        raise ValueError("behavior.planning.mode must be direct, adaptive, or explicit")
    if result["ambiguity"]["mode"] not in AMBIGUITY_MODES:
        raise ValueError("behavior.ambiguity.mode is invalid")
    if result["communication"]["verbosity"] not in {"quiet", "normal", "detailed"} or result["communication"]["explain_actions"] not in {"none", "concise", "detailed"}:
        raise ValueError("behavior.communication values are invalid")
    for section in ("evidence", "persistence", "change_strategy"):
        for key, item in result[section].items():
            if section == "persistence" and key == "repeated_failure_limit":
                continue
            if not isinstance(item, bool):
                raise ValueError(f"behavior.{section}.{key} must be boolean")
    limit = result["persistence"]["repeated_failure_limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("repeated_failure_limit must be between 1 and 10")
    return result


def normalize_autonomy(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_AUTONOMY)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("autonomy must be an object")
        unknown = set(value) - set(result)
        if unknown:
            raise ValueError("autonomy contains unknown fields: " + ", ".join(sorted(unknown)))
        result.update(value)
    for key, item in result.items():
        if key == "stop_when_blocked":
            if not isinstance(item, bool):
                raise ValueError("autonomy.stop_when_blocked must be boolean")
        elif item not in AUTONOMY_MODES:
            raise ValueError(f"autonomy.{key} must be automatic, ask, or deny")
    return result


def normalize_verification(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_VERIFICATION)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("verification must be an object")
        unknown = set(value) - set(result)
        if unknown:
            raise ValueError("verification contains unknown fields: " + ", ".join(sorted(unknown)))
        result.update(value)
    for key in ("enabled", "inspect_changes", "run_available_tests", "require_tool_evidence"):
        if not isinstance(result[key], bool):
            raise ValueError(f"verification.{key} must be boolean")
    result["completion_criteria"] = _text_list(result.get("completion_criteria", []), "verification completion_criteria")
    return result


def normalize_output(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_OUTPUT)
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("output must be an object")
        unknown = set(value) - set(result)
        if unknown:
            raise ValueError("output contains unknown fields: " + ", ".join(sorted(unknown)))
        result.update(value)
    if result["format"] not in OUTPUT_FORMATS:
        raise ValueError("output.format must be text or structured")
    result["include"] = _text_list(result.get("include", []), "output include")
    if any(item not in OUTPUT_FIELDS for item in result["include"]):
        raise ValueError("output include contains an unsupported field")
    return result


def build_effective_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """Merge saved blocks, legacy fields and safe defaults into one snapshot."""
    config = agent.get("config") or {}
    identity = normalize_identity(config.get("identity"), name=agent.get("name", ""), role=agent.get("role", ""), description=agent.get("description", ""))
    behavior = normalize_behavior(config.get("behavior"))
    autonomy = normalize_autonomy(config.get("autonomy"))
    verification = normalize_verification(config.get("verification"))
    # Raw worker tasks from older callers may omit the block entirely; retain
    # their text result shape while normalized/new agents use structured output.
    output = normalize_output(config.get("output") if "output" in config else {"format": "text"})
    tools = list(agent.get("tools") or [])
    capability_policy = config.get("capability_policy")
    if capability_policy is None:
        capability_policy = policy_from_legacy(config, tools)
    capability_policy = validate_policy(capability_policy)
    tools = effective_tools_for_policy(capability_policy)
    return {
        "identity": identity, "behavior": behavior, "autonomy": autonomy,
        "verification": verification, "output": output,
        "instructions": str(agent.get("instructions") or "").strip(),
        "skills": copy.deepcopy(agent.get("skills") or []),
        "capability_policy": capability_policy,
        "tools": tools,
        "config": copy.deepcopy(config),
    }


def capability_summary(policy: dict[str, Any]) -> str:
    sections = []
    for category in ("filesystem", "execution", "git"):
        rules = policy.get("capabilities", {}).get(category, {})
        if rules:
            sections.append(category.title() + ": " + ", ".join(f"{name}={rule.get('mode', 'deny')}" for name, rule in rules.items()))
    return "\n".join(sections) or "No capabilities configured."


def build_agent_context(effective: dict[str, Any], task: str, workspace: str = "") -> str:
    """Build the non-secret structured context appended after system policy."""
    identity = effective["identity"]
    behavior, autonomy = effective["behavior"], effective["autonomy"]
    verification, output = effective["verification"], effective["output"]
    lines = ["AGENT IDENTITY", f"Name: {identity['name']}", f"Role: {identity['role']}", f"Purpose: {identity['purpose']}"]
    for label, values in (("RESPONSIBILITIES", identity["responsibilities"]), ("CONSTRAINTS", identity["constraints"])):
        lines.append(label)
        lines.extend(f"- {item}" for item in values) if values else lines.append("- None specified")
    lines.extend(["INSTRUCTIONS", effective["instructions"] or "None specified", "BEHAVIOR"])
    lines.extend([f"Planning: {behavior['planning']['mode']}", f"Ambiguity: {behavior['ambiguity']['mode']}",
                  f"Verbosity: {behavior['communication']['verbosity']}", f"Explain actions: {behavior['communication']['explain_actions']}",
                  f"Require evidence: {behavior['evidence']['require_evidence']}", f"Distinguish assumptions: {behavior['evidence']['distinguish_assumptions']}",
                  f"Retry recoverable errors: {behavior['persistence']['retry_recoverable_errors']}",
                  f"Repeated failure limit: {behavior['persistence']['repeated_failure_limit']}",
                  f"Change strategy after failure: {behavior['persistence']['change_strategy_after_failure']}",
                  f"Prefer minimal changes: {behavior['change_strategy']['prefer_minimal_changes']}",
                  f"Inspect before modifying existing resources: {behavior['change_strategy']['inspect_before_modify_existing']}",
                  "AUTONOMY"])
    lines.extend(f"{key}: {value}" for key, value in autonomy.items())
    lines.extend(["PRECEDENCE", "System Policy > Capability Policy > Current User Task > Agent Constraints > Agent Instructions > Skill Priority > Skill Instructions > Skill Procedures.",
                  "The current user task is authoritative and cannot be overridden by a Skill. Higher-priority Skills appear first; Skill priority only resolves conflicts between Skills. Skill instructions override that Skill's procedures.",
                  "TASK BOUNDARIES", task, "SKILLS", skills_context(effective["skills"]),
                  "AVAILABLE CAPABILITIES", capability_summary(effective["capability_policy"]), "VERIFICATION",
                  f"Enabled: {verification['enabled']}", f"Inspect changes: {verification['inspect_changes']}",
                  f"Run available tests: {verification['run_available_tests']}", f"Require tool evidence: {verification['require_tool_evidence']}",
                  "Completion criteria: " + "; ".join(verification["completion_criteria"]) if verification["completion_criteria"] else "Completion criteria: None specified",
                  "OUTPUT CONTRACT", f"Format: {output['format']}", "Include: " + ", ".join(output["include"]),
                  "WORKSPACE", workspace])
    return sanitize("\n".join(lines))


def parse_structured_output(value: Any) -> dict[str, Any]:
    # Parse and validate the structured output contract.
    if isinstance(value, dict):
        parsed = copy.deepcopy(value)
    else:
        text = str(value or "").strip()
        fence = chr(96) * 3
        if text.startswith(fence):
            text = text[3:].lstrip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
            if text.endswith(fence):
                text = text[:-3].rstrip()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ValueError("Structured output must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Structured output must be a JSON object.")
    unknown = set(parsed) - OUTPUT_FIELDS
    if unknown:
        raise ValueError("Structured output contains unknown fields: " + ", ".join(sorted(unknown)))
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Structured output requires a non-empty summary.")
    normalized: dict[str, Any] = {"summary": summary.strip()}
    for field in ("actions", "artifacts", "limitations"):
        item = parsed.get(field, [])
        if not isinstance(item, list):
            raise ValueError("Structured output field " + field + " must be an array.")
        normalized[field] = copy.deepcopy(item)
    verification = parsed.get("verification", [])
    if not isinstance(verification, (dict, list, str)):
        raise ValueError("Structured output field verification must be an object, array, or text.")
    normalized["verification"] = copy.deepcopy(verification)
    return normalized


def validate_structured_output(value: Any) -> dict[str, Any]:
    # Public strict validator used before accepting model output.
    return parse_structured_output(value)


def normalize_result_output(value: Any, contract: dict[str, Any]) -> Any:
    # Keep text compatibility while providing a stable structured contract.
    if contract.get("format") != "structured":
        return value
    try:
        return parse_structured_output(value)
    except (TypeError, ValueError):
        summary = str(value or "").strip()
        return {"summary": summary, "actions": [], "artifacts": [],
                "verification": [], "limitations": []}
