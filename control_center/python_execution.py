"""Lazy orchestration-owned Python environments (dependency isolation, not a sandbox)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .security import sanitize

INFRASTRUCTURE_ERRORS = {"ENVIRONMENT_UNAVAILABLE", "PYTHON_NOT_AVAILABLE",
                         "DEPENDENCY_SETUP_FAILED", "POLICY_DENIED", "SandboxUnavailable"}
TERMINAL = {"Success", "Failed", "Cancelled"}
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


class ExecutionFailure(RuntimeError):
    def __init__(self, error_class, message, *, stdout="", stderr="", program_started=False):
        super().__init__(message)
        self.error_class = error_class
        self.stdout, self.stderr = stdout, stderr
        self.program_started = program_started


@lru_cache(maxsize=1)
def resolve_host_python() -> tuple[str, str]:
    """Resolve once in each process; never use PATH for subsequent executions."""
    candidates = [sys.executable] + [shutil.which(name) for name in ("py", "python", "python3")]
    for candidate in dict.fromkeys(filter(None, candidates)):
        try:
            result = subprocess.run([candidate, "-I", "-c",
                "import sys,json; print(json.dumps([sys.executable, list(sys.version_info[:3])]))"],
                capture_output=True, text=True, timeout=10, shell=False)
            executable, version = json.loads(result.stdout)
            if result.returncode == 0 and tuple(version) >= (3, 10) and Path(executable).is_file():
                return str(Path(executable).resolve()), ".".join(map(str, version))
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    raise ExecutionFailure("PYTHON_NOT_AVAILABLE", "Python 3.10+ is unavailable")


def _environment() -> dict[str, str]:
    # Avoid forwarding provider credentials, PYTHONPATH or user site packages.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
    return env


def controlled_process(argv, cwd, timeout, stdin=None):
    """One process per invocation; terminate the whole process tree on timeout."""
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, env=_environment(),
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        raise ExecutionFailure("ENVIRONMENT_UNAVAILABLE", str(exc)) from exc
    try:
        stdout, stderr = process.communicate((stdin or "").encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, shell=False)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        stdout, stderr = process.communicate()
        raise ExecutionFailure("PROCESS_TIMEOUT", "Process exceeded execution timeout",
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"), program_started=True) from exc
    return subprocess.CompletedProcess(argv, process.returncode,
        stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"))


class ExecutionBackend(ABC):
    @abstractmethod
    def run(self, workspace, argv, timeout, stdin=None):
        """Execute normalized, policy-approved argv."""


class PythonVenvBackend(ExecutionBackend):
    def __init__(self, runtime_root: Path, orchestration_id: str, emit=None, dependencies=()):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", orchestration_id):
            raise ValueError("Invalid runtime environment owner ID")
        self.root = Path(runtime_root).resolve()
        self.owner = orchestration_id
        self.directory = self.root / orchestration_id
        self.python = self.directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.emit = emit or (lambda event: None)
        self.dependencies = tuple(dependencies)

    def event(self, suffix, **fields):
        event = sanitize({"event_type": "python_env." + suffix, "backend": "venv",
            "orchestration_id": self.owner, "environment_id": self.owner, **fields})
        try:
            self.emit(event)
        except Exception:
            # Observability cannot prevent process termination or infrastructure cleanup.
            print(json.dumps(event), flush=True)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_root = self.root / ".locks"
        lock_root.mkdir(exist_ok=True)
        path = lock_root / (self.owner + ".lock")
        with _guard:
            lock = _locks.setdefault(str(path), threading.RLock())
        with lock, path.open("a+b") as stream:
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def read_state(self):
        path = self.directory / "environment.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(state, dict) or not isinstance(state.get("python_version"), str):
                raise ValueError("Invalid environment metadata")
            return state
        except (OSError, ValueError) as exc:
            raise ExecutionFailure("ENVIRONMENT_UNAVAILABLE", "Python environment metadata is unavailable") from exc

    def save_state(self, state):
        temporary = self.directory / "environment.tmp"
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.directory / "environment.json")

    def ensure_python_environment(self):
        if self.python.is_file():
            return self.read_state()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.event("creating")
        started = time.monotonic()
        try:
            host, version = resolve_host_python()
            result = controlled_process([host, "-m", "venv", str(self.directory / "venv")],
                                        self.directory, 120)
            if result.returncode or not self.python.is_file():
                raise ExecutionFailure("ENVIRONMENT_UNAVAILABLE", "venv creation failed",
                                       stdout=result.stdout, stderr=result.stderr)
            state = {"orchestration_id": self.owner, "environment_id": self.owner,
                     "python_path": str(self.python), "host_python": host, "python_version": version,
                     "created_at": time.time(), "dependency_state": {}}
            self.save_state(state)
            self.event("created", python_version=version, duration=time.monotonic() - started)
            return state
        except ExecutionFailure as exc:
            self.event("execution.failed", error_class=exc.error_class, program_started=False)
            raise

    def dependency_sources(self, workspace, argv):
        sources = []
        def manifest_bytes(path, seen=None):
            seen = set() if seen is None else seen
            path = path.resolve()
            if workspace not in path.parents:
                raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Dependency manifest escapes workspace")
            if path in seen:
                return b""
            seen.add(path)
            content = path.read_bytes()
            nested = re.findall(r"(?m)^\s*(?:-r\s*|--requirement[=\s]+|-c\s*|--constraint[=\s]+)([^\s#]+)",
                                content.decode("utf-8", errors="replace"))
            for name in nested:
                content += manifest_bytes(path.parent / name, seen)
            return content
        def manifest(name):
            path = (workspace / name).resolve()
            if workspace not in path.parents:
                raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Dependency manifest escapes workspace")
            if path.is_file():
                try:
                    content = manifest_bytes(path)
                except OSError as exc:
                    raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Missing referenced dependency manifest: " + name) from exc
                sources.append((name, ["-r", name], content))
        manifest("requirements.txt")
        testing = argv[:2] in (["-m", "pytest"], ["-m", "unittest"])
        if testing:
            manifest("requirements-test.txt")
        if testing or argv[:2] == ["-m", "ruff"]:
            manifest("requirements-dev.txt")
        project = workspace / "pyproject.toml"
        # A build-system or standard project table declares an installable project.
        installable = project.is_file() and bool(re.search(
            rb"(?m)^\s*\[(?:build-system|project)\]\s*$", project.read_bytes()))
        legacy = [workspace / name for name in ("setup.py", "setup.cfg")]
        if installable or any(path.is_file() for path in legacy):
            paths = [path for path in [project, *legacy] if path.is_file()]
            extras = []
            if project.is_file():
                table = re.search(r"(?ms)^\s*\[project.optional-dependencies\]\s*\n(.*?)(?=^\s*\[|\Z)",
                                  project.read_text(encoding="utf-8"))
                declared = set(re.findall(r'(?m)^\s*[\"\x27]?([\w-]+)[\"\x27]?\s*=', table[1])) if table else set()
                if testing:
                    extras.extend(name for name in ("test", "tests") if name in declared)
                if testing or argv[:2] == ["-m", "ruff"]:
                    extras.extend(name for name in ("dev",) if name in declared)
            target = ".[" + ",".join(extras) + "]" if extras else "."
            sources.append(("project:" + target, [target], b"".join(path.read_bytes() for path in paths)))
        # Explicit runtime-context declarations only; never infer packages from imports.
        for dependency in self.dependencies:
            if not isinstance(dependency, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~+-]*", dependency):
                raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Invalid explicit Python dependency")
            sources.append(("explicit:" + dependency, [dependency], dependency.encode()))
        module = argv[1] if len(argv) > 1 and argv[0] == "-m" else ""
        if module in {"pytest", "ruff"}:
            sources.append(("execution-tool:" + module, [module], module.encode()))
        return sources

    def sync_dependencies(self, state, workspace, argv, force=False):
        cache = state.setdefault("dependency_state", {})
        for source, args, content in self.dependency_sources(workspace, argv):
            digest = hashlib.sha256(str(workspace).encode() + content).hexdigest()
            if not force and cache.get(source, {}).get("dependency_manifest_hash") == digest:
                continue
            if source.startswith("execution-tool:"):
                module = source.split(":", 1)[1]
                probe = controlled_process([str(self.python), "-c",
                    "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)", module],
                    self.directory, 10)
                if probe.returncode == 0:
                    cache[source] = {"dependency_manifest_hash": digest, "installed_at": time.time()}
                    self.save_state(state)
                    continue
            fields = {"dependency_source": source, "dependency_manifest_hash": digest}
            self.event("dependencies.started", **fields)
            started = time.monotonic()
            try:
                result = controlled_process([str(self.python), "-m", "pip", "install", *args], workspace, 120)
                if result.returncode:
                    raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Dependency setup failed: " + source,
                                           stdout=result.stdout, stderr=result.stderr)
            except ExecutionFailure as exc:
                self.event("dependencies.failed", **fields, error_class="DEPENDENCY_SETUP_FAILED")
                raise ExecutionFailure("DEPENDENCY_SETUP_FAILED", "Dependency setup failed: " + source,
                                       stdout=exc.stdout, stderr=exc.stderr) from exc
            cache[source] = {"dependency_manifest_hash": digest, "installed_at": time.time()}
            self.save_state(state)
            self.event("dependencies.completed", **fields, duration=time.monotonic() - started)

    def run(self, workspace, argv, timeout, stdin=None):
        try:
            return self._run(workspace, argv, timeout, stdin)
        except (OSError, ValueError, KeyError) as exc:
            self.event("execution.failed", error_class="ENVIRONMENT_UNAVAILABLE", program_started=False)
            raise ExecutionFailure("ENVIRONMENT_UNAVAILABLE", "Python runtime setup failed: " + type(exc).__name__) from exc

    def _run(self, workspace, argv, timeout, stdin=None):
        workspace = Path(workspace).resolve()
        with self.locked():
            state = self.ensure_python_environment()
            self.sync_dependencies(state, workspace, argv)
            self.event("execution.started", python_version=state["python_version"])
            started = time.monotonic()
            try:
                result = controlled_process([str(self.python), *argv], workspace, timeout, stdin)
                # Retry once from declarative sources; never guess PyPI names.
                if result.returncode and "ModuleNotFoundError" in result.stderr and self.dependency_sources(workspace, argv):
                    self.sync_dependencies(state, workspace, argv, force=True)
                    result = controlled_process([str(self.python), *argv], workspace, timeout, stdin)
                self.event("execution.completed" if result.returncode == 0 else "execution.failed",
                           duration=time.monotonic() - started, exit_code=result.returncode,
                           program_started=True, error_class="" if result.returncode == 0 else "PROGRAM_FAILURE")
                return result
            except ExecutionFailure as exc:
                self.event("execution.failed", duration=time.monotonic() - started,
                           error_class=exc.error_class, program_started=exc.program_started)
                raise

    def cleanup(self):
        try:
            self._cleanup()
        except (OSError, ValueError) as exc:
            self.event("cleanup.failed", error_class=type(exc).__name__)

    def _cleanup(self):
        if not self.directory.exists():
            return
        with self.locked():
            if not self.directory.exists():
                return
            self.event("cleanup.started")
            try:
                # Only a direct owned child; reject symlink/junction redirection.
                if self.directory.resolve().parent != self.root:
                    raise ValueError("Runtime environment escaped its root")
                shutil.rmtree(self.directory)
                self.event("cleanup.completed")
            except (OSError, ValueError) as exc:
                self.event("cleanup.failed", error_class=type(exc).__name__)


PythonExecutionManager = PythonVenvBackend


def cleanup_runtime_environments(root, status_for, emit=None, *, orphan_ttl=86400):
    """Never expire an active orchestration, even when older than the orphan TTL."""
    root = Path(root)
    if not root.exists():
        return
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        status = status_for(directory.name)
        if status in TERMINAL or (status is None and time.time() - directory.stat().st_mtime > orphan_ttl):
            backend = PythonVenvBackend(root, directory.name, emit)
            backend.event("stale_cleanup", reason="terminal" if status else "orphan")
            backend.cleanup()
