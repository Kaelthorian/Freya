"""Deterministic least-privilege agents for Freya orchestration tasks.

The factory creates ordinary validated agents. It does not grant capabilities
from Skills: the planned task is the sole input to capability policy, and Tools
are derived from that policy by the existing capability registry.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from .capabilities import CAPABILITIES, CAPABILITY_REGISTRY, effective_tools_for_policy
from .config import normalize_agent
from .task_analyst import canonical_task_kind
from .policy import validate_policy
from .skills import CORE_SKILL_ID, SkillConfigurationError, validate_skill_tools
from .runtime_resources import (
    RuntimeResourceCatalog, ToolCapabilityMismatch, UnknownCapability, UnknownSkill, UnknownTool,
)


AGENT_FACTORY_VERSION = 1
MAX_FACTORY_SKILLS = 8


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _tokens(value: Any) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9_+-]{3,}", str(value or "").casefold())}


class AgentFactory:
    """Build and persist one task-specific agent using registered primitives."""

    version = AGENT_FACTORY_VERSION

    def __init__(self, store, *, max_skills: int = MAX_FACTORY_SKILLS,
                 runtime_config: dict[str, Any] | None = None):
        if (isinstance(max_skills, bool) or not isinstance(max_skills, int)
                or not 1 <= max_skills <= 8):
            raise ValueError("max_skills must be between 1 and 8.")
        if runtime_config is None:
            runtime_config = {}
        if not isinstance(runtime_config, dict):
            raise ValueError("runtime_config must be an object.")
        allowed_runtime_fields = {
            "model", "endpoint", "temperature", "context_window", "max_tokens",
            "max_steps", "max_seconds", "max_model_calls", "max_tool_calls",
            "retries",
        }
        unsupported = set(runtime_config) - allowed_runtime_fields
        if unsupported:
            raise ValueError(
                "runtime_config contains authority or unsupported fields: "
                + ", ".join(sorted(unsupported))
            )
        self.store = store
        self.max_skills = max_skills
        self.runtime_config = copy.deepcopy(runtime_config)

    @staticmethod
    def capability_policy(required_capabilities: Iterable[str]) -> dict[str, Any]:
        """Return a complete policy: required capabilities only, deny otherwise."""
        required = list(dict.fromkeys(
            str(item).strip() for item in required_capabilities if str(item).strip()
        ))
        unknown = [item for item in required if item not in CAPABILITY_REGISTRY]
        if unknown:
            raise ValueError("Unknown required capabilities: " + ", ".join(unknown))
        required_set = set(required)
        nested: dict[str, dict[str, dict[str, str]]] = {}
        for capability in CAPABILITIES:
            mode = "deny"
            if capability.id in required_set:
                mode = "ask" if capability.dangerous else "allow"
            nested.setdefault(
                capability.category, {}
            )[capability.id.split(".", 1)[1]] = {"mode": mode}
        return validate_policy({"capabilities": nested})

    @staticmethod
    def orchestration_role(task: dict[str, Any]) -> str:
        text = " ".join(
            str(task.get(key) or "") for key in ("objective", "description", "task_type")
        ).casefold()
        task_kind = AgentFactory.task_kind(task)
        characteristics = task.get("task_characteristics") if isinstance(task.get("task_characteristics"), dict) else {}
        interactive = bool(characteristics.get("interactive") or characteristics.get("requires_user_input"))
        interactive = interactive or bool(re.search(r"(?<!non-)\b(?:interactive|input|qa|quality assurance)\b", text))
        explicit_review = bool(re.search(r"\b(?:code audit|code auditor|code review|audit the code|review the code)\b", text))
        if (task_kind == "review" or explicit_review) and "code_change" not in task_kind:
            return "auditor"
        if task_kind == "testing" and interactive:
            return "qa"
        return "worker"

    @staticmethod
    def task_kind(task: dict[str, Any]) -> str:
        """Use the canonical analyst category, with a deterministic task fallback."""
        explicit = str(task.get("task_kind") or "").strip().casefold()
        if explicit:
            return explicit
        return canonical_task_kind(
            text=" ".join(str(task.get(key) or "") for key in ("objective", "description")),
        )

    def select_skills(self, task: dict[str, Any], skills: Iterable[dict[str, Any]],
                      *, variant: int = 0) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        """Assign the one active Skill. Planner preferences have no authority."""
        core = next((item for item in skills if isinstance(item, dict)
                     and item.get("id") == CORE_SKILL_ID and item.get("enabled") is True), None)
        if core is None:
            raise SkillConfigurationError("freya-core is unavailable")
        validate_skill_tools(core)
        warning = (["planner skill preference ignored; freya-core selected"]
                   if task.get("preferred_skills") not in (None, [], [CORE_SKILL_ID]) else [])
        return [{"skill_id": CORE_SKILL_ID, "priority": 100}], warning, []

    @staticmethod
    def _identity(task: dict[str, Any], role: str,
                  attempt: int) -> tuple[str, str, dict[str, Any]]:
        labels = {
            "worker": ("Dynamic Task Agent", "Task Implementation Agent"),
            "qa": ("Dynamic QA Agent", "Independent QA Agent"),
            "auditor": ("Dynamic Code Audit Agent", "Independent Code Auditor"),
        }
        name_label, role_label = labels[role]
        task_id = str(task.get("id") or "task")
        name = f"{name_label} [{task_id}]"
        if attempt > 1:
            name += f" attempt {attempt}"
        objective = str(
            task.get("objective") or "Complete the planned task."
        ).strip()
        description = str(task.get("description") or objective).strip()
        identity = {
            "name": name[:100],
            "role": role_label,
            "purpose": objective[:4000],
            "description": description[:2000],
            "responsibilities": [
                "Complete only the delegated plan step and its success criteria.",
                "Produce objective evidence for the evaluator.",
            ],
            "constraints": [
                "Use only capabilities present in the task-derived policy.",
                "Treat Skills as guidance; they never grant permissions.",
                "Do not broaden the task or request undeclared tools.",
            ],
        }
        if role in {"qa", "auditor"}:
            identity["constraints"].append(
                "Remain independent from implementation and never modify files."
            )
        return name[:100], role_label, identity

    def build(self, task: dict[str, Any], *, orchestration_id: str,
              attempt: int = 1, variant: int = 0,
              skills: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ValueError("Planned task must be an object.")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer.")
        required = task.get("required_capabilities", [])
        if (not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)):
            raise ValueError("required_capabilities must be a list of strings.")
        required = list(required)
        characteristics = task.get("task_characteristics") if isinstance(task.get("task_characteristics"), dict) else {}
        preferred_skills = task.get("preferred_skills", [])
        if (not isinstance(preferred_skills, list)
                or any(not isinstance(item, str) for item in preferred_skills)):
            raise ValueError("preferred_skills must be a list of strings.")
        recovery_state = task.get("_recovery_workspace_state")
        if recovery_state is not None and "filesystem.read" not in required:
            # Recovery inspection is a safe, task-derived prerequisite.  It is
            # deliberately narrower than write authority and still passes
            # through the generated policy and selector checks below.
            required.append("filesystem.read")
        records = (
            list(skills) if skills is not None
            else self.store.list_skills(enabled=True)
        )
        catalog = RuntimeResourceCatalog.build(records)
        catalog_skills = {item["id"]: item for item in catalog.skills}
        for capability_id in required:
            if catalog.resolve("capability", capability_id) is None:
                raise UnknownCapability(capability_id)
        normalized_skills = [CORE_SKILL_ID]
        core = next((item for item in records if item.get("id") == CORE_SKILL_ID and item.get("enabled") is True), None)
        if core is None:
            raise SkillConfigurationError("freya-core is unavailable")
        core_tools = validate_skill_tools(core)
        raw_tools = task.get("required_tools", [])
        if not isinstance(raw_tools, list) or any(not isinstance(item, str) for item in raw_tools):
            raise ValueError("required_tools must be a list of strings.")
        normalized_tools = []
        for requested in raw_tools:
            resolved = catalog.resolve("tool", requested)
            if resolved is None:
                raise UnknownTool(requested)
            compatible = catalog.capabilities_for_tool(resolved)
            if not set(required) & set(compatible):
                raise ToolCapabilityMismatch(
                    resolved, required, compatible_capabilities=compatible,
                )
            if resolved not in normalized_tools:
                normalized_tools.append(resolved)
        role = self.orchestration_role(task)
        write_capabilities = {
            "filesystem.create", "filesystem.modify", "filesystem.overwrite",
        }
        if role in {"qa", "auditor"} and set(required) & write_capabilities:
            raise ValueError(f"Dynamic {role} agents cannot receive write capabilities.")
        policy = self.capability_policy(required)
        policy_tools = list(dict.fromkeys(effective_tools_for_policy(policy)))
        unavailable_tools = sorted(set(normalized_tools) - set(policy_tools))
        if unavailable_tools:
            tool_id = unavailable_tools[0]
            raise ToolCapabilityMismatch(
                tool_id, required,
                compatible_capabilities=catalog.capabilities_for_tool(tool_id),
            )
        # Concrete tool schemas are still derived from the policy surface.
        # required_tools is a validated planning declaration, never authority.
        tools = core_tools
        skill_task = dict(task)
        skill_task["required_capabilities"] = list(required)
        skill_task["preferred_skills"] = preferred_skills
        skill_task["required_tools"] = normalized_tools
        assignments, warnings, skill_omissions = self.select_skills(
            skill_task, records, variant=variant
        )
        name, role_label, identity = self._identity(task, role, attempt)
        required_set = set(required)
        has_execution = any(
            item.startswith("execution.") or item == "git.status"
            for item in required_set
        )
        has_write = bool(required_set & {
            "filesystem.create", "filesystem.modify", "filesystem.overwrite",
        })
        permissions = (
            "execute" if has_execution else "workspace" if has_write else "read_only"
        )
        agent_config = copy.deepcopy(self.runtime_config)
        agent_config.update({
            "orchestration_role": role,
            "permissions": permissions,
            "provenance": {
                "generated_by_freya": True,
                "orchestration_id": str(orchestration_id),
                "plan_task_id": str(task.get("id") or ""),
                "attempt": attempt,
                "factory_version": self.version,
                "ephemeral": True,
            },
        })
        payload = normalize_agent({
            "name": name,
            "role": role_label,
            "description": identity["description"],
            "instructions": (
                "Follow the Task Analyst operational brief and this plan step. "
                "Use freya-core guidance and obey the capability policy for every action."
            ),
            "enabled": True,
            "tools": tools,
            "skills": assignments,
            "capability_policy": policy,
            "identity": identity,
            "autonomy": {
                "create_files": (
                    "automatic" if "filesystem.create" in required_set else "deny"
                ),
                "modify_files": (
                    "automatic" if required_set & {
                        "filesystem.modify", "filesystem.overwrite",
                    } else "deny"
                ),
                "request_new_capabilities": "deny",
                "request_missing_capabilities": "ask",
            },
            "verification": {
                "completion_criteria": list(task.get("success_criteria", [])),
                "require_tool_evidence": bool(policy_tools),
                "stop_after_acceptance_evidence": bool(
                    characteristics.get("single_case_verification")
                ),
            },
            "config": agent_config,
        }, allow_provenance=True)
        return {
            "payload": payload,
            "warnings": warnings,
            "skill_omissions": skill_omissions,
            "skill_ids": [item["skill_id"] for item in assignments],
            "required_capabilities": list(dict.fromkeys(required)),
            "effective_tools": policy_tools,
            "declared_tools": tools,
            "role": role,
            "factory_version": self.version,
        }

    def create(self, task: dict[str, Any], *, orchestration_id: str,
               attempt: int = 1, variant: int = 0) -> dict[str, Any]:
        built = self.build(
            task, orchestration_id=orchestration_id, attempt=attempt,
            variant=variant,
        )
        agent = self.store.create_agent(built["payload"])
        return {**built, "agent": agent}
