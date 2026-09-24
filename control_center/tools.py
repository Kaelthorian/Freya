"""Restricted filesystem, search, command, and Git tools for the platform."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .security import sanitize


MAX_FILE_BYTES = 512_000
MAX_WRITE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 20_000
MAX_STDIN_CHARS = 16_000
MAX_SEARCH_HITS = 200
IGNORED_DIRECTORIES = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache"}


def _clip(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[output truncated]"


@dataclass
class ToolResult:
    name: str
    output: str
    success: bool
    duration_seconds: float
    exit_code: int | None = None
    capability: str = ""
    policy_decision: str = ""
    policy_reason: str = ""
    executed: bool = False
    error_class: str = ""
    changed: bool | None = None
    already_satisfied: bool = False


class Toolbox:
    """Tools whose read and write paths are confined to a task workspace."""

    # Aliases are declarations in the concrete tool registry. They are kept
    # separate from model-facing JSON schemas and never grant a capability.
    TOOL_ALIASES = {"run_command": ("run_workspace_command",)}

    def __init__(
        self,
        project_root: Path,
        workspace: Path,
        timeout_seconds: int = 30,
    ) -> None:
        self.project_root = project_root.resolve()
        self.workspace = workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.workspace.mkdir(parents=True, exist_ok=True)

    def safe_path(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("A non-empty relative path is required.")
        candidate = Path(path)
        if candidate.is_absolute():
            raise ValueError("Absolute paths are not allowed; use a workspace-relative path.")
        target = (self.workspace / candidate).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            raise ValueError("Path is outside the task workspace: {}".format(path))
        return target

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self.schema_catalog()

    @classmethod
    def schema_catalog(cls) -> list[dict[str, Any]]:
        """Return the schemas used by workers without creating a workspace."""
        return [
            cls._schema(
                "list_files",
                "List files available in the task workspace.",
                {"path": {"type": "string", "description": "Optional relative directory (default: workspace root)."}},
                [],
            ),
            cls._schema(
                "read_file",
                "Read a UTF-8 text file from the task workspace.",
                {"path": {"type": "string", "description": "Workspace-relative file path."}},
                ["path"],
            ),
            cls._schema(
                "write_file",
                "Create or overwrite a UTF-8 text file in the task workspace.",
                {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                    "content": {"type": "string", "description": "Complete file contents."},
                },
                ["path", "content"],
            ),
            cls._schema(
                "edit_file",
                "Replace one exact, unique text fragment in a workspace file.",
                {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "Exact existing text; must occur once."},
                    "new": {"type": "string", "description": "Replacement text."},
                },
                ["path", "old", "new"],
            ),
            cls._schema(
                "search_code",
                "Search workspace text files for a literal string or regular expression.",
                {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional workspace-relative file or directory."},
                    "regex": {"type": "boolean", "description": "Treat query as a regular expression (default false)."},
                },
                ["query"],
            ),
            cls._schema(
                "run_command",
                "Run a restricted Python test/script, Ruff check, or read-only Git status/diff command. No shell is used. Interactive Python scripts require bounded stdin.",
                {
                    "argv": {"type": "array", "items": {"type": "string"}, "description": "Command and arguments as a list."},
                    "timeout_seconds": {"type": "integer", "description": "Timeout from 1 to 120 seconds (default 30)."},
                    "stdin": {"type": "string", "description": "Optional newline-delimited input for a Python program (maximum 16000 characters)."},
                },
                ["argv"],
            ),
            cls._schema(
                "git_diff",
                "Show staged, unstaged, and new-file changes under the task workspace.",
                {},
                [],
            ),
        ]

    @classmethod
    def tool_catalog(cls) -> list[dict[str, Any]]:
        """Summarize actual worker tools from the schemas they expose."""
        from .capabilities import CAPABILITIES

        capabilities_by_tool: dict[str, list[str]] = {}
        for capability in CAPABILITIES:
            capabilities_by_tool.setdefault(capability.tool, []).append(capability.id)
        result = []
        for schema in cls.schema_catalog():
            function = schema["function"]
            properties = function.get("parameters", {}).get("properties", {})
            operations = [function["description"]]
            for name, definition in properties.items():
                description = definition.get("description") if isinstance(definition, dict) else None
                operations.append(
                    f"Accepts {name}" + (f": {description}" if isinstance(description, str) and description else ".")
                )
            tool_id = function["name"]
            result.append({
                "id": tool_id,
                "description": function["description"],
                "operations": operations,
                "capabilities": sorted(capabilities_by_tool.get(tool_id, [])),
                "aliases": list(cls.TOOL_ALIASES.get(tool_id, ())),
            })
        return result

    @staticmethod
    def _schema(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        start = time.perf_counter()
        args = arguments or {}
        try:
            handler = getattr(self, "tool_" + name, None)
            if handler is None:
                raise ValueError("Unknown tool: {}".format(name))
            output, success, exit_code = handler(**args)
        except Exception as exc:  # Keep a tool failure observable to the model.
            output, success, exit_code = "ERROR: {}".format(exc), False, None
        error_class = ""
        if name == "git_diff" and not success and "not inside a Git repository" in str(output):
            error_class = "not_applicable"
        if name == "run_command" and not success and "INTERACTIVE_INPUT_REQUIRED" in str(output):
            error_class = "interactive_input_required"
        return ToolResult(
            name=name,
            output=_clip(str(output)),
            success=bool(success),
            duration_seconds=time.perf_counter() - start,
            exit_code=exit_code,
            error_class=error_class,
        )

    def git_repository_available(self) -> bool:
        """Return whether the task workspace is inside a usable Git repository."""
        try:
            self._git_scope()
        except (OSError, ValueError):
            return False
        return True

    def tool_list_files(self, path: str = ".") -> tuple[str, bool, int | None]:
        directory = self.safe_path(path)
        if not directory.exists() or not directory.is_dir():
            raise ValueError("Directory does not exist: {}".format(path))
        found: list[str] = []
        for current, dirs, files in os.walk(str(directory)):
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES)
            for filename in sorted(files):
                file_path = Path(current) / filename
                found.append(str(file_path.relative_to(self.workspace)))
        return ("\n".join(found) if found else "Workspace is empty."), True, 0

    def tool_read_file(self, path: str) -> tuple[str, bool, int | None]:
        target = self.safe_path(path)
        if not target.is_file():
            raise ValueError("File does not exist: {}".format(path))
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File exceeds the 512 KB read limit.")
        return target.read_text(encoding="utf-8"), True, 0

    def tool_write_file(self, path: str, content: str) -> tuple[str, bool, int | None]:
        target = self.safe_path(path)
        if not isinstance(content, str):
            raise ValueError("content must be a string.")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError("File exceeds the 1 MB write limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return "Wrote {} ({} bytes).".format(path, target.stat().st_size), True, 0

    def tool_edit_file(self, path: str, old: str, new: str) -> tuple[str, bool, int | None]:
        target = self.safe_path(path)
        if not target.is_file():
            raise ValueError("File does not exist: {}".format(path))
        if not old:
            raise ValueError("old must not be empty.")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences != 1:
            raise ValueError("Expected the old text exactly once; found {} occurrences.".format(occurrences))
        updated = text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError("Updated file exceeds the 1 MB write limit.")
        target.write_text(updated, encoding="utf-8", newline="")
        return "Updated {}.".format(path), True, 0

    def tool_search_code(
        self,
        query: str,
        path: str = ".",
        regex: bool = False,
    ) -> tuple[str, bool, int | None]:
        if not query:
            raise ValueError("query must not be empty.")
        if len(query) > 2000:
            raise ValueError("query exceeds the 2000 character limit.")
        root = self.safe_path(path)
        if not root.exists():
            raise ValueError("Search path does not exist: {}".format(path))
        pattern = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        files: list[Path] = [root] if root.is_file() else []
        if root.is_dir():
            for current, dirs, names in os.walk(str(root)):
                dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES)
                files.extend(Path(current) / filename for filename in names)
        hits: list[str] = []
        for file_path in sorted(files):
            try:
                file_path = self.safe_path(str(file_path.relative_to(self.workspace)))
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except (ValueError, OSError):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, 1):
                if pattern.search(line):
                    relative = file_path.relative_to(self.workspace).as_posix()
                    hits.append("{}:{}: {}".format(relative, line_number, line.strip()[:300]))
                    if len(hits) >= MAX_SEARCH_HITS:
                        hits.append("[results capped at {} hits]".format(MAX_SEARCH_HITS))
                        return "\n".join(hits), True, 0
        return ("\n".join(hits) if hits else "No matches found."), True, 0

    def tool_run_command(
        self,
        argv: list[str],
        timeout_seconds: int = 30,
        stdin: str | None = None,
    ) -> tuple[str, bool, int | None]:
        if not isinstance(argv, list) or not argv or len(argv) > 40:
            raise ValueError("argv must be a non-empty list with at most 40 entries.")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ValueError("Every argv item must be a string without NUL characters.")
        if any(len(arg) > 4000 for arg in argv):
            raise ValueError("An argv item exceeds the 4000 character limit.")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120.")
        if stdin is not None:
            if not isinstance(stdin, str) or "\x00" in stdin:
                raise ValueError("stdin must be text without NUL characters.")
            if len(stdin) > MAX_STDIN_CHARS:
                raise ValueError("stdin exceeds the 16000 character limit.")

        executable = Path(argv[0]).name.lower()
        command = argv[1:]
        cwd = self.workspace
        normalized: list[str]

        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            if not command:
                raise ValueError("Python command is incomplete.")
            if command[0] == "-m":
                if len(command) < 2:
                    raise ValueError("Python module name is required.")
                module = command[1]
                module_args = command[2:]
                if module == "py_compile":
                    if not module_args:
                        raise ValueError("py_compile requires workspace file paths.")
                    normalized = ["-m", module] + [str(self.safe_path(item)) for item in module_args]
                elif module == "pytest":
                    paths = [item for item in module_args if not item.startswith("-")]
                    for item in paths:
                        self.safe_path(item)
                    normalized = ["-m", module] + (module_args or ["."])
                elif module == "unittest":
                    if not module_args or module_args[0] != "discover":
                        raise ValueError("Only unittest discovery within the workspace is allowed.")
                    normalized = ["-m", module] + self._validate_unittest_discovery(module_args)
                else:
                    raise ValueError("Allowed Python modules: unittest, pytest, py_compile.")
            else:
                script = self.safe_path(command[0])
                if not script.is_file() or script.suffix.lower() != ".py":
                    raise ValueError("Python scripts must be .py files inside the workspace.")
                normalized = [str(script)] + command[1:]
        elif executable in {"git", "git.exe"}:
            if stdin is not None:
                raise ValueError("stdin is only supported for Python commands.")
            git_root, pathspec = self._git_scope()
            if command == ["status", "--short"]:
                normalized = ["-C", str(git_root), "status", "--short", "--", pathspec]
            elif command == ["diff"]:
                normalized = [
                    "-C", str(git_root), "diff", "--no-ext-diff", "--unified=3", "--", pathspec
                ]
            else:
                raise ValueError("Only git status --short and git diff for this workspace are allowed.")
            executable = "git"
            cwd = git_root
        elif executable in {"ruff", "ruff.exe"} and command == ["check", "."]:
            if stdin is not None:
                raise ValueError("stdin is only supported for Python commands.")
            normalized = ["check", "."]
            executable = "ruff"
        else:
            raise ValueError("Command blocked. Allowed commands are workspace Python, Ruff check, and scoped Git status/diff.")

        run_options: dict[str, Any] = {
            "cwd": str(cwd), "capture_output": True, "text": True,
            "encoding": "utf-8", "errors": "replace", "timeout": timeout_seconds,
            "shell": False,
        }
        if stdin is None:
            # A background worker has no human terminal. Closing stdin makes an
            # accidental input() fail immediately instead of consuming the
            # orchestration deadline.
            run_options["stdin"] = subprocess.DEVNULL
        else:
            run_options["input"] = stdin
        result = subprocess.run(
            [sys.executable, *normalized] if executable.startswith("python") or executable == "py" else [executable, *normalized],
            **run_options,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if stdin is None and result.returncode != 0 and "EOFError" in output:
            output = (
                "INTERACTIVE_INPUT_REQUIRED: the program requested terminal input, but workers have no live "
                "terminal. Retry with run_command stdin containing bounded newline-delimited test input.\n\n"
                + output
            )
        return _clip(output or "Command completed with no output."), result.returncode == 0, result.returncode

    def _validate_unittest_discovery(self, args: list[str]) -> list[str]:
        if len(args) < 1 or args[0] != "discover":
            raise ValueError("Only unittest discover is allowed.")
        normalized = ["discover"]
        index = 1
        while index < len(args):
            item = args[index]
            if item == "-s" and index + 1 < len(args):
                normalized.extend([item, str(self.safe_path(args[index + 1]))])
                index += 2
            elif item == "-p" and index + 1 < len(args):
                if Path(args[index + 1]).name != args[index + 1]:
                    raise ValueError("unittest pattern must be a filename pattern.")
                normalized.extend([item, args[index + 1]])
                index += 2
            elif item in {"-v", "-q"}:
                normalized.append(item)
                index += 1
            else:
                raise ValueError("Only -s workspace-path, -p filename-pattern, -v, and -q are allowed.")
        return normalized

    def _git_scope(self) -> tuple[Path, str]:
        try:
            top_level = subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Could not inspect Git repository: {}".format(exc)) from exc
        if top_level.returncode != 0:
            raise ValueError("The workspace is not inside a Git repository.")
        git_root = Path(top_level.stdout.strip()).resolve()
        try:
            pathspec = self.workspace.relative_to(git_root).as_posix() or "."
        except ValueError as exc:
            raise ValueError("Could not scope Git operations to the workspace.") from exc
        return git_root, pathspec

    def tool_git_diff(self) -> tuple[str, bool, int | None]:
        try:
            git_root, repo_path = self._git_scope()
        except ValueError:
            return "The workspace is not inside a Git repository.", False, None

        pieces: list[str] = []
        for args in (
            ["diff", "--no-ext-diff", "--unified=3", "--", repo_path],
            ["diff", "--cached", "--no-ext-diff", "--unified=3", "--", repo_path],
        ):
            result = subprocess.run(
                ["git", "-C", str(git_root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
            if result.returncode not in {0, 1}:
                raise ValueError((result.stderr or "git diff failed").strip())
            if result.stdout.strip():
                pieces.append(result.stdout)

        status = subprocess.run(
            ["git", "-C", str(git_root), "status", "--short", "--untracked-files=all", "--", repo_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        if status.returncode != 0:
            raise ValueError((status.stderr or "git status failed").strip())
        pieces.insert(0, "Git status:\n" + (status.stdout.strip() or "clean") + "\n")
        for status_line in status.stdout.splitlines():
            if status_line.startswith("?? "):
                relative = status_line[3:].strip()
                try:
                    target = (git_root / relative).resolve()
                    target.relative_to(self.workspace)
                    if target.is_file() and target.stat().st_size <= MAX_FILE_BYTES:
                        content = target.read_text(encoding="utf-8").splitlines(keepends=True)
                        pieces.append("".join(difflib.unified_diff([], content, fromfile="/dev/null", tofile=relative)))
                except (ValueError, OSError, UnicodeDecodeError):
                    continue
        return (_clip("\n".join(pieces) if pieces else "No changes under {}.".format(repo_path)), True, 0)


def argument_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep result logs useful without persisting full source contents."""
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in {"content", "old", "new", "stdin"}:
            summary[key] = {"redacted": True, "characters": len(value) if isinstance(value, str) else None}
        else:
            summary[key] = sanitize({key: value}).get(str(key))
    return summary


def output_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
