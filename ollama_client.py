"""Small standard-library client for Ollama's local HTTP API."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_TIMEOUT_SECONDS = 300


class OllamaError(RuntimeError):
    """Raised when the local Ollama API cannot serve a request."""


def base_url(override: str | None = None) -> str:
    value = override or os.environ.get("OLLAMA_HOST") or DEFAULT_BASE_URL
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value


def _request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise OllamaError("Ollama returned HTTP {}: {}".format(exc.code, details)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OllamaError(
            "Could not reach Ollama at {}. Start Ollama and check its local API. ({})".format(
                url.split("/api/", 1)[0], exc
            )
        ) from exc

    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OllamaError("Ollama returned invalid JSON: {}".format(exc)) from exc
    if not isinstance(result, dict):
        raise OllamaError("Ollama returned an unexpected response shape.")
    return result


def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    host: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Make one non-streaming chat call and return its message plus usage metrics."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"temperature": temperature},
        "keep_alive": "5m",
    }
    result = _request_json(
        "POST", base_url(host) + "/api/chat", payload, timeout=timeout
    )
    if not isinstance(result.get("message"), dict):
        raise OllamaError("Ollama chat response did not contain a message.")
    return result


def list_models(*, host: str | None = None) -> list[dict[str, Any]]:
    """Return models available in this Ollama installation."""
    result = _request_json("GET", base_url(host) + "/api/tags", timeout=10)
    models = result.get("models", [])
    if not isinstance(models, list):
        raise OllamaError("Ollama model list had an unexpected response shape.")
    return [model for model in models if isinstance(model, dict)]
