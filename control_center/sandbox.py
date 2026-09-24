"""Ephemeral Docker execution for workspace commands.

The host only copies workspace bytes into a disposable directory. Model supplied
code runs in the container, never against the persistent host workspace.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


SANDBOX_IMAGE = "freya-sandbox:py311"
MAX_SNAPSHOT_FILES = 10000
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
SKIP_DIRECTORIES = {".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


class SandboxUnavailable(RuntimeError):
    """The required Docker execution boundary could not be established."""


def _inside(path: Path, workspace: Path) -> bool:
    resolved = path.resolve()
    return resolved == workspace or workspace in resolved.parents


def _copy_workspace(workspace: Path, destination: Path) -> None:
    count = size = 0
    for current, dirs, files in os.walk(workspace, followlinks=False):
        source_dir = Path(current)
        if not _inside(source_dir, workspace):
            raise SandboxUnavailable("workspace traversal escaped its root")
        relative = source_dir.relative_to(workspace)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o777)
        kept_dirs = []
        for name in dirs:
            child = source_dir / name
            if name in SKIP_DIRECTORIES or child.is_symlink():
                continue
            if not _inside(child, workspace):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            source = source_dir / name
            if source.is_symlink() or not source.is_file() or not _inside(source, workspace):
                continue
            stat = source.stat()
            count += 1
            size += stat.st_size
            if count > MAX_SNAPSHOT_FILES or size > MAX_SNAPSHOT_BYTES:
                raise SandboxUnavailable("workspace snapshot exceeds the sandbox limit")
            target = target_dir / name
            shutil.copyfile(source, target)
            target.chmod(0o666)


def run_in_sandbox(workspace: Path, argv: list[str], timeout_seconds: int,
                   stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a previously allowlisted argv in Docker with no host environment."""
    docker = shutil.which("docker")
    if docker is None:
        raise SandboxUnavailable("Docker CLI is unavailable")
    with tempfile.TemporaryDirectory(prefix="freya-sandbox-") as temporary:
        temporary_root = Path(temporary)
        snapshot = temporary_root / "workspace"
        snapshot.mkdir()
        snapshot.chmod(0o777)
        _copy_workspace(workspace, snapshot)
        docker_config = temporary_root / "docker-config"
        docker_config.mkdir()
        cidfile = temporary_root / "container.cid"
        host_env = {"DOCKER_CONFIG": str(docker_config)}
        if os.name == "nt":
            host_env["DOCKER_HOST"] = "npipe:////./pipe/dockerDesktopLinuxEngine"
            host_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        else:
            host_env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        command = [
            docker, "run", "--rm", "--interactive", "--pull=never",
            "--cidfile", str(cidfile), "--stop-timeout=1",
            "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--memory=512m", "--memory-swap=512m", "--pids-limit=64", "--cpus=1",
            "--user=65534:65534", "--workdir=/workspace",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount", f"type=bind,source={snapshot},target=/workspace",
        ]
        for name, value in {
            "HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "PYTHONPATH": "/workspace",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        }.items():
            command.extend(["--env", f"{name}={value}"])
        command.extend([SANDBOX_IMAGE, *argv])
        try:
            result = subprocess.run(
                command, input=stdin or "", capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_seconds + 5,
                shell=False, env=host_env,
            )
        except subprocess.TimeoutExpired as exc:
            if cidfile.exists():
                cid = cidfile.read_text(encoding="ascii").strip()
                if cid:
                    try:
                        subprocess.run([docker, "rm", "--force", cid], env=host_env,
                                       capture_output=True, timeout=5, shell=False)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            raise SandboxUnavailable(f"Docker execution exceeded {timeout_seconds} seconds") from exc
        except OSError as exc:
            raise SandboxUnavailable(f"Docker execution unavailable: {exc}") from exc
        if result.returncode in {125, 126, 127}:
            raise SandboxUnavailable((result.stderr or "Docker sandbox could not start").strip()[:1000])
        return result
