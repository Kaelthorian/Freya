"""Process-start observability settings; never load credentials or dotenv files."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import threading
from typing import Mapping


DEFAULT_DEBUG_MAX_CHARS = 4_000_000
MAX_DEBUG_CHARS = 4_000_000


def parse_bool(value: str | None) -> bool:
    """Unknown values fail closed, like empty/unset and explicit false values."""
    return isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    debug_llm_prompts: bool = False
    debug_prompt_max_chars: int = DEFAULT_DEBUG_MAX_CHARS
    debug_llm_prompts_source: str = "default"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Settings:
        environment = os.environ if environment is None else environment
        flag = environment.get("FREYA_DEBUG_LLM_PROMPTS")
        try:
            limit = int(environment.get("FREYA_DEBUG_PROMPT_MAX_CHARS", str(DEFAULT_DEBUG_MAX_CHARS)))
        except (TypeError, ValueError):
            limit = DEFAULT_DEBUG_MAX_CHARS
        return cls(parse_bool(flag), max(1000, min(limit, MAX_DEBUG_CHARS)),
                   "environment" if flag is not None else "default")

    def diagnostic_event(self) -> dict[str, object]:
        return {"event_type": "freya.config.loaded", "debug_llm_prompts": self.debug_llm_prompts,
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
