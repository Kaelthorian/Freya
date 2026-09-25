"""Capability registry and deterministic tool-to-action resolution.

Tools are the transport exposed to Ollama; capabilities are the authorization
model.  This module deliberately contains no policy decisions so it can be
shared by the API, worker and future orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Capability:
    id: str
    category: str
    description: str
    dangerous: bool = False
    tool: str = ""
    actions: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actions"] = list(self.actions)
        value["operations"] = list(self.operations)
        value["aliases"] = list(self.aliases)
        return value


CAPABILITIES: tuple[Capability, ...] = (
    Capability("filesystem.list", "filesystem", "List files in the task workspace", tool="list_files", actions=("path",), operations=("List workspace files and directories.",)),
    Capability("filesystem.read", "filesystem", "Read UTF-8 text files from the task workspace", tool="read_file", actions=("path",), operations=("Read a workspace-relative text file.", "Inspect generated source and output files.")),
    Capability("filesystem.search", "filesystem", "Search text files in the task workspace", tool="search_code", actions=("path", "query"), operations=("Search workspace files by text or regular expression.",)),
    Capability("filesystem.create", "filesystem", "Create a new file in the task workspace", tool="write_file", actions=("path", "content", "extension", "size_bytes"), operations=("Create a workspace-relative file that does not already exist.",)),
    Capability("filesystem.modify", "filesystem", "Replace a specific part of an existing workspace file", tool="edit_file", actions=("path", "old", "new", "extension", "size_bytes"), operations=("Replace one exact, unique text fragment in an existing file.",)),
    Capability("filesystem.overwrite", "filesystem", "Replace the complete contents of an existing workspace file", tool="write_file", actions=("path", "content", "extension", "size_bytes"), operations=("Overwrite an existing workspace-relative file.",)),
    Capability("git.status", "git", "Inspect scoped Git status", dangerous=False, tool="run_command", actions=("argv",), operations=("Run the allowlisted read-only git status --short command in a Git workspace.",)),
    Capability("git.diff", "git", "Inspect scoped Git differences", dangerous=False, tool="git_diff", actions=("workspace",), operations=("Read staged, unstaged, and new-file diffs inside the selected workspace.",)),
    Capability("execution.python_script", "execution", "Run a Python script in the task workspace with bounded input and captured output", dangerous=True, tool="run_command", actions=("argv",), operations=("Execute a Python script from the workspace.", "Provide bounded controlled stdin as newline-delimited input to programs that call input().", "Capture stdout, stderr, and the process exit code."), aliases=("run_python_script", "execute_python_script")),
    Capability("execution.pytest", "execution", "Run pytest against the task workspace", dangerous=True, tool="run_command", actions=("argv",), operations=("Run the allowlisted pytest module command and capture its result.",), aliases=("run_pytest",)),
    Capability("execution.unittest", "execution", "Run unittest discovery against the task workspace", dangerous=True, tool="run_command", actions=("argv",), operations=("Run the allowlisted unittest discovery command and capture its result.",), aliases=("run_unittest",)),
    Capability("execution.py_compile", "execution", "Compile Python files in the task workspace", dangerous=True, tool="run_command", actions=("argv",), operations=("Run Python bytecode compilation for workspace files.",)),
    Capability("execution.ruff", "execution", "Run Ruff checks against the task workspace", dangerous=True, tool="run_command", actions=("argv",), operations=("Run the allowlisted ruff check command and capture its result.",)),
)
CAPABILITY_REGISTRY = {item.id: item for item in CAPABILITIES}

CAPABILITY_TO_TOOL = {item.id: item.tool for item in CAPABILITIES}


def tool_for_capability(capability: str) -> str:
    '''Return the concrete tool required by one registered capability.'''
    item = CAPABILITY_REGISTRY.get(capability)
    return item.tool if item else ""


def effective_tools_for_policy(policy: dict[str, Any] | None) -> list[str]:
    '''Resolve active policy rules to the tools the model may actually see.

    Allow and ask both need a tool transport. Deny deliberately contributes
    nothing, so a stale advanced-tool selection cannot expose an executable
    tool without a corresponding capability rule.
    '''
    if not isinstance(policy, dict):
        return []
    capabilities = policy.get("capabilities", policy)
    if not isinstance(capabilities, dict):
        return []
    active: set[str] = set()
    for category, actions in capabilities.items():
        if not isinstance(actions, dict):
            continue
        for action, rule in actions.items():
            capability = action if "." in str(action) else f"{category}.{action}"
            if isinstance(rule, dict) and rule.get("mode") in {"allow", "ask"}:
                tool = tool_for_capability(capability)
                if tool:
                    active.add(tool)
    return list(dict.fromkeys(item.tool for item in CAPABILITIES if item.tool in active))


class CapabilityResolver:
    """Resolve a concrete tool request to one known capability."""

    def __init__(self, workspace: Path | str | None = None):
        self.workspace = Path(workspace).resolve() if workspace else None

    def resolve(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        args = arguments or {}
        if tool == "read_file":
            return "filesystem.read"
        if tool == "list_files":
            return "filesystem.list"
        if tool == "search_code":
            return "filesystem.search"
        if tool == "edit_file":
            return "filesystem.modify"
        if tool == "git_diff":
            return "git.diff"
        if tool == "write_file":
            path = args.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("write_file requires a path")
            if self.workspace is not None:
                candidate = Path(path)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("Path is outside the task workspace")
                lexical_target = self.workspace / candidate
                for parent in lexical_target.parents:
                    if parent == self.workspace:
                        break
                    try:
                        resolved_parent = parent.resolve()
                    except OSError:
                        continue
                    if (resolved_parent.is_file()
                            and self.workspace in resolved_parent.parents):
                        return "filesystem.create"
                target = lexical_target.resolve()
                if target != self.workspace and self.workspace not in target.parents:
                    raise ValueError("Path is outside the task workspace")
                return "filesystem.overwrite" if target.exists() else "filesystem.create"
            return "filesystem.overwrite" if bool(args.get("exists")) else "filesystem.create"
        if tool == "run_command":
            argv = args.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
                raise ValueError("run_command requires argv")
            executable = Path(argv[0].replace("\\", "/")).name.lower().removesuffix(".exe")
            command = argv[1:]
            if executable in {"python", "python3", "py"} and command[:1] == ["-m"]:
                modules = {"pytest": "execution.pytest", "unittest": "execution.unittest", "py_compile": "execution.py_compile"}
                if len(command) > 1 and command[1] in modules:
                    return modules[command[1]]
                raise ValueError("Unknown or unsupported Python module")
            if executable in {"python", "python3", "py"}:
                if len(command) >= 1 and command[0].lower().endswith(".py"):
                    return "execution.python_script"
                raise ValueError("Python script is required")
            if executable == "ruff" and command == ["check", "."]:
                return "execution.ruff"
            if executable == "git" and command == ["status", "--short"]:
                return "git.status"
            if executable == "git" and command == ["diff"]:
                return "git.diff"
            raise ValueError("Command has no supported capability")
        raise ValueError("Unknown tool: " + str(tool))

    def resolve_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
        return self.resolve(tool, arguments)


def capability_catalog() -> list[dict[str, Any]]:
    return [item.as_dict() for item in CAPABILITIES]
