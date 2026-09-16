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

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actions"] = list(self.actions)
        return value


CAPABILITIES: tuple[Capability, ...] = (
    Capability("filesystem.list", "filesystem", "List files in the workspace", tool="list_files", actions=("path",)),
    Capability("filesystem.read", "filesystem", "Read a file in the workspace", tool="read_file", actions=("path",)),
    Capability("filesystem.search", "filesystem", "Search text in the workspace", tool="search_code", actions=("path", "query")),
    Capability("filesystem.create", "filesystem", "Create a new file", tool="write_file", actions=("path", "content", "extension", "size_bytes")),
    Capability("filesystem.modify", "filesystem", "Modify an existing file", tool="edit_file", actions=("path", "old", "new", "extension", "size_bytes")),
    Capability("filesystem.overwrite", "filesystem", "Overwrite an existing file", tool="write_file", actions=("path", "content", "extension", "size_bytes")),
    Capability("git.status", "git", "Inspect scoped Git status", dangerous=False, tool="run_command", actions=("argv",)),
    Capability("git.diff", "git", "Inspect scoped Git differences", dangerous=False, tool="git_diff", actions=("workspace",)),
    Capability("execution.python_script", "execution", "Run a workspace Python script", dangerous=True, tool="run_command", actions=("argv",)),
    Capability("execution.pytest", "execution", "Run pytest in the workspace", dangerous=True, tool="run_command", actions=("argv",)),
    Capability("execution.unittest", "execution", "Run unittest discovery", dangerous=True, tool="run_command", actions=("argv",)),
    Capability("execution.py_compile", "execution", "Compile workspace Python files", dangerous=True, tool="run_command", actions=("argv",)),
    Capability("execution.ruff", "execution", "Run Ruff checks", dangerous=True, tool="run_command", actions=("argv",)),
)
CAPABILITY_REGISTRY = {item.id: item for item in CAPABILITIES}


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
                target = (self.workspace / candidate).resolve()
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
