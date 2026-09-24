"""Deterministic planning catalog assembled from Freya's live registries.

The catalog describes what the runtime implements. It is not an authorization
grant: worker actions still pass through the capability policy engine.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

from .capabilities import capability_catalog
from .skills import BUILTIN_SKILLS
from .tools import Toolbox


class UnsupportedResourceRequirement(ValueError):
    """A plan references an unknown resource or declares an unsupported need."""

    def __init__(self, resource_type: str, unknown_resource_id: str = "", *,
                 semantic_need: str = "", reason: str = ""):
        self.resource_type = str(resource_type or "resource")
        self.unknown_resource_id = str(unknown_resource_id or "")
        self.semantic_need = str(semantic_need or "")
        self.reason = str(reason or "")
        detail = (
            f"Unsupported {self.resource_type} requirement"
            + (f": unknown ID '{self.unknown_resource_id}'" if self.unknown_resource_id else "")
            + (f" for '{self.semantic_need}'" if self.semantic_need else "")
        )
        if self.reason:
            detail += f". {self.reason}"
        elif not self.unknown_resource_id:
            detail += ": no registered runtime resource satisfies the need."
        super().__init__(detail)


class UnknownTool(UnsupportedResourceRequirement):
    """A selected tool ID does not exist in the global worker tool catalog."""

    error_type = "UnknownTool"

    def __init__(self, tool_id: str, *, semantic_need: str = ""):
        super().__init__(
            "tool", tool_id, semantic_need=semantic_need,
            reason="The tool ID is not registered in the global worker tool catalog.",
        )


class ToolCapabilityMismatch(UnsupportedResourceRequirement):
    """A registered tool cannot satisfy any capability declared by a task."""

    error_type = "ToolCapabilityMismatch"

    def __init__(self, tool_id: str, required_capabilities: Iterable[str], *,
                 compatible_capabilities: Iterable[str] = (), semantic_need: str = ""):
        self.tool_id = str(tool_id or "")
        self.required_capabilities = sorted({str(item) for item in required_capabilities})
        self.compatible_capabilities = sorted({str(item) for item in compatible_capabilities})
        requested = ", ".join(self.required_capabilities) or "none"
        compatible = ", ".join(self.compatible_capabilities) or "none"
        super().__init__(
            "tool_capability_mismatch", semantic_need=semantic_need,
            reason=(f"ToolCapabilityMismatch: registered tool '{self.tool_id}' does not support the declared "
                    f"capabilities ({requested}); compatible capabilities: {compatible}."),
        )


class RuntimeResourceCatalog:
    """A fresh, serializable view of capabilities, worker tools, and Skills."""

    def __init__(self, capabilities: Iterable[dict[str, Any]],
                 tools: Iterable[dict[str, Any]], skills: Iterable[dict[str, Any]]):
        capability_records, tool_records, skill_records = (
            list(capabilities), list(tools), list(skills))
        self.capabilities = self._ordered(capability_records)
        self.tools = self._ordered(tool_records)
        self.skills = self._ordered(skill_records)
        for resource_type, records in (("capability", capability_records),
                                       ("tool", tool_records), ("skill", skill_records)):
            ids = [item.get("id") for item in records if isinstance(item, dict)
                   and isinstance(item.get("id"), str) and item.get("id")]
            if len(ids) != len(set(ids)):
                raise ValueError(f"The {resource_type} registry contains duplicate IDs.")
        self._by_type = {
            "capability": {item["id"]: item for item in self.capabilities},
            "tool": {item["id"]: item for item in self.tools},
            "skill": {item["id"]: item for item in self.skills},
        }
        global_tools = Toolbox.tool_catalog()
        self._global_tools = {item["id"]: item for item in global_tools}
        self._global_tool_ids = set(self._global_tools)
        global_capabilities_by_tool: dict[str, set[str]] = {
            tool_id: set() for tool_id in self._global_tool_ids
        }
        self._global_capability_tools: dict[str, str] = {}
        for capability in capability_catalog():
            tool_id = capability.get("tool")
            if isinstance(tool_id, str) and tool_id in global_capabilities_by_tool:
                global_capabilities_by_tool[tool_id].add(capability["id"])
                self._global_capability_tools[capability["id"]] = tool_id
        for tool_id, tool in self._global_tools.items():
            declared = tool.get("capabilities")
            if isinstance(declared, list) and set(declared) != global_capabilities_by_tool[tool_id]:
                raise ValueError(
                    f"Tool catalog capability mapping disagrees with the capability registry: {tool_id}."
                )
        self._capabilities_by_tool = {
            tool_id: sorted(capability_ids)
            for tool_id, capability_ids in global_capabilities_by_tool.items()
        }
        tool_ids = set(self._by_type["tool"])
        missing_tools = sorted({item.get("tool") for item in self.capabilities
                                if item.get("tool") and item.get("tool") not in tool_ids})
        if missing_tools:
            raise ValueError("Capability registry references unregistered tools: "
                             + ", ".join(missing_tools))
        version_payload = {
            "capabilities": self.capabilities,
            "tools": self.tools,
            "skills": self.skills,
        }
        canonical = json.dumps(version_payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        self.version = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _ordered(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [copy.deepcopy(item) for item in items if isinstance(item, dict)
                      and isinstance(item.get("id"), str) and item["id"]]
        normalized.sort(key=lambda item: item["id"])
        return normalized

    @classmethod
    def build(cls, skills: Iterable[dict[str, Any]] | None = None) -> "RuntimeResourceCatalog":
        """Derive catalog contents from the capability, Toolbox, and Skill registries."""
        skill_records = BUILTIN_SKILLS if skills is None else skills
        skill_summaries = []
        for skill in skill_records:
            if not isinstance(skill, dict) or skill.get("enabled", True) is not True:
                continue
            metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
            description = str(skill.get("description") or "").strip()
            use_when = metadata.get("use_when")
            if not isinstance(use_when, str) or not use_when.strip():
                use_when = description
            aliases = metadata.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            skill_summaries.append({
                "id": skill["id"],
                "name": str(skill.get("name") or skill["id"]),
                "description": description,
                "use_when": str(use_when).strip(),
                "category": str(skill.get("category") or "General"),
                "tags": [item for item in skill.get("tags", []) if isinstance(item, str)],
                "required_capabilities": [item for item in skill.get("required_capabilities", [])
                                           if isinstance(item, str)],
                "aliases": [item for item in aliases if isinstance(item, str) and item.strip()],
            })
        return cls(capability_catalog(), Toolbox.tool_catalog(), skill_summaries)

    @classmethod
    def from_context(cls, context: dict[str, Any] | None) -> "RuntimeResourceCatalog":
        """Use global capability/tool registries and per-run enabled Skills."""
        context = context if isinstance(context, dict) else {}
        skills = context.get("skills")
        registered_capabilities = capability_catalog()
        registered_tools = Toolbox.tool_catalog()
        if not isinstance(skills, list):
            skills = BUILTIN_SKILLS
        return cls(registered_capabilities, registered_tools, skills)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_catalog_version": self.version,
            "capabilities": copy.deepcopy(self.capabilities),
            "tools": copy.deepcopy(self.tools),
            "skills": copy.deepcopy(self.skills),
        }

    def ids(self, resource_type: str) -> list[str]:
        return sorted(self._by_type[resource_type])

    def resolve(self, resource_type: str, reference: str) -> str | None:
        """Resolve an exact ID or one explicitly declared, unambiguous alias."""
        records = self._by_type.get(resource_type)
        if records is None or not isinstance(reference, str):
            return None
        token = reference.strip()
        if token in records:
            return token
        matches = [item["id"] for item in records.values()
                   if token and token in item.get("aliases", [])]
        return matches[0] if len(matches) == 1 else None

    def _resolve_global_tool(self, reference: str, *, allow_aliases: bool) -> str | None:
        if not isinstance(reference, str):
            return None
        token = reference.strip()
        if token in self._global_tools:
            return token
        if not allow_aliases or not token:
            return None
        matches = [tool_id for tool_id, tool in self._global_tools.items()
                   if token in tool.get("aliases", [])]
        return matches[0] if len(matches) == 1 else None

    def capabilities_for_tool(self, tool_id: str) -> list[str]:
        """Return the capabilities linked to a globally registered tool."""
        if tool_id not in self._global_tool_ids:
            raise UnknownTool(tool_id)
        return list(self._capabilities_by_tool.get(tool_id, []))

    def tools_for_capabilities(self, capability_ids: Iterable[str]) -> list[str]:
        """Resolve required tool transports from registered capability records."""
        tools = []
        for capability_id in capability_ids:
            tool_id = self._global_capability_tools.get(capability_id)
            if isinstance(tool_id, str) and tool_id and tool_id not in tools:
                tools.append(tool_id)
        return tools

    def validate_semantic_plan(self, value: Any, *, allow_aliases: bool = True) -> dict[str, Any]:
        """Validate and canonicalize resource references before PlanCompiler."""
        if not isinstance(value, dict):
            raise ValueError("Semantic plan must be an object.")
        plan = copy.deepcopy(value)
        unsupported = plan.get("unsupported_requirements", [])
        if not isinstance(unsupported, list):
            raise ValueError("unsupported_requirements must be a list.")
        if unsupported:
            item = unsupported[0]
            if not isinstance(item, dict):
                raise ValueError("unsupported_requirements entries must be objects.")
            raise UnsupportedResourceRequirement(
                item.get("resource_type", "resource"), item.get("resource_id", ""),
                semantic_need=item.get("semantic_need", ""),
                reason=item.get("reason", "No available runtime resource satisfies this requirement."),
            )
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("Semantic plan must contain tasks.")
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ValueError("Semantic tasks must be objects.")
            needs = task.get("semantic_needs", [])
            if not isinstance(needs, list) or any(not isinstance(item, str) or not item.strip()
                                                   for item in needs):
                raise ValueError(f"Semantic task {index} semantic_needs must be a list of non-empty strings.")
            if "semantic_needs" in task:
                task["semantic_needs"] = list(dict.fromkeys(needs))
            references = (
                ("required_capabilities", "capability"),
                ("required_tools", "tool"),
                ("preferred_skills", "skill"),
            )
            for field, resource_type in references:
                if field not in task:
                    continue
                requested = task.get(field, [])
                if not isinstance(requested, list):
                    raise ValueError(f"Semantic task {index} {field} must be a list.")
                normalized = []
                for reference in requested:
                    if not isinstance(reference, str) or not reference.strip():
                        raise ValueError(f"Semantic task {index} {field} entries must be non-empty strings.")
                    canonical = (self._resolve_global_tool(reference, allow_aliases=allow_aliases)
                                 if resource_type == "tool" else
                                 self.resolve(resource_type, reference) if allow_aliases else
                                 reference if reference in self._by_type[resource_type] else None)
                    if canonical is None:
                        semantic_need = needs[0] if len(needs) == 1 else ""
                        if resource_type == "tool":
                            raise UnknownTool(reference, semantic_need=semantic_need)
                        raise UnsupportedResourceRequirement(
                            resource_type, reference, semantic_need=semantic_need,
                            reason="No exact ID or unique declared alias exists in the runtime catalog.",
                        )
                    if canonical not in normalized:
                        normalized.append(canonical)
                task[field] = normalized
            required_tools = set(task.get("required_tools", []))
            if required_tools:
                requested_capabilities = list(task.get("required_capabilities", []))
                derived_capabilities = list(dict.fromkeys(
                    capability_id
                    for tool_id in task["required_tools"]
                    for capability_id in self.capabilities_for_tool(tool_id)
                ))
                if requested_capabilities:
                    mismatched = [
                        tool_id for tool_id in task["required_tools"]
                        if not set(self.capabilities_for_tool(tool_id)) & set(requested_capabilities)
                    ]
                    if mismatched:
                        tool_id = sorted(mismatched)[0]
                        raise ToolCapabilityMismatch(
                            tool_id, requested_capabilities,
                            compatible_capabilities=self.capabilities_for_tool(tool_id),
                            semantic_need=needs[0] if len(needs) == 1 else "",
                        )
                else:
                    task["required_capabilities"] = derived_capabilities
        plan.pop("unsupported_requirements", None)
        return plan


def planner_resource_context(skills: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a compact planning view, excluding Skill instructions and procedures."""
    return RuntimeResourceCatalog.build(skills).as_dict()
