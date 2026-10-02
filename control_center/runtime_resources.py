"""Deterministic planning catalog assembled from Freya's live registries.

The catalog describes what the runtime implements. It is not an authorization
grant: worker actions still pass through the capability policy engine.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable

from .capabilities import capability_catalog
from .plan_scope import semantic_categories
from .skills import BUILTIN_SKILLS, CORE_SKILL_ID, validate_skill_tools
from .tools import Toolbox


# Planner-facing names describe semantic work. The catalog resolves them to a
# registered capability and then to that capability's concrete tool. Capability
# identifiers and tool identifiers are never Planner authority.
SEMANTIC_OPERATION_CAPABILITIES = {
    "create_file": "filesystem.create",
    "read_file": "filesystem.read",
    "modify_file": "filesystem.modify",
    "overwrite_file": "filesystem.overwrite",
    "list_workspace": "filesystem.list",
    "search_workspace": "filesystem.search",
    "inspect_git_status": "git.status",
    "inspect_git_diff": "git.diff",
    "run_python_script": "execution.python_script",
    "run_pytest": "execution.pytest",
    "run_unittest": "execution.unittest",
    "compile_python": "execution.py_compile",
    "lint_python": "execution.ruff",
}

# An existing-file operation cannot be carried out from project metadata alone.
# This is a runtime requirement, not an extra Planner-authored operation.
OPERATION_PREREQUISITES = {
    "modify_file": ("read_file",),
    "overwrite_file": ("read_file",),
}

SEMANTIC_OPERATION_DESCRIPTIONS = {
    "create_file": "Create a new file in the selected workspace.",
    "read_file": "Read or inspect a file in the selected workspace.",
    "modify_file": "Change a specific part of an existing workspace file.",
    "overwrite_file": "Replace the contents of an existing workspace file.",
    "list_workspace": "List files and directories in the selected workspace.",
    "search_workspace": "Search workspace text by a query.",
    "inspect_git_status": "Inspect the selected Git workspace status.",
    "inspect_git_diff": "Inspect changes in the selected Git workspace.",
    "run_python_script": "Run a Python script in the bounded workspace sandbox.",
    "run_pytest": "Run pytest in the bounded workspace sandbox.",
    "run_unittest": "Run unittest discovery in the bounded workspace sandbox.",
    "compile_python": "Compile Python files in the bounded workspace sandbox.",
    "lint_python": "Run Ruff checks in the bounded workspace sandbox.",
}

TOOL_SEMANTIC_OPERATIONS = {
    "read_file": {"read_file"},
    "edit_file": {"modify_file"},
    "list_files": {"list_workspace"},
    "search_code": {"search_workspace"},
    "git_diff": {"inspect_git_diff"},
    "write_file": {"create_file", "overwrite_file"},
    "run_command": {
        "inspect_git_status", "run_python_script", "run_pytest",
        "run_unittest", "compile_python", "lint_python",
    },
}


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


class UnknownSkill(UnsupportedResourceRequirement):
    error_type = "UnknownSkill"

    def __init__(self, skill_id: str, *, semantic_need: str = ""):
        super().__init__("skill", skill_id, semantic_need=semantic_need,
                         reason="The Skill ID is not registered or uniquely aliased.")


class UnknownCapability(UnsupportedResourceRequirement):
    error_type = "UnknownCapability"

    def __init__(self, capability_id: str, *, semantic_need: str = ""):
        super().__init__("capability", capability_id, semantic_need=semantic_need,
                         reason="The capability ID is not registered or uniquely aliased.")


class UnknownSemanticOperation(UnsupportedResourceRequirement):
    """A semantic operation is not implemented by this runtime."""

    error_type = "UnknownSemanticOperation"

    def __init__(self, operation_id: str, *, semantic_need: str = ""):
        super().__init__("semantic_operation", operation_id, semantic_need=semantic_need,
                         reason="The operation ID is not registered in the runtime catalog.")


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


class AmbiguousToolCapability(UnsupportedResourceRequirement):
    """A tool maps to several capabilities and the task does not distinguish one."""

    error_type = "AmbiguousToolCapability"

    def __init__(self, tool_id: str, compatible_capabilities: Iterable[str]):
        self.tool_id = tool_id
        self.compatible_capabilities = sorted(set(compatible_capabilities))
        super().__init__(
            "tool_capability", tool_id,
            reason=("The tool maps to multiple capabilities ("
                    + ", ".join(self.compatible_capabilities)
                    + "); declare the required capability or a specific semantic need."),
        )


class RuntimeResourceCatalog:
    """Resolve semantic operations through one runtime resource registry."""

    def __init__(self, capabilities: Iterable[dict[str, Any]],
                 tools: Iterable[dict[str, Any]], skills: Iterable[dict[str, Any]]):
        capability_records, tool_records, skill_records = (
            list(capabilities), list(tools), list(skills))
        self.capabilities = self._ordered(capability_records)
        self.tools = self._ordered(tool_records)
        self.skills = self._ordered(skill_records)
        self.semantic_operations = [
            {"id": operation_id, "description": SEMANTIC_OPERATION_DESCRIPTIONS[operation_id]}
            for operation_id in SEMANTIC_OPERATION_CAPABILITIES
        ]
        self.preferred_skill_warnings: list[dict[str, Any]] = []
        self.resource_resolutions: list[dict[str, Any]] = []
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
            "operation": {item["id"]: item for item in self.semantic_operations},
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
        for operation_id, capability_id in SEMANTIC_OPERATION_CAPABILITIES.items():
            if capability_id not in self._global_capability_tools:
                raise ValueError(
                    f"Semantic operation {operation_id} references an unregistered capability."
                )
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
            "semantic_operations": self.semantic_operations,
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
        skill_records = list(BUILTIN_SKILLS if skills is None else skills)
        if not any(isinstance(item, dict) and item.get("id") == CORE_SKILL_ID for item in skill_records):
            skill_records.extend(BUILTIN_SKILLS)
        skill_summaries = []
        for skill in skill_records:
            if not isinstance(skill, dict) or skill.get("enabled", True) is not True or skill.get("id") != CORE_SKILL_ID:
                continue
            declared_tools = validate_skill_tools(skill)
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
                "tools": declared_tools,
                "required_capabilities": [item for item in skill.get("required_capabilities", [])
                                           if isinstance(item, str)],
                "recommended_capabilities": [item for item in skill.get("recommended_capabilities", [])
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
        return cls(registered_capabilities, registered_tools, cls.build(skills).skills)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_catalog_version": self.version,
            "capabilities": copy.deepcopy(self.capabilities),
            "tools": copy.deepcopy(self.tools),
            "skills": copy.deepcopy(self.skills),
            "semantic_operations": copy.deepcopy(self.semantic_operations),
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

    @staticmethod
    def _task_text(task: dict[str, Any]) -> str:
        parts = [str(task.get("objective") or ""), str(task.get("description") or "")]
        needs = task.get("semantic_needs", [])
        if isinstance(needs, list):
            parts.extend(str(item) for item in needs)
        return " ".join(parts).casefold()

    @classmethod
    def _infer_semantic_operations(cls, task: dict[str, Any]) -> list[str]:
        """Infer only explicit, recognizable local work from Planner semantics."""
        text = cls._task_text(task)
        if "external_action" in semantic_categories(text):
            return []
        artifact = bool(re.search(
            r"\b(file|source|script|program|artifact|website|web\s+page|web\s+calculator|"
            r"ui|html|archivo|c[oó]digo|programa|sitio\s+web|p[aá]gina)\b|"
            r"(?<![\w])[a-z0-9_-]+(?:/[a-z0-9_-]+)*\.[a-z0-9]{1,10}(?![\w])", text,
        ))
        result: list[str] = []
        if re.search(r"\b(overwrite|replace existing|rewrite|sobrescribir|"
                     r"reemplazar existente|reescribir)\b", text) and artifact:
            result.append("overwrite_file")
        elif re.search(r"\b(modify|edit|change|patch|fix|update|refactor|"
                       r"modificar|editar|cambiar|corregir|actualizar|parchear)\b", text) and artifact:
            result.append("modify_file")
        elif re.search(r"\b(create|generate|write|save|build|crear|generar|"
                       r"escribir|guardar|construir)\b", text) and artifact:
            result.append("create_file")
        if re.search(r"\b(read|inspect|review|leer|inspeccionar|revisar)\b", text) and artifact:
            result.append("read_file")
        if re.search(r"\b(list|enumerate|listar|enumerar)\b", text) and re.search(
                r"\b(files?|directories|archivos?|directorios?)\b", text):
            result.append("list_workspace")
        if re.search(r"\b(search|find|buscar|encontrar)\b", text) and re.search(
                r"\b(code|text|files?|c[oó]digo|texto|archivos?)\b", text):
            result.append("search_workspace")
        if re.search(r"\bgit\s+status\b", text):
            result.append("inspect_git_status")
        if re.search(r"\bgit\s+diff\b", text):
            result.append("inspect_git_diff")
        if re.search(r"\bpytest\b", text):
            result.append("run_pytest")
        if re.search(r"\bunittest\b", text):
            result.append("run_unittest")
        if re.search(r"\b(py_compile|bytecode compil|compile[- ]check python|"
                     r"compile python|compilar python)\b", text):
            result.append("compile_python")
        if re.search(r"\bruff\b", text):
            result.append("lint_python")
        if (re.search(r"\b(run|execute|start|ejecut\w*|correr|probar)\b", text)
                and re.search(r"\b(python\s+(script|program|file)|script\s+de\s+python|"
                              r"programa\s+python|archivo\s+python|bounded\s+stdin)\b|\.py\b", text)):
            result.append("run_python_script")
        return list(dict.fromkeys(result))

    def _tool_hints(self, task: dict[str, Any], *, allow_aliases: bool) -> list[str]:
        requested = task.get("required_tools", [])
        if not isinstance(requested, list):
            raise ValueError("Semantic task required_tools must be a list of strings.")
        resolved = []
        for reference in requested:
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("Semantic task required_tools entries must be non-empty strings.")
            canonical = self._resolve_global_tool(reference, allow_aliases=allow_aliases)
            if canonical is None:
                need = next(iter(task.get("semantic_needs", [])), "")
                raise UnknownTool(reference, semantic_need=str(need))
            if canonical not in resolved:
                resolved.append(canonical)
        return resolved

    def resources_for_operations(self, operation_ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Derive registered capabilities and tools from semantic operation IDs."""
        operations = list(dict.fromkeys(operation_ids))
        expanded = []
        for operation_id in operations:
            expanded.append(operation_id)
            expanded.extend(OPERATION_PREREQUISITES.get(operation_id, ()))
        capabilities = []
        for operation_id in expanded:
            capability_id = SEMANTIC_OPERATION_CAPABILITIES.get(operation_id)
            if capability_id is None:
                raise UnknownSemanticOperation(operation_id)
            if capability_id not in self._global_capability_tools:
                raise UnknownCapability(capability_id)
            if capability_id not in capabilities:
                capabilities.append(capability_id)
        return capabilities, self.tools_for_capabilities(capabilities)

    def preferred_skill_warnings_for_tasks(self, tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Planner Skill preferences are ignored under the single-Skill model."""
        warnings = []
        for index, task in enumerate(tasks, 1):
            if task.get("preferred_skills") and task["preferred_skills"] != [CORE_SKILL_ID]:
                warnings.append({"task_id": str(task.get("id") or f"task-{index}"),
                                 "message": "planner skill preference ignored; freya-core selected"})
        return warnings

    def tools_for_capabilities(self, capability_ids: Iterable[str]) -> list[str]:
        """Resolve required tool transports from registered capability records."""
        tools = []
        for capability_id in capability_ids:
            tool_id = self._global_capability_tools.get(capability_id)
            if isinstance(tool_id, str) and tool_id and tool_id not in tools:
                tools.append(tool_id)
        return tools

    def validate_semantic_plan(self, value: Any, *, allow_aliases: bool = True) -> dict[str, Any]:
        """Normalize semantic operations while treating legacy resource fields as hints."""
        if not isinstance(value, dict):
            raise ValueError("Semantic plan must be an object.")
        plan = copy.deepcopy(value)
        self.resource_resolutions = []
        unsupported = plan.get("unsupported_requirements", [])
        if not isinstance(unsupported, list):
            raise ValueError("unsupported_requirements must be a list.")
        if any(not isinstance(item, dict) for item in unsupported):
            raise ValueError("unsupported_requirements entries must be objects.")
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
            task["semantic_needs"] = list(dict.fromkeys(needs))
            task_key = str(task.get("key") or task.get("objective") or index)
            requested_skills = task.get("preferred_skills", [])
            if requested_skills and requested_skills != [CORE_SKILL_ID]:
                warning = {"task_id": str(task.get("id") or f"task-{index + 1}"),
                           "message": "planner skill preference ignored; freya-core selected"}
                if warning not in self.preferred_skill_warnings:
                    self.preferred_skill_warnings.append(warning)
            raw_operations = task.get("operations", task.get("semantic_operations", []))
            if ("operations" in task and "semantic_operations" in task
                    and task["operations"] != task["semantic_operations"]):
                raise ValueError("operations and semantic_operations must not contradict each other.")
            if not isinstance(raw_operations, list) or any(
                    not isinstance(item, str) or not item.strip() for item in raw_operations):
                raise ValueError(f"Semantic task {index} operations must be a list of operation IDs.")
            if len(raw_operations) > len(SEMANTIC_OPERATION_CAPABILITIES):
                raise ValueError(f"Semantic task {index} has too many operations.")
            operations = []
            for operation_id in raw_operations:
                canonical = self.resolve("operation", operation_id) if allow_aliases else (
                    operation_id if operation_id in self._by_type["operation"] else None
                )
                if canonical is None:
                    raise UnknownSemanticOperation(operation_id,
                        semantic_need=needs[0] if len(needs) == 1 else "")
                if canonical not in operations:
                    operations.append(canonical)

            tool_hints = self._tool_hints(task, allow_aliases=allow_aliases)
            if not operations:
                operations = self._infer_semantic_operations(task)
            semantic_categories_found = semantic_categories(self._task_text(task))
            if (not operations and semantic_categories_found.intersection({
                    "external_action", "deployment", "publication", "network_action"})):
                semantic_need = (needs[0] if needs else str(task.get("objective") or ""))
                raise UnsupportedResourceRequirement(
                    "semantic_operation", semantic_need=semantic_need,
                    reason="No registered local runtime operation can perform this requested action.",
                )
            if not operations and tool_hints:
                for tool_id in tool_hints:
                    candidates = [operation_id for operation_id in
                                  TOOL_SEMANTIC_OPERATIONS.get(tool_id, set())
                                  if self._global_capability_tools.get(
                                      SEMANTIC_OPERATION_CAPABILITIES[operation_id]) == tool_id]
                    if len(candidates) != 1:
                        raise AmbiguousToolCapability(tool_id, self.capabilities_for_tool(tool_id))
                    if candidates[0] not in operations:
                        operations.append(candidates[0])

            _, derived_tools = self.resources_for_operations(operations)
            for operation_id in operations:
                capability_id = SEMANTIC_OPERATION_CAPABILITIES[operation_id]
                tool_id = self._global_capability_tools[capability_id]
                self.resource_resolutions.append({
                    "task_key": task_key,
                    "semantic_operation": operation_id,
                    "semantic_needs": list(needs),
                    "semantic_source": "planner_semantics",
                    "resolved_capability": capability_id,
                    "resolved_tool": tool_id,
                    "resolution_source": "runtime_catalog",
                })
            for tool_id in tool_hints:
                if tool_id not in derived_tools:
                    self.resource_resolutions.append({
                        "task_key": task_key, "action": "planner_tool_hint_ignored",
                        "declared_tool": tool_id, "reason": "not_derived_from_semantic_operations",
                    })
            declared_capabilities = task.get("required_capabilities", [])
            if "required_capabilities" in task:
                if not isinstance(declared_capabilities, list) or any(
                        not isinstance(item, str) for item in declared_capabilities):
                    raise ValueError(
                        f"Semantic task {index} required_capabilities must be a list of strings."
                    )
                self.resource_resolutions.append({
                    "task_key": task_key, "action": "planner_capability_declaration_ignored",
                    "declared_count": len(declared_capabilities),
                    "reason": "capabilities_are_compiler_derived",
                })
            task["operations"] = operations
            task.pop("required_capabilities", None)
            task.pop("required_tools", None)
            task.pop("preferred_skills", None)
        # Preserve warnings recorded before replacing Planner preferences.
        plan.pop("unsupported_requirements", None)
        return plan


def planner_resource_context(skills: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a compact planning view, excluding Skill instructions and procedures."""
    return RuntimeResourceCatalog.build(skills).as_dict()
