"""Single immutable startup configuration, shared with spawned workers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import threading
import math
import subprocess
from pathlib import Path
from typing import Mapping


DEFAULT_DEBUG_MAX_CHARS = 4_000_000
MAX_DEBUG_CHARS = 4_000_000
PREVIOUS_ORCHESTRATION_TIMEOUT_SECONDS = 900
DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS = PREVIOUS_ORCHESTRATION_TIMEOUT_SECONDS * 2
LOCAL_SETTINGS_FILE = ".freya-local.json"


def orchestration_timeout(value) -> float:
    if isinstance(value, bool):
        raise ValueError("Orchestration timeout must be a positive finite number.")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Orchestration timeout must be a positive finite number.")
    return seconds


def parse_bool(value: str | None) -> bool:
    """Unknown values fail closed, like empty/unset and explicit false values."""
    return isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    debug_llm_prompts: bool = False
    debug_prompt_max_chars: int = DEFAULT_DEBUG_MAX_CHARS
    debug_llm_prompts_source: str = "default"
    orchestration_timeout_seconds: float = DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS
    orchestration_timeout_source: str = "default"
    repo_root: str = ""
    process_working_directory: str = ""
    git_commit: str | None = None
    entrypoint: str = "python -m control_center"
    python_execution_backend: str = "venv"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Settings:
        environment = os.environ if environment is None else environment
        flag = environment.get("FREYA_DEBUG_LLM_PROMPTS")
        try:
            limit = int(environment.get("FREYA_DEBUG_PROMPT_MAX_CHARS", str(DEFAULT_DEBUG_MAX_CHARS)))
        except (TypeError, ValueError):
            limit = DEFAULT_DEBUG_MAX_CHARS
        timeout = environment.get("FREYA_ORCHESTRATION_TIMEOUT_SECONDS")
        backend = environment.get("FREYA_PYTHON_EXECUTION_BACKEND", "venv").strip().lower()
        if backend != "venv":
            raise ValueError("FREYA_PYTHON_EXECUTION_BACKEND currently supports only venv.")
        return cls(parse_bool(flag), max(1000, min(limit, MAX_DEBUG_CHARS)),
                   "environment" if flag is not None else "default",
                   orchestration_timeout(timeout) if timeout is not None else DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
                   "environment" if timeout is not None else "default", python_execution_backend=backend)

    @classmethod
    def from_sources(cls, repo_root: Path, environment: Mapping[str, str] | None = None) -> Settings:
        """Environment overrides ignored local development settings, then defaults.

        Only recognized settings are read. No dotenv, credentials, or DB overrides.
        """
        environment = dict(os.environ if environment is None else environment)
        local_path = repo_root / LOCAL_SETTINGS_FILE
        local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.is_file() else {}
        names = {"FREYA_DEBUG_LLM_PROMPTS", "FREYA_DEBUG_PROMPT_MAX_CHARS",
                 "FREYA_ORCHESTRATION_TIMEOUT_SECONDS", "FREYA_PYTHON_EXECUTION_BACKEND"}
        if not isinstance(local, dict) or set(local) - names:
            raise ValueError("Local Freya configuration contains unknown settings.")
        effective = {**{key: str(value).lower() for key, value in local.items()}, **environment}
        settings = cls.from_environment(effective)
        from dataclasses import replace
        try:
            commit = subprocess.run(["git", "-c", f"safe.directory={repo_root.as_posix()}",
                                     "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
                                    text=True, timeout=2, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = None
        def source(name):
            return ("environment" if name in environment else "local_file:" + str(local_path)
                    if name in local else "default")
        return replace(settings, debug_llm_prompts_source=source("FREYA_DEBUG_LLM_PROMPTS"),
                       orchestration_timeout_source=source("FREYA_ORCHESTRATION_TIMEOUT_SECONDS"),
                       repo_root=str(repo_root.resolve()), process_working_directory=str(Path.cwd()),
                       git_commit=commit)

    def diagnostic_event(self) -> dict[str, object]:
        return {"event_type": "freya.runtime.configuration", "debug_llm_prompts": self.debug_llm_prompts,
                "python_execution_backend": self.python_execution_backend,
                "configuration_source": self.debug_llm_prompts_source,
                "repo_root": self.repo_root, "process_working_directory": self.process_working_directory,
                "git_commit": self.git_commit, "entrypoint": self.entrypoint,
                "orchestration_timeout_seconds": self.orchestration_timeout_seconds,
                "orchestration_timeout_source": self.orchestration_timeout_source,
                "source": self.debug_llm_prompts_source, "llm_provider": "ollama",
                "debug_prompt_max_chars": self.debug_prompt_max_chars}


_settings: Settings | None = None
_lock = threading.RLock()


def initialize_settings(settings: Settings | None = None) -> Settings:
    """Called at server startup, or with the parent's snapshot in spawned workers."""
    global _settings
    with _lock:
        _settings = settings if settings is not None else Settings.from_environment()
        return _settings


def get_settings() -> Settings:
    """Standalone library users initialize lazily; later environment changes have no effect."""
    with _lock:
        return _settings if _settings is not None else initialize_settings()


def report_startup_config(settings: Settings) -> None:
    print(json.dumps(settings.diagnostic_event(), sort_keys=True), flush=True)
    print("Debug LLM prompts: " + ("ENABLED" if settings.debug_llm_prompts else "DISABLED"), flush=True)
