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


AGENT_FACTORY_VERSION = 1
MAX_FACTORY_SKILLS = 8


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _tokens(value: Any) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9_+-]{3,}", str(value or "").casefold())}


def _skill_required(skill: dict[str, Any]) -> set[str]:
    return {str(item) for item in skill.get("required_capabilities", [])
            if isinstance(item, str)}


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
        preferred = {_slug(item) for item in task.get("preferred_skills", [])}
        text = " ".join(
            str(task.get(key) or "") for key in ("objective", "description")
        ).casefold()
        if ("code-review" in preferred
                or re.search(r"\b(?:code audit|code auditor|code review)\b", text)):
            return "auditor"
        if ("interactive-testing" in preferred
                or re.search(r"\b(?:qa|quality assurance|interactive test)\b", text)):
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
                      *, variant: int = 0) -> tuple[list[dict[str, Any]], list[str]]:
        """Select up to eight enabled existing Skills without changing policy."""
        available = [
            item for item in skills
            if isinstance(item, dict) and item.get("enabled") is True
            and isinstance(item.get("id"), str)
        ]
        available.sort(key=lambda item: (
            str(item.get("id")).casefold(), str(item.get("name") or "").casefold()
        ))
        by_id = {str(item["id"]).casefold(): item for item in available}
        by_normalized: dict[str, dict[str, Any]] = {}
        for item in available:
            for key in (_slug(item.get("id")), _slug(item.get("name"))):
                if key:
                    by_normalized.setdefault(key, item)

        required = {str(item) for item in task.get("required_capabilities", [])}
        selected: list[tuple[dict[str, Any], int]] = []
        selected_ids: set[str] = set()
        warnings: list[str] = []

        def add(skill: dict[str, Any], priority: int) -> None:
            skill_id = str(skill["id"])
            if skill_id not in selected_ids and len(selected) < self.max_skills:
                selected_ids.add(skill_id)
                selected.append((skill, priority))

        preferred = [
            str(item).strip() for item in task.get("preferred_skills", [])
            if str(item).strip()
        ]
        for index, requested in enumerate(preferred):
            skill = by_id.get(requested.casefold())
            match_priority = 1000 - index
            if skill is None:
                skill = by_normalized.get(_slug(requested))
                match_priority = 900 - index
            if skill is None:
                warnings.append(
                    f"Preferred Skill '{requested}' is not registered and was ignored."
                )
                continue
            missing = sorted(_skill_required(skill) - required)
            if missing:
                if not selected:
                    raise ValueError(
                        f"Primary Skill '{skill['id']}' is incompatible with the planned capability set: "
                        + ", ".join(missing)
                    )
                warnings.append(
                    f"Optional Skill '{skill['id']}' was omitted because the plan lacks: "
                    + ", ".join(missing)
                )
                continue
            add(skill, match_priority)

        task_text = " ".join([
            str(task.get("objective") or ""),
            str(task.get("description") or ""),
            " ".join(preferred),
        ])
        task_tokens = _tokens(task_text)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for skill in available:
            if str(skill["id"]) in selected_ids or not _skill_required(skill) <= required:
                continue
            metadata = " ".join([
                str(skill.get("name") or ""),
                str(skill.get("category") or ""),
                *[str(item) for item in skill.get("tags", [])],
            ])
            score = len(task_tokens & _tokens(metadata))
            if score:
                scored.append((score, str(skill["id"]), skill))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if variant and len(scored) > 1:
            offset = variant % len(scored)
            scored = scored[offset:] + scored[:offset]
        for score, _, skill in scored:
            add(skill, 100 + score)

        role = self.orchestration_role(task)
        fallback_ids = {
            "qa": ("interactive-testing", "software-testing"),
            "auditor": ("code-review",),
            "worker": (("simple-file-artifact",) if self.task_kind(task) == "file_creation"
                       else ("python-development", "debugging")),
        }[role]
        for fallback_id in fallback_ids:
            skill = by_id.get(fallback_id)
            if skill is not None and _skill_required(skill) <= required:
                add(skill, 10)

        return (
            [{"skill_id": skill["id"], "priority": priority}
             for skill, priority in selected],
            warnings,
        )

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
        required = list(task.get("required_capabilities", []))
        if (not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)):
            raise ValueError("required_capabilities must be a list of strings.")
        recovery_state = task.get("_recovery_workspace_state")
        if recovery_state is not None and "filesystem.read" not in required:
            # Recovery inspection is a safe, task-derived prerequisite.  It is
            # deliberately narrower than write authority and still passes
            # through the generated policy and selector checks below.
            required.append("filesystem.read")
        role = self.orchestration_role(task)
        write_capabilities = {
            "filesystem.create", "filesystem.modify", "filesystem.overwrite",
        }
        if role in {"qa", "auditor"} and set(required) & write_capabilities:
            raise ValueError(f"Dynamic {role} agents cannot receive write capabilities.")
        policy = self.capability_policy(required)
        tools = list(dict.fromkeys(effective_tools_for_policy(policy)))
        records = (
            list(skills) if skills is not None
            else self.store.list_skills(enabled=True)
        )
        skill_task = dict(task)
        skill_task["required_capabilities"] = list(required)
        assignments, warnings = self.select_skills(
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
                "Use assigned Skills only when compatible with the capability policy."
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
                "require_tool_evidence": bool(tools),
            },
            "config": agent_config,
        }, allow_provenance=True)
        return {
            "payload": payload,
            "warnings": warnings,
            "skill_ids": [item["skill_id"] for item in assignments],
            "required_capabilities": list(dict.fromkeys(required)),
            "effective_tools": tools,
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
