"""Reusable, declarative agent skills.

Skills provide knowledge and recommended operating procedures.  They never
grant a capability, alter a workspace, or bypass the policy engine.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .capabilities import CAPABILITY_REGISTRY, effective_tools_for_policy, tool_for_capability


SKILL_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
SECRET_FIELD_RE = re.compile(r"(?:secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|authorization)", re.I)
SECRET_VALUE_RE = re.compile(r"(?:secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|authorization)\s*[:=]\s*[^\s]+", re.I)
CONTROL_FIELD_RE = re.compile(r"^(?:permissions?|capability_policy|tools?|allowed_tools|allowed_directories|forbidden_commands|workspace(?:_path)?|commands?|grant(?:s)?|allowlist|denylist)$", re.I)
MAX_SKILLS_PER_AGENT = 100
MAX_SKILLS_IMPORT = 500
MAX_PROCEDURES = 30
MAX_STEPS = 50
MAX_TAGS = 30
MAX_INSTRUCTIONS = 16000
MAX_STEP_LENGTH = 1000
MAX_METADATA_DEPTH = 4
MAX_CONTEXT_CHARS = 64000


BUILTIN_SKILLS: tuple[dict[str, Any], ...] = (
    {
        "id": "simple-file-artifact", "name": "Simple File Artifact", "category": "Artifact Creation",
        "version": 1,
        "description": "Create and verify one small local file without unnecessary project scaffolding.",
        "instructions": [
            "Create only the requested artifact in the selected workspace.",
            "Inspect before modifying an existing artifact during recovery.",
            "Read the artifact after creation and stop when objective evidence is sufficient.",
            "Never repeat an identical policy-denied write and do not add tests or audits unless requested.",
        ],
        "procedures": [{
            "name": "Create and Verify Artifact",
            "steps": [
                "Inspect the target only when needed to distinguish a new file from an existing artifact.",
                "Create the requested file with the exact requested content.",
                "Read it back and compare the content with the objective.",
                "Report the read-back evidence and finish without creating extra files.",
            ],
        }],
        "recommended_capabilities": ["filesystem.read", "filesystem.create"],
        "required_capabilities": ["filesystem.read", "filesystem.create"],
        "tags": ["file", "artifact", "local", "creation", "readback", "simple"],
        "source": "builtin", "enabled": True,
    },
    {
        "id": "python-development", "name": "Python Development", "category": "Software Development",
        "version": 1, "description": "Develop, debug and validate Python software.",
        "instructions": ["Follow existing project conventions.", "Prefer small reviewable changes.", "Validate syntax and relevant tests when available."],
        "procedures": [{"name": "Implement Change", "description": "A practical sequence for a Python change.", "steps": ["Inspect relevant existing files.", "Identify affected code.", "Implement the minimal change.", "Validate syntax.", "Run relevant tests when available.", "Inspect the resulting diff."]}],
        "recommended_capabilities": ["filesystem.read", "filesystem.search", "filesystem.create", "filesystem.modify", "filesystem.overwrite", "execution.python_script", "execution.pytest", "execution.py_compile", "git.diff"],
        "required_capabilities": ["filesystem.read"], "tags": ["python", "development", "backend", "debugging"], "source": "builtin", "enabled": True,
    },
    {
        "id": "code-review", "name": "Code Review", "category": "Software Quality", "version": 1,
        "description": "Review changes for correctness, risks and maintainability.",
        "instructions": ["Ground findings in the current code and diff.", "Prioritize concrete correctness and security issues."],
        "procedures": [{"name": "Review Change", "steps": ["Inspect the relevant diff.", "Trace affected callers and data flow.", "Check edge cases and error handling.", "Report findings with evidence."]}],
        "recommended_capabilities": ["filesystem.read", "filesystem.search", "git.diff"], "required_capabilities": ["filesystem.read"],
        "tags": ["review", "quality", "security"], "source": "builtin", "enabled": True,
    },
    {
        "id": "software-testing", "name": "Software Testing", "category": "Software Quality", "version": 1,
        "description": "Design, run and interpret focused software tests.",
        "instructions": ["Prefer focused tests that verify observable behavior.", "Explain unavailable test infrastructure instead of fabricating results."],
        "procedures": [{"name": "Validate Change", "steps": ["Locate relevant tests.", "Add or adapt focused coverage when appropriate.", "Run the available test command.", "Inspect failures and report evidence."]}],
        "recommended_capabilities": ["filesystem.read", "filesystem.create", "filesystem.modify", "execution.pytest", "execution.unittest", "execution.py_compile"],
        "required_capabilities": ["filesystem.read"], "tags": ["testing", "pytest", "unittest", "verification"], "source": "builtin", "enabled": True,
    },
    {
        "id": "interactive-testing", "name": "Interactive Program Testing", "category": "Software Quality", "version": 1,
        "description": "Exercise command-line programs with bounded controlled input and verify observable output.",
        "instructions": [
            "Use run_command stdin for programs that call input; never wait for a human terminal.",
            "Test representative and edge-case inputs, then cite exit status and observed output.",
            "Do not modify the implementation while acting as QA; report failures to Freya.",
        ],
        "procedures": [{
            "name": "Test Interactive CLI", "description": "Validate an interactive Python program without a live terminal.",
            "steps": [
                "Inspect the implementation to determine its input sequence.",
                "Prepare bounded newline-delimited stdin for a representative case.",
                "Run the workspace Python script with controlled stdin and a short timeout.",
                "Compare the exit status and output with the expected logical result.",
                "Repeat only with a distinct edge case when it adds useful evidence.",
                "Report the exact command, input case, exit status and observed output.",
            ],
        }],
        "recommended_capabilities": ["filesystem.read", "filesystem.search", "execution.python_script"],
        "required_capabilities": ["filesystem.read", "execution.python_script"],
        "tags": ["qa", "interactive", "stdin", "cli", "testing"], "source": "builtin", "enabled": True,
    },
    {
        "id": "debugging", "name": "Debugging", "category": "Engineering", "version": 1,
        "description": "Find root causes and apply evidence-based fixes.",
        "instructions": ["Start from observable symptoms and reproduce when possible.", "Separate confirmed causes from hypotheses."],
        "procedures": [{"name": "Debug Failure", "steps": ["Inspect the error and surrounding implementation.", "Reproduce the failure when possible.", "Locate the root cause.", "Apply a focused fix.", "Run relevant verification."]}],
        "recommended_capabilities": ["filesystem.read", "filesystem.search", "filesystem.modify", "execution.python_script", "execution.pytest"],
        "required_capabilities": ["filesystem.read"], "tags": ["debugging", "diagnostics", "errors"], "source": "builtin", "enabled": True,
    },
    {
        "id": "git-inspection", "name": "Git Inspection", "category": "Engineering", "version": 1,
        "description": "Inspect repository state and review the resulting changes.",
        "instructions": ["Use Git as evidence about the current workspace state.", "Keep repository inspection read-only."],
        "procedures": [{"name": "Inspect Repository", "steps": ["Inspect repository status.", "Review the relevant diff.", "Summarize tracked changes and limitations."]}],
        "recommended_capabilities": ["git.status", "git.diff"], "required_capabilities": ["git.diff"],
        "tags": ["git", "diff", "repository"], "source": "builtin", "enabled": True,
    },
)


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None and not required:
        value = ""
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{field} must be {'non-empty ' if required else ''}text up to {maximum} characters")
    return value.strip()


def _string_list(value: Any, field: str, maximum: int, item_maximum: int = 2000) -> list[str]:
    if isinstance(value, str):
        # Existing rows stored instructions as a text blob.  Accepting a
        # newline-separated legacy value keeps those rows readable.
        value = [line.strip() for line in value.splitlines() if line.strip()]
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a list with at most {maximum} items")
    if any(not isinstance(item, str) or not item.strip() or len(item) > item_maximum for item in value):
        raise ValueError(f"{field} must contain non-empty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if any(SECRET_VALUE_RE.search(item) for item in result):
        raise ValueError(f"{field} cannot contain credential values")
    return result


def _safe_metadata(value: Any, depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("metadata is too deeply nested")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if (not isinstance(key, str) or len(key) > 100 or SECRET_FIELD_RE.search(key)
                    or CONTROL_FIELD_RE.fullmatch(key)):
                raise ValueError("skill metadata cannot contain secret, credential, or policy-control fields")
            result[key] = _safe_metadata(item, depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("skill metadata list is too large")
        return [_safe_metadata(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 4000:
            raise ValueError("skill metadata text is too long")
        return value
    raise ValueError("skill metadata contains an unsupported value")


def _procedures(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_PROCEDURES:
        raise ValueError(f"procedures must be a list with at most {MAX_PROCEDURES} items")
    result = []
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("each procedure must be an object")
        unknown = set(raw) - {"name", "description", "steps"}
        if unknown:
            raise ValueError("procedure contains unknown fields: " + ", ".join(sorted(unknown)))
        name = _text(raw.get("name"), "procedure.name", 120, required=True)
        key = name.casefold()
        if key in names:
            raise ValueError("procedure names must be unique")
        names.add(key)
        description = _text(raw.get("description", ""), "procedure.description", 2000)
        steps = _string_list(raw.get("steps", []), "procedure.steps", MAX_STEPS, MAX_STEP_LENGTH)
        result.append({"name": name, "description": description, "steps": steps})
    return result


def _capability_list(value: Any, field: str) -> list[str]:
    values = _string_list(value or [], field, len(CAPABILITY_REGISTRY), 120)
    unknown = [item for item in values if item not in CAPABILITY_REGISTRY]
    if unknown:
        raise ValueError(f"{field} contains unknown capabilities: {', '.join(unknown)}")
    return values


def normalize_skill(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and normalize a persisted skill definition."""
    if not isinstance(data, dict):
        raise ValueError("Skill must be an object")
    allowed = {"id", "name", "description", "category", "version", "instructions", "procedures",
               "recommended_capabilities", "required_capabilities", "tags", "enabled", "source", "metadata",
               "created_at", "updated_at"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Skill contains unknown fields: " + ", ".join(sorted(unknown)))
    baseline = existing or {}
    result = {key: copy.deepcopy(baseline.get(key, default)) for key, default in {
        "id": "", "name": "", "description": "", "category": "General", "version": 1,
        "instructions": [], "procedures": [], "recommended_capabilities": [], "required_capabilities": [],
        "tags": [], "enabled": True, "source": "user", "metadata": {},
    }.items()}
    result.update(data)
    result["id"] = _text(result["id"], "id", 120, required=True).lower()
    if not SKILL_ID_RE.fullmatch(result["id"]):
        raise ValueError("id must use lowercase letters, numbers, '-' or '_' separators")
    result["name"] = _text(result["name"], "name", 120, required=True)
    result["description"] = _text(result["description"], "description", 2000)
    result["category"] = _text(result["category"], "category", 120, required=True)
    version = result["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1 or version > 1000000:
        raise ValueError("version must be a positive integer")
    result["version"] = version
    result["instructions"] = _string_list(result["instructions"], "instructions", 100, MAX_INSTRUCTIONS)
    if sum(len(item) for item in result["instructions"]) > MAX_INSTRUCTIONS:
        raise ValueError(f"instructions must contain at most {MAX_INSTRUCTIONS} characters")
    result["procedures"] = _procedures(result["procedures"])
    result["recommended_capabilities"] = _capability_list(result["recommended_capabilities"], "recommended_capabilities")
    result["required_capabilities"] = _capability_list(result["required_capabilities"], "required_capabilities")
    result["tags"] = _string_list(result["tags"], "tags", MAX_TAGS, 80)
    if not isinstance(result["enabled"], bool):
        raise ValueError("enabled must be boolean")
    if result["source"] not in {"builtin", "user"}:
        raise ValueError("source must be builtin or user")
    result["metadata"] = _safe_metadata(result.get("metadata") or {})
    return result


def validate_skill(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public registry alias used by API adapters and integrations."""
    return normalize_skill(data, existing)


def skill_summary(skill: dict[str, Any]) -> dict[str, Any]:
    """Small selection payload safe to pass to Freya or an agent card."""
    return {"id": skill["id"], "name": skill["name"], "category": skill.get("category", "General"),
            "version": skill["version"], "tags": list(skill.get("tags", [])),
            "operational": bool(skill.get("operational", False)),
            "missing_required_capabilities": list(skill.get("missing_required_capabilities", [])),
            "missing_required_tools": list(skill.get("missing_required_tools", [])),
            "missing_recommended_capabilities": list(skill.get("missing_recommended_capabilities", [])),
            "missing_recommended_tools": list(skill.get("missing_recommended_tools", [])),
            "required_tools": list(skill.get("required_tools", [])),
            "priority": int(skill.get("priority", 0))}


def skill_snapshot(skill: dict[str, Any]) -> dict[str, Any]:
    """Copy the effective skill data needed for an immutable task record."""
    return {key: copy.deepcopy(skill.get(key, default)) for key, default in {
        "id": "", "name": "", "description": "", "category": "General", "version": 1,
        "instructions": [], "procedures": [], "required_capabilities": [],
        "recommended_capabilities": [], "tags": [], "priority": 0,
        "operational": False, "missing_required_capabilities": [], "missing_required_tools": [],
        "missing_recommended_capabilities": [], "missing_recommended_tools": [], "required_tools": [], "active": True,
    }.items()}


def normalize_skill_assignments(value: Any) -> list[dict[str, Any]]:
    """Normalize an agent's stable skill references and per-agent priority."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SKILLS_PER_AGENT:
        raise ValueError(f"skills must contain at most {MAX_SKILLS_PER_AGENT} assignments")
    result = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            skill_id, priority = item, 0
        elif isinstance(item, dict):
            skill_id = item.get("skill_id", item.get("id"))
            priority = item.get("priority", 0)
        else:
            raise ValueError("each skill assignment must be an id or object")
        skill_id = _text(skill_id, "skill id", 120, required=True).lower()
        if not SKILL_ID_RE.fullmatch(skill_id):
            raise ValueError("skill ids must use lowercase letters, numbers, '-' or '_' separators")
        if skill_id in seen:
            raise ValueError("duplicate skill assignment: " + skill_id)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < -1000000 or priority > 1000000:
            raise ValueError("skill priority must be an integer between -1000000 and 1000000")
        seen.add(skill_id)
        result.append({"skill_id": skill_id, "priority": priority})
    return result


def _policy_mode(policy: dict[str, Any], capability: str) -> str:
    category, action = capability.split(".", 1)
    rule = policy.get("capabilities", {}).get(category, {}).get(action, {})
    return rule.get("mode", "deny") if isinstance(rule, dict) else "deny"


def _relevance(skill: dict[str, Any], task: str) -> int:
    if not task.strip():
        return 0
    haystack = " ".join([skill.get("name", ""), skill.get("category", ""), *skill.get("tags", [])]).casefold()
    words = {word for word in re.findall(r"[a-z0-9_+-]{3,}", task.casefold())}
    return sum(1 for word in words if word in haystack)


def resolve_agent_skills(agent: dict[str, Any], assigned_skills: Iterable[dict[str, Any]] | None = None,
                         task: str = "", policy: dict[str, Any] | None = None,
                         available_tools: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Resolve assigned skills and annotate policy compatibility.

    ``assigned_skills`` are complete skill records with an optional
    ``priority`` field.  This function deliberately only reads policy modes;
    it never changes the policy or selected tools.
    """
    records = list(assigned_skills if assigned_skills is not None else agent.get("skills", []))
    policy = policy or agent.get("capability_policy") or {"capabilities": {}}
    if available_tools is None:
        if "tools" in agent:
            available = set(agent.get("tools", []))
        else:
            available = set(effective_tools_for_policy(policy))
    else:
        available = set(available_tools)
    resolved = []
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            continue
        try:
            # Assignment/runtime annotations are not part of the persisted
            # skill definition and must not make an otherwise valid record
            # fail schema validation.
            definition = {key: value for key, value in raw.items()
                          if key not in {"priority", "skill_priority", "operational", "missing_required_capabilities",
                                         "missing_recommended_capabilities", "missing_required_tools",
                                         "missing_recommended_tools", "relevance", "assignment_order", "active",
                                         "assigned_agents", "assigned_agent_ids", "required_tools"}}
            skill = normalize_skill(definition)
        except ValueError:
            continue
        priority = raw.get("priority", raw.get("skill_priority", 0))
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = 0
        missing = [cap for cap in skill["required_capabilities"] if _policy_mode(policy, cap) != "allow"]
        missing_recommended = [cap for cap in skill["recommended_capabilities"] if _policy_mode(policy, cap) != "allow"]
        missing_tools = [tool_for_capability(cap) or cap for cap in skill["required_capabilities"]
                         if _policy_mode(policy, cap) == "allow" and (tool_for_capability(cap) or cap) not in available]
        missing_recommended_tools = [tool_for_capability(cap) or cap for cap in skill["recommended_capabilities"]
                                     if _policy_mode(policy, cap) == "allow" and (tool_for_capability(cap) or cap) not in available]
        required_tools = list(dict.fromkeys(tool_for_capability(cap) for cap in skill["required_capabilities"]
                                            if tool_for_capability(cap)))
        item = {**skill, "priority": priority, "operational": bool(skill["enabled"] and not missing and not missing_tools),
                "missing_required_capabilities": missing, "missing_required_tools": missing_tools,
                "missing_recommended_capabilities": missing_recommended, "missing_recommended_tools": missing_recommended_tools,
                "required_tools": required_tools, "relevance": _relevance(skill, task), "assignment_order": index}
        resolved.append(item)
    resolved.sort(key=lambda item: (-item["priority"], -item["relevance"], item["assignment_order"], item["id"]))
    active_candidates = [item for item in resolved if item["enabled"]]
    if task and len(active_candidates) > 8:
        # Keep high-priority skills while allowing a relevant lower-priority
        # specialty into the active context.  The assigned list remains intact.
        active_ids = {item["id"] for item in sorted(active_candidates, key=lambda item: (-item["relevance"], -item["priority"], item["assignment_order"]))[:8]}
        for item in resolved:
            item["active"] = item["enabled"] and item["id"] in active_ids
    else:
        for item in resolved:
            item["active"] = item["enabled"]
    return resolved


def render_skill(skill: dict[str, Any], *, full: bool = False) -> str:
    lines = [f"## {skill['name']}", f"Priority: {int(skill.get('priority', 0))}", f"Version: {skill['version']}", f"Category: {skill.get('category', 'General')}",
             f"Operational: {skill.get('operational', False)}"]
    if skill.get("description"):
        lines.extend(["Purpose:", skill["description"]])
    instructions = skill.get("instructions", [])
    if instructions:
        lines.append("Instructions:")
        lines.extend(f"- {item}" for item in instructions[:12])
    if full and skill.get("procedures"):
        lines.append("Relevant procedures:")
        for procedure in skill["procedures"][:5]:
            lines.append(f"### {procedure['name']}")
            if procedure.get("description"):
                lines.append(procedure["description"])
            lines.extend(f"{index}. {step}" for index, step in enumerate(procedure.get("steps", [])[:MAX_STEPS], 1))
    elif skill.get("procedures"):
        lines.append("Procedures: " + "; ".join(item["name"] for item in skill["procedures"][:5]))
    if skill.get("required_capabilities"):
        lines.append("Required capabilities: " + ", ".join(skill["required_capabilities"]))
    if skill.get("recommended_capabilities"):
        lines.append("Recommended capabilities: " + ", ".join(skill["recommended_capabilities"][:12]))
    if skill.get("missing_required_capabilities"):
        lines.append("Missing required capabilities: " + ", ".join(skill["missing_required_capabilities"]))
    if skill.get("missing_required_tools"):
        lines.append("Missing tools/runtime support: " + ", ".join(skill["missing_required_tools"]))
    if skill.get("missing_recommended_capabilities"):
        lines.append("Missing recommended capabilities: " + ", ".join(skill["missing_recommended_capabilities"]))
    if skill.get("missing_recommended_tools"):
        lines.append("Missing recommended tools/runtime support: " + ", ".join(skill["missing_recommended_tools"]))
    return "\n".join(lines)


def skills_context(skills: Iterable[dict[str, Any]], *, full: bool = False) -> str:
    selected = [skill for skill in skills if skill.get("active", True)]
    if not selected:
        return "No active skills assigned."
    detailed = len(selected) <= 3
    header = "Use skill procedures as guidance. Adapt them when steps are irrelevant or impossible.\n\n"
    context = header + "\n\n".join(render_skill(skill, full=full or detailed or bool(skill.get("relevance"))) for skill in selected)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS - 45].rstrip() + "\n[Skill context truncated for budget.]"
    return context


@dataclass(frozen=True)
class SkillRegistry:
    """Small central facade used by storage, context and API adapters."""

    def validate(self, skill: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        return normalize_skill(skill, existing)

    def list(self, skills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [copy.deepcopy(skill) for skill in skills]

    def get(self, skills: Iterable[dict[str, Any]], skill_id: str) -> dict[str, Any]:
        for skill in skills:
            if isinstance(skill, dict) and skill.get("id") == skill_id:
                return copy.deepcopy(skill)
        raise KeyError(skill_id)

    def resolve(self, agent: dict[str, Any], assigned_skills: Iterable[dict[str, Any]] | None = None,
                task: str = "", policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return resolve_agent_skills(agent, assigned_skills, task, policy)

    def summary(self, skill: dict[str, Any]) -> dict[str, Any]:
        return skill_summary(skill)

    def compatibility(self, skill: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        resolved = resolve_agent_skills({}, [skill], policy=policy)
        return resolved[0] if resolved else {"operational": False, "missing_required_capabilities": list(skill.get("required_capabilities", []))}
