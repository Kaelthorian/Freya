"""Defense-in-depth redaction at persistence/API boundaries; no secret storage."""

from __future__ import annotations

import os
import re
import threading
from typing import Any

REDACTED = "[REDACTED]"
_secrets: set[str] = set()
_lock = threading.RLock()
_key = re.compile(r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|credential|private[_-]?key|cookie|session[_-]?(?:id|token))", re.I)
_assignment = re.compile(r'''(?i)((?:[\w.-]{0,128}(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|credential|cookie|session[_-]?(?:id|token))[\w.-]{0,128}|token)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|(?:Bearer\s+)?[^\s,;}]+)''')
_bearer = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_private_key = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S)
_think = re.compile(r"<(?:think|thinking|analysis)(?:\s[^>]*)?>.*?(?:</(?:think|thinking|analysis)>|$)", re.I | re.S)


def register_secret(value: str | None) -> None:
    if value:
        with _lock:
            _secrets.add(value)


def strip_thinking(text: str) -> str:
    return _think.sub("", text)


def sanitize(value: Any, *, max_string_chars: int = 24000) -> Any:
    """Keep metric names/counts and env references, remove credentials and thinking."""
    if isinstance(value, dict):
        if value.get("event_type") in {"llm.call", "llm.validation"} and value.get("debug_prompts_enabled") is True:
            from .settings import MAX_DEBUG_CHARS
            max_string_chars = MAX_DEBUG_CHARS
        clean = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in {"thinking", "reasoning", "analysis"}:
                continue
            if name != "secret_env" and (_key.search(name) or name.lower() in {"token", "secrets"}):
                clean[name] = REDACTED
            else:
                clean[name] = sanitize(item, max_string_chars=max_string_chars)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize(item, max_string_chars=max_string_chars) for item in value]
    if not isinstance(value, str):
        return value
    text = strip_thinking(value)
    with _lock:
        known = tuple(_secrets)
    # Redact configured credentials even when a model repeats a bare value.
    known += tuple(v for k, v in os.environ.items() if k.startswith("ACC_SECRET_") and v)
    for secret in sorted(known, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _private_key.sub(REDACTED, text)
    text = _bearer.sub("Bearer " + REDACTED, text)
    text = re.sub(r"(?i)((?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", REDACTED, text)
    if any(marker in text.casefold() for marker in (
            "password", "passwd", "secret", "key", "token", "authorization", "credential",
            "cookie", "session")):
        text = _assignment.sub(lambda match: match.group(1) + REDACTED, text)
    return text[:max_string_chars]
